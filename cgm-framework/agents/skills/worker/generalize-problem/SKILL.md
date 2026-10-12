---
name: generalize-problem
description: Prove a more general or stronger statement P' of which the current goal P is a special case, when the generalization is genuinely easier to prove — e.g. it enables induction, exposes symmetry, frees a parameter, or removes distracting specifics. Use when direct attacks on P keep tripping over its special form. Merely adding parameters or widening the object class without gaining new structure is not progress.
---

# Generalize Problem

Do not prove P directly; prove a stronger/more general P' and recover P as a
special case. The whole move stands or falls on **why P' is easier**.

## Input Contract

Read:

- the goal P (quote it exactly) and its assigned context in `TASK.md`
- `gm_search` over `dead_end`/`obstacle`: why direct attacks on P failed (the
  generalization should be designed to bypass exactly those failures)
- `fact_search`: partial results that already hold in more generality
- literature via `$search-math-results`: is the generalization already a known
  theorem? (Then cite it instead of proving it.)

## Procedure

1. State P' precisely, and state the specialization map: exactly which choice
   of parameters/objects in P' yields P.
2. **Justify why P' is easier (mandatory)** — name the mechanism:
   - induction becomes available (P' has a parameter to induct on);
   - symmetry/invariance appears that the special case breaks;
   - the generalized objects form a closed class under the operations the
     proof needs;
   - distracting numerical specifics are replaced by structural hypotheses.
   If the only change is "more parameters / fewer hypotheses / bigger class"
   with no named mechanism, abort and log — that is not a generalization, it
   is inflation.
3. Sanity-check with `$construct-counterexamples`: a too-strong P' may simply
   be false. Test the boundary of the generalization before investing rounds.
4. Attack P' through the normal loop (decompose, prove, verify). P' lands in
   the fact graph via `$verify-proof` like any result.
5. **Recover P explicitly**: submit the specialization P' ⇒ P as its own
   verified fact (citing the P' fact as predecessor). The instantiation
   argument — checking P's objects satisfy P''s hypotheses — is a real proof
   obligation, not a formality. The original target statement remains the
   final deliverable.

## Output Contract

Publish to global memory with `gm_add` (kind `plan`): `claim` = "prove P via
the generalization P'", `evidence` = the easiness mechanism + specialization
map, plus:

```json
{
  "original_goal": "P verbatim",
  "generalized_goal": "P' verbatim",
  "specialization_map": "...",
  "why_easier_mechanism": "induction | symmetry | closure | de-specialization | ...",
  "generalization_fact_id": "filled when P' is verified",
  "recovery_fact_id": "filled when P' => P is verified"
}
```

If step 2 fails (no mechanism) or step 3 refutes P', publish `dead_end` with
the refuting instance so siblings do not repeat the over-generalization.

## Graph edges (declare with `graph_link` at publish time)

- `generalizes` from P' to P, with the specialization fact id in the rationale
  once verified. Closure flows DOWN (P' closed + specialization ⇒ P closed);
  refutation flows UP (P refuted ⇒ P' refuted); P' refuted says nothing
  about P.
- the generalization plan `addresses` P.

## Tools

- `gm_add` / `gm_search` (publish; recall failed direct attacks)
- `fact_search` (partial results already general)
- `search_arxiv_theorems` and Codex built-in web search (known general theorems)

## Failure Logging

If no generalization with a named easiness mechanism survives, append:

- `events.event_type="generalization_without_mechanism"`
