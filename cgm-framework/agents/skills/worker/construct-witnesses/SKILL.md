---
name: construct-witnesses
description: Prove an existential goal (exists x with P(x)) by producing an explicit, recursive, algorithmic, or indirect construction of a witness and then verifying every required property of it. Use when the goal (or a subgoal) asserts existence of an object — a matrix, factorization, coloring, sequence, function, embedding, certificate. A plausible-looking candidate is not a witness until every condition is checked.
---

# Construct Witnesses

Settle ∃x P(x) by building x₀ and **verifying P(x₀) clause by clause**. The
skill's discipline is the hard separation between *candidate* and *verified
witness*.

(For exploratory examples and refutations, the sibling skills
`$construct-toy-examples` and `$construct-counterexamples` apply; this skill is
for constructions that discharge an actual existence goal.)

## Input Contract

Read:

- the existential goal, with P(x) split into an explicit checklist of clauses
  (domain conditions, boundary conditions, compatibility conditions, target
  properties, quantitative bounds)
- `fact_search`: verified facts supplying building blocks or partial
  constructions
- `gm_search` over `example`/`counterexample`: constructions already tried
- literature via `$search-math-results`: standard construction templates
  (explicit formulas, greedy/recursive schemes, probabilistic existence,
  algebraic or duality-based witnesses)

## Procedure

1. Write the full **property checklist** for P(x) before constructing anything.
   Hidden clauses (well-definedness, membership in the right space, edge cases
   at boundary parameters) go on the list too.
2. Choose a construction mode and build the candidate x₀:
   - explicit closed form;
   - recursive/inductive construction (state the invariant maintained at every
     step);
   - algorithmic construction (argue termination and correctness);
   - indirect existence (probabilistic method, pigeonhole, duality,
     compactness — in the last case follow `$apply-compactness`).
3. **Verify every clause of the checklist against x₀**, one by one, with an
   explicit argument each. No clause may be waved through; unverified clauses
   are exactly what the verifier and the P5 prohibition will reject.
4. If a clause fails: record what breaks (an `obstacle` or informative
   `example`), repair or re-choose the construction, and re-run the full
   checklist — a repaired candidate is a NEW candidate.
5. Only after all clauses pass, write the self-contained statement
   ("there exists x with ⟨properties⟩, witnessed by ⟨construction⟩") and
   submit via `$verify-proof`. The proof must contain the construction AND the
   clause-by-clause verification.

## Output Contract

While the candidate is unverified, publish to global memory with `gm_add`
(kind `proof_attempt`): `claim` = the existence statement targeted,
`evidence` = the construction so far + checklist status, plus:

```json
{
  "existence_goal": "...",
  "construction_mode": "explicit | recursive | algorithmic | indirect",
  "candidate": "...",
  "checklist": [{"clause": "...", "status": "verified | open | failed"}],
  "witness_fact_id": "filled when $verify-proof accepts"
}
```

An informative candidate that fails P but clarifies structure is published as
an `example` finding (see `$construct-toy-examples` output shape).

## Graph edges (declare with `graph_link` at publish time)

- the construction plan `addresses` the existential goal.
- one `decomposes-into` edge per clause of the property checklist (step 1),
  all sharing an `and_group` and tagged `technique: witness` — every clause is
  a member, so a candidate can never close the goal with a clause unchecked,
  and a repaired candidate opens a NEW group (it is a new candidate).
- each published candidate/attempt `supports` (or, if it exposes a defect,
  `blocks`) the existential goal node.
- on success: `promoted-to` from the finding to the witness `fact_id`.

## Tools

- `gm_add` / `gm_search` (publish attempts; recall prior constructions)
- `fact_search` (verified building blocks to cite as predecessors)
- `search_arxiv_theorems` and Codex built-in web search (construction templates)

## Failure Logging

If no candidate survives its checklist, append:

- `events.event_type="witness_construction_failed"`
