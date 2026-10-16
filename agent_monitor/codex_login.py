"""ChatGPT/OpenAI-account (OAuth device-code) login for the Codex CLI engine.

By default the ``codex`` engine bills against ``OPENAI_API_KEY`` (metered API
usage). Logging Codex in with a ChatGPT/OpenAI account instead makes runs
consume that account's subscription usage (Plus/Pro/Team/Codex) rather than
per-token API cost. ``agent_monitor.jobs._ensure_codex_auth`` prefers this
login, when present, for that user's ``codex`` engine runs.

Every login is scoped to the signed-in user (own ``CODEX_HOME`` directory, own
in-memory login state) — the same isolation model as per-user API keys in
``agent_monitor.settings``/``agent_monitor.auth``. Callers must always pass the
``user_id`` from the authenticated session; nothing here accepts a caller-
supplied user id from request bodies, so one user can never see, cancel, or
disconnect another user's Codex account.

This module drives ``codex login --device-auth`` as a background subprocess
so the console's Settings UI can show the sign-in URL/code and poll for
completion without blocking the request thread.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from agent_monitor import CACHE_DIR

ACCOUNT_HOME_ROOT = CACHE_DIR.parent / "codex_home" / "users"

_URL_RE = re.compile(r"https?://\S+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CODE_RE = re.compile(r"^[A-Z0-9]{3,6}-[A-Z0-9]{3,6}$")
_MAX_OUTPUT_CHARS = 4000

# A newly completed device login has valid OAuth tokens before Codex has made
# its first model-list request and written models_cache.json. Keep the first
# run usable during that short bootstrap window; the account's real cache takes
# precedence as soon as Codex creates it. This is the visible catalog of the
# vetted CLI (codex-cli 0.160.0, `codex debug models --bundled`), which is also
# what connected accounts report, so the list does not change under the UI
# after the first run. ``refresh_models_cache`` fetches the real catalog right
# after a login.
DEFAULT_CODEX_MODELS = (
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
)
# ``codex debug models`` renders the catalog after refreshing it with the
# account's login (a metadata request, not a model call) and writes
# models_cache.json; its stdout is discarded.
_MODELS_REFRESH_TIMEOUT_S = 30

_lock = threading.Lock()
_states: dict[int, dict[str, Any]] = {}
# Codex records the CLI build that wrote models_cache.json and ignores another
# build's cache, but only the next time it runs for that account. The model
# list below reads the file directly, so after a CLI upgrade it refreshes such
# a cache in the background, once per account and installed build.
_CACHE_CHECKS: set[tuple[str, str, float]] = set()


def _state_for(user_id: int) -> dict[str, Any]:
    """Caller must hold ``_lock``."""
    return _states.setdefault(user_id, {"status": "idle", "running": False})


def account_home(user_id: int) -> Path:
    """Each user gets their own CODEX_HOME — never shared across accounts."""
    root = os.environ.get("AGENT_MONITOR_CODEX_ACCOUNT_HOME")
    base = Path(root).expanduser() if root else ACCOUNT_HOME_ROOT
    return base / f"user_{user_id}"


def account_login_ready(home: Path) -> bool:
    """True if ``home`` holds a ChatGPT-account (OAuth) Codex login.

    An account login's auth.json carries a ``tokens`` bundle (id_token /
    access_token / refresh_token from the ChatGPT OAuth exchange); a bare
    API-key login is just ``{"OPENAI_API_KEY": "..."}`` with no tokens.
    """
    auth_path = home / "auth.json"
    if not auth_path.exists():
        return False
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    tokens = data.get("tokens") or {}
    return bool(tokens.get("access_token") or tokens.get("id_token"))


def _installed_cli_version(codex: str) -> str:
    """Bare version (``0.160.0``) that ``codex --version`` reports."""
    # The npm launcher cannot forward SIGKILL; this helper stops the whole
    # process group (native codex included) when the timeout expires.
    from agent_monitor.runners.codex_backend import _run_codex_process

    with tempfile.TemporaryDirectory() as isolated_home:
        try:
            completed = _run_codex_process(
                [codex, "--version"],
                input="",
                timeout=_MODELS_REFRESH_TIMEOUT_S,
                env={"PATH": os.environ.get("PATH", os.defpath), "HOME": isolated_home,
                     "CODEX_HOME": isolated_home, "NO_COLOR": "1"},
                cwd=isolated_home,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
    return completed.stdout.strip().rsplit(" ", 1)[-1]


def _refresh_cache_from_another_cli(user_id: int, home: Path, cached_version: str) -> None:
    codex = codex_binary()
    if not codex:
        return
    try:
        key = (str(home), codex, os.path.getmtime(codex))
    except OSError:
        return
    with _lock:
        if key in _CACHE_CHECKS:
            return
        _CACHE_CHECKS.add(key)

    def check() -> None:
        installed = _installed_cli_version(codex)
        if installed and installed != cached_version:
            refresh_models_cache(user_id)

    threading.Thread(target=check, daemon=True, name=f"codex-models-{user_id}").start()


def available_models(user_id: int) -> list[str]:
    """Return visible model IDs from this user's Codex cache without reading tokens."""
    home = account_home(user_id)
    logged_in = account_login_ready(home)
    fallback = list(DEFAULT_CODEX_MODELS) if logged_in else []
    cache = home / "models_cache.json"
    if not cache.exists():
        return fallback
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    if logged_in and isinstance(payload, dict):
        _refresh_cache_from_another_cli(user_id, home, str(payload.get("client_version") or ""))
    entries = payload.get("models") if isinstance(payload, dict) else []
    ranked: list[tuple[int, str]] = []
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if not slug or (slug != "gpt-6-astra" and str(item.get("visibility") or "").lower() in {"hide", "hidden"}):
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        ranked.append((rank, slug))
    ranked.sort(key=lambda item: (item[0], item[1]))
    models = list(dict.fromkeys(slug for _, slug in ranked))
    if models and "gpt-6-astra" not in models and any(m.startswith("gpt-5.6-") for m in models):
        models.insert(0, "gpt-6-astra")
    return models or fallback


