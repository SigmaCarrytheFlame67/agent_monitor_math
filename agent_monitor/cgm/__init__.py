"""CGM ("Ansatz") pipeline support for the response-only Math Agent Harness.

This package holds the tool-free prompt pack, tolerant reply parsers, the
deterministic curator propagation, and the on-disk fact/exploring-graph
contracts shared by ``agent_monitor.runners.math_harness_runner`` (writer) and
``agent_monitor.engine_graph`` (validator).

The prompts and data contracts are adapted from the Ansatz /
continual-graph-memory project (Copyright 2026 FrenzyMath, Apache License 2.0).
See ``LICENSE`` and ``NOTICE.md`` in this directory for the license text and
the list of changes; no upstream code is imported or executed.
"""
from __future__ import annotations

from .closure import ClosureError, closure_order, render_verified_closure
from .prompts import prompt_pack_sha256

__all__ = [
    "ClosureError",
    "closure_order",
    "prompt_pack_sha256",
    "render_verified_closure",
]
