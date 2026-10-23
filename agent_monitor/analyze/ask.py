"""Questions about a finished analysis (the Ask panel of an Analyze run).

The console writes ``analysis/asks/<ask_id>.json`` and runs
``analyze_runner.py --ask <ask_id>`` with the analysis's own route; the files
and the conversation so far are :mod:`agent_monitor.manuscript_ask`'s.  A
question is answered from what the analysis published, never from anything
fetched again: the title, abstract and analysis summary (:func:`overview`),
the results with their verdicts and the verifier's main findings, as many as
fit (:func:`results_list`), and in full (statement, the proof the manuscript
gives, verdict and findings, the results it uses and is used by,
:func:`result_text`) the result asked about from the DAG, or the results a
selected passage belongs to or names (:func:`related`).  Verdicts are a model
verifier's judgements; nothing here calls them verified.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_monitor.analyze import report
from agent_monitor.analyze.graph import ref_labels
from agent_monitor.analyze.prompts import clip, fence, fence_piece, utf8_len
from agent_monitor.manuscript_ask import AskStore
from agent_monitor.readability.ask import comparable

STORE = AskStore(("analysis", "asks"), ("report", "source"),
                 node_refusal="node_id must be a result of this analysis")
VERDICT_TEXT = {
    "checked": "checked (no issue found)",
    "gap": "gap",
    "error": "error",
    "unclear": "unclear",
    "not_checked": "not checked",
}
# What a result list puts first when it cannot list every result.
_SEVERITY = {"error": 0, "gap": 1, "unclear": 2, "checked": 3, "not_checked": 4}
PROVENANCE_TEXT = {"ref": "a reference in the manuscript", "inferred": "inferred from the text",
                   "model": "proposed by a model"}
# At most this many results a passage belongs to or names; the first
# characters of a passage that are looked up.
MAX_RELATED = 2
# At most this many results are shown in full with a question: the one asked
# about, the ones a passage belongs to or names, and the ones the question names.
MAX_SUBJECTS = 3
# The start of a passage is looked up at these lengths in turn (a passage
# selected across the end of a statement and the start of its proof is found
# by its shorter start); a start shorter than the last is not looked up.
_LOOKUP_LENGTHS = (300, 120, 48)
_MIN_LOOKUP_CHARS = 12
# The stored source files a passage selected in the source is looked up in
# (each at most this many bytes, and this many in all).
SOURCE_FILE_BYTES = 2_000_000
SOURCE_BYTES = 8_000_000
_SPACE_RE = re.compile(r"\s+")
_NONSPACE_RE = re.compile(r"\S")
# The characters ``\s`` matches (str.isspace), deleted by str.translate: a
# source file is compacted in one pass however many lines it has.
_WHITESPACE = dict.fromkeys(
    [0x9, 0xA, 0xB, 0xC, 0xD, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
     0x2028, 0x2029, 0x202F, 0x205F, 0x3000])
# The chunks a source file is counted in when a place in it is looked up.
_CHUNK = 65_536
# What a lookup finds when the start of a passage stands in more than one result.
AMBIGUOUS = "\x00ambiguous"
# The places in a file where a passage's start is found that are looked at.
_SOURCE_HITS = 16
# "\ref{lem:x}" (and its variants) in a passage of the source names the result labelled so.
_REF_RE = re.compile(r"\\(?:eq|c|C|auto|name)?ref\*?\s*\{([^{}\n]{1,120})\}")
_KIND_WORDS = ("theorem|lemma|proposition|corollary|claim|definition|conjecture|remark|fact|observation"
               "|example|assumption|hypothesis|axiom|condition|principle|notation|step")
_NAME_RE = re.compile(rf"(?i)\b({_KIND_WORDS})s?\s*~?\s*((?:[A-Z]\.)?\d+(?:\.\d+)*[a-z]?)\b")
# A question about "the main theorem" (or "main result") without naming it.
_MAIN_RE = re.compile(r"(?i)\bmain\s+(?:theorem|result|claim)s?\b")


def _one_line(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def segments_by_id(segments: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(node.get("id")): node for node in segments.get("nodes") or []
            if isinstance(node, Mapping) and node.get("id")}


def printed(node: Mapping[str, Any], segment: Mapping[str, Any] | None = None) -> str:
    """A result as the manuscript prints it ("Lemma 3.2"; the graph label,
    which may carry a title, when the number is unknown)."""
    meta = node.get("meta") or {}
    number = " ".join(str(meta.get("number") or (segment or {}).get("number") or "").split())
    display = " ".join(str((segment or {}).get("display") or "").split())
    if not display:
        display = str(node.get("kind") or "result").replace("_", " ").capitalize()
    if number:
        return f"{display} {number}"
    return _one_line(node.get("label") or display, 160)


def names(graph: Mapping[str, Any], segments: Mapping[str, Any]) -> dict[str, str]:
    """``{id: printed name}`` of the graph's results."""
    by_id = segments_by_id(segments)
    return {node["id"]: printed(node, by_id.get(node["id"])) for node in graph.get("nodes") or []}


