"""Deterministic exploring-graph propagation (CGM curator.md edge algebra).

The CGM curator contract calls its propagation rules "mechanical, not judgment
calls"; this module executes them in code and treats the curator model's reply
only as scores, comments and non-terminal statuses.  Adapted from
``agents/contracts/curator.md`` (Apache-2.0, Copyright 2026 FrenzyMath).

Rules applied here:

* ``goal:root`` is ``closed`` iff the target fact was verifier-accepted -- the
  CGM deliverable is the target statement standing as a fact, so goal
  transformations (reduces-to / equivalent-to / generalizes) never close the
  root on their own in this pipeline;
* a subgoal goal node is ``closed`` iff a verifier-accepted fact supports it;
* AND groups: the assembly of the target theorem is an implicit member of
  every plan's group (as CGM requires the reassembly member for localization),
  so a plan is ``closed`` only when its own attempt produced the accepted
  target; all listed members closed without that assembly leaves it
  ``active``; a ``dead`` member kills the plan's route through that group;
* once the target is accepted, a subgoal that never closed keeps its
  non-terminal status (the console shows it as "not needed"): an accepted
  assembly does not require every listed member; the annotation text itself
  is a pinned phase-one artifact and is left unchanged;
* OR branches (alternative-of) are independent: nothing propagates;
* the model may never close a node; closed nodes score 1.0 and dead nodes 0.0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .parsing import CuratorNote
from .schema import GOAL_ROOT


@dataclass(frozen=True)
class PlanShape:
    node_id: str
    subgoals: tuple[str, ...]
    accepted: bool = False


@dataclass(frozen=True)
class Annotation:
    node_id: str
    status: str
    score: float | None
    comment: str
    evidence: tuple[str, ...]


def propagate(
    *,
    plans: Sequence[PlanShape],
    closed_goals: Mapping[str, Sequence[str]],
    root_closed_by: str | None,
    notes: Mapping[str, CuratorNote],
    default_evidence: Mapping[str, Sequence[str]] | None = None,
) -> list[Annotation]:
    """Combine model notes with the mechanical rules; stable output order.

    ``closed_goals`` maps a subgoal node id to the verifier-accepted fact ids
    supporting it; ``root_closed_by`` is the accepted target fact id, if any;
    ``notes`` are the parsed curator notes keyed by real node id (their cited
    aliases already resolved to node ids).
    """
    evidence_defaults = default_evidence or {}
    out: list[Annotation] = []

    def build(node_id: str, status: str, reason: str, evidence: Sequence[str]) -> Annotation:
        note = notes.get(node_id)
        comment_parts = [reason] if reason else []
        if note is not None and note.comment:
            comment_parts.append(note.comment)
        if status == "closed":
            score: float | None = 1.0
        elif status == "dead":
            score = 0.0
        else:
            score = note.score if note is not None else None
        cited = list(dict.fromkeys([*evidence, *(note.cited if note else ())]))
        return Annotation(
            node_id=node_id,
            status=status,
            score=score,
            comment=" ".join(comment_parts).strip() or "No curator judgment available.",
            evidence=tuple(cited),
        )

    def model_status(node_id: str, *, allow_dead: bool) -> str | None:
        note = notes.get(node_id)
        if note is None or note.status is None or note.status == "closed":
            return None
        if note.status == "dead" and not allow_dead:
            return "blocked"
        return note.status

    subgoal_status: dict[str, str] = {}
    subgoal_rows: list[Annotation] = []
    for plan in plans:
        for goal in plan.subgoals:
            facts = list(closed_goals.get(goal, ()))
            if facts:
                status = "closed"
                reason = "Deterministic: closed by verifier-accepted fact " + ", ".join(facts) + "."
            else:
                status = model_status(goal, allow_dead=True) or "open"
                reason = "" if goal in notes else "Deterministic: no verifier-accepted fact yet."
            subgoal_status[goal] = status
            subgoal_rows.append(
                build(goal, status, reason, [*facts, *evidence_defaults.get(goal, ())])
            )

    if root_closed_by:
        root = build(
            GOAL_ROOT,
            "closed",
            f"Deterministic: target fact {root_closed_by} was verifier-accepted.",
            [root_closed_by, *evidence_defaults.get(GOAL_ROOT, ())],
        )
    else:
        status = model_status(GOAL_ROOT, allow_dead=False) or "active"
        root = build(
            GOAL_ROOT,
            status,
            "Deterministic: the target is not verifier-accepted.",
            evidence_defaults.get(GOAL_ROOT, ()),
        )
    out.append(root)

    for plan in plans:
        members = [subgoal_status[goal] for goal in plan.subgoals]
        if plan.accepted:
            status, reason = "closed", "Deterministic: this plan's attempt produced the accepted target."
        elif members and all(member == "closed" for member in members):
            status = "active"
            reason = (
                "Deterministic: every listed subgoal is closed, but the assembly of "
                "the target theorem is not verifier-accepted."
            )
        elif any(member == "dead" for member in members):
            status, reason = "dead", "Deterministic: a member of its AND group is dead."
        else:
            status = model_status(plan.node_id, allow_dead=True) or "open"
            reason = ""
        out.append(build(plan.node_id, status, reason, evidence_defaults.get(plan.node_id, ())))
    out.extend(subgoal_rows)
    return out
