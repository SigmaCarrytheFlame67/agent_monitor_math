#!/usr/bin/env bash
# =============================================================================
# CGM environment — source this in any shell that talks to the system.
#
#   source <repo>/scripts/env.sh
#
# Resolves all paths from config/codex.env + config/cgm.env + runtime/
# runtime.env, then exports sane defaults and puts bin/ + the provisioned node +
# venv on PATH. The bin/ wrappers source this for you, so `cgm`, `consult`,
# `codex` work without you sourcing it manually. Sourcing twice is harmless
# (idempotent).
# =============================================================================

# Self-locate the repo root (this file lives at <repo>/scripts/env.sh).
CGM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export CGM_ROOT

# 0) codex backend (BYO OpenAI-compatible endpoint + key; gitignored)
if [ -f "$CGM_ROOT/config/codex.env" ]; then
  set -a; . "$CGM_ROOT/config/codex.env"; set +a
fi

# 1) user config (accounts / models / ports / toggles) — overrides the codex file
if [ -f "$CGM_ROOT/config/cgm.env" ]; then
  set -a; . "$CGM_ROOT/config/cgm.env"; set +a
fi

# 2) machine-derived paths written by bootstrap.sh (node, codex.js, venv)
if [ -f "$CGM_ROOT/runtime/runtime.env" ]; then
  set -a; . "$CGM_ROOT/runtime/runtime.env"; set +a
fi

# 3) defaults for anything still unset
export CGM_RUNTIME="${CGM_RUNTIME:-$CGM_ROOT/runtime}"
export CGM_AGENTS_ROOT="${CGM_AGENTS_ROOT:-$CGM_RUNTIME/projects}"
export VERIFIER_RESULTS_DIR="${VERIFIER_RESULTS_DIR:-$CGM_RUNTIME/verify-runs}"
export CODEX_HOME="${CODEX_HOME:-$CGM_RUNTIME/codex-home}"
export VERIFY_PORT="${VERIFY_PORT:-8091}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-8099}"
export CGM_VERIFY_URL="${CGM_VERIFY_URL:-http://127.0.0.1:${VERIFY_PORT}/verify}"
export CGM_CODEX_MODEL="${CGM_CODEX_MODEL:-${CODEX_API_MODEL:-gpt-5.5}}"   # neutral default model for every codex call (defers to the api backend model)
export CGM_CODEX_EFFORT="${CGM_CODEX_EFFORT:-xhigh}"   # neutral default reasoning effort
export CGM_CONSULT_TRANSPORT="${CGM_CONSULT_TRANSPORT:-gpt_pro}"   # gpt_pro | claude_api | claude_code | off
export CGM_CHROME_BIN="${CGM_CHROME_BIN:-}"        # headless Chrome/Chromium for human-summary PDF (empty = auto-detect)
export CODEX_BACKEND="${CODEX_BACKEND:-api}"            # api (BYO key) | chatgpt (your login)

# 4) PATH: bin wrappers first, then the provisioned node + venv (if bootstrapped)
_cgm_path="$CGM_ROOT/bin"
[ -n "${CGM_NODE_BIN:-}" ] && [ -d "$CGM_NODE_BIN" ] && _cgm_path="$_cgm_path:$CGM_NODE_BIN"
[ -n "${CGM_VENV:-}" ]     && [ -d "$CGM_VENV/bin" ] && _cgm_path="$_cgm_path:$CGM_VENV/bin"
case ":$PATH:" in *":$_cgm_path:"*) : ;; *) export PATH="$_cgm_path:$PATH" ;; esac

# 5) the python the engine runs on (venv if bootstrapped, else system python3)
if [ -n "${CGM_VENV:-}" ] && [ -x "$CGM_VENV/bin/python" ]; then
  export CGM_PY="$CGM_VENV/bin/python"
else
  export CGM_PY="${CGM_PY:-$(command -v python3 || true)}"
fi

# silent unless CGM_ENV_VERBOSE=1
if [ "${CGM_ENV_VERBOSE:-0}" = "1" ]; then
  echo "CGM_ROOT=$CGM_ROOT"
  echo "CGM_PY=$CGM_PY"
  echo "CGM_AGENTS_ROOT=$CGM_AGENTS_ROOT"
  echo "CGM_VERIFY_URL=$CGM_VERIFY_URL"
  echo "CODEX_HOME=$CODEX_HOME"
  echo "consult transport=$CGM_CONSULT_TRANSPORT"
fi

# 6) verify-service identity probe (shared by doctor.sh / services.sh).
# A bare `/health` 200 does NOT prove the responder is OURS: on a shared host a
# second CGM deployment can hold the same VERIFY_PORT, and its verifier would
# answer our probe (a false "up" that silently routes fact_submit to the wrong
# verifier). This classifies the port by matching the pid /health self-reports
# against our own runtime/run/verify.pid. Echoes one word + sets a code:
#   ours    (0)  -> healthy AND the responder is our pidfile's process
#   foreign (3)  -> something answers, but it is not ours (port collision)
#   stale   (4)  -> our pidfile process is dead and nothing answers the port
#   down    (5)  -> nothing answers and we have no pidfile process
cgm_verify_health(){
  local url="http://127.0.0.1:${VERIFY_PORT}/health"
  local pf="$CGM_RUNTIME/run/verify.pid"
  local our_pid resp health_pid
  our_pid="$(cat "$pf" 2>/dev/null || true)"
  resp="$(curl -s --max-time 5 "$url" 2>/dev/null || true)"
  if [ -z "$resp" ]; then
    if [ -n "$our_pid" ] && kill -0 "$our_pid" 2>/dev/null; then echo down; return 5; fi
    { [ -n "$our_pid" ]; } && { echo stale; return 4; }
    echo down; return 5
  fi
  # pull "pid": <n> out of the JSON without a json dep
  health_pid="$(printf '%s' "$resp" | sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p')"
  if [ -n "$our_pid" ] && [ -n "$health_pid" ] && [ "$our_pid" = "$health_pid" ]; then
    echo ours; return 0
  fi
  # answered, but not by our pidfile's process (or an old build with no pid field)
  echo foreign; return 3
}
