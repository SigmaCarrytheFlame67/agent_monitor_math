"""Tolerant, fail-closed parsers for the CGM pipeline's model replies.

Control replies (strategist, verifier, curator) are small JSON objects that may
arrive wrapped in a fence or preceded by prose; ``find_json_object`` extracts
the last balanced object carrying the required key and repairs unescaped TeX
backslashes.  The verifier is stricter because its verdict gates acceptance:
its reply must contain exactly one verdict-bearing object with exactly the
contract's keys, so an echoed template or a quoted JSON blob can never turn a
``wrong`` into a ``correct``.  Worker replies are fixed delimited Markdown
blocks so proofs never travel inside JSON strings.  Every parser raises
``ParseError`` on a reply it cannot interpret; the runner then sends one format
reminder.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .schema import ANNOTATION_STATUSES, FINDING_KINDS, PLAN_TECHNIQUES, normalize_text

PSEUDO_TOOL_MARKERS = (
    "<function_calls>",
    "</function_calls>",
    "<invoke ",
    "<parameter ",
    '"tool_calls"',
)
STATUS_VALUES = ("solved", "partial", "stuck")
THEOREM_LABEL = "THEOREM"
_RESERVED_LABELS = frozenset({"THEOREM", "T", "NONE", "TARGET", "MAIN"})
_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_.'-]{0,23}$")
_SUBGOAL_RE = re.compile(r"^S[1-9][0-9]?$")
_SPACE_RE = re.compile(r"\s+")
_MAX_JSON_OBJECTS = 64
_VERDICT_KEY_RE = re.compile(r'"verdict"\s*:')
_VERIFIER_KEYS = frozenset({"verification_report", "verdict", "repair_hints"})
_VERIFIER_REPORT_KEYS = frozenset({"summary", "critical_errors", "gaps"})
# Per-block bounds keep every verifier prompt (statement, proof and the
# statements of the block's direct predecessors) exact and unclipped.  The
# THEOREM statement is exempt: the runner always verifies the problem text.
MAX_BLOCK_STATEMENT_BYTES = 8_000
MAX_BLOCK_PROOF_BYTES = 60_000

_DECOR = r"[\s#>*_`]*"
# Possessive quantifiers throughout: a model line with a long whitespace run
# made the former ``\s*[*_`]*\s*`` sequences backtrack cubically, holding the
# GIL (and so Stop) for minutes.  Captured groups are stripped by the callers.
_HEADER_RE = re.compile(
    rf"^{_DECOR}={{3,}}\s*+(?P<kind>LEMMA|THEOREM|STATUS|FINDING)\b"
    rf"(?P<arg>[^=]*+)={{3,}}{_DECOR}$",
    re.IGNORECASE,
)
_END_RE = re.compile(rf"^{_DECOR}={{3,}}\s*END\b[^=]*={{3,}}{_DECOR}$", re.IGNORECASE)
_KEY_RE = re.compile(
    r"^[\s>*_`-]*+(?P<key>subgoals?|uses|depends on|dependencies|cites)[\s*_`]*+:"
    r"[\s*_`]*+(?P<value>.*)$",
    re.IGNORECASE,
)
# "Statement:", "**Proof.**", and a short qualifier: "Proof (sketch):".
_QUALIFIER = r"(?:\s*+\([^()\n]{0,400}\))?"
_STATEMENT_RE = re.compile(
    rf"^[\s#>*_`]*+statement{_QUALIFIER}[\s*_`]*+(?:[:.][\s*_`]*+(?P<rest>.*))?$",
    re.IGNORECASE,
)
_PROOF_RE = re.compile(
    rf"^[\s#>*_`]*+proof{_QUALIFIER}[\s*_`]*+(?:[:.][\s*_`]*+(?P<rest>.*))?$",
    re.IGNORECASE,
)
# "Proof (structure of the computation," with the parenthesis closing on a
# later line (seen in a field-test reply): the proof starts there.
_PROOF_OPEN_RE = re.compile(r"^[\s#>*_`]*+proof\s*+(?P<rest>\([^()\n]*)$", re.IGNORECASE)
_CLAIM_RE = re.compile(r"^[\s#>*_`]*+claim[\s*_`]*+:[\s*_`]*+(?P<rest>.*)$", re.IGNORECASE)
_EVIDENCE_RE = re.compile(
    r"^[\s#>*_`]*+evidence[\s*_`]*+(?:[:.][\s*_`]*+(?P<rest>.*))?$",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*$")

# TeX commands whose leading letter is also a JSON escape (\b \f \n \r \t \u).
_TEX_AFTER_ESCAPE = re.compile(
    r"(?:b(?:eta|ar|ig|egin|inom|mod|oldsymbol|ot|ox|ullet|ackslash)"
    r"|f(?:rac|orall|lat)"
    r"|n(?:abla|atural|eq|eg|exists|leq|mid|ot|otin|u|e|i|ewline|ormalsize)"
    r"|r(?:ho|ight|angle|ceil|floor|m|oot|ightarrow)"
    r"|t(?:au|heta|imes|ext|frac|o|op|riangle|herefore|ilde|an)"
    r"|u(?:nderbrace|nderline|psilon|p|parrow|nlhd))(?![a-zA-Z])"
)


class ParseError(ValueError):
    """A model reply does not satisfy the stage's output contract."""


