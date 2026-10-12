from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_monitor.runners import codex_backend


def _advertised_tools(payload: object) -> list[object]:
    """Every tool a Responses request offers, wherever the client put it.

    Classic requests use the top-level ``tools`` field.  Code-mode "responses
    lite" models instead carry tools inside an ``input`` item (for example
    ``{"type": "additional_tools", "tools": [...]}``), so checking only the
    top-level field would pass vacuously.
    """
    found: list[object] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "tools" and isinstance(value, list):
                found.extend(value)
            found.extend(_advertised_tools(value))
    elif isinstance(payload, list):
        for value in payload:
            found.extend(_advertised_tools(value))
    return found


def _environment_cwd(payload: dict) -> str | None:
    text = json.dumps(payload)
    marker = "<cwd>"
    if marker not in text:
        return None
    return text.split(marker, 1)[1].split("</cwd>", 1)[0]


class CodexResponseOnlyContractTests(unittest.TestCase):
    def test_subscription_turn_disables_tools_and_uses_empty_backend_cwd(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
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
                    stdout=json.dumps(
                        {
                            "models": [
                                {
                                    "slug": "gpt-5.6-sol",
                                    "apply_patch_tool_type": "freeform",
                                    "experimental_supported_tools": ["clock"],
                                    "tool_mode": "code_mode_only",
                                    "multi_agent_version": "v2",
                                    "multi_agent_reasoning_effort": "xhigh",
                                    "web_search_tool_type": "text_and_image",
                                }
                            ]
                        }
                    ),
                    stderr="",
                )
            captured["command"] = list(command)
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = dict(kwargs.get("env") or {})
            config = next(
                token
                for token in command
                if token.startswith("model_catalog_json=")
            )
            catalog_path = Path(json.loads(config.split("=", 1)[1]))
            captured["catalog"] = json.loads(catalog_path.read_text(encoding="utf-8"))
            captured["catalog_mode"] = stat.S_IMODE(catalog_path.stat().st_mode)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text("A response-only proof result.", encoding="utf-8")
            event = json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }
            )
            return subprocess.CompletedProcess(command, 0, stdout=event, stderr="")

        with tempfile.TemporaryDirectory() as exposed:
            with (
                patch.object(
                    codex_backend.shutil, "which", return_value="/bin/true"
                ),
                patch.object(
                    codex_backend, "_run_codex_process", side_effect=fake_run
                ),
            ):
                result = codex_backend.codex_exec(
                    "system",
                    "user",
                    model="gpt-5.6-sol",
                    workspace=exposed,
                    enable_tools=False,
                    source_env={
                        "CODEX_HOME": "/tmp/account-codex-home",
                        "PATH": "/usr/bin:/bin",
                        "OPENAI_API_KEY": "must-not-leak",
                        "ANTHROPIC_API_KEY": "must-not-leak",
                        "KIMI_API_KEY": "must-not-leak",
                    },
                )

        command = captured["command"]
        assert isinstance(command, list)
        disabled = {
            command[index + 1]
            for index, token in enumerate(command[:-1])
            if token == "--disable"
        }
        self.assertTrue(
            {
                "apps",
                "artifact",
                "auth_elicitation",
                "browser_use",
                "browser_use_external",
                "browser_use_full_cdp_access",
                "code_mode",
                "code_mode_host",
                "computer_use",
                "enable_mcp_apps",
                "goals",
                "hooks",
                "image_generation",
                "in_app_browser",
                "memories",
                "multi_agent",
                "network_proxy",
                "plugins",
                "plugin_sharing",
                "remote_plugin",
                "request_permissions_tool",
                "skill_mcp_dependency_install",
                "skill_search",
                "sleep_tool",
                "standalone_web_search",
                "tool_call_mcp_elicitation",
                "tool_suggest",
                "view_image",
                "workspace_dependencies",
                "shell_tool",
                "unified_exec",
            }.issubset(disabled)
        )
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--strict-config", command)
        self.assertIn('web_search="disabled"', command)
        self.assertIn("tools.web_search=false", command)
        self.assertIn("tools.update_plan.enabled=false", command)
        self.assertIn("tools.experimental_request_user_input.enabled=false", command)
        self.assertIn("skills.include_instructions=false", command)
        self.assertIn("skills.bundled.enabled=false", command)
        self.assertNotIn("--cd", command)
        self.assertNotEqual(captured["cwd"], exposed)
        self.assertFalse(Path(str(captured["cwd"])).exists())
        child_env = captured["env"]
        assert isinstance(child_env, dict)
        self.assertFalse(
            any(
                str(name).endswith(("_API_KEY", "_API_KEYS", "_AUTH_TOKEN"))
                for name in child_env
            )
        )
        self.assertEqual(captured["catalog_mode"], 0o600)
        self.assertEqual(
            captured["catalog"],
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "experimental_supported_tools": [],
                        "node_repl_disabled": True,
                        "include_apps_usage_instructions": False,
                        "include_plugin_usage_instructions": False,
                        "include_skills_usage_instructions": False,
                        "supports_search_tool": False,
                        "supports_parallel_tool_calls": False,
                    }
                ]
            },
        )
        self.assertEqual(codex_backend.RESPONSE_ONLY_TOOL_ISOLATION_VERSION, 5)
        self.assertEqual(result.text, "A response-only proof result.")

    def _installed_cli_version(self, codex: str) -> str:
        with tempfile.TemporaryDirectory() as isolated_home:
            completed = subprocess.run(
                [codex, "--version"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", os.defpath),
                    "HOME": isolated_home,
                    "CODEX_HOME": isolated_home,
                    "NO_COLOR": "1",
                },
                cwd=isolated_home,
            )
        return completed.stdout.strip()

    def _bundled_model_slugs(self, codex: str) -> set[str]:
        with tempfile.TemporaryDirectory() as isolated_home:
            completed = subprocess.run(
                [codex, "debug", "models", "--bundled"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", os.defpath),
                    "HOME": isolated_home,
                    "CODEX_HOME": isolated_home,
                    "NO_COLOR": "1",
                },
                cwd=isolated_home,
            )
        models = json.loads(completed.stdout or "{}").get("models") or []
        return {str(item.get("slug")) for item in models if isinstance(item, dict)}

    def _capture_response_only_turn(
        self, model: str, workspace: str
    ) -> tuple[list[str], list[dict]]:
        """Run one real response-only turn against a loopback capture provider."""
        captured: list[dict] = []
        captured_paths: list[str] = []
        received = threading.Event()

        class CaptureHandler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                try:
                    captured_paths.append(self.path)
                    size = int(self.headers.get("Content-Length") or "0")
                    payload = json.loads(self.rfile.read(size))
                    if isinstance(payload, dict):
                        captured.append(payload)
                finally:
                    received.set()
                body = json.dumps(
                    {"error": {"message": "offline capture complete"}}
                ).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        original_args = codex_backend._response_only_args
        base_url = f"http://127.0.0.1:{server.server_port}/v1"

        def capture_provider_args(*, enable_tools: bool) -> list[str]:
            return original_args(enable_tools=enable_tools) + [
                "-c",
                'model_provider="capture"',
                "-c",
                'model_providers.capture.name="Offline capture"',
                "-c",
                "model_providers.capture.base_url=" + json.dumps(base_url),
                "-c",
                'model_providers.capture.env_key="CODEX_HOME"',
                "-c",
                'model_providers.capture.wire_api="responses"',
                "-c",
                "model_providers.capture.requires_openai_auth=false",
            ]

        try:
            # A throwaway CODEX_HOME: this contract never touches a real login.
            with tempfile.TemporaryDirectory() as account_home:
                with patch.object(
                    codex_backend,
                    "_response_only_args",
                    side_effect=capture_provider_args,
                ):
                    with self.assertRaises(codex_backend.CodexBackendError):
                        codex_backend.codex_exec(
                            "Return one short sentence.",
                            "Say that this is an offline transport test.",
                            model=model,
                            timeout=30,
                            workspace=workspace,
                            enable_tools=False,
                            source_env={
                                "CODEX_HOME": account_home,
                                "PATH": os.environ.get("PATH", os.defpath),
                                "AGENT_MONITOR_CODEX_LEGACY_LANDLOCK": "0",
                            },
                        )
            self.assertTrue(
                received.wait(2), f"Codex sent no loopback request for {model}"
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
        return captured_paths, captured

    def test_installed_cli_sends_zero_tools_to_loopback_provider(self) -> None:
        codex = shutil.which("codex")
        if not codex:
            self.skipTest("installed Codex CLI is unavailable")
        installed = self._installed_cli_version(codex)
        if installed not in codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSIONS:
            self.fail(
                f"{codex} reports {installed or 'no version'!r}, which is not a "
                "vetted response-only Codex build "
                f"{sorted(codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSIONS)}. "
                "Install one exactly (for example "
                f"`{codex_backend.RESPONSE_ONLY_CODEX_INSTALL_HINT}`) or re-vet "
                "this build with this test before adding it to the pinned set."
            )
        # One code-mode "responses lite" model, one with bundled experimental
        # tools, and one classic Responses model, all present in every vetted
        # build's bundled catalog, plus GPT-6.1 Sol where the build ships it.
        models = ["gpt-5.6-sol", "gpt-6-astra", "gpt-5.5"]
        if "gpt-6.1-sol" in self._bundled_model_slugs(codex):
            models.append("gpt-6.1-sol")
        for model in models:
            with self.subTest(model=model):
                with tempfile.TemporaryDirectory() as exposed:
                    paths, requests = self._capture_response_only_turn(
                        model, exposed
                    )
                self.assertTrue(requests)
                self.assertEqual(paths[0], "/v1/responses")
                request = requests[0]
                self.assertEqual(request.get("model"), model)
                self.assertEqual(_advertised_tools(request), [])
                serialized = json.dumps(request)
                # Base instructions may mention tools by name in prose; these
                # markers only appear when tool or skill payloads are attached.
                for marker in ("spawn_agent", "<skills_instructions>"):
                    self.assertNotIn(marker, serialized)
                if model == "gpt-5.6-sol":
                    self.assertIs(request.get("parallel_tool_calls"), False)
                cwd = _environment_cwd(request)
                self.assertIsNotNone(cwd)
                assert cwd is not None
                self.assertNotEqual(Path(cwd).resolve(), Path(exposed).resolve())
                self.assertTrue(Path(cwd).name.startswith("agent-monitor-codex-"))
                self.assertFalse(Path(cwd).exists())

    def test_response_only_mode_rejects_an_unvetted_cli_version(self) -> None:
        # The pin is an exact set of re-vetted builds, not a minimum version.
        self.assertEqual(
            codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSIONS,
            frozenset({"codex-cli 0.153.4", "codex-cli 0.157.1", "codex-cli 0.160.0"}),
        )
        self.assertIn(
            codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION,
            codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSIONS,
        )
        for reported in ("codex-cli 999.0.0\n", "codex-cli 0.157.2\n", "codex-cli 0.157.1 beta\n"):
            calls: list[list[str]] = []

            def fake_run(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                calls.append(list(command))
                return subprocess.CompletedProcess(
                    command, 0, stdout=reported, stderr=""
                )

            with (
                self.subTest(reported=reported.strip()),
                patch.object(codex_backend.shutil, "which", return_value="/bin/true"),
                patch.object(codex_backend, "_run_codex_process", side_effect=fake_run),
            ):
                with self.assertRaisesRegex(
                    codex_backend.CodexBackendError, "requires vetted CLI"
                ) as raised:
                    codex_backend.codex_exec(
                        "system",
                        "user",
                        model="gpt-5.6-sol",
                        source_env={"CODEX_HOME": "/tmp/account-codex-home"},
                    )
                message = str(raised.exception)
                self.assertIn(repr(reported.strip()), message)
                self.assertIn(codex_backend.RESPONSE_ONLY_CODEX_INSTALL_HINT, message)
                self.assertEqual(
                    calls, [[str(Path("/bin/true").resolve()), "--version"]]
                )

    def test_response_only_mode_rejects_model_missing_from_catalog(self) -> None:
        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=codex_backend.RESPONSE_ONLY_CODEX_CLI_VERSION + "\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"models": [{"slug": "gpt-5.6-sol"}]}),
                stderr="",
            )

        with (
            patch.object(codex_backend.shutil, "which", return_value="/bin/true"),
            patch.object(codex_backend, "_run_codex_process", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(
                codex_backend.CodexBackendError, "not in the vetted bundled catalog"
            ):
                codex_backend.codex_exec(
                    "system",
                    "user",
                    model="unknown-model",
                    source_env={"CODEX_HOME": "/tmp/account-codex-home"},
                )


if __name__ == "__main__":
    unittest.main()
