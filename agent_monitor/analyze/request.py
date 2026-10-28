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
     "own_subscriptions": false,          # optional; see below
     "created_at": "2026-10-11T12:00:00Z"}

``own_subscriptions`` is true when the account may use the Codex and Claude
Code subscriptions it linked itself (an administrator, or an account given
full access: ``auth.may_use_own_subscriptions``, as ``jobs`` decides it for
every route); never for a guest.  The runner runs a subscription route only
for an administrator or such an account, and ``jobs`` gives it that
account's own linked login.
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


def clean_source(source: object, *, noun: str = "analysis") -> dict[str, Any]:
    """A validated copy of a request's ``source`` (``noun`` names the
    request in refusals); raises :class:`RequestError`."""
    if not isinstance(source, Mapping) or source.get("kind") not in SOURCE_KINDS:
        raise RequestError(f"The {noun} request has no valid source")
    kind = source["kind"]
    clean: dict[str, Any] = {"kind": kind}
    if kind == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url.strip() or len(url) > 4096:
            raise RequestError(f"The {noun} request has no valid link")
        clean["url"] = url.strip()
    elif kind == "paste":
        text = source.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise RequestError("The pasted manuscript is empty or too long")
        clean["text"] = text
        if source.get("format") not in (None, ""):
            if source.get("format") not in FORMATS:
                raise RequestError("The pasted manuscript format must be tex, md or txt")
            clean["format"] = source["format"]
    else:
        ids = source.get("attachments") or []
        if not isinstance(ids, list) or len(ids) > 80 or not all(
            isinstance(item, str) and _ATTACHMENT_ID_RE.fullmatch(item) for item in ids
        ):
            raise RequestError(f"The {noun} request lists invalid uploads")
        clean["attachments"] = list(ids)
    return clean


def clean_route(data: Mapping[str, Any], *, noun: str = "analysis") -> dict[str, Any]:
    """The validated ``role``, ``route``, ``model``, ``reasoning_effort``,
    ``sponsored``, ``own_subscriptions`` and ``created_at`` of a request."""
    role = data.get("role")
    if role not in ROLES:
        raise RequestError(f"The {noun} request has no valid role")
    route = data.get("route")
    if route not in ROUTES:
        raise RequestError(f"The {noun} request has no valid route")
    model = data.get("model") or ""
    if not isinstance(model, str) or len(model) > 120:
        raise RequestError(f"The {noun} request has an invalid model")
    effort = data.get("reasoning_effort") or "default"
    if not isinstance(effort, str) or not re.fullmatch(r"[a-z]{1,20}", effort):
        raise RequestError(f"The {noun} request has an invalid reasoning effort")
    sponsored = data.get("sponsored") is True
    if sponsored != (route == "sponsored_kimi"):
        raise RequestError(f"The {noun} request's sponsored flag does not match its route")
    own_subscriptions = data.get("own_subscriptions") is True
    if own_subscriptions and role == "guest":
        raise RequestError(f"The {noun} request gives a guest account subscriptions")
    return {
        "role": role,
        "route": route,
        "model": model.strip(),
        "reasoning_effort": effort,
        "sponsored": sponsored,
        "own_subscriptions": own_subscriptions,
        "created_at": str(data.get("created_at") or "")[:40],
    }


def validate(data: object) -> dict[str, Any]:
    """A normalized copy of a request; raises :class:`RequestError`."""
    if not isinstance(data, Mapping):
        raise RequestError("The analysis request is not an object")
    source = clean_source(data.get("source"))
    caps: dict[str, int] = {}
    for key, value in (data.get("caps") or {}).items() if isinstance(data.get("caps"), Mapping) else []:
        if key in ("max_nodes", "max_calls", "max_seconds") and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            caps[key] = value
    route = clean_route(data)
    focus = data.get("focus") or ""
    if not isinstance(focus, str) or len(focus) > MAX_FOCUS_CHARS:
        raise RequestError("The analysis focus is too long")
    return {
        "schema": SCHEMA,
        "source": source,
        "caps": caps,
        "focus": focus.strip(),
        "formalize": data.get("formalize") is True,
        "role": route["role"],
        "route": route["route"],
        "model": route["model"],
        "reasoning_effort": route["reasoning_effort"],
        "sponsored": route["sponsored"],
        "own_subscriptions": route["own_subscriptions"],
        "created_at": route["created_at"],
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
