"""Manuscript sources for Analyze: links, pasted text and uploads.

Connectors (every network read goes through :mod:`agent_monitor.safe_fetch`):

* **arXiv** — ``arxiv.org/abs|pdf|e-print|src/<id>[vN]`` and bare ids
  (``2510.01234v2``, ``math/0601001``).  Metadata from the arXiv API, source
  from ``/e-print/<id>vN`` (version pinned; a gzipped tar, a gzipped single
  ``.tex`` or a PDF), arXiv requests starting at least 3 s apart (across all
  of the server's processes once ``ARXIV_SLOT_PATH`` is set).
* **GitHub** — ``blob``/``tree``/``raw`` links and ``raw.githubusercontent.com``.
  Refs resolve to a commit SHA through the smart-HTTP ref advertisement (no
  REST API call).  A ``.tex`` file brings its ``\\input`` closure (fetched raw);
  a folder needs one ``git/trees`` API call (an optional operator token is sent
  with that call only).  Every path segment is validated after decoding (no
  dot segment, no encoded separator) and re-encoded on its own; links to
  GitHub's API and download hosts are refused.
* **openai/math** — the repository root lists the manuscripts (``choices``
  from ``CONTENTS.md``); a manuscript folder brings its ``build/`` TeX
  closure, README version notes and withdrawal notice (a withdrawn
  manuscript is analyzed at its archived revision), and upstream Lean pointers
  (``lean/docs/<family>.md``, Comparator statements) for display only.
* **HTTPS** (members and admins) — a ``.pdf``, ``.tex``, ``.zip`` or ``.tar.gz``
  link; web pages are refused.
* **paste** and **upload** — text written by the console; uploads may be
  archives (expanded by :mod:`agent_monitor.analyze.archives`).

Files are stored under ``analysis/source``: never compiled or executed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, unquote, urlsplit

from agent_monitor import safe_fetch
from agent_monitor.analyze import archives
from agent_monitor.analyze.titles import plain_title
from agent_monitor.safe_fetch import Budget, FetchError, SafeFetcher

ARXIV_HOSTS = ("arxiv.org", "export.arxiv.org", "www.arxiv.org")
GITHUB_HOSTS = ("github.com", "www.github.com", "raw.githubusercontent.com", "api.github.com")
# GitHub service hosts are never fetched as a user's link (their requests are
# the connector's own, and archive links there carry signed tokens).
GITHUB_SERVICE_HOSTS = ("api.github.com", "codeload.github.com", "uploads.github.com", "objects.githubusercontent.com")
RAW_HOST = "raw.githubusercontent.com"
OPENAI_MATH = ("openai", "math")
ARXIV_API = "https://export.arxiv.org/api/query?id_list={id}"
ARXIV_OAI = "https://export.arxiv.org/oai2?verb=GetRecord&identifier=oai:arXiv.org:{id}&metadataPrefix=arXiv"
ARXIV_EPRINT = "https://export.arxiv.org/e-print/{id}"
ARXIV_PDF = "https://export.arxiv.org/pdf/{id}"
ARXIV_POLITENESS_SECONDS = 3.0
ARXIV_LOCK_SECONDS = 30.0
# The next free arXiv request slot, shared by every process of the server
# (the console's link previews and each Analyze runner) through a small
# flock'd file: "auto" puts it in $AGENT_MONITOR_DATA_DIR/cache (the runner's
# command line and the console set this), None keeps the spacing per process.
ARXIV_SLOT_PATH: "Path | str | None" = None
RESOLVE_SECONDS = 10.0
MAX_GITHUB_FILES = 200
MAX_INCLUDE_FETCHES = 120
MAX_PASTE_CHARS = 400_000
GUEST_PASTE_CHARS = 20_000
TEXT_SUFFIXES = (".tex", ".bib", ".bbl", ".sty", ".cls", ".md", ".txt", ".lean")
GUEST_CONNECTORS = frozenset({"arxiv", "github", "openai_math"})
ROLES = ("guest", "member", "admin")

_NEW_ARXIV = r"(?P<nid>\d{4}\.\d{4,5})(?P<nver>v\d{1,3})?"
_OLD_ARXIV = r"(?P<oid>[a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})(?P<over>v\d{1,3})?"
_BARE_ARXIV_RE = re.compile(rf"^(?:arxiv:)?\s*(?:{_NEW_ARXIV}|{_OLD_ARXIV})$", re.I)
_ARXIV_PATH_RE = re.compile(rf"^/(?:abs|pdf|e-print|src|format)/(?:{_NEW_ARXIV}|{_OLD_ARXIV})(?:\.pdf)?/?$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHORT_SHA_RE = re.compile(r"^[0-9a-f]{7,39}$")
_GITHUB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_README_TITLE_RE = re.compile(r"^#\s*\[(?P<title>.*?)\]\((?P<pdf>[^)\s]*)\)", re.M)
_README_PLAIN_TITLE_RE = re.compile(r"^#\s+(?P<title>.+)$", re.M)
_WITHDRAWN_RE = re.compile(r"\*\*Withdrawn on (?P<date>[A-Za-z]+ \d{1,2}, \d{4})\.\*\*")
_REVISION_RE = re.compile(r"Repository revision:\s*`(?P<sha>[0-9a-f]{40})`|/blob/(?P<blob>[0-9a-f]{40})/")
_VERSION_NOTE_RE = re.compile(r"^## Version note\s*\n(?P<body>.*?)(?=^## |\Z)", re.M | re.S)
_PREVIOUS_RE = re.compile(r"\]\(\.\./(?P<dir>[^/)]+)/[^)]*\)")
_DATE_RE = re.compile(r"\*\*Date:\*\*\s*(?P<date>[A-Za-z]+ \d{1,2}, \d{4})")
_FAMILY_RE = re.compile(r"^\*\*(?P<number>\d{3})\.\s*(?P<title>.+?)\*\*(?P<rest>.*)$", re.M)
_MANUSCRIPT_RE = re.compile(r"^&emsp;\[(?P<title>[^\]]+)\]\(preprints/(?P<dir>[^/)]+)/(?P<pdf>[^)]*)\)", re.M)
_LEAN_LINK_RE = re.compile(r"\(\[Lean\]\((?P<path>lean/docs/\d{3}\.md)\)\)")
_COMPARATOR_RE = re.compile(r"\]\(\.\./ComparatorChallenges/(?P<file>[A-Za-z0-9_.-]+\.lean)\)")
_INCLUDE_RE = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")
_BARE_INPUT_RE = re.compile(r"\\input\s+([A-Za-z0-9_./-]+)")
_IMPORT_RE = re.compile(r"\\(?:import|subimport|inputfrom|subinputfrom)\s*\{([^}]*)\}\s*\{([^}]+)\}")
_BIB_RE = re.compile(r"\\(?:bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_PACKAGE_RE = re.compile(r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_STANDARD_PACKAGES = frozenset(
    "amsmath amssymb amsthm amsfonts mathtools hyperref cleveref graphicx xcolor geometry enumitem "
    "tikz tikz-cd pgfplots booktabs microtype inputenc fontenc lmodern babel url natbib biblatex "
    "thmtools mathrsfs bm bbm dsfont stmaryrd caption subcaption float array tabularx multirow "
    "fullpage authblk xspace comment verbatim listings algorithm algorithmic algpseudocode "
    "fancyhdr setspace parskip titlesec etoolbox xparse ifthen calc aliascnt amscd mathabx "
    "eucal upgreek tensor physics siunitx csquotes footmisc todonotes appendix color soul "
    "nicefrac dsfont accents relsize scalerel longtable hhline colortbl wrapfig cite mleftright "
    "orcidlink doi cancel centernot extarrows faktor yhmath esint marvosym latexsym amsxtra "
    "dutchcal mathptmx times palatino newtxtext newtxmath libertine txfonts pxfonts"
    .split()
)
_ARXIV_LOCK = threading.Lock()
_ARXIV_LAST = [0.0]  # this process's last reserved arXiv request start (its clock)
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"
_LICENSES = {
    "http://creativecommons.org/licenses/by/4.0/": "CC BY 4.0",
    "http://creativecommons.org/licenses/by-sa/4.0/": "CC BY-SA 4.0",
    "http://creativecommons.org/licenses/by-nc-sa/4.0/": "CC BY-NC-SA 4.0",
    "http://creativecommons.org/licenses/by-nc-nd/4.0/": "CC BY-NC-ND 4.0",
    "http://creativecommons.org/publicdomain/zero/1.0/": "CC0 1.0",
    "http://arxiv.org/licenses/nonexclusive-distrib/1.0/": "arXiv non-exclusive distribution licence",
    "http://creativecommons.org/licenses/by/3.0/": "CC BY 3.0",
    "http://creativecommons.org/licenses/by-nc-sa/3.0/": "CC BY-NC-SA 3.0",
    "http://creativecommons.org/licenses/publicdomain/": "Public domain",
}


class SourceError(ValueError):
    """A public-safe source problem; ``kind`` is ``unsupported``, ``forbidden``,
    ``not_found``, ``unavailable`` or ``invalid``."""

    def __init__(self, message: str, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# classify (no network)
# ---------------------------------------------------------------------------


def _arxiv_id(match: re.Match) -> tuple[str, str]:
    if match.group("nid"):
        return match.group("nid"), match.group("nver") or ""
    return match.group("oid"), match.group("over") or ""


def classify(value: object) -> dict[str, Any]:
    """Recognize a link without any network use.

    Returns ``{"ok", "connector", "canonical_url", "kind", ...}`` (``error``
    when ``ok`` is false).  ``connector`` is ``arxiv``, ``github``,
    ``openai_math`` or ``https``.
    """
    try:
        return _classify(value)
    except (SourceError, FetchError) as exc:
        return {"ok": False, "connector": None, "canonical_url": None, "error": str(exc)}


def _classify(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise SourceError("Paste a link to analyze")
    text = value.strip()
    if len(text) > safe_fetch.MAX_URL_CHARS:
        raise SourceError("The link is too long")
    bare = _BARE_ARXIV_RE.match(text)
    if bare:
        identifier, version = _arxiv_id(bare)
        return _arxiv_classified(identifier, version)
    if "://" not in text and re.match(r"^(?:www\.)?(?:arxiv\.org|github\.com|raw\.githubusercontent\.com)/", text, re.I):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        raise SourceError("The link is not a valid URL") from None
    try:
        host = (parts.hostname or "").lower()
        credentials = parts.username is not None or parts.password is not None
    except ValueError:
        raise SourceError("The link is not a valid URL") from None
    if credentials or "@" in parts.netloc:
        raise SourceError("Links with embedded credentials are not allowed")
    scheme = parts.scheme.lower()
    if scheme == "http" and host in ARXIV_HOSTS + GITHUB_HOSTS:
        scheme = "https"
    if scheme != "https":
        raise SourceError("Only https:// links are supported")
    if host in ARXIV_HOSTS:
        match = _ARXIV_PATH_RE.match(parts.path)
        if not match:
            raise SourceError("That arXiv link does not name a paper (use an abs, pdf or e-print link)")
        identifier, version = _arxiv_id(match)
        return _arxiv_classified(identifier, version)
    if host in ("github.com", "www.github.com", RAW_HOST):
        return _github_classified(host, parts.path)
    if host in GITHUB_SERVICE_HOSTS:
        raise SourceError("Link the file or folder on github.com instead of a GitHub API or download address")
    # Any other public HTTPS link (members): validated without DNS here.
    canonical = safe_fetch.public_https_url(text)
    return {"ok": True, "connector": "https", "canonical_url": canonical, "kind": "file"}


def _arxiv_classified(identifier: str, version: str) -> dict[str, Any]:
    identifier = identifier.strip()
    return {
        "ok": True,
        "connector": "arxiv",
        "canonical_url": f"https://arxiv.org/abs/{identifier}{version}",
        "kind": "paper",
        "arxiv_id": identifier,
        "version": version or None,
    }


def _github_part_ok(part: str) -> bool:
    """One decoded path segment: no separator (an encoded "/" or "\\"), no
    dot segment, no control character."""
    return bool(part) and "/" not in part and "\\" not in part and part not in (".", "..") and not any(
        ord(char) < 32 or ord(char) == 127 for char in part)


def _github_classified(host: str, path: str) -> dict[str, Any]:
    segments = [unquote(part) for part in path.split("/") if part]
    if not all(_github_part_ok(part) for part in segments):
        raise SourceError("That GitHub link has an invalid path")
    if host == RAW_HOST:
        if len(segments) < 4:
            raise SourceError("That raw GitHub link does not name a file")
        owner, repo = segments[0], segments[1]
        rest = segments[2:]
        if rest[:2] == ["refs", "heads"] and len(rest) > 3:
            rest = rest[2:]
        view, ref_and_path = "raw", rest
    else:
        if len(segments) < 2:
            raise SourceError("That GitHub link does not name a repository")
        owner, repo = segments[0], segments[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        if len(segments) == 2:
            view, ref_and_path = "root", []
        elif segments[2] in ("blob", "tree", "raw") and len(segments) >= 4:
            view, ref_and_path = segments[2], segments[3:]
        else:
            raise SourceError("Use a GitHub link to a repository, folder (tree) or file (blob)")
    if not (_GITHUB_NAME_RE.fullmatch(owner) and _GITHUB_NAME_RE.fullmatch(repo)) or set(owner) <= {"."} or set(
            repo) <= {"."}:
        raise SourceError("That GitHub link has an invalid owner or repository name")
    connector = "openai_math" if (owner.lower(), repo.lower()) == OPENAI_MATH else "github"
    kind = {"root": "repo", "tree": "tree", "blob": "file", "raw": "file"}[view]
    canonical = f"https://github.com/{owner}/{repo}"
    if ref_and_path:
        canonical += f"/{'tree' if kind == 'tree' else 'blob'}/" + "/".join(quote(part) for part in ref_and_path)
    result = {
        "ok": True,
        "connector": connector,
        "canonical_url": canonical,
        "kind": kind,
        "owner": owner,
        "repo": repo,
        "ref_and_path": "/".join(ref_and_path),
    }
    if connector == "openai_math":
        result["kind"] = _openai_kind(kind, ref_and_path)
    return result


def _openai_kind(kind: str, ref_and_path: list[str]) -> str:
    if kind == "repo":
        return "root"
    joined = "/".join(ref_and_path)
    if re.search(r"(?:^|/)preprints/?$", joined):
        return "preprints"
    if "/preprints/" in f"/{joined}":
        after = joined.split("preprints/", 1)[1]
        return "folder" if kind == "tree" and "/" not in after.strip("/") else ("file" if kind == "file" else "folder-path")
    return kind


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class ResolvedSource:
    """What :func:`resolve` learns about a link without downloading the
    manuscript.  ``size_bytes`` is optional and stays ``None`` (no connector
    knows the size before the download; show no size rather than a guess).
    ``format`` is the expected format: ``"tex"`` for arXiv means "e-print
    expected" (a PDF-only paper is found out when the source is fetched,
    and guests are told then); ``""`` when unknown."""

    ok: bool
    connector: str | None
    canonical_url: str | None = None
    canonical_id: str | None = None
    title: str = ""
    version: str = ""
    license: str = ""
    format: str = ""
    size_bytes: int | None = None
    warnings: list[str] = field(default_factory=list)
    choices: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "connector": self.connector,
            "canonical_url": self.canonical_url,
            "canonical_id": self.canonical_id,
            "title": self.title,
            "version": self.version,
            "license": self.license,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "warnings": list(self.warnings),
        }
        if self.choices:
            out["choices"] = list(self.choices)
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Bundle:
    """A fetched or pasted manuscript bundle under ``root``."""

    root: Path
    source_kind: str
    connector: str
    input_url: str = ""
    resolved_url: str = ""
    canonical_id: str = ""
    title: str = ""
    version: str = ""
    license: str = ""
    warnings: list[str] = field(default_factory=list)
    upstream: dict[str, Any] = field(default_factory=dict)
    skipped: list[dict[str, str]] = field(default_factory=list)
    computational: list[str] = field(default_factory=list)
    readme_excluded: bool = False
    # ``https://github.com/<o>/<r>/blob/<sha>/<dir>`` when the bundle's files
    # mirror one repository folder (line permalinks in the report and graph).
    file_base_url: str = ""

    def files(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            relative = path.relative_to(self.root).as_posix()
            out.append({
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "role": _file_role(relative),
            })
        return out

    def manuscript(self, *, root_file: str | None = None, fmt: str | None = None) -> dict[str, Any]:
        files = self.files()
        digest = hashlib.sha256(
            json.dumps([[item["path"], item["sha256"]] for item in files], separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "schema": "ansatze-analyze-manuscript-v1",
            "source_kind": self.source_kind,
            "connector": self.connector,
            "input_url": self.input_url,
            "resolved_url": self.resolved_url,
            "canonical_id": self.canonical_id,
            "title": self.title,
            "version": self.version,
            "license": self.license,
            "format": fmt or bundle_format(files),
            "root_file": root_file,
            "files": files,
            "skipped": self.skipped[:200],
            "computational": self.computational[:200],
            "warnings": self.warnings,
            "bundle_sha256": digest,
            "upstream": self.upstream,
        }


def _file_role(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return {".tex": "tex", ".bib": "bib", ".bbl": "bib", ".pdf": "pdf", ".md": "md", ".txt": "txt"}.get(suffix, "other")


def bundle_format(files: Iterable[Mapping[str, Any]]) -> str:
    roles = {item["role"] for item in files}
    for role in ("tex", "md", "pdf", "txt"):
        if role in roles:
            return role
    return "unknown"


# ---------------------------------------------------------------------------
# shared fetch helpers
# ---------------------------------------------------------------------------


class _Context:
    """Fetch state for one resolve or bundle: fetcher, budget, politeness."""

    def __init__(self, fetcher: SafeFetcher | None, budget: Budget | None, sleep: Callable[[float], None],
                 clock: Callable[[], float]) -> None:
        self.fetcher = fetcher or SafeFetcher()
        self.budget = budget
        self.sleep = sleep
        self.clock = clock
        self.api_calls = 0

    def get(self, url: str, **kwargs: Any) -> safe_fetch.FetchResult:
        return self.fetcher.get(url, budget=self.budget, **kwargs)

    def arxiv(self, url: str, **kwargs: Any) -> safe_fetch.FetchResult:
        """An arXiv request in its own reserved slot (requests start at least
        3 s apart).  The slot is reserved under a short lock and the request
        runs outside it, so a slow arXiv answer never blocks other callers."""
        wait = _reserve_arxiv_slot(self.clock, self.budget)
        if wait > 0:
            self.sleep(wait)
        return self.get(url, allowed_hosts=ARXIV_HOSTS, **kwargs)


_PAUSE = "arXiv asks for a pause between requests; try again shortly"


def _reserve_arxiv_slot(clock: Callable[[], float], budget: Budget | None) -> float:
    """Seconds to wait before the next arXiv request, whose slot is now
    reserved; raises :class:`SourceError` when the wait does not fit the
    budget (the slot is then not taken)."""
    limit = ARXIV_LOCK_SECONDS if budget is None else min(ARXIV_LOCK_SECONDS, budget.remaining_seconds())
    if limit <= 0 or not _ARXIV_LOCK.acquire(timeout=limit):
        raise SourceError(_PAUSE, "unavailable")
    try:
        now = clock()
        wait = max(0.0, _ARXIV_LAST[0] + ARXIV_POLITENESS_SECONDS - now)
        wait = _shared_arxiv_wait(wait, budget)
        if budget is not None and wait > budget.remaining_seconds():
            raise SourceError(_PAUSE, "unavailable")
        _ARXIV_LAST[0] = now + wait
        return wait
    finally:
        _ARXIV_LOCK.release()


def _arxiv_slot_file() -> Path | None:
    if ARXIV_SLOT_PATH is None:
        return None
    if ARXIV_SLOT_PATH != "auto":
        return Path(ARXIV_SLOT_PATH)
    data = os.environ.get("AGENT_MONITOR_DATA_DIR")
    return Path(data) / "cache" / "analyze-arxiv-slot" if data else None


def _shared_arxiv_wait(wait: float, budget: Budget | None) -> float:
    """``wait`` lengthened to the server-wide next free slot (wall clock),
    which it then reserves; best effort: any file problem keeps ``wait``."""
    path = _arxiv_slot_file()
    if path is None:
        return wait
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return wait
    try:
        give_up = time.monotonic() + 2.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > give_up:
                    return wait
                time.sleep(0.01)
        try:
            next_free = float(os.pread(descriptor, 64, 0).decode("ascii", "replace").strip() or 0)
        except ValueError:
            next_free = 0.0
        now = time.time()
        if next_free - now > 3600:
            next_free = 0.0  # a clock change or a damaged file
        wait = max(wait, next_free - now)
        if budget is not None and wait > budget.remaining_seconds():
            return wait
        os.ftruncate(descriptor, 0)
        os.pwrite(descriptor, f"{now + wait + ARXIV_POLITENESS_SECONDS:.3f}".encode("ascii"), 0)
        return wait
    except OSError:
        return wait
    finally:
        os.close(descriptor)


def _public_error(exc: FetchError, what: str) -> SourceError:
    if exc.kind == "not_found":
        return SourceError(f"{what} was not found", "not_found")
    if exc.kind in ("too_large", "type", "refused", "encoding"):
        return SourceError(f"{what}: {exc}", "forbidden" if exc.kind == "refused" else "invalid")
    if exc.kind == "rate_limited":
        return SourceError(f"{what}: {exc}", "unavailable")
    return SourceError(f"{what} could not be fetched ({exc})", "unavailable")


def _parse_xml(body: bytes) -> ET.Element:
    if len(body) > 4 * 1024 * 1024 or b"\x00" in body:
        raise SourceError("The metadata response is invalid", "unavailable")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        raise SourceError("The metadata response is invalid", "unavailable")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise SourceError("The metadata response is invalid", "unavailable") from None
    if sum(1 for _ in root.iter()) > 20000:
        raise SourceError("The metadata response is invalid", "unavailable")
    return root


def _write(root: Path, relative: str, data: bytes) -> str:
    """Write one source file below ``root``; raises :class:`SourceError`
    (kind ``conflict`` when the path is taken by another file or folder)."""
    name = archives.safe_member_name(relative)
    if name is None:
        raise SourceError("A source file has an unsafe name")
    target = root.joinpath(*name.split("/"))
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        raise SourceError(f"A source file path conflicts with another file ({PurePosixPath(name).name})",
                          "conflict") from None
    resolved_root = root.resolve()
    if resolved_root != target.parent.resolve() and resolved_root not in target.parent.resolve().parents:
        raise SourceError("A source file escapes the source folder")
    try:
        with open(target, "xb") as handle:
            handle.write(data)
    except (FileExistsError, IsADirectoryError, NotADirectoryError):
        raise SourceError(f"Two source files have the same name ({PurePosixPath(name).name})", "conflict") from None
    target.chmod(0o600)
    return name


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------


def arxiv_metadata(body: bytes) -> dict[str, Any]:
    """Title, version, abstract, dates, comment and withdrawal flag from an
    arXiv API Atom response with one entry."""
    root = _parse_xml(body)
    entry = root.find(f"{_ATOM}entry")
    if entry is None:
        raise SourceError("arXiv has no paper with that identifier", "not_found")
    identifier = (entry.findtext(f"{_ATOM}id") or "").strip()
    if "/api/errors" in identifier or not identifier:
        raise SourceError("arXiv has no paper with that identifier", "not_found")
    match = re.search(r"arxiv\.org/abs/(.+?)(v\d+)?$", identifier)
    if not match:
        raise SourceError("arXiv returned unexpected metadata", "unavailable")
    title = " ".join((entry.findtext(f"{_ATOM}title") or "").split())
    comment = " ".join((entry.findtext(f"{_ARXIV_NS}comment") or "").split())
    summary = " ".join((entry.findtext(f"{_ATOM}summary") or "").split())
    authors = [" ".join((author.findtext(f"{_ATOM}name") or "").split())
               for author in entry.findall(f"{_ATOM}author")][:20]
    withdrawn = bool(re.search(r"\bwithdrawn\b", f"{title} {comment}", re.I))
    return {
        "arxiv_id": match.group(1),
        "version": match.group(2) or "",
        "title": title[:300],
        "abstract": summary[:4000],
        "comment": comment[:400],
        "authors": [author for author in authors if author],
        "published": (entry.findtext(f"{_ATOM}published") or "").strip()[:40],
        "updated": (entry.findtext(f"{_ATOM}updated") or "").strip()[:40],
        "withdrawn": withdrawn,
    }


def arxiv_license(body: bytes) -> str:
    root = _parse_xml(body)
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "license" and (element.text or "").strip():
            url = element.text.strip()
            return _LICENSES.get(url, url[:200])
    return ""


def _resolve_arxiv(ctx: _Context, info: Mapping[str, Any]) -> ResolvedSource:
    identifier = info["arxiv_id"] + (info.get("version") or "")
    try:
        response = ctx.arxiv(ARXIV_API.format(id=quote(identifier, safe="/.v")), kinds={"text"})
    except FetchError as exc:
        raise _public_error(exc, "The arXiv metadata") from None
    meta = arxiv_metadata(response.body)
    version = info.get("version") or meta["version"]
    pinned = f"{meta['arxiv_id']}{version}"
    warnings = []
    if meta["withdrawn"]:
        warnings.append("arXiv marks this paper as withdrawn")
    return ResolvedSource(
        ok=True,
        connector="arxiv",
        canonical_url=f"https://arxiv.org/abs/{pinned}",
        canonical_id=f"arxiv:{pinned}",
        title=meta["title"],
        version=version,
        format="tex",
        warnings=warnings,
        meta={"arxiv": dict(meta, pinned=pinned)},
    )


def _fetch_arxiv(ctx: _Context, info: Mapping[str, Any], bundle: Bundle, *, role: str) -> None:
    resolved = _resolve_arxiv(ctx, info)
    meta = resolved.meta["arxiv"]
    pinned = meta["pinned"]
    bundle.title, bundle.version = resolved.title, resolved.version
    bundle.canonical_id, bundle.resolved_url = resolved.canonical_id, resolved.canonical_url
    bundle.warnings.extend(resolved.warnings)
    bundle.upstream["arxiv"] = {key: meta[key] for key in ("arxiv_id", "version", "published", "updated", "authors")}
    bundle.upstream["arxiv"]["abstract"] = meta["abstract"]
    try:
        license_body = ctx.arxiv(ARXIV_OAI.format(id=quote(meta["arxiv_id"], safe="/.")), kinds={"text"}).body
        bundle.license = arxiv_license(license_body) or "see the arXiv abstract page"
    except (FetchError, SourceError):
        bundle.license = "see the arXiv abstract page"
    try:
        payload = ctx.arxiv(ARXIV_EPRINT.format(id=quote(pinned, safe="/.v")),
                            kinds={"gzip", "tar", "pdf", "text", "html", "zip"})
    except FetchError as exc:
        raise _public_error(exc, "The arXiv source") from None
    name_hint = ""
    disposition = payload.headers.get("content-disposition", "")
    match = re.search(r'filename="?([^";]+)', disposition)
    if match:
        name_hint = match.group(1)
    if payload.kind in ("gzip", "tar", "zip"):
        _expand_into(bundle, payload.body, name_hint=name_hint or f"arXiv-{pinned}.gz")
        roles = {item["role"] for item in bundle.files()}
        if "tex" in roles:
            return
        bundle.warnings.append("The arXiv source holds no TeX file")
        if "pdf" in roles:
            # The archive's own PDF (kept because there is no TeX).
            if role == "guest":
                raise SourceError("This arXiv paper has no TeX source; sign in to analyze its PDF", "forbidden")
            bundle.warnings.append("PDF only: formulas extracted from a PDF may be garbled")
            return
    elif payload.kind == "text":
        _write(bundle.root, "main.tex", payload.body)
        return
    elif payload.kind == "pdf":
        if role == "guest":
            raise SourceError("This arXiv paper has no TeX source; sign in to analyze its PDF", "forbidden")
        _write(bundle.root, "paper.pdf", payload.body)
        bundle.warnings.append("PDF only: formulas extracted from a PDF may be garbled")
        return
    if role == "guest":
        raise SourceError("This arXiv paper has no TeX source; sign in to analyze its PDF", "forbidden")
    try:
        pdf = ctx.arxiv(ARXIV_PDF.format(id=quote(pinned, safe="/.v")), kinds={"pdf"})
    except FetchError as exc:
        raise _public_error(exc, "The arXiv PDF") from None
    _write(bundle.root, "paper.pdf", pdf.body)
    bundle.warnings.append("PDF only: formulas extracted from a PDF may be garbled")


def _expand_into(bundle: Bundle, data: bytes, *, name_hint: str = "", folder: str = "") -> None:
    staging = bundle.root.parent / f".{bundle.root.name}-expand-{hashlib.sha256(data).hexdigest()[:8]}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        result = archives.expand(data, staging, name_hint=name_hint)
        for name in result.files:
            target = f"{folder}/{name}" if folder else name
            try:
                _write(bundle.root, target, staging.joinpath(*name.split("/")).read_bytes())
            except SourceError as exc:
                if exc.kind != "conflict":
                    raise
                result.skipped.append({"path": target[:200], "reason": "conflicting path"})
    except archives.ArchiveError as exc:
        raise SourceError(str(exc)) from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    bundle.skipped.extend(result.skipped)
    bundle.computational.extend(result.computational)
    if result.computational:
        bundle.warnings.append(
            f"{len(result.computational)} computational file(s) were skipped and never run"
        )


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def parse_ref_advertisement(body: bytes) -> tuple[dict[str, str], str | None]:
    """Refs (``refs/heads/x``, ``refs/tags/y`` peeled) and the HEAD branch from
    a git smart-HTTP ``info/refs`` advertisement."""
    refs: dict[str, str] = {}
    head_branch = None
    position = 0
    while position + 4 <= len(body):
        try:
            length = int(body[position:position + 4], 16)
        except ValueError:
            break
        if length == 0:
            position += 4
            continue
        if length < 4 or position + length > len(body):
            break
        line = body[position + 4:position + length].rstrip(b"\n")
        position += length
        if line.startswith(b"#"):
            continue
        payload, _, capabilities = line.partition(b"\x00")
        if capabilities:
            match = re.search(rb"symref=HEAD:refs/heads/(\S+)", capabilities)
            if match:
                head_branch = match.group(1).decode("utf-8", "replace")
        sha, _, name = payload.partition(b" ")
        if not re.fullmatch(rb"[0-9a-f]{40}", sha):
            continue
        ref = name.decode("utf-8", "replace")
        if ref.endswith("^{}"):
            refs[ref[:-3]] = sha.decode()
        else:
            refs.setdefault(ref, sha.decode())
    return refs, head_branch


class _GitHub:
    def __init__(self, ctx: _Context, owner: str, repo: str) -> None:
        self.ctx = ctx
        self.owner = owner
        self.repo = repo
        self.truncated = False
        self._refs: tuple[dict[str, str], str | None] | None = None
        self._refs_too_large = False

    def refs(self) -> tuple[dict[str, str], str | None] | None:
        """The ref advertisement, or ``None`` when it exceeds the size cap
        (repositories with very many refs: :meth:`split_ref` then asks the API)."""
        if self._refs is None and not self._refs_too_large:
            url = f"https://github.com/{quote(self.owner)}/{quote(self.repo)}.git/info/refs?service=git-upload-pack"
            try:
                response = self.ctx.get(url, allowed_hosts=("github.com",), max_bytes=4 * 1024 * 1024)
            except FetchError as exc:
                if exc.kind == "too_large":
                    self._refs_too_large = True
                    return None
                raise _public_error(exc, "The GitHub repository") from None
            self._refs = parse_ref_advertisement(response.body)
        return self._refs

    def commit_via_api(self, ref: str) -> str:
        """One ``commits/<ref>`` API call (the fallback for huge advertisements)."""
        url = (f"https://{safe_fetch.GITHUB_API_HOST}/repos/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}"
               f"/commits/{quote(ref, safe='')}")
        self.ctx.api_calls += 1
        try:
            response = self.ctx.get(url, allowed_hosts=(safe_fetch.GITHUB_API_HOST,), kinds={"text"},
                                    accept="application/vnd.github+json", max_bytes=2 * 1024 * 1024,
                                    github_api=True)
            sha = json.loads(response.body).get("sha")
        except FetchError as exc:
            raise _public_error(exc, "That branch or tag") from None
        except (ValueError, AttributeError):
            sha = None
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise SourceError("That branch or tag was not found", "not_found")
        return sha

    def split_ref(self, ref_and_path: str) -> tuple[str, str, str]:
        """``(sha, ref name, path)``: the longest known ref prefix wins; a
        full commit SHA is used as is."""
        parts = [part for part in ref_and_path.split("/") if part]
        if parts and _SHA_RE.fullmatch(parts[0]):
            return parts[0], parts[0], "/".join(parts[1:])
        advertised = self.refs()
        if advertised is None:
            ref = parts[0] if parts else "HEAD"
            return self.commit_via_api(ref), ref, "/".join(parts[1:])
        if not parts:
            refs, head = advertised
            branch = head or "main"
            sha = refs.get(f"refs/heads/{branch}") or refs.get("HEAD")
            if not sha:
                raise SourceError("The repository's default branch could not be resolved", "not_found")
            return sha, branch, ""
        refs, _head = advertised
        for size in range(len(parts), 0, -1):
            name = "/".join(parts[:size])
            for candidate in (f"refs/heads/{name}", f"refs/tags/{name}"):
                if candidate in refs:
                    return refs[candidate], name, "/".join(parts[size:])
        if _SHORT_SHA_RE.fullmatch(parts[0]):
            raise SourceError("Use the full 40-character commit SHA in the link", "invalid")
        raise SourceError("That branch or tag was not found", "not_found")

    def raw_url(self, sha: str, path: str) -> str:
        return (f"https://{RAW_HOST}/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}/{sha}/"
                + _quoted_path(path))

    def raw(self, sha: str, path: str, *, kinds: Iterable[str] | None = None, max_bytes: int | None = None) -> bytes:
        try:
            return self.ctx.get(self.raw_url(sha, path), allowed_hosts=(RAW_HOST,), kinds=kinds,
                                max_bytes=max_bytes).body
        except FetchError as exc:
            raise _public_error(exc, f"GitHub file {PurePosixPath(path).name}") from None

    def tree(self, sha: str, path: str) -> list[dict[str, Any]]:
        """Blobs under ``path`` at ``sha`` (one ``git/trees`` API call).

        The repository must answer the anonymous ref advertisement first: the
        operator's token is used only for repositories anyone can read, so a
        link to a private repository never gets a listing through it.
        """
        self.refs()  # raises SourceError for a private or missing repository
        quoted = _quoted_path(path)
        spec = f"{quote(sha, safe='')}:{quoted}" if quoted else quote(sha, safe="")
        url = (f"https://{safe_fetch.GITHUB_API_HOST}/repos/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}"
               f"/git/trees/{spec}?recursive=1")
        self.ctx.api_calls += 1
        try:
            response = self.ctx.get(url, allowed_hosts=(safe_fetch.GITHUB_API_HOST,), kinds={"text"},
                                    accept="application/vnd.github+json", max_bytes=8 * 1024 * 1024,
                                    github_api=True)
        except FetchError as exc:
            raise _public_error(exc, "The GitHub folder listing") from None
        try:
            document = json.loads(response.body)
        except ValueError:
            raise SourceError("GitHub returned an invalid folder listing", "unavailable") from None
        entries = document.get("tree") if isinstance(document, dict) else None
        if not isinstance(entries, list):
            raise SourceError("GitHub returned an invalid folder listing", "unavailable")
        if document.get("truncated") is True:
            self.truncated = True
        blobs = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "blob" or entry.get("mode") in ("120000", "160000"):
                continue
            name = archives.safe_member_name(entry.get("path"))
            size = entry.get("size")
            if name and isinstance(size, int) and size >= 0:
                blobs.append({"path": name, "size": size})
        return blobs


def _quoted_path(path: str) -> str:
    """A repository path for a URL: every segment percent-encoded on its own;
    a dot segment or an encoded separator is refused."""
    parts = [part for part in str(path).split("/") if part]
    if not all(_github_part_ok(part) for part in parts):
        raise SourceError("That GitHub path is invalid")
    return "/".join(quote(part, safe="") for part in parts)


def _tex_closure(github: _GitHub, sha: str, base: str, root_name: str, bundle: Bundle) -> int:
    """Fetch ``root_name`` and the files it ``\\input``s (raw), relative to
    ``base``; returns the number of files written."""
    pending = [root_name]
    seen: set[str] = set()
    written = 0
    fetches = 0
    while pending and fetches < MAX_INCLUDE_FETCHES:
        name = pending.pop(0)
        clean = archives.safe_member_name(name)
        if clean is None or clean in seen:
            continue
        seen.add(clean)
        fetches += 1
        remote = f"{base}/{clean}" if base else clean
        try:
            data = github.raw(sha, remote, kinds={"text"} if not clean.endswith(".pdf") else {"pdf"})
        except SourceError as exc:
            if exc.kind == "not_found":
                continue
            raise
        try:
            _write(bundle.root, clean, data)
        except SourceError as exc:
            if exc.kind != "conflict":
                raise
            bundle.skipped.append({"path": clean[:200], "reason": "conflicting path"})
            continue
        written += 1
        if not clean.endswith(".tex"):
            continue
        text = data.decode("utf-8", "replace")
        text = re.sub(r"(?m)(?<!\\)%.*$", "", text)
        for include in _INCLUDE_RE.findall(text) + _BARE_INPUT_RE.findall(text):
            include = include.strip()
            pending.append(include if include.endswith(".tex") else include + ".tex")
        for folder, include in _IMPORT_RE.findall(text):
            joined = f"{folder.strip().rstrip('/')}/{include.strip()}".lstrip("/")
            pending.append(joined if joined.endswith(".tex") else joined + ".tex")
        for bib in _BIB_RE.findall(text):
            for item in bib.split(","):
                item = item.strip()
                if item:
                    pending.append(item if item.endswith(".bib") else item + ".bib")
        for packages in _PACKAGE_RE.findall(text):
            for package in packages.split(","):
                package = package.strip()
                if package and package not in _STANDARD_PACKAGES and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", package):
                    pending.append(package + ".sty")
        stem = PurePosixPath(clean).stem
        if clean == root_name and _BIB_RE.search(text):
            pending.append(f"{stem}.bbl")
    return written


def _keep_listing(blobs: list[dict[str, Any]], bundle: Bundle) -> list[dict[str, Any]]:
    members = [archives._Member(blob["path"], blob["size"], None) for blob in blobs]
    result = archives.Expanded(kind="listing")
    kept = archives._plan(members, result)
    bundle.skipped.extend(result.skipped)
    bundle.computational.extend(result.computational)
    if result.computational:
        bundle.warnings.append(f"{len(result.computational)} computational file(s) were skipped and never run")
    if len(kept) > MAX_GITHUB_FILES:
        raise SourceError(f"The folder has more than {MAX_GITHUB_FILES} manuscript files; link a smaller folder or the main .tex file")
    total = sum(member.size for member in kept)
    if total > safe_fetch.MAX_BUNDLE_BYTES:
        raise SourceError("The folder exceeds the 30 MiB source limit")
    return [{"path": member.name, "size": member.size} for member in kept]


def _fetch_github(ctx: _Context, info: Mapping[str, Any], bundle: Bundle, *, role: str) -> None:
    github = _GitHub(ctx, info["owner"], info["repo"])
    sha, ref, path = github.split_ref(info.get("ref_and_path") or "")
    bundle.version = sha[:12]
    bundle.canonical_id = f"github:{info['owner']}/{info['repo']}@{sha}" + (f":{path}" if path else "")
    view = "tree" if info["kind"] in ("tree", "repo") else "blob"
    bundle.resolved_url = f"https://github.com/{info['owner']}/{info['repo']}/{view}/{sha}/{path}".rstrip("/")
    bundle.title = PurePosixPath(path).name or info["repo"]
    blob_root = f"https://github.com/{info['owner']}/{info['repo']}/blob/{sha}"
    if info["kind"] == "file":
        suffix = PurePosixPath(path).suffix.lower()
        if suffix == ".tex":
            base = str(PurePosixPath(path).parent) if "/" in path else ""
            _tex_closure(github, sha, base, PurePosixPath(path).name, bundle)
            bundle.file_base_url = f"{blob_root}/{base}".rstrip("/")
            return
        data = github.raw(sha, path, kinds={"text", "pdf", "gzip", "zip", "tar"})
        kind = safe_fetch.sniff(data)
        if kind in ("gzip", "zip", "tar"):
            if role == "guest":
                raise SourceError("Guests can analyze TeX and text sources only", "forbidden")
            _expand_into(bundle, data, name_hint=PurePosixPath(path).name)
        elif kind == "pdf":
            if role == "guest":
                raise SourceError("Guests can analyze TeX and text sources only; sign in to analyze a PDF", "forbidden")
            _write(bundle.root, PurePosixPath(path).name, data)
            bundle.warnings.append("PDF only: formulas extracted from a PDF may be garbled")
        else:
            name = PurePosixPath(path).name
            if PurePosixPath(name).suffix.lower() not in TEXT_SUFFIXES:
                name = name + ".txt"
            _write(bundle.root, name, data)
        return
    blobs = _keep_listing(github.tree(sha, path), bundle)
    if github.truncated:
        bundle.warnings.append("GitHub truncated the folder listing; link a smaller folder or the main .tex file")
    for blob in blobs:
        if blob["path"].lower().endswith(".pdf") and role == "guest":
            continue
        remote = f"{path}/{blob['path']}" if path else blob["path"]
        _write(bundle.root, blob["path"], github.raw(sha, remote))
    bundle.file_base_url = f"{blob_root}/{path}".rstrip("/")


# ---------------------------------------------------------------------------
# openai/math
# ---------------------------------------------------------------------------


def parse_contents(markdown: str, *, sha: str) -> list[dict[str, Any]]:
    """Families and their manuscripts from openai/math ``CONTENTS.md``."""
    families: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    markers = sorted(
        [(match.start(), "family", match) for match in _FAMILY_RE.finditer(markdown)]
        + [(match.start(), "manuscript", match) for match in _MANUSCRIPT_RE.finditer(markdown)],
        key=lambda item: item[0],
    )
    for _position, kind, match in markers:
        if kind == "family":
            lean = _LEAN_LINK_RE.search(match.group("rest"))
            current = {
                "family": match.group("number"),
                "title": _md_plain(match.group("title")).rstrip(".")[:200],
                "lean_doc": lean.group("path") if lean else None,
                "manuscripts": [],
            }
            families.append(current)
        elif current is not None:
            folder = match.group("dir")
            current["manuscripts"].append({
                "dir": folder,
                "title": _md_plain(match.group("title"))[:300],
                "url": f"https://github.com/openai/math/tree/{sha}/preprints/{folder}",
            })
    return families


def _md_plain(value: str) -> str:
    # Titles hold GitHub math ($`n\log n`$), <sub>/<sup> and entities.
    return plain_title(value)


def parse_readme(markdown: str) -> dict[str, Any]:
    """Title, PDF, date, withdrawal and version notes of a manuscript README."""
    title_match = _README_TITLE_RE.search(markdown) or _README_PLAIN_TITLE_RE.search(markdown)
    title = _md_plain(title_match.group("title")) if title_match else ""
    pdf = title_match.groupdict().get("pdf") if title_match else None
    withdrawn = _WITHDRAWN_RE.search(markdown)
    is_withdrawn = bool(withdrawn) or title.lower().startswith("withdrawal notice")
    notice = ""
    archived = None
    if is_withdrawn:
        body = markdown.split("## Archived manuscript")[0]
        body = re.sub(r"^#.*\n", "", body, count=1)
        notice = " ".join(body.split())[:1200]
        revision = _REVISION_RE.search(markdown)
        if revision:
            archived = revision.group("sha") or revision.group("blob")
        title = re.sub(r"^Withdrawal notice:\s*", "", title, flags=re.I)
    version = _VERSION_NOTE_RE.search(markdown)
    date = _DATE_RE.search(markdown)
    return {
        "title": title[:300],
        "pdf": pdf,
        "date": date.group("date") if date else (withdrawn.group("date") if withdrawn else None),
        "withdrawn": is_withdrawn,
        "withdrawn_on": withdrawn.group("date") if withdrawn else None,
        "notice": notice,
        "archived_revision": archived,
        "version_note": " ".join(version.group("body").split())[:600] if version else "",
        "previous_versions": _PREVIOUS_RE.findall(version.group("body")) if version else [],
    }


def _openai_folder(info: Mapping[str, Any]) -> str:
    joined = info.get("ref_and_path") or ""
    after = joined.split("preprints/", 1)[1] if "preprints/" in joined else ""
    folder = after.strip("/").split("/")[0]
    if not folder or archives.safe_member_name(folder) != folder:
        raise SourceError("Link one manuscript folder of openai/math (preprints/<title>)")
    return folder


def _resolve_openai(ctx: _Context, info: Mapping[str, Any]) -> ResolvedSource:
    github = _GitHub(ctx, "openai", "math")
    if info["kind"] in ("root", "preprints"):
        sha, _ref, _path = github.split_ref("" if info["kind"] == "root" else info.get("ref_and_path", "").split("/preprints")[0])
        contents = github.raw(sha, "CONTENTS.md", kinds={"text"}).decode("utf-8", "replace")
        families = parse_contents(contents, sha=sha)
        return ResolvedSource(
            ok=True, connector="openai_math", canonical_url=f"https://github.com/openai/math/tree/{sha}",
            canonical_id=f"github:openai/math@{sha}", title="openai/math manuscript collection",
            version=sha[:12], license="Apache-2.0", format="",
            warnings=["Pick one manuscript to analyze; the whole collection is not analyzed at once"],
            choices=families, meta={"sha": sha},
        )
    folder = _openai_folder(info)
    sha, _ref, _path = github.split_ref(info.get("ref_and_path") or "")
    try:
        readme = parse_readme(github.raw(sha, f"preprints/{folder}/README.md", kinds={"text"}).decode("utf-8", "replace"))
    except SourceError as exc:
        if exc.kind != "not_found":
            raise
        readme = parse_readme("")
    warnings = []
    analyzed_sha = sha
    notice_sha = sha
    if not readme["withdrawn"]:
        # A link pinned to an older revision: the current README may carry a
        # withdrawal notice the pinned one predates (best effort).
        try:
            advertised = github.refs()
            head_sha = advertised[0].get(f"refs/heads/{advertised[1] or 'main'}") if advertised else None
            current = None
            if head_sha and head_sha != sha:
                current = parse_readme(github.raw(head_sha, f"preprints/{folder}/README.md", kinds={"text"})
                                       .decode("utf-8", "replace"))
        except SourceError:
            current = None
        if current and current["withdrawn"]:
            readme = dict(current, title=readme["title"] or current["title"], archived_revision=None,
                          version_note=readme["version_note"], previous_versions=readme["previous_versions"],
                          date=readme["date"])
            notice_sha = head_sha
    if readme["withdrawn"]:
        when = f" on {readme['withdrawn_on']}" if readme["withdrawn_on"] else ""
        warnings.append(f"Withdrawn by its authors{when}; see the withdrawal notice")
        if readme["archived_revision"]:
            analyzed_sha = readme["archived_revision"]
            warnings.append(f"The archived pre-withdrawal revision {analyzed_sha[:7]} is analyzed")
    if readme["version_note"]:
        warnings.append("Revised manuscript: " + readme["version_note"][:240])
    return ResolvedSource(
        ok=True, connector="openai_math",
        canonical_url=f"https://github.com/openai/math/tree/{analyzed_sha}/preprints/{folder}",
        canonical_id=f"github:openai/math@{analyzed_sha}:preprints/{folder}",
        title=readme["title"] or folder.replace("-", " "), version=readme["date"] or analyzed_sha[:12],
        license="Apache-2.0", format="tex", warnings=warnings,
        meta={"sha": notice_sha, "analyzed_sha": analyzed_sha, "folder": folder, "readme": readme},
    )


def _fetch_openai(ctx: _Context, info: Mapping[str, Any], bundle: Bundle, *, role: str) -> None:
    if info["kind"] in ("root", "preprints"):
        raise SourceError("Pick one manuscript of openai/math; the whole collection is not analyzed at once")
    resolved = _resolve_openai(ctx, info)
    meta = resolved.meta
    sha, folder, readme = meta["analyzed_sha"], meta["folder"], meta["readme"]
    bundle.title, bundle.version, bundle.license = resolved.title, resolved.version, "Apache-2.0"
    bundle.canonical_id, bundle.resolved_url = resolved.canonical_id, resolved.canonical_url
    bundle.warnings.extend(resolved.warnings)
    github = _GitHub(ctx, "openai", "math")
    base = f"preprints/{folder}"
    written = 0
    for root_name in ("build/main.tex", "build/paper.tex"):
        try:
            head = github.raw(sha, f"{base}/{root_name}", kinds={"text"})
        except SourceError as exc:
            if exc.kind == "not_found":
                continue
            raise
        if b"\\documentclass" not in head:
            continue
        written = _tex_closure(github, sha, f"{base}/build", PurePosixPath(root_name).name, bundle)
        bundle.file_base_url = f"https://github.com/openai/math/blob/{sha}/{base}/build"
        break
    if not written:
        blobs = [blob for blob in github.tree(sha, base)
                 if not blob["path"].startswith(("verification/",)) and blob["path"] != "README.md"]
        for blob in _keep_listing(blobs, bundle):
            if blob["path"].lower().endswith(".pdf") and role == "guest":
                continue
            _write(bundle.root, blob["path"], github.raw(sha, f"{base}/{blob['path']}"))
            written += 1
        bundle.file_base_url = f"https://github.com/openai/math/blob/{sha}/{base}"
    if not written:
        raise SourceError("No TeX source was found for that manuscript", "not_found")
    # The README (and any withdrawal notice) is never part of the analyzed text.
    bundle.readme_excluded = True
    upstream: dict[str, Any] = {
        "manuscript_dir": folder,
        "commit": sha,
        "withdrawn": readme["withdrawn"],
        "revised": bool(readme["version_note"]),
    }
    if readme["withdrawn"]:
        upstream["withdrawal_notice"] = readme["notice"][:600]
        upstream["notice_url"] = f"https://github.com/openai/math/blob/{meta['sha']}/{base}/README.md"
        upstream["withdrawn_on"] = readme["withdrawn_on"]
    if readme["version_note"]:
        upstream["version_note"] = readme["version_note"]
        upstream["previous_versions"] = readme["previous_versions"][:6]
    try:
        contents = github.raw(sha, "CONTENTS.md", kinds={"text"}).decode("utf-8", "replace")
        family = next((item for item in parse_contents(contents, sha=sha)
                       if any(manuscript["dir"] == folder for manuscript in item["manuscripts"])), None)
    except SourceError:
        family = None
    if family is not None:
        upstream["family"] = family["family"]
        upstream["family_title"] = family["title"]
        if family["lean_doc"]:
            upstream["lean_docs_url"] = f"https://github.com/openai/math/blob/{sha}/{family['lean_doc']}"
            try:
                doc = github.raw(sha, family["lean_doc"], kinds={"text"}).decode("utf-8", "replace")
                upstream["comparator"] = [
                    f"https://github.com/openai/math/blob/{sha}/lean/ComparatorChallenges/{name}"
                    for name in dict.fromkeys(_COMPARATOR_RE.findall(doc))
                ][:12]
            except SourceError:
                pass
    upstream["lean_note"] = "Upstream Lean (v4.34.1 + dependencies); not re-checked by this server"
    bundle.upstream["openai_math"] = upstream


# ---------------------------------------------------------------------------
# generic HTTPS, paste, uploads
# ---------------------------------------------------------------------------


def _fetch_https(ctx: _Context, info: Mapping[str, Any], bundle: Bundle, *, role: str) -> None:
    if role == "guest":
        raise SourceError("Guests can analyze arXiv and GitHub links; sign in to use other links", "forbidden")
    try:
        response = ctx.get(info["canonical_url"], kinds={"pdf", "gzip", "zip", "tar", "text", "html"})
    except FetchError as exc:
        raise _public_error(exc, "The link") from None
    bundle.resolved_url = response.url
    bundle.canonical_id = f"https:{response.url}"
    name = PurePosixPath(urlsplit(response.url).path).name or "source"
    bundle.title = name
    if response.kind == "html":
        raise SourceError("That link is a web page; link the PDF or TeX source, or upload the file", "invalid")
    if response.kind in ("gzip", "zip", "tar"):
        _expand_into(bundle, response.body, name_hint=name)
    elif response.kind == "pdf":
        _write(bundle.root, "paper.pdf", response.body)
        bundle.warnings.append("PDF only: formulas extracted from a PDF may be garbled")
    else:
        text = response.text()
        _write(bundle.root, f"source.{guess_format(text, suffix=PurePosixPath(name).suffix)}", text.encode("utf-8"))


def guess_format(text: str, *, hint: str | None = None, suffix: str = "") -> str:
    """``tex``, ``md`` or ``txt`` for pasted or downloaded text."""
    if hint in ("tex", "md", "txt"):
        return hint
    suffix = suffix.lower()
    if suffix in (".tex", ".ltx"):
        return "tex"
    if suffix in (".md", ".markdown"):
        return "md"
    if re.search(r"\\(?:documentclass|begin\s*\{|section\*?\s*\{|newtheorem)", text):
        return "tex"
    if re.search(r"(?m)^#{1,6} \S|\*\*(?:Theorem|Lemma|Proposition|Proof)", text):
        return "md"
    return "txt"


def bundle_from_paste(text: str, dest: Path, *, fmt: str | None = None, role: str = "member") -> Bundle:
    limit = GUEST_PASTE_CHARS if role == "guest" else MAX_PASTE_CHARS
    if not isinstance(text, str) or not text.strip():
        raise SourceError("The pasted manuscript is empty")
    if len(text) > limit:
        raise SourceError(f"Pasted manuscripts are limited to {limit:,} characters")
    if "\x00" in text:
        raise SourceError("The pasted text contains invalid characters")
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    chosen = guess_format(text, hint=fmt)
    bundle = Bundle(root=dest, source_kind="paste", connector="paste")
    _write(dest, f"pasted.{chosen}", text.replace("\r\n", "\n").encode("utf-8"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    bundle.canonical_id = f"paste:{digest[:16]}"
    return bundle


def bundle_from_uploads(workspace: Path, dest: Path, *, ids: Iterable[str] | None = None,
                        role: str = "member") -> Bundle:
    """Copy (and expand) the run's attachments into ``dest``."""
    workspace = Path(workspace).resolve()
    manifest = workspace / "attachments.json"
    try:
        items = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
    except (OSError, ValueError):
        raise SourceError("The uploaded files could not be read") from None
    wanted = set(ids or [])
    chosen = [item for item in items if isinstance(item, dict) and (not wanted or item.get("id") in wanted)]
    if not chosen:
        raise SourceError("No uploaded manuscript files were found")
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    bundle = Bundle(root=dest, source_kind="upload", connector="upload")
    folder = (workspace / "attachments").resolve()
    stems: set[str] = set()
    for index, item in enumerate(chosen):
        relative = str(item.get("path") or "")
        path = (workspace / relative).resolve()
        if folder not in path.parents or not path.is_file() or path.is_symlink():
            raise SourceError("An uploaded file has an invalid path")
        data = path.read_bytes()
        original = archives.safe_member_name(PurePosixPath(str(item.get("name") or "")).name) or f"upload-{index}"
        kind = safe_fetch.sniff(data)
        if kind in ("zip", "tar", "gzip"):
            if role == "guest":
                raise SourceError("Guests can upload text files only", "forbidden")
            stem = re.sub(r"(\.tar)?\.(gz|tgz|zip|tar)$", "", original, flags=re.I) or f"upload-{index}"
            # paper.zip and paper.tar.gz each get their own folder.
            unique, counter = stem, 2
            while unique.lower() in stems or (len(chosen) > 1 and (dest / unique).exists()):
                unique, counter = f"{stem}-{counter}", counter + 1
            stems.add(unique.lower())
            _expand_into(bundle, data, name_hint=original, folder=unique if len(chosen) > 1 else "")
        elif kind == "pdf":
            if role == "guest":
                raise SourceError("Guests can upload text files only", "forbidden")
            _write(dest, original if original.lower().endswith(".pdf") else original + ".pdf", data)
            bundle.warnings.append("PDF only: formulas extracted from a PDF may be garbled")
        elif kind == "text":
            name = original if PurePosixPath(original).suffix.lower() in TEXT_SUFFIXES else original + ".txt"
            if (dest / name).exists():
                name = f"{index}-{name}"
            _write(dest, name, data)
        else:
            bundle.skipped.append({"path": original, "reason": "not a manuscript file"})
    digest = hashlib.sha256(json.dumps(sorted(str(item.get("id")) for item in chosen)).encode()).hexdigest()
    bundle.canonical_id = f"upload:{digest[:16]}"
    return bundle


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def resolve(
    url: str,
    *,
    fetcher: SafeFetcher | None = None,
    github_token: str | None = None,
    role: str = "member",
    budget: Budget | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ResolvedSource:
    """Metadata for a link (title, version, format, warnings, ``choices`` for
    the openai/math root) within ``RESOLVE_SECONDS``; never downloads the
    manuscript itself.  Errors come back as ``ok: false`` with a message."""
    info = classify(url)
    if not info.get("ok"):
        return ResolvedSource(ok=False, connector=None, error=info.get("error"))
    if role == "guest" and info["connector"] not in GUEST_CONNECTORS:
        return ResolvedSource(ok=False, connector=info["connector"],
                              error="Guests can analyze arXiv and GitHub links; sign in to use other links")
    if fetcher is None:
        fetcher = SafeFetcher(github_token=github_token)
    ctx = _Context(fetcher, budget or Budget(seconds=RESOLVE_SECONDS), sleep, clock)
    try:
        if info["connector"] == "arxiv":
            return _resolve_arxiv(ctx, info)
        if info["connector"] == "openai_math":
            return _resolve_openai(ctx, info)
        if info["connector"] == "github":
            github = _GitHub(ctx, info["owner"], info["repo"])
            sha, _ref, path = github.split_ref(info.get("ref_and_path") or "")
            suffix = PurePosixPath(path).suffix.lower()
            fmt = {".tex": "tex", ".md": "md", ".txt": "txt", ".pdf": "pdf"}.get(suffix, "tex" if info["kind"] != "file" else "")
            return ResolvedSource(
                ok=True, connector="github", canonical_url=info["canonical_url"],
                canonical_id=f"github:{info['owner']}/{info['repo']}@{sha}" + (f":{path}" if path else ""),
                title=PurePosixPath(path).name or info["repo"], version=sha[:12], format=fmt,
                meta={"sha": sha, "path": path},
            )
        return ResolvedSource(ok=True, connector="https", canonical_url=info["canonical_url"],
                              canonical_id=f"https:{info['canonical_url']}",
                              title=PurePosixPath(urlsplit(info["canonical_url"]).path).name or info["canonical_url"],
                              format={".pdf": "pdf", ".tex": "tex"}.get(PurePosixPath(urlsplit(info["canonical_url"]).path).suffix.lower(), ""))
    except SourceError as exc:
        return ResolvedSource(ok=False, connector=info["connector"], canonical_url=info.get("canonical_url"), error=str(exc))
    except FetchError as exc:
        return ResolvedSource(ok=False, connector=info["connector"], canonical_url=info.get("canonical_url"),
                              error=str(_public_error(exc, "The link")))


def fetch_bundle(
    source: Mapping[str, Any],
    dest: Path,
    *,
    workspace: Path,
    role: str = "member",
    fetcher: SafeFetcher | None = None,
    github_token: str | None = None,
    budget: Budget | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Bundle:
    """Fetch, expand or write the request's source into ``dest`` (created)."""
    if role not in ROLES:
        raise SourceError("Unknown role")
    kind = str(source.get("kind") or "")
    if kind == "paste":
        return bundle_from_paste(str(source.get("text") or ""), dest, fmt=source.get("format"), role=role)
    if kind == "upload":
        return bundle_from_uploads(workspace, dest, ids=source.get("attachments"), role=role)
    if kind != "url":
        raise SourceError("The analysis request has no source")
    url = str(source.get("url") or "")
    info = classify(url)
    if not info.get("ok"):
        raise SourceError(str(info.get("error") or "Unsupported link"), "unsupported")
    if role == "guest" and info["connector"] not in GUEST_CONNECTORS:
        raise SourceError("Guests can analyze arXiv and GitHub links; sign in to use other links", "forbidden")
    if info["connector"] == "openai_math" and info["kind"] in ("root", "preprints"):
        raise SourceError("Pick one manuscript of openai/math; the whole collection is not analyzed at once")
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    bundle = Bundle(root=dest, source_kind="url", connector=info["connector"], input_url=url[:2000],
                    resolved_url=str(info.get("canonical_url") or ""))
    if fetcher is None:
        fetcher = SafeFetcher(github_token=github_token)
    ctx = _Context(fetcher, budget or Budget(), sleep, clock)
    try:
        {
            "arxiv": _fetch_arxiv,
            "github": _fetch_github,
            "openai_math": _fetch_openai,
            "https": _fetch_https,
        }[info["connector"]](ctx, info, bundle, role=role)
    except FetchError as exc:
        raise _public_error(exc, "The source") from None
    if not bundle.files():
        raise SourceError("The source contained no manuscript files", "not_found")
    return bundle
