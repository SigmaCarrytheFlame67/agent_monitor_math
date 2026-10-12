"""Run the write-paper service as a stdio MCP server:
``python -m cgm.write_paper``.

Launched by ``bin/write-paper-mcp`` (which exports CGM_WRITE_PAPER_SKILL_DIR and the
codex/project env).
"""

from .server import build_app

if __name__ == "__main__":
    build_app().run()
