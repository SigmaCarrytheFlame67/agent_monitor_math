"""exploring graph — a typed-edge + annotation OVERLAY on the shared stores.

Nodes are NOT stored here: a node id is an existing global-memory entry id, a
fact id (bare hex), or a symbolic goal id (``goal:<slug>``). This module owns
only the two overlay files (both under ``global_memory/``, append-only):

- ``edges.jsonl``       — typed relations between nodes, declared by workers at
                          publish time (the ``graph_link`` tool).
- ``annotations.jsonl`` — the curator's status/score/comment events per node
                          (the ``graph_annotate`` tool). Latest event wins on
                          read (folded), exactly like the global-memory status
                          sidecar.

The graph VIEW (adjacency, frontier ranking, staleness) is derived on demand
from these files plus the existing stores — never persisted, so there is no
second truth and no double-write drift (same discipline as the fact index).

Edge semantics live in prose (the curator contract carries the AND-OR
propagation algebra: closure flows forward along ``reduces-to`` /
``generalizes`` / ``equivalent-to`` only WITH their witnessing facts;
refutation flows backward along ``generalizes`` and both ways along
``equivalent-to``). Code here is pure data-structure I/O, per the repo rule.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._util import append_jsonl, read_jsonl, utc_now
from .global_memory import GlobalMemory

# Typed edges. The three goal-transformation edges (reduces-to / equivalent-to /
# generalizes) are produced by the goal-transformation proving skills; the
# ``technique`` field further tags a decomposes-into edge with the proving
# technique whose obligation checklist the AND-group encodes.
EDGE_TYPES = (
    "decomposes-into",   # plan/goal -> subgoal (AND within one and_group)
    "alternative-of",    # competing approaches for the same goal (OR)
    "addresses",         # approach -> the goal/subgoal it targets
    "supports",          # evidence/fact -> approach/subgoal/claim
    "refutes",           # counterexample -> claim/approach
    "blocks",            # obstacle/dead_end -> approach/subgoal
    "refines",           # direction -> the plan it matured into; v_n -> v_{n+1}
    "depends-on",        # subgoal ordering / prerequisite
    "promoted-to",       # finding -> fact id (awareness -> truth bridge)
    "supersedes",        # guidance_{n+1} -> guidance_n
    "prioritizes",       # guidance -> approach it weights up/down
    "reduces-to",        # goal P -> goal Q (bridge fact Q => P required to close P)
    "equivalent-to",     # goal P <-> goal Q (both-direction facts required)
    "generalizes",       # goal P' -> goal P (specialization fact required)
)

# ``technique`` values a decomposes-into edge may carry (why the AND-group has
# the shape it has; see the corresponding proving-technique skills).
TECHNIQUES = (
    "plain",
    "localization",
    "invariant",
    "extremal",
    "compactness",
    "witness",
)

# Node-id convention. A node id is a bare fact id, a bare global-memory entry
# id, or a SYMBOLIC goal: ``goal:<slug>``. A symbolic id deliberately resolves in
# no store — naming a goal nobody has written down yet is what it is for — so it
# is the one endpoint form that is expected to dangle. ``gateway.graph_link``
# uses this prefix to tell an intentional symbolic node from a mistyped id.
SYMBOLIC_PREFIX = "goal:"

# Curator annotation statuses for a node's exploration lifecycle.
ANNOTATION_STATUSES = ("open", "active", "blocked", "dead", "closed")

# Global-memory kinds whose open entries form the default explore frontier.
FRONTIER_KINDS = ("plan", "direction")

_EDGES_FILE = "edges.jsonl"
_ANNOTATIONS_FILE = "annotations.jsonl"


class ExploringGraph:
    """Rooted at the project directory; overlay files live in global_memory/."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "global_memory"
        self.edges_path = self.dir / _EDGES_FILE
        self.annotations_path = self.dir / _ANNOTATIONS_FILE

    # ------------------------------------------------------------------ write
    def add_edge(
        self,
        *,
        src: str,
        dst: str,
        edge_type: str,
        author: str,
        rationale: str = "",
        technique: Optional[str] = None,
        and_group: Optional[str] = None,
        **extra: Any,
    ) -> str:
        """Append one typed edge; returns its id. ``technique`` is only
        meaningful on ``decomposes-into`` edges; ``and_group`` names the AND
        bundle a decomposition edge belongs to (all members must close for the
        parent to close)."""
        if edge_type not in EDGE_TYPES:
            raise ValueError(f"unknown edge_type '{edge_type}'. Known: {sorted(EDGE_TYPES)}")
        if technique is not None and technique not in TECHNIQUES:
            raise ValueError(f"unknown technique '{technique}'. Known: {sorted(TECHNIQUES)}")
        if not (src or "").strip() or not (dst or "").strip():
            raise ValueError("src and dst node ids are required")
        ts = utc_now()
        edge_id = hashlib.sha256(
            json.dumps([src, dst, edge_type, author, ts], ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        append_jsonl(
            self.edges_path,
            {
                "id": edge_id,
                "timestamp_utc": ts,
                "author": author,
                "src": src,
                "dst": dst,
                "type": edge_type,
                "technique": technique,
                "and_group": and_group,
                "rationale": rationale,
                **extra,
            },
        )
        return edge_id

    def annotate(
        self,
        node_id: str,
        *,
        author: str,
        status: Optional[str] = None,
        score: Optional[float] = None,
        comment: str = "",
        evidence_considered: Optional[List[str]] = None,
        **extra: Any,
    ) -> str:
        """Append one curator annotation event for ``node_id`` (latest wins on
        read). ``score`` is the promising-ness estimate in [0, 1]; ``comment``
        should cite the evidence node ids the judgment rests on."""
        if status is not None and status not in ANNOTATION_STATUSES:
            raise ValueError(
                f"invalid status '{status}'. Valid: {ANNOTATION_STATUSES}"
            )
        if score is not None and not (0.0 <= float(score) <= 1.0):
            raise ValueError("score must be in [0, 1]")
        ts = utc_now()
        annot_id = hashlib.sha256(
            json.dumps([node_id, author, ts], ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        append_jsonl(
            self.annotations_path,
            {
                "id": annot_id,
                "timestamp_utc": ts,
                "author": author,
                "node_id": node_id,
                "status": status,
                "score": None if score is None else float(score),
                "comment": comment,
                "evidence_considered": evidence_considered or [],
                **extra,
            },
        )
        return annot_id

    # ------------------------------------------------------------------- read
    def edges(
        self,
        node: Optional[str] = None,
        edge_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """All edges, optionally filtered to those touching ``node`` (as src or
        dst) and/or of one ``edge_type``."""
        out = []
        for e in read_jsonl(self.edges_path):
            if node is not None and node not in (e.get("src"), e.get("dst")):
                continue
            if edge_type is not None and e.get("type") != edge_type:
                continue
            out.append(e)
        return out

    def latest_annotations(self) -> Dict[str, Dict[str, Any]]:
        """node_id -> its most recent annotation (file is chronological; the
        last event wins, mirroring the global-memory status fold)."""
        latest: Dict[str, Dict[str, Any]] = {}
        for rec in read_jsonl(self.annotations_path):
            if rec.get("node_id"):
                latest[rec["node_id"]] = rec
        return latest

    def fact_hops(self, fact_id: str, is_fact, max_results: int = 50) -> List[Dict[str, Any]]:
        """Facts reachable from ``fact_id`` within two overlay steps, with the
        path that connects them (``is_fact`` decides which node ids are facts).

        One step finds a directly linked fact (e.g. via ``depends-on``); two
        steps finds a fact on the far side of a shared non-fact node — the
        typical shape is  fact —supports→ goal:X ←promoted-to— fact:  two facts
        that bear on the same goal. Measured on the real runs, this hop lifts
        predecessor recall from 0.33 to 0.62 at ~13 returned items, with zero
        circularity (it traverses a different graph than the DAG). Derived on
        demand from ``edges.jsonl``; nothing is persisted."""
        adj: Dict[str, List[Any]] = {}
        for e in read_jsonl(self.edges_path):
            s, d, t = e.get("src"), e.get("dst"), e.get("type")
            if not s or not d:
                continue
            adj.setdefault(s, []).append((d, t, "→"))
            adj.setdefault(d, []).append((s, t, "←"))
        out: List[Dict[str, Any]] = []
        seen = {fact_id}
        for mid, t1, a1 in adj.get(fact_id, []):
            seg1 = f"—{t1}{a1}" if a1 == "→" else f"←{t1}—"
            if is_fact(mid):
                if mid not in seen:
                    seen.add(mid)
                    out.append({"fact_id": mid, "path": seg1})
            else:
                for far, t2, a2 in adj.get(mid, []):
                    if far in seen or not is_fact(far):
                        continue
                    seen.add(far)
                    seg2 = f"—{t2}{a2}" if a2 == "→" else f"←{t2}—"
                    out.append({"fact_id": far, "via": mid,
                                "path": f"{seg1} {mid} {seg2}"})
            if len(out) >= max_results:
                break
        return out[:max_results]

    def frontier(self, top_k: int = 20, kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """The ranked to-explore list: open/active ``plan`` and ``direction``
        entries — plus open/active annotated **symbolic goals** (pseudo-kind
        ``"goal"``) — with the latest curator annotation folded in, best score
        first (unscored nodes rank after scored ones, oldest first). ``stale``
        flags a node touched by an edge newer than its last annotation — the
        score predates evidence the curator has not yet weighed. ``unlinked``
        flags a plan/direction that no edge touches at all: it has no place in
        the graph yet (no goal it addresses, no plan it refines), so it cannot
        be propagated to or go stale — it ranks after every linked node until
        its author declares the edge (``graph_link``).

        Symbolic goals live in no global-memory channel, so before they were
        enumerated here a curator's goal annotations were unreachable through
        any tool. A goal row's ``claim`` is its ``goal:<slug>`` id; one with no
        plan addressing it is an unclaimed gap — often exactly what a worker
        choosing a direction should see."""
        latest = self.latest_annotations()
        newest_edge_ts: Dict[str, str] = {}   # also the set of linked nodes
        for e in read_jsonl(self.edges_path):
            ts = e.get("timestamp_utc", "")
            for n in (e.get("src"), e.get("dst")):
                if n and ts >= newest_edge_ts.get(n, ""):
                    newest_edge_ts[n] = ts

        gm = GlobalMemory(self.root)
        rows: List[Dict[str, Any]] = []
        kinds = kinds if kinds is not None else list(FRONTIER_KINDS) + ["goal"]

        if "goal" in kinds:
            # Symbolic goals exist only as annotated node ids — enumerate the
            # open/active ones from the annotation log itself.
            for node_id, ann in latest.items():
                if not node_id.startswith(SYMBOLIC_PREFIX):
                    continue
                status = ann.get("status") or "open"
                if status not in ("open", "active"):
                    continue
                annotated_at = ann.get("timestamp_utc", "")
                rows.append(
                    {
                        "node_id": node_id,
                        "kind": "goal",
                        "author": ann.get("author"),
                        "claim": node_id,               # the slug is the only text a goal has
                        "status": status,
                        "score": ann.get("score"),
                        "comment": ann.get("comment", ""),
                        "last_annotated": annotated_at or None,
                        "stale": newest_edge_ts.get(node_id, "") > annotated_at,
                        "unlinked": node_id not in newest_edge_ts,
                        "created": annotated_at,
                    }
                )

        for kind in kinds:
            if kind == "goal":
                continue
            for entry in gm.read(kind):
                node_id = entry.get("id")
                ann = latest.get(node_id, {})
                status = ann.get("status") or "open"
                if status not in ("open", "active"):
                    continue
                annotated_at = ann.get("timestamp_utc", "")
                rows.append(
                    {
                        "node_id": node_id,
                        "kind": kind,
                        "author": entry.get("author"),
                        "claim": entry.get("claim", ""),
                        "status": status,
                        "score": ann.get("score"),
                        "comment": ann.get("comment", ""),
                        "last_annotated": annotated_at or None,
                        "stale": newest_edge_ts.get(node_id, "") > annotated_at,
                        "unlinked": node_id not in newest_edge_ts,
                        "created": entry.get("timestamp_utc", ""),
                    }
                )
        rows.sort(
            key=lambda r: (
                r.get("unlinked", False),                 # linked nodes first
                r["score"] is None,                       # scored first
                -(r["score"] or 0.0),                     # best score first
                r["created"],                             # then oldest first (FIFO)
            )
        )
        return rows[:top_k]
