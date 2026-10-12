"""The Analyze manuscript graph (``analysis/analysis_graph.json``).

Schema ``ansatze-analyze-graph-v1``::

    {schema, generated_at, parser, dependency_basis: structure|model,
     source: {...source card...},
     nodes: [{id, kind, label, statement, citations,
              meta: {env, number, title, main_result, checkable, external_result,
                     source_ref: {file, lines, proof_lines?, pages?, url?},
                     verdict, interpreted, closure, closure_path, closure_external,
                     closure_assumptions, relies_on_external, issues, external,
                     notes, confidence, checked_by, proof_excerpt, reason,
                     claim_restated, missing_dependencies}}],
     edges: [{from, to, label: "uses", provenance: ref|inferred|model,
              context: statement|proof, basis, strength, weak?}],
     main_results, main_closure, coverage: {checked, total, uninterpretable},
     counts, macros}

``from`` is the result that is used and ``to`` the result that uses it.
Verdicts are a model verifier's judgements, never certificates.  The closure
verdict of a result is the worst verdict over the results it depends on
through ``ref`` and ``inferred`` edges (error > gap > unclear > not_checked >
checked).  Definitions, remarks, examples and other statements without a
verdict of their own are transparent: they pass on the worst closure, the
cited external results and the assumed statements of whatever they use.
Cited external results and assumed statements make a closure conditional.
Weak document-order edges and edges the model proposed are shown but never
change a closure; when every dependency is model-proposed (statements located
by the model, ``dependency_basis: "model"``) no closure is given at all.
``coverage.checked`` counts results with a usable verdict: a reply that could
not be interpreted is shown as ``unclear`` with ``interpreted: false`` and is
counted in ``coverage.uninterpretable`` instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent_monitor.analyze.dag import Reachability
from agent_monitor.analyze.prompts import node_label, source_locator
from agent_monitor.analyze.titles import plain_title

SCHEMA = "ansatze-analyze-graph-v1"
GRAPH_PATH = ("analysis", "analysis_graph.json")
PROJECTION_MODEL = "analyze-graph"
MAX_NODES = 256
MAX_EDGES = 4096
MAX_GRAPH_BYTES = 8_000_000
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,79}$")
VERDICTS = ("checked", "gap", "error", "unclear", "not_checked")
PROVENANCE = ("ref", "inferred", "model")
CONTEXTS = ("statement", "proof")
BASES = ("label-ref", "equation-ref", "prose-proof", "nested-claim", "document-order", "text-reference", "model")
DEPENDENCY_BASES = ("structure", "model")
NO_PROOF_REASON = "No proof was located by the parser"
CLAIM_KINDS = frozenset({"theorem", "lemma", "proposition", "corollary", "claim", "fact", "observation", "step"})
ASSUMED_KINDS = frozenset({"conjecture", "hypothesis", "assumption", "axiom", "condition", "principle"})
_RANK = {"checked": 0, "not_checked": 1, "unclear": 2, "gap": 3, "error": 4}
_PROJECTION_KINDS = {
    "definition": "definition", "lemma": "lemma", "proposition": "lemma", "theorem": "lemma",
    "corollary": "claim", "claim": "claim", "fact": "lemma", "observation": "claim", "step": "step",
    "assumption": "assumption", "hypothesis": "assumption", "axiom": "assumption",
    "conjecture": "assumption", "condition": "assumption", "principle": "assumption",
}
BASIS = ("manuscript structure (labels and references) with model-verifier annotations; "
         "not a certificate")


def _text(value: object, limit: int) -> str:
    return str(value if isinstance(value, (str, int, float)) else "")[:limit]


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# building from segments
# ---------------------------------------------------------------------------


def build(segments: Mapping[str, Any], manuscript: Mapping[str, Any], *, generated_at: str,
          file_base_url: str | None = None) -> dict[str, Any]:
    """The un-annotated graph of a segments document (every verdict ``not_checked``)."""
    nodes = []
    for segment in segments.get("nodes") or []:
        kind = str(segment.get("kind") or "step")
        checkable = (kind in CLAIM_KINDS and bool(segment.get("has_proof")) and not segment.get("external"))
        source_ref: dict[str, Any] = {"file": segment.get("file"), "lines": segment.get("lines")}
        if segment.get("proof_lines"):
            source_ref["proof_lines"] = segment["proof_lines"]
            if segment.get("proof_file") and segment.get("proof_file") != segment.get("file"):
                source_ref["proof_file"] = segment["proof_file"]
        if segment.get("pages"):
            source_ref["pages"] = segment["pages"]
        if file_base_url and segment.get("file") and segment.get("lines"):
            end = (segment.get("proof_lines") or segment["lines"])[1] if not source_ref.get("proof_file") else segment["lines"][1]
            source_ref["url"] = f"{file_base_url.rstrip('/')}/{segment['file']}#L{segment['lines'][0]}-L{end}"
        reason = ""
        if not checkable:
            if segment.get("external"):
                reason = "Cited result (stated in the manuscript, proved elsewhere)"
            elif kind in CLAIM_KINDS:
                # The parser may have missed a proof written in prose: never
                # claim that someone's manuscript has none.
                reason = NO_PROOF_REASON
            else:
                reason = f"A {kind}, not a claim with a proof"
        nodes.append({
            "id": segment["id"],
            "kind": kind,
            "label": node_label(segment),
            "statement": str(segment.get("statement") or "")[:8000],
            "citations": [cite["key"] for cite in segment.get("cites") or [] if cite.get("key")][:12],
            "meta": {
                "env": segment.get("env"),
                "number": segment.get("number"),
                "title": segment.get("title"),
                "main_result": bool(segment.get("main_result")),
                "checkable": checkable,
                "external_result": bool(segment.get("external")),
                "labels": list(segment.get("labels") or [])[:8],
                "section": [section.get("title") for section in segment.get("section") or []][-3:],
                "source_ref": source_ref,
                "locator": source_locator(segment),
                "verdict": "not_checked",
                "interpreted": True,
                "reason": reason,
                "proof_excerpt": str(segment.get("proof") or "")[:4000],
                "issues": [],
                "external": [],
                "relies_on_external": False,
            },
        })
    edges = [dict(edge) for edge in segments.get("edges") or []]
    graph = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "parser": segments.get("parser"),
        # Statements located by the model carry only model-proposed edges,
        # which never establish a dependency.
        "dependency_basis": "model" if str(segments.get("parser") or "").endswith("+model-segments") else "structure",
        "source": source_card(manuscript),
        "nodes": nodes,
        "edges": edges,
        "macros": dict(list((segments.get("macros") or {}).items())[:400]),
        "math_fidelity": segments.get("math_fidelity"),
    }
    return graph


def source_card(manuscript: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("source_kind", "connector", "input_url", "resolved_url", "canonical_id", "title", "version",
            "license", "format", "root_file", "bundle_sha256", "warnings", "math_fidelity")
    card = {key: manuscript.get(key) for key in keys if manuscript.get(key) not in (None, "", [])}
    upstream = manuscript.get("upstream") or {}
    if upstream:
        card["upstream"] = upstream
    return card


# ---------------------------------------------------------------------------
# annotations, roll-up
# ---------------------------------------------------------------------------


def merge_annotations(graph: dict[str, Any], results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Apply per-node check results (``analysis/nodes/<id>.json``) to the graph
    and add the model's ``depends_on`` as ``model`` edges when they keep the
    graph acyclic and no structural edge exists already."""
    by_id = {node["id"]: node for node in graph["nodes"]}
    reach = Reachability()
    present = set()
    for edge in graph["edges"]:
        reach.add(edge["from"], edge["to"])
        present.add((edge["from"], edge["to"]))
    for node_id in sorted(results):
        node = by_id.get(node_id)
        result = results[node_id]
        if node is None:
            continue
        meta = node["meta"]
        verdict = result.get("verdict")
        meta["verdict"] = verdict if verdict in VERDICTS else "not_checked"
        # A reply that could not be interpreted is recorded as "unclear" but
        # is no judgement: it never counts as checked.
        meta["interpreted"] = result.get("interpreted") is not False
        report = result.get("verification_report") or {}
        issues = [{"type": "error", "location": item.get("location", ""), "issue": item.get("issue", "")}
                  for item in report.get("critical_errors") or []]
        issues += [{"type": "gap", "location": item.get("location", ""), "issue": item.get("issue", "")}
                   for item in report.get("gaps") or []]
        meta["issues"] = issues[:16]
        meta["summary"] = str(report.get("summary") or "")[:2000]
        meta["external"] = list(result.get("external") or [])[:12]
        meta["relies_on_external"] = bool(meta["external"]) or meta.get("external_result", False)
        meta["notes"] = str(result.get("notes") or "")[:1200]
        meta["confidence"] = str(result.get("confidence") or "")
        meta["claim_restated"] = str(result.get("claim") or "")[:4000]
        meta["missing_dependencies"] = list(result.get("missing_dependencies") or [])[:8]
        if result.get("checked_by"):
            meta["checked_by"] = dict(result["checked_by"])
        if result.get("reason"):
            meta["reason"] = str(result["reason"])[:300]
        elif meta["verdict"] != "not_checked":
            meta["reason"] = ""
        if result.get("parts", 1) and int(result.get("parts") or 1) > 1:
            meta["parts"] = int(result["parts"])
        for dependency in result.get("depends_on") or []:
            used = str(dependency.get("id") or "")
            if used not in by_id or used == node_id or (used, node_id) in present:
                continue
            if not reach.add(used, node_id):
                continue
            graph["edges"].append({"from": used, "to": node_id, "label": "uses", "provenance": "model",
                                   "context": "proof", "basis": "model", "strength": 1})
            present.add((used, node_id))
    return graph


