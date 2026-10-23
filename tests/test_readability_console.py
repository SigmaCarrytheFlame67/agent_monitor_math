"""The console's Readability mode: the engine listing and each viewer's
policy, starting a run (validation by role, routes, caps), the run view and
the paper map, questions about a run and their limits, and the security
gates a manuscript run shares with Analyze (no proof promotion, no derived
proof views, no compilation, no continuation, shares without the source).

Job creation is stubbed for the start tests; finished runs are built by the
real runner in-process against a scripted model, and the end-to-end test
runs the runner subprocess through ``jobs`` against a loopback stand-in for
the sponsored broker.  Nothing touches the network.
"""
from __future__ import annotations

import base64
import io
import json
import threading
import time
import zipfile
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pylatexenc")

from agent_monitor import analyze_runs, auto_pipeline, jobs, readability_runs, research, run_sharing  # noqa: E402
from agent_monitor import console_server as console  # noqa: E402
from agent_monitor import projects  # noqa: E402
from agent_monitor.readability import record as readability_record  # noqa: E402
from agent_monitor.readability import request as readability_request  # noqa: E402
from agent_monitor.runners import math_harness_runner as harness  # noqa: E402
from agent_monitor.runners import readability_runner  # noqa: E402

from test_analyze_console import _admin_cookie, _get, _post  # noqa: E402
from test_console_guest_access import (  # noqa: E402,F401  (fixture import)
    APP_MOUNT,
    _issue_guest,
    guest_console,
    request,
    running_server,
)
from test_member_accounts import _member, member_console  # noqa: E402,F401
from test_readability_runner import KINDS, PAPER, FakeModel, check_json, guide_json, kimi_env, readable  # noqa: E402

ARXIV = "https://arxiv.org/abs/2302.05537"
PDF_LINK = "https://example.org/papers/result.pdf"
TOKEN = "short-lived-loopback-token-" + "z" * 20
SOURCE_MARKER = "Distributivity-unique-source-marker-5521"
MARKED_PAPER = PAPER.replace("Distributivity.", SOURCE_MARKER + ".")
REFERENCE_MARKER = "A. Author, Even numbers."
_ENV = ("AGENT_MONITOR_READABILITY_ENABLED", "AGENT_MONITOR_READABILITY_GUESTS", "READABILITY_CMD",
        "AGENT_MONITOR_ANALYZE_ENABLED", "AGENT_MONITOR_ANALYZE_GUESTS", "ANALYZE_CMD", "AGENT_MONITOR_CLI_TIMEOUT",
        analyze_runs.DAILY_CAP_ENV, analyze_runs.CONCURRENCY_ENV, analyze_runs.GUEST_DAILY_CAP_ENV,
        analyze_runs.GUEST_CONCURRENCY_ENV, analyze_runs.ASK_DAILY_CAP_ENV, analyze_runs.GUEST_ASK_DAILY_CAP_ENV)


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(analyze_runs.GATE, "usage_path", tmp_path / "sponsored_usage.json")
    analyze_runs.GATE.reset()
    yield analyze_runs.GATE
    analyze_runs.GATE.reset()


@pytest.fixture()
def readability_console(member_console, gate, monkeypatch):
    monkeypatch.setattr(analyze_runs, "_RESOLVED_TITLES", OrderedDict())
    windows = (console._GUEST_READABILITY_WINDOWS, console._GUEST_READABILITY_CLIENT_WINDOWS,
               console._ASK_WINDOWS, console._ASK_CLIENT_WINDOWS,
               console._GUEST_ANALYZE_WINDOWS,
               console._GUEST_ANALYZE_CLIENT_WINDOWS, console._ANALYZE_RESOLVE_WINDOWS,
               console._ANALYZE_RESOLVE_CLIENT_WINDOWS)
    for window in windows:
        window.clear()
    yield member_console
    for window in windows:
        window.clear()


def _readable(source, **extra):
    return {"run_kind": "readability", "source": source, **extra}


def _finished_run(run_id: str, owner_id, monkeypatch, *, text: str = MARKED_PAPER, model: FakeModel | None = None,
                  **fields) -> Path:
    """A readability run made in-process by the real runner (scripted model)."""
    monkeypatch.setattr(harness, "api_chat", model or FakeModel())
    workspace = jobs.workspace_dir(run_id)
    readability_request.write(workspace, source={"kind": "paste", "text": text}, caps={}, audience="graduate",
                              focus="", role="member", route="api_key", model="kimi-k3",
                              reasoning_effort="default", sponsored=False)
    readability_runner.run_readability(workspace, readability_request.load(workspace), env=kimi_env(),
                                       sink=lambda _event: None, sleep=lambda _s: None)
    record = {
        "run_id": run_id, "engine": "readability", "run_kind": "readability", "status": "finished",
        "workspace": str(workspace), "owner_id": owner_id, "guest": False, "formalize": False,
        "credential_source": "user", "auth_route": "api_key", "model": "kimi-k3",
        "problem_text": "Readability · Sums of evens", "pipeline": jobs._pipeline_for("readability"),
        "agents": [{"role": "agent", "output": "Readability · rewrite-1"}], "edges": [], "totals": {},
        "readability": readability_record.run_block(workspace),
    }
    record.update(fields)
    jobs._write_run(record)
    return workspace


# ---------------------------------------------------------------------------
# the listing and each viewer's policy
# ---------------------------------------------------------------------------


