# Notice: adapted Ansatz / continual-graph-memory material

The files in `agent_monitor/cgm/` contain prompts and data contracts **adapted
from** the Ansatz project ("continual-graph-memory", Python package `cgm`).

- Source: user-supplied `continual-graph-memory-main.zip`
- Archive SHA-256:
  `d12630af90d13688a950c8c8ea14c2cf5ac6a52afbc4e69d4c876dc3944cb825`
- Copyright 2026 FrenzyMath
- License: Apache License, Version 2.0 (a copy is in `LICENSE` in this
  directory). The archive contains no `NOTICE` file. Its `results/*`
  manuscripts carry no license grant and are **not** included here.

No upstream code is imported, vendored, or executed. ProvingConsole's runners
(`agent_monitor/runners/math_harness_runner.py`,
`agent_monitor/runners/analyze_runner.py`) and graph validator
(`agent_monitor/engine_graph.py`) are original code that follow the upstream
on-disk formats.

## Adapted material and changes

| ProvingConsole location | Upstream source (inside the archive) | Changes |
| --- | --- | --- |
| `prompts.py` `STRATEGIST_SYSTEM` | `cgm/strategy/transport.py` `ADVISOR_SYSTEM` (verbatim opening); `agents/contracts/main_agent.md` strategy and dispatch rules; `agents/skills/worker/propose-subgoal-decomposition-plans` and `obtain-immediate-conclusions` (Procedure); technique triggers from `agents/contracts/worker.md` | Tool-free operating context added; consult, assign and CLI mechanics, human interviews and all tool/file/memory references removed; output changed to a small JSON plan contract with a `technique` per plan. |
| `prompts.py` `WORKER_SYSTEM` | `agents/contracts/worker_text.md` (writing discipline, invariants); `agents/skills/worker/verify-proof` ("ugly-proof" rules); `agents/contracts/worker.md` Step 4 (repair loop) | Tools, MCP calls, web search, downloads, local memory and `TASK.md` removed; "Fact N" citations replaced by local lemma labels; a tool-free citation rule added; output changed to fixed delimited Markdown blocks (LEMMA / THEOREM / FINDING / STATUS). |
| `prompts.py` `TECHNIQUE_PROCEDURES` | Procedure sections of `agents/skills/worker/{direct-proving, localize-problem, extract-invariants, select-extremal-objects, apply-compactness, construct-witnesses, reduce-problem, reformulate-problem, generalize-problem}` | Skill and tool invocations (`$verify-proof`, `gm_add`, `external_refs`, ...) rewritten as plain instructions; lightly condensed. |
| `prompts.py` `VERIFIER_SYSTEM` | `agents/contracts/verifier.md` (Objective, Input Contract, Steps 1, 2, 4, 5, verdict rule, Output JSON Contract, Hard Prohibitions P1/P3/P5/P6 and the P3 chain check); `agents/skills/verify/verify-sequential-statements` (steps 1-8); `synthesize-verification-report` (steps 1-5) | Step 3 (arXiv lookup) replaced by a tool-free citation policy (named standard theorems only with restated statements; unrestated citations are P5 critical errors; the verifier never claims it checked a reference); Step 6 (file write), run ids and fact-graph file reads removed; 16-hex `fact_id` citations replaced by local lemma labels whose statements are supplied inline. |
| `prompts.py` `CURATOR_SYSTEM`, `curation.py` | `agents/contracts/curator.md` (maintenance, deterministic propagation rules, scoring discipline) | One response-only scoring pass over a serialized snapshot; the propagation algebra is executed by code; the target-theorem assembly is treated as an implicit AND-group member; tool surface, event loop and retrospective removed. |
| `schema.py` | `cgm/core/schema.py` (`GLOBAL_KINDS`, `compute_fact_id`), `cgm/core/factgraph.py` (`serialize_fact`), `cgm/core/exploring_graph.py` (`EDGE_TYPES`, `TECHNIQUES`, annotation statuses) | Re-implemented; global-memory entry and edge ids add a per-file ordinal because one generation shares one timestamp. |
| `agent_monitor/analyze/prompts.py` `ANALYZE_VERIFIER_SYSTEM` | Derived from `VERIFIER_SYSTEM` above (hence from `agents/contracts/verifier.md`, `verify-sequential-statements` and `synthesize-verification-report`) | Checks one result of an existing manuscript and does not repair it; inputs are the manuscript's statement, proof, cited predecessor statements, notation and bibliography, all delimited as untrusted data; Step 3 and Hard Prohibition P5 now let a specific external result (bibliography key plus theorem number) be used and listed under `external` instead of being a critical error, while vague "well known" gestures remain gaps; verdict `checked`, `gap`, `error` or `unclear`; output contract adds `claim`, `role`, `depends_on`, `missing_dependencies`, `external`, `notes` and `confidence` and drops `repair_hints`. |

Model-verifier acceptance produced by these pipelines, and every Analyze
verdict, is never a Lean or other formal certificate.
