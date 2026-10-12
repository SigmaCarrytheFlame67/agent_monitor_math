"""Role -> allowed-tools table — the permission skeleton of the whole system.

This is the single, first-class source of truth for separation of duties: which
MCP tools each agent role may even see. It is data (easy to audit / change), not
logic scattered across the server.

Invariants (load-bearing — see ARCHITECTURE.md §3):
  - ``main`` has NO ``fact_submit``: the orchestrator does no math and can never
    fabricate a fact.
  - ``verifier`` is read-only: only ``search_arxiv_theorems`` (it reads the fact
    graph as files, writes nothing).
  - ``worker`` is the only role that can ``fact_submit`` (verifier-gated write).
  - ``curator`` is annotation-only: it may score/status exploring-graph nodes
    (``graph_annotate``) but can neither publish findings (no ``gm_add``), nor
    declare edges (no ``graph_link``), nor touch facts. Workers assert, the
    verifier gates truth, the curator ranks the frontier, main commands.
``worker``, ``main`` and ``curator`` get ``fact_search`` / ``fact_neighbors``
(read views over verified facts — lexical hits, and one graph hop out);
``worker`` alone declares typed exploring-graph edges (``graph_link``);
``graph_frontier`` (the ranked to-explore view) is readable by worker, main,
and curator.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# All tools the gateway can expose (names must match the functions registered in
# server.py). Kept here so the role table and the implementation can't drift.
ALL_TOOLS: Tuple[str, ...] = (
    "gm_add",
    "gm_search",
    "gm_get",
    "fact_submit",
    "fact_search",
    "fact_neighbors",
    "fact_revoke",
    "search_arxiv_theorems",
    "graph_link",
    "graph_annotate",
    "graph_frontier",
    "lesson_add",
    "memory_recall",
)

ROLE_TOOLS: Dict[str, Tuple[str, ...]] = {
    "worker": ("gm_add", "gm_search", "gm_get", "fact_submit", "fact_search", "fact_neighbors",
               "search_arxiv_theorems", "graph_link", "graph_frontier", "memory_recall"),
    "main": ("gm_add", "gm_search", "gm_get", "fact_search", "fact_neighbors", "fact_revoke",
             "search_arxiv_theorems", "graph_frontier", "memory_recall"),
    "verifier": ("search_arxiv_theorems",),
    "curator": ("gm_search", "gm_get", "fact_search", "fact_neighbors", "graph_annotate",
                "graph_frontier", "lesson_add"),
    "all": ALL_TOOLS,
}


def tools_for(role: str) -> List[str]:
    """The tool names a given role may use. An unknown / misconfigured role falls
    back to the most-restrictive read-only set (``verifier``), so a typo can never
    grant write access (fail-closed); the full set requires the explicit
    ``"all"`` key (dev use)."""
    return list(ROLE_TOOLS.get(role, ROLE_TOOLS["verifier"]))
