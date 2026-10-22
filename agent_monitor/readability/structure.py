"""The results of a Markdown or text manuscript, as Readability reads them.

Analyze's text segmenter (``analyze.text_structure``) with two additions, run
in its resource-limited child like Analyze's (:func:`segment_sandboxed`):

* A verified fact closure of an Ansatz CGM run (its ``proof.md``) is read by
  its own grammar: the numbered rendering ("## Lemma 2 · fact <16 hex>"), the
  labelled one ("## Lemma L2 · fact <16 hex>") and their "## Main theorem ·
  fact <16 hex>", and the rendering of a candidate the verifier did not accept
  ("## Lemma L2 (verifier-accepted)", "## Lemma L3 (not verifier-accepted)",
  "## Main theorem (not verifier-accepted)").  Each block is one result:
  its statement is the text after **Statement.**, its proof the text after
  **Proof.**, and its **Uses:** line ("Lemma 2 (fact <id>)", "Lemma L2 (fact
  <id>)" or "L1, L2") gives its dependencies.  A closure copied from the
  console reads the same (headings without "#", markers without "**").
  Only a text whose every block keeps that shape, with no result of its own
  before the first block, is read as a closure; anything else is not
  guessed at.
* Otherwise a Markdown heading that names a result ("## Theorem 2.1",
  "### Lemma L2 (Name)", "## Main theorem") opens that result
  (``text_structure.segment_text(heading_results=True)``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from agent_monitor.analyze import latex_structure, text_structure

PARSER_ID = f"{text_structure.PARSER_ID}+cgm-closure"
# A lemma's label in a closure heading: a number ("Lemma 2") or the label
# its proofs cite it by ("Lemma L2").
_LABEL = r"[A-Za-z0-9][A-Za-z0-9_.'-]{0,31}"
_RESULT_HEADING_RE = re.compile(
    r"^(?:#{1,6}[ \t]+)?(?:Lemma[ \t]+(?P<label>" + _LABEL + r")|(?P<main>Main theorem))[ \t]+"
    r"(?:·[ \t]+fact[ \t]+(?P<fact>[0-9a-f]{16})|\((?P<status>verifier-accepted|not verifier-accepted)\))$"
)
# What ends the last block of a candidate's rendering: its findings, or the
# main theorem it could not prove.
_END_HEADING_RE = re.compile(r"^(?:#{1,6}[ \t]+)?(?:Findings \(unverified\)|Main theorem)$")
_USES_RE = re.compile(r"^(?:\*\*)?Uses:(?:\*\*)?[ \t]*(?P<uses>.*)$")
_STATEMENT_RE = re.compile(r"^(?:\*\*)?Statement\.(?:\*\*)?$")
_PROOF_RE = re.compile(r"^(?:\*\*)?Proof\.(?:\*\*)?$")
_CITED_FACT_RE = re.compile(r"\(fact[ \t]+([0-9a-f]{16})\)")
_CITED_LABEL_RE = re.compile(r"^(?:Lemma[ \t]+)?(" + _LABEL + r")$")


class _NotClosure(ValueError):
    """The text does not keep the shape of a fact closure."""


def _heading(line: str) -> dict[str, Any] | None:
    match = _RESULT_HEADING_RE.match(line)
    if not match or (match.group("main") and match.group("status") == "verifier-accepted"):
        return None
    return {"label": match.group("label"), "main": bool(match.group("main")), "fact": match.group("fact"),
            "status": match.group("status")}


def _blocks(lines: list[str]) -> list[dict[str, Any]]:
    """The closure's result blocks, in order (raises :class:`_NotClosure`)."""
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def close(end: int) -> None:
        if current is None:
            return
        if current["statement_at"] is None or current["proof_at"] is None:
            raise _NotClosure("a block has no Statement or no Proof")
        current["end"] = end
        blocks.append(current)

    for index, raw in enumerate(lines):
        line = raw.strip()
        head = _heading(line)
        if head is not None:
            close(index - 1)
            current = dict(head, start=index, uses=None, statement_at=None, proof_at=None)
            continue
        if current is None:
            continue  # the title, the closure's note, or text before it
        if current["proof_at"] is not None:
            if _END_HEADING_RE.match(line):
                close(index - 1)
                current = None
            continue
        if current["statement_at"] is not None:
            if _PROOF_RE.match(line):
                current["proof_at"] = index
            continue
        uses = _USES_RE.match(line)
        if uses and current["uses"] is None:
            current["uses"] = uses.group("uses")
        elif _STATEMENT_RE.match(line):
            current["statement_at"] = index
        elif line:
            raise _NotClosure("text between a heading and its statement")
    close(len(lines) - 1)
    return blocks


def _text(lines: list[str], start: int, end: int) -> tuple[str, int, int]:
    """The text of lines ``[start, end]``, and the first and last of them that
    are not blank (``end < start`` when all are)."""
    while start <= end and not lines[start].strip():
        start += 1
    while end >= start and not lines[end].strip():
        end -= 1
    return "\n".join(lines[start:end + 1]).strip(), start, end


