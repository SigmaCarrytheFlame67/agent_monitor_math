"""The manuscript in reading order, and the parts it is rewritten in.

The runner reads the stored copy of the manuscript (``readability/source``;
never compiled or executed):

* TeX: the root file with its ``\\input`` / ``\\include`` / ``\\subfile`` closure
  inlined where each file is pulled in (``latex_structure.resolve_includes``:
  confined to the bundle, bounded in depth, files and bytes), comments and
  ``\\iffalse`` blocks removed, and only the body between ``\\begin{document}``
  and ``\\end{document}`` kept (the author's macros come from the segmenter).
  An included file that is a document of its own (a ``\\subfile``, a
  standalone figure) contributes its body only, as LaTeX reads it; text after
  an ``\\end{document}``, which LaTeX ignores, is left out with a warning;
* Markdown and text: the main file, normalized as the segmenter read it;
* PDF: the text pypdf extracted (formulas may be garbled).

Every line remembers the file and line it came from, so each result the
segmenter found is placed in the part that holds the start of its
statement.  The text is ``readability/original.txt``.

Parts follow the manuscript's top-level sections (TeX ``\\part``,
``\\chapter``, ``\\section``; Markdown ``#`` and ``##``; PDF pages).  A section
longer than the part size is split at its subsections, then at paragraphs,
then at lines; small neighbours are merged, and the pages of a PDF (whose
breaks carry no structure) fill each part in turn.  Blank lines never make a
part of their own.  A ``thebibliography`` environment (or a Markdown
"References" section) is a part of its own that is never rewritten.  A short
manuscript is one part.
"""
from __future__ import annotations

import bisect
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from agent_monitor.analyze import latex_structure, text_structure
from agent_monitor.analyze.titles import plain_title

MAX_DOCUMENT_CHARS = text_structure.MAX_TEXT_CHARS
# Longer lines (a pasted paragraph without line breaks) are wrapped at a
# space, so that every line fits in a part.
MAX_LINE_CHARS = 2_000
# Parts shorter than this share a part with a neighbour when both fit.
SMALL_PART_SHARE = 4
_TEX_HEADING_RE = re.compile(r"^\s*\\(part|chapter|section|subsection|subsubsection|paragraph)\*?"
                             r"\s*(?:\[[^\]\n]*\])?\s*\{")
_TEX_LEVELS = {"part": 1, "chapter": 1, "section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 3}
_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*)$")
_MD_REFERENCES_RE = re.compile(r"(?i)^#{1,6}[ \t]+(?:references|bibliography)[ \t#.:]*$")
_BEGIN_DOCUMENT_RE = re.compile(r"\\begin\s*\{document\}")
_END_DOCUMENT_RE = re.compile(r"\\end\s*\{document\}")
_BEGIN_BIBLIOGRAPHY_RE = re.compile(r"\\begin\s*\{thebibliography\}")
_END_BIBLIOGRAPHY_RE = re.compile(r"\\end\s*\{thebibliography\}")


class DocumentError(ValueError):
    """The manuscript text could not be read (public-safe message)."""


@dataclass
class Document:
    format: str
    lines: list[str]
    # (file, line) each line came from; ("", 0) where none applies.
    origins: list[tuple[str, int]]
    warnings: list[str] = field(default_factory=list)
    # PDF: the first line of each page.
    page_starts: list[int] = field(default_factory=list)
    _index: dict[tuple[str, int], int] | None = None
    _prefix: list[int] | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def size(self, start: int, end: int) -> int:
        """Characters of lines ``[start, end)``, line breaks included."""
        if self._prefix is None or len(self._prefix) != len(self.lines) + 1:
            self._prefix = [0]
            for line in self.lines:
                self._prefix.append(self._prefix[-1] + len(line) + 1)
        return self._prefix[end] - self._prefix[start]

    def locate(self, node: Mapping[str, Any]) -> int | None:
        """The line where a segmented result's statement starts, if it is in
        the document (a result in a preamble or a dropped file is not)."""
        if self._index is None:
            self._index = {}
            for position, origin in enumerate(self.origins):
                self._index.setdefault(origin, position)
        lines = node.get("lines") or []
        file = str(node.get("file") or "")
        if len(lines) != 2:
            return None
        for line in range(int(lines[0]), int(lines[1]) + 1):
            if (file, line) in self._index:
                return self._index[(file, line)]
        return None


