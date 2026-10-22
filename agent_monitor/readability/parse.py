"""Strict, fail-closed parsing of Readability model replies.

A reply that does not keep its contract raises ``ParseError``; the turn
machinery then sends one format reminder (``math_harness_runner
._call_and_parse``).  Pseudo-tool markup is refused there for every reply.

* The guide is one JSON object with the guide's keys only; ids that are not
  in the result list are dropped, and every string is cleaned (URLs removed,
  bounded).  A guide that still cannot be read is replaced by a deterministic
  one: the rewrite never depends on it.
* A rewrite sits between ``=== READABLE ===`` and ``=== END ===`` (a missing
  END means the reply was cut off), carries exactly one ``[[result:<id>]]``
  per listed result (none unknown, none twice), and has no code fence and no
  raw HTML.  A rewrite whose only defect is a missing anchor is kept in
  :attr:`RewriteParser.lenient` so that, once its format retry is spent, the
  runner can accept it and record a coverage issue.
* A check is exactly one object with a verdict: ``faithful`` if and only if
  no issue is major (a faithful verdict with a major issue becomes
  ``unfaithful``; an unfaithful one without a major issue is refused).
* An answer is Markdown of at most 20,000 characters, with no code fence and
  no raw HTML; ``[[result:<id>]]`` survives only for the run's own results.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from agent_monitor.analyze.verdicts import clean_text
from agent_monitor.cgm import parsing as cgm_parsing
from agent_monitor.cgm.parsing import ParseError
from agent_monitor.readability.prompts import MARKER_RE, MAX_ANSWER_CHARS

GUIDE_KEYS = frozenset({
    "title", "one_line", "main_results", "key_idea", "roadmap", "notation", "terms", "map", "extra_edges",
    "reader_notes",
})
CHECK_KEYS = frozenset({"verdict", "summary", "issues"})
ISSUE_KEYS = ("severity", "location", "problem", "fix")
VERDICTS = ("faithful", "unfaithful")
SEVERITIES = ("major", "minor")
MAX_ISSUES = 12
_DECOR = r"[\s#>*_`]*"
_START_RE = re.compile(rf"(?im)^{_DECOR}={{3,}}\s*READABLE\s*={{3,}}{_DECOR}$")
_END_RE = re.compile(rf"(?im)^{_DECOR}={{3,}}\s*END\s*={{3,}}{_DECOR}$")
_FENCE_RE = re.compile(r"(?m)^ {0,3}(?:```|~~~)")
# Math, set aside before the Markdown is read as the console and the shared
# viewer do (display environments, $$...$$, \[...\], \(...\), then $...$
# within a line, in that order): ``<a>`` there is a group, not a tag.  Each
# pattern is linear in the reply (a body ends at the next opener; escapes are
# unrolled), so a hostile reply cannot stall the parser.
_MATH_ENV = r"\\begin\{(?:equation|align|alignat|gather)\*?\}"
_MATH_RES = (
    re.compile(rf"\\begin\{{((?:equation|align|alignat|gather)\*?)\}}(?:(?!{_MATH_ENV}|\\end\{{\1\}})[\s\S])*"
               r"\\end\{\1\}"),
    re.compile(r"\$\$[\s\S]+?\$\$"),
    re.compile(r"\\\[(?:[^\\]|\\(?![\[\]]))+\\\]"),
    re.compile(r"\\\((?:[^\\]|\\(?![()]))+\\\)"),
    re.compile(r"(?<![\\$])\$(?!\$)(?:[^$\n\\]|\\.)+\$"),
)
_NOT_NEWLINE_RE = re.compile(r"[^\n]")
# Space inside a tag: never a blank line or a line opening a quote, which
# end the paragraph in Markdown.
_TAG_SPACE = r"(?:[ \t/]|\n(?![ \t]*[>\n]))"
# Raw HTML outside math, as a Markdown reader passes it through: a comment, a
# processing instruction or declaration, a tag of any element (attributes
# after spaces, line breaks or "/"; an unquoted value is matched possessively),
# or a line (in a quote or a list item, say) that opens an HTML block.
# ``a<b``, ``0<x<1`` and ``<https://...>`` are not tags.
_HTML_RE = re.compile(
    r"<!--|<[?!][A-Za-z\[]"
    rf"|</?[A-Za-z][A-Za-z0-9-]*(?:{_TAG_SPACE}+[A-Za-z_:][\w.:-]*"
    rf"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]++))?)*{_TAG_SPACE}*>"
    r"|^[ \t]*(?:(?:>|[-*+]|\d{1,9}[.)])[ \t]*)*</?[A-Za-z][A-Za-z0-9-]*(?=[\s/>]|$)",
    re.M,
)
# Refused anywhere, math included (so that setting math aside never lets
# them through): an element that runs code, loads content or changes how the
# page is read, a tag (of any name a browser reads) with an event handler, or
# a comment.
_ACTIVE_HTML_RE = re.compile(
    r"<!--|</?(?:applet|audio|base|body|button|canvas|details|dialog|embed|form|frame|frameset|head|html|iframe|"
    r"image|img|input|isindex|link|listing|marquee|math|meta|noembed|noframes|noscript|object|param|picture|"
    r"plaintext|portal|script|select|source|style|svg|template|textarea|title|track|video|xmp)(?=[\s/>]|$)"
    r"|<[A-Za-z][^\s/<>]*(?:[\s/][^<>]*)?[\s/]on[a-z]{3,}\s*=",
    re.I | re.M,
)
# A link reference definition the model wrote: neutralized, so that no line
# of model text becomes an anchor (``[result:<id>]: #`` lines are ours).
_LINK_DEFINITION_RE = re.compile(r"(?m)^( {0,3})\[(?=[^\]\n]+\]:)")


def _single_object(text: str, *, required: str, allowed: frozenset[str]) -> dict[str, Any]:
    """The reply's one JSON object carrying ``required``; any ambiguity (two
    such objects, an echoed template, keys outside the contract) fails."""
    if cgm_parsing.has_pseudo_tool_markup(text):
        raise ParseError("reply contains prohibited pseudo-tool markup")
    stripped = str(text or "").strip()
    if not stripped:
        raise ParseError("empty reply")
    bearing = []
    pattern = re.compile(rf'"{re.escape(required)}"\s*:')
    for span in cgm_parsing._balanced_objects(stripped):
        value = cgm_parsing._load_object(span)
        if pattern.search(span) or cgm_parsing._contains_key(value, required):
            bearing.append(value)
    if not bearing:
        raise ParseError(f"no JSON object with {required!r}")
    if len(bearing) > 1:
        raise ParseError(f"reply contains more than one object with {required!r}; return exactly one")
    value = bearing[0]
    if not isinstance(value, dict) or required not in value:
        raise ParseError(f"the object with {required!r} is not valid JSON")
    if not set(value).issubset(allowed):
        raise ParseError("the object has keys outside the output contract")
    return value


def _list(value: object, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ParseError(f"{name} must be a list")
    return value


def _strings(value: object, name: str, *, limit: int, size: int) -> list[str]:
    out = []
    for entry in _list(value, name)[:limit]:
        text = clean_text(entry, size) if isinstance(entry, str) else ""
        if text:
            out.append(" ".join(text.split()))
    return out


def _known(value: object, known: set[str]) -> str | None:
    identifier = str(value or "").strip().lower()
    return identifier if identifier in known else None


# ---------------------------------------------------------------------------
# the guide
# ---------------------------------------------------------------------------


def parse_guide(text: str, *, node_ids: Iterable[str]) -> dict[str, Any]:
    """The reading guide (see the module docstring)."""
    raw = _single_object(text, required="one_line", allowed=GUIDE_KEYS)
    known = set(node_ids)
    main_results = []
    for entry in _list(raw.get("main_results"), "main_results")[:8]:
        if isinstance(entry, Mapping) and clean_text(entry.get("plain"), 800):
            main_results.append({"id": _known(entry.get("id"), known), "plain": clean_text(entry.get("plain"), 800)})
    notation = []
    for entry in _list(raw.get("notation"), "notation")[:80]:
        if isinstance(entry, Mapping) and clean_text(entry.get("symbol"), 120):
            notation.append({
                "symbol": clean_text(entry.get("symbol"), 120),
                "meaning": clean_text(entry.get("meaning"), 400),
                "where": clean_text(entry.get("where"), 160),
                "id": _known(entry.get("id"), known),
            })
    terms = []
    for entry in _list(raw.get("terms"), "terms")[:40]:
        if isinstance(entry, Mapping) and clean_text(entry.get("term"), 120):
            terms.append({key: clean_text(entry.get(key), size)
                          for key, size in (("term", 120), ("meaning", 400), ("where", 160))})
    importance: dict[str, dict[str, Any]] = {}
    for entry in _list(raw.get("map"), "map"):
        if not isinstance(entry, Mapping):
            continue
        identifier = _known(entry.get("id"), known)
        try:
            level = int(entry.get("importance"))
        except (TypeError, ValueError):
            continue
        if identifier and 1 <= level <= 5 and identifier not in importance:
            importance[identifier] = {"id": identifier, "importance": level, "role": clean_text(entry.get("role"), 300)}
    edges = []
    for entry in _list(raw.get("extra_edges"), "extra_edges")[:60]:
        if not isinstance(entry, Mapping):
            continue
        used, user = _known(entry.get("from"), known), _known(entry.get("to"), known)
        if used and user and used != user and {"from": used, "to": user} not in edges:
            edges.append({"from": used, "to": user})
    guide = {
        "title": clean_text(raw.get("title"), 300),
        "one_line": clean_text(raw.get("one_line"), 600),
        "main_results": main_results,
        "key_idea": clean_text(raw.get("key_idea"), 1500),
        "roadmap": _strings(raw.get("roadmap"), "roadmap", limit=10, size=400),
        "notation": notation,
        "terms": terms,
        "map": list(importance.values()),
        "extra_edges": edges,
        "reader_notes": _strings(raw.get("reader_notes"), "reader_notes", limit=6, size=400),
    }
    if not guide["one_line"]:
        raise ParseError("one_line must say what the manuscript proves or does")
    return guide


# ---------------------------------------------------------------------------
# a rewrite
# ---------------------------------------------------------------------------


def _without_math(text: str) -> str:
    """``text`` with its math blanked out (line breaks kept)."""
    for pattern in _MATH_RES:
        text = pattern.sub(lambda match: _NOT_NEWLINE_RE.sub(" ", match.group(0)), text)
    return text


def math_spans(text: str) -> list[tuple[int, int]]:
    """Where the math of ``text`` is, as the console sets it aside: the
    outermost spans, in order."""
    spans = []
    for pattern in _MATH_RES:
        spans.extend(match.span() for match in pattern.finditer(text))
        text = pattern.sub(lambda match: _NOT_NEWLINE_RE.sub(" ", match.group(0)), text)
    outermost: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not outermost or start >= outermost[-1][1]:
            outermost.append((start, end))
    return outermost


def _plain_markdown(body: str, what: str) -> None:
    if _FENCE_RE.search(body):
        raise ParseError(f"code fences are not allowed in {what}")
    if _ACTIVE_HTML_RE.search(body) or _HTML_RE.search(_without_math(body)):
        raise ParseError(f"raw HTML is not allowed in {what} (use Markdown, and write < and > only inside $...$)")


def readable_body(text: str) -> str:
    """The Markdown between ``=== READABLE ===`` and ``=== END ===``."""
    starts = list(_START_RE.finditer(text or ""))
    if not starts:
        raise ParseError("no === READABLE === line")
    if len(starts) > 1:
        raise ParseError("more than one === READABLE === block; return exactly one")
    end = _END_RE.search(text, starts[0].end())
    if end is None:
        raise ParseError("the reply was cut off before === END ===; keep the rewrite shorter if you must")
    body = text[starts[0].end():end.start()].strip("\n")
    if not body.strip():
        raise ParseError("the readable part is empty")
    _plain_markdown(body, "the readable part")
    return _LINK_DEFINITION_RE.sub(r"\1\\[", body)


class RewriteParser:
    """Parses the rewrite of one part whose listed results are ``node_ids``.

    One instance per turn.  :attr:`lenient` keeps the last reply that kept
    the contract except for missing anchors (``{"markdown", "missing"}``).
    """

    def __init__(self, node_ids: Iterable[str]) -> None:
        self.node_ids = list(node_ids)
        self.lenient: dict[str, Any] | None = None

    def __call__(self, text: str) -> dict[str, Any]:
        body = readable_body(text)
        found = [match.group(1).lower() for match in MARKER_RE.finditer(body)]
        unknown = sorted({identifier for identifier in found if identifier not in self.node_ids})
        if unknown:
            raise ParseError("[[result:...]] names results that are not listed: " + ", ".join(unknown[:6]))
        repeated = sorted({identifier for identifier in found if found.count(identifier) > 1})
        if repeated:
            raise ParseError("write [[result:<id>]] once per listed result; repeated: " + ", ".join(repeated[:6]))
        missing = [identifier for identifier in self.node_ids if identifier not in found]
        result = {"markdown": body, "missing": missing}
        if missing:
            self.lenient = result
            raise ParseError("the [[result:<id>]] line is missing for " + ", ".join(missing[:6])
                             + "; write it once, on its own line, before each listed result")
        return result


# ---------------------------------------------------------------------------
# a check
# ---------------------------------------------------------------------------


def parse_check(text: str) -> dict[str, Any]:
    """The faithfulness check of one rewritten part (see the module docstring)."""
    raw = _single_object(text, required="verdict", allowed=CHECK_KEYS)
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise ParseError('verdict must be "faithful" or "unfaithful"')
    issues = []
    for entry in _list(raw.get("issues"), "issues")[:MAX_ISSUES]:
        if not isinstance(entry, Mapping) or not set(entry).issubset(ISSUE_KEYS):
            raise ParseError("an issue is malformed")
        severity = str(entry.get("severity") or "").strip().lower()
        problem = clean_text(entry.get("problem"), 1000)
        if severity not in SEVERITIES or not problem:
            raise ParseError("every issue needs a severity (major or minor) and a problem")
        issues.append({"severity": severity, "location": clean_text(entry.get("location"), 200) or "unspecified",
                       "problem": problem, "fix": clean_text(entry.get("fix"), 600)})
    major = any(item["severity"] == "major" for item in issues)
    if verdict == "unfaithful" and not major:
        raise ParseError("an unfaithful verdict needs at least one major issue")
    return {
        "verdict": "unfaithful" if major else "faithful",
        "model_verdict": verdict,
        "summary": clean_text(raw.get("summary"), 1200),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# an answer
# ---------------------------------------------------------------------------


def parse_answer(text: str, *, node_ids: Iterable[str]) -> dict[str, Any]:
    """A reader's answer: ``{"answer": markdown, "refs": [ids]}``."""
    answer = str(text or "").strip()
    if not answer:
        raise ParseError("empty reply")
    if len(answer) > MAX_ANSWER_CHARS:
        raise ParseError(f"the answer is longer than {MAX_ANSWER_CHARS:,} characters; answer briefly")
    _plain_markdown(answer, "the answer")
    known = set(node_ids)
    refs: list[str] = []

    def keep(match: re.Match) -> str:
        identifier = match.group(1).lower()
        if identifier not in known:
            return ""
        if identifier not in refs:
            refs.append(identifier)
        return f"[[result:{identifier}]]"

    answer = MARKER_RE.sub(keep, answer)
    return {"answer": _LINK_DEFINITION_RE.sub(r"\1\\[", answer), "refs": refs}
