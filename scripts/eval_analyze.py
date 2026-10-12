#!/usr/bin/env python3
"""Evaluate Analyze against openai/math ground truth (an operator tool, not a test).

Cases (``--case`` selects some; all by default):

* ``weil-withdrawn`` — "Algebraicity of Weil classes on split abelian
  eightfolds", withdrawn on Oct 6/7, 2026 for a sign error in the
  stabilization-trace argument; analyzed at the archived revision ``adc7f12``.
  Expected: Lemma ``top:negative-doubles`` flagged (error or gap) and the flag
  carried to Proposition ``top:brane`` (``main_closure`` is reported too, but
  Theorem 1.1 has no proof environment and later sections cite ``top:brane``
  only in section prose, so the flag is not expected to reach it).  The README and
  withdrawal notice never reach the verifier (the connector excludes them);
  ``--hint`` adds a focus note naming the lemma, for the with/without-hint
  comparison.
* ``taming-sep23`` — the September 23 edition of "Taming implies
  compatibility on four-manifolds", whose cone-equality claim was corrected on
  Oct 7 (flags are listed for manual comparison with the revision).
* controls ``pi-exponent``, ``kaplansky-char2``, ``thompson-f`` — unchanged
  papers; every error or gap reported on them counts towards the false-positive
  rate (and deserves a manual look).

Modes:

* ``--dry-run`` makes no model call: it ingests and segments each case and
  prints the check plan (statements, checkable results, planned checks under
  the caps, prompt sizes and parts).  With ``--local NAME=DIR`` a case reads a
  local copy of its TeX folder instead of the network, so this mode can run
  fully offline (``DIR`` is packed as an upload).
* Without ``--dry-run`` the analyses run with the model route configured in
  the environment, exactly as the console's runner would (role ``admin``),
  for example an own key (``OPENAI_API_KEY`` + ``AGENT_MONITOR_SELECTED_MODEL``)
  or an account route (``AGENT_MONITOR_CODEX_SUBSCRIPTION=1`` + ``CODEX_HOME``).
  Do not run the full suite on the sponsored Kimi budget.

Results: one line per case on stdout and, with ``--json PATH``, every metric
(verdict counts, expected nodes found, closure propagation, flags on controls,
calls, tokens, wall time).  Nothing secret is printed.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_monitor.analyze import graph as analysis_graph  # noqa: E402
from agent_monitor.analyze import prompts  # noqa: E402
from agent_monitor.analyze import request as analysis_request  # noqa: E402
from agent_monitor.runners import analyze_runner as runner  # noqa: E402

OLD = "adc7f1241b42e322a6451854ab7e4b4c146bf78a"
HEAD = "fd4aeeb2ee4fc729c18d98444fed42fd0529eeeb"
TREE = "https://github.com/openai/math/tree/{sha}/preprints/{folder}"
SUITE: list[dict[str, Any]] = [
    {
        "name": "weil-withdrawn",
        "kind": "withdrawn",
        "url": TREE.format(sha=OLD, folder="Algebraicity-of-Weil-classes-on-split-abelian-eightfolds-September-18-2026"),
        "expect_flagged": ["top:negative-doubles"],
        "expect_closure_flagged": ["top:brane"],
        "hint": "Check Lemma top:negative-doubles (the negative double points) carefully.",
        "note": "sign error in the stabilization-trace argument (withdrawal notice; history.md, Oct 7)",
    },
    {
        "name": "taming-sep23",
        "kind": "revised",
        "url": TREE.format(sha=HEAD, folder="Taming-implies-compatibility-on-four-manifolds-September-23-2026"),
        "note": "cone-equality claim corrected in the Oct 6 edition (history.md, Oct 7)",
    },
    {"name": "pi-exponent", "kind": "control",
     "url": TREE.format(sha=HEAD, folder="The-irrationality-exponent-of-pi-is-2-September-24-2026")},
    {"name": "kaplansky-char2", "kind": "control",
     "url": TREE.format(sha=HEAD, folder="A-Counterexample-to-Kaplanskys-Direct-Finiteness-Conjecture-in-Characteristic-Two-September-23-2026")},
    {"name": "thompson-f", "kind": "control",
     "url": TREE.format(sha=HEAD, folder="Thompsons-group-F-is-nonamenable-September-23-2026")},
]


def _pack(directory: Path) -> bytes:
    """A local TeX folder as a tar.gz upload (regular files only)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.name.lower() != "readme.md":
                archive.add(path, arcname=str(path.relative_to(directory)), recursive=False)
    return buffer.getvalue()


