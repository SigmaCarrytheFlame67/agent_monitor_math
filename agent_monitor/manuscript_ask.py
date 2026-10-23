"""Questions about a finished manuscript run (a readable version or an
analysis): the files a question and its answer are kept in, and the
conversation so far.

The console writes ``<folder>/<ask_id>.json`` (:meth:`AskStore.write_request`:
``{question, selection, view, node_id}``) and runs the mode's runner with
``--ask <ask_id>`` on the run's own route; the runner answers with one model
call and writes ``<ask_id>.reply.json`` (``{answer, refs, model, usage,
created_at, status, ...}``; the file name avoids the words that
``jobs._promote_proof_artifact`` looks for).  Readability keeps its questions
in ``readability/asks`` (views ``readable`` and ``original``), Analyze in
``analysis/asks`` (views ``report`` and ``source``).

A question is answered with the questions answered before it about the same
run (:meth:`AskStore.earlier`, at most :data:`EARLIER_EXCHANGES`), so that a
follow-up ("why is that?") can be read; they are model output and the
reader's words, so the prompt carries them as data.  A follow-up that selects
nothing of its own is also given what the last answer was about (its result,
or its part of a readable version).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_monitor.analyze.graph import NODE_ID_RE
from agent_monitor.analyze.prompts import EARLIER_BYTES, EARLIER_EXCHANGES, fence, utf8_len  # noqa: F401

ASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")
REPLY_SUFFIX = ".reply.json"
MAX_QUESTION_CHARS = 2_000
MAX_SELECTION_CHARS = 4_000
# The conversation a question is answered with: the last few questions
# answered about the run, within a byte budget (full and bounded routes);
# kept with Analyze's prompt constants, whose pack hash covers them.
# The newest answer files read to find them.
_EARLIER_SCAN = 40
_EARLIER_QUESTION_BYTES = 600
# Half of a character outside the Basic Multilingual Plane, alone: a browser
# that cuts text by UTF-16 units can send one, and no UTF-8 file can hold it.
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_EARLIER_ABOUT_BYTES = 240


class AskError(ValueError):
    """A refused question (plain message)."""


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


def _clip_bytes(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` UTF-8 bytes, with an ellipsis when cut."""
    if utf8_len(text) <= limit:
        return text
    return text.encode("utf-8")[:max(0, limit - 3)].decode("utf-8", "ignore").rstrip() + "…"