def _strong(edge: Mapping[str, Any]) -> bool:
    return edge.get("provenance") in ("ref", "inferred") and not edge.get("weak")


def rollup(graph: dict[str, Any]) -> dict[str, Any]:
    """Closure verdicts, blocking paths, conditional counts, main closure,
    coverage and verdict counts (deterministic)."""
    nodes = graph["nodes"]
    by_id = {node["id"]: node for node in nodes}
    predecessors: dict[str, list[str]] = defaultdict(list)
    successors: dict[str, list[str]] = defaultdict(list)
    for edge in graph["edges"]:
        if _strong(edge) and edge["from"] in by_id and edge["to"] in by_id:
            predecessors[edge["to"]].append(edge["from"])
            successors[edge["from"]].append(edge["to"])
    # Kahn order over strong edges (the graph is acyclic by construction).
    indegree = {node_id: len(set(predecessors[node_id])) for node_id in by_id}
    queue = deque(sorted(node_id for node_id, count in indegree.items() if count == 0))
    order: list[str] = []
    while queue:
        current = queue.popleft()
        order.append(current)
        for following in sorted(set(successors[current])):
            indegree[following] -= 1
            if indegree[following] == 0:
                queue.append(following)
    order += [node_id for node_id in by_id if node_id not in order]
    structural = graph.get("dependency_basis") != "model"
    # The worst closure a node passes on to what uses it: its own closure for
    # a claim; for a transparent node (definition, remark, cited result,
    # assumed statement, ...) the worst closure of what it uses, or None.
    carried: dict[str, str | None] = {}
    externals: dict[str, set[str]] = {}
    assumptions: dict[str, set[str]] = {}
    for node_id in order:
        node = by_id[node_id]
        meta = node["meta"]
        kind = node["kind"]
        external_set = {str(item.get("citation")) for item in meta.get("external") or [] if item.get("citation")}
        if meta.get("external_result"):
            external_set.add(node["label"])
        assumed: set[str] = {node_id} if kind in ASSUMED_KINDS else set()
        inherited: str | None = None
        for used in sorted(set(predecessors[node_id])):
            external_set |= externals.get(used, set())
            assumed |= assumptions.get(used, set())
            used_closure = carried.get(used)
            if used_closure is not None and (inherited is None or _RANK[used_closure] > _RANK[inherited]):
                inherited = used_closure
        externals[node_id] = external_set
        assumptions[node_id] = assumed
        meta["closure_external"] = len(external_set)
        meta["closure_assumptions"] = sorted(assumed)[:24]
        meta["closure_path"] = []
        if kind not in CLAIM_KINDS or meta.get("external_result"):
            carried[node_id] = inherited
            meta["closure"] = None
            continue
        worst = meta.get("verdict") or "not_checked"
        if inherited is not None and _RANK[inherited] > _RANK[worst]:
            worst = inherited
        carried[node_id] = worst
        meta["closure"] = worst if structural else None
        if structural and worst in ("error", "gap"):
            meta["closure_path"] = _blocking_path(node_id, worst, by_id, predecessors)
    mains = [node["id"] for node in nodes if node["meta"].get("main_result")]
    graph["main_results"] = mains
    main_closures = [by_id[node_id]["meta"]["closure"] for node_id in mains if by_id[node_id]["meta"].get("closure")]
    graph["main_closure"] = max(main_closures, key=lambda value: _RANK[value]) if main_closures else None
    checkable = [node for node in nodes if node["meta"].get("checkable")]
    graph["coverage"] = {
        "checked": sum(1 for node in checkable
                       if node["meta"]["verdict"] != "not_checked" and node["meta"].get("interpreted", True)),
        "total": len(checkable),
        "uninterpretable": sum(1 for node in checkable if node["meta"].get("interpreted", True) is False),
    }
    counts = {verdict: 0 for verdict in VERDICTS}
    for node in nodes:
        counts[node["meta"]["verdict"]] += 1
    graph["counts"] = counts
    return graph