@dataclass
class Part:
    index: int
    title: str
    start: int
    end: int
    kind: str = "text"  # "references" parts are never rewritten
    nodes: list[str] = field(default_factory=list)

    def text(self, document: Document) -> str:
        return "\n".join(document.lines[self.start:self.end]).strip("\n")

    def chars(self, document: Document) -> int:
        """Characters of the part's text, as a rewrite is sent it."""
        return len(self.text(document))


# ---------------------------------------------------------------------------
# reading the manuscript
# ---------------------------------------------------------------------------


def _wrapped(lines: list[str], origins: list[tuple[str, int]]) -> tuple[list[str], list[tuple[str, int]]]:
    out_lines: list[str] = []
    out_origins: list[tuple[str, int]] = []
    for line, origin in zip(lines, origins):
        while len(line) > MAX_LINE_CHARS:
            cut = line.rfind(" ", MAX_LINE_CHARS // 2, MAX_LINE_CHARS)
            cut = cut if cut > 0 else MAX_LINE_CHARS
            out_lines.append(line[:cut].rstrip())
            out_origins.append(origin)
            line = line[cut:].lstrip()
        out_lines.append(line)
        out_origins.append(origin)
    return out_lines, out_origins


def _capped(document: Document) -> Document:
    lines, origins = _wrapped(document.lines, document.origins)
    used = 0
    for position, line in enumerate(lines):
        used += len(line) + 1
        if used > MAX_DOCUMENT_CHARS:
            lines, origins = lines[:position], origins[:position]
            document.warnings.append(f"Only the first {MAX_DOCUMENT_CHARS:,} characters of the manuscript were read")
            break
    document.lines, document.origins = lines, origins
    return document


def _clean_tex(path: Path) -> str:
    """A TeX file as the include resolver saw it (comments dropped, lines
    kept), with ``\\iffalse ... \\fi`` blanked in place."""
    text = latex_structure.strip_comments(latex_structure.read_text(path)[0])
    return latex_structure.IFFALSE_RE.sub(
        lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)), text)


def _command_end(text: str, offset: int) -> int:
    for pattern in (latex_structure.INCLUDE_RE, latex_structure.BARE_INPUT_RE, latex_structure.IMPORT_RE):
        match = pattern.match(text, offset)
        if match:
            return match.end()
    return offset


def tex_document(project: Path, root_file: str | None = None) -> Document:
    """The TeX manuscript under ``project`` in reading order (see the module
    docstring); raises :class:`DocumentError`."""
    project = Path(project).resolve()
    root = None
    if root_file:
        candidate = project / root_file
        if latex_structure._inside(candidate, project) and candidate.is_file() and not candidate.is_symlink():
            root = candidate.resolve()
    try:
        root = root or latex_structure.find_root(project)
    except latex_structure.LatexStructureError as exc:
        raise DocumentError(str(exc)) from None
    if root is None:
        raise DocumentError("No TeX file was found in the source")
    order, missing, _total, inserted = latex_structure.resolve_includes(root, project)
    if not order:
        raise DocumentError("The root TeX file could not be read")
    texts = {path: _clean_tex(path) for path in order}
    children: dict[Path, list[tuple[int, int, Path]]] = defaultdict(list)
    for child, (parent, row, column) in inserted.items():
        if child in texts:
            children[parent].append((row, column, child))
    lines: list[str] = []
    origins: list[tuple[str, int]] = []
    # Files with text after an \end{document} (LaTeX ignores it).
    ignored: set[str] = set()

    def emit(text: str, starts: list[int], name: str, begin: int, end: int) -> None:
        if begin >= end:
            return
        first = bisect.bisect_right(starts, begin)
        for offset, piece in enumerate(text[begin:end].split("\n")):
            lines.append(piece.rstrip())
            origins.append((name, first + offset))

    def expand(path: Path, depth: int) -> None:
        text = texts[path]
        name = path.relative_to(project).as_posix()
        starts = [0] + [index + 1 for index, char in enumerate(text) if char == "\n"]
        low, high = 0, len(text)
        # An included file that is a document of its own (a \subfile, a
        # standalone figure) contributes its body only: its preamble, its
        # \end{document} and what follows are not read.
        begin = _BEGIN_DOCUMENT_RE.search(text) if depth else None
        end = _END_DOCUMENT_RE.search(text, begin.end()) if begin else None
        if end:
            low, high = begin.end(), end.start()
            if text[end.end():].strip():
                ignored.add(name)
        cursor = low
        for row, column, child in sorted(children.get(path, ())):
            offset = starts[row] + column if row < len(starts) else len(text)
            # An include inside a blanked \iffalse block, or outside the body
            # of a document of its own, pulls nothing in.
            if offset < cursor or offset >= high or text[offset] != "\\":
                continue
            emit(text, starts, name, cursor, offset)
            if depth < latex_structure.MAX_INCLUDE_DEPTH:
                expand(child, depth + 1)
            cursor = max(offset, _command_end(text, offset))
        emit(text, starts, name, cursor, high)

    expand(root, 0)
    # The main document's \begin and \end{document}: the only ones left (they
    # may sit in an included header or footer file).
    begin = next((index for index, line in enumerate(lines) if _BEGIN_DOCUMENT_RE.search(line)), None)
    if begin is not None:
        lines[begin] = lines[begin][_BEGIN_DOCUMENT_RE.search(lines[begin]).end():]
        end = next((index for index in range(begin, len(lines)) if _END_DOCUMENT_RE.search(lines[index])), None)
        if end is not None:
            match = _END_DOCUMENT_RE.search(lines[end])
            lines[end], tail = lines[end][:match.start()], lines[end][match.end():]
            ignored.update(name for (name, _row), text in zip(origins[end:], [tail, *lines[end + 1:]]) if text.strip())
            lines, origins = lines[:end + 1], origins[:end + 1]
        lines, origins = lines[begin:], origins[begin:]
    warnings = []
    if missing:
        warnings.append("Some included files were not found or were refused: " + ", ".join(sorted(set(missing))[:12]))
    if ignored:
        warnings.append("Text after \\end{document}, which LaTeX ignores, was not read: "
                        + ", ".join(sorted(ignored)[:12]))
    return _capped(Document("tex", lines, origins, warnings))


