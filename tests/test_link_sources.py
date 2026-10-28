"""Offline tests: papers linked in a proof run's problem or follow-up are read
for the model (agent_monitor.link_sources), against a scripted fetcher."""
from __future__ import annotations

import base64
import gzip
import json

import pytest

from agent_monitor import attachments, link_sources
from agent_monitor.analyze import sources
from agent_monitor.safe_fetch import FetchError
from tests.test_analyze_sources import OAI, FakeFetcher, atom, targz
from tests.test_research_attachments import isolated_jobs  # noqa: F401 - fixture

MAIN = (b"\\documentclass{article}\n% a private note\n\\usepackage[margin=1in]{geometry}\n"
        b"\\title{Relative prisms}\n\\newcommand{\\Prism}{\n  \\mathbbl{\\Delta}}\n"
        b"\\begin{document}\n\\input{sec/intro}\n"
        b"\\begin{theorem}Main\\end{theorem}\n\\end{document}\n")
INTRO = b"\\section{Intro} Prismatic cohomology % hidden\n"


@pytest.fixture(autouse=True)
def _no_arxiv_wait(monkeypatch):
    monkeypatch.setattr(sources, "_ARXIV_LAST", [0.0])
    monkeypatch.setattr(sources, "ARXIV_SLOT_PATH", None)


def arxiv_routes(ident="2310.12770", version="v2", eprint=None):
    return {
        sources.ARXIV_API.format(id=ident): atom(ident + version, title="Prismatic cohomology relative to delta rings"),
        sources.ARXIV_OAI.format(id=ident): OAI,
        sources.ARXIV_EPRINT.format(id=ident + version): eprint if eprint is not None else targz(
            {"main.tex": MAIN, "sec/intro.tex": INTRO, "main.bbl": b"\\bibitem{B} Bhatt."}),
    }


def fetcher_for(routes):
    fake = FakeFetcher(routes)

    def fetch(source, dest, **kwargs):
        return sources.fetch_bundle(source, dest, fetcher=fake, sleep=lambda _s: None, **kwargs)

    fetch.fake = fake
    return fetch


def decoded(item):
    return base64.b64decode(item["data"])


def test_find_links_takes_urls_and_arxiv_ids_from_prose():
    text = ("Prismatic cohomology relative to a delta ring is introduced here https://arxiv.org/pdf/2310.12770. "
            "See also (arXiv:math/0701630v2), arxiv.org/abs/2007.03528, www.example.org/page and "
            "https://arxiv.org/pdf/2310.12770.")
    assert link_sources.find_links(text) == [
        "https://arxiv.org/pdf/2310.12770", "arxiv.org/abs/2007.03528", "math/0701630v2"]
    assert link_sources.find_links("no links, version 2.0, e-mail a@b.c") == []


def test_an_arxiv_link_becomes_one_flattened_tex_attachment():
    fetch = fetcher_for(arxiv_routes())
    out = link_sources.fetch_links("Compute it; reference: https://arxiv.org/pdf/2310.12770", fetch_bundle=fetch)
    assert [item["name"] for item in out.items] == ["arXiv-2310.12770v2.tex"]
    text = decoded(out.items[0]).decode()
    # Include order, the included file in place after the root, comments gone, references last.
    assert text.index("% ===== main.tex =====") < text.index("% ===== sec/intro.tex =====") < text.index("main.bbl")
    assert "Prismatic cohomology" in text and "private note" not in text and "hidden" not in text
    # The preamble keeps the title and macros (with their continuation lines), not the layout.
    assert "\\title{Relative prisms}" in text and "\\mathbbl{\\Delta}}" in text
    assert "usepackage" not in text and "documentclass" not in text
    assert out.read == {"arxiv:2310.12770": "arXiv-2310.12770v2.tex"}
    assert out.notes == [
        "🔗 Read from the link https://arxiv.org/pdf/2310.12770 — “Prismatic cohomology relative to delta rings”: "
        "attached as arXiv-2310.12770v2.tex (TeX source)."]
    # The attachment passes the same checks as an upload.
    assert attachments.validate(out.items)[0][0]["type"] == "text/plain"