def _verdict_line(meta: Mapping[str, Any], *, structural: bool) -> str:
    verdict = str(meta.get("verdict") or "not_checked")
    text = VERDICT_TEXT.get(verdict, verdict)
    if meta.get("interpreted") is False:
        text += " (the verifier's reply could not be read)"
    if meta.get("confidence"):
        text += f", confidence {meta['confidence']}"
    closure = meta.get("closure")
    if closure:
        text += f"; with everything it depends on: {VERDICT_TEXT.get(closure, closure)}"
    elif not structural:
        text += "; no verdict with dependencies (its dependencies were proposed by a model)"
    conditions = []
    if meta.get("closure_external"):
        conditions.append(f"{meta['closure_external']} cited external result(s)")
    if meta.get("closure_assumptions"):
        conditions.append(f"{len(meta['closure_assumptions'])} assumed statement(s)")
    if conditions:
        text += "; conditional on " + " and ".join(conditions)
    return text


def _finding(meta: Mapping[str, Any]) -> str:
    """The verifier's main finding about a result, in one line."""
    issues = [item for item in meta.get("issues") or [] if isinstance(item, Mapping)]
    first = next((item for item in issues if item.get("type") == "error"), None) or (issues[0] if issues else None)
    if first:
        where = f" at {_one_line(first.get('location'), 80)}" if first.get("location") else ""
        more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
        return f"{first.get('type')}{where}: {_one_line(first.get('issue'), 260)}{more}"
    if meta.get("verdict") == "not_checked" and meta.get("reason"):
        return "not checked: " + _one_line(meta["reason"], 200)
    if meta.get("summary"):
        return _one_line(meta["summary"], 260)
    return ""