def text_document(path: Path, *, file: str, fmt: str) -> Document:
    """A Markdown or text manuscript as the segmenter read it."""
    text, warnings = text_structure.read_text_file(path, file=file)
    lines = text.split("\n")
    return _capped(Document(fmt, lines, [(file, index + 1) for index in range(len(lines))], warnings))


def pdf_document(text_path: Path, *, file: str) -> Document:
    """The text extracted from a PDF (``pdf_text.txt``, a form feed line
    between pages), numbered as the segmenter numbered it."""
    text, warnings = text_structure.read_text_file(text_path, file=file)
    lines: list[str] = []
    page_starts: list[int] = []
    for page in text.split("\n" + text_structure.PAGE_MARK + "\n"):
        page_starts.append(len(lines))
        lines.extend(page.split("\n"))
    document = Document("pdf", lines, [(file, index + 1) for index in range(len(lines))],
                        warnings + ["Text extracted from a PDF: symbols and formulas may be garbled"],
                        page_starts=page_starts)
    return _capped(document)


# ---------------------------------------------------------------------------
# parts
# ---------------------------------------------------------------------------


def _heading(line: str, fmt: str) -> tuple[int, str] | None:
    """(level, title) of a heading line; level 1 starts a top-level section."""
    if fmt == "tex":
        match = _TEX_HEADING_RE.match(line)
        if not match:
            return None
        body, _end = latex_structure._braced(line, match.end() - 1)
        return _TEX_LEVELS[match.group(1)], plain_title(body if body is not None else line[match.end():], 120)
    if fmt in ("md", "txt"):
        match = _MD_HEADING_RE.match(line)
        if not match:
            return None
        level = len(match.group(1))
        return (1 if level <= 2 else level - 1), plain_title(match.group(2).rstrip(" #"), 120)
    return None


def _references(document: Document) -> tuple[int, int] | None:
    """The line span of the bibliography, which is never rewritten."""
    lines = document.lines
    if document.format == "tex":
        start = next((index for index, line in enumerate(lines) if _BEGIN_BIBLIOGRAPHY_RE.search(line)), None)
        if start is None:
            return None
        end = next((index for index in range(start, len(lines)) if _END_BIBLIOGRAPHY_RE.search(lines[index])),
                   len(lines) - 1)
        return start, end + 1
    if document.format in ("md", "txt"):
        start = next((index for index, line in enumerate(lines) if _MD_REFERENCES_RE.match(line)), None)
        if start is None:
            return None
        level = len(lines[start]) - len(lines[start].lstrip("#"))
        end = next((index for index in range(start + 1, len(lines))
                    if (match := _MD_HEADING_RE.match(lines[index])) and len(match.group(1)) <= level), len(lines))
        return start, end
    return None