def test_the_listing_offers_readability_with_each_viewers_policy(readability_console):
    with running_server() as port:
        _guest, guest_cookie, _ = _issue_guest(port)
        _s, guest_listing = _get(port, "/api/engines", guest_cookie)
        _user, cookie = _member(port)
        _s, member_listing = _get(port, "/api/engines", cookie)
        _s, admin_listing = _get(port, "/api/engines", _admin_cookie())
        _s, settings = _get(port, "/api/settings", guest_cookie)
    for listing in (guest_listing, member_listing, admin_listing):
        engines = {engine["id"]: engine for engine in listing["engines"]}
        assert engines["readability"]["modes"] == ["readability"] and engines["analyze"]["modes"] == ["analyze"]
        assert engines["readability"]["label"] == "Ansatz Readability"
    guest = next(engine for engine in guest_listing["engines"] if engine["id"] == "readability")
    policy = guest["readability"]
    assert not guest.get("requires_login") and guest["auth_modes"] == ["api_key"]
    assert policy["enabled"] is True and policy["role"] == "guest" and policy["routes"] == ["sponsored_kimi"]
    assert policy["api_models"] == ["kimi-k3"] and policy["runs_per_hour"] == 1
    assert policy["caps"]["sponsored"] == {"max_parts": 6, "max_calls": 16, "max_seconds": 2400}
    assert policy["caps"]["own_key"] is None
    assert policy["sources"]["links"] == ["arxiv", "github", "openai_math"]
    assert policy["sources"]["paste_chars"] == 20_000 and "zip" not in policy["sources"]["upload_extensions"]
    assert policy["asks"] == {"enabled": True, "reason": None, "sponsored": True, "sponsored_reason": None,
                              "per_hour": 6, "per_run": 3, "in_flight": 1, "question_chars": 2000,
                              "selection_chars": 4000}
    assert [item["id"] for item in policy["audiences"]] == ["research", "graduate", "undergraduate"]
    assert policy["check_note"] == "checked against the original by a model — not a certificate"
    assert policy["continue"] is False
    assert settings["guest_policy"]["readability"]["enabled"] is True
    member = next(engine for engine in member_listing["engines"] if engine["id"] == "readability")
    assert member["auth_modes"] == ["api_key"] and member["subscription_supported"] is False
    assert member["readability"]["routes"] == ["sponsored_kimi", "api_key"]
    assert member["readability"]["caps"]["sponsored"]["max_calls"] == 30
    assert member["readability"]["caps"]["own_key"] == {"max_parts": 40, "max_calls": 90, "max_seconds": 3600}
    assert member["readability"]["asks"]["per_hour"] == 30 and member["readability"]["asks"]["per_run"] is None
    admin = next(engine for engine in admin_listing["engines"] if engine["id"] == "readability")
    assert admin["readability"]["routes"] == ["sponsored_kimi", "api_key", "codex_subscription", "claude_subscription"]
    assert admin["auth_modes"] == ["codex_subscription", "claude_subscription", "api_key"]
    assert admin["readability"]["caps"]["own_key"]["max_parts"] == 120


def test_switches_hide_and_refuse_readability(readability_console, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_READABILITY_GUESTS", "0")
    with running_server() as port:
        _guest, guest_cookie, _ = _issue_guest(port)
        refused = _post(port, "/api/runs", guest_cookie, _readable({"url": ARXIV}))
        _s, listing = _get(port, "/api/engines", guest_cookie)
        _user, cookie = _member(port)
        member_ok = _post(port, "/api/runs", cookie, _readable({"url": ARXIV}))
        monkeypatch.setenv("AGENT_MONITOR_READABILITY_ENABLED", "0")
        member_refused = _post(port, "/api/runs", cookie, _readable({"url": ARXIV}))
        _s, member_listing = _get(port, "/api/engines", cookie)
    assert refused[0] == 403 and refused[2]["error"] == readability_runs.GUEST_DISABLED_MESSAGE
    entry = next(engine for engine in listing["engines"] if engine["id"] == "readability")
    assert entry["requires_login"] is True and entry["readability"]["enabled"] is False
    assert member_ok[0] == 200
    assert member_refused[0] == 403 and member_refused[2]["error"] == readability_runs.DISABLED_MESSAGE
    assert all(engine["id"] != "readability" for engine in member_listing["engines"])
    assert len(readability_console["started"]) == 1


def test_missing_parser_packages_mark_readability_as_needing_setup(monkeypatch):
    from agent_monitor import engines_registry

    monkeypatch.setattr(engines_registry, "analyze_missing_packages", lambda: ["pypdf"])
    entry = next(engine for engine in engines_registry.list_engines() if engine["id"] == "readability")
    assert entry["available"] is False and entry["health_detail"].startswith("Readability needs the Python package")


# ---------------------------------------------------------------------------
# starting a run
# ---------------------------------------------------------------------------


def test_a_guest_starts_a_readability_run_of_an_arxiv_link_once_an_hour(readability_console):
    with running_server() as port:
        _guest, cookie, _ = _issue_guest(port)
        status, _headers, body = _post(port, "/api/runs", cookie, _readable(
            {"url": ARXIV}, readability={"audience": "undergraduate", "focus": "Lemma 3"}))
        again = _post(port, "/api/runs", cookie, _readable({"url": ARXIV}))
    assert status == 200, body
    call = readability_console["started"][0]
    assert call["engine"] == "readability" and call["run_kind"] == "readability"
    assert call["source"] == {"url": ARXIV}
    assert call["readability"] == {"audience": "undergraduate", "focus": "Lemma 3"}
    assert call["formalize"] is False and call["use_subagents"] is False
    assert body["guest_policy"]["readability"]["enabled"] is True
    assert again[0] == 429 and "one readability run per hour" in again[2]["error"]
    assert len(readability_console["started"]) == 1


@pytest.mark.parametrize(
    ("source", "extra", "message"),
    [
        ({"url": PDF_LINK}, {}, readability_runs.GUEST_LINK_REFUSAL),
        ({"text": "x" * 20_001}, {}, "limited to 20,000 characters for guests"),
        ({}, {}, readability_runs.SOURCE_MISSING),
        ({"url": ARXIV, "text": "\\section{x}"}, {}, readability_runs.ONE_SOURCE),
        ({"text": "body"}, {"readability": {"audience": "children"}}, "readability.audience"),
        ({"text": "body"}, {"readability": {"focus": "x" * 2001}}, "2,000 characters"),
        ({"text": "body"}, {"readability": "graduate"}, "readability must be an object"),
        ({"text": "body", "extra": 1}, {}, "only url, text and format"),
    ],
)
def test_guest_sources_and_options_are_validated(readability_console, source, extra, message):
    with running_server() as port:
        _guest, cookie, _ = _issue_guest(port)
        status, _headers, body = _post(port, "/api/runs", cookie, _readable(source, **extra))
    assert status == 400 and message in body["error"]
    assert readability_console["started"] == []


def test_run_kind_and_engine_must_agree(readability_console):
    with running_server() as port:
        _user, cookie = _member(port)
        prove = _post(port, "/api/runs", cookie, {"engine": "readability", "problem_text": "x"})
        wrong_engine = _post(port, "/api/runs", cookie, _readable({"url": ARXIV}, engine="analyze"))
        explore = _post(port, "/api/runs", cookie, {"run_kind": "explore", "engine": "kimi", "problem_text": "x"})
    assert prove[0] == 400 and "Readability mode" in prove[2]["error"]
    assert wrong_engine[0] == 400 and '"readability" engine' in wrong_engine[2]["error"]
    assert explore[0] == 400 and '"prove", "analyze" or "readability"' in explore[2]["error"]
    assert readability_console["started"] == []


def test_members_read_more_than_guests_but_only_on_kimi(readability_console):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("main.tex", PAPER)
    upload = {"name": "paper.zip", "data": base64.b64encode(archive.getvalue()).decode()}
    with running_server() as port:
        _user, cookie = _member(port)
        link = _post(port, "/api/runs", cookie, _readable({"url": PDF_LINK}))
        paste = _post(port, "/api/runs", cookie, _readable({"text": "\\section{A}" + "x" * 30_000}))
        bundle = _post(port, "/api/runs", cookie, _readable(None, attachments=[upload]))
        foreign = _post(port, "/api/runs", cookie, _readable({"url": ARXIV}, model="gpt-5.2"))
        subscription = _post(port, "/api/runs", cookie, _readable({"url": ARXIV}, auth_route="codex_subscription"))
    assert (link[0], paste[0], bundle[0]) == (200, 200, 200), (link, paste, bundle)
    assert foreign[0] == 403 and "Kimi K3" in foreign[2]["error"]
    assert subscription[0] == 403
    calls = readability_console["started"]
    assert [call["source"] for call in calls[:2]] == [{"url": PDF_LINK}, calls[1]["source"]]
    assert calls[2]["attachments"][0]["name"] == "paper.zip" and len(calls) == 3


def test_sponsored_capacity_refusals_name_readability(readability_console, gate, monkeypatch):
    gate.reserve("busy-1", run_id="busy-1")
    gate.reserve("busy-2", run_id="busy-2")
    with running_server() as port:
        _guest, cookie, _ = _issue_guest(port)
        busy = _post(port, "/api/runs", cookie, _readable({"url": ARXIV}))
        gate.reset()
        monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "0")
        off = _post(port, "/api/runs", cookie, _readable({"url": ARXIV}))
    assert busy[0] == 429 and busy[2]["error"].startswith("Sponsored Readability is busy (2 manuscript runs")
    assert off[0] == 403 and off[2]["error"] == readability_runs.GATE_MESSAGES.guest_off
    assert readability_console["started"] == []


