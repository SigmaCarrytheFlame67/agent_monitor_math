"""The ``analysis/request.json`` contract between ``jobs.start_job`` and the
Analyze runner.

``jobs.start_job`` writes it with :func:`write` before launching the runner;
the runner reads it with :func:`load` and refuses (exit 2) anything that does
not validate.  Fields::

    {"schema": "ansatze-analyze-request-v1",
     "source": {"kind": "url", "url": "https://arxiv.org/abs/2302.05537"}
             | {"kind": "paste", "text": "...", "format": "tex|md|txt"}   # format optional
             | {"kind": "upload", "attachments": ["<attachment id>", ...]},  # empty = all
     "caps": {"max_nodes": 30, "max_calls": 36, "max_seconds": 3600},     # may only lower the role caps
     "focus": "optional note: what to look at",
     "formalize": false,
     "role": "guest|member|admin",
     "route": "sponsored_kimi|api_key|codex_subscription|claude_subscription",
     "model": "kimi-k3",
     "reasoning_effort": "default|low|high|max|...",
     "sponsored": true,
     "created_at": "2026-10-11T12:00:00Z"}
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "ansatze-analyze-request-v1"
REQUEST_PATH = ("analysis", "request.json")
ROUTES = ("sponsored_kimi", "api_key", "codex_subscription", "claude_subscription")
ROLES = ("guest", "member", "admin")
SOURCE_KINDS = ("url", "paste", "upload")
FORMATS = ("tex", "md", "txt")
MAX_TEXT_CHARS = 400_000
MAX_FOCUS_CHARS = 2_000
_ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class RequestError(ValueError):
    """The analysis request is missing or invalid (public-safe message)."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(data: object) -> dict[str, Any]:
    """A normalized copy of a request; raises :class:`RequestError`."""
    if not isinstance(data, Mapping):
        raise RequestError("The analysis request is not an object")
    source = data.get("source")
    if not isinstance(source, Mapping) or source.get("kind") not in SOURCE_KINDS:
        raise RequestError("The analysis request has no valid source")
    kind = source["kind"]
    clean_source: dict[str, Any] = {"kind": kind}
    if kind == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url.strip() or len(url) > 4096:
            raise RequestError("The analysis request has no valid link")
        clean_source["url"] = url.strip()
    elif kind == "paste":
        text = source.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise RequestError("The pasted manuscript is empty or too long")
        clean_source["text"] = text
        if source.get("format") not in (None, ""):
            if source.get("format") not in FORMATS:
                raise RequestError("The pasted manuscript format must be tex, md or txt")
            clean_source["format"] = source["format"]
    else:
        ids = source.get("attachments") or []
        if not isinstance(ids, list) or len(ids) > 80 or not all(
            isinstance(item, str) and _ATTACHMENT_ID_RE.fullmatch(item) for item in ids
        ):
            raise RequestError("The analysis request lists invalid uploads")
        clean_source["attachments"] = list(ids)
    caps: dict[str, int] = {}
    for key, value in (data.get("caps") or {}).items() if isinstance(data.get("caps"), Mapping) else []:
        if key in ("max_nodes", "max_calls", "max_seconds") and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            caps[key] = value
    role = data.get("role")
    if role not in ROLES:
        raise RequestError("The analysis request has no valid role")
    route = data.get("route")
    if route not in ROUTES:
        raise RequestError("The analysis request has no valid route")
    focus = data.get("focus") or ""
    if not isinstance(focus, str) or len(focus) > MAX_FOCUS_CHARS:
        raise RequestError("The analysis focus is too long")
    model = data.get("model") or ""
    if not isinstance(model, str) or len(model) > 120:
        raise RequestError("The analysis request has an invalid model")
    effort = data.get("reasoning_effort") or "default"
    if not isinstance(effort, str) or not re.fullmatch(r"[a-z]{1,20}", effort):
        raise RequestError("The analysis request has an invalid reasoning effort")
    sponsored = data.get("sponsored") is True
    if sponsored != (route == "sponsored_kimi"):
        raise RequestError("The analysis request's sponsored flag does not match its route")
    created = data.get("created_at") or ""
    return {
        "schema": SCHEMA,
        "source": clean_source,
        "caps": caps,
        "focus": focus.strip(),
        "formalize": data.get("formalize") is True,
        "role": role,
        "route": route,
        "model": model.strip(),
        "reasoning_effort": effort,
        "sponsored": sponsored,
        "created_at": str(created)[:40],
    }


def build(**fields: Any) -> dict[str, Any]:
    fields.setdefault("created_at", _now())
    return validate(fields)


def write(workspace: Path, **fields: Any) -> Path:
    """Validate and write ``analysis/request.json`` (0600) atomically."""
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
        raise RequestError("analysis/request.json is missing")
    if path.stat().st_size > 2 * MAX_TEXT_CHARS * 4:
        raise RequestError("analysis/request.json is too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RequestError("analysis/request.json is not valid JSON") from None
    return validate(data)
