"""Safe expansion of manuscript archives (zip, tar, gzip) into a source folder.

Archives are data: nothing is extracted with ``extractall`` and nothing is
executed.  Only regular files are written, under normalized relative paths
(no absolute paths, ``..``, links, devices or sparse files), each opened with
``O_EXCL | O_NOFOLLOW`` inside a freshly created folder.  Limits: at most
2,000 members, 20 MiB per member, 60 MiB in total and a 200:1 expansion
ratio.  Only manuscript files are kept (``.tex .bib .bbl .sty .cls .md .txt
.lean``, plus at most one PDF when the archive has no TeX); computational
files (``.py``, ``.cpp``, notebooks, ...) are recorded as skipped and never
run.
"""
from __future__ import annotations

import io
import os
import re
import stat
import struct
import tarfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agent_monitor.safe_fetch import sniff

MAX_MEMBERS = 2000
MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
MAX_RATIO = 200
# The ratio guard applies once an expansion passes this size: tiny archives
# (tar padding, short files) legitimately compress far better than 200:1.
RATIO_FLOOR_BYTES = 4 * 1024 * 1024
MAX_PATH_CHARS = 240
MAX_PATH_DEPTH = 12

KEEP_SUFFIXES = frozenset({".tex", ".bib", ".bbl", ".sty", ".cls", ".md", ".txt", ".lean"})
TEXT_SUFFIXES = KEEP_SUFFIXES
COMPUTATIONAL_SUFFIXES = frozenset(
    {
        ".py", ".ipynb", ".sage", ".spyx", ".m", ".mpl", ".mw", ".nb", ".wl",
        ".jl", ".r", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".rs",
        ".go", ".java", ".js", ".ts", ".sh", ".bash", ".zsh", ".pl", ".rb",
        ".lua", ".hs", ".ml", ".f", ".f90", ".gp", ".mag", ".magma", ".g",
        ".gap", ".sing", ".mac", ".sql", ".mk", ".cmake", ".bat", ".ps1",
        ".exe", ".so", ".dll", ".o", ".a", ".jar", ".class", ".wasm",
    }
)
_COMPUTATIONAL_NAMES = frozenset({"makefile", "gnumakefile", "dockerfile", "cmakelists.txt"})


class ArchiveError(ValueError):
    """The archive is unsafe or unsupported (public-safe message)."""


@dataclass
class Expanded:
    kind: str
    files: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    computational: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "files": list(self.files),
            "skipped": list(self.skipped)[:200],
            "computational": list(self.computational)[:200],
            "warnings": list(self.warnings),
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True)
class _Member:
    name: str
    size: int
    token: object  # tarfile.TarInfo or zipfile.ZipInfo


def safe_member_name(raw: object) -> str | None:
    """A normalized relative POSIX path, or ``None`` when it is unsafe.

    A name that is not valid UTF-8 (tarfile keeps such bytes as lone
    surrogates, as in old arXiv tarballs with Latin-1 names) is read as
    Latin-1, the way the LaTeX reader reads a file that is not UTF-8.
    """
    if not isinstance(raw, str):
        return None
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        try:
            raw = raw.encode("utf-8", "surrogateescape").decode("latin-1")
        except UnicodeError:
            return None
    name = raw.replace("\\", "/")
    if not name or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in name):
        return None
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    clean = "/".join(parts)
    if len(parts) > MAX_PATH_DEPTH or len(clean) > MAX_PATH_CHARS:
        return None
    return clean


def classify_member(name: str) -> str:
    """``keep``, ``pdf``, ``computational`` or ``other`` for a member path."""
    path = PurePosixPath(name)
    suffix = path.suffix.lower()
    if suffix in KEEP_SUFFIXES:
        return "keep"
    if suffix == ".pdf":
        return "pdf"
    if suffix in COMPUTATIONAL_SUFFIXES or path.name.lower() in _COMPUTATIONAL_NAMES:
        return "computational"
    return "other"


def gunzip(data: bytes, *, max_bytes: int = MAX_TOTAL_BYTES, max_ratio: int = MAX_RATIO) -> bytes:
    """Decompress a (possibly multi-member) gzip stream within the limits."""
    out = bytearray()
    pending = data
    while pending:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        tail = pending
        while True:
            try:
                chunk = decoder.decompress(tail, max(1, max_bytes + 1 - len(out)))
            except zlib.error:
                raise ArchiveError("The gzip data is corrupt") from None
            out.extend(chunk)
            if len(out) > max_bytes:
                raise ArchiveError("The archive expands beyond its size limit")
            if len(out) > RATIO_FLOOR_BYTES and len(out) > max_ratio * max(1, len(data)):
                raise ArchiveError("The archive expands suspiciously (compression bomb)")
            if decoder.eof:
                break
            tail = decoder.unconsumed_tail
            if not tail and not chunk:
                raise ArchiveError("The gzip data is truncated")
        pending = decoder.unused_data.lstrip(b"\x00")
    return bytes(out)


