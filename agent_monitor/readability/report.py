"""``readability/readable.md``: the readable version of a manuscript.

Deterministic: the same parts, guide and counts give the same bytes (no
clock is read here).  The file opens with the title, a fixed provenance note
(model, route, audience, how many parts an independent model check found
faithful; never a certificate) and "At a glance" from the guide, then the
parts in reading order.  Invisible Markdown link-reference definitions, each
on its own line with blank lines around, mark places for the console:

* ``[part:<n>]: #`` where part ``n`` starts;
* ``[result:<id>]: #`` where a result's statement starts (the rewrite's
  ``[[result:<id>]]`` anchors);
* ``[original:<n>]: #`` and ``[original-end:<n>]: #`` around a part shown
  in its original form (not rewritten), which shared links leave out.

A part whose check found problems that change the meaning starts with a
``> ⚠ **Check:**`` blockquote listing them; a part that was not checked or not
rewritten says so.  Manuscript and model text in the title, the provenance,
"At a glance" and the check notes carry no links (URLs removed, link syntax
broken up, as in ``analyze.report``); the console escapes raw HTML itself and
typesets the math, which HTML escaping here would garble.

A rewritten part (and "At a glance") has its leftover LaTeX references,
citations and labels written out (:mod:`.references`), and its headings
shifted so that its top one is "##": "#" is the document's title.
"""
from __future__ import annotations

import bisect
import re
from typing import Any, Mapping, Sequence

from agent_monitor.analyze.report import _LINK_RE, _LINK_SYNTAX_RE, ROUTE_TEXT
from agent_monitor.analyze.titles import plain_title
from agent_monitor.readability import parse, texprose
from agent_monitor.readability.prompts import AUDIENCE_LABELS, MARKER_RE
from agent_monitor.readability.references import resolve

STATUSES = ("faithful", "issues", "unchecked", "not-rewritten")
# What the console and a shared link show of ``readable.md``, in characters.
VIEW_CHARS = 400_000
# Bytes read for such a view: more than VIEW_CHARS characters of any UTF-8.
VIEW_BYTES = 4 * VIEW_CHARS + 4
# Original text repeated for parts that were not rewritten, in characters
# over the whole file; past it the reader is sent to the Original view.
ORIGINAL_CHARS = 120_000
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_ORIGINAL_BLOCK_RE = re.compile(r"(?ms)^\[original:(\d+)\]: #\n.*?^\[original-end:\1\]: #\n?")
_ORIGINAL_START_RE = re.compile(r"(?m)^\[original:\d+\]: #$")
_NOT_SHARED = "*The original text of this part is not included in shared links.*\n"
# A line of the manuscript that looks like one of these marks is indented,
# so that it marks nothing (in a share, say).
_MARK_LIKE_RE = re.compile(r"(?m)^(?=\[(?:part|result|original|original-end):)")
_HEADING_RE = re.compile(r"(?m)^( {0,3})(#{1,6})(?=[ \t]|$)")


def _line(value: object, limit: int) -> str:
    """Manuscript or model text on one line, without links."""
    text = _LINK_SYNTAX_RE.sub("] (", _LINK_RE.sub("[link removed]", " ".join(str(value or "").split())))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def part_marker(index: int) -> str:
    return f"[part:{index}]: #"


def result_marker(node_id: str) -> str:
    return f"[result:{node_id}]: #"


def anchors(markdown: str) -> str:
    """A rewrite with its ``[[result:<id>]]`` anchors as invisible
    definitions on their own lines."""
    text = MARKER_RE.sub(lambda match: "\n\n" + result_marker(match.group(1).lower()) + "\n\n", markdown)
    return _BLANK_RUN_RE.sub("\n\n", text).strip("\n")


