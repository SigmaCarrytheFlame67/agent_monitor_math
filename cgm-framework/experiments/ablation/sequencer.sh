#!/usr/bin/env bash
# ablation sequencer — survives the operator-console session. Per problem:
# wait for swarm closure (all workers terminal after >=1 round) -> collect
# usage -> mechanical ledger row -> kill cgm-main + curator -> kickoff next.
set -uo pipefail
cd "$(dirname "$0")/../.."
. scripts/env.sh
LOG="$CGM_ROOT/runtime/logs/ablation-sequencer.log"
say(){ echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }

QUEUE="${ABL_QUEUE:-ab1_fpb05_spde ab1_fpb06_treelattice ab1_fpb07_matching ab1_fpb08_dressian ab1_fpb09_hilbert}"
MARKER="${ABL_MARKER:-ABLATION1-MAIN}"
MAXMIN=360   # hard backstop per problem (main agent's own cap is 4h)

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
  local P="$1" kf="experiments/ablation/kickoff/${1%%_*}_${1#ab1_}.txt"
  kf="experiments/ablation/kickoff/$(echo "$P" | sed 's/\(ab[0-9]*_fpb[0-9]*\).*/\1/').txt"
  tmux kill-session -t cgm-main 2>/dev/null
  tmux new-session -d -s cgm-main -c "$CGM_ROOT" "claude --dangerously-skip-permissions"
  sleep 15
  printf '\n===== ablation1 main start %s (%s) =====\n' "$(date -u +%FT%TZ)" "$P" >> "$CGM_ROOT/runtime/logs/ablation-main.log"
  tmux pipe-pane -t cgm-main -o "cat >> $CGM_ROOT/runtime/logs/ablation-main.log"
  tmux send-keys -t cgm-main -l "$(cat "$kf")"
  sleep 2
  tmux send-keys -t cgm-main Enter
  say "kickoff sent for $P (file $kf)"
}

collect(){ # $1=project
  local P="$1"
  python3 scripts/ablation-usage.py --project "$P" --main-marker "$MARKER $P" --json \
    > "experiments/ablation/results/$P.json" 2>>"$LOG" && say "usage collected for $P"
  python3 - "$P" >> experiments/ablation/results/LEDGER.md <<'PY'
import json, sys
P = sys.argv[1]
r = json.load(open(f"experiments/ablation/results/{P}.json"))
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

for P in $QUEUE; do
  if [ ! -d "runtime/projects/$P" ] || ! closed "$P"; then
    if [ ! -d "runtime/projects/$P" ]; then
      # not yet started: if cgm-main is idle on the PREVIOUS problem this is
      # the sequencer's cue to kick it off (first queue item is pre-launched
      # by the console; guard anyway)
      if ! tmux has-session -t cgm-main 2>/dev/null; then kickoff "$P"; fi
      # if session exists but project not created within 45 min, re-kick once
      waited=0
      while [ ! -d "runtime/projects/$P" ]; do
        sleep 60; waited=$((waited+1))
        if [ "$waited" -eq 45 ]; then say "WARN: $P not created after 45min — re-kicking"; kickoff "$P"; fi
        if [ "$waited" -gt 90 ]; then say "FATAL: $P never created; stopping sequencer"; exit 1; fi
      done
    fi
    say "waiting for $P to close"
    start_ts=$(date +%s)
    until closed "$P"; do
      sleep 60
      mins=$(( ($(date +%s) - start_ts) / 60 ))
      if [ "$mins" -gt "$MAXMIN" ]; then
        say "BACKSTOP: $P exceeded ${MAXMIN}min — forcing graceful stop"
        bin/cgm stop "$P" >> "$LOG" 2>&1
        sleep 300
        closed "$P" || bin/cgm stop "$P" --force >> "$LOG" 2>&1
        break
      fi
    done
    say "$P closed"
  else
    say "$P already closed — collecting"
  fi
  sleep 90   # let the main agent finish its closing report
  tmux kill-session -t cgm-curator 2>/dev/null && say "killed leftover curator for $P"
  collect "$P"
  # advance: next iteration's kickoff happens here for the NEXT problem
  NEXT=$(echo "$QUEUE" | tr ' ' '\n' | grep -A1 "^$P$" | tail -1)
  if [ -n "$NEXT" ] && [ "$NEXT" != "$P" ] && [ ! -d "runtime/projects/$NEXT" ]; then
    kickoff "$NEXT"
  fi
done
say "queue complete: $QUEUE"
