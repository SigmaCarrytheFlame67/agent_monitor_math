"""Statement segmentation for Markdown, plain text and PDF text sources.

Headers such as ``Theorem 2.1 (Title).``, ``**Lemma 3.**``, ``### Proposition 4``
or ``\\textbf{Corollary 5.}`` open a statement; ``Proof.`` (or ``Proof of
Theorem 2.1.``) opens its proof, which runs to an end mark (QED, a box) or the
next header or heading.  Textual references ("by Lemma 2.3", "Lemmas 2.1 and
2.2") between statements become ``inferred`` edges.  PDF text comes from pypdf
(at most 300 pages) and is marked ``math_fidelity: "pdf-text"``: formulas
extracted from a PDF are often garbled.  The runner extracts and segments in
resource-limited children like the LaTeX parser (:func:`segment_sandboxed`,
:func:`pdf_document`); every pattern is linear-time, statements are capped
before references are resolved, and cycle checks use bitset reachability.

When fewer than three statements are found the runner may ask the model to
segment the text (``SEGMENT_PROMPT``); :func:`apply_model_segments` accepts
only items whose quotes occur verbatim in the text.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent_monitor.analyze import latex_structure as _latex
from agent_monitor.analyze.dag import Reachability

SCHEMA = _latex.SCHEMA
PARSER_ID = "text-1"
MAX_TEXT_CHARS = 2_000_000
MAX_PDF_PAGES = 300
MAX_PDF_BYTES = 25 * 1024 * 1024
PDF_CPU_SECONDS = 90
PDF_WALL_SECONDS = 150
SEGMENT_WALL_SECONDS = 90
MIN_STRUCTURED_NODES = 3
MODEL_CHUNK_CHARS = 24_000

_KIND_WORDS = {
    "theorem": "theorem", "lemma": "lemma", "proposition": "proposition", "corollary": "corollary",
    "claim": "claim", "definition": "definition", "conjecture": "conjecture", "remark": "remark",
    "example": "example", "fact": "fact", "observation": "observation", "hypothesis": "hypothesis",
    "assumption": "assumption", "question": "question", "problem": "problem", "step": "step",
}
_KIND_ALTERNATION = "(?i:" + "|".join(sorted((word.capitalize() for word in _KIND_WORDS), key=len, reverse=True)) + ")"
_NUMBER = r"(?:[A-Z]\.)?\d+(?:\.\d+)*[a-z]?|[A-Z](?![a-z])"
# Every run of blanks or emphasis marks is possessive (*+): a header is
# matched in one pass, so a line of thousands of spaces costs microseconds,
# not hours (no backtracking between adjacent optional groups).
HEADER_RE = re.compile(
    r"^(?P<lead>[ \t>#*_]*+(?:\\(?:noindent|textbf|textit|emph|paragraph\*?|subsubsection\*?)\s*+\{?\s*+)*+)"
    r"(?P<kind>" + _KIND_ALTERNATION + r")(?![A-Za-z])"
    r"[ \t]*+(?P<number>" + _NUMBER + r")?"
    r"[ \t*_}]*+(?:\((?P<title>[^()\n]{1,160})\))?[ \t*_}]*+(?P<punct>\.(?!\d)|:|$)",
)
PROOF_RE = re.compile(
    r"^[ \t>*_]*+(?:\\(?:noindent|textit|emph|textbf)\s*+\{?\s*+)*+(?P<word>(?i:Proof))(?![A-Za-z])"
    r"(?:[ \t]++(?i:of)[ \t]++(?P<kind>" + _KIND_ALTERNATION + r")[ \t~]*+(?P<number>" + _NUMBER + r"))?"
    r"[^\n.:]{0,40}?[.:]",
)
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]++(?P<title>[^\n]+)$")
END_MARK_RE = re.compile(r"(?:∎|□|■|◻|▮|\\qed\b|\\blacksquare\b|\\square\b|\bQ\.?E\.?D\.?|\bq\.e\.d\.)\s*+\$?\s*+$")
REFERENCE_RE = re.compile(
    r"\b(?P<kind>" + _KIND_ALTERNATION + r")(?P<plural>s)?[ \t~]++"
    r"(?P<numbers>(?:" + _NUMBER + r")(?:[ \t]*+(?:,|and|&|or)[ \t]*+(?:" + _NUMBER + r"))*+)"
)
MAX_REFERENCES = 64  # distinct results one statement or proof may cite
MAX_PROOF_BLOCKS = 8  # proof blocks joined to one statement
_SPLIT_NUMBERS_RE = re.compile(r"[ \t]*(?:,|and|&|or)[ \t]*")


class TextStructureError(ValueError):
    """The text has no usable structure (public-safe message)."""


def normalize(text: str) -> str:
    """NFKC (ligatures), LF line ends, no NUL."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")[:MAX_TEXT_CHARS]


