"""Keep every test away from the checkout's real data directory and .env.

Several modules compute state paths at import time: ``agent_monitor.DATA_DIR``
from ``AGENT_MONITOR_DATA_DIR`` (``auth.DB_PATH``, ``research.DB_PATH``, ...),
and ``settings.ENV_PATH`` / the console's dotenv load from
``AGENT_MONITOR_ENV_PATH``.  Point both variables at a private session
directory *before* any ``agent_monitor`` import, then give each test its own
account database, ledgers, server ``.env`` and system-settings overlay.

The per-test patches run before a test's own fixtures and ``patch.object``
calls, so tests that already isolate these paths keep working unchanged.

Nothing reaches the network either, however the suite is run:

* ``claude auth status`` (``claude_login._run_auth_status``) contacts
  Anthropic: the probe runs only a fake CLI a test made (a file under the
  session's temporary directories, found through ``claude_binary``,
  ``which_tool``, ``PATH`` or ``HOME``), and answers "not signed in" for any
  other ``claude`` without running it, the one installed on this host
  included, whatever version its auto-updater put behind that path since the
  session started.  A test of the real probe is marked
  ``real_claude_auth_status``.
* The price table (``llm_cost_tracker.pricing``) is :data:`PRICE_TABLE`, a
  fixed LiteLLM-shaped table, never the live one the module would download
  from GitHub; a test that needs other prices installs its own
  (``tests/test_engine_cost_model.py``).
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
from agent_monitor.paths import ensure_import_paths  # noqa: E402

ensure_import_paths()  # monitor_core's llm_cost_tracker, as the console has it

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
def _restricted_members_by_default(monkeypatch):
    # The site gives every member full access by default; most tests are
    # about a restricted member (one an administrator switched off), so they
    # start from that.  tests/test_full_access.py checks the default itself.
    monkeypatch.setenv("AGENT_MONITOR_FULL_ACCESS_DEFAULT", "0")


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


# ---------------------------------------------------------------------------
# no network: the installed Claude Code CLI's sign-in probe, the price table
# ---------------------------------------------------------------------------


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_claude_auth_status: the test runs claude_login's real `claude auth status` probe "
        "(on a fake CLI of its own; the installed one contacts Anthropic)",
    )


def _temporary_roots(tmp_path_factory) -> tuple[str, ...]:
    """Where the session's tests make their files: pytest's base temporary
    directory (``tmp_path``) and the system's (``tempfile``, which the
    tests' ``TemporaryDirectory`` and this module's session root use)."""
    roots = {tempfile.gettempdir(), str(tmp_path_factory.getbasetemp())}
    return tuple(sorted({os.path.realpath(root) for root in roots}))


def _made_by_a_test(binary: str, roots: tuple[str, ...]) -> bool:
    """Whether ``binary`` (its real path) is a file under ``roots``: a fake
    CLI a test made, never one installed on the host."""
    real = os.path.realpath(binary)
    return any(os.path.commonpath([real, root]) == root for root in roots)


_SIGNED_OUT = {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}


@pytest.fixture(autouse=True)
def _no_installed_claude_auth_probe(request, monkeypatch, tmp_path_factory):
    if request.node.get_closest_marker("real_claude_auth_status"):
        return
    claude_login = _optional_module("agent_monitor.claude_login")
    if claude_login is None or not hasattr(claude_login, "_run_auth_status"):
        return
    probe = claude_login._run_auth_status
    roots = _temporary_roots(tmp_path_factory)

    def run_auth_status(home):
        binary = claude_login.claude_binary()
        if binary and not _made_by_a_test(binary, roots):
            # Any other `claude` is a real install, whatever version an
            # auto-update has put behind its path meanwhile: its `auth status`
            # would contact Anthropic, and a home no real sign-in reached is
            # signed out.
            return dict(_SIGNED_OUT)
        return probe(home)

    monkeypatch.setattr(claude_login, "_run_auth_status", run_auth_status)


def _price(inp: float, out: float, cache_read: float, cache_write: float | None = None) -> dict:
    """A LiteLLM entry from prices per million tokens."""
    entry = {"input_cost_per_token": inp / 1e6, "output_cost_per_token": out / 1e6,
             "cache_read_input_token_cost": cache_read / 1e6}
    if cache_write is not None:
        entry["cache_creation_input_token_cost"] = cache_write / 1e6
    return entry


# Synthetic prices (per million: input, output, cache read, cache write) for
# the models the console offers (settings.PROVIDERS) and Max (API)'s, Kimi K3
# and gpt-5.2 under the keys pricing's aliases name (moonshot/kimi-k3,
# azure/gpt-5.2), and gpt-4o, the fallback of jobs._estimate_cost.
PRICE_TABLE = {
    "gpt-4o": _price(2.5, 10, 1.25),
    "gpt-6-astra": _price(10, 50, 1),
    "gpt-6.1-sol": _price(2, 10, 0.2),
    "gpt-6-sol": _price(1.5, 7.5, 0.15),
    "gpt-6-luna": _price(0.5, 2.5, 0.05),
    "chat-latest": _price(5, 15, 0.5),
    "gpt-5.6-sol": _price(1.75, 14, 0.175),
    "gpt-5.6-terra": _price(1, 8, 0.1),
    "gpt-5.6-luna": _price(0.25, 2, 0.025),
    "gpt-5.5-2026-04-23": _price(5, 30, 0.5),
    "azure/gpt-5.2": _price(1.75, 14, 0.175),
    "gpt-5.2-2025-12-11": _price(1.75, 14, 0.175),
    "gpt-5-mini-2025-08-07": _price(0.25, 2, 0.025),
    "moonshot/kimi-k3": _price(3, 15, 0.3),
    "claude-fable-5-1": _price(10, 50, 0.25, 12.5),
    "claude-opus-5-5": _price(4, 20, 0.2, 5),
    "claude-sonnet-5-5": _price(2, 10, 0.2, 2.5),
    "claude-opus-5": _price(5, 25, 0.5, 6.25),
    "claude-fable-5": _price(8, 40, 0.8, 10),
    "claude-opus-4-8": _price(5, 25, 0.5, 6.25),
    "claude-sonnet-5": _price(3, 15, 0.3, 3.75),
    "claude-haiku-4-5": _price(1, 5, 0.1, 1.25),
    "gemini-3.6-flash": _price(0.3, 2.5, 0.03),
    "gemini-3.1-pro-preview": _price(2, 12, 0.2),
    "deepseek-v4-flash": _price(0.3, 1.2, 0.03),
    "deepseek-v4-pro": _price(1, 4, 0.1),
    "xai/grok-4.5": _price(3, 15, 0.75),
}


def _refuse_download(*_args, **_kwargs):
    raise OSError("tests never download the live price table (tests/conftest.py)")


@pytest.fixture(autouse=True)
def _fixed_price_table(monkeypatch):
    from types import SimpleNamespace

    for name in ("llm_cost_tracker.pricing", "monitor_core.llm_cost_tracker.pricing"):
        pricing = _optional_module(name)
        if pricing is None or not hasattr(pricing, "_pricing_cache"):
            continue
        monkeypatch.setattr(pricing, "_pricing_cache", {key: dict(value) for key, value in PRICE_TABLE.items()})
        # Even a test that empties the cache never reaches GitHub.
        monkeypatch.setattr(pricing, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=_refuse_download)))