def codex_binary() -> str | None:
    from agent_monitor.engines_registry import which_tool

    return which_tool("codex")


def refresh_models_cache(user_id: int, *, timeout: float = _MODELS_REFRESH_TIMEOUT_S) -> bool:
    """Ask the Codex CLI to fetch this account's model catalog into its cache.

    Runs right after a successful login so the model list the Settings UI
    shows is the list the server validates runs against (both read
    ``available_models``), instead of the built-in fallback changing after the
    first run. Best effort: no tokens are read here and failures are ignored.
    """
    home = account_home(user_id)
    codex = codex_binary()
    if not codex or not account_login_ready(home):
        return False
    from agent_monitor.runners.codex_backend import _run_codex_process
    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env()
    env["CODEX_HOME"] = str(home)
    env.setdefault("NO_COLOR", "1")
    try:
        # A timeout stops native codex too (see _installed_cli_version); the
        # rendered catalog on stdout is discarded.
        _run_codex_process(
            [codex, "debug", "models"], input="", timeout=timeout, env=env, cwd=None
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (home / "models_cache.json").is_file()


def _augment(user_id: int, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Attach install/connection info so every response shape is self-contained.

    The UI renders directly off whatever a request returns (start/poll/status
    all feed the same render function), so each of them must carry
    ``codex_installed`` / ``connected`` — leaving them out reads as "false" on
    the frontend and wrongly claims Codex isn't installed mid-login.
    """
    home = account_home(user_id)
    snapshot["connected"] = account_login_ready(home)
    snapshot["codex_installed"] = codex_binary() is not None
    snapshot["codex_home"] = str(home)
    return snapshot


def status(user_id: int) -> dict[str, Any]:
    """Snapshot of the current/last login attempt plus static install info."""
    with _lock:
        st = {k: v for k, v in _state_for(user_id).items() if k != "proc"}
    return _augment(user_id, st)


def _reader(proc: subprocess.Popen, home: Path, user_id: int) -> None:
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            # codex CLI emits ANSI color codes even with NO_COLOR=1 set.
            line = _ANSI_RE.sub("", raw_line.rstrip("\n"))
            lines.append(line)
            text = "\n".join(lines)
            with _lock:
                st = _state_for(user_id)
                if st.get("status") != "running":
                    break  # cancelled from elsewhere
                st["output"] = text[-_MAX_OUTPUT_CHARS:]
                urls = _URL_RE.findall(text)
                if urls:
                    st["url"] = urls[-1].rstrip(").,")
                if _CODE_RE.match(line.strip()):
                    st["code"] = line.strip()
    except (OSError, ValueError):
        pass
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
    if rc == 0 and account_login_ready(home):
        # Before reporting success (the UI then reloads its model list), fetch
        # the account's real catalog.
        refresh_models_cache(user_id)
    with _lock:
        st = _state_for(user_id)
        if st.get("status") == "cancelled":
            return
        st["running"] = False
        if rc == 0 and account_login_ready(home):
            st["status"] = "success"
        else:
            st["status"] = "error"
            if rc != 0 and not st.get("error"):
                st["error"] = f"codex login exited with code {rc}"


def start_login(user_id: int) -> dict[str, Any]:
    with _lock:
        st = _state_for(user_id)
        if st.get("status") == "running":
            return _augment(user_id, {k: v for k, v in st.items() if k != "proc"})
        codex = codex_binary()
        if not codex:
            st.clear()
            st.update({"status": "error", "running": False, "error": "codex CLI not installed"})
            return _augment(user_id, dict(st))
        home = account_home(user_id)
        home.mkdir(parents=True, exist_ok=True)
        from agent_monitor.subprocess_env import child_process_env

        env = child_process_env()
        env["CODEX_HOME"] = str(home)
        env.setdefault("NO_COLOR", "1")
        try:
            proc = subprocess.Popen(
                [codex, "login", "--device-auth"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            st.clear()
            st.update({"status": "error", "running": False, "error": str(exc)})
            return _augment(user_id, dict(st))
        st.clear()
        st.update(
            {
                "status": "running",
                "running": True,
                "output": "",
                "url": None,
                "started_at": time.time(),
                "proc": proc,
            }
        )
        snapshot = {k: v for k, v in st.items() if k != "proc"}
    threading.Thread(target=_reader, args=(proc, home, user_id), daemon=True).start()
    return _augment(user_id, snapshot)


def cancel_login(user_id: int) -> dict[str, Any]:
    with _lock:
        st = _state_for(user_id)
        proc: subprocess.Popen | None = st.get("proc")
        st["status"] = "cancelled"
        st["running"] = False
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
    return _augment(user_id, {"status": "cancelled", "running": False})


def poll_login(user_id: int) -> dict[str, Any]:
    with _lock:
        st = {k: v for k, v in _state_for(user_id).items() if k != "proc"}
    return _augment(user_id, st)


def logout(user_id: int) -> dict[str, Any]:
    """Remove the cached account login (does not touch API-key homes)."""
    home = account_home(user_id)
    auth_path = home / "auth.json"
    if auth_path.exists():
        auth_path.unlink()
    return _augment(user_id, {"ok": True})