def _blocking_path(node_id: str, verdict: str, by_id: Mapping[str, Any],
                   predecessors: Mapping[str, list[str]]) -> list[str]:
    """Shortest chain from the nearest result whose own verdict is ``verdict``
    to ``node_id`` (inclusive), over strong edges and through transparent
    statements (a remark or definition that passes the verdict on)."""
    if by_id[node_id]["meta"].get("verdict") == verdict:
        return [node_id]
    previous: dict[str, str] = {}
    queue = deque([node_id])
    seen = {node_id}
    while queue:
        current = queue.popleft()
        for used in sorted(set(predecessors.get(current, []))):
            if used in seen:
                continue
            seen.add(used)
            previous[used] = current
            if by_id[used]["meta"].get("verdict") == verdict:
                path = [used]
                while path[-1] != node_id:
                    path.append(previous[path[-1]])
                return path[:32]
            queue.append(used)
    return []


def summary(graph: Mapping[str, Any], *, calls: int, model: str, route: str) -> dict[str, Any]:
    """The ``run["analyze"]["summary"]`` block."""
    counts = graph.get("counts") or {}
    return {
        "nodes": len(graph.get("nodes") or []),
        "checked": counts.get("checked", 0),
        "gap": counts.get("gap", 0),
        "error": counts.get("error", 0),
        "unclear": counts.get("unclear", 0),
        "not_checked": counts.get("not_checked", 0),
        "coverage": dict(graph.get("coverage") or {}),
        "main_results": list(graph.get("main_results") or []),
        "main_closure": graph.get("main_closure"),
        "calls": int(calls),
        "model": model,
        "route": route,
    }


