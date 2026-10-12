"""Keep every test away from the checkout's real data directory and .env.

Several modules compute state paths at import time: ``agent_monitor.DATA_DIR``
from ``AGENT_MONITOR_DATA_DIR`` (``auth.DB_PATH``, ``research.DB_PATH``, ...),
and ``settings.ENV_PATH`` / the console's dotenv load from
``AGENT_MONITOR_ENV_PATH``.  Point both variables at a private session
directory *before* any ``agent_monitor`` import, then give each test its own
account database, ledgers, server ``.env`` and system-settings overlay.

The per-test patches run before a test's own fixtures and ``patch.object``
calls, so tests that already isolate these paths keep working unchanged.
"""

import atexit
import importlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_SESSION_ROOT = Path(tempfile.mkdtemp(prefix="agent-monitor-tests-"))
_SESSION_ENV = _SESSION_ROOT / "server.env"
_SESSION_ENV.touch(mode=0o600)
# Always override: a developer shell (or a service environment) may point these
# at production state, and a test run must never read or write it.
os.environ["AGENT_MONITOR_DATA_DIR"] = str(_SESSION_ROOT / "data")
os.environ["AGENT_MONITOR_ENV_PATH"] = str(_SESSION_ENV)
atexit.register(shutil.rmtree, _SESSION_ROOT, ignore_errors=True)

from agent_monitor import usage_ledger  # noqa: E402  (must follow the env override)

# (module, attribute, per-test file name).  Missing modules/attributes are
# skipped so an unrelated import error cannot fail the whole suite here.
_STATE_PATHS = (
    ("agent_monitor.auth", "DB_PATH", "users.db"),
    ("agent_monitor.settings", "ENV_PATH", "server.env"),
    ("agent_monitor.settings", "SYSTEM_SETTINGS_PATH", "system_settings.json"),
    ("agent_monitor.research", "DB_PATH", "research.sqlite3"),
    ("agent_monitor.projects", "DB_PATH", "projects.sqlite3"),
    ("agent_monitor.run_sharing", "DB_PATH", "run_shares.sqlite3"),
)


def _optional_module(name: str):
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - the owning test reports the real error
        return None


@pytest.fixture(autouse=True)
def _isolated_usage_ledger(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(usage_ledger, "DB_PATH", tmp_path_factory.mktemp("ledger") / "usage.db")
    monkeypatch.setattr(usage_ledger, "_INITIALIZED", set())
    monkeypatch.setattr(usage_ledger, "_OWNER_CACHE", {})


@pytest.fixture(autouse=True)
def _isolated_state_paths(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("state")
    for module_name, attribute, filename in _STATE_PATHS:
        module = _optional_module(module_name)
        if module is not None and hasattr(module, attribute):
            monkeypatch.setattr(module, attribute, root / filename)
    auth = _optional_module("agent_monitor.auth")
    if auth is not None and hasattr(auth, "_INITIALIZED"):
        # The schema flag belongs to the database path patched above.
        monkeypatch.setattr(auth, "_INITIALIZED", False)
    yield root
