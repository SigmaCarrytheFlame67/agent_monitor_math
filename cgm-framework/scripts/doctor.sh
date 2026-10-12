#!/usr/bin/env bash
# Health check: config, runtime, python deps, codex, verify service. Read-only.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../scripts/env.sh"
ok(){ printf '  \033[32mok\033[0m   %s\n' "$1"; }
no(){ printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
wn(){ printf '  \033[33mwarn\033[0m %s\n' "$1"; }
echo "== CGM doctor =="
echo "CGM_ROOT=$CGM_ROOT"
[ -f "$CGM_ROOT/config/cgm.env" ] && ok "config/cgm.env present" || wn "config/cgm.env missing (copy from .example)"
[ -f "$CGM_ROOT/config/codex.env" ] && ok "config/codex.env present" || wn "config/codex.env missing (copy from .example, fill BYO key)"
[ -n "${CGM_PY:-}" ] && [ -x "$CGM_PY" ] && ok "python: $CGM_PY" || no "no python (run bootstrap.sh)"
"$CGM_PY" -c 'import mcp' 2>/dev/null && ok "python dep: mcp" || no "python dep: mcp missing"
(cd / && "$CGM_PY" -c 'import cgm' 2>/dev/null) && ok "python pkg: cgm (importable from any cwd)" || no "cgm package not installed in the venv — worker MCP gateway and bin/ wrappers fail off the repo root (run bootstrap.sh)"
"$CGM_PY" -c 'import fastapi, uvicorn, pydantic' 2>/dev/null && ok "python deps: fastapi/uvicorn/pydantic" || no "verify-service deps missing"
"$CGM_PY" -c 'import openai' 2>/dev/null && ok "python dep: openai (gpt_pro consult)" || wn "openai missing (gpt_pro consult disabled)"
"$CGM_PY" -c 'import anthropic' 2>/dev/null && ok "python dep: anthropic (claude_api consult)" || wn "anthropic missing (claude_api consult disabled)"
if [ -n "${CGM_NODE:-}" ] && [ -x "${CGM_NODE:-/nonexist}" ]; then ok "node: $CGM_NODE"; else wn "node not provisioned"; fi
if "$CGM_ROOT/bin/codex" --version >/dev/null 2>&1; then ok "codex: $("$CGM_ROOT/bin/codex" --version 2>/dev/null)"; else no "codex wrapper not working (run bootstrap.sh)"; fi
if [ "${CODEX_BACKEND:-api}" = "api" ]; then
  if [ -f "$CODEX_HOME/config.toml" ] && grep -q cgm_api "$CODEX_HOME/config.toml" 2>/dev/null; then ok "codex backend: api provider configured"; else no "codex api provider missing (scripts/setup-codex.sh api)"; fi
  bash "$CGM_ROOT/scripts/check-codex.sh" >/dev/null 2>&1 && ok "codex API live ping ok" || wn "codex API ping FAILED (bash scripts/check-codex.sh — endpoint down/rate-limited?)"
else
  env CODEX_HOME="$CODEX_HOME" "$CGM_ROOT/bin/codex" login status >/dev/null 2>&1 && ok "codex login ok ($CODEX_HOME)" || wn "codex not logged in (scripts/setup-codex.sh login)"
fi
case "$(cgm_verify_health)" in
  ours)    ok "verify service up :$VERIFY_PORT (ours)" ;;
  foreign) no "verify port :$VERIFY_PORT answered by a FOREIGN process — another deployment holds this port; set a distinct VERIFY_PORT/DASHBOARD_PORT in config/cgm.env (fact_submit would post to the wrong verifier)" ;;
  stale)   wn "verify service down :$VERIFY_PORT (stale pidfile; scripts/services.sh up verify)" ;;
  *)       wn "verify service down :$VERIFY_PORT (scripts/services.sh up verify)" ;;
esac
# write-paper PDF render (soft): TEX_ENGINE override, else pdflatex on PATH.
TEX="${TEX_ENGINE:-pdflatex}"
command -v "$TEX" >/dev/null 2>&1 && ok "latex: $TEX ($($TEX --version 2>/dev/null | head -1))" || wn "no $TEX on PATH (write-paper PDF render needs it; set TEX_ENGINE or install TeX)"
# human-summary PDF render (soft): CGM_CHROME_BIN, else chrome/chromium on PATH.
CHROME="${CGM_CHROME_BIN:-}"; [ -z "$CHROME" ] && CHROME="$(command -v chromium chromium-browser google-chrome google-chrome-stable 2>/dev/null | head -1)"
{ [ -n "$CHROME" ] && [ -x "$CHROME" ]; } && ok "chrome: $CHROME (human-summary PDF)" || wn "no Chrome/Chromium (human-summary PDF render needs it; set CGM_CHROME_BIN or install)"
echo "consult transport: $CGM_CONSULT_TRANSPORT"
echo "done."
