"""Offline tests: safe archive expansion for Analyze sources."""
from __future__ import annotations

import gzip
import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from agent_monitor.analyze import archives
from agent_monitor.analyze.archives import ArchiveError

TEX = b"\\documentclass{article}\n\\begin{document}\n\\begin{lemma}x\\end{lemma}\n\\end{document}\n"


def make_tar(entries, *, compress=False) -> bytes:
    """entries: (name, data | None, type) with type in reg/sym/lnk/chr/dir/fifo."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "reg":
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
                continue
            info.type = {
                "sym": tarfile.SYMTYPE,
                "lnk": tarfile.LNKTYPE,
                "chr": tarfile.CHRTYPE,
                "dir": tarfile.DIRTYPE,
                "fifo": tarfile.FIFOTYPE,
            }[kind]
            info.linkname = data.decode() if isinstance(data, bytes) else ""
            archive.addfile(info)
    payload = buffer.getvalue()
    return gzip.compress(payload) if compress else payload


def make_zip(entries) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def listing(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def test_tar_gz_keeps_manuscript_files_and_records_computational_ones(tmp_path):
    data = make_tar(
        [
            ("main.tex", TEX, "reg"),
            ("sections/intro.tex", b"\\section{Intro}", "reg"),
            ("refs.bib", b"@article{a, title={T}}", "reg"),
            ("fig/plot.pdf", b"%PDF-1.4 figure", "reg"),
            ("fig/plot.png", b"\x89PNG", "reg"),
            ("check.py", b"import os; os.system('rm -rf /')", "reg"),
            ("verify/run.sage", b"print(1)", "reg"),
            ("Makefile", b"all:\n\trm -rf /", "reg"),
            ("sections", None, "dir"),
        ],
        compress=True,
    )
    dest = tmp_path / "src"
    result = archives.expand(data, dest, name_hint="arXiv-2007.03528v3.tar.gz")
    assert result.kind == "tar.gz"
    assert listing(dest) == ["main.tex", "refs.bib", "sections/intro.tex"]
    assert sorted(result.computational) == ["Makefile", "check.py", "verify/run.sage"]
    reasons = {item["path"]: item["reason"] for item in result.skipped}
    assert reasons["fig/plot.pdf"].startswith("extra PDF")
    assert reasons["check.py"] == "computational artifact (not executed)"
    for path in dest.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize(
    "name",
    ["../evil.tex", "a/../../evil.tex", "/etc/passwd.tex", "C:\\temp\\x.tex", "..\\..\\x.tex", "a\x00b.tex"],
)
def test_traversal_and_absolute_names_are_never_written(tmp_path, name):
    data = make_tar([(name, b"bad", "reg"), ("ok.tex", TEX, "reg")])
    dest = tmp_path / "src"
    result = archives.expand(data, dest)
    assert listing(dest) == ["ok.tex"]
    if "\x00" not in name:  # tar itself truncates a name at NUL
        assert any(item["reason"] == "unsafe path" for item in result.skipped)
    assert not (tmp_path / "evil.tex").exists()
    assert archives.safe_member_name(name) is None


def test_links_devices_and_fifos_are_skipped(tmp_path):
    data = make_tar(
        [
            ("link.tex", b"/etc/passwd", "sym"),
            ("hard.tex", b"main.tex", "lnk"),
            ("dev.tex", None, "chr"),
            ("pipe.tex", None, "fifo"),
            ("main.tex", TEX, "reg"),
        ]
    )
    dest = tmp_path / "src"
    result = archives.expand(data, dest)
    assert listing(dest) == ["main.tex"]
    assert not any(path.is_symlink() for path in dest.rglob("*"))
    reasons = {item["path"]: item["reason"] for item in result.skipped}
    assert reasons["link.tex"].startswith("link")
    assert reasons["hard.tex"].startswith("link")
    assert reasons["dev.tex"] == "not a regular file"


def test_zip_symlinks_and_encrypted_members_are_skipped(tmp_path):
    data = make_zip(
        [
            ("main.tex", TEX, stat.S_IFREG | 0o644),
            ("evil.tex", b"/etc/passwd", stat.S_IFLNK | 0o777),
            ("../escape.tex", b"x", None),
        ]
    )
    dest = tmp_path / "src"
    result = archives.expand(data, dest)
    assert listing(dest) == ["main.tex"]
    reasons = {item["path"]: item["reason"] for item in result.skipped}
    assert reasons["evil.tex"].startswith("link")


def test_gzip_bomb_is_refused(tmp_path):
    bomb = gzip.compress(b"\0" * (archives.MAX_TOTAL_BYTES + 1024))
    with pytest.raises(ArchiveError, match="size limit|compression bomb"):
        archives.expand(bomb, tmp_path / "src")


def test_ratio_guard_trips_before_the_size_limit(tmp_path):
    data = gzip.compress(b"\\relax " * (2 * 1024 * 1024))  # 14 MiB of one token
    assert len(data) * archives.MAX_RATIO < 14 * 1024 * 1024
    with pytest.raises(ArchiveError, match="compression bomb"):
        archives.expand(data, tmp_path / "src")


def test_zip_bomb_by_declared_size_is_refused(tmp_path):
    data = make_zip([("big.tex", b"\0" * (8 * 1024 * 1024), None)])
    with pytest.raises(ArchiveError, match="compression bomb"):
        archives.expand(data, tmp_path / "src")


def test_member_count_cap(tmp_path):
    entries = [(f"f{index}.txt", b"x", "reg") for index in range(archives.MAX_MEMBERS + 1)]
    with pytest.raises(ArchiveError, match="more than 2000 members"):
        archives.expand(make_tar(entries), tmp_path / "src")
    zipped = make_zip([(f"f{index}.txt", b"x", None) for index in range(archives.MAX_MEMBERS + 1)])
    with pytest.raises(ArchiveError, match="more than 2000 members"):
        archives.expand(zipped, tmp_path / "src2")


def test_oversized_members_are_skipped_and_total_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "MAX_MEMBER_BYTES", 1024)
    monkeypatch.setattr(archives, "MAX_TOTAL_BYTES", 1500)
    data = make_tar([("big.tex", b"x" * 2048, "reg"), ("a.tex", b"y" * 900, "reg"), ("b.tex", b"z" * 900, "reg")])
    with pytest.raises(ArchiveError, match="size limit"):
        archives.expand(data, tmp_path / "src")
    data = make_tar([("big.tex", b"x" * 2048, "reg"), ("a.tex", b"y" * 900, "reg")])
    result = archives.expand(data, tmp_path / "src2")
    assert result.files == ["a.tex"]
    assert {"path": "big.tex", "reason": "member exceeds 20 MiB"} in result.skipped


def test_pdf_only_archives_keep_their_largest_pdf(tmp_path):
    data = make_zip([("small.pdf", b"%PDF-1.4 a", None), ("paper.pdf", b"%PDF-1.4 " + b"b" * 100, None)])
    result = archives.expand(data, tmp_path / "src")
    assert result.files == ["paper.pdf"]


def test_single_file_gzip_like_arxiv(tmp_path):
    result = archives.expand(gzip.compress(TEX), tmp_path / "a", name_hint="arXiv-2302.05537v2.gz")
    assert result.kind == "gzip" and result.files == ["main.tex"]
    result = archives.expand(gzip.compress(TEX), tmp_path / "b", name_hint="paper.tex.gz")
    assert result.files == ["paper.tex"]
    result = archives.expand(gzip.compress(b"%PDF-1.7 x"), tmp_path / "c")
    assert result.files == ["paper.pdf"]
    with pytest.raises(ArchiveError):
        archives.expand(gzip.compress(b"\x00\x01binary"), tmp_path / "d")


def test_duplicates_corrupt_and_unsupported_archives(tmp_path):
    data = make_tar([("main.tex", TEX, "reg"), ("./main.tex", b"second", "reg")])
    result = archives.expand(data, tmp_path / "a")
    assert (tmp_path / "a" / "main.tex").read_bytes() == TEX
    assert {"path": "main.tex", "reason": "duplicate member"} in result.skipped
    with pytest.raises(ArchiveError):
        archives.expand(b"\x1f\x8b\x08\x00garbage", tmp_path / "b")
    with pytest.raises(ArchiveError, match="not supported"):
        archives.expand(b"BZh91AY&SY", tmp_path / "c")
    with pytest.raises(ArchiveError, match="not a zip"):
        archives.expand(b"just text", tmp_path / "d")


def test_destination_must_be_fresh(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(FileExistsError):
        archives.expand(make_tar([("a.tex", TEX, "reg")]), tmp_path / "src")


def test_member_names_are_normalized():
    assert archives.safe_member_name("./a//b/./c.tex") == "a/b/c.tex"
    assert archives.safe_member_name("a\\b.tex") == "a/b.tex"
    assert archives.safe_member_name("../x") is None
    assert archives.safe_member_name("/x") is None
    assert archives.safe_member_name("x" * 300) is None
    assert archives.safe_member_name("/".join(["d"] * 20) + "/x.tex") is None
    assert archives.classify_member("x/run.PY") == "computational"
    assert archives.classify_member("paper.PDF") == "pdf"
    assert archives.classify_member("notes.md") == "keep"
    assert os.sep == "/"


def test_non_utf8_member_names_are_read_as_latin1_and_can_be_stored(tmp_path):
    import json

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT, encoding="latin-1") as archive:
        for name, data in (("main.tex", TEX), ("figures/sch\xe9ma.eps", b"%!PS"), ("th\xe9or\xe8me.tex", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    result = archives.expand(gzip.compress(buffer.getvalue()), tmp_path / "out")
    assert sorted(result.files) == ["main.tex", "th\xe9or\xe8me.tex"]
    assert {"path": "figures/sch\xe9ma.eps", "reason": "not a manuscript file"} in result.skipped
    json.dumps(result.as_dict(), ensure_ascii=False).encode("utf-8")  # no lone surrogate survives
    assert archives.safe_member_name("a\udce9b.tex") == "a\xe9b.tex"
    assert archives.safe_member_name("a\ud800b.tex") is None
    assert archives.safe_member_name("bell\x85.tex") is None


def test_a_file_where_a_folder_is_needed_is_skipped(tmp_path):
    data = make_tar([("main.tex", TEX, "reg"), ("sec.tex", b"a", "reg"), ("sec.tex/x.tex", b"b", "reg")])
    result = archives.expand(data, tmp_path / "out")
    assert sorted(result.files) == ["main.tex", "sec.tex"]
    assert {"path": "sec.tex/x.tex", "reason": "conflicting path"} in result.skipped


def test_a_zip_that_understates_its_entry_count_is_refused_before_parsing(tmp_path):
    import resource
    import struct
    import time

    count = 5000
    directory = bytearray()
    for index in range(count):
        name = b"%x.tex" % index
        directory += struct.pack("<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, 0, 0, 0, 0, 0, 0, len(name),
                                 0, 0, 0, 0, 0, 0) + name
    local = struct.pack("<4s5H3L2H", b"PK\x03\x04", 20, 0, 0, 0, 0, 0, 0, 0, 1, 0) + b"a"
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(directory), len(local), 0)
    started = time.monotonic()
    with pytest.raises(ArchiveError, match="more than 2000 members"):
        archives.expand(local + bytes(directory) + end, tmp_path / "out")
    assert time.monotonic() - started < 1
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > 0
