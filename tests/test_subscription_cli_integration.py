"""Claude Code 2.1.296 / Codex 0.160.0 integration contracts (subscription routes).

The event shapes below were captured from the installed CLIs run offline with
an empty home and the network blocked (not logged in, API outage), and from
codex 0.160.0 against a loopback provider that answers with the ChatGPT
backend's usage-limit error.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import auto_pipeline, cgm_max, claude_login, jobs, lean_verify, proof_graph
from agent_monitor.cli_events import CLIEventParser
from agent_monitor.runners import analyze_runner, claude_backend, codex_backend
from agent_monitor.runners import math_harness_runner as harness


def _synthetic_failure(text: str, category: str, *, status: int | None = None) -> tuple[dict, ...]:
    """Claude Code 2.1.296: an API error is a synthetic assistant message
    (model ``<synthetic>``, ``error`` category) plus an is_error result whose
    subtype still says ``success``."""
    return (
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "tools": []},
        {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "role": "assistant",
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "content": [{"type": "text", "text": text}],
            },
            "error": category,
            "is_api_error_message": True,
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": text,
            "terminal_reason": "api_error",
            "api_error_status": status,
            "num_turns": 1,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "modelUsage": {},
        },
    )


def _success(text: str = "Completed.") -> tuple[dict, ...]:
    return (
        {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
        {
            "type": "assistant",
            "message": {
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "modelUsage": {"claude-opus-5-5": {}},
        },
    )


CLAUDE_2_1_296_AUTH_TEXTS = (
    "Not logged in · Please run /login",
    "Authentication required · Sign in again to continue",
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "OAuth refresh token is no longer valid; run /login to re-authenticate",
    "Failed to authenticate: OAuth token revoked. Please log in again or contact your administrator.",
    "Login expired · Please run /login",
)
# Claude Code 2.1.301 files every plan and usage-credit refusal under
# ``rate_limit``, the same category as a transient 429.
CLAUDE_2_1_301_LIMIT_TEXTS = (
    "You've reached your Fable limit. Run /usage-credits to keep using Fable or /model to switch models.",
    "You're out of usage credits · resets 5pm",
    "You're out of usage credits. /model to switch models.",
    "You're out of extra usage · resets 5pm",
    "Your org is out of usage · add funds to continue",
    "Your org is out of usage · contact your admin",
    "Your seat type doesn't include usage credits",
    "Your seat type doesn't include extra usage",
    "Your usage allocation has been disabled by your admin",
    "Your group's usage limit is set to $0",
    "Fable 5 requires usage credits. Run /usage-credits to turn them on or /model to switch models.",
    "Opus 5.5 requires usage credits. Run /usage-credits to turn them on or /model to switch models.",
    "Your organization is out of usage credits. Contact your admin to add more.",
    "You’re out of usage credits · resets 5pm",  # typographic apostrophe
    "API Error: Usage credits required for 1M context · run /usage-credits to turn them on "
    "(they take effect in a new session), or /model to switch to standard context",
)
# The same category, but a capacity problem that the next call may not hit.
CLAUDE_TRANSIENT_RATE_LIMIT_TEXTS = (
    "API Error: Request rejected (429) · this may be a temporary capacity issue.",
    "Opus is experiencing high load, please use /model to switch to Sonnet",
)
REFRESH_LOCK = (
    "Failed to refresh OAuth token: another Claude Code process is refreshing it or "
    "exited mid-refresh. This is usually transient; retry in a minute, and if it "
    "persists close other Claude Code processes or sign in again"
)
# codex exec --json 0.160.0 against the ChatGPT usage-limit answer: exit 1 and
# an empty stderr; the first error item is only the landlock notice.
CODEX_USAGE_LIMIT_STDOUT = "\n".join(
    json.dumps(event)
    for event in (
        {"type": "thread.started", "thread_id": "01a12cde-45e5-77e3-a161-0bf23033d7a4"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": (
            "`[features].use_legacy_landlock` is deprecated and will be removed soon. "
            "(Remove this setting to stop opting into the legacy Linux sandbox behavior.)")}},
        {"type": "turn.started"},
        {"type": "error", "message": (
            "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at 10:28 PM.")},
        {"type": "turn.failed", "error": {"message": (
            "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at 10:28 PM.")}},
    )
)


def _claude_env(config: str) -> dict[str, str]:
    return {
        "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
        "CLAUDE_CONFIG_DIR": config,
        "PATH": os.environ.get("PATH", ""),
    }


def _fake_claude_cli(bin_dir: Path, events: tuple[dict, ...], *, exit_code: int = 0, proof: str = "") -> Path:
    """A stand-in ``claude`` that appends its argv and stdin prompt to
    ``calls.jsonl`` beside it, writes ``proof`` (if any) to ./proof.md, prints
    ``events`` as stream-json and exits with ``exit_code``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "claude"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        prompt = sys.stdin.read()
        with open({str(bin_dir / "calls.jsonl")!r}, "a", encoding="utf-8") as calls:
            calls.write(json.dumps({{"argv": sys.argv[1:], "stdin": prompt}}) + "\\n")
        if {proof!r}:
            with open("proof.md", "w", encoding="utf-8") as artifact:
                artifact.write({proof!r})
        for event in {list(events)!r}:
            print(json.dumps(event), flush=True)
        sys.exit({exit_code})
        """), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def _fake_claude_calls(fake: Path) -> list[dict]:
    calls = fake.with_name("calls.jsonl")
    return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []


class ClaudeCode2_1_296FailureTests(unittest.TestCase):
    def test_every_real_auth_failure_marks_the_account_for_reconnection(self) -> None:
        for text in CLAUDE_2_1_296_AUTH_TEXTS:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as config:
                failed = claude_backend._ProcessResult(1, "", _synthetic_failure(
                    text, "authentication_failed", status=401))
                with (
                    patch.object(claude_backend, "_run_stream", side_effect=[failed, failed]) as run,
                    patch.object(claude_backend, "_claude_binary", return_value="/bin/claude"),
                    patch.object(claude_backend.time, "sleep"),
                    patch.object(claude_login, "mark_reauth_required") as mark,
                    self.assertRaises(claude_backend.ClaudeAuthenticationError) as raised,
                ):
                    claude_backend.claude_exec("system", "user", model="sonnet",
                                               source_env=_claude_env(config))
                message = str(raised.exception)
                self.assertIn("subscription authentication failed", message)
                self.assertIn("reconnect Claude Code", message)
                self.assertIn(text.split("·")[0].strip().rstrip("."), message)
                self.assertEqual(raised.exception.kind, "auth")
                self.assertEqual(run.call_count, 2)  # one pre-inference replay
                mark.assert_called_once()

    def test_refresh_lock_is_replayed_and_never_marks_reauthentication(self) -> None:
        locked = claude_backend._ProcessResult(1, "", _synthetic_failure(REFRESH_LOCK, "server_error"))
        with tempfile.TemporaryDirectory() as config, (
            patch.object(claude_backend, "_run_stream",
                         side_effect=[locked, claude_backend._ProcessResult(0, "", _success())])
        ) as run, patch.object(claude_backend, "_claude_binary", return_value="/bin/claude"), patch.object(
            claude_backend.time, "sleep"
        ) as sleep, patch.object(claude_login, "mark_reauth_required") as mark:
            result = claude_backend.claude_exec("system", "user", model="opus",
                                                source_env=_claude_env(config))
        self.assertEqual(result.text, "Completed.")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(claude_backend._REFRESH_LOCK_RETRY_SECONDS)
        mark.assert_not_called()

    def test_usage_limit_is_a_typed_clear_failure(self) -> None:
        process = claude_backend._ProcessResult(1, "", _synthetic_failure(
            "You've hit your session limit · resets 11pm (UTC)", "rate_limit", status=429))
        with self.assertRaises(claude_backend.ClaudeBackendError) as raised:
            claude_backend._terminal_result(process)
        self.assertNotIsInstance(raised.exception, claude_backend.ClaudeAuthenticationError)
        self.assertEqual(raised.exception.kind, "usage_limit")
        self.assertIn("You've hit your session limit · resets 11pm (UTC)", str(raised.exception))

    def test_a_transient_429_is_not_a_usage_limit(self) -> None:
        # 2.1.296 files every 429 under ``rate_limit``, a capacity hiccup included.
        process = claude_backend._ProcessResult(1, "", _synthetic_failure(
            "API Error: Request rejected (429) · this may be a temporary capacity issue.", "rate_limit", status=429))
        with self.assertRaises(claude_backend.ClaudeBackendError) as raised:
            claude_backend._terminal_result(process)
        self.assertIsNone(raised.exception.kind)
        self.assertIn("temporary capacity issue", str(raised.exception))

    def test_plan_and_usage_credit_limits_are_usage_limits(self) -> None:
        for text in CLAUDE_2_1_301_LIMIT_TEXTS:
            with self.subTest(text=text):
                process = claude_backend._ProcessResult(1, "", _synthetic_failure(text, "rate_limit", status=429))
                with self.assertRaises(claude_backend.ClaudeBackendError) as raised:
                    claude_backend._terminal_result(process)
                self.assertNotIsInstance(raised.exception, claude_backend.ClaudeAuthenticationError)
                self.assertEqual(raised.exception.kind, "usage_limit")
                self.assertIn(text, str(raised.exception))
        for text in CLAUDE_TRANSIENT_RATE_LIMIT_TEXTS:
            with self.subTest(text=text):
                process = claude_backend._ProcessResult(1, "", _synthetic_failure(text, "rate_limit", status=429))
                with self.assertRaises(claude_backend.ClaudeBackendError) as raised:
                    claude_backend._terminal_result(process)
                self.assertIsNone(raised.exception.kind)

    def test_the_typed_limit_on_the_result_is_a_usage_limit_whatever_its_wording(self) -> None:
        # 2.1.301 copies the refusal's typed cause onto the result; the text
        # is only the fallback and may change.
        for typed in ("usage_limit_reached", "model_requires_usage_credits", "long_context_credits_required"):
            with self.subTest(api_error=typed):
                events = _synthetic_failure("Weekly allowance used · back Monday", "rate_limit", status=429)
                events = (*events[:-1], {**events[-1], "api_error": typed})
                with self.assertRaises(claude_backend.ClaudeBackendError) as raised:
                    claude_backend._terminal_result(claude_backend._ProcessResult(1, "", events))
                self.assertEqual(raised.exception.kind, "usage_limit")
                self.assertIn("Weekly allowance used", str(raised.exception))

    def test_max_turns_reports_the_cli_reason(self) -> None:
        process = claude_backend._ProcessResult(1, "", (
            {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
            {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 6,
             "errors": ["Reached maximum number of turns (6)"]},
        ))
        with self.assertRaises(claude_backend.ClaudeBackendError) as raised:
            claude_backend._terminal_result(process)
        self.assertIn("Reached maximum number of turns (6)", str(raised.exception))

    def test_synthetic_error_is_never_forwarded_as_model_output(self) -> None:
        projected: list[dict] = []
        for event in _synthetic_failure("API Error: Connection refused", "server_error"):
            claude_backend.forward_as_codex_event(event, projected.append)
        # Shown as a (non-terminal) error, never as an agent message.
        self.assertEqual(
            projected[1:],
            [{"type": "item.completed", "item": {"type": "error", "message": "API Error: Connection refused"}}],
        )
        projected.clear()
        for event in _synthetic_failure("Not logged in · Please run /login", "authentication_failed"):
            claude_backend.forward_as_codex_event(event, projected.append)
        # An auth failure is reported once, by the typed exception.
        self.assertEqual([event["type"] for event in projected], ["thread.started"])

    def test_a_synthetic_message_is_not_model_telemetry(self) -> None:
        haiku = "claude-haiku-4-5-20251001"
        events = (
            {"type": "assistant", "message": {"model": haiku, "content": [{"type": "text", "text": "ok"}]}},
            {"type": "assistant", "is_api_error_message": True, "error": "unknown",
             "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "note"}]}},
        )
        result = claude_backend.ClaudeResult("ok", {}, haiku, None, events, {"type": "result"})
        # An exact-model run is not failed as a "model mismatch" by a
        # CLI-generated message.
        claude_backend._require_requested_model(result, "claude-haiku-4-5")

    def test_native_stream_shows_synthetic_errors_as_errors(self) -> None:
        parser = CLIEventParser("claude", source_engine="claude")
        for event in _synthetic_failure("Not logged in · Please run /login", "authentication_failed"):
            parser.feed(json.dumps(event))
        self.assertEqual(parser.final_message(), "")
        self.assertIn("error · Not logged in · Please run /login", parser.output())
        self.assertNotIn("assistant · Not logged in", parser.output())
        self.assertEqual(parser.terminal_error, "Not logged in · Please run /login")
        parser = CLIEventParser("claude", source_engine="claude")
        parser.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                                "errors": ["Reached maximum number of turns (40)"]}))
        self.assertEqual(parser.terminal_error, "Reached maximum number of turns (40)")


class ClaudePromptOnStdinTests(unittest.TestCase):
    """One argv string is limited to 128 KiB on Linux; prompts go on stdin."""

    def test_large_multibyte_prompt_reaches_the_cli_on_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as config:
            record = Path(td) / "record.json"
            fake = Path(td) / "claude"
            fake.write_text(textwrap.dedent(f"""\
                #!{sys.executable}
                import json, sys
                prompt = sys.stdin.read()
                json.dump({{"argv": sys.argv[1:], "stdin": prompt}}, open({str(record)!r}, "w"))
                for event in (
                    {{"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"}},
                    {{"type": "assistant", "message": {{"model": "claude-sonnet-5-5",
                      "content": [{{"type": "text", "text": "ok"}}], "usage": {{"output_tokens": 1}}}}}},
                    {{"type": "result", "subtype": "success", "is_error": False, "result": "ok",
                      "usage": {{"input_tokens": 1, "output_tokens": 1}}}},
                ):
                    print(json.dumps(event), flush=True)
                """), encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            user = "证明：" * 60_000  # 540,000 bytes of UTF-8, far above 128 KiB
            env = {**_claude_env(config), "PATH": f"{td}:{os.environ.get('PATH', '')}"}
            with patch.object(claude_backend, "_claude_binary", return_value=str(fake)):
                result = claude_backend.claude_exec("be brief", user, model="sonnet", source_env=env)
            seen = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(result.text, "ok")
        self.assertEqual(seen["stdin"], "USER REQUEST:\n" + user)
        self.assertEqual(seen["argv"][0], "-p")
        self.assertFalse(any("证明" in arg for arg in seen["argv"]))
        self.assertIn("be brief", seen["argv"])  # the system prompt stays an option

    def test_a_long_problem_reaches_claude_code_through_the_native_engine(self) -> None:
        # The console starts claude_runner with the registry's own command;
        # that hop must not put the prompt in argv either.
        problem = "设 n 为偶数，证明：" * 9_000  # 90,000 characters, 270 KB of UTF-8
        proof = "# Claim\n\nThe statement holds.\n\n## Proof\n\n" + "Each step follows. " * 30 + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            workspace = root / "workspace"
            workspace.mkdir()
            fake = _fake_claude_cli(root / "bin", _success("Done."), proof=proof)
            extra_env = {**_claude_env(str(root / "config")),
                         "PATH": os.pathsep.join([str(fake.parent), "/usr/bin", "/bin"])}
            # HOME keeps the registry's ~/.local/bin (a real install) off PATH.
            with patch.dict(os.environ, {"HOME": str(root)}), patch(
                "agent_monitor.engines_registry.which_tool", return_value=str(fake)
            ), patch("agent_monitor.agent_config.persona_preamble", return_value=""), patch.object(jobs, "_write_run"):
                os.environ.pop("CLAUDE_CMD", None)
                run = jobs._run_cli_engine(run_id="claude_long", engine="claude", problem_id="p",
                                           problem_text=problem, started=time.time(),
                                           workspace=workspace, extra_env=extra_env)
            calls = _fake_claude_calls(fake)
            leftovers = sorted(path.name for path in workspace.iterdir())
        self.assertEqual(run["status"], "finished", run.get("error"))
        self.assertEqual(len(calls), 1)
        self.assertIn(problem, calls[0]["stdin"])
        self.assertEqual(leftovers, ["proof.md"])  # the prompt file is gone

    def test_a_long_problem_reaches_claude_code_through_plain_and_meta_harness(self) -> None:
        # Both also run on the linked Claude Code account; the console starts
        # them with the registry's own command too.
        problem = "设 n 为偶数，证明：" * 9_000  # 90,000 characters, 270 KB of UTF-8
        # The evaluator's score ends Meta-Harness after its first round.
        answer = "SCORE: 9\n\n# Claim\n\nThe statement holds.\n\n## Proof\n\n" + "Each step follows. " * 30 + "\n"
        for engine in ("plain", "metaharness"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "config").mkdir()
                workspace = root / "workspace"
                workspace.mkdir()
                fake = _fake_claude_cli(root / "bin", _success(answer))
                extra_env = {**_claude_env(str(root / "config")),
                             "PATH": os.pathsep.join([str(fake.parent), "/usr/bin", "/bin"])}
                # HOME keeps the registry's ~/.local/bin (a real install) off PATH.
                with patch.dict(os.environ, {"HOME": str(root)}), patch(
                    "agent_monitor.agent_config.persona_preamble", return_value=""
                ), patch.object(jobs, "_write_run"):
                    os.environ.pop("PLAIN_CMD", None)
                    os.environ.pop("METAHARNESS_CMD", None)
                    run = jobs._run_cli_engine(run_id=f"{engine}_long", engine=engine, problem_id="p",
                                               problem_text=problem, started=time.time(),
                                               workspace=workspace, extra_env=extra_env)
                calls = _fake_claude_calls(fake)
                leftovers = sorted(path.name for path in workspace.iterdir())
                self.assertEqual(run["status"], "finished", run.get("error"))
                self.assertIn(problem, calls[0]["stdin"])
                self.assertEqual(leftovers, ["proof.md"])  # the prompt file is gone


class CodexFailureReportingTests(unittest.TestCase):
    def test_usage_limit_is_reported_cleanly_and_typed(self) -> None:
        def fake_run(command, **_kwargs):
            if command[1:] == ["--version"]:
                return codex_backend.subprocess.CompletedProcess(command, 0, "codex-cli 0.160.0\n", "")
            if command[1:] == ["debug", "models", "--bundled"]:
                return codex_backend.subprocess.CompletedProcess(
                    command, 0, json.dumps({"models": [{"slug": "gpt-6-astra"}]}), "")
            return codex_backend.subprocess.CompletedProcess(command, 1, CODEX_USAGE_LIMIT_STDOUT, "")

        with tempfile.TemporaryDirectory() as home, patch.object(
            codex_backend.shutil, "which", return_value="/bin/true"
        ), patch.object(codex_backend, "_run_codex_process", side_effect=fake_run):
            with self.assertRaises(codex_backend.CodexBackendError) as raised:
                codex_backend.codex_exec("system", "user", model="gpt-6-astra",
                                         source_env={"CODEX_HOME": home, "PATH": "/usr/bin:/bin"})
        message = str(raised.exception)
        self.assertEqual(raised.exception.kind, "usage_limit")
        self.assertTrue(message.startswith("codex exec failed: You’ve hit your usage limit."), message)
        self.assertNotIn("use_legacy_landlock", message)
        self.assertNotIn('{"type"', message)

    def test_usage_keeps_cached_and_reasoning_tokens(self) -> None:
        def fake_run(command, **_kwargs):
            if command[1:] == ["--version"]:
                return codex_backend.subprocess.CompletedProcess(command, 0, "codex-cli 0.160.0\n", "")
            if command[1:] == ["debug", "models", "--bundled"]:
                return codex_backend.subprocess.CompletedProcess(
                    command, 0, json.dumps({"models": [{"slug": "gpt-6-astra"}]}), "")
            Path(command[command.index("--output-last-message") + 1]).write_text("answer")
            event = {"type": "turn.completed", "usage": {"input_tokens": 20000, "cached_input_tokens": 18000,
                                                          "output_tokens": 1500, "reasoning_output_tokens": 700}}
            return codex_backend.subprocess.CompletedProcess(command, 0, json.dumps(event), "")

        with tempfile.TemporaryDirectory() as home, patch.object(
            codex_backend.shutil, "which", return_value="/bin/true"
        ), patch.object(codex_backend, "_run_codex_process", side_effect=fake_run):
            result = codex_backend.codex_exec("system", "user", model="gpt-6-astra",
                                              source_env={"CODEX_HOME": home, "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.usage, {"input_tokens": 20000, "cached_input_tokens": 18000,
                                        "output_tokens": 1500, "reasoning_output_tokens": 700})


class BackendFailureNeverEndsTheRunTests(unittest.TestCase):
    def test_failed_codex_call_is_a_non_terminal_error_item(self) -> None:
        emitted: list[dict] = []
        for line in CODEX_USAGE_LIMIT_STDOUT.splitlines():
            harness._emit_backend_event(json.loads(line), out=emitted.append)
        self.assertFalse(any(event["type"] == "turn.failed" for event in emitted))
        parser = CLIEventParser("codex", source_engine="math_harness")
        for event in emitted:
            parser.feed(json.dumps(event))
        # The pipeline went on (a curator failure only degrades the run) and
        # published its proof; the run must not be marked failed for it.
        parser.feed(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                                   "text": "Math Harness completed"}}))
        self.assertIsNone(parser.terminal_error)
        self.assertIn("You’ve hit your usage limit", parser.output())


class AnalyzeSubscriptionFailureTests(unittest.TestCase):
    def _run(self, cause: Exception):
        run = analyze_runner.AnalysisRun.__new__(analyze_runner.AnalysisRun)
        run.permanent_failure = False
        run.provider_failure = ""
        run.degraded = []
        run.ctx = SimpleNamespace(run_processed=lambda: True)
        try:
            raise harness.ProviderCallError("check-1 provider call failed") from cause
        except harness.ProviderCallError as error:
            return run, run._note_failure(error)

    def test_usage_limit_stops_the_checks_and_says_why(self) -> None:
        run, message = self._run(claude_backend.ClaudeBackendError(
            "Claude Code failed: You've hit your session limit · resets 11pm (UTC)", kind="usage_limit"))
        self.assertTrue(run.permanent_failure)
        self.assertIn("You've hit your session limit", message)

    def test_a_spent_model_allowance_stops_the_checks_and_says_why(self) -> None:
        process = claude_backend._ProcessResult(1, "", _synthetic_failure(
            "You've reached your Fable limit. Run /usage-credits to keep using Fable or /model to switch models.",
            "rate_limit", status=429))
        with self.assertRaises(claude_backend.ClaudeBackendError) as raised:
            claude_backend._terminal_result(process)
        run, message = self._run(raised.exception)
        self.assertTrue(run.permanent_failure)
        self.assertIn("You've reached your Fable limit", message)

    def test_reconnect_required_stops_the_checks(self) -> None:
        run, message = self._run(codex_backend.CodexBackendError(
            "codex exec failed: Your access token could not be refreshed. Please log out and sign in again.",
            kind="auth"))
        self.assertTrue(run.permanent_failure)
        self.assertIn("sign in again", message)

    def test_timeout_is_explained_but_not_permanent(self) -> None:
        run, message = self._run(claude_backend.ClaudeBackendError(
            "Claude Code timed out after 600s", kind="timeout"))
        self.assertFalse(run.permanent_failure)
        self.assertIn("timed out after 600s", message)


class AutoPipelineWaitsForLongRunsTests(unittest.TestCase):
    def test_a_max_run_gets_its_derived_views_after_three_hours(self) -> None:
        waits: list[float] = []
        max_record = {"run_id": "max-run", "engine": "math_harness", "model": "max",
                      "auth_route": cgm_max.ROUTE, "status": "finished",
                      "route_plan": {"worker": {"model": "gpt-6-astra"}}}

        def wait_for_source(_run_id, _workspace, *, timeout):
            waits.append(timeout)
            return "proof.md:final", {"status": "finished", "engine": "math_harness"}

        with tempfile.TemporaryDirectory() as raw, patch.dict(
            os.environ, {"AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT": "7200"}
        ), patch.object(jobs, "_load_run_record", return_value=max_record), patch.object(
            auto_pipeline, "_wait_for_source", side_effect=wait_for_source
        ), patch.object(auto_pipeline, "_source_signature", return_value="proof.md:final"), patch.object(
            auto_pipeline, "_run_batch"
        ) as batch, patch.object(auto_pipeline, "_run_user", return_value={"id": 1}):
            auto_pipeline._run("max-run", Path(raw), 1, "gpt-6-astra", "math_harness")
        # The Max run's own watchdog (jobs._cli_timeout_seconds) is 11,100 s.
        watchdog = int(min(16 * cgm_max.CLAUDE_CALL_TIMEOUT, cgm_max.RUN_SECONDS) + 300)
        self.assertGreater(waits[0], watchdog)
        batch.assert_called_once()

    def test_other_proof_runs_keep_the_operator_timeout(self) -> None:
        waits: list[float] = []
        with tempfile.TemporaryDirectory() as raw, patch.dict(
            os.environ, {"AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT": "60"}
        ), patch.object(jobs, "_load_run_record", return_value={"engine": "claude", "auth_route": "claude_subscription"}), patch.object(
            auto_pipeline, "_run_record", return_value={"status": "running", "engine": "claude"}
        ), patch.object(
            auto_pipeline, "_wait_for_source",
            side_effect=lambda *_a, timeout: waits.append(timeout) or ("", {"status": "running"}),
        ):
            auto_pipeline._run("claude-run", Path(raw), 1, "opus", "claude")
        self.assertEqual(waits, [60.0])

    def test_a_cgm_run_is_awaited_for_its_own_wall_clock(self) -> None:
        # A CGM run writes proof.md only when it publishes, which may be as late
        # as its watchdog: past the two-hour default once the operator raises
        # AGENT_MONITOR_CGM_MAX_SECONDS to its four-hour ceiling.
        waits: list[float] = []
        with tempfile.TemporaryDirectory() as raw, patch.dict(
            os.environ,
            {"AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT": "7200", "AGENT_MONITOR_CGM_MAX_SECONDS": "14400"},
        ), patch.object(jobs, "_load_run_record", return_value={"engine": "math_harness", "auth_route": "api_key"}), patch.object(
            auto_pipeline, "_run_record", return_value={"status": "running", "engine": "math_harness"}
        ), patch.object(
            auto_pipeline, "_wait_for_source",
            side_effect=lambda *_a, timeout: waits.append(timeout) or ("", {"status": "running"}),
        ):
            watchdogs = [jobs._cli_timeout_seconds(engine, dict(os.environ)) for engine in ("math_harness", "kimi")]
            for engine in ("math_harness", "kimi"):
                auto_pipeline._run(f"{engine}-run", Path(raw), 1, "gpt-6-astra", engine)
            # A longer operator wait is never shortened.
            os.environ["AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT"] = "20000"
            auto_pipeline._run("cgm-run", Path(raw), 1, "gpt-6-astra", "math_harness")
        self.assertGreater(min(watchdogs), 14_400)
        for wait, watchdog in zip(waits[:2], watchdogs, strict=True):
            self.assertGreater(wait, watchdog)
        self.assertEqual(waits[2], 20000.0)


class ClaudeDerivedModelTests(unittest.TestCase):
    def test_resolved_model_names_route_to_their_alias(self) -> None:
        self.assertEqual(lean_verify._claude_account_model("claude-opus-5-5", {}), "opus")
        self.assertEqual(lean_verify._claude_account_model("claude-fable-5", {}), "fable")
        self.assertEqual(lean_verify._claude_account_model("claude-haiku-4-5-20251001", {}), "claude-haiku-4-5")
        self.assertEqual(lean_verify._claude_account_model(None, {"AGENT_MONITOR_CLAUDE_MODEL": "opus"}), "opus")
        self.assertEqual(lean_verify._claude_account_model("gpt-6-astra", {}), "sonnet")

    def test_lean_repairs_keep_the_selected_model(self) -> None:
        calls: list[str] = []

        def exec_(*_args, **kwargs):
            calls.append(kwargs["model"])
            return SimpleNamespace(text="{}", model="claude-opus-5-5", usage={}, cost_usd=None)

        env = {"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1", "CLAUDE_CONFIG_DIR": "/tmp/claude-user"}
        with patch("agent_monitor.runners.claude_backend.claude_exec", side_effect=exec_):
            used, _ = lean_verify._chat(env, "opus", "translate")
            lean_verify._chat(env, used, "repair")
        self.assertEqual(calls, ["opus", "opus"])
        self.assertEqual(used.observed_model, "claude-opus-5-5")

    def test_dag_rebuild_of_a_native_claude_run_keeps_its_model(self) -> None:
        captured = {}

        def exec_(*_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text='{"nodes": []}', model="claude-opus-5-5", usage={}, cost_usd=None)

        with patch("agent_monitor.jobs._configure_auth_route", side_effect=lambda env, **_kw: env), patch(
            "agent_monitor.runners.claude_backend.claude_exec", side_effect=exec_
        ):
            proof_graph._run_claude_prompt({"id": 1}, "graph", model="claude-opus-5-5")
        self.assertEqual(captured["model"], "opus")


class DerivedCodexEffortTests(unittest.TestCase):
    def test_codex_runs_derive_at_no_more_than_high_effort(self) -> None:
        record = {"auth_route": "codex_subscription", "model": "gpt-6-astra", "reasoning_effort": "max"}
        view = cgm_max.formal_view(record)
        self.assertEqual(view["reasoning_effort"], "high")
        self.assertEqual(record["reasoning_effort"], "max")  # the record itself is untouched
        for unchanged in (
            {"auth_route": "codex_subscription", "reasoning_effort": "medium"},
            {"auth_route": "claude_subscription", "reasoning_effort": "max"},
            {"auth_route": "api_key", "reasoning_effort": "max"},
        ):
            self.assertIs(cgm_max.formal_view(unchanged), unchanged)


class HermesTurnErrorTests(unittest.TestCase):
    def _run(self, result: dict) -> dict:
        class FakeAgent:
            model = "gpt-6.1-sol"
            session_input_tokens = 0
            session_output_tokens = 0
            session_estimated_cost_usd = 0.0

            def run_conversation(self, _prompt, **_kwargs):
                return result

        from agent_monitor.runners import hermes as hermes_runner

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            hermes_runner, "create_agent", return_value=FakeAgent()
        ), patch.object(hermes_runner, "close_agent"), patch.object(jobs, "_write_run"), patch.object(
            jobs, "_attach_analysis", side_effect=lambda run, _ws: run
        ):
            workspace = Path(tmp)
            run = jobs._run_hermes(run_id="hermes-err", problem_id="p", problem_text="Prove it.",
                                   model="gpt-6.1-sol", max_iterations=2, started=time.time(),
                                   workspace=workspace, use_subagents=False)
            run["_proof_exists"] = (workspace / "proof.md").exists()
        return run

    def test_codex_usage_limit_is_the_run_error(self) -> None:
        run = self._run({
            "completed": False, "partial": True, "final_response": "", "messages": [],
            "error": ("turn ended status=failed: You’ve hit your usage limit. Visit "
                      "https://chatgpt.com/codex/settings/usage to purchase more credits or try "
                      "again at 10:55 PM.\ncodex stderr (last 4 lines):\n\x1b[2m2026\x1b[0m WARN plugins"),
        })
        self.assertEqual(run["status"], "failed")
        self.assertIn("You’ve hit your usage limit", run["error"])
        self.assertNotIn("codex stderr", run["error"])
        self.assertNotIn("\x1b", run["error"])

    def test_a_crashed_app_server_report_is_not_saved_as_the_proof(self) -> None:
        run = self._run({
            "completed": False, "partial": True, "messages": [],
            "final_response": ("Codex app-server turn failed: codex app-server exited unexpectedly "
                               "(status 1). Fall back to default runtime with `/codex-runtime auto`."),
            "error": "codex app-server exited unexpectedly (status 1)",
        })
        self.assertEqual(run["status"], "failed")
        self.assertFalse(run["_proof_exists"])
        self.assertIn("exited unexpectedly", run["error"])

    def test_a_truncated_draft_is_still_kept(self) -> None:
        draft = "# Proof\n\nLet $n$ be even, so $n = 2k$ for an integer $k$. " + "Then the argument continues. " * 40
        run = self._run({
            "completed": False, "partial": True, "messages": [], "final_response": draft,
            "error": "Response remained truncated after 4 continuation attempts",
        })
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["_proof_exists"])
        self.assertIn("remained truncated", run["error"])

    def test_a_failure_marked_failed_or_echoing_its_error_is_not_a_draft(self) -> None:
        for result in (
            {"completed": False, "failed": True, "messages": [],
             "final_response": "First response truncated due to output length limit",
             "error": "First response truncated due to output length limit"},
            {"completed": False, "partial": True, "messages": [],
             "final_response": "Response truncated due to output length limit",
             "error": "Response truncated due to output length limit"},
        ):
            with self.subTest(error=result["error"]):
                run = self._run(result)
                self.assertEqual(run["status"], "failed")
                self.assertFalse(run["_proof_exists"])


class InterruptedContinuationTests(unittest.TestCase):
    def test_a_stopped_follow_up_keeps_the_earlier_sessions(self) -> None:
        previous = {"run_id": "r", "agents": [{"trace_id": "r::a"}, {"trace_id": "r::b"}],
                    "totals": {"input_tokens": 50000, "output_tokens": 5000, "cost_usd": 1.0}}
        heartbeat = {"run_id": "r", "agents": [{"trace_id": "r::iter-1"}],
                     "totals": {"input_tokens": 20000, "output_tokens": 2000, "cost_usd": 0.07}}
        merged = jobs._interrupted_continuation_record(previous, heartbeat, job_id="j2")
        self.assertEqual([a["trace_id"] for a in merged["agents"]][:2], ["r::a", "r::b"])
        self.assertEqual(len(merged["agents"]), 3)
        self.assertEqual(merged["totals"]["input_tokens"], 70000)
        # A record that already holds the history is kept as it is.
        self.assertIs(jobs._interrupted_continuation_record(previous, previous, job_id="j2"), previous)

    def test_hermes_ids_restarting_each_session_do_not_hide_the_history(self) -> None:
        # The first session was itself stopped (no ::finalize node); the
        # follow-up's heartbeat reuses r::iter-1 and r::iter-2.
        previous = {"run_id": "r", "agents": [{"trace_id": "r::iter-1", "output": "a"},
                                             {"trace_id": "r::iter-2", "output": "b"}],
                    "totals": {"input_tokens": 900, "output_tokens": 90}}
        heartbeat = {"run_id": "r", "agents": [{"trace_id": f"r::iter-{n}", "output": "follow-up"} for n in (1, 2, 3)],
                     "totals": {"input_tokens": 30, "output_tokens": 3}}
        merged = jobs._interrupted_continuation_record(previous, heartbeat, job_id="j2")
        self.assertEqual([a["output"] for a in merged["agents"]][:2], ["a", "b"])
        self.assertEqual(len(merged["agents"]), 5)
        self.assertEqual(merged["totals"]["input_tokens"], 930)

    def test_a_live_merged_or_unstarted_follow_up_is_kept_as_it_is(self) -> None:
        previous = {"run_id": "r", "agents": [{"trace_id": "r::a"}], "totals": {"input_tokens": 50}}
        live = jobs._merge_continuation_run(
            previous, {"run_id": "r", "agents": [{"trace_id": "r::a"}], "totals": {"input_tokens": 5}}, job_id="j2")
        self.assertIs(jobs._interrupted_continuation_record(previous, live, job_id="j2"), live)
        unstarted = dict(previous, status="running")
        self.assertIs(jobs._interrupted_continuation_record(previous, unstarted, job_id="j2"), unstarted)


class StoppedFollowUpTests(unittest.TestCase):
    """Stop pressed during a follow-up wins over what the stopped engine returns."""

    def _seed(self, engine: str) -> tuple[str, str, Path]:
        """A finished one-session run whose follow-up job is running."""
        run_id, job_id = f"{engine}_stopped_follow_up", f"job-{engine}-stopped"
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        workspace = root / "runs" / "workspaces" / run_id
        workspace.mkdir(parents=True)
        (workspace / "proof.md").write_text("# Claim\n\nThe first session's proof.\n", encoding="utf-8")
        jobs._write_run({
            "run_id": run_id, "engine": engine, "problem_id": "p", "status": "finished",
            "problem_text": "Prove it.", "problems": [{"text": "Prove it.", "source": "initial"}],
            "agents": [{"trace_id": f"{run_id}::first", "output": "first session", "status": "finished"}],
            "edges": [],
        })
        jobs._JOBS[job_id] = {"job_id": job_id, "run_id": run_id, "status": "running", "engine": engine}
        self.addCleanup(jobs._JOBS.pop, job_id, None)
        self.addCleanup(jobs._STOP_EVENTS.pop, run_id, None)
        jobs._stop_event(run_id).clear()
        return run_id, job_id, workspace

    def _execute(self, engine: str, run_id: str, job_id: str, workspace: Path, engine_run) -> bool:
        """Run the follow-up with ``engine_run`` as its engine; True if a refresh started."""
        with (
            patch("agent_monitor.runners.improof.run_problem", side_effect=engine_run),
            patch.object(jobs, "_run_cli_engine", side_effect=engine_run),
            patch.object(jobs, "_launch_pending_feedback"),
            patch.object(auto_pipeline, "start") as derived,
        ):
            jobs._execute_continue(
                job_id=job_id, run_id=run_id, engine=engine, message="Add the boundary case.",
                problem_text="Prove it.", model=None, max_iterations=2, workspace=str(workspace),
            )
        return derived.called

    def _continue(self, engine: str, *, stopped_result) -> tuple[dict, dict, bool]:
        run_id, job_id, workspace = self._seed(engine)

        def engine_stopped_by_user(*_args, **_kwargs):
            self.assertEqual(jobs.stop_run(run_id)["status"], "stopped")
            return stopped_result(run_id)

        derived = self._execute(engine, run_id, job_id, workspace, engine_stopped_by_user)
        return jobs._load_run_record(run_id), dict(jobs._JOBS[job_id]), derived

    def test_a_stopped_improof_follow_up_is_not_recorded_as_failed(self) -> None:
        # The killed workflow exits -9, which run_problem reports as failed.
        def killed_workflow(_run_id: str) -> dict:
            return {"engine": "improof", "status": "failed", "returncode": -9,
                    "stdout_tail": "", "stderr_tail": ""}

        run, job, derived = self._continue("improof", stopped_result=killed_workflow)

        self.assertEqual(run["status"], "stopped")
        self.assertIsNone(run.get("error"))
        self.assertEqual(job["status"], "stopped")
        self.assertFalse(derived)
        self.assertEqual(run["agents"][0]["output"], "first session")

    def test_a_stopped_cli_follow_up_is_not_recorded_as_finished(self) -> None:
        def stopped_cli(run_id: str) -> dict:
            # _run_cli_engine writes and returns the follow-up's own nodes.
            partial = {"run_id": run_id, "status": "stopped", "agents": [
                {"trace_id": f"{run_id}::turn-1", "output": "partial follow-up", "status": "stopped"}]}
            jobs._write_run(dict(partial))
            return partial

        run, job, derived = self._continue("codex", stopped_result=stopped_cli)

        self.assertEqual(run["status"], "stopped")
        self.assertEqual(job["status"], "stopped")
        self.assertFalse(derived)
        self.assertEqual(
            [agent["output"] for agent in run["agents"]], ["first session", "partial follow-up"]
        )

    def test_a_stopped_follow_up_tells_the_chat_once(self) -> None:
        def killed_workflow(_run_id: str) -> dict:
            return {"engine": "improof", "status": "failed", "returncode": -9,
                    "stdout_tail": "", "stderr_tail": ""}

        run, _job, _derived = self._continue("improof", stopped_result=killed_workflow)

        posted = [message["content"] for message in jobs.list_chat(run["run_id"])]
        self.assertEqual(posted.count("Run stopped by user."), 1)

    def test_deleting_the_run_during_a_follow_up_keeps_it_deleted(self) -> None:
        run_id, job_id, workspace = self._seed("improof")
        self.addCleanup(jobs._DELETED_RUNS.discard, run_id)

        def engine_deleted_with_its_run(*_args, **_kwargs):
            # delete_run stops the follow-up as Stop does, then removes it all.
            jobs.delete_run(run_id)
            return {"engine": "improof", "status": "failed", "returncode": -9,
                    "stdout_tail": "", "stderr_tail": ""}

        derived = self._execute("improof", run_id, job_id, workspace, engine_deleted_with_its_run)

        with self.assertRaises(FileNotFoundError):
            jobs._load_run_record(run_id)
        self.assertFalse(workspace.exists())
        self.assertNotIn(job_id, jobs._JOBS)
        self.assertFalse(derived)


class MetaHarnessAuditFailureTests(unittest.TestCase):
    PROMPT = "Prove x.\n\nCANDIDATE AUDIT GATE: run the audit"

    def _run(self, fail_at: int | None) -> tuple[int, int, bool, str | None]:
        from agent_monitor.runners import metaharness_runner
        from agent_monitor.runners.api_backend import APIBackendError

        calls: list[str] = []

        def fake_chat(_client, _model, system, _user, **_kwargs):
            calls.append(system)
            if len(calls) == fail_at:
                raise APIBackendError("Openai model call failed for gpt-5.2 (HTTP 429): insufficient_quota")
            if system == metaharness_runner.EVALUATOR_SYSTEM:
                return "SCORE: 9\nfine", {"input_tokens": 1, "output_tokens": 1}
            return ("# Claim\nProof. Trivial.\n\nProvingConsole outcome: Partial Progress\n" + "x" * 500,
                    {"input_tokens": 1, "output_tokens": 1})

        with tempfile.TemporaryDirectory() as td:
            output = io.StringIO()
            with (
                patch.object(metaharness_runner, "chat", side_effect=fake_chat),
                patch.object(metaharness_runner.Path, "cwd", return_value=Path(td)),
                patch.object(sys, "argv", ["metaharness_runner.py", self.PROMPT]),
                patch.dict(os.environ, {"METAHARNESS_ROUNDS": "1", "PATH": os.environ.get("PATH", "")}, clear=True),
                redirect_stdout(output),
            ):
                returncode = metaharness_runner.main()
            parser = CLIEventParser("codex", source_engine="metaharness")
            parser.feed(output.getvalue())
            return returncode, len(calls), (Path(td) / "proof.md").is_file(), parser.terminal_error

    def test_a_failed_audit_pass_leaves_a_finished_run_finished(self) -> None:
        returncode, calls, proof, error = self._run(None)
        self.assertEqual((returncode, proof, error), (0, True, None))
        self.assertGreater(calls, 2)  # solver, evaluator, then the audit passes
        # The adjudication (the last audit pass) fails: the runner still
        # writes proof.md and exits 0, and the run must not read as failed.
        returncode, _, proof, error = self._run(calls)
        self.assertEqual((returncode, proof, error), (0, True, None))

    def test_a_failed_solver_call_is_still_the_run_error(self) -> None:
        returncode, _, proof, error = self._run(1)
        self.assertEqual((returncode, proof), (1, False))
        self.assertIn("insufficient_quota", error or "")


class CodexAuditFailureTests(unittest.TestCase):
    """On the Codex account route a failed audit pass streams Codex's own
    ``turn.failed``; the runner tolerates it, so the run must not read as failed."""

    PROMPT = MetaHarnessAuditFailureTests.PROMPT
    # What codex_exec forwards for the captured 0.160.0 usage-limit stream.
    USAGE_LIMIT_EVENTS = [
        event
        for event in map(json.loads, CODEX_USAGE_LIMIT_STDOUT.splitlines())
        if not codex_backend._legacy_landlock_notice(event)
    ]

    def _run(self, runner, engine: str, fail_from: int) -> tuple[int, int, bool, str | None]:
        calls: list[str] = []

        def fake_codex_exec(system, _user, *, emit_event=None, **_kwargs):
            calls.append(system)
            emit = emit_event or (lambda _event: None)
            if len(calls) >= fail_from:
                for event in self.USAGE_LIMIT_EVENTS:
                    emit(event)
                raise codex_backend.CodexBackendError(
                    "codex exec failed: " + self.USAGE_LIMIT_EVENTS[-1]["error"]["message"],
                    kind="usage_limit",
                )
            text = ("SCORE: 9\nfine" if "strict proof evaluator" in system
                    else "# Claim\nProof. Trivial.\n\nProvingConsole outcome: Partial Progress\n" + "x" * 500)
            for event in (
                {"type": "thread.started", "thread_id": f"t{len(calls)}"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}},
            ):
                emit(event)
            return codex_backend.CodexResult(text, {"input_tokens": 3, "output_tokens": 2})

        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as td:
            output = io.StringIO()
            os.chdir(td)
            try:
                with (
                    patch.object(runner, "codex_exec", side_effect=fake_codex_exec),
                    patch.object(sys, "argv", [runner.__name__, self.PROMPT]),
                    patch.dict(os.environ, {"AGENT_MONITOR_CODEX_SUBSCRIPTION": "1", "METAHARNESS_ROUNDS": "1",
                                            "PATH": os.environ.get("PATH", "")}, clear=True),
                    redirect_stdout(output),
                ):
                    returncode = runner.main()
            finally:
                os.chdir(previous)
            parser = CLIEventParser("codex", source_engine=engine)
            parser.feed(output.getvalue())
            return returncode, len(calls), (Path(td) / "proof.md").is_file(), parser.terminal_error

    def test_only_a_failed_turn_is_rewritten(self) -> None:
        projected: list[dict] = []
        completed = {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}
        for event in [*self.USAGE_LIMIT_EVENTS, completed]:
            codex_backend.forward_nonterminal(event, projected.append)
        self.assertFalse(any(event["type"] in {"turn.failed", "turn.cancelled"} for event in projected))
        self.assertEqual(projected[-2]["item"]["type"], "error")
        self.assertIn("You’ve hit your usage limit", projected[-2]["item"]["message"])
        self.assertNotIn("terminal", projected[-2]["item"])
        self.assertEqual(projected[-1], completed)  # the call's usage still reaches the run

    def test_a_failed_meta_harness_audit_pass_leaves_the_run_finished(self) -> None:
        from agent_monitor.runners import metaharness_runner

        # Solver and evaluator succeed; every audit pass hits the usage limit.
        returncode, calls, proof, error = self._run(metaharness_runner, "metaharness", 3)
        self.assertGreater(calls, 3)
        self.assertEqual((returncode, proof, error), (0, True, None))

    def test_a_failed_deepseek_harness_audit_pass_leaves_the_run_finished(self) -> None:
        from agent_monitor.runners import deepseek_harness_runner

        # The proving call succeeds; every audit pass hits the usage limit.
        returncode, calls, proof, error = self._run(deepseek_harness_runner, "deepseek_harness", 2)
        self.assertGreater(calls, 2)
        self.assertEqual((returncode, proof, error), (0, True, None))

    def test_a_failed_proving_call_is_still_the_run_error(self) -> None:
        from agent_monitor.runners import deepseek_harness_runner, metaharness_runner

        for runner, engine in ((metaharness_runner, "metaharness"), (deepseek_harness_runner, "deepseek_harness")):
            with self.subTest(engine=engine):
                returncode, _, proof, error = self._run(runner, engine, 1)
                self.assertEqual((returncode, proof), (1, False))
                self.assertIn("You’ve hit your usage limit", error or "")


class OpenClaudeRecoveryTests(unittest.TestCase):
    def test_a_listed_reason_keeps_the_subtype_the_retry_keys_on(self) -> None:
        for subtype, listed in (
            ("error_max_turns", "Reached maximum number of turns (60)"),
            ("error_during_execution", "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null"),
        ):
            with self.subTest(subtype=subtype):
                parser = CLIEventParser("openclaude")
                parser.feed(json.dumps({"type": "result", "subtype": subtype, "is_error": True, "num_turns": 60,
                                        "usage": {}, "errors": [listed]}))
                reason = jobs._cli_failure_reason(label="OpenClaude", returncode=1, timed_out=False,
                                                  timeout_s=3600, proof_missing=False, detail=parser.terminal_error)
                self.assertIn(listed, reason)
                self.assertEqual(jobs._openclaude_recoverable_failure(reason), subtype)


class TransientRateLimitTests(unittest.TestCase):
    def test_codex_classifies_plan_limits_not_a_bare_429(self) -> None:
        self.assertIsNone(codex_backend._failure_kind("exceeded retry limit, last status: 429 Too Many Requests"))
        self.assertEqual(codex_backend._failure_kind(
            "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage"), "usage_limit")
        self.assertEqual(codex_backend._failure_kind("Quota exceeded. Check your plan and billing details."),
                         "usage_limit")
        # An API key out of credit answers 429 too, but it stays out of credit.
        self.assertEqual(codex_backend._failure_kind(
            "unexpected status 429 Too Many Requests: You exceeded your current quota, please check your plan "
            "and billing details. (insufficient_quota)"), "usage_limit")

    def test_one_transient_429_fails_only_its_own_check(self) -> None:
        run = analyze_runner.AnalysisRun.__new__(analyze_runner.AnalysisRun)
        run.permanent_failure = False
        run.provider_failure = ""
        run.degraded = []
        run.ctx = SimpleNamespace(run_processed=lambda: True)
        try:
            raise harness.ProviderCallError("check-1 provider call failed") from codex_backend.CodexBackendError(
                "codex exec failed: exceeded retry limit, last status: 429 Too Many Requests",
                kind=codex_backend._failure_kind("exceeded retry limit, last status: 429 Too Many Requests"))
        except harness.ProviderCallError as error:
            run._note_failure(error)
        self.assertFalse(run.permanent_failure)


class IMProofClaudeModelTests(unittest.TestCase):
    def test_continuation_hands_the_selected_model_to_the_claude_worker(self) -> None:
        captured = {}

        def fake_run(*_args, **kwargs):
            captured.update(kwargs)
            return {"status": "finished", "returncode": 0}

        with tempfile.TemporaryDirectory() as td, patch(
            "agent_monitor.runners.improof.run_problem", side_effect=fake_run
        ), patch.object(jobs, "_wrap_subprocess_result", return_value={"status": "finished"}), patch.object(
            jobs, "_live_output_flusher", return_value=lambda _line: None
        ):
            jobs._run_improof_continuation(
                run_id="improof_demo", job_id="job1", problem_id="demo", problem_text="Again.",
                display_problem_text="Original.", model="opus", started=0.0, workspace=Path(td),
                extra_env={"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1"},
            )
        self.assertEqual(captured["extra_env"]["AGENT_MONITOR_CLAUDE_MODEL"], "opus")


class CliEngineFailureDetailTests(unittest.TestCase):
    def test_wrapper_reason_reaches_the_run_error(self) -> None:
        command = [sys.executable, "-c", (
            "import json, sys\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-5-5'}), flush=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': ''}), flush=True)\n"
            "print('[agent-monitor] Claude Code completed without a fresh substantive regular root proof artifact', file=sys.stderr)\n"
            "sys.exit(1)\n")]
        with tempfile.TemporaryDirectory() as tmp, patch(
            "agent_monitor.engines_registry.build_cli_command", return_value=command
        ), patch("agent_monitor.agent_config.persona_preamble", return_value=""), patch.object(jobs, "_write_run"):
            run = jobs._run_cli_engine(run_id="claude_detail", engine="claude", problem_id="p",
                                       problem_text="Prove it.", started=time.time(), workspace=Path(tmp))
        self.assertEqual(run["status"], "failed")
        self.assertIn("without a fresh substantive regular root proof artifact", run["error"])

    def test_a_claude_sign_in_failure_tells_the_web_user_to_reconnect(self) -> None:
        # Claude Code's own result says "Please run /login", which a web user
        # cannot do; claude_runner's typed message says what to do instead.
        runner = Path(claude_backend.__file__).with_name("claude_runner.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "workspace").mkdir()
            fake = _fake_claude_cli(root / "bin", _synthetic_failure(
                "Not logged in · Please run /login", "authentication_failed"), exit_code=1)
            extra_env = {**_claude_env(str(root / "config")),
                         "PATH": os.pathsep.join([str(fake.parent), "/usr/bin", "/bin"])}
            # HOME keeps the registry's ~/.local/bin (a real install) off PATH.
            with patch.dict(os.environ, {"HOME": str(root)}), patch(
                "agent_monitor.engines_registry.build_cli_command",
                return_value=[sys.executable, "-u", str(runner), "Prove it."],
            ), patch("agent_monitor.agent_config.persona_preamble", return_value=""), patch.object(jobs, "_write_run"):
                run = jobs._run_cli_engine(run_id="claude_reauth", engine="claude", problem_id="p",
                                           problem_text="Prove it.", started=time.time(),
                                           workspace=root / "workspace", extra_env=extra_env)
            self.assertTrue(_fake_claude_calls(fake))
        self.assertEqual(run["status"], "failed")
        self.assertIn("reconnect Claude Code in Settings", run["error"])

    def test_a_prompt_too_large_for_the_command_line_is_explained(self) -> None:
        # An engine (or an operator's CLAUDE_CMD) that still passes {prompt}.
        with tempfile.TemporaryDirectory() as tmp, patch(
            "agent_monitor.engines_registry.build_cli_command",
            return_value=[sys.executable, "-c", "pass", "x" * 200_000],
        ), patch("agent_monitor.agent_config.persona_preamble", return_value=""), patch.object(jobs, "_write_run"):
            run = jobs._run_cli_engine(run_id="claude_e2big", engine="claude", problem_id="p",
                                       problem_text="Prove it.", started=time.time(), workspace=Path(tmp))
        self.assertEqual(run["status"], "failed")
        self.assertIn("too large to pass on the command line", run["error"])
        # Attaching helps only because an excerpt is sent, and the profile
        # and library context count too.
        self.assertIn("agent profile", run["error"])
        self.assertIn("only an excerpt", run["error"])

    def test_a_prompt_file_the_wrapper_never_read_is_removed(self) -> None:
        # It holds the agent profile; the wrapper removes it only once read.
        for name, command in (
            ("could not start", "/nonexistent/python --prompt-file {prompt_file}"),
            ("ended without reading it", f"{sys.executable} -c pass --prompt-file {{prompt_file}}"),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"PLAIN_CMD": command}
            ), patch("agent_monitor.agent_config.persona_preamble", return_value=""), patch.object(jobs, "_write_run"):
                run = jobs._run_cli_engine(run_id="plain_unread", engine="plain", problem_id="p",
                                           problem_text="Prove it.", started=time.time(), workspace=Path(tmp))
                self.assertEqual(run["status"], "failed")
                self.assertEqual(list(Path(tmp).iterdir()), [])



class IMProofClaudeUsageTests(unittest.TestCase):
    def test_projected_claude_usage_counts_cached_input(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines" / "improof" / "src"))
        try:
            from proofstack.cli_usage import parse_codex_jsonl
        finally:
            sys.path.pop(0)
        projected: list[dict] = []
        claude_backend.forward_as_codex_event(
            {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
             "usage": {"input_tokens": 12, "cache_read_input_tokens": 9000,
                       "cache_creation_input_tokens": 3000, "output_tokens": 800}},
            projected.append,
        )
        usage = parse_codex_jsonl("\n".join(json.dumps(event) for event in projected))
        self.assertEqual(usage.input_tokens, 12_012)
        self.assertEqual(usage.cached_input_tokens, 9000)
        self.assertEqual(usage.output_tokens, 800)


if __name__ == "__main__":
    unittest.main()
