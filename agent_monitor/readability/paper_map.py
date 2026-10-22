"""The paper map (``readability/paper_map.json``) and the notation table
(``readability/notation.json``).

Paper map, schema ``ansatze-readability-map-v1``::

    {schema, title, nodes: [{id, kind, label ("Lemma 3.2"), title, statement
     (at most 1,200 characters), section, importance (1-5), role, main, part}],
     edges: [{from (the result used), to (the result using it),
              kind: "proof" | "statement", provenance: "ref" | "inferred" | "model"}],
     macros}

Results and dependencies come from the segmenter (weak document-order guesses
are left out); the guide adds dependencies the parser missed (provenance
``model``, never one that would close a cycle), each result's role and its
importance.  Without a guide, importance is structural: 5 for a main result,
4 for a result a main result uses directly, 3 for one it uses through
others, 2 for anything else that is used, 1 otherwise.

Notation, schema ``ansatze-readability-notation-v1``::

    {schema, rows: [{symbol, meaning, where, id}], terms: [{term, meaning, where}],
     macros_used: [names of the author's macros the text uses]}

Both are model output made safe for the console: bounded, ids checked, URLs
removed (``parse``); the console typesets symbols and statements with
KaTeX.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent_monitor.analyze.dag import Reachability
from agent_monitor.analyze.graph import NODE_ID_RE
from agent_monitor.readability.prompts import printed, section_of

SCHEMA = "ansatze-readability-map-v1"
NOTATION_SCHEMA = "ansatze-readability-notation-v1"
MAP_PATH = ("readability", "paper_map.json")
NOTATION_PATH = ("readability", "notation.json")
MAX_NODES = 256
MAX_EDGES = 4096
MAX_STATEMENT_CHARS = 1_200
MAX_MACROS = 400
EDGE_KINDS = ("proof", "statement")
PROVENANCE = ("ref", "inferred", "model")


def _text(value: object, limit: int) -> str:
    return str(value if isinstance(value, (str, int, float)) else "")[:limit]


def _structural_importance(ids: list[str], edges: list[dict[str, Any]], mains: set[str]) -> dict[str, int]:
    users: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        users[edge["from"]].add(edge["to"])

    def reaches_main(start: str) -> bool:
        seen, stack = set(), list(users[start])
        while stack:
            current = stack.pop()
            if current in mains:
                return True
            if current not in seen:
                seen.add(current)
                stack.extend(users[current])
        return False

    out = {}
    for node_id in ids:
        if node_id in mains:
            out[node_id] = 5
        elif users[node_id] & mains:
            out[node_id] = 4
        elif reaches_main(node_id):
            out[node_id] = 3
        else:
            out[node_id] = 2 if users[node_id] else 1
    return out


def build(
    segments: Mapping[str, Any],
    *,
    title: str,
    parts: Mapping[str, int],
    guide: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The paper map of a segmented manuscript; ``parts`` maps a result to
    the part (1-based) its statement is in."""
    guide = guide or {}
    nodes = [node for node in segments.get("nodes") or [] if NODE_ID_RE.fullmatch(str(node.get("id") or ""))]
    nodes = nodes[:MAX_NODES]
    ids = [node["id"] for node in nodes]
    known = set(ids)
    edges: list[dict[str, Any]] = []
    reach = Reachability()
    seen: set[tuple[str, str]] = set()
    for edge in segments.get("edges") or []:
        used, user = edge.get("from"), edge.get("to")
        if edge.get("weak") or used not in known or user not in known or (used, user) in seen:
            continue
        if not reach.add(used, user):
            continue
        seen.add((used, user))
        edges.append({"from": used, "to": user,
                      "kind": "proof" if edge.get("context") == "proof" else "statement",
                      "provenance": edge.get("provenance") if edge.get("provenance") in PROVENANCE else "inferred"})
    for edge in guide.get("extra_edges") or []:
        used, user = edge.get("from"), edge.get("to")
        if used in known and user in known and (used, user) not in seen and reach.add(used, user):
            seen.add((used, user))
            edges.append({"from": used, "to": user, "kind": "proof", "provenance": "model"})
    mains = {node["id"] for node in nodes if node.get("main_result")}
    mains |= {entry["id"] for entry in guide.get("main_results") or [] if entry.get("id") in known}
    by_guide = {entry["id"]: entry for entry in guide.get("map") or [] if entry.get("id") in known}
    structural = _structural_importance(ids, edges, mains)
    out_nodes = []
    for node in nodes:
        mapped = by_guide.get(node["id"]) or {}
        out_nodes.append({
            "id": node["id"],
            "kind": _text(node.get("kind") or "result", 40),
            "label": _text(printed(node), 120),
            "title": _text(node.get("title") or "", 200),
            "statement": " ".join(_text(node.get("statement"), MAX_STATEMENT_CHARS * 2).split())[:MAX_STATEMENT_CHARS],
            "section": section_of(node),
            "importance": int(mapped.get("importance") or structural[node["id"]]),
            "role": _text(mapped.get("role") or "", 300),
            "main": node["id"] in mains,
            "part": parts.get(node["id"]),
        })
    return {
        "schema": SCHEMA,
        "title": _text(title, 300),
        "nodes": out_nodes,
        "edges": edges[:MAX_EDGES],
        "macros": dict(list((segments.get("macros") or {}).items())[:MAX_MACROS]),
    }