def _workspace(work: Path, case: dict[str, Any], local: Path | None) -> tuple[Path, dict[str, Any]]:
    workspace = work / f"{case['name']}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(parents=True)
    if local is None:
        return workspace, {"kind": "url", "url": case["url"]}
    identifier = uuid.uuid4().hex
    (workspace / "attachments").mkdir(mode=0o700)
    target = workspace / "attachments" / f"{identifier}.tar.gz"
    target.write_bytes(_pack(local))
    (workspace / "attachments.json").write_text(json.dumps([{
        "id": identifier, "name": f"{case['name']}.tar.gz", "size": target.stat().st_size,
        "type": "application/gzip", "path": f"attachments/{identifier}.tar.gz"}]))
    return workspace, {"kind": "upload", "attachments": [identifier]}


def _route_name(env: dict[str, str]) -> str:
    if env.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        return "codex_subscription"
    if env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1":
        return "claude_subscription"
    return "api_key"


def _score(case: dict[str, Any], graph: dict[str, Any], receipt: dict[str, Any], seconds: float) -> dict[str, Any]:
    nodes = {node["id"]: node for node in graph["nodes"]}
    flagged = [node_id for node_id, node in nodes.items() if node["meta"]["verdict"] in ("error", "gap")]
    usage = {"input_tokens": 0, "output_tokens": 0}
    for call in receipt.get("call_log") or []:
        for key in usage:
            usage[key] += int((call.get("usage") or {}).get(key) or 0)
    result: dict[str, Any] = {
        "case": case["name"],
        "kind": case["kind"],
        "counts": graph.get("counts"),
        "coverage": graph.get("coverage"),
        "main_closure": graph.get("main_closure"),
        "flagged": flagged,
        "calls": receipt.get("calls"),
        "tokens": usage,
        "seconds": round(seconds, 1),
        "degraded": receipt.get("degraded"),
    }
    if case["kind"] == "withdrawn":
        expected = case.get("expect_flagged") or []
        result["expected_found"] = {node_id: nodes.get(node_id, {}).get("meta", {}).get("verdict", "missing")
                                    for node_id in expected}
        result["detected"] = any(value in ("error", "gap") for value in result["expected_found"].values())
        result["closure_propagated"] = {
            node_id: nodes.get(node_id, {}).get("meta", {}).get("closure", "missing")
            for node_id in case.get("expect_closure_flagged") or []
        }
    if case["kind"] == "control":
        checked = (graph.get("coverage") or {}).get("checked") or 0
        result["false_positive_rate"] = round(len(flagged) / checked, 3) if checked else None
    return result


