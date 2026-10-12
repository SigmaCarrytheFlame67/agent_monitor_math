"""Offline tests: Analyze source connectors (arXiv, GitHub, openai/math, HTTPS,
paste, uploads) against a scripted fetcher."""
from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from agent_monitor import safe_fetch
from agent_monitor.analyze import sources
from agent_monitor.analyze.sources import SourceError
from agent_monitor.safe_fetch import FetchError, FetchResult

SHA = "fd4aeeb2ee4fc729c18d98444fed42fd0529eeeb"
OLD = "adc7f1241b42e322a6451854ab7e4b4c146bf78a"
REFS = (
    b"001e# service=git-upload-pack\n0000"
    b"0159" + SHA.encode() + b" HEAD\x00multi_ack thin-pack side-band side-band-64k ofs-delta shallow "
    b"deepen-since deepen-not deepen-relative no-progress include-tag multi_ack_detailed "
    b"allow-tip-sha1-in-want allow-reachable-sha1-in-want no-done symref=HEAD:refs/heads/main filter "
    b"object-format=sha1 agent=git/github-e0611d5c1305-Linux\n"
    b"004a301488868beec11bfd897168433b0a64f5258559 refs/heads/codex/update-10-7\n"
    b"003d" + SHA.encode() + b" refs/heads/main\n"
    b"003e301488868beec11bfd897168433b0a64f5258559 refs/pull/1/head\n0000"
)
RAW = f"https://raw.githubusercontent.com/openai/math/{SHA}/"


def atom(identifier: str, title: str = "Sumsets and things", comment: str = "17 pages") -> bytes:
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/{identifier}</id>
    <title>{title}</title>
    <updated>2023-02-11T00:00:00Z</updated>
    <summary>We prove a strong bound.</summary>
    <published>2023-02-10T00:00:00Z</published>
    <arxiv:comment>{comment}</arxiv:comment>
    <author><name>Zander Kelley</name></author>
    <author><name>Raghu Meka</name></author>
  </entry>
</feed>""".encode()


OAI = b"""<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><GetRecord><record><metadata>
<arXiv xmlns="http://arxiv.org/OAI/arXiv/"><id>2302.05537</id><license>http://creativecommons.org/licenses/by/4.0/</license></arXiv>
</metadata></record></GetRecord></OAI-PMH>"""
TEX = b"\\documentclass{article}\n\\begin{document}\n\\begin{theorem}x\\end{theorem}\n\\end{document}\n"


def targz(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeFetcher:
    """Scripted ``SafeFetcher.get`` that still enforces URL policy and kinds."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[str] = []
        self.api_calls: list[str] = []
        self.has_github_token = False

    def get(self, url, *, allowed_hosts=None, kinds=None, budget=None, max_bytes=None, accept=None, headers=None,
            github_api=False):
        safe_fetch.public_https_url(url, allowed_hosts=allowed_hosts)
        self.calls.append(url)
        if github_api:
            self.api_calls.append(url)
        value = self.routes.get(url)
        if value is None:
            raise FetchError("The link was not found (HTTP 404)", kind="not_found", status=404)
        if isinstance(value, FetchError):
            raise value
        body, extra = value if isinstance(value, tuple) else (value, {})
        kind = safe_fetch.sniff(body)
        if kinds is not None and kind not in set(kinds):
            raise FetchError("The link returned an unsupported file type", kind="type")
        if budget is not None:
            budget.charge(len(body))
        return FetchResult(url=url, body=body, kind=kind, headers=dict(extra))


@pytest.fixture(autouse=True)
def _reset_arxiv_politeness(monkeypatch):
    monkeypatch.setattr(sources, "_ARXIV_LAST", [0.0])
    monkeypatch.setattr(sources, "ARXIV_SLOT_PATH", None)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


