"""Run the human-summary service as a stdio MCP server:
``python -m cgm.human_summary``.

Launched by ``bin/human-summary-mcp`` (which exports
CGM_HUMAN_SUMMARY_SKILL_DIR and the codex/project env).
"""

from .server import build_app

if __name__ == "__main__":
    build_app().run()
