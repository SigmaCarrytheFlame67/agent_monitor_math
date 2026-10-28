from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_monitor import claude_login, console_server, settings

# Stands in for `claude auth login`: prints the sign-in URL, waits for the
# code on stdin, then stores a credential the way the real CLI does.
FAKE_LOGIN_CLI = """#!/bin/sh
[ "$1 $2" = "auth login" ] || exit 2
echo "https://claude.ai/oauth/authorize?code=true&state=test"
echo "Paste code here if prompted > "
read -r code || exit 1
[ -n "$code" ] || exit 1
printf '{"claudeAiOauth":{"refreshToken":"test-refresh","refreshTokenExpiresAt":4102444800000}}' \\
  > "$CLAUDE_CONFIG_DIR/.credentials.json"
"""


class ClaudeLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        with claude_login._lock:
            claude_login._states.clear()
        with claude_login._cache_lock:
            claude_login._auth_cache.clear()

    def test_account_environment_is_private_and_scrubs_api_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "user_8"
            claude_login._ensure_account_home(home)
            env = claude_login.account_environment(
                home,
                {
                    "PATH": "/bin",
                    "ANTHROPIC_API_KEY": "secret",
                    "ANTHROPIC_AUTH_TOKEN": "token",
                    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-access",
                    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "oauth-refresh",
                    "CLAUDE_CODE_OAUTH_SCOPES": "user:inference",
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "AWS_ACCESS_KEY_ID": "aws",
                },
            )
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(home))
            self.assertEqual(env["HOME"], str(home))
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_SCOPES", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
            self.assertNotIn("AWS_ACCESS_KEY_ID", env)

    @pytest.mark.real_claude_auth_status
    def test_auth_status_whitelists_metadata_and_never_returns_tokens(self) -> None:
        raw = {
            "loggedIn": True,
            "authMethod": "oauth",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
            "accessToken": "must-not-leak",
        }
        completed = SimpleNamespace(stdout=json.dumps(raw), returncode=0)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(claude_login, "claude_binary", return_value="/bin/claude"),
            patch.object(claude_login.subprocess, "run", return_value=completed),
        ):
            result = claude_login._run_auth_status(Path(tmp))
        self.assertTrue(result["loggedIn"])
        self.assertEqual(result["subscriptionType"], "max")
        self.assertNotIn("accessToken", result)
        self.assertNotIn("must-not-leak", json.dumps(result))

    def test_api_key_auth_is_not_treated_as_subscription(self) -> None:
        with patch.object(
            claude_login,
            "_auth_status",
            return_value={"loggedIn": True, "authMethod": "apiKey"},
        ):
            self.assertFalse(claude_login.account_login_ready(Path("/tmp/claude")))

    def test_access_expiry_does_not_override_a_valid_refresh_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "expired-but-normal",
                            "expiresAt": 1_700_000_000_000,
                            "refreshToken": "must-not-leak",
                            "refreshTokenExpiresAt": 1_900_000_000_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(
                    claude_login,
                    "_auth_status",
                    return_value={"loggedIn": True, "authMethod": "oauth"},
                ),
                patch.object(claude_login.time, "time", return_value=1_800_000_000.0),
            ):
                self.assertTrue(claude_login.account_login_ready(home))

    def test_final_oauth_failure_marker_is_fail_closed_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "must-not-leak",
                            "expiresAt": 1_700_000_000_000,
                            "refreshToken": "also-secret",
                            "refreshTokenExpiresAt": 1_900_000_000_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            auth = {
                "loggedIn": True,
                "authMethod": "oauth",
                "apiProvider": "firstParty",
                "subscriptionType": "max",
            }
            with (
                patch.object(claude_login, "account_home", return_value=home),
                patch.object(claude_login, "_auth_status", return_value=auth),
                patch.object(claude_login, "claude_binary", return_value="/bin/claude"),
                patch.object(claude_login.time, "time", return_value=1_800_000_000.0),
            ):
                claude_login.mark_reauth_required(
                    home, reason="oauth_refresh_failed"
                )
                self.assertFalse(claude_login.account_login_ready(home))
                result = claude_login.status(7)
                marker = home / claude_login._REAUTH_MARKER_NAME
                self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
                marker_payload = marker.read_text(encoding="utf-8")
                claude_login.clear_reauth_required(home)
                self.assertTrue(claude_login.account_login_ready(home))
        self.assertFalse(result["connected"])
        self.assertTrue(result["reauth_required"])
        self.assertTrue(result["auth_expired"])
        self.assertEqual(result["models"], [])
        self.assertNotIn("expiresAt", result)
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertNotIn("also-secret", json.dumps(result))
        self.assertNotIn("must-not-leak", marker_payload)
        self.assertNotIn("also-secret", marker_payload)

    def test_successful_official_login_clears_reauth_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "refreshToken": "secret",
                            "refreshTokenExpiresAt": 1_900_000_000_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            auth = {"loggedIn": True, "authMethod": "oauth"}
            proc = SimpleNamespace(stdout=[], wait=lambda timeout: 0)
            with (
                patch.object(claude_login, "_auth_status", return_value=auth),
                patch.object(claude_login.time, "time", return_value=1_800_000_000.0),
            ):
                claude_login.mark_reauth_required(home)
                with claude_login._lock:
                    claude_login._states[7] = {"status": "running", "running": True, "proc": proc}
                claude_login._reader(proc, home, 7)
            self.assertFalse((home / claude_login._REAUTH_MARKER_NAME).exists())
            with claude_login._lock:
                self.assertEqual(claude_login._states[7]["status"], "success")

    def test_a_replaced_login_reader_leaves_the_newer_login_alone(self) -> None:
        # Cancel and Connect again while the old CLI is still being read: the
        # old reader must not touch the new login or its reauth marker.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "refreshToken": "secret",
                            "refreshTokenExpiresAt": 1_900_000_000_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            newer = SimpleNamespace(stdin=io.StringIO())
            replaced = {"status": "running", "running": True, "url": None, "awaiting_code": False, "proc": newer}

            def old_output():
                yield "https://claude.ai/oauth/authorize?state=first\n"
                with claude_login._lock:
                    claude_login._states[7] = dict(replaced)
                yield "https://claude.ai/oauth/authorize?state=stale\n"

            old = SimpleNamespace(stdout=old_output(), wait=lambda timeout: 0)
            with (
                patch.object(
                    claude_login,
                    "_auth_status",
                    return_value={"loggedIn": True, "authMethod": "oauth"},
                ),
                patch.object(claude_login.time, "time", return_value=1_800_000_000.0),
            ):
                claude_login.mark_reauth_required(home)
                with claude_login._lock:
                    claude_login._states[7] = {"status": "running", "running": True, "proc": old}
                claude_login._reader(old, home, 7)
            self.assertTrue((home / claude_login._REAUTH_MARKER_NAME).exists())
            with claude_login._lock:
                state = claude_login._states[7]
                self.assertIs(state.get("proc"), newer)
                self.assertEqual({k: v for k, v in state.items() if k != "proc"},
                                 {k: v for k, v in replaced.items() if k != "proc"})

    def test_connect_cancel_connect_keeps_the_second_sign_in_usable(self) -> None:
        user_id = 71
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text(FAKE_LOGIN_CLI, encoding="utf-8")
            fake.chmod(0o755)
            checking = threading.Event()
            release = threading.Event()

            def auth_status(home, *, force=False):
                # The reader's check after its CLI exits stands for the slow
                # `claude auth status` run, and holds there until released.
                if force and not checking.is_set():
                    checking.set()
                    release.wait(10)
                signed_in = (Path(home) / ".credentials.json").is_file()
                return {"loggedIn": signed_in, "authMethod": "claude.ai" if signed_in else "none"}

            def login_proc():
                with claude_login._lock:
                    return claude_login._states[user_id].get("proc")

            def reader_threads():
                return {t for t in threading.enumerate() if t.name == f"claude-login-{user_id}"}

            procs = []
            # The readers must finish while the fakes are patched in: the real
            # lookup would find an installed `claude`.
            with (
                patch.dict(os.environ, {"AGENT_MONITOR_CLAUDE_ACCOUNT_HOME": str(Path(tmp) / "homes")}),
                patch.object(claude_login, "claude_binary", return_value=str(fake)),
                patch.object(claude_login, "_auth_status", side_effect=auth_status),
            ):
                try:
                    claude_login.start_login(user_id)
                    procs.append(login_proc())
                    old_readers = reader_threads()
                    self.assertEqual(claude_login.cancel_login(user_id)["status"], "cancelled")
                    self.assertTrue(checking.wait(10))
                    self.assertEqual(claude_login.start_login(user_id)["status"], "running")
                    procs.append(login_proc())
                    new_readers = reader_threads() - old_readers
                    release.set()
                    for thread in old_readers:
                        thread.join(10)
                    with claude_login._lock:
                        state = dict(claude_login._states[user_id])
                    self.assertEqual(state["status"], "running")
                    self.assertIs(state.get("proc"), procs[1])
                    self.assertNotIn("error", state)
                    claude_login.submit_code(user_id, "one-time-code#state")
                    self.assertEqual(procs[1].wait(10), 0)
                    for thread in new_readers:
                        thread.join(10)
                    final = claude_login.status(user_id)
                finally:
                    release.set()
                    for proc in procs:
                        if proc is not None and proc.poll() is None:
                            proc.kill()
                    for thread in reader_threads():
                        thread.join(10)
            self.assertEqual(final["status"], "success")
            self.assertTrue(final["connected"])
            self.assertEqual(procs[0].returncode, -15)

    def test_a_probe_started_before_a_newer_one_never_replaces_its_answer(self) -> None:
        # A poll's `claude auth status` that began before the sign-in saved its
        # credentials can finish after the login reader's own check: the
        # reader's newer answer must stay cached, or polls report the finished
        # sign-in as not connected.
        probes = []
        started = threading.Event()
        release = threading.Event()

        def probe(home):
            probes.append(home)
            if len(probes) == 1:
                started.set()
                release.wait(10)
                return {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}
            return {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(claude_login, "_run_auth_status", side_effect=probe),
        ):
            home = Path(tmp)
            slow: dict = {}
            poll = threading.Thread(target=lambda: slow.update(claude_login._auth_status(home)))
            poll.start()
            try:
                self.assertTrue(started.wait(10))
                fresh = claude_login._auth_status(home, force=True)
            finally:
                release.set()
                poll.join(10)
            cached = claude_login._auth_status(home)
        self.assertFalse(slow["loggedIn"])
        self.assertTrue(fresh["loggedIn"])
        self.assertTrue(cached["loggedIn"])
        self.assertEqual(len(probes), 2)

    def test_expired_refresh_token_is_fail_closed_without_model_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "refreshToken": "secret",
                            "refreshTokenExpiresAt": 1_700_000_000_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(
                    claude_login,
                    "_auth_status",
                    return_value={"loggedIn": True, "authMethod": "oauth"},
                ),
                patch.object(claude_login.time, "time", return_value=1_800_000_000.0),
            ):
                self.assertFalse(claude_login.account_login_ready(home))

    def test_malformed_refresh_metadata_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".credentials.json").write_text("not json", encoding="utf-8")
            with patch.object(
                claude_login,
                "_auth_status",
                return_value={"loggedIn": True, "authMethod": "oauth"},
            ):
                self.assertFalse(claude_login.account_login_ready(home))

    def test_oauth_code_is_only_written_to_stdin(self) -> None:
        stdin = io.StringIO()
        proc = SimpleNamespace(stdin=stdin)
        with claude_login._lock:
            claude_login._states[17] = {
                "status": "running",
                "running": True,
                "proc": proc,
                "output": "should-not-leak",
            }
        with patch.object(
            claude_login,
            "_augment",
            side_effect=lambda _uid, snapshot: claude_login._public_state(snapshot),
        ):
            result = claude_login.submit_code(17, "one-time-secret#state")
        self.assertEqual(stdin.getvalue(), "one-time-secret#state\n")
        self.assertNotIn("one-time-secret", json.dumps(result))
        self.assertNotIn("output", result)
        with claude_login._lock:
            self.assertNotIn("one-time-secret", json.dumps(claude_login._states[17], default=str))


