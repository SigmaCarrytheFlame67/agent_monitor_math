#!/usr/bin/env python3
"""claude-transcript.py — render Claude Code session JSONLs into readable transcript logs.

The main agent and the curators are Claude Code sessions; Claude Code persists
their full raw record (every user/assistant message, thinking block, tool call,
tool result, and per-request token usage) as JSONL under
~/.claude/projects/<repo-slug>/*.jsonl. That JSONL is the authoritative log.
This script materializes the harness-side readable view — one plain-text
transcript per session under runtime/logs/claude-sessions/ — analogous to the
codex workers' round_N.log.

Usage:
  python3 scripts/claude-transcript.py              # export all sessions (incremental)
  python3 scripts/claude-transcript.py <id-prefix>  # export matching sessions only
  python3 scripts/claude-transcript.py --full       # don't truncate tool results
  python3 scripts/claude-transcript.py --repo DIR --out DIR

Incremental: a session is re-rendered only if its JSONL is newer than the
existing transcript, so running this repeatedly (e.g. from a SessionEnd hook)
is cheap.
"""

import argparse
import json
import re
import sys
from pathlib import Path

CURATOR_RE = re.compile(r"You are the curator for project '([^']+)'")
TRUNCATE_AT = 20000  # chars per tool result / attachment unless --full


def repo_slug(repo: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(repo))


def block_text(block, full: bool) -> str:
    if isinstance(block, str):
        return block
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    else:
        text = ""
    if not full and len(text) > TRUNCATE_AT:
        text = text[:TRUNCATE_AT] + f"\n… [truncated {len(text) - TRUNCATE_AT} chars — full content in the session JSONL]"
    return text


def render(path: Path, full: bool):
    """Yield transcript lines; return (role, project, started) via final dict."""
    lines = []
    role, project, started = "main", None, None
    tool_names = {}  # tool_use_id -> tool name
    seen_usage = set()  # (message id, requestId) — usage repeats on every line of a request

    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = d.get("type")
            ts = (d.get("timestamp") or "")[:19]
            side = " [subagent]" if d.get("isSidechain") else ""

            if kind == "user":
                msg = d.get("message", {})
                content = msg.get("content")
                blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        text = b.get("text", "")
                        if started is None and text:
                            started = ts
                            m = CURATOR_RE.search(text)
                            if m:
                                role, project = "curator", m.group(1)
                        lines.append(f"\n[{ts}] user{side}:\n{text}\n")
                    elif bt == "tool_result":
                        name = tool_names.get(b.get("tool_use_id"), "?")
                        err = " ERROR" if b.get("is_error") else ""
                        lines.append(f"[{ts}] tool_result{err} ({name}){side}:\n{block_text(b, full)}\n")
            elif kind == "assistant":
                msg = d.get("message", {})
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "thinking":
                        lines.append(f"[{ts}] thinking{side}:\n{b.get('thinking', '')}\n")
                    elif bt == "text":
                        lines.append(f"[{ts}] assistant{side}:\n{b.get('text', '')}\n")
                    elif bt == "tool_use":
                        tool_names[b.get("id")] = b.get("name", "?")
                        arg = json.dumps(b.get("input"), ensure_ascii=False)
                        if not full and len(arg) > 4000:
                            arg = arg[:4000] + " …[truncated]"
                        lines.append(f"[{ts}] tool_use {b.get('name', '?')}{side}: {arg}\n")
                usage = msg.get("usage")
                key = (msg.get("id"), d.get("requestId"))
                if usage and key not in seen_usage:
                    seen_usage.add(key)
                    lines.append(
                        f"[{ts}] tokens: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)} "
                        f"cache_w={usage.get('cache_creation_input_tokens', 0)} cache_r={usage.get('cache_read_input_tokens', 0)}\n")

    return lines, role, project, started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prefix", nargs="?", help="only sessions whose id starts with this")
    parser.add_argument("--repo", default=None, help="repo root (default: parent of this script's dir)")
    parser.add_argument("--out", default=None, help="output dir (default: <repo>/runtime/logs/claude-sessions)")
    parser.add_argument("--full", action="store_true", help="do not truncate tool results")
    args = parser.parse_args()

    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parent.parent
    src_dir = Path.home() / ".claude" / "projects" / repo_slug(repo)
    out_dir = Path(args.out) if args.out else repo / "runtime" / "logs" / "claude-sessions"
    if not src_dir.is_dir():
        print(f"no Claude Code transcripts found at {src_dir}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    exported = skipped = 0
    for src in sorted(src_dir.glob("*.jsonl")):
        if args.prefix and not src.stem.startswith(args.prefix):
            continue
        existing = list(out_dir.glob(f"*_{src.stem[:8]}.log"))
        if existing and existing[0].stat().st_mtime >= src.stat().st_mtime:
            skipped += 1
            continue
        lines, role, project, started = render(src, args.full)
        if not lines:
            continue
        label = f"curator-{project}" if role == "curator" else "main"
        day = (started or "unknown")[:10]
        dest = out_dir / f"{day}_{label}_{src.stem[:8]}.log"
        for stale in existing:
            if stale != dest:
                stale.unlink()
        with dest.open("w", encoding="utf-8") as handle:
            handle.write(f"===== session {src.stem} ({label}) started {started} =====\n")
            handle.write(f"===== source: {src} =====\n")
            handle.writelines(lines)
        exported += 1
    print(f"exported {exported} session(s), {skipped} up to date → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
