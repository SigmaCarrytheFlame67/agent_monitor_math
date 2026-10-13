#!/bin/bash
# Installed root-owned. All package/build hooks run inside the unprivileged unit.
set -euo pipefail
stage=$1
python=$2
node_bin=$3
cd "$stage"
export PATH="$node_bin:/usr/bin:/bin"
export PIP_CACHE_DIR="$stage/.build-cache/pip"
export npm_config_cache="$stage/.build-cache/npm"
"$python" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check .
npm ci --no-audit --no-fund
npm run docs:build
# Check imports and real console entrypoints before root can publish this tree.
# These paths are build-local, empty, and contain no production state or keys.
export AGENT_MONITOR_ENV_PATH=/dev/null
export AGENT_MONITOR_DATA_DIR="$stage/.build-check-data"
.venv/bin/python -m pip check
.venv/bin/python -c 'from agent_monitor import console_server, public_kimi_server; from agent_monitor.runners import math_harness_runner; print("Core console, broker and CGM imports passed")'
.venv/bin/agent-monitor --help >/dev/null
# Build caches and development dependencies do not belong to the release.
rm -rf node_modules .build-cache .build-check-data build agent_monitor.egg-info
