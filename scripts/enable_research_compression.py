#!/usr/bin/env python3
"""Retired: one-off compression patch for the former sslip-based Caddyfile.

The ansatze.ai edge configuration (deploy/ansatze/Caddyfile) already applies
`encode zstd gzip` to every response and serves the prebuilt `.json.gz`
research catalogues directly, so there is nothing left to patch. Change the
edge by editing that template and re-running
`sudo deploy/ansatze/install.sh --steps render,caddy`.
"""
import sys


def main() -> int:
    print(
        "scripts/enable_research_compression.py is retired and changes nothing; "
        "compression is part of deploy/ansatze/Caddyfile.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
