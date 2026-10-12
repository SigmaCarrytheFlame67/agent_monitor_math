"""Problem catalogue v3: categories, badges and evidence derived deterministically.

Inputs: the source fact files (``agent_monitor/data/problem_sources/``), the
editorial curation (statements, sourced partial results and claims), the
deep-research file and the operator decisions.  Output: the static catalogue
served at ``/research/open-problems.json`` plus an empty status overlay.

A row's category comes from, in order: an operator decision; a deterministic
source-of-record signal (teorth state, Kourovka header/part markers, the Clay
listing); research for that key (only when included, not "uncertain" and backed
by qualifying evidence); reviewed curation; otherwise "open".  Research can
never make a row solved without an evidence item whose acceptance is refereed,
accepted-by-maintainer or broadly-accepted, and withdrawn or refuted claims
never count.  A category that rests on dated answers or claims records when the
first of them appeared (``status_changed``); a source-marker category records no
change date, since a marker is dated by its edition.  The build is a pure
function of its inputs: the same inputs give byte-identical output.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from agent_monitor.problem_sources import (ERDOS_OPEN_STATES, ERDOS_SOLVED_STATES, clean_text, openai_manuscript_dir,
                                           safe_url)

SCHEMA_VERSION = 3
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,159}")
CATEGORIES = (
    {"id": "solved", "label": "Solved",
     "description": "Answered, and the answer is accepted (source of record, refereed publication, or broad expert acceptance)."},
    {"id": "ai-proposed", "label": "AI proposed",
     "description": "An AI system produced a claimed full solution that mathematicians have not yet confirmed. Treated as unsolved until confirmed."},
    {"id": "partial", "label": "Partial",
     "description": "An explicitly stated part of the problem is answered; the rest remains open."},
    {"id": "open", "label": "Open",
     "description": "No accepted answer. Partial results, human claims under review, and AI partial results are shown as badges."},
)
CATEGORY_IDS = tuple(item["id"] for item in CATEGORIES)
RULES = {"solved": ("S1", "S2"), "ai-proposed": ("A1", "A2"), "partial": ("P1", "P2"), "open": ("O",)}
RULE_TEXT = {
    "S1": "Source-of-record marker: the source that maintains the problem lists it as answered.",
    "S2": "Refereed or broadly accepted full answer.",
    "A1": "Source marker: the maintainers flag an unconfirmed AI-produced solution of the whole problem.",
    "A2": "Documented AI-produced full claim that has not been confirmed.",
    "P1": "Source marker on an explicitly stated part.",
    "P2": "Accepted answer to an explicitly stated part.",
    "O": "No accepted answer recorded.",
}
BADGES = {
    "ai-partial": ("AI partial result", "An AI system produced a result on a part, special case or variant; it does not answer the whole problem."),
    "ai-confirmed": ("AI solution confirmed", "The source of record accepted a solution produced by an AI system after expert checking."),
    "ai-assisted-solution": ("AI-assisted solution", "The accepted solution was produced with substantial help from an AI system."),
    "human-claim": ("Claim under review", "A human-authored claimed solution has not yet been accepted."),
    "lean-pending": ("Lean proof awaiting review", "A Lean proof is published upstream but has not been reviewed by people; not re-checked by this server."),
    "withdrawn-claim": ("Withdrawn claim", "A claimed solution was withdrawn by its authors; withdrawn claims never count."),
    "disputed": ("Disputed", "A published claim or its formalization is publicly disputed."),
    "sources-conflict": ("Sources disagree", "Sources disagree about the status; the source of record decides the category."),
    "prize": ("Prize", "A prize is attached to this problem."),
    "ambiguous-statement": ("Ambiguous statement", "The intended statement is uncertain or open to interpretation."),
    "not-in-current-edition": ("Not in current edition", "The current edition of the source no longer lists this problem; the entry is kept for its links and discussion."),
    "formal-statement": ("Lean statement", "A Lean statement of the problem exists (a statement, not a proof)."),
    "upstream-substatus": ("Upstream sub-status", "The source of record lists the problem as open with a sub-status, shown as the label (from source_status)."),
    "reported-signal": ("New report (unreviewed)", "Matched automatically from primary sources; not reviewed; does not change the category."),
}
CATALOGUE_BADGES = tuple(key for key in BADGES if key != "reported-signal")
EVIDENCE_KINDS = ("accepted-result", "claim", "partial-result", "withdrawal", "refutation", "source-marker",
                  "formalization", "survey", "other")
ACTORS = ("human", "ai", "ai-assisted", "maintainer", "unknown")
SCOPES = ("full", "part", "special-case", "related", "variant")
OUTCOMES = ("proof", "disproof", "counterexample", "independence", "bound", "construction", "computation", "other")
ACCEPTANCE = ("refereed", "accepted-by-maintainer", "broadly-accepted", "preprint", "announced",
              "lean-checked-unreviewed", "withdrawn", "refuted", "disputed", "unknown")
QUALIFYING = frozenset({"refereed", "accepted-by-maintainer", "broadly-accepted"})
DATE = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2})?)?")
RESEARCH_KEY = re.compile(r"(?:wpu-\d{3}|wps-\d{3}|erdos-\d{4}|kourovka-\d{2}-\d{3}|hilbert-\d{2}|hilbert-16-ii|"
                          r"hilbert-10-q|smale-\d{2}|clay-[a-z0-9-]{1,60}|ramanujan-[a-z0-9-]{1,40})")
VERIFIED = ("confirmed", "corrected", "uncertain", "unchecked")
RELATIONS = ("same", "part", "related", "none")
TEXT_LIMITS = {"title": 300, "claimant": 200, "venue": 200, "note": 240}
COLLECTION_ORDER = ("erdos", "kourovka", "clay", "hilbert", "smale", "ramanujan", "wikipedia", "wikipedia-solved")
# Fact files under agent_monitor/data/problem_sources/ (built by scripts/refresh_problem_sources.py).
FACT_FILES = {"erdos": "erdos.json", "ai_wiki": "erdos_ai_wiki.json", "kourovka": "kourovka.json",
              "formal_conjectures": "formal_conjectures.json", "openai_math": "openai_math.json",
              "wikipedia": "wikipedia.json", "clay": "clay.json"}
DEFAULT_FORMAL_STATUS = "No proof certified by this catalogue"

# Millennium problems; the Clay page decides which are solved.
CLAY_PROBLEMS = (
    ("birch-and-swinnerton-dyer-conjecture", "Birch and Swinnerton-Dyer conjecture", ["number theory", "elliptic curves"],
     "Relate the rank of an elliptic curve over the rationals to its L-function at s = 1.", "BSD"),
    ("hodge-conjecture", "Hodge conjecture", ["algebraic geometry", "topology"],
     "Determine which rational Hodge classes on smooth complex projective varieties arise from algebraic cycles.", None),
    ("navier-stokes-equation", "Navier–Stokes existence and smoothness", ["analysis", "partial differential equations"],
     "Resolve global existence and smoothness, or breakdown, for the three-dimensional incompressible fluid equations in the official formulation.", "NavierStokes"),
    ("p-vs-np", "P versus NP", ["computer science", "complexity theory"],
     "Determine whether every decision problem with efficiently checkable certificates also has an efficient solving algorithm.", "PvsNP"),
    ("poincare-conjecture", "Poincaré conjecture", ["topology", "geometric analysis"],
     "Every simply connected, closed three-dimensional manifold is homeomorphic to the three-sphere.", "Poincare"),
    ("riemann-hypothesis", "Riemann hypothesis", ["number theory", "analysis"],
     "Determine whether every nontrivial zero of the Riemann zeta function has real part one half.", "RiemannHypothesis"),
    ("yang-mills-the-maths-gap", "Yang–Mills existence and mass gap", ["mathematical physics", "quantum field theory"],
     "Construct the four-dimensional quantum Yang–Mills theory required by the official problem and establish a positive mass gap.", None),
)
CLAY_PRIZE = "$1,000,000"
CURATED_RATING = {"score": 2, "label": "Literature first", "confidence": "low", "method": "exploration-fit-v1",
                  "reason": "Read the statement and cited progress, then choose a tractable special case. A curated starting point does not predict a full solution."}


class CatalogueError(ValueError):
    """The inputs violate a catalogue invariant; nothing is written."""


def _text(value: object, limit: int) -> str:
    return clean_text(value, limit)


def _date(value: object) -> str | None:
    text = str(value or "").strip()[:10]
    return text if DATE.fullmatch(text) else None


def _precision(date: str) -> str:
    return {4: "year", 7: "month", 10: "day"}[len(date)]


NOT_RECHECKED = "Not re-checked by this server."


def with_label(note: object, label: str, limit: int = TEXT_LIMITS["note"]) -> str:
    """An upstream note followed by ``label``, which always fits: the note is cut at
    its last full sentence (or else at a word, with an ellipsis) when needed."""
    text = " ".join(str(note or "").split()).rstrip("…").strip()
    room = limit - len(label) - 1
    if len(text) > room:
        cut = text[:room]
        ends = [match.end() for match in re.finditer(r"(?<=[a-z0-9)\]])\.(?=\s)", cut)]
        text = cut[:ends[-1]] if ends and ends[-1] > room // 3 else cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return f"{text} {label}".strip()


def link_list(items: object, *, context: str, lenient: bool = False) -> list[dict[str, str]]:
    """Validated labelled https links (statements, results and claims).

    Curated links must all be valid; research links are filtered (``lenient``)
    and at least one valid link must remain.
    """
    if not isinstance(items, list) or not items:
        raise CatalogueError(f"{context} needs at least one source link")
    links = []
    for item in items:
        url = safe_url(item.get("url")) if isinstance(item, dict) else None
        label = _text(item.get("label"), 300) if isinstance(item, dict) else ""
        if not url or not label:
            if lenient:
                continue
            raise CatalogueError(f"{context} links must be labelled HTTPS URLs without credentials")
        links.append({"label": label, "url": url})
    if not links:
        raise CatalogueError(f"{context} has no labelled HTTPS source link")
    return links


# The teorth AI-contributions wiki rates rows with coloured discs (🟢 full solution, 🟡 partial result, …). The site
# never shows a verdict as a colour (green is reserved for Lean-kernel results), so evidence text keeps the words only.
_COLOUR_MARKS = re.compile("[\U0001F534\U0001F535\U0001F7E0-\U0001F7EB\u26AA\u26AB\u2B55]\uFE0F?\\s*")


def _no_colour_marks(value: object) -> object:
    return _COLOUR_MARKS.sub("", value) if isinstance(value, str) else value


# ASCII comparison and arrow operators in prose we write (headlines, notes), outside $…$ math: typeset as Unicode.
_ASCII_OPERATORS = (("<=>", "⇔"), ("<->", "↔"), ("->", "→"), ("=>", "⇒"), ("<=", "≤"), (">=", "≥"), ("!=", "≠"))
_INLINE_MATH = re.compile(r"\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|(?<![\\$])\$(?!\$)(?:\\.|[^$])+?\$")


def _typeset_operators(value: object) -> object:
    if not isinstance(value, str) or not any(token in value for token, _ in _ASCII_OPERATORS):
        return value
    parts, position = [], 0
    for match in _INLINE_MATH.finditer(value):
        parts.append((value[position:match.start()], True))
        parts.append((match.group(0), False))
        position = match.end()
    parts.append((value[position:], True))
    out = []
    for text, prose in parts:
        if prose:
            for token, symbol in _ASCII_OPERATORS:
                text = text.replace(token, symbol)
        out.append(text)
    return "".join(out)


def evidence_item(item: object) -> dict[str, Any]:
    """One validated evidence item (§1.1); raises CatalogueError when unusable."""
    if not isinstance(item, dict):
        raise CatalogueError("evidence must be an object")
    for field, allowed in (("kind", EVIDENCE_KINDS), ("actor", ACTORS), ("scope", SCOPES),
                           ("outcome", OUTCOMES), ("acceptance", ACCEPTANCE)):
        if item.get(field) not in allowed:
            raise CatalogueError(f"evidence {field} is not an allowed value")
    url = safe_url(item.get("url"))
    if not url:
        raise CatalogueError("evidence url must be https without credentials (≤ 500 characters)")
    title = _text(_no_colour_marks(item.get("title")), TEXT_LIMITS["title"])
    if not title:
        raise CatalogueError("evidence needs a title")
    result: dict[str, Any] = {"kind": item["kind"], "actor": item["actor"]}
    claimant = _text(item.get("claimant"), TEXT_LIMITS["claimant"])
    if claimant:
        result["claimant"] = claimant
    systems = [_text(system, 60) for system in item.get("ai_systems") or [] if isinstance(system, str) and system.strip()][:8]
    if systems:
        result["ai_systems"] = systems
    result.update(scope=item["scope"], outcome=item["outcome"])
    date = _date(item.get("date"))
    if item.get("date") not in (None, "") and not date:
        raise CatalogueError("evidence date must be YYYY, YYYY-MM or YYYY-MM-DD")
    if date:
        result["date"] = date
        result["date_precision"] = item.get("date_precision") if item.get("date_precision") in ("day", "month", "year") \
            and {"day": 10, "month": 7, "year": 4}[item["date_precision"]] <= len(date) else _precision(date)
    result["title"] = title
    result["url"] = url
    venue = _text(item.get("venue"), TEXT_LIMITS["venue"])
    if venue:
        result["venue"] = venue
    result["acceptance"] = item["acceptance"]
    note = _text(_typeset_operators(_no_colour_marks(item.get("note"))), TEXT_LIMITS["note"])
    if note:
        result["note"] = note
    return result


def _evidence_list(items: object, warnings: list[str], context: str) -> list[dict[str, Any]]:
    result = []
    for item in items if isinstance(items, list) else []:
        try:
            result.append(evidence_item(item))
        except CatalogueError as error:
            warnings.append(f"{context}: dropped evidence ({error})")
    return result


_ARXIV_LINK = re.compile(r"^https://(?:www\.)?arxiv\.org/(?:abs|pdf|html)/((?:\d{4}\.\d{4,5})|(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}))"
                         r"(v\d+)?(?:\.pdf)?/?$")
_RAW_GITHUB = re.compile(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/(.+)$")
# Optional details a later copy of the same item may supply when the first lacks them.
_FILLABLE = ("claimant", "ai_systems", "venue", "note", "date", "date_precision")


def _document(url: str) -> tuple[str, str | None]:
    """The document a link names, and its arXiv version if the link pins one.

    A fragment (a line anchor, a page) and a trailing slash do not change the
    document, a raw.githubusercontent.com link names the same file as its
    github.com/…/blob/ page, and an unversioned arXiv link names the paper in
    every version.
    """
    bare = url.split("#", 1)[0].rstrip("/")
    raw = _RAW_GITHUB.match(bare)
    if raw:
        bare = f"https://github.com/{raw.group(1)}/{raw.group(2)}/blob/{raw.group(3)}"
    match = _ARXIV_LINK.match(bare)
    if match:
        return "arxiv:" + match.group(1), match.group(2)
    return bare, None


def _same_item(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    if (first["kind"], first.get("scope")) != (second["kind"], second.get("scope")):
        return False
    if first["url"] == second["url"]:
        return True
    (doc, version), (other, other_version) = _document(first["url"]), _document(second["url"])
    return doc == other and (version is None or other_version is None or version == other_version)


def _merge_evidence(*lists: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Concatenate evidence lists, keeping the first copy of each item.

    Two items are copies when they have the same kind and scope and name the
    same document (see ``_document``): curation, research and decisions often
    cite one paper as ``…/abs/2610.08144`` and ``…/abs/2610.08144v1``.  The kept
    copy wins on every field it has; missing optional details come from later
    copies.  Callers put the most authoritative list first.
    """
    merged: list[dict[str, Any]] = []
    for items in lists:
        for item in items:
            for index, kept in enumerate(merged):
                if _same_item(kept, item):
                    extra = {key: item[key] for key in _FILLABLE if not kept.get(key) and item.get(key)}
                    if extra:
                        merged[index] = {**kept, **extra}
                    break
            else:
                merged.append(item)
    return merged


