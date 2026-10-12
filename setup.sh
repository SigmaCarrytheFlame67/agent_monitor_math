#!/usr/bin/env bash
# One-shot setup for Agent Monitor. Run from the repo root:  ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"
export PATH="${HOME}/.local/node24/bin:${HOME}/.npm-global/bin:${HOME}/.local/bin:${PATH}"

say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

# ── 1. Main virtualenv (console + Math Harness + Hermes + Monitor) ──────────
# pyproject.toml requires Python >=3.11,<3.14. Ubuntu 26.04 ships 3.14 as
# python3, so fall back to python3.13, then to a uv-managed 3.13. An explicit
# PYTHON=/path/to/python always wins.
py_supported() {
  "$1" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' 2>/dev/null
}
pick_python() {
  if [ -n "${PYTHON:-}" ]; then
    printf '%s\n' "$PYTHON"; return 0
  fi
  local cand
  # Prefer uv's own path for an installed 3.13 (…/cpython-3.13-<platform>/bin).
  # The ~/.local/bin/python3.13 shim would give the venv `home = ~/.local/bin`,
  # which deploy/ansatze/install.sh rejects (ansatze-kimi.service bind-mounts
  # exactly the uv interpreter directory).
  if command -v uv >/dev/null; then
    cand=$(uv python find 3.13 2>/dev/null || true)
    if [ -n "$cand" ] && py_supported "$cand"; then
      printf '%s\n' "$cand"; return 0
    fi
  fi
  for cand in python3 python3.13 python3.12 python3.11; do
    if command -v "$cand" >/dev/null && py_supported "$cand"; then
      command -v "$cand"; return 0
    fi
  done
  if command -v uv >/dev/null; then
    uv python find 3.13 2>/dev/null && return 0
    warn "Installing a uv-managed Python 3.13 (system python3 is $(python3 -V 2>&1))" >&2
    uv python install 3.13 >&2 && uv python find 3.13 && return 0
  fi
  return 1
}
if ! PY=$(pick_python); then
  echo "No Python 3.11-3.13 found. Install uv (https://docs.astral.sh/uv/) or set PYTHON=/path/to/python3.13." >&2
  exit 1
fi
if ! command -v "$PY" >/dev/null; then
  echo "PYTHON=$PY is not an executable." >&2; exit 1
fi
if ! py_supported "$PY"; then
  warn "$PY is $($PY -V 2>&1); pyproject.toml requires >=3.11,<3.14 and pip will refuse it."
fi
if [ -d .venv ] && ! py_supported ./.venv/bin/python; then
  warn ".venv uses $(./.venv/bin/python -V 2>&1); remove .venv to recreate it with $($PY -V 2>&1)."
fi
say "Creating main venv (.venv) with $($PY -V)"
[ -d .venv ] || "$PY" -m venv .venv
VPY=./.venv/bin/python
"$VPY" -m pip --version >/dev/null 2>&1 || "$VPY" -m ensurepip --upgrade
"$VPY" -m pip install -q --upgrade pip
"$VPY" -m pip install -q -e .
say "Main venv ready"

# ── 2. IMProof engine venv (needs Python >= 3.12) ───────────────────────────
IMPROOF_PY=""
for cand in "$PY" python3.13 python3.12 python3; do
  if command -v "$cand" >/dev/null; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
      IMPROOF_PY="$cand"; break
    fi
  fi
done
if [ -n "$IMPROOF_PY" ]; then
  say "Creating IMProof venv (engines/improof/.venv) with $($IMPROOF_PY -V)"
  [ -d engines/improof/.venv ] || "$IMPROOF_PY" -m venv engines/improof/.venv
  IPY=./engines/improof/.venv/bin/python
  "$IPY" -m pip --version >/dev/null 2>&1 || "$IPY" -m ensurepip --upgrade
  "$IPY" -m pip install -q --upgrade pip
  "$IPY" -m pip install -q -e engines/improof
  say "IMProof venv ready"
else
  warn "Python 3.12+ not found — IMProof engine skipped (install python3.12 and re-run)."
fi

# ── 2b. DeepAgents engine venv (needs Python >= 3.12) ────────────────────────
if [ -n "$IMPROOF_PY" ]; then
  say "Creating DeepAgents venv (engines/deepagents/.venv) with $($IMPROOF_PY -V)"
  mkdir -p engines/deepagents
  [ -d engines/deepagents/.venv ] || "$IMPROOF_PY" -m venv engines/deepagents/.venv
  DPY=./engines/deepagents/.venv/bin/python
  "$DPY" -m pip --version >/dev/null 2>&1 || "$DPY" -m ensurepip --upgrade
  "$DPY" -m pip install -q --upgrade pip
  "$DPY" -m pip install -q deepagents langchain-openai
  say "DeepAgents venv ready"
