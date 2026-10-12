#!/usr/bin/env python3
"""Retired (October 2026): the problem catalogue is built by scripts/build_problem_catalogue.py.

This script produced or promoted the schema-2 catalogue.  Its September 2026
Kourovka input no longer exists upstream, and running it now would replace the
schema-3 catalogue.  Refresh the pinned source facts with
scripts/refresh_problem_sources.py, rebuild with scripts/build_problem_catalogue.py,
and publish through the normal deployment (docs/problem-sources.md, OPERATIONS.md).
"""
import sys

if __name__ == "__main__":
    print("Retired: rebuild the problem catalogue with scripts/build_problem_catalogue.py "
          "(source facts: scripts/refresh_problem_sources.py). See docs/problem-sources.md.", file=sys.stderr)
    raise SystemExit(2)
