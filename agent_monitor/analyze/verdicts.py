"""Strict, fail-closed parsing of Analyze model replies.

The node verdict gates what the report says about someone's manuscript, so
its reply must contain exactly one object that mentions ``"verdict"`` (an
echoed template, a quoted blob or text copied from the manuscript makes the
reply ambiguous and it is rejected), with only the contract's keys and enum
values.  Normalization: any critical error makes the verdict ``error``; gaps
alone make it ``gap``; ``checked`` with findings is corrected; ``error``
without a critical error, ``gap`` without a gap and ``unclear`` without a
reason are rejected (the runner then sends one format reminder).  A reply
that still cannot be parsed becomes ``unclear`` ("the verifier output could
not be interpreted") — never ``checked`` and never an accusation of error.
URLs (and ``javascript:``, ``data:``, ``vbscript:`` and ``file:`` targets) are
removed from every model-written string; the report also breaks up Markdown
link syntax, so no model text becomes a link.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from agent_monitor.cgm import parsing as cgm_parsing
from agent_monitor.cgm.parsing import ParseError
from agent_monitor.cgm.schema import normalize_text

MODEL_VERDICTS = ("checked", "gap", "error", "unclear")
VERDICTS = ("checked", "gap", "error", "unclear", "not_checked")
ROLES = ("definition", "assumption", "lemma", "proposition", "theorem", "corollary", "claim", "remark")
CONFIDENCE = ("high", "medium", "low")
HYPOTHESES = ("yes", "partly", "no")
NODE_KEYS = frozenset({
    "claim", "role", "depends_on", "missing_dependencies", "external",
    "verification_report", "verdict", "notes", "confidence",
})
REPORT_KEYS = frozenset({"summary", "critical_errors", "gaps"})
MAX_ISSUES = 8
_URL_RE = re.compile(r"(?i)\b(?:(?:https?|ftp)://|www\.|(?:javascript|vbscript|data|file):)\S+")
_VERDICT_KEY_RE = re.compile(r'"verdict"\s*:')
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def clean_text(value: object, limit: int) -> str:
    """Model-written text: line-normalized, URLs removed, bounded."""
    if not isinstance(value, str):
        return ""
    text = _URL_RE.sub("[link removed]", normalize_text(value)).strip()
    return text[:limit]


@dataclass(frozen=True)
class Issue:
    location: str
    issue: str

    def as_dict(self) -> dict[str, str]:
        return {"location": self.location, "issue": self.issue}


@dataclass(frozen=True)
class NodeVerdict:
    verdict: str
    summary: str
    critical_errors: tuple[Issue, ...] = ()
    gaps: tuple[Issue, ...] = ()
    claim: str = ""
    role: str = ""
    depends_on: tuple[dict[str, str], ...] = ()
    missing_dependencies: tuple[dict[str, str], ...] = ()
    external: tuple[dict[str, str], ...] = ()
    notes: str = ""
    confidence: str = ""
    normalized: bool = False
    model_verdict: str = ""
    interpreted: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "model_verdict": self.model_verdict,
            "normalized": self.normalized,
            "interpreted": self.interpreted,
            "claim": self.claim,
            "role": self.role,
            "depends_on": list(self.depends_on),
            "missing_dependencies": list(self.missing_dependencies),
            "external": list(self.external),
            "verification_report": {
                "summary": self.summary,
                "critical_errors": [item.as_dict() for item in self.critical_errors],
                "gaps": [item.as_dict() for item in self.gaps],
            },
            "notes": self.notes,
            "confidence": self.confidence,
        }


def find_verdict_object(text: str, *, allowed: frozenset[str] = NODE_KEYS) -> dict[str, Any]:
    """The reply's single verdict object; any ambiguity fails closed."""
    if cgm_parsing.has_pseudo_tool_markup(text):
        raise ParseError("reply contains prohibited pseudo-tool markup")
    stripped = str(text or "").strip()
    if not stripped:
        raise ParseError("empty reply")
    bearing = []
    for span in cgm_parsing._balanced_objects(stripped):
        value = cgm_parsing._load_object(span)
        if _VERDICT_KEY_RE.search(span) or cgm_parsing._contains_key(value, "verdict"):
            bearing.append(value)
    if not bearing:
        raise ParseError("no JSON object with 'verdict'")
    if len(bearing) > 1:
        raise ParseError("reply contains more than one verdict object; return exactly one")
    value = bearing[0]
    if not isinstance(value, dict) or "verdict" not in value:
        raise ParseError("the verdict object is not valid JSON")
    if not set(value).issubset(allowed):
        raise ParseError("verdict object has keys outside the output contract")
    report = value.get("verification_report")
    if isinstance(report, Mapping) and (
        not set(report).issubset(REPORT_KEYS) or cgm_parsing._contains_key(report, "verdict")
    ):
        raise ParseError("verification_report has keys outside the output contract")
    for key in NODE_KEYS - {"verdict"}:
        if key in value and cgm_parsing._contains_key(value[key], "verdict"):
            raise ParseError("a nested verdict is not allowed")
    return value


