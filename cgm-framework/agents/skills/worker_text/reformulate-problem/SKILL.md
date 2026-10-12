---
name: reformulate-problem
description: Rewrite the current goal P as a mathematically equivalent statement Q (P <=> Q) whose structure is more transparent or better matches known tools — dual forms, variational forms, combinatorial/probabilistic/geometric re-encodings. Use when the goal's current phrasing obscures structure that an equivalent phrasing would expose. Both directions must be established; a one-directional rewrite is a reduction, not a reformulation.
---

# Reformulate Problem

Replace the working form of a goal by an **equivalent** one (P ⟺ Q) that makes
structure visible or unlocks known tools. Equivalence means both directions.

## Input Contract

Read:

- the goal P to reformulate (quote it exactly)
- the symbol definitions used in the verified results you build on (read their
  entries in `fact_text/facts.md`) — a reformulation must reuse the project's
  terminology, not fork it, and must define every non-universal symbol inside
  its own statement/proof text (universal notation — Z, Q, R, C, floor/ceil,
  gcd/lcm, intervals, standard Greek parameter names — needs no definition)
- `gm_search` over `conclusion`/`plan`: reformulations already in play
- `fact_search`: equivalences already verified (do not re-prove)

## Procedure

1. State Q precisely and self-containedly.
2. Prove **both directions**, P ⇒ Q and Q ⇒ P, explicitly. Do not wave at
   "clearly equivalent" — the verifier will reject it, and P5-style vague
   gestures are prohibited.
3. **Direction audit (mandatory)**: if only Q ⇒ P is actually provable, this is
   a reduction — switch to `$reduce-problem` and follow its bridge discipline
   instead. Do not label a one-directional rewrite as a reformulation.
4. Justify the gain: what does Q expose that P hides (a duality, a variational
   principle, an induction-friendly shape, a known problem template)? A rewrite
   with no structural gain is churn — abort and log.
5. **Land the equivalence in the verified results file (`fact_text/facts.md`)**:
   submit "P ⟺ Q" via
   `$verify-proof` — either as one self-contained fact containing both
   directions, or as two facts (P ⇒ Q, Q ⇒ P) when the directions have
   genuinely different proofs. Only then work on the Q form.
6. Downstream work may use the equivalence result — restating it where it is
   used, with its number (e.g. "by Fact 12: <its statement>"), so each
   submitted proof is fully self-contained — and target Q; any final
   result must still be discharged back to the original target statement (the
   project goal is sacred and is what ultimately lands as the target fact).

## Output Contract

Publish to global memory with `gm_add` (kind `conclusion` once the equivalence
is proved, or `plan` while it is still a proposal): `claim` = "P is equivalent
to Q", `evidence` = both direction arguments (or the proposal rationale), plus:

```json
{
  "original_form": "P verbatim",
  "reformulated_form": "Q verbatim",
  "equivalence_fact_numbers": ["Fact number(s) after $verify-proof accepts"],
  "structural_gain": "...",
  "tools_unlocked": ["..."]
}
```

If the reformulation collapses at step 3 or 4, log the reason as `dead_end`
(failed route) or hand off to `$reduce-problem` (one-directional case).

## Graph edges (declare with `graph_link` at publish time)

- once the equivalence verifies: one `equivalent-to` edge between P and Q,
  referencing the equivalence result(s) as "Fact N" in the rationale. Semantics are bidirectional —
  closure AND refutation propagate both ways; the curator keeps the two nodes
  mirrored.

## Tools

- `gm_add` / `gm_search` (publish; recall prior reformulations)
- `fact_search` (already-verified equivalences)
- `search_arxiv_theorems` and Codex built-in web search (standard equivalent
  forms and dualities in the literature)

## Failure Logging

If no equivalence with a genuine structural gain is found, append:

- `events.event_type="reformulation_without_gain"`
