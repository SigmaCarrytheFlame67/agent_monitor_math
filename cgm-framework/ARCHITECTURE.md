# Ansatz — Architecture

Ansatz turns the Rethlas single-problem proof engine (a codex agent plus proving
skills, inherited rather than redesigned) into a multi-agent, long-running,
strategy-steered research system, and renders verified results into papers and
human progress reports. This is the as-built map: the layered model, the folder
layout, the invariants, and the pinned cross-module contracts.

For the main agent's operating contract, see `CLAUDE.md`
(→ `agents/contracts/main_agent.md`).

---

## 1. Layered model

```
operator → ① orchestration (main agent + cgm CLI)   — conducts, never does math
              ② strategy   (elaboration → consult → master_guidance)
              ③ execution  (worker swarm; each round = one codex session running the Rethlas proving skills)
   gm_* │         │ fact_submit
        ▼         ▼
   ⑤ truth      ④ verification (cold-start codex judge; correct ⟺ no critical_errors AND no gaps)
   (fact graph + memory)   — a fact exists iff the verifier accepted it
        ▲
        │ every read/write goes through …
   ⑥ gateway (role-gated MCP: 13 tools; main has NO fact_submit; verifier read-only; curator annotation-only)

cross-cutting: ⑦ observability (dashboard · theorem-search · human-summary · initialize)
               ⑧ ops/runtime (bootstrap · services · doctor · config)
bottom (inherited, don't redesign): Rethlas proof core = codex + worker proving skills
output: write-paper (publication) · human-summary (progress report) — each rendered by an isolated codex
```

---

## 2. Folder layout

```
Ansatz/
├─ ARCHITECTURE.md              this file (map + invariants + interface contract)
├─ README.md   pyproject.toml   top-level intro + the installable `cgm` package
├─ .gitignore  .mcp.json        MCP wiring: the `cgm` gateway + the `write-paper` and `human-summary` services
├─ config/                      env templates (BYO key; only *.env.example committed)
├─ cgm/                       THE ENGINE (installable Python package)
│  ├─ core/                     ⑤ truth: schema · factgraph · global/local memory · bm25 · glossary
│  ├─ gateway/                  ⑥ role-gated MCP: 13 tools · role table (roles.py)
│  ├─ verify/                   ④ verification HTTP service · prechecks · cold-start codex launcher
│  ├─ execution/                ③ worker swarm: round loop · project/worker lifecycle + layout
│  ├─ strategy/                 ② consult gateway (gpt_pro|claude_api|claude_code|off transport)
│  ├─ orchestration/            ① the `cgm` CLI verbs
│  ├─ integrations/             arXiv theorem search (Matlas)
│  ├─ observability/            read-only dashboard
│  ├─ authoring/                shared main-only renderer primitives: the one-shot isolated codex driver + common helpers
│  ├─ write_paper/              write-paper MCP service: assembler + tools (drives `cgm.authoring`)
│  └─ human_summary/            human-summary MCP service: scrubbing assembler + tool (drives `cgm.authoring`)
├─ agents/                      AGENT CONTRACTS + CODEX-FACING SKILLS (data, not Python)
│  ├─ contracts/                main_agent.md · worker.md · verifier.md
│  └─ skills/
│     ├─ worker/                17 proving skills (9 workflow, inherited from Rethlas; 8 techniques)
│     ├─ verify/                3 verify skills
│     └─ write-paper/           paper role prompts + house style (embedded by the write-paper MCP)
├─ .claude/skills/              MAIN-AGENT SKILLS (Claude Code auto-discovers)
│  ├─ elaboration/  consult/  human-summary/  initialize/
│  └─ write-paper/              the recipe SKILL.md + driver/ scripts + templates/
├─ bin/                         thin wrappers: cgm · cgm-mcp · write-paper-mcp · human-summary-mcp · codex · consult
├─ scripts/                     bootstrap · doctor · services · env · setup/check-codex · start-verify/-dashboard · recover · install-tex
├─ docs/                        human docs: getting started · concepts · operating guide · security & trust · …
└─ examples/                    unattended-ops examples + a toy project
```

---

## 3. Design invariants (must not regress)

1. Three memory tiers, one correctness boundary: local (private) → global
   (shared awareness) → fact graph (the only truth). A proof may build only on
   `fact_id`s; global memory is never a correctness source.
2. Permission is enforced by which tools a role can even see (the gateway role
   table), not by prompt convention. `main` cannot `fact_submit`; `verifier` is
   read-only. The same discipline covers the two renderer MCP services: their
   tools exist only at `CGM_ROLE=main`, so a curator or worker session that
   loads them gets an empty tool list rather than a contract asking it to abstain.
3. The verifier is the sole write-gate. A fact exists only if a `correct` verdict
   came back; the gate lives in the `fact_submit` code path, not in prose.