def _issues(value: object, field_name: str) -> tuple[Issue, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ParseError(f"{field_name} is not a list")
    items: list[Issue] = []
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            items.append(Issue("unspecified", clean_text(entry, 1000)))
        elif isinstance(entry, Mapping):
            issue = clean_text(entry.get("issue"), 1000)
            if not issue:
                raise ParseError(f"{field_name} entry lacks an issue")
            location = clean_text(entry.get("location"), 200) or "unspecified"
            items.append(Issue(" ".join(location.split()), issue))
        else:
            raise ParseError(f"{field_name} entry is malformed")
    return tuple(items[:MAX_ISSUES])


def _records(value: object, fields: dict[str, int], *, limit: int, name: str) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ParseError(f"{name} is not a list")
    out = []
    for entry in value[:limit]:
        if not isinstance(entry, Mapping):
            raise ParseError(f"{name} entry is malformed")
        out.append({key: clean_text(entry.get(key), size) for key, size in fields.items()})
    return tuple(out)


def parse_node_verdict(text: str, *, known_ids: Iterable[str] = ()) -> NodeVerdict:
    """Validate one node-check reply (see the module docstring)."""
    raw = find_verdict_object(text)
    model_verdict = str(raw.get("verdict") or "").strip().lower()
    if model_verdict not in MODEL_VERDICTS:
        raise ParseError("verdict must be checked, gap, error or unclear")
    report = raw.get("verification_report")
    if not isinstance(report, Mapping):
        raise ParseError("reply lacks verification_report")
    critical = _issues(report.get("critical_errors"), "critical_errors")
    gaps = _issues(report.get("gaps"), "gaps")
    summary = clean_text(report.get("summary"), 2000)
    notes = clean_text(raw.get("notes"), 1200)
    role = str(raw.get("role") or "").strip().lower()
    confidence = str(raw.get("confidence") or "").strip().lower()
    if role and role not in ROLES:
        role = ""
    if confidence and confidence not in CONFIDENCE:
        raise ParseError("confidence must be high, medium or low")
    known = set(known_ids)
    depends = []
    for entry in _records(raw.get("depends_on"), {"id": 80, "how": 300}, limit=24, name="depends_on"):
        identifier = entry["id"].strip().lower()
        if identifier in known:
            depends.append({"id": identifier, "how": entry["how"]})
    missing = _records(raw.get("missing_dependencies"), {"description": 400, "suggested_id": 80},
                       limit=8, name="missing_dependencies")
    external = []
    for entry in _records(raw.get("external"), {"citation": 200, "statement_as_used": 600, "hypotheses_checked": 10},
                          limit=12, name="external"):
        checked = entry["hypotheses_checked"].strip().lower()
        if not entry["citation"]:
            continue
        external.append(dict(entry, hypotheses_checked=checked if checked in HYPOTHESES else "unknown"))
    verdict = model_verdict
    normalized = False
    if critical:
        verdict = "error"
    elif gaps:
        verdict = "gap"
    elif model_verdict == "error":
        raise ParseError('verdict "error" needs at least one critical error')
    elif model_verdict == "gap":
        raise ParseError('verdict "gap" needs at least one gap')
    if model_verdict == "unclear" and not (critical or gaps):
        if not (notes or summary):
            raise ParseError('verdict "unclear" needs a reason in notes')
        verdict = "unclear"
    if verdict != model_verdict:
        normalized = True
    return NodeVerdict(
        verdict=verdict,
        summary=summary or {"checked": "No issues found.", "gap": "Gaps found.", "error": "Errors found.",
                            "unclear": "Could not be judged."}[verdict],
        critical_errors=critical,
        gaps=gaps,
        claim=clean_text(raw.get("claim"), 4000),
        role=role,
        depends_on=tuple(depends),
        missing_dependencies=missing,
        external=tuple(external),
        notes=notes,
        confidence=confidence,
        normalized=normalized,
        model_verdict=model_verdict,
    )


def uninterpretable(reason: str) -> NodeVerdict:
    """The verdict of a check whose reply could not be parsed."""
    return NodeVerdict(
        verdict="unclear",
        summary=f"The verifier output could not be interpreted: {clean_text(reason, 300)}",
        notes="The model's reply did not follow the output contract, so no judgement is recorded.",
        interpreted=False,
    )


def merge_parts(parts: list[NodeVerdict]) -> NodeVerdict:
    """One verdict for a proof checked in parts: error > gap > unclear >
    checked, findings concatenated with their part numbers."""
    if len(parts) == 1:
        return parts[0]
    order = {"error": 3, "gap": 2, "unclear": 1, "checked": 0}
    worst = max(parts, key=lambda item: order[item.verdict]).verdict
    critical: list[Issue] = []
    gaps: list[Issue] = []
    external: list[dict[str, str]] = []
    depends: dict[str, dict[str, str]] = {}
    notes = []
    for index, part in enumerate(parts, 1):
        critical += [Issue(f"part {index}: {item.location}", item.issue) for item in part.critical_errors]
        gaps += [Issue(f"part {index}: {item.location}", item.issue) for item in part.gaps]
        external += [item for item in part.external if item not in external]
        for item in part.depends_on:
            depends.setdefault(item["id"], item)
        if part.notes:
            notes.append(f"Part {index}: {part.notes}")
    summary = " ".join(f"Part {index}: {part.summary}" for index, part in enumerate(parts, 1))
    return NodeVerdict(
        verdict=worst,
        summary=summary[:2000],
        critical_errors=tuple(critical[:MAX_ISSUES]),
        gaps=tuple(gaps[:MAX_ISSUES]),
        claim=next((part.claim for part in parts if part.claim), ""),
        role=next((part.role for part in parts if part.role), ""),
        depends_on=tuple(depends.values())[:24],
        missing_dependencies=tuple(item for part in parts for item in part.missing_dependencies)[:8],
        external=tuple(external[:12]),
        notes=(" ".join(notes) + f" (checked in {len(parts)} parts; reasoning that crosses parts was not checked as a whole)")[:1200],
        confidence=min((part.confidence for part in parts if part.confidence),
                       key=lambda value: CONFIDENCE.index(value), default=""),
        normalized=any(part.normalized for part in parts),
        model_verdict=worst,
        interpreted=all(part.interpreted for part in parts),
    )


def _numbers_supported(text: str, evidence: str) -> bool:
    haystack = " ".join(evidence.split())
    return all(number in haystack for number in _NUMBER_RE.findall(text))


def parse_paper_map(text: str, *, node_ids: Iterable[str], evidence: str) -> dict[str, Any]:
    """Validate the paper-map reply: ids from the result list only, no URLs,
    and the summary dropped when it states a number that is not in the input."""
    raw = cgm_parsing.find_json_object(text, required_key="main_results")
    allowed = set(node_ids)
    main = []
    for identifier in raw.get("main_results") or []:
        value = str(identifier or "").strip().lower()
        if value in allowed and value not in main:
            main.append(value)
    if not isinstance(raw.get("main_results"), list):
        raise ParseError("main_results must be a list")
    focus = [str(item).strip().lower() for item in raw.get("focus_results") or [] if str(item).strip().lower() in allowed]
    summary = clean_text(raw.get("summary"), 900)
    if summary and not _numbers_supported(summary, evidence):
        summary = ""
    techniques = [clean_text(item, 120) for item in (raw.get("techniques") or [])[:6] if clean_text(item, 120)]
    literature = []
    for entry in (raw.get("literature") or [])[:8]:
        if isinstance(entry, Mapping):
            citation = clean_text(entry.get("citation"), 120)
            if citation:
                literature.append({"citation": citation, "role": clean_text(entry.get("role"), 200)})
    return {
        "summary": summary,
        "main_results": main[:8],
        "focus_results": list(dict.fromkeys(focus))[:12],
        "techniques": techniques,
        "literature": literature,
    }


def parse_segments(text: str) -> list[dict[str, Any]]:
    """Items of a segmentation reply (quotes are checked against the text by
    ``text_structure.apply_model_segments``)."""
    raw = cgm_parsing.find_json_object(text, required_key="items")
    items = raw.get("items")
    if not isinstance(items, list):
        raise ParseError("items must be a list")
    out = []
    for entry in items[:24]:
        if not isinstance(entry, Mapping):
            continue
        out.append({
            "kind": str(entry.get("kind") or "")[:20],
            "label": clean_text(entry.get("label"), 60),
            "statement_quote": str(entry.get("statement_quote") or "")[:1200],
            "proof_start_quote": str(entry.get("proof_start_quote") or "")[:300],
            "proof_end_quote": str(entry.get("proof_end_quote") or "")[:300],
            "uses": [clean_text(item, 60) for item in (entry.get("uses") or [])[:16] if isinstance(item, str)],
        })
    return out