@pytest.mark.parametrize(
    ("value", "connector", "canonical"),
    [
        ("2302.05537", "arxiv", "https://arxiv.org/abs/2302.05537"),
        ("arXiv:2302.05537v2", "arxiv", "https://arxiv.org/abs/2302.05537v2"),
        ("math/0601001v3", "arxiv", "https://arxiv.org/abs/math/0601001v3"),
        ("https://arxiv.org/abs/2007.03528v3", "arxiv", "https://arxiv.org/abs/2007.03528v3"),
        ("http://arxiv.org/pdf/2007.03528.pdf", "arxiv", "https://arxiv.org/abs/2007.03528"),
        ("arxiv.org/e-print/2007.03528v1", "arxiv", "https://arxiv.org/abs/2007.03528v1"),
        ("https://export.arxiv.org/src/2302.05537", "arxiv", "https://arxiv.org/abs/2302.05537"),
        ("https://github.com/o/r/blob/main/paper/main.tex", "github", "https://github.com/o/r/blob/main/paper/main.tex"),
        ("https://raw.githubusercontent.com/o/r/refs/heads/dev/x.tex", "github", "https://github.com/o/r/blob/dev/x.tex"),
        ("https://github.com/openai/math", "openai_math", "https://github.com/openai/math"),
        ("https://example.org/papers/a.pdf", "https", "https://example.org/papers/a.pdf"),
    ],
)
def test_classify_recognizes_links_without_network(value, connector, canonical):
    info = sources.classify(value)
    assert info["ok"] and info["connector"] == connector and info["canonical_url"] == canonical


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("https://github.com/openai/math", "root"),
        ("https://github.com/openai/math/tree/main/preprints", "preprints"),
        ("https://github.com/openai/math/tree/main/preprints/A-Paper-September-1-2026", "folder"),
        ("https://github.com/openai/math/blob/main/preprints/A-Paper-September-1-2026/paper.pdf", "file"),
        ("https://github.com/openai/math/tree/main/preprints/A-Paper-September-1-2026/build", "folder-path"),
    ],
)
def test_openai_math_link_kinds(value, kind):
    assert sources.classify(value)["kind"] == kind


@pytest.mark.parametrize(
    "value",
    [
        "", "http://example.org/a.pdf", "https://127.0.0.1/a.pdf", "file:///etc/passwd",
        "javascript:alert(1)", "https://arxiv.org/list/math.NT/recent", "https://github.com/onlyowner",
        "https://github.com/o/r/issues/1", "https://github.com/o/r/blob/main/../../x",
        "https://user:pw@github.com/o/r", "https://internal.corp/a.pdf",
    ],
)
def test_classify_refuses_unsupported_or_unsafe_links(value):
    info = sources.classify(value)
    assert info["ok"] is False and info["error"]


def test_ref_advertisement_parsing():
    refs, head = sources.parse_ref_advertisement(REFS)
    assert head == "main"
    assert refs["refs/heads/main"] == SHA
    assert refs["refs/heads/codex/update-10-7"].startswith("3014888")


def test_arxiv_bundle_pins_the_version_and_waits_between_requests(tmp_path):
    clock = Clock()
    fetcher = FakeFetcher({
        sources.ARXIV_API.format(id="2302.05537"): atom("2302.05537v2"),
        sources.ARXIV_OAI.format(id="2302.05537"): OAI,
        sources.ARXIV_EPRINT.format(id="2302.05537v2"): (
            targz({"main.tex": TEX, "refs.bib": b"@article{a,title={T}}", "run.py": b"import os"}),
            {"content-disposition": 'attachment; filename="arXiv-2302.05537v2.tar.gz"'},
        ),
    })
    bundle = sources.fetch_bundle({"kind": "url", "url": "https://arxiv.org/abs/2302.05537"}, tmp_path / "src",
                                  workspace=tmp_path, fetcher=fetcher, sleep=clock.sleep, clock=clock)
    assert [item["path"] for item in bundle.files()] == ["main.tex", "refs.bib"]
    assert bundle.canonical_id == "arxiv:2302.05537v2" and bundle.version == "v2"
    assert bundle.license == "CC BY 4.0"
    assert bundle.computational == ["run.py"]
    assert clock.sleeps == [3.0, 3.0]  # three arXiv requests, 3 s apart
    manifest = bundle.manuscript(root_file="main.tex")
    assert manifest["format"] == "tex" and manifest["upstream"]["arxiv"]["authors"] == ["Zander Kelley", "Raghu Meka"]
    assert len(manifest["bundle_sha256"]) == 64


def test_arxiv_single_gzipped_tex_and_pdf_only_sources(tmp_path):
    clock = Clock()
    routes = {
        sources.ARXIV_API.format(id="2007.03528v1"): atom("2007.03528v1"),
        sources.ARXIV_EPRINT.format(id="2007.03528v1"): gzip.compress(TEX),
    }
    bundle = sources.fetch_bundle({"kind": "url", "url": "2007.03528v1"}, tmp_path / "a", workspace=tmp_path,
                                  fetcher=FakeFetcher(routes), sleep=clock.sleep, clock=clock)
    assert [item["path"] for item in bundle.files()] == ["main.tex"]
    assert bundle.license == "see the arXiv abstract page"
    pdf_routes = {
        sources.ARXIV_API.format(id="2007.03528v1"): atom("2007.03528v1"),
        sources.ARXIV_EPRINT.format(id="2007.03528v1"): b"%PDF-1.5 only a pdf",
    }
    with pytest.raises(SourceError, match="sign in") as error:
        sources.fetch_bundle({"kind": "url", "url": "2007.03528v1"}, tmp_path / "b", workspace=tmp_path,
                             role="guest", fetcher=FakeFetcher(pdf_routes), sleep=clock.sleep, clock=clock)
    assert error.value.kind == "forbidden"
    bundle = sources.fetch_bundle({"kind": "url", "url": "2007.03528v1"}, tmp_path / "c", workspace=tmp_path,
                                  fetcher=FakeFetcher(pdf_routes), sleep=clock.sleep, clock=clock)
    assert [item["path"] for item in bundle.files()] == ["paper.pdf"]
    assert any("PDF only" in warning for warning in bundle.warnings)