def qualifying_evidence(category: str, evidence: Iterable[dict[str, Any]], rule: str | None = None) -> list[dict[str, Any]]:
    """The live evidence items that can carry a non-open category (none for open)."""
    live = [item for item in evidence if item["acceptance"] not in ("withdrawn", "refuted")]
    # Only an answer can make a problem solved or partly answered: a listing that
    # says the problem is open, or an accepted formal statement, has outcome
    # "other".  A source-of-record marker (S1, P1) is the answer itself, even
    # when the source does not say how it was answered (teorth's "solved").
    answers = live if rule in ("S1", "P1") else [item for item in live if item["outcome"] != "other"]
    if category == "solved":
        # §1.4: never solved from research without refereed, maintainer-accepted
        # or broadly accepted evidence answering the whole problem (S2 is a full
        # answer; an answered part is at most partial).  A "refutation" counts
        # only when it refutes the conjecture itself (disproof or
        # counterexample), not when it refutes someone's claim.
        return [item for item in answers if item["acceptance"] in QUALIFYING and item["scope"] == "full"
                and (item["kind"] not in ("survey", "other", "withdrawal", "refutation")
                     or (item["kind"] == "refutation" and item["outcome"] in ("disproof", "counterexample")))]
    if category == "ai-proposed":
        return [item for item in live if item["scope"] == "full" and item["actor"] in ("ai", "ai-assisted")
                and item["kind"] in ("claim", "accepted-result")]
    if category == "partial":
        # An explicitly stated part, not a special case: otherwise every famous
        # problem with a classical special case would become partial.
        return [item for item in answers if item["scope"] == "part" and item["acceptance"] in QUALIFYING
                and item["kind"] in ("accepted-result", "partial-result", "source-marker")]
    return []


def supports_category(category: str, rule: str, evidence: list[dict[str, Any]]) -> bool:
    """Whether evidence can carry a non-deterministic category (research, curation or a decision)."""
    if category not in ("solved", "ai-proposed", "partial"):
        return True
    if category == "ai-proposed" and rule == "A1":
        return False  # a source marker is deterministic; research cannot assert it
    return bool(qualifying_evidence(category, evidence, rule))


# Evidence whose date is the event that moved a status: an answer, a claim or a
# refutation.  A source marker is dated by the edition or database update that
# carries it and a formalization by when it was written, so neither dates a
# change (the page reads evidence dates the same way).
CHANGE_KINDS = frozenset({"accepted-result", "claim", "partial-result", "refutation"})


def _date_bounds(date: str) -> tuple[str, str]:
    """The first and last day a YYYY[-MM[-DD]] date can stand for, as comparable strings."""
    if len(date) == 4:
        return f"{date}-01-01", f"{date}-12-31"
    if len(date) == 7:
        return f"{date}-01", f"{date}-31"
    return date, date


def status_changed(category: str, evidence: Iterable[dict[str, Any]]) -> str | None:
    """When the evidence behind a researched, curated or decided category first held.

    The date of the earliest answer or claim that carries the category
    (``qualifying_evidence``), never a source marker's or a formalization's
    date and never the build date.  None when one of them is undated, since it
    may be the earliest.  With mixed precisions the result is the finest date
    that surely contains the earliest one ("2005" and "2005-06-01" give "2005").
    """
    items = [item for item in qualifying_evidence(category, evidence) if item["kind"] in CHANGE_KINDS]
    if not items or not all(item.get("date") for item in items):
        return None
    bounds = [_date_bounds(item["date"]) for item in items]
    first, last = min(low for low, _ in bounds), min(high for _, high in bounds)
    # The earliest event lies between ``first`` and ``last``, both in one year.
    return next((first[:size] for size in (10, 7, 4) if first[:size] == last[:size]), None)


# Source-marker evidence is believable only where a deterministic parser reads
# the source of record: the teorth database, the Kourovka Notebook on arXiv,
# the Clay Millennium listing and formal-conjectures' maintainer categories.
_SOURCE_OF_RECORD_URL = re.compile(
    r"^https://(?:github\.com/teorth/erdosproblems/(?:blob|tree)/[^/]+/data/problems\.yaml"
    r"|raw\.githubusercontent\.com/teorth/erdosproblems/[^/]+/data/problems\.yaml"
    r"|arxiv\.org/(?:abs|pdf)/1401\.0300(?:v\d+)?(?:$|[#?])"
    r"|www\.claymath\.org/millennium(?:-problems)?/"
    r"|github\.com/google-deepmind/formal-conjectures/blob/[0-9a-f]{40}/FormalConjectures/)")
# A source-marker rule (S1, A1, P1) comes only from the parsers.  A researched
# S1/A1/P1 is read as S2/A2/P2 and must meet that rule's evidence standard.
RESEARCH_RULES = {"S1": "S2", "A1": "A2", "P1": "P2"}

