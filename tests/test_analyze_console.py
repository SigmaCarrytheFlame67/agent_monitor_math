"""The console's Analyze HTTP API: validation by role, kill switches, caps,
link previews, the analysis graph, re-checks, Lean targets, compile denial,
continuation refusal and the Sandbox report.

Job creation is stubbed for the run-start tests; the analysis artifacts are
built by the real runner in-process against a local fake provider.  Nothing
here touches the network.
"""
from __future__ import annotations

import base64
import io
import json
import threading
import zipfile
from collections import OrderedDict
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("pylatexenc")

from agent_monitor import analyze_runs, auth, jobs, lean_verify  # noqa: E402
from agent_monitor import console_server as console  # noqa: E402
from agent_monitor.analyze import request as analysis_request  # noqa: E402
from agent_monitor.analyze import sources as analysis_sources  # noqa: E402
from agent_monitor.runners import analyze_runner  # noqa: E402
from agent_monitor.safe_fetch import FetchResult  # noqa: E402

from test_analyze_runner import PAPER, Provider, default_script, own_key_env  # noqa: E402
from test_console_guest_access import (  # noqa: E402,F401  (fixture import)
    APP_MOUNT,
    _issue_guest,
    guest_console,
    request,
    running_server,
)
from test_member_accounts import _member, member_console  # noqa: E402,F401

ARXIV = "https://arxiv.org/abs/2302.05537"
PDF_LINK = "https://example.org/papers/result.pdf"


@pytest.fixture()
def analyze_console(member_console, tmp_path, monkeypatch):
    for name in (analyze_runs.DAILY_CAP_ENV, analyze_runs.CONCURRENCY_ENV, analyze_runs.GUEST_DAILY_CAP_ENV,
                 analyze_runs.GUEST_CONCURRENCY_ENV, "AGENT_MONITOR_ANALYZE_ENABLED",
                 "AGENT_MONITOR_ANALYZE_GUESTS", "ANALYZE_CMD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(analyze_runs.GATE, "usage_path", tmp_path / "analyze_usage.json")
    # Previewed titles label later analyses: keep them inside each test.
    monkeypatch.setattr(analyze_runs, "_RESOLVED_TITLES", OrderedDict())
    analyze_runs.GATE.reset()
    windows = (console._GUEST_ANALYZE_WINDOWS, console._GUEST_ANALYZE_CLIENT_WINDOWS,
               console._ANALYZE_RESOLVE_WINDOWS, console._ANALYZE_RESOLVE_CLIENT_WINDOWS,
               console._GUEST_RECHECK_WINDOWS, console._GUEST_RECHECK_CLIENT_WINDOWS,
               console._GUEST_DERIVED_WINDOWS, console._GUEST_DERIVED_ACTIVE_USERS,
               console._GUEST_DERIVED_CLIENT_WINDOWS)
    for window in windows:
        window.clear()
    console._GUEST_DERIVED_GLOBAL_WINDOW.clear()
    yield member_console
    analyze_runs.GATE.reset()
    for window in windows:
        window.clear()
    console._GUEST_DERIVED_GLOBAL_WINDOW.clear()


def _admin_cookie() -> str:
    _user, token = auth.login_and_create_session("owner@example.test", "long-enough-password")
    return f"{auth.SESSION_COOKIE}={token}"


def _post(port, path, cookie, body, headers=None):
    status, response_headers, raw = request(port, "POST", f"{APP_MOUNT}{path}", body=body,
                                            headers={"Cookie": cookie, **(headers or {})})
    return status, response_headers, json.loads(raw or "{}")


def _get(port, path, cookie):
    status, _headers, raw = request(port, "GET", f"{APP_MOUNT}{path}", headers={"Cookie": cookie})
    return status, json.loads(raw or "{}")


def _analyze(source, **extra):
    return {"run_kind": "analyze", "source": source, **extra}


# ---------------------------------------------------------------------------
# starting an analysis
# ---------------------------------------------------------------------------


def test_guest_starts_an_analysis_of_an_arxiv_link(analyze_console):
    with running_server() as port:
        _guest, cookie, _ = _issue_guest(port)
        status, _, body = _post(port, "/api/runs", cookie,
                                _analyze({"url": ARXIV}, analyze={"focus": "Lemma 3"}))
        formal, _, _ = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}, formalize=True))
    assert status == 200, body
    started = analyze_console["started"]
    call = started[0]
    assert call["engine"] == "analyze" and call["run_kind"] == "analyze"
    assert call["source"] == {"url": ARXIV} and call["analyze"] == {"focus": "Lemma 3"}
    # Analyze formalizes only when asked.
    assert call["formalize"] is False and call["use_subagents"] is False
    assert body["guest_policy"]["analyze"]["enabled"] is True
    # One Analyze per guest per hour.
    assert formal == 429 and len(started) == 1


def test_formalize_is_passed_only_when_true(analyze_console):
    with running_server() as port:
        _user, cookie = _member(port)
        status, _, _ = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}, formalize=True))
    assert status == 200 and analyze_console["started"][0]["formalize"] is True


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ({"url": PDF_LINK}, analyze_runs.GUEST_LINK_REFUSAL),
        ({"url": "https://github.com/openai/math"}, "Pick one manuscript of openai/math"),
        ({"url": "ftp://example.org/x.tex"}, "https://"),
        ({"text": "x" * 20_001}, "limited to 20,000 characters for guests"),
        ({}, "Paste a link or a manuscript"),
        ({"url": ARXIV, "text": "\\section{x}"}, "one source at a time"),
        ({"text": "body", "format": "html"}, "tex, md or txt"),
        ({"text": "body", "extra": 1}, "only url, text and format"),
    ],
)
def test_guest_sources_are_validated_with_plain_messages(analyze_console, source, message):
    with running_server() as port:
        _guest, cookie, _ = _issue_guest(port)
        status, _, body = _post(port, "/api/runs", cookie, _analyze(source))
    assert status == 400 and message in body["error"]
    assert analyze_console["started"] == []


