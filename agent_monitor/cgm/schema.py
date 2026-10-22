"""On-disk contracts shared by the CGM pipeline runner and ``engine_graph``.

The fact format and content id follow CGM ``cgm/core/factgraph.py``
(``serialize_fact``) and ``cgm/core/schema.py`` (``compute_fact_id``) exactly,
so fact files stay byte-compatible with the upstream fact graph.  The global
memory kinds, edge types, techniques and annotation statuses mirror
``cgm/core/schema.py`` and ``cgm/core/exploring_graph.py`` (Apache-2.0,
Copyright 2026 FrenzyMath; see ``agent_monitor/cgm/NOTICE.md``).

Identifier scheme (one scheme for every generation of this pipeline):

* fact id: ``sha256(json{problem_id, sorted predecessors, sorted glossary,
  whitespace-normalized statement, whitespace-normalized proof})[:16]`` --
  CGM's ``compute_fact_id``;
* global-memory entry id: ``sha256(json[kind, claim, author, timestamp,
  ordinal])[:16]`` where ``ordinal`` is the 1-based row index inside that
  kind's JSONL file (CGM omits the ordinal because its timestamps are unique
  per write; this pipeline publishes one generation with one timestamp);
* edge id: ``sha256(json[src, dst, type, author, timestamp, ordinal])[:16]``
  with the 1-based row index in ``edges.jsonl``;
* annotation id: ``sha256(json[node_id, author, timestamp])[:16]`` (CGM's
  scheme; each node is annotated at most once per generation);
* symbolic goal nodes: ``goal:root`` and ``goal:p<plan>-s<subgoal>``.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

AUTHOR = "math_harness_cgm"
PIPELINE_SOURCE = "agent-monitor-math-harness-cgm@2"
# Receipt schema 3 (current writer): bounded concurrent workers and
# verification waves, calls in canonical turn order, the resolved
# concurrency in ``workflow.budgets`` and ``workflow.call_order``.  Schema 2
# (the sequential phase-1 writer) stays valid for already-written runs.
RECEIPT_SCHEMA_VERSION = 3
# Max runs (Claude Code advisor + Codex worker) write schema 4: schema 3 plus
# ``workflow.routes``, which binds every call family to one of the two routes.
HYBRID_RECEIPT_SCHEMA_VERSION = 4
CGM_RECEIPT_SCHEMA_VERSIONS = (2, 3, 4)
HYBRID_TRANSPORT = "hybrid"
HYBRID_CONTRACT = {"transport": HYBRID_TRANSPORT, "isolation_version": 1}
HYBRID_EFFORTS = frozenset({"default", "low", "medium", "high", "xhigh", "max"})
SEMANTICS = "model-verifier"
VERIFIER_LABEL = "independent-response-only-model-verifier"
PROOF_RENDERING_VERIFIED = "verified-closure@1"
# The current writer's rendering of an accepted closure: each lemma is named
# by the label its proofs cite it by (``closure.render_verified_closure`` with
# ``labels``).  @1 numbers the lemmas in topological order instead.
PROOF_RENDERING_VERIFIED_V2 = "verified-closure@2"
PROOF_RENDERING_UNVERIFIED = "unverified-candidate@1"
# The renderings an accepted receipt of each schema may name.  Schema 2 (the
# sequential phase-1 writer) only ever wrote @1.
ACCEPTED_PROOF_RENDERINGS = {
    2: frozenset({PROOF_RENDERING_VERIFIED}),
    3: frozenset({PROOF_RENDERING_VERIFIED, PROOF_RENDERING_VERIFIED_V2}),
    4: frozenset({PROOF_RENDERING_VERIFIED, PROOF_RENDERING_VERIFIED_V2}),
}
PIPELINE_NAME = "cgm-strategist-workers-verifier-curator@1"
# Candidates are verified one at a time, solved first; inside a candidate the
# THEOREM closure is verified in its written topological order, in waves of
# at most ``concurrency`` calls for blocks whose predecessors are already
# accepted, and the first block in that order that is not accepted decides
# the outcome (one call at a time: exactly the schema-2 policy).
VERIFICATION_POLICY = "solved-first-topological-waves-stop-on-target@1"
# The policy the current writer records for each receipt schema.
VERIFICATION_POLICIES = {
    2: "solved-first-topological-stop-on-target@1",
    3: VERIFICATION_POLICY,
}
# Every policy a valid receipt of each schema may carry.  Schema 3 also keeps
# the policy of its first, briefly used writer, which verified the closure
# level by level (``Uses`` depth, then written order); its receipts are
# otherwise identical in shape and stay attested.
ACCEPTED_VERIFICATION_POLICIES = {
    2: frozenset({VERIFICATION_POLICIES[2]}),
    3: frozenset({VERIFICATION_POLICY, "solved-first-level-waves-stop-on-target@2"}),
}
ACCEPTED_VERIFICATION_POLICIES[4] = ACCEPTED_VERIFICATION_POLICIES[3]
# Schema-3 ``calls`` are ordered by logical turn (strategist, workers by
# index, verifier/repair turns in wave order, curator), each turn's retries in
# grammar order -- never by completion time (see ``canonical_call_order``).
CALL_ORDER = "canonical-turn-order@1"
MAX_CONCURRENCY = 6
GUEST_MAX_CONCURRENCY = 2
GOAL_ROOT = "goal:root"

# Stage ids match jobs._pipeline_for("math_harness").
STAGE_LABELS = {
    "scaffold": "1 Scaffold",
    "work": "2 Explore",
    "verify": "3 Verify",
    "curate": "4 Curate",
    "finalize": "5 Synthesize",
}
STAGE_ORDER = tuple(STAGE_LABELS)

# cgm/core/schema.py GLOBAL_KINDS: kind -> default ``verifiable``.
GLOBAL_KINDS: dict[str, bool] = {
    "conclusion": True,
    "example": True,
    "counterexample": True,
    "proof_attempt": True,
    "plan": False,
    "dead_end": False,
    "direction": False,
    "obstacle": False,
    "master_guidance": False,
    "verification": False,
    "elaboration": False,
    "report": False,
}

# The subset of kinds this pipeline emits, with the statuses it may assign.
ENTRY_STATUSES: dict[str, frozenset[str]] = {
    "plan": frozenset({"open", "supported"}),
    "proof_attempt": frozenset({"verified", "unverified"}),
    "verification": frozenset({"supported", "challenged", "open"}),
    "dead_end": frozenset({"open"}),
    "obstacle": frozenset({"open"}),
    "counterexample": frozenset({"unverified"}),
    "example": frozenset({"unverified"}),
    "conclusion": frozenset({"unverified"}),
}
FINDING_KINDS = ("dead_end", "obstacle", "counterexample", "example", "conclusion")
REQUIRED_MEMORY_FILES = (
    "plan.jsonl",
    "proof_attempt.jsonl",
    "verification.jsonl",
    "edges.jsonl",
    "annotations.jsonl",
)
OPTIONAL_MEMORY_FILES = tuple(f"{kind}.jsonl" for kind in FINDING_KINDS)
MEMORY_FILES = REQUIRED_MEMORY_FILES + OPTIONAL_MEMORY_FILES
ENTRY_FILES = tuple(
    name for name in MEMORY_FILES if name not in {"edges.jsonl", "annotations.jsonl"}
)

# cgm/core/exploring_graph.py
EDGE_TYPES = (
    "decomposes-into",
    "alternative-of",
    "addresses",
    "supports",
    "refutes",
    "blocks",
    "refines",
    "depends-on",
    "promoted-to",
    "supersedes",
    "prioritizes",
    "reduces-to",
    "equivalent-to",
    "generalizes",
)
TECHNIQUES = ("plain", "localization", "invariant", "extremal", "compactness", "witness")
# Techniques the strategist may assign: CGM TECHNIQUES plus the three goal
# transformations, which map onto reduces-to / equivalent-to / generalizes
# edges and tag their decomposition as "plain".  A plan row's evidence starts
# with ``Technique: <one of these>`` (PLAN_TECHNIQUE_RE).
PLAN_TECHNIQUES = (*TECHNIQUES, "reduction", "reformulation", "generalization")
PLAN_TECHNIQUE_RE = re.compile(
    r"\ATechnique: (?P<technique>" + "|".join(PLAN_TECHNIQUES) + r")(?:\n|\Z)"
)
ANNOTATION_STATUSES = ("open", "active", "blocked", "dead", "closed")

ENTRY_FIELDS = frozenset(
    {
        "id", "timestamp_utc", "author", "kind", "claim", "evidence",
        "verifiable", "status", "fact_id", "links", "glossary",
    }
)
EDGE_FIELDS = frozenset(
    {
        "id", "timestamp_utc", "author", "src", "dst", "type",
        "technique", "and_group", "rationale",
    }
)
ANNOTATION_FIELDS = frozenset(
    {
        "id", "timestamp_utc", "author", "node_id", "status", "score",
        "comment", "evidence_considered",
    }
)

GOAL_ID_RE = re.compile(r"^goal:(?:root|p[1-9][0-9]?-s[1-9][0-9]?)$")
FACT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
# ``<family>[-<index>][-retry][-low]``: ``-retry`` is the one format-reminder
# retry and ``-low`` the one low-effort retry after an empty Kimi reply; the
# empty-reply retry may follow a format retry (``worker-1-retry-low``).
CALL_STAGE_RE = re.compile(
    r"^(?P<family>strategist|worker|verify|repair|curator)"
    r"(?:-(?P<index>[1-9][0-9]{0,2}))?(?P<retry>-retry)?(?P<low>-low)?$"
)
CALL_FAMILIES = ("strategist", "worker", "verify", "repair", "curator")
# Max mode: the advisor route (Claude Code) answers these families and the
# worker route (Codex) the others.
ADVISOR_CALL_FAMILIES = frozenset({"strategist", "verify", "curator"})
WORKER_CALL_FAMILIES = frozenset({"worker", "repair"})
_SPACE_RE = re.compile(r"\s+")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# ``str.splitlines`` (and some JSONL readers) also break on these; json.dumps
# with ensure_ascii=False leaves them raw inside strings.
_LINE_SEPARATOR_ESCAPES = {"\x85": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}


def jsonl_line(row: Mapping[str, Any]) -> str:
    """One JSONL row whose only line break is the terminating ``\\n``."""
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    # These characters can only occur inside JSON strings, where the escape
    # decodes to the identical value.
    for raw, escaped in _LINE_SEPARATOR_ESCAPES.items():
        line = line.replace(raw, escaped)
    return line + "\n"


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def normalize_text(text: str) -> str:
    """Canonical line structure for fact text.

    ``str.splitlines`` treats several Unicode separators as line breaks.  Fact
    readers split on them too, so normalizing up front guarantees that the
    statement/proof bytes that are hashed are the bytes a reader recovers.
    """
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    return "\n".join(lines).strip()


def normalize_for_hash(text: str) -> str:
    return _SPACE_RE.sub(" ", text or "").strip()


def compute_fact_id(
    *,
    problem_id: str,
    predecessors: Sequence[str],
    glossary_introduces: Mapping[str, str],
    statement: str,
    proof: str,
) -> str:
    """CGM ``compute_fact_id``: 16-hex SHA-256 of the canonical content."""
    body = {
        "problem_id": problem_id,
        "predecessors": sorted(predecessors),
        "glossary_introduces": dict(
            sorted((str(key), str(value)) for key, value in glossary_introduces.items())
        ),
        "statement": normalize_for_hash(statement),
        "proof": normalize_for_hash(proof),
    }
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def serialize_fact(
    *,
    fact_id: str,
    problem_id: str,
    author: str,
    predecessors: Sequence[str],
    statement: str,
    proof: str,
    glossary_introduces: Mapping[str, str] | None = None,
) -> str:
    """CGM ``serialize_fact`` (frontmatter + ``## statement`` / ``## proof``)."""
    glossary = dict(glossary_introduces or {})
    lines = [
        "---",
        f"fact_id: {fact_id}",
        f"problem_id: {problem_id}",
        f"author: {author}",
        f"predecessors: [{', '.join(predecessors)}]",
    ]
    if glossary:
        lines.append("glossary_introduces:")
        for key in sorted(glossary):
            lines.append(f"  {key}: {glossary[key]}")
    else:
        lines.append("glossary_introduces: {}")
    lines.append("external_refs: []")
    lines += ["---", "", "## statement", statement.strip(), "", "## proof", proof.strip()]
    lines.append("")
    return "\n".join(lines)


def entry_id(kind: str, claim: str, author: str, timestamp: str, ordinal: int) -> str:
    payload = json.dumps([kind, claim, author, timestamp, ordinal], ensure_ascii=False)
    return sha256_hex(payload.encode("utf-8"))[:16]


def edge_id(
    src: str, dst: str, edge_type: str, author: str, timestamp: str, ordinal: int
) -> str:
    payload = json.dumps([src, dst, edge_type, author, timestamp, ordinal], ensure_ascii=False)
    return sha256_hex(payload.encode("utf-8"))[:16]


def annotation_id(node_id: str, author: str, timestamp: str) -> str:
    payload = json.dumps([node_id, author, timestamp], ensure_ascii=False)
    return sha256_hex(payload.encode("utf-8"))[:16]


def receipt_binding_digest(receipt: Mapping[str, Any]) -> str:
    """Hash every receipt field except the binding itself."""
    body = {key: value for key, value in receipt.items() if key != "binding_sha256"}
    return sha256_hex(canonical_json(body))


def response_only_versions() -> dict[str, int]:
    """Isolation contract versions per transport (see the runner's routes)."""
    return {"codex_exec": 4, "claude_code": 1, "api_chat": 1}


def call_family(stage: object) -> str | None:
    match = CALL_STAGE_RE.fullmatch(str(stage or ""))
    if match is None:
        return None
    family = match.group("family")
    indexed = family in {"worker", "verify", "repair"}
    if indexed != (match.group("index") is not None):
        return None
    return family


def call_turn(stage: object) -> tuple[str, int, int] | None:
    """``(family, index, variant)`` of a call stage; ``None`` if invalid.

    ``index`` is 0 for the unindexed strategist/curator turns; ``variant``
    ranks a turn's calls: 0 base, 1 ``-low``, 2 ``-retry``, 3 ``-retry-low``.
    """
    family = call_family(stage)
    if family is None:
        return None
    match = CALL_STAGE_RE.fullmatch(str(stage))
    assert match is not None
    index = int(match.group("index") or 0)
    variant = (2 if match.group("retry") else 0) + (1 if match.group("low") else 0)
    return family, index, variant


def retry_counts(stages: Sequence[object]) -> dict[str, int]:
    """Retries implied by the call stages: every ``-retry`` call is one format
    reminder and every ``-low`` call one low-effort retry after an empty reply."""
    counts = {"format": 0, "empty": 0}
    for stage in stages:
        turn = call_turn(stage)
        if turn is None:
            continue
        if turn[2] == 2:
            counts["format"] += 1
        if turn[2] in {1, 3}:
            counts["empty"] += 1
    return counts


def canonical_call_order(stages: Sequence[object]) -> bool:
    """Whether call stages follow the schema-3 canonical turn order.

    One strategist turn first; worker turns by strictly increasing index;
    then verifier and repair turns, each family by strictly increasing index
    (interleaved in wave order); at most one curator turn, last.  Inside a
    turn the base call comes first and retries follow in grammar order.
    """
    turns: list[tuple[str, int]] = []
    last_variant = -1
    for stage in stages:
        turn = call_turn(stage)
        if turn is None:
            return False
        family, index, variant = turn
        if turns and turns[-1] == (family, index):
            if variant <= last_variant:
                return False
        else:
            if variant != 0 or (family, index) in turns:
                return False
            turns.append((family, index))
        last_variant = variant
    if not turns or turns[0] != ("strategist", 0):
        return False
    phase = {"strategist": 0, "worker": 1, "verify": 2, "repair": 2, "curator": 3}
    last_index = {"worker": 0, "verify": 0, "repair": 0}
    previous_phase = 0
    for family, index in turns[1:]:
        current = phase[family]
        if current < previous_phase or family == "strategist":
            return False
        if family in last_index:
            if index <= last_index[family]:
                return False
            last_index[family] = index
        previous_phase = current
    curator_turns = [turn for turn in turns if turn[0] == "curator"]
    return len(curator_turns) <= 1 and (not curator_turns or turns[-1][0] == "curator")