# Answers and claims say whether their link is a manuscript about the problem
# (``manuscript``), so the page offers Analyze only for those.  A manuscript link
# is what the Analyze connectors read: an arXiv paper, a GitHub repository,
# folder or file, or a direct PDF, TeX or archive file.  Linked as the answer
# for more than MAX_MANUSCRIPT_PROBLEMS entries, a document is a collection
# (a wiki table, a compilation of solutions); the page used the same bound.
ANSWER_KINDS = frozenset({"accepted-result", "claim", "partial-result"})
MAX_MANUSCRIPT_PROBLEMS = 10
_ARXIV_PAPER = re.compile(r"/(?:abs|pdf|e-print|src|format)/(?:\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[a-z]{2})?/\d{7})"
                          r"(?:v\d{1,3})?(?:\.pdf)?/?", re.I)
_DIRECT_FILE = re.compile(r"\.(?:pdf|tex|zip|tar\.gz|tgz)$", re.I)
# Problem lists, statement collections and wikis are indexes, whatever their link looks like.
_INDEX_URL = re.compile(
    r"^https://(?:(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/1401\.0300(?!\d)"          # the Kourovka Notebook
    r"|(?:github\.com|raw\.githubusercontent\.com)/teorth/erdosproblems/"              # the Erdős problems database
    r"|(?:github\.com|raw\.githubusercontent\.com)/google-deepmind/formal-conjectures/"  # Lean statements
    r"|raw\.githubusercontent\.com/wiki/)")                                              # GitHub wiki pages


def manuscript_link(url: str) -> bool:
    """Whether a link is a manuscript itself rather than an index (see the comment above
    ``ANSWER_KINDS``).  In openai/math only a manuscript folder under ``preprints`` counts."""
    if _INDEX_URL.match(url):
        return False
    parts = urlsplit(url)
    host, segments = (parts.hostname or "").lower(), [segment for segment in parts.path.split("/") if segment]
    if host in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
        return bool(_ARXIV_PAPER.fullmatch(parts.path))
    if host in ("github.com", "www.github.com", "raw.githubusercontent.com") and \
            [segment.lower() for segment in segments[:2]] == ["openai", "math"]:
        return openai_manuscript_dir(url) is not None
    if host == "raw.githubusercontent.com":
        return len(segments) >= 4
    if host in ("github.com", "www.github.com"):
        return len(segments) == 2 or (len(segments) >= 4 and segments[2] in ("blob", "tree", "raw"))
    return bool(_DIRECT_FILE.search(parts.path))


def withdrawal_notice(withdrawn: Iterable[Mapping[str, Any]], revision: str) -> dict[str, Any]:
    """One withdrawal record, citing openai/math's ``history.md`` at ``revision``, for the withdrawn
    manuscripts (fact-file rows with ``dir``, ``title`` and ``withdrawn_on``) an entry cites."""
    rows = sorted(withdrawn, key=lambda row: row["dir"])
    titles = "; ".join(f"“{_text(row.get('title'), 120) or row['dir']}”" for row in rows)
    return evidence_item({
        "kind": "withdrawal", "actor": "ai", "claimant": "OpenAI", "scope": "full", "outcome": "other",
        "date": max((row.get("withdrawn_on") or "" for row in rows), default="") or None,
        "title": f"openai/math withdrew {titles}",
        "url": f"https://github.com/openai/math/blob/{revision}/history.md", "acceptance": "withdrawn",
        "note": "A withdrawal concerns the proof; it does not assert that the statement is false."})


