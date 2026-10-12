"""Run the cgm CLI: ``python -m cgm.orchestration <verb> …`` (bin/cgm execs this)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