# ---------------------------------------------------------------------------
# jobs.start_job
# ---------------------------------------------------------------------------


def _broker(monkeypatch, issued: list, base: str = "http://127.0.0.1:4610/internal/v1"):
    from agent_monitor import sponsored_kimi_client as broker

    monkeypatch.setattr(broker, "configured", lambda: True)

    def credentials(**kwargs):
        issued.append(kwargs)
        return {"KIMI_API_KEY": TOKEN, "KIMI_API_BASE": base, "KIMI_REASONING_EFFORT": "high",
                "AGENT_MONITOR_SPONSORED_KIMI": "1", "AGENT_MONITOR_SPONSORED_KIMI_URL": base.rsplit("/internal", 1)[0]}

    monkeypatch.setattr(broker, "issue_credentials", credentials)


@pytest.fixture()
def captured_start(monkeypatch, tmp_path):
    """start_job without a worker: the thread arguments and records are kept."""
    captured: list[dict] = []
    records: list[dict] = []
    monkeypatch.setattr(jobs.threading, "Thread",
                        lambda **kwargs: captured.append(kwargs) or SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(jobs, "workspace_dir", lambda run_id: (tmp_path / run_id).mkdir(exist_ok=True) or tmp_path / run_id)
    monkeypatch.setattr(jobs, "_write_run", records.append)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    started_pipelines: list = []
    monkeypatch.setattr(auto_pipeline, "start", lambda **kwargs: started_pipelines.append(kwargs) or {})
    return SimpleNamespace(threads=captured, records=records, pipelines=started_pipelines)


GUEST = {"id": 81, "email": "guest+r@public.local", "guest": True}
MEMBER = {"id": 82, "email": "reader@example.test", "is_admin": False}
ADMIN = {"id": 83, "email": "owner@example.test", "is_admin": True}


def test_a_guest_run_uses_the_sponsored_math_harness_scope_and_writes_its_request(gate, monkeypatch, captured_start):
    issued: list = []
    _broker(monkeypatch, issued)
    monkeypatch.setattr(jobs, "_user_extra_env",
                        lambda user, *models: {"AGENT_MONITOR_PYTHON": "python", "KIMI_API_KEY": "guest-own-key-12345"})
    job = jobs.start_job(engine="readability", run_kind="readability", source={"url": ARXIV}, user=GUEST,
                         readability={"audience": "research", "focus": "Section 2"})
    assert job["model"] == "kimi-k3" and job["run_kind"] == "readability" and job["formalize"] is False
    assert job["credential_source"] == "sponsored_kimi"
    assert issued[0]["engine"] == "math_harness" and issued[0]["minimum_ttl_seconds"] >= 2400 + 630
    kwargs = captured_start.threads[0]["kwargs"]
    env = kwargs["extra_env"]
    assert kwargs["run_kind"] == "readability" and kwargs["engine"] == "readability"
    assert env["KIMI_API_KEY"] == TOKEN and env["AGENT_MONITOR_GUEST"] == "1"
    assert env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] == "8192" and "guest-own-key-12345" not in json.dumps(env)
    assert int(env[readability_runs.WATCHDOG_ENV]) == 2400 + 630 + 300
    record = captured_start.records[0]
    req = readability_request.load(Path(record["workspace"]))
    assert (req["role"], req["route"], req["sponsored"], req["model"]) == ("guest", "sponsored_kimi", True, "kimi-k3")
    assert req["caps"] == {"max_parts": 6, "max_calls": 16, "max_seconds": 2400}
    assert (req["audience"], req["focus"]) == ("research", "Section 2")
    assert record["run_kind"] == "readability" and record["readability"]["audience"] == "research"
    assert record["problem_text"].startswith(f"Readability · {ARXIV}")
    assert record["pipeline"][2] == {"id": "verify", "label": "3 Rewrite", "title": "Readable version, one part at a time"}
    assert record["engine_impl"] == "ansatz-readability"
    assert gate.holds(job["run_id"]) and gate.usage_today() == 1
    # No derived-views sidecar for a readability run.
    assert captured_start.pipelines == []


