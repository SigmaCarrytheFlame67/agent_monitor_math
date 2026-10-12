#!/usr/bin/env python3
"""ablation-usage.py — per-agent token/round/time accounting for one project run.

Measurement-only tool (reads logs; touches nothing). Aggregates, for a single
project (one ablation run of one problem):

  workers    per-worker rounds + codex tokens  (workers/<w>/logs/round_*.log)
  verifier   verify runs + codex tokens        (runtime/verify-runs/*/log.md,
             attributed by time window — runs are sequential per problem)
  consult    calls + tokens + cost             (<project>/spend/consult.jsonl)
  claude     main-agent / curator sessions     (~/.claude/projects/<slug>/*.jsonl,
             curator matched by its launch prompt; the ablation main agent
             matched by a marker string in its first message)
  facts      count (fact_text/facts.md entries if present, else fact_graph)
  window     wall-clock [first worker start - margin, last round-log write]

Usage:
  python3 scripts/ablation-usage.py --project ab1_fpb01_automorphism \
      [--main-marker ABLATION1-MAIN] [--from ISO] [--to ISO] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOKENS_RE = re.compile(r"^tokens used\s*$")
NUM_RE = re.compile(r"^[\d,]+$")
CURATOR_RE = re.compile(r"You are the curator for project '([^']+)'")


def parse_iso(s):
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=timezone.utc)
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rollout_tokens(session_id):
    """Recover a killed session's token count from the codex rollout file:
    blended = total_tokens - cached_input_tokens (equals the printed footer)."""
    import glob as _glob
    home = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    hits = _glob.glob(os.path.join(home, "sessions", "**",
                                   f"rollout-*{session_id}.jsonl"), recursive=True)
    if not hits:
        return None
    last = None
    with open(hits[0], errors="replace") as fh:
        for line in fh:
            if '"token_count"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            info = (rec.get("payload") or {}).get("info") or {}
            tu = info.get("total_token_usage") or {}
            if tu:
                last = tu
    if not last:
        return None
    blended = int(last.get("total_tokens", 0)) - int(last.get("cached_input_tokens", 0))
    return blended if blended > 0 else None


def codex_tokens(log_path: Path):
    """Last `tokens used` + number block in a codex exec log (session total).
    Falls back to the codex session rollout for force-killed sessions whose
    footer never printed (matched via the log's `session id:` line)."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    val = None
    for i, line in enumerate(lines):
        if TOKENS_RE.match(line.strip()) and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if NUM_RE.match(nxt):
                val = int(nxt.replace(",", ""))
    if val is None:
        for line in lines[:40]:
            m = re.match(r"session id:\s*([0-9a-f-]{36})", line.strip())
            if m:
                val = _rollout_tokens(m.group(1))
                break
    return val


def worker_stats(pdir: Path):
    out = {}
    wroot = pdir / "workers"
    for wdir in sorted(p for p in wroot.iterdir() if p.is_dir()) if wroot.exists() else []:
        logs = sorted((wdir / "logs").glob("round_*.log"),
                      key=lambda p: int(re.search(r"round_(\d+)", p.name).group(1)))
        rounds, tokens, missing = 0, 0, 0
        last_mtime = None
        for lg in logs:
            rounds += 1
            t = codex_tokens(lg)
            if t is None:
                missing += 1
            else:
                tokens += t
            m = lg.stat().st_mtime
            last_mtime = max(last_mtime or m, m)
        status = {}
        sp = wdir / ".status.json"
        if sp.exists():
            try:
                status = json.loads(sp.read_text())
            except Exception:
                pass
        out[wdir.name] = {
            "rounds": rounds, "tokens": tokens, "rounds_without_token_line": missing,
            "status_round": status.get("round"), "state": status.get("state"),
            "started_at": status.get("started_at"), "last_round_at": status.get("last_round_at"),
            "last_log_mtime": last_mtime,
        }
    return out


def project_window(pdir: Path, workers):
    starts = [w["started_at"] for w in workers.values() if w.get("started_at")]
    t0 = min(parse_iso(s) for s in starts) if starts else \
        datetime.fromtimestamp((pdir / "project.json").stat().st_mtime, tz=timezone.utc)
    mts = [w["last_log_mtime"] for w in workers.values() if w.get("last_log_mtime")]
    t1 = max(datetime.fromtimestamp(m, tz=timezone.utc) for m in mts) if mts else datetime.now(timezone.utc)
    return t0 - timedelta(minutes=5), t1 + timedelta(minutes=10)


def verify_stats(t0, t1):
    root = Path(os.environ.get("VERIFIER_RESULTS_DIR", str(REPO / "runtime" / "verify-runs")))
    runs, tokens, missing = 0, 0, 0
    for d in root.iterdir() if root.exists() else []:
        lg = d / "log.md"
        if not lg.is_file():
            continue
        try:
            head = lg.open(encoding="utf-8", errors="replace").readline()
        except OSError:
            continue
        m = re.match(r"started_at_utc:\s*(\S+)", head)
        if not m:
            continue
        try:
            ts = parse_iso(m.group(1))
        except ValueError:
            continue
        if not (t0 <= ts <= t1):
            continue
        runs += 1
        t = codex_tokens(lg)
        if t is None:
            missing += 1
        else:
            tokens += t
    return {"runs": runs, "tokens": tokens, "runs_without_token_line": missing}


def consult_stats(pdir: Path):
    led = pdir / "spend" / "consult.jsonl"
    calls, tin, tout, treas, cost = 0, 0, 0, 0, 0.0
    if led.exists():
        for line in led.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            calls += 1
            tin += int(r.get("input_tokens") or 0)
            tout += int(r.get("output_tokens") or 0)
            treas += int(r.get("reasoning_tokens") or 0)
            cost += float(r.get("cost_usd") or 0.0)
    return {"calls": calls, "input_tokens": tin, "output_tokens": tout,
            "reasoning_tokens": treas, "cost_usd": round(cost, 2)}


def claude_sessions(project: str, marker, t0, t1):
    """Scan Claude Code session JSONLs for this repo; return curator sessions for
    the project and (if marker given) the ablation main-agent sessions."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(REPO))
    root = Path.home() / ".claude" / "projects" / slug
    out = []
    for f in sorted(root.glob("*.jsonl")) if root.exists() else []:
        seen, tok = set(), {"in": 0, "out": 0, "cache_w": 0, "cache_r": 0}
        first_text, started, ended = None, None, None
        try:
            fh = f.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("timestamp")
            if ts:
                started = started or ts
                ended = ts
            if first_text is None and rec.get("type") == "user" and not rec.get("isSidechain"):
                msg = rec.get("message") or {}
                c = msg.get("content")
                if isinstance(c, str):
                    first_text = c
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") == "text":
                            first_text = part.get("text", "")
                            break
            if rec.get("type") == "assistant":
                msg = rec.get("message") or {}
                u = msg.get("usage")
                key = (msg.get("id"), rec.get("requestId"))
                if u and key not in seen:
                    seen.add(key)
                    tok["in"] += u.get("input_tokens", 0) or 0
                    tok["out"] += u.get("output_tokens", 0) or 0
                    tok["cache_w"] += u.get("cache_creation_input_tokens", 0) or 0
                    tok["cache_r"] += u.get("cache_read_input_tokens", 0) or 0
        fh.close()
        if not started:
            continue
        try:
            s, e = parse_iso(started), parse_iso(ended)
        except ValueError:
            continue
        if e < t0 or s > t1:  # session entirely outside the window
            continue
        ft = first_text or ""
        cm = CURATOR_RE.search(ft)
        if cm and cm.group(1) == project:
            role = "curator"
        elif marker and marker in ft:
            role = "main"
        else:
            continue
        out.append({"session": f.stem[:8], "role": role, "started": started,
                    "ended": ended, **tok,
                    "total_new_tokens": tok["in"] + tok["out"] + tok["cache_w"]})
    return out


def facts_count(pdir: Path):
    ft = pdir / "fact_text" / "facts.md"
    if ft.exists():
        txt = ft.read_text(encoding="utf-8", errors="replace")
        n = len(re.findall(r"^## Fact \d+\b", txt, re.M))
        r = len(set(re.findall(r"^## Retraction of Fact (\d+)\b", txt, re.M)))
        return {"store": "fact_text", "facts": n - r, "retracted": r}
    fg = pdir / "fact_graph" / "facts"
    if fg.exists():
        return {"store": "fact_graph", "facts": len(list(fg.glob("*.md"))),
                "revoked": len(list((pdir / "fact_graph" / "_revoked").glob("*.md")))}
    return {"store": "none", "facts": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--main-marker", default=None)
    ap.add_argument("--from", dest="t0", default=None)
    ap.add_argument("--to", dest="t1", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    pdir = REPO / "runtime" / "projects" / a.project
    if not pdir.is_dir():
        sys.exit(f"no such project: {pdir}")

    workers = worker_stats(pdir)
    t0, t1 = project_window(pdir, workers)
    if a.t0:
        t0 = parse_iso(a.t0)
    if a.t1:
        t1 = parse_iso(a.t1)

    rep = {
        "project": a.project,
        "window": {"from": t0.isoformat(), "to": t1.isoformat(),
                   "wall_minutes": round((t1 - t0).total_seconds() / 60, 1)},
        "workers": {k: {kk: vv for kk, vv in v.items() if kk != "last_log_mtime"}
                    for k, v in workers.items()},
        "worker_totals": {
            "rounds": sum(w["rounds"] for w in workers.values()),
            "tokens": sum(w["tokens"] for w in workers.values()),
        },
        "verifier": verify_stats(t0, t1),
        "consult": consult_stats(pdir),
        "claude_sessions": claude_sessions(a.project, a.main_marker, t0, t1),
        "facts": facts_count(pdir),
    }
    rep["grand_total_codex_tokens"] = rep["worker_totals"]["tokens"] + rep["verifier"]["tokens"]
    rep["grand_total_claude_new_tokens"] = sum(s["total_new_tokens"] for s in rep["claude_sessions"])
    rep["grand_total_consult_tokens"] = (rep["consult"]["input_tokens"]
                                         + rep["consult"]["output_tokens"])

    if a.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return

    print(f"# {a.project}")
    w = rep["window"]
    print(f"window: {w['from']} .. {w['to']}  ({w['wall_minutes']} min)")
    print(f"facts:  {rep['facts']}")
    print("\nworkers (codex):")
    for k, v in rep["workers"].items():
        note = f"  [{v['rounds_without_token_line']} rounds w/o token line]" if v["rounds_without_token_line"] else ""
        print(f"  {k:8s} rounds={v['rounds']:<3d} tokens={v['tokens']:>12,}{note}")
    wt = rep["worker_totals"]
    print(f"  {'TOTAL':8s} rounds={wt['rounds']:<3d} tokens={wt['tokens']:>12,}")
    v = rep["verifier"]
    print(f"\nverifier (codex): runs={v['runs']} tokens={v['tokens']:,}"
          + (f"  [{v['runs_without_token_line']} runs w/o token line]" if v["runs_without_token_line"] else ""))
    c = rep["consult"]
    print(f"consult: calls={c['calls']} in={c['input_tokens']:,} out={c['output_tokens']:,} "
          f"(reasoning {c['reasoning_tokens']:,}) cost=${c['cost_usd']}")
    print("claude sessions:")
    for s in rep["claude_sessions"]:
        print(f"  {s['role']:8s} {s['session']} in={s['in']:,} out={s['out']:,} "
              f"cache_w={s['cache_w']:,} cache_r={s['cache_r']:,} new={s['total_new_tokens']:,}")
    print(f"\nGRAND TOTALS: codex={rep['grand_total_codex_tokens']:,}  "
          f"claude_new={rep['grand_total_claude_new_tokens']:,}  "
          f"consult={rep['grand_total_consult_tokens']:,}")


if __name__ == "__main__":
    main()
