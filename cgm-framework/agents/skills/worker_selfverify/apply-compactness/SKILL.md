---
name: apply-compactness
description: Obtain the target object as a limit — build a sequence of controlled approximate objects, prove uniform (n-independent) bounds, extract a convergent subsequence by a NAMED compactness theorem, and pass hypotheses and target properties to the limit. Use for existence goals reachable by approximation, minimizing sequences, or finite truncations. Every limit passage must be justified; naming which compactness theorem and which convergence mode is mandatory.
---

# Apply Compactness

The pipeline `Xₙ ⟶ X_{n_k} ⟶ X` with five stations, each a proof obligation.
The two classic failure points are an n-DEPENDENT bound smuggled into step 2
and an unjustified limit passage in step 5.

## Input Contract

Read:

- the target statement and which of its properties must survive the limit
- `fact_search`: verified approximate/truncated versions of the goal (they may
  serve directly as the Xₙ)
- `gm_search` over `obstacle`: known losses of compactness in this problem
  (concentration, escape to infinity, oscillation)
- literature via `$search-math-results`: the right compactness theorem for the
  ambient space

## Procedure

1. **Approximants.** Construct Xₙ (approximate solutions, minimizing sequence,
   finite truncations, discretizations) and state exactly which relaxed
   property each Xₙ satisfies. Verified truncated facts are ideal approximants
   — cite their fact_ids.
2. **Uniform bounds.** Prove bounds on Xₙ with constants INDEPENDENT of n, in
   the norm/space the compactness theorem needs. Audit every constant: an
   n-dependent constant here voids the whole argument.
3. **Precompactness.** Name the compactness theorem and check its hypotheses
   explicitly. Cite it as a specific verified fact or a structured external
   reference (`external_refs` at submission) — "by compactness" with no named
   theorem is exactly the vague gesture the verifier rejects (P5).
4. **Subsequence extraction.** State the convergence mode granted (weak, weak-*,
   uniform-on-compacts, a.e., in norm, …). If several quantities converge in
   different modes, extract successive subsequences and say so (diagonal
   argument when countably many constraints).
5. **Pass to the limit.** For EACH hypothesis, equation, and target property,
   justify the passage in the granted mode: continuity, weak lower
   semicontinuity (norms/energies may only drop — check the inequality points
   the right way for the goal), dominated/monotone convergence, Fatou,
   compact embeddings for products of weakly convergent factors, closedness of
   constraint sets. An identity that holds for every Xₙ does NOT automatically
   hold for X — each clause gets its own argument.
6. Land via `$verify-proof`: the uniform-bound lemma(s) as facts; the
   limit-existence-and-properties statement as the closing fact, citing the
   bound facts (and the approximant facts) as predecessors.

## Output Contract

Publish to global memory with `gm_add` (kind `plan` while proposed,
`proof_attempt`/`conclusion` as it progresses): `claim` = the compactness
scheme, `evidence` = the five-station status, plus:

```json
{
  "target": "...",
  "approximants": "...",
  "uniform_bounds": [{"bound": "...", "n_independent_check": "..."}],
  "compactness_theorem": "named theorem + where cited (fact_id or external ref)",
  "convergence_mode": "...",
  "limit_passages": [{"clause": "...", "justification": "...", "status": "proved | open"}],
  "bound_fact_ids": ["filled as verified"],
  "limit_fact_id": "filled when the closing fact verifies"
}
```

If compactness genuinely fails (concentration/escape/oscillation), publish the
failure mode as `obstacle` — knowing WHICH loss occurs is what the repair
techniques (concentration-compactness, profile decomposition) key on.

## Graph edges (declare with `graph_link` at publish time)

- the scheme plan `addresses` the goal; `decomposes-into` edges for the five
  stations sharing an `and_group`, tagged `technique: compactness`; chain the
  stations with `depends-on` (bounds before extraction before limit passage).

## Tools

- `gm_add` / `gm_search` (publish; recall known compactness losses)
- `fact_search` (truncated versions to reuse as approximants)
- `search_arxiv_theorems` and Codex built-in web search (the right compactness
  theorem and its exact hypotheses)

## Failure Logging

If uniform bounds or a valid limit passage cannot be established, append:

- `events.event_type="compactness_argument_failed"`
