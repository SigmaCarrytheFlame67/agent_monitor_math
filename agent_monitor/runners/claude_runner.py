#!/usr/bin/env python3
"""Native Claude Code proof harness wrapper.

The official CLI's JSONL is forwarded byte-for-byte (modulo universal newline
normalization) so Conversation, Monitor, and DAG consume Claude's real events.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__:
    from .claude_backend import ClaudeBackendError, claude_proof
    from .task_prompt import read_task_prompt
else:
    # The registry launches this file by path. Give that direct-script process
    # a package root before importing the backend; a sibling/top-level import
    # breaks the backend's own relative imports.
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from agent_monitor.runners.claude_backend import ClaudeBackendError, claude_proof
    from agent_monitor.runners.task_prompt import read_task_prompt


def _emit_raw(line: str) -> None:
    sys.stdout.write(line if line.endswith("\n") else line + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        prompt = read_task_prompt(sys.argv)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"[agent-monitor] Claude Code could not read its task prompt: {exc}", file=sys.stderr, flush=True)
        return 2
    if not prompt.strip():
        print("Claude Code received an empty proof prompt", file=sys.stderr, flush=True)
        return 2
    try:
        result = claude_proof(
            prompt,
            Path.cwd(),
            model=os.environ.get("AGENT_MONITOR_CLAUDE_MODEL"),
            emit_line=_emit_raw,
        )
    except ClaudeBackendError as exc:
        print(f"[agent-monitor] {exc}", file=sys.stderr, flush=True)
        return 1
    if getattr(result, "unchanged", False):
        print("[agent-monitor] Claude Code left Proof.lean unchanged", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
