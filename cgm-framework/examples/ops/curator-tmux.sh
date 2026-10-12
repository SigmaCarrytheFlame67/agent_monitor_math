#!/usr/bin/env bash
# =============================================================================
# EXAMPLE, NOT CORE. Copy-pasteable demonstration of running CGM unattended.
# Nothing in the engine depends on examples/. See examples/README.md.
# =============================================================================
# curator-tmux.sh <project> — run a resident exploring-graph curator in tmux.
#
# The curator is a single-purpose Claude Code session that keeps the project's
# exploring-graph frontier ranked (statuses, scores, comments) while the swarm
# runs. It is NOT a second main agent: its contract
# (agents/contracts/curator.md) is appended as a system prompt and supersedes
# the repo CLAUDE.md's main-agent framing, and its gateway role is `curator`
# (annotation-only: graph_annotate + reads; no gm_add, no facts, no edges).
#
# CGM_ROLE=curator is exported BEFORE claude starts, so the cgm MCP server
# spawned from .mcp.json (bin/cgm-mcp honors a pre-set CGM_ROLE) exposes
# the curator tool set instead of the main one.
#
#   bash examples/ops/curator-tmux.sh <project>
#   tmux attach -t cgm-curator     # watch / interact
#
# Env:
#   CGM_CURATOR_MODEL    claude model for the curator (default claude-fable-5)
#   CGM_CURATOR_EFFORT   reasoning effort low|medium|high|xhigh|max (default xhigh)
#   CGM_CURATOR_TMUX     tmux session name (default cgm-curator)
#
# Requires: tmux, and the `claude` CLI on PATH. Run ONE curator per project.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../../scripts/env.sh"

PROJECT="${1:?usage: curator-tmux.sh <project>}"
PROJDIR="$CGM_AGENTS_ROOT/$PROJECT"
[ -d "$PROJDIR" ] || { echo "no such project: $PROJDIR" >&2; exit 1; }

SESSION="${CGM_CURATOR_TMUX:-cgm-curator}"
CURATOR_MODEL="${CGM_CURATOR_MODEL:-claude-fable-5}"
CURATOR_EFFORT="${CGM_CURATOR_EFFORT:-xhigh}"

command -v tmux   >/dev/null 2>&1 || { echo "need tmux on PATH"   >&2; exit 1; }
command -v claude >/dev/null 2>&1 || { echo "need the claude CLI on PATH" >&2; exit 1; }

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "[$SESSION] already running — attach with: tmux attach -t $SESSION"
  exit 0
fi

CONTRACT="$CGM_ROOT/agents/contracts/curator.md"
[ -f "$CONTRACT" ] || { echo "missing curator contract: $CONTRACT" >&2; exit 1; }

# Scoring lessons from previous projects (the curator's evolution loop): the
# contract itself never changes; recent lessons are appended to the launch
# prompt. Visibility follows the ONE cross-project-memory switch,
# CGM_MEMORY_SCOPE (off = none injected; batch = same-batch sources only;
# all = everything) — the same rule memory_recall applies.
LESSONS_FILE="$CGM_ROOT/runtime/curator_lessons.jsonl"
PROMPT_FILE="$CGM_ROOT/runtime/logs/curator-prompt-$PROJECT.md"
mkdir -p "$CGM_ROOT/runtime/logs"
cat "$CONTRACT" > "$PROMPT_FILE"
if [ -f "$LESSONS_FILE" ] && [ "${CGM_MEMORY_SCOPE:-off}" != "off" ]; then
  {
    printf '\n\n## Scoring lessons from previous projects (methodology only; latest 20)\n'
    SCOPE="${CGM_MEMORY_SCOPE}" PROJDIR="$PROJDIR" LF="$LESSONS_FILE" python3 - <<'PYEOF'
import json, os, pathlib
scope, lf = os.environ["SCOPE"].lower(), pathlib.Path(os.environ["LF"])
pdir = pathlib.Path(os.environ["PROJDIR"])
def batch(d):
    try: return json.loads((d / "project.json").read_text()).get("batch")
    except Exception: return None
mine = batch(pdir)
rows = []
for line in lf.read_text(encoding="utf-8", errors="ignore").splitlines():
    try: r = json.loads(line)
    except Exception: continue
    if scope == "batch":
        if not mine or batch(pdir.parent / str(r.get("source_project", ""))) != mine:
            continue
    rows.append(r)
for r in rows[-20:]:
    print(json.dumps(r, ensure_ascii=False))
PYEOF
  } >> "$PROMPT_FILE"
fi

# Start detached in the repo root (so .mcp.json wires the gateway), with the
# curator role + author in the environment and the curator contract appended.
# --dangerously-skip-permissions: the curator is an unattended resident agent
# (same intended mode as the main agent — see docs/security-and-trust.md); a
# permission prompt would silently stall its watch loop forever.
tmux new-session -d -s "$SESSION" -c "$CGM_ROOT" \
  "CGM_ROLE=curator CGM_AUTHOR=curator claude \
     --dangerously-skip-permissions \
     --model '$CURATOR_MODEL' --effort '$CURATOR_EFFORT' \
     --append-system-prompt \"\$(cat '$PROMPT_FILE')\" \
     \"You are the curator for project '$PROJECT' (dir: $PROJDIR). Follow your \
contract: watch the shared stores, propagate statuses by the edge algebra, \
score the open frontier with cited evidence, and self-pace with /loop \
(debounced wakes). Begin by reading the current frontier via graph_frontier.\""

# Mirror the curator's raw terminal output into runtime/logs so the transcript
# survives tmux (scrollback is finite and dies with the server). This is the
# rendered pane stream (ANSI included); the authoritative structured transcript
# with per-request token usage is Claude Code's own session JSONL — aggregate it
# with scripts/claude-usage.py.
CURATOR_LOG="$CGM_ROOT/runtime/logs/curator-$PROJECT.log"
mkdir -p "$CGM_ROOT/runtime/logs"
printf '\n===== curator start %s (tmux session %s) =====\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SESSION" >> "$CURATOR_LOG"
tmux pipe-pane -t "$SESSION" -o "cat >> '$CURATOR_LOG'"

echo "[$SESSION] started for project $PROJECT — attach with: tmux attach -t $SESSION"
echo "[$SESSION] raw output → $CURATOR_LOG ; token ledger → python3 scripts/claude-usage.py"
