"""Deterministic adapters that turn pinned upstream inputs into small fact files.

The fact files under ``agent_monitor/data/problem_sources/`` hold numbers,
states, markers, page numbers, dates, names, links and revision ids only: no
problem text from the Kourovka Notebook, erdosproblems.com or Wikipedia.  They
are the inputs of ``scripts/build_problem_catalogue.py`` and the "last
published" baseline of the problem-status updater.

Nothing here executes or compiles upstream material.  Network access goes
through :class:`HTTPSFetcher` (or an injected fake with the same ``get``
signature); every adapter is a pure function of the bytes it is given.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from typing import Any, Iterable, Mapping
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

UTC = timezone.utc
USER_AGENT = "AnsatzeProblems/1.0 (+https://ansatze.ai/problem-sources)"
MAX_BYTES = 8 * 1024 * 1024
MAX_URL = 500  # links that are stored or published
# GET requests the fetcher sends: a MediaWiki query for 50 article titles is
# 1,200-1,600 characters long (the API accepts about 8 KB).
MAX_REQUEST_URL = 2000
SHRINK_LIMIT = 0.02

TEORTH_REPO = "https://github.com/teorth/erdosproblems"
TEORTH_COMMITS_ATOM = TEORTH_REPO + "/commits/main.atom"
TEORTH_RAW = "https://raw.githubusercontent.com/teorth/erdosproblems/{commit}/data/problems.yaml"
TEORTH_BLOB = TEORTH_REPO + "/blob/{commit}/data/problems.yaml"
AI_WIKI_PAGE = "AI-contributions-to-Erd%C5%91s-problems"
AI_WIKI_URL = TEORTH_REPO + "/wiki/" + AI_WIKI_PAGE
AI_WIKI_RAW = "https://raw.githubusercontent.com/wiki/teorth/erdosproblems/" + AI_WIKI_PAGE + ".md"
KOUROVKA_ARXIV_ID = "1401.0300"
KOUROVKA_PDF = "https://arxiv.org/pdf/1401.0300v{version}"
KOUROVKA_ABS = "https://arxiv.org/abs/1401.0300v{version}"
ARXIV_API = "https://export.arxiv.org/api/query"
FC_REPO = "https://github.com/google-deepmind/formal-conjectures"
FC_COMMITS_ATOM = FC_REPO + "/commits/main.atom"
OPENAI_REPO = "https://github.com/openai/math"
OPENAI_COMMITS_ATOM = OPENAI_REPO + "/commits/main.atom"
OPENAI_RAW = "https://raw.githubusercontent.com/openai/math/{commit}/{path}"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_LIST = "List_of_unsolved_problems_in_mathematics"
PALOMAR_API = "https://data.palomar-registry.org/api/v1/results"
PALOMAR_ENTRY = "https://palomar-registry.org/entry?id={id}&version={version}"
CLAY_PAGE = "https://www.claymath.org/millennium-problems/"

ERDOS_SOLVED_STATES = {"proved": "proof", "disproved": "disproof", "solved": "other", "independent": "independence"}
ERDOS_OPEN_STATES = ("open", "falsifiable", "verifiable", "decidable", "not provable", "not disprovable")
ERDOS_SPECIAL_COMMENTS = {"ambiguous statement", "literature review sought"}
# teorth `comments` also carries short status remarks ("partial r=2 result"),
# which must not become a problem's name.
_REMARK = re.compile(r"\b(?:result|results|partial|solved|proved|disproved|see|open)\b", re.I)


class SourceError(ValueError):
    """A public-safe adapter or transport failure (no upstream text in the message)."""


def timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def safe_url(value: object, *, allow_http: bool = False, max_length: int = MAX_URL) -> str | None:
    """Return a normalized https URL (no credentials, no control characters), or None.

    ``max_length`` defaults to the limit for stored and published links; the
    fetcher allows longer request URLs (``MAX_REQUEST_URL``).
    """
    if not isinstance(value, str) or not value or len(value) > max_length or any(ord(c) < 33 for c in value):
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
    except ValueError:
        return None
    schemes = ("https", "http") if allow_http else ("https",)
    if (parts.scheme not in schemes or not host or "." not in host or parts.username is not None
            or parts.password is not None or port not in (None, 443)):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, parts.fragment))


def check_shrink(new_count: int, old_count: int | None, *, what: str, limit: float = SHRINK_LIMIT) -> None:
    """Fail closed when an upstream parse suddenly loses more than ``limit`` of its records."""
    if old_count and new_count < old_count * (1 - limit):
        raise SourceError(f"{what}: parsed {new_count} records, previously {old_count}; refusing to replace the last good file")


# ── transport ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FetchResult:
    status: int
    body: bytes = b""
    etag: str = ""
    last_modified: str = ""
    url: str = ""


def _header(value: object) -> str:
    return value if isinstance(value, str) and len(value) <= 512 and not any(ord(c) < 32 for c in value) else ""


class HTTPSFetcher:
    """GET over HTTPS with the transport rules of ``news.HTTPSFetcher``.

    Only public addresses; the checked address is pinned for the TCP connection
    while TLS still verifies the publisher hostname; at most three redirects,
    each re-validated against ``allowed_hosts``; identity encoding; size and
    body-time caps.  Conditional requests use ETag / Last-Modified.
    """

    def __init__(self, timeout: float = 30, resolver=None, connection_factory=None, user_agent: str = USER_AGENT):
        self.timeout = timeout
        self.resolver = resolver or socket.getaddrinfo
        self.connection_factory = connection_factory or http.client.HTTPSConnection
        self.context = ssl.create_default_context()
        # Advertise the protocol we speak: some CDNs (WordPress.com among them)
        # answer TLS clients that offer no ALPN with HTTP 403.
        self.context.set_alpn_protocols(["http/1.1"])
        self.user_agent = user_agent

    def public_addresses(self, host: str) -> list[str]:
        addresses = list(dict.fromkeys(row[4][0] for row in self.resolver(host, 443, type=socket.SOCK_STREAM)))
        if not addresses:
            raise SourceError("Source hostname did not resolve")
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise SourceError("Source hostname resolved to a non-public address")
        return addresses

    def get(self, url: str, *, allowed_hosts: Iterable[str], max_bytes: int = MAX_BYTES,
            headers: Mapping[str, str] | None = None, accept: str = "*/*") -> FetchResult:
        hosts = {host.lower() for host in allowed_hosts}
        current = url
        for redirects in range(4):
            checked = safe_url(current, max_length=MAX_REQUEST_URL)
            parts = urlsplit(checked or "")
            if not checked or parts.hostname not in hosts or parts.fragment:
                raise SourceError("URL is outside the trusted source list")
            addresses = self.public_addresses(parts.hostname)
            connection = self.connection_factory(parts.hostname, 443, timeout=self.timeout, context=self.context)
            address = addresses[0]
            connection._create_connection = lambda ignored, timeout, source_address=None: socket.create_connection((address, 443), timeout, source_address)
            try:
                request_headers = {"User-Agent": self.user_agent, "Accept": accept, "Accept-Encoding": "identity"}
                for key in ("If-None-Match", "If-Modified-Since"):
                    if headers and _header(headers.get(key)):
                        request_headers[key] = headers[key]
                connection.request("GET", urlunsplit(("", "", parts.path or "/", parts.query, "")), headers=request_headers)
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    if redirects == 3:
                        raise SourceError("Too many redirects")
                    location = response.getheader("Location")
                    if not location:
                        raise SourceError("Redirect has no target")
                    current = urljoin(checked, location)
                    continue
                if response.status == 304:
                    return FetchResult(304, etag=_header(response.getheader("ETag")),
                                       last_modified=_header(response.getheader("Last-Modified")), url=checked)
                if response.status != 200:
                    return FetchResult(response.status, url=checked)
                if (response.getheader("Content-Encoding") or "identity").lower() not in ("", "identity"):
                    raise SourceError("Unsupported content encoding")
                length = response.getheader("Content-Length", "")
                if length.isdigit() and int(length) > max_bytes:
                    raise SourceError("Response exceeds size limit")
                deadline = time.monotonic() + self.timeout
                body = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SourceError("Response body deadline exceeded")
                    if connection.sock is not None:
                        connection.sock.settimeout(min(self.timeout, remaining))
                    chunk = response.read1(min(65536, max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise SourceError("Response exceeds size limit")
                return FetchResult(200, bytes(body), _header(response.getheader("ETag")),
                                   _header(response.getheader("Last-Modified")), checked)
            finally:
                connection.close()
        raise SourceError("Too many redirects")


def fetch_ok(fetcher, url: str, *, allowed_hosts: Iterable[str], max_bytes: int = MAX_BYTES, accept: str = "*/*") -> bytes:
    result = fetcher.get(url, allowed_hosts=allowed_hosts, max_bytes=max_bytes, accept=accept)
    if result.status != 200:
        raise SourceError(f"Source returned HTTP {result.status}")
    return result.body


def _xml_root(body: bytes, *, max_elements: int = 20000) -> ET.Element:
    if len(body) > MAX_BYTES or b"\x00" in body:
        raise SourceError("Feed is too large or not XML text")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        raise SourceError("Feed DTDs and entity declarations are forbidden")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise SourceError("Feed contains invalid XML") from None
    if sum(1 for _ in root.iter()) > max_elements:
        raise SourceError("Feed exceeds element limit")
    return root


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ET.Element, name: str) -> str:
    for child in node:
        if _local(child.tag) == name:
            return "".join(child.itertext()).strip()
    return ""


def parse_commits_atom(body: bytes) -> list[dict[str, str]]:
    """Commits from a GitHub ``commits/<branch>.atom`` feed, newest first."""
    root = _xml_root(body)
    commits = []
    for entry in (node for node in root.iter() if _local(node.tag) == "entry"):
        match = re.search(r"Commit/([0-9a-f]{40})$", _child_text(entry, "id"))
        if not match:
            continue
        commits.append({"sha": match.group(1), "title": clean_text(_child_text(entry, "title"), 200),
                        "updated": _child_text(entry, "updated")[:20]})
    if not commits:
        raise SourceError("Commit feed has no commits")
    return commits


# ── Erdős problems (teorth/erdosproblems) ─────────────────────────────────────


def _erdos_lines(text: str) -> dict[str, int]:
    lines = {}
    for index, line in enumerate(text.splitlines(), 1):
        match = re.match(r'^- number:\s*"?(\d{1,5})"?\s*$', line)
        if match:
            lines.setdefault(match.group(1), index)
    return lines


def erdos_name(comment: object) -> str | None:
    if not isinstance(comment, str):
        return None
    text = clean_text(comment, 120)
    if not text or text.lower() in ERDOS_SPECIAL_COMMENTS or _REMARK.search(text):
        return None
    return text


def parse_erdos_yaml(text: str) -> dict[str, dict[str, Any]]:
    """Facts for every problem in teorth's ``data/problems.yaml`` (all states)."""
    import yaml

    try:
        rows = yaml.safe_load(text)
    except yaml.YAMLError:
        raise SourceError("Erdős data is not valid YAML") from None
    if not isinstance(rows, list) or not rows:
        raise SourceError("Erdős data has no problem list")
    lines = _erdos_lines(text)
    problems: dict[str, dict[str, Any]] = {}
    known = set(ERDOS_SOLVED_STATES) | set(ERDOS_OPEN_STATES)
    for row in rows:
        if not isinstance(row, dict):
            raise SourceError("Erdős record is not a mapping")
        number = str(row.get("number", "")).strip()
        if not re.fullmatch(r"[1-9]\d{0,4}", number) or number in problems:
            raise SourceError("Erdős record has an invalid or duplicate number")
        informal = row.get("informal_status") or {}
        state = str(informal.get("state", "")).strip().lower()
        if state not in known:
            raise SourceError(f"Erdős #{number} has an unknown state")
        formal = row.get("formal_status") or {}
        formal_state = str(formal.get("state", "unformalized")).strip() or "unformalized"
        comments = row.get("comments")
        comment_list = comments if isinstance(comments, list) else [comments] if comments else []
        names = [name for name in (erdos_name(item) for item in comment_list) if name]
        flags = {clean_text(item, 60).lower() for item in comment_list if isinstance(item, str)}
        prize = str(row.get("prize", "no")).strip()
        tags = [clean_text(tag, 60) for tag in row.get("tags") or [] if isinstance(tag, str) and tag.strip()][:8]
        record: dict[str, Any] = {
            "state": state,
            "last_update": str(informal.get("last_update", ""))[:10] or None,
            "formal_state": formal_state,
            "formal_url": safe_url(formal.get("url")) if formal_state != "unformalized" else None,
            "formal_note": clean_text(formal.get("note"), 300) or None,
            "formal_last_update": str(formal.get("last_update", ""))[:10] or None,
            "statement_formalized": str((row.get("formalized") or {}).get("state", "")).lower() == "yes",
            "prize": None if prize.lower() in ("", "no", "none") else clean_text(prize, 40),
            "tags": tags,
            "names": names,
            "ambiguous": "ambiguous statement" in flags,
            "literature_review_sought": "literature review sought" in flags,
            "line": lines.get(number),
        }
        problems[number] = {key: value for key, value in record.items() if value not in (None, [], False) or key in ("state",)}
    return dict(sorted(problems.items(), key=lambda item: int(item[0])))


