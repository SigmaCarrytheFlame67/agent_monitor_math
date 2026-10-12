#!/usr/bin/env bash
# Start the Agent Monitor proving console in the foreground (development).
# Production on ansatze.ai runs under systemd instead: see deploy/ansatze/.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "No .venv found — run ./setup.sh first." >&2; exit 1
fi
export PATH="${HOME}/.local/node24/bin:${HOME}/.npm-global/bin:${HOME}/.local/bin:${PATH}"
# The server itself defaults to 0.0.0.0; a development console should not be
# reachable from the network unless explicitly asked (AGENT_MONITOR_HOST=0.0.0.0).
export AGENT_MONITOR_HOST="${AGENT_MONITOR_HOST:-127.0.0.1}"
if [ "${AGENT_MONITOR_PORT:-4600}" = 4600 ] \
  && systemctl is-active --quiet agent-monitor.service 2>/dev/null; then
  echo "agent-monitor.service already serves port 4600 on this host; set AGENT_MONITOR_PORT to another port." >&2
  exit 1
fi
exec ./.venv/bin/python -m agent_monitor.console_server "$@"
