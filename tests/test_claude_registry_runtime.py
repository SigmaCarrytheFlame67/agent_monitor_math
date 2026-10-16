from __future__ import annotations

import contextlib
import io
import os
import stat
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_monitor import jobs
from agent_monitor.engines_registry import (
    CLAUDE_SUBSCRIPTION_ENGINES,
    CLI_ENGINES,
    ENGINE_AUTH_MODES,
    build_cli_command,
    list_engines,
)
from agent_monitor.runners import (
    claude_runner,
    deepagents_runner,
    kimi_runner,
    metaharness_runner,
    plain_runner,
)
from agent_monitor.runners import math_harness_runner as harness


class ClaudeRegistryRuntimeTests(unittest.TestCase):
    def test_claude_is_a_dedicated_subscription_only_engine(self) -> None:
        spec = CLI_ENGINES["claude"]
        self.assertEqual(spec["parser_style"], "claude")
        self.assertEqual(
            spec["supported_models"],
            ["sonnet", "opus", "fable", "claude-haiku-4-5"],
        )
        self.assertEqual(ENGINE_AUTH_MODES["claude"], ["claude_subscription"])
        self.assertIn("claude", CLAUDE_SUBSCRIPTION_ENGINES)

        with patch(
            "agent_monitor.engines_registry.which_tool", return_value="/bin/claude"
        ), tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            command = build_cli_command(
                "claude",
                prompt="Prove 1 + 1 = 2.",
                workspace=workspace,
                problem_file=workspace / "problem.txt",
            )
            by_id = {entry["id"]: entry for entry in list_engines()}
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command[0], sys.executable)
        self.assertIn("claude_runner.py", command[2])
        self.assertEqual(by_id["claude"]["auth_modes"], ["claude_subscription"])
        self.assertTrue(by_id["claude"]["claude_subscription_supported"])
        self.assertFalse(by_id["claude"]["subscription_supported"])

    def test_the_task_prompt_reaches_the_runner_in_a_private_file(self) -> None:
        # One argv string is limited to 128 KiB on Linux: 50,000 characters
        # of a 3-byte script are 150 KB.
        prompt = "Prove it.\r\n" + "证明：" * 50_000
        with patch(
            "agent_monitor.engines_registry.which_tool", return_value="/bin/claude"
        ), patch.dict(os.environ), tempfile.TemporaryDirectory() as td:
            os.environ.pop("CLAUDE_CMD", None)
            workspace = Path(td)
            command = build_cli_command(
                "claude", prompt=prompt, workspace=workspace, problem_file=workspace / "problem.txt"
            )
            assert command is not None
            self.assertFalse(any("证明" in arg for arg in command))
            self.assertLess(max(len(arg.encode("utf-8")) for arg in command), 4096)
            path = Path(command[command.index("--prompt-file") + 1])
            self.assertTrue(path.is_absolute())
            self.assertEqual(path.parent.resolve(), workspace.resolve())
            # A dot-file: the run's file list and export never show it.
            self.assertTrue(path.name.startswith("."))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), prompt.encode("utf-8"))

    def test_the_runner_reads_then_removes_its_prompt_file(self) -> None:
        prompts: list[str] = []

        def proof(prompt, *_args, **_kwargs):
            prompts.append(prompt)

        with tempfile.TemporaryDirectory() as td, patch.object(claude_runner, "claude_proof", side_effect=proof):
            path = Path(td) / ".prompt"
            path.write_bytes("Prove it.\r\n证明".encode("utf-8"))
            with patch.object(sys, "argv", ["claude_runner.py", "--prompt-file", str(path)]):
                self.assertEqual(claude_runner.main(), 0)
            self.assertFalse(path.exists())
            # An operator's CLAUDE_CMD that still passes {prompt} keeps working.
            with patch.object(sys, "argv", ["claude_runner.py", "Prove that."]):
                self.assertEqual(claude_runner.main(), 0)
        self.assertEqual(prompts, ["Prove it.\r\n证明", "Prove that."])

    def test_whatever_the_agent_left_under_the_prompt_file_name_is_replaced(self) -> None:
        # The workspace is the agent's: an earlier turn of the run may have
        # made a directory, or a link, where the next prompt file goes.
        with patch(
            "agent_monitor.engines_registry.which_tool", return_value="/bin/claude"
        ), patch.dict(os.environ), tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            os.environ.pop("CLAUDE_CMD", None)
            (Path(outside) / "keep.txt").write_text("keep", encoding="utf-8")
            for name, leave in (
                ("directory", lambda path: (path / "notes").mkdir(parents=True)),
                ("link to a directory", lambda path: path.symlink_to(outside, target_is_directory=True)),
            ):
                with self.subTest(name):
                    workspace = Path(td) / name.replace(" ", "-")
                    workspace.mkdir()
                    leave(workspace / ".agent-monitor-prompt")
                    command = build_cli_command(
                        "claude", prompt="Prove it.", workspace=workspace, problem_file=workspace / "problem.txt"
                    )
                    assert command is not None
                    path = Path(command[command.index("--prompt-file") + 1])
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(path.read_text(encoding="utf-8"), "Prove it.")
            self.assertEqual(sorted(p.name for p in Path(outside).iterdir()), ["keep.txt"])


