#!/usr/bin/env python3
"""curator-calibration.py — the exam any curator-scoring change must sit.

Measurement-only (reads annotation logs; touches nothing). For every annotated
node whose annotation history reached a terminal status, compare the LAST score
the curator gave it while it was still open/active against how the route
actually ended:

  label     closed -> positive | dead / blocked -> negative
            still open/active at project end -> censored, EXCLUDED (a route the
            stop cut short has no right answer)
  feature   the last score assigned while status was open/active/unset — i.e.
            "with the evidence in, how well did the curator call it?"

Outputs the overall AUC, a calibration curve (score bucket -> realized closure
rate), per-kind and per-project slices, and the worst-called nodes — the raw
material a close-of-project retrospective (lesson_add) is written from.

Honest limitation, do not over-read: the positive label is `closed`, which is
NOT "contributed to the final theorem" — a side lemma that closed but fed
nothing still counts positive. The stricter label needs the target closure
(`cgm finalize`), which no measured project has run. This exam grades
"did the curator predict whether a route could be walked", not "was it worth
walking".

Baseline at introduction (2026-08, 30 projects): AUC 0.700, closed n=187 mean
0.639 vs dead/blocked n=129 mean 0.534.

Usage:
  python3 scripts/curator-calibration.py --all
  python3 scripts/curator-calibration.py --project fpb05_spde [--json out.json]
  python3 scripts/curator-calibration.py --compare fpb05_spde,fpb03_bernoulli
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
PROJECTS_ROOT = REPO / "runtime" / "projects"

TERMINAL_POS = ("closed",)
TERMINAL_NEG = ("dead", "blocked")
BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))


def _read_jsonl(path: Path) -> List[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _node_kind(project_dir: Path, node_id: str, gm_cache: Dict[str, str]) -> str:
    """goal: / a gm channel name / fact / unresolved — for the per-kind slice."""
    if node_id.startswith("goal:"):
        return "goal"
    if node_id in gm_cache:
        return gm_cache[node_id]
    if (project_dir / "fact_graph" / "facts" / f"{node_id}.md").exists():
        return "fact"
    return "unresolved"


def _gm_ids(project_dir: Path) -> Dict[str, str]:
    """entry id -> channel (kind) across the project's gm channels."""
    out: Dict[str, str] = {}
    gm = project_dir / "global_memory"
    if not gm.is_dir():
        return out
    for f in gm.glob("*.jsonl"):
        if f.name in ("edges.jsonl", "annotations.jsonl", "_status.jsonl"):
            continue
        for rec in _read_jsonl(f):
            if rec.get("id"):
                out[rec["id"]] = f.stem
    return out


def collect(projects: List[str]) -> List[Dict[str, Any]]:
    """One row per node with a terminal outcome: project, node, kind,
    last-open score, label, and the comment that accompanied that score."""
    rows: List[Dict[str, Any]] = []
    for name in projects:
        pdir = PROJECTS_ROOT / name
        anns = _read_jsonl(pdir / "global_memory" / "annotations.jsonl")
        if not anns:
            continue
        gm_cache = _gm_ids(pdir)
        hist: Dict[str, List[dict]] = defaultdict(list)
        for a in anns:  # file is chronological
            if a.get("node_id"):
                hist[a["node_id"]].append(a)
        for node, seq in hist.items():
            label: Optional[bool] = None
            for a in seq:
                st = a.get("status")
                if st in TERMINAL_POS:
                    label = True
                elif st in TERMINAL_NEG:
                    label = False
            if label is None:
                continue  # censored — the project ended before the route did
            pre = [a for a in seq
                   if a.get("score") is not None
                   and a.get("status") in ("open", "active", None)]
            if not pre:
                continue
            rows.append({
                "project": name,
                "node_id": node,
                "kind": _node_kind(pdir, node, gm_cache),
                "score": float(pre[-1]["score"]),
                "label": label,
                "comment": (pre[-1].get("comment") or "")[:200],
            })
    return rows