def _upload(name: str, data: bytes) -> dict:
    return {"name": name, "data": base64.b64encode(data).decode()}


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.tex", PAPER)
    return buffer.getvalue()


def test_members_may_send_more_than_guests(analyze_console):
    with running_server() as port:
        _guest, guest_cookie, _ = _issue_guest(port)
        guest_pdf = _post(port, "/api/runs", guest_cookie,
                          _analyze(None, attachments=[_upload("paper.pdf", b"%PDF-1.7 test")]))
        guest_zip = _post(port, "/api/runs", guest_cookie,
                          _analyze(None, attachments=[_upload("paper.zip", _zip())]))
        _user, cookie = _member(port)
        link = _post(port, "/api/runs", cookie, _analyze({"url": PDF_LINK}))
        paste = _post(port, "/api/runs", cookie, _analyze({"text": "\\section{A}" + "x" * 30_000}))
        bundle = _post(port, "/api/runs", cookie, _analyze(None, attachments=[_upload("paper.zip", _zip())]))
    assert guest_pdf[0] == 400 and "text files only" in guest_pdf[2]["error"]
    assert guest_zip[0] == 400
    assert (link[0], paste[0], bundle[0]) == (200, 200, 200), (link, paste, bundle)
    calls = analyze_console["started"]
    assert calls[0]["source"] == {"url": PDF_LINK}
    assert len(calls[1]["source"]["text"]) == 30_011
    assert calls[2]["source"] is None and calls[2]["attachments"][0]["name"] == "paper.zip"


def test_run_kind_and_engine_must_agree(analyze_console):
    with running_server() as port:
        _user, cookie = _member(port)
        bad_kind = _post(port, "/api/runs", cookie, {"run_kind": "explore", "engine": "kimi", "problem_text": "x"})
        prove_with_analyze = _post(port, "/api/runs", cookie, {"engine": "analyze", "problem_text": "x"})
        analyze_with_kimi = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}, engine="kimi"))
    assert bad_kind[0] == 400 and "run_kind" in bad_kind[2]["error"]
    assert prove_with_analyze[0] == 400 and "Analyze mode" in prove_with_analyze[2]["error"]
    assert analyze_with_kimi[0] == 400 and '"analyze" engine' in analyze_with_kimi[2]["error"]
    assert analyze_console["started"] == []


def test_kill_switches_hide_and_refuse_analyze(analyze_console, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_ANALYZE_GUESTS", "0")
    with running_server() as port:
        _guest, guest_cookie, _ = _issue_guest(port)
        refused = _post(port, "/api/runs", guest_cookie, _analyze({"url": ARXIV}))
        _status, listing = _get(port, "/api/engines", guest_cookie)
        _user, cookie = _member(port)
        member_ok = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}))
        monkeypatch.setenv("AGENT_MONITOR_ANALYZE_ENABLED", "0")
        member_refused = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}))
        _status, member_listing = _get(port, "/api/engines", cookie)
        preview = _post(port, "/api/analyze/resolve", cookie, {"url": ARXIV})
    assert refused[0] == 403 and refused[2]["error"] == analyze_runs.GUEST_DISABLED_MESSAGE
    analyze = next(engine for engine in listing["engines"] if engine["id"] == "analyze")
    assert analyze["requires_login"] is True and analyze["analyze"]["enabled"] is False
    assert member_ok[0] == 200
    assert member_refused[0] == 403 and member_refused[2]["error"] == analyze_runs.DISABLED_MESSAGE
    assert all(engine["id"] != "analyze" for engine in member_listing["engines"])
    assert preview[0] == 403
    assert len(analyze_console["started"]) == 1


def test_sponsored_capacity_refuses_before_any_admission(analyze_console, monkeypatch):
    analyze_runs.GATE.reserve("busy-1", run_id="busy-1")
    analyze_runs.GATE.reserve("busy-2", run_id="busy-2")
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        busy = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}))
        analyze_runs.GATE.reset()
        monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "0")
        off = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}))
        # The two busy slots above were counted today; a cap of 2 is reached.
        monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "2")
        daily = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}))
    assert busy[0] == 429 and busy[1]["retry-after"] == "60"
    assert busy[2]["error"].startswith("Sponsored Analyze is busy")
    assert off[0] == 403 and off[2]["error"] == analyze_runs.GUEST_SPONSORED_OFF_MESSAGE
    assert daily[0] == 429 and daily[2]["error"] == analyze_runs.GUEST_DAILY_LIMIT_MESSAGE
    assert int(daily[1]["retry-after"]) >= 60
    assert analyze_console["started"] == []
    # Refusals spent none of the guest's run allowance.
    assert auth.guest_runs_started(guest["id"]) == 0


