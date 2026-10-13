# Contributing to Ansätze

**English** · [简体中文](CONTRIBUTING.zh-CN.md) · [Français](CONTRIBUTING.fr.md)

Thank you for helping make mathematical research easier to inspect and reproduce. Contributions can be code, documentation, translations, tests, interface improvements, or carefully sourced examples. You do not need access to the production server or a paid model account to begin.

## Choose a useful first task

| Area | Examples | Where to look |
| --- | --- | --- |
| Documentation and translation | Clarify installation, explain a graph, synchronize English/Chinese/French guides. | [README](README.md), [docs/](docs/) |
| Interface and accessibility | Improve keyboard focus, narrow screens, readable formulas, or empty states. | [agent_monitor/web/](agent_monitor/web/) |
| Graph correctness | Reproduce a missing dependency, distinguish search edges from proof dependencies, test cyclic input. | [tests/](tests/) and graph modules under [agent_monitor/](agent_monitor/) |
| Analysis and evidence | Add a minimal parser fixture or improve how verdict scope is communicated. | [agent_monitor/analyze/](agent_monitor/analyze/) |
| Standalone CGM | Improve role contracts, orchestration, tests, or offline examples. | [cgm-framework/](cgm-framework/README.md) |
| Public problem sources | Correct a source link or dated status with a primary reference and attribution. | [source guide](docs/problem-sources.md) |

Browse [existing issues](https://github.com/ericjiang18/agent_monitor_math/issues) before opening a new one. The examples above are suggestions, not a promise that a particular issue is unassigned. For a substantial change, discuss the user problem and proposed scope first. Small fixes can go straight to a pull request.

## Report a bug or propose a feature

Use the [issue chooser](https://github.com/ericjiang18/agent_monitor_math/issues/new/choose). English, Simplified Chinese, and French are welcome.

For a bug, include the commit or deployed version if known, operating system and browser, affected view, reproduction steps, expected and actual behavior, and relevant **redacted** logs. A small synthetic example is more useful than a large private run. For visual bugs, add a screenshot and viewport size.

For a feature, describe the research task, who needs it, the current obstacle, and what observable result would count as success. If it changes a graph or verification status, explain what each node, edge, or label would mean.

Do not submit passwords, API keys, access tokens, cookie values, private share links, account databases, or unpublished manuscripts without permission. A share link can itself grant access, even without a password.

## Security reports

Do not publish exploitable security details in a normal issue. Use **Security → Report a vulnerability** on this repository if private reporting is enabled. If that option is unavailable, open a minimal issue asking the maintainers to arrange a private reporting channel, without including exploit details or credentials. Never include a secret as proof that it leaked; revoke it through its provider and describe the affected credential type.

## Set up a development checkout

Fork the repository, clone your fork into a development directory, and create a branch:

```bash
git clone https://github.com/YOUR-USERNAME/agent_monitor_math.git
cd agent_monitor_math
git switch -c improve/short-description
```

Follow [Local setup](README.md#local-setup). Work in a separate checkout if another copy serves production: editing live HTML or running a production build there can immediately change the hosted site. Keep provider calls disabled or mocked when the change does not need them.

The website CGM and the standalone framework are separate implementations. Run standalone commands from `cgm-framework/` with its own environment; changing one does not automatically change the other.

## Keep the repository focused

Include code, public assets, configuration templates, tests, and documentation for CGM, math monitoring, proof analysis, and ansatze.ai. Keep public catalogues and their provenance with the source that consumes them. Evaluation scripts and prompt inputs are source; generated evaluation runs are not.

Do not commit local `.env` files, credentials, databases, uploads, saved run workspaces, private transcripts, instance settings, dependency installs, build output, paper archives, or unrelated research projects. `data/` is for ignored runtime state, with only its README and empty-directory placeholders tracked. `.gitignore` is helpful but does not replace reviewing the diff.

For fixtures, prefer short synthetic data. Label it as synthetic; do not claim it is a live run or experimentally verified result. Preserve third-party licences and document adaptations. Check the terms of a manuscript or dataset before redistributing it.

## Implement and validate

Keep the change focused on one coherent problem. Follow nearby conventions, make error and empty states explicit, and add a regression test when fixing meaningful behavior. Do not introduce model calls merely to render a saved result.

| Changed area | Relevant checks |
| --- | --- |
| Console, API, jobs, analysis, graphs | Targeted pytest tests, then `python -m pytest tests/ -q` when appropriate. |
| Browser helpers | `node --test tests/*.mjs`; inspect the changed interface in a browser. |
| Public site | `npm ci` and `npm run docs:build` in a development checkout. |
| Standalone framework | From `cgm-framework/`: `python -m pytest cgm/ -q`. |
| README or translations | Resolve local links and image paths; keep commands, feature scope, and limitations consistent across all three languages. |

Some tests open loopback sockets or start subprocesses. Record environment-related failures accurately. Do not relabel a failed or skipped check as passed. The full CI matrix is defined in [Source checks](.github/workflows/ci.yml).

For a UI change, inspect desktop and narrow mobile layouts, keyboard interaction, light/dark themes where applicable, loading states, and missing-artifact states. Attach before/after screenshots from synthetic or already public data. For sharing changes, verify that the page stays read-only and that older snapshots remain understandable.

## Preserve the meaning of evidence

- **Explore** describes search and execution. Its edges are not automatically proof dependencies.
- **Facts** records model-verifier acceptance and its saved dependencies; it is not a Lean certificate.
- **Lean compilation**, **axiom checks**, and **statement-fidelity judgments** are distinct. Preserve their scope and missing-result states.
- **Analyze** reports model judgments about a manuscript. Compiling its formalized statements does not prove them.
- **Shared snapshots** are explicit saved disclosures. Reading them must not run models, start checks, or expand into private workspace files.

## Submit a pull request

1. Explain the concrete problem and resulting behavior. Reference a related issue if one exists.
2. Include the checks you ran, their results, and any relevant limitations.
3. Add before/after images for visible changes; keep image captions and provenance accurate.
4. Update documentation, translations, and component attribution as needed.
5. Review `git status`, `git diff --check`, and the staged diff. Remove secrets, private data, and generated files.
6. Open a pull request against `main`; a draft is welcome for work that needs early feedback.

Maintainers review correctness, usability, scope, evidence semantics, and operational impact. Respond to feedback with focused revisions. A review approval is not a deployment.

## Review, merge, and publication

Pull requests run Source checks. After a merge to `main`, checks run again for the merged commit. The [server deployment timer](deploy/ansatze/AUTO_DEPLOY.md), once installed, publishes that exact commit only after the required jobs pass; it performs health checks and supports rollback. Approval alone does not publish unmerged code. Contributors do not need deployment credentials or a GitHub personal access token.

## Working together

Be respectful, explain disagreements with evidence, and separate criticism of an idea from criticism of a person. Make room for contributors with different mathematical, programming, and language backgrounds. An automated or AI-assisted contribution still needs human-readable reasoning, accurate sources, meaningful validation, and a reviewed diff.

For licences and bundled material, read [ATTRIBUTION.md](ATTRIBUTION.md) and the licence next to the component you change. Keep those notices intact.