# ---------------------------------------------------------------------------
# sanitize, load
# ---------------------------------------------------------------------------


def _sanitize_issue(item: object) -> dict[str, str] | None:
    if not isinstance(item, Mapping):
        return None
    kind = item.get("type") if item.get("type") in ("error", "gap") else None
    issue = _text(item.get("issue"), 1000)
    if not kind or not issue:
        return None
    return {"type": kind, "location": _text(item.get("location"), 200), "issue": issue}


def _ints(value: object, count: int = 2) -> list[int] | None:
    if (isinstance(value, list) and len(value) == count
            and all(isinstance(item, int) and not isinstance(item, bool) and 0 <= item < 10_000_000 for item in value)):
        return list(value)
    return None


def _https(value: object) -> str | None:
    text = _text(value, 500)
    return text if text.startswith("https://") and not re.search(r"[\s\"<>]", text) else None


def sanitize(raw: object) -> dict[str, Any]:
    """A bounded, validated copy of an analysis graph (for files and the API).

    Raises ``ValueError`` for a graph that is not an analysis graph.
    """
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
        raise ValueError("not an analysis graph")
    raw_nodes = raw.get("nodes")
    raw_edges = raw.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("analysis graph lacks nodes or edges")
    nodes = []
    seen: set[str] = set()
    for item in raw_nodes[:MAX_NODES]:
        if not isinstance(item, Mapping):
            continue
        node_id = str(item.get("id") or "")
        if not NODE_ID_RE.fullmatch(node_id) or node_id in seen:
            continue
        seen.add(node_id)
        meta = item.get("meta") if isinstance(item.get("meta"), Mapping) else {}
        source = meta.get("source_ref") if isinstance(meta.get("source_ref"), Mapping) else {}
        clean_source: dict[str, Any] = {"file": _text(source.get("file"), 300) or None}
        for key in ("lines", "proof_lines", "pages"):
            numbers = _ints(source.get(key))
            if numbers:
                clean_source[key] = numbers
        if source.get("proof_file"):
            clean_source["proof_file"] = _text(source.get("proof_file"), 300)
        url = _https(source.get("url"))
        if url:
            clean_source["url"] = url
        checked_by = meta.get("checked_by") if isinstance(meta.get("checked_by"), Mapping) else None
        verdict = meta.get("verdict") if meta.get("verdict") in VERDICTS else "not_checked"
        closure = meta.get("closure") if meta.get("closure") in VERDICTS else None
        clean_meta = {
            "env": _text(meta.get("env"), 60),
            "number": _text(meta.get("number"), 40) or None,
            "title": _text(meta.get("title"), 200) or None,
            "main_result": meta.get("main_result") is True,
            "checkable": meta.get("checkable") is True,
            "external_result": meta.get("external_result") is True,
            "labels": [_text(label, 120) for label in (meta.get("labels") or [])[:8] if isinstance(label, str)],
            "section": [_text(title, 160) for title in (meta.get("section") or [])[:3] if isinstance(title, str)],
            "source_ref": clean_source,
            "locator": _text(meta.get("locator"), 300),
            "verdict": verdict,
            "interpreted": meta.get("interpreted") is not False,
            "closure": closure,
            "closure_path": [step for step in (meta.get("closure_path") or [])[:32]
                             if isinstance(step, str) and NODE_ID_RE.fullmatch(step)],
            "closure_external": meta.get("closure_external") if isinstance(meta.get("closure_external"), int) else 0,
            "closure_assumptions": [step for step in (meta.get("closure_assumptions") or [])[:24]
                                    if isinstance(step, str) and NODE_ID_RE.fullmatch(step)],
            "relies_on_external": meta.get("relies_on_external") is True,
            "issues": [issue for issue in (_sanitize_issue(entry) for entry in (meta.get("issues") or [])[:16]) if issue],
            "external": [
                {"citation": _text(entry.get("citation"), 200),
                 "statement_as_used": _text(entry.get("statement_as_used"), 600),
                 "hypotheses_checked": entry.get("hypotheses_checked")
                 if entry.get("hypotheses_checked") in ("yes", "partly", "no", "unknown") else "unknown"}
                for entry in (meta.get("external") or [])[:12] if isinstance(entry, Mapping)
            ],
            "summary": _text(meta.get("summary"), 2000),
            "notes": _text(meta.get("notes"), 1200),
            "confidence": meta.get("confidence") if meta.get("confidence") in ("high", "medium", "low") else "",
            "reason": _text(meta.get("reason"), 300),
            "proof_excerpt": _text(meta.get("proof_excerpt"), 4000),
            "claim_restated": _text(meta.get("claim_restated"), 4000),
            "missing_dependencies": [
                {"description": _text(entry.get("description"), 400), "suggested_id": _text(entry.get("suggested_id"), 80)}
                for entry in (meta.get("missing_dependencies") or [])[:8] if isinstance(entry, Mapping)
            ],
        }
        if isinstance(meta.get("parts"), int) and 1 < meta["parts"] <= 64:
            clean_meta["parts"] = meta["parts"]
        if checked_by:
            clean_meta["checked_by"] = {key: _text(checked_by.get(key), 120) for key in ("model", "route", "effort")}
        nodes.append({
            "id": node_id,
            "kind": re.sub(r"[^a-z_]", "", _text(item.get("kind"), 40).lower()) or "step",
            "label": _text(item.get("label"), 200) or node_id,
            "statement": _text(item.get("statement"), 8000),
            "citations": [_text(cite, 200) for cite in (item.get("citations") or [])[:12] if isinstance(cite, str)],
            "meta": clean_meta,
        })
    ids = {node["id"] for node in nodes}
    candidates = []
    edge_seen: set[tuple[str, str, str]] = set()
    for item in raw_edges[:MAX_EDGES]:
        if not isinstance(item, Mapping):
            continue
        source, target = str(item.get("from") or ""), str(item.get("to") or "")
        provenance = item.get("provenance") if item.get("provenance") in PROVENANCE else None
        if (source not in ids or target not in ids or source == target or provenance is None
                or (source, target, provenance) in edge_seen):
            continue
        edge_seen.add((source, target, provenance))
        edge = {"from": source, "to": target, "label": "uses", "provenance": provenance,
                "context": item.get("context") if item.get("context") in CONTEXTS else "proof"}
        if item.get("basis") in BASES:
            edge["basis"] = item["basis"]
        if isinstance(item.get("strength"), int) and 0 <= item["strength"] <= 3:
            edge["strength"] = item["strength"]
        if item.get("weak") is True:
            edge["weak"] = True
        candidates.append(edge)
    # Edges are admitted strongest first (references before weak guesses and
    # model proposals), so a cycle in a stored graph never costs a real
    # dependency; the kept edges stay in their stored order.
    reach = Reachability()
    admitted = set()
    for index, edge in sorted(enumerate(candidates),
                              key=lambda pair: (not _strong(pair[1]), -pair[1].get("strength", 0), pair[0])):
        if reach.add(edge["from"], edge["to"]):
            admitted.add(index)
    edges = [edge for index, edge in enumerate(candidates) if index in admitted]
    clean = {
        "schema": SCHEMA,
        "generated_at": _text(raw.get("generated_at"), 40),
        "parser": _text(raw.get("parser"), 80),
        "dependency_basis": "model" if raw.get("dependency_basis") == "model" else "structure",
        "source": _sanitize_source(raw.get("source")),
        "nodes": nodes,
        "edges": edges,
        "main_results": [node_id for node_id in (raw.get("main_results") or []) if node_id in ids][:16],
        "main_closure": raw.get("main_closure") if raw.get("main_closure") in VERDICTS else None,
        "coverage": {key: int((raw.get("coverage") or {}).get(key) or 0)
                     for key in ("checked", "total", "uninterpretable")},
        "counts": {verdict: int((raw.get("counts") or {}).get(verdict) or 0) for verdict in VERDICTS},
        "macros": {_text(key, 60): _text(value, 400) for key, value in list((raw.get("macros") or {}).items())[:400]
                   if isinstance(key, str) and re.fullmatch(r"\\[A-Za-z]+", key)},
        "math_fidelity": "pdf-text" if raw.get("math_fidelity") == "pdf-text" else None,
    }
    return clean