# What `claude auth login --claudeai` prints before it reads the code.  The
# prompt ends without a newline, and the link's state is opaque text.
_LOGIN_PROMPT = """[ "$1 $2" = "auth login" ] || exit 2
echo "Opening browser to sign in…"
echo "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=error-failed"
printf "Paste code here if prompted > "
read -r code
"""


class ClaudeLoginFailureReasonTests(unittest.TestCase):
    USER_ID = 4545

    def _login(self, script: str) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            claude = root / "claude"
            claude.write_text("#!/bin/sh\n" + _LOGIN_PROMPT + script, encoding="utf-8")
            claude.chmod(0o755)
            self.addCleanup(lambda: claude_login._states.pop(self.USER_ID, None))
            # The reader must finish while the fake is patched in: the real
            # lookup would find an installed `claude`.
            with (
                patch.dict(os.environ, {"AGENT_MONITOR_CLAUDE_ACCOUNT_HOME": str(root / "homes")}),
                patch.object(claude_login, "claude_binary", return_value=str(claude)),
            ):
                status = claude_login.start_login(self.USER_ID)
                claude_login.submit_code(self.USER_ID, "wrong-code#state")
                deadline = time.monotonic() + 10
                while status["running"] and time.monotonic() < deadline:
                    time.sleep(0.05)
                    status = claude_login.status(self.USER_ID)
        self.assertEqual(status["status"], "error")
        self.assertFalse(status["connected"])
        self.assertNotIn("https://", status["error"])
        self.assertNotIn("wrong-code", status["error"])
        return status

    def test_a_rejected_code_reports_the_clis_reason(self) -> None:
        # The CLI writes its error on stderr, which joins the unfinished prompt line.
        status = self._login('echo "Login failed: Request failed with status code 400" >&2\nexit 1\n')
        self.assertEqual(
            status["error"],
            "claude auth login exited with code 1: Login failed: Request failed with status code 400",
        )

    def test_a_reason_on_its_own_line_is_reported(self) -> None:
        status = self._login('printf "\\nOAuth error: Invalid code\\n"\nexit 1\n')
        self.assertEqual(status["error"], "claude auth login exited with code 1: OAuth error: Invalid code")

    def test_exit_without_a_reason_keeps_the_exit_code_message(self) -> None:
        status = self._login("exit 3\n")
        self.assertEqual(status["error"], "claude auth login exited with code 3")

    def test_success_exit_without_a_saved_login_is_explained(self) -> None:
        status = self._login('echo "Login successful."\nexit 0\n')
        self.assertEqual(
            status["error"], "claude auth login finished but no usable Claude sign-in was saved"
        )

    def test_a_code_without_its_state_is_refused_and_the_sign_in_waits_for_another(self) -> None:
        # claude 2.1.301 takes a pasted line only as code#state: it answers any
        # other line on stderr and keeps waiting, without exiting.  This fake
        # also records each line it reads.
        script = """
while true; do
  echo "$code" >> "$CLAUDE_CONFIG_DIR/received"
  case "$code" in ?*"#"?*) break ;; esac
  echo "Invalid code. Please make sure the full code was copied." >&2
  read -r code || exit 1
done
printf '{"claudeAiOauth":{"refreshToken":"test-refresh","refreshTokenExpiresAt":4102444800000}}' \\
  > "$CLAUDE_CONFIG_DIR/.credentials.json"
"""

        def auth_status(home, *, force=False):
            signed_in = (Path(home) / ".credentials.json").is_file()
            return {"loggedIn": signed_in, "authMethod": "claude.ai" if signed_in else "none"}

        def wait_for(condition) -> dict:
            deadline = time.monotonic() + 10
            status = claude_login.status(self.USER_ID)
            while not condition(status) and time.monotonic() < deadline:
                time.sleep(0.05)
                status = claude_login.status(self.USER_ID)
            return status

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            claude = root / "claude"
            claude.write_text("#!/bin/sh\n" + _LOGIN_PROMPT + script, encoding="utf-8")
            claude.chmod(0o755)
            self.addCleanup(lambda: claude_login._states.pop(self.USER_ID, None))
            # The reader must finish while the fakes are patched in: the real
            # lookup would find an installed `claude`.
            with (
                patch.dict(os.environ, {"AGENT_MONITOR_CLAUDE_ACCOUNT_HOME": str(root / "homes")}),
                patch.object(claude_login, "claude_binary", return_value=str(claude)),
                patch.object(claude_login, "_auth_status", side_effect=auth_status),
            ):
                try:
                    claude_login.start_login(self.USER_ID)
                    self.assertTrue(wait_for(lambda status: status.get("awaiting_code"))["awaiting_code"])
                    for partial in ("only-the-code", "only-the-code#", "#only-the-state"):
                        with self.subTest(code=partial), self.assertRaisesRegex(
                            ValueError, r"^Invalid code\. Please make sure the full code was copied"
                        ):
                            claude_login.submit_code(self.USER_ID, partial)
                    waiting = claude_login.status(self.USER_ID)
                    claude_login.submit_code(self.USER_ID, "the-code#the-state")
                    final = wait_for(lambda status: not status["running"])
                    received = (claude_login.account_home(self.USER_ID) / "received").read_text()
                finally:
                    if claude_login.status(self.USER_ID)["running"]:
                        claude_login.cancel_login(self.USER_ID)
                    for thread in threading.enumerate():
                        if thread.name == f"claude-login-{self.USER_ID}":
                            thread.join(10)
        # The card keeps the code step, and the CLI only ever saw the whole code.
        self.assertEqual(
            (waiting["status"], waiting["awaiting_code"], waiting["code_submitted"]),
            ("running", True, False),
        )
        self.assertEqual(final["status"], "success")
        self.assertTrue(final["connected"])
        self.assertEqual(received, "the-code#the-state\n")


