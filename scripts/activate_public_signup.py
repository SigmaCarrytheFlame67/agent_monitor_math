#!/usr/bin/env python3
"""Retired: public-signup promotion for the former sslip-based host.

This script rewrote a login page in a checkout that no longer exists and
installed a root-owned systemd drop-in for the old console mount. Running it on
the ansatze.ai host could only fail or, worse, open registration unreviewed.

Opening self-registration is a deliberate owner decision: registered accounts
can run tool-capable harnesses on the host. Follow "Opening registration" in
deploy/ansatze/README.md instead.
"""
import sys


def main() -> int:
    print(
        "scripts/activate_public_signup.py is retired and changes nothing. "
        "See 'Opening registration' in deploy/ansatze/README.md.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