def auc(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Exact Mann–Whitney AUC (ties count half)."""
    pos = sorted(r["score"] for r in rows if r["label"])
    neg = [r["score"] for r in rows if not r["label"]]
    if not pos or not neg:
        return None
    import bisect
    wins = 0.0
    for n in neg:
        lo = bisect.bisect_left(pos, n)
        hi = bisect.bisect_right(pos, n)
        wins += (len(pos) - hi) + 0.5 * (hi - lo)
    return wins / (len(pos) * len(neg))


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pos = [r for r in rows if r["label"]]
    neg = [r for r in rows if not r["label"]]
    curve = []
    for lo, hi in BUCKETS:
        b = [r for r in rows if lo <= r["score"] < hi]
        curve.append({
            "bucket": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(b),
            "closure_rate": (sum(r["label"] for r in b) / len(b)) if b else None,
        })
    by_kind = {}
    for k in sorted({r["kind"] for r in rows}):
        sub = [r for r in rows if r["kind"] == k]
        by_kind[k] = {"n": len(sub), "auc": auc(sub)}
    by_project = {}
    for p in sorted({r["project"] for r in rows}):
        sub = [r for r in rows if r["project"] == p]
        by_project[p] = {"n": len(sub), "auc": auc(sub)}
    worst = sorted(
        rows, key=lambda r: (r["score"] if not r["label"] else 1.0 - r["score"]),
        reverse=True)[:10]
    return {
        "n_closed": len(pos), "n_dead_blocked": len(neg),
        "mean_score_closed": statistics.mean(r["score"] for r in pos) if pos else None,
        "mean_score_dead": statistics.mean(r["score"] for r in neg) if neg else None,
        "auc": auc(rows),
        "calibration_curve": curve,
        "by_kind": by_kind,
        "by_project": by_project,
        "worst_calls": worst,
    }


def render(s: Dict[str, Any]) -> None:
    f = lambda x: "—" if x is None else f"{x:.3f}"
    print(f"samples: closed {s['n_closed']} / dead+blocked {s['n_dead_blocked']}")
    print(f"AUC {f(s['auc'])}    mean score  closed {f(s['mean_score_closed'])}"
          f"  vs  dead {f(s['mean_score_dead'])}")
    print("\ncalibration curve (score bucket -> realized closure rate):")
    for c in s["calibration_curve"]:
        rate = "—" if c["closure_rate"] is None else f"{c['closure_rate']:.0%}"
        print(f"  {c['bucket']}   n={c['n']:4d}   {rate}")
    print("\nby kind:")
    for k, v in s["by_kind"].items():
        print(f"  {k:12s} n={v['n']:4d}  AUC {f(v['auc'])}")
    print("\nby project (n>=10):")
    for p, v in s["by_project"].items():
        if v["n"] >= 10:
            print(f"  {p:26s} n={v['n']:4d}  AUC {f(v['auc'])}")
    print("\nworst calls (retrospective material):")
    for r in s["worst_calls"]:
        end = "closed" if r["label"] else "dead"
        print(f"  {r['score']:.2f} -> {end:6s}  [{r['project']}] {r['node_id'][:40]}")
        if r["comment"]:
            print(f"        comment: {r['comment'][:110]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="one project")
    g.add_argument("--all", action="store_true", help="every project on disk")
    g.add_argument("--compare", help="'a,b:c,d' compares two groups; a plain 'a,b' reports each project separately")
    ap.add_argument("--json", dest="json_out", default="", help="also write the summary JSON here")
    args = ap.parse_args()

    if args.compare:
        parts = args.compare.split(":")
        groups = [p.split(",") for p in parts] if len(parts) == 2 else [args.compare.split(",")]
        if len(groups) == 1:  # a plain comma list = per-project comparison
            for name in groups[0]:
                rows = collect([name])
                print(f"\n===== {name} =====")
                if rows:
                    render(summarize(rows))
                else:
                    print("no terminal-labelled annotations")
            return 0
        for i, gp in enumerate(groups, 1):
            rows = collect(gp)
            print(f"\n===== group {i}: {','.join(gp)} =====")
            render(summarize(rows)) if rows else print("no data")
        return 0

    projects = ([args.project] if args.project
                else sorted(p.name for p in PROJECTS_ROOT.iterdir() if p.is_dir()))
    rows = collect(projects)
    if not rows:
        print("no terminal-labelled annotations found", file=sys.stderr)
        return 1
    s = summarize(rows)
    render(s)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(s, ensure_ascii=False, indent=2))
        print(f"\njson -> {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