def has_pseudo_tool_markup(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in PSEUDO_TOOL_MARKERS)


def _require_no_tools(text: str) -> None:
    if has_pseudo_tool_markup(text):
        raise ParseError("reply contains prohibited pseudo-tool markup")


def repair_json_escapes(blob: str) -> str:
    """Escape unpaired TeX backslashes inside JSON strings.

    Paired backslashes are left alone; an odd trailing backslash is escaped
    when it starts a TeX command (even one that begins with a JSON escape
    letter such as ``\\frac``) or when JSON would reject it.
    """
    out: list[str] = []
    index = 0
    in_string = False
    while index < len(blob):
        char = blob[index]
        if not in_string:
            out.append(char)
            in_string = char == '"'
            index += 1
            continue
        if char == '"':
            out.append(char)
            in_string = False
            index += 1
            continue
        if char != "\\":
            out.append(char)
            index += 1
            continue
        end = index
        while end < len(blob) and blob[end] == "\\":
            end += 1
        count = end - index
        if count % 2 == 0:
            out.append("\\" * count)
            index = end
            continue
        following = blob[end : end + 32]
        if _TEX_AFTER_ESCAPE.match(following):
            out.append("\\" * (count + 1))
            index = end
        elif following[:1] in {'"', "/", "\\", "b", "f", "n", "r", "t"}:
            out.append("\\" * count + following[:1])
            index = end + 1
        elif re.match(r"u[0-9A-Fa-f]{4}", following):
            out.append("\\" * count + following[:5])
            index = end + 5
        else:
            out.append("\\" * (count + 1))
            index = end
    return "".join(out)