def test_a_refused_guest_analysis_gives_back_its_hourly_run_slots(analyze_console, monkeypatch):
    def refuse(**_kwargs):
        raise ValueError("The analysis could not start")

    monkeypatch.setattr(console.job_manager, "start_job", refuse)
    client = "203.0.113.9"
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        status, _, body = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}),
                                headers={"X-Forwarded-For": client})
    assert status == 400 and body["error"] == "The analysis could not start"
    assert auth.guest_runs_started(guest["id"]) == 0
    assert not console._GUEST_ANALYZE_WINDOWS.get(guest["id"])
    assert not console._GUEST_RUN_WINDOWS.get(guest["id"])
    assert not console._GUEST_RUN_CLIENT_WINDOWS.get(client)
    assert not console._GUEST_GLOBAL_RUN_WINDOW


def test_member_capacity_refusal_from_start_job_is_a_429(analyze_console, monkeypatch):
    def full(**_kwargs):
        raise analyze_runs.AnalyzeCapacityError(analyze_runs.DAILY_LIMIT_MESSAGE, retry_after=120)

    monkeypatch.setattr(console.job_manager, "start_job", full)
    with running_server() as port:
        _user, cookie = _member(port)
        status, headers, body = _post(port, "/api/runs", cookie, _analyze({"url": ARXIV}))
    assert status == 429 and body["error"] == analyze_runs.DAILY_LIMIT_MESSAGE
    assert headers["retry-after"] == "120"


def test_engine_listing_advertises_modes_and_each_viewers_policy(analyze_console):
    with running_server() as port:
        _guest, guest_cookie, _ = _issue_guest(port)
        _s, guest_listing = _get(port, "/api/engines", guest_cookie)
        _user, cookie = _member(port)
        _s, member_listing = _get(port, "/api/engines", cookie)
        _s, admin_listing = _get(port, "/api/engines", _admin_cookie())
    for listing in (guest_listing, member_listing, admin_listing):
        engines = {engine["id"]: engine for engine in listing["engines"]}
        assert engines["analyze"]["modes"] == ["analyze"]
        assert engines["kimi"]["modes"] == ["prove"] and engines["math_harness"]["modes"] == ["prove"]
    guest = next(e for e in guest_listing["engines"] if e["id"] == "analyze")
    assert not guest.get("requires_login")
    policy = guest["analyze"]
    assert policy["role"] == "guest" and policy["routes"] == ["sponsored_kimi"] and policy["enabled"] is True
    assert policy["caps"]["sponsored"] == {"max_nodes": 12, "max_calls": 16, "max_seconds": 2400}
    assert policy["caps"]["own_key"] is None and policy["runs_per_hour"] == 1
    assert policy["sources"]["links"] == ["arxiv", "github", "openai_math"]
    assert policy["sources"]["paste_chars"] == 20_000 and "zip" not in policy["sources"]["upload_extensions"]
    assert policy["formalize_label"] == "Statements compiled · proofs not attempted"
    assert policy["verdict_note"] == "checked by a model verifier — not a certificate"
    member = next(e for e in member_listing["engines"] if e["id"] == "analyze")
    assert member["auth_modes"] == ["api_key"] and member["subscription_supported"] is False
    assert member["analyze"]["routes"] == ["sponsored_kimi", "api_key"]
    assert member["analyze"]["caps"]["own_key"]["max_nodes"] == 120
    assert "https" in member["analyze"]["sources"]["links"]
    admin = next(e for e in admin_listing["engines"] if e["id"] == "analyze")
    assert "claude_subscription" in admin["analyze"]["routes"]
    assert admin["auth_modes"] == ["codex_subscription", "claude_subscription", "api_key"]


def test_missing_parser_packages_mark_analyze_as_needing_setup(monkeypatch):
    from agent_monitor import engines_registry

    monkeypatch.setattr(engines_registry, "analyze_missing_packages", lambda: ["pypdf"])
    analyze = next(e for e in engines_registry.list_engines() if e["id"] == "analyze")
    assert analyze["available"] is False and "pypdf" in analyze["health_detail"]
    with pytest.raises(ValueError, match="needs setup"):
        jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER},
                       user={"id": 1, "is_admin": True}, model="kimi-k3")


# ---------------------------------------------------------------------------
# link preview
# ---------------------------------------------------------------------------


class _FakeFetcher:
    """Answers the arXiv metadata request from a fixture (no network)."""

    calls: list[str] = []

    def get(self, url, **_kwargs):
        _FakeFetcher.calls.append(url)
        atom = (b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom' "
                b"xmlns:arxiv='http://arxiv.org/schemas/atom'><entry><id>http://arxiv.org/abs/2302.05537v2</id>"
                b"<title>Sums of evens</title><summary>s</summary></entry></feed>")
        return FetchResult(url=url, body=atom, kind="text", content_type="application/atom+xml")


def test_link_preview_returns_metadata_and_refuses_guests_generic_files(analyze_console, monkeypatch):
    monkeypatch.setattr(analyze_runs, "RESOLVE_FETCHER_FACTORY", _FakeFetcher)
    monkeypatch.setattr(analyze_runs, "RESOLVE_SLEEP", lambda _seconds: None)
    monkeypatch.setattr(analysis_sources, "ARXIV_SLOT_PATH", None)
    with running_server() as port:
        _guest, cookie, _ = _issue_guest(port)
        arxiv = _post(port, "/api/analyze/resolve", cookie, {"url": ARXIV})
        generic = _post(port, "/api/analyze/resolve", cookie, {"url": PDF_LINK})
        missing = _post(port, "/api/analyze/resolve", cookie, {})
    assert arxiv[0] == 200, arxiv
    preview = arxiv[2]
    assert preview["ok"] is True and preview["connector"] == "arxiv" and preview["title"] == "Sums of evens"
    assert preview["size_bytes"] is None and preview["format"] == "tex"
    assert set(preview) >= {"ok", "connector", "title", "version", "size_bytes", "format", "warnings"}
    assert generic[0] == 200 and generic[2]["ok"] is False and generic[2]["member_only"] is True
    assert generic[2]["error"] == analyze_runs.GUEST_LINK_REFUSAL
    assert missing[0] == 400
    assert all("example.org" not in url for url in _FakeFetcher.calls)