def test_a_pdf_only_arxiv_paper_is_attached_as_its_pdf():
    fetch = fetcher_for(arxiv_routes(ident="1002.0105", version="v1", eprint=b"%PDF-1.5 a paper"))
    out = link_sources.fetch_links("tell me main results from https://arxiv.org/pdf/1002.0105", fetch_bundle=fetch)
    assert [item["name"] for item in out.items] == ["arXiv-1002.0105v1.pdf"]
    assert decoded(out.items[0]).startswith(b"%PDF-")
    assert "PDF; formulas extracted from a PDF may be garbled" in out.notes[0]


def test_web_pages_and_folders_are_not_fetched_and_say_nothing():
    fetch = fetcher_for({})
    out = link_sources.fetch_links(
        "see https://en.wikipedia.org/wiki/Zeta_function and https://github.com/o/r/tree/main/paper",
        fetch_bundle=fetch)
    assert out.items == []
    assert out.notes == [
        "🔗 Not read: https://en.wikipedia.org/wiki/Zeta_function, https://github.com/o/r/tree/main/paper. "
        "Ansatze reads arXiv papers and direct links to a PDF or TeX file, not web pages; to give the model a "
        "document, attach the file."]
    assert fetch.fake.calls == []


def test_direct_pdf_and_github_tex_links_are_read():
    routes = {"https://example.org/papers/zeta.pdf": b"%PDF-1.4 zeta"}
    out = link_sources.fetch_links("use https://example.org/papers/zeta.pdf.", fetch_bundle=fetcher_for(routes))
    assert [item["name"] for item in out.items] == ["zeta.pdf"]


def test_a_link_that_cannot_be_read_never_refuses_and_says_why():
    routes = {sources.ARXIV_API.format(id="2310.12770"): FetchError("arXiv is busy (HTTP 503)", kind="unavailable",
                                                                    status=503)}
    out = link_sources.fetch_links("ref https://arxiv.org/abs/2310.12770", fetch_bundle=fetcher_for(routes))
    assert out.items == [] and out.read == {}
    assert len(out.notes) == 1
    assert out.notes[0].startswith("🔗 Could not read https://arxiv.org/abs/2310.12770: ")
    assert out.notes[0].endswith("The model has not seen this paper; download the PDF and attach it to give it to the model.")


def test_a_fetch_that_raises_anything_is_reported_not_raised():
    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    out = link_sources.fetch_links("arXiv:2310.12770", fetch_bundle=broken)
    assert out.items == [] and "Could not read 2310.12770: boom" in out.notes[0]


def test_links_already_read_for_the_run_are_not_fetched_again():
    fetch = fetcher_for(arxiv_routes())
    out = link_sources.fetch_links("You have not used the reference https://arxiv.org/abs/2310.12770",
                                   known={"arxiv:2310.12770": "arXiv-2310.12770v2.tex"},
                                   fetch_bundle=fetch)
    assert out.items == [] and fetch.fake.calls == []
    assert out.notes == ["🔗 https://arxiv.org/abs/2310.12770 was already read for this run "
                         "(attached as arXiv-2310.12770v2.tex)."]


def test_the_paper_must_fit_beside_the_users_own_uploads():
    uploads = [{"name": f"f{i}.txt", "data": base64.b64encode(b"x").decode()} for i in range(attachments.MAX_FILES)]
    out = link_sources.fetch_links("arXiv:2310.12770", existing=uploads, fetch_bundle=fetcher_for(arxiv_routes()))
    assert out.items == []
    assert "at most 8 files" in out.notes[0]


def test_at_most_three_paper_links_per_message():
    routes = {}
    for n in range(4):
        routes.update(arxiv_routes(ident=f"2310.1277{n}", version="v1", eprint=gzip.compress(MAIN)))
    text = " ".join(f"arXiv:2310.1277{n}" for n in range(4))
    out = link_sources.fetch_links(text, fetch_bundle=fetcher_for(routes))
    assert len(out.items) == 3
    assert out.notes[0] == "🔗 Only the first 3 paper links of a message are read; attach the others as files."


def test_remember_and_read_known_round_trip(tmp_path):
    link_sources.remember(tmp_path, {"a": "x.tex"})
    link_sources.remember(tmp_path, {"b": "y.pdf"})
    assert link_sources.read_known(tmp_path) == {"a": "x.tex", "b": "y.pdf"}
    (tmp_path / link_sources.KNOWN_FILENAME).write_text("not json")
    assert link_sources.read_known(tmp_path) == {}
    assert json.loads(json.dumps(link_sources.read_known(None))) == {}