def _balanced_objects(text: str) -> list[str]:
    """Top-level ``{...}`` spans, string-aware, in order of appearance."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"' and depth > 0:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : index + 1])
                start = -1
                if len(spans) > _MAX_JSON_OBJECTS:
                    raise ParseError("reply contains too many JSON objects")
    return spans


_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def without_lone_surrogates(text: str) -> str:
    """``text`` with any unpaired UTF-16 surrogate (a JSON ``\\ud83d`` escape
    on its own, say) replaced by U+FFFD, so it encodes as UTF-8."""
    return _LONE_SURROGATE_RE.sub("\ufffd", text)


def _clean_json(value: Any) -> Any:
    if isinstance(value, str):
        return without_lone_surrogates(value)
    if isinstance(value, list):
        return [_clean_json(item) for item in value]
    if isinstance(value, dict):
        return {_clean_json(key): _clean_json(item) for key, item in value.items()}
    return value


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a JSON object repeats a key")
    return dict(pairs)


def _load_object(span: str, *, unique_keys: bool = False) -> Any:
    """``span`` as JSON, or None.  With ``unique_keys`` (a verdict object) a
    repeated key makes it invalid: JSON keeps the last one, so a report with
    two "critical_errors" lists could hide the errors it lists."""
    hook = _unique_keys if unique_keys else None
    # The repaired form first: JSON silently accepts ``\frac`` as a form feed,
    # so an unrepaired parse can succeed with corrupted text.
    for candidate in (repair_json_escapes(span), span):
        try:
            return _clean_json(json.loads(candidate, object_pairs_hook=hook))
        except (ValueError, RecursionError):  # JSONDecodeError, too-long ints, repeated keys
            continue
    return None


def find_json_object(text: str, *, required_key: str) -> dict[str, Any]:
    """Return the last JSON object in ``text`` that carries ``required_key``."""
    _require_no_tools(text)
    stripped = str(text or "").strip()
    if not stripped:
        raise ParseError("empty reply")
    found: dict[str, Any] | None = None
    for span in _balanced_objects(stripped):
        value = _load_object(span)
        if isinstance(value, dict) and required_key in value:
            found = value
    if found is None:
        raise ParseError(f"no JSON object with {required_key!r}")
    return found


def _contains_key(value: Any, key: str, depth: int = 0) -> bool:
    if depth > 32:
        return True
    if isinstance(value, Mapping):
        return key in value or any(
            _contains_key(item, key, depth + 1) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(item, key, depth + 1) for item in value)
    return False


def find_verdict_object(text: str) -> dict[str, Any]:
    """The verifier's single verdict object; any ambiguity fails closed.

    Fences and prose around the object are tolerated, but a reply with more
    than one top-level object that mentions a ``"verdict"`` key -- valid JSON
    or not, e.g. an echoed output template or a quoted blob -- is rejected, as
    is an object with keys outside the output contract or a nested verdict.
    """
    _require_no_tools(text)
    stripped = str(text or "").strip()
    if not stripped:
        raise ParseError("empty reply")
    bearing: list[Any] = []
    for span in _balanced_objects(stripped):
        value = _load_object(span, unique_keys=True)
        # Textual match catches invalid JSON; the parsed check catches keys
        # spelled with escapes (``"\u0076erdict"``).
        if _VERDICT_KEY_RE.search(span) or _contains_key(value, "verdict"):
            bearing.append(value)
    if not bearing:
        raise ParseError("no JSON object with 'verdict'")
    if len(bearing) > 1:
        raise ParseError("reply contains more than one verdict object; return exactly one")
    value = bearing[0]
    if not isinstance(value, dict) or "verdict" not in value:
        raise ParseError("the verdict object is not valid JSON")
    if not set(value).issubset(_VERIFIER_KEYS):
        raise ParseError("verifier object has keys outside the output contract")
    report = value.get("verification_report")
    if isinstance(report, Mapping) and (
        not set(report).issubset(_VERIFIER_REPORT_KEYS)
        or _contains_key(report, "verdict")
    ):
        raise ParseError("verification_report has keys outside the output contract")
    return value


def _text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    rendered = normalize_text(value)
    if len(rendered.encode("utf-8")) > limit:
        raise ParseError("a field exceeds its byte limit")
    return rendered


# --------------------------------------------------------------------------
# strategist
# --------------------------------------------------------------------------

_TECHNIQUE_ALIASES = {
    "direct": "plain",
    "direct proof": "plain",
    "direct-proving": "plain",
    "induction": "plain",
    "contradiction": "plain",
    "counterexample": "plain",
    "reduce": "reduction",
    "reduce-problem": "reduction",
    "reformulate": "reformulation",
    "reformulate-problem": "reformulation",
    "equivalence": "reformulation",
    "generalize": "generalization",
    "generalise": "generalization",
    "generalisation": "generalization",
    "generalize-problem": "generalization",
    "localize": "localization",
    "localisation": "localization",
    "localize-problem": "localization",
    "invariants": "invariant",
    "monovariant": "invariant",
    "extract-invariants": "invariant",
    "extremal principle": "extremal",
    "extremality": "extremal",
    "select-extremal-objects": "extremal",
    "apply-compactness": "compactness",
    "witnesses": "witness",
    "construction": "witness",
    "construct-witnesses": "witness",
}


def normalize_technique(value: object) -> str:
    raw = _SPACE_RE.sub(" ", str(value or "")).strip().lower()
    if raw in PLAN_TECHNIQUES:
        return raw
    return _TECHNIQUE_ALIASES.get(raw, "plain")


@dataclass(frozen=True)
class Subgoal:
    id: str
    statement: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    label: str
    technique: str
    approach: str
    subgoals: tuple[Subgoal, ...]
    risks: str


@dataclass(frozen=True)
class Conclusion:
    statement: str
    justification: str
    fragile: bool
    fragility_reason: str


@dataclass(frozen=True)
class Strategy:
    plans: tuple[Plan, ...]
    conclusions: tuple[Conclusion, ...] = ()
    obstacles: tuple[str, ...] = ()
    fallback: bool = False


def parse_strategist(
    text: str, *, max_plans: int, max_subgoals: int = 6
) -> Strategy:
    raw = find_json_object(text, required_key="plans")
    plans_raw = raw.get("plans")
    if not isinstance(plans_raw, list) or not plans_raw:
        raise ParseError("strategist returned no plans")
    plans: list[Plan] = []
    seen: set[str] = set()
    for index, item in enumerate(plans_raw, 1):
        if not isinstance(item, Mapping):
            continue
        approach = _text(item.get("approach"), limit=8000)
        if not approach:
            continue
        label = _text(item.get("label"), limit=300) or f"Plan {index}"
        key = _SPACE_RE.sub(" ", label).casefold()
        approach_key = _SPACE_RE.sub(" ", approach).casefold()
        if key in seen or approach_key in seen:
            continue
        seen.update({key, approach_key})
        subgoals: list[Subgoal] = []
        renamed: dict[str, str] = {}
        raw_subgoals = item.get("subgoals")
        for sub in raw_subgoals if isinstance(raw_subgoals, list) else []:
            if len(subgoals) >= max_subgoals:
                break
            if isinstance(sub, str):
                statement, depends, given = _text(sub, limit=4000), [], ""
            elif isinstance(sub, Mapping):
                statement = _text(sub.get("statement"), limit=4000)
                depends = sub.get("depends_on")
                given = str(sub.get("id") or "").strip().upper()
            else:
                continue
            if not statement:
                continue
            # Ids are renumbered by position; dependencies may only point to
            # earlier subgoals, which also keeps the goal graph acyclic.
            subgoal_id = f"S{len(subgoals) + 1}"
            dependencies = tuple(
                dict.fromkeys(
                    renamed[token]
                    for token in (
                        str(value).strip().upper()
                        for value in (depends if isinstance(depends, list) else [])
                    )
                    if token in renamed
                )
            )
            if given:
                renamed.setdefault(given, subgoal_id)
            subgoals.append(Subgoal(subgoal_id, statement, dependencies))
        plans.append(
            Plan(
                label=label,
                technique=normalize_technique(item.get("technique")),
                approach=approach,
                subgoals=tuple(subgoals),
                risks=_text(item.get("risks"), limit=4000),
            )
        )
        if len(plans) >= max_plans:
            break
    if not plans:
        raise ParseError("strategist returned no usable plan")
    conclusions: list[Conclusion] = []
    raw_conclusions = raw.get("immediate_conclusions")
    for item in raw_conclusions if isinstance(raw_conclusions, list) else []:
        if len(conclusions) >= 6 or not isinstance(item, Mapping):
            continue
        statement = _text(item.get("statement"), limit=2000)
        if not statement:
            continue
        conclusions.append(
            Conclusion(
                statement=statement,
                justification=_text(item.get("justification"), limit=2000),
                fragile=item.get("fragile") is True,
                fragility_reason=_text(item.get("fragility_reason"), limit=1000),
            )
        )
    obstacles: list[str] = []
    raw_obstacles = raw.get("obstacles")
    for item in raw_obstacles if isinstance(raw_obstacles, list) else []:
        rendered = _text(item, limit=2000)
        if rendered and len(obstacles) < 4:
            obstacles.append(rendered)
    return Strategy(tuple(plans), tuple(conclusions), tuple(obstacles))


def fallback_strategy() -> Strategy:
    """Single direct plan used when the strategist reply is unusable."""
    return Strategy(
        plans=(
            Plan(
                label="Direct proof",
                technique="plain",
                approach=(
                    "Check the exact quantifiers and boundary cases, then prove the "
                    "statement directly from the definitions, isolating each "
                    "nontrivial step as its own lemma."
                ),
                subgoals=(),
                risks="No strategist guidance was available for this run.",
            ),
        ),
        fallback=True,
    )


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    label: str
    statement: str
    proof: str
    uses: tuple[str, ...]
    subgoal: str | None = None


@dataclass(frozen=True)
class Finding:
    kind: str
    claim: str
    evidence: str


@dataclass(frozen=True)
class Candidate:
    lemmas: tuple[Block, ...]
    theorem: Block | None
    status: str
    findings: tuple[Finding, ...] = ()

    def lemma(self, label: str) -> Block:
        for block in self.lemmas:
            if block.label == label:
                return block
        raise KeyError(label)


def _strip_outer_fence(lines: list[str]) -> list[str]:
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if (
        first is not None
        and last is not None
        and first < last
        and _FENCE_RE.match(lines[first])
        and lines[last].strip() == "```"
    ):
        return lines[first + 1 : last]
    return lines


def _normalize_label(raw: str) -> str:
    token = re.sub(r"^(?:lemma|lem|claim|proposition|prop)\b\.?\s*", "", raw.strip(), flags=re.I)
    token = token.strip().strip("[]()*_`:.,").upper()
    if re.fullmatch(r"\d{1,3}", token):
        token = f"L{int(token)}"
    return token


def _parse_uses(value: str) -> tuple[str, ...]:
    cleaned = re.sub(r"\([^)]*\)", " ", value)
    tokens = re.split(r"[\s,;]+", cleaned.replace("[", " ").replace("]", " "))
    labels: list[str] = []
    for token in tokens:
        token = token.strip().strip("*_`.:")
        if not token or token.lower() in {
            "none", "nothing", "-", "n/a", "na", "and", "lemma", "lemmas", "by",
            "(none)", "no", "uses",
        }:
            continue
        label = _normalize_label(token)
        if label and label not in labels:
            labels.append(label)
    return tuple(labels)


# A follow-up's task lists the session problems with this tag on the last
# line of each one new this turn (jobs._execute_continue); a copy of the list
# or of one problem may keep or drop it, on any of its problems.
_NEW_THIS_TURN = "← new this turn"


def _untagged(line: str) -> str:
    """``line`` without a trailing "← new this turn" tag, which follows the
    line's text after whitespace as the task writes it."""
    text = line.rstrip()
    head = text.removesuffix(_NEW_THIS_TURN)
    if head != text and head[-1:].isspace() and not head.isspace():
        return head.rstrip()
    return line


