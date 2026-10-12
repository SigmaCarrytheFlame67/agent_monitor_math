---
name: verify-proof
description: Verify a result and, on acceptance, write it as a fact — via the fact_submit tool. Use for the full target theorem AND for every sharply-delimited intermediate result, lemma, construction, or formula you intend to build on. The verifier is the sole authority on mathematical correctness.
---

# Verify Proof

The verifier is the **canonical and sole authority on mathematical correctness**.
Mathematics requires 100% accuracy; even though this verifier is not a formal
proof assistant, it is the strongest correctness check in the system. No LLM
consultation, panel, or self-critique substitutes for it.

**You verify and write a fact through one tool: `fact_submit`.** It calls the
verifier and appends the fact to the verified results file (`fact_text/facts.md`)
**iff the verifier accepts** — there is no other way a fact enters the file.

## When to submit

- **The full target theorem** — when you have assembled a complete proof of the
  whole problem (as a self-contained statement + proof, restating every result it
  depends on where it is used, with its Fact number — e.g. "by Fact 12: <its
  statement>").
- **Every intermediate result you intend to USE downstream** — a lemma, a
  candidate construction, an arithmetic/closed-form claim, a saturation or
  local-to-global claim, any sharply-delimited step. **Adopting an unverified
  partial result as a building block is the single biggest correctness risk.** When
  in doubt, submit.

Do **not** build on an unverified finding from global memory. A `conclusion` /
`example` / `counterexample` there is awareness, not a brick — re-derive it as a
self-contained statement+proof and submit it before relying on it.

## Before you submit — write an "ugly-proof" fact

A fact in the verified results file is written in **"ugly-but-rigorous"** form
(the operator may call it an **"ugly-proof"**). The one goal of
this form is that the fact is **mechanically checkable for correctness** by a
reader with no memory and no math intuition (an agent with no recall, a human, the
verifier). It is allowed — encouraged — to be **ugly**: redundant, machine-flavored,
verbose. It is **not allowed** to be ambiguous, vague, or context-dependent.
"Ugly" is the deliberate contrast with the polished arXiv paper (a separate
pipeline); here, only mechanical correctness matters.

Concretely, before you submit:

- **Self-contained, period.** A reader holding only this fact's own text — with
  no memory of the project — can decide whether the math is correct. When a step
  depends on an established result, restate that result where you use it and
  mention its number ("by Fact 12: <its statement>"). No appeal to chart
  positions, parse status, project history, or "as we know".
- **Define every symbol — in the text itself.** Each symbol used in the
  statement/proof is defined in that statement/proof itself, except truly
  universal notation (Z, Q, R, C, floor/ceil, gcd/lcm, intervals, standard Greek
  parameter names) — don't redefine universal notation. When you restate an
  earlier verified result, restate its symbol definitions too. Reuse the
  project's existing symbol for the same object.
- **Restate every dependency where it is used, with its Fact number** ("by
  Fact N: <its statement>") — never "by the result above", never the
  problem statement as a math source.
- **Every quantifier explicit; every introduced parameter (epsilon, k, …) carries
  an explicit range.**
- **No handwave** ("obviously", "easy to see", "routine", "analogously",
  "by some classical argument") and **no chart-position references** ("as above").
- **Avoid duplicates.** `fact_search` the verified results file / `gm_search`
  global memory (or read `fact_text/facts.md`) for an existing fact with the same
  statement; if one exists, use it (restate it + "by Fact N") instead of
  re-proving it.

## Submit and repair

Call `fact_submit(statement, proof)` (optionally `intuition`, and `source_id` of
the source finding). Read the result:

- `accepted: true, fact_number` — the fact is appended to the verified results
  file. **Refer to it as "Fact N"** downstream (restating it where used).
- `accepted: false, verdict: "wrong", repair_hints` (+ `verification_report`) —
  revise: resolve critical errors first, then all remaining gaps; do not assume
  the fix is local — change strategy or backtrack if needed; then resubmit. Treat
  any `wrong` verdict, any critical error, or any gap as failure.
- `verdict: "error"` — the verify service was unavailable; retry.

Every outcome is auto-logged to global memory (kind `verification`), so the
feedback is shared — `gm_search` it to learn from others' rejections.

## The verifier is the only correctness authority

If your own reasoning, the main agent's `master_guidance`, or any other LLM calls
a result correct but `fact_submit` rejects it, the verifier wins. Always. Note the
disagreement (a `dead_end` finding) and treat the "looks correct" opinion as the
unreliable signal it was. A non-verifier opinion (including `master_guidance`) is
for ideas and directions, never for correctness.

## Graph edges (declare with `graph_link` at publish time)

- on accept: `promoted-to` from the source finding (pass its id as
  `source_id`) to the accepted fact's node id `fact:<N>` (from the returned
  `fact_number`, e.g. `fact:12`) — the awareness-to-truth bridge the
  curator uses to close subgoals.

## Tools

- `fact_submit` (the only path to verify a result and write a fact)
- `fact_search` / `gm_search` (check for an existing fact before submitting; read others' verification outcomes)
- the verified results file is read directly (`fact_text/facts.md` — find the "## Fact N" entry)