def overview(*, graph: Mapping[str, Any], segments: Mapping[str, Any], manuscript: Mapping[str, Any],
             receipt: Mapping[str, Any], limit: int) -> str:
    """The body of ``<OVERVIEW>``: title, abstract, the model-written summary
    of the claims, the verdict counts, the main results and how the analysis
    was made, within ``limit`` UTF-8 bytes."""
    title = _one_line(manuscript.get("title") or segments.get("title") or "untitled manuscript", 300)
    lines = [f"Title: {title}"]
    if manuscript.get("version"):
        lines.append(f"Version: {_one_line(manuscript['version'], 120)}")
    abstract = " ".join(str(segments.get("abstract") or "").split())
    if abstract:
        lines.append("Abstract: " + clip(abstract, max(600, limit // 4)))
    paper_map = receipt.get("paper_map") if isinstance(receipt.get("paper_map"), Mapping) else {}
    if paper_map.get("summary"):
        lines.append("What the manuscript claims (model-written from its abstract and introduction): "
                     + _one_line(paper_map["summary"], 1200))
    counts = graph.get("counts") or {}
    coverage = graph.get("coverage") or {}
    lines.append(
        f"The analysis: {len(graph.get('nodes') or [])} statements; {coverage.get('checked', 0)} of "
        f"{coverage.get('total', 0)} results with a located proof were checked. Verdicts: "
        + ", ".join(f"{int(counts.get(key) or 0)} {VERDICT_TEXT[key]}" for key in VERDICT_TEXT) + ".")
    by_name = names(graph, segments)
    nodes = {node["id"]: node for node in graph.get("nodes") or []}
    structural = graph.get("dependency_basis") != "model"
    mains = [nodes[node_id] for node_id in graph.get("main_results") or [] if node_id in nodes]
    for node in mains:
        lines.append(f"Main result: {by_name[node['id']]} [[result:{node['id']}]]: "
                     + _verdict_line(node.get("meta") or {}, structural=structural))
    if not mains:
        lines.append("No main result was identified.")
    if not structural:
        lines.append("The statements were located by a model, so the dependencies between results were "
                     "proposed by a model and no verdict with dependencies is given.")
    degraded = [str(item) for item in receipt.get("degraded") or [] if isinstance(item, str)]
    if degraded:
        lines.append("The analysis was cut short: " + ", ".join(degraded[:6]) + ".")
    if receipt.get("model"):
        lines.append(f"Checked by the model verifier {_one_line(receipt['model'], 80)}, one result at a time "
                     "(not a certificate).")
    # Fenced before it is cut to size: fencing writes "<" as the three-byte "‹".
    return clip(fence("\n".join(lines)), limit)


def results_list(graph: Mapping[str, Any], segments: Mapping[str, Any], limit: int) -> str:
    """The body of ``<RESULTS>``: one line per result (id, printed name, kind,
    verdict, the start of its statement and the verifier's main finding) in
    document order within ``limit`` UTF-8 bytes; when they do not all fit,
    the main results and the results with an error, a gap or an unclear
    verdict are kept first, and the rest are counted as omitted."""
    by_name = names(graph, segments)
    nodes = list(graph.get("nodes") or [])
    lines: dict[str, str] = {}
    for node in nodes:
        meta = node.get("meta") or {}
        title = f" \"{_one_line(meta['title'], 100)}\"" if meta.get("title") else ""
        verdict = VERDICT_TEXT.get(str(meta.get("verdict") or "not_checked"), "not checked")
        if meta.get("closure") and meta.get("closure") != meta.get("verdict"):
            verdict += f" (with dependencies: {VERDICT_TEXT.get(meta['closure'], meta['closure'])})"
        line = (f"[{node['id']}] {by_name[node['id']]}{title} · {node.get('kind') or 'result'}"
                + (" · main result" if meta.get("main_result") or node["id"] in (graph.get("main_results") or []) else "")
                + f" · {verdict}: {_one_line(node.get('statement'), 240)}")
        finding = _finding(meta)
        if finding and meta.get("verdict") != "checked":
            line += f" — finding: {finding}"
        # Fenced as if text followed (a line may end in "</RESULTS").
        lines[node["id"]] = fence_piece(line)
    ranked = sorted(nodes, key=lambda node: (
        0 if node["id"] in (graph.get("main_results") or []) else 1,
        _SEVERITY.get(str((node.get("meta") or {}).get("verdict") or "not_checked"), 5)))
    kept: set[str] = set()
    used = 0
    for node in ranked:
        size = utf8_len(lines[node["id"]]) + 1
        if used + size > limit - 80:
            continue
        kept.add(node["id"])
        used += size
    shown = [lines[node["id"]] for node in nodes if node["id"] in kept]
    if len(kept) < len(nodes):
        shown.append(f"[... {len(nodes) - len(kept)} further result(s) omitted for length ...]")
    return "\n".join(shown) if shown else "none: the parser found no results"


def result_text(node_id: str, *, graph: Mapping[str, Any], segments: Mapping[str, Any], limit: int) -> str:
    """One result in full, as the analysis knows it, within ``limit`` UTF-8
    bytes: its name, kind, place and verdicts, statement, the proof the
    manuscript gives (shortened in the middle when long), the verifier's
    summary, findings, missing and external dependencies and notes, and the
    results it uses and is used by.  ``""`` for an unknown id."""
    nodes = {node["id"]: node for node in graph.get("nodes") or []}
    node = nodes.get(node_id)
    if node is None:
        return ""
    segment = segments_by_id(segments).get(node_id) or {}
    by_name = names(graph, segments)
    meta = node.get("meta") or {}
    structural = graph.get("dependency_basis") != "model"
    head = f"[{node_id}] {by_name[node_id]}"
    if meta.get("title"):
        head += f" \"{_one_line(meta['title'], 160)}\""
    head += f" ({node.get('kind') or 'result'})"
    if meta.get("section"):
        head += " · section " + _one_line(" / ".join(meta["section"]), 160)
    if meta.get("locator"):
        head += f" · source {_one_line(meta['locator'], 160)}"
    lines = [head, "Verdict: " + _verdict_line(meta, structural=structural)]
    if meta.get("verdict") == "not_checked" and meta.get("reason"):
        lines.append(f"Not checked because: {_one_line(meta['reason'], 300)}")
    findings: list[str] = []
    if meta.get("summary"):
        findings.append("Verifier's summary: " + _one_line(meta["summary"], 2000))
    for item in meta.get("issues") or []:
        if isinstance(item, Mapping):
            where = f" at {_one_line(item.get('location'), 200)}" if item.get("location") else ""
            findings.append(f"- {item.get('type')}{where}: {_one_line(item.get('issue'), 1000)}")
    for item in meta.get("missing_dependencies") or []:
        if isinstance(item, Mapping) and item.get("description"):
            findings.append("- missing dependency: " + _one_line(item["description"], 400))
    for item in meta.get("external") or []:
        if isinstance(item, Mapping) and item.get("citation"):
            findings.append(f"- external result used: {_one_line(item['citation'], 200)}: "
                            f"{_one_line(item.get('statement_as_used'), 400)} (hypotheses checked: "
                            f"{item.get('hypotheses_checked') or 'unknown'})")
    if meta.get("notes"):
        findings.append("Verifier's notes: " + _one_line(meta["notes"], 1200))
    uses, used_by, guessed = [], [], []
    for edge in graph.get("edges") or []:
        # A weak edge is the parser's guess from document order (a proof that
        # cites nothing "may use" the result before it): never a dependency.
        if edge.get("to") == node_id and edge.get("from") in nodes:
            (guessed if edge.get("weak") else uses).append(edge)
        elif edge.get("from") == node_id and edge.get("to") in nodes and not edge.get("weak"):
            used_by.append(edge)

    def listed(edges: Sequence[Mapping[str, Any]], key: str) -> str:
        items = []
        for edge in edges[:24]:
            other = nodes[edge[key]]
            verdict = VERDICT_TEXT.get(str((other.get("meta") or {}).get("verdict") or "not_checked"), "")
            items.append(f"{by_name[other['id']]} [[result:{other['id']}]] ({verdict}; "
                         f"{PROVENANCE_TEXT.get(str(edge.get('provenance')), 'a link')})")
        more = f" and {len(edges) - 24} more" if len(edges) > 24 else ""
        return "; ".join(items) + more

    links = []
    if uses:
        links.append("It uses: " + listed(uses, "from"))
    if guessed:
        links.append("Possibly uses (a guess from document order, the result before it; not a dependency): "
                     + listed(guessed, "from"))
    if used_by:
        links.append("It is used by: " + listed(used_by, "to"))
    statement = str(segment.get("statement") or node.get("statement") or "").strip()
    proof = str(segment.get("proof") or "").strip()
    fixed = "\n".join(lines + links + findings)
    room = max(1_000, limit - utf8_len(fixed) - 200)
    statement = clip(statement, max(800, room // 4)) if statement else "(no statement was found)"
    if not segment.get("has_proof") and not proof:
        proof_text = "(the parser located no proof for this result)"
    else:
        proof_text = clip(proof, max(800, room - utf8_len(statement))) if proof else "(no proof text was kept)"
    body = "\n".join(lines + [f"Statement:\n{statement}", f"Proof as the manuscript gives it:\n{proof_text}"]
                     + findings + links)
    # Fenced before it is cut to size: fencing writes "<" as the three-byte "‹".
    return clip(fence(body), limit)


def _printed(node: Mapping[str, Any], refs: Mapping[str, tuple[str, str]] | None) -> str:
    """What the report prints about a result, as the console shows it
    (entities read as characters): its label, its findings (location first,
    references resolved, cut to the report's length) and why it was not
    checked."""
    meta = node.get("meta") or {}
    parts = [str(node.get("label") or ""), str(meta.get("reason") or "")]
    # The report's Findings are about the results with an error, a gap or an
    # unclear verdict that could be read (report.render prints those only).
    if meta.get("verdict") in ("error", "gap", "unclear") and meta.get("interpreted", True):
        parts += [report.finding_cell(node, refs or {}), *report.finding_bullets(node, refs or {})]
    return "\n".join(html.unescape(part) for part in parts)


def _haystack(node: Mapping[str, Any], segment: Mapping[str, Any], *, view: str) -> str:
    """The text of a result a passage selected in ``view`` may come from: in
    the source its statement and proof; in the report (after what the
    report prints, see :func:`_printed`) the verifier's raw findings,
    summary and notes, as an older report may print them."""
    if view == "report":
        meta = node.get("meta") or {}
        parts = [str(item.get("issue") or "") for item in meta.get("issues") or [] if isinstance(item, Mapping)]
        parts += [str(meta.get("summary") or ""), str(meta.get("notes") or "")]
        return "\n".join(parts)
    return "\n".join([str(segment.get("statement") or node.get("statement") or ""), str(segment.get("proof") or "")])


def _start_lookup(start: str, find) -> str | None:
    """``find(needle)`` for the start of a passage at each of
    :data:`_LOOKUP_LENGTHS` in turn (a shorter start only when the longer one
    was not found and is not the same text).  ``find`` returns the one result
    that holds a start, :data:`AMBIGUOUS` when several do (returned as it is:
    a shorter start would not tell them apart either), or ``None``."""
    previous = None
    for length in _LOOKUP_LENGTHS:
        needle = start[:length]
        if len(needle) < _MIN_LOOKUP_CHARS:
            break
        if needle == previous:
            continue
        previous = needle
        found = find(needle)
        if found:
            return found
    return None


def _one_of(holders: list[str]) -> str | None:
    """The result that holds a start when only one does, else ``AMBIGUOUS`` (or ``None`` for none)."""
    distinct = list(dict.fromkeys(holders))
    return distinct[0] if len(distinct) == 1 else AMBIGUOUS if distinct else None


def source_texts(analysis: Path, segments: Mapping[str, Any]) -> dict[str, str]:
    """The stored manuscript files of an analysis (``analysis/source/<file>``)
    that its results were read from, by file name: regular files inside the
    folder only, within :data:`SOURCE_FILE_BYTES` each and
    :data:`SOURCE_BYTES` in all."""
    folder = Path(analysis) / "source"
    if folder.is_symlink() or not folder.is_dir():
        return {}
    root = folder.resolve()
    names: list[str] = []
    for node in segments.get("nodes") or []:
        for key in ("file", "proof_file"):
            name = node.get(key) if isinstance(node, Mapping) else None
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    texts: dict[str, str] = {}
    total = 0
    for name in names:
        path = folder / name
        try:
            size = path.stat().st_size
            if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root)
                    or size > SOURCE_FILE_BYTES or total + size > SOURCE_BYTES):
                continue
            texts[name] = path.read_bytes().decode("utf-8", "replace")
        except (OSError, ValueError):
            continue
        total += size
    return texts


def _offset_of(text: str, index: int) -> int:
    """The offset in ``text`` of its ``index``-th character that is not
    whitespace, counted chunk by chunk (no per-character or per-line lists)."""
    seen = 0
    for begin in range(0, len(text), _CHUNK):
        chunk = text[begin:begin + _CHUNK]
        count = len(chunk.translate(_WHITESPACE))
        if seen + count > index:
            for match in _NONSPACE_RE.finditer(chunk):
                if seen == index:
                    return begin + match.start()
                seen += 1
        seen += count
    return len(text)


def _in_source(selection: str, *, sources: Mapping[str, str], segments: Mapping[str, Any],
               exclude: str | None) -> str | None:
    """The result whose statement or proof lines hold the start of a passage
    selected in the stored source (the start found in a file, whitespace
    aside; the innermost result when results nest), or ``None``, also when
    the start is found in more than one result.  Each file is compacted once
    and searched as a string, and at most :data:`_SOURCE_HITS` places a
    start is found at are looked at, so the cost stays linear in the files'
    size."""
    compact: dict[str, str] = {}

    def holder(name: str, line: int) -> str | None:
        best: tuple[int, str] | None = None
        for node in segments.get("nodes") or []:
            if not isinstance(node, Mapping) or not node.get("id") or node.get("id") == exclude:
                continue
            spans = []
            if node.get("file") == name:
                spans.append(node.get("lines"))
            if (node.get("proof_file") or node.get("file")) == name:
                spans.append(node.get("proof_lines"))
            for span in spans:
                if (isinstance(span, list) and len(span) == 2 and all(isinstance(end, int) for end in span)
                        and span[0] <= line <= span[1] and (best is None or span[1] - span[0] < best[0])):
                    best = (span[1] - span[0], str(node["id"]))
        return best[1] if best else None

    def find(needle: str) -> str | None:
        holders: list[str] = []
        hits = 0
        for name, text in sources.items():
            if name not in compact:
                compact[name] = text.translate(_WHITESPACE)
            at = compact[name].find(needle)
            while at >= 0 and hits < _SOURCE_HITS:
                hits += 1
                node_id = holder(name, text.count("\n", 0, _offset_of(text, at)) + 1)
                if node_id:
                    holders.append(node_id)
                at = compact[name].find(needle, at + 1)
        return _one_of(holders)

    located = _start_lookup(comparable(selection, markdown=False), find)
    return None if located == AMBIGUOUS else located


def _mentions(text: str, nodes: Sequence[Mapping[str, Any]], segments: Mapping[str, Any]) -> list[str]:
    """The results ``text`` names, in its order: by printed number ("Lemma
    3.2") and by label (``\\ref{lem:x}``, every key of ``\\cref{a,b}``)."""
    by_segment = segments_by_id(segments)
    by_name: dict[str, str] = {}
    by_label: dict[str, str] = {}
    for node in nodes:
        name = printed(node, by_segment.get(node["id"])).lower()
        match = _NAME_RE.fullmatch(name)
        if match:
            by_name.setdefault(f"{match.group(1)} {match.group(2)}", node["id"])
        for label in (node.get("meta") or {}).get("labels") or []:
            if isinstance(label, str):
                by_label.setdefault(label.strip(), node["id"])
    mentions = [(match.start(), by_name.get(f"{match.group(1).lower()} {match.group(2).lower()}"))
                for match in _NAME_RE.finditer(text)]
    mentions += [(match.start(), by_label.get(key.strip()))
                 for match in _REF_RE.finditer(text) for key in match.group(1).split(",")]
    found: list[str] = []
    for _position, node_id in sorted(mentions, key=lambda item: item[0]):
        if node_id and node_id not in found:
            found.append(node_id)
    return found


def related(selection: str, *, view: str, graph: Mapping[str, Any], segments: Mapping[str, Any],
            exclude: str | None = None, sources: Mapping[str, str] | None = None) -> list[str]:
    """The results a selected passage belongs to, then the results it names
    ("Lemma 3.2", or ``\\ref{lem:x}`` by the result's label), at most
    :data:`MAX_RELATED`, without ``exclude``.  A passage of the stored source
    belongs to the result whose statement or proof lines hold its start in
    ``sources`` (the files, :func:`source_texts`); otherwise, and in the
    report, its start is looked for in the statement or proof of a result,
    or in the findings about it as the report prints them (a shorter start
    when the whole one is not found, as for a passage that runs from a
    statement into its proof or past the end of a finding)."""
    selection = str(selection or "")
    if not selection.strip():
        return []
    nodes = [node for node in graph.get("nodes") or [] if node["id"] != exclude]
    by_segment = segments_by_id(segments)
    found: list[str] = []
    markdown = view == "report"
    start = comparable(selection, markdown=markdown)
    if view == "source" and sources:
        located = _in_source(selection, sources=sources, segments=segments, exclude=exclude)
        if located in {node["id"] for node in nodes}:
            found.append(located)
    if not found:
        refs = ref_labels(graph) if markdown else None
        # In the report, what it prints about each result is searched first:
        # a short phrase of one result's finding may also stand in another
        # result's summary, which the report never shows.
        texts = [(lambda node: _printed(node, refs))] if markdown else []
        texts.append(lambda node: _haystack(node, by_segment.get(node["id"]) or {}, view=view))
        for text_of in texts:
            haystacks = {node["id"]: comparable(text_of(node), markdown=markdown) for node in nodes}
            located = _start_lookup(start, lambda needle: _one_of(
                [node_id for node_id, haystack in haystacks.items() if needle in haystack]))
            if located == AMBIGUOUS:
                break  # the passage stands in several results: it belongs to none
            if located:
                found.append(located)
                break
    for node_id in _mentions(selection, nodes, segments):
        if len(found) >= MAX_RELATED:
            break
        if node_id not in found:
            found.append(node_id)
    return found[:MAX_RELATED]


def named(question: str, *, graph: Mapping[str, Any], segments: Mapping[str, Any],
          exclude: Sequence[str] = ()) -> list[str]:
    """The results a typed question names ("Why is Lemma 2.3 a gap?",
    ``\\ref{lem:x}``), or, when it names none but asks about "the main
    theorem" (or result), the analysis's main results; at most
    :data:`MAX_RELATED`, without ``exclude``."""
    nodes = [node for node in graph.get("nodes") or [] if node["id"] not in exclude]
    found = _mentions(str(question or ""), nodes, segments)
    if not found and _MAIN_RE.search(str(question or "")):
        ids = {node["id"] for node in nodes}
        found = [node_id for node_id in graph.get("main_results") or [] if node_id in ids]
    return found[:MAX_RELATED]
