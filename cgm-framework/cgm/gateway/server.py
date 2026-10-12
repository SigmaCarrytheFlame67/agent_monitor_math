#!/usr/bin/env python3
"""CGM gateway — the role-gated MCP server.

A thin MCP wrapper over ``cgm.core`` (the truth stores) + one external
integration (``cgm.integrations`` arXiv search). It exposes only the verbs an
LLM can't do reliably itself (content-addressed writes, cascade integrity, the
verifier-gated fact write, BM25) — reads / local memory / novelty judgment are
the agent's own file operations, deliberately NOT tools.

The permission model (which tools each role sees) lives in ``roles.py``. The
``fact_submit`` tool is the ONLY fact-write path: it runs the glossary-coverage
check, calls the verify service, writes the node IFF the verdict is ``correct``,
and ALWAYS traces the verdict to global memory (kind ``verification``) — accept,
reject, or accept-but-write-failed — so a verdict is never stored by nobody (the
verify service is stateless).

Config is read from the environment at CALL time (not import time) so the server
is testable and reconfigurable:
  CGM_PROJECT_DIR   the project dir a worker is pinned to (fallback for main)
  CGM_AGENTS_ROOT   root holding all projects (<root>/<project>); lets main
                      address any project by name via the ``project`` arg
  CGM_AUTHOR        this agent's id, for attribution
  CGM_ROLE          worker | main | verifier | all  (selects exposed tools;
                      unset falls back to the read-only verifier set — fail-closed)
  CGM_VERIFY_URL    verify-service endpoint for fact_submit
  CGM_PROBLEM_ID    problem id stamped on written facts (default: project name)
  CGM_SELF_VERIFY_GATE  ABLATION: replace the independent verifier with the
                      worker's own review (fact_submit takes the verdict in)
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp.server.fastmcp import FastMCP

from cgm.core import ExploringGraph, FactGraph, FactText, GlobalMemory
from cgm.core._util import append_jsonl, utc_now
from cgm.core.factgraph import statement_of
from cgm.core.schema import GLOBAL_KINDS
from cgm.core.exploring_graph import SYMBOLIC_PREFIX as _SYMBOLIC_PREFIX
from cgm.core.exploring_graph import FRONTIER_KINDS as _FRONTIER_KINDS
from cgm.integrations import search as _arxiv_search

from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------- #
# config resolution (env read at call time — testable / reconfigurable)       #
# --------------------------------------------------------------------------- #

def _author() -> str:
    return os.environ.get("CGM_AUTHOR", "unknown")


def _role() -> str:
    # Fail-closed: an UNSET role gets the most-restrictive read-only set, same as
    # a mis-typed one (roles.tools_for). Dev use of the full set is explicit:
    # CGM_ROLE=all.
    return os.environ.get("CGM_ROLE", "verifier")


def _project(project: Optional[str] = None) -> Path:
    """Resolve the project dir to operate on.

    ``project`` (the main agent's per-call selector) wins: it names a project
    under ``CGM_AGENTS_ROOT`` (``<root>/<project>``), so one session can touch
    several projects. With no ``project`` we fall back to ``CGM_PROJECT_DIR``
    (a worker is always pinned this way). The name is validated to a single path
    segment — no ``/`` or ``..`` — so it can never escape the agents root."""
    agents_root = os.environ.get("CGM_AGENTS_ROOT", "")
    project_dir = os.environ.get("CGM_PROJECT_DIR", "")
    if project:
        if not agents_root:
            raise RuntimeError("CGM_AGENTS_ROOT is not set; cannot resolve a project by name")
        if not _PROJECT_NAME_RE.match(project):
            raise RuntimeError(f"invalid project name: {project!r}")
        pdir = Path(agents_root) / project
        if not pdir.is_dir():
            raise RuntimeError(f"no such project: {project!r} (under {agents_root})")
        return pdir
    if not project_dir:
        raise RuntimeError("CGM_PROJECT_DIR is not set and no project was given")
    return Path(project_dir)


def _gm(project: Optional[str] = None) -> GlobalMemory:
    return GlobalMemory(_project(project))


def _fg(project: Optional[str] = None) -> FactGraph:
    return FactGraph(_project(project))


def _graph(project: Optional[str] = None) -> ExploringGraph:
    return ExploringGraph(_project(project))


def _ft(project: Optional[str] = None) -> FactText:
    return FactText(_project(project))


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false")


def _fact_text_mode() -> bool:
    """ABLATION switch: when CGM_FACT_TEXT_MODE is set, the fact store is the
    plain sequential text file (cgm.core.facttext) instead of the
    content-addressed DAG. Default (unset) keeps the fact graph unchanged."""
    return _truthy_env("CGM_FACT_TEXT_MODE")


def _exploring_disabled() -> bool:
    """ABLATION switch: when CGM_DISABLE_EXPLORING_GRAPH is set, the three
    exploring-graph tools are disabled (they return an error envelope and write
    nothing). Default (unset) keeps the overlay unchanged."""
    return _truthy_env("CGM_DISABLE_EXPLORING_GRAPH")


def _self_verify_gate() -> bool:
    """ABLATION switch: when CGM_SELF_VERIFY_GATE is set, the independent
    cold-start verifier is replaced by the worker's OWN review of its own draft
    — ``fact_submit`` takes the worker-supplied verdict instead of calling the
    verify service. Everything else (glossary check, write path, verification
    trace) is unchanged, so the single variable is *who judges*: an isolated
    cold-start codex, or the same session that wrote the proof. Default (unset)
    keeps the verifier-gated write unchanged."""
    return _truthy_env("CGM_SELF_VERIFY_GATE")


def _gm_kind_of(project: Optional[str], entry_id: str) -> Optional[str]:
    """The global-memory channel a given entry id lives in — i.e. its ``kind`` —
    or None if no channel holds it.

    The file name IS the kind, so locating the file answers both "does this id
    exist" and "what kind of entry is it". A raw substring scan rather than a
    parse: we only ever ask about one id, and this runs on a worker's write
    path."""
    needles = ('"id": "%s"' % entry_id, '"id":"%s"' % entry_id)
    gm_dir = Path(_project(project)) / "global_memory"
    if not gm_dir.is_dir():
        return None
    for path in sorted(gm_dir.glob("*.jsonl")):
        if path.name in ("edges.jsonl", "annotations.jsonl", "_status.jsonl"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(n in text for n in needles):
            return path.stem
    return None


def _node_resolves(project: Optional[str], node_id: str) -> bool:
    """Does an exploring-graph node id name something that actually exists? A
    bare id is either a fact id (a file in ``fact_graph/facts/``) or a
    global-memory entry id (an ``id`` field in one of the ``<kind>.jsonl``
    channels)."""
    return _fg(project).exists(node_id) or _gm_kind_of(project, node_id) is not None


def _unresolved_endpoints(
    project: Optional[str], nodes: List[str]
) -> Tuple[List[str], List[str]]:
    """ADVISORY endpoint check for ``graph_link`` — never blocks, never raises.

    A node id is meant to be a bare fact id, a bare global-memory entry id, or a
    symbolic ``goal:<slug>``. A symbolic goal resolves in no store BY DESIGN —
    naming a goal nobody has written down yet is exactly what it is for — so it
    is never reported. Anything else that resolves nowhere is a silent miss: the
    edge is stored, but no reader can ever join it back to what it was about.

    The common slip is decorating a real id with a namespace (``finding:<hex>``,
    ``fact:<hex>``); that one we can name precisely, because stripping the prefix
    lands on an existing node. Returns ``(unresolved_ids, hints)``."""
    unresolved: List[str] = []
    hints: List[str] = []
    for node_id in nodes:
        node_id = (node_id or "").strip()
        if not node_id or node_id.startswith(_SYMBOLIC_PREFIX):
            continue
        if _node_resolves(project, node_id):
            continue
        unresolved.append(node_id)
        prefix, sep, rest = node_id.partition(":")
        if sep and rest and _node_resolves(project, rest):
            hints.append(
                f"'{node_id}': dropping the '{prefix}:' prefix names an existing node "
                f"('{rest}') — pass node ids bare, without a namespace."
            )
        else:
            hints.append(
                f"'{node_id}': no fact or global-memory entry has this id. Use the bare "
                f"id of an existing node, or '{_SYMBOLIC_PREFIX}<slug>' if you mean a "
                f"symbolic goal that has not been written down yet."
            )
    return unresolved, hints


def _backlink_promotion(source_id: Optional[str], fact_id: Optional[str]) -> Dict[str, Any]:
    """Close the awareness → truth loop once a fact has been written.

    Two writes, both append-only:

    - **global memory** — stamp the originating finding with the ``fact_id`` and
      advance its status (``verified`` for an objectively-checkable kind,
      ``supported`` for a judgment kind, whose lifecycle has no ``verified``).
    - **exploring graph** — the ``promoted-to`` edge from that finding to the
      fact, unless an equivalent edge is already there.

    Why this is code and not prose: ``DATA_MODEL.md`` §4 describes the back-link
    as something the agent does after a promotion, but no tool ever exposed
    ``set_status``, so across 37 real projects not one ``_status.jsonl`` was ever
    written — every finding's status stayed frozen at creation, and the only
    surviving record that an idea became a fact was an edge a worker might or
    might not remember to declare. A promotion is a mechanical consequence of an
    accepted write, so the write path records it.

    Advisory and non-fatal: the fact is already written and accepted, so a bogus
    ``source_id`` is reported rather than raised, and the exploring-graph half is
    skipped when the overlay is ablated off."""
    if not source_id or not fact_id:
        return {}
    kind = _gm_kind_of(None, source_id)
    if kind is None:
        prefix, sep, rest = source_id.partition(":")
        detail = (f" (dropping the '{prefix}:' prefix names an existing entry — pass it bare)"
                  if sep and rest and _gm_kind_of(None, rest) else "")
        return {"backlink_skipped": f"source_id '{source_id}' names no global-memory entry{detail}"}

    out: Dict[str, Any] = {"promoted_from": source_id}
    # Objectively-checkable kinds get `verified`; judgment kinds have no such
    # state in their lifecycle, so the strongest honest one is `supported`.
    _gm().set_status(source_id, "verified" if GLOBAL_KINDS.get(kind) else "supported", fact_id)

    if not _exploring_disabled():
        graph = _graph()
        already = any(
            e.get("dst") == fact_id and e.get("src") == source_id
            for e in graph.edges(node=source_id, edge_type="promoted-to")
        )
        if not already:
            graph.add_edge(
                src=source_id, dst=fact_id, edge_type="promoted-to", author=_author(),
                rationale="recorded automatically on accept by fact_submit",
            )
            out["promoted_to_edge"] = True
    return out


def _verify(statement: str, proof: str) -> Dict[str, Any]:
    """POST {statement, proof} to the verify service; return its JSON."""
    verify_url = os.environ.get("CGM_VERIFY_URL", "")
    if not verify_url:
        raise RuntimeError("CGM_VERIFY_URL is not set (verify service not wired yet)")
    try:
        timeout = int(os.environ.get("CGM_VERIFY_TIMEOUT", "3600"))
    except ValueError:
        timeout = 3600
    data = json.dumps({"statement": statement, "proof": proof}).encode("utf-8")
    req = urllib.request.Request(
        verify_url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted local URL)
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# global memory                                                               #
# --------------------------------------------------------------------------- #

def gm_add(
    kind: str,
    claim: str,
    evidence: str = "",
    verifiable: Optional[bool] = None,
    glossary: Optional[Dict[str, str]] = None,
    links: Optional[Dict[str, Any]] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish a finding to shared global memory (claim + evidence). Verifiable
    kinds (conclusion/example/counterexample/proof_attempt) require explicit
    evidence; judgments (plan/direction/obstacle/report/master_guidance/
    elaboration) do not. Round status notes, hand-offs and "X is now verified
    as <fact>" announcements are kind ``report`` (not a frontier node); a
    ``direction`` is a proposal to explore something. Define your symbols in
    ``glossary`` and reuse project terminology.

    A new ``plan``/``direction`` has no edge yet, so the reply carries
    ``unlinked: true``: declare its place in the exploring graph now
    (``graph_link`` — ``addresses`` a ``goal:<slug>``, or ``refines`` a plan);
    an unlinked node ranks after every linked one on the frontier.

    Main agent: pass ``project`` to target one of several projects by name;
    workers omit it (pinned to their own project)."""
    entry_id = _gm(project).append(
        kind, claim=claim, evidence=evidence, author=_author(),
        verifiable=verifiable, glossary=glossary, links=links,
    )
    out: Dict[str, Any] = {"id": entry_id, "kind": kind}
    if kind in _FRONTIER_KINDS:
        out["unlinked"] = True
        out["hint"] = (f"{kind} {entry_id} has no edge yet — graph_link it now "
                       f"(addresses goal:<slug> / refines <plan id>); unlinked nodes rank "
                       f"last on the frontier and cannot be propagated to or go stale.")
    return out


_GM_CLAIM_PREVIEW_CHARS = 600
_GM_SUMMARY_FIELDS = ("id", "kind", "timestamp_utc", "author", "verifiable",
                      "status", "fact_id", "verdict")


def gm_search(query: str, kinds: Optional[List[str]] = None, limit_per_kind: int = 10,
              project: Optional[str] = None) -> Dict[str, Any]:
    """Search full shared findings with BM25; return compact previews for selection.

    Each hit keeps its score and entry metadata, with at most 600 characters of
    the original claim and a claim_truncated flag. Evidence, glossary, links,
    and reports are omitted. Call gm_get(entry_ids=[...]) for selected entries
    BEFORE relying on their evidence, rejection reasons, or master_guidance.
    Previews are not complete mathematical statements. Main: pass project;
    workers omit it. Ranking and per-kind limits are unchanged.
    """
    result = _gm(project).search(query, kinds=kinds, limit_per_kind=limit_per_kind)
    for group in result["results_by_kind"].values():
        for hit in group["results"]:
            entry = hit["entry"]
            claim = str(entry.get("claim", ""))
            hit["entry"] = {**{key: entry[key] for key in _GM_SUMMARY_FIELDS if key in entry},
                            "claim": claim[:_GM_CLAIM_PREVIEW_CHARS],
                            "claim_truncated": len(claim) > _GM_CLAIM_PREVIEW_CHARS}
    return {**result, "view": "summary"}


def gm_get(entry_ids: List[str], project: Optional[str] = None) -> Dict[str, Any]:
    """Read selected global-memory entries in full after gm_search.

    Batch IDs across kinds in one call. Returns entries in requested order
    (duplicate IDs removed), including evidence, glossary, links, reports, and
    the latest status. Unknown IDs are listed in missing_ids. Main: pass the
    SAME project used for search; workers omit it. Findings remain awareness,
    not verified facts.
    """
    entries = _gm(project).get_many(entry_ids)
    found = {entry["id"] for entry in entries}
    return {"entries": entries,
            "missing_ids": [entry_id for entry_id in dict.fromkeys(entry_ids) if entry_id not in found]}


# --------------------------------------------------------------------------- #
# fact graph                                                                  #
# --------------------------------------------------------------------------- #

def fact_submit(
    statement: str,
    proof: str,
    predecessors: Optional[List[str]] = None,
    glossary_introduces: Optional[Dict[str, str]] = None,
    intuition: str = "",
    source_id: Optional[str] = None,
    external_refs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The only way to write a fact. Runs the glossary-coverage check, calls the
    verifier, and writes the node IFF accepted. On reject, returns repair hints
    and writes nothing. Cite the returned ``fact_id`` in downstream proofs.

    Once a verdict exists, the verification outcome is **always** recorded to
    global memory (kind ``verification``) — accept, reject, or accept-but-write-
    failed — so a verdict is never stored by nobody (the verifier is stateless;
    this worker tool persists it).

    **Pass ``source_id``** — the id of the global-memory finding this fact grew
    out of. On accept it closes the loop for you: the finding is stamped with the
    new ``fact_id`` and advanced out of ``unverified``, and the ``promoted-to``
    edge is declared automatically. Without it, nothing records which idea became
    this fact.

    When your proof cites an external (published) result, pass it in
    ``external_refs`` as a structured entry — e.g.
    ``{"key": "HL26", "authors": ["Han", "Liu"], "title": "...",
    "arxiv": "2603.03817", "year": 2026, "cited_for": "Theorem 1.2"}`` (ground it
    with ``search_arxiv_theorems``). This is captured on the fact so the paper
    pipeline can cite it without re-deriving; it is mutable metadata and does not
    affect the ``fact_id``."""
    fg = _fg()
    gm = _gm()
    problem_id = os.environ.get("CGM_PROBLEM_ID", Path(_project()).name)

    # glossary coverage is advisory — never let a heuristic bug block submission
    try:
        undefined = fg.undefined_symbols(
            statement=statement, proof=proof, intuition=intuition,
            predecessors=predecessors, glossary_introduces=glossary_introduces,
        )
    except Exception:
        undefined = []

    # Avoid paying for verification when the write is already impossible.
    # add() checks again in case a predecessor is revoked during verification.
    try:
        fg.check_predecessors(predecessors)
    except ValueError as e:
        return {"accepted": False, "verdict": "error", "error": str(e),
                "repair_hints": "Re-prove or avoid the revoked predecessor before resubmitting.",
                "undefined_symbols": undefined}

    # 1) Verify. If the verify service errors, no verdict exists yet: return a
    #    clean error so the worker retries. Nothing is lost.
    try:
        result = _verify(statement, proof)
    except Exception as e:
        return {"accepted": False, "verdict": "error", "error": str(e),
                "undefined_symbols": undefined}
    # A successful call that returned a non-dict body (e.g. a bare list) would make
    # the .get() below throw uncaught; treat it as a verify error (clean retry
    # envelope, no verdict to store) rather than leaking a stack trace to the worker.
    if not isinstance(result, dict):
        return {"accepted": False, "verdict": "error",
                "error": f"verify service returned a non-dict body ({type(result).__name__})",
                "undefined_symbols": undefined}
    verdict = result.get("verdict")
    accepted = verdict == "correct"

    # 2) Write the fact iff accepted. Catch write failures (e.g. a revoked
    #    predecessor) so they do NOT skip the trace below.
    fact_id = None
    write_error = None
    if accepted:
        try:
            fact_id = fg.add(
                problem_id=problem_id, author=_author(), statement=statement, proof=proof,
                predecessors=predecessors, glossary_introduces=glossary_introduces,
                intuition=intuition, external_refs=external_refs,
            )
        except Exception as e:
            write_error = str(e)

    # 3) ALWAYS record the verification outcome to global memory once a verdict exists.
    gm.append(
        "verification",
        claim=statement,
        evidence="verdict: correct" if accepted else (result.get("repair_hints") or "verdict: wrong"),
        author=_author(),
        verifiable=False,
        links={"source_id": source_id, "predecessors": predecessors or []},
        verdict=verdict,
        fact_id=fact_id,
        write_error=write_error,
        verification_report=result.get("verification_report"),
    )

    # 3b) Back-link the promoted finding. Advisory: a failure here must never
    #     turn an accepted, already-written fact into an error.
    backlink: Dict[str, Any] = {}
    if accepted and fact_id:
        try:
            backlink = _backlink_promotion(source_id, fact_id)
        except Exception as e:
            backlink = {"backlink_skipped": str(e)}

    # 4) Return.
    if not accepted:
        return {
            "accepted": False,
            "verdict": verdict,
            "repair_hints": result.get("repair_hints"),
            "verification_report": result.get("verification_report"),
            "undefined_symbols": undefined,
        }
    if write_error:
        return {"accepted": True, "fact_id": None, "write_error": write_error,
                "undefined_symbols": undefined}
    return {"accepted": True, "fact_id": fact_id, "undefined_symbols": undefined, **backlink}


# --------------------------------------------------------------------------- #
# self-verify gate (ABLATION variant of the write gate — CGM_SELF_VERIFY_GATE) #
# --------------------------------------------------------------------------- #

_SELF_REVIEW_MIN_CHARS = 200


def fact_submit_selfverify(
    statement: str,
    proof: str,
    self_verdict: str,
    self_review_report: str,
    predecessors: Optional[List[str]] = None,
    glossary_introduces: Optional[Dict[str, str]] = None,
    intuition: str = "",
    source_id: Optional[str] = None,
    external_refs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The only way to write a fact. **You are your own verifier here**: before
    submitting, review your own statement and proof with the verification skills
    (`$verify-sequential-statements` -> `$check-referenced-statements` ->
    `$synthesize-verification-report`) and pass the outcome in.

    ``self_verdict`` must be exactly ``"correct"`` or ``"wrong"``; the rubric is
    the standard one — ``"correct"`` iff the proof has **no critical errors and
    no gaps**. ``self_review_report`` is that review, written out (what you
    checked, step by step, and what you found). The fact is written iff your own
    verdict is ``"correct"``. A ``"wrong"`` verdict writes nothing and is the
    honest outcome when your review found a gap — record it, repair, resubmit.

    Everything else is unchanged: the glossary-coverage check runs, the outcome
    is always traced to global memory (kind ``verification``), passing
    ``source_id`` back-links the originating finding and declares its
    ``promoted-to`` edge on accept, and you cite the returned ``fact_id`` in
    downstream proofs."""
    fg = _fg()
    gm = _gm()
    problem_id = os.environ.get("CGM_PROBLEM_ID", Path(_project()).name)

    # glossary coverage is advisory — never let a heuristic bug block submission
    try:
        undefined = fg.undefined_symbols(
            statement=statement, proof=proof, intuition=intuition,
            predecessors=predecessors, glossary_introduces=glossary_introduces,
        )
    except Exception:
        undefined = []

    # 1) Take the worker's own verdict. Two mechanical preconditions only (they
    #    mirror what the verify service structurally guaranteed: a verdict in the
    #    enum, and a report that exists) — no correctness judgment is made here.
    verdict = (self_verdict or "").strip().lower()
    if verdict not in ("correct", "wrong"):
        return {"accepted": False, "verdict": "error",
                "error": "self_verdict must be exactly 'correct' or 'wrong'",
                "undefined_symbols": undefined}
    report = (self_review_report or "").strip()
    if len(report) < _SELF_REVIEW_MIN_CHARS:
        return {"accepted": False, "verdict": "error",
                "error": (f"self_review_report is missing or too short "
                          f"({len(report)} chars, need >= {_SELF_REVIEW_MIN_CHARS}): "
                          "run the verification skills on your own draft and pass "
                          "the resulting report"),
                "undefined_symbols": undefined}
    accepted = verdict == "correct"

    # 2) Write the fact iff self-accepted. Catch write failures (e.g. a revoked
    #    predecessor) so they do NOT skip the trace below.
    fact_id = None
    write_error = None
    if accepted:
        try:
            fact_id = fg.add(
                problem_id=problem_id, author=_author(), statement=statement, proof=proof,
                predecessors=predecessors, glossary_introduces=glossary_introduces,
                intuition=intuition, external_refs=external_refs,
            )
        except Exception as e:
            write_error = str(e)

    # 3) ALWAYS trace the outcome, flagged as self-reviewed so the run is never
    #    mistaken for a verifier-gated one after the fact.
    gm.append(
        "verification",
        claim=statement,
        evidence="self-verdict: correct" if accepted else "self-verdict: wrong",
        author=_author(),
        verifiable=False,
        links={"source_id": source_id, "predecessors": predecessors or []},
        verdict=verdict,
        fact_id=fact_id,
        write_error=write_error,
        verification_report=report,
        gate="self_verify",
    )

    # 3b) Back-link the promoted finding — same as the verifier-gated path, so
    #     the ablation's only variable stays *who judges*.
    backlink: Dict[str, Any] = {}
    if accepted and fact_id:
        try:
            backlink = _backlink_promotion(source_id, fact_id)
        except Exception as e:
            backlink = {"backlink_skipped": str(e)}

    # 4) Return.
    if not accepted:
        return {"accepted": False, "verdict": verdict, "undefined_symbols": undefined}
    if write_error:
        return {"accepted": True, "fact_id": None, "write_error": write_error,
                "undefined_symbols": undefined}
    return {"accepted": True, "fact_id": fact_id, "undefined_symbols": undefined, **backlink}


def fact_search(query: str, limit: int = 10, project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 search over the verified fact graph (statement + proof + glossary),
    the derived fact index rebuilt on demand from the fact files — the fact graph
    stays the single source of truth. Use it **before proving** to check whether a
    fact like yours already exists, and to find the verified facts that bear on
    your subgoal so you can cite their ``fact_id``. Returns ranked ``{fact_id,
    score, statement}``. Main agent: pass ``project`` to search a specific
    project's graph; workers omit it."""
    return {"query": query, "results": _fg(project).search(query, limit=limit)}


def fact_neighbors(
    fact_id: str,
    include: Optional[List[str]] = None,
    limit: int = 25,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """One hop out from a verified fact — the graph context ``fact_search``
    cannot show. Use it AFTER a search hit (or on any fact id you hold): what
    the fact stands on, how load-bearing it is, and which other facts the
    project has built around the same goals.

    ``include`` selects the hops (default ``["predecessors", "exploring"]``):
    ``predecessors`` — the facts it cites; ``exploring`` — facts within two
    exploring-graph steps, each with the path that connects them (e.g.
    ``—supports→ goal:x ←promoted-to—``: a sibling result on the same goal);
    ``dependents`` — the facts that cite it (opt-in: measured, it adds little
    for finding what to build on, but the COUNT is always returned — a high
    count marks a load-bearing fact). Statements only, never proofs — read a
    fact's file for its proof."""
    fg = _fg(project)
    raw = fg.get_raw(fact_id)
    if raw is None:
        return {"error": f"unknown fact_id: {fact_id}"}
    include = include or ["predecessors", "exploring"]
    nb = fg.neighbors(fact_id)

    def _snip(fid: str) -> Dict[str, Any]:
        return {"fact_id": fid, "statement": statement_of(fg.get_raw(fid) or "")}

    out: Dict[str, Any] = {
        "fact_id": fact_id,
        "statement": statement_of(raw),
        "dependent_count": len(nb["dependents"]),
    }
    budget = max(1, limit)
    if "predecessors" in include:
        out["predecessors"] = [_snip(f) for f in nb["predecessors"][:budget]]
        budget -= len(out["predecessors"])
    if "dependents" in include and budget > 0:
        out["dependents"] = [_snip(f) for f in nb["dependents"][:budget]]
        budget -= len(out["dependents"])
    if "exploring" in include and budget > 0 and not _exploring_disabled():
        hops = _graph(project).fact_hops(fact_id, fg.exists, max_results=budget)
        already = {p["fact_id"] for p in out.get("predecessors", [])}
        out["via_exploring"] = [{**_snip(h["fact_id"]), "path": h["path"]}
                                for h in hops if h["fact_id"] not in already]
    return out


def fact_revoke(fact_id: str, reason: str, project: Optional[str] = None) -> Dict[str, Any]:
    """Cascade-revoke a wrong fact and everything that depends on it. Destructive;
    operator / main-agent only. Main agent: pass ``project`` to target the project
    that owns the fact."""
    revoked = _fg(project).revoke(fact_id, reason=reason)
    return {"revoked": revoked}


# --------------------------------------------------------------------------- #
# fact text (ABLATION variant of the fact store — CGM_FACT_TEXT_MODE)       #
# --------------------------------------------------------------------------- #

def fact_submit_text(
    statement: str,
    proof: str,
    intuition: str = "",
    source_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The only way to record a verified result. Calls the verifier and, IFF the
    verdict is ``correct``, appends the result to the shared plain-text store of
    verified results (``fact_text/facts.md``), numbered in submission order.
    Returns the assigned ``fact_number`` — refer to the result as "Fact N" in
    later writing. On reject, returns repair hints and writes nothing.

    Your proof must be self-contained: when it uses an earlier verified result,
    restate that result where it is used (and mention its number, e.g.
    "by Fact 12: <its statement>"). The verification outcome is **always**
    recorded to global memory (kind ``verification``) once a verdict exists.
    ``source_id`` optionally links to the global-memory finding being promoted."""
    ft = _ft()
    gm = _gm()

    # 1) Verify. If the verify service errors, no verdict exists yet: return a
    #    clean error so the worker retries. Nothing is lost.
    try:
        result = _verify(statement, proof)
    except Exception as e:
        return {"accepted": False, "verdict": "error", "error": str(e)}
    if not isinstance(result, dict):
        return {"accepted": False, "verdict": "error",
                "error": f"verify service returned a non-dict body ({type(result).__name__})"}
    verdict = result.get("verdict")
    accepted = verdict == "correct"

    # 2) Append the result iff accepted.
    fact_number = None
    write_error = None
    if accepted:
        try:
            fact_number = ft.add(author=_author(), statement=statement,
                                 proof=proof, intuition=intuition)
        except Exception as e:
            write_error = str(e)

    # 3) ALWAYS record the verification outcome to global memory once a verdict exists.
    gm.append(
        "verification",
        claim=statement,
        evidence="verdict: correct" if accepted else (result.get("repair_hints") or "verdict: wrong"),
        author=_author(),
        verifiable=False,
        links={"source_id": source_id},
        verdict=verdict,
        fact_number=fact_number,
        write_error=write_error,
        verification_report=result.get("verification_report"),
    )

    # 4) Return.
    if not accepted:
        return {
            "accepted": False,
            "verdict": verdict,
            "repair_hints": result.get("repair_hints"),
            "verification_report": result.get("verification_report"),
        }
    if write_error:
        return {"accepted": True, "fact_number": None, "write_error": write_error}
    return {"accepted": True, "fact_number": fact_number}


def fact_search_text(query: str, limit: int = 10, project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 search over the shared plain-text store of verified results
    (``fact_text/facts.md``), rebuilt on demand. Use it **before proving** to
    check whether a result like yours is already established, and to find the
    verified results that bear on your subgoal. Returns ranked ``{fact_number,
    score, statement}`` — read the full statement/proof from
    ``fact_text/facts.md``. Main agent: pass ``project`` to search a specific
    project's store; workers omit it."""
    return {"query": query, "results": _ft(project).search(query, limit=limit)}


def fact_revoke_text(fact_number: int, reason: str, project: Optional[str] = None) -> Dict[str, Any]:
    """Retract a wrong verified result from the plain-text store: appends a
    retraction note (the entry stays in place; do not build on it). Operator /
    main-agent only. Main agent: pass ``project`` to target the project that
    owns the result."""
    retracted = _ft(project).retract(int(fact_number), author=_author(), reason=reason)
    return {"retracted": retracted}


# --------------------------------------------------------------------------- #
# exploring graph (typed-edge + annotation overlay on the shared stores)      #
# --------------------------------------------------------------------------- #

_EXPLORING_DISABLED_MSG = (
    "exploring-graph disabled for this run (CGM_DISABLE_EXPLORING_GRAPH is set): "
    "no edges/annotations/frontier. Do not retry; proceed without it."
)

def graph_link(
    src: str,
    dst: str,
    type: str,
    rationale: str = "",
    technique: Optional[str] = None,
    and_group: Optional[str] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Declare one typed exploring-graph edge between two nodes. Node ids are
    existing global-memory entry ids, fact ids, or symbolic goal ids
    (``goal:<slug>``). Edge types: decomposes-into (AND groups via
    ``and_group``; tag ``technique`` = localization/invariant/extremal/
    compactness/witness when the group encodes that skill's obligation
    checklist), alternative-of (OR), addresses, supports, refutes, blocks,
    refines, depends-on, promoted-to, supersedes, prioritizes, and the three
    goal-transformation edges reduces-to / equivalent-to / generalizes (record
    their witnessing bridge/equivalence/specialization fact ids in
    ``rationale``). Declare edges WHEN you publish the finding — post-hoc
    archaeology loses the relations.

    Pass node ids BARE (``a1b2c3d4e5f60718``), never namespaced — a decorated id
    like ``fact:a1b2...`` stores fine but no reader can join it back to the node.
    The one prefixed form is ``goal:<slug>``, for a goal not yet written down.
    The reply carries ``unresolved`` + ``hint`` when an endpoint names nothing;
    the edge is still written, so fix it by declaring the corrected edge."""
    if _exploring_disabled():
        return {"error": _EXPLORING_DISABLED_MSG}
    edge_id = _graph(project).add_edge(
        src=src, dst=dst, edge_type=type, author=_author(),
        rationale=rationale, technique=technique, and_group=and_group,
    )
    out: Dict[str, Any] = {"id": edge_id, "src": src, "dst": dst, "type": type}
    # Advisory endpoint check — an edge is never blocked by it, and a bug in it
    # must never break edge writing (same discipline as the glossary check in
    # fact_submit). Skipped in fact-text mode, where facts have no ids at all so
    # every reference to a verified result would be reported.
    if not _fact_text_mode():
        try:
            unresolved, hints = _unresolved_endpoints(project, [src, dst])
            if unresolved:
                out["unresolved"] = unresolved
                out["hint"] = " ".join(hints)
        except Exception:
            pass
    return out


def graph_annotate(
    node_id: str,
    status: Optional[str] = None,
    score: Optional[float] = None,
    comment: str = "",
    evidence_considered: Optional[List[str]] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Curator-only: append a status/score/comment event for one node (latest
    wins on read; append-only, never a mutation). ``status`` in
    open/active/blocked/dead/closed; ``score`` = promising-ness for proving the
    project target, in [0, 1]. The ``comment`` must cite the evidence node ids
    the judgment rests on (list them in ``evidence_considered`` — they power
    staleness detection); scores without cited evidence are vibes, not
    curation."""
    if _exploring_disabled():
        return {"error": _EXPLORING_DISABLED_MSG}
    annot_id = _graph(project).annotate(
        node_id, author=_author(), status=status, score=score,
        comment=comment, evidence_considered=evidence_considered,
    )
    return {"id": annot_id, "node_id": node_id, "status": status, "score": score}


def graph_frontier(
    top_k: int = 20,
    kinds: Optional[List[str]] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """The ranked to-explore frontier: open/active plan and direction nodes —
    plus open/active annotated symbolic goals (kind ``goal``; a goal row with no
    plan addressing it is an unclaimed gap) —
    with the curator's latest score/status/comment folded in, best score first.
    ``stale: true`` marks a node touched by edges newer than its last
    annotation — treat its score as outdated. Use it to pick the next
    direction WITHIN your assigned lane; master_guidance outranks scores when
    they disagree."""
    if _exploring_disabled():
        return {"error": _EXPLORING_DISABLED_MSG, "frontier": []}
    return {"frontier": _graph(project).frontier(top_k=top_k, kinds=kinds)}


# --------------------------------------------------------------------------- #
# cross-project memory (the continuous memory map)                            #
# --------------------------------------------------------------------------- #

_RECALL_METHOD_KINDS = ("dead_end", "obstacle", "counterexample")

_MEMORY_SCOPES = ("off", "batch", "all")


def _memory_scope() -> str:
    """THE switch for cross-project memory — one enum, no companions:

      off    no cross-project memory at all (default; independent trials and
             frozen benchmark runs are both simply this)
      batch  see ONLY projects sharing this project's batch label — the
             learning-curve protocol (problem N sees problems 1..N-1 of its
             own run, nothing historical)
      all    see every other project — research mode

    Fail-closed: unset, empty, or a typo all read as ``off``."""
    v = os.environ.get("CGM_MEMORY_SCOPE", "off").strip().lower()
    return v if v in _MEMORY_SCOPES else "off"


def _batch_of(project_dir: Path) -> Optional[str]:
    try:
        meta = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        return meta.get("batch") or None
    except Exception:
        return None


def memory_recall(
    query: str,
    scope: Optional[List[str]] = None,
    limit: int = 15,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Recall what PREVIOUS projects learned — the continuous memory map.
    Visibility is the operator's ``CGM_MEMORY_SCOPE`` (off | batch | all);
    when off, do not retry.

    Three scopes (default all): ``lessons`` — distilled scoring/methodology
    retrospectives, the highest-signal tier, check these first; ``methodology``
    — other projects' dead ends, obstacles and counterexamples (reusable
    negative knowledge); ``facts`` — other projects' verified STATEMENTS.
    Every result carries provenance (project / kind / timestamp).

    IRON RULE for the ``facts`` scope: a cross-project fact is awareness, NEVER
    a brick. Its id does not exist in this project's store — citing it as a
    predecessor dangles. To build on one, submit its statement here through
    ``fact_submit`` so it passes THIS project's gate.

    Under ``batch`` scope the search set is projects sharing this project's
    batch label (``cgm new <p> --batch <tag>``; lessons filter the same way;
    an unlabelled project sees nothing). Strategy-tier content (guidance /
    elaborations) is never returned: it is the measured leakage channel."""
    mode = _memory_scope()
    if mode == "off":
        return {"error": "cross-project memory is off for this run "
                         "(CGM_MEMORY_SCOPE). Do not retry; proceed with "
                         "this project's own stores."}
    from cgm.core import bm25

    scope = scope or ["lessons", "methodology", "facts"]
    here = Path(_project(project))
    agents_root = Path(os.environ.get("CGM_AGENTS_ROOT") or here.parent)
    my_batch = _batch_of(here)

    others: List[Path] = []
    if agents_root.is_dir():
        for p in sorted(agents_root.iterdir()):
            if not p.is_dir() or p.resolve() == here.resolve():
                continue
            if mode == "batch" and (not my_batch or _batch_of(p) != my_batch):
                continue
            others.append(p)

    def _rank(docs: List[str], payloads: List[Dict[str, Any]], k: int):
        scores = bm25.bm25_scores(query, [bm25.tokenize(d) for d in docs])
        ranked = [{"score": s, **pl} for pl, s in
                  sorted(zip(payloads, scores), key=lambda t: -t[1]) if s > 0]
        return ranked[:k]

    out: Dict[str, Any] = {"query": query, "memory_scope": mode,
                           "projects_searched": [p.name for p in others]}
    per = max(1, limit // max(1, len(scope)))

    if "lessons" in scope:
        allowed = ({p.name for p in others} | {here.name}) if mode == "batch" else None
        docs, pls = [], []
        for rec in _read_lessons():
            if allowed is not None and rec.get("source_project") not in allowed:
                continue
            docs.append(" ".join(str(rec.get(k, "")) for k in
                                 ("pattern", "evidence", "adjustment")))
            pls.append({"provenance": {"project": rec.get("source_project"),
                                       "kind": "lesson",
                                       "timestamp_utc": rec.get("timestamp_utc")},
                        "pattern": rec.get("pattern"),
                        "adjustment": rec.get("adjustment")})
        out["lessons"] = _rank(docs, pls, per)

    if "methodology" in scope:
        docs, pls = [], []
        for pdir in others:
            for kind in _RECALL_METHOD_KINDS:
                f = pdir / "global_memory" / f"{kind}.jsonl"
                if not f.exists():
                    continue
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    docs.append(str(rec.get("claim", "")) + " " + str(rec.get("evidence", ""))[:800])
                    pls.append({"provenance": {"project": pdir.name, "kind": kind,
                                               "timestamp_utc": rec.get("timestamp_utc")},
                                "claim": rec.get("claim"),
                                "evidence": str(rec.get("evidence", ""))[:400]})
        out["methodology"] = _rank(docs, pls, per)

    if "facts" in scope:
        docs, pls = [], []
        for pdir in others:
            fg = FactGraph(pdir)
            for fid in fg.list():
                stmt = statement_of(fg.get_raw(fid) or "")
                if not stmt:
                    continue
                docs.append(stmt)
                pls.append({"provenance": {"project": pdir.name, "kind": "fact",
                                           "fact_id": fid},
                            "statement": stmt,
                            "note": "awareness only — re-verify via fact_submit "
                                    "before building on it"})
        out["facts"] = _rank(docs, pls, per)

    return out


def _read_lessons() -> List[Dict[str, Any]]:
    path = _lessons_path()
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# curator lessons (deployment-level, cross-project scoring retrospectives)    #
# --------------------------------------------------------------------------- #

def _lessons_path() -> Path:
    """Deployment-level lesson store — NOT inside any project. Survives project
    boundaries; gitignored with the rest of runtime/."""
    env = os.environ.get("CGM_LESSONS_FILE")
    if env:
        return Path(env)
    root = os.environ.get("CGM_ROOT") or str(Path(__file__).resolve().parents[2])
    return Path(root) / "runtime" / "curator_lessons.jsonl"


_LESSON_FIELD_MAX = 2000


def lesson_add(pattern: str, evidence: str, adjustment: str,
               project: Optional[str] = None) -> Dict[str, Any]:
    """Record one scoring lesson at project close — the write half of the
    curator's evolution loop. Lessons are DEPLOYMENT-level: they outlive this
    project and are injected into the next curator's launch prompt, amending a
    contract that itself never changes. Write them against the calibration
    exam's worst-calls list, when the main agent asks for your retrospective.

    ``pattern`` — the scoring behaviour, as a falsifiable habit ("I over-score
    endgame assembly plans by ~0.15"), NOT a narrative. ``evidence`` — what in
    this project's outcomes shows it (cite node ids / calibration numbers).
    ``adjustment`` — the concrete rule to apply next time ("cap assembly plans
    at 0.7 until the reassembly member is separately verified").

    HARD RED LINE — methodology only. Lessons cross problems, so a lesson that
    names a theorem, construction, problem object, or any mathematical content
    is a leakage channel (a route summary from one benchmark problem has been
    measured to be worth a whole problem to the next). Describe how you scored,
    never what the mathematics was."""
    fields = {"pattern": pattern, "evidence": evidence, "adjustment": adjustment}
    for name, val in fields.items():
        if not (val or "").strip():
            return {"error": f"'{name}' is required and must be non-empty"}
        if len(val) > _LESSON_FIELD_MAX:
            return {"error": f"'{name}' exceeds {_LESSON_FIELD_MAX} chars — a lesson "
                             "is a distilled rule, not a report"}
    try:
        source = Path(_project(project)).name
    except Exception:
        source = os.environ.get("CGM_PROBLEM_ID", "unknown")
    record = {
        "timestamp_utc": utc_now(),
        "author": _author(),
        "source_project": source,
        "pattern": pattern.strip(),
        "evidence": evidence.strip(),
        "adjustment": adjustment.strip(),
    }
    append_jsonl(_lessons_path(), record)
    return {"recorded": True, "source_project": source, "path": str(_lessons_path())}


# --------------------------------------------------------------------------- #
# arXiv theorem search (external integration)                                 #
# --------------------------------------------------------------------------- #

def search_arxiv_theorems(query: str, num_results: int = 10) -> Dict[str, Any]:
    """Semantic search over arXiv theorem statements (Matlas). Returns
    **verbatim, as-published** theorem / lemma / definition statements — statement
    fidelity matters for math reasoning and citation checking. Phrase the query as
    a *complete mathematical statement* when possible. Returns ranked results,
    each with ``title``, the full ``theorem`` text, ``arxiv_id``, and the in-paper
    ``theorem_id``. External HTTP, no auth; on outage returns an ``error`` and
    empty ``results`` (retry / fall back to built-in web search)."""
    return _arxiv_search(query, num_results=num_results)


# --------------------------------------------------------------------------- #
# role-based registration                                                     #
# --------------------------------------------------------------------------- #

_TOOLS = {
    "gm_add": gm_add,
    "gm_search": gm_search,
    "gm_get": gm_get,
    "fact_submit": fact_submit,
    "fact_search": fact_search,
    "fact_neighbors": fact_neighbors,
    "fact_revoke": fact_revoke,
    "search_arxiv_theorems": search_arxiv_theorems,
    "lesson_add": lesson_add,
    "memory_recall": memory_recall,
    "graph_link": graph_link,
    "graph_annotate": graph_annotate,
    "graph_frontier": graph_frontier,
}


# ABLATION (CGM_FACT_TEXT_MODE): same tool NAMES, text-store implementations —
# role gating is untouched; only the fact-store backend changes.
_TEXT_MODE_TOOLS = {
    "fact_submit": fact_submit_text,
    "fact_search": fact_search_text,
    "fact_revoke": fact_revoke_text,
}


# ABLATION (CGM_SELF_VERIFY_GATE): same tool NAME, the worker's own review
# replaces the independent cold-start verifier. Role gating and the fact store
# are untouched; only who produces the verdict changes.
_SELF_VERIFY_TOOLS = {
    "fact_submit": fact_submit_selfverify,
}


def build_app(role: Optional[str] = None) -> FastMCP:
    """Build the stdio MCP app exposing exactly the tools ``role`` may use.
    ``role`` defaults to ``CGM_ROLE`` (env); unset falls back to the read-only
    verifier set (fail-closed)."""
    app = FastMCP("cgm-core")
    text_mode = _fact_text_mode()
    self_verify = _self_verify_gate()
    for name in tools_for(role if role is not None else _role()):
        fn = None
        if text_mode:
            fn = _TEXT_MODE_TOOLS.get(name)
        elif self_verify:
            fn = _SELF_VERIFY_TOOLS.get(name)
        app.tool(name=name)(fn or _TOOLS[name])
    return app