def test_link_previews_are_rate_limited_per_role(analyze_console, monkeypatch):
    monkeypatch.setattr(analyze_runs, "RESOLVE_FETCHER_FACTORY", _FakeFetcher)
    monkeypatch.setattr(analyze_runs, "RESOLVE_SLEEP", lambda _seconds: None)
    monkeypatch.setattr(analysis_sources, "ARXIV_SLOT_PATH", None)
    monkeypatch.setattr(analysis_sources, "ARXIV_POLITENESS_SECONDS", 0)
    monkeypatch.setattr(analysis_sources, "_ARXIV_LAST", [0.0])
    with running_server() as port:
        _guest, guest_cookie, _ = _issue_guest(port)
        guest_codes = [_post(port, "/api/analyze/resolve", guest_cookie, {"url": ARXIV})[0] for _ in range(11)]
        _user, cookie = _member(port)
        member_codes = [_post(port, "/api/analyze/resolve", cookie, {"url": ARXIV})[0] for _ in range(31)]
    assert guest_codes[:10] == [200] * 10 and guest_codes[10] == 429
    assert member_codes[:30] == [200] * 30 and member_codes[30] == 429


def test_link_previews_refused_without_a_lookup_never_count(analyze_console, monkeypatch):
    """A mistyped, half-typed or member-only link is refused at once and does
    not use up the previews of the links that need a lookup."""
    monkeypatch.setattr(analyze_runs, "RESOLVE_FETCHER_FACTORY", _FakeFetcher)
    monkeypatch.setattr(analyze_runs, "RESOLVE_SLEEP", lambda _seconds: None)
    monkeypatch.setattr(analysis_sources, "ARXIV_SLOT_PATH", None)
    monkeypatch.setattr(analysis_sources, "ARXIV_POLITENESS_SECONDS", 0)
    monkeypatch.setattr(analysis_sources, "_ARXIV_LAST", [0.0])
    _FakeFetcher.calls = []
    with running_server() as port:
        _guest, cookie, _ = _issue_guest(port)
        refused = [_post(port, "/api/analyze/resolve", cookie, {"url": url})
                   for url in ("not a link", "https://arxiv.org/abs/2302", "https://github.com/openai",
                               "http://example.org/paper.pdf", PDF_LINK) * 4]
        lookups = [_post(port, "/api/analyze/resolve", cookie, {"url": ARXIV})[0] for _ in range(11)]
    assert all(code == 200 and body["ok"] is False and body["error"] for code, _headers, body in refused)
    assert refused[4][2]["member_only"] is True
    assert lookups[:10] == [200] * 10 and lookups[10] == 429, lookups
    assert len(_FakeFetcher.calls) == 10, _FakeFetcher.calls


def test_a_previewed_title_labels_the_analysis_from_its_start(analyze_console, monkeypatch):
    monkeypatch.setattr(analyze_runs, "RESOLVE_FETCHER_FACTORY", _FakeFetcher)
    monkeypatch.setattr(analyze_runs, "RESOLVE_SLEEP", lambda _seconds: None)
    monkeypatch.setattr(analysis_sources, "ARXIV_SLOT_PATH", None)
    monkeypatch.setattr(analysis_sources, "ARXIV_POLITENESS_SECONDS", 0)
    monkeypatch.setattr(analysis_sources, "_ARXIV_LAST", [0.0])
    unseen = analyze_runs.normalize_request({"source": {"url": "https://arxiv.org/abs/2302.99999"}}, {"id": 5})
    assert unseen["label"] == "https://arxiv.org/abs/2302.99999"
    preview = analyze_runs.resolve_link(ARXIV, role="member")
    assert preview["ok"] is True, preview
    seen = analyze_runs.normalize_request({"source": {"url": ARXIV}}, {"id": 5})
    assert seen["label"] == "Sums of evens"
    assert analyze_runs.problem_text(seen["label"]) == "Analyze · Sums of evens"


# ---------------------------------------------------------------------------
# finished analyses: graph, sandbox, compile denial, continuation, re-check, Lean
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    server.seen, server.lock, server.script = [], threading.Lock(), default_script({"lem:sum": "gap"})
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _finished_analysis(run_id: str, owner_id: int, server, **fields) -> Path:
    workspace = jobs.workspace_dir(run_id)
    analysis_request.write(workspace, source={"kind": "paste", "text": PAPER}, caps={}, focus="",
                           formalize=False, role="member", route="api_key", model="deepseek-test",
                           reasoning_effort="default", sponsored=False)
    analyze_runner.run_analysis(workspace, analysis_request.load(workspace), env=own_key_env(server),
                                sink=lambda _e: None, sleep=lambda _s: None)
    (workspace / "analysis" / "source" / "secret-notes.tex").write_text("SOURCE-ONLY-MARKER")
    from agent_monitor.analyze import record as analyze_record

    record = {
        "run_id": run_id, "engine": "analyze", "run_kind": "analyze", "status": "finished",
        "workspace": str(workspace), "owner_id": owner_id, "guest": False, "formalize": False,
        "credential_source": "user", "auth_route": "api_key", "model": "deepseek-test",
        "problem_text": "Analyze · Sums of evens", "pipeline": jobs._pipeline_for("analyze"),
        "agents": [{"role": "agent", "output": "progress line"}], "edges": [], "totals": {},
        "analyze": analyze_record.run_block(workspace),
    }
    record.update(fields)
    jobs._write_run(record)
    return workspace


