"""Codex on the API-key route: a short-lived login of its own, never a saved one.

A fake ``codex`` first on a minimal PATH records what each invocation saw;
the service's own tool directories are not searched, so the real CLI never
runs, and nothing here contacts a provider.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from agent_monitor import CACHE_DIR, jobs

# `codex login --with-api-key` stores the key in $CODEX_HOME/auth.json.
_FAKE_CODEX = """#!PYTHON
import json, os, sys
home = os.environ.get("CODEX_HOME") or ""
if sys.argv[1:3] == ["login", "--with-api-key"]:
    with open(os.path.join(home, "auth.json"), "w") as handle:
        json.dump({"OPENAI_API_KEY": sys.stdin.read().strip()}, handle)
    sys.exit(0)
auth = os.path.join(home, "auth.json")
with open(os.environ["FAKE_CODEX_LOG"], "a") as log:
    log.write(json.dumps({
        "CODEX_HOME": home or None,
        "files": sorted(os.listdir(home)) if home and os.path.isdir(home) else None,
        "auth": open(auth).read() if home and os.path.exists(auth) else None,
    }) + "\\n")
print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
"""


@pytest.fixture()
def fake_codex(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(_FAKE_CODEX.replace("PYTHON", sys.executable), encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setattr("agent_monitor.engines_registry.engine_extra_path", lambda _engine: [])
    monkeypatch.setattr("agent_monitor.engines_registry.which_tool", lambda _name: None)
    monkeypatch.setattr(
        "agent_monitor.engines_registry.build_cli_command",
        lambda _engine, *, prompt, **_kwargs: [str(codex), "exec", "--json", prompt],
    )
    monkeypatch.setattr("agent_monitor.agent_config.persona_preamble", lambda **_kwargs: "")
    monkeypatch.setattr(jobs, "_write_run", lambda _run: None)
    log = tmp_path / "codex.jsonl"
    return {
        "log": log,
        "env": {
            "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
            "FAKE_CODEX_LOG": str(log),
            "AGENT_MONITOR_AUTH_MODE": "api_key",
            "AGENT_MONITOR_USE_CODEX": "0",
        },
    }


def _codex_runs(fake: dict) -> list[dict]:
    return [json.loads(line) for line in fake["log"].read_text(encoding="utf-8").splitlines()]


def _run_codex(fake: dict, workspace: Path, **extra_env: str) -> dict:
    workspace.mkdir(parents=True, exist_ok=True)
    return jobs._run_cli_engine(
        run_id=f"codex_{workspace.name}",
        engine="codex",
        problem_id="p",
        problem_text="Prove that 1 = 1.",
        started=time.time(),
        workspace=workspace,
        extra_env={**fake["env"], **extra_env},
        requested_model="gpt-6-astra",
        owner_id=7,
    )


def _files_mentioning(root: Path, text: str) -> list[Path]:
    if not root.exists():
        return []
    return [
        path for path in root.rglob("*")
        if path.is_file() and text in path.read_text(encoding="utf-8", errors="replace")
    ]


def test_api_key_codex_runs_log_in_to_a_private_home_removed_after_the_run(fake_codex, tmp_path):
    data_dir = CACHE_DIR.parent
    for key in ("sk-test-old-leaked-1111", "sk-test-rotated-2222"):
        _run_codex(fake_codex, tmp_path / key[-4:], OPENAI_API_KEY=key)
        seen = _codex_runs(fake_codex)[-1]
        home = Path(seen["CODEX_HOME"])
        # Codex ran on the run's own key, from a home outside the data directory...
        assert json.loads(seen["auth"]) == {"OPENAI_API_KEY": key}
        assert not home.is_relative_to(data_dir)
        # ...which is gone once it exits, so no copy outlives the run or a rotation.
        assert not home.exists()
        assert _files_mentioning(data_dir / "codex_home", key) == []


def test_start_up_removes_the_per_key_logins_earlier_versions_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "data" / "cache")
    root = tmp_path / "data" / "codex_home"
    saved = root / "e5a4f442a4c02cf8"
    account = root / "users" / "user_7"
    other = root / "notes"
    for directory in (saved, account, other):
        directory.mkdir(parents=True)
        (directory / "auth.json").write_text("{}", encoding="utf-8")
    (root / "0123456789abcdef").symlink_to(account, target_is_directory=True)

    assert jobs.remove_legacy_codex_key_homes() == 1
    assert not saved.exists()
    # ChatGPT sign-ins and anything else stay, and a link is never followed.
    assert (account / "auth.json").exists() and (other / "auth.json").exists()
    assert (root / "0123456789abcdef").is_symlink()
    assert jobs.remove_legacy_codex_key_homes() == 0
