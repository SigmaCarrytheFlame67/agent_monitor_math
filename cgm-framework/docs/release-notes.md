# Source import notes

## Scope

This subproject imports the implementation from the supplied
`continual-graph-memory-main` release into the ansatze.ai source repository.
The Python code, agent contracts and skills, configuration templates, operational
examples, test fixtures, benchmark problem inputs, and evaluation scripts are
retained. The website's CGM adaptation remains in `agent_monitor/cgm/` and has
its own account and run controls.

Historical experiment output, result manuscripts, and archive-level checksums
are not included. README and packaging notes are adapted to this layout; the
upstream Apache 2.0 licence and copyright notice are preserved. The original
archive's checksums are not a validation record for this differently packaged
source tree.

## Validation

Run from `cgm-framework/`, in its own virtual environment:

```bash
python -m pip install -e '.[dev]'
python -m pytest cgm/ -q
```

The development extra includes the optional MCP, HTTP service, and provider
libraries needed to collect the complete suite. Some tests require subprocesses
or loopback networking. Live deployments target Linux and need configured
backends; no model credentials are required for the mocked software tests.

Software tests check implementation behavior. They do not reproduce historical
benchmark outcomes, certify research claims, or replace independent mathematical
verification. Model-verifier acceptance is distinct from a formal certificate.
