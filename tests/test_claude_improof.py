"""Offline regressions for IMProof's Claude Code Author/Critic route."""
from __future__ import annotations

import io
import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from agent_monitor import jobs
from agent_monitor.runners import improof
from agent_monitor.runners import improof_audit_adapter as audit_adapter
from agent_monitor.runners import improof_claude_worker as worker


CLAUDE_MODEL = "claude-haiku-4-5"


class IMProofClaudeWorkerTests(unittest.TestCase):
    def test_worker_environment_is_oauth_only(self) -> None:
        source = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "AGENT_MONITOR_CLAUDE_MODEL": CLAUDE_MODEL,
            "CLAUDE_CONFIG_DIR": "/private/account",
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "must-not-pass",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "OPENAI_API_KEY": "must-not-pass",
        }
        selected = worker._source_environment(source)
        self.assertEqual(selected["AGENT_MONITOR_CLAUDE_MODEL"], CLAUDE_MODEL)
        self.assertEqual(selected["CLAUDE_CONFIG_DIR"], "/private/account")
        self.assertNotIn("ANTHROPIC_API_KEY", selected)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", selected)
        self.assertNotIn("OPENAI_API_KEY", selected)

    def test_author_worker_uses_exact_model_without_tools(self) -> None:
        result = SimpleNamespace(text="A rigorous self-contained proof body.")
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            with (
                patch.object(worker.Path, "cwd", return_value=workspace),
                patch.object(worker.sys, "stdin", io.StringIO("Prove the claim.")),
                patch.object(worker, "claude_exec", return_value=result) as claude,
                patch.dict(
                    os.environ,
                    {
                        "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                        "AGENT_MONITOR_CLAUDE_MODEL": CLAUDE_MODEL,
                        "CLAUDE_CONFIG_DIR": str(workspace),
                        "ANTHROPIC_API_KEY": "must-not-pass",
                    },
                    clear=True,
                ),
            ):
                self.assertEqual(
                    worker.main(
                        ["--role", "author", "--output", "answer.tex", "--model", CLAUDE_MODEL]
                    ),
                    0,
                )
            self.assertEqual(
                (workspace / "answer.tex").read_text(encoding="utf-8").strip(),
                result.text,
            )
        kwargs = claude.call_args.kwargs
        self.assertEqual(kwargs["model"], CLAUDE_MODEL)
        self.assertFalse(kwargs["enable_tools"])
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["source_env"])

    def test_failed_turn_leaves_no_previous_round_output(self) -> None:
        # ProofStack collects critique.md whatever the exit status, so a
        # failed round must not leave the previous round's report behind.
        failure = worker.ClaudeBackendError(
            "Claude Code failed: You've hit your session limit", kind="usage_limit"
        )
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "critique.md").write_text("Round 1 report.\n", encoding="utf-8")
            with (
                patch.object(worker.Path, "cwd", return_value=workspace),
                patch.object(worker.sys, "stdin", io.StringIO("Review the proof.")),
                patch.object(worker.sys, "stderr", stderr),
                patch.object(worker, "claude_exec", side_effect=failure),
                patch.dict(
                    os.environ,
                    {
                        "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                        "CLAUDE_CONFIG_DIR": str(workspace),
                    },
                    clear=True,
                ),
            ):
                self.assertEqual(
                    worker.main(["--role", "critic", "--output", "critique.md"]), 1
                )
            self.assertFalse((workspace / "critique.md").exists())
        self.assertIn("session limit", stderr.getvalue())


def _cli_exit(agent: str, **payload) -> dict:
    return {"kind": "cli.exit", "agent": agent, "payload": payload}


_SESSION_LIMIT = "Claude Code failed: You've hit your session limit · resets 11pm (UTC)"


