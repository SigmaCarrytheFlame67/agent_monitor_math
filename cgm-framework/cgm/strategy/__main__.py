"""Run the consult gateway: ``python -m cgm.strategy`` (bin/consult wraps it)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