def test_arxiv_archive_with_only_a_pdf_uses_that_pdf(tmp_path):
    clock = Clock()
    routes = {
        sources.ARXIV_API.format(id="2007.03528v1"): atom("2007.03528v1"),
        sources.ARXIV_EPRINT.format(id="2007.03528v1"): targz({"paper.pdf": b"%PDF-1.5 x", "fig.png": b"png"}),
    }
    fetcher = FakeFetcher(routes)
    bundle = sources.fetch_bundle({"kind": "url", "url": "2007.03528v1"}, tmp_path / "a", workspace=tmp_path,
                                  fetcher=fetcher, sleep=clock.sleep, clock=clock)
    assert [item["path"] for item in bundle.files()] == ["paper.pdf"]
    assert not any("/pdf/" in call for call in fetcher.calls)
    with pytest.raises(SourceError, match="sign in"):
        sources.fetch_bundle({"kind": "url", "url": "2007.03528v1"}, tmp_path / "b", workspace=tmp_path,
                             role="guest", fetcher=FakeFetcher(routes), sleep=clock.sleep, clock=clock)


def test_arxiv_withdrawn_and_missing_papers():
    clock = Clock()
    fetcher = FakeFetcher({sources.ARXIV_API.format(id="1234.56789"): atom("1234.56789v3", comment="This paper has been withdrawn")})
    resolved = sources.resolve("1234.56789", fetcher=fetcher, sleep=clock.sleep, clock=clock)
    assert resolved.ok and resolved.version == "v3" and resolved.warnings == ["arXiv marks this paper as withdrawn"]
    missing = FakeFetcher({sources.ARXIV_API.format(id="9999.99999"): b"""<?xml version='1.0'?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#incorrect_id_format</id></entry></feed>"""})
    resolved = sources.resolve("9999.99999", fetcher=missing, sleep=clock.sleep, clock=clock)
    assert not resolved.ok and "no paper" in resolved.error


