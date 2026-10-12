"""Analyze mode: check an existing manuscript into a verdict-annotated DAG.

Pipeline (``agent_monitor/runners/analyze_runner.py``): Ingest (``sources``,
``archives``) -> Segment (``latex_structure`` / ``text_structure``) -> Check
(``prompts``, ``verdicts``) -> Roll-up (``graph``) -> Report (``report``).
Every verdict is a model verifier's judgement, never a certificate.

The submodules load lazily so that the sandboxed parser subprocess
(``python -m agent_monitor.analyze.latex_structure``) imports only what it
needs.  Server-facing API::

    analyze.sources.classify(url)            # no network
    analyze.sources.resolve(url, fetcher=...)  # metadata only
    analyze.graph.load(workspace) / to_proof_graph(graph) / lean_blueprint(graph, ids)
    analyze.report.render(graph, manuscript, receipt)
    analyze.recheck.run(workspace, node_id, route_env=...)
    analyze.record.run_block(workspace)        # the run record's "analyze" block
    analyze.limits.caps_for(role, sponsored=..., env=...)
    analyze.request.write(workspace, ...) / load(workspace)
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

_SUBMODULES = (
    "archives",
    "dag",
    "graph",
    "latex_structure",
    "limits",
    "prompts",
    "recheck",
    "record",
    "report",
    "request",
    "sources",
    "text_structure",
    "verdicts",
)

__all__ = list(_SUBMODULES)


def __getattr__(name: str) -> Any:
    if name in _SUBMODULES:
        module = import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
