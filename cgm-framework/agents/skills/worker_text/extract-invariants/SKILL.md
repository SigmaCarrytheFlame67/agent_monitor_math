---
name: extract-invariants
description: Find a quantity preserved (or monotone) under all legal operations, transformations, or evolution steps of the problem, prove its invariance operation by operation, and then use it to distinguish objects, rule out states, or constrain classifications. Use when the problem involves moves/operations/dynamics and the goal is an impossibility, a separation, or a conserved constraint. Observing invariance on finitely many examples is not a proof.
---

# Extract Invariants

Three obligations, in order: **enumerate the legal operations**, **prove
invariance under each**, **derive an actual conclusion from the invariant**.
Skipping any of the three is the standard way invariant arguments fail.

## Input Contract

Read:

- the goal, and the system's legal operations/moves/transformations (from the
  problem statement or the active reduction — quote them exactly)
- `gm_search` over `example`/`counterexample`: computed instances whose values
  suggest candidate invariants
- `fact_search`: invariance lemmas already verified
- literature via `$search-math-results`: standard invariants for this object
  class (parity, weightings, potentials, monovariants, spectra, homology
  classes, norms, entropy)

## Procedure

1. **List the legal operations exhaustively.** The invariance claim is
   quantified over this list; a missing operation silently voids the whole
   argument. If the operation set is unclear, that is the first subgoal.
2. Propose the candidate invariant I (or monovariant — monotone rather than
   constant; say which, and in which direction).
3. **Prove invariance/monotonicity under EACH listed operation separately.**
   Checking a few instances is evidence for the candidate, never the proof —
   each operation gets its own argument. Ill-definedness of I on the state
   space is a clause to verify too.
4. Compute I on the relevant boundary data: initial state(s), target state(s),
   the objects to be distinguished.
5. **Derive the conclusion**: state exactly how the invariant produces the
   result — two objects with different I-values cannot be related by legal
   operations; a state with the wrong I-value is unreachable; the
   classification is constrained to level sets of I. An invariant with no
   derived conclusion is trivia — log and move on.
6. Land in the verified results file via `$verify-proof`:
   - the invariance lemma ("I is preserved by every operation in ⟨list⟩") —
     one fact, or one per operation family when proofs differ materially;
   - the application fact (the impossibility/separation/constraint), restating
     the invariance lemma and the boundary computations inline at the point of
     use, with their Fact numbers.

## Output Contract

Publish to global memory with `gm_add` (kind `conclusion` once proved, `plan`
or `direction` while a candidate): `claim` = the invariant and what it rules
out, `evidence` = per-operation proofs (or candidate rationale), plus:

```json
{
  "legal_operations": ["..."],
  "invariant": "...",
  "invariant_type": "conserved | monotone-increasing | monotone-decreasing",
  "per_operation_status": [{"operation": "...", "status": "proved | open | fails"}],
  "boundary_values": "...",
  "conclusion_derived": "...",
  "invariance_fact_numbers": ["filled as lemmas verify"],
  "application_fact_number": "filled when the application verifies"
}
```

If some operation breaks the candidate, that is informative: publish the
breaking instance as `example` (or `dead_end` if it kills the whole approach).

## Graph edges (declare with `graph_link` at publish time)

- the invariant plan `addresses` the goal; one `decomposes-into` edge per
  per-operation invariance lemma plus one for the application step, sharing an
  `and_group`, tagged `technique: invariant`.
- an operation that breaks the candidate: the breaking `example` `refutes` the
  invariant plan node.

## Tools

- `gm_add` / `gm_search` (publish; recall instance computations)
- `fact_search` (verified invariance lemmas)
- `search_arxiv_theorems` and Codex built-in web search (standard invariants of
  the object class)

## Failure Logging

If no candidate survives all listed operations, append:

- `events.event_type="invariant_not_found"`