def test_analysis_graph_sandbox_and_run_payload(analyze_console, fake_provider):
    with running_server() as port:
        user, cookie = _member(port)
        _finished_analysis("analyze_view_1", user["id"], fake_provider)
        jobs.workspace_dir("plain_view_1")
        jobs._write_run({"run_id": "plain_view_1", "engine": "plain", "status": "finished", "owner_id": user["id"]})
        status, graph = _get(port, "/api/run/analyze_view_1/proof_graph?kind=analysis", cookie)
        _s, informal = _get(port, "/api/run/analyze_view_1/proof_graph?kind=informal", cookie)
        prove_status, refused = _get(port, "/api/run/plain_view_1/proof_graph?kind=analysis", cookie)
        _s, run = _get(port, "/api/run/analyze_view_1", cookie)
        refresh = _post(port, "/api/run/analyze_view_1/proof_graph", cookie, {"kind": "informal"})
    assert status == 200 and graph["status"] == "ready" and graph["kind"] == "analysis"
    assert graph["schema"] == "ansatze-analyze-graph-v1" and graph["certificate"] is False
    nodes = {node["id"]: node for node in graph["graph"]["nodes"]}
    assert nodes["lem:sum"]["meta"]["verdict"] == "gap" and nodes["thm:main"]["meta"]["closure"] == "gap"
    assert informal["model"] == "analyze-graph"
    assert prove_status == 400 and "no manuscript analysis" in refused["error"]
    assert refresh[0] == 200 and refresh[2]["model"] == "analyze-graph"
    # The record: run_kind and run["analyze"]; run["analysis"] stays the Monitor block.
    assert run["run_kind"] == "analyze" and run["analyze"]["summary"]["gap"] == 1
    assert "summary" not in (run.get("analysis") or {})
    preview = run["sandbox_preview"]
    assert preview["kind"] == "analysis" and preview["source"] == "analysis/report.md"
    assert preview["text"].startswith("# Analysis: Sums of evens") and preview["editable"] is False
    assert preview["source_file"] == "analysis/source/pasted.tex"
    assert "SOURCE-ONLY-MARKER" not in json.dumps(run)


def test_analyses_are_never_compiled_or_continued(analyze_console, fake_provider):
    with running_server() as port:
        user, cookie = _member(port)
        _finished_analysis("analyze_nocompile_1", user["id"], fake_provider)
        admin = _admin_cookie()
        pdf = request(port, "GET", f"{APP_MOUNT}/api/workspace/analyze_nocompile_1/pdf?path=analysis/source/pasted.tex",
                      headers={"Cookie": admin})
        final_pdf = request(port, "GET", f"{APP_MOUNT}/api/run/analyze_nocompile_1/final.pdf", headers={"Cookie": admin})
        final_latex = request(port, "GET", f"{APP_MOUNT}/api/run/analyze_nocompile_1/final_latex", headers={"Cookie": admin})
        continued = _post(port, "/api/runs/analyze_nocompile_1/continue", cookie, {"message": "more"})
        chat = _post(port, "/api/runs/analyze_nocompile_1/chat", cookie, {"message": "more"})
        edit = _post(port, "/api/workspace/analyze_nocompile_1/file", cookie, {"path": "proof.md", "content": "x"})
    for response in (pdf, final_pdf, final_latex):
        assert response[0] == 403 and "never compiled" in json.loads(response[2])["error"]
    for status, _headers, body in (continued, chat):
        assert status == 400 and body["error"] == analyze_runs.CONTINUE_REFUSAL
    assert edit[0] == 400 and "no editable proof" in edit[2]["error"]


def test_recheck_is_for_the_owner_or_admin_and_counts_as_a_derived_action(analyze_console, fake_provider, monkeypatch):
    started: list = []

    def recheck(run_id, node_id, *, user, on_exit=None):
        started.append((run_id, node_id, user.get("id")))
        if on_exit:
            on_exit()
        return {"ok": True, "status": "rechecking", "run_id": run_id, "node_id": node_id, "job_id": "j1"}

    monkeypatch.setattr(console.job_manager, "start_analysis_recheck", recheck)
    with running_server() as port:
        owner, cookie = _member(port)
        _other, other_cookie = _member(port, "other@example.test")
        _finished_analysis("analyze_recheck_1", owner["id"], fake_provider)
        jobs.workspace_dir("plain_recheck_1")
        jobs._write_run({"run_id": "plain_recheck_1", "engine": "plain", "status": "finished", "owner_id": owner["id"]})
        foreign = _post(port, "/api/run/analyze_recheck_1/analysis/recheck", other_cookie, {"node_id": "lem:sum"})
        not_analysis = _post(port, "/api/run/plain_recheck_1/analysis/recheck", cookie, {"node_id": "lem:sum"})
        missing = _post(port, "/api/run/analyze_recheck_1/analysis/recheck", cookie, {})
        ok = _post(port, "/api/run/analyze_recheck_1/analysis/recheck", cookie, {"node_id": "lem:sum"})
        admin = _post(port, "/api/run/analyze_recheck_1/analysis/recheck", _admin_cookie(), {"node_id": "thm:main"})
    assert foreign[0] == 404 and not_analysis[0] == 400 and missing[0] == 400
    assert ok[0] == 200 and ok[2]["status"] == "rechecking"
    assert admin[0] == 200
    assert [item[:2] for item in started] == [("analyze_recheck_1", "lem:sum"), ("analyze_recheck_1", "thm:main")]


