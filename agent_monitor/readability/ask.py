"""Questions about a finished readability run.

The console writes ``readability/asks/<ask_id>.json`` (:func:`write_request`:
``{question, selection, view, node_id}``) and runs
``readability_runner.py --ask <ask_id>`` with the run's own route; the runner
answers with one model call and writes ``<ask_id>.reply.json``
(``{answer, refs, model, usage, created_at, status}``; the file name avoids
the words that ``jobs._promote_proof_artifact`` looks for).  The context of the
question is the part that holds the selected passage (looked up in the
readable version, then in the original), else the part of the selected
result, else the guide alone.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from agent_monitor.analyze.graph import NODE_ID_RE
from agent_monitor.analyze.prompts import clip

ASKS = ("readability", "asks")
ASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")
REPLY_SUFFIX = ".reply.json"
VIEWS = ("readable", "original")
MAX_QUESTION_CHARS = 2_000
MAX_SELECTION_CHARS = 4_000
_PART_RE = re.compile(r"(?m)^\[part:(\d+)\]: #$")
_LOOKUP_CHARS = 300


class AskError(ValueError):
    """A refused question (plain message)."""


def normalize(body: Mapping[str, Any]) -> dict[str, Any]:
    """``{question, selection, view, node_id}`` of an ask request body."""
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise AskError("Type a question to ask")
    if len(question) > MAX_QUESTION_CHARS:
        raise AskError(f"Questions are limited to {MAX_QUESTION_CHARS:,} characters")
    selection = body.get("selection")
    if selection is None:
        selection = ""
    if not isinstance(selection, str):
        raise AskError("selection must be text")
    if len(selection) > MAX_SELECTION_CHARS:
        raise AskError(f"The selected passage is limited to {MAX_SELECTION_CHARS:,} characters")
    view = body.get("view") or "readable"
    if view not in VIEWS:
        raise AskError('view must be "readable" or "original"')
    node_id = body.get("node_id")
    if node_id is not None and (not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id)):
        raise AskError("node_id must be a result of this run")
    if "\x00" in question + selection:
        raise AskError("The question contains invalid characters")
    return {"question": question.strip(), "selection": selection.strip(), "view": view, "node_id": node_id}


def _folder(workspace: Path) -> Path:
    return Path(workspace).joinpath(*ASKS)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _checked_id(ask_id: str) -> str:
    if not isinstance(ask_id, str) or not ASK_ID_RE.fullmatch(ask_id):
        raise AskError("Unknown question")
    return ask_id


def write_request(workspace: Path, ask_id: str, ask: Mapping[str, Any]) -> Path:
    path = _folder(workspace) / f"{_checked_id(ask_id)}.json"
    _write(path, normalize(ask))
    return path


def load_request(workspace: Path, ask_id: str) -> dict[str, Any]:
    value = _read(_folder(workspace) / f"{_checked_id(ask_id)}.json")
    if value is None:
        raise AskError("The question is missing")
    return normalize(value)


def write_answer(workspace: Path, ask_id: str, answer: Mapping[str, Any]) -> Path:
    path = _folder(workspace) / f"{_checked_id(ask_id)}{REPLY_SUFFIX}"
    _write(path, answer)
    return path


def load_answer(workspace: Path, ask_id: str) -> dict[str, Any] | None:
    return _read(_folder(workspace) / f"{_checked_id(ask_id)}{REPLY_SUFFIX}")


# ---------------------------------------------------------------------------
# the context of a question
# ---------------------------------------------------------------------------


def readable_parts(readable: str) -> dict[int, str]:
    """``readable.md`` split at its ``[part:<n>]: #`` marks."""
    marks = list(_PART_RE.finditer(readable))
    return {int(mark.group(1)): readable[mark.end():(marks[position + 1].start() if position + 1 < len(marks)
                                                  else len(readable))].strip()
            for position, mark in enumerate(marks)}


def _squash(text: str) -> str:
    return " ".join(text.split())


def part_of(selection: str, *, readable: Mapping[int, str], original: Mapping[int, str]) -> int | None:
    """The part whose text holds the start of ``selection`` (whitespace
    ignored): in the readable version first, then in the original."""
    needle = _squash(selection)[:_LOOKUP_CHARS]
    if len(needle) < 3:
        return None
    for texts in (readable, original):
        for index, text in sorted(texts.items()):
            if needle in _squash(text):
                return index
    return None


def context(
    *,
    ask: Mapping[str, Any],
    readable: str,
    original_lines: list[str],
    parts: list[Mapping[str, Any]],
    node_parts: Mapping[str, int | None],
    limit: int,
) -> tuple[str, int | None]:
    """The CONTEXT of a question and the part it comes from (``None``: the
    guide alone).  ``parts`` are ``check.json``'s parts (index, title,
    lines)."""
    readable_by_part = readable_parts(readable)
    original_by_part = {
        int(part["index"]): "\n".join(original_lines[max(0, part["lines"][0] - 1):part["lines"][1]])
        for part in parts if isinstance(part.get("lines"), list) and len(part["lines"]) == 2
    }
    index = part_of(ask.get("selection") or "", readable=readable_by_part, original=original_by_part)
    if index is None and ask.get("node_id"):
        index = node_parts.get(ask["node_id"])
    if index is None:
        return "", None
    title = next((str(part.get("title") or "") for part in parts if int(part["index"]) == index), "")
    half = limit // 2
    text = (f"READABLE VERSION, part {index} ({title}):\n{clip(readable_by_part.get(index, ''), half)}\n\n"
            f"ORIGINAL, part {index}:\n{clip(original_by_part.get(index, ''), half)}")
    return text, index
