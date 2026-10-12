# Monitor evaluations

These scripts and inputs exercise the console's proof-audit and prompt contracts.
They are retained with the regression tests that import them. Historical run
outputs and narrative result reports are not part of this source tree.

- `research_proof_audit_24h.py` schedules configured audit and baseline runs
  against a running console; `research_audit*.json` contains target inputs.
- `finalize_research_proof_audit.py` checks persisted evidence and writes an
  evaluation summary.
- `promote_research_prompt.py` checks and optionally applies an explicit prompt
  promotion. Its default inputs are `system_prompt_candidate_s.md` and
  `system_prompt_candidate_t_delta.md`; it defaults to a dry run.
- The other candidate prompts and bounded mathematical checkers are evaluation
  inputs and regression tools, not certified mathematical results.

Use each script's `--help` before running it. Live console evaluations can make
model calls; keep generated output under the ignored `evals/results/` directory.
