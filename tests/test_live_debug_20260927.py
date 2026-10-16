"""Regressions for the 2026-09-27 Codex / Claude Code live debug.

* Claude-route Lean checks used the account HOME's elan and downloaded a
  second toolchain there (ELAN_HOME was never set).
* Claude run totals left cache reads/writes out of the input count.
* The Codex fallback model list differed from the real account catalog, so
  the UI could offer a model the server then rejected.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import claude_login, codex_login, jobs, lean_verify
from agent_monitor.cli_events import CLIEventParser
from agent_monitor.runners import claude_backend
from agent_monitor.runners import math_harness_runner
from agent_monitor.subprocess_env import server_elan_home


class ServerElanHomeTests(unittest.TestCase):
    def test_explicit_absolute_elan_home_wins(self) -> None:
        with patch.dict(os.environ, {"ELAN_HOME": "/opt/elan/../elan", "HOME": "/home/x"}):
            self.assertEqual(server_elan_home(), "/opt/elan")

    def test_default_is_the_server_home_and_relative_values_are_ignored(self) -> None:
        with patch.dict(os.environ, {"ELAN_HOME": "relative/elan", "HOME": "/srv/console"}):
            self.assertEqual(server_elan_home(), "/srv/console/.elan")
        env = {k: v for k, v in os.environ.items() if k not in {"ELAN_HOME", "HOME"}}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(os.path.isabs(server_elan_home()))
            self.assertTrue(server_elan_home().endswith(".elan"))


class CheckScriptElanHomeTests(unittest.TestCase):
    def test_check_sh_pins_the_server_elan_home_even_under_an_account_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server_elan = root / "server-elan"
            account_home = root / "claude_home" / "users" / "user_3"
            account_home.mkdir(parents=True)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            # A stand-in `lean` (like elan's shim) reports which elan home it sees.
            lean = fake_bin / "lean"
            lean.write_text('#!/bin/sh\necho "ELAN_HOME=$ELAN_HOME"\n', encoding="utf-8")
            lean.chmod(0o755)
            ldir = root / "lean"
            ldir.mkdir()
            (ldir / "Proof.lean").write_text("theorem t : True := trivial\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"ELAN_HOME": str(server_elan), "AGENT_MONITOR_MATHLIB_PROJECT": ""},
            ):
                script = lean_verify._write_harness_checker(ldir)
            text = script.read_text(encoding="utf-8")
            self.assertIn(f"ELAN_HOME={server_elan}\nexport ELAN_HOME\n", text)
            completed = subprocess.run(
                [str(script)],
                cwd=ldir,
                env={"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(account_home)},
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), f"ELAN_HOME={server_elan}")

    def test_harness_env_sets_the_server_elan_home(self) -> None:
        with (
            patch("agent_monitor.settings.resolved_user_env", return_value={"OPENAI_API_KEY": "k"}),
            patch.object(lean_verify, "toolchain_status", return_value={}),
            patch.object(lean_verify, "_harness_lean_bin_dirs", return_value=[]),
            patch.dict(os.environ, {"ELAN_HOME": "/opt/server-elan"}),
        ):
            env = lean_verify._harness_env("codex", {"id": 7}, model="gpt-test", auth_route="api_key")
        self.assertEqual(env["ELAN_HOME"], "/opt/server-elan")

    def test_harness_lean_bin_dirs_use_the_server_elan_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            elan_bin = Path(tmp) / "bin"
            elan_bin.mkdir()
            (elan_bin / "lean").write_text("", encoding="utf-8")
            with (
                patch.dict(os.environ, {"ELAN_HOME": tmp}),
                patch("shutil.which", return_value=None),
            ):
                self.assertEqual(lean_verify._harness_lean_bin_dirs(), [str(elan_bin)])


class SubscriptionRouteElanHomeTests(unittest.TestCase):
    def test_claude_route_keeps_the_account_home_but_the_server_elan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_home = Path(tmp)
            with (
                patch.object(claude_login, "account_home", return_value=account_home),
                patch.object(claude_login, "account_login_ready", return_value=True),
                patch.dict(os.environ, {"ELAN_HOME": "/opt/server-elan"}),
            ):
                configured = jobs._configure_auth_route(
                    {"HOME": "/home/server", "PATH": "/bin"},
                    route="claude_subscription",
                    user_id=3,
                )
        self.assertEqual(configured["HOME"], str(account_home))
        self.assertEqual(configured["ELAN_HOME"], "/opt/server-elan")

    def test_claude_account_environment_keeps_lean_on_the_server_elan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user_4"
            with patch.dict(os.environ, {"ELAN_HOME": "/opt/server-elan"}):
                env = claude_login.account_environment(home, {"PATH": "/bin", "HOME": "/home/server"})
                routed = claude_login.account_environment(
                    home, {"PATH": "/bin", "ELAN_HOME": "/opt/route-elan"}
                )
        self.assertEqual(env["HOME"], str(home))
        self.assertEqual(env["ELAN_HOME"], "/opt/server-elan")
        self.assertEqual(routed["ELAN_HOME"], "/opt/route-elan")

    def test_codex_route_sets_the_server_elan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(codex_login, "account_home", return_value=Path(tmp)),
                patch.object(codex_login, "account_login_ready", return_value=True),
                patch.dict(os.environ, {"ELAN_HOME": "/opt/server-elan"}),
            ):
                configured = jobs._configure_auth_route(
                    {"PATH": "/bin"}, route="codex_subscription", user_id=3
                )
        self.assertEqual(configured["ELAN_HOME"], "/opt/server-elan")
        self.assertEqual(configured["CODEX_HOME"], tmp)

    def test_claude_cli_environment_passes_elan_home_through(self) -> None:
        self.assertIn("ELAN_HOME", claude_backend._PASSTHROUGH_ENV)
        with tempfile.TemporaryDirectory() as config:
            with patch(
                "agent_monitor.engines_registry.tool_search_path",
                return_value="/approved/claude",
            ):
                env = claude_backend._claude_environment(
                    {
                        "PATH": "/bin",
                        "HOME": config,
                        "CLAUDE_CONFIG_DIR": config,
                        "ELAN_HOME": "/opt/server-elan",
                    }
                )
        self.assertEqual(env["ELAN_HOME"], "/opt/server-elan")
        self.assertEqual(env["HOME"], config)


def _claude_stream(cache_read: int, cache_write: int) -> str:
    events = [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "Proof written."}],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 40,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                },
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Proof written.",
            "num_turns": 1,
            "total_cost_usd": 0.05,
            "usage": {
                "input_tokens": 14,
                "output_tokens": 90,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class ClaudeTokenAccountingTests(unittest.TestCase):
    def test_parser_totals_include_cache_reads_and_writes(self) -> None:
        parser = CLIEventParser("claude")
        parser.feed(_claude_stream(43_000, 2_100))
        self.assertEqual(parser.usage["input_tokens"], 14)
        self.assertEqual(parser.total_input_tokens(), 14 + 43_000 + 2_100)
        node = parser.agent_nodes("r", prompt="p")[0]
        self.assertEqual(node["total_input_tokens"], 7 + 43_000 + 2_100)

    def test_openclaw_stream_also_adds_cache(self) -> None:
        parser = CLIEventParser("openclaw")
        self.assertFalse(parser.input_includes_cache)
        self.assertEqual(parser.input_total(5, 10, 2), 17)

    def test_run_totals_show_all_input_and_the_cached_part(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stream = _claude_stream(43_000, 2_100)
            command = [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "Path('proof.md').write_text('# Proof\\n\\nA sufficiently long bounded proof artifact for this runner.\\n', encoding='utf-8'); "
                    f"sys.stdout.write({stream!r}); sys.stdout.flush()"
                ),
            ]
            with (
                patch("agent_monitor.engines_registry.build_cli_command", return_value=command),
                patch("agent_monitor.agent_config.persona_preamble", return_value=""),
                patch.object(jobs, "_write_run"),
            ):
                result = jobs._run_cli_engine(
                    run_id="openclaude_tokens",
                    engine="openclaude",
                    problem_id="tokens",
                    problem_text="Prove it.",
                    started=time.time(),
                    workspace=workspace,
                    requested_model="kimi-k3",
                    max_iterations=3,
                )
        totals = result["totals"]
        self.assertEqual(totals["input_tokens"], 14 + 43_000 + 2_100)
        self.assertEqual(totals["cached_input_tokens"], 43_000)
        self.assertEqual(totals["cache_write_tokens"], 2_100)
        self.assertEqual(totals["output_tokens"], 90)

    def test_continuations_keep_cache_write_totals(self) -> None:
        source = Path(jobs.__file__).read_text(encoding="utf-8")
        self.assertIn('"input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens",', source)

    def test_codex_projection_of_claude_usage_uses_codex_semantics(self) -> None:
        projected: list[dict] = []
        for line in _claude_stream(300, 20).splitlines():
            claude_backend.forward_as_codex_event(json.loads(line), projected.append)
        usage = [event for event in projected if event["type"] == "turn.completed"][0]["usage"]
        # On the wire the cache stays apart from input, flagged, so a server
        # parser that predates the flag never prices cached input twice.
        self.assertEqual(usage["input_tokens"], 14)
        self.assertEqual(usage["cached_input_tokens"], 300)
        self.assertEqual(usage["cache_write_tokens"], 20)
        self.assertIs(usage["input_excludes_cache"], True)
        # The parser folds it in: Codex semantics from here on.
        parser = CLIEventParser("codex")
        parser.feed("\n".join(json.dumps(event) for event in projected) + "\n")
        self.assertEqual(parser.usage["input_tokens"], 14 + 300 + 20)
        self.assertEqual(parser.total_input_tokens(), 14 + 300 + 20)
        self.assertEqual(parser.usage["cache_read_tokens"], 300)
        self.assertEqual(parser.usage["cache_write_tokens"], 20)

    def test_cgm_stage_usage_on_claude_counts_cache_as_input(self) -> None:
        events: list[dict] = []
        usage = {"input_tokens": 9, "output_tokens": 5, "cache_read_tokens": 400, "cache_write_tokens": 30}
        math_harness_runner._emit_stage_usage("worker-1", usage, out=events.append, input_includes_cache=False)
        math_harness_runner._emit_stage_usage("worker-2", {"input_tokens": 500, "cache_read_tokens": 400}, out=events.append)
        self.assertEqual(events[0]["usage"]["input_tokens"], 9)
        self.assertIs(events[0]["usage"]["input_excludes_cache"], True)
        self.assertEqual(events[0]["usage"]["cached_input_tokens"], 400)
        # Codex/API usage already counts its cached input and is not flagged.
        self.assertEqual(events[1]["usage"]["input_tokens"], 500)
        self.assertEqual(events[1]["usage"]["cached_input_tokens"], 400)
        self.assertNotIn("input_excludes_cache", events[1]["usage"])
        parser = CLIEventParser("codex", source_engine="math_harness")
        for event in events:
            parser.feed(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}) + "\n")
            parser.feed(json.dumps(event) + "\n")
        self.assertEqual(parser.total_input_tokens(), 439 + 500)
        self.assertEqual(parser.usage["cache_read_tokens"], 800)

    def test_an_unflagged_parser_still_prices_projected_claude_usage_once(self) -> None:
        """The runners load fresh per run but the server parser only on restart.

        Until then the old parser reads input_tokens and cached_input_tokens
        and prices them separately; the wire input must therefore exclude
        the cache (as it did before the flag existed).
        """
        projected: list[dict] = []
        for line in _claude_stream(40_000, 2_000).splitlines():
            claude_backend.forward_as_codex_event(json.loads(line), projected.append)
        usage = [event for event in projected if event["type"] == "turn.completed"][0]["usage"]
        self.assertEqual(usage["input_tokens"] + usage["cached_input_tokens"], 14 + 40_000)
        stage: list[dict] = []
        math_harness_runner._emit_stage_usage(
            "worker-1",
            {"input_tokens": 9, "cache_read_tokens": 400, "cache_write_tokens": 30},
            out=stage.append,
            input_includes_cache=False,
        )
        self.assertEqual(stage[0]["usage"]["input_tokens"], 9)


def _run_plain_with_stream(stream: str, run_id: str) -> tuple[dict, list[dict]]:
    """Run the plain (Codex-style) CLI engine over ``stream``; record estimates."""
    calls: list[dict] = []
    real_estimate = jobs._estimate_cost

    def recording_estimate(model, **kwargs):
        calls.append(dict(kwargs))
        return real_estimate(model, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        command = [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                "Path('proof.md').write_text('# Proof\\n\\nA sufficiently long bounded proof artifact for this runner.\\n', encoding='utf-8'); "
                f"sys.stdout.write({stream!r}); sys.stdout.flush()"
            ),
        ]
        with (
            patch("agent_monitor.engines_registry.build_cli_command", return_value=command),
            patch("agent_monitor.agent_config.persona_preamble", return_value=""),
            patch.object(jobs, "_write_run"),
            patch.object(jobs, "_estimate_cost", side_effect=recording_estimate),
        ):
            result = jobs._run_cli_engine(
                run_id=run_id,
                engine="plain",
                problem_id="cost",
                problem_text="Prove it.",
                started=time.time(),
                workspace=Path(tmp),
                requested_model="claude-sonnet-5",
                max_iterations=3,
            )
    # The first flush (before any output) asks for an all-zero estimate.
    return result, [call for call in calls if any(call.values())]


class ProjectedUsageCostTests(unittest.TestCase):
    """Codex-semantics input already contains cached input: price it once."""

    def test_claude_projected_turn_costs_the_same_as_the_direct_claude_estimate(self) -> None:
        projected: list[dict] = []
        for line in _claude_stream(40_000, 2_000).splitlines():
            claude_backend.forward_as_codex_event(json.loads(line), projected.append)
        stream = "\n".join(json.dumps(event) for event in projected) + "\n"
        result, calls = _run_plain_with_stream(stream, "plain_claude_cost")
        turn = next(a for a in result["agents"] if a["stage_name"].startswith("turn-"))
        self.assertEqual(turn["input_tokens"], 14 + 40_000 + 2_000)
        self.assertEqual(turn["cache_read_tokens"], 40_000)
        self.assertEqual(turn["cache_write_tokens"], 2_000)
        # Priced exactly like the native Claude usage: uncached input, cache
        # reads and cache writes each at their own rate, nothing twice.
        self.assertEqual(
            calls[0],
            {"input_tokens": 14, "output_tokens": 90, "cache_read": 40_000, "cache_write": 2_000},
        )
        direct = jobs._estimate_cost(
            "claude-sonnet-5", input_tokens=14, output_tokens=90, cache_read=40_000, cache_write=2_000
        )
        self.assertAlmostEqual(turn["cost_usd"], direct)
        self.assertAlmostEqual(result["totals"]["cost_usd"], direct)
        self.assertEqual(result["totals"]["input_tokens"], 14 + 40_000 + 2_000)
        self.assertEqual(result["totals"]["cached_input_tokens"], 40_000)
        self.assertEqual(result["totals"]["cache_write_tokens"], 2_000)

    def test_real_codex_usage_prices_cached_input_once(self) -> None:
        stream = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "thread.started", "thread_id": "t"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Proof written."}},
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 1_000, "cached_input_tokens": 800, "output_tokens": 50},
                },
            )
        ) + "\n"
        result, calls = _run_plain_with_stream(stream, "plain_codex_cost")
        self.assertEqual(
            calls[0],
            {"input_tokens": 200, "output_tokens": 50, "cache_read": 800, "cache_write": 0},
        )
        self.assertEqual(result["totals"]["input_tokens"], 1_000)

    def test_cgm_claude_stage_usage_round_trips_through_the_codex_parser(self) -> None:
        events: list[dict] = []
        usage = {"input_tokens": 9, "output_tokens": 5, "cache_read_tokens": 400, "cache_write_tokens": 30}
        math_harness_runner._emit_stage_usage("worker-1", usage, out=events.append, input_includes_cache=False)
        parser = CLIEventParser("codex", source_engine="math_harness")
        parser.feed(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}) + "\n")
        parser.feed(json.dumps(events[0]) + "\n")
        self.assertEqual(parser.usage["cache_write_tokens"], 30)
        self.assertEqual(parser.total_input_tokens(), 439)
        u = parser.usage
        self.assertEqual(
            parser.uncached_input(u["input_tokens"], u["cache_read_tokens"], u["cache_write_tokens"]), 9
        )

    def test_claude_parser_input_is_already_uncached(self) -> None:
        parser = CLIEventParser("claude")
        self.assertEqual(parser.uncached_input(14, 40_000, 2_000), 14)

    def test_out_of_run_claude_calls_price_cache_writes_at_their_rate(self) -> None:
        from agent_monitor import usage_ledger

        usage = usage_ledger.cli_usage(
            {"input_tokens": 14, "cache_read_tokens": 40_000, "cache_write_tokens": 2_000, "output_tokens": 90},
            input_includes_cache=False,
        )
        calls: list[dict] = []
        with (
            patch.object(jobs, "_estimate_cost", side_effect=lambda model, **kw: calls.append(kw) or 0.5),
            patch.object(usage_ledger, "record_call"),
        ):
            usage_ledger.record_usage(model="claude-sonnet-5", usage=usage, run_id="r", stage="lean")
        self.assertEqual(
            calls[0],
            {"input_tokens": 14, "output_tokens": 90, "cache_read": 40_000, "cache_write": 2_000},
        )


class CodexModelCatalogTests(unittest.TestCase):
    def test_fallback_list_is_the_current_catalog(self) -> None:
        self.assertEqual(
            list(codex_login.DEFAULT_CODEX_MODELS),
            ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"],
        )
        for retired in ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"):
            self.assertNotIn(retired, codex_login.DEFAULT_CODEX_MODELS)

    class _InlineThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self) -> None:
            self.target()

    def _cached_models(self, client_version: str, slug: str, refresh) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cli = home / "codex"
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            (home / "models_cache.json").write_text(
                json.dumps({"client_version": client_version,
                            "models": [{"slug": slug, "visibility": "list", "priority": 1}]}),
                encoding="utf-8",
            )
            with (
                patch.object(codex_login, "_CACHE_CHECKS", set()),
                patch.object(codex_login, "account_home", return_value=home),
                patch.object(codex_login, "account_login_ready", return_value=True),
                patch.object(codex_login, "codex_binary", return_value=str(cli)),
                patch.object(codex_login, "_installed_cli_version", return_value="0.160.0"),
                patch.object(codex_login, "refresh_models_cache", side_effect=refresh),
                patch.object(codex_login.threading, "Thread", self._InlineThread),
            ):
                first = codex_login.available_models(5)
                self.assertEqual(codex_login.available_models(5), first)
        return first

    def test_a_cache_written_by_another_cli_build_is_refreshed_once_in_the_background(self) -> None:
        refreshed: list[int] = []
        # The stale list is served at once; the account is refreshed once per build.
        self.assertEqual(self._cached_models("0.157.1", "gpt-6-astra", refreshed.append), ["gpt-6-astra"])
        self.assertEqual(refreshed, [5])

    def test_a_cache_from_the_installed_cli_is_not_refreshed(self) -> None:
        refreshed: list[int] = []
        self.assertEqual(self._cached_models("0.160.0", "gpt-6.1-sol", refreshed.append), ["gpt-6.1-sol"])
        self.assertEqual(refreshed, [])

    def test_refresh_runs_the_cli_catalog_in_the_account_home_and_reads_no_tokens(self) -> None:
        calls: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)

            def fake_run(argv, **kwargs):
                calls.append({"argv": argv, **kwargs})
                (home / "models_cache.json").write_text(
                    json.dumps({"models": [
                        {"slug": "gpt-6-sol", "visibility": "list", "priority": 2},
                        {"slug": "gpt-6-astra", "visibility": "list", "priority": 1},
                        {"slug": "gpt-hidden", "visibility": "hide", "priority": 3},
                    ]}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0)

            with (
                patch.object(codex_login, "account_home", return_value=home),
                patch.object(codex_login, "account_login_ready", return_value=True),
                patch.object(codex_login, "codex_binary", return_value="/usr/bin/codex"),
                patch("agent_monitor.runners.codex_backend._run_codex_process", side_effect=fake_run),
            ):
                self.assertTrue(codex_login.refresh_models_cache(5))
                models = codex_login.available_models(5)
        self.assertEqual(models, ["gpt-6-astra", "gpt-6-sol"])
        self.assertEqual(calls[0]["argv"], ["/usr/bin/codex", "debug", "models"])
        self.assertEqual(calls[0]["env"]["CODEX_HOME"], str(home))
        self.assertIn("timeout", calls[0])

    def test_refresh_is_skipped_without_a_login_or_cli_and_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(codex_login, "account_home", return_value=Path(tmp)),
                patch.object(codex_login, "codex_binary", return_value="/usr/bin/codex"),
                patch("agent_monitor.runners.codex_backend._run_codex_process") as run,
            ):
                with patch.object(codex_login, "account_login_ready", return_value=False):
                    self.assertFalse(codex_login.refresh_models_cache(5))
                run.assert_not_called()
                with (
                    patch.object(codex_login, "account_login_ready", return_value=True),
                    patch(
                        "agent_monitor.runners.codex_backend._run_codex_process",
                        side_effect=subprocess.TimeoutExpired("codex", 30),
                    ),
                ):
                    self.assertFalse(codex_login.refresh_models_cache(5))

    def test_successful_login_refreshes_the_catalog_before_reporting_success(self) -> None:
        order: list[str] = []

        class FakeProc:
            stdout = iter(["Signed in\n"])

            def wait(self, timeout=None):
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with codex_login._lock:
                codex_login._states[91] = {"status": "running", "running": True}

            def fake_refresh(user_id):
                with codex_login._lock:
                    order.append(f"refresh:{codex_login._states[user_id]['status']}")

            with (
                patch.object(codex_login, "account_login_ready", return_value=True),
                patch.object(codex_login, "refresh_models_cache", side_effect=fake_refresh),
            ):
                codex_login._reader(FakeProc(), home, 91)
            with codex_login._lock:
                final = dict(codex_login._states.pop(91))
        self.assertEqual(order, ["refresh:running"])
        self.assertEqual(final["status"], "success")


if __name__ == "__main__":
    unittest.main()
