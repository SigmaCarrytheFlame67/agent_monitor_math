#!/usr/bin/env bash
# =============================================================================
# EXAMPLE, NOT CORE — ABLATION-1 VARIANT of curator-tmux.sh (fact TEXT mode).
# Identical to curator-tmux.sh except the launch prompt tells the curator that
# this deployment stores verified results in a plain sequential text file
# (fact_text/facts.md) instead of a fact graph. The curator contract file is
# unchanged; where it says fact_graph/facts/, read fact_text/facts.md instead.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../../scripts/env.sh"

PROJECT="${1:?usage: curator-tmux-text.sh <project>}"
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

tmux new-session -d -s "$SESSION" -c "$CGM_ROOT" \
  "CGM_ROLE=curator CGM_AUTHOR=curator claude \
     --dangerously-skip-permissions \
     --model '$CURATOR_MODEL' --effort '$CURATOR_EFFORT' \
     --append-system-prompt \"\$(cat '$CONTRACT')\" \
     \"You are the curator for project '$PROJECT' (dir: $PROJDIR). ABLATION \
NOTE — this deployment stores verified results as a plain sequential text \
file, NOT a fact graph: watch $PROJDIR/fact_text/facts.md (entries '## Fact \
N') instead of fact_graph/facts/; fact_search returns fact_number; the node \
id for a verified result in edges/annotations is fact:<N>. Everything else \
per your contract: watch the shared stores, propagate statuses by the edge \
algebra, score the open frontier with cited evidence, and self-pace with \
/loop (debounced wakes). Begin by reading the current frontier via \
graph_frontier.\""

CURATOR_LOG="$CGM_ROOT/runtime/logs/curator-$PROJECT.log"
mkdir -p "$CGM_ROOT/runtime/logs"
printf '\n===== curator start %s (tmux session %s) =====\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SESSION" >> "$CURATOR_LOG"
tmux pipe-pane -t "$SESSION" -o "cat >> '$CURATOR_LOG'"

echo "[$SESSION] started for project $PROJECT (TEXT-mode) — attach with: tmux attach -t $SESSION"
echo "[$SESSION] raw output → $CURATOR_LOG ; token ledger → python3 scripts/claude-usage.py"