def test_recheck_refusals_return_the_member_start(analyze_console, fake_provider, monkeypatch):
    def refuse(run_id, node_id, *, user, on_exit=None):
        raise analyze_runs.AnalyzeCapacityError(analyze_runs.DAILY_LIMIT_MESSAGE, retry_after=90)

    monkeypatch.setattr(console.job_manager, "start_analysis_recheck", refuse)
    monkeypatch.setenv("AGENT_MONITOR_MEMBER_RUNS_PER_HOUR", "1")
    with running_server() as port:
        owner, cookie = _member(port)
        _finished_analysis("analyze_recheck_2", owner["id"], fake_provider)
        first = _post(port, "/api/run/analyze_recheck_2/analysis/recheck", cookie, {"node_id": "lem:sum"})
        second = _post(port, "/api/run/analyze_recheck_2/analysis/recheck", cookie, {"node_id": "lem:sum"})
    assert first[0] == 429 and first[2]["error"] == analyze_runs.DAILY_LIMIT_MESSAGE
    assert first[1]["retry-after"] == "90"
    # The refused re-check did not use up the member's hourly start.
    assert second[0] == 429 and second[2]["error"] == analyze_runs.DAILY_LIMIT_MESSAGE


def test_lean_on_an_analysis_formalizes_selected_statements_only(analyze_console, fake_provider, monkeypatch):
    calls: list = []

    def generate(**kwargs):
        calls.append(kwargs)
        return {"status": "incomplete", "status_label": lean_verify.STATEMENTS_ONLY_LABEL, "scope": "statements"}

    monkeypatch.setattr(lean_verify, "generate", generate)
    from agent_monitor import auto_pipeline

    monkeypatch.setattr(auto_pipeline, "refresh_formal", lambda **kwargs: True)
    with running_server() as port:
        owner, cookie = _member(port)
        _finished_analysis("analyze_lean_1", owner["id"], fake_provider)
        prove = _post(port, "/api/run/analyze_lean_1/lean_verify", cookie, {"action": "verify", "prove": True})
        unknown = _post(port, "/api/run/analyze_lean_1/lean_verify", cookie,
                        {"action": "verify", "targets": ["lem:nope"]})
        audit = _post(port, "/api/run/analyze_lean_1/lean_verify", cookie, {"action": "audit"})
        verify = _post(port, "/api/run/analyze_lean_1/lean_verify", cookie,
                       {"action": "verify", "targets": ["lem:sum"], "prove": False})
        default = _post(port, "/api/run/analyze_lean_1/lean_verify", cookie, {"action": "verify"})
    assert prove[0] == 400 and "statements only" in prove[2]["error"]
    assert unknown[0] == 400 and "not a node" in unknown[2]["error"]
    assert audit[0] == 400
    assert verify[0] == 200 and verify[2]["status_label"] == "Statements compiled · proofs not attempted"
    first = calls[0]
    assert first["prove"] is False and first["targets"] == ["lem:sum"]
    assert "2a+2b=2(a+b)" in first["problem_override"] and first["source_label"] == "analysis/analysis_graph.json"
    assert "MANUSCRIPT STATEMENT BLUEPRINT" in first["guidance"]
    # Without targets, Verify states the main results.
    assert default[0] == 200 and calls[1]["targets"] == ["thm:main"]


def test_guest_recheck_takes_a_derived_action_slot(analyze_console, fake_provider, monkeypatch):
    held: list = []

    def recheck(run_id, node_id, *, user, on_exit=None):
        held.append(set(console._GUEST_DERIVED_ACTIVE_USERS))
        on_exit()
        return {"ok": True, "status": "rechecking", "run_id": run_id, "node_id": node_id, "job_id": "j"}

    monkeypatch.setattr(console.job_manager, "start_analysis_recheck", recheck)
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        _finished_analysis("analyze_guest_recheck_1", guest["id"], fake_provider, guest=True)
        ok = _post(port, "/api/run/analyze_guest_recheck_1/analysis/recheck", cookie, {"node_id": "lem:sum"})
    assert ok[0] == 200 and held == [{guest["id"]}]
    assert guest["id"] not in console._GUEST_DERIVED_ACTIVE_USERS
    assert len(console._GUEST_DERIVED_WINDOWS[guest["id"]]) == 1


def test_project_runs_report_their_kind(analyze_console, monkeypatch):
    from agent_monitor import projects

    monkeypatch.setattr(projects, "get", lambda pid, uid: {"problem": "p", "runs": [{"run_id": "analyze_p_1"},
                                                                                     {"run_id": "plain_p_1"}]})
    monkeypatch.setattr(console.research_projects, "get", projects.get)
    from agent_monitor import run_sharing

    monkeypatch.setattr(run_sharing, "record", lambda run_id: {"engine": run_id.split("_")[0], "status": "finished"})
    with running_server() as port:
        _user, cookie = _member(port)
        status, project = _get(port, "/api/projects/p1", cookie)
    assert status == 200
    assert [run["run_kind"] for run in project["runs"]] == ["analyze", "prove"]


# ---------------------------------------------------------------------------
# guest re-checks spend the guest Analyze allowance (sponsored calls)
# ---------------------------------------------------------------------------

NETWORK = {"X-Forwarded-For": "203.0.113.50"}


