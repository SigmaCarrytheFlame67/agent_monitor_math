---
name: select-extremal-objects
description: Argue via an extremal object — pick, among all candidates, one minimizing or maximizing a complexity, length, energy, size, or other quantity, then exploit its extremality (any legal improving modification yields a contradiction, or the extremal structure is forced). Use for existence-of-structure, impossibility, and minimal-counterexample arguments. The existence of the extremal object must itself be proved, not assumed.
---

# Select Extremal Objects

The pattern: `choose an extremal object ⟶ apply a legal local modification ⟶
obtain a strictly better object ⟶ contradiction (or forced structure)`. Three
proof obligations guard it: existence of the extremum, legality of the
modification, and STRICT improvement.

## Input Contract

Read:

- the goal and the candidate class over which to extremize (quote the class's
  defining constraints exactly — the modification must stay inside them)
- `gm_search` over `plan`/`dead_end`: extremal quantities already tried
- `fact_search`: verified facts about the candidate class (non-emptiness,
  discreteness, compactness, bounds usable for existence)
- literature via `$search-math-results`: standard extremal choices in this
  area (minimal counterexample, minimal support, maximal chain, minimal
  energy, first failure time)

## Procedure

1. Choose the extremal quantity μ and direction (minimize/maximize), and state
   why extremality should force structure relevant to the goal.
2. **Prove the extremal object exists (mandatory first lemma):**
   - finite nonempty candidate set → direct selection (prove non-emptiness!);
   - integer-valued μ bounded in the relevant direction → well-ordering;
   - infinite candidate space → name the mechanism: compactness of the class
     + (lower/upper semi-)continuity of μ, coercivity, or a minimizing-sequence
     argument (then follow `$apply-compactness` for the limit extraction);
   - if existence cannot be established, consider an approximate-extremal
     (ε-minimal) variant and say how the argument tolerates the ε.
3. Define the local modification precisely, and **verify it is legal**: the
   modified object satisfies every constraint of the candidate class. A
   modification that silently exits the class proves nothing.
4. **Verify strict improvement**: μ strictly decreases (resp. increases).
   Non-strict improvement gives no contradiction. Track the exact inequality.
5. Conclude: contradiction with extremality (goal proved / counterexample
   impossible), or extract the forced structure of the extremal object and
   hand it to the next step of your lane.
6. Land via `$verify-proof`: the existence lemma as one fact; the extremal
   argument (modification + strict improvement + conclusion) as another,
   restating the existence fact inline at the point of use, with its Fact
   number.

## Output Contract

Publish to global memory with `gm_add` (kind `plan` while proposed,
`conclusion` when the argument closes): `claim` = the extremal setup and what
it forces, `evidence` = existence + modification + strictness arguments, plus:

```json
{
  "candidate_class": "...",
  "extremal_quantity": "...",
  "direction": "min | max",
  "existence_mechanism": "finite | well-ordering | compactness+lsc | epsilon-extremal",
  "modification": "...",
  "strictness_argument": "...",
  "existence_fact_number": "filled when verified",
  "argument_fact_number": "filled when verified"
}
```

If the modification cannot be kept legal, or improvement is only non-strict,
publish the failure as `obstacle` — these two are the canonical collapse modes
of extremal arguments and siblings must know.

## Graph edges (declare with `graph_link` at publish time)

- the extremal plan `addresses` the goal; `decomposes-into` edges for the
  existence lemma, the modification-legality check, and the strict-improvement
  step, sharing an `and_group`, tagged `technique: extremal`.

## Tools

- `gm_add` / `gm_search` (publish; recall tried extremal quantities)
- `fact_search` (class facts supporting existence)
- `search_arxiv_theorems` and Codex built-in web search (standard extremal
  arguments in the area)

## Failure Logging

If no extremal quantity yields a legal strictly-improving modification, append:

- `events.event_type="extremal_argument_failed"`
