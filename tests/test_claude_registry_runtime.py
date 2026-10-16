from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor.engines_registry import (
    CLAUDE_SUBSCRIPTION_ENGINES,
    CLI_ENGINES,
    ENGINE_AUTH_MODES,
    build_cli_command,
    list_engines,
)
from agent_monitor.runners import claude_runner


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


if __name__ == "__main__":
    unittest.main()
