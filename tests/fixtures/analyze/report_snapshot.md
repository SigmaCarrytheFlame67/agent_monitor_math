# Analysis: Algebraicity of Weil classes on split abelian eightfolds

> Checked by a model verifier (kimi-k3 via sponsored_kimi) — not a certificate. Each result was checked on its own, given the statements it cites; verdicts are model judgements that can be wrong in both directions.

## Source

- **Title:** Algebraicity of Weil classes on split abelian eightfolds
- **Version:** September 18, 2026
- **Link:** <https://github.com/openai/math/tree/adc7f1241b42e322a6451854ab7e4b4c146bf78a/preprints/Weil-Paper>
- **Identifier:** `github:openai/math@adc7f1241b42e322a6451854ab7e4b4c146bf78a:preprints/Weil-Paper`
- **Licence:** Apache-2.0
- **Format:** tex
- **Source bundle:** `0123456789abcdef`
- **Note:** Withdrawn by its authors on October 6, 2026; see the withdrawal notice
- **Withdrawn upstream on October 6, 2026:** the authors withdrew this manuscript (notice: <https://github.com/openai/math/blob/main/preprints/Weil-Paper/README.md>). The archived revision was analyzed; the notice was not shown to the verifier.
- **openai/math family:** 032 — The rational Hodge conjecture for CM abelian varieties
- **Not executed:** 1 computational file(s) in the source were skipped.

## Summary

Checked **4 of 5** results whose proofs were located in the manuscript (7 statements in total).

| Verdict | Results |
| --- | --- |
| checked (no issue found) | 2 |
| gap | 1 |
| error | 1 |
| unclear | 0 |
| not checked | 3 |

*Overview (model-written from the abstract and introduction):* The manuscript claims that every rational Weil class is algebraic.

## Main results

- **Theorem 1.1 (Main theorem)** — own verdict: gap; with everything it depends on: **error** (blocked by Lemma 3.7 → Proposition 3.8 → Theorem 1.1 (Main theorem)); conditional on 1 cited external result(s)

## Proof architecture

- 7 statements, 5 dependencies from the manuscript's own references, 1 weaker inferred or model-proposed links.
- Longest dependency chain: 5 result(s).
- Critical path to the main result: Definition 2.1 → Lemma 3.2 → Lemma 3.7 → Proposition 3.8 → Theorem 1.1 (Main theorem).
- Most used: Definition 2.1 (1), Lemma 4.1 (1), Proposition 3.8 (1), Lemma 3.2 (1), Lemma 3.7 (1).

## Findings

| Result | Location | Verdict | With dependencies | Finding |
| --- | --- | --- | --- | --- |
| Lemma 3.7 | [sections/03-topology.tex L461-L467, proof L469-L536](https://github.com/openai/math/blob/adc7f1241b42e322a6451854ab7e4b4c146bf78a/preprints/Weil-Paper/build/sections/03-topology.tex#L461-L536) | error | error | eq. top:trace-signs: reverse traces have sign -1 \| not +1 &lt;script&gt;x&lt;/script&gt; |
| Theorem 1.1 (Main theorem) | [sections/03-topology.tex L461-L467, proof L469-L536](https://github.com/openai/math/blob/adc7f1241b42e322a6451854ab7e4b4c146bf78a/preprints/Weil-Paper/build/sections/03-topology.tex#L461-L536) | gap | error | Step 4: uses an unstated bound |

**Lemma 3.7** — all findings:
- error: eq. top:trace-signs — reverse traces have sign -1 \| not +1 &lt;script&gt;x&lt;/script&gt;
- error: last paragraph — the count is -2m, see [link removed]

## External results relied on

- [EM12, Thm 1.1] — hypotheses checked: partly (used in Lemma 3.2)

## Not checked

- 1 × A definition, not a claim with a proof
- 1 × No proof was located by the parser
- 1 × Not checked: the model-call budget for this run was used

## Formalization

Statement formalization in Lean 4.14 was requested: selected statements are translated and type-checked, proofs are not attempted. See the Lean tab.

## How this analysis was made

- Structure: deterministic parse of the manuscript (pylatexenc-2.11); dependencies come from its own labels and references (inferred and model-proposed links are marked as such).
- Checks: 7 model call(s) to kimi-k3 via sponsored_kimi; each result was checked independently, assuming the statements it cites.
- A verdict of "checked" means the verifier found no error or gap in the proof as written. It is not a certificate, a refereed review or a Lean proof.
- Limits reached: budget.
- Analyzed on 2026-10-11.