def test_arxiv_metadata_refuses_entities():
    with pytest.raises(SourceError):
        sources.arxiv_metadata(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><feed/>')


def _github_routes(extra: dict) -> dict:
    return {"https://github.com/o/r.git/info/refs?service=git-upload-pack": REFS.replace(b"openai", b"o"), **extra}


def test_github_tex_file_brings_its_include_closure_at_the_resolved_commit(tmp_path):
    base = f"https://raw.githubusercontent.com/o/r/{SHA}/paper/"
    fetcher = FakeFetcher(_github_routes({
        base + "main.tex": b"\\documentclass{article}\\usepackage{amsmath,mymacros}\\begin{document}"
                           b"\\input{sections/intro}\\include{appendix}\\import{chapters/}{one}\\input bare"
                           b"% \\input{commented}\n\\input{../../etc/passwd}\\bibliography{refs}\\end{document}",
        base + "sections/intro.tex": b"\\section{Intro}",
        base + "appendix.tex": b"\\section{App}",
        base + "chapters/one.tex": b"\\section{One}",
        base + "refs.bib": b"@article{a, title={T}}",
        base + "mymacros.sty": b"\\newtheorem{thm}{Theorem}",
        base + "bare.tex": b"\\section{Bare}",
    }))
    bundle = sources.fetch_bundle({"kind": "url", "url": "https://github.com/o/r/blob/main/paper/main.tex"},
                                  tmp_path / "src", workspace=tmp_path, fetcher=fetcher)
    assert sorted(item["path"] for item in bundle.files()) == [
        "appendix.tex", "bare.tex", "chapters/one.tex", "main.tex", "mymacros.sty", "refs.bib", "sections/intro.tex"]
    assert not any("commented" in call or "passwd" in call or "amsmath" in call for call in fetcher.calls)
    assert bundle.canonical_id == f"github:o/r@{SHA}:paper/main.tex"
    assert all(call.startswith(("https://github.com/o/r.git/info/refs", base)) for call in fetcher.calls)


def test_github_refs_with_slashes_and_full_shas(tmp_path):
    fetcher = FakeFetcher(_github_routes({
        "https://raw.githubusercontent.com/o/r/301488868beec11bfd897168433b0a64f5258559/x.tex": TEX,
    }))
    bundle = sources.fetch_bundle({"kind": "url", "url": "https://github.com/o/r/blob/codex/update-10-7/x.tex"},
                                  tmp_path / "a", workspace=tmp_path, fetcher=fetcher)
    assert bundle.version == "301488868bee"
    direct = FakeFetcher({f"https://raw.githubusercontent.com/o/r/{SHA}/x.tex": TEX})
    sources.fetch_bundle({"kind": "url", "url": f"https://github.com/o/r/blob/{SHA}/x.tex"},
                         tmp_path / "b", workspace=tmp_path, fetcher=direct)
    assert direct.calls == [f"https://raw.githubusercontent.com/o/r/{SHA}/x.tex"]
    with pytest.raises(SourceError, match="40-character"):
        sources.fetch_bundle({"kind": "url", "url": "https://github.com/o/r/blob/fd4aeeb/x.tex"},
                             tmp_path / "c", workspace=tmp_path, fetcher=FakeFetcher(_github_routes({})))
    with pytest.raises(SourceError, match="not found"):
        sources.fetch_bundle({"kind": "url", "url": "https://github.com/o/r/blob/nope/x.tex"},
                             tmp_path / "d", workspace=tmp_path, fetcher=FakeFetcher(_github_routes({})))


def test_huge_ref_advertisements_fall_back_to_one_api_lookup(tmp_path):
    too_large = FetchError("The file exceeds its size limit", kind="too_large")
    fetcher = FakeFetcher({
        "https://github.com/o/r.git/info/refs?service=git-upload-pack": too_large,
        "https://api.github.com/repos/o/r/commits/main": json.dumps({"sha": SHA}).encode(),
        f"https://raw.githubusercontent.com/o/r/{SHA}/x.tex": TEX,
    })
    bundle = sources.fetch_bundle({"kind": "url", "url": "https://github.com/o/r/blob/main/x.tex"},
                                  tmp_path / "a", workspace=tmp_path, fetcher=fetcher)
    assert bundle.canonical_id == f"github:o/r@{SHA}:x.tex"
    assert sum(1 for call in fetcher.calls if "api.github.com" in call) == 1


def test_github_folder_uses_one_tree_call_and_skips_computational_files(tmp_path):
    tree = {"truncated": True, "tree": [
        {"path": "main.tex", "type": "blob", "mode": "100644", "size": len(TEX)},
        {"path": "check.py", "type": "blob", "mode": "100644", "size": 10},
        {"path": "fig.png", "type": "blob", "mode": "100644", "size": 10},
        {"path": "link.tex", "type": "blob", "mode": "120000", "size": 10},
        {"path": "sub", "type": "tree", "mode": "040000"},
        {"path": "sub/part.tex", "type": "blob", "mode": "100644", "size": 5},
    ]}
    api = f"https://api.github.com/repos/o/r/git/trees/{SHA}:papers/x?recursive=1"
    fetcher = FakeFetcher(_github_routes({
        api: json.dumps(tree).encode(),
        f"https://raw.githubusercontent.com/o/r/{SHA}/papers/x/main.tex": TEX,
        f"https://raw.githubusercontent.com/o/r/{SHA}/papers/x/sub/part.tex": b"part",
    }))
    bundle = sources.fetch_bundle({"kind": "url", "url": "https://github.com/o/r/tree/main/papers/x"},
                                  tmp_path / "src", workspace=tmp_path, fetcher=fetcher)
    assert sorted(item["path"] for item in bundle.files()) == ["main.tex", "sub/part.tex"]
    assert bundle.computational == ["check.py"]
    assert bundle.file_base_url == f"https://github.com/o/r/blob/{SHA}/papers/x"
    assert any("truncated" in warning for warning in bundle.warnings)
    assert sum(1 for call in fetcher.calls if "api.github.com" in call) == 1


def test_github_folder_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_GITHUB_FILES", 2)
    tree = {"tree": [{"path": f"f{index}.tex", "type": "blob", "mode": "100644", "size": 1} for index in range(3)]}
    fetcher = FakeFetcher(_github_routes({
        f"https://api.github.com/repos/o/r/git/trees/{SHA}?recursive=1": json.dumps(tree).encode()}))
    with pytest.raises(SourceError, match="more than 2"):
        sources.fetch_bundle({"kind": "url", "url": "https://github.com/o/r"}, tmp_path / "s",
                             workspace=tmp_path, fetcher=fetcher)


CONTENTS = f"""# Mathematics manuscript collection

**017. The irrationality exponent of <i>π</i> is 2.** Proves it. ([Lean](lean/docs/017.md))

&emsp;[The irrationality exponent of $\\pi$ is 2](preprints/Pi-Paper-September-24-2026/paper.pdf)

Abstract text.

**032. The rational Hodge conjecture for CM abelian varieties.** Proves it.

&emsp;[Weil classes on eightfolds](preprints/Weil-Paper-September-18-2026/paper.pdf)
""".encode()
README = b"""# [The irrationality exponent of $\\pi$ is 2](paper.pdf)

**Author:** OpenAI

**Date:** September 24, 2026

## Version note

This version updates a citation; see the [previous version](../Pi-Paper-September-20-2026/paper.pdf).
"""
WITHDRAWN = f"""# [Withdrawal notice: Weil classes on eightfolds](paper.pdf)

OpenAI
**Withdrawn on October 6, 2026.**

The proof contains a sign error in the stabilization-trace argument.

This withdrawal concerns the proof; it does not assert that the mathematical statement is false.

## Archived manuscript

[Pre-withdrawal PDF](https://github.com/openai/math/blob/{OLD}/preprints/Weil-Paper-September-18-2026/paper.pdf)
Repository revision: `{OLD}`.
""".encode()


def _openai_routes(extra: dict) -> dict:
    return {"https://github.com/openai/math.git/info/refs?service=git-upload-pack": REFS, **extra}


def test_openai_math_folder_brings_tex_versions_and_upstream_lean(tmp_path):
    folder = "Pi-Paper-September-24-2026"
    fetcher = FakeFetcher(_openai_routes({
        RAW + f"preprints/{folder}/README.md": README,
        RAW + f"preprints/{folder}/build/main.tex": b"\\documentclass{amsart}\\begin{document}\\input{sections/series}\\bibliography{references}\\end{document}",
        RAW + f"preprints/{folder}/build/sections/series.tex": b"\\begin{lemma}x\\end{lemma}",
        RAW + f"preprints/{folder}/build/references.bib": b"@article{a,title={T}}",
        RAW + "CONTENTS.md": CONTENTS,
        RAW + "lean/docs/017.md": b"| r | [PiExponent.lean](../ComparatorChallenges/PiExponent.lean) |",
    }))
    bundle = sources.fetch_bundle({"kind": "url", "url": f"https://github.com/openai/math/tree/main/preprints/{folder}"},
                                  tmp_path / "src", workspace=tmp_path, fetcher=fetcher)
    assert sorted(item["path"] for item in bundle.files()) == ["main.tex", "references.bib", "sections/series.tex"]
    upstream = bundle.upstream["openai_math"]
    assert upstream["family"] == "017" and upstream["revised"] and upstream["previous_versions"] == ["Pi-Paper-September-20-2026"]
    assert upstream["comparator"] == [f"https://github.com/openai/math/blob/{SHA}/lean/ComparatorChallenges/PiExponent.lean"]
    assert "not re-checked" in upstream["lean_note"]
    assert bundle.title == "The irrationality exponent of π is 2" and bundle.license == "Apache-2.0"
    assert bundle.file_base_url == f"https://github.com/openai/math/blob/{SHA}/preprints/{folder}/build"
    assert not any(item["path"].endswith("README.md") for item in bundle.files())
    assert any(warning.startswith("Revised manuscript") for warning in bundle.warnings)


def test_withdrawn_openai_math_folder_is_analyzed_at_its_archived_revision(tmp_path):
    folder = "Weil-Paper-September-18-2026"
    old_raw = f"https://raw.githubusercontent.com/openai/math/{OLD}/"
    fetcher = FakeFetcher(_openai_routes({
        RAW + f"preprints/{folder}/README.md": WITHDRAWN,
        old_raw + f"preprints/{folder}/build/paper.tex": b"\\documentclass{article}\\begin{document}\\input{sections/03-topology}\\end{document}",
        old_raw + f"preprints/{folder}/build/sections/03-topology.tex": b"\\begin{lemma}\\label{top:negative-doubles}x\\end{lemma}",
        old_raw + "CONTENTS.md": CONTENTS,
    }))
    bundle = sources.fetch_bundle({"kind": "url", "url": f"https://github.com/openai/math/tree/main/preprints/{folder}"},
                                  tmp_path / "src", workspace=tmp_path, fetcher=fetcher)
    assert sorted(item["path"] for item in bundle.files()) == ["paper.tex", "sections/03-topology.tex"]
    upstream = bundle.upstream["openai_math"]
    assert upstream["withdrawn"] and upstream["commit"] == OLD and upstream["withdrawn_on"] == "October 6, 2026"
    assert bundle.file_base_url == f"https://github.com/openai/math/blob/{OLD}/preprints/{folder}/build"
    assert "sign error" in upstream["withdrawal_notice"]
    assert bundle.canonical_id == f"github:openai/math@{OLD}:preprints/{folder}"
    assert any("Withdrawn by its authors on October 6, 2026" in warning for warning in bundle.warnings)
    assert bundle.readme_excluded
    # The notice never becomes part of the analyzed source.
    assert all(b"sign error" not in (tmp_path / "src" / item["path"]).read_bytes() for item in bundle.files())


def test_a_link_pinned_before_a_withdrawal_still_reports_it(tmp_path):
    folder = "Weil-Paper-September-18-2026"
    old_raw = f"https://raw.githubusercontent.com/openai/math/{OLD}/"
    fetcher = FakeFetcher(_openai_routes({
        old_raw + f"preprints/{folder}/README.md": b"# [Weil classes on eightfolds](paper.pdf)\n\n**Date:** September 18, 2026\n",
        RAW + f"preprints/{folder}/README.md": WITHDRAWN,
        old_raw + f"preprints/{folder}/build/paper.tex": b"\\documentclass{article}\\begin{document}x\\end{document}",
        old_raw + "CONTENTS.md": CONTENTS,
    }))
    bundle = sources.fetch_bundle({"kind": "url", "url": f"https://github.com/openai/math/tree/{OLD}/preprints/{folder}"},
                                  tmp_path / "src", workspace=tmp_path, fetcher=fetcher)
    upstream = bundle.upstream["openai_math"]
    assert upstream["withdrawn"] and upstream["commit"] == OLD
    assert upstream["notice_url"] == f"https://github.com/openai/math/blob/{SHA}/preprints/{folder}/README.md"
    assert any(warning.startswith("Withdrawn by its authors") for warning in bundle.warnings)
    assert [item["path"] for item in bundle.files()] == ["paper.tex"]
    assert bundle.version == "September 18, 2026"


def test_the_withdrawal_lookup_for_a_pinned_link_is_best_effort():
    folder = "Weil-Paper-September-18-2026"
    old_raw = f"https://raw.githubusercontent.com/openai/math/{OLD}/"
    fetcher = FakeFetcher({
        "https://github.com/openai/math.git/info/refs?service=git-upload-pack":
            FetchError("The site did not respond in time", kind="timeout"),
        old_raw + f"preprints/{folder}/README.md": b"# [Weil classes on eightfolds](paper.pdf)\n",
    })
    resolved = sources.resolve(f"https://github.com/openai/math/tree/{OLD}/preprints/{folder}", fetcher=fetcher)
    assert resolved.ok and resolved.canonical_id == f"github:openai/math@{OLD}:preprints/{folder}"
    assert not any("Withdrawn" in warning for warning in resolved.warnings)


def test_openai_math_root_resolves_to_choices():
    fetcher = FakeFetcher(_openai_routes({RAW + "CONTENTS.md": CONTENTS}))
    resolved = sources.resolve("https://github.com/openai/math", fetcher=fetcher)
    assert resolved.ok and [choice["family"] for choice in resolved.choices] == ["017", "032"]
    first = resolved.choices[0]
    assert first["title"] == "The irrationality exponent of π is 2" and first["lean_doc"] == "lean/docs/017.md"
    assert first["manuscripts"][0]["url"] == f"https://github.com/openai/math/tree/{SHA}/preprints/Pi-Paper-September-24-2026"
    payload = resolved.as_dict()
    assert payload["choices"] and payload["format"] == ""
    with pytest.raises(SourceError, match="Pick one manuscript"):
        sources.fetch_bundle({"kind": "url", "url": "https://github.com/openai/math"}, Path("/nonexistent/x"),
                             workspace=Path("/nonexistent"), fetcher=fetcher)


def test_https_links_are_members_only_and_web_pages_are_refused(tmp_path):
    fetcher = FakeFetcher({"https://example.org/p.pdf": b"%PDF-1.7 x", "https://example.org/page": b"<html><body>hi</body></html>"})
    with pytest.raises(SourceError) as error:
        sources.fetch_bundle({"kind": "url", "url": "https://example.org/p.pdf"}, tmp_path / "a",
                             workspace=tmp_path, role="guest", fetcher=fetcher)
    assert error.value.kind == "forbidden" and not (tmp_path / "a").exists()
    bundle = sources.fetch_bundle({"kind": "url", "url": "https://example.org/p.pdf"}, tmp_path / "b",
                                  workspace=tmp_path, fetcher=fetcher)
    assert [item["path"] for item in bundle.files()] == ["paper.pdf"]
    with pytest.raises(SourceError, match="web page"):
        sources.fetch_bundle({"kind": "url", "url": "https://example.org/page"}, tmp_path / "c",
                             workspace=tmp_path, fetcher=fetcher)
    resolved = sources.resolve("https://example.org/p.pdf", role="guest", fetcher=fetcher)
    assert not resolved.ok and "sign in" in resolved.error


def test_paste_format_guess_and_limits(tmp_path):
    bundle = sources.bundle_from_paste("\\begin{lemma}a\\end{lemma}", tmp_path / "a")
    assert [item["path"] for item in bundle.files()] == ["pasted.tex"]
    bundle = sources.bundle_from_paste("## Lemma\n**Theorem 1.** x", tmp_path / "b")
    assert [item["path"] for item in bundle.files()] == ["pasted.md"]
    bundle = sources.bundle_from_paste("Theorem 1. x", tmp_path / "c", fmt="txt")
    assert [item["path"] for item in bundle.files()] == ["pasted.txt"]
    with pytest.raises(SourceError, match="20,000"):
        sources.bundle_from_paste("x" * 20_001, tmp_path / "d", role="guest")
    with pytest.raises(SourceError):
        sources.bundle_from_paste("   ", tmp_path / "e")
    with pytest.raises(SourceError):
        sources.bundle_from_paste("a\x00b", tmp_path / "f")


def _attachments(workspace: Path, items: list[tuple[str, bytes]]) -> list[dict]:
    folder = workspace / "attachments"
    folder.mkdir(parents=True)
    records = []
    for index, (name, data) in enumerate(items):
        suffix = "".join(Path(name).suffixes)
        path = f"attachments/{index:032x}{suffix}"
        (workspace / path).write_bytes(data)
        records.append({"id": f"{index:032x}", "name": name, "size": len(data), "type": "x", "path": path})
    (workspace / "attachments.json").write_text(json.dumps(records))
    return records


def test_uploads_are_copied_and_archives_expanded(tmp_path):
    workspace = tmp_path / "ws"
    _attachments(workspace, [("paper.tar.gz", targz({"main.tex": TEX, "tool.py": b"x"})),
                             ("extra.bib", b"@article{a,title={T}}")])
    bundle = sources.bundle_from_uploads(workspace, workspace / "analysis" / "source")
    assert sorted(item["path"] for item in bundle.files()) == ["extra.bib", "paper/main.tex"]
    assert bundle.computational == ["tool.py"]
    guest_ws = tmp_path / "guest"
    _attachments(guest_ws, [("paper.zip", b"PK\x05\x06" + b"\0" * 18)])
    with pytest.raises(SourceError, match="text files only"):
        sources.bundle_from_uploads(guest_ws, guest_ws / "analysis" / "source", role="guest")


def test_upload_manifest_paths_must_stay_in_attachments(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.tex").write_bytes(TEX)
    (workspace / "attachments").mkdir()
    (workspace / "attachments.json").write_text(json.dumps([{"id": "x", "name": "a.tex", "path": "../secret.tex"}]))
    with pytest.raises(SourceError, match="invalid path"):
        sources.bundle_from_uploads(workspace, workspace / "analysis" / "source")


def test_resolve_never_raises_and_reports_fetch_errors():
    fetcher = FakeFetcher({})
    resolved = sources.resolve("https://github.com/o/r/blob/main/x.tex", fetcher=fetcher)
    assert not resolved.ok and "not found" in resolved.error
    resolved = sources.resolve("not a link", fetcher=fetcher)
    assert not resolved.ok and resolved.connector is None
    limited = FakeFetcher({sources.ARXIV_API.format(id="2302.05537"): FetchError("busy", kind="rate_limited", status=429)})
    resolved = sources.resolve("2302.05537", fetcher=limited, sleep=lambda _s: None)
    assert not resolved.ok and "busy" in resolved.error


def test_arxiv_requests_reserve_slots_without_holding_a_lock_and_share_them(tmp_path, monkeypatch):
    import threading

    # A slow arXiv answer no longer blocks other requests behind a lock: each
    # request reserves its own start, 3 s after the previous one.
    clock = Clock()
    gate, entered = threading.Event(), threading.Event()

    class SlowFetcher(FakeFetcher):
        def get(self, url, **kwargs):
            if not entered.is_set():
                entered.set()
                gate.wait(10)
            return super().get(url, **kwargs)

    fetcher = SlowFetcher({sources.ARXIV_API.format(id="2302.05537"): atom("2302.05537v2")})
    first = threading.Thread(target=sources.resolve, args=("2302.05537",),
                             kwargs={"fetcher": fetcher, "sleep": clock.sleep, "clock": clock})
    first.start()
    assert entered.wait(10)
    second = sources.resolve("2302.05537", fetcher=fetcher, sleep=clock.sleep, clock=clock)
    gate.set()
    first.join(10)
    assert second.ok and clock.sleeps == [3.0]
    # Across processes: the next free slot lives in a small shared file.
    monkeypatch.setattr(sources, "ARXIV_SLOT_PATH", tmp_path / "slot")
    monkeypatch.setattr(sources, "_ARXIV_LAST", [0.0])
    waits = [sources._reserve_arxiv_slot(lambda: 1e9, None) for _ in range(3)]
    assert waits[0] == 0 and 2.9 < waits[1] < 3.1 and 5.9 < waits[2] < 6.1
    monkeypatch.setattr(sources, "_ARXIV_LAST", [0.0])  # as if another process
    assert 8.9 < sources._reserve_arxiv_slot(lambda: 1e9, None) < 9.1
    with pytest.raises(SourceError, match="pause"):
        sources._reserve_arxiv_slot(lambda: 1e9, safe_fetch.Budget(seconds=5))


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/o/r/tree/main/x%2F..%2F..%2F..%2Fuser",
        "https://github.com/o/r/blob/main/a%5C..%5Cb.tex",
        "https://github.com/../../tree/main/docs",
        "https://github.com/o/..../blob/main/x.tex",
        "https://github.com/o/r/blob/main/%2E%2E/x.tex",
        "https://raw.githubusercontent.com/o/r/main/x%2F../y.tex",
        "https://api.github.com/repos/o/private/tarball/main",
        "https://api.github.com/user",
        "https://codeload.github.com/o/r/tar.gz/main",
        "https://uploads.github.com/x",
    ],
)
def test_github_links_cannot_reach_other_api_paths(value):
    info = sources.classify(value)
    assert info["ok"] is False and info["error"]