def _sanitize_source(raw: object) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    clean: dict[str, Any] = {}
    for key, limit in (("source_kind", 20), ("connector", 20), ("canonical_id", 300), ("title", 300),
                       ("version", 80), ("license", 200), ("format", 10), ("root_file", 300),
                       ("bundle_sha256", 64), ("math_fidelity", 20)):
        if source.get(key):
            clean[key] = _text(source.get(key), limit)
    if clean.get("title"):
        clean["title_text"] = plain_title(clean["title"], 300)  # the title as typeset, for plain text
    for key in ("input_url", "resolved_url"):
        url = _https(source.get(key))
        if url:
            clean[key] = url
    clean["warnings"] = [_text(item, 400) for item in (source.get("warnings") or [])[:12] if isinstance(item, str)]
    upstream = source.get("upstream")
    if isinstance(upstream, Mapping):
        clean["upstream"] = json.loads(json.dumps(upstream, default=str))  # plain JSON, bounded below
        if len(_canonical(clean["upstream"])) > 20_000:
            clean["upstream"] = {}
    return clean


def load(workspace: Path) -> dict[str, Any] | None:
    """The run's sanitized analysis graph, or ``None`` (no links followed)."""
    path = Path(workspace).resolve().joinpath(*GRAPH_PATH)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_GRAPH_BYTES:
                return None
            raw = handle.read(MAX_GRAPH_BYTES + 1)
        value = json.loads(raw.decode("utf-8"))
        return sanitize(value)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------