def withdraw_cited(evidence: list[dict[str, Any]], openai_math: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Evidence with the openai/math manuscripts the fact file lists as withdrawn marked "withdrawn",
    plus a withdrawal item citing ``history.md`` when any is cited (withdrawals override immediately)."""
    withdrawn = {row["dir"]: row for row in openai_math.get("withdrawn") or []}
    marked, cited = [], {}
    for item in evidence:
        directory = openai_manuscript_dir(item["url"])
        if directory in withdrawn:
            cited[directory] = withdrawn[directory]
            if item["acceptance"] not in ("withdrawn", "refuted"):
                item = item | {"acceptance": "withdrawn"}
        marked.append(item)
    if cited:
        marked.append(withdrawal_notice(cited.values(), openai_math["revision"]))
    return marked


# ── research and decisions ──────────────────────────────────────────────────


def _partial_results(items: object, warnings: list[str], context: str) -> list[dict[str, Any]]:
    converted = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = safe_url(item.get("url"))
        title, text = _text(item.get("title"), 200), _text(item.get("text"), 900)
        sources = [{"label": "Source", "url": url}] if url else []
        if isinstance(item.get("sources"), list):
            try:
                sources = link_list(item["sources"], context=context, lenient=True)
            except CatalogueError:
                sources = []
        if not (title and text and sources):
            warnings.append(f"{context}: dropped a partial result without title, text or source")
            continue
        status = item.get("status") or {"refereed": "Refereed result", "accepted-by-maintainer": "Accepted by the source",
                                        "broadly-accepted": "Broadly accepted", "preprint": "Preprint",
                                        "announced": "Announced"}.get(item.get("acceptance"), "See cited source")
        converted.append({"title": title, "text": text, "status": _text(status, 80), "sources": sources})
    return converted


def normalize_research(research: Mapping[str, Any] | None) -> tuple[dict[str, dict], list[str]]:
    """Validate the research file; unusable entries are skipped with a warning."""
    warnings: list[str] = []
    if not research:
        return {}, warnings
    if research.get("schema_version") != 1 or not isinstance(research.get("entries"), dict):
        raise CatalogueError("Unsupported problem-status research file")
    entries = {}
    for key, raw in sorted(research["entries"].items()):
        if not RESEARCH_KEY.fullmatch(str(key)) or not isinstance(raw, dict):
            warnings.append(f"research {key}: unknown key format; ignored")
            continue
        if raw.get("include", True) is False:
            continue  # out of scope for the catalogue (for example a list of problems)
        evidence = _evidence_list(raw.get("evidence"), warnings, f"research {key}")
        entry = {"key": key, "include": raw.get("include", True) is not False,
                 "name": _text(raw.get("name"), 200), "headline": _text(raw.get("status_headline"), 280),
                 "category": raw.get("category"), "rule": raw.get("category_rule"),
                 "badges": [badge for badge in raw.get("badges") or [] if badge in CATALOGUE_BADGES],
                 # A "source marker" outside the parsed sources of record is an ordinary citation.
                 "evidence": [item | {"kind": "other"} if item["kind"] == "source-marker"
                              and not _SOURCE_OF_RECORD_URL.match(item["url"]) else item for item in evidence],
                 "statement": _text(raw.get("statement"), 6000), "statement_sources": [],
                 "partial_results": _partial_results(raw.get("partial_results"), warnings, f"research {key}"),
                 "topics": [_text(topic, 60) for topic in raw.get("topics") or [] if isinstance(topic, str) and topic.strip()][:8],
                 "formal_statement_url": safe_url(raw.get("formal_statement_url")),
                 "needs_review": bool(raw.get("needs_human_review")) or raw.get("verified") == "uncertain",
                 "verified": raw.get("verified") if raw.get("verified") in VERIFIED else "unchecked",
                 "researched_at": _date(raw.get("researched_at")), "maps_to": None}
        if entry["category"] not in RULES or entry["rule"] not in RULES.get(entry["category"], ()):
            warnings.append(f"research {key}: invalid category or rule; category ignored")
            entry["category"] = entry["rule"] = None
        if entry["rule"] in RESEARCH_RULES:
            entry["marker_rule"], entry["rule"] = entry["rule"], RESEARCH_RULES[entry["rule"]]
        if entry["statement"]:
            try:
                entry["statement_sources"] = link_list(raw.get("statement_sources"), context=f"research {key} statement",
                                                       lenient=True)
            except CatalogueError as error:
                warnings.append(str(error))
                entry["statement"] = ""
        maps = raw.get("maps_to") if isinstance(raw.get("maps_to"), dict) else {}
        target, relation = maps.get("catalogue_id"), maps.get("relation", "none")
        if relation in RELATIONS and relation != "none" and isinstance(target, str) and ID_PATTERN.fullmatch(target):
            entry["maps_to"] = {"id": target, "relation": relation}
        entries[key] = entry
    return entries, warnings


def normalize_decisions(decisions: Mapping[str, Any] | None) -> dict[str, dict]:
    if not decisions:
        return {}
    if decisions.get("schema_version") != 1 or not isinstance(decisions.get("decisions", {}), dict):
        raise CatalogueError("Unsupported problem-status decisions file")
    result = {}
    for problem_id, raw in sorted(decisions.get("decisions", {}).items()):
        if not ID_PATTERN.fullmatch(problem_id) or not isinstance(raw, dict):
            raise CatalogueError(f"Invalid decision for {problem_id}")
        category, rule = raw.get("category"), raw.get("category_rule")
        if category is not None and (category not in RULES or rule not in RULES[category]):
            raise CatalogueError(f"Decision for {problem_id} has an invalid category or rule")
        if rule in RESEARCH_RULES:
            # S1, A1 and P1 state that the source of record marks the problem; only its parser may say so.
            raise CatalogueError(f"Decision for {problem_id}: {rule} is a source-of-record marker; "
                                 f"a reviewed decision uses {RESEARCH_RULES[rule]}")
        warnings: list[str] = []
        evidence = _evidence_list(raw.get("evidence"), warnings, f"decision {problem_id}")
        if warnings:
            raise CatalogueError("; ".join(warnings))
        urls = [safe_url(url) for url in raw.get("evidence_urls") or []]
        if any(url is None for url in urls):
            raise CatalogueError(f"Decision for {problem_id} has an invalid evidence URL")
        if category in ("solved", "ai-proposed", "partial") and not (urls or evidence):
            raise CatalogueError(f"Decision for {problem_id} must cite evidence")
        if category == "solved" and not supports_category("solved", rule, evidence):
            # The decision's own evidence, not the row's: an answered part or a
            # bare link never makes a problem solved.
            raise CatalogueError(f"Decision for {problem_id}: solved needs qualifying evidence of its own "
                                 "(a refereed, maintainer-accepted or broadly accepted full answer)")
        if not _text(raw.get("decided_by"), 80) or not _date(raw.get("decided_at")) or not _text(raw.get("note"), 400):
            raise CatalogueError(f"Decision for {problem_id} must record who, when and why")
        result[problem_id] = {"category": category, "rule": rule, "headline": _text(raw.get("status_headline"), 280),
                              "badges_add": [b for b in raw.get("badges_add") or [] if b in CATALOGUE_BADGES],
                              "badges_remove": [b for b in raw.get("badges_remove") or [] if b in CATALOGUE_BADGES],
                              "evidence": evidence, "evidence_urls": urls, "decided_at": _date(raw.get("decided_at")),
                              "decided_by": _text(raw.get("decided_by"), 80), "note": _text(raw.get("note"), 400)}
    return result


# ── deterministic rows ──────────────────────────────────────────────────────


def _kourovka_id(number: str) -> str:
    issue, problem = (int(part) for part in number.split("."))
    return f"kourovka-{issue:02d}-{problem:03d}"


def _sort_key(row: Mapping[str, Any]) -> tuple:
    source = row["source"]
    number = row.get("number", "")
    if source == "erdos":
        natural: tuple = (int(number),)
    elif source == "kourovka":
        natural = tuple(int(part) for part in number.split("."))
    else:
        digits = re.match(r"\d+", number)
        natural = (int(digits.group(0)) if digits else 10_000, number)
    return (COLLECTION_ORDER.index(source), not row.get("statement"), natural, row["id"])


def _month_name(date: str | None) -> str:
    if not date:
        return ""
    names = ("January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December")
    return f"{names[int(date[5:7]) - 1]} {date[:4]}"


class Builder:
    def __init__(self, facts: Mapping[str, Mapping], curation: Mapping, research: Mapping | None,
                 decisions: Mapping | None, *, rating_method: Mapping, input_hashes: Mapping[str, str]):
        self.facts = facts
        self.curation = curation
        self.research, self.warnings = normalize_research(research)
        self.research_generated_at = (research or {}).get("generated_at")
        self.decisions = normalize_decisions(decisions)
        self.decision_dates = [item["decided_at"] for item in self.decisions.values() if item["decided_at"]]
        self.rating_method = rating_method
        self.input_hashes = dict(input_hashes)
        self.rows: dict[str, dict[str, Any]] = {}
        self.ctx: dict[str, dict[str, Any]] = {}
        self.merged: dict[str, dict[str, Any]] = {}
        erdos, kourovka = facts["erdos"], facts["kourovka"]
        self.erdos_date = str(erdos["fetched_at"])[:10]
        self.kourovka_date = str(kourovka["fetched_at"])[:10]
        self.clay_date = str(facts["clay"]["checked_at"])[:10]
        self.kourovka_label = f"the {_month_name(kourovka.get('published'))} edition (arXiv:{kourovka['arxiv_id']}v{kourovka['arxiv_version']})".replace("the  edition", "the current edition")

    # Context collects deterministic facts used by the category step.
    def add(self, row: dict[str, Any], **context: Any) -> None:
        if row["id"] in self.rows:
            raise CatalogueError("Duplicate problem id " + row["id"])
        if not ID_PATTERN.fullmatch(row["id"]):
            raise CatalogueError("Invalid problem id " + row["id"])
        self.rows[row["id"]] = row
        self.ctx[row["id"]] = {"badges": [], "evidence": [], "deterministic": None, "curated": None, **context}

    def erdos_rows(self) -> None:
        fact = self.facts["erdos"]
        fc = self.facts["formal_conjectures"]
        wiki = self.facts["ai_wiki"]
        commit = fact["revision"]
        blob = f"https://github.com/teorth/erdosproblems/blob/{commit}/data/problems.yaml"
        for number, item in fact["problems"].items():
            problem_id = f"erdos-{int(number):04d}"
            state = item["state"]
            names = item.get("names") or []
            row = {"id": problem_id, "number": number,
                   "title": f"Erdős #{number} · {names[0]}" if names else f"Erdős problem #{number}",
                   "source": "erdos", "source_url": f"https://www.erdosproblems.com/{number}",
                   "source_locator": f"Erdős problem {number}", "topics": item.get("tags") or ["mathematics"],
                   "source_status": state}
            if item.get("last_update"):
                row["source_status_updated"] = item["last_update"]
            if item.get("prize"):
                row["prize"] = item["prize"]
            if len(names) > 1:
                row["aliases"] = names[1:]
            fc_item = fc["erdos"].get(number)
            if fc_item:
                row["formal_statement_url"] = fc_item["url"]
            self.add(row)
            ctx = self.ctx[problem_id]
            marker_url = f"{blob}#L{item['line']}" if item.get("line") else blob
            open_like = state in ERDOS_OPEN_STATES
            if state in ERDOS_SOLVED_STATES:
                ctx["deterministic"] = ("solved", "S1", f"{state.capitalize()} according to the Erdős problems database"
                                        + (f" (updated {item['last_update']})." if item.get("last_update") else "."))
                ctx["evidence"].append(evidence_item({
                    "kind": "source-marker", "actor": "maintainer", "claimant": "Erdős problems database (teorth/erdosproblems)",
                    "scope": "full", "outcome": ERDOS_SOLVED_STATES[state], "date": item.get("last_update"),
                    "title": f"Erdős problems database: state “{state}”", "url": marker_url,
                    "acceptance": "accepted-by-maintainer"}))
            else:
                substate = "" if state == "open" else f" ({state})"
                headline = f"Listed as open{substate} in the Erdős problems database."
                if item.get("formal_state") == "Lean":
                    headline += " A Lean solution is recorded but has not yet been digested by a human reader."
                ctx["open_headline"] = headline
            if open_like and state != "open":
                ctx["badges"].append("upstream-substatus")
            if open_like and item.get("formal_state") == "Lean":
                ctx["badges"].append("lean-pending")
                if item.get("formal_url"):
                    ctx["evidence"].append(evidence_item({
                        "kind": "formalization", "actor": "unknown", "scope": "full", "outcome": "proof",
                        "date": item.get("formal_last_update"),
                        "title": "Lean solution recorded in the Erdős problems database (not yet digested by a human reader)",
                        "url": item["formal_url"], "acceptance": "lean-checked-unreviewed",
                        "note": with_label(item.get("formal_note"), NOT_RECHECKED)}))
            if item.get("ambiguous"):
                ctx["badges"].append("ambiguous-statement")
            if open_like and fc_item and fc_item.get("category") == "research solved":
                ctx["badges"].append("sources-conflict")
                ctx["evidence"].append(evidence_item({
                    "kind": "source-marker", "actor": "maintainer", "claimant": "google-deepmind/formal-conjectures",
                    "scope": "full", "outcome": "other",
                    "title": "formal-conjectures marks the statement “research solved”", "url": fc_item["url"],
                    "acceptance": "accepted-by-maintainer",
                    "note": "formal-conjectures disagrees with the Erdős problems database, which lists the problem as open."}))
            ai = wiki["problems"].get(number)
            if ai:
                self._ai_wiki(problem_id, number, ai, solved=state in ERDOS_SOLVED_STATES)

    def _ai_wiki(self, problem_id: str, number: str, ai: Mapping[str, Any], *, solved: bool) -> None:
        wiki = self.facts["ai_wiki"]
        ctx = self.ctx[problem_id]
        url = wiki.get("url") or "https://github.com/teorth/erdosproblems/wiki/AI-contributions-to-Erd%C5%91s-problems"
        frozen = wiki.get("frozen_on") or "2026-06-30"
        badge = None
        if solved and ai.get("full_solution"):
            badge, kind, scope, acceptance, label = "ai-assisted-solution", "accepted-result", "full", "accepted-by-maintainer", "full solution"
        elif not solved and ai.get("full_solution"):
            badge, kind, scope, acceptance, label = "sources-conflict", "claim", "full", "unknown", "full solution"
        elif not solved and ai.get("partial_result"):
            badge, kind, scope, acceptance, label = "ai-partial", "partial-result", "part", "unknown", "partial result"
        if not badge:
            return
        ctx["badges"].append(badge)
        rows = [row for row in ai.get("rows", []) if row["section"].startswith("1")]
        collaborating = all(row["section"] == "1(d)" for row in rows if row.get("outcome") in ("full", "partial"))
        note = f"The wiki rates problem {number} as a {label}; sections {', '.join(sorted({row['section'] for row in rows}))}."
        if badge == "sources-conflict":
            note += " The Erdős problems database still lists the problem as open."
        ctx["evidence"].append(evidence_item({
            "kind": kind, "actor": "ai-assisted" if collaborating else "ai", "ai_systems": ai.get("systems") or [],
            "scope": scope, "outcome": "other", "date": ai.get("last_primary_date"),
            "title": f"AI-contributions wiki (frozen {frozen}): {label}", "url": url, "acceptance": acceptance,
            "note": note}))

    def kourovka_rows(self) -> None:
        fact = self.facts["kourovka"]
        fc = self.facts["formal_conjectures"].get("kourovka", {})
        merged = {key: value for key, value in (self.curation.get("merged") or {}).items() if key.startswith("kourovka-")}
        version, published = fact["arxiv_version"], fact.get("published")
        for number, item in fact["problems"].items():
            problem_id = _kourovka_id(number)
            page = item["page"]
            url = f"https://arxiv.org/pdf/{fact['arxiv_id']}v{version}#page={page}"
            if problem_id in merged:
                self.merged[problem_id] = {"url": url, "number": number, **merged[problem_id]}
                continue
            row = {"id": problem_id, "number": number, "title": f"Kourovka problem {number}", "source": "kourovka",
                   "source_url": url, "source_locator": f"Kourovka {number} · arXiv:{fact['arxiv_id']}v{version}, page {page}",
                   "topics": ["group theory", *item.get("topics", [])], "source_status": self._kourovka_status(item)}
            if number in fc:
                row["formal_statement_url"] = fc[number]["url"]
            self.add(row)
            self._kourovka_category(problem_id, number, item, url, published)

    def _kourovka_status(self, item: Mapping[str, Any]) -> str:
        edition = self.kourovka_label
        if item["header_marker"] == "*":
            return f"Marked ∗ (answered) in {edition}"
        if item["header_marker"] == "#":
            return f"Marked ♯ (unverified AI solution) in {edition}"
        parts = item.get("part_markers") or []
        if parts:
            described = ", ".join(f"part ({part['part']}) {'∗' if part['marker'] == '*' else '♯'}" for part in parts)
            return f"Marked {described} in {edition}"
        return f"Listed without an answer marker in {edition}"

    def _kourovka_category(self, problem_id: str, number: str, item: Mapping[str, Any], url: str, published: str | None) -> None:
        ctx = self.ctx[problem_id]
        header, parts = item["header_marker"], item.get("part_markers") or []
        starred_parts = [part["part"] for part in parts if part["marker"] == "*"]
        sharp_parts = [part["part"] for part in parts if part["marker"] == "#"]
        edition = self.kourovka_label
        marker = {"kind": "source-marker", "actor": "maintainer", "claimant": "Kourovka Notebook editors",
                  "date": published, "url": url, "outcome": "other"}
        for solution in item.get("solutions") or []:
            ctx["evidence"].append(evidence_item(_kourovka_solution(number, solution, whole=header is not None)))
        if header == "*":
            confirmer = item.get("confirmer")
            note = ""
            if confirmer:
                note = f" The AI-produced solution is recorded as confirmed by {confirmer}."
            elif item.get("ai") == "confirmed":
                note = " The Notebook attributes the solution to an AI system."
            ctx["deterministic"] = ("solved", "S1", f"Marked solved (∗) in {edition}.{note}")
            ctx["evidence"].append(evidence_item(marker | {"scope": "full", "title": f"Kourovka {number} marked ∗ (answered)",
                                                           "acceptance": "accepted-by-maintainer", "note": note.strip()}))
            if item.get("ai") == "confirmed":
                ctx["badges"].append("ai-confirmed")
        elif header == "#":
            ctx["deterministic"] = ("ai-proposed", "A1", f"Marked ♯ in {edition}: an AI system proposed a solution that "
                                    "mathematicians have not confirmed; the editors regard the problem as unsolved.")
            ctx["evidence"].append(evidence_item(marker | {"scope": "full", "title": f"Kourovka {number} marked ♯ (unverified AI solution)",
                                                           "acceptance": "announced",
                                                           "note": "Such a problem should be regarded as unsolved until confirmed (Notebook preface)."}))
        elif starred_parts:
            names = ", ".join(f"({part})" for part in starred_parts)
            ctx["deterministic"] = ("partial", "P1", f"Part {names} marked solved (∗) in {edition}; the rest remains open.")
            ctx["evidence"].append(evidence_item(marker | {"scope": "part", "title": f"Kourovka {number}: part {names} marked ∗",
                                                           "acceptance": "accepted-by-maintainer"}))
            if item.get("ai"):
                ctx["badges"].append("ai-partial")
        else:
            ctx["open_headline"] = f"Listed without an answer marker in {edition}."
        if sharp_parts and header != "#":
            names = ", ".join(f"({part})" for part in sharp_parts)
            ctx["badges"].append("ai-partial")
            if not starred_parts:
                ctx["open_headline"] = (f"Open. Part {names} is marked ♯ in {edition}: an unconfirmed AI solution of that part; "
                                        "the rest has no proposed answer.")
            ctx["evidence"].append(evidence_item(marker | {"scope": "part", "title": f"Kourovka {number}: part {names} marked ♯",
                                                           "acceptance": "announced"}))

    def retained_rows(self) -> None:
        for problem_id, item in sorted((self.curation.get("retained") or {}).items()):
            if problem_id in self.rows:
                continue  # back in the current edition: nothing to retain
            source = item.get("source")
            row = {"id": problem_id, "number": _text(item.get("number"), 40), "title": _text(item.get("title"), 300),
                   "source": source, "source_url": safe_url(item.get("source_url")),
                   "source_locator": _text(item.get("source_locator"), 300),
                   "topics": [topic for topic in item.get("topics") or [] if isinstance(topic, str)],
                   "source_status": _text(item.get("source_status"), 300)}
            if source not in COLLECTION_ORDER or not all(row.values()):
                raise CatalogueError(f"Retained entry {problem_id} needs source, number, title, source_url, locator, topics and source_status")
            self.add(row, retained=True)
            ctx = self.ctx[problem_id]
            ctx["badges"].append("not-in-current-edition")
            ctx["open_headline"] = _text(item.get("status_headline"), 280) or "Not listed in the current edition; kept open pending confirmation."

    def clay_rows(self) -> None:
        sections = self.facts["clay"]["sections"]
        fc = self.facts["formal_conjectures"].get("millennium", {})
        for slug, title, topics, summary, fc_name in CLAY_PROBLEMS:
            listed = next((name for name, slugs in sections.items() if slug in slugs), None)
            if listed is None:
                raise CatalogueError(f"The Clay listing does not mention {slug}")
            url = f"https://www.claymath.org/millennium/{slug}/"
            row = {"id": f"clay-{slug}", "number": slug, "title": title, "summary": summary, "source": "clay",
                   "source_url": url, "source_locator": title, "topics": list(topics), "prize": CLAY_PRIZE,
                   "source_status": {"active": "Listed under Active problems on the Clay Millennium page",
                                     "unsolved": "Listed under Unsolved problems on the Clay Millennium page",
                                     "solved": "Listed under Solved problems on the Clay Millennium page"}[listed],
                   "rating": {"score": 1, "label": "Long-term theory", "confidence": "low", "method": "exploration-fit-v1",
                              "reason": "A broad foundational problem. Start with a sharply defined special case and specialist literature; the full problem is a long-term research direction."}}
            if fc_name and fc_name in fc:
                row["formal_statement_url"] = fc[fc_name]["url"]
            self.add(row)
            ctx = self.ctx[row["id"]]
            if listed == "solved":
                ctx["deterministic"] = ("solved", "S1", "Solved: the Clay Mathematics Institute lists it among the solved Millennium problems.")
                ctx["evidence"].append(evidence_item({
                    "kind": "source-marker", "actor": "maintainer", "claimant": "Clay Mathematics Institute", "scope": "full",
                    "outcome": "proof", "title": f"Clay Millennium page: {title} listed under Solved problems",
                    "url": "https://www.claymath.org/millennium-problems/", "acceptance": "accepted-by-maintainer"}))
            else:
                ctx["open_headline"] = f"Listed as {'active' if listed == 'active' else 'unsolved'} on the Clay Millennium page."

    def curated_rows(self) -> None:
        for raw in self.curation.get("problems") or []:
            row = deepcopy(raw)
            if row.get("source") not in ("hilbert", "smale", "ramanujan"):
                raise CatalogueError("Curated new problems belong to hilbert, smale or ramanujan")
            for field in ("id", "number", "title", "summary", "source_url", "source_locator", "source_status", "status_as_of"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise CatalogueError(f"Problem metadata requires nonempty {field}")
            if not safe_url(row["source_url"]):
                raise CatalogueError("Curated source_url must be HTTPS without credentials")
            if not row.get("statement"):
                raise CatalogueError("New curated problems must have a curated statement")
            for field in ("status", "kind", "rating", "formal_status"):
                row.pop(field, None)
            self.add(row, curated_status_as_of=row.pop("status_as_of"))

    def apply_overlays(self) -> None:
        identity = ("id", "number", "source", "source_url", "source_name", "source_locator")
        content = ("title", "summary", "statement", "partial_results", "proof_claims", "detail_note", "details_as_of",
                   "aliases", "related", "additional_sources", "source_status_note")
        for problem_id, overlay in sorted((self.curation.get("overlays") or {}).items()):
            if problem_id not in self.rows:
                raise CatalogueError("Curated overlay has no source problem: " + problem_id)
            if any(field in overlay for field in identity):
                raise CatalogueError("Overlay cannot replace canonical source identity")
            row, ctx = self.rows[problem_id], self.ctx[problem_id]
            for field in content:
                if field in overlay:
                    row[field] = deepcopy(overlay[field])
            if "category" in overlay:
                category, rule = overlay.get("category"), overlay.get("category_rule")
                if category not in RULES or rule not in RULES[category]:
                    raise CatalogueError(f"Overlay for {problem_id} has an invalid category or rule")
                ctx["curated"] = (category, rule, _text(overlay.get("status_headline"), 280))
            warnings: list[str] = []
            ctx["evidence"].extend(_evidence_list(overlay.get("status_evidence"), warnings, f"curation {problem_id}"))
            if warnings:
                raise CatalogueError("; ".join(warnings))
            ctx["badges"].extend(badge for badge in overlay.get("badges") or [] if badge in CATALOGUE_BADGES)

    def merged_links(self) -> None:
        for problem_id, item in sorted(self.merged.items()):
            target = self.rows.get(item.get("into"))
            if target is None:
                raise CatalogueError(f"{problem_id} is merged into a missing entry")
            label = f"Same problem · Kourovka {item['number']}"
            links = [link for link in target.get("additional_sources") or [] if "Kourovka" not in link.get("label", "")]
            target["additional_sources"] = [*links, {"label": label, "url": item["url"], "relation": "same-problem"}]

    def research_rows(self) -> None:
        wiki = self.facts["wikipedia"]
        by_wp = {row["wp_id"]: row for row in [*wiki["entries"], *wiki["solved"]]}
        fc_wiki = self.facts["formal_conjectures"].get("wikipedia", {})
        for key, entry in self.research.items():
            if key in self.rows or not entry["include"]:
                continue
            target = entry["maps_to"]
            if key.startswith(("wpu-", "wps-")):
                fact = by_wp.get(key)
                if fact is None:
                    self.warnings.append(f"research {key}: not in the pinned Wikipedia list; ignored")
                    continue
                if fact.get("group_header") or fact.get("duplicate_of"):
                    self.warnings.append(f"research {key}: group header or duplicate list entry; ignored")
                    continue
                if target and target["relation"] == "same" and target["id"] in self.rows:
                    self._alias(target["id"], key, entry, fact)
                    continue
                if target and target["relation"] == "same":
                    self.warnings.append(f"research {key}: maps to missing {target['id']}; listed separately for review")
                self._wikipedia_row(key, entry, fact, wiki, fc_wiki)
            elif key.startswith(("hilbert-", "smale-")):
                self._named_row(key, entry)
            else:
                self.warnings.append(f"research {key}: no catalogue row with this id; ignored")

    def _alias(self, problem_id: str, key: str, entry: Mapping[str, Any], fact: Mapping[str, Any]) -> None:
        from agent_monitor.problem_sources import slugify

        row = self.rows[problem_id]
        name = fact.get("name") or entry["name"]
        aliases = list(row.get("aliases") or [])
        known = {slugify(text) for text in [row["title"], *aliases]} | {slugify(row["title"].split("·", 1)[-1])}
        if name and slugify(name) not in known:
            aliases.append(name)
        if aliases:
            row["aliases"] = aliases
        self.ctx[problem_id].setdefault("merged_research", []).append(key)
        self.ctx[problem_id]["evidence"].extend(entry["evidence"])

    def _wikipedia_row(self, key: str, entry: Mapping[str, Any], fact: Mapping[str, Any], wiki: Mapping[str, Any],
                       fc_wiki: Mapping[str, str]) -> None:
        solved = key.startswith("wps-")
        problem_id = fact["slug"]
        if not entry["name"] or not entry["statement"]:
            self.warnings.append(f"research {key}: a new row needs a name and an original statement; ignored")
            return
        article = fact.get("article_url") if fact.get("article_revid") else None
        section = " › ".join(part for part in (fact.get("field"), fact.get("subfield")) if part)
        list_name = "Problems solved since 2015" if solved else "List of unsolved problems in mathematics"
        row = {"id": problem_id, "number": key, "title": entry["name"],
               "summary": _first_sentences(entry["statement"]), "source": "wikipedia-solved" if solved else "wikipedia",
               "source_url": article or wiki["permalink"] + ("#Problems_solved_since_2015" if solved else ""),
               "source_locator": f"Wikipedia · {list_name}{' › ' + section if section else ''} (revision {wiki['revision_id']}, entry {key})",
               "topics": entry["topics"] or [part.lower() for part in (fact.get("field"), fact.get("subfield")) if part] or ["mathematics"],
               "source_status": ("Listed under Problems solved since 2015" if solved else "Listed as unsolved") + f" on Wikipedia (revision {wiki['revision_id']})",
               "source_key": key}
        name = fact.get("name")
        if name and name != entry["name"]:
            row["aliases"] = [name]
        formal = entry["formal_statement_url"] or (fc_wiki.get(_wiki_key(fact.get("article_title"))) if fact.get("article_title") else None)
        if formal:
            row["formal_statement_url"] = formal
        target = entry["maps_to"]
        self.add(row, research_key=key, review=bool(target and target["relation"] == "same" and target["id"] not in self.rows))

    def _named_row(self, key: str, entry: Mapping[str, Any]) -> None:
        collection = key.split("-", 1)[0]
        number = str(int(key.split("-")[1]))
        if not entry["name"] or not entry["statement"]:
            self.warnings.append(f"research {key}: a new row needs a name and an original statement; ignored")
            return
        source = entry["statement_sources"][0]["url"] if entry["statement_sources"] else None
        source = source or {"hilbert": "https://doi.org/10.1090/S0002-9904-1902-00923-3",
                            "smale": "https://www.fim.uni-passau.de/fileadmin/dokumente/fakultaeten/fim/lehrstuhl/muller/SmaleProblems1998.pdf"}[collection]
        row = {"id": key, "number": number, "title": entry["name"], "summary": _first_sentences(entry["statement"]),
               "source": collection, "source_url": source,
               "source_locator": f"{collection.capitalize()} problem {number}",
               "topics": entry["topics"] or ["mathematics"],
               "source_status": "Status from the cited literature (no single source of record)"}
        if collection == "hilbert":
            fc = self.facts["formal_conjectures"].get("hilbert", {}).get(number)
            if fc:
                row["formal_statement_url"] = fc["url"]
        self.add(row, research_key=key)

    def apply_research_content(self) -> None:
        for problem_id, row in self.rows.items():
            key = self.ctx[problem_id].get("research_key") or problem_id
            entry = self.research.get(key)
            if not entry or not entry["include"]:
                continue
            ctx = self.ctx[problem_id]
            ctx["research"] = entry
            if entry["statement"] and not row.get("statement"):
                row["statement"] = {"text": entry["statement"], "kind": "editorial", "sources": entry["statement_sources"]}
            if entry["partial_results"]:
                # A partial result already listed (by curation, or earlier in the same list) is not repeated.
                existing = {_document(source["url"])[0] for item in row.get("partial_results") or [] for source in item.get("sources", [])}
                extra = []
                for item in entry["partial_results"]:
                    documents = {_document(source["url"])[0] for source in item["sources"]}
                    if not documents & existing:
                        extra.append(item)
                        existing |= documents
                if extra:
                    row["partial_results"] = [*(row.get("partial_results") or []), *extra]
            if entry["formal_statement_url"] and not row.get("formal_statement_url"):
                row["formal_statement_url"] = entry["formal_statement_url"]
            if row["source"] == "erdos" and entry["name"] and row["title"] == f"Erdős problem #{row['number']}":
                name = re.sub(r"^Erd[őo]s\s*(?:problem\s*)?#?\s*\d+\s*[:·.,–-]?\s*", "", entry["name"], flags=re.I)
                if name:
                    row["title"] = f"Erdős #{row['number']} · {name}"
            target = entry["maps_to"]
            if target and target["id"] != problem_id and target["id"] not in self.rows:
                self.warnings.append(f"research {key}: maps_to target {target['id']} is not a catalogue id; link dropped")
            if target and target["id"] != problem_id and target["id"] in self.rows:
                related = [item for item in row.get("related") or [] if item["id"] != target["id"]]
                row["related"] = [*related, {"id": target["id"], "relation": target["relation"]}]

    def decide(self) -> None:
        for problem_id, row in self.rows.items():
            ctx = self.ctx[problem_id]
            research = ctx.get("research")
            decision = self.decisions.get(problem_id)
            evidence = list(ctx["evidence"])
            if research:
                evidence = _merge_evidence(evidence, research["evidence"])
            badges = list(ctx["badges"])
            if research:
                badges += research["badges"]
            needs_review = bool(research and research["needs_review"]) or bool(ctx.get("review"))
            chosen = None
            if decision:
                # The decision's date and reasoning are not evidence about the problem, so a bare link the reviewer cited
                # carries neither (they read as an event dated the day of the decision, with internal notes).
                cited = [evidence_item({"kind": "other", "actor": "unknown", "scope": "related", "outcome": "other",
                                        "title": "Evidence cited by the reviewer", "url": url, "acceptance": "unknown"})
                         for url in dict.fromkeys(decision["evidence_urls"]) if url not in {item["url"] for item in [*decision["evidence"], *evidence]}]
                # A decision's own items come first so that they win over research copies of the same item.
                evidence = _merge_evidence(decision["evidence"], cited, evidence)
            # Manuscripts openai/math has withdrawn never count, whoever cites them.
            evidence = _merge_evidence(withdraw_cited(evidence, self.facts["openai_math"]))
            if decision and decision["category"]:
                chosen = (decision["category"], decision["rule"], decision["headline"], "decision")
            if chosen is None and ctx["deterministic"]:
                category, rule, headline = ctx["deterministic"]
                chosen = (category, rule, headline, "source")
                if research and research["category"] and research["category"] != category:
                    badges.append("sources-conflict")
                    needs_review = True
                elif research and research["category"] == category and research["headline"]:
                    chosen = (category, rule, research["headline"], "source")
            if chosen is None and research and research["category"] and research["category"] != "open":
                usable = research["verified"] != "uncertain"
                if usable and supports_category(research["category"], research["rule"], evidence):
                    chosen = (research["category"], research["rule"], research["headline"], "research")
                    if research.get("marker_rule"):
                        # Research named a source marker that the parsed source does not show.
                        needs_review = True
                        self.warnings.append(f"{problem_id}: research rule {research['marker_rule']} read as "
                                             f"{research['rule']} (no such marker in the source of record)")
                else:
                    needs_review = True
                    self.warnings.append(f"{problem_id}: research category {research['category']} not applied "
                                         "(uncertain or without qualifying evidence)")
            if chosen is None and ctx["curated"]:
                category, rule, headline = ctx["curated"]
                if category == "open" or supports_category(category, rule, evidence):
                    chosen = (category, rule, headline, "curation")
                else:
                    raise CatalogueError(f"Curated category for {problem_id} lacks qualifying evidence")
            if chosen is None:
                headline = (research["headline"] if research and research["headline"] and (research["category"] == "open" or research["headline"].startswith("Open"))
                            else ctx.get("open_headline") or "No accepted answer is recorded in the cited sources.")
                chosen = ("open", "O", headline, "default")
            category, rule, headline, origin = chosen
            if origin == "research" and category == "solved" and row["source"] in ("erdos", "kourovka", "clay"):
                # The source of record does not (yet) list it as answered.
                badges.append("sources-conflict")
                needs_review = True
            if decision:
                badges = [b for b in badges + decision["badges_add"] if b not in decision["badges_remove"]]
                if decision["headline"] and origin != "decision":
                    headline = decision["headline"]
            if any(item["acceptance"] == "withdrawn" or item["kind"] == "withdrawal" for item in evidence):
                badges.append("withdrawn-claim")
            row.update(category=category, category_rule=rule,
                       status_headline=_text(_typeset_operators(headline), 280) or RULE_TEXT[rule])
            if category != "open" and re.match(r"\s*Open\b", row["status_headline"]):
                # "Open. …" above an AI proposed or Solved chip reads as a contradiction.
                self.warnings.append(f"{problem_id}: the headline starts with 'Open' but the category is {category}")
            row["badges"] = _clean_badges(badges, category, row)
            row["status_evidence"] = sorted(evidence, key=_evidence_order)
            row["status_as_of"] = self._status_as_of(row, ctx, origin, decision)
            if origin == "decision":
                # Who set the category, when and why: a decision is shown, never presented as a source's finding.
                row["decision"] = {"by": decision["decided_by"], "at": decision["decided_at"], "note": decision["note"]}
            if origin in ("research", "curation", "decision"):
                # A source marker's category is dated by its edition, not by when the
                # marker appeared, so only categories that rest on dated answers or
                # claims get a change date.
                changed = status_changed(category, evidence)
                if changed:
                    row["status_changed"] = changed
            if row.get("statement") and row["source"] != "clay":
                row["rating"] = dict(CURATED_RATING)
            row["status"] = category
            if needs_review:
                row["needs_review"] = True
            if research and research["verified"] != "unchecked":
                row["research_verified"] = research["verified"]

    def mark_manuscripts(self) -> None:
        """Mark each answer or claim with ``manuscript``: a manuscript link (``manuscript_link``)
        that at most MAX_MANUSCRIPT_PROBLEMS entries cite as an answer or claim.  A document is
        counted once per entry, whatever its ``#`` fragment (pages of one compilation)."""
        citing: dict[str, set[str]] = {}
        for problem_id, row in self.rows.items():
            for item in row["status_evidence"]:
                if item["kind"] in ANSWER_KINDS:
                    citing.setdefault(item["url"].split("#", 1)[0], set()).add(problem_id)
        for row in self.rows.values():
            row["status_evidence"] = [
                item | {"manuscript": manuscript_link(item["url"])
                        and len(citing[item["url"].split("#", 1)[0]]) <= MAX_MANUSCRIPT_PROBLEMS}
                if item["kind"] in ANSWER_KINDS else item for item in row["status_evidence"]]

    def _status_as_of(self, row: Mapping[str, Any], ctx: Mapping[str, Any], origin: str, decision) -> str:
        """The date the category's basis was last checked."""
        research = ctx.get("research") or {}
        # Research re-checks an entry on the day the research file records (or its own date).
        researched = research.get("researched_at") or (_date(str(self.research_generated_at or "")[:10]) if research else None)
        if origin == "decision" and decision["decided_at"]:
            return decision["decided_at"]
        if origin == "research" or (origin == "default" and researched):
            return researched or self.snapshot_date()
        if origin == "curation":
            dates = [date for date in (_date(self.curation.get("as_of")), researched) if date]
            return max(dates) if dates else self.snapshot_date()
        fact_date = {"erdos": self.erdos_date, "kourovka": self.kourovka_date, "clay": self.clay_date}.get(row["source"])
        return fact_date or ctx.get("curated_status_as_of") or self.snapshot_date()

    def snapshot_date(self) -> str:
        dates = [self.erdos_date, self.kourovka_date, self.clay_date, *self.decision_dates]
        if _date(self.research_generated_at):
            dates.append(str(self.research_generated_at)[:10])
        return max(dates)

    def generated_at(self) -> str:
        stamps = [str(self.facts[name].get("fetched_at") or self.facts[name].get("checked_at") or "")
                  for name in ("erdos", "kourovka", "clay", "openai_math", "ai_wiki", "formal_conjectures", "wikipedia")]
        stamps += [f"{date}T00:00:00Z" for date in self.decision_dates]
        if isinstance(self.research_generated_at, str):
            stamps.append(self.research_generated_at)
        return max(stamp for stamp in stamps if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp))

    def build(self) -> dict[str, Any]:
        self.erdos_rows()
        self.kourovka_rows()
        self.retained_rows()
        self.clay_rows()
        self.curated_rows()
        self.apply_overlays()
        self.merged_links()
        self.research_rows()
        self.apply_research_content()
        self.decide()
        self.mark_manuscripts()
        problems = [self._finish(row) for row in sorted(self.rows.values(), key=_sort_key)]
        return self._document(problems)

    def _finish(self, row: dict[str, Any]) -> dict[str, Any]:
        for field in ("statement",):
            if row.get(field):
                statement = row[field]
                if statement.get("kind") not in ("editorial", "exact") or not _text(statement.get("text"), 6000):
                    raise CatalogueError(f"{row['id']}: invalid statement")
                statement["sources"] = link_list(statement.get("sources"), context=f"{row['id']} statement")
        for field in ("partial_results", "proof_claims"):
            for item in row.get(field) or []:
                if not all(item.get(key) for key in ("title", "text", "status")):
                    raise CatalogueError(f"{row['id']}: results require title, text and explicit status")
                item["sources"] = link_list(item.get("sources"), context=f"{row['id']} {field}")
        if row.get("proof_claims") and not row.get("statement"):
            raise CatalogueError(f"{row['id']}: a proof claim needs a documented problem statement")
        if row.get("additional_sources"):
            for link in row["additional_sources"]:
                if not safe_url(link.get("url")) or not link.get("label"):
                    raise CatalogueError(f"{row['id']}: additional sources must be labelled HTTPS links")
        for field in ("title", "source_url", "source_locator", "source_status", "status_headline", "category"):
            if not row.get(field):
                raise CatalogueError(f"{row['id']}: missing {field}")
        if not safe_url(row["source_url"]):
            raise CatalogueError(f"{row['id']}: source_url must be HTTPS without credentials")
        if row.get("formal_statement_url") and not safe_url(row["formal_statement_url"]):
            raise CatalogueError(f"{row['id']}: invalid formal_statement_url")
        if not isinstance(row.get("topics"), list) or not row["topics"]:
            raise CatalogueError(f"{row['id']}: topics required")
        for link in row.get("related") or []:
            if (not isinstance(link, dict) or set(link) != {"id", "relation"} or link["relation"] not in ("same", "part", "related")
                    or link["id"] not in self.rows or link["id"] == row["id"]):
                raise CatalogueError(f"{row['id']}: related links need an existing id and a same/part/related relation")
        order = ("id", "number", "title", "summary", "source", "source_name", "source_url", "source_locator", "source_key", "topics",
                 "kind", "category", "category_rule", "status_headline", "status_as_of", "status_changed", "status_evidence", "badges", "decision",
                 "statement", "partial_results", "proof_claims", "detail_note", "details_as_of", "aliases", "related",
                 "additional_sources", "formal_statement_url", "source_status", "source_status_updated",
                 "source_status_note", "source_revision", "prize", "rating", "needs_review", "research_verified", "status")
        unknown = sorted(set(row) - set(order))
        if unknown:
            raise CatalogueError(f"{row['id']}: unexpected fields {unknown}")
        return {key: row[key] for key in order if key in row and row[key] not in (None, [], "")}

    def _sources(self, counts: Mapping[str, int]) -> list[dict[str, Any]]:
        erdos, kourovka, wiki = self.facts["erdos"], self.facts["kourovka"], self.facts["wikipedia"]
        rating = {"score": 2, "label": "Literature first", "confidence": "low", "method": "exploration-fit-v1",
                  "reason": "Read the source statement and recent literature to choose an approach; bibliographic metadata alone does not establish an experimental target."}
        curated = {item["id"]: item for item in self.curation.get("sources") or []}

        def defaults(source_name: str, summary: str, revision: str) -> dict[str, Any]:
            return {"source_name": source_name, "kind": "research-question", "summary": summary,
                    "formal_status": DEFAULT_FORMAL_STATUS, "rating": rating, "source_revision": revision,
                    "badges": [], "status_evidence": []}
        sources = [
            {"id": "erdos", "name": "Erdős Problems", "abbreviation": "EP", "url": "https://github.com/teorth/erdosproblems",
             "attribution": "Thomas Bloom, Terence Tao, and the Erdős Problems contributors",
             "license": "Apache-2.0 (maintainer metadata)", "license_url": "https://github.com/teorth/erdosproblems/blob/main/LICENSE",
             "revision": erdos["revision"], "sha256": erdos["sha256"], "as_of": self.erdos_date,
             "description": "Every problem in the maintainers' database at the pinned commit, in every state. Categories follow the database's informal state; open-like states (falsifiable, verifiable, decidable, not provable, not disprovable) stay open with a sub-status badge.",
             "defaults": defaults("Erdős Problems", "A research question from the Erdős problems database. Read the current statement and discussion on erdosproblems.com.", erdos["revision"])},
            {"id": "kourovka", "name": "The Kourovka Notebook", "abbreviation": "KN",
             "url": f"https://arxiv.org/abs/{kourovka['arxiv_id']}v{kourovka['arxiv_version']}",
             "attribution": "E. I. Khukhro and V. D. Mazurov, editors; individual problem authors credited in the source",
             "license": "arXiv non-exclusive distribution licence; numbers, pages, markers and links only",
             "revision": f"{kourovka['edition']}, arXiv:{kourovka['arxiv_id']}v{kourovka['arxiv_version']}",
             "sha256": kourovka["pdf_sha256"], "as_of": self.kourovka_date,
             "description": "Every problem in the main section (the solved archive is excluded). ∗ before the number: answered; ∗ before a part: that part answered; ♯: an unverified AI solution, regarded as unsolved until confirmed.",
             "defaults": defaults("Kourovka Notebook", "A research question from the Kourovka Notebook of unsolved problems in group theory. The statement and author credits are on the linked page.",
                                  f"{kourovka['edition']}, arXiv:{kourovka['arxiv_id']}v{kourovka['arxiv_version']}")},
            {"id": "clay", "name": "Millennium Prize Problems", "abbreviation": "CM", "url": "https://www.claymath.org/millennium-problems/",
             "attribution": "Clay Mathematics Institute and the authors of the official problem descriptions",
             "license": "Bibliographic pointers and original short summaries", "revision": f"Clay page checked {self.clay_date}",
             "sha256": self.facts["clay"].get("sha256"), "as_of": self.clay_date,
             "description": "All seven Millennium problems; the Clay page's Solved / Active / Unsolved headings decide the source status.",
             "defaults": defaults("Millennium Prize Problems", "A Millennium Prize problem.", f"Clay page checked {self.clay_date}")},
        ]
        for ident in ("hilbert", "smale", "ramanujan"):
            item = deepcopy(curated.get(ident) or {})
            if not item:
                continue
            item["as_of"] = self.snapshot_date()
            item["description"] = {"hilbert": "Hilbert's 23 problems of 1900 and two clearly labelled open extensions (16, part II; 10 over ℚ).",
                                   "smale": "Steve Smale's 18 problems (1998); entries without a researched record are not listed.",
                                   "ramanujan": "Questions posed by Ramanujan or arising from his mathematics; later conjectures keep their authorship."}[ident]
            item["defaults"] = defaults(item["name"], f"A problem from {item['name']}.", item["revision"])
            sources.append(item)
        wiki_defaults = defaults("Wikipedia", "A problem from Wikipedia's list; the statement and status here are original and sourced.",
                                 f"List of unsolved problems in mathematics, revision {wiki['revision_id']}")
        sources += [
            {"id": "wikipedia", "name": "Wikipedia: List of unsolved problems in mathematics", "abbreviation": "WP",
             "url": wiki["permalink"], "attribution": "Wikipedia contributors (discovery only)",
             "license": "CC BY-SA 4.0 list used for names, links and revision ids only; statements and status are original",
             "license_url": "https://creativecommons.org/licenses/by-sa/4.0/", "revision": str(wiki["revision_id"]),
             "as_of": str(wiki.get("revision_timestamp") or "")[:10],
             "description": "Entries of the pinned list revision that are not the same problem as an entry above, each researched from primary sources.",
             "defaults": wiki_defaults | {"source_name": "Wikipedia list of unsolved problems"}},
            {"id": "wikipedia-solved", "name": "Recently solved (Wikipedia list)", "abbreviation": "WS",
             "url": wiki["permalink"], "attribution": "Wikipedia contributors (discovery only)",
             "license": "CC BY-SA 4.0 list used for names, links and revision ids only; statements and status are original",
             "license_url": "https://creativecommons.org/licenses/by-sa/4.0/", "revision": str(wiki["revision_id"]),
             "as_of": str(wiki.get("revision_timestamp") or "")[:10],
             "description": "\"Problems solved since 2015\" entries not already present, each checked against primary sources.",
             "defaults": wiki_defaults | {"source_name": "Wikipedia: problems solved since 2015"}},
        ]
        fc, om, ai = self.facts["formal_conjectures"], self.facts["openai_math"], self.facts["ai_wiki"]
        sources += [
            {"id": "formal-conjectures", "collection": False, "name": "google-deepmind/formal-conjectures",
             "url": fc["url"], "license": "Apache-2.0", "license_url": "https://github.com/google-deepmind/formal-conjectures/blob/main/LICENSE",
             "revision": fc["revision"], "as_of": str(fc.get("committed_at") or "")[:10],
             "description": "Lean statements (formal_statement_url) and the maintainers' research open / solved categories, used as a cross-check."},
            {"id": "openai-math", "collection": False, "name": "openai/math", "url": om["url"], "license": "Apache-2.0",
             "revision": om["revision"], "as_of": str(om.get("fetched_at") or "")[:10],
             "description": "Manuscript families, withdrawals and Lean documentation of OpenAI's October 2026 release; claims are evidence, never certificates."},
            {"id": "erdos-ai-wiki", "collection": False, "name": "AI contributions to Erdős problems (teorth wiki)",
             "url": ai.get("url") or ai.get("page"), "license": "Facts only (colours, systems, dates, sections)",
             "revision": ai.get("revision"), "as_of": ai.get("frozen_on"),
             "description": "Frozen on 2026-06-30. 🟡 rows give the AI partial result badge; 🟢 rows on solved problems give the AI-assisted solution badge."},
        ]
        order = {name: index for index, name in enumerate(COLLECTION_ORDER)}
        sources.sort(key=lambda item: (item.get("collection") is False, order.get(item["id"], 99)))
        for item in sources:
            if item.get("collection") is not False and item["id"] not in counts:
                item["empty"] = True
        return [{key: value for key, value in item.items() if value not in (None, "")} for item in sources]

    def _document(self, problems: list[dict[str, Any]]) -> dict[str, Any]:
        sources = self._sources(Counter(row["source"] for row in problems))
        defaults = {item["id"]: item.get("defaults", {}) for item in sources}
        # Fields equal to the collection default are stored once (sources[].defaults).
        compact = []
        for row in problems:
            base = defaults.get(row["source"], {})
            compact.append({key: value for key, value in row.items() if not (key in base and base[key] == value)})
        counts = {name: count for name, count in sorted(Counter(row["source"] for row in problems).items(), key=lambda item: COLLECTION_ORDER.index(item[0]))}
        category_counts = {category: sum(1 for row in problems if row["category"] == category) for category in CATEGORY_IDS}
        by_collection = {name: {category: sum(1 for row in problems if row["source"] == name and row["category"] == category)
                                for category in CATEGORY_IDS} for name in counts}
        content_sha256 = hashlib.sha256(canonical_json(compact).encode("utf-8")).hexdigest()
        return {
            "schema_version": SCHEMA_VERSION, "snapshot_date": self.snapshot_date(), "generated_at": self.generated_at(),
            "content_sha256": content_sha256, "total": len(compact),
            "categories": [dict(item) for item in CATEGORIES],
            "badges": {key: {"label": label, "description": description} for key, (label, description) in BADGES.items()},
            "category_rules": dict(RULE_TEXT),
            "category_counts": category_counts, "category_counts_by_collection": by_collection, "counts": counts,
            "sources": sources, "status_method": STATUS_METHOD,
            "status_note": "Categories come from the sources of record and reviewed research; model output is never a certificate. "
                           "AI proposed is never counted as solved. Lean results upstream are not re-checked by this server.",
            "content_note": "Original statements and sourced evidence for researched entries; other entries are bibliographic "
                            "pointers with source markers. No problem text from the Kourovka Notebook, erdosproblems.com or Wikipedia is reproduced.",
            "rating_method": deepcopy(self.rating_method), "source_input_hashes": dict(sorted(self.input_hashes.items())),
            "problems": compact,
        }