def _cited(uses: str) -> list[str]:
    """The items of a **Uses:** line ("none." is none)."""
    text = uses.strip()
    if text.endswith("."):
        text = text[:-1]
    if text.strip().lower() in ("", "none"):
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def closure_results(text: str, *, file: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]] | None:
    """``(nodes, edges, warnings)`` of a fact closure in segments form, or
    None when ``text`` is not clearly one (see the module docstring)."""
    lines = text.split("\n")
    try:
        blocks = _blocks(lines)
    except _NotClosure:
        return None
    mains = [block for block in blocks if block["main"]]
    labels = [block["label"] for block in blocks if not block["main"]]
    facts = [block["fact"] for block in blocks if block["fact"]]
    if (not blocks or len(mains) > 1 or (mains and mains[0] is not blocks[-1])
            or len(set(labels)) != len(labels) or len(set(facts)) != len(facts)):
        return None
    nodes: list[dict[str, Any]] = []
    taken: set[str] = set()
    for block in blocks:
        statement, first, last = _text(lines, block["statement_at"] + 1, block["proof_at"] - 1)
        proof, proof_first, proof_last = _text(lines, block["proof_at"] + 1, block["end"])
        if not statement or not proof:
            return None
        display = "Main theorem" if block["main"] else "Lemma"
        base = latex_structure.slug(f"{display} {block['label'] or ''}") or "result"
        identifier, suffix = base, 2
        while identifier in taken:
            identifier, suffix = f"{base[:74]}-{suffix}", suffix + 1
        taken.add(identifier)
        nodes.append({
            "id": identifier,
            "kind": "theorem" if block["main"] else "lemma",
            "env": "theorem" if block["main"] else "lemma",
            "display": display,
            "number": block["label"],
            # The candidate's rendering says which blocks the verifier accepted.
            "title": block["status"],
            "labels": [],
            "inner_labels": [],
            "file": file,
            "lines": [block["start"] + 1, last + 1],
            "proof_file": file,
            "proof_lines": [block["proof_at"] + 1, proof_last + 1],
            "section": [],
            "statement": statement[: latex_structure.MAX_STATEMENT_CHARS],
            "statement_chars": len(statement),
            "proof": proof[: latex_structure.MAX_PROOF_CHARS],
            "proof_chars": len(proof),
            "has_proof": True,
            "proof_link": ["closure"],
            "cites": [],
            "nested_in": None,
            "external": False,
            "main_result": block["main"],
        })
        if len(nodes) >= latex_structure.MAX_NODES:
            break
    by_fact = {block["fact"]: node["id"] for block, node in zip(blocks, nodes) if block["fact"]}
    by_label = {block["label"]: node["id"] for block, node in zip(blocks, nodes) if block["label"]}
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    unknown = 0
    for block, node in zip(blocks, nodes):
        for item in _cited(block["uses"] or ""):
            fact = _CITED_FACT_RE.search(item)
            label = _CITED_LABEL_RE.match(item)
            used = by_fact.get(fact.group(1)) if fact else by_label.get(label.group(1)) if label else None
            if used is None:
                unknown += 1
            elif used != node["id"] and (used, node["id"]) not in seen:
                seen.add((used, node["id"]))
                edges.append({"from": used, "to": node["id"], "label": "uses", "provenance": "ref",
                              "context": "proof", "basis": "closure-uses", "strength": 3})
    warnings = [f"{unknown} fact(s) named in Uses lines are not in the text"] if unknown else []
    return nodes, edges, warnings


def segment_text(text: str, *, file: str, fmt: str) -> dict[str, Any]:
    """The segments document of a Markdown or text manuscript (already
    normalized): a fact closure by its own grammar, else Analyze's
    segmentation with result headings."""
    document = text_structure.segment_text(text, file=file, fmt=fmt, heading_results=True)
    found = closure_results(text, file=file)
    if found is None:
        return document
    nodes, edges, warnings = found
    if any(node["lines"][0] < nodes[0]["lines"][0] for node in document["nodes"]):
        return document  # results of its own before the closure: not clearly one
    edges, dropped = text_structure._acyclic(edges)
    if not any(node["main_result"] for node in nodes):
        text_structure._mark_main(nodes, edges)
    return dict(document, parser=PARSER_ID, nodes=nodes, edges=edges, dropped_for_acyclicity=dropped[:50],
                truncated=None, warnings=warnings, stats={
                    "statements": len(nodes), "proofs_attached": len(nodes), "uses_edges": len(edges),
                    "numbered": sum(1 for node in nodes if node["number"])})


def segment_file(path: Path, *, file: str, fmt: str) -> dict[str, Any]:
    text, warnings = text_structure.read_text_file(path, file=file)
    document = segment_text(text, file=file, fmt=fmt)
    document["warnings"] = warnings + document["warnings"]
    return document


def segment_sandboxed(path: Path, out: Path, *, file: str, fmt: str, cancel: Any = None) -> dict[str, Any]:
    """:func:`segment_file` in the resource-limited child Analyze's text
    segmentation runs in (same errors as ``text_structure.segment_sandboxed``)."""
    return text_structure.segment_sandboxed(path, out, file=file, fmt=fmt, cancel=cancel, module=__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Readability text segmentation (sandboxed)")
    parser.add_argument("--segment", nargs=2, metavar=("TEXT", "OUT"), required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--format", default="md", choices=("md", "txt"))
    args = parser.parse_args(argv)
    source, target = Path(args.segment[0]), Path(args.segment[1])
    try:
        document = segment_file(source, file=args.name or source.name, fmt=args.format)
    except (OSError, ValueError, RecursionError) as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 3
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8", errors="replace")
    os.replace(temporary, target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
