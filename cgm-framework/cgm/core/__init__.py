"""cgm.core — the truth layer.

Three tiered stores + their shared schema, ranking, and glossary:

- ``FactGraph``   — the project-shared, content-addressed DAG of verifier-accepted
                    facts. The ONLY correctness source.
- ``GlobalMemory``— project-shared, strongly-typed findings (awareness, never truth).
- ``LocalMemory`` — per-worker private scratch log.

Pure data-structure I/O only; *when* to publish / verify / promote is prose in
the agent prompts, not code here. See the repo ARCHITECTURE.md.
"""

from __future__ import annotations

from . import bm25, glossary
from .exploring_graph import (
    ANNOTATION_STATUSES,
    EDGE_TYPES,
    FRONTIER_KINDS,
    TECHNIQUES,
    ExploringGraph,
)
from .factgraph import FactGraph, parse_frontmatter, serialize_fact, statement_of
from .facttext import FactText
from .global_memory import GlobalMemory
from .local_memory import DEFAULT_CHANNELS, LocalMemory
from .schema import (
    EXTERNAL_REF_KEYS,
    GLOBAL_KINDS,
    STATUSES,
    Fact,
    clean_external_refs,
    compute_fact_id,
)

__all__ = [
    "FactGraph",
    "FactText",
    "GlobalMemory",
    "LocalMemory",
    "ExploringGraph",
    "EDGE_TYPES",
    "TECHNIQUES",
    "ANNOTATION_STATUSES",
    "FRONTIER_KINDS",
    "DEFAULT_CHANNELS",
    "Fact",
    "GLOBAL_KINDS",
    "STATUSES",
    "EXTERNAL_REF_KEYS",
    "clean_external_refs",
    "compute_fact_id",
    "serialize_fact",
    "parse_frontmatter",
    "statement_of",
    "bm25",
    "glossary",
]
