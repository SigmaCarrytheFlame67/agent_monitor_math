"""Codex sign-in state and Formal Lean Codex timeouts.

The npm-installed ``codex`` is a Node launcher (``bin/codex.js``) that runs the
native binary as a child and forwards only SIGINT/SIGTERM/SIGHUP to it, so a
timeout that SIGKILLs the launcher alone leaves native Codex running.  The fake
launcher below behaves the same way, and its native child records its pid.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_monitor import codex_login, lean_verify

SOURCE = "theorem main : True := by trivial\n"

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


if __name__ == "__main__":
    unittest.main()