class WrapperPromptFileTests(unittest.TestCase):
    """The Python wrappers that also run on a Claude Code or Codex account
    (and Kimi) get their prompt in a file too."""

    # One argv string is limited to 128 KiB on Linux: 50,000 characters of a
    # 3-byte script are 150 KB.
    PROMPT = "Prove it.\r\n" + "证明：" * 50_000

    def test_the_registry_hands_each_wrapper_a_long_prompt_in_a_private_file(self) -> None:
        for engine in ("plain", "metaharness", "deepagents", "kimi"):
            with self.subTest(engine=engine), patch(
                "agent_monitor.engines_registry._cli_available",
                return_value=(True, CLI_ENGINES[engine]["default_cmd"]),
            ), tempfile.TemporaryDirectory() as td:
                workspace = Path(td)
                command = build_cli_command(
                    engine, prompt=self.PROMPT, workspace=workspace, problem_file=workspace / "problem.txt"
                )
                assert command is not None
                self.assertLess(max(len(arg.encode("utf-8")) for arg in command), 4096)
                path = Path(command[command.index("--prompt-file") + 1])
                self.assertEqual(path.parent.resolve(), workspace.resolve())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.read_bytes(), self.PROMPT.encode("utf-8"))

    def _main(self, runner: types.ModuleType, workspace: Path) -> int:
        """``runner.main()`` started in ``workspace`` as the registry starts it."""
        path = workspace / ".agent-monitor-prompt"
        path.write_bytes(self.PROMPT.encode("utf-8"))
        argv = [Path(runner.__file__).name, "--prompt-file", str(path)]
        with contextlib.chdir(workspace), patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            returncode = runner.main()
        self.assertFalse(path.exists())  # read, then removed
        return returncode

    def test_plain_reads_its_prompt_file(self) -> None:
        prompts: list[str] = []

        def claude_exec(_system, user, **_kwargs):
            prompts.append(user)
            return SimpleNamespace(text="# Proof\n\nIt holds.\n", usage={})

        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True), patch.object(
            plain_runner, "subscription_enabled", return_value=False
        ), patch.object(plain_runner, "claude_subscription_enabled", return_value=True), patch.object(
            plain_runner, "claude_code_exec", side_effect=claude_exec
        ):
            self.assertEqual(self._main(plain_runner, Path(td)), 0)
        self.assertEqual(prompts, [self.PROMPT])

    def test_metaharness_reads_its_prompt_file(self) -> None:
        prompts: list[str] = []

        def claude_exec(_system, user, **_kwargs):
            prompts.append(user)
            return SimpleNamespace(text="SCORE: 9\n\n# Proof\n\nIt holds.\n", usage={})

        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True), patch.object(
            metaharness_runner, "subscription_enabled", return_value=False
        ), patch.object(metaharness_runner, "claude_subscription_enabled", return_value=True), patch.object(
            metaharness_runner, "claude_code_exec", side_effect=claude_exec
        ):
            self.assertEqual(self._main(metaharness_runner, Path(td)), 0)
        self.assertEqual(prompts[0], self.PROMPT)  # the solver's task

    def test_deepagents_reads_its_prompt_file(self) -> None:
        requests: list[str] = []

        class Agent:
            def stream(self, state, **_kwargs):
                requests.append(state["messages"][0]["content"])
                yield {"messages": []}

        deepagents = types.ModuleType("deepagents")
        deepagents.create_deep_agent = lambda **_kwargs: Agent()
        filesystem = types.ModuleType("deepagents.backends.filesystem")
        filesystem.FilesystemBackend = lambda **_kwargs: None
        stubs = {"deepagents": deepagents, "deepagents.backends": types.ModuleType("deepagents.backends"),
                 "deepagents.backends.filesystem": filesystem}
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1", "CLAUDE_CONFIG_DIR": td}, clear=True
        ), patch.dict(sys.modules, stubs), patch.object(deepagents_runner, "_install_termination_handlers"), patch.object(
            deepagents_runner, "_create_claude_subscription_model", return_value=object()
        ), patch.object(deepagents_runner, "_run_supplemental_audit"):
            self._main(deepagents_runner, Path(td))  # the stub agent writes no proof
        self.assertEqual(len(requests), 1)
        self.assertIn("证明：" * 50_000, requests[0])

    def test_kimi_reads_its_prompt_file(self) -> None:
        tasks: list[str] = []

        def pipeline(_root, _problem, _route, *, budgets, task):
            tasks.append(task)
            return {"written": [], "fact_ids": [], "accepted": False, "proof_text": ""}

        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"KIMI_API_KEY": "sk-test-kimi-123456789"}, clear=True
        ), patch.object(harness, "run_pipeline", side_effect=pipeline):
            (Path(td) / "problem.txt").write_text("Prove it.", encoding="utf-8")
            self.assertEqual(self._main(kimi_runner, Path(td)), 0)
        self.assertEqual(tasks, [self.PROMPT])

    def test_the_console_starts_kimi_with_a_long_prompt(self) -> None:
        # The registry's own command, run as a script: with no key in the
        # run's environment the runner stops right after reading its prompt.
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ), patch(
            "agent_monitor.agent_config.persona_preamble", return_value=""
        ), patch.object(jobs, "_write_run"):
            os.environ.pop("KIMI_PROOF_CMD", None)
            run = jobs._run_cli_engine(run_id="kimi_long", engine="kimi", problem_id="p",
                                       problem_text=self.PROMPT, started=time.time(), workspace=Path(td))
            leftovers = list(Path(td).iterdir())
        self.assertEqual(run["status"], "failed")
        self.assertNotIn("too large", run["error"])
        self.assertIn("Add a Kimi API key in Settings", str(run["agents"]))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
