"""Papers linked in a proof run's problem or follow-up, attached as files.

The proof engines cannot open web links (the Ansatz CGM pipeline and Plain
make tool-free model calls), so a participant who pastes an arXiv link gets a
model that never read the paper.  Before a signed-in account's run starts, or
its message is sent, the console finds the links that name a paper and
fetches them with Analyze's connectors (:mod:`agent_monitor.analyze.sources`,
every request through :mod:`agent_monitor.safe_fetch`):

* arXiv (abs / pdf links and ``arXiv:`` ids): the TeX source when the
  e-print has one, flattened into one ``.tex`` file in include order with
  comments removed; otherwise the PDF;
* GitHub links to one ``.tex``, ``.md``, ``.txt`` or ``.pdf`` file;
* other HTTPS links whose path ends in ``.pdf`` or ``.tex``.

Each paper becomes one ordinary attachment (checked by
``attachments.validate`` like an upload), so everything downstream (the CGM
SOURCE MATERIAL, the "📎 Read for the model" line, the run's file list and
its limits) is unchanged.  Web pages are not read.  A link that cannot be
read never refuses the run: the conversation says why.  Links are fetched
once per run (``link_sources.json`` in the workspace remembers them).
Guests are skipped: their attachment allowance is a little text.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from agent_monitor import attachments

MAX_LINKS = 3
# Wall-clock and bytes for all the links of one message (the console waits).
FETCH_SECONDS = 40.0
FETCH_BYTES = 30 * 1024 * 1024
KNOWN_FILENAME = "link_sources.json"
_GITHUB_FILE_SUFFIXES = (".tex", ".md", ".txt", ".pdf")
_HTTPS_SUFFIXES = (".pdf", ".tex")
# A link in prose: up to whitespace or a closing bracket/quote; trailing
# punctuation of the sentence is dropped.
_URL_RE = re.compile(r"(?:https?://|(?<![\w/.])(?:www\.)?arxiv\.org/)[^\s<>\"'`)\]}*,，。、；：！？「」（）]+", re.I)
_ARXIV_ID_RE = re.compile(
    r"(?<![\w./-])arxiv:\s*(\d{4}\.\d{4,5}(?:v\d{1,3})?|[a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7}(?:v\d{1,3})?)(?![\w.]*\d)",
    re.I,
)
_TRAILING = ".,;:!?_~"
_KNOWN_LOCK = threading.Lock()


@dataclass
class Fetched:
    """What one message's links brought: attachments to add (as the browser
    sends them: ``{"name", "data"}``), lines for the conversation, and the
    links read (``canonical id -> file name``) to remember for the run."""

    items: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    read: dict[str, str] = field(default_factory=dict)


def find_links(text: str) -> list[str]:
    """Links in ``text`` that may name a paper, in order, without repeats."""
    found: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(_TRAILING)
        if url and url not in found:
            found.append(url)
    for match in _ARXIV_ID_RE.finditer(text or ""):
        ident = match.group(1)
        if ident not in found:
            found.append(ident)
    return found


def _paper_link(url: str) -> dict[str, Any] | None:
    """Analyze's classification of ``url`` when it names a paper we read,
    else None (a web page, a repository folder, a link we do not try)."""
    from agent_monitor.analyze import sources

    info = sources.classify(url)
    if not info.get("ok"):
        return None
    connector = info.get("connector")
    if connector == "arxiv":
        return info
    if connector == "github":
        path = str(info.get("ref_and_path") or "")
        if info.get("kind") == "file" and PurePosixPath(path).suffix.lower() in _GITHUB_FILE_SUFFIXES:
            return info
        return None
    if connector == "https":
        suffix = PurePosixPath(urlsplit(str(info.get("canonical_url") or "")).path).suffix.lower()
        return info if suffix in _HTTPS_SUFFIXES else None
    return None


def _flatten_tex(root_dir: Path) -> str | None:
    """The bundle's TeX as one text: the root file and what it includes, in
    include order, comments removed, then its ``.bbl`` (the references)."""
    from agent_monitor.analyze import latex_structure

    root_dir = root_dir.resolve()
    try:
        root = latex_structure.find_root(root_dir)
    except latex_structure.LatexStructureError:
        root = None
    if root is None:
        return None
    try:
        order, _missing, _total, _at = latex_structure.resolve_includes(root, root_dir)
    except (OSError, latex_structure.LatexStructureError):
        order = [root]
    parts = []
    base = root_dir.resolve()
    for path in order:
        try:
            text = latex_structure.strip_comments(latex_structure.read_text(path)[0])
        except OSError:
            continue
        text = _trim_preamble(text)
        text = re.sub(r"\n[ \t]*(?:\n[ \t]*){2,}", "\n\n", text).strip()
        parts.append(f"% ===== {path.relative_to(base).as_posix()} =====\n{text}")
    for bbl in sorted(root_dir.rglob("*.bbl"))[:1]:
        if bbl.is_file() and not bbl.is_symlink() and bbl.stat().st_size <= 512 * 1024:
            parts.append(f"% ===== {bbl.relative_to(base).as_posix()} (references) =====\n"
                         + latex_structure.read_text(bbl)[0].strip())
    return "\n\n".join(parts) if parts else None


# Preamble lines worth the model's reading budget: the title and authors and
# the definitions the body's notation depends on.  Packages, page layout and
# the rest of the preamble are dropped.
_PREAMBLE_KEEP = re.compile(
    r"^\s*\\(?:title|author|date|newcommand|renewcommand|providecommand|def|edef|let|"
    r"DeclareMathOperator|newtheorem|theoremstyle|newenvironment|DeclarePairedDelimiter|"
    r"NewDocumentCommand|RenewDocumentCommand|DeclareRobustCommand|makeatletter|makeatother)\b")


def _trim_preamble(text: str) -> str:
    """A root file's preamble (before ``\\begin{document}``) reduced to the
    lines in :data:`_PREAMBLE_KEEP` and their continuation lines."""
    match = re.search(r"\\begin\s*\{document\}", text)
    if not match:
        return text
    kept: list[str] = []
    depth = 0
    for line in text[:match.start()].split("\n"):
        if depth > 0 or _PREAMBLE_KEEP.match(line):
            kept.append(line)
            depth = max(0, depth + line.count("{") - line.count("\\{") - line.count("}") + line.count("\\}"))
    return "\n".join(kept + [text[match.start():]])


def _safe_name(value: str, suffix: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.") or "paper"
    return stem[:120] + suffix


def _bundle_file(bundle: Any, info: dict[str, Any]) -> tuple[str, bytes, str]:
    """One attachment ``(name, bytes, what)`` for a fetched bundle."""
    root = Path(bundle.root)
    if info["connector"] == "arxiv":
        label = (bundle.canonical_id or f"arxiv:{info['arxiv_id']}").replace("arxiv:", "arXiv-").replace("/", "_")
    else:
        label = PurePosixPath(urlsplit(str(bundle.resolved_url or info.get("canonical_url") or "")).path).stem or "paper"
    flat = _flatten_tex(root)
    if flat:
        return _safe_name(label, ".tex"), flat.encode("utf-8"), "TeX source"
    files = [path for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()]
    for path in files:
        if path.suffix.lower() == ".pdf":
            return _safe_name(label, ".pdf"), path.read_bytes(), "PDF; formulas extracted from a PDF may be garbled"
    for path in files:
        if path.suffix.lower() in (".md", ".txt"):
            return _safe_name(label, path.suffix.lower()), path.read_bytes(), "text"
    raise ValueError("the link held no TeX, PDF or text file")


def read_known(workspace: Path | None) -> dict[str, str]:
    if workspace is None:
        return {}
    try:
        data = json.loads((workspace / KNOWN_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def remember(workspace: Path, read: dict[str, str]) -> None:
    """Add the links read for this run (best effort: a lost entry only means
    the paper would be fetched again)."""
    if not read:
        return
    with _KNOWN_LOCK:
        known = read_known(workspace)
        known.update(read)
        try:
            temp = workspace / f".{KNOWN_FILENAME}.tmp"
            temp.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
            temp.replace(workspace / KNOWN_FILENAME)
        except OSError:
            pass


def _key(info: dict[str, Any], url: str) -> str:
    """One paper, however it is linked: an arXiv paper by its id (any version)."""
    if info.get("connector") == "arxiv":
        return f"arxiv:{info['arxiv_id']}"
    return str(info.get("canonical_url") or url)


def _short(url: str) -> str:
    return url if len(url) <= 80 else url[:77] + "…"


def fetch_links(
    text: str,
    *,
    existing: Iterable[dict[str, Any]] | None = None,
    known: dict[str, str] | None = None,
    run_files: Iterable[dict[str, Any]] | None = None,
    role: str = "member",
    fetch_bundle: Callable[..., Any] | None = None,
    budget: Any = None,
) -> Fetched:
    """Fetch the papers linked in ``text`` as attachments that still fit the
    message's limits beside the user's own ``existing`` uploads.  Never raises
    for a link: each one that is not read gets a note saying why."""
    from agent_monitor.analyze import sources
    from agent_monitor.safe_fetch import Budget

    out = Fetched()
    known = dict(known or {})
    uploads = [item for item in (existing or []) if isinstance(item, dict)]
    count = len(uploads)
    total = sum(len(str(item.get("data") or "")) * 3 // 4 for item in uploads)
    # What the run already holds (attachments.save's per-run limits).
    saved = [item for item in (run_files or []) if isinstance(item, dict)]
    run_count = len(saved) + count
    run_total = sum(int(item.get("size") or 0) for item in saved) + total
    papers: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    skipped: list[str] = []
    for url in find_links(text):
        info = _paper_link(url)
        if info is None:
            if url.lower().startswith(("http://", "https://")) and url not in skipped:
                skipped.append(url)
            continue
        key = _key(info, url)
        if key in seen:
            continue
        seen.add(key)
        papers.append((url, info))
    if skipped:
        shown = ", ".join(_short(url) for url in skipped[:3]) + (" …" if len(skipped) > 3 else "")
        out.notes.append(
            f"🔗 Not read: {shown}. Ansatze reads arXiv papers and direct links to a PDF or TeX file, "
            "not web pages; to give the model a document, attach the file.")
    if not papers:
        return out
    if len(papers) > MAX_LINKS:
        out.notes.append(f"🔗 Only the first {MAX_LINKS} paper links of a message are read; attach the others as files.")
        papers = papers[:MAX_LINKS]
    fetch = fetch_bundle or sources.fetch_bundle
    budget = budget or Budget(max_bytes=FETCH_BYTES, seconds=FETCH_SECONDS)
    scratch = Path(tempfile.mkdtemp(prefix="link-sources-"))
    try:
        for index, (url, info) in enumerate(papers):
            key = _key(info, url)
            if key in known:
                out.notes.append(f"🔗 {_short(url)} was already read for this run (attached as {known[key]}).")
                continue
            try:
                bundle = fetch({"kind": "url", "url": url}, scratch / f"link-{index}",
                               workspace=scratch, role=role, budget=budget)
                name, data, what = _bundle_file(bundle, info)
                if len(data) > attachments.MAX_FILE_BYTES:
                    raise ValueError("the paper is larger than the 10 MB file limit")
                if count + 1 > attachments.MAX_FILES or total + len(data) > attachments.MAX_BATCH_BYTES:
                    raise ValueError(
                        f"the message already has {count} files; at most {attachments.MAX_FILES} files and "
                        f"{attachments.MAX_BATCH_BYTES // (1024 * 1024)} MB go with one message")
                if run_count + 1 > attachments.MAX_RUN_FILES or run_total + len(data) > attachments.MAX_RUN_BYTES:
                    raise ValueError("this run has reached its attachment limit")
                item = {"name": name, "data": base64.b64encode(data).decode("ascii")}
                attachments.validate([item])  # the same checks as an upload
            except Exception as exc:  # noqa: BLE001 - a link never refuses the run
                reason = str(exc).strip().rstrip(".") or type(exc).__name__
                out.notes.append(
                    f"🔗 Could not read {_short(url)}: {reason}. The model has not seen this paper; "
                    "download the PDF and attach it to give it to the model.")
                continue
            count += 1
            total += len(data)
            run_count += 1
            run_total += len(data)
            out.items.append(item)
            out.read[key] = name
            title = str(getattr(bundle, "title", "") or "").strip()
            out.notes.append(
                f"🔗 Read from the link {_short(url)}"
                + (f" — “{title[:160]}”" if title and title != name else "")
                + f": attached as {name} ({what}).")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out
