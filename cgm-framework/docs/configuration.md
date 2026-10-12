# Ansatz — Configuration Reference

All host- and account-specific configuration lives in gitignored `config/*.env`
files; **no path or secret is hardcoded** elsewhere. `scripts/env.sh` sources the
chain and fills defaults:

```
config/codex.env  →  config/cgm.env  →  runtime/runtime.env  →  built-in defaults
   (BYO backend)      (host/account)      (machine paths, auto)   (scripts/env.sh)
```

Only `*.env.example` templates are committed; copy them to the real names and edit.
The `bin/` wrappers source `env.sh` for you. Values below are the defaults from
`scripts/env.sh` / `config/cgm.env.example`.

## Codex backend (workers + verifier)

| variable | default | meaning |
|---|---|---|
| `CODEX_BACKEND` | `api` | `api` (BYO OpenAI-compatible key) or `chatgpt` (your ChatGPT login) |
| `CODEX_HOME` | `runtime/codex-home` | codex auth/config home (gitignored) |
| `CODEX_API_BASE_URL` | — | (api) your OpenAI-compatible Responses endpoint |
| `CODEX_API_MODEL` | `gpt-5.5` | (api) backend model |
| `CGM_CODEX_API_KEY` | — | (api) key, **read at run time**, never stored in a file |

These live in `config/codex.env`. See `getting-started.md` §2 and
`scripts/setup-codex.sh`.

## Strategy consult (the system's brain)

| variable | default | meaning |
|---|---|---|
| `CGM_CONSULT_TRANSPORT` | `gpt_pro` | `gpt_pro` \| `claude_api` \| `claude_code` \| `off` |
| `CGM_CONSULT_API_KEY` | — | (gpt_pro) key for the OpenAI-compatible Responses API |
| `CGM_CONSULT_BASE_URL` | `https://api.openai.com/v1` | (gpt_pro) endpoint |
| `CGM_CONSULT_MODEL` | `gpt-5.5-pro` | (gpt_pro) model |
| `CGM_CONSULT_CLAUDE_CODE_MODEL` | `claude-fable-5` | (claude_code) model via the `claude` CLI |
| `CGM_CONSULT_CLAUDE_CODE_BIN` | `claude` | (claude_code) path to the CLI |
| `CGM_CONSULT_CLAUDE_CODE_MAX_WALL` | `1800` | (claude_code) hard wall-clock cap per consult (s) |
| `CGM_CONSULT_CLAUDE_CODE_PRICE_IN` | `10.0` | (claude_code) ledger estimate, USD per 1M input tokens |
| `CGM_CONSULT_CLAUDE_CODE_PRICE_OUT` | `50.0` | (claude_code) ledger estimate, USD per 1M output tokens |
| `CGM_CONSULT_CLAUDE_API_KEY` | — (falls back to `ANTHROPIC_API_KEY`) | (claude_api) BYO Anthropic API key |
| `CGM_CONSULT_CLAUDE_API_BASE_URL` | Anthropic default | (claude_api) only for a proxy |
| `CGM_CONSULT_CLAUDE_API_MODEL` | `claude-fable-5` | (claude_api) any Claude model |
| `CGM_CONSULT_CLAUDE_API_FALLBACK` | `claude-opus-4-8` | (claude_api) refusal-fallback model; `off` disables |
| `CGM_CONSULT_CLAUDE_API_PRICE_IN` | `10.0` | (claude_api) USD per 1M input tokens (real usage) |
| `CGM_CONSULT_CLAUDE_API_PRICE_OUT` | `50.0` | (claude_api) USD per 1M output tokens (real usage) |

- `gpt_pro` = a paid, per-token OpenAI-compatible model. `claude_api` = the
  Anthropic API via the native SDK (per-token, BYO key; cost from real usage).
  `claude_code` = your Claude subscription via the Claude Code CLI (`claude -p`).
  `off` = the main agent reasons on its own, no consult.
- The `claude_code` consult runs **isolated**: a throwaway cwd, no settings and no MCP
  servers loaded (`--setting-sources "" --strict-mcp-config` — needs a recent
  `claude` CLI), web-only tools, and the prompt on stdin (never argv, which is
  world-readable on a shared host). It sees the elaboration and the public web,
  nothing else.

## Models & reasoning effort

All three codex-exec sites (workers, verifier, paper/report renderers) resolve
binary + model + effort through the shared launcher, so names are unified. Neutral
defaults apply everywhere; per-service overrides win.

