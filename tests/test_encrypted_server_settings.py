"""Verify operator-managed settings stay immutable while user settings remain writable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_monitor import ROOT, settings


def test_server_settings_are_read_only_in_encrypted_mode() -> None:
    with patch.dict(
        settings.os.environ,
        {"AGENT_MONITOR_SERVER_SETTINGS_READ_ONLY": "1"},
        clear=False,
    ):
        with pytest.raises(PermissionError, match="operator-managed"):
            settings.save_settings({"updates": {"SMTP_PASSWORD": "new-secret"}})


def test_per_user_settings_remain_writable_in_encrypted_mode() -> None:
    user = {"id": 7, "email": "user@example.test"}
    with (
        patch.dict(
            settings.os.environ,
            {"AGENT_MONITOR_SERVER_SETTINGS_READ_ONLY": "1"},
            clear=False,
        ),
        patch("agent_monitor.auth.set_user_env") as set_user_env,
        patch.object(settings, "get_settings", return_value={"ok": True}),
    ):
        response = settings.save_settings(
            {"updates": {"OPENAI_API_KEY": "user-key"}}, user=user
        )

    set_user_env.assert_called_once()
    assert response == {"ok": True, "settings": {"ok": True}}


def test_system_settings_overlay_follows_the_data_directory(tmp_path: Path) -> None:
    # Module paths are computed at import, so check them in a fresh interpreter.
    probe = (
        "import json\n"
        "from agent_monitor import DATA_DIR, settings\n"
        "print(json.dumps([str(DATA_DIR), str(settings.SYSTEM_SETTINGS_PATH)]))\n"
    )
    env = {**os.environ, "AGENT_MONITOR_DATA_DIR": str(tmp_path / "state")}
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    data_dir, overlay = json.loads(completed.stdout.strip().splitlines()[-1])
    assert Path(data_dir) == (tmp_path / "state").resolve()
    assert Path(overlay) == Path(data_dir) / "system_settings.json"
    assert not Path(overlay).is_relative_to(ROOT / "data")


def test_overlay_is_read_only_from_the_configured_path(tmp_path: Path) -> None:
    overlay = tmp_path / "system_settings.json"
    overlay.write_text(json.dumps({"AGENT_MONITOR_MODEL": "kimi-k3"}), encoding="utf-8")
    with (
        patch.object(settings, "ENV_PATH", tmp_path / "server.env"),
        patch.object(settings, "SYSTEM_SETTINGS_PATH", overlay),
    ):
        values = settings._read_env_file()
    assert values == {"AGENT_MONITOR_MODEL": "kimi-k3"}


def test_fresh_checkout_needs_no_system_settings_seed(tmp_path: Path) -> None:
    # Instance settings are local runtime state, not a tracked seed. A fresh
    # checkout must not inherit a provider credential from a bundled overlay.
    overlay = tmp_path / "system_settings.json"
    with (
        patch.object(settings, "ENV_PATH", tmp_path / "server.env"),
        patch.object(settings, "SYSTEM_SETTINGS_PATH", overlay),
    ):
        assert settings._read_env_file() == {}
    assert not overlay.exists()