def notation(guide: Mapping[str, Any] | None, macros: Mapping[str, str], text: str) -> dict[str, Any]:
    """The notation table: the guide's symbols and terms, and which of the
    author's macros the text uses."""
    guide = guide or {}
    used = [name for name in list(macros)[:MAX_MACROS]
            if re.search(re.escape(name) + r"(?![A-Za-z])", text)]
    return {
        "schema": NOTATION_SCHEMA,
        "rows": [dict(row) for row in guide.get("notation") or []],
        "terms": [dict(row) for row in guide.get("terms") or []],
        "macros_used": used,
    }


# ---------------------------------------------------------------------------
# reading them back (console, shares)
# ---------------------------------------------------------------------------


def _load(path: Path, limit: int = 8_000_000) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def sanitize(raw: object) -> dict[str, Any]:
    """A bounded, validated copy of a paper map; raises ``ValueError`` for
    anything that is not one."""
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
        raise ValueError("not a paper map")
    nodes = []
    for node in raw.get("nodes") or []:
        if not isinstance(node, Mapping) or not NODE_ID_RE.fullmatch(str(node.get("id") or "")):
            continue
        importance = node.get("importance")
        part = node.get("part")
        nodes.append({
            "id": node["id"],
            "kind": _text(node.get("kind"), 40),
            "label": _text(node.get("label"), 120),
            "title": _text(node.get("title"), 200),
            "statement": _text(node.get("statement"), MAX_STATEMENT_CHARS),
            "section": _text(node.get("section"), 160),
            "importance": importance if isinstance(importance, int) and 1 <= importance <= 5 else 1,
            "role": _text(node.get("role"), 300),
            "main": node.get("main") is True,
            "part": part if isinstance(part, int) and not isinstance(part, bool) and part > 0 else None,
        })
        if len(nodes) >= MAX_NODES:
            break
    ids = {node["id"] for node in nodes}
    edges = []
    for edge in raw.get("edges") or []:
        if (isinstance(edge, Mapping) and edge.get("from") in ids and edge.get("to") in ids
                and edge.get("from") != edge.get("to")):
            edges.append({"from": edge["from"], "to": edge["to"],
                          "kind": edge.get("kind") if edge.get("kind") in EDGE_KINDS else "proof",
                          "provenance": edge.get("provenance") if edge.get("provenance") in PROVENANCE else "model"})
        if len(edges) >= MAX_EDGES:
            break
    macros = raw.get("macros") if isinstance(raw.get("macros"), Mapping) else {}
    return {
        "schema": SCHEMA,
        "title": _text(raw.get("title"), 300),
        "nodes": nodes,
        "edges": edges,
        "macros": {_text(name, 80): _text(body, 400) for name, body in list(macros.items())[:MAX_MACROS]
                   if isinstance(name, str) and name.startswith("\\")},
    }


def load(workspace: Path) -> dict[str, Any] | None:
    """The run's sanitized paper map, or ``None`` before it is published."""
    raw = _load(Path(workspace).joinpath(*MAP_PATH))
    if raw is None:
        return None
    try:
        return sanitize(raw)
    except ValueError:
        return None


def load_notation(workspace: Path, *, rows: int = 300) -> dict[str, Any] | None:
    """The run's notation table (at most ``rows`` rows), bounded."""
    raw = _load(Path(workspace).joinpath(*NOTATION_PATH), limit=2_000_000)
    return clean_notation(raw, rows=rows)


def clean_notation(raw: object, *, rows: int = 300) -> dict[str, Any] | None:
    """A bounded, validated copy of a notation table (``None`` if it is not one)."""
    if not isinstance(raw, Mapping) or raw.get("schema") != NOTATION_SCHEMA:
        return None

    def records(value: object, fields: Iterable[tuple[str, int]], limit: int) -> list[dict[str, Any]]:
        out = []
        for entry in value if isinstance(value, list) else []:
            if isinstance(entry, Mapping):
                out.append({key: _text(entry.get(key), size) for key, size in fields})
            if len(out) >= limit:
                break
        return out

    table = records(raw.get("rows"), (("symbol", 120), ("meaning", 400), ("where", 160), ("id", 80)), rows)
    for row in table:
        row["id"] = row["id"] if NODE_ID_RE.fullmatch(row["id"]) else None
    return {
        "schema": NOTATION_SCHEMA,
        "rows": table,
        "terms": records(raw.get("terms"), (("term", 120), ("meaning", 400), ("where", 160)), 100),
        "macros_used": [_text(name, 80) for name in (raw.get("macros_used") or [])[:MAX_MACROS]
                        if isinstance(name, str) and name.startswith("\\")],
    }