STATUS_METHOD = (
    "Each entry has exactly one category, checked in order: Solved (S1 source-of-record marker: teorth proved/disproved/"
    "solved/independent, a Kourovka ∗ before the number, the Clay Solved listing; S2 refereed or broadly accepted full "
    "answer), AI proposed (A1 a Kourovka ♯ on the whole problem; A2 a documented AI-produced full claim not yet "
    "confirmed), Partial (P1 a Kourovka ∗ on a part; P2 an accepted answer to an explicitly stated part), otherwise Open. "
    "Withdrawn or refuted claims never count. Source-of-record markers come from deterministic parsers of pinned inputs; "
    "other categories come from deep research from primary sources (October 2026, AI-assisted, with a verification pass) "
    "or committed operator decisions; research can only mark an entry solved with a refereed, maintainer-accepted or "
    "broadly accepted full answer and never asserts a source marker. Badges add partial results, claims under review, AI "
    "involvement, Lean status, prizes and conflicts between sources. The live overlay changes categories only on "
    "deterministic source changes."
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_catalogue(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"


def empty_overlay(document: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "catalogue_sha256": document["content_sha256"], "generated_at": document["generated_at"],
            "last_checked_at": None, "last_successful_refresh_at": None, "refresh_interval_minutes": 360,
            "sources": [], "entries": {}, "signals": {}, "recent_changes": [],
            "category_counts": dict(document["category_counts"]),
            "model": {"name": "kimi-k3", "calls_today": 0, "daily_cap": 0}, "source_errors": []}


def expand_row(document: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """A problem with its collection defaults filled in (what the page's indexCatalogue does)."""
    defaults = next((item.get("defaults", {}) for item in document["sources"] if item["id"] == row["source"]), {})
    return {**deepcopy(defaults), **row}


def _kourovka_solution(number: str, solution: Mapping[str, Any], *, whole: bool) -> dict[str, Any]:
    """Evidence for one solution link cited by the Notebook (links only, no text)."""
    part = solution.get("part")
    scope = "part" if part or not whole else "full"
    suffix = f", part ({part})" if part else ""
    if solution["marker"] == "#" or solution.get("ai") == "unverified":
        return {"kind": "claim", "actor": "ai", "scope": scope, "outcome": "other", "url": solution["url"],
                "title": f"Unverified AI solution cited for Kourovka {number}{suffix}", "acceptance": "preprint"}
    return {"kind": "accepted-result" if scope == "full" else "partial-result",
            "actor": "ai" if solution.get("ai") == "confirmed" else "human", "scope": scope, "outcome": "other",
            "url": solution["url"], "title": f"{'Solution' if scope == 'full' else 'Partial answer'} cited for Kourovka {number}{suffix}",
            "acceptance": "accepted-by-maintainer"}


# The math the docs site renders (mathText.js): $$…$$, \[…\], \(…\) and $…$.
_MATH_SPAN = re.compile(r"(?<!\\)\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|(?<![\\$])\$(?!\$)(?:\\.|[^$])+?\$")


def _first_sentences(text: str, limit: int = 280) -> str:
    """The first sentences of ``text`` within ``limit`` characters, never cutting inside math."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    spans = [match.span() for match in _MATH_SPAN.finditer(flat)]
    cut = limit
    for start, end in spans:
        if start < cut < end:
            cut = start  # a formula is never split
            break

    def outside(index: int) -> bool:
        return not any(start <= index < end for start, end in spans)

    head = flat[:cut]
    ends = [match.start() for match in re.finditer(r"[.?](?= |$)", head) if outside(match.start())]
    if ends and ends[-1] > 80:
        return head[: ends[-1] + 1]
    spaces = [index for index, character in enumerate(head) if character == " " and outside(index)]
    return (head[: spaces[-1]] if spaces else head).rstrip(" ,;:") + "…"


def _wiki_key(title: str | None) -> str:
    from agent_monitor.problem_sources import wikipedia_key

    return wikipedia_key(title or "")


def _evidence_order(item: Mapping[str, Any]) -> tuple:
    priority = {"source-marker": 0, "accepted-result": 1, "claim": 2, "partial-result": 3, "withdrawal": 4,
                "refutation": 5, "formalization": 6, "survey": 7, "other": 8}
    return (priority[item["kind"]], "".join(chr(0x10FFFF - ord(c)) for c in item.get("date", "")), item["url"])


def _clean_badges(badges: Iterable[str], category: str, row: Mapping[str, Any]) -> list[str]:
    wanted = set(badges)
    if row.get("prize"):
        wanted.add("prize")
    else:
        wanted.discard("prize")
    if row.get("formal_statement_url"):
        wanted.add("formal-statement")
    else:
        wanted.discard("formal-statement")
    if category == "solved":
        wanted -= {"ai-partial"}
    if category != "solved":
        wanted -= {"ai-assisted-solution", "ai-confirmed"}
    if category == "ai-proposed":
        wanted -= {"ai-partial"}
    if row["source"] != "erdos" or row.get("source_status") in (None, "open"):
        wanted.discard("upstream-substatus")
    return [badge for badge in CATALOGUE_BADGES if badge in wanted]


def validate_catalogue(document: Mapping[str, Any]) -> None:
    """Structural invariants of a built catalogue (used by the builder and the tests)."""
    if document.get("schema_version") != SCHEMA_VERSION:
        raise CatalogueError("Not a v3 catalogue")
    problems = document["problems"]
    if document["total"] != len(problems) or sum(document["counts"].values()) != len(problems):
        raise CatalogueError("Catalogue totals do not match its rows")
    ids = [row["id"] for row in problems]
    if len(ids) != len(set(ids)):
        raise CatalogueError("Duplicate problem ids")
    if hashlib.sha256(canonical_json(problems).encode("utf-8")).hexdigest() != document["content_sha256"]:
        raise CatalogueError("content_sha256 does not match the problems array")
    counts = Counter(row["category"] for row in problems)
    if {key: counts.get(key, 0) for key in CATEGORY_IDS} != document["category_counts"]:
        raise CatalogueError("category_counts do not match the rows")
    for row in problems:
        if row["category"] not in RULES or row["category_rule"] not in RULES[row["category"]] or row["status"] != row["category"]:
            raise CatalogueError(f"{row['id']}: invalid category")
        if row["category"] != "open" and not row.get("status_evidence"):
            raise CatalogueError(f"{row['id']}: a non-open category needs evidence")
        if "status_changed" in row and (row["category"] == "open" or not isinstance(row["status_changed"], str)
                                        or not DATE.fullmatch(row["status_changed"])):
            raise CatalogueError(f"{row['id']}: status_changed must be a YYYY[-MM[-DD]] date on a non-open entry")
        for item in row.get("status_evidence", []):
            evidence_item(item)
            if "manuscript" in item and (item["kind"] not in ANSWER_KINDS or not isinstance(item["manuscript"], bool)):
                raise CatalogueError(f"{row['id']}: manuscript marks answers and claims with true or false")
        for badge in row.get("badges", []):
            if badge not in CATALOGUE_BADGES:
                raise CatalogueError(f"{row['id']}: unknown badge {badge}")
        decision = row.get("decision")
        if decision is not None and not (isinstance(decision, dict) and _text(decision.get("by"), 80)
                                         and _date(decision.get("at")) and _text(decision.get("note"), 400)):
            raise CatalogueError(f"{row['id']}: a decision must name who, when and why")
        if row["category"] == "solved" and not supports_category("solved", row["category_rule"], row["status_evidence"]):
            raise CatalogueError(f"{row['id']}: solved without qualifying evidence")
        url = urlsplit(row["source_url"] if "source_url" in row else expand_row(document, row).get("source_url", ""))
        if url.scheme != "https":
            raise CatalogueError(f"{row['id']}: source_url must be https")