def test_http_run_and_follow_up_read_a_linked_paper_once(isolated_jobs, monkeypatch):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from agent_monitor import console_server as console, jobs

    real_fetch = sources.fetch_bundle
    fake = FakeFetcher(arxiv_routes())
    monkeypatch.setattr(sources, "fetch_bundle",
                        lambda source, dest, **kw: real_fetch(source, dest, fetcher=fake, sleep=lambda _s: None, **kw))
    monkeypatch.setattr(console, "BASE_PATH", "/proof-deadbeef")
    monkeypatch.setattr(console, "PUBLIC_MODE", False)
    monkeypatch.setattr(console.Handler, "_current_user", lambda self: {"id": 7})
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path, body):
        conn = http.client.HTTPConnection(*server.server_address, timeout=10)
        conn.request("POST", "/proof-deadbeef" + path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        result = conn.getresponse()
        data = result.read()
        conn.close()
        return result.status, json.loads(data)

    try:
        status, job = post("/api/runs", {"engine": "plain",
                                         "problem_text": "Compute the complex, see https://arxiv.org/pdf/2310.12770"})
        assert status == 200, job
        rid = job["run_id"]
        ws = jobs.workspace_dir(rid)
        files = attachments.list_files(ws)
        assert [item["name"] for item in files] == ["arXiv-2310.12770v2.tex"]
        assert "Prismatic cohomology" in attachments.file_path(ws, files[0]).read_text()
        chat = jobs.list_chat(rid)
        assert any(m["content"].startswith("🔗 Read from the link https://arxiv.org/pdf/2310.12770") for m in chat)
        assert link_sources.read_known(ws) == {"arxiv:2310.12770": "arXiv-2310.12770v2.tex"}
        calls = len(fake.calls)
        jobs._JOBS[job["job_id"]]["status"] = "finished"
        status, reply = post(f"/api/runs/{rid}/chat",
                             {"message": "You have not used the reference https://arxiv.org/abs/2310.12770"})
        assert status == 200, reply
        assert len(fake.calls) == calls  # not fetched again
        assert len(attachments.list_files(ws)) == 1
        assert reply["messages"][-1]["content"].startswith("🔗 https://arxiv.org/abs/2310.12770 was already read")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_prose_formatting_around_links_is_not_part_of_them():
    text = ("**https://arxiv.org/abs/2401.12345**, https://arxiv.org/abs/2401.12346。"
            "https://arxiv.org/abs/2401.12347,https://arxiv.org/abs/2401.12348 arxiv:2401.1234567")
    assert link_sources.find_links(text) == [
        "https://arxiv.org/abs/2401.12345", "https://arxiv.org/abs/2401.12346",
        "https://arxiv.org/abs/2401.12347", "https://arxiv.org/abs/2401.12348"]


def test_one_arxiv_paper_linked_two_ways_is_fetched_once():
    fetch = fetcher_for(arxiv_routes())
    out = link_sources.fetch_links("https://arxiv.org/abs/2310.12770 and arXiv:2310.12770v2", fetch_bundle=fetch)
    assert len(out.items) == 1 and out.read == {"arxiv:2310.12770": "arXiv-2310.12770v2.tex"}
    again = link_sources.fetch_links("arXiv:2310.12770v1", known=out.read, fetch_bundle=fetch)
    assert again.items == [] and "already read" in again.notes[0]


def test_a_run_at_its_attachment_limit_gets_a_note_not_a_refusal():
    full = [{"size": 1} for _ in range(attachments.MAX_RUN_FILES)]
    out = link_sources.fetch_links("arXiv:2310.12770", run_files=full, fetch_bundle=fetcher_for(arxiv_routes()))
    assert out.items == [] and "this run has reached its attachment limit" in out.notes[0]


def test_flatten_works_through_a_symlinked_scratch_dir(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setattr(link_sources.tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "link" / "s") if not (real / "s").mkdir() else "")
    out = link_sources.fetch_links("arXiv:2310.12770", fetch_bundle=fetcher_for(arxiv_routes()))
    assert [item["name"] for item in out.items] == ["arXiv-2310.12770v2.tex"]
    assert "main.bbl" in decoded(out.items[0]).decode()