def test_member_and_admin_routes(gate, monkeypatch, captured_start):
    issued: list = []
    _broker(monkeypatch, issued)
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {
        "AGENT_MONITOR_PYTHON": "python", "KIMI_API_KEY": "sk-member-kimi-123456789"})
    member = jobs.start_job(engine="readability", run_kind="readability", source={"text": PAPER}, user=MEMBER)
    env = captured_start.threads[-1]["kwargs"]["extra_env"]
    assert member["model"] == "kimi-k3" and member["credential_source"] == "user" and issued == []
    assert env["AGENT_MONITOR_SPONSORED_KIMI"] == "0" and env["AGENT_MONITOR_KIMI_BASE_URL"].startswith("https://")
    req = readability_request.load(Path(captured_start.records[-1]["workspace"]))
    assert req["route"] == "api_key" and req["caps"] == {"max_parts": 40, "max_calls": 90, "max_seconds": 3600}
    with pytest.raises(ValueError, match="Readability runs on Kimi K3 for members"):
        jobs.start_job(engine="readability", run_kind="readability", source={"text": PAPER}, user=MEMBER,
                       model="gpt-5.2")
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {
        "AGENT_MONITOR_PYTHON": "python", "OPENAI_API_KEY": "sk-admin-openai-123456789"})
    with pytest.raises(ValueError, match=readability_runs.KIMI_ONLY_MESSAGE):
        jobs.start_job(engine="readability", run_kind="readability", source={"text": PAPER}, user=ADMIN,
                       model="gpt-5.2", auth_route="api_key")
    with pytest.raises(ValueError, match="Readability mode"):
        jobs.start_job(engine="readability", problem_text="Prove it.", user=ADMIN, run_kind="prove")


def test_readability_refusals_never_keep_the_sponsored_start(gate, monkeypatch, captured_start):
    issued: list = []
    _broker(monkeypatch, issued)
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {"AGENT_MONITOR_PYTHON": "python"})

    def broken(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jobs, "_start_job_launch", broken)
    with pytest.raises(OSError):
        jobs.start_job(engine="readability", run_kind="readability", source={"url": ARXIV}, user=GUEST)
    assert gate.usage_today() == 0 and gate.active() == 0


def test_the_cli_watchdog_follows_the_readability_caps():
    env = {readability_runs.WATCHDOG_ENV: "4000"}
    assert jobs._cli_timeout_seconds("readability", env) == 4000
    assert jobs._cli_timeout_seconds("readability", {**env, "AGENT_MONITOR_CLI_TIMEOUT": "60"}) == 4000
    assert jobs._cli_timeout_seconds("readability", {readability_runs.WATCHDOG_ENV: "15000",
                                                      "AGENT_MONITOR_SPONSORED_KIMI": "1"}) == 6600


# ---------------------------------------------------------------------------
# the run view, the paper map and the gates
# ---------------------------------------------------------------------------


def test_the_run_view_shows_the_readable_version_and_its_companions(readability_console, monkeypatch):
    with running_server() as port:
        user, cookie = _member(port)
        _finished_run("readability_view_1", user["id"], monkeypatch)
        _s, run = _get(port, "/api/run/readability_view_1", cookie)
        _s, original = _get(port, "/api/workspace/readability_view_1/file?path=readability/original.txt", cookie)
    assert run["run_kind"] == "readability" and run["readability"]["schema"] == "ansatze-readability-v1"
    assert run["readability"]["summary"] == {"parts": 1, "rewritten": 1, "faithful": 1, "issues": 0, "unchecked": 0,
                                             "not_rewritten": 0, "kept": 1, "calls": 3, "model": "kimi-k3",
                                             "route": "api_key"}
    preview = run["sandbox_preview"]
    assert preview["kind"] == "readability" and preview["source"] == "readability/readable.md"
    assert preview["text"].startswith("# Sums of evens — Readable version") and preview["has_readable"] is True
    assert preview["editable"] is False and preview["has_proof_md"] is False
    assert preview["original_available"] is True and preview["original_path"] == "readability/original.txt"
    assert preview["source_file"] == "readability/source/pasted.tex" and preview["audience"] == "graduate"
    assert preview["parts"]["faithful"] == 1 and preview["parts"]["not_rewritten"] == 0
    assert preview["parts"]["kept"] == 1
    assert [part["status"] for part in preview["check"]] == ["faithful", "not-rewritten"]
    assert preview["check"][1]["kind"] == "references"
    assert preview["notation"]["rows"][0]["symbol"] == "\\Z" and preview["macros"] == {"\\Z": "\\mathbb{Z}"}
    # The owner reads the reading-order text through the workspace file API.
    assert SOURCE_MARKER in original["content"]


def test_a_faithful_parts_minor_issues_are_its_check_notes(readability_console, monkeypatch):
    minor = {"severity": "minor", "location": "Lemma 2.2, added remark",
             "problem": "the added explanation says the tags increase strictly along R", "fix": "say weakly"}
    model = FakeModel(check=lambda _user: check_json("faithful", [minor]))
    with running_server() as port:
        user, cookie = _member(port)
        workspace = _finished_run("readability_notes_1", user["id"], monkeypatch, model=model)
        _s, run = _get(port, "/api/run/readability_notes_1", cookie)
    part = run["sandbox_preview"]["check"][0]
    assert part["index"] == 1 and part["status"] == "faithful"
    # Every issue with its severity: the minor ones are the part's check notes.
    assert part["issues"] == [minor]
    # They are not marked in the readable version (only major problems are).
    assert "⚠ **Check:**" not in (workspace / "readability" / "readable.md").read_text()
    share = run_sharing.snapshot("readability_notes_1", jobs._load_run_record("readability_notes_1"))
    assert share["readability"]["check"][0]["issues"] == [minor]