def to_proof_graph(graph: Mapping[str, Any]) -> dict[str, Any]:
    """The deterministic ``proof_graph.json`` envelope for Outline, coverage,
    sharing and Lean blueprints (passes ``proof_graph._sanitize`` losslessly)."""
    nodes = []
    for node in graph["nodes"][:MAX_NODES]:
        meta = node["meta"]
        kind = _PROJECTION_KINDS.get(node["kind"], "step")
        if meta.get("external_result"):
            kind = "assumption"
        elif meta.get("main_result") and node["kind"] in CLAIM_KINDS:
            kind = "conclusion"
        nodes.append({
            "id": node["id"],
            "kind": kind,
            "label": node["label"][:120],
            "statement": " ".join(str(node.get("statement") or "").split())[:1200],
            "citations": [str(cite)[:120] for cite in node.get("citations") or []][:6],
        })
    ids = {node["id"] for node in nodes}
    edges = [{"from": edge["from"], "to": edge["to"], "label": "uses"}
             for edge in graph["edges"]
             if _strong(edge) and edge["from"] in ids and edge["to"] in ids][:4096]
    title = plain_title((graph.get("source") or {}).get("title"), 200)
    projected = {"title": title, "nodes": nodes, "edges": edges}
    return {
        "generated_at": graph.get("generated_at"),
        "model": PROJECTION_MODEL,
        "source": "analysis/analysis_graph.json",
        "source_sha256": hashlib.sha256(_canonical(projected)).hexdigest(),
        "kind": "informal",
        "status": "derived",
        "derivation": "deterministic",
        "attested": False,
        "certificate": False,
        "basis": BASIS,
        "graph": projected,
    }


