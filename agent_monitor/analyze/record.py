"""The ``run["analyze"]`` block of a run record, built from the workspace.

``jobs.py`` stores this on the run record after the runner exits (and after a
re-check), so the console never has to read the artifacts itself::

    {"schema": "ansatze-analyze-v1",
     "source": {kind, input_url, resolved_url, canonical_id, title, version,
                license, format, warnings, bundle_sha256},
     "caps": {max_nodes, max_calls}, "focus": "...",
     "summary": {nodes, checked, gap, error, unclear, not_checked, coverage,
                 main_results, main_closure, calls, model, route}}

Never ``run["analysis"]``: that key belongs to the Monitor block.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_monitor.analyze import graph as analysis_graph
from agent_monitor.analyze import request as analysis_request

SCHEMA = "ansatze-analyze-v1"


def _load(path: Path, limit: int = 8 * 1024 * 1024) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def run_block(workspace: Path) -> dict[str, Any] | None:
    """The record block, or ``None`` before an analysis request exists.

    ``summary`` is present only once the analysis was published.
    """
    root = Path(workspace)
    try:
        req = analysis_request.load(root)
    except analysis_request.RequestError:
        return None
    block: dict[str, Any] = {
        "schema": SCHEMA,
        "source": {"kind": req["source"]["kind"]},
        "caps": {key: value for key, value in req["caps"].items() if key in ("max_nodes", "max_calls")},
        "focus": req["focus"],
    }
    if req["source"]["kind"] == "url":
        block["source"]["input_url"] = req["source"]["url"]
    manuscript = _load(root / "analysis" / "manuscript.json")
    if manuscript:
        for key in ("input_url", "resolved_url", "canonical_id", "title", "version", "license", "format",
                    "bundle_sha256"):
            if manuscript.get(key):
                block["source"][key] = manuscript[key]
        block["source"]["warnings"] = list(manuscript.get("warnings") or [])[:12]
        if manuscript.get("math_fidelity"):
            block["source"]["math_fidelity"] = manuscript["math_fidelity"]
    receipt = _load(root / "analysis" / "receipt.json")
    graph = analysis_graph.load(root)
    if receipt and graph:
        caps = receipt.get("caps") or {}
        block["caps"] = {key: caps[key] for key in ("max_nodes", "max_calls") if isinstance(caps.get(key), int)}
        block["summary"] = analysis_graph.summary(
            graph, calls=int(receipt.get("calls") or 0), model=str(receipt.get("model") or ""),
            route=str(receipt.get("route") or ""))
        block["stopped"] = bool(receipt.get("stopped"))
    return block