def test_while_running_the_preview_is_the_progress_without_model_output(readability_console, monkeypatch):
    with running_server() as port:
        user, cookie = _member(port)
        workspace = jobs.workspace_dir("readability_live_1")
        readability_request.write(workspace, source={"kind": "paste", "text": PAPER}, caps={}, audience="graduate",
                                  focus="", role="member", route="api_key", model="kimi-k3",
                                  reasoning_effort="default", sponsored=False)
        jobs._write_run({
            "run_id": "readability_live_1", "engine": "readability", "run_kind": "readability", "status": "running",
            "workspace": str(workspace), "owner_id": user["id"], "updated_at": jobs._now(),
            "agents": [{"output": "Readability · Ingest · Sums of evens · 1 file"},
                       {"output": "Readability · rewrite-1"},
                       {"output": "=== READABLE === leaked model output"},
                       {"output": "Readability · Rewrite · now working on part 1 (Introduction)"},
                       {"output": "Readability · Rewrite · part 1 (Introduction) · rewritten"}],
        })
        jobs._JOBS["live-1"] = {"job_id": "live-1", "run_id": "readability_live_1", "status": "running"}
        try:
            _s, run = _get(port, "/api/run/readability_live_1", cookie)
        finally:
            jobs._JOBS.pop("live-1", None)
    preview = run["sandbox_preview"]
    assert preview["source"] == "live" and preview["has_readable"] is False
    assert preview["text"] == "Ingest · Sums of evens · 1 file\nRewrite · part 1 (Introduction) · rewritten"


def test_the_paper_map_is_served_deterministically(readability_console, monkeypatch):
    with running_server() as port:
        user, cookie = _member(port)
        _finished_run("readability_map_1", user["id"], monkeypatch)
        jobs.workspace_dir("plain_map_1")
        jobs._write_run({"run_id": "plain_map_1", "engine": "plain", "status": "finished", "owner_id": user["id"]})
        status, payload = _get(port, "/api/run/readability_map_1/proof_graph?kind=paper-map", cookie)
        refused = _get(port, "/api/run/plain_map_1/proof_graph?kind=paper-map", cookie)
        refresh = _post(port, "/api/run/readability_map_1/proof_graph", cookie, {"kind": "paper-map"})
        generate = _post(port, "/api/run/readability_map_1/proof_graph", cookie, {"kind": "informal"})
        lean = _post(port, "/api/run/readability_map_1/lean_verify", cookie, {"action": "verify"})
    assert status == 200 and payload["status"] == "ready" and payload["kind"] == "paper-map"
    assert payload["schema"] == "ansatze-readability-map-v1" and payload["certificate"] is False
    nodes = {node["id"]: node for node in payload["graph"]["nodes"]}
    assert nodes["thm:main"]["importance"] == 5 and nodes["thm:main"]["label"] == "Theorem 1.1"
    assert payload["summary"]["main_results"] == ["thm:main"] and payload["summary"]["nodes"] == 3
    assert payload["macros"] == {"\\Z": "\\mathbb{Z}"}
    assert refused[0] == 400 and "no paper map" in refused[1]["error"]
    assert refresh[0] == 200 and refresh[2]["kind"] == "paper-map"
    assert generate[0] == 400 and "paper map, not a proof DAG" in generate[2]["error"]
    assert lean[0] == 400 and "no proof to formalize" in lean[2]["error"]


def test_readability_runs_are_never_compiled_continued_or_edited(readability_console, monkeypatch):
    with running_server() as port:
        user, cookie = _member(port)
        _finished_run("readability_gate_1", user["id"], monkeypatch)
        admin = _admin_cookie()
        pdf = request(port, "GET", f"{APP_MOUNT}/api/workspace/readability_gate_1/pdf?path=readability/source/pasted.tex",
                      headers={"Cookie": admin})
        final_pdf = request(port, "GET", f"{APP_MOUNT}/api/run/readability_gate_1/final.pdf", headers={"Cookie": admin})
        final_latex = request(port, "GET", f"{APP_MOUNT}/api/run/readability_gate_1/final_latex",
                              headers={"Cookie": admin})
        continued = _post(port, "/api/runs/readability_gate_1/continue", cookie, {"message": "more"})
        chat = _post(port, "/api/runs/readability_gate_1/chat", cookie, {"message": "more"})
        edit = _post(port, "/api/workspace/readability_gate_1/file", cookie, {"path": "proof.md", "content": "x"})
    for response in (pdf, final_pdf, final_latex):
        assert response[0] == 403 and json.loads(response[2])["error"] == readability_runs.COMPILE_REFUSAL
    for status, _headers, body in (continued, chat):
        assert status == 400 and body["error"] == readability_runs.CONTINUE_REFUSAL
    assert edit[0] == 400 and edit[2]["error"] == readability_runs.EDIT_REFUSAL
    with pytest.raises(ValueError, match="can't be continued"):
        jobs.continue_run("readability_gate_1", user=None)


def test_no_derived_views_research_memory_or_continuation_start_from_a_readability_run(monkeypatch, tmp_path):
    record = {"run_id": "readability_x", "engine": "readability", "run_kind": "readability", "status": "finished",
              "agents": [{"output": "A long final message that reads like a proof. " * 20}]}
    assert analyze_runs.is_manuscript_record(record) and not analyze_runs.is_analyze_record(record)
    assert auto_pipeline._source_signature(tmp_path, record) == ""
    assert auto_pipeline.start(run_id="readability_x", workspace=tmp_path, owner_id=None, model="kimi-k3",
                               engine="readability")["status"] == "disabled"
    assert not (tmp_path / "auto_pipeline.json").exists()
    assert jobs.tool_free_run("readability_x", record) is False
    monkeypatch.setattr(jobs, "run_owner", lambda run_id: 5)
    monkeypatch.setattr(run_sharing, "record", lambda run_id: record)
    with pytest.raises(ValueError, match="can't become shared DAG memory"):
        research.publish_memory({"id": 5}, {"source_run_id": "readability_x", "publish": True,
                                            "node_id": "lem-sum", "layer": "informal"})
    monkeypatch.setattr(projects, "get", lambda pid, uid: {"problem": "Project problem text"})
    body = {"project_id": "p1", "project_task": "Explain Lemma 2", "run_kind": "readability",
            "source": {"url": ARXIV}}
    projects.prepare_run(body, {"id": 3})
    assert body["readability"] == {"focus": "Explain Lemma 2"} and body["problem_text"] is None