def _cuts(document: Document, start: int, end: int, target: int) -> list[int]:
    """Where to cut ``[start, end)`` so that every piece fits in ``target``:
    at subsection headings, then blank lines, then any line."""
    lines = document.lines
    tiers = []
    if document.format != "pdf":
        tiers.append([index for index in range(start + 1, end) if (heading := _heading(lines[index], document.format))
                      and heading[0] > 1])
    tiers.append([index for index in range(start + 1, end) if not lines[index].strip()])
    tiers.append(list(range(start + 1, end)))
    for tier in tiers:
        cuts: list[int] = []
        begin = start
        candidates = [index for index in tier if index > begin]
        for position, index in enumerate(candidates):
            following = candidates[position + 1] if position + 1 < len(candidates) else end
            if document.size(begin, following) > target and index > begin:
                cuts.append(index)
                begin = index
        pieces = list(zip([start] + cuts, cuts + [end]))
        if all(document.size(low, high) <= target for low, high in pieces):
            return cuts
    return list(range(start + 1, end))


def split_parts(document: Document, nodes: list[Mapping[str, Any]], *, target: int) -> list[Part]:
    """The document's parts in reading order, each with the ids of the
    results whose statements start in it (see the module docstring)."""
    count = len(document.lines)
    if not count:
        return []
    references = _references(document)
    boundaries = {0}
    if document.format == "pdf":
        boundaries.update(start for start in document.page_starts if 0 < start < count)
    else:
        boundaries.update(index for index, line in enumerate(document.lines)
                          if (heading := _heading(line, document.format)) and heading[0] == 1)
    if references:
        boundaries -= set(range(references[0] + 1, references[1]))
        boundaries.update(edge for edge in references if edge < count)
    if document.size(0, count) <= target and not references:
        boundaries = {0}
    marks = sorted(boundaries)
    units = [(low, high, "references" if references and low == references[0] else "text")
             for low, high in zip(marks, marks[1:] + [count])]
    pieces: list[tuple[int, int, str]] = []
    for low, high, kind in units:
        cuts = [] if kind == "references" else _cuts(document, low, high, target)
        pieces.extend((piece_low, piece_high, kind) for piece_low, piece_high in zip([low] + cuts, cuts + [high]))
    small = target // SMALL_PART_SHARE
    # PDF page breaks carry no structure: pages fill a part in turn.
    greedy = document.format == "pdf"
    merged: list[list[Any]] = []
    lead: int | None = None
    for low, high, kind in pieces:
        if kind == "text" and not any(line.strip() for line in document.lines[low:high]):
            # Blank lines are not a part of their own: they end the part
            # before them, or open the first one.
            if merged:
                merged[-1][1] = high
            elif lead is None:
                lead = low
            continue
        if lead is not None:
            low, lead = lead, None
        if merged and kind == "text" and merged[-1][2] == "text":
            last_low, last_high, _kind = merged[-1]
            size, last = document.size(low, high), document.size(last_low, last_high)
            if size + last <= target and (greedy or size < small or last < small):
                merged[-1][1] = high
                continue
        merged.append([low, high, kind])
    if lead is not None:
        merged.append([lead, count, "text"])  # a manuscript of blank lines
    parts = [Part(index, _title(document, low, high, kind, index), low, high, kind)
             for index, (low, high, kind) in enumerate(merged, 1)]
    located = sorted((position, node["id"]) for node in nodes
                     if (position := document.locate(node)) is not None)
    starts = [part.start for part in parts]
    for position, node_id in located:
        part = parts[bisect.bisect_right(starts, position) - 1]
        if part.kind == "text":
            part.nodes.append(node_id)
    return parts


def _title(document: Document, start: int, end: int, kind: str, index: int) -> str:
    if kind == "references":
        return "References"
    if document.format == "pdf" and document.page_starts:
        first = bisect.bisect_right(document.page_starts, start)
        last = bisect.bisect_right(document.page_starts, max(start, end - 1))
        return f"Page {first}" if first == last else f"Pages {first}–{last}"
    for line in document.lines[start:end]:
        heading = _heading(line, document.format)
        if heading and heading[1]:
            return heading[1]
    for line in reversed(document.lines[:start]):
        heading = _heading(line, document.format)
        if heading and heading[1]:
            return f"{heading[1]} (continued)"
    return "Opening" if index == 1 else f"Part {index}"
