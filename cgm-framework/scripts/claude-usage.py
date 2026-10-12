#!/usr/bin/env python3
"""claude-usage.py — token-usage ledger for the Claude-side agents (main agent + curators).

Claude Code already persists every session transcript — including per-request
token usage — as JSONL under ~/.claude/projects/<repo-slug>/*.jsonl. That is
the authoritative raw log for the main agent and every curator (workers and the
verifier are codex-side and have their own logs under runtime/). What was
missing is the ledger view: this script walks those transcripts, dedupes usage
records by API request, classifies each session (curator sessions are detected
by their launch prompt "You are the curator for project '<p>'"), and prints
per-session and total token usage.

Usage:
  python3 scripts/claude-usage.py            # human-readable table
  python3 scripts/claude-usage.py --json     # one JSON record per session (machine-readable)
  python3 scripts/claude-usage.py --repo /path/to/repo   # default: this repo
"""

import argparse
import json
import re
import sys
from pathlib import Path

CURATOR_RE = re.compile(r"You are the curator for project '([^']+)'")


def repo_slug(repo: Path) -> str:
    # Claude Code maps a cwd to a project dir by replacing every
    # non-alphanumeric character of the absolute path with '-'.
    return re.sub(r"[^A-Za-z0-9]", "-", str(repo))


def first_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def scan_session(path: Path) -> dict:
    tokens = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    seen_requests = set()
    first_user, title = None, None
    models = set()
    t_min = t_max = None
    requests = 0

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = d.get("type")
            ts = d.get("timestamp")
            if ts:
                t_min = ts if t_min is None or ts < t_min else t_min
                t_max = ts if t_max is None or ts > t_max else t_max
            if kind == "ai-title" and not title:
                title = d.get("aiTitle")
            elif kind == "user" and first_user is None and not d.get("isSidechain"):
                text = first_text(d.get("message", {}).get("content"))
                if text:
                    first_user = text
            elif kind == "assistant":
                msg = d.get("message", {})
                usage = msg.get("usage")
                if not usage:
                    continue
                # One API request can emit several assistant lines that all
                # carry the same usage object — count each request once.
                key = (msg.get("id"), d.get("requestId"))
                if key in seen_requests:
                    continue
                seen_requests.add(key)
                requests += 1
                if msg.get("model"):
                    models.add(msg["model"])
                tokens["input"] += usage.get("input_tokens", 0) or 0
                tokens["output"] += usage.get("output_tokens", 0) or 0
                tokens["cache_creation"] += usage.get("cache_creation_input_tokens", 0) or 0
                tokens["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0

    role, project = "main", None
    if first_user:
        match = CURATOR_RE.search(first_user)
        if match:
            role, project = "curator", match.group(1)

    return {
        "session": path.stem,
        "role": role,
        "project": project,
        "title": title,
        "first_user": (first_user or "")[:120],
        "models": sorted(models),
        "started": t_min,
        "ended": t_max,
        "requests": requests,
        "tokens": tokens,
        "total_new_tokens": tokens["input"] + tokens["output"] + tokens["cache_creation"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=None, help="repo root (default: parent of this script's dir)")
    parser.add_argument("--json", action="store_true", help="emit one JSON record per session")
    args = parser.parse_args()

    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parent.parent
    project_dir = Path.home() / ".claude" / "projects" / repo_slug(repo)
    if not project_dir.is_dir():
        print(f"no Claude Code transcripts found at {project_dir}", file=sys.stderr)
        return 1

    sessions = [scan_session(p) for p in sorted(project_dir.glob("*.jsonl"))]
    sessions = [s for s in sessions if s["requests"] > 0]
    sessions.sort(key=lambda s: s["started"] or "")

    if args.json:
        for s in sessions:
            print(json.dumps(s, ensure_ascii=False))
        return 0

    header = f"{'started':<20} {'role':<18} {'req':>5} {'input':>12} {'output':>10} {'cache_w':>12} {'cache_r':>14}  label"
    print(header)
    print("-" * len(header))
    totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    for s in sessions:
        t = s["tokens"]
        for k in totals:
            totals[k] += t[k]
        role = s["role"] if s["role"] == "main" else f"curator:{s['project']}"
        label = s["title"] or s["first_user"].replace("\n", " ")
        print(f"{(s['started'] or '?')[:19]:<20} {role[:18]:<18} {s['requests']:>5} "
              f"{t['input']:>12,} {t['output']:>10,} {t['cache_creation']:>12,} {t['cache_read']:>14,}  {label[:50]}")
    print("-" * len(header))
    print(f"{'TOTAL':<20} {len(sessions):<18} {'':>5} {totals['input']:>12,} {totals['output']:>10,} "
          f"{totals['cache_creation']:>12,} {totals['cache_read']:>14,}")
    print("\ninput/cache_w/cache_r are prompt-side tokens (uncached / cache-write / cache-read); "
          "output is generated tokens. Raw transcripts: " + str(project_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
