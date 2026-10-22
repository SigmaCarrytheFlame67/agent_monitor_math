"""Pure helpers over a content-addressed fact DAG.

``render_verified_closure`` is the single deterministic rendering of a target
fact's predecessor closure.  The runner publishes it as ``proof.md`` and
``engine_graph`` recomputes it from the fact files during attestation, so the
two can never disagree about what "the verified proof" is.  Phase-2 callers
(for example a delegating Kimi runner) should reuse it rather than re-render.

There are two renderings, named by the receipt's ``proof_rendering``, and a
published one never changes.  ``verified-closure@1`` numbers the lemmas
("Lemma 1", "Lemma 2", ...) in topological order with a fact-id tie-break;
``verified-closure@2`` names each lemma by the label its proofs cite it by
("Lemma L1") and breaks ties by that label.
"""
from __future__ import annotations

import heapq
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

CLOSURE_HEADER = (
    "This proof is the verified fact closure of the target theorem. Each lemma "
    "and the main theorem were accepted one at a time by an independent, "
    "response-only model verifier that saw only that statement, its proof, and "
    "the statements of the facts it cites. Model-verifier acceptance is not a "
    "Lean or other formal certificate."
)
# A lemma label names the lemma in a heading: one token, as long as a receipt
# label may be.
_LABEL_RE = re.compile(r"\S{1,32}")
_DIGITS_RE = re.compile(r"([0-9]+)")


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


def natural_key(label: str) -> tuple[Any, ...]:
    """Sort key that compares digit runs by value, so "L2" precedes "L10"."""
    parts = _DIGITS_RE.split(label)
    # Split on a captured group: the digit runs sit at the odd positions.
    return tuple(int(part) if position % 2 else part for position, part in enumerate(parts))


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


def topological_order(
    facts: Mapping[str, Mapping[str, Any]],
    *,
    key: Callable[[str], Any] | None = None,
) -> list[str]:
    """Kahn's order; of the facts ready at once, the smallest ``key`` comes
    first (default: the fact id, a lexicographic tie-break identical to
    engine_graph)."""
    rank = key or (lambda fact_id: fact_id)
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
    # Fact ids are unique, so a tie on the key always falls to the fact id.
    ready = [(rank(fact_id), fact_id) for fact_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _rank, fact_id = heapq.heappop(ready)
        order.append(fact_id)
        for child in sorted(children[fact_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, (rank(child), child))
    if len(order) != len(facts):
        raise ClosureError("fact graph contains a cycle")
    return order


def _lemma_labels(
    closure: set[str], target_id: str, labels: Mapping[str, str]
) -> dict[str, str]:
    """The label of every lemma of the closure (and of the target, if given).

    Every lemma needs a one-token label and no two facts of the closure may
    share one; otherwise the closure cannot be rendered by label.
    """
    named: dict[str, str] = {}
    for fact_id in sorted(closure):
        label = labels.get(fact_id)
        if label is None and fact_id == target_id:
            continue
        if not isinstance(label, str) or _LABEL_RE.fullmatch(label) is None:
            raise ClosureError(f"fact has no usable label: {fact_id}")
        named[fact_id] = label
    if len(set(named.values())) != len(named):
        raise ClosureError("two facts of the closure share a label")
    return named


def closure_order(
    facts: Sequence[Mapping[str, Any]],
    target_id: str,
    *,
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    """Topological order of the target's closure; the target is last.

    Facts that are ready at once are taken by fact id, or with ``labels``
    (fact id -> label) in natural label order and then by fact id.
    """
    indexed = _index(facts)
    closure = predecessor_closure(indexed, target_id)
    named = None if labels is None else _lemma_labels(closure, target_id, labels)
    order = topological_order(
        {fact_id: indexed[fact_id] for fact_id in closure},
        # The target is ready only after every other fact, so it needs no label.
        key=None if named is None else (lambda fact_id: natural_key(named.get(fact_id, ""))),
    )
    if not order or order[-1] != target_id:
        raise ClosureError("target is not the unique sink of its closure")
    return order


def render_verified_closure(
    facts: Sequence[Mapping[str, Any]],
    target_id: str,
    *,
    labels: Mapping[str, str] | None = None,
) -> str:
    """Deterministic Markdown rendering of the target's verified closure.

    ``facts`` are mappings with ``fact_id``, ``statement``, ``proof`` and
    ``predecessors`` (fact ids).  Facts outside the closure are ignored.  The
    statement and proof strings are emitted exactly as given (already stripped
    fact sections), lemmas in topological order, then the main theorem.

    Without ``labels`` this is ``verified-closure@1``: lemma ``k`` is "Lemma
    k".  ``labels`` (fact id -> the label the proofs cite the fact by, for
    every lemma of the closure) gives ``verified-closure@2``: each lemma is
    "Lemma <label>" and ties are broken by label; a lemma without a one-token
    label, or two closure facts with the same label, raise ``ClosureError``.
    """
    indexed = _index(facts)
    order = closure_order(facts, target_id, labels=labels)
    names = (
        {fact_id: str(number) for number, fact_id in enumerate(order[:-1], 1)}
        if labels is None
        else {fact_id: labels[fact_id] for fact_id in order[:-1]}
    )
    position = {fact_id: index for index, fact_id in enumerate(order)}

    def reference(fact_id: str) -> str:
        return f"Lemma {names[fact_id]} (fact {fact_id})"

    sections = ["# Proof", "", CLOSURE_HEADER, ""]
    for fact_id in order:
        fact = indexed[fact_id]
        predecessors = sorted(
            (str(pred) for pred in _field(fact, "predecessors")),
            key=lambda pred: position[pred],
        )
        heading = (
            f"## Main theorem · fact {fact_id}"
            if fact_id == target_id
            else f"## Lemma {names[fact_id]} · fact {fact_id}"
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