def test_only_the_connectors_own_github_calls_may_use_the_token(tmp_path):
    too_large = FetchError("The file exceeds its size limit", kind="too_large")
    tree = {"tree": [{"path": "main.tex", "type": "blob", "mode": "100644", "size": len(TEX)}]}
    fetcher = FakeFetcher({
        "https://github.com/o/r.git/info/refs?service=git-upload-pack": too_large,
        "https://api.github.com/repos/o/r/commits/main": json.dumps({"sha": SHA}).encode(),
        f"https://api.github.com/repos/o/r/git/trees/{SHA}:papers/x?recursive=1": json.dumps(tree).encode(),
        f"https://raw.githubusercontent.com/o/r/{SHA}/papers/x/main.tex": TEX,
    })
    sources.fetch_bundle({"kind": "url", "url": "https://github.com/o/r/tree/main/papers/x"}, tmp_path / "s",
                         workspace=tmp_path, fetcher=fetcher)
    assert fetcher.api_calls == ["https://api.github.com/repos/o/r/commits/main",
                                 f"https://api.github.com/repos/o/r/git/trees/{SHA}:papers/x?recursive=1"]
    assert all("api.github.com" in call for call in fetcher.api_calls)


def test_conflicting_paths_are_skipped_not_fatal(tmp_path):
    base = f"https://raw.githubusercontent.com/o/r/{SHA}/"
    fetcher = FakeFetcher(_github_routes({
        base + "main.tex": b"\\documentclass{article}\\begin{document}\\input{sec}\\input{sec.tex/x}\\end{document}",
        base + "sec.tex": b"\\section{S}",
        base + "sec.tex/x.tex": b"\\section{X}",
    }))
    bundle = sources.fetch_bundle({"kind": "url", "url": "https://github.com/o/r/blob/main/main.tex"},
                                  tmp_path / "src", workspace=tmp_path, fetcher=fetcher)
    assert sorted(item["path"] for item in bundle.files()) == ["main.tex", "sec.tex"]
    assert {"path": "sec.tex/x.tex", "reason": "conflicting path"} in bundle.skipped
    archive = targz({"main.tex": TEX, "sec.tex": b"x", "sec.tex/x.tex": b"y"})
    workspace = tmp_path / "ws"
    _attachments(workspace, [("paper.zip", archive), ("paper.tar.gz", targz({"main.tex": TEX}))])
    bundle = sources.bundle_from_uploads(workspace, workspace / "analysis" / "source")
    assert sorted(item["path"] for item in bundle.files()) == ["paper-2/main.tex", "paper/main.tex", "paper/sec.tex"]
    assert any(item["reason"] == "conflicting path" for item in bundle.skipped)


def test_the_token_is_never_used_to_list_a_repository_anyone_cannot_read(tmp_path):
    # A full-SHA link skips ref resolution; the anonymous ref advertisement is
    # still required before the (token-bearing) tree call.
    fetcher = FakeFetcher({
        f"https://api.github.com/repos/victim/private/git/trees/{SHA}:docs?recursive=1": json.dumps({"tree": [
            {"path": "secret-plan.tex", "type": "blob", "mode": "100644", "size": 10}]}).encode(),
    })
    with pytest.raises(SourceError, match="not found") as error:
        sources.fetch_bundle({"kind": "url", "url": f"https://github.com/victim/private/tree/{SHA}/docs"},
                             tmp_path / "s", workspace=tmp_path, fetcher=fetcher)
    assert fetcher.api_calls == [] and "secret" not in str(error.value)
