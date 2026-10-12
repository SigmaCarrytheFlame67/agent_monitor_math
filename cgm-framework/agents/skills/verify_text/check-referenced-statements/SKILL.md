---
name: check-referenced-statements
description: Validate externally referenced theorems by querying arXiv theorem search. Use when a markdown proof cites statements from external papers. No other network access is permitted.
---

# Check Referenced Statements

Validate every external-paper reference used in the proof.

## Input Contract

For each cited external theorem/lemma/definition:

- location where it is used,
- the full referenced statement text.

## Procedure

1. Query `search_arxiv_theorems` using the full referenced statement as `query`.
2. Inspect returned results and compare theorem text directly to the referenced statement in reasoning.
3. Expand the definitions and terminology appearing in the cited statement using the cited paper's context before deciding whether the theorem applies.
4. Check whether the same words in the current proof mean the same thing as they do in the cited paper. In mathematics, identical words can carry different definitions in different contexts. Distinguish similar-looking definitions: compare their exact formulas, notation, and quantifiers; do not collapse two just because the names or formulas look close.
5. Accept as matched and applicable only when both are true:
   - the result clearly corresponds to the cited statement,
   - the contextual definitions and hypotheses align with the current problem.
6. If the theorem exists but the current proof uses different definitions, hypotheses, ambient objects, or a subtly different defining formula, record a critical error for incorrect application.
7. If the proof uses the cited statement to derive further conclusions, verify that transition too: a hand-wavy specialization or instantiation is a `gap`; a logically invalid transition is a `critical_error`; if it deduces one property from another, compare their exact defining formulas before accepting.
8. If no match is found — including when external search is disabled by the deployment (the tool returns an `error` envelope) — do NOT try to reach the internet by any other means (no web search, no `curl`/`wget`, no downloads).
9. If not found, emit a critical error:
   - location: where the citation is used,
   - issue: referenced theorem appears non-existent or incorrectly cited.
10. When a step cites an internal verified result as "Fact N" rather than an external paper, apply the verifier contract's P3-supplement **chain check** (`agents/contracts/verifier_text.md`): read the cited "## Fact N" entry from `runtime/projects/<PROJECT>/fact_text/facts.md` — if the file contains a later "## Retraction of Fact N" block for the cited N, the citation is invalid (record a `critical_error`) — and, if its own statement carries an unproven conditional premise, record the inherited defect as a `critical_error`. Read and apply the wording from the contract; do not fork it here.
11. Keep each reference check in context for the synthesis step (you persist nothing —
   the verifier is stateless).

Do not rely on dedicated comparison utility code; perform comparison through careful reasoning.

## Output Contract

Produce one record per reference check, kept in context for synthesis:

```json
{
  "location": "Lemma 2",
  "referenced_statement": "Exact statement text",
  "context_expansion": "In the cited paper, 'regular' means regular with respect to the valuation topology.",
  "arxiv_match_found": false,
  "critical_error": {
    "location": "Lemma 2",
    "issue": "Referenced external theorem was not found in arXiv search."
  }
}
```

## Tools

- `search_arxiv_theorems` (the ONLY external lookup; when the deployment disables it, references are simply unverifiable — never substitute any other network access)

(Findings stay in context for synthesis — nothing is persisted.)