def normalize_headings(markdown: str) -> str:
    """A rewritten part with its headings shifted together so that the top
    one is "##" (never "#", the document's title; at most "######").  A
    line in math is not a heading."""
    spans = parse.math_spans(markdown)
    starts = [start for start, _end in spans]

    def in_math(offset: int) -> bool:
        index = bisect.bisect_right(starts, offset) - 1
        return index >= 0 and offset < spans[index][1]

    headings = [match for match in _HEADING_RE.finditer(markdown) if not in_math(match.start(2))]
    if not headings:
        return markdown
    shift = 2 - min(len(match.group(2)) for match in headings)
    if not shift:
        return markdown
    pieces, cursor = [], 0
    for match in headings:
        level = min(6, max(2, len(match.group(2)) + shift))
        pieces += [markdown[cursor:match.start(2)], "#" * level]
        cursor = match.end(2)
    return "".join(pieces) + markdown[cursor:]


def without_original(text: str) -> str:
    """``readable.md`` without the parts repeated in their original form (a
    shared link carries the rewrite only)."""
    text = _ORIGINAL_BLOCK_RE.sub(_NOT_SHARED, text)
    # A block without its end (a file read in part) is left out to the end.
    stray = _ORIGINAL_START_RE.search(text)
    return text[: stray.start()] + _NOT_SHARED if stray else text


def clip(text: str, limit: int = VIEW_CHARS) -> str:
    """``text`` (a readable version) as a view shows it: whole when it fits
    in ``limit`` characters, else cut before the first part that does not
    fit (so no fence is left open), with a note saying so."""
    if len(text) <= limit:
        return text
    note = (f"\n\n> **This readable version is longer than this view shows ({limit:,} characters).** "
            "The rest is in `readability/readable.md` of the run.\n")
    room = limit - len(note)
    cut = text.rfind("\n[part:", 0, room)
    if cut <= 0:
        cut = text.rfind("\n\n", 0, room)
    return text[: cut if cut > 0 else room].rstrip("\n") + note


def _fence(text: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"(?m)^ {0,3}~+", text)), default=0)
    return "~" * max(4, longest + 1)


def _counts_line(counts: Mapping[str, int]) -> str:
    text = (f"{counts.get('faithful', 0)} of {counts.get('total', 0)} parts faithful, "
            f"{counts.get('issues', 0)} with issues (marked), {counts.get('unchecked', 0)} unchecked")
    if counts.get("not_rewritten"):
        text += f", {counts['not_rewritten']} not rewritten (marked)"
    return text


def provenance(*, model: str, route: str, audience: str, counts: Mapping[str, int],
               math_fidelity: str | None = None) -> str:
    text = (
        f"> **Readable version** for {AUDIENCE_LABELS.get(audience, audience).lower()}, written by "
        f"{_line(model, 80)} via {ROUTE_TEXT.get(route, _line(route, 60))}. Every statement and argument is meant "
        f"to be kept; an independent model check compared each part with the original: {_counts_line(counts)}. "
        "This is not a certificate of correctness."
    )
    if math_fidelity == "pdf-text":
        text += " The manuscript's text was extracted from a PDF, so its symbols and formulas may be garbled."
    return text


def at_a_glance(guide: Mapping[str, Any], labels: Mapping[str, str],
                references: Mapping[str, tuple[str, str]] | None = None) -> str:
    """The "At a glance" section from the guide (empty parts are left out);
    ``references`` (``references.label_map``) writes out leftover LaTeX
    references."""
    refs = references or {}

    def line(value: object, limit: int) -> str:
        return _line(texprose.convert(resolve(str(value or ""), refs), labels), limit)

    out = ["## At a glance"]
    if guide.get("one_line"):
        out.append(f"**What it shows.** {line(guide['one_line'], 600)}")
    mains = []
    for entry in guide.get("main_results") or []:
        label = labels.get(entry.get("id") or "", "")
        mains.append(f"- {'**' + _line(label, 120) + '.** ' if label else ''}{line(entry.get('plain'), 800)}")
    if mains:
        out.append("**Main results.**\n\n" + "\n".join(mains))
    if guide.get("key_idea"):
        out.append(f"**Key idea.** {line(guide['key_idea'], 1500)}")
    if guide.get("roadmap"):
        out.append("**Roadmap.**\n\n" + "\n".join(f"{index}. {line(step, 400)}"
                                                  for index, step in enumerate(guide["roadmap"], 1)))
    if guide.get("reader_notes"):
        out.append("**Before you start.**\n\n" + "\n".join(f"- {line(note, 400)}" for note in guide["reader_notes"]))
    return "\n\n".join(out) if len(out) > 1 else ""


