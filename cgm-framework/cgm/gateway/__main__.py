"""Run the cgm gateway as a stdio MCP server: ``python -m cgm.gateway``.

Role is taken from ``CGM_ROLE`` (env). Launched by ``bin/cgm-mcp`` (main) and
by each worker's ``.codex/config.toml`` (worker) / the verifier's ``-c`` override.
"""

from .server import build_app

if __name__ == "__main__":
    build_app().run()