def erdos_fact_file(text: str, *, commit: str, fetched_at: str, previous: Mapping | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise SourceError("Erdős data needs the full commit SHA it was read at")
    problems = parse_erdos_yaml(text)
    check_shrink(len(problems), len((previous or {}).get("problems") or {}), what="Erdős problems")
    states: dict[str, int] = {}
    for item in problems.values():
        states[item["state"]] = states.get(item["state"], 0) + 1
    return {
        "schema_version": 1, "source": "teorth/erdosproblems", "url": TEORTH_REPO,
        "file": "data/problems.yaml", "revision": commit, "sha256": sha256_bytes(text.encode("utf-8")),
        "fetched_at": fetched_at, "license": "Apache-2.0",
        "counts": {"problems": len(problems), "states": dict(sorted(states.items()))},
        "problems": problems,
    }


# ── teorth AI-contributions wiki (frozen 2026-06-30) ──────────────────────────

_MONTHS = {name: index for index, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_COLOURS = {"🟢": "full", "🟡": "partial", "🔴": "incorrect", "⚪": "unverified"}
_OUTCOME_RANK = {"full": 3, "partial": 2, "unverified": 1, "incorrect": 0}


def _wiki_date(cell: str) -> str | None:
    found = re.findall(r"(\d{1,2})\s+([A-Z][a-z]{2}),?\s*(\d{4})", cell)
    if not found:
        return None
    day, month, year = found[-1]
    if month not in _MONTHS:
        return None
    return f"{int(year):04d}-{_MONTHS[month]:02d}-{int(day):02d}"


def parse_ai_wiki(markdown: str) -> dict[str, dict[str, Any]]:
    """Per-problem facts from the AI-contributions wiki tables: colours, systems, dates, sections."""
    section = None
    columns: list[str] = []
    problems: dict[str, dict[str, Any]] = {}
    rows = 0
    for raw in markdown.splitlines():
        heading = re.match(r"^#{2,4}\s+(\d\([a-d]\))\.", raw.strip())
        if heading:
            section, columns = heading.group(1), []
            continue
        line = raw.strip()
        if not section or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not columns:
            columns = [cell.lower() for cell in cells]
            continue
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        record = dict(zip(columns, cells))
        numbers = re.findall(r"\[\[(\d{1,5})\]\]", record.get("problem", ""))
        if not numbers:
            continue
        # Primary tables put the colour in "outcome"; formalization and rewriting
        # tables colour the proof being formalized or rewritten instead.
        coloured = record.get("outcome") or record.get("proof to formalize") or record.get("argument to rewrite") or ""
        colour = next((value for symbol, value in _COLOURS.items() if symbol in coloured), None)
        systems = [clean_text(item, 60) for item in (record.get("ai systems") or "").split(",") if item.strip()][:8]
        text = coloured.lower()
        row = {"section": section, "outcome": colour,
               "lean": "(lean)" in text, "variant": "variant" in text or "related result" in text,
               "new_proof": "new proof" in text, "systems": systems, "date": _wiki_date(record.get("date", ""))}
        rows += 1
        for number in numbers:
            entry = problems.setdefault(number, {"rows": []})
            entry["rows"].append({key: value for key, value in row.items() if value not in (None, False, [])})
    if rows < 10:
        raise SourceError("AI-contributions wiki tables were not recognized")
    for entry in problems.values():
        primary = [row for row in entry["rows"] if row["section"].startswith("1") and row.get("outcome")]
        best = max(primary, key=lambda row: _OUTCOME_RANK[row["outcome"]], default=None)
        entry["best_primary"] = best["outcome"] if best else None
        # "Full" for the badge logic means a full primary solution, not a new
        # proof of an already solved problem and not a variant.
        entry["full_solution"] = any(row.get("outcome") == "full" and not row.get("variant") and not row.get("new_proof") for row in primary)
        entry["partial_result"] = any(row.get("outcome") == "partial" and not row.get("variant") for row in primary)
        entry["systems"] = sorted({system for row in primary for system in row.get("systems", [])})[:12]
        dates = sorted(row["date"] for row in primary if row.get("date"))
        entry["last_primary_date"] = dates[-1] if dates else None
        entry["sections"] = sorted({row["section"] for row in entry["rows"]})
    return dict(sorted(problems.items(), key=lambda item: int(item[0])))


def ai_wiki_fact_file(markdown: str, *, commit: str, fetched_at: str, frozen_on: str | None = None) -> dict:
    problems = parse_ai_wiki(markdown)
    frozen = frozen_on
    match = re.search(r"latest data is as of (\w{3}) (\d{1,2}), (\d{4})", markdown)
    if match and match.group(1) in _MONTHS:
        frozen = f"{int(match.group(3)):04d}-{_MONTHS[match.group(1)]:02d}-{int(match.group(2)):02d}"
    # Rows are summarized per problem; keep the per-row facts compact.
    compact = {}
    for number, entry in problems.items():
        compact[number] = {key: value for key, value in entry.items() if value not in (None, [], False)}
    return {
        "schema_version": 1, "source": "teorth/erdosproblems wiki", "page": AI_WIKI_URL,
        "revision": commit, "url": f"{AI_WIKI_URL}/{commit}" if commit else AI_WIKI_URL,
        "sha256": sha256_bytes(markdown.encode("utf-8")), "fetched_at": fetched_at, "frozen_on": frozen,
        "colour_legend": {"full": "🟢", "partial": "🟡", "incorrect": "🔴", "unverified": "⚪"},
        "counts": {"problems": len(compact),
                   "primary_problems": sum(1 for entry in compact.values() if entry.get("best_primary"))},
        "problems": compact,
    }


# ── Kourovka Notebook (arXiv 1401.0300, main section) ─────────────────────────

KOUROVKA_TOPICS = {
    "finite groups": r"\bfinite group",
    "infinite groups": r"\binfinite group",
    "group rings": r"\bgroup (?:ring|algebra)",
    "free groups": r"\bfree (?:\w+ )?group",
    "automorphisms": r"automorphism",
    "representations": r"representation|character(?:s|istic)?\b",
    "subgroups": r"subgroup",
    "nilpotent groups": r"nilpoten",
    "soluble groups": r"solubl|solvabl",
    "simple groups": r"simple group",
    "p-groups": r"p-group|p−group",
    "profinite groups": r"profinite",
    "orderable groups": r"orderable|order(?:ed|ing) group",
    "group presentations": r"presentation|presented|defining relation",
    "algorithmic group theory": r"algorithm|decidab|recursive|word problem",
    "geometric group theory": r"hyperbolic|quasi-isometr|growth|amenab",
    "group cohomology": r"cohomolog|homolog",
}
_KV_SECTION = re.compile(r"^(?:Problems from the (\d{1,2})(?:st|nd|rd|th) Issue \(\d{4}\)|New Problems(?: \((\d{1,2})(?:st|nd|rd|th) [Ii]ssue, \d{4}\))?|Archive of [Ss]olved [Pp]roblems)\s*$")
# The table of contents names the issue of the bare "New Problems" heading.
_KV_CONTENTS_NEW = re.compile(r"^New Problems \((\d{1,2})(?:st|nd|rd|th) [Ii]ssue, \d{4}\)")
_KV_HEADING = re.compile(r"^([∗*♯]?)(\d{1,2})\.(\d{1,3})\.")
_KV_RUNNING_HEAD = re.compile(r"^(?:\d{1,3}|\d{1,3}\s+.{0,40}(?:Issue|issue|Problems).{0,40}|.{0,40}(?:Issue|issue|Problems).{0,40}\s+\d{1,3})$")
_KV_LABEL = r"(?:[a-h]|i{1,3}|iv|vi{0,3})"
# Two-letter labels such as "(ae)" and "(ao)" (part a, even and odd cases) are
# accepted only in parentheses at the start of a line.
_KV_LONG_LABEL = r"\(([a-h][a-z])\)"
# A part marker opens a line or follows sentence punctuation; its label is
# followed by a space, a comma or the end of the line.  This keeps notation
# such as "δ ∗(x)f", "W∗(F_k)", "G ∗ H" or "property (∗)" out.
_KV_PART = re.compile(r"(?:^\s?([∗*♯])\s?" + _KV_LONG_LABEL + r"|(?:^|(?<=[.?!:;)]))\s?([∗*♯])\s?\(?(" + _KV_LABEL
                      + r")\))(?=[\s,]|$)")
_KV_ANSWER = re.compile(r"^([∗*♯])\s?(?=[A-Z(“\"])")
_KV_SEGMENT = re.compile(r"^([∗*♯])\s?(?:" + _KV_LONG_LABEL + r"(?=[\s,]|$)|\(?(" + _KV_LABEL + r")\)(?=[\s,]|$))?")
KOUROVKA_STRAY_LIMIT = 20
# A new edition may remove a few markers (a refuted AI solution loses its ♯);
# losing more at once looks like a text-extraction change and needs review.
KOUROVKA_MARKER_LOSS = (5, 0.03)  # at least 5, or 3% of the marked problems
# Gaining markers is normal (October 2026 added about 110), but notation read as
# markers would mark far more problems answered at once.
KOUROVKA_MARKER_GAIN = (50, 0.15)  # at least 50, or 15% of the unmarked problems
KOUROVKA_NEW_ANOMALIES = 5
_KV_UNVERIFIED = re.compile(r"unverified\s+AI\s+solutions?", re.I)
_KV_CONFIRMED = re.compile(r"AI(?:\s+solutions?|[-\s]proofs?)\s+(?:confirmed|verified)\s+by\s+([^:;)]{2,80}?)\s*[:;)]")
_KV_BY_AI = re.compile(r"AI\s+solutions?\s+by\s+([^:;)]{2,60}?)\s*[:;)]")


def _marker(char: str) -> str:
    return "#" if char == "♯" else "*"


def _join_url_breaks(lines: list[str]) -> list[str]:
    """Rejoin URLs that PDF text extraction split across lines."""
    joined: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        while index + 1 < len(lines):
            tail = re.search(r"(https?:(?://)?[^\s()]*)$", line)
            following = lines[index + 1].strip()
            if not tail or not following:
                break
            token = tail.group(1)
            if (token in ("https:", "http:") or token.endswith(("/", ".", ":", "-", "_", "=", "?", "&"))) and \
                    re.match(r"[A-Za-z0-9/~_%.\-]", following):
                line = line + following
                index += 1
                continue
            break
        joined.append(line)
        index += 1
    return joined


def _citation_pieces(text: str) -> list[tuple[str, bool]]:
    """The text split at top-level parentheses: (piece, inside a citation group)."""
    pieces, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            if depth == 0:
                pieces.append((text[start:index], False))
                start = index + 1
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            if depth == 0:
                pieces.append((text[start:index], True))
                start = index + 1
    pieces.append((text[start:], depth > 0))
    return [(piece, group) for piece, group in pieces if piece.strip()]


def _ai_qualifier(unit: str, marker: str) -> str | None:
    if _KV_UNVERIFIED.search(unit):
        return "unverified"
    if re.search(r"AI(?:\s+solutions?|[-\s]proofs?)\s+(?:confirmed|verified)\s+by", unit):
        return "confirmed"
    if re.search(r"AI\s+solutions?\s+by\b", unit):
        return "confirmed" if marker == "*" else "unverified"
    return None


# arXiv identifiers: YYMM.NNNN until 2014, YYMM.NNNNN since 2015, or archive/NNNNNNN.
_ARXIV_ID_URL = re.compile(r"^https://arxiv\.org/(?:abs|pdf)/(?:(?:0[7-9]|1[0-4])\d{2}\.\d{4}|(?:1[5-9]|[2-9]\d)\d{2}\.\d{5}"
                           r"|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?")
_ZENODO_ID_URL = re.compile(r"^https://zenodo\.org/records?/\d+")


def _cited_url(raw: str) -> str | None:
    """A cited link without trailing punctuation; text glued to an arXiv or Zenodo identifier is cut off.

    PDF extraction can drop the space after a link ("…/abs/1910.00712forn= 5"
    for "…/abs/1910.00712 for n = 5"); an arXiv link that is not an identifier
    is dropped rather than published.
    """
    url = safe_url(raw.rstrip(".").rstrip(")"))
    if not url or url.endswith(("/", ":")):
        return None
    host = urlsplit(url).hostname
    if host == "arxiv.org":
        match = _ARXIV_ID_URL.match(url)
        return match.group(0) if match else None
    match = _ZENODO_ID_URL.match(url) if host == "zenodo.org" else None
    if match and url[match.end():match.end() + 1] not in ("", "/", "?", "#"):
        return match.group(0)
    return url


def _solutions(lines: list[str]) -> list[dict[str, Any]]:
    """Solution links cited in answer lines, each with its marker, part and AI qualifier.

    An answer starts at a line led by a marker: "∗No, ..." (the whole problem) or
    "♯b) These ..." (a part).  A marked part that ends in a question is the
    part's statement, unless the same part was already marked earlier.  Every
    link of an answer counts, inside or outside parentheses.  A citation group
    starts with the qualifier stated just before it (or none); "; "-separated
    units inherit "unverified AI solution" / "AI solution confirmed by" until
    another qualifier appears, also past a ")" that closed the group early.
    """
    segments: list[list[Any]] = []
    for line in _join_url_breaks(lines):
        stripped = line.lstrip()
        match = _KV_SEGMENT.match(stripped)
        part = match and (match.group(2) or match.group(3))
        if match and (part or _KV_ANSWER.match(stripped)):
            segments.append([_marker(match.group(1)), part or None, [stripped]])
        elif segments:
            segments[-1][2].append(line)
    seen: set[str] = set()
    found: list[dict[str, Any]] = []
    for marker, part, body in segments:
        text = re.sub(r"-\n(?=[a-z])", "", "\n".join(body)).replace("\n", " ")
        answer = part is None or part in seen or "?" not in text
        if part:
            seen.add(part)
        if not answer:
            continue
        qualifier = lead = None
        for piece, group in _citation_pieces(text):
            if group:
                qualifier = lead
            lead = None
            for unit in piece.split(";"):
                stated = _ai_qualifier(unit, marker)
                if stated:
                    qualifier = stated
                    lead = None if group else stated
                for raw in re.findall(r"https://[^\s;,()<>\"]+", unit):
                    url = _cited_url(raw)
                    if url and all(item["url"] != url for item in found):
                        found.append({"marker": marker, "part": part, "url": url, "ai": qualifier})
    return found[:8]


def parse_kourovka_pages(pages: list[str]) -> dict[str, Any]:
    """Problems of the main section (before the solved archive) with their markers.

    Header markers (``∗``/``♯`` before the number) and part markers (before a
    sub-item label such as ``a)``) are recorded separately; ``∗`` inside
    mathematics is notation and is ignored.  Page numbers are 1-based PDF pages.
    """
    if not pages:
        raise SourceError("Kourovka PDF has no pages")
    issue = None
    archive_page = None
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    last = (0, 0)
    stray: list[str] = []
    new_issue = next((int(match.group(1)) for page in pages[:12] for line in page.split("\n")
                      if (match := _KV_CONTENTS_NEW.match(line.strip()))), None)
    pending = ""  # a ∗/♯ glyph that text extraction put on a line of its own (an ASCII "*" may be a footnote)
    for page_number, page in enumerate(pages, 1):
        lines = page.split("\n")
        if lines and _KV_RUNNING_HEAD.match(lines[0].strip()):
            lines = lines[1:]
        for line in lines:
            section = _KV_SECTION.match(line.strip())
            if section:
                if line.strip().lower().startswith("archive"):
                    archive_page = page_number
                    break
                if section.group(1) or section.group(2):
                    issue = int(section.group(1) or section.group(2))
                elif new_issue or issue:
                    # A bare "New Problems" heading: the issue named in the contents, else the next one.
                    issue = new_issue or issue + 1
                else:
                    raise SourceError("Cannot tell which Kourovka issue the New Problems section belongs to")
                current, pending = None, ""
                continue
            if issue is None:
                continue
            if line.strip() in ("∗", "♯"):
                pending = line.strip()
                continue
            marker, pending = pending, ""
            heading = _KV_HEADING.match(line)
            if heading and int(heading.group(2)) == issue:
                problem = int(heading.group(3))
                if last[0] != issue or problem > last[1]:
                    marker = heading.group(1) or marker
                    current = {"number": f"{issue}.{problem}", "page": page_number,
                               "header_marker": _marker(marker) if marker else None,
                               "lines": [line[heading.end():]]}
                    entries.append(current)
                    last = (issue, problem)
                    continue
                stray.append(f"{issue}.{problem}")
            elif heading:
                stray.append(f"{heading.group(2)}.{heading.group(3)}")
            if current is not None:
                current["lines"].append(marker + line if marker else line)
        if archive_page is not None:
            break
    if archive_page is None or not entries:
        raise SourceError("Cannot locate the Kourovka main section and its solved archive")
    if len(set(stray)) > KOUROVKA_STRAY_LIMIT:
        # Problem headings outside their issue section: most likely a section read
        # with the wrong issue number, which would silently drop its problems.
        raise SourceError(f"Kourovka parse found {len(set(stray))} problem headings outside their issue section; refusing")
    problems: dict[str, dict[str, Any]] = {}
    anomalies: list[dict[str, str]] = []
    for entry in entries:
        lines = entry.pop("lines")
        text = "\n".join(lines)
        flat = re.sub(r"-\n(?=[a-z])", "", text).replace("\n", " ")
        parts: list[dict[str, str]] = []
        for line in lines:
            for match in _KV_PART.finditer(line):
                item = {"part": match.group(2) or match.group(4), "marker": _marker(match.group(1) or match.group(3))}
                if item not in parts:
                    parts.append(item)
        answers = [_marker(match.group(1)) for line in lines[1:] if (match := _KV_ANSWER.match(line))]
        confirmed = [clean_text(name, 80) for name in _KV_CONFIRMED.findall(flat)]
        by_ai = [clean_text(name, 60) for name in _KV_BY_AI.findall(flat)]
        marked = bool(entry["header_marker"] or parts)
        starred = entry["header_marker"] == "*" or any(p["marker"] == "*" for p in parts)
        unverified = bool(_KV_UNVERIFIED.search(flat))
        # A header ♯ is the editors' statement that the problem has an
        # unconfirmed AI solution, whatever else its parts record.
        if entry["header_marker"] == "#":
            ai = "unverified"
        elif confirmed or (by_ai and starred):
            ai = "confirmed"
        elif unverified or any(p["marker"] == "#" for p in parts):
            ai = "unverified"
        else:
            ai = None
        if not marked and answers:
            anomalies.append({"number": entry["number"], "issue": "answer marker without a header or part marker"})
        if (entry["header_marker"] == "#" or any(p["marker"] == "#" for p in parts)) and not unverified:
            anomalies.append({"number": entry["number"], "issue": "sharp marker without an unverified-AI attribution"})
        record: dict[str, Any] = {
            "page": entry["page"], "header_marker": entry["header_marker"], "part_markers": parts,
            "ai": ai, "in_main_section": True,
            "topics": [topic for topic, pattern in KOUROVKA_TOPICS.items() if re.search(pattern, flat, flags=re.I)],
        }
        names = list(dict.fromkeys(confirmed))
        if names:
            record["confirmer"] = "; ".join(names)[:160]
        if marked:
            solutions = _solutions(lines)
            if solutions:
                record["solutions"] = [{key: value for key, value in item.items() if value is not None} for item in solutions]
        problems[entry["number"]] = record
    return {"problems": problems, "archive_page": archive_page, "pages": len(pages),
            "stray_headings": sorted(set(stray)), "anomalies": anomalies}


def _kourovka_markers(item: Mapping[str, Any]) -> set[str]:
    """Where a problem carries a marker: "header" and the labels of marked parts."""
    marked = {"header"} if item.get("header_marker") else set()
    return marked | {part["part"] for part in item.get("part_markers") or []}


def check_kourovka_drift(parsed: Mapping[str, Any], previous: Mapping[str, Any] | None) -> None:
    """Fail closed when a new parse looks like a text-extraction change rather than an editorial one.

    Checks that the previously listed problems survive (not only their total),
    that markers do not vanish in bulk from problems that are still listed (a
    glyph that no longer extracts would reopen every marked problem) or appear
    on far more of them than an edition answers, and that parse anomalies and
    stray headings do not grow.
    """
    old = (previous or {}).get("problems") or {}
    if not old:
        return
    new = parsed["problems"]
    missing = [number for number in old if number not in new]
    if len(missing) > len(old) * SHRINK_LIMIT:
        raise SourceError(f"Kourovka problems: {len(missing)} of {len(old)} previously listed problems are missing; "
                          "refusing to replace the last good file (a new edition needs a reviewed refresh)")
    marked = {number for number, item in old.items() if _kourovka_markers(item)}
    lost = [number for number in marked if number in new and _kourovka_markers(old[number]) - _kourovka_markers(new[number])]
    floor, share = KOUROVKA_MARKER_LOSS
    if len(lost) > max(floor, round(len(marked) * share)):
        raise SourceError(f"Kourovka markers: {len(lost)} listed problems lost a ∗/♯ marker at once; refusing to replace "
                          "the last good file")
    unmarked = [number for number in old if number not in marked and number in new]
    gained = [number for number in unmarked if _kourovka_markers(new[number])]
    floor, share = KOUROVKA_MARKER_GAIN
    if len(gained) > max(floor, round(len(unmarked) * share)):
        raise SourceError(f"Kourovka markers: {len(gained)} listed problems gained a ∗/♯ marker at once; refusing to replace "
                          "the last good file")
    for key, what in (("anomalies", "parse anomalies"), ("stray_headings", "stray headings")):
        grown = len(parsed.get(key) or []) - len((previous or {}).get(key) or [])
        if grown > KOUROVKA_NEW_ANOMALIES:
            raise SourceError(f"Kourovka parse has {grown} more {what} than the last good file; refusing")


def kourovka_fact_file(pages: list[str], *, pdf_sha256: str, arxiv_version: int, fetched_at: str,
                       edition: str, published: str | None = None, previous: Mapping | None = None) -> dict:
    parsed = parse_kourovka_pages(pages)
    problems = parsed["problems"]
    check_shrink(len(problems), len((previous or {}).get("problems") or {}), what="Kourovka problems")
    check_kourovka_drift(parsed, previous)
    counts = {"problems": len(problems),
              "header_solved": sum(1 for row in problems.values() if row["header_marker"] == "*"),
              "header_ai_proposed": sum(1 for row in problems.values() if row["header_marker"] == "#"),
              "part_solved_only": sum(1 for row in problems.values() if not row["header_marker"] and any(p["marker"] == "*" for p in row["part_markers"])),
              "part_ai_proposed_only": sum(1 for row in problems.values() if not row["header_marker"] and any(p["marker"] == "#" for p in row["part_markers"]) and not any(p["marker"] == "*" for p in row["part_markers"])),
              "ai_confirmed": sum(1 for row in problems.values() if row["ai"] == "confirmed"),
              "ai_unverified": sum(1 for row in problems.values() if row["ai"] == "unverified")}
    return {
        "schema_version": 1, "source": "Kourovka Notebook", "edition": edition,
        "arxiv_id": KOUROVKA_ARXIV_ID, "arxiv_version": int(arxiv_version),
        "url": KOUROVKA_ABS.format(version=int(arxiv_version)), "pdf_url": KOUROVKA_PDF.format(version=int(arxiv_version)),
        "pdf_sha256": pdf_sha256, "published": published, "fetched_at": fetched_at,
        "license": "arXiv non-exclusive distribution licence; numbers, pages, markers and links only",
        "pages": parsed["pages"], "archive_page": parsed["archive_page"],
        "markers": {"*": "answered (∗)", "#": "unverified AI solution (♯); regarded as unsolved until confirmed"},
        "counts": counts, "anomalies": parsed["anomalies"], "stray_headings": parsed["stray_headings"],
        "problems": problems,
    }


def pdf_pages(data: bytes) -> list[str]:
    """Extract per-page text with pypdf (BSD); never executes PDF content."""
    from io import BytesIO
    from pypdf import PdfReader

    if not data.startswith(b"%PDF-"):
        raise SourceError("Download is not a PDF")
    reader = PdfReader(BytesIO(data))
    if len(reader.pages) > 600:
        raise SourceError("PDF has too many pages")
    return [page.extract_text() or "" for page in reader.pages]


def parse_arxiv_feed(body: bytes) -> list[dict[str, str]]:
    """Entries of an arXiv API Atom response (ids keep their version suffix)."""
    root = _xml_root(body)
    entries = []
    for entry in (node for node in root.iter() if _local(node.tag) == "entry"):
        identifier = _child_text(entry, "id")
        match = re.search(r"arxiv\.org/abs/([^\s/]+?)(v\d+)?$", identifier)
        if not match:
            continue
        arxiv_id, version = match.group(1), match.group(2) or ""
        entries.append({"id": arxiv_id, "version": version, "title": clean_text(_child_text(entry, "title"), 300),
                        "summary": clean_text(_child_text(entry, "summary"), 1800),
                        "published": _child_text(entry, "published")[:20], "updated": _child_text(entry, "updated")[:20],
                        "url": f"https://arxiv.org/abs/{arxiv_id}{version}"})
    return entries


def kourovka_latest_version(fetcher) -> int:
    body = fetch_ok(fetcher, ARXIV_API + "?" + urlencode({"id_list": KOUROVKA_ARXIV_ID}),
                    allowed_hosts=("export.arxiv.org",), max_bytes=256 * 1024)
    entries = parse_arxiv_feed(body)
    if not entries or entries[0]["id"] != KOUROVKA_ARXIV_ID or not entries[0]["version"]:
        raise SourceError("arXiv did not report a Kourovka version")
    return int(entries[0]["version"][1:])


# ── google-deepmind/formal-conjectures ────────────────────────────────────────

_FC_ATTR = re.compile(r"@\[(?P<attrs>[^\]]*?\bcategory\s+(?P<cat>research open|research solved|textbook|API|test)\b[^\]]*)\]\s*(?:--[^\n]*\s*)*(?:theorem|lemma)\s+(?P<name>[^\s:({]+)", re.S)
_FC_PROOF = re.compile(r"formal_proof\s+using\s+\w+\s+at\s+\"([^\"]+)\"")
_FC_WIKI = re.compile(r"https?://en\.wikipedia\.org/wiki/([^\s)\]>#\"]+)")


def wikipedia_key(title: str) -> str:
    """Normalize a Wikipedia article title or URL path for matching."""
    text = unquote(title).replace("_", " ").strip()
    return " ".join(text.split()).casefold()


def _fc_statements(text: str) -> list[dict[str, Any]]:
    statements = []
    for match in _FC_ATTR.finditer(text):
        statements.append({"name": match.group("name"), "category": match.group("cat"),
                           "formal_proof": bool(_FC_PROOF.search(match.group("attrs")))})
    return statements


def _fc_aggregate(statements: list[dict[str, Any]]) -> dict[str, Any] | None:
    research = [row for row in statements if row["category"] in ("research open", "research solved")]
    if not research:
        return None
    solved = sum(row["category"] == "research solved" for row in research)
    category = ("research solved" if solved == len(research) else
                "research open" if solved == 0 else "partially solved")
    return {"category": category, "statements": len(research), "solved": solved,
            "formal_proofs": sum(row["formal_proof"] for row in research)}


def erdos_file_category(text: str, number: str) -> dict[str, Any] | None:
    """Aggregated research category of the core statements (``erdos_N``, ``erdos_N.parts.*``) of one file."""
    core = [row for row in _fc_statements(text) if re.fullmatch(rf"erdos_{number}(?:\.parts\.[\w.]+)?", row["name"])]
    return _fc_aggregate(core)


def parse_formal_conjectures(files: Mapping[str, str], *, commit: str) -> dict[str, Any]:
    """Erdős, Millennium, Hilbert and Kourovka statement files plus Wikipedia article links."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise SourceError("formal-conjectures needs the full commit SHA")
    base = f"{FC_REPO}/blob/{commit}/"
    erdos, wikipedia, millennium, hilbert, kourovka = {}, {}, {}, {}, {}
    for path, text in sorted(files.items()):
        if not path.endswith(".lean"):
            continue
        url = base + quote(path)
        statements = _fc_statements(text)
        match = re.fullmatch(r"FormalConjectures/ErdosProblems/(\d{1,5})\.lean", path)
        if match:
            number = match.group(1)
            summary = erdos_file_category(text, number) or {"category": None, "statements": 0, "solved": 0, "formal_proofs": 0}
            erdos[number] = {"url": url, **summary}
            continue
        if path.startswith("FormalConjectures/Wikipedia/"):
            header = text.split("namespace", 1)[0][:20000]
            for title in dict.fromkeys(_FC_WIKI.findall(header)):
                wikipedia.setdefault(wikipedia_key(title), url)
            continue
        match = re.fullmatch(r"FormalConjectures/Millennium/(\w+)\.lean", path)
        if match:
            millennium[match.group(1)] = {"url": url, **(_fc_aggregate(statements) or {"category": None})}
            continue
        match = re.fullmatch(r"FormalConjectures/HilbertProblems/\D*(\d{1,2})\w*\.lean", path)
        if match:
            hilbert[str(int(match.group(1)))] = {"url": url, **(_fc_aggregate(statements) or {"category": None})}
            continue
        match = re.fullmatch(r"FormalConjectures/Kourovka/(\d{1,2})_(\d{1,3})[a-z]?\.lean", path)
        if match:
            kourovka.setdefault(f"{int(match.group(1))}.{int(match.group(2))}",
                                {"url": url, **(_fc_aggregate(statements) or {"category": None})})
    return {"erdos": dict(sorted(erdos.items(), key=lambda item: int(item[0]))), "wikipedia": dict(sorted(wikipedia.items())),
            "millennium": dict(sorted(millennium.items())), "hilbert": dict(sorted(hilbert.items(), key=lambda item: int(item[0]))),
            "kourovka": dict(sorted(kourovka.items()))}


def formal_conjectures_fact_file(files: Mapping[str, str], *, commit: str, committed_at: str | None,
                                 fetched_at: str, previous: Mapping | None = None) -> dict:
    parsed = parse_formal_conjectures(files, commit=commit)
    check_shrink(len(parsed["erdos"]), len((previous or {}).get("erdos") or {}), what="formal-conjectures Erdős files")
    return {"schema_version": 1, "source": "google-deepmind/formal-conjectures", "url": FC_REPO,
            "revision": commit, "committed_at": committed_at, "fetched_at": fetched_at, "license": "Apache-2.0",
            "category_meaning": {"research open": "open research problem",
                                 "research solved": "an informal solution is widely accepted; no formal proof required",
                                 "partially solved": "some but not all core statements are research solved"},
            "counts": {key: len(value) for key, value in parsed.items()}, **parsed}


# ── openai/math ───────────────────────────────────────────────────────────────

_OM_FAMILY = re.compile(r"^\*\*(\d{3})\.\s+(.+?)\.\*\*")
# Directory names may contain parentheses ("...-CAT(0)-spaces-..."); the file
# name after the last slash does not.  A link may carry a trailing remark
# ("— secondary writeup").
_OM_MANUSCRIPT = re.compile(r"^(?:&emsp;)?\[([^\]]+)\]\(preprints/([^/\s]+)/[^/\s()]*\)(?=\s|$)")
_OM_DATE = re.compile(r"-(January|February|March|April|May|June|July|August|September|October|November|December)-(\d{1,2})-(\d{4})$")
_LONG_MONTHS = {name: index for index, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}


def _plain_markdown(value: str, limit: int) -> str:
    text = re.sub(r"<[^>]+>", "", value)
    text = re.sub(r"\$`([^`]*)`\$", r"\1", text)
    return clean_text(text.replace("&emsp;", " "), limit)


def manuscript_date(directory: str) -> str | None:
    match = _OM_DATE.search(directory)
    if not match:
        return None
    return f"{int(match.group(3)):04d}-{_LONG_MONTHS[match.group(1)]:02d}-{int(match.group(2)):02d}"


def parse_openai_contents(markdown: str) -> dict[str, dict[str, Any]]:
    families: dict[str, dict[str, Any]] = {}
    current = None
    for raw in markdown.splitlines():
        line = raw.strip()
        family = _OM_FAMILY.match(line)
        if family:
            current = {"title": _plain_markdown(family.group(2), 200), "lean_doc": f"(lean/docs/{family.group(1)}.md)" in line,
                       "manuscripts": []}
            if family.group(1) in families:
                raise SourceError("openai/math contents list a family twice")
            families[family.group(1)] = current
            continue
        manuscript = _OM_MANUSCRIPT.match(line)
        if manuscript and current is not None:
            directory = manuscript.group(2)
            if not re.fullmatch(r"[A-Za-z0-9._()\-]{1,200}", directory):
                raise SourceError("openai/math manuscript directory is invalid")
            current["manuscripts"].append({"dir": directory, "title": _plain_markdown(manuscript.group(1), 240),
                                           "date": manuscript_date(directory)})
    if len(families) < 10:
        raise SourceError("openai/math contents were not recognized")
    return dict(sorted(families.items()))


def parse_openai_history(markdown: str) -> list[dict[str, Any]]:
    """Dated sections of ``history.md`` with the titles listed under Withdrawals."""
    sections: list[dict[str, Any]] = []
    current = None
    block = None
    for raw in markdown.splitlines():
        line = raw.strip()
        heading = re.match(r"^##\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})\s*$", line)
        if heading:
            current = {"date": f"{int(heading.group(3)):04d}-{_LONG_MONTHS[heading.group(1)]:02d}-{int(heading.group(2)):02d}",
                       "withdrawn_titles": [], "subsections": []}
            sections.append(current)
            block = None
            continue
        if current is None:
            continue
        sub = re.match(r"^\*\*([^*]{2,60})\*\*$", line)
        if sub:
            block = sub.group(1).strip().lower()
            current["subsections"].append(sub.group(1).strip())
            continue
        if block == "withdrawals" and line.startswith("- "):
            current["withdrawn_titles"].append(clean_text(line[2:], 240))
        numbers = re.findall(r"\b(\d{1,4})\s*/\s*(\d{1,4})\b", line)
        if numbers:
            current.setdefault("ratios", []).extend(f"{a}/{b}" for a, b in numbers)
    return sections


def _title_key(title: str) -> str:
    text = unquote(title).lower().replace("–", "-").replace("—", "-")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def openai_math_fact_file(contents_md: str, history_md: str, *, commit: str, fetched_at: str,
                          previous_contents_md: str | None = None, previous_commit: str | None = None,
                          mapping: list[dict[str, Any]] | None = None, previous: Mapping | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise SourceError("openai/math needs the full commit SHA")
    families = parse_openai_contents(contents_md)
    history = parse_openai_history(history_md)
    current_dirs = {item["dir"] for family in families.values() for item in family["manuscripts"]}
    withdrawn: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    unmatched: list[dict[str, str]] = []
    if previous_contents_md:
        before = parse_openai_contents(previous_contents_md)
        listed = [(title, section["date"]) for section in history for title in section["withdrawn_titles"]]
        titles = {_title_key(title): date for title, date in listed}
        for family_id, family in before.items():
            for item in family["manuscripts"]:
                if item["dir"] in current_dirs:
                    continue
                record = {"family": family_id, "dir": item["dir"], "title": item["title"], "date": item["date"]}
                if _title_key(item["title"]) in titles:
                    withdrawn.append(record | {"withdrawn_on": titles[_title_key(item["title"])],
                                               "archived_url": f"{OPENAI_REPO}/tree/{previous_commit}/preprints/{item['dir']}" if previous_commit else None})
                else:
                    superseded.append(record)
        matched = {_title_key(item["title"]) for item in withdrawn}
        earlier = {_title_key(row["title"]) for row in (previous or {}).get("withdrawn", [])}
        # A withdrawal of a manuscript added after the previous revision, or a
        # history bullet worded differently from its contents link, matches no
        # removed manuscript.  It is recorded for review; the matched
        # withdrawals still apply.
        unmatched = [{"title": clean_text(title, 240), "withdrawn_on": date} for title, date in listed
                     if _title_key(title) not in matched and _title_key(title) not in earlier]
    # Withdrawals are permanent facts: keep the ones found against older revisions.
    known = {item["dir"] for item in withdrawn}
    withdrawn += [dict(row) for row in (previous or {}).get("withdrawn", []) if row["dir"] not in known]
    check_shrink(len(families), len((previous or {}).get("families") or {}), what="openai/math families")
    return {
        "schema_version": 1, "source": "openai/math", "url": OPENAI_REPO, "revision": commit,
        "previous_revision": previous_commit, "fetched_at": fetched_at, "license": "Apache-2.0",
        "counts": {"families": len(families),
                   "manuscripts": sum(len(family["manuscripts"]) for family in families.values()),
                   "manuscript_dirs": len(current_dirs),
                   "lean_docs": sum(family["lean_doc"] for family in families.values()),
                   "withdrawn": len(withdrawn), "superseded": len(superseded),
                   "unmatched_withdrawals": len(unmatched)},
        "history": history, "withdrawn": withdrawn, "superseded": superseded,
        "unmatched_withdrawals": unmatched, "mapping": mapping or [], "families": families,
    }


def openai_manuscript_dir(url: str) -> str | None:
    """The manuscript directory an evidence URL into openai/math points at, if any."""
    match = re.match(r"^https://(?:github\.com/openai/math/(?:blob|tree)|raw\.githubusercontent\.com/openai/math)"
                     r"/[^/]+/preprints/([^/?#]+)", url or "")
    return match.group(1) if match else None


# ── Wikipedia (list pinned to one revision) ───────────────────────────────────

_QUESTION = re.compile(r"^(?:is|are|does|do|can|could|what|which|how|given|find|determine|prove|show|suppose|in|for|must|many|let|if|whether|when|there)\b", re.I)


def wikipedia_name(name: object) -> str | None:
    """A short entry name (a fact) or None when the bullet text is a sentence."""
    if not isinstance(name, str):
        return None
    text = clean_text(name, 200)
    if not text or len(text) > 90 or text.endswith("?") or "$" in text or _QUESTION.match(text):
        return None
    return text


def slugify(value: str, limit: int = 60) -> str:
    import unicodedata

    text = re.sub(r"[‐-―−]", "-", value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > limit:
        text = text[:limit].rsplit("-", 1)[0] or text[:limit]
    return text.strip("-")


def _article(link: Mapping | None) -> dict[str, Any]:
    link = link or {}
    url = link.get("resolved_url") or link.get("url")
    title = link.get("resolved_title") or link.get("title")
    if link.get("redirect_fragment"):
        url = f"{url}#{quote(str(link['redirect_fragment']).replace(' ', '_'))}"
    if isinstance(url, str):
        parts = urlsplit(url.replace(" ", "_"))
        url = urlunsplit((parts.scheme, parts.netloc, quote(unquote(parts.path), safe="/:_-.,()'!*~"),
                          parts.query, quote(unquote(parts.fragment), safe="_-.:")))
    return {"article_title": clean_text(title, 200) or None,
            "article_url": safe_url(url) if isinstance(url, str) else None,
            "article_revid": link.get("article_revid") if isinstance(link.get("article_revid"), int) else None}


def _assign_slugs(rows: list[dict[str, Any]], prefix: str) -> None:
    seen: dict[str, int] = {}
    for row in rows:
        base = slugify(row.get("name") or row.get("article_title") or row.get("slug_hint") or row["wp_id"]) or row["wp_id"]
        count = seen.get(base, 0) + 1
        seen[base] = count
        row["slug"] = f"{prefix}{base}" if count == 1 else f"{prefix}{base}-{count}"
        row.pop("slug_hint", None)


def wikipedia_fact_file(unsolved: Mapping, solved: Mapping, *, fetched_at: str) -> dict:
    """Entries of the pinned list revision: ids, names (when they are names), articles, revids."""
    source = unsolved.get("source") or {}
    revid = source.get("revision_id")
    if not isinstance(revid, int):
        raise SourceError("Wikipedia list needs a pinned revision id")
    entries = []
    for row in unsolved.get("entries") or []:
        if not re.fullmatch(r"wpu-\d{3}", str(row.get("id", ""))):
            raise SourceError("Wikipedia entry has an invalid id")
        entry = {"wp_id": row["id"], "name": wikipedia_name(row.get("name")), "field": row.get("field"),
                 "subfield": row.get("subfield"), "parent_id": row.get("parent_id"),
                 "group_header": bool(row.get("is_group_header")), "duplicate_of": row.get("duplicate_of"),
                 **_article(row.get("primary_link"))}
        if not entry["name"]:
            entry["slug_hint"] = " ".join(str(row.get("name") or "").split()[:8])
        entries.append({key: value for key, value in entry.items() if value not in (None, False)} | {"wp_id": row["id"]})
    _assign_slugs(entries, "wiki-")
    solved_rows = []
    for row in solved.get("entries") or []:
        if not re.fullmatch(r"wps-\d{3}", str(row.get("id", ""))):
            raise SourceError("Wikipedia solved entry has an invalid id")
        entry = {"wp_id": row["id"], "name": wikipedia_name(row.get("name")), "field": row.get("field"),
                 "year": row.get("year") if isinstance(row.get("year"), int) else None,
                 "outcome": clean_text(row.get("outcome"), 40) or None, "ai_involved": bool(row.get("ai_involved")),
                 **_article(row.get("primary_link"))}
        if not entry["name"]:
            entry["slug_hint"] = " ".join(str(row.get("name") or "").split()[:8])
        solved_rows.append({key: value for key, value in entry.items() if value not in (None, False)} | {"wp_id": row["id"]})
    _assign_slugs(solved_rows, "solved-")
    if len(entries) < 100:
        raise SourceError("Wikipedia list parse is unexpectedly small")
    return {
        "schema_version": 1, "source": "Wikipedia", "page": source.get("page"),
        "url": source.get("url"), "revision_id": revid, "revision_timestamp": source.get("revision_timestamp"),
        "permalink": source.get("permalink"), "fetched_at": fetched_at,
        "license": "CC BY-SA 4.0 page text is not stored: names, links and revision ids only",
        "counts": {"entries": len(entries), "group_headers": sum(1 for row in entries if row.get("group_header")),
                   "solved": len(solved_rows)},
        "entries": entries, "solved": solved_rows,
    }


WIKIPEDIA_INFO_QUERY = {"action": "query", "format": "json", "formatversion": "2", "prop": "info", "redirects": "1"}


def title_query_batches(titles: list[str], *, limit: int = 50, max_length: int = MAX_REQUEST_URL) -> list[tuple[list[str], str]]:
    """MediaWiki revision queries for ``titles``: (titles, url), at most ``limit`` titles and ``max_length`` characters each."""
    def url(items: list[str]) -> str:
        return f"{WIKIPEDIA_API}?{urlencode({**WIKIPEDIA_INFO_QUERY, 'titles': '|'.join(items)})}"

    batches: list[tuple[list[str], str]] = []
    batch: list[str] = []
    for title in titles:
        if batch and (len(batch) == limit or len(url([*batch, title])) > max_length):
            batches.append((batch, url(batch)))
            batch = []
        batch.append(title)
    if batch:
        batches.append((batch, url(batch)))
    return batches


def wikipedia_revids(fetcher, url: str, titles: list[str]) -> dict[str, int]:
    """Current revision ids from one revision query; an API error reply is a source error, not "no pages"."""
    try:
        data = json.loads(fetch_ok(fetcher, url, allowed_hosts=("en.wikipedia.org",), max_bytes=2 * 1024 * 1024,
                                   accept="application/json"))
    except ValueError as error:
        if isinstance(error, SourceError):
            raise
        raise SourceError("Wikipedia returned invalid JSON") from None
    query = data.get("query") if isinstance(data, dict) else None
    if not isinstance(query, dict) or data.get("error") or not isinstance(query.get("pages"), list):
        raise SourceError("The Wikipedia API answered with an error")
    redirects = {row.get("from"): row.get("to") for row in query.get("redirects") or [] if isinstance(row, dict)}
    pages = {row.get("title"): row.get("lastrevid") for row in query["pages"] if isinstance(row, dict)}
    current = {}
    for title in titles:
        revid = pages.get(redirects.get(title, title))
        if isinstance(revid, int):
            current[title] = revid
    return current


def refresh_article_revids(fact: dict, fetcher, *, sleeper=time.sleep, batch: int = 50) -> dict[str, int]:
    """Current revision ids of the linked articles (one API call per batch of up to 50 titles)."""
    titles = sorted({row["article_title"] for row in [*fact["entries"], *fact["solved"]] if row.get("article_title")})
    current: dict[str, int] = {}
    for chunk, url in title_query_batches(titles, limit=batch):
        current.update(wikipedia_revids(fetcher, url, chunk))
        sleeper(1.0)
    return current


# ── Clay Mathematics Institute ────────────────────────────────────────────────

_CLAY_SECTIONS = {"active problems": "active", "unsolved problems": "unsolved", "solved problems": "solved"}


def parse_clay_listing(html: str) -> dict[str, list[str]]:
    """Problem slugs under the Active / Unsolved / Solved headings of the Millennium page."""
    headings = [(match.start(), re.sub(r"<[^>]+>", "", match.group(1)).strip().lower())
                for match in re.finditer(r"<h[1-4][^>]*>(.*?)</h[1-4]>", html, re.S)]
    marks = [(position, _CLAY_SECTIONS[text]) for position, text in headings if text in _CLAY_SECTIONS]
    if not any(name == "solved" for _, name in marks) or not any(name in ("unsolved", "active") for _, name in marks):
        raise SourceError("The Clay Millennium page layout changed")
    sections: dict[str, list[str]] = {"active": [], "unsolved": [], "solved": []}
    for index, (position, name) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else position + 6000
        for slug in re.findall(r'href="https://www\.claymath\.org/millennium/([a-z0-9-]+)/"', html[position:end]):
            if slug not in sections[name]:
                sections[name].append(slug)
    listed = [slug for values in sections.values() for slug in values]
    if len(listed) != len(set(listed)) or len(listed) != 7:
        raise SourceError("The Clay Millennium page no longer lists seven problems once each")
    return sections


def clay_fact_file(html: str, *, checked_at: str) -> dict:
    return {"schema_version": 1, "source": "Clay Mathematics Institute", "url": CLAY_PAGE,
            "sha256": sha256_bytes(html.encode("utf-8")), "checked_at": checked_at,
            "sections": parse_clay_listing(html)}


# ── Palomar registry (CC0) ────────────────────────────────────────────────────


def parse_palomar_results(body: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise SourceError("Palomar response is not JSON") from None
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("entries"), list):
        raise SourceError("Palomar response schema is unsupported")
    results = []
    for entry in data["entries"][:100]:
        if not isinstance(entry, dict) or not re.fullmatch(r"PALOMAR-\d{4}-\d{2}-\d{2}-\d{6}", str(entry.get("id", ""))):
            continue
        version = entry.get("version") if isinstance(entry.get("version"), int) else 1
        theorem_names = ((entry.get("formalization") or {}).get("theorem_names") or [])[:10]
        results.append({"id": entry["id"], "version": version, "title": clean_text(entry.get("title"), 300),
                        "abstract": clean_text(entry.get("abstract"), 1800),
                        "published_at": str(entry.get("published_at") or "")[:20],
                        "status": clean_text(entry.get("status"), 40),
                        "theorem_names": [clean_text(name, 120) for name in theorem_names if isinstance(name, str)],
                        "url": PALOMAR_ENTRY.format(id=entry["id"], version=version)})
    return results


def load_json(path) -> dict:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SourceError("Fact file is not a JSON object")
    return value


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
