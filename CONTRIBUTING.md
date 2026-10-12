# Contributing

## Repository scope

Keep changes focused on CGM, mathematical agent monitoring and proof analysis,
and the code, configuration, tests, and public assets used by ansatze.ai.

The standalone framework belongs in `cgm-framework/`. Its Python environment,
services, contracts, and agent skills are separate from the website's
`agent_monitor.cgm` implementation. Run each project's tests from its own root.

Keep public catalogue and graph data, website media, illustrative examples,
regression fixtures, and their attribution alongside the source that uses them.
Evaluation scripts and prompt inputs are source; generated evaluation runs are
not. Preserve third-party licences and document adaptations.

Do not commit local `.env` files, credentials, databases, user uploads, run
workspaces, instance settings, model transcripts, dependency installs, generated
builds, paper archives, or unrelated research projects. `data/` contains only a
README and empty-directory placeholders. Runtime state belongs in ignored local
directories or in the configured external data directory.

## Development and review

Use a fresh clone for development when the existing checkout serves production.
Follow the setup and validation commands in [README.md](README.md), then review
`git status` and the staged diff before committing. Validate relevant Python
changes with the existing pytest suite, browser helpers with `node --test
tests/*.mjs`, and website changes with `npm run docs:build` in the development
clone. Validate standalone CGM changes with its own test suite.

A source update does not deploy the site. Follow the
[deployment guide](deploy/ansatze/README.md) for an intentional deployment.