class ClaudeAccountRuntimeSettingsTests(unittest.TestCase):
    def test_account_runtime_is_mutually_exclusive_and_legacy_compatible(self) -> None:
        self.assertEqual(settings.account_runtime(env={"AGENT_MONITOR_USE_CODEX": "0"}), "api")
        self.assertEqual(settings.account_runtime(env={"AGENT_MONITOR_USE_CODEX": "1"}), "codex")
        self.assertEqual(settings.account_runtime(env={"AGENT_MONITOR_USE_CLAUDE": "1"}), "claude")
        env = {
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
            "AGENT_MONITOR_USE_CODEX": "1",
            "AGENT_MONITOR_USE_CLAUDE": "1",
        }
        self.assertEqual(settings.account_runtime(env=env), "api")
        self.assertFalse(settings.codex_enabled(env=env))
        self.assertFalse(settings.claude_enabled(env=env))

    # Subscription runtimes belong to the site administrator; a member's
    # settings always report the API route (tests/test_member_accounts.py).
    def test_claude_runtime_exposes_only_account_aliases_for_claude(self) -> None:
        env = {"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"}
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch("agent_monitor.codex_login.account_login_ready", return_value=True),
            patch("agent_monitor.codex_login.available_models", return_value=["gpt-account"]),
            patch("agent_monitor.claude_login.account_login_ready", return_value=True),
        ):
            result = settings.get_settings({"id": 7, "is_admin": True})
        self.assertEqual(result["account_runtime"], "claude")
        self.assertTrue(result["claude_enabled"])
        self.assertFalse(result["codex_enabled"])
        self.assertTrue(result["claude_connected"])
        self.assertEqual(
            result["claude_models"],
            ["sonnet", "opus", "fable", "claude-haiku-4-5"],
        )
        self.assertEqual(result["codex_models"], ["gpt-account"])
        self.assertEqual(result["model_presets"], result["claude_models"])
        self.assertEqual(result["models_by_runtime"]["codex"], ["gpt-account"])
        self.assertFalse(result["api_providers_active"])
        self.assertEqual(result["auth_mode"], "claude_subscription")

    def test_expired_claude_runtime_stays_selected_but_not_ready(self) -> None:
        env = {"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"}
        with (
            patch.object(settings, "resolved_user_env", return_value=env),
            patch("agent_monitor.codex_login.account_login_ready", return_value=False),
            patch("agent_monitor.claude_login.account_login_ready", return_value=False),
            patch(
                "agent_monitor.claude_login.account_reauth_required",
                return_value=True,
            ),
        ):
            result = settings.get_settings({"id": 7, "is_admin": True})
        self.assertEqual(result["account_runtime"], "claude")
        self.assertEqual(result["auth_mode"], "claude_subscription")
        self.assertFalse(result["runtime_ready"])
        self.assertFalse(result["claude_connected"])
        self.assertTrue(result["claude_reauth_required"])
        self.assertEqual(result["claude_models"], [])
        self.assertEqual(result["model_presets"], [])

    def test_save_runtime_writes_mutually_exclusive_flags(self) -> None:
        with (
            patch("agent_monitor.auth.set_user_env") as set_env,
            patch.object(settings, "get_settings", return_value={"account_runtime": "claude"}),
        ):
            settings.save_settings({"account_runtime": "claude"}, user={"id": 9, "is_admin": True})
        set_env.assert_called_once_with(
            9,
            {
                "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
                "AGENT_MONITOR_USE_CODEX": "0",
                "AGENT_MONITOR_USE_CLAUDE": "1",
            },
            clear=[],
        )

    def test_conflicting_legacy_toggles_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot both"):
            settings.save_settings({"codex_enabled": True, "claude_enabled": True}, user={"id": 9})


