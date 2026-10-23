"""Questions about a finished readability run.

The console writes ``readability/asks/<ask_id>.json`` (:func:`write_request`:
``{question, selection, view, node_id}``) and runs
``readability_runner.py --ask <ask_id>`` with the run's own route; the runner
answers with one model call and writes ``<ask_id>.reply.json``
(``{answer, refs, model, usage, created_at, status}``).  The files and the
conversation so far are :mod:`agent_monitor.manuscript_ask`'s (Analyze keeps
its questions the same way).  The context of the question is the part that
holds the selected passage (looked up in the view it was selected in, then in
the other, as :func:`comparable` compares them), else the part of the
selected result, else, for a question that selects nothing, the part the
last answered question was read in, else the guide alone.
"""
from __future__ import annotations

import bisect
import re
from typing import Any, Mapping

from agent_monitor.analyze.prompts import clip
from agent_monitor.manuscript_ask import (  # noqa: F401  (re-exported)
    ASK_ID_RE,
    MAX_QUESTION_CHARS,
    MAX_SELECTION_CHARS,
    REPLY_SUFFIX,
    AskError,
    AskStore,
)
from agent_monitor.readability import parse

ASKS = ("readability", "asks")
VIEWS = ("readable", "original")
STORE = AskStore(ASKS, VIEWS)
normalize = STORE.normalize
write_request = STORE.write_request
load_request = STORE.load_request
write_answer = STORE.write_answer
load_answer = STORE.load_answer
_PART_RE = re.compile(r"(?m)^\[part:(\d+)\]: #$")
_LOOKUP_CHARS = 300
# What a passage selected in the readable version no longer shows
# (``comparable``): the invisible mark lines of readable.md and the fences
# around an original part, the quote, heading and list markers that open a
# line outside math, result links, math delimiters, backslash escapes, and
# emphasis, code and table marks.
_MARK_LINE_RE = re.compile(r"(?m)^(?:\[(?:part|result|original|original-end):[^\]\n]*\]: #"
                           r"|[ \t]{0,3}(?:~{3,}|`{3,})[^\n]*)[ \t]*$")
_LINE_MARKS_RE = re.compile(r"(?m)^[ \t]*+(?:>[ \t]*+)*+(?:#{1,6}[ \t]++|(?:[-*+]|\d{1,9}[.)])[ \t]++)?")
_RESULT_LINK_RE = re.compile(r"\[\[\s*result\s*:[^\]\n]{0,100}\]\]")
_MATH_DELIMITER_RE = re.compile(r"\$|\\[()\[\]]")
_ESCAPE_RE = re.compile(r"\\(?=[!-/:-@\[-`{-~])")
_MARKUP_RE = re.compile(r"[*_`|]+")
_SPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# the context of a question
# ---------------------------------------------------------------------------


def readable_parts(readable: str) -> dict[int, str]:
    """``readable.md`` split at its ``[part:<n>]: #`` marks."""
    marks = list(_PART_RE.finditer(readable))
    return {int(mark.group(1)): readable[mark.end():(marks[position + 1].start() if position + 1 < len(marks)
                                                  else len(readable))].strip()
            for position, mark in enumerate(marks)}


def _outside_math(pattern: "re.Pattern[str]", text: str) -> str:
    """``text`` without the matches of ``pattern`` that begin outside its
    math: a line of a display that starts "+ c" or "- d" is math, never a
    list item (the console sets math aside before it reads Markdown)."""
    spans = parse.math_spans(text)
    starts = [start for start, _end in spans]

    def drop(match: "re.Match[str]") -> str:
        index = bisect.bisect_right(starts, match.start()) - 1
        return match.group(0) if index >= 0 and match.start() < spans[index][1] else ""

    return pattern.sub(drop, text)


def comparable(text: str, *, markdown: bool = True) -> str:
    """``text`` as a selected passage compares with it, the same on both
    sides.  The console sends a passage on one line, without the marks the
    readable version renders away (anchor, part and fence lines, quote,
    heading and list markers, emphasis, code ticks, table bars, backslash
    escapes) and with typeset math as ``$tex$``; so for ``markdown`` text
    those marks and every math delimiter are dropped.  Whitespace is always
    dropped (the Original view shows plain lines as they are); case
    counts."""
    if markdown:
        text = _outside_math(_MARK_LINE_RE, text)
        text = _outside_math(_LINE_MARKS_RE, text)
        text = _RESULT_LINK_RE.sub("", text)
        text = _MATH_DELIMITER_RE.sub("", text)
        text = _ESCAPE_RE.sub("", text)
        text = _MARKUP_RE.sub("", text)
    return _SPACE_RE.sub("", text)


def part_of(selection: str, *, readable: Mapping[int, str], original: Mapping[int, str],
            view: str = "readable") -> int | None:
    """The part whose text holds the start of ``selection`` (compared as
    :func:`comparable` says): in the view it was selected in first, then in
    the other one."""
    searches = [(readable, True), (original, False)]
    for texts, markdown in (searches[::-1] if view == "original" else searches):
        needle = comparable(selection, markdown=markdown)[:_LOOKUP_CHARS]
        if len(needle) < 3:
            continue
        for index, text in sorted(texts.items()):
            if needle in comparable(text, markdown=markdown):
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
    carried_part: int | None = None,
) -> tuple[str, int | None]:
    """The CONTEXT of a question and the part it comes from (``None``: the
    guide alone).  ``parts`` are ``check.json``'s parts (index, title,
    lines); a follow-up that selects nothing reads ``carried_part``, the part
    the last answer was read in."""
    readable_by_part = readable_parts(readable)
    original_by_part = {
        int(part["index"]): "\n".join(original_lines[max(0, part["lines"][0] - 1):part["lines"][1]])
        for part in parts if isinstance(part.get("lines"), list) and len(part["lines"]) == 2
    }
    index = part_of(ask.get("selection") or "", readable=readable_by_part, original=original_by_part,
                    view=str(ask.get("view") or "readable"))
    if index is None and ask.get("node_id"):
        index = node_parts.get(ask["node_id"])
    if index is None and not ask.get("selection") and not ask.get("node_id"):
        index = carried_part
    if index is None or (index not in readable_by_part and index not in original_by_part):
        return "", None
    title = next((str(part.get("title") or "") for part in parts if int(part["index"]) == index), "")
    half = limit // 2
    text = (f"READABLE VERSION, part {index} ({title}):\n{clip(readable_by_part.get(index, ''), half)}\n\n"
            f"ORIGINAL, part {index}:\n{clip(original_by_part.get(index, ''), half)}")
    return text, index