4. Content-addressed, cascade-revocable fact graph. `fact_id` hashes content
   (problem_id + predecessors + glossary_introduces + statement + proof);
   `external_refs` is deliberately excluded so the paper pipeline can rewrite
   citations without breaking the DAG.
5. Autonomy and resumability. Workers run detached; a "round" continues from
   persisted memory rather than adding one increment, so no single crash loses
   verified work.
6. The strategy consult is the brain. Between rounds the main agent consults a
   top-tier reasoning model (gpt-5.5-pro over the `gpt_pro` transport, or
   claude-fable-5 over the `claude_api` / `claude_code` transports) to set direction;
   its reply becomes the swarm's `master_guidance`. Transport is `gpt_pro` (default),
   `claude_api`, `claude_code`, or `off` (no key — the main agent reasons on its own). The consult is not
   optional — it is how the swarm gets steered.
7. Portable and BYO. No hardcoded absolute paths, no committed secrets; keys come
   from gitignored `config/*.env` (templates committed as `*.example`).
8. Clean author context. Any agent that produces an artifact for an outside
   audience (a paper, a human report) is a fresh isolated codex fed a scoped,
   machinery-free prompt, never the orchestrator's own contaminated window. It
   cannot leak `fact_id`s or swarm vocabulary it never received.

---

## 4. Interfaces & ports — the coordination contract

> **Rule:** these rows are the seams where two modules meet. If a change touches a
> row, update both ends in the same change. Ports and contract shapes are pinned:
> one side must treat the other's contract as fixed, and must not change a port or
> interface unilaterally.

**Network ports (loopback — do not renumber):**

| port | service | producer → consumer |
|---|---|---|
| 8091 | verify `/verify`, `/health` | `cgm.gateway` `fact_submit` → `cgm.verify` (via `CGM_VERIFY_URL`) |
| 8099 | dashboard | operator browser → `cgm.observability` (read-only) |

**Cross-module contracts (both ends must agree):**

| contract | pinned shape | ends |
|---|---|---|
| MCP tool set + role gating | 13 tools; `roles.py` `ROLE_TOOLS` (main has NO `fact_submit`; verifier read-only; curator annotation-only) | `cgm.gateway` ↔ worker/main/verifier/curator agents |
| renderer role gate | the two renderer services expose their tools ONLY at `CGM_ROLE=main` (fail-closed in `build_app`); `.mcp.json` passes the session's role through instead of hard-coding `main`, so a curator session loading them sees an empty tool list | `cgm.authoring.common.role_allows_tools` ↔ `.mcp.json` · `bin/{write-paper,human-summary}-mcp` |
| MCP launch | `python -m cgm.gateway` + `CGM_ROLE` env | `cgm.verify` launcher · worker `.codex/config.toml` · `.mcp.json` (main) → `cgm.gateway` |
| memory retrieval | `gm_search` keeps full-content BM25 ranking but returns metadata + claim previews (600 chars, `claim_truncated`); `gm_get {entry_ids,project?}` returns full `entries` + `missing_ids`; both available to worker/main/curator | `cgm.gateway` ↔ worker/main/curator contracts + query-memory/elaboration skills |
| verify HTTP | `POST /verify {statement,proof}` → `{verification_report,verdict,repair_hints}`; verdict ⟺ no critical_errors & no gaps | `cgm.gateway.fact_submit` ↔ `cgm.verify` |
| fact id inputs | `problem_id + sorted(predecessors) + sorted(glossary) + normalized(statement,proof)`; **external_refs EXCLUDED** | `cgm.core` ↔ everyone (write-paper reads `external_refs`) |
| global-memory kinds | the 11 `GLOBAL_KINDS` (incl. `master_guidance`/`elaboration`/`verification`) | `cgm.core` ↔ agents · strategy · consult |
| consult JSON envelope | `{transport,reply,usage,cost_usd,…}` | `cgm.strategy` CLI ↔ consult skill |
| write-paper prompt assets | codex role prompts + style read from `agents/skills/write-paper/` (via `CGM_WRITE_PAPER_SKILL_DIR`) | `cgm.write_paper` assembler ↔ `agents/skills/write-paper/` |
| env-var contract | `CGM_* / CODEX_* / VERIFY_* / CONSULT_*` names; the codex CALL + env (bin/model/effort/PATH/`exec` prefix) is resolved through the shared `cgm.codex` launcher: neutral `CGM_CODEX_BIN` / `CGM_CODEX_MODEL` / `CGM_CODEX_EFFORT` + per-service `CGM_{VERIFY,WRITE_PAPER,HUMAN_SUMMARY}_{MODEL,EFFORT}` overrides | `cgm.codex` + `config/` + `scripts/env.sh` ↔ every codex-exec site (`cgm.execution.loop` · `cgm.verify.launcher` · `cgm.authoring.driver`) |
