"""Codex sign-in state and Formal Lean Codex timeouts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_monitor import cgm_max, claude_login, codex_login, jobs, lean_verify, settings
from agent_monitor.runners import codex_backend

SOURCE = "theorem main : True := by trivial\n"

# The npm-installed ``codex`` is a Node launcher (``bin/codex.js``) that runs
# the native binary as a child and forwards only SIGINT/SIGTERM/SIGHUP to it,
# so a timeout that SIGKILLs the launcher alone leaves native Codex running.
# This fake launcher behaves the same way; its native child records its pid.
_NATIVE = """
import os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(60)
"""

_LAUNCHER = """#!{python}
import signal, subprocess, sys
child = subprocess.Popen([sys.executable, {native!r}, {pidfile!r}])
for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, lambda signum, _frame: child.send_signal(signum))
sys.exit(child.wait())
"""


def _fake_npm_codex(root: Path) -> tuple[Path, Path]:
    native = root / "native_codex.py"
    native.write_text(_NATIVE, encoding="utf-8")
    pidfile = root / "native.pid"
    launcher = root / "codex"
    launcher.write_text(
        _LAUNCHER.format(python=sys.executable, native=str(native), pidfile=str(pidfile)),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher, pidfile


def _running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


@unittest.skipUnless(sys.platform.startswith("linux"), "uses /proc")
class CodexLauncherTimeoutTests(unittest.TestCase):
    def _assert_native_stopped(self, pidfile: Path) -> None:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pidfile.exists(), "the fake native codex never started")
        pid = int(pidfile.read_text())
        self.addCleanup(lambda: _running(pid) and os.kill(pid, signal.SIGKILL))
        while _running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_running(pid), "native codex outlived the timeout")

    def test_formal_lean_intervention_timeout_stops_native_codex(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            launcher, pidfile = _fake_npm_codex(root)
            workspace = root / "run"
            workspace.mkdir()
            with (
                patch.dict(os.environ, {"AGENT_MONITOR_LEAN_INTERVENTION_TIMEOUT": "2"}),
                patch("agent_monitor.codex_login.account_login_ready", return_value=True),
                patch("agent_monitor.codex_login.account_home", return_value=root / "account"),
                patch("agent_monitor.settings.codex_enabled", return_value=True),
                patch(
                    "agent_monitor.engines_registry.build_cli_command",
                    return_value=[str(launcher), "exec", "--json"],
                ),
                patch.object(
                    lean_verify,
                    "_harness_env",
                    return_value={"PATH": os.environ.get("PATH", os.defpath), "HOME": raw},
                ),
            ):
                with self.assertRaisesRegex(ValueError, "timed out after 2s"):
                    lean_verify._codex_subscription_interaction(
                        workspace=workspace,
                        user={"id": 7},
                        prompt="Revise the proof.",
                        lean_source=SOURCE,
                        edit=True,
                    )
            self._assert_native_stopped(pidfile)

    def test_model_catalog_refresh_timeout_stops_native_codex(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            launcher, pidfile = _fake_npm_codex(root)
            with (
                patch.object(codex_login, "codex_binary", return_value=str(launcher)),
                patch.object(codex_login, "account_home", return_value=root / "account"),
                patch.object(codex_login, "account_login_ready", return_value=True),
            ):
                self.assertFalse(codex_login.refresh_models_cache(7, timeout=2))
            self._assert_native_stopped(pidfile)

    def test_cli_version_probe_timeout_stops_native_codex(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            launcher, pidfile = _fake_npm_codex(Path(raw))
            with patch.object(codex_login, "_MODELS_REFRESH_TIMEOUT_S", 2):
                self.assertEqual(codex_login._installed_cli_version(str(launcher)), "")
            self._assert_native_stopped(pidfile)


# What ``codex login --device-auth`` prints before it waits for approval.
_DEVICE_PROMPT = """
echo "Welcome to Codex [v0.160.0]"
echo "1. Open this link in your browser and sign in to your account"
echo "   https://auth.openai.com/codex/device"
echo "2. Enter this one-time code (expires in 15 minutes)"
echo "   ABCD-12345"
echo "Continue only if you started this login in Codex. If a website or another person gave you this code, cancel."
"""
# Captured from codex-cli 0.160.0 when auth.openai.com cannot be reached.
_UNREACHABLE = (
    "Error logging in with device code: error sending request for url "
    "(https://auth.openai.com/api/accounts/deviceauth/usercode)"
)


class CodexLoginFailureReasonTests(unittest.TestCase):
    USER_ID = 4242

    def _login(self, script: str) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            codex = root / "codex"
            codex.write_text("#!/bin/sh\n" + script, encoding="utf-8")
            codex.chmod(0o755)
            self.addCleanup(lambda: codex_login._states.pop(self.USER_ID, None))
            with (
                patch.dict(os.environ, {"AGENT_MONITOR_CODEX_ACCOUNT_HOME": str(root / "users")}),
                patch.object(codex_login, "codex_binary", return_value=str(codex)),
            ):
                status = codex_login.start_login(self.USER_ID)
                deadline = time.monotonic() + 10
                while status["running"] and time.monotonic() < deadline:
                    time.sleep(0.05)
                    status = codex_login.status(self.USER_ID)
        self.assertEqual(status["status"], "error")
        self.assertFalse(status["connected"])
        return status

    def test_failed_sign_in_reports_codex_reason(self) -> None:
        status = self._login(f'echo "{_UNREACHABLE}"\nexit 1\n')
        self.assertEqual(status["error"], f"codex login exited with code 1: {_UNREACHABLE}")

    def test_reason_after_the_device_prompt_is_reported_without_the_link_or_code(self) -> None:
        status = self._login(
            _DEVICE_PROMPT
            + 'echo "Error logging in with device code: device auth timed out" >&2\nexit 1\n'
        )
        self.assertEqual(
            status["error"],
            "codex login exited with code 1: "
            "Error logging in with device code: device auth timed out",
        )

    def test_exit_without_a_reason_keeps_the_exit_code_message(self) -> None:
        status = self._login(_DEVICE_PROMPT + "exit 3\n")
        self.assertEqual(status["error"], "codex login exited with code 3")

    def test_success_exit_without_a_saved_login_is_explained(self) -> None:
        status = self._login('echo "Successfully logged in"\nexit 0\n')
        self.assertIn("no ChatGPT sign-in was saved", status.get("error") or "")


def _stream(*events: dict) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


# codex exec --json 0.160.0 once the saved ChatGPT login has expired (captured
# offline): a transient "Reconnecting..." 401, then the terminal failure.
_EXPIRED = (
    "unexpected status 401 Unauthorized: Your authentication token has expired. "
    "Please try signing in again., url: http://127.0.0.1:43557/v1/responses"
)
_EXPIRED_LOGIN = _stream(
    {"type": "thread.started", "thread_id": "01a12cde-4aa0-7f30-a4cd-4fa325edddad"},
    {"type": "turn.started"},
    {"type": "error", "message": f"Reconnecting... 1/1 ({_EXPIRED})"},
    {"type": "error", "message": _EXPIRED},
    {"type": "turn.failed", "error": {"message": _EXPIRED}},
)
# A 401 that Codex recovered from on its own.
_RECOVERED = _stream(
    {"type": "turn.started"},
    {"type": "error", "message": f"Reconnecting... 1/5 ({_EXPIRED})"},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "done"}},
    {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}},
)
_USAGE_LIMIT = _stream(
    {"type": "turn.started"},
    {"type": "turn.failed", "error": {"message": (
        "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at 10:28 PM.")}},
)


def _fake_codex_exec(stdout: str, returncode: int):
    def run(command, **_kwargs):
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.160.0\n", "")
        if command[1:] == ["debug", "models", "--bundled"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"models": [{"slug": "gpt-6-astra"}]}), "")
        if returncode == 0:
            Path(command[command.index("--output-last-message") + 1]).write_text("done")
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    return run


class CodexReauthTests(unittest.TestCase):
    """An expired or revoked ChatGPT login must read as "reconnect", not "connected"."""

    USER = {"id": 4343, "email": "owner@example.com", "is_admin": True}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        accounts = patch.dict(
            os.environ, {"AGENT_MONITOR_CODEX_ACCOUNT_HOME": str(self.root / "users")}
        )
        accounts.start()
        self.addCleanup(accounts.stop)
        # USER stands for the site administrator: subscription routes bind
        # only for operator accounts.
        operator = patch("agent_monitor.auth.is_operator_account", return_value=True)
        operator.start()
        self.addCleanup(operator.stop)
        self.home = codex_login.account_home(self.USER["id"])
        self.home.mkdir(parents=True)
        (self.home / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "test-access", "refresh_token": "test-refresh"}}),
            encoding="utf-8",
        )

    def _exec(self, home: Path, stdout: str, returncode: int) -> codex_backend.CodexResult:
        with (
            patch.object(codex_backend.shutil, "which", return_value="/bin/true"),
            patch.object(
                codex_backend, "_run_codex_process", side_effect=_fake_codex_exec(stdout, returncode)
            ),
        ):
            return codex_backend.codex_exec(
                "system", "user", model="gpt-6-astra",
                source_env={"CODEX_HOME": str(home), "PATH": "/usr/bin:/bin"},
            )

    def _subscription_env(self) -> dict[str, str]:
        return jobs._configure_auth_route(
            {"OPENAI_API_KEY": "sk-test-dormant", "PATH": os.environ.get("PATH", os.defpath)},
            route="codex_subscription",
            user_id=self.USER["id"],
        )

    def test_final_401_flags_the_login_for_reconnection_everywhere(self) -> None:
        self.assertTrue(codex_login.status(self.USER["id"])["connected"])
        with self.assertRaises(codex_backend.CodexBackendError) as raised:
            self._exec(self.home, _EXPIRED_LOGIN, 1)
        self.assertEqual(raised.exception.kind, "auth")

        status = codex_login.status(self.USER["id"])
        self.assertFalse(status["connected"])
        self.assertTrue(status["reauth_required"])
        with (
            patch.object(
                settings, "resolved_user_env", return_value={"AGENT_MONITOR_ACCOUNT_RUNTIME": "codex"}
            ),
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
        ):
            current = settings.get_settings(self.USER)
            plan = cgm_max.plan_for(self.USER)
        self.assertFalse(current["codex_connected"])
        self.assertTrue(current["codex_reauth_required"])
        self.assertFalse(current["runtime_ready"])
        self.assertEqual(current["codex_models"], [])
        self.assertFalse(plan["available"])
        self.assertIn("connect Codex", plan["reason"])
        # New subscription runs refuse the login; a saved API key is no fallback.
        with self.assertRaisesRegex(ValueError, "Codex is not connected"):
            self._subscription_env()

    def test_completed_turn_clears_the_flag(self) -> None:
        codex_login.mark_reauth_required(self.home)
        self.assertFalse(codex_login.account_login_ready(self.home))
        self.assertEqual(self._exec(self.home, _RECOVERED, 0).text, "done")
        self.assertFalse(codex_login.account_reauth_required(self.home))
        self.assertTrue(codex_login.status(self.USER["id"])["connected"])

    def test_recovered_401_usage_limit_and_api_key_homes_never_flag(self) -> None:
        self._exec(self.home, _RECOVERED, 0)
        with self.assertRaises(codex_backend.CodexBackendError):
            self._exec(self.home, _USAGE_LIMIT, 1)
        self.assertFalse(codex_login.account_reauth_required(self.home))
        api_key_home = self.root / "codex_home" / "0123456789abcdef"
        api_key_home.mkdir(parents=True)
        with self.assertRaises(codex_backend.CodexBackendError):
            self._exec(api_key_home, _EXPIRED_LOGIN, 1)
        self.assertEqual(list(api_key_home.iterdir()), [])
        self.assertTrue(codex_login.status(self.USER["id"])["connected"])

    def test_new_sign_in_and_disconnect_clear_the_flag(self) -> None:
        class SignedIn:
            stdout = iter(["Successfully logged in\n"])

            def wait(self, timeout=None):
                return 0

        user_id = self.USER["id"]
        codex_login.mark_reauth_required(self.home)
        with codex_login._lock:
            codex_login._states[user_id] = {"status": "running", "running": True}
        self.addCleanup(lambda: codex_login._states.pop(user_id, None))
        with patch.object(codex_login, "refresh_models_cache", return_value=True):
            codex_login._reader(SignedIn(), self.home, user_id)
        status = codex_login.status(user_id)
        self.assertEqual(status["status"], "success")
        self.assertTrue(status["connected"])
        self.assertFalse(status["reauth_required"])

        codex_login.mark_reauth_required(self.home)
        result = codex_login.logout(user_id)
        self.assertFalse(result["connected"])
        self.assertFalse(result["reauth_required"])

    def _run_codex_engine(self, extra_env: dict[str, str], stdout: str, returncode: int) -> dict:
        workspace = self.root / f"run-{returncode}"
        workspace.mkdir()
        command = [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                "Path('proof.md').write_text('# Proof\\n\\n' + 'A bounded proof artifact. ' * 20); "
                "sys.stdout.write(sys.argv[1]); sys.exit(int(sys.argv[2]))"
            ),
            stdout,
            str(returncode),
        ]
        with (
            patch("agent_monitor.engines_registry.build_cli_command", return_value=command),
            patch("agent_monitor.agent_config.persona_preamble", return_value=""),
            patch.object(jobs, "_write_run"),
        ):
            return jobs._run_cli_engine(
                run_id=f"codex_reauth_{returncode}",
                engine="codex",
                problem_id="reauth",
                problem_text="Prove it.",
                started=time.time(),
                workspace=workspace,
                extra_env=extra_env,
                requested_model="gpt-6-astra",
                max_iterations=3,
                owner_id=self.USER["id"],
            )

    def test_codex_engine_runs_flag_and_clear_the_login(self) -> None:
        extra_env = self._subscription_env()
        failed = self._run_codex_engine(extra_env, _EXPIRED_LOGIN, 1)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("401 Unauthorized", failed["error"])
        self.assertTrue(codex_login.account_reauth_required(self.home))
        self.assertFalse(codex_login.status(self.USER["id"])["connected"])

        # A run already routed to the account keeps it (never an API key), and
        # its completed turn shows the login works again.
        self._run_codex_engine(extra_env, _RECOVERED, 0)
        self.assertFalse(codex_login.account_reauth_required(self.home))
        self.assertTrue(codex_login.status(self.USER["id"])["connected"])

    def test_a_flagged_login_never_switches_a_routed_run_to_an_api_key(self) -> None:
        env = self._subscription_env()
        codex_login.mark_reauth_required(self.home)
        jobs._ensure_codex_auth(env, self.USER["id"])
        self.assertEqual(env["CODEX_HOME"], str(self.home))
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_a_flagged_login_never_moves_a_legacy_continuation_to_an_api_key(self) -> None:
        # A run recorded before auth_route existed follows the current runtime.
        legacy = {"run_id": "plain_legacy", "engine": "plain", "owner_id": self.USER["id"]}
        engine = {"id": "plain", "auth_modes": ["api_key", "codex_subscription", "claude_subscription"]}
        claude_home = self.root / "claude-account"

        def continue_on(runtime: str, model: str) -> str:
            env = {
                "AGENT_MONITOR_ACCOUNT_RUNTIME": runtime,
                "OPENAI_API_KEY": "sk-test-dormant",
                "ANTHROPIC_API_KEY": "sk-ant-test-dormant",
            }
            return jobs._continuation_auth_environment(
                run_data=legacy, engine="plain", model=model, user=self.USER, env=env
            )[1]

        with (
            patch("agent_monitor.engines_registry.list_engines", return_value=[engine]),
            patch.object(claude_login, "account_home", return_value=claude_home),
            patch.object(
                claude_login,
                "_auth_status",
                return_value={"loggedIn": True, "authMethod": "oauth", "apiProvider": "firstParty"},
            ),
        ):
            codex_login.mark_reauth_required(self.home)
            claude_login.mark_reauth_required(claude_home)
            for runtime, model, product in (
                ("codex", "gpt-6-astra", "Codex"),
                ("claude", "sonnet", "Claude Code"),
            ):
                with (
                    self.subTest(runtime=runtime),
                    self.assertRaisesRegex(ValueError, f"{product} is not connected"),
                ):
                    continue_on(runtime, model)

            # A disconnected account keeps the historical pin to API.
            codex_login.logout(self.USER["id"])
            self.assertEqual(continue_on("codex", "gpt-6-astra"), "api_key")

    def _formal_lean_codex(self, stdout: str, returncode: int, *, during=None) -> dict:
        workspace = self.root / "formal-run"
        workspace.mkdir(exist_ok=True)

        def codex(command, *, env, **_kwargs):
            self.assertEqual(env["CODEX_HOME"], str(self.home))
            if during:
                during()
            return subprocess.CompletedProcess(command, returncode, stdout, "")

        with (
            patch.object(settings, "resolved_user_env", return_value={}),
            patch(
                "agent_monitor.engines_registry.build_cli_command",
                return_value=["codex", "exec", "--json"],
            ),
            patch.object(codex_backend, "_run_codex_process", side_effect=codex),
        ):
            return lean_verify._codex_subscription_interaction(
                workspace=workspace,
                user=self.USER,
                run_record={"owner_id": self.USER["id"], "auth_route": "codex_subscription"},
                prompt="Is this proof complete?",
                lean_source=SOURCE,
                edit=False,
            )

    def test_formal_lean_codex_calls_flag_and_clear_the_login(self) -> None:
        with self.assertRaisesRegex(ValueError, "(?s)Codex intervention failed .*401 Unauthorized"):
            self._formal_lean_codex(_EXPIRED_LOGIN, 1)
        self.assertTrue(codex_login.account_reauth_required(self.home))
        self.assertTrue(codex_login.status(self.USER["id"])["reauth_required"])
        # The next Formal Lean call stops before Codex runs.
        with self.assertRaisesRegex(ValueError, "subscription is not connected"):
            self._formal_lean_codex(_RECOVERED, 0, during=lambda: self.fail("Codex ran"))

        # A completed turn (here one that raced a rejection elsewhere) clears it.
        codex_login.clear_reauth_required(self.home)
        result = self._formal_lean_codex(
            _RECOVERED, 0, during=lambda: codex_login.mark_reauth_required(self.home)
        )
        self.assertEqual(result["answer"], "done")
        self.assertFalse(codex_login.account_reauth_required(self.home))
        self.assertTrue(codex_login.status(self.USER["id"])["connected"])

    def _formal_lean_harness(self, env: dict[str, str], stdout: str, returncode: int) -> None:
        workspace = self.root / "harness-run"
        ldir = workspace / lean_verify.LEAN_DIRNAME
        ldir.mkdir(parents=True, exist_ok=True)
        with patch.object(lean_verify, "_harness_finish") as finish:
            lean_verify._harness_worker(
                workspace=workspace,
                ldir=ldir,
                argv=[
                    sys.executable, "-c",
                    "import sys; sys.stdout.write(sys.argv[1]); sys.exit(int(sys.argv[2]))",
                    stdout, str(returncode),
                ],
                engine="codex",
                label="Codex",
                user=self.USER,
                model="gpt-6-astra",
                run_record={"owner_id": self.USER["id"], "auth_route": "codex_subscription"},
                env=env,
                problem="Prove True.",
                informal="Trivial.",
                base={},
                initial_fingerprint=None,
                audit=False,
            )
        self.assertEqual(finish.call_args.kwargs["exit_code"], returncode)

    def test_formal_lean_codex_harness_flags_and_clears_the_login(self) -> None:
        env = self._subscription_env()
        self._formal_lean_harness(env, _EXPIRED_LOGIN, 1)
        self.assertTrue(codex_login.account_reauth_required(self.home))
        self.assertFalse(codex_login.status(self.USER["id"])["connected"])

        # A harness already routed to the account completes: the login works.
        self._formal_lean_harness(env, _RECOVERED, 0)
        self.assertFalse(codex_login.account_reauth_required(self.home))
        self.assertTrue(codex_login.status(self.USER["id"])["connected"])


HTML = (Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html").read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"


def _js_function(name: str) -> str:
    function_start = SCRIPT.index(f"function {name}(")
    start = function_start - 6 if SCRIPT[function_start - 6 : function_start] == "async " else function_start
    opening = SCRIPT.index("{", function_start)
    depth = 0
    for index in range(opening, len(SCRIPT)):
        if SCRIPT[index] == "{":
            depth += 1
        elif SCRIPT[index] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


@unittest.skipUnless(NODE, "node is unavailable")
class CodexReauthCardTests(unittest.TestCase):
    def test_rejected_login_offers_reconnect_instead_of_connected(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in ("runtimeUiSpec", "codexCardBody", "renderCodexCard", "applyCodexStatus")
        )
        program = f"""
const LIB_ESC=value=>String(value);
let SETTINGS={{configured_providers:[],codex_connected:true,codex_reauth_required:false,subscription_policy:{{}}}};
const selectedAccountRuntime=()=>'codex';
const els={{}}; const $=id=>els[id]||(els[id]={{id,innerHTML:'',dataset:{{}}}});
let LAST_CODEX_STATUS={{}};
const loadSettings=async()=>{{}}, renderRuntimeSelector=()=>{{}}, syncRuntimeModelControls=()=>{{}}, renderEngines=()=>{{}};
{source}
(async()=>{{
  await applyCodexStatus({{status:'idle',running:false,connected:false,reauth_required:true,codex_installed:true}});
  const card=els['codex-card'].innerHTML;
  console.log(JSON.stringify([runtimeUiSpec('codex').state,SETTINGS.codex_reauth_required,card.includes('reauth required'),
    card.includes('Reconnect via ChatGPT'),card.includes('id="codex-connect-btn"'),card.includes('Connected and in use')]));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
        result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            ["Reauthorization required", True, True, True, True, False],
        )


if __name__ == "__main__":
    unittest.main()