else
  warn "Python 3.12+ not found — DeepAgents engine skipped."
fi

# ── 2c. OpenClaw CLI (needs Node >= 24.15; user-level install) ────────────────
if [ ! -x "$HOME/.npm-global/bin/openclaw" ]; then
  if command -v npm >/dev/null; then
    NODE_OK=$(node -e 'const [a,b]=process.versions.node.split(".").map(Number); console.log((a===22&&b>=22)||(a===24&&b>=15)||a>=25 ? 1 : 0)' 2>/dev/null || echo 0)
    if [ "$NODE_OK" != "1" ] && [ ! -x "$HOME/.local/node24/bin/node" ]; then
      say "Installing standalone Node 24.15 to ~/.local/node24 (openclaw needs >= 24.15)"
      ARCH=$(uname -m); [ "$ARCH" = "arm64" ] && NARCH=darwin-arm64 || NARCH=darwin-x64
      case "$(uname -s)" in Linux) [ "$ARCH" = "aarch64" ] && NARCH=linux-arm64 || NARCH=linux-x64;; esac
      curl -sL "https://nodejs.org/dist/v24.15.0/node-v24.15.0-$NARCH.tar.gz" | tar -xz -C "$HOME/.local" \
        && mv "$HOME/.local/node-v24.15.0-$NARCH" "$HOME/.local/node24" || warn "node download failed"
    fi
    say "Installing OpenClaw CLI to ~/.npm-global"
    mkdir -p "$HOME/.npm-global"
    PATH="$HOME/.local/node24/bin:$PATH" npm install -g --prefix "$HOME/.npm-global" openclaw@latest \
      || warn "openclaw install failed — engine will show as unavailable"
  else
    warn "npm not found — OpenClaw engine skipped"
  fi
else
  say "OpenClaw CLI already installed"
fi

# ── 2d. Official DeepSeek Harness sources ────────────────────────────
if [ ! -d engines/deepseek-harness/.git ]; then
  say "Cloning official DeepSeek Harness"
  git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git engines/deepseek-harness \
    || warn "DeepSeek Harness clone failed"
else
  say "DeepSeek Harness source already present"
fi
if ! command -v dsh >/dev/null && command -v npm >/dev/null; then
  say "Installing official DeepSeek Harness CLI to ~/.npm-global"
  PATH="$HOME/.local/node24/bin:$PATH" npm install -g --prefix "$HOME/.npm-global" @deepseek-ai/dsh \
    || warn "dsh install failed — npx will retry on the first API run"
fi

# ── 3. .env ──────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  say "Created .env — add your API keys (or use the in-app Settings page)"
else
  say ".env already exists — leaving it untouched"
fi
chmod 600 .env

# ── 4. Optional CLI engines ──────────────────────────────────────────────────
say "Optional CLI engines (skip if you only need Math Harness / Hermes / IMProof):"
command -v codex      >/dev/null && echo "  codex:      installed" || echo "  codex:      npm install -g @openai/codex"
command -v claude     >/dev/null && echo "  claude:     installed ($(claude --version 2>/dev/null || echo version unknown))" || echo "  claude:     install the official CLI: https://code.claude.com/docs/en/installation"
command -v openclaude >/dev/null && echo "  openclaude: installed" || echo "  openclaude: npm install -g @gitlawb/openclaude@latest"
command -v openhands  >/dev/null && echo "  openhands:  installed" || echo "  openhands:  uv tool install openhands"

# Codex stores its own auth; log it in from OPENAI_API_KEY if possible.
if command -v codex >/dev/null; then
  KEY=$(grep -E '^OPENAI_API_KEY=.+' .env | head -1 | cut -d= -f2- || true)
  if [ -n "${KEY:-}" ] && ! codex login status >/dev/null 2>&1; then
    say "Logging Codex CLI in with OPENAI_API_KEY from .env"
    printf '%s' "$KEY" | codex login --with-api-key || warn "codex login failed — run it manually"
  fi
fi

# ── 5. LaTeX (for compiled PDF preview) ──────────────────────────────────────
if ! command -v pdflatex >/dev/null && ! command -v tectonic >/dev/null; then
  warn "No LaTeX compiler found — PDF preview needs one. macOS: brew install --cask basictex   Linux: apt install texlive"
fi

say "Done. Start the console with:  ./start.sh   →  http://localhost:4600"