def _sponsored_broker(monkeypatch) -> None:
    from agent_monitor import sponsored_kimi_client as broker

    monkeypatch.setattr(broker, "configured", lambda: True)
    monkeypatch.setattr(broker, "issue_credentials", lambda **_kwargs: {
        "KIMI_API_KEY": "short-lived-loopback-token-" + "z" * 20,
        "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
        "AGENT_MONITOR_SPONSORED_KIMI": "1",
        "AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610",
    })


def _guest_analysis(port, run_id: str, server) -> tuple[dict, str]:
    guest, cookie, _ = _issue_guest(port)
    _finished_analysis(run_id, guest["id"], server, guest=True, credential_source="sponsored_kimi",
                       model="kimi-k3", owner=guest.get("email"))
    return guest, cookie


def _settled(run_id: str) -> None:
    import time

    deadline = time.monotonic() + 10
    while jobs.run_is_active(run_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not jobs.run_is_active(run_id)


def test_guest_rechecks_from_one_network_leave_sponsored_analyze_to_members(analyze_console, fake_provider,
                                                                             monkeypatch):
    _sponsored_broker(monkeypatch)

    def settle_at_once(**kwargs):
        # The runner's work is not the point here: the re-check settles at
        # once and gives its sponsored slot back, as _execute_recheck does.
        jobs._set_recheck_state(kwargs["run_id"], {"node_id": kwargs["node_id"], "status": "done",
                                                    "job_id": kwargs["job_id"]})
        jobs._update_job(kwargs["job_id"], status="finished")
        if kwargs["gate_key"]:
            analyze_runs.GATE.release(kwargs["gate_key"])
        kwargs["on_exit"]()

    monkeypatch.setattr(jobs, "_execute_recheck", settle_at_once)
    nodes = ["lem:sum", "lem:rep", "thm:main"]
    outcomes: dict[str, list] = {}
    with running_server() as port:
        guests = [_guest_analysis(port, f"analyze_guest_net_{index}", fake_provider) for index in range(3)]
        for index, (_guest, cookie) in enumerate(guests[:2]):
            run_id = f"analyze_guest_net_{index}"
            outcomes[run_id] = []
            for node in nodes:
                status, headers, body = _post(port, f"/api/run/{run_id}/analysis/recheck", cookie,
                                              {"node_id": node}, headers=NETWORK)
                outcomes[run_id].append((status, body.get("error")))
                _settled(run_id)
        # A third guest session on the same network: the network's hourly allowance is spent.
        third = _post(port, "/api/run/analyze_guest_net_2/analysis/recheck", guests[2][1],
                      {"node_id": "lem:sum"}, headers=NETWORK)
        # A second analysis of the first guest: that session's hourly allowance is spent.
        _finished_analysis("analyze_guest_net_3", guests[0][0]["id"], fake_provider, guest=True,
                           credential_source="sponsored_kimi", model="kimi-k3")
        again = _post(port, "/api/run/analyze_guest_net_3/analysis/recheck", guests[0][1],
                      {"node_id": "lem:sum"}, headers={"X-Forwarded-For": "198.51.100.9"})
        record = jobs._load_run_record("analyze_guest_net_0")
    limit = analyze_runs.GUEST_RECHECK_LIMIT_MESSAGE.format(limit=analyze_runs.GUEST_RECHECKS_PER_ANALYSIS)
    for run_id in ("analyze_guest_net_0", "analyze_guest_net_1"):
        assert [status for status, _error in outcomes[run_id]] == [200, 200, 403], outcomes
        assert outcomes[run_id][2][1] == limit
    assert third[0] == 429 and "from your network" in third[2]["error"]
    assert again[0] == 429 and "per hour" in again[2]["error"]
    assert record["analyze"]["rechecks"]["started"] == 2
    # Four sponsored re-checks in all, every one from the guests' share, and a
    # member can still start a sponsored analysis right away.
    assert analyze_runs.GATE.usage_today() == 4 and analyze_runs.GATE.guest_usage_today() == 4
    assert analyze_runs.GATE.capacity_error(guest=False) is None
    analyze_runs.GATE.reserve("analyze_member_after_guests", run_id="analyze_member_after_guests")


def test_a_guest_recheck_never_takes_the_last_sponsored_slot(analyze_console, fake_provider, monkeypatch):
    _sponsored_broker(monkeypatch)
    release = threading.Event()

    def hold(**kwargs):
        release.wait(10)
        jobs._set_recheck_state(kwargs["run_id"], {"node_id": kwargs["node_id"], "status": "done",
                                                    "job_id": kwargs["job_id"]})
        jobs._update_job(kwargs["job_id"], status="finished")
        if kwargs["gate_key"]:
            analyze_runs.GATE.release(kwargs["gate_key"])
        kwargs["on_exit"]()

    monkeypatch.setattr(jobs, "_execute_recheck", hold)
    try:
        with running_server() as port:
            (_a, cookie_a), (b, cookie_b) = (_guest_analysis(port, "analyze_guest_slot_a", fake_provider),
                                             _guest_analysis(port, "analyze_guest_slot_b", fake_provider))
            first = _post(port, "/api/run/analyze_guest_slot_a/analysis/recheck", cookie_a, {"node_id": "lem:sum"},
                          headers={"X-Forwarded-For": "203.0.113.60"})
            second = _post(port, "/api/run/analyze_guest_slot_b/analysis/recheck", cookie_b, {"node_id": "lem:sum"},
                           headers={"X-Forwarded-For": "198.51.100.60"})
            assert first[0] == 200, first
            assert second[0] == 429 and second[2]["error"].startswith("Sponsored Analyze for guests is busy")
            # One slot stays for members; the refused guest got its allowance back.
            assert analyze_runs.GATE.active() == 1 and analyze_runs.GATE.capacity_error(guest=False) is None
            assert len(console._GUEST_RECHECK_WINDOWS.get(b["id"]) or []) == 0
            assert b["id"] not in console._GUEST_DERIVED_ACTIVE_USERS
    finally:
        release.set()
        _settled("analyze_guest_slot_a")
    assert analyze_runs.GATE.active() == 0


# ---------------------------------------------------------------------------
# the Analyze switches also stop statement formalization of existing analyses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "switch, owner, message",
    [
        ("AGENT_MONITOR_ANALYZE_ENABLED", "member", analyze_runs.DISABLED_MESSAGE),
        ("AGENT_MONITOR_ANALYZE_GUESTS", "guest", analyze_runs.GUEST_DISABLED_MESSAGE),
        (analyze_runs.DAILY_CAP_ENV, "member", analyze_runs.SPONSORED_DERIVED_OFF_MESSAGE),
        (analyze_runs.GUEST_DAILY_CAP_ENV, "guest", analyze_runs.SPONSORED_DERIVED_OFF_MESSAGE),
    ],
)
def test_switches_refuse_statement_formalization_of_existing_analyses(analyze_console, fake_provider, monkeypatch,
                                                                       switch, owner, message):
    generated: list = []
    checked: list = []
    monkeypatch.setattr(lean_verify, "generate", lambda **kwargs: generated.append(kwargs) or {"status": "running"})
    monkeypatch.setattr(lean_verify, "compile_or_check",
                        lambda **kwargs: checked.append(kwargs) or {"status": "compiled", "mode": kwargs["mode"]})
    from agent_monitor import auto_pipeline

    monkeypatch.setattr(auto_pipeline, "refresh_formal", lambda **kwargs: True)
    run_id = f"analyze_switch_{switch.lower()}_{owner}"
    with running_server() as port:
        if owner == "guest":
            user, cookie, _ = _issue_guest(port)
        else:
            user, cookie = _member(port)
        _finished_analysis(run_id, user["id"], fake_provider, guest=owner == "guest",
                           credential_source="sponsored_kimi", model="kimi-k3")
        monkeypatch.setenv(switch, "0")
        verify = _post(port, f"/api/run/{run_id}/lean_verify", cookie,
                       {"action": "verify", "prove": False, "targets": ["thm:main"]})
        default = _post(port, f"/api/run/{run_id}/lean_verify", cookie, {"action": "verify"})
        # Checking Lean that already exists is not model work.
        check = _post(port, f"/api/run/{run_id}/lean_verify", cookie, {"action": "check", "lean": "theorem t : True := trivial"})
    for status, _headers, body in (verify, default):
        assert status == 403 and body["error"] == message
    assert generated == []
    assert check[0] == 200 and checked and checked[0]["mode"] == "check"
    # Checking an analysis's Lean keeps it a statement-only formalization.
    assert checked[0]["statements_only"] is True


