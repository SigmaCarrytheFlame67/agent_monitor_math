#!/usr/bin/env bash
# MEMRUN sequencer — learning-curve run — survives the operator-console session.
# Per problem: kick off a fresh claude main-agent session in tmux `cgm-main`,
# wait for swarm closure (all workers terminal after >=1 round) with a PER-PROBLEM
# wall backstop, collect usage, write a mechanical ledger row, kill the session +
# curator, kick off the next.
#
#   bash experiments/memrun/sequencer.sh
#
# Env:
#   MEMRUN_QUEUE  space-separated project names (default: the 10-problem queue)
#   MEMRUN_GRACE  extra minutes on top of the main agent's own cap (default 40)
set -uo pipefail
cd "$(dirname "$0")/../.."
. scripts/env.sh
LOG="$CGM_ROOT/runtime/logs/memrun-sequencer.log"
say(){ echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

QUEUE="${MEMRUN_QUEUE:-mem01_automorphism mem02_moebius mem03_bernoulli mem04_dilation mem05_spde mem06_treelattice mem07_matching mem08_dressian mem09_hilbert mem10_vonneumann}"
MARKER="MEMRUN-MAIN"
GRACE="${MEMRUN_GRACE:-40}"

# the main agent's own hard cap per problem (minutes); the sequencer backstop is
# this + GRACE, so the main agent always gets to stop itself first.
cap_for(){  # per-problem hard cap (minutes) — must match the kickoff prompts.
  # Closed problems: original closure time + margin (50>40, 75>60, 195>173,
  # 55>43, 85>70, 165>148, 135>119). Unclosed ones (03/04/10): 120, spend cap.
  case "$1" in
    mem01_*) echo 50 ;;  mem02_*) echo 75 ;;  mem05_*) echo 195 ;;
    mem06_*) echo 55 ;;  mem07_*) echo 85 ;;  mem08_*) echo 165 ;;
    mem09_*) echo 135 ;; *)       echo 120 ;;
  esac
}

closed(){ # $1=project -> 0 if all workers terminal after >=1 round
  P="$1" python3 - <<'PY'
import json, glob, os, sys
P=os.environ["P"]
ss=[]
for p in glob.glob(f"runtime/projects/{P}/workers/*/.status.json"):
    try: ss.append(json.load(open(p)))
    except Exception: pass
term={"stopped","terminated","deadline","max_rounds","error"}
ok = bool(ss) and all(s.get("state") in term for s in ss) and all((s.get("round") or 0)>0 for s in ss)
sys.exit(0 if ok else 1)
PY
}

kickoff(){ # $1=project
  local P="$1" kf
  kf="experiments/memrun/kickoff/$P.txt"
  if [ ! -f "$kf" ]; then say "FATAL: no kickoff file $kf"; exit 1; fi
  tmux kill-session -t cgm-main 2>/dev/null
  tmux new-session -d -s cgm-main -c "$CGM_ROOT" "claude --dangerously-skip-permissions"
  sleep 15
  printf '\n===== memrun main start %s (%s) =====\n' "$(date -u +%FT%TZ)" "$P" \
    >> "$CGM_ROOT/runtime/logs/memrun-main.log"
  tmux pipe-pane -t cgm-main -o "cat >> $CGM_ROOT/runtime/logs/memrun-main.log"
  tmux send-keys -t cgm-main -l "$(cat "$kf")"
  sleep 2
  tmux send-keys -t cgm-main Enter
  say "kickoff sent for $P (file $kf, cap $(cap_for "$P")min)"
}