| variable | default | applies to |
|---|---|---|
| `CGM_CODEX_BIN` | `<repo>/bin/codex`, else `codex` on PATH | all codex calls |
| `CGM_CODEX_MODEL` | `gpt-5.5` | neutral default (all sites) |
| `CGM_CODEX_EFFORT` | `xhigh` | neutral default effort (all sites) |
| `CGM_VERIFY_MODEL` / `_EFFORT` | neutral | verifier — the correctness authority; keep effort at `xhigh` |
| `CGM_WRITE_PAPER_MODEL` / `_EFFORT` | neutral | paper renderer |
| `CGM_HUMAN_SUMMARY_MODEL` / `_EFFORT` | neutral | human-summary renderer |

## Ports (all loopback)

| variable | default | service |
|---|---|---|
| `VERIFY_PORT` | `8091` | verify service (`127.0.0.1`) |
| `DASHBOARD_PORT` | `8099` | read-only dashboard (`127.0.0.1`) |
| `CGM_VERIFY_URL` | `http://127.0.0.1:8091/verify` | where `fact_submit` posts |
| `VERIFY_HOST` | `127.0.0.1` | verify bind host (keep loopback — see security doc) |

## Runtime data locations (gitignored, under `runtime/`)

| variable | default | holds |
|---|---|---|
| `CGM_RUNTIME` | `<repo>/runtime` | the whole self-contained runtime |
| `CGM_AGENTS_ROOT` | `runtime/projects` | where `cgm new` puts projects |
| `VERIFIER_RESULTS_DIR` | `runtime/verify-runs` | per-verification run logs |
| `CGM_PY` | `runtime/venv/bin/python` (else system `python3`) | the engine's Python |

## Worker loop pacing (optional; engine defaults are sane)

| variable | default | meaning |
|---|---|---|
| `CGM_ROUND_HARD_TIMEOUT` | `14400` (4h) | per-round wall-clock cap |
| `CGM_MAX_ROUNDS` | `0` (unlimited) | round backstop |
| `CGM_MAX_CONSEC_FAILURES` | `5` | bail after N consecutive failed rounds (rc 124 is **not** a failure) |
| `CGM_MAX_CONSEC_TIMEOUTS` | `0` (never) | opt-in: bail after N consecutive hard-timeout rounds |
| `CGM_ROUND_BEAT` | `5` | seconds between rounds |
| `CGM_IDLE_ROUND_SECS` | `600` | a clean (rc 0) round shorter than this that neither banks a new fact nor attempts a `fact_submit` counts as idle |
| `CGM_IDLE_BACKOFF_BASE` | `60` | from the 2nd consecutive idle round, sleep base·2^(k−2) s instead of the beat (`0` disables) |
| `CGM_IDLE_BACKOFF_MAX` | `1800` | cap on that idle sleep; every wait wakes early on `.stop`, the deadline, or a new `TASK.md` |

## Stall detection (`cgm status` soft signals; read by `watchdog.sh`)

| variable | default | meaning |
|---|---|---|
| `CGM_TIMEOUT_STREAK_ALARM` | `2` | consecutive hard-timeout rounds before the `timeouts?` label |
| `CGM_FACT_STALE_ALARM` | `2 ×` the hard timeout | how long with no NEW fact counts as no progress |

Both conditions must hold for `timeouts?`, so a slow but **productive** worker is
never flagged; `0` for the streak disables the label.

## Rendering & misc

| variable | default | meaning |
|---|---|---|
| `CGM_CHROME_BIN` | (auto-detect) | headless Chrome/Chromium for human-summary PDF |
| `TEX_ENGINE` | `pdflatex` | write-paper LaTeX engine (`xelatex`/`lualatex`/`tectonic`) |
| `CGM_WRITE_PAPER_RUN_LOG` | on | per-call write-paper diagnostic logs (`0` disables) |
| `CGM_PAPER_VERIFY_WHOLE_DOC_CAP` | `700000` | char budget for one whole-paper math-verify call; over it the tool reports `too_large` (the main agent decomposes — the tool never auto-splits) |

## LaTeX-git push (write-paper deliver, optional)

In `config/latex-git.env` (gitignored): `LATEX_GIT_URL`, `LATEX_GIT_TOKEN`, and
optional `LATEX_GIT_AUTHOR_NAME` / `_EMAIL`. Pushing outward is an operator-gated
action.

---

Ports and the verify HTTP contract are **pinned** cross-module interfaces
(`../ARCHITECTURE.md` §4) — do not renumber `8091`/`8099` without changing both
ends. See `operations.md` to run the services and `cli-and-tools.md` for the
commands that use these.
