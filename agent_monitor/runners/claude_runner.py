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
else:
    # The registry launches this file by path. Give that direct-script process
    # a package root before importing the backend; a sibling/top-level import
    # breaks the backend's own relative imports.
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from agent_monitor.runners.claude_backend import ClaudeBackendError, claude_proof


def _emit_raw(line: str) -> None:
    sys.stdout.write(line if line.endswith("\n") else line + "\n")
    sys.stdout.flush()


def _task_prompt(argv: list[str]) -> str:
    """``--prompt-file PATH`` (the registry's command), else one argument (an
    operator's ``CLAUDE_CMD`` that still passes ``{prompt}``)."""
    if len(argv) > 2 and argv[1] == "--prompt-file":
        path = Path(argv[2])
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                return handle.read()
        finally:
            # The workspace is Claude's: the prompt (agent profile included)
            # must not stay there.
            path.unlink(missing_ok=True)
    return argv[1] if len(argv) > 1 else ""


def main() -> int:
    try:
        prompt = _task_prompt(sys.argv)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"[agent-monitor] Claude Code could not read its task prompt: {exc}", file=sys.stderr, flush=True)
        return 2
    if not prompt.strip():
        print("Claude Code received an empty proof prompt", file=sys.stderr, flush=True)
        return 2
    try:
        claude_proof(
            prompt,
            Path.cwd(),
            model=os.environ.get("AGENT_MONITOR_CLAUDE_MODEL"),
            emit_line=_emit_raw,
        )
    except ClaudeBackendError as exc:
        print(f"[agent-monitor] {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