def _zip_entry_count(data: bytes) -> int | None:
    """Entries in a zip's central directory: the larger of the count its
    end-of-central-directory record declares and the entry signatures in the
    directory itself (``zipfile`` reads the directory by its size, so an
    understated count would let it build every entry before the cap is
    checked)."""
    window_start = max(0, len(data) - (65536 + 22))
    window = data[window_start:]
    index = window.rfind(b"PK\x05\x06")
    if index < 0 or index + 22 > len(window):
        return None
    total = struct.unpack("<H", window[index + 10 : index + 12])[0]
    size = struct.unpack("<L", window[index + 12 : index + 16])[0]
    end = window_start + index
    if total == 0xFFFF or size == 0xFFFFFFFF:
        locator = window.rfind(b"PK\x06\x07", 0, index)
        if locator >= 0 and locator + 20 <= len(window):
            offset = struct.unpack("<Q", window[locator + 8 : locator + 16])[0]
            if 0 <= offset <= len(data) - 56 and data[offset : offset + 4] == b"PK\x06\x06":
                total = struct.unpack("<Q", data[offset + 32 : offset + 40])[0]
                size = struct.unpack("<Q", data[offset + 40 : offset + 48])[0]
                end = offset
        if total == 0xFFFF:
            total = 0
    # As zipfile does: the directory ends where its end record starts.
    start = max(0, end - size)
    return max(total, data.count(b"PK\x01\x02", start, end))


class _Writer:
    """Writes kept members as regular files below ``dest``."""

    def __init__(self, dest: Path, result: Expanded) -> None:
        self.dest = dest
        self.result = result
        self.seen: set[str] = set()

    def write(self, name: str, data: bytes) -> None:
        if name in self.seen:
            self.result.skipped.append({"path": name, "reason": "duplicate member"})
            return
        if self.result.total_bytes + len(data) > MAX_TOTAL_BYTES:
            raise ArchiveError("The archive expands beyond its size limit")
        target = self.dest.joinpath(*name.split("/"))
        parent = target.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            # "sec.tex" and "sec.tex/x.tex": a file where a folder is needed.
            self.result.skipped.append({"path": name, "reason": "conflicting path"})
            return
        root = self.dest.resolve()
        resolved_parent = parent.resolve()
        if resolved_parent != root and root not in resolved_parent.parents:
            raise ArchiveError("An archive member escapes the source folder")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(target, flags, 0o600)
        except FileExistsError:
            self.result.skipped.append({"path": name, "reason": "duplicate member"})
            return
        except (IsADirectoryError, NotADirectoryError):
            self.result.skipped.append({"path": name, "reason": "conflicting path"})
            return
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        self.seen.add(name)
        self.result.files.append(name)
        self.result.total_bytes += len(data)


def _plan(members: list[_Member], result: Expanded) -> list[_Member]:
    """Choose what to keep: manuscript files, plus one PDF if there is no TeX."""
    keep: list[_Member] = []
    pdfs: list[_Member] = []
    for member in members:
        category = classify_member(member.name)
        if member.size > MAX_MEMBER_BYTES:
            result.skipped.append({"path": member.name, "reason": "member exceeds 20 MiB"})
        elif category == "keep":
            keep.append(member)
        elif category == "pdf":
            pdfs.append(member)
        elif category == "computational":
            result.computational.append(member.name)
            result.skipped.append({"path": member.name, "reason": "computational artifact (not executed)"})
        else:
            result.skipped.append({"path": member.name, "reason": "not a manuscript file"})
    has_tex = any(PurePosixPath(member.name).suffix.lower() == ".tex" for member in keep)
    if pdfs and not has_tex:
        largest = max(pdfs, key=lambda member: (member.size, member.name))
        keep.append(largest)
        pdfs = [member for member in pdfs if member is not largest]
    for member in pdfs:
        result.skipped.append({"path": member.name, "reason": "extra PDF (figures or duplicates)"})
    return keep