def _check_block(part: Mapping[str, Any], labels: Mapping[str, str]) -> str:
    lines = []
    major = [issue for issue in part.get("issues") or [] if issue.get("severity") == "major"]
    if major:
        lines.append("> ⚠ **Check:** the independent check found problems that change the meaning here; "
                     "compare with the original.")
        lines.extend(f"> - **{_line(issue.get('location'), 160)}:** {_line(issue.get('problem'), 600)}"
                     for issue in major)
    missing = [labels.get(node_id, node_id) for node_id in part.get("missing") or []]
    if missing:
        if not lines:
            lines.append("> ⚠ **Check:** compare this part with the original.")
        lines.append("> - The rewrite has no anchor for " + ", ".join(_line(label, 120) for label in missing)
                     + "; it may have lost the statement.")
    return "\n".join(lines)


def render_part(part: Mapping[str, Any], *, labels: Mapping[str, str], original: str,
                original_room: list[int], references: Mapping[str, tuple[str, str]] | None = None) -> str:
    """One part of ``readable.md``; ``original_room`` is the original text
    still allowed (shared across the parts, decremented here)."""
    index = int(part["index"])
    out = [part_marker(index)]
    status = part.get("status")
    if status in ("faithful", "issues", "unchecked"):
        warning = _check_block(part, labels)
        if warning:
            out.append(warning)
        if status == "unchecked":
            out.append(f"> **Not checked** ({_line(part.get('reason') or 'no check', 300)}): no model compared "
                       "this part with the original.")
        markdown = texprose.convert(resolve(str(part.get("markdown") or ""), references or {}), labels)
        out.append(anchors(normalize_headings(markdown)))
        return "\n\n".join(out)
    if part.get("kind") == "references":
        out.append("## References")
        out.append("> The references are kept as in the original.")
    else:
        out.append(f"> **Not rewritten** ({_line(part.get('reason') or 'not attempted', 300)}).")
    lines = part.get("lines") or [0, 0]
    if original.strip() and len(original) <= original_room[0]:
        original_room[0] -= len(original)
        original = _MARK_LIKE_RE.sub(" ", original)
        fence = _fence(original)
        out.append(f"[original:{index}]: #\n\nThe original text of this part follows unchanged.\n\n"
                   f"{fence}text\n{original}\n{fence}\n\n[original-end:{index}]: #")
    else:
        out.append(f"Read this part in the original (lines {lines[0]}–{lines[1]}).")
    return "\n\n".join(out)


def render(
    *,
    title: str,
    model: str,
    route: str,
    audience: str,
    guide: Mapping[str, Any],
    parts: Sequence[Mapping[str, Any]],
    originals: Mapping[int, str],
    labels: Mapping[str, str],
    counts: Mapping[str, int],
    math_fidelity: str | None = None,
    references: Mapping[str, tuple[str, str]] | None = None,
) -> str:
    """The readable version (see the module docstring); ``originals`` maps a
    part's index to its original text, ``labels`` a result id to its printed
    label, ``references`` a LaTeX label to its printed name and number
    (``references.label_map``)."""
    heading = _line(plain_title(title) or "Untitled manuscript", 300)
    blocks = [f"# {heading} — Readable version", provenance(model=model, route=route, audience=audience,
                                                                counts=counts, math_fidelity=math_fidelity)]
    glance = at_a_glance(guide, labels, references)
    if glance:
        blocks.append(glance)
    room = [ORIGINAL_CHARS]
    for part in parts:
        blocks.append(render_part(part, labels=labels, original=originals.get(int(part["index"]), ""),
                                  original_room=room, references=references))
    return "\n\n".join(block.strip("\n") for block in blocks if block.strip()) + "\n"