def lean_blueprint(graph: Mapping[str, Any], node_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Facts for ``proof_bridge.guidance_from_facts`` (statement-first Lean):
    the selected results plus the definitions they cite, in dependency order;
    exactly one target (the main result if selected, else the last)."""
    by_id = {node["id"]: node for node in graph["nodes"]}
    selected = [node_id for node_id in dict.fromkeys(node_ids) if node_id in by_id][:24]
    if not selected:
        return []
    predecessors: dict[str, list[str]] = defaultdict(list)
    for edge in graph["edges"]:
        if _strong(edge):
            predecessors[edge["to"]].append(edge["from"])
    chosen = set(selected)
    for node_id in selected:
        for used in predecessors[node_id]:
            if by_id[used]["kind"] == "definition":
                chosen.add(used)
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(node_id: str) -> None:
        if state.get(node_id) or node_id not in chosen:
            return
        state[node_id] = 1
        for used in sorted(set(predecessors[node_id])):
            visit(used)
        state[node_id] = 2
        order.append(node_id)

    for node_id in [identifier for identifier in by_id if identifier in chosen]:
        visit(node_id)
    mains = [node_id for node_id in order if by_id[node_id]["meta"].get("main_result")]
    target = mains[-1] if mains else next((node_id for node_id in reversed(order) if node_id in selected), order[-1])
    facts = []
    for node_id in order[:40]:
        node = by_id[node_id]
        statement = node["meta"].get("claim_restated") or node.get("statement") or ""
        facts.append({
            "fact_id": node_id,
            "statement": " ".join(str(statement).split())[:4000],
            "predecessors": [used for used in sorted(set(predecessors[node_id])) if used in chosen],
            "proof": " ".join(str(node["meta"].get("proof_excerpt") or "").split())[:1500],
            "is_target": node_id == target,
            "label": node["label"],
        })
    return facts