def _dry_run(workspace: Path, req: dict[str, Any], fetcher: Any = None) -> dict[str, Any]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "DEEPSEEK_API_KEY": "dry-run",
           "AGENT_MONITOR_SELECTED_MODEL": "deepseek-dry-run", "PYTHONPATH": os.environ.get("PYTHONPATH", "")}
    run = runner.AnalysisRun(workspace, req, env=env, sink=lambda _event: None, fetcher=fetcher)
    bundle = run.ingest()
    segments, manuscript = run.segment(bundle)
    ranked = run.priority(segments)
    plan = []
    calls = 0
    for node in ranked[: run.caps.max_nodes]:
        texts = run.prompts_for(node, segments)
        plan.append({"id": node["id"], "label": prompts.node_label(node), "parts": len(texts),
                     "prompt_bytes": max(prompts.utf8_len(text) for text in texts)})
        calls += len(texts)
    sizes = sorted(item["prompt_bytes"] for item in plan)
    return {
        "title": manuscript.get("title"),
        "warnings": manuscript.get("warnings"),
        "parser": segments.get("parser"),
        "statements": len(segments["nodes"]),
        "checkable": len(ranked),
        "planned_checks": len(plan),
        "planned_calls": calls + 1,
        "caps": run.caps.as_dict(),
        "prompt_bytes_median": sizes[len(sizes) // 2] if sizes else 0,
        "prompt_bytes_max": sizes[-1] if sizes else 0,
        "split_results": [item["id"] for item in plan if item["parts"] > 1],
        "main_results": [node["id"] for node in segments["nodes"] if node.get("main_result")],
        "first_checks": [item["id"] for item in plan[:8]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", help="case name (repeatable); default: all")
    parser.add_argument("--local", action="append", default=[], metavar="NAME=DIR",
                        help="read a case from a local TeX folder instead of the network")
    parser.add_argument("--work", type=Path, default=Path("eval-analyze-work"), help="workspace root")
    parser.add_argument("--dry-run", action="store_true", help="no model calls: ingest, segment and plan only")
    parser.add_argument("--hint", action="store_true", help="add the focus hint to cases that have one")
    parser.add_argument("--model", default="", help="model id for API routes (default: the environment's)")
    parser.add_argument("--max-nodes", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--json", type=Path, help="write all metrics here")
    args = parser.parse_args(argv)
    locals_by_name = {}
    for item in args.local:
        name, _, directory = item.partition("=")
        if not directory or not Path(directory).is_dir():
            parser.error(f"--local {item}: not NAME=DIR with an existing directory")
        locals_by_name[name] = Path(directory).resolve()
    cases = [case for case in SUITE if not args.case or case["name"] in args.case]
    if not cases:
        parser.error("no such case")
    env = dict(os.environ)
    if args.model:
        env["AGENT_MONITOR_ANALYZE_MODEL"] = args.model
    caps = {key: value for key, value in (("max_nodes", args.max_nodes), ("max_calls", args.max_calls)) if value > 0}
    results = []
    for case in cases:
        workspace, source = _workspace(args.work.resolve(), case, locals_by_name.get(case["name"]))
        req = analysis_request.build(
            source=source, caps=caps, focus=case.get("hint", "") if args.hint else "", formalize=False,
            role="admin", route=_route_name(env), model=args.model or env.get("AGENT_MONITOR_SELECTED_MODEL", ""),
            reasoning_effort="default", sponsored=False)
        analysis_request.write(workspace, **{key: value for key, value in req.items() if key != "schema"})
        started = time.monotonic()
        try:
            if args.dry_run:
                outcome = {"case": case["name"], "kind": case["kind"], "dry_run": _dry_run(workspace, req)}
            else:
                runner.run_analysis(workspace, analysis_request.load(workspace), env=env,
                                    sink=lambda _event: None)
                graph = analysis_graph.load(workspace) or {"nodes": []}
                receipt = json.loads((workspace / "analysis" / "receipt.json").read_text())
                outcome = _score(case, graph, receipt, time.monotonic() - started)
        except runner.AnalyzeError as exc:
            outcome = {"case": case["name"], "kind": case["kind"], "error": str(exc)[:300]}
        outcome["workspace"] = str(workspace)
        results.append(outcome)
        print(json.dumps({key: value for key, value in outcome.items() if key != "workspace"}, ensure_ascii=False))
    if not args.dry_run:
        withdrawn = [item for item in results if item.get("kind") == "withdrawn" and "detected" in item]
        controls = [item for item in results if item.get("kind") == "control" and item.get("false_positive_rate") is not None]
        summary = {
            "detected": sum(1 for item in withdrawn if item["detected"]),
            "withdrawn_cases": len(withdrawn),
            "control_flags": sum(len(item["flagged"]) for item in controls),
            "control_checked": sum((item.get("coverage") or {}).get("checked") or 0 for item in controls),
            "calls": sum(item.get("calls") or 0 for item in results),
        }
        print(json.dumps({"summary": summary}))
        results.append({"summary": summary})
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