def _squash(line: str) -> str:
    """``line`` as a copy of the problem is compared: without its tag
    (``_untagged``), whitespace runs squashed."""
    return " ".join(_untagged(line).split())


def _problem_copies(problem: str | Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """The forms of the problem a worker may copy into THEOREM's Statement
    (the whole text, the excerpt a prompt showed, one problem of a session),
    as their non-blank lines, each squashed (``_squash``)."""
    forms = (problem,) if isinstance(problem, str) else tuple(problem)
    copies: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for form in forms:
        lines = tuple(
            _squash(line) for line in normalize_text(str(form or "")).split("\n") if line.strip()
        )
        if lines and lines not in seen:
            seen.add(lines)
            copies.append(lines)
    return tuple(copies)


def _copy_ends(
    lines: Sequence[str], start: int, first: str, copies: Sequence[tuple[str, ...]]
) -> list[tuple[int, int | None, tuple[str, ...]]]:
    """Every verbatim copy of a form of the problem that opens at the
    "Statement:" line ``lines[start]``, longest first: the index of the copy's
    last line, the index of the next non-blank line (None at the end), and
    the form.

    ``first`` is the Statement line's own text after the colon; the line's
    pattern drops leading ``*``, ``_`` and backticks, so a first problem line
    made only of them (a code fence) may be on the Statement line.  Blank
    lines, runs of whitespace and a line's "← new this turn" tag may differ;
    anything else must match.  The reply's lines are squashed once, whatever
    the number of forms.
    """
    if not copies:
        return []
    budget = max(len(copy) for copy in copies) + 1
    after: list[tuple[str, int]] = []  # non-blank lines after the Statement line
    position = start + 1
    while len(after) < budget and position < len(lines):
        line = _squash(lines[position])
        if line:
            after.append((line, position))
        position += 1
    head = _squash(first)
    found: dict[int, tuple[int, int | None, tuple[str, ...]]] = {}
    for copy in copies:
        if head:
            skips: tuple[int, ...] = (1,) if head in {copy[0], copy[0].lstrip("*_` ")} else ()
        else:
            skips = (0, 1) if not copy[0].strip("*_` ") else (0,)
        for skip in skips:
            rest = copy[skip:]
            if len(rest) > len(after) or any(after[i][0] != rest[i] for i in range(len(rest))):
                continue
            last = after[len(rest) - 1][1] if rest else start
            following = after[len(rest)][1] if len(rest) < len(after) else None
            found.setdefault(last, (last, following, copy))
    return [found[last] for last in sorted(found, reverse=True)]


def _is_proof_marker(line: str) -> re.Match[str] | None:
    return _PROOF_RE.match(line) or _PROOF_OPEN_RE.match(line)


def _copied_problem_marker(
    lines: Sequence[str], start: int, first: str, copies: Sequence[tuple[str, ...]]
) -> tuple[int, int] | None:
    """For a THEOREM whose Statement (the line ``lines[start]``, with its own
    text ``first``) copies the problem verbatim: the end of the copy and the
    line of the reply's Proof marker.

    The problem's own lines are then not the reply's markers ("Proof." in a
    problem with a proof to complete, "=== END ===").  The reply's marker is
    the line right after the copy; a problem that ends with its own "Proof:"
    line, which the reply used as its marker too, keeps that line (read
    without its tag, ``_untagged``).  Of copies of several forms (one a
    prefix of another: a session's problems), the longest so followed is
    used.  None in any other case: the reply is read as before.
    """
    for last, following, copy in _copy_ends(lines, start, first, copies):
        if following is not None and _is_proof_marker(lines[following]):
            return last + 1, following
        if _is_proof_marker(copy[-1]) and last > start and _is_proof_marker(_untagged(lines[last])):
            return last + 1, last
    return None


def _split_blocks(
    lines: list[str], copies: Sequence[tuple[str, ...]] = ()
) -> list[tuple[str, str, list[str]]]:
    blocks: list[tuple[str, str, list[str]]] = []
    current: tuple[str, str, list[str]] | None = None
    # The first THEOREM's Statement may copy the problem verbatim; a marker
    # line of the problem's own inside a copy that is used ("=== END ===", a
    # block header) does not end the block.  Only the first THEOREM is tried:
    # a second one is refused anyway.
    before_statement = False
    theorem_seen = False
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        header = _HEADER_RE.match(line)
        if header:
            if current is not None:
                blocks.append(current)
            current = (header.group("kind").upper(), header.group("arg").strip(), [])
            before_statement = bool(copies) and current[0] == "THEOREM" and not theorem_seen
            theorem_seen = theorem_seen or current[0] == "THEOREM"
            continue
        if _END_RE.match(line):
            if current is not None:
                blocks.append(current)
                current = None
            before_statement = False
            continue
        if current is not None:
            current[2].append(line)
            if before_statement and line.strip() and not _KEY_RE.match(line):
                before_statement = False
                statement = _STATEMENT_RE.match(line)
                found = statement and _copied_problem_marker(
                    lines, index - 1, statement.group("rest") or "", copies
                )
                if found:
                    current[2].extend(lines[index : found[0]])
                    index = found[0]
    if current is not None:
        blocks.append(current)
    return blocks


def _parse_statement_block(
    body: list[str], *, label: str, copies: Sequence[tuple[str, ...]] = ()
) -> tuple[str, str, tuple[str, ...], str | None]:
    uses: tuple[str, ...] = ()
    subgoal: str | None = None
    index = 0
    statement_start: int | None = None
    first_statement = ""
    while index < len(body):
        line = body[index]
        if not line.strip():
            index += 1
            continue
        key = _KEY_RE.match(line)
        if key:
            name = key.group("key").lower()
            if name.startswith("subgoal"):
                # "Subgoal: S2" or "Subgoals: S1, S2": a block serves the first.
                tokens = re.split(r"[\s,;]+", key.group("value").strip().strip("*_`[]().").upper())
                token = tokens[0] if tokens else ""
                subgoal = token if _SUBGOAL_RE.fullmatch(token) else None
            else:
                uses = _parse_uses(key.group("value"))
            index += 1
            continue
        match = _STATEMENT_RE.match(line)
        if match:
            statement_start = index + 1
            first_statement = match.group("rest") or ""
        break
    if statement_start is None:
        raise ParseError(f"block {label} has no Statement section")
    proof_start: int | None = None
    first_proof = ""
    after_copy = (
        _copied_problem_marker(body, statement_start - 1, first_statement, copies)
        if copies
        else None
    )
    # The marker may be the copy's own last line ("Proof:"), whose tag is the
    # task's, never proof text.
    marker = _is_proof_marker(_untagged(body[after_copy[1]])) if after_copy is not None else None
    if after_copy is not None and marker is not None:
        proof_start = after_copy[1] + 1
        first_proof = marker.group("rest") or ""
    else:
        for position in range(statement_start, len(body)):
            match = _is_proof_marker(body[position])
            if match:
                proof_start = position + 1
                first_proof = match.group("rest") or ""
                break
    if proof_start is None:
        raise ParseError(f"block {label} has no Proof section")
    statement = normalize_text(
        "\n".join([first_statement, *body[statement_start : proof_start - 1]])
    )
    proof = normalize_text("\n".join([first_proof, *body[proof_start:]]))
    if not statement:
        raise ParseError(f"block {label} has an empty statement")
    if len(proof) < 20:
        raise ParseError(f"block {label} has an empty or trivial proof")
    return statement, proof, uses, subgoal


def _parse_finding(kind_arg: str, body: list[str]) -> Finding | None:
    kind = kind_arg.strip().lower().replace("-", "_").replace(" ", "_")
    kind = {"deadend": "dead_end", "dead": "dead_end"}.get(kind, kind)
    if kind not in FINDING_KINDS:
        return None
    claim = ""
    evidence_lines: list[str] = []
    in_evidence = False
    for line in body:
        if not in_evidence:
            claim_match = _CLAIM_RE.match(line)
            if claim_match and not claim:
                claim = claim_match.group("rest")
                continue
            evidence_match = _EVIDENCE_RE.match(line)
            if evidence_match:
                in_evidence = True
                if evidence_match.group("rest"):
                    evidence_lines.append(evidence_match.group("rest"))
                continue
        evidence_lines.append(line)
    evidence = normalize_text("\n".join(evidence_lines))
    claim = normalize_text(claim) or (evidence.splitlines()[0] if evidence else "")
    if not claim:
        return None
    return Finding(kind, claim[:2000], (evidence or claim)[:20_000])


def _check_acyclic(lemmas: Mapping[str, Block]) -> None:
    state: dict[str, int] = {}

    def visit(label: str, depth: int) -> None:
        if depth > 64:
            raise ParseError("lemma dependencies are too deep")
        mark = state.get(label, 0)
        if mark == 1:
            raise ParseError("lemma dependencies contain a cycle")
        if mark == 2:
            return
        state[label] = 1
        for used in lemmas[label].uses:
            visit(used, depth + 1)
        state[label] = 2

    for label in lemmas:
        visit(label, 0)


def _check_block_size(label: str, statement: str | None, proof: str) -> None:
    if statement is not None and len(statement.encode("utf-8")) > MAX_BLOCK_STATEMENT_BYTES:
        raise ParseError(
            f"block {label} statement exceeds {MAX_BLOCK_STATEMENT_BYTES} bytes; "
            "state it concisely"
        )
    if len(proof.encode("utf-8")) > MAX_BLOCK_PROOF_BYTES:
        raise ParseError(
            f"block {label} proof exceeds {MAX_BLOCK_PROOF_BYTES} bytes; split it "
            "into smaller lemmas"
        )


def parse_worker(
    text: str, *, max_lemmas: int = 8, problem: str | Sequence[str] = ()
) -> Candidate:
    """Parse the delimited worker blocks into a validated candidate.

    ``problem`` is the problem as the worker may have copied it (one text, or
    each form a prompt showed): when the THEOREM's Statement copies one
    verbatim and the reply's Proof marker follows the copy, a line of the
    problem's own ("Proof.", "=== END ===") is not read as a marker.  Any
    other reply is read as without it.
    """
    _require_no_tools(text)
    lines = _strip_outer_fence(normalize_text(text).split("\n"))
    copies = _problem_copies(problem)
    blocks = _split_blocks(lines, copies)
    if not blocks:
        raise ParseError("reply contains no delimited blocks")
    lemmas: dict[str, Block] = {}
    theorem: Block | None = None
    status = ""
    findings: list[Finding] = []
    for kind, arg, body in blocks:
        if kind == "LEMMA":
            label = _normalize_label(arg) if arg else f"L{len(lemmas) + 1}"
            if not _LABEL_RE.fullmatch(label) or label in _RESERVED_LABELS:
                raise ParseError(f"invalid lemma label {arg!r}")
            if label in lemmas:
                raise ParseError(f"duplicate lemma label {label}")
            if len(lemmas) >= max_lemmas:
                raise ParseError(f"more than {max_lemmas} lemmas")
            statement, proof, uses, subgoal = _parse_statement_block(body, label=label)
            _check_block_size(label, statement, proof)
            lemmas[label] = Block(label, statement, proof, uses, subgoal)
        elif kind == "THEOREM":
            if theorem is not None:
                raise ParseError("more than one THEOREM block")
            statement, proof, uses, _subgoal = _parse_statement_block(
                body, label=THEOREM_LABEL, copies=copies
            )
            _check_block_size(THEOREM_LABEL, None, proof)
            theorem = Block(THEOREM_LABEL, statement, proof, uses, None)
        elif kind == "STATUS":
            words = " ".join(body).strip().strip("*_`.").lower().split()
            status = words[0].strip("*_`.:") if words else ""
        elif kind == "FINDING" and len(findings) < 6:
            finding = _parse_finding(arg, body)
            if finding is not None:
                findings.append(finding)
    for block in [*lemmas.values(), *([theorem] if theorem else [])]:
        unknown = [label for label in block.uses if label not in lemmas]
        if unknown:
            raise ParseError(f"{block.label} uses unknown lemma {unknown[0]}")
        if block.label in block.uses:
            raise ParseError(f"{block.label} uses itself")
    _check_acyclic(lemmas)
    if status not in STATUS_VALUES:
        status = "partial" if theorem is not None else "stuck"
    if theorem is None and status == "solved":
        raise ParseError("status solved without a THEOREM block")
    if theorem is None and not lemmas and not findings:
        raise ParseError("reply contains no lemma, theorem or finding")
    return Candidate(tuple(lemmas.values()), theorem, status, tuple(findings))


def closure_labels(candidate: Candidate) -> list[str]:
    """Theorem-closure lemma labels in a deterministic topological order."""
    if candidate.theorem is None:
        return []
    by_label = {block.label: block for block in candidate.lemmas}
    needed: set[str] = set()
    pending = list(candidate.theorem.uses)
    while pending:
        label = pending.pop()
        if label not in needed:
            needed.add(label)
            pending.extend(by_label[label].uses)
    written = [block.label for block in candidate.lemmas if block.label in needed]
    order: list[str] = []
    placed: set[str] = set()
    while len(order) < len(written):
        progressed = False
        for label in written:
            if label not in placed and all(dep in placed for dep in by_label[label].uses):
                order.append(label)
                placed.add(label)
                progressed = True
        if not progressed:
            raise ParseError("lemma dependencies contain a cycle")
    return order


def render_candidate(candidate: Candidate) -> str:
    """Canonical block rendering (repair prompts, attempt evidence)."""
    parts: list[str] = []

    def block(kind: str, item: Block) -> None:
        header = "=== THEOREM ===" if kind == "THEOREM" else f"=== LEMMA {item.label} ==="
        lines = [header]
        if item.subgoal:
            lines.append(f"Subgoal: {item.subgoal}")
        lines.append(f"Uses: {', '.join(item.uses) if item.uses else 'none'}")
        lines += ["Statement:", item.statement, "Proof:", item.proof, "=== END ==="]
        parts.append("\n".join(lines))

    for lemma in candidate.lemmas:
        block("LEMMA", lemma)
    if candidate.theorem is not None:
        block("THEOREM", candidate.theorem)
    for finding in candidate.findings:
        parts.append(
            f"=== FINDING {finding.kind} ===\nClaim: {finding.claim}\n"
            f"Evidence:\n{finding.evidence}\n=== END ==="
        )
    parts.append(f"=== STATUS ===\n{candidate.status}\n=== END ===")
    return "\n\n".join(parts) + "\n"


# --------------------------------------------------------------------------
# verifier
# --------------------------------------------------------------------------

_EMPTY_HINTS = frozenset({"", "none", "n/a", "na", "-", "no repairs needed", "none needed"})


@dataclass(frozen=True)
class Issue:
    location: str
    issue: str


@dataclass(frozen=True)
class VerifierReport:
    verdict: str  # "correct" | "wrong" | "error"
    summary: str
    critical_errors: tuple[Issue, ...]
    gaps: tuple[Issue, ...]
    repair_hints: str
    normalized: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "verification_report": {
                "summary": self.summary,
                "critical_errors": [
                    {"location": item.location, "issue": item.issue}
                    for item in self.critical_errors
                ],
                "gaps": [
                    {"location": item.location, "issue": item.issue} for item in self.gaps
                ],
            },
            "verdict": self.verdict,
            "repair_hints": self.repair_hints,
        }


