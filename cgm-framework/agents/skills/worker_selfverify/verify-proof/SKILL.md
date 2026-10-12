---
name: verify-proof
description: Review your own result against the verification rubric and, on your own acceptance, write it as a fact — via the fact_submit tool. Use for the full target theorem AND for every sharply-delimited intermediate result, lemma, construction, or formula you intend to build on. You are your own verifier.
---

# Verify Proof

**In this deployment you are your own verifier.** There is no separate
verification service and no second opinion: the verdict that decides whether a
result becomes a fact is the one *you* produce by reviewing your own draft.
Mathematics still requires 100% accuracy, so the review is the strongest
correctness check in the system — and it is only as strong as you make it.

**You review and write a fact through one tool: `fact_submit`.** It runs the
glossary-coverage check and writes the fact to the fact graph **iff the verdict
you pass in is `"correct"`** — there is no other way a fact enters the graph.

## When to submit

- **The full target theorem** — when you have assembled a complete proof of the
  whole problem (as a self-contained statement + proof, citing its predecessors by
  `fact_id`).
- **Every intermediate result you intend to USE downstream** — a lemma, a
  candidate construction, an arithmetic/closed-form claim, a saturation or
  local-to-global claim, any sharply-delimited step. **Adopting an unverified
  partial result as a building block is the single biggest correctness risk.** When
  in doubt, submit.

Do **not** build on an unreviewed finding from global memory. A `conclusion` /
`example` / `counterexample` there is awareness, not a brick — re-derive it as a
self-contained statement+proof and review it before relying on it.

## Before you submit — write an "ugly-proof" fact

A fact in the fact graph is written in **"ugly-but-rigorous"** form (the operator
may call it an **"ugly-proof"**). The one goal of
this form is that the fact is **mechanically checkable for correctness** by a
reader with no memory and no math intuition (an agent with no recall, a human, the
verifier). It is allowed — encouraged — to be **ugly**: redundant, machine-flavored,
verbose. It is **not allowed** to be ambiguous, vague, or context-dependent.
"Ugly" is the deliberate contrast with the polished arXiv paper (a separate
pipeline); here, only mechanical correctness matters.

Concretely, before you submit:

- **Self-contained.** A reader using only this fact + its declared predecessors +
  the project glossary can decide whether the math is correct. No appeal to chart
  positions, parse status, project history, or "as we know".
- **Define every symbol.** Each symbol used in the statement/proof is defined: in
  this fact's `glossary_introduces`, in a cited predecessor's glossary, in the
  project glossary, or in the **global glossary** of universal notation (Z, Q, R,
  C, floor/ceil, gcd/lcm, intervals, Greek parameter names). Don't redefine
  universal notation — `glossary_introduces` is for project-specific symbols only.
  Reuse the project's existing symbol for the same object. `fact_submit` returns
  `undefined_symbols` if you missed one.
- **Cite every dependency by `fact_id`** — never "by the result above", never the
  problem statement as a math source.
- **Every quantifier explicit; every introduced parameter (epsilon, k, …) carries
  an explicit range.**
- **No handwave** ("obviously", "easy to see", "routine", "analogously",
  "by some classical argument") and **no chart-position references** ("as above").
- **Avoid duplicates.** `gm_search` the fact graph / global memory (or read
  `fact_graph/facts/`) for an existing fact with the same statement; if one
  exists, cite its `fact_id` instead of re-proving it.

## The self-review — run it before every submission

Review the exact `statement` and `proof` you are about to submit, using the
verification skills, in this order:

1. `$verify-sequential-statements` — check each statement and subproof **in the
   order written**, logging every local issue (unjustified step, misapplied
   theorem, unproved sub-claim, undefined object, quantifier slip).
2. `$check-referenced-statements` — for every cited `fact_id`, read
   `fact_graph/facts/<id>.md` and confirm the cited fact really says what this
   step needs; resolve project symbols against `fact_graph/glossary.json`.
3. `$synthesize-verification-report` — synthesize the findings into one report
   and one verdict.

**The rubric is the standard one: `"correct"` iff the proof has no critical
errors AND no gaps.** A gap is a step you cannot fully justify from what is
written plus the cited facts — including one you personally believe but have not
written down. "I know how to fill this in" is a gap. "Routine" is a gap.

Review the draft as an adversary who is trying to find the flaw, not as the
author who wants it to pass. You wrote this proof, so you are the reader most
likely to read your own intent into a step instead of what the text actually
says — read only what the text says.

## Submit and repair

Call `fact_submit(statement, proof, self_verdict=..., self_review_report=...,
predecessors=[...], glossary_introduces={...})`, where `self_verdict` is exactly
`"correct"` or `"wrong"` and `self_review_report` is the report from step 3
(what you checked and what you found — it is stored as the fact's verification
trace). Read the result:

- `accepted: true, fact_id` — the fact is written. **Cite `fact_id`** downstream.
- `accepted: false, verdict: "wrong"` — your own review found a gap. This is the
  honest outcome, not a failure of the tool: revise using your report — resolve
  critical errors first, then all remaining gaps; do not assume the fix is local —
  change strategy or backtrack if needed; then re-review and resubmit.
- `verdict: "error"` — the submission was malformed (verdict not in the enum, or
  a missing/too-short review report); fix the call and resubmit.
- `accepted: true, write_error` (e.g. a predecessor was revoked) — the fact was not
  written; re-prove or avoid that predecessor.

Every outcome is auto-logged to global memory (kind `verification`), so the
feedback is shared — `gm_search` it to learn from others' rejected attempts.

## Your review is the only correctness gate

Nothing downstream re-checks a fact you accepted. If your review passes something
with a gap, that gap is now part of the project's truth and everything built on it
inherits it — so a `"correct"` verdict is a claim you are staking the whole
project on. When your review and your intuition disagree, the review wins: if you
cannot write the justification down, the verdict is `"wrong"`, however sure you
feel. `master_guidance` and any other LLM opinion are for ideas and directions,
never for correctness.

## Graph edges (declare with `graph_link` at publish time)

- on accept: `promoted-to` from the source finding (pass its id as
  `source_id`) to the returned `fact_id` — the awareness-to-truth bridge the
  curator uses to close subgoals.

## Tools

- `fact_submit` (the only path to write a fact; carries your verdict + review report)
- `gm_search` (check for an existing fact before submitting; read others' outcomes)
- the fact graph is read directly (`fact_graph/facts/`, `glossary.json`)
