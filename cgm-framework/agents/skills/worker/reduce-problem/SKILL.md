---
name: reduce-problem
description: Reduce the current goal P to a simpler, more standard, or better-understood goal Q together with an explicit bridge argument Q => P, so that solving Q settles P. Use when the current goal resists direct attack but resembles (or can be transported into) a problem class with established structure or tools, and you can articulate why Q is genuinely easier.
---

# Reduce Problem

Replace the current goal P by a simpler goal Q **plus a proved bridge Q ⇒ P**. A
reduction without a bridge, or with a bridge as hard as P itself, is not
progress.

## Input Contract

Read:

- the current goal P (the project target or an assigned subgoal — quote it exactly)
- `master_guidance` and your `TASK.md` (a reduction must serve your lane)
- `fact_search` results: existing facts that already bridge or partially bridge
- `gm_search` over `dead_end`/`obstacle`: reductions that already failed

## Procedure

1. State the candidate reduced goal Q precisely and self-containedly (every
   symbol defined; same rigor as a fact statement).
2. Write the **bridge argument** Q ⇒ P in full. This is mandatory — proposing Q
   without the bridge is not a reduction.
3. Justify the **complexity drop**: name what structure Q has that P lacks
   (standard problem class, known technique applies, fewer coupled quantifiers,
   smaller parameter space, …). If you cannot name it, abort and log.
4. Honesty checks (each failure kills the reduction):
   - Is the bridge itself as hard as P? Then nothing was gained.
   - Is Q secretly equivalent to P with no clearer structure? Then this is at
     best a reformulation — switch to `$reformulate-problem`.
   - Does the bridge quietly strengthen hypotheses or weaken the conclusion of
     P? The project target is sacred; a reduction must recover P exactly.
5. **Land the bridge in the fact graph**: submit the implication "Q ⇒ P" as a
   self-contained unconditional fact via `$verify-proof` (this is a legitimate
   fact — an implication statement proved outright, not a conditional
   assumption). Only a verified bridge may be built upon.
6. Publish Q as the new working subgoal (a `plan` finding) and continue the
   loop on Q (typically via `$propose-subgoal-decomposition-plans` or
   `$direct-proving`).
7. When Q later becomes a fact, derive P by citing the bridge fact and the Q
   fact — P itself must still land as its own verified fact; the original
   target statement remains the final deliverable.

## Output Contract

Publish to global memory with `gm_add` (kind `plan`): `claim` = the reduction
("attack P via Q"), `evidence` = the bridge argument sketch + the complexity-drop
justification, plus:

```json
{
  "original_goal": "P verbatim",
  "reduced_goal": "Q verbatim",
  "bridge_fact_id": "filled after $verify-proof accepts the bridge",
  "why_simpler": "...",
  "known_tools_for_Q": ["..."]
}
```

If the reduction is abandoned at step 4, publish the reason as `dead_end` or
`obstacle` so siblings do not re-derive it.

## Graph edges (declare with `graph_link` at publish time)

- the reduction plan `addresses` the original goal node P.
- once the bridge fact verifies: `reduces-to` from P to Q, with the bridge
  `fact_id` in the rationale — closure of Q + the bridge closes P; Q dying does
  NOT kill P (other routes may exist).

## Tools

- `gm_add` / `gm_search` (publish the plan; recall failed reductions)
- `fact_search` (existing bridges and partial results toward Q)
- `search_arxiv_theorems` and Codex built-in web search (locate the standard
  problem class / known results about Q)

## Failure Logging

If no reduction with a genuine complexity drop is found, append:

- `events.event_type="reduction_not_found"`
