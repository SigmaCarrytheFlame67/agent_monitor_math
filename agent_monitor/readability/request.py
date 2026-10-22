"""The ``readability/request.json`` contract between ``jobs.start_job`` and
the Readability runner.

``jobs.start_job`` writes it with :func:`write` before launching the runner;
the runner reads it with :func:`load` and refuses (exit 2) anything that does
not validate.  Fields::

    {"schema": "ansatze-readability-request-v1",
     "source": {"kind": "url", "url": "https://arxiv.org/abs/2302.05537"}
             | {"kind": "paste", "text": "...", "format": "tex|md|txt"}   # format optional
             | {"kind": "upload", "attachments": ["<attachment id>", ...]},  # empty = all
     "caps": {"max_parts": 12, "max_calls": 30, "max_seconds": 3600},    # may only lower the role caps
     "audience": "research|graduate|undergraduate",
     "focus": "optional note from the reader",
     "role": "guest|member|admin",
     "route": "sponsored_kimi|api_key|codex_subscription|claude_subscription",
     "model": "kimi-k3",
     "reasoning_effort": "default|low|high|max|...",
     "sponsored": true,
     "created_at": "2026-10-22T12:00:00Z"}

The source and route fields are validated exactly as Analyze's
(``analyze.request.clean_source`` and ``clean_route``).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent_monitor.analyze import request as analysis_request
from agent_monitor.analyze.request import MAX_TEXT_CHARS, RequestError

SCHEMA = "ansatze-readability-request-v1"
REQUEST_PATH = ("readability", "request.json")
AUDIENCES = ("research", "graduate", "undergraduate")
DEFAULT_AUDIENCE = "graduate"
MAX_FOCUS_CHARS = 2_000
CAP_KEYS = ("max_parts", "max_calls", "max_seconds")

__all__ = ["RequestError", "SCHEMA", "AUDIENCES", "validate", "build", "write", "load"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(data: object) -> dict[str, Any]:
    """A normalized copy of a request; raises :class:`RequestError`."""
    if not isinstance(data, Mapping):
        raise RequestError("The readability request is not an object")
    source = analysis_request.clean_source(data.get("source"), noun="readability")
    caps: dict[str, int] = {}
    for key, value in (data.get("caps") or {}).items() if isinstance(data.get("caps"), Mapping) else []:
        if key in CAP_KEYS and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            caps[key] = value
    route = analysis_request.clean_route(data, noun="readability")
    audience = data.get("audience") or DEFAULT_AUDIENCE
    if audience not in AUDIENCES:
        raise RequestError("The readability request has no valid audience")
    focus = data.get("focus") or ""
    if not isinstance(focus, str) or len(focus) > MAX_FOCUS_CHARS:
        raise RequestError("The readability focus is too long")
    return {"schema": SCHEMA, "source": source, "caps": caps, "audience": audience, "focus": focus.strip(), **route}


def build(**fields: Any) -> dict[str, Any]:
    fields.setdefault("created_at", _now())
    return validate(fields)


def write(workspace: Path, **fields: Any) -> Path:
    """Validate and write ``readability/request.json`` (0600) atomically."""
    document = build(**fields)
    folder = Path(workspace) / REQUEST_PATH[0]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / REQUEST_PATH[1]
    descriptor, temporary = tempfile.mkstemp(prefix=".request-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def load(workspace: Path) -> dict[str, Any]:
    path = Path(workspace).joinpath(*REQUEST_PATH)
    if path.is_symlink() or not path.is_file():
        raise RequestError("readability/request.json is missing")
    if path.stat().st_size > 2 * MAX_TEXT_CHARS * 4:
        raise RequestError("readability/request.json is too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RequestError("readability/request.json is not valid JSON") from None
    return validate(data)