class ClaudeSettingsRouteTests(unittest.TestCase):
    def test_code_endpoint_uses_authenticated_user_not_body_user_id(self) -> None:
        body = json.dumps({"user_id": 999, "code": "oauth-once"}).encode()
        handler = object.__new__(console_server.Handler)
        handler.path = "/api/settings/claude/login/code"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._resolve_path = lambda: "/api/settings/claude/login/code"
        handler._require_user = lambda _path: {"id": 42, "is_admin": True}
        sent: list[tuple[int, dict]] = []
        handler._send = lambda code, payload, *args, **kwargs: sent.append((code, json.loads(payload)))
        with patch.object(claude_login, "submit_code", return_value={"status": "running"}) as submit:
            handler.do_POST()
        submit.assert_called_once_with(42, "oauth-once")
        self.assertEqual(sent[0][0], 200)


# A stand-in for `claude` that records each run (`claude auth status` of the
# installed CLI would contact Anthropic).
RECORDING_CLI = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/runs.log"
printf '{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty"}\\n'
"""


def test_the_suite_never_runs_the_installed_clis_sign_in_probe(tmp_path, monkeypatch, tmp_path_factory):
    """tests/conftest.py runs a test's own fake CLI for the probe (a file
    under the session's temporary directories), and never the Claude Code
    CLI installed on the host, whose `auth status` would contact Anthropic
    (the next test: it answers "not signed in")."""
    import conftest

    roots = conftest._temporary_roots(tmp_path_factory)
    # The CLI this host has where the console looks for it (on a host without one there is
    # nothing to guard) is no test's own.
    installed = claude_login.claude_binary()
    if installed:
        assert not conftest._made_by_a_test(installed, roots), installed
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir()
    cli.write_text(RECORDING_CLI, encoding="utf-8")
    cli.chmod(0o755)
    runs = tmp_path / "bin" / "runs.log"
    assert conftest._made_by_a_test(str(cli), roots)
    monkeypatch.setattr(claude_login, "claude_binary", lambda: str(cli))
    # A test's own fake: the real probe runs it.
    assert claude_login._run_auth_status(tmp_path / "home")["loggedIn"] is True
    assert runs.read_text(encoding="utf-8").splitlines() == ["auth status --json"]


# A session of its own whose HOME holds an installed `claude`: a symlink into
# the CLI's versions, as Claude Code's installer makes it, which its updater
# re-points at a new version while the session runs.
INSTALLED_PROBE_TEST = """
import os
from pathlib import Path

