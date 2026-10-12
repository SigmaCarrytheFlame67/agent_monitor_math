# cgm/gateway — role-gated MCP server (the permission gate)

The **only sanctioned door to the truth stores.** A stdio MCP server (`cgm-core`)
whose exposed tools depend on the caller's role — permission is enforced by *which
tools a role can even see*, not by prompt convention.

```
cgm/gateway/
  server.py            the role-gated MCP tools + the fact_submit write-gate; build_app(role)
  roles.py             ROLE_TOOLS — the role→tools table (the security surface)
  __main__.py          `python -m cgm.gateway` → build_app().run() (role from CGM_ROLE)
  tests/test_gateway.py
```

## The role table (`roles.py`)

| role | tools |
|---|---|
| worker | `gm_add gm_search gm_get fact_submit fact_search search_arxiv_theorems` |
| main | `gm_add gm_search gm_get fact_search fact_revoke search_arxiv_theorems` (**no `fact_submit`**) |
| verifier | `search_arxiv_theorems` only (read-only) |

Ungated tools are **physically absent** from the surface. Unknown, mis-typed, or
*unset* role → **fail-closed** to the verifier set; the full dev set requires the
explicit `CGM_ROLE=all`.

## Memory retrieval (`gm_search` → `gm_get`)

`gm_search(query, kinds?, limit_per_kind?, project?)` searches full entries with
unchanged BM25 ranking and per-kind limits, but returns `view: "summary"` with
`results_by_kind[kind].results = [{score, entry}]`. Each entry contains ID, kind,
author, timestamp, verifiable/status/fact_id, verdict when present, and up to 600
characters of the original claim (`claim_truncated` indicates a cut). Evidence,
glossary, links, reports and other extra fields are omitted; no LLM summarizes.

`gm_get(entry_ids=[...], project?)` returns `{entries, missing_ids}`: selected full
entries across kinds, including all extra fields and current status, in requested
order with duplicate IDs removed. Use the same project as search. Batch selected
IDs into one call; read full evidence/reports/guidance before relying on them.
Both tools are available to worker, main and curator. Storage is unchanged.

## The write-gate (`fact_submit`, in `server.py`)

Before calling the verifier, reject already-revoked predecessors with
`accepted=false`, `verdict="error"`, `error="predecessor_revoked: ..."`, and repair
hints. No verification verdict exists yet, so no verification trace is written.
The same predecessor check runs again at fact-write time to catch revocation
during verification.

The single path a fact enters truth: (1) call the verify service
(`CGM_VERIFY_URL`); (2) **write the fact iff `verdict == "correct"`**; (3) **always**
trace the verdict to global memory. Service unreachable / non-dict body → clean error,
nothing written. The gate trusts the verdict string; it does not recompute it.

## Launched by

`bin/cgm-mcp` (role=main, for Claude Code via `.mcp.json`); each worker's
`.codex/config.toml` (role=worker); the verify launcher injects it (role=verifier) so
the judge can call `search_arxiv_theorems`. Config (`CGM_PROJECT_DIR`,
`CGM_AGENTS_ROOT`, `CGM_VERIFY_URL`, role, author) is read at **call time**.

## Pinned interfaces (ARCHITECTURE §4 — change both ends together)

The tool set + role table; `python -m cgm.gateway` launch; the verify HTTP seam.

## Tests

`python -m pytest cgm/gateway/` (offline; the verify call is stubbed).
