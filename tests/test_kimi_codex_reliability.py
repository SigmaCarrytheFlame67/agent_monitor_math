from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

from agent_monitor import kimi_responses_proxy
from agent_monitor.runners import codex_backend, kimi_codex_runner
from agent_monitor.runners.codex_backend import CodexBackendError, CodexResult

PROOF = (
    "# Proof\n\nLet a = 2m and b = 2n for integers m and n. Then a + b = 2(m + n), "
    "so the sum of two even integers is even.\n\nProvingConsole outcome: Solved\n"
)


def install_fake_codex(bin_dir: Path, body: str) -> None:
    """A fake ``codex`` CLI whose ``exec`` runs ``body``.

    Tool-enabled (Kimi) calls never ask for the model catalog.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "codex"
    script.write_text(
        f"#!{sys.executable}\nPROOF = {PROOF!r}\n"
        + textwrap.dedent(
            """\
            import json, sys, time
            from pathlib import Path
            args = sys.argv[1:]
            if args == ["--version"]:
                print("codex-cli 0.160.0")
                sys.exit(0)
            sys.stdin.read()
            workspace = Path(args[args.index("--cd") + 1])
            last_message = Path(args[args.index("--output-last-message") + 1])
            def event(payload):
                print(json.dumps(payload), flush=True)
            """
        )
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    script.chmod(0o755)


def run_kimi_wrapper(body: str, *, timeout: str = "60") -> tuple[int, str | None, str]:
    """Run the real Kimi Codex wrapper in-process against a fake ``codex``.

    Returns the exit status, the final proof.md text (if any) and stderr.
    """
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        install_fake_codex(root / "bin", body)
        workspace = root / "workspace"
        workspace.mkdir()
        stderr = io.StringIO()
        os.chdir(workspace)
        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": f"{root / 'bin'}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
                        "KIMI_API_KEY": "test-key-not-real",
                        "KIMI_API_BASE": "http://127.0.0.1:9/v1",
                        "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
                        "AGENT_MONITOR_CODEX_TIMEOUT": timeout,
                    },
                ),
                patch.object(sys, "argv", ["kimi_codex_runner.py", "Prove that a sum of evens is even."]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                status = kimi_codex_runner.main()
        finally:
            os.chdir(previous)
        proof = workspace / "proof.md"
        text = proof.read_text(encoding="utf-8") if proof.exists() else None
        return status, text, stderr.getvalue()


class KimiResponsesReliabilityTests(unittest.TestCase):
    def test_relay_uses_read1_and_flushes_each_available_chunk(self) -> None:
        upstream = Mock()
        upstream.read1.side_effect = [b"data: first\n\n", b"data: second\n\n", b""]
        upstream.read.side_effect = AssertionError("buffering read must not be used")
        downstream = Mock()

        kimi_responses_proxy._relay_body(upstream, downstream)

        self.assertEqual(downstream.write.call_count, 2)
        self.assertEqual(downstream.flush.call_count, 2)
        upstream.read.assert_not_called()

    def test_retry_after_allows_only_valid_delta_or_http_date(self) -> None:
        self.assertEqual(kimi_responses_proxy._safe_retry_after("17"), "17")
        date = "Wed, 21 Oct 2015 07:28:00 GMT"
        self.assertEqual(kimi_responses_proxy._safe_retry_after(date), date)
        self.assertIsNone(kimi_responses_proxy._safe_retry_after("soon"))
        self.assertIsNone(kimi_responses_proxy._safe_retry_after("1\r\nX-Evil: yes"))

    def test_upstream_timeout_is_configurable_and_bounded(self) -> None:
        with patch.dict(os.environ, {"KIMI_UPSTREAM_TIMEOUT": "1800"}, clear=False):
            self.assertEqual(kimi_responses_proxy._configured_upstream_timeout(), 1800)
        self.assertEqual(kimi_responses_proxy._configured_upstream_timeout("1"), 30)
        self.assertEqual(kimi_responses_proxy._configured_upstream_timeout("99999"), 7200)
        self.assertEqual(
            kimi_responses_proxy._configured_upstream_timeout("invalid"),
            kimi_responses_proxy._DEFAULT_UPSTREAM_TIMEOUT,
        )


class CodexTerminalReliabilityTests(unittest.TestCase):
    def _call_with_stdout(self, stdout: str) -> CodexResult:
        def fake_run(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    stderr="",
                )
            if command[1:] == ["debug", "models", "--bundled"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"models": [{"slug": "gpt-5.6-sol"}]}),
                    stderr="",
                )
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text("finished", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with (
            tempfile.TemporaryDirectory() as launcher_dir,
            patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex-test"}, clear=True),
            patch.object(
                codex_backend.shutil, "which", return_value=str(Path(launcher_dir) / "codex")
            ),
            patch.object(codex_backend, "_run_codex_process", side_effect=fake_run),
        ):
            # The process itself is mocked; executable resolution still uses
            # a real fixture rather than assuming a developer's CLI install.
            launcher = Path(launcher_dir) / "codex"
            launcher.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            launcher.chmod(0o755)
            return codex_backend.codex_exec("system", "user")

    def test_zero_exit_without_terminal_event_is_failure(self) -> None:
        with self.assertRaisesRegex(CodexBackendError, "without a terminal turn"):
            self._call_with_stdout(json.dumps({"type": "item.completed"}))

    def test_failed_terminal_event_is_failure_even_with_zero_exit(self) -> None:
        failed = {"type": "turn.failed", "error": {"message": "upstream failed"}}
        with self.assertRaisesRegex(CodexBackendError, "turn.failed"):
            self._call_with_stdout(json.dumps(failed))

    def test_completed_terminal_event_succeeds(self) -> None:
        result = self._call_with_stdout(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            )
        )
        self.assertEqual(result.usage, {"input_tokens": 3, "output_tokens": 2})

    def test_timeout_cleanup_terminates_then_kills_process_group(self) -> None:
        process = Mock()
        process.pid = 4321
        process.poll.return_value = None
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("codex", 2),
            ("", ""),
        ]
        with (
            patch.object(codex_backend.os, "name", "posix"),
            patch.object(codex_backend.os, "killpg") as killpg,
        ):
            codex_backend._stop_process_group(process)

        self.assertEqual(
            killpg.call_args_list,
            [
                call(4321, signal.SIGTERM),
                call(4321, signal.SIGKILL),
            ],
        )


class KimiArtifactReliabilityTests(unittest.TestCase):
    def test_stale_preexisting_artifact_does_not_count_as_success(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path("proof.md").write_text(
                    "# Existing\n\nThis is a substantive but stale proof artifact. " * 3,
                    encoding="utf-8",
                )
                with (
                    patch.dict(os.environ, {"AGENT_MONITOR_SELECTED_MODEL": "kimi-k3"}),
                    patch.object(os.sys, "argv", ["kimi_codex_runner.py", "prove it"]),
                    patch.object(
                        kimi_codex_runner,
                        "codex_exec",
                        return_value=CodexResult("done", {}),
                    ),
                ):
                    self.assertEqual(kimi_codex_runner.main(), 1)
            finally:
                os.chdir(previous)

    def test_changed_substantive_artifact_counts_as_success(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path("proof.md").write_text(
                    "# Existing\n\nThis is a substantive but stale proof artifact. " * 3,
                    encoding="utf-8",
                )

                def update(*_args: object, **_kwargs: object) -> CodexResult:
                    Path("proof.md").write_text(
                        "# New proof\n\nA corrected, substantive mathematical argument. " * 4,
                        encoding="utf-8",
                    )
                    return CodexResult("done", {})

                with (
                    patch.dict(os.environ, {"AGENT_MONITOR_SELECTED_MODEL": "kimi-k3"}),
                    patch.object(os.sys, "argv", ["kimi_codex_runner.py", "prove it"]),
                    patch.object(kimi_codex_runner, "codex_exec", side_effect=update),
                ):
                    self.assertEqual(kimi_codex_runner.main(), 0)
            finally:
                os.chdir(previous)


# Kimi K3 can spend its whole output budget on reasoning and end the turn with
# no visible text; codex 0.160 then writes an empty --output-last-message file.
EMPTY_FINAL_MESSAGE = """\
(workspace / "proof.md").write_text(PROOF, encoding="utf-8")
event({"type": "thread.started", "thread_id": "t1"})
event({"type": "turn.started"})
event({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "proof.md", "kind": "add"}]}})
event({"type": "item.completed", "item": {"type": "agent_message", "text": ""}})
event({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
last_message.write_text("")
"""


class KimiCodexEmptyFinalMessageTests(unittest.TestCase):
    def test_a_new_proof_with_an_empty_final_message_succeeds(self) -> None:
        status, proof, stderr = run_kimi_wrapper(EMPTY_FINAL_MESSAGE)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(proof, PROOF)

    def test_an_empty_final_message_without_a_new_proof_still_fails(self) -> None:
        status, proof, stderr = run_kimi_wrapper(
            EMPTY_FINAL_MESSAGE.replace('(workspace / "proof.md").write_text(PROOF, encoding="utf-8")\n', "")
        )
        self.assertEqual((status, proof), (1, None))
        self.assertIn("without a new substantive proof.md", stderr)

    def test_a_failed_turn_fails_even_with_a_proof_on_disk(self) -> None:
        status, _, stderr = run_kimi_wrapper(
            """\
            (workspace / "proof.md").write_text(PROOF, encoding="utf-8")
            event({"type": "thread.started", "thread_id": "t1"})
            event({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}})
            last_message.write_text("")
            sys.exit(1)
            """
        )
        self.assertEqual(status, 1)
        self.assertIn("stream disconnected before completion", stderr)

    def test_other_callers_still_reject_an_empty_answer(self) -> None:
        # Callers that need the answer text (sidecars, news, analysis) keep
        # failing closed: only the wrapper opts in to an empty final message.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install_fake_codex(root / "bin", EMPTY_FINAL_MESSAGE)
            (root / "workspace").mkdir()
            with self.assertRaisesRegex(CodexBackendError, "empty response"):
                codex_backend.codex_exec(
                    "system",
                    "user",
                    model="kimi-k3",
                    workspace=root / "workspace",
                    sandbox="danger-full-access",
                    enable_tools=True,
                    source_env={
                        "PATH": f"{root / 'bin'}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
                        "KIMI_API_KEY": "test-key-not-real",
                        "KIMI_API_BASE": "http://127.0.0.1:9/v1",
                    },
                )


if __name__ == "__main__":
    unittest.main()