from agent_monitor import claude_login

SIGNED_OUT = {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}


def test_the_installed_cli_is_never_probed(tmp_path):
    link = Path(os.environ["HOME"]) / ".local" / "bin" / "claude"
    assert claude_login.claude_binary() == str(link)
    assert claude_login._run_auth_status(tmp_path / "account") == SIGNED_OUT
    # The updater re-points the installed CLI at a new version.
    link.unlink()
    link.symlink_to(os.environ["NEXT_CLAUDE_VERSION"])
    assert claude_login._run_auth_status(tmp_path / "account") == SIGNED_OUT
"""


def test_the_installed_cli_is_never_probed_even_after_its_updater_re_points_it(tmp_path):
    """The guard of tests/conftest.py, in a pytest session of its own (the
    files below lie outside that session's temporary directories, as an
    installed CLI does): neither the version its ``claude`` points at when
    the session starts nor the one an auto-update re-points it at meanwhile
    is ever run (each stand-in records a run).  A guard that knew only the
    versions installed when the session started ran the new one."""
    import subprocess
    import sys

    home, versions, runs = tmp_path / "home", tmp_path / "versions", tmp_path / "runs.log"
    for version in ("1", "2"):
        stand_in = versions / version / "claude"
        stand_in.parent.mkdir(parents=True)
        stand_in.write_text(f'#!/bin/sh\nprintf "{version} %s\\n" "$*" >> "{runs}"\n'
                            "printf '{\"loggedIn\": true, \"authMethod\": \"claude.ai\"}\\n'\n", encoding="utf-8")
        stand_in.chmod(0o755)
    link = home / ".local" / "bin" / "claude"
    link.parent.mkdir(parents=True)
    link.symlink_to(versions / "1" / "claude")
    inner = tmp_path / "session"
    inner.mkdir()
    (inner / "test_installed_probe.py").write_text(INSTALLED_PROBE_TEST, encoding="utf-8")
    (inner / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "session-tmp").mkdir()
    repo = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.defpath, "HOME": str(home), "TMPDIR": str(tmp_path / "session-tmp"),
        "PYTHONPATH": os.pathsep.join([str(repo / "tests"), str(repo)]), "PYTHONDONTWRITEBYTECODE": "1",
        "NEXT_CLAUDE_VERSION": str(versions / "2" / "claude"),
        # A network guard this suite runs under guards the session too.
        **{name: os.environ[name] for name in ("LD_PRELOAD", "NETBLOCK_LOG") if os.environ.get(name)},
    }
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider", "-q",
         "-c", str(inner / "pytest.ini"), "--rootdir", str(inner), "--basetemp", str(tmp_path / "session-pt"),
         str(inner / "test_installed_probe.py")],
        cwd=repo, env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    assert completed.returncode == 0, (completed.stdout + completed.stderr)[-4000:]
    assert not runs.exists(), runs.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