class IMProofClaudeWorkerFailureTests(unittest.TestCase):
    """A failed Author/Critic call must fail the run with Claude's reason."""

    def _run(
        self,
        events: list[dict],
        *,
        returncode: int = 0,
        draft: bool = True,
        blobs: dict[str, str] | None = None,
    ):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        problem = root / "problem.txt"
        problem.write_text("Prove that one plus one equals two.", encoding="utf-8")
        output = root / "out"

        def fake_stream(command, **kwargs):
            native = Path(
                next(
                    value.split("=", 1)[1]
                    for value in command
                    if value.startswith("workspace=")
                )
            )
            if draft:
                # The round-1 draft that later failed calls leave in place,
                # stashed as the final solution.
                proof = (
                    "\\begin{document}\n\\section*{Proof}\n"
                    + "A rigorous contradiction establishes the exact claim. " * 12
                    + "\n\\end{document}\n"
                )
                (native / "answer.tex").write_text(proof, encoding="utf-8")
                (native / "solutions").mkdir(parents=True, exist_ok=True)
                (native / "solutions" / "demo.tex").write_text(proof, encoding="utf-8")
            (native / "run-metadata.json").write_text(
                json.dumps({"status": "ok", "outputs": {"solution": ""}}),
                encoding="utf-8",
            )
            (native / "events.jsonl").write_text(
                "not json\n" + "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            for name, text in (blobs or {}).items():
                (native / "events_blobs").mkdir(exist_ok=True)
                (native / "events_blobs" / name).write_text(text, encoding="utf-8")
            return "workflow output", returncode, False

        selected_env = {
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "AGENT_MONITOR_CLAUDE_MODEL": CLAUDE_MODEL,
            "CLAUDE_CONFIG_DIR": str(root),
        }
        with (
            patch(
                "agent_monitor.claude_login.account_environment",
                return_value=selected_env,
            ),
            patch("agent_monitor.runners._stream.stream_subprocess", side_effect=fake_stream),
        ):
            result = improof.run_problem(
                problem,
                problem_id="demo",
                output_dir=output,
                extra_env=selected_env,
            )
        return result, output

    def test_failed_critic_after_round_one_fails_instead_of_publishing_the_draft(self) -> None:
        result, output = self._run(
            [
                _cli_exit("cfg_claude_author", exit_code=0, status="done", stderr_tail=""),
                _cli_exit(
                    "cfg_claude_critic",
                    exit_code=1,
                    status="error",
                    stderr_tail=f"IMProof Claude critic failed: {_SESSION_LIMIT}\n",
                ),
                _cli_exit(
                    "cfg_claude_author",
                    exit_code=1,
                    status="error",
                    stderr_tail=f"IMProof Claude author failed: {_SESSION_LIMIT}\n",
                ),
            ]
        )

        self.assertEqual(result["status"], "failed")
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["error"], f"IMProof Claude critic failed: {_SESSION_LIMIT}")
        self.assertIn(_SESSION_LIMIT, result["stdout_tail"])
        self.assertFalse((output / "proof.tex").exists())

    def test_every_call_failing_reports_claude_reason_not_missing_artifact(self) -> None:
        result, _output = self._run(
            [
                _cli_exit(
                    "cfg_claude_author",
                    exit_code=1,
                    status="error",
                    stderr_tail=f"IMProof Claude author failed: {_SESSION_LIMIT}\n",
                ),
            ],
            draft=False,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], f"IMProof Claude author failed: {_SESSION_LIMIT}")

    def test_timed_out_call_without_exit_code_fails_the_run(self) -> None:
        result, output = self._run(
            [_cli_exit("cfg_claude_critic", status="partial", reason="timeout")]
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("critic", result["error"])
        self.assertIn("timed out", result["error"])
        self.assertFalse((output / "proof.tex").exists())

    def test_failed_workflow_process_still_names_the_claude_failure(self) -> None:
        result, _output = self._run(
            [
                _cli_exit(
                    "cfg_claude_author",
                    exit_code=1,
                    status="error",
                    stderr_tail="IMProof Claude author failed: Claude Code timed out after 600s\n",
                ),
            ],
            returncode=1,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(
            result["error"], "IMProof Claude author failed: Claude Code timed out after 600s"
        )

    def test_spilled_stderr_tail_is_read_from_its_blob(self) -> None:
        # ProofStack moves payload strings over 2 KiB to events_blobs/.
        stderr = "warning: noisy setup\n" * 120 + f"IMProof Claude author failed: {_SESSION_LIMIT}\n"
        result, _output = self._run(
            [
                _cli_exit(
                    "cfg_claude_author",
                    exit_code=1,
                    status="error",
                    stderr_tail={"$ref": "events_blobs/author-stderr.txt"},
                ),
            ],
            blobs={"author-stderr.txt": stderr},
        )

        self.assertEqual(result["error"], f"IMProof Claude author failed: {_SESSION_LIMIT}")

    def test_successful_calls_keep_the_run_finished(self) -> None:
        result, output = self._run(
            [
                _cli_exit("cfg_claude_author", exit_code=0, status="done", stderr_tail=""),
                _cli_exit("cfg_claude_critic", exit_code=0, status="done", stderr_tail=""),
            ]
        )

        self.assertEqual(result["status"], "finished")
        self.assertIsNone(result.get("error"))
        self.assertTrue((output / "proof.tex").is_file())


def _process_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rpartition(") ")[2].split()[0] != "Z"


class IMProofClaudePlannerTimeoutTests(unittest.TestCase):
    def test_planner_timeout_also_stops_the_claude_cli(self) -> None:
        # The worker starts the CLI in its own session, out of reach of a
        # kill of the worker's process group.
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir, home, config, workspace = (
            root / name for name in ("bin", "home", "config", "workspace")
        )
        for directory in (bin_dir, home, config, workspace):
            directory.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(
            '#!/bin/sh\necho $$ > "$HOME/claude.pid"\nexec sleep 120\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        pid_file = home / "claude.pid"

        def kill_leftover() -> None:
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
            except (OSError, ValueError):
                pass

        self.addCleanup(kill_leftover)
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
            "HOME": str(home),
            "TMPDIR": str(root),
            "CLAUDE_CONFIG_DIR": str(config),
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        }
        with patch.object(improof, "_PLANNER_TIMEOUT_S", 3):
            packet = improof._research_packet(
                problem_text="Prove that one plus one equals two.",
                workspace=workspace,
                env=env,
                model=CLAUDE_MODEL,
            )

        self.assertIn("query planner timed out", packet)
        self.assertTrue(pid_file.is_file(), "the fake Claude CLI never started")
        pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while _process_running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_process_running(pid), "the Claude CLI outlived the planner timeout")


class IMProofClaudeWorkflowTests(unittest.TestCase):
    def test_workflow_preserves_author_critic_and_final_author_dag(self) -> None:
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / "engines/improof/configs/workflows/claude_author_critic.yaml"
        )
        document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        components = document["components"]
        self.assertEqual(set(components), {"cfg_claude_author", "cfg_claude_critic"})
        for component in components.values():
            self.assertEqual(component["model"], CLAUDE_MODEL)
            self.assertEqual(component["sandbox"]["provider_keys"], [])
            allowlist = component["sandbox"]["env_allowlist"]
            self.assertIn("CLAUDE_CONFIG_DIR", allowlist)
            self.assertNotIn("ANTHROPIC_API_KEY", allowlist)
            self.assertIn("agent_monitor.runners.improof_claude_worker", component["cmd"])
        nodes = document["dag"]["nodes"]
        self.assertEqual([node["id"] for node in nodes], ["author_critic_loop", "final_revision"])
        body = nodes[0]["body"]["nodes"]
        self.assertEqual([node["id"] for node in body], ["author", "critic"])
        self.assertEqual(nodes[1]["name"], "cfg_claude_author")

    def test_runner_selects_native_claude_workflow_and_promotes_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            problem = root / "problem.txt"
            problem.write_text("Prove that one plus one equals two.", encoding="utf-8")
            output = root / "out"

            def fake_stream(command, **kwargs):
                inputs = [
                    command[index + 1]
                    for index, value in enumerate(command[:-1])
                    if value == "--input"
                ]
                workspace_value = next(
                    value for value in inputs if value.startswith("workspace=")
                )
                native = Path(workspace_value.split("=", 1)[1])
                (native / "solutions").mkdir(parents=True, exist_ok=True)
                proof = (
                    "\\begin{document}\n\\section*{Proof}\n"
                    + "A rigorous contradiction establishes the exact claim. " * 12
                    + "\n\\end{document}\n"
                )
                (native / "solutions" / "demo.tex").write_text(proof, encoding="utf-8")
                (native / "run-metadata.json").write_text(
                    json.dumps({"status": "ok", "outputs": {"solution": proof}}),
                    encoding="utf-8",
                )
                return "", 0, False

            selected_env = {
                "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                "AGENT_MONITOR_CLAUDE_MODEL": CLAUDE_MODEL,
                "CLAUDE_CONFIG_DIR": str(root),
            }
            with (
                patch(
                    "agent_monitor.claude_login.account_environment",
                    return_value=selected_env,
                ),
                patch("agent_monitor.runners._stream.stream_subprocess", side_effect=fake_stream),
            ):
                result = improof.run_problem(
                    problem,
                    problem_id="demo",
                    output_dir=output,
                    extra_env=selected_env,
                )

            self.assertEqual(result["status"], "finished")
            self.assertEqual(result["workflow"], improof.CLAUDE_WORKFLOW)
            self.assertTrue((output / "proof.tex").is_file())
            self.assertNotIn("ANTHROPIC_API_KEY", selected_env)

    def test_hitl_continuation_propagates_selected_model_to_both_roles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            captured = {}

            def fake_run(*args, **kwargs):
                captured.update(kwargs)
                return {"status": "finished", "returncode": 0}

            with (
                patch("agent_monitor.runners.improof.run_problem", side_effect=fake_run),
                patch.object(jobs, "_wrap_subprocess_result", return_value={"status": "finished"}),
                patch.object(jobs, "_live_output_flusher", return_value=lambda _line: None),
            ):
                result = jobs._run_improof_continuation(
                    run_id="improof_demo",
                    job_id="job1",
                    problem_id="demo",
                    problem_text="Strengthen the proof.",
                    display_problem_text="Original problem.",
                    model=CLAUDE_MODEL,
                    started=0.0,
                    workspace=workspace,
                    extra_env={"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1"},
                )

            self.assertEqual(result["status"], "finished")
            self.assertEqual(
                captured["extra_args"],
                [
                    "--component",
                    f"cfg_claude_author.model={CLAUDE_MODEL}",
                    "--component",
                    f"cfg_claude_critic.model={CLAUDE_MODEL}",
                ],
            )
            self.assertEqual(captured["research_model"], CLAUDE_MODEL)


class IMProofClaudeAuditTests(unittest.TestCase):
    def test_audit_call_stays_on_claude_and_projects_events(self) -> None:
        emitted = []
        response = SimpleNamespace(text="audited", usage={})
        with (
            patch.object(audit_adapter, "claude_exec", return_value=response) as claude,
            patch.object(audit_adapter, "forward_as_codex_event") as project,
        ):
            actual = audit_adapter._claude_call(
                "system",
                "candidate",
                model=CLAUDE_MODEL,
                emit_event=emitted.append,
            )
            self.assertIs(actual, response)
            kwargs = claude.call_args.kwargs
            self.assertEqual(kwargs["model"], CLAUDE_MODEL)
            self.assertFalse(kwargs["enable_tools"])
            event = {"type": "assistant"}
            kwargs["emit_event"](event)
            project.assert_called_once_with(event, emitted.append)


if __name__ == "__main__":
    unittest.main()