@dataclass(frozen=True)
class AskStore:
    """Where one mode keeps its questions, and the views a passage may be
    selected in (the first is the default)."""

    folder: tuple[str, ...]
    views: tuple[str, ...]
    node_refusal: str = "node_id must be a result of this run"

    def normalize(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """``{question, selection, view, node_id}`` of an ask request body
        (half a character a browser cut off is dropped)."""
        question = body.get("question")
        if isinstance(question, str):
            question = _LONE_SURROGATE_RE.sub("", question)
        if not isinstance(question, str) or not question.strip():
            raise AskError("Type a question to ask")
        if len(question) > MAX_QUESTION_CHARS:
            raise AskError(f"Questions are limited to {MAX_QUESTION_CHARS:,} characters")
        selection = body.get("selection")
        if selection is None:
            selection = ""
        if not isinstance(selection, str):
            raise AskError("selection must be text")
        selection = _LONE_SURROGATE_RE.sub("", selection)
        if len(selection) > MAX_SELECTION_CHARS:
            raise AskError(f"The selected passage is limited to {MAX_SELECTION_CHARS:,} characters")
        view = body.get("view") or self.views[0]
        if view not in self.views:
            raise AskError("view must be " + " or ".join(f'"{name}"' for name in self.views))
        node_id = body.get("node_id")
        if node_id is not None and (not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id)):
            raise AskError(self.node_refusal)
        if "\x00" in question + selection:
            raise AskError("The question contains invalid characters")
        return {"question": question.strip(), "selection": selection.strip(), "view": view, "node_id": node_id}

    def directory(self, workspace: Path) -> Path:
        return Path(workspace).joinpath(*self.folder)

    def write_request(self, workspace: Path, ask_id: str, ask: Mapping[str, Any]) -> Path:
        path = self.directory(workspace) / f"{_checked_id(ask_id)}.json"
        _write(path, self.normalize(ask))
        return path

    def load_request(self, workspace: Path, ask_id: str) -> dict[str, Any]:
        value = _read(self.directory(workspace) / f"{_checked_id(ask_id)}.json")
        if value is None:
            raise AskError("The question is missing")
        return self.normalize(value)

    def write_answer(self, workspace: Path, ask_id: str, answer: Mapping[str, Any]) -> Path:
        path = self.directory(workspace) / f"{_checked_id(ask_id)}{REPLY_SUFFIX}"
        _write(path, answer)
        return path

    def load_answer(self, workspace: Path, ask_id: str) -> dict[str, Any] | None:
        return _read(self.directory(workspace) / f"{_checked_id(ask_id)}{REPLY_SUFFIX}")

    def earlier(self, workspace: Path, current: str, *, exchanges: int = EARLIER_EXCHANGES) -> list[dict[str, Any]]:
        """The questions answered about this run before ``current``, oldest
        first (at most ``exchanges``): ``{question, selection, view, node_id,
        answer, about, part}`` (``about``: the results its answer was given
        with, ``part``: the part of a readable version it was read in).
        Unanswered and failed questions are left out."""
        folder = self.directory(workspace)
        if exchanges <= 0 or folder.is_symlink() or not folder.is_dir():
            return []
        dated: list[tuple[float, Path]] = []
        for path in folder.glob(f"*{REPLY_SUFFIX}"):
            ask_id = path.name[: -len(REPLY_SUFFIX)]
            if ask_id == current or not ASK_ID_RE.fullmatch(ask_id):
                continue
            try:
                if not path.is_symlink():
                    dated.append((path.stat().st_mtime, path))
            except OSError:
                continue
        found: list[tuple[str, dict[str, Any]]] = []
        for _mtime, path in sorted(dated)[-_EARLIER_SCAN:]:
            ask_id = path.name[: -len(REPLY_SUFFIX)]
            answer = _read(path)
            if not answer or answer.get("status") != "done" or not str(answer.get("answer") or "").strip():
                continue
            try:
                request = self.load_request(workspace, ask_id)
            except AskError:
                continue
            about = [item for item in answer.get("about") or [] if isinstance(item, str) and NODE_ID_RE.fullmatch(item)]
            part = answer.get("part") if isinstance(answer.get("part"), int) and not isinstance(answer.get("part"), bool) else None
            found.append((str(answer.get("created_at") or ""),
                          {**request, "answer": str(answer["answer"]), "about": about[:4], "part": part}))
        found.sort(key=lambda item: item[0])
        return [item for _created, item in found[-exchanges:]]


def earlier_text(exchanges: list[Mapping[str, Any]], *, limit: int,
                 name_of: Callable[[str], str] | None = None) -> str:
    """The body of ``<EARLIER>``: each earlier question, what it was about (a
    result or the start of a passage) and its answer, oldest first, within
    ``limit`` UTF-8 bytes (older exchanges are dropped before an answer is
    shortened to nothing); ``""`` without any."""
    entries: list[tuple[str, str]] = []
    for item in exchanges:
        lines = [f"Question: {_clip_bytes(' '.join(str(item.get('question') or '').split()), _EARLIER_QUESTION_BYTES)}"]
        node_id = str(item.get("node_id") or "")
        passage = " ".join(str(item.get("selection") or "").split())
        if node_id:
            name = (name_of(node_id) if name_of else "") or node_id
            lines.append(f"About: {_clip_bytes(name, _EARLIER_ABOUT_BYTES)} [[result:{node_id}]]")
        elif passage:
            lines.append(f"About the passage: \"{_clip_bytes(passage, _EARLIER_ABOUT_BYTES)}\"")
        lines.append("Answer: ")
        # Fenced before they are measured: fencing writes "<" as the
        # three-byte "‹" (an answer's last word is fenced as if text followed).
        entries.append((fence("\n".join(lines)), fence(str(item.get("answer") or "").strip() + "\n")[:-1]))
    # Keep the newest exchanges that fit with at least a few hundred bytes of answer each.
    while entries:
        room = limit - sum(utf8_len(head) + 2 for head, _answer in entries)
        share = room // len(entries)
        if share >= 300 or len(entries) == 1:
            break
        entries.pop(0)
    if not entries:
        return ""
    share = max(0, (limit - sum(utf8_len(head) + 2 for head, _answer in entries)) // len(entries))
    text = "\n\n".join(head + _clip_bytes(answer, share) for head, answer in entries)
    return fence(text)