def test_switches_stop_the_automatic_statement_formalization(analyze_console, fake_provider, monkeypatch):
    from agent_monitor import auto_pipeline

    calls: list = []
    monkeypatch.setattr(lean_verify, "generate", lambda **kwargs: calls.append(kwargs) or {"status": "incomplete"})
    workspace = _finished_analysis("analyze_switch_auto_1", 5, fake_provider, formalize=True,
                                   credential_source="sponsored_kimi", model="kimi-k3")
    record = jobs._load_run_record("analyze_switch_auto_1")
    member = {"id": 5, "email": "m@example.test", "is_admin": False}
    for switch, value, expected in (
        ("AGENT_MONITOR_ANALYZE_ENABLED", "0", "Analyze is turned off on this site"),
        (analyze_runs.DAILY_CAP_ENV, "0", analyze_runs.SPONSORED_DERIVED_OFF_MESSAGE),
    ):
        monkeypatch.setenv(switch, value)
        auto_pipeline._run_batch(workspace, record, member, "kimi-k3", "analyze")
        stages = auto_pipeline.load_state(workspace)["stages"]
        assert stages["informal_dag"]["status"] == "done"  # the projection is not model work
        assert stages["lean"]["status"] == "off" and expected in stages["lean"]["detail"], stages["lean"]
        monkeypatch.delenv(switch)
    assert calls == []
    # Switched back on, the main statements are formalized as before.
    auto_pipeline._run_batch(workspace, record, member, "kimi-k3", "analyze")
    assert len(calls) == 1 and calls[0]["prove"] is False
    # The run was admitted when it started: an operator's ANALYZE_CMD (members
    # lose Analyze) must not stop the member owner's automatic formalization.
    monkeypatch.setenv("ANALYZE_CMD", "/bin/true")
    auto_pipeline._run_batch(workspace, record, auto_pipeline._run_user(5, record), "kimi-k3", "analyze")
    assert len(calls) == 2
    monkeypatch.delenv("ANALYZE_CMD")
    # A guest's analysis stops when guests are switched off.
    monkeypatch.setenv("AGENT_MONITOR_ANALYZE_GUESTS", "0")
    guest_record = {**record, "guest": True}
    auto_pipeline._run_batch(workspace, guest_record, auto_pipeline._run_user(7, guest_record), "kimi-k3", "analyze")
    assert len(calls) == 2
    assert analyze_runs.GUEST_DISABLED_MESSAGE.rstrip(".") in auto_pipeline.load_state(workspace)["stages"]["lean"]["detail"]