def test_a_shared_readability_run_carries_the_rewrite_but_never_the_source(readability_console, monkeypatch):
    workspace = _finished_run("readability_share_1", 5, monkeypatch)
    (workspace / "readability" / "source" / "notes.txt").write_text("PRIVATE-SOURCE-NOTE")
    record = jobs._load_run_record("readability_share_1")
    snapshot = run_sharing.snapshot("readability_share_1", record)
    blob = json.dumps(snapshot)
    assert snapshot["kind"] == "readability" and snapshot["engine"] == "readability" and snapshot["agents"] == []
    assert snapshot["title"] == "Readability · Sums of evens"
    assert snapshot["proof"].startswith("# Sums of evens — Readable version")
    # Neither the stored source nor the parts shown in their original form.
    for secret in (SOURCE_MARKER, "PRIVATE-SOURCE-NOTE", REFERENCE_MARKER, "pasted.tex"):
        assert secret not in blob, secret
    assert "not included in shared links" in snapshot["proof"]
    assert "not a certificate" in snapshot["notice"]
    share = snapshot["readability"]
    assert share["summary"]["faithful"] == 1 and share["notation"]["rows"][0]["symbol"] == "\\Z"
    assert {node["id"] for node in share["paper_map"]["nodes"]} == {"thm:main", "lem:rep", "lem:sum"}
    assert snapshot["graphs"]["manuscript"]["status"] == "recorded"
    assert [part["status"] for part in share["check"]] == ["faithful", "not-rewritten"]


def test_a_long_readable_version_is_cut_at_a_part_in_the_view_and_the_share(readability_console, monkeypatch):
    from agent_monitor.readability import report

    with running_server() as port:
        user, cookie = _member(port)
        workspace = _finished_run("readability_long_1", user["id"], monkeypatch)
        long_text = "# Sums of evens — Readable version\n\n" + "\n\n".join(
            f"[part:{index}]: #\n\n" + f"Step {index} of the argument. " * 300 for index in range(1, 60)) + "\n"
        (workspace / "readability" / "readable.md").write_text(long_text)
        _s, run = _get(port, "/api/run/readability_long_1", cookie)
    shown = run["sandbox_preview"]["text"]
    shared = run_sharing.snapshot("readability_long_1", jobs._load_run_record("readability_long_1"))["proof"]
    for view in (shown, shared):
        assert len(long_text) > report.VIEW_CHARS >= len(view)
        assert view.endswith("The rest is in `readability/readable.md` of the run.\n")
        assert long_text.startswith(view.rsplit("\n\n> **This readable version is longer", 1)[0])


def test_a_project_lists_a_readability_run_with_its_kind(readability_console, monkeypatch):
    with running_server() as port:
        user, cookie = _member(port)
        _finished_run("readability_project_1", user["id"], monkeypatch)
        jobs._write_run({"run_id": "kimi_project_1", "engine": "kimi", "status": "finished", "owner_id": user["id"]})
        project = projects.create(user, "Reading group", "Read the paper.")
        projects.attach(project["id"], user["id"], "readability_project_1", "Explain Lemma 2")
        projects.attach(project["id"], user["id"], "kimi_project_1", "Prove it")
        status, payload = _get(port, f"/api/projects/{project['id']}", cookie)
    assert status == 200, payload
    kinds = {run["run_id"]: run["run_kind"] for run in payload["runs"]}
    assert kinds == {"readability_project_1": "readability", "kimi_project_1": "prove"}


def test_deploys_wait_for_a_question_in_flight():
    from importlib import util

    spec = util.spec_from_file_location("auto_deploy", Path(__file__).resolve().parents[1] / "deploy" / "ansatze"
                                        / "auto_deploy.py")
    deploy = util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    assert deploy.record_busy({"status": "finished", "readability": {"ask": {"status": "running"}}})
    assert not deploy.record_busy({"status": "finished", "readability": {"asks": [{"status": "done"}]}})


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------


def _fake_ask(monkeypatch, answers: list):
    """start_readability_ask with a stubbed route and answer (no runner)."""
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))

    def answer(**kwargs):
        answers.append(kwargs)
        final = {**kwargs["state"], "status": "done", "answer": "Yes. See [[result:thm:main]].", "refs": ["thm:main"],
                 "finished_at": jobs._now()}
        jobs._set_ask_state(kwargs["run_id"], final)
        jobs._update_job(kwargs["job_id"], status="finished")
        # As _execute_ask does, the sponsored slot is given back.
        if kwargs["gate_key"]:
            analyze_runs.GATE.release(kwargs["gate_key"])
        if kwargs["on_exit"]:
            kwargs["on_exit"]()

    monkeypatch.setattr(jobs, "_execute_ask", answer)


def _answered(run_id: str) -> bool:
    return _wait(lambda: not jobs.run_is_active(run_id), 10)


