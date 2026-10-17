"""Offline coverage for DeepAgents' Claude subscription chat adapter."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs
from agent_monitor.cli_events import CLIEventParser
from agent_monitor.runners import deepagents_runner


_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write one virtual file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}


class ClaudeDeepAgentsContractTests(unittest.TestCase):
    def test_strict_parser_accepts_typed_call_and_rejects_extra_argument(self) -> None:
        accepted = deepagents_runner._parse_claude_deepagents_response(
            json.dumps(
                {
                    "kind": "tool_calls",
                    "text": "",
                    "tool_calls": [
                        {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "/proof.md", "content": "proof"}
                            ),
                        }
                    ],
                }
            ),
            [_TOOL],
        )
        self.assertEqual(accepted["tool_calls"][0]["name"], "write_file")

        with self.assertRaisesRegex(ValueError, "unexpected field"):
            deepagents_runner._parse_claude_deepagents_response(
                json.dumps(
                    {
                        "kind": "tool_calls",
                        "text": "",
                        "tool_calls": [
                            {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": "/proof.md",
                                        "content": "proof",
                                        "outside": True,
                                    }
                                ),
                            }
                        ],
                    }
                ),
                [_TOOL],
            )

    def test_strict_parser_canonicalizes_schema_valid_object_arguments(self) -> None:
        accepted = deepagents_runner._parse_claude_deepagents_response(
            json.dumps(
                {
                    "kind": "tool_calls",
                    "text": "",
                    "tool_calls": [
                        {
                            "name": "write_file",
                            "arguments": {
                                "path": "/proof.md",
                                "content": "proof",
                            },
                        }
                    ],
                }
            ),
            [_TOOL],
        )
        canonical = accepted["tool_calls"][0]["arguments"]
        self.assertIsInstance(canonical, str)
        self.assertEqual(
            json.loads(canonical),
            {"path": "/proof.md", "content": "proof"},
        )

        with self.assertRaisesRegex(ValueError, "unexpected field"):
            deepagents_runner._parse_claude_deepagents_response(
                json.dumps(
                    {
                        "kind": "tool_calls",
                        "text": "",
                        "tool_calls": [
                            {
                                "name": "write_file",
                                "arguments": {
                                    "path": "/proof.md",
                                    "content": "proof",
                                    "outside": True,
                                },
                            }
                        ],
                    }
                ),
                [_TOOL],
            )

    def test_text_recovery_contract_is_exact(self) -> None:
        parsed = deepagents_runner._parse_claude_deepagents_response(
            '{"text":"self-contained fallback"}', [], text_only=True
        )
        self.assertEqual(parsed["kind"], "text")
        with self.assertRaisesRegex(ValueError, "exactly"):
            deepagents_runner._parse_claude_deepagents_response(
                '{"text":"x","tool_calls":[]}', [], text_only=True
            )

    def test_parser_accepts_only_a_single_whole_json_fence(self) -> None:
        payload = json.dumps(
            {"kind": "text", "text": "complete proof", "tool_calls": []}
        )
        parsed = deepagents_runner._parse_claude_deepagents_response(
            f"```json\n{payload}\n```", []
        )
        self.assertEqual(parsed["text"], "complete proof")

        for invalid in (
            f"preface\n```json\n{payload}\n```",
            f"```javascript\n{payload}\n```",
            f"```json\n{payload}\n```\ntrailer",
        ):
            with self.subTest(invalid=invalid[:20]):
                with self.assertRaisesRegex(
                    ValueError, "one exact JSON object"
                ):
                    deepagents_runner._parse_claude_deepagents_response(
                        invalid, []
                    )

    def test_codex_and_claude_routes_fail_before_model_import(self) -> None:
        output = io.StringIO()
        with (
            patch.object(deepagents_runner, "_install_termination_handlers"),
            patch.object(sys, "argv", ["deepagents_runner.py", "prove"]),
            patch.dict(
                os.environ,
                {
                    "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                    "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
                },
                clear=True,
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(deepagents_runner.main(), 2)
        self.assertIn("cannot both be active", output.getvalue())

    def test_isolated_langchain_adapter_repair_usage_and_no_tool_access(self) -> None:
        root = Path(__file__).resolve().parents[1]
        python = root / "engines" / "deepagents" / ".venv" / "bin" / "python"
        if not python.is_file():
            self.skipTest("DeepAgents isolated environment is not installed")
        script = textwrap.dedent(
            r'''
            import json
            import os
            import tempfile
            from pathlib import Path

            from langchain_core.messages import HumanMessage, ToolMessage
            from deepagents import create_deep_agent
            from deepagents.backends.filesystem import FilesystemBackend

            from agent_monitor.runners import claude_backend, deepagents_runner

            canonical = "claude-haiku-4-5-20251001"
            tool = {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write one virtual file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                },
            }
            calls = []
            responses = [
                json.dumps({
                    "kind": "tool_calls",
                    "text": "",
                    "tool_calls": [{"name": "write_file", "arguments": "{}"}],
                }),
                json.dumps({
                    "kind": "tool_calls",
                    "text": "",
                    "tool_calls": [{
                        "name": "write_file",
                        "arguments": json.dumps({
                            "path": "/proof.md",
                            "content": "complete proof",
                        }),
                    }],
                }),
            ]

            def fake_exec(system, user, **kwargs):
                calls.append((system, user, kwargs))
                return claude_backend.ClaudeResult(
                    text=responses.pop(0),
                    usage={"input_tokens": 2, "output_tokens": 3},
                    model=canonical,
                    cost_usd=None,
                    events=(),
                    terminal_event={},
                )

            claude_backend.claude_exec = fake_exec
            with tempfile.TemporaryDirectory() as workspace:
                os.environ["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"] = "1"
                os.environ["AGENT_MONITOR_CLAUDE_MODEL"] = "claude-haiku-4-5"
                os.environ["CLAUDE_CONFIG_DIR"] = workspace
                model = deepagents_runner._create_claude_subscription_model(
                    Path(workspace), "anthropic:claude-haiku-4-5"
                )
                assert model._get_ls_params()["ls_provider"] == "anthropic"
                assert model._get_ls_params()["ls_model_name"] == "claude-haiku-4-5"
                graph = create_deep_agent(
                    model=model,
                    tools=[],
                    backend=FilesystemBackend(
                        root_dir=workspace, virtual_mode=True
                    ),
                )
                assert graph is not None
                result = model._generate(
                    [HumanMessage(content="write the proof")], tools=[tool]
                )
                message = result.generations[0].message
                assert len(calls) == 2, calls
                assert message.tool_calls[0]["name"] == "write_file"
                assert message.tool_calls[0]["args"]["path"] == "/proof.md"
                assert message.usage_metadata == {
                    "input_tokens": 4,
                    "output_tokens": 6,
                    "total_tokens": 10,
                }
                assert message.response_metadata["provider"] == "anthropic"
                assert message.response_metadata["requested_model"] == (
                    "claude-haiku-4-5"
                )
                for _, _, kwargs in calls:
                    assert kwargs["model"] == "claude-haiku-4-5"
                    assert kwargs["enable_tools"] is False
                    assert kwargs["workspace"] == Path(workspace)
                    assert kwargs["source_env"] is os.environ

                calls.clear()
                responses.extend([
                    "not-json",
                    json.dumps({
                        "kind": "tool_calls",
                        "text": "",
                        "tool_calls": [{
                            "name": "write_file",
                            "arguments": "{}",
                        }],
                    }),
                    json.dumps({"text": "safe text fallback"}),
                ])
                recovered = model._generate(
                    [HumanMessage(content="answer directly")], tools=[tool]
                ).generations[0].message
                assert len(calls) == 3, calls
                assert recovered.content == "safe text fallback"
                assert recovered.tool_calls == []
                assert recovered.response_metadata["attempts"] == 3

                calls.clear()
                responses.extend([
                    json.dumps({
                        "kind": "text",
                        "text": "Proof.lean is complete and compiles.",
                        "tool_calls": [],
                    }),
                    json.dumps({
                        "kind": "tool_calls",
                        "text": "",
                        "tool_calls": [{
                            "name": "write_file",
                            "arguments": {
                                "path": "/proof.md",
                                "content": "formal artifact",
                            },
                        }],
                    }),
                ])
                os.environ["AGENT_MONITOR_CLAUDE_LEAN_MODE"] = "1"
                formal = model._generate(
                    [HumanMessage(content="formalize and compile")], tools=[tool]
                ).generations[0].message
                assert len(calls) == 2, calls
                assert formal.tool_calls[0]["name"] == "write_file"
                assert formal.tool_calls[0]["args"] == {
                    "path": "/proof.md",
                    "content": "formal artifact",
                }
                assert 'tool_choice: "required"' in calls[0][1]
                assert "kind=text is still invalid" in calls[1][1]

                calls.clear()
                responses.append(json.dumps({
                    "kind": "text",
                    "text": "The actual Lean checker passed.",
                    "tool_calls": [],
                }))
                completed = model._generate(
                    [
                        HumanMessage(content="formalize and compile"),
                        ToolMessage(
                            content="exit 0\ncompiled",
                            tool_call_id="call_lean",
                            name="lean_check",
                        ),
                    ],
                    tools=[tool],
                ).generations[0].message
                assert len(calls) == 1, calls
                assert completed.content == "The actual Lean checker passed."
                assert completed.tool_calls == []
                assert 'tool_choice: null' in calls[0][1]
                os.environ.pop("AGENT_MONITOR_CLAUDE_LEAN_MODE", None)
            '''
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root)
        completed = subprocess.run(
            [str(python), "-c", script],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )


def _deepagents_stub_modules(agent) -> dict[str, types.ModuleType]:
    """The DeepAgents packages main() imports (not installed here)."""
    deepagents = types.ModuleType("deepagents")
    deepagents.create_deep_agent = lambda **_kwargs: agent
    filesystem = types.ModuleType("deepagents.backends.filesystem")
    filesystem.FilesystemBackend = lambda **_kwargs: None
    return {
        "deepagents": deepagents,
        "deepagents.backends": types.ModuleType("deepagents.backends"),
        "deepagents.backends.filesystem": filesystem,
    }


class _SilentAgent:
    def stream(self, *_args, **_kwargs):
        yield {"messages": []}


class ClaudeDeepAgentsRunErrorTests(unittest.TestCase):
    """A fatal DeepAgents failure is the run's error, not just an exit code."""

    # The real runner; its agent's first model call fails as claude_exec does
    # for the Claude Code stream given in argv[2].
    RUNNER = textwrap.dedent("""\
        import json, sys, types
        from agent_monitor.runners import claude_backend, deepagents_runner

        class Agent:
            def stream(self, *_args, **_kwargs):
                events = tuple(json.loads(sys.argv[2]))
                claude_backend._terminal_result(claude_backend._ProcessResult(1, "", events))
                yield {}

        deepagents = types.ModuleType("deepagents")
        deepagents.create_deep_agent = lambda **_kwargs: Agent()
        filesystem = types.ModuleType("deepagents.backends.filesystem")
        filesystem.FilesystemBackend = lambda **_kwargs: None
        sys.modules.update({"deepagents": deepagents, "deepagents.backends": types.ModuleType("deepagents.backends"),
                            "deepagents.backends.filesystem": filesystem})
        deepagents_runner._create_claude_subscription_model = lambda _workspace, model: model
        sys.exit(deepagents_runner.main())
        """)

    def _run_on_claude(self, text: str, category: str) -> dict:
        events = [
            {"type": "assistant", "is_api_error_message": True, "error": category,
             "message": {"model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0},
                         "content": [{"type": "text", "text": text}]}},
            {"type": "result", "subtype": "success", "is_error": True, "result": text},
        ]
        command = [sys.executable, "-c", self.RUNNER, "Prove it.", json.dumps(events)]
        with tempfile.TemporaryDirectory() as tmp, patch(
            "agent_monitor.engines_registry.build_cli_command", return_value=command
        ), patch("agent_monitor.agent_config.persona_preamble", return_value=""), patch.object(jobs, "_write_run"):
            return jobs._run_cli_engine(
                run_id="deepagents_claude", engine="deepagents", problem_id="p", problem_text="Prove it.",
                started=time.time(), workspace=Path(tmp),
                extra_env={"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1", "CLAUDE_CONFIG_DIR": tmp,
                           "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            )

    def test_a_claude_usage_limit_is_the_run_error(self) -> None:
        run = self._run_on_claude("You've hit your session limit · resets 11pm (UTC)", "rate_limit")
        self.assertEqual(run["status"], "failed")
        self.assertIn("You've hit your session limit · resets 11pm (UTC)", run["error"])

    def test_an_expired_claude_sign_in_says_to_reconnect(self) -> None:
        run = self._run_on_claude(
            "Failed to authenticate: OAuth session expired and could not be refreshed", "authentication_failed")
        self.assertEqual(run["status"], "failed")
        self.assertIn("reconnect Claude Code in Settings", run["error"])

    def test_every_other_fatal_exit_names_its_reason(self) -> None:
        claude = {"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1", "CLAUDE_CONFIG_DIR": "/nonexistent"}
        scenarios = (
            ("empty prompt", {}, " ", None, "empty prompt"),
            ("both account routes", {**claude, "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1"}, "Prove it.", None,
             "cannot both be active"),
            ("model setup", claude, "Prove it.", RuntimeError("CLAUDE_CONFIG_DIR is required for DeepAgents Claude"),
             "CLAUDE_CONFIG_DIR is required"),
            ("no proof", claude, "Prove it.", None, "ended without a non-empty proof.md"),
        )
        for name, env, prompt, model_error, reason in scenarios:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                output = io.StringIO()
                previous = Path.cwd()
                os.chdir(tmp)
                try:
                    with (
                        patch.dict(os.environ, env, clear=True),
                        patch.dict(sys.modules, _deepagents_stub_modules(_SilentAgent())),
                        patch.object(sys, "argv", ["deepagents_runner.py", prompt]),
                        patch.object(deepagents_runner, "_install_termination_handlers"),
                        patch.object(deepagents_runner, "_create_claude_subscription_model",
                                     side_effect=model_error, return_value=object()),
                        patch.object(deepagents_runner, "_run_supplemental_audit"),
                        contextlib.redirect_stdout(output),
                    ):
                        returncode = deepagents_runner.main()
                finally:
                    os.chdir(previous)
                parser = CLIEventParser("codex", source_engine="deepagents")
                parser.feed(output.getvalue())
                self.assertNotEqual(returncode, 0)
                self.assertIn(reason, parser.terminal_error or "")


def _langchain_stub_modules() -> dict[str, types.ModuleType]:
    """The langchain_core names the Claude chat model uses (not installed here)."""

    class BaseChatModel:
        def __init__(self, **fields):
            self.__dict__.update(fields)

    class AIMessage:
        def __init__(self, content="", tool_calls=None, usage_metadata=None, response_metadata=None):
            self.content, self.tool_calls = content, tool_calls or []
            self.usage_metadata, self.response_metadata = usage_metadata, response_metadata

    class ChatGeneration:
        def __init__(self, message):
            self.message = message

    class ChatResult:
        def __init__(self, generations, llm_output=None):
            self.generations, self.llm_output = generations, llm_output

    def module(name: str, **attributes) -> types.ModuleType:
        stub = types.ModuleType(name)
        stub.__dict__.update(attributes)
        return stub

    return {
        "langchain_core": module("langchain_core"),
        "langchain_core.language_models": module("langchain_core.language_models"),
        "langchain_core.language_models.chat_models": module(
            "langchain_core.language_models.chat_models", BaseChatModel=BaseChatModel
        ),
        "langchain_core.messages": module("langchain_core.messages", AIMessage=AIMessage),
        "langchain_core.outputs": module(
            "langchain_core.outputs", ChatGeneration=ChatGeneration, ChatResult=ChatResult
        ),
        "langchain_core.utils": module("langchain_core.utils"),
        "langchain_core.utils.function_calling": module(
            "langchain_core.utils.function_calling", convert_to_openai_tool=lambda tool: tool
        ),
    }


class ClaudeDeepAgentsCacheUsageTests(unittest.TestCase):
    """Claude Code reports nearly all of a step's prompt as cache reads/writes."""

    def _usage_metadata(self, usages: list[dict[str, int]]) -> dict:
        from agent_monitor.runners import claude_backend

        # The first reply is invalid, so the step makes a repair call too.
        replies = ["not-json", json.dumps({"kind": "text", "text": "done", "tool_calls": []})]

        def fake_exec(_system, _user, **_kwargs):
            return claude_backend.ClaudeResult(
                text=replies.pop(0), usage=usages.pop(0), model="claude-haiku-4-5-20251001",
                cost_usd=None, events=(), terminal_event={},
            )

        human = types.SimpleNamespace(type="human", content="Prove it.")
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(sys.modules, _langchain_stub_modules()),
            patch.object(claude_backend, "claude_exec", side_effect=fake_exec),
            patch.dict(os.environ, {"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1", "CLAUDE_CONFIG_DIR": tmp}, clear=True),
        ):
            model = deepagents_runner._create_claude_subscription_model(Path(tmp), "claude-haiku-4-5")
            return model._generate([human], tools=[]).generations[0].message.usage_metadata

    def test_step_usage_counts_cache_reads_and_writes_as_input(self) -> None:
        usage = self._usage_metadata([
            {"input_tokens": 6, "output_tokens": 20, "cache_read_tokens": 41000, "cache_write_tokens": 9000},
            {"input_tokens": 3, "output_tokens": 100, "cache_read_tokens": 40000, "cache_write_tokens": 500},
        ])

        self.assertEqual(usage, {
            "input_tokens": 90509,
            "output_tokens": 120,
            "total_tokens": 90629,
            "input_token_details": {"cache_read": 81000, "cache_creation": 9500},
        })

    def test_step_usage_without_cache_is_unchanged(self) -> None:
        usage = self._usage_metadata([
            {"input_tokens": 2, "output_tokens": 3},
            {"input_tokens": 2, "output_tokens": 3},
        ])

        self.assertEqual(usage, {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10})

    def test_monitor_totals_include_the_cached_input(self) -> None:
        # handle_message dispatches on the LangChain class name.
        message = type("AIMessage", (), {"id": "m1", "content": "", "tool_calls": []})()
        message.usage_metadata = {
            "input_tokens": 90509,
            "output_tokens": 120,
            "total_tokens": 90629,
            "input_token_details": {"cache_read": 81000, "cache_creation": 9500},
        }

        class Agent:
            def stream(self, *_args, **_kwargs):
                yield {"messages": [message]}

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                with (
                    patch.dict(os.environ, {"AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1", "CLAUDE_CONFIG_DIR": tmp},
                               clear=True),
                    patch.dict(sys.modules, _deepagents_stub_modules(Agent())),
                    patch.object(sys, "argv", ["deepagents_runner.py", "Prove it."]),
                    patch.object(deepagents_runner, "_install_termination_handlers"),
                    patch.object(deepagents_runner, "_create_claude_subscription_model", return_value=object()),
                    patch.object(deepagents_runner, "_run_supplemental_audit"),
                    contextlib.redirect_stdout(output),
                ):
                    deepagents_runner.main()
            finally:
                os.chdir(previous)

        parser = CLIEventParser("codex", source_engine="deepagents")
        parser.feed(output.getvalue())
        self.assertEqual(parser.usage["input_tokens"], 90509)
        self.assertEqual(parser.usage["cache_read_tokens"], 81000)
        self.assertEqual(parser.usage["cache_write_tokens"], 9500)
        self.assertEqual(parser.usage["output_tokens"], 120)
        self.assertEqual(parser.total_input_tokens(), 90509)


if __name__ == "__main__":
    unittest.main()
