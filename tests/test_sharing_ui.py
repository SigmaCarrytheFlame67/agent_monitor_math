"""The run-sharing script adds the Share button and nothing to the landing page."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

SHARING_JS = Path(__file__).parents[1] / "agent_monitor" / "web" / "sharing.js"
SHARING = SHARING_JS.read_text(encoding="utf-8")
NODE = shutil.which("node") or "/home/ubuntu/.local/node24/bin/node"


def test_landing_page_has_no_worked_example_row() -> None:
    assert "#view-landing" not in SHARING
    assert "Worked example" not in SHARING
    assert "/examples/erdos-straus" not in SHARING


def test_run_sessions_keep_the_share_button() -> None:
    assert "share-run-btn" in SHARING


def test_sharing_script_is_valid_javascript() -> None:
    if not Path(NODE).is_file():
        pytest.skip("node is unavailable")
    result = subprocess.run(
        [NODE, "--check", str(SHARING_JS)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
