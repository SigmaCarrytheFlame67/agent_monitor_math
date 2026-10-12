# Ansatz: CGM and Math Agent Monitor

Source for [ansatze.ai](https://ansatze.ai): a mathematical research console,
Continual Graph Memory (CGM), proof monitoring and analysis, and the public
problem and research website.

## What's in this repository

| Path | Purpose |
| --- | --- |
| [`agent_monitor/`](agent_monitor/) | Console API, accounts, projects, sharing, job orchestration, model routing, proof analysis, and Lean integration. |
| [`agent_monitor/cgm/`](agent_monitor/cgm/) | The website's bounded CGM pipeline: strategist, workers, independent model verifiers, repairs, and curator. |
| [`cgm-framework/`](cgm-framework/README.md) | The complete standalone CGM implementation, with persistent memory, MCP services, orchestration, agent contracts, skills, tests, and operational examples. |
| [`monitor_core/`](monitor_core/) | Shared event and artifact processing, dependency graphs, and monitor views. |
| [`agent_monitor/web/`](agent_monitor/web/) | The interactive proving and analysis console. |
| [`docs/`](docs/) | The VitePress website, Vue components, public problem catalogues, research graphs, and media served by ansatze.ai. |
| [`engines/`](engines/) | Bundled Hermes and IMProof/ProofStack sources and external-engine integration notes. |
| [`deploy/`](deploy/) | Caddy, systemd, sponsored-model gateway, and sandboxed Lean broker deployment. |
| [`tests/`](tests/), [`evals/`](evals/README.md), [`scripts/`](scripts/) | Regression tests, evaluation inputs and tools, catalogue builders, and operational utilities. |
| [`problems/`](problems/) | Problem manifest and source problems used by the console. |

The two CGM implementations have separate execution models. The website runs
`agent_monitor.cgm` through its account, budget, and job controls. The standalone
framework is a separate Python project under `cgm-framework/`, with its own
configuration and services; installing it is optional for running the website.

This repository tracks source, tests, configuration templates, and the public
assets needed by the site. Saved user runs, databases, instance settings, API
credentials, dependency installs, build output, research manuscripts, and
historical report archives are excluded. See [the contribution guide](CONTRIBUTING.md)
for the source boundary.

## Local console

Use Python **3.11–3.13** (3.13 recommended). From a fresh clone:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
agent-monitor create-admin
./start.sh
```

The development console listens on `http://127.0.0.1:4600`. Set provider keys
in the ignored `.env` or through the console's Settings page. The
[environment template](.env.example) documents authentication, CGM budgets,
Analyze limits, and optional integrations. Local runtime state defaults to
`data/`; `AGENT_MONITOR_DATA_DIR` selects another location.

The broader [`setup.sh`](setup.sh) provisions optional engines and may install
CLI tools and download upstream sources. Read it before using it. Individual
engines can require their own environments or authenticated CLIs;
[`engines/EXTERNAL_CHECKOUTS.md`](engines/EXTERNAL_CHECKOUTS.md) records external
source revisions. The authoritative engine list is
[`agent_monitor/engines_registry.py`](agent_monitor/engines_registry.py).

## Local website

With Node.js and npm installed:

```bash
npm ci
npm run docs:dev
```

For a production build in a development clone:

```bash
npm run docs:build
npm run docs:preview
```

Public catalogue JSON, graph data, guide media, and their licences are retained
under `docs/public/` because the website reads them directly. They are distinct
from private run output. Builders and refresh tools live under `scripts/`;
source provenance is described in [the problem source guide](docs/problem-sources.md)
and [ATTRIBUTION.md](ATTRIBUTION.md).

## Standalone CGM

Start with [`cgm-framework/README.md`](cgm-framework/README.md) and its
[architecture guide](cgm-framework/ARCHITECTURE.md). Run commands from that
subdirectory so its configuration, contracts, and skills resolve correctly:

```bash
cd cgm-framework
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m cgm.orchestration --help
python -m pytest cgm/ -q
```

The framework includes an offline example that creates and inspects a project
without making model calls. Live workers and services need separately configured
backends; follow its [getting-started guide](cgm-framework/docs/getting-started.md).

## How a monitored run works

1. `console_server.py` authenticates a request and applies account policy.
2. `jobs.py` creates the run workspace and selects a runner.
3. The CGM runner coordinates proof attempts and persists fact and exploration
   graphs; other runner adapters normalize their engines into the same run model.
4. `monitor_core` and the graph modules assemble events, artifacts, usage, and
   proof dependencies for the console.
5. Analyze reads existing manuscripts; Lean integration checks formal artifacts
   through the configured tooling or sandbox broker.

Model-verifier acceptance and Analyze verdicts are model judgments. They are
separate from Lean compilation and its recorded verification evidence.

## Checks

From the repository root, with the console's development dependencies installed:

```bash
python -m pytest tests/ -q
node --test tests/*.mjs
npm run docs:build
```

Run the standalone framework's test command separately from `cgm-framework/`.
The suites use local fixtures and mocked providers; some tests start loopback
servers or subprocesses and need an environment that permits them.

## Hosted deployment

[`deploy/ansatze/README.md`](deploy/ansatze/README.md) is the deployment guide;
[OPERATIONS.md](OPERATIONS.md) covers ongoing operations. Caddy serves the static
website and forwards the console mount to the Python service. Sponsored Kimi
calls and Lean checks use separate services.

On the existing host, `/home/repo/agent_monitor_math` is the live source tree.
Editing served HTML can affect requests immediately, and `npm run docs:build`
publishes the static site there. Prepare and test changes in a separate clone.
Production state and credentials live outside Git, principally under
`/var/lib/ansatze/` and `/etc/ansatze/`. Deployment and service restarts are
separate from a Git push.

## Attribution and licences

Preserve [ATTRIBUTION.md](ATTRIBUTION.md) and the bundled licence files. The
standalone CGM framework retains its [Apache 2.0 licence](cgm-framework/LICENSE);
the website adaptation includes [its licence](agent_monitor/cgm/LICENSE) and
[change notices](agent_monitor/cgm/NOTICE.md). Bundled engines and public data
retain their own terms.
