#!/usr/bin/env python3
"""Post-hoc strict judging of a project's facts by the REAL verify service.

The self-verify ablation (CGM_SELF_VERIFY_GATE) lets a worker's own review
decide what becomes a fact, so its graph is "accepted" by a gate we are testing.
This tool re-judges those facts with the independent cold-start verifier — the
same protocol used to judge the bare-codex baselines — and records the verdicts.

    python3 experiments/ablation/judge-facts.py --project ab3_fpb01_automorphism \
        --facts 1a2b...,3c4d... [--sample 5] [--all] [--out results/x.json]

Selection: --facts (explicit ids, e.g. the targets the main agent's report names)
and/or --sample N (N further facts, deterministic by id sort + fixed stride) or
--all. Each selected fact is POSTed as {statement, proof} exactly as the worker
submitted it. Results stream to the output JSON as they land, so a long run is
never lost.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cgm.core.factgraph import statement_of  # noqa: E402


def _section(text: str, name: str) -> str:
    """Body of '## <name>' up to the next '## ' heading."""
    out, inside = [], False
    for line in text.splitlines():
        if line.strip().lower() == f"## {name}":
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def _verify(url: str, statement: str, proof: str, timeout: int) -> dict:
    data = json.dumps({"statement": statement, "proof": proof}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--facts", default="", help="comma-separated fact ids")
    ap.add_argument("--sample", type=int, default=0,
                    help="judge N additional facts, evenly strided over the id-sorted rest")
    ap.add_argument("--all", action="store_true", help="judge every fact")
    ap.add_argument("--out", default="")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    facts_dir = ROOT / "runtime" / "projects" / args.project / "fact_graph" / "facts"
    if not facts_dir.is_dir():
        print(f"no fact graph at {facts_dir}", file=sys.stderr)
        return 1
    on_disk = sorted(p.stem for p in facts_dir.glob("*.md"))

    named = [f.strip() for f in args.facts.split(",") if f.strip()]
    unknown = [f for f in named if f not in on_disk]
    if unknown:
        print(f"unknown fact ids: {unknown}", file=sys.stderr)
        return 1

    if args.all:
        selected = on_disk
    else:
        rest = [f for f in on_disk if f not in named]
        extra = []
        if args.sample > 0 and rest:
            n = min(args.sample, len(rest))
            stride = len(rest) / n
            extra = [rest[int(i * stride)] for i in range(n)]
        selected = named + extra

    url = os.environ.get("CGM_VERIFY_URL", "http://127.0.0.1:8091/verify")
    out_path = Path(args.out) if args.out else (
        ROOT / "experiments" / "ablation" / "results" / f"{args.project}_posthoc.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    report = {"project": args.project, "verify_url": url,
              "facts_on_disk": len(on_disk), "named_targets": named,
              "judged": [], "summary": {}}

    for i, fid in enumerate(selected, 1):
        raw = (facts_dir / f"{fid}.md").read_text()
        statement, proof = statement_of(raw), _section(raw, "proof")
        t0 = time.time()
        try:
            res = _verify(url, statement, proof, args.timeout)
            verdict = res.get("verdict") if isinstance(res, dict) else "error"
            hints = res.get("repair_hints", "") if isinstance(res, dict) else str(res)
        except Exception as e:  # a verify outage must not lose the earlier rows
            verdict, hints = "error", str(e)
        row = {"fact_id": fid, "is_named_target": fid in named,
               "verdict": verdict, "seconds": round(time.time() - t0, 1),
               "statement_head": statement[:200], "repair_hints": hints}
        report["judged"].append(row)
        counts = {}
        for r in report["judged"]:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        report["summary"] = counts
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"[{i}/{len(selected)}] {fid} -> {verdict} ({row['seconds']}s)", flush=True)

    print(f"\nsummary: {report['summary']}  ->  {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
