"""Re-check one result of a finished analysis.

The console runs this in a subprocess with the run's own route environment
(``python -u agent_monitor/runners/analyze_runner.py --recheck <node_id>`` in
the run workspace).  It re-runs the verifier on that result only (at most
eight calls and 15 minutes), stores ``analysis/nodes/<id>.json``, rebuilds the
graph, the roll-up, the report and the projection, and appends the re-check
to the receipt.  Other results keep their verdicts.  A re-check that yields
no verdict (provider failure, empty or uninterpretable answer, time limit)
keeps the previous result, logs the attempt and its calls in the receipt and
fails; a stopped re-check changes nothing.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_monitor.analyze import graph as analysis_graph
from agent_monitor.analyze import request as analysis_request


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _snapshot(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _restore(path: Path, saved: bytes | None, write: Callable[[Path, bytes], None]) -> None:
    if saved is not None:
        write(path, saved)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def previous_results(analysis: Path, segments: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Stored per-node results, plus the reasons of results never checked."""
    ids = {node["id"] for node in segments.get("nodes") or []}
    results: dict[str, dict[str, Any]] = {}
    folder = analysis / "nodes"
    if folder.is_dir() and not folder.is_symlink():
        for path in sorted(folder.glob("*.json")):
            value = _load_json(path)
            if value and value.get("id") in ids and path.stem == value["id"]:
                results[value["id"]] = value
    old = analysis_graph.load(analysis.parent)
    for node in (old or {}).get("nodes") or []:
        meta = node.get("meta") or {}
        if node["id"] in ids and node["id"] not in results and meta.get("reason") and meta.get("verdict") == "not_checked":
            results[node["id"]] = {"id": node["id"], "verdict": "not_checked", "reason": meta["reason"]}
    return results


def run(
    workspace: Path,
    node_id: str,
    *,
    route_env: Mapping[str, str],
    sink: Callable[[dict[str, Any]], None] | None = None,
    fetcher: Any = None,
    process_env: bool = False,
) -> dict[str, Any]:
    """Re-check ``node_id``; returns ``{ok, node_id, verdict, closure,
    main_closure, calls, reason}``.  Raises ``analyze_runner.AnalyzeError``
    (or its ``ConfigError`` and ``Stopped``) with a public-safe message."""
    from agent_monitor.runners import analyze_runner as runner
    from agent_monitor.runners import math_harness_runner as harness

    root = Path(workspace).resolve()
    analysis = root / "analysis"
    try:
        req = analysis_request.load(root)
    except analysis_request.RequestError as exc:
        raise runner.ConfigError(str(exc)) from None
    segments = _load_json(analysis / "segments.json")
    manuscript = _load_json(analysis / "manuscript.json")
    receipt = _load_json(analysis / "receipt.json")
    if not segments or not manuscript or not receipt:
        raise runner.AnalyzeError("This run has no finished analysis to re-check")
    node = next((item for item in segments.get("nodes") or [] if item.get("id") == node_id), None)
    if node is None or not runner._checkable(node):
        raise runner.AnalyzeError("That result cannot be re-checked (it is not a result with a proof)")
    env = dict(route_env)
    if process_env:
        probe = dict(env)
        runner.configure_route(probe, req)
        if probe.get("NO_PROXY") == "*":
            for name in runner._DORMANT_ENV:
                os.environ.pop(name, None)
            os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    check = runner.AnalysisRun(root, req, env=env, sink=sink, fetcher=fetcher,
                               caps_override={"max_calls": runner.RECHECK_MAX_CALLS,
                                              "max_seconds": runner.RECHECK_SECONDS, "max_nodes": 1},
                               cancel=threading.Event())
    check.paper_map = dict(receipt.get("paper_map") or {})
    node_file = analysis / "nodes" / f"{node_id}.json"
    failure = ""
    token = harness._RUN_EMITTER.set(check.emitter)
    try:
        with harness._stop_on_sigterm(check.cancel), runner.analysis_lock(analysis):
            results = previous_results(analysis, segments)
            saved = _snapshot(node_file)
            try:
                fresh = check.check(segments, only=node_id).get(node_id)
            except harness.RunCancelled:
                _restore(node_file, saved, runner._write_bytes)
                raise runner.Stopped("The re-check was stopped; nothing changed") from None
            if fresh is None or (fresh.get("verdict") == "not_checked" and check.cancel.is_set()):
                _restore(node_file, saved, runner._write_bytes)
                raise runner.Stopped("The re-check was stopped; nothing changed")
            if fresh.get("verdict") == "not_checked":
                failure = str(fresh.get("reason") or "").removeprefix("Not checked: ") or "no verdict"
            elif fresh.get("interpreted") is False:
                failure = "the verifier output could not be interpreted"
            if failure:
                # Keep the previous result; the receipt still logs the calls.
                _restore(node_file, saved, runner._write_bytes)
            else:
                results[node_id] = fresh
            graph = check.rollup(segments, manuscript, results)
            entry = {"node_id": node_id, "at": runner._utc_now(), "calls": check.ctx.calls_used,
                     "verdict": fresh.get("verdict"), "model": check.info.route.model, "route": check.info.name}
            if failure:
                entry["failed"] = failure[:300]
            check.publish(graph, segments, manuscript, base_receipt=receipt, recheck=entry)
    finally:
        check.emitter.close()
        harness._RUN_EMITTER.reset(token)
    if failure:
        raise runner.AnalyzeError(f"The re-check failed: {failure}. The previous verdict is kept.")
    node_meta = next((item["meta"] for item in graph["nodes"] if item["id"] == node_id), {})
    return {
        "ok": True,
        "node_id": node_id,
        "verdict": node_meta.get("verdict"),
        "closure": node_meta.get("closure"),
        "main_closure": graph.get("main_closure"),
        "calls": check.ctx.calls_used,
        "reason": node_meta.get("reason") or "",
    }
