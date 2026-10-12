"""Kimi K3 tasks for the problem-status updater (through the sponsored broker).

Two small, strictly validated tasks, both run with ``client_id='ansatze-problems'``
via :func:`agent_monitor.kimi_tasks.call_json`:

``match_claims``
    One newly published item (arXiv abstract, Palomar registration, news link)
    and at most five deterministic candidate problems.  The model may only pick
    candidate ids, a relation from a fixed list and a confidence, and must quote
    the item verbatim.  It has no category field: the model can never mark a
    problem solved, and its matches are published only as unreviewed signals.

``article_change_triage``
    Text added to a linked Wikipedia article since the last check.  The model
    says whether the change is about the problem's status and quotes the added
    text verbatim.

Fetched text is sent only as delimited data; titles, links and dates come from
our own fetch, never from the model.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from agent_monitor import kimi_tasks
from agent_monitor.kimi_tasks import DailyBudget, KimiTaskError

CLIENT_ID = "ansatze-problems"
CACHE_KEY = "problems-refresh"
TIMEOUT_SECONDS = 90
MAX_CANDIDATES = 5
MAX_ITEM_TEXT = 1800
MAX_ADDED_TEXT = 6000
MATCH_RELATIONS = ("claims-resolution", "claims-partial", "disputes", "withdraws", "formalization", "mentions", "unrelated")
TRIAGE_RELATIONS = ("claims-resolution", "claims-partial", "disputes", "withdraws", "formalization", "mentions", "none")
CONFIDENCE = ("low", "medium", "high")

MATCH_SYSTEM = """You link one newly published mathematical item to entries of a catalogue of research problems.
You receive the item (title and text) and up to five candidate problems (id, title, short summary).
For each candidate the item is about, choose exactly one relation:
- claims-resolution: the item claims to answer the whole candidate problem (proof, disproof or counterexample);
- claims-partial: the item claims a special case, a part, a bound or a variant of the candidate problem;
- disputes: the item disputes or finds an error in a claimed solution of the candidate problem;
- withdraws: the item withdraws a claimed solution of the candidate problem;
- formalization: the item formalizes (for example in Lean) the statement or a known proof of the candidate problem;
- mentions: the item only mentions the candidate problem;
- unrelated: the item is not about the candidate problem.
A claim is not an accepted result: never judge correctness and never say a problem is solved.
Quote the item exactly (8 to 240 characters copied verbatim from its title or text) to support each relation other than unrelated.
Return {"matches": [{"problem_id": "<candidate id>", "relation": "<relation>", "confidence": "low|medium|high", "evidence_quote": "<verbatim quote>"}]} with at most one match per candidate and no other keys."""

TRIAGE_SYSTEM = """You read text that was added to a Wikipedia article about a research problem since the last check.
Decide whether the added text reports a change in the problem's status: a claimed or accepted solution, a partial result,
a dispute, a withdrawal or a formalization. Edits to wording, references or unrelated sections are not status changes.
Never judge whether a claim is correct.
Return {"status_relevant": true|false, "relation": "<claims-resolution|claims-partial|disputes|withdraws|formalization|mentions|none>",
"evidence_quote": "<8 to 240 characters copied verbatim from the added text, or an empty string when not relevant>"} and no other keys."""


def parse_matches(value: Mapping[str, Any], candidate_ids: set[str], source_text: str) -> list[dict[str, str]]:
    """Strict validator for ``match_claims`` replies; raises ValueError on anything unexpected."""
    if not isinstance(value, Mapping) or set(value) != {"matches"} or not isinstance(value["matches"], list):
        raise ValueError("schema")
    if len(value["matches"]) > len(candidate_ids):
        raise ValueError("too many matches")
    matches, seen = [], set()
    for row in value["matches"]:
        if not isinstance(row, Mapping) or set(row) != {"problem_id", "relation", "confidence", "evidence_quote"}:
            raise ValueError("match keys")
        problem_id, relation = row["problem_id"], row["relation"]
        if problem_id not in candidate_ids or problem_id in seen:
            raise ValueError("unknown or duplicate candidate")
        if relation not in MATCH_RELATIONS or row["confidence"] not in CONFIDENCE:
            raise ValueError("enum")
        seen.add(problem_id)
        if relation == "unrelated":
            continue
        quote = kimi_tasks.exact_quote(row["evidence_quote"], source_text)
        matches.append({"problem_id": problem_id, "relation": relation, "confidence": row["confidence"],
                        "evidence_quote": quote})
    return matches


def parse_triage(value: Mapping[str, Any], added_text: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"status_relevant", "relation", "evidence_quote"}:
        raise ValueError("schema")
    relevant, relation = value["status_relevant"], value["relation"]
    if not isinstance(relevant, bool) or relation not in TRIAGE_RELATIONS:
        raise ValueError("enum")
    if not relevant or relation == "none":
        return {"status_relevant": False}
    return {"status_relevant": True, "relation": relation,
            "evidence_quote": kimi_tasks.exact_quote(value["evidence_quote"], added_text)}


def match_claims(item: Mapping[str, Any], candidates: list[Mapping[str, Any]], *, budget: DailyBudget | None,
                 request: Callable[..., dict] | None = None) -> list[dict[str, str]]:
    """Relations between one item and up to five candidate problems (validated; may be empty)."""
    rows = [{"id": str(row["id"]), "title": str(row.get("title") or "")[:300],
             "summary": " ".join(str(row.get("summary") or "").split())[:600]} for row in candidates[:MAX_CANDIDATES]]
    if not rows:
        return []
    title = " ".join(str(item.get("title") or "").split())[:300]
    text = " ".join(str(item.get("text") or "").split())[:MAX_ITEM_TEXT]
    payload = {"item": {"title": title, "text": text}, "candidates": rows}
    source_text = f"{title}\n{text}"
    ids = {row["id"] for row in rows}
    return kimi_tasks.call_json(
        "match_claims", MATCH_SYSTEM, payload, client_id=CLIENT_ID, cache_key=CACHE_KEY,
        validate=lambda value: {"matches": parse_matches(value, ids, source_text)},
        max_tokens=2048, effort="low", timeout=TIMEOUT_SECONDS, budget=budget, request=request,
    )["matches"]


def article_change_triage(change: Mapping[str, Any], *, budget: DailyBudget | None,
                          request: Callable[..., dict] | None = None) -> dict[str, Any]:
    added = str(change.get("added_text") or "")[:MAX_ADDED_TEXT]
    if not added.strip():
        return {"status_relevant": False}
    payload = {"problem": {"title": str(change.get("title") or "")[:300]},
               "article": {"title": str(change.get("article_title") or "")[:200], "added_text": added}}
    return kimi_tasks.call_json(
        "article_change_triage", TRIAGE_SYSTEM, payload, client_id=CLIENT_ID, cache_key=CACHE_KEY,
        validate=lambda value: parse_triage(value, added),
        max_tokens=1024, effort="low", timeout=TIMEOUT_SECONDS, budget=budget, request=request,
    )


__all__ = ["CLIENT_ID", "DailyBudget", "KimiTaskError", "article_change_triage", "match_claims",
           "parse_matches", "parse_triage"]
