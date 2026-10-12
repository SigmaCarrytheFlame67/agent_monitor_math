#!/usr/bin/env bash
# =============================================================================
# EXAMPLE, NOT CORE. Copy-pasteable demonstration of running CGM unattended.
# Nothing in the engine depends on examples/. See examples/README.md.
# =============================================================================
# main-agent-tmux.sh — run Claude Code as a resident main agent inside tmux.
#
# This is the ONLY unattended mode in CGM: a long-lived Claude Code
# session in the repo root. Because it starts in CGM_ROOT it inherits the
# repo's CLAUDE.md, its skills, and .mcp.json — and .mcp.json is what wires the
# gateway MCP server (`python -m cgm.gateway` via bin/cgm-mcp). This script
# deliberately does NOT wire MCP itself; it only launches `claude` in the right
# directory. The strategic judgment (elaborate -> consult -> record
# master_guidance -> dispatch) lives in that main agent and its skills, not here.
#
#   bash examples/ops/main-agent-tmux.sh
#   tmux attach -t cgm-main     # to watch / interact
#
# Requires: tmux, and the `claude` CLI on PATH.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../../scripts/env.sh"

SESSION="${CGM_MAIN_TMUX:-cgm-main}"

command -v tmux   >/dev/null 2>&1 || { echo "need tmux on PATH"   >&2; exit 1; }
command -v claude >/dev/null 2>&1 || { echo "need the claude CLI on PATH" >&2; exit 1; }

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "[$SESSION] already running — attach with: tmux attach -t $SESSION"
  exit 0
fi

# Start Claude Code detached, in the repo root, so it picks up CLAUDE.md/.mcp.json/skills.
tmux new-session -d -s "$SESSION" -c "$CGM_ROOT" "claude"
echo "[$SESSION] started in $CGM_ROOT — attach with: tmux attach -t $SESSION"