def test_the_owner_asks_and_the_answer_lands_in_the_record(readability_console, monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    with running_server() as port:
        owner, cookie = _member(port)
        _other, other_cookie = _member(port, "other@example.test")
        _finished_run("readability_ask_1", owner["id"], monkeypatch)
        asked = _post(port, "/api/run/readability_ask_1/readability/ask", cookie,
                      {"question": "Why is the sum even?", "selection": "This part explains", "view": "readable"})
        assert _answered("readability_ask_1")
        foreign = _post(port, "/api/run/readability_ask_1/readability/ask", other_cookie, {"question": "Why?"})
        empty = _post(port, "/api/run/readability_ask_1/readability/ask", cookie, {"question": " "})
        unknown = _post(port, "/api/run/readability_ask_1/readability/ask", cookie,
                        {"question": "Why?", "node_id": "lem:nope"})
        _s, run = _get(port, "/api/run/readability_ask_1", cookie)
        monkeypatch.setattr(console.research_projects, "can_read_run", lambda run_id, user_id: True)
        viewer = _post(port, "/api/run/readability_ask_1/readability/ask", other_cookie, {"question": "Why?"})
    assert asked[0] == 202 and asked[2]["status"] == "asking" and len(asked[2]["ask_id"]) == 12
    assert foreign[0] == 404 and empty[0] == 400 and "Type a question" in empty[2]["error"]
    assert unknown[0] == 400 and "node_id must be a result" in unknown[2]["error"]
    # A project member reads the answers but may not ask.
    assert viewer[0] == 403 and "Only the member who started this run can ask about it" in viewer[2]["error"]
    block = run["readability"]
    assert "ask" not in block and block["asks_started"] == 1
    entry = block["asks"][0]
    assert entry["question"] == "Why is the sum even?" and entry["status"] == "done"
    assert entry["refs"] == ["thm:main"] and entry["ask_id"] == asked[2]["ask_id"]
    stored = readability_runner.readability_ask.load_request(jobs.workspace_dir("readability_ask_1"), entry["ask_id"])
    assert stored == {"question": "Why is the sum even?", "selection": "This part explains", "view": "readable",
                      "node_id": None}


def test_guest_asks_are_limited_per_run(readability_console, monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        _finished_run("readability_ask_guest", guest["id"], monkeypatch, guest=True, credential_source="user")
        codes = []
        for number in range(4):
            codes.append(_post(port, "/api/run/readability_ask_guest/readability/ask", cookie,
                               {"question": f"Q{number}?"}))
            assert _answered("readability_ask_guest")
    assert [code[0] for code in codes[:3]] == [202, 202, 202]
    assert codes[3][0] == 403 and codes[3][2]["login_required"] is True
    assert "Guests can ask 3 questions" in codes[3][2]["error"]
    assert len(answers) == 3


def test_guest_questions_are_limited_per_network(readability_console, monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    monkeypatch.setattr(console, "GUEST_ASKS_PER_CLIENT_PER_HOUR", 2)
    network = {"X-Forwarded-For": "203.0.113.40"}
    with running_server() as port:
        cookies = []
        for index in range(2):
            guest, cookie, _ = _issue_guest(port)
            _finished_run(f"readability_ask_net_{index}", guest["id"], monkeypatch, guest=True,
                          credential_source="user")
            cookies.append(cookie)
        codes = []
        for index in (0, 0, 1):
            codes.append(_post(port, f"/api/run/readability_ask_net_{index}/readability/ask", cookies[index],
                               {"question": "Why?"}, headers=network))
            assert _answered(f"readability_ask_net_{index}")
        elsewhere = _post(port, "/api/run/readability_ask_net_1/readability/ask", cookies[1], {"question": "Why?"},
                          headers={"X-Forwarded-For": "198.51.100.41"})
    # A second guest session on the same network finds the network's hourly allowance spent.
    assert [code[0] for code in codes] == [202, 202, 429]
    assert "Too many guest questions from your network" in codes[2][2]["error"]
    assert elsewhere[0] == 202 and len(answers) == 3


def test_sponsored_questions_count_apart_from_the_manuscript_runs(readability_console, gate, monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    monkeypatch.setenv(analyze_runs.ASK_DAILY_CAP_ENV, "2")
    with running_server() as port:
        owner, cookie = _member(port)
        _finished_run("readability_ask_sponsored", owner["id"], monkeypatch, credential_source="sponsored_kimi")
        codes = []
        for number in range(3):
            codes.append(_post(port, "/api/run/readability_ask_sponsored/readability/ask", cookie,
                               {"question": f"Q{number}?"}))
            assert _answered("readability_ask_sponsored")
    assert [code[0] for code in codes] == [202, 202, 429]
    assert codes[2][2]["error"] == readability_runs.ASK_GATE_MESSAGES.daily
    assert [answer["gate_key"] for answer in answers] == ["readability_ask_sponsored#ask"] * 2
    # The day's manuscript runs are untouched: a sponsored run may still start.
    usage = json.loads(gate.usage_path.read_text())
    assert (usage["runs"], usage["asks"]) == (0, 2) and gate.capacity_error() is None


def test_the_gate_keeps_the_guests_share_of_questions_and_refunds_them(gate, monkeypatch):
    monkeypatch.setenv(analyze_runs.ASK_DAILY_CAP_ENV, "4")
    ask = {"messages": readability_runs.ASK_GATE_MESSAGES, "daily": readability_runs.ASK_DAILY}
    for run_id in ("a", "b"):
        gate.reserve(f"{run_id}#ask", run_id=run_id, guest=True, **ask)
        gate.release(f"{run_id}#ask")
    # Guests asked their half of the day's questions; an account may still ask.
    with pytest.raises(analyze_runs.AnalyzeCapacityError) as refused:
        gate.reserve("c#ask", run_id="c", guest=True, **ask)
    assert str(refused.value) == readability_runs.ASK_GATE_MESSAGES.guest_share_daily
    gate.reserve("d#ask", run_id="d", **ask)
    assert gate.holds("d#ask") and gate.capacity_error(guest=True) is None
    # A question that never started gives its count back; the runs were never touched.
    gate.release("d#ask", refund=True)
    usage = json.loads(gate.usage_path.read_text())
    assert (usage["asks"], usage["guest_asks"], usage["runs"], usage["guest_runs"]) == (2, 2, 0, 0)


def test_asks_per_hour_and_one_in_flight(readability_console, monkeypatch):
    monkeypatch.setattr(analyze_runs, "ASKS_PER_HOUR", {"guest": 6, "member": 2, "admin": 120})
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))
    launched: list = []
    # The question is never answered: its job stays "running".
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: launched.append(kwargs))
    with running_server() as port:
        owner, cookie = _member(port)
        _finished_run("readability_ask_2", owner["id"], monkeypatch)
        first = _post(port, "/api/run/readability_ask_2/readability/ask", cookie, {"question": "One?"})
        # The first question is still being answered (its job runs).
        second = _post(port, "/api/run/readability_ask_2/readability/ask", cookie, {"question": "Two?"})
        for job in list(jobs._JOBS.values()):
            if job.get("run_id") == "readability_ask_2":
                job["status"] = "finished"
        third = _post(port, "/api/run/readability_ask_2/readability/ask", cookie, {"question": "Three?"})
        fourth = _post(port, "/api/run/readability_ask_2/readability/ask", cookie, {"question": "Four?"})
    assert first[0] == 202
    assert second[0] == 409 and second[2]["error"] == "Wait for the run (or the question before) to finish"
    # The refused question gave its hourly slot back.
    assert third[0] == 202
    assert fourth[0] == 429 and "2 questions per hour" in fourth[2]["error"]
    assert len(launched) == 2


def test_questions_need_a_finished_readability_run(readability_console, monkeypatch):
    with running_server() as port:
        owner, cookie = _member(port)
        jobs.workspace_dir("plain_ask_1")
        jobs._write_run({"run_id": "plain_ask_1", "engine": "plain", "status": "finished", "owner_id": owner["id"]})
        not_readability = _post(port, "/api/run/plain_ask_1/readability/ask", cookie, {"question": "Why?"})
        workspace = jobs.workspace_dir("readability_ask_early")
        readability_request.write(workspace, source={"kind": "paste", "text": PAPER}, caps={}, audience="graduate",
                                  focus="", role="member", route="api_key", model="kimi-k3",
                                  reasoning_effort="default", sponsored=False)
        jobs._write_run({"run_id": "readability_ask_early", "engine": "readability", "run_kind": "readability",
                         "status": "running", "owner_id": owner["id"], "workspace": str(workspace)})
        early = _post(port, "/api/run/readability_ask_early/readability/ask", cookie, {"question": "Why?"})
    assert not_readability[0] == 400
    assert early[0] == 409 and "no readable version" in early[2]["error"]


def test_a_lost_question_is_repaired_on_read():
    run_id = "readability_ask_lost"
    jobs._write_run({"run_id": run_id, "engine": "readability", "run_kind": "readability", "status": "finished",
                     "readability": {"ask": {"ask_id": "abcdefabcdef", "status": "running", "job_id": "gone",
                                             "question": "Why?"}}})
    record = jobs.reconcile_run_status(run_id)
    block = record["readability"]
    assert "ask" not in block and block["asks"][0]["status"] == "interrupted"
    assert block["asks"][0]["error"] == jobs.ASK_INTERRUPTED


# ---------------------------------------------------------------------------
# a whole run through jobs (runner subprocess, loopback sponsored broker)
# ---------------------------------------------------------------------------


class Broker(BaseHTTPRequestHandler):
    """The sponsored broker's chat endpoint on loopback, scripted by system prompt."""

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        system = next(message["content"] for message in payload["messages"] if message["role"] == "system")
        user = payload["messages"][-1]["content"]
        kind = KINDS.get(system)
        self.server.seen.append(kind)
        content = {"guide": guide_json, "check": check_json, "rewrite": lambda: readable(user),
                   "ask": lambda: "Because $2a+2b=2(a+b)$; see [[result:lem:sum]]."}[kind]()
        body = json.dumps({"model": payload["model"], "choices": [{"message": {"role": "assistant", "content": content},
                                                                   "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _wait(predicate, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_a_sponsored_run_and_a_question_through_jobs(gate, monkeypatch):
    monkeypatch.setattr(jobs, "_estimate_cost", lambda *_args, **_kwargs: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Broker)
    server.seen = []
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        issued: list = []
        _broker(monkeypatch, issued, base=f"http://127.0.0.1:{server.server_address[1]}/internal/v1")
        monkeypatch.setenv("NO_PROXY", "*")
        monkeypatch.setenv("no_proxy", "*")
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            # A manuscript file named like a proof must never be promoted.
            bundle.writestr("proof.tex", MARKED_PAPER)
        upload = {"name": "paper.zip", "data": base64.b64encode(archive.getvalue()).decode()}
        job = jobs.start_job(engine="readability", run_kind="readability", attachments=[upload], user=None,
                             model="kimi-k3", auth_route="api_key")
        assert job["credential_source"] == "sponsored_kimi" and issued[0]["engine"] == "math_harness"
        assert _wait(lambda: jobs.get_job(job["job_id"])["status"] != "running")
        assert jobs.get_job(job["job_id"])["status"] == "finished", jobs.get_job(job["job_id"])
        # The sponsored slot is freed last, once the record and the chat are written.
        assert _wait(lambda: not gate.holds(job["run_id"]))
        record = jobs._load_run_record(job["run_id"])
        workspace = Path(record["workspace"])
        assert record["run_kind"] == "readability" and record["status"] == "finished"
        assert record["problem_text_preview"] == "Readability · Sums of evens"
        assert record["readability"]["summary"]["faithful"] == 1 and record["readability"]["summary"]["calls"] == 3
        assert (workspace / "readability" / "source" / "proof.tex").is_file()
        assert not (workspace / "proof.md").exists() and not (workspace / "proof.tex").exists()
        assert not (workspace / "auto_pipeline.json").exists()
        assert server.seen == ["guide", "rewrite", "check"]
        assert "Readable version published: 1 of 1 parts rewritten (1 faithful)." in "\n".join(
            message["content"] for message in jobs.list_chat(job["run_id"]))

        result = jobs.start_readability_ask(job["run_id"], {"question": "Why is it even?", "node_id": "lem:sum"},
                                            user=None)
        assert result["status"] == "asking"
        assert _wait(lambda: not jobs.run_is_active(job["run_id"]))
        block = jobs._load_run_record(job["run_id"])["readability"]
        entry = block["asks"][-1]
        assert entry["status"] == "done", entry
        assert entry["answer"].startswith("Because $2a+2b=2(a+b)$") and entry["refs"] == ["lem:sum"]
        assert entry["part"] == 1 and entry["usage"]["calls"] == 1 and block["ask_usage"]["calls"] == 1
        assert server.seen[-1] == "ask"
        assert _wait(lambda: not gate.holds(readability_runs.ask_key(job["run_id"])))
        # One sponsored start today (the run); its question counts among the day's questions.
        usage = json.loads(gate.usage_path.read_text())
        assert (usage["runs"], usage["asks"]) == (1, 1) and gate.usage_today() == 1
    finally:
        server.shutdown()
        server.server_close()
