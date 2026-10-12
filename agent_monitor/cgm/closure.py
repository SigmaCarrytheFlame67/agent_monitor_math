"""Pure helpers over a content-addressed fact DAG.

``render_verified_closure`` is the single deterministic rendering of a target
fact's predecessor closure.  The runner publishes it as ``proof.md`` and
``engine_graph`` recomputes it from the fact files during attestation, so the
two can never disagree about what "the verified proof" is.  Phase-2 callers
(for example a delegating Kimi runner) should reuse it rather than re-render.
"""
from __future__ import annotations

import heapq
from typing import Any, Iterable, Mapping, Sequence

CLOSURE_HEADER = (
    "This proof is the verified fact closure of the target theorem. Each lemma "
    "and the main theorem were accepted one at a time by an independent, "
    "response-only model verifier that saw only that statement, its proof, and "
    "the statements of the facts it cites. Model-verifier acceptance is not a "
    "Lean or other formal certificate."
)


class ClosureError(ValueError):
    """The fact collection is not a well-formed DAG for the requested target."""


def _field(fact: Mapping[str, Any], name: str) -> Any:
    if name not in fact:
        raise ClosureError(f"fact is missing {name}")
    return fact[name]


def _index(facts: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for fact in facts:
        fact_id = str(_field(fact, "fact_id"))
        if fact_id in indexed:
            raise ClosureError(f"duplicate fact id: {fact_id}")
        indexed[fact_id] = fact
    return indexed


def predecessor_closure(
    facts: Mapping[str, Mapping[str, Any]], target_id: str
) -> set[str]:
    """Return the target and every fact it transitively depends on."""
    if target_id not in facts:
        raise ClosureError(f"unknown target fact: {target_id}")
    seen: set[str] = set()
    pending = [target_id]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        if current not in facts:
            raise ClosureError(f"unknown predecessor: {current}")
        seen.add(current)
        pending.extend(str(pred) for pred in _field(facts[current], "predecessors"))
    return seen


def topological_order(facts: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Kahn's order with a lexicographic tie-break (identical to engine_graph)."""
    children: dict[str, list[str]] = {fact_id: [] for fact_id in facts}
    indegree: dict[str, int] = {}
    for fact_id, fact in facts.items():
        predecessors = [str(pred) for pred in _field(fact, "predecessors")]
        if fact_id in predecessors:
            raise ClosureError(f"self dependency: {fact_id}")
        if len(set(predecessors)) != len(predecessors):
            raise ClosureError(f"duplicate predecessor in {fact_id}")
        for pred in predecessors:
            if pred not in facts:
                raise ClosureError(f"unknown predecessor: {pred}")
            children[pred].append(fact_id)
        indegree[fact_id] = len(predecessors)
    ready = [fact_id for fact_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        fact_id = heapq.heappop(ready)
        order.append(fact_id)
        for child in sorted(children[fact_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(facts):
        raise ClosureError("fact graph contains a cycle")
    return order


def closure_order(facts: Sequence[Mapping[str, Any]], target_id: str) -> list[str]:
    """Topological order of the target's closure; the target is last."""
    indexed = _index(facts)
    closure = predecessor_closure(indexed, target_id)
    order = topological_order({fact_id: indexed[fact_id] for fact_id in closure})
    if not order or order[-1] != target_id:
        raise ClosureError("target is not the unique sink of its closure")
    return order


def render_verified_closure(
    facts: Sequence[Mapping[str, Any]], target_id: str
) -> str:
    """Deterministic Markdown rendering of the target's verified closure.

    ``facts`` are mappings with ``fact_id``, ``statement``, ``proof`` and
    ``predecessors`` (fact ids).  Facts outside the closure are ignored.  The
    statement and proof strings are emitted exactly as given (already stripped
    fact sections), lemmas in topological order, then the main theorem.
    """
    indexed = _index(facts)
    order = closure_order(facts, target_id)
    numbers = {fact_id: index for index, fact_id in enumerate(order[:-1], 1)}

    def reference(fact_id: str) -> str:
        return f"Lemma {numbers[fact_id]} (fact {fact_id})"

    sections = ["# Proof", "", CLOSURE_HEADER, ""]
    for fact_id in order:
        fact = indexed[fact_id]
        predecessors = sorted(
            (str(pred) for pred in _field(fact, "predecessors")),
            key=lambda pred: numbers.get(pred, 0),
        )
        heading = (
            f"## Main theorem · fact {fact_id}"
            if fact_id == target_id
            else f"## Lemma {numbers[fact_id]} · fact {fact_id}"
        )
        uses = ", ".join(reference(pred) for pred in predecessors) or "none"
        sections += [
            heading,
            "",
            f"**Uses:** {uses}.",
            "",
            "**Statement.**",
            "",
            str(_field(fact, "statement")).strip(),
            "",
            "**Proof.**",
            "",
            str(_field(fact, "proof")).strip(),
            "",
        ]
    return "\n".join(sections).rstrip() + "\n"
