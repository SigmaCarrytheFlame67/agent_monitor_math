"""Static browser-hardening contracts for the console's HTML pages."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    "agent_monitor/web/console.html",
    "agent_monitor/web/login.html",
    "monitor_core/harness_dashboard/web/index.html",
)
_EXTERNAL = re.compile(
    r"<(script|link)\b[^>]*\b(?:src|href)\s*=\s*\"(https?:)?//[^\"]+\"[^>]*>",
    re.IGNORECASE,
)


@pytest.mark.parametrize("relative_path", PAGES)
def test_every_third_party_script_and_stylesheet_is_pinned_by_sri(relative_path: str):
    html = (ROOT / relative_path).read_text(encoding="utf-8")
    for match in _EXTERNAL.finditer(html):
        tag = match.group(0)
        if match.group(1).lower() == "link" and "stylesheet" not in tag.lower():
            continue
        # Google Fonts serves user-agent-specific CSS (no stable hash); it is
        # a stylesheet only, never script.
        if match.group(1).lower() == "link" and "https://fonts.googleapis.com/" in tag:
            continue
        assert re.search(r'\bintegrity="sha(?:384|512)-[A-Za-z0-9+/=]{40,}"', tag), tag
        assert 'crossorigin="anonymous"' in tag, tag


def test_pdf_worker_is_integrity_checked_and_eval_is_disabled():
    html = (ROOT / "agent_monitor/web/console.html").read_text(encoding="utf-8")
    # The worker is fetched with an integrity value and started from a blob
    # URL; it is never pointed at the CDN directly.
    assert re.search(r"PDF_WORKER_SRI='sha512-[A-Za-z0-9+/=]{80,}'", html)
    assert "fetch(PDF_WORKER_URL,{integrity:PDF_WORKER_SRI" in html
    assert not re.search(r"workerSrc\s*=\s*['\"]https?:", html)
    assert "getDocument({url,isEvalSupported:false})" in html
    assert "getDocument(url)" not in html