def _expand_tar(data: bytes, dest: Path, result: Expanded) -> None:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except (tarfile.TarError, EOFError, OSError):
        raise ArchiveError("The tar archive is corrupt or unsupported") from None
    members: list[_Member] = []
    with archive:
        count = 0
        while True:
            try:
                info = archive.next()
            except (tarfile.TarError, EOFError, OSError, UnicodeError):
                raise ArchiveError("The tar archive is corrupt or unsupported") from None
            if info is None:
                break
            count += 1
            if count > MAX_MEMBERS:
                raise ArchiveError(f"The archive has more than {MAX_MEMBERS} members")
            if info.isdir():
                continue
            name = safe_member_name(info.name)
            if name is None:
                result.skipped.append({"path": str(info.name)[:200], "reason": "unsafe path"})
                continue
            if info.issym() or info.islnk():
                result.skipped.append({"path": name, "reason": "link (not followed)"})
                continue
            if not info.isreg() or info.type == tarfile.GNUTYPE_SPARSE or info.issparse():
                result.skipped.append({"path": name, "reason": "not a regular file"})
                continue
            members.append(_Member(name, int(info.size), info))
        writer = _Writer(dest, result)
        for member in _plan(members, result):
            handle = archive.extractfile(member.token)  # type: ignore[arg-type]
            if handle is None:
                continue
            payload = handle.read(MAX_MEMBER_BYTES + 1)
            if len(payload) > MAX_MEMBER_BYTES:
                raise ArchiveError("An archive member exceeds its size limit")
            writer.write(member.name, payload)


def _expand_zip(data: bytes, dest: Path, result: Expanded) -> None:
    declared = _zip_entry_count(data)
    if declared is not None and declared > MAX_MEMBERS:
        raise ArchiveError(f"The archive has more than {MAX_MEMBERS} members")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError):
        raise ArchiveError("The zip archive is corrupt or unsupported") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArchiveError(f"The archive has more than {MAX_MEMBERS} members")
        members: list[_Member] = []
        declared_total = 0
        for info in infos:
            if info.is_dir():
                continue
            name = safe_member_name(info.filename)
            if name is None:
                result.skipped.append({"path": str(info.filename)[:200], "reason": "unsafe path"})
                continue
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                result.skipped.append({"path": name, "reason": "link or special file (not followed)"})
                continue
            if info.flag_bits & 0x1:
                result.skipped.append({"path": name, "reason": "encrypted member"})
                continue
            declared_total += max(0, int(info.file_size))
            members.append(_Member(name, int(info.file_size), info))
        if declared_total > RATIO_FLOOR_BYTES and declared_total > MAX_RATIO * max(1, len(data)):
            raise ArchiveError("The archive expands suspiciously (compression bomb)")
        writer = _Writer(dest, result)
        actual = 0
        for member in _plan(members, result):
            try:
                with archive.open(member.token) as handle:  # type: ignore[arg-type]
                    payload = handle.read(MAX_MEMBER_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, OSError, zlib.error, NotImplementedError, EOFError):
                result.skipped.append({"path": member.name, "reason": "unreadable member"})
                continue
            if len(payload) > MAX_MEMBER_BYTES:
                raise ArchiveError("An archive member exceeds its size limit")
            actual += len(payload)
            if actual > RATIO_FLOOR_BYTES and actual > MAX_RATIO * max(1, len(data)):
                raise ArchiveError("The archive expands suspiciously (compression bomb)")
            writer.write(member.name, payload)


def _single_name(hint: str, payload: bytes) -> str:
    """A file name for a gzip that holds one file (arXiv single-file e-prints)."""
    stem = PurePosixPath(str(hint or "").replace("\\", "/")).name
    for suffix in (".gz", ".gzip", ".tgz"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    kind = sniff(payload)
    if kind == "pdf":
        return "paper.pdf"
    candidate = safe_member_name(stem) or ""
    if candidate and PurePosixPath(candidate).suffix.lower() in KEEP_SUFFIXES:
        return candidate
    if kind == "text":
        return "main.tex"
    raise ArchiveError("The compressed file is not a manuscript (TeX, text or PDF)")


def expand(data: bytes, dest: Path, *, name_hint: str = "") -> Expanded:
    """Expand ``data`` (zip, tar, tar.gz or a gzipped single file) into ``dest``.

    ``dest`` must not exist yet (it is created with mode 0700).  Raises
    :class:`ArchiveError` for unsafe or oversized archives; files written
    before such a refusal stay in ``dest`` and the caller discards it.
    """
    dest = Path(dest)
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    kind = sniff(data)
    if kind == "zip":
        result = Expanded(kind="zip")
        _expand_zip(data, dest, result)
        return result
    if kind == "tar":
        result = Expanded(kind="tar")
        _expand_tar(data, dest, result)
        return result
    if kind == "gzip":
        payload = gunzip(data)
        inner = sniff(payload)
        if inner == "tar":
            result = Expanded(kind="tar.gz")
            _expand_tar(payload, dest, result)
            return result
        if inner == "zip":
            raise ArchiveError("Nested archives are not supported")
        result = Expanded(kind="gzip")
        _Writer(dest, result).write(_single_name(name_hint, payload), payload)
        return result
    if kind in ("bzip2", "xz"):
        raise ArchiveError("bzip2 and xz archives are not supported; use zip or tar.gz")
    raise ArchiveError("The file is not a zip, tar or gzip archive")


def is_archive(data: bytes) -> bool:
    return sniff(data) in ("zip", "tar", "gzip", "bzip2", "xz")