def _section_title(line: str) -> tuple[int, str] | None:
    match = HEADING_RE.match(line)
    if match:
        title = match.group("title").rstrip(" \t#").strip() or match.group("title").strip()[:1]
        return len(match.group("hashes")), title
    return None


def _header(line: str) -> re.Match | None:
    match = HEADER_RE.match(line)
    if not match:
        return None
    word = match.group("kind")
    if not word[0].isupper():
        # "conjecture." or "lemma 2." starting a wrapped line is running text;
        # headers are capitalized (or all capitals in some PDFs).
        return None
    lead = match.group("lead") or ""
    if match.group("number") is None and not match.group("title") and not re.search(r"[*_#\\]", lead):
        # An unnumbered "Theorem." needs emphasis or a "(Title)" to be a header.
        return None
    # "Lemma 2 shows that ..." in running text is a reference, not a header:
    # a header ends its label with punctuation, a closing emphasis or the line.
    if match.group("punct") == "" and not lead.strip() and not line.rstrip().endswith((".", ":")):
        if len(line.strip()) > len(match.group(0).strip()) + 1:
            return None
    return match


def segment_text(text: str, *, file: str, fmt: str, page_starts: list[int] | None = None) -> dict[str, Any]:
    """Segment ``text`` (already normalized) into a segments document."""
    lines = text.split("\n")
    blocks: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    current_section: list[dict[str, Any]] = []
    index = 0
    open_block: dict[str, Any] | None = None
    seen_numbers: set[tuple[str, str]] = set()

    def close(end_line: int) -> None:
        nonlocal open_block
        if open_block is not None:
            open_block["end"] = max(open_block["start"], end_line)
            blocks.append(open_block)
            open_block = None

    while index < len(lines):
        line = lines[index]
        heading = _section_title(line) if fmt == "md" else None
        if heading is not None:
            close(index - 1)
            level, title = heading
            current_section = [entry for entry in current_section if entry["level"] < level]
            entry = {"level": level, "title": title[:160], "line": index + 1}
            current_section.append(entry)
            sections.append(dict(entry, file=file, number=None))
            index += 1
            continue
        header = _header(line)
        if header is not None and fmt == "pdf" and header.group("number"):
            # PDF text wraps lines anywhere, so "...by\nLemma 6.2. Thus" can look
            # like a header; a number already seen is a reference (or a restatement).
            key = (header.group("kind").lower(), header.group("number"))
            if key in seen_numbers:
                header = None
            else:
                seen_numbers.add(key)
        if header is not None:
            close(index - 1)
            open_block = {
                "type": "statement",
                "kind": _KIND_WORDS[header.group("kind").lower()],
                "display": header.group("kind"),
                "number": header.group("number"),
                "title": (header.group("title") or "").strip() or None,
                "start": index,
                "body_start": index,
                "first_line": re.sub(r"^[\s*_}]+", "", line[header.end():]),
                "section": [dict(entry) for entry in current_section],
            }
            index += 1
            continue
        proof = PROOF_RE.match(line)
        if proof is not None:
            close(index - 1)
            open_block = {
                "type": "proof",
                "target_kind": (proof.group("kind") or "").lower() or None,
                "target_number": proof.group("number"),
                "start": index,
                "first_line": re.sub(r"^[\s*_}]+", "", line[proof.end():]),
                "section": [dict(entry) for entry in current_section],
            }
            if END_MARK_RE.search(line):
                close(index)
            index += 1
            continue
        if open_block is not None:
            if open_block["type"] == "proof" and END_MARK_RE.search(line):
                close(index)
                index += 1
                continue
            if open_block["type"] == "statement" and not line.strip():
                # A statement is one paragraph unless the next paragraph is a
                # list of its items, (i)/(a)/1./-.
                following = next((candidate for candidate in lines[index + 1:index + 3] if candidate.strip()), "")
                if not re.match(r"^\s*(?:\([ivxlcdm]+\)|\([a-z]\)|\d+[.)]|[-*•])\s", following):
                    close(index - 1)
        index += 1
    close(len(lines) - 1)

    statements = [block for block in blocks if block["type"] == "statement"]
    proofs = [block for block in blocks if block["type"] == "proof"]
    warnings: list[str] = []
    truncated = None
    if len(statements) > _latex.MAX_NODES:
        # Capped before any edge is built: the reference and cycle work then
        # stays bounded whatever the text holds.
        truncated = {"statements": len(statements), "kept": _latex.MAX_NODES}
        warnings.append(f"The text has {len(statements)} statements; the first {_latex.MAX_NODES} are kept")
        statements = statements[: _latex.MAX_NODES]
    nodes: list[dict[str, Any]] = []
    for block in statements:
        body = "\n".join([block["first_line"], *lines[block["start"] + 1: block["end"] + 1]]).strip()
        nodes.append({
            "kind": block["kind"],
            "env": block["kind"],
            "display": block["display"],
            "number": block["number"],
            "title": block["title"],
            "labels": [],
            "inner_labels": [],
            "file": file,
            "lines": [block["start"] + 1, block["end"] + 1],
            "proof_file": None,
            "proof_lines": None,
            "section": [{"level": f"h{entry['level']}", "title": entry["title"], "number": None}
                        for entry in block["section"]],
            "statement": body[: _latex.MAX_STATEMENT_CHARS],
            "statement_chars": len(body),
            "proof": "",
            "proof_chars": 0,
            "has_proof": False,
            "proof_link": [],
            "cites": [],
            "nested_in": None,
            "external": bool(re.search(r"\[[A-Z][A-Za-z]*\d{2,4}[a-z]?\]|\bsee\s+\[", block["title"] or "")),
            "_start": block["start"],
        })
    by_number = {(node["kind"], node["number"]): node for node in nodes if node["number"]}
    order = sorted([("s", node["_start"], node) for node in nodes] + [("p", block["start"], block) for block in proofs],
                   key=lambda item: item[1])
    last = None
    parts: dict[int, list[str]] = defaultdict(list)
    unattached = 0
    for tag, _start, item in order:
        if tag == "s":
            if item["kind"] in _latex.PROVABLE:
                last = item
            continue
        target = None
        how = "preceding"
        if item.get("target_kind") and item.get("target_number"):
            # "Proof of Theorem 2.1." names its result: never the preceding one.
            target = by_number.get((_KIND_WORDS.get(item["target_kind"], ""), item["target_number"]))
            how = "title-ref"
        elif last is not None and not last["has_proof"]:
            target = last
        if target is None or len(parts[id(target)]) >= MAX_PROOF_BLOCKS:
            unattached += 1
            continue
        proof_text = "\n".join([item["first_line"], *lines[item["start"] + 1: item["end"] + 1]]).strip()
        parts[id(target)].append(proof_text)
        target["proof_chars"] += len(proof_text)
        target["has_proof"] = True
        target["proof_link"].append(how)
        target["proof_file"] = file
        if target["proof_lines"] is None:
            target["proof_lines"] = [item["start"] + 1, item["end"] + 1]
    for node in nodes:
        if parts.get(id(node)):
            node["proof"] = "\n\n".join(parts[id(node)])[: _latex.MAX_PROOF_CHARS]
    if unattached:
        warnings.append(f"{unattached} proof(s) could not be matched to a statement and were left out")

    _latex.assign_ids(nodes)
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for node in nodes:
        # Proof references first: a proof citing a result is stronger evidence
        # than the statement mentioning it.
        cited: set[str] = set()
        for context, text_part in (("proof", node["proof"]), ("statement", node["statement"])):
            for match in REFERENCE_RE.finditer(text_part):
                kind = _KIND_WORDS[match.group("kind").lower()]
                for number in _SPLIT_NUMBERS_RE.split(match.group("numbers")):
                    used = by_number.get((kind, number.strip()))
                    if used is None or used is node or (used["id"] not in cited and len(cited) >= MAX_REFERENCES):
                        continue
                    cited.add(used["id"])
                    found.setdefault((used["id"], node["id"]), {
                        "from": used["id"], "to": node["id"], "label": "uses", "provenance": "inferred",
                        "context": context, "basis": "text-reference", "strength": 3 if context == "proof" else 1})
    edges, dropped = _acyclic(list(found.values()))
    _mark_main(nodes, edges)
    if page_starts:
        for node in nodes:
            node["pages"] = [_page_of(page_starts, node["lines"][0]), _page_of(page_starts, node["lines"][1])]
    for node in nodes:
        node.pop("_start", None)
    title = next((section["title"] for section in sections if section["level"] == 1), "")
    if not title:
        title = next((line.strip().lstrip("#").strip() for line in lines if line.strip()), "")[:300]
    abstract = ""
    match = re.search(r"(?im)^[#*_ \t]*abstract[*_ \t.:]*\n?(.{20,4000}?)(?:\n\s*\n|\Z)", text, re.S)
    if match:
        abstract = _latex.collapse(match.group(1))[: _latex.MAX_ABSTRACT_CHARS]
    intro = ""
    intro_section = next((section for section in sections if re.search(r"intro", section["title"], re.I)), None)
    if intro_section is not None:
        start = intro_section["line"]
        later = [section["line"] for section in sections if section["line"] > start and section["level"] <= intro_section["level"]]
        intro = "\n".join(lines[start: (later[0] - 1) if later else len(lines)])[: _latex.MAX_INTRO_CHARS].strip()
    else:
        first = nodes[0]["lines"][0] - 1 if nodes else len(lines)
        intro = "\n".join(lines[:first])[: _latex.MAX_INTRO_CHARS].strip()
    return {
        "schema": SCHEMA,
        "format": fmt,
        "parser": PARSER_ID,
        "root_file": file,
        "files": [file],
        "missing_includes": [],
        "file_errors": [],
        "title": title[:300],
        "abstract": abstract,
        "intro": intro,
        "sections": sections[:400],
        "theorem_decls": {},
        "macros": {},
        "bibliography": {},
        "nodes": nodes,
        "edges": edges,
        "dropped_for_acyclicity": dropped[:50],
        "truncated": truncated,
        "warnings": warnings,
        "stats": {
            "statements": len(nodes),
            "proofs_attached": sum(1 for node in nodes if node["has_proof"]),
            "uses_edges": len(edges),
            "numbered": sum(1 for node in nodes if node["number"]),
        },
        "bytes": len(text.encode("utf-8")),
    }


