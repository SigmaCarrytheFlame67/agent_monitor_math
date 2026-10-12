---
name: localize-problem
description: Split a global goal into a family of local problems (at a point, neighborhood, scale, frequency band, prime, coordinate patch, or block) AND identify the local-to-global mechanism that reassembles the local solutions. Use when the goal has a natural local structure that is analyzable piece by piece. Local results alone are not the theorem — without a named gluing mechanism the localization is incomplete.
---

# Localize Problem

Turn `global problem ⟶ {local problems} + reassembly`. The skill has TWO
mandatory halves: the local family AND the local-to-global mechanism. Producing
only the first half is the classic way this move fails.

## Input Contract

Read:

- the global goal, and what "local" naturally means for it (points, scales,
  dyadic blocks, frequency bands, primes/places, coordinate patches,
  components, group elements)
- `fact_search`: local statements already verified (do not re-prove pieces)
- `gm_search` over `dead_end`: localizations whose reassembly already failed
- literature via `$search-math-results`: standard local-to-global machinery in
  this area

## Procedure

1. Define the localization precisely: the index set of local environments, the
   local statement L_i at each, and how the family covers the global object
   (exact cover, dyadic decomposition, partition, limit of scales, …).
2. **Name the local-to-global mechanism up front, before proving local pieces**
   — one (or a combination) of:
   - consistency/compatibility conditions between overlapping pieces;
   - a gluing/patching theorem (state which one and its hypotheses);
   - a covering argument with controlled multiplicity/overlap;
   - a partition of unity / decomposition of the identity;
   - compactness (finite subcover; then follow `$apply-compactness`);
   - summation/union bounds with loss accounting (track constants per piece);
   - absence of a topological/cohomological/arithmetic obstruction — say which
     obstruction group or invariant must vanish and why.
   If no mechanism can be named, the localization is decorative — abort, log,
   and try another split.
3. Budget the losses: if reassembly sums over pieces, state how many pieces
   and what per-piece loss the global bound can afford. A localization that
   wins locally and loses globally in the constants is a known trap.
4. Prove local statements through the normal loop; each self-contained L_i
   lands via `$verify-proof` as its own fact.
5. Prove the reassembly step itself as a fact ("if L_i holds for all i in ⟨index⟩
   and ⟨mechanism hypotheses⟩, then ⟨global statement⟩"), recording the
   mechanism's external theorem in full in the proof text (complete statement +
   paper_id / arXiv id / theorem_id) when it is published machinery.
6. Derive the global goal restating the local facts + the reassembly fact
   inline at the point of use, with their Fact numbers.

## Output Contract

Publish to global memory with `gm_add` (kind `plan`): `claim` = the
localization scheme, `evidence` = local family + mechanism + loss budget, plus:

```json
{
  "global_goal": "...",
  "local_environment": "points | scales | blocks | frequencies | primes | patches | ...",
  "local_statements": ["L_i schema"],
  "reassembly_mechanism": "gluing | covering | partition-of-unity | compactness | union-bound | obstruction-vanishing",
  "loss_budget": "...",
  "local_fact_numbers": ["filled as pieces verify"],
  "reassembly_fact_number": "filled when the mechanism fact verifies"
}
```

If reassembly fails after local pieces succeed, that is a high-value finding:
publish it as `obstacle` (the pieces are true but do not glue) with specifics.

## Graph edges (declare with `graph_link` at publish time)

- the scheme plan `addresses` the global goal.
- one `decomposes-into` edge per local statement AND one for the reassembly
  mechanism, all sharing an `and_group` and tagged `technique: localization` —
  the reassembly member is part of the AND group, so local pieces alone never
  close the parent.

## Tools

- `gm_add` / `gm_search` (publish the scheme; recall failed reassemblies)
- `fact_search` (verified local pieces)
- `search_arxiv_theorems` and Codex built-in web search (gluing/covering
  theorems, obstruction theory for this area)

## Failure Logging

If no local structure with a nameable reassembly mechanism exists, append:

- `events.event_type="localization_without_mechanism"`