collect(){ # $1=project
  local P="$1"
  python3 scripts/ablation-usage.py --project "$P" --main-marker "$MARKER $P" --json \
    > "experiments/memrun/results/$P.json" 2>>"$LOG" && say "usage collected for $P"
  python3 - "$P" >> "experiments/memrun/results/LEDGER.md" <<'PY'
import json, sys
P = sys.argv[1]
r = json.load(open(f"experiments/memrun/results/{P}.json"))
w = r["workers"]; wt = r["worker_totals"]; v = r["verifier"]; c = r["consult"]
rounds = "/".join(str(w[k]["rounds"]) for k in sorted(w))
cl = {s["role"]: s["total_new_tokens"] for s in r["claude_sessions"]}
mins = max(0, r["window"]["wall_minutes"] - 15)
short = P.split("_")[0] + "_" + P.split("_")[1]
print(f"| {short} | (待审计) | {r['facts']['facts']} ({r['facts'].get('retracted',0)}撤回) | "
      f"{wt['rounds']} ({rounds}) | ~{mins:.0f} min | {wt['tokens']:,} | "
      f"{v['tokens']:,} ({v['runs']}) | {c['calls']}× ${c['cost_usd']} | "
      f"{cl.get('main',0):,} / {cl.get('curator',0):,} |")
PY
}

mkdir -p "$CGM_ROOT/experiments/memrun/results"
say "=== MEMRUN sequencer starting; queue: $QUEUE ==="
for P in $QUEUE; do
  CAP=$(cap_for "$P")
  MAXMIN=$(( CAP + GRACE ))
  if [ ! -d "runtime/projects/$P" ] || ! closed "$P"; then
    if [ ! -d "runtime/projects/$P" ]; then
      if ! tmux has-session -t cgm-main 2>/dev/null; then kickoff "$P"; fi
      waited=0
      while [ ! -d "runtime/projects/$P" ]; do
        sleep 60; waited=$((waited+1))
        if [ "$waited" -eq 30 ]; then say "WARN: $P not created after 30min — re-kicking"; kickoff "$P"; fi
        if [ "$waited" -gt 70 ]; then say "FATAL: $P never created; stopping sequencer"; exit 1; fi
      done
    fi
    say "waiting for $P to close (backstop ${MAXMIN}min)"
    start_ts=$(date +%s)
    until closed "$P"; do
      sleep 60
      mins=$(( ($(date +%s) - start_ts) / 60 ))
      if [ "$mins" -gt "$MAXMIN" ]; then
        say "BACKSTOP: $P exceeded ${MAXMIN}min — forcing graceful stop"
        bin/cgm stop "$P" >> "$LOG" 2>&1
        sleep 300
        closed "$P" || { say "BACKSTOP: $P still open — force stop"; bin/cgm stop "$P" --force >> "$LOG" 2>&1; }
        break
      fi
    done
    say "$P closed"
  else
    say "$P already closed — collecting"
  fi
  # PROCESS GUARD — learned on mem03: `closed` can be true (status files show a
  # terminal state) while the worker loops are still alive and overwrite the
  # status on their next write; mem03's five loops ran ~8h past its force stop
  # and grew the graph 73 -> 499 facts. Kill anything still running for $P.
  for _try in 1 2 3; do
    left=$(pgrep -f "[d]anus.execution .*/${P}/" | wc -l)
    [ "$left" -eq 0 ] && break
    say "GUARD: $P still has $left worker loop(s) alive after close — killing (try $_try)"
    bin/cgm stop "$P" --force >> "$LOG" 2>&1
    for pid in $(pgrep -f "[d]anus.execution .*/${P}/"); do kill -TERM "$pid" 2>/dev/null; done
    sleep 10
    for pid in $(pgrep -f "[d]anus.execution .*/${P}/"); do kill -KILL "$pid" 2>/dev/null; done
    sleep 3
  done
  sleep 420   # let the main agent finish its closing report (it only starts
              # writing it once graceful stop has actually drained every worker,
              # which lags the `closed` check by minutes — 120s truncated it)
  tmux kill-session -t cgm-curator 2>/dev/null && say "killed leftover curator for $P"
  collect "$P"
  NEXT=$(echo "$QUEUE" | tr ' ' '\n' | grep -A1 "^$P$" | tail -1)
  if [ -n "$NEXT" ] && [ "$NEXT" != "$P" ] && [ ! -d "runtime/projects/$NEXT" ]; then
    kickoff "$NEXT"
  fi
done
say "queue complete: $QUEUE"