def _page_of(page_starts: list[int], line: int) -> int:
    page = 1
    for index, start in enumerate(page_starts, 1):
        if line >= start:
            page = index
    return page


def _acyclic(edges: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop edges that would close a cycle, weakest (statement context) first."""
    reach = Reachability()
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for edge in sorted(edges, key=lambda item: (-item["strength"], item["to"], item["from"])):
        if not reach.add(edge["from"], edge["to"]):
            dropped.append({"from": edge["from"], "to": edge["to"], "where": [edge["context"]]})
            continue
        kept.append(edge)
    kept.sort(key=lambda item: (item["to"], item["from"]))
    return kept, dropped


def _mark_main(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    used = {edge["from"] for edge in edges}
    theorems = [node for node in nodes if node["kind"] == "theorem" and not node["external"]]
    main = {node["id"] for node in theorems if "main" in (node["title"] or "").lower()}
    if not main and any(node["section"] for node in nodes):
        first = next((tuple(section["title"] for section in node["section"][:1]) for node in nodes if node["section"]), None)
        main = {node["id"] for node in theorems
                if node["section"] and tuple(section["title"] for section in node["section"][:1]) == first}
    if not main and not any(node["section"] for node in nodes):
        # No headings (plain or PDF text): theorems of section 1, else the first.
        main = {node["id"] for node in theorems if re.fullmatch(r"1(?:\.\d+)*[a-z]?", node["number"] or "")}
        if not main and theorems:
            main = {theorems[0]["id"]}
    if not main:
        main = {node["id"] for node in theorems if node["id"] not in used}
    if not main:
        for kind in ("proposition", "corollary", "lemma", "claim"):
            candidates = [node["id"] for node in nodes
                          if node["kind"] == kind and node["has_proof"] and node["id"] not in used]
            if candidates:
                main = set(candidates[:8])
                break
    for node in nodes:
        node["main_result"] = node["id"] in main


def read_text_file(path: Path, *, file: str) -> tuple[str, list[str]]:
    """A pasted or uploaded text file, NFKC-normalized, and its warnings:
    UTF-8 (a character cut by the size cap is dropped), else Latin-1."""
    with open(path, "rb") as handle:
        raw = handle.read(MAX_TEXT_CHARS * 4 + 1)
    warnings: list[str] = []
    cut = len(raw) > MAX_TEXT_CHARS * 4
    raw = raw[: MAX_TEXT_CHARS * 4]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        text = None
        if exc.start >= len(raw) - 3:
            # Only the last character is incomplete (the size cap, or a
            # resource limit, cut it): keep the rest as UTF-8.
            try:
                text = raw[: exc.start].decode("utf-8-sig")
            except UnicodeDecodeError:
                text = None
        if text is None:
            text = raw.decode("latin-1")
            warnings.append(f"{file} is not UTF-8; read as Latin-1")
    if cut or len(text) > MAX_TEXT_CHARS:
        warnings.append(f"Only the first {MAX_TEXT_CHARS:,} characters of {file} were read")
    return normalize(text), warnings


def segment_file(path: Path, *, file: str, fmt: str, pages: bool = False) -> dict[str, Any]:
    """Segment a text file; ``pages``: PDF text with a form feed line
    between pages (nodes then carry their pages)."""
    text, warnings = read_text_file(path, file=file)
    page_starts = None
    if pages:
        parts = text.split("\n" + PAGE_MARK + "\n")
        page_starts = []
        line = 1
        for part in parts:
            page_starts.append(line)
            line += part.count("\n") + 1
        text = "\n".join(parts)
    document = segment_text(text, file=file, fmt=fmt, page_starts=page_starts)
    document["warnings"] = warnings + document["warnings"]
    return document


def parse_text_file(path: Path, *, file: str, fmt: str) -> dict[str, Any]:
    """In-process segmentation of a Markdown or text file (the runner uses
    :func:`segment_sandboxed`)."""
    return segment_file(path, file=file, fmt=fmt)


def segment_sandboxed(path: Path, out: Path, *, file: str, fmt: str, pages: bool = False,
                      cancel: Any = None, timeout: float = SEGMENT_WALL_SECONDS) -> dict[str, Any]:
    """:func:`segment_file` in a resource-limited child (60 s of CPU, 1 GiB):
    a hostile text can cost a run its structure, never the runner.

    Raises :class:`TextStructureError` with a public-safe message, or
    ``latex_structure.SandboxCancelled`` when ``cancel`` is set.
    """
    args = ["--segment", str(Path(path).resolve()), str(Path(out).resolve()), f"--name={file}", f"--format={fmt}"]
    if pages:
        args.append("--pages")
    try:
        completed = _latex.run_module_sandboxed("agent_monitor.analyze.text_structure", args, timeout=timeout,
                                                cancel=cancel)
    except subprocess.TimeoutExpired:
        raise TextStructureError("Segmenting the text took too long") from None
    try:
        if completed.returncode != 0:
            raise TextStructureError("The text could not be segmented (resource limit or malformed input)")
        try:
            raw = Path(out).read_bytes()
            if len(raw) > 64 * 1024 * 1024:
                raise ValueError
            document = json.loads(raw)
        except (OSError, ValueError):
            raise TextStructureError("The text could not be segmented") from None
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            raise TextStructureError("The text could not be segmented")
        return document
    finally:
        Path(out).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PDF text (pypdf, sandboxed)
# ---------------------------------------------------------------------------

PAGE_MARK = "\f"


def extract_pdf_text(pdf: Path, out: Path, *, max_pages: int = MAX_PDF_PAGES) -> int:
    """Write the text of up to ``max_pages`` pages to ``out`` (form feed between
    pages), flushing per page so a killed extraction keeps what it has.
    Returns the number of pages in the document."""
    import logging

    logging.disable(logging.CRITICAL)
    from pypdf import PdfReader

    if pdf.stat().st_size > MAX_PDF_BYTES:
        raise TextStructureError("The PDF exceeds its size limit")
    reader = PdfReader(str(pdf), strict=False)
    total = len(reader.pages)
    with out.open("w", encoding="utf-8", errors="replace") as handle:
        for number, page in enumerate(reader.pages, 1):
            if number > max_pages:
                break
            try:
                # A broken ToUnicode map can yield lone surrogates: replace
                # them, so one bad page never stops the pages after it.
                text = normalize(page.extract_text() or "").encode("utf-8", "replace").decode("utf-8")
                handle.write(text)
            except Exception:  # noqa: BLE001 - one bad page must not stop the rest
                pass
            handle.write("\n" + PAGE_MARK + "\n")
            handle.flush()
    return total


def pdf_document(pdf: Path, text_path: Path, *, file: str, cancel: Any = None) -> dict[str, Any]:
    """Extract and segment a PDF, each step in a resource-limited child;
    ``text_path`` receives the text.  Raises :class:`TextStructureError`
    (``latex_structure.ParserUnavailable`` when pypdf is missing)."""
    args = ["--pdf", str(Path(pdf).resolve()), str(Path(text_path).resolve())]
    warnings = []
    pages_total = None
    try:
        completed = _latex.run_module_sandboxed("agent_monitor.analyze.text_structure", args,
                                                timeout=PDF_WALL_SECONDS, limits=_pdf_limits, cancel=cancel)
        if completed.returncode == 4:
            raise _latex.ParserUnavailable("The PDF text extractor (pypdf) is not installed on this server")
        if completed.returncode == 0:
            try:
                pages_total = int(completed.stdout.decode().strip() or 0)
            except ValueError:
                pages_total = None
        else:
            warnings.append("PDF text extraction stopped early (resource limit or malformed PDF)")
    except subprocess.TimeoutExpired:
        warnings.append("PDF text extraction stopped early (time limit)")
    try:
        with open(text_path, "rb") as handle:
            has_text = bool(handle.read(MAX_TEXT_CHARS * 4).decode("utf-8", "replace").replace(PAGE_MARK, "").strip())
    except OSError:
        has_text = False
    if not has_text:
        raise TextStructureError("No text could be extracted from the PDF (it may be scanned images)")
    if pages_total and pages_total > MAX_PDF_PAGES:
        warnings.append(f"Only the first {MAX_PDF_PAGES} of {pages_total} PDF pages were read")
    document = segment_sandboxed(text_path, Path(str(text_path) + ".segments.json"), file=file, fmt="pdf",
                                 pages=True, cancel=cancel)
    document["parser"] = f"{_pypdf_id()}+{PARSER_ID}"
    document["math_fidelity"] = "pdf-text"
    document["warnings"] = warnings + document["warnings"]
    return document


def _pypdf_id() -> str:
    try:
        import pypdf

        return f"pypdf-{pypdf.__version__}"
    except ImportError:  # pragma: no cover
        return "pypdf-missing"


def _pdf_limits() -> None:  # pragma: no cover - runs in the child
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (_latex.SANDBOX_MEMORY_BYTES, _latex.SANDBOX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (PDF_CPU_SECONDS, PDF_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


# ---------------------------------------------------------------------------
# model-assisted segmentation (validated quotes only)
# ---------------------------------------------------------------------------


def chunks(text: str, size: int = MODEL_CHUNK_CHARS, *, limit: int = 4) -> list[tuple[int, str]]:
    """Up to ``limit`` paragraph-aligned chunks of at most ``size`` chars,
    with their start offsets."""
    out: list[tuple[int, str]] = []
    position = 0
    while position < len(text) and len(out) < limit:
        end = min(len(text), position + size)
        if end < len(text):
            cut = text.rfind("\n\n", position + size // 2, end)
            end = cut if cut > position else end
        out.append((position, text[position:end]))
        position = end
    return out


def _locate(haystack: str, quote: str) -> tuple[int, int] | None:
    """Span of ``quote`` in ``haystack`` with whitespace runs matched loosely."""
    words = [re.escape(word) for word in str(quote or "").split()]
    if len(words) < 3:
        return None
    match = re.search(r"\s+".join(words), haystack)
    return (match.start(), match.end()) if match else None


def apply_model_segments(text: str, items: Iterable[Mapping[str, Any]], *, file: str, fmt: str) -> dict[str, Any]:
    """A segments document from model-proposed statements.

    Each item must quote its statement (and optionally the first and last
    words of its proof) verbatim from ``text``; anything else is dropped.
    Statements and proofs are sliced from ``text`` itself, never taken from
    the model, and every edge is ``model`` provenance.  An item may carry
    ``_chunk: (offset, length)``, the chunk the model saw: its quotes are
    then looked for there (and a proof up to one chunk further), so the
    search cost does not grow with the whole text.
    """
    nodes: list[dict[str, Any]] = []
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)

    def line_of(offset: int) -> int:
        import bisect

        return bisect.bisect_right(starts, offset)

    for item in list(items)[:64]:
        kind = _KIND_WORDS.get(str(item.get("kind") or "").strip().lower())
        window = item.get("_chunk")
        if isinstance(window, (tuple, list)) and len(window) == 2:
            low, high = max(0, int(window[0])), min(len(text), int(window[0]) + int(window[1]))
        else:
            low, high = 0, len(text)
        found = _locate(text[low:high], str(item.get("statement_quote") or "")) if kind is not None else None
        if found is None:
            continue
        span = (low + found[0], low + found[1])
        limit = min(len(text), high + MODEL_CHUNK_CHARS)
        proof = ""
        proof_lines = None
        proof_start = _locate(text[span[1]:limit], str(item.get("proof_start_quote") or ""))
        if proof_start is not None:
            begin = span[1] + proof_start[0]
            end_span = _locate(text[begin:min(len(text), begin + 20_000)], str(item.get("proof_end_quote") or ""))
            end = begin + end_span[1] if end_span is not None else min(len(text), begin + 20_000)
            proof = text[begin:end].strip()
            proof_lines = [line_of(begin), line_of(max(begin, end - 1))]
        label = str(item.get("label") or "").strip()[:60]
        number_match = re.search(_NUMBER, label)
        nodes.append({
            "kind": kind, "env": kind, "display": kind.capitalize(),
            "number": number_match.group(0) if number_match else None,
            "title": None, "labels": [], "inner_labels": [], "file": file,
            "lines": [line_of(span[0]), line_of(max(span[0], span[1] - 1))],
            "proof_file": file if proof else None, "proof_lines": proof_lines, "section": [],
            "statement": text[span[0]:span[1]].strip()[: _latex.MAX_STATEMENT_CHARS],
            "statement_chars": span[1] - span[0], "proof": proof[: _latex.MAX_PROOF_CHARS],
            "proof_chars": len(proof), "has_proof": bool(proof), "proof_link": ["model"] if proof else [],
            "cites": [], "nested_in": None, "external": False, "_uses": item.get("uses") or [],
        })
    nodes.sort(key=lambda node: node["lines"][0])
    deduped: list[dict[str, Any]] = []
    for node in nodes:
        if deduped and deduped[-1]["lines"] == node["lines"]:
            continue
        deduped.append(node)
    nodes = deduped[: _latex.MAX_NODES]
    _latex.assign_ids(nodes)
    by_display = {f'{node["display"]} {node["number"]}'.lower(): node for node in nodes if node["number"]}
    edges = []
    for node in nodes:
        for reference in list(node.pop("_uses"))[:16]:
            used = by_display.get(str(reference or "").strip().lower())
            if used is not None and used is not node:
                edges.append({"from": used["id"], "to": node["id"], "label": "uses", "provenance": "model",
                              "context": "proof", "basis": "model", "strength": 1})
    edges, dropped = _acyclic(edges)
    _mark_main(nodes, edges)
    return {
        "schema": SCHEMA, "format": fmt, "parser": f"{PARSER_ID}+model-segments", "root_file": file,
        "files": [file], "missing_includes": [], "file_errors": [], "title": "", "abstract": "", "intro": "",
        "sections": [], "theorem_decls": {}, "macros": {}, "bibliography": {}, "nodes": nodes, "edges": edges,
        "dropped_for_acyclicity": dropped, "truncated": None,
        "warnings": ["Statements were located by the model (quotes checked against the text)"],
        "stats": {"statements": len(nodes), "uses_edges": len(edges)}, "bytes": len(text.encode("utf-8")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze text helpers (sandboxed)")
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--pdf", nargs=2, metavar=("PDF", "OUT"), help="extract PDF text")
    task.add_argument("--segment", nargs=2, metavar=("TEXT", "OUT"), help="segment a text file into JSON")
    parser.add_argument("--name", default="")
    parser.add_argument("--format", default="txt", choices=("md", "txt", "pdf", "tex"))
    parser.add_argument("--pages", action="store_true")
    args = parser.parse_args(argv)
    if args.segment:
        source, target = Path(args.segment[0]), Path(args.segment[1])
        try:
            document = segment_file(source, file=args.name or source.name, fmt=args.format, pages=args.pages)
        except (OSError, ValueError, RecursionError) as exc:
            print(type(exc).__name__, file=sys.stderr)
            return 3
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8", errors="replace")
        os.replace(temporary, target)
        return 0
    try:
        total = extract_pdf_text(Path(args.pdf[0]), Path(args.pdf[1]))
    except ImportError:
        print("pypdf is not installed", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 - reported as a code to the parent
        print(type(exc).__name__, file=sys.stderr)
        return 3
    print(total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