def _issues(value: object, *, field_name: str) -> tuple[Issue, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ParseError(f"verifier {field_name} is not a list")
    items: list[Issue] = []
    for entry in value[:48]:
        if isinstance(entry, str) and entry.strip():
            items.append(Issue("unspecified", normalize_text(entry)[:4000]))
        elif isinstance(entry, Mapping):
            issue = normalize_text(str(entry.get("issue") or ""))
            location = _SPACE_RE.sub(" ", str(entry.get("location") or "")).strip()
            if issue:
                items.append(Issue((location or "unspecified")[:300], issue[:4000]))
            else:
                raise ParseError(f"verifier {field_name} entry lacks an issue")
        else:
            raise ParseError(f"verifier {field_name} entry is malformed")
    return tuple(items)


def parse_verifier(text: str) -> VerifierReport:
    """Validate the CGM verifier contract; any finding forces ``wrong``.

    Report strings are line-normalized (``normalize_text``), so no Unicode
    line separator reaches the published memory rows.
    """
    raw = find_verdict_object(text)
    report = raw.get("verification_report")
    if not isinstance(report, Mapping):
        raise ParseError("verifier reply lacks verification_report")
    verdict_raw = str(raw.get("verdict") or "").strip().lower()
    if verdict_raw not in {"correct", "wrong"}:
        raise ParseError("verifier verdict must be correct or wrong")
    summary = normalize_text(str(report.get("summary") or ""))[:8000]
    critical = _issues(report.get("critical_errors"), field_name="critical_errors")
    gaps = _issues(report.get("gaps"), field_name="gaps")
    hints_value = raw.get("repair_hints")
    hints = normalize_text(hints_value) if isinstance(hints_value, str) else ""
    normalized = False
    if verdict_raw == "correct":
        if critical or gaps:
            # The strict rule: any error or gap forces "wrong".
            verdict_raw = "wrong"
            normalized = True
        elif hints.lower().strip(".") not in _EMPTY_HINTS:
            raise ParseError("verifier correct verdict carries repair hints")
        else:
            hints = ""
    if verdict_raw == "wrong" and not hints:
        listed = [f"{item.location}: {item.issue}" for item in (*critical, *gaps)]
        hints = (
            "Repair every listed issue: " + " | ".join(listed)
            if listed
            else "The verifier rejected the proof without itemized issues; rewrite it "
            "with every step justified explicitly."
        )[:8000]
        normalized = True
    return VerifierReport(
        verdict=verdict_raw,
        summary=summary or ("Accepted." if verdict_raw == "correct" else "Rejected."),
        critical_errors=critical,
        gaps=gaps,
        repair_hints=hints[:8000],
        normalized=normalized,
    )


def error_report(reason: str) -> VerifierReport:
    """A non-verdict: the verifier reply could not be interpreted."""
    return VerifierReport(
        verdict="error",
        summary=f"Verifier output could not be interpreted: {reason}"[:2000],
        critical_errors=(),
        gaps=(),
        repair_hints="",
    )


# --------------------------------------------------------------------------
# curator
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CuratorNote:
    status: str | None
    score: float | None
    comment: str
    cited: tuple[str, ...] = field(default_factory=tuple)


def parse_curator(
    text: str,
    *,
    aliases: Iterable[str],
    citable: Iterable[str] | None = None,
) -> dict[str, CuratorNote]:
    """Parse curator notes for ``aliases`` (frontier nodes).

    Evidence citations (``N<k>`` tokens in a comment) are kept when they name
    any alias in ``citable`` (default: the frontier aliases).
    """
    raw = find_json_object(text, required_key="annotations")
    items = raw.get("annotations")
    if not isinstance(items, list):
        raise ParseError("curator annotations must be a list")
    known = set(aliases)
    citable_set = set(citable) if citable is not None else known
    notes: dict[str, CuratorNote] = {}
    for item in items[:256]:
        if not isinstance(item, Mapping):
            continue
        alias = str(item.get("node") or item.get("node_id") or "").strip().upper()
        if alias not in known or alias in notes:
            continue
        status = str(item.get("status") or "").strip().lower() or None
        if status not in ANNOTATION_STATUSES:
            status = None
        score_raw = item.get("score")
        score: float | None = None
        if isinstance(score_raw, (int, float)) and not isinstance(score_raw, bool):
            if 0.0 <= float(score_raw) <= 1.0:
                score = round(float(score_raw), 4)
        comment = normalize_text(str(item.get("comment") or ""))[:1200]
        cited = tuple(
            dict.fromkeys(
                token.upper()
                for token in re.findall(r"\bN\d{1,3}\b", comment, flags=re.I)
                if token.upper() in citable_set
            )
        )
        notes[alias] = CuratorNote(status, score, comment, cited)
    if not notes:
        raise ParseError("curator returned no usable annotation")
    return notes
