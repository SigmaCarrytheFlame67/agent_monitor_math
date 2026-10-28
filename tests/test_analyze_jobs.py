"""Analyze runs through jobs: routes, sponsored capacity, success, derived views.

Offline: the provider is a local fake (``test_analyze_runner.Provider``), the
sponsored broker is stubbed, and sources are pasted text.  The full-run test
starts the real runner subprocess through ``jobs.start_job``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pylatexenc")

from agent_monitor import analyze_runs, auto_pipeline, jobs, lean_verify, proof_graph, research  # noqa: E402
from agent_monitor import projects, run_sharing  # noqa: E402
from agent_monitor.analyze import graph as analysis_graph  # noqa: E402
from agent_monitor.analyze import request as analysis_request  # noqa: E402
from agent_monitor.runners import analyze_runner  # noqa: E402

from test_analyze_runner import LONG_PAPER, PAPER, Provider, default_script, own_key_env  # noqa: E402
from test_analyze_runner import guest_long_proof_analysis  # noqa: E402

TOKEN = "short-lived-loopback-token-" + "y" * 20
SECRET_PROOF = "Distributivity-unique-proof-marker-7731"
MARKED_PAPER = PAPER.replace("Distributivity.", SECRET_PROOF + ".")


@pytest.fixture()
def provider():
    servers = []

    def start(script):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server.daemon_threads = True
        server.seen, server.lock, server.script = [], threading.Lock(), script
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    for name in (analyze_runs.DAILY_CAP_ENV, analyze_runs.CONCURRENCY_ENV, analyze_runs.GUEST_DAILY_CAP_ENV,
                 analyze_runs.GUEST_CONCURRENCY_ENV, "AGENT_MONITOR_ANALYZE_ENABLED",
                 "AGENT_MONITOR_ANALYZE_GUESTS", "ANALYZE_CMD", "AGENT_MONITOR_CLI_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(analyze_runs.GATE, "usage_path", tmp_path / "analyze_usage.json")
    analyze_runs.GATE.reset()
    yield analyze_runs.GATE
    analyze_runs.GATE.reset()


def analysis_workspace(workspace: Path, server, *, text: str = PAPER, formalize: bool = False) -> dict:
    """Run the real Analyze pipeline in-process into ``workspace``."""
    workspace.mkdir(parents=True, exist_ok=True)
    analysis_request.write(workspace, source={"kind": "paste", "text": text}, caps={}, focus="",
                           formalize=formalize, role="member", route="api_key", model="deepseek-test",
                           reasoning_effort="default", sponsored=False)
    req = analysis_request.load(workspace)
    return analyze_runner.run_analysis(workspace, req, env=own_key_env(server), sink=lambda _e: None,
                                       sleep=lambda _s: None)


def write_record(run_id: str, workspace: Path, **fields) -> dict:
    record = {
        "run_id": run_id, "engine": "analyze", "run_kind": "analyze", "status": "finished",
        "workspace": str(workspace), "owner_id": 5, "guest": False, "formalize": False,
        "credential_source": "user", "auth_route": "api_key", "model": "deepseek-test",
        "problem_text": "Analyze · Sums of evens", "pipeline": jobs._pipeline_for("analyze"),
        "agents": [], "edges": [], "totals": {},
    }
    record.update(fields)
    from agent_monitor.analyze import record as analyze_record

    record.setdefault("analyze", analyze_record.run_block(workspace) or {})
    jobs._write_run(record)
    return record


# ---------------------------------------------------------------------------
# sponsored capacity
# ---------------------------------------------------------------------------


def test_sponsored_gate_holds_two_slots_and_a_persisted_daily_cap(gate, monkeypatch, tmp_path):
    gate.reserve("a", run_id="a")
    gate.reserve("b", run_id="b")
    with pytest.raises(analyze_runs.AnalyzeCapacityError) as busy:
        gate.reserve("c", run_id="c")
    assert busy.value.status == 429 and busy.value.retry_after == 60
    assert "Sponsored Analyze is busy (2 analyses are running)" in str(busy.value)
    assert "use your own API key" in str(busy.value)
    # A guest is not told to use a key it cannot have.
    assert "API key" not in str(gate.capacity_error(guest=True))
    # Releasing is idempotent and frees the slot; the daily count stays.
    assert gate.release("a") is True and gate.release("a") is False
    gate.reserve("c", run_id="c")
    usage = json.loads((tmp_path / "analyze_usage.json").read_text())
    assert usage["runs"] == 3 and usage["day"] == analyze_runs._utc_day()
    # A refund (nothing started) gives the count back.
    gate.release("c", refund=True)
    assert gate.usage_today() == 2
    # The count is persisted: a new gate (a restarted console) sees it.
    fresh = analyze_runs.SponsoredGate(usage_path=tmp_path / "analyze_usage.json")
    assert fresh.usage_today() == 2 and fresh.active() == 0
    monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "2")
    gate.release("b")
    with pytest.raises(analyze_runs.AnalyzeCapacityError) as daily:
        gate.reserve("d", run_id="d")
    assert str(daily.value) == analyze_runs.DAILY_LIMIT_MESSAGE
    assert daily.value.status == 429 and daily.value.retry_after >= 60
    assert str(gate.capacity_error(guest=True)) == analyze_runs.GUEST_DAILY_LIMIT_MESSAGE
    # 0 turns sponsored Analyze off (403, plain message).
    monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "0")
    off = gate.capacity_error()
    assert off.status == 403 and str(off) == analyze_runs.SPONSORED_OFF_MESSAGE
    # Yesterday's count does not carry over.
    monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "2")
    (tmp_path / "analyze_usage.json").write_text(json.dumps({"day": "2000-01-01", "runs": 99}))
    assert gate.usage_today() == 0
    gate.reserve("e", run_id="e")
    assert json.loads((tmp_path / "analyze_usage.json").read_text())["runs"] == 1


def test_sponsored_concurrency_is_configurable(gate, monkeypatch):
    monkeypatch.setenv(analyze_runs.CONCURRENCY_ENV, "1")
    gate.reserve("a", run_id="a")
    with pytest.raises(analyze_runs.AnalyzeCapacityError):
        gate.reserve("b", run_id="b")
    assert gate.snapshot()["concurrency"] == 1 and gate.snapshot()["active"] == 1


def test_guests_hold_only_their_share_of_the_sponsored_pool(gate, monkeypatch, tmp_path):
    # Two slots: guests may hold one of them, a member still gets the other.
    gate.reserve("g1", run_id="g1", guest=True)
    with pytest.raises(analyze_runs.AnalyzeCapacityError) as busy:
        gate.reserve("g2", run_id="g2", guest=True)
    assert busy.value.status == 429 and busy.value.retry_after == 60
    assert str(busy.value).startswith("Sponsored Analyze for guests is busy (1 running)")
    assert gate.capacity_error(guest=False) is None
    gate.reserve("m1", run_id="m1")
    assert gate.snapshot(guest=True)["guest_active"] == 1 and gate.snapshot()["active"] == 2
    gate.release("g1")
    gate.release("m1")
    # The daily pool: guests may use half of it (rounded down), members the rest.
    monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "5")
    assert analyze_runs.guest_sponsored_runs_per_day() == 2
    gate.reserve("g3", run_id="g3", guest=True)
    gate.release("g3")
    with pytest.raises(analyze_runs.AnalyzeCapacityError) as share:
        gate.reserve("g4", run_id="g4", guest=True)
    assert str(share.value) == analyze_runs.GUEST_SHARE_DAILY_MESSAGE and share.value.retry_after >= 60
    assert gate.snapshot(guest=True)["remaining_today"] == 0 and gate.snapshot()["remaining_today"] == 2
    gate.reserve("m2", run_id="m2")
    gate.release("m2")
    gate.reserve("m3", run_id="m3")
    usage = json.loads((tmp_path / "analyze_usage.json").read_text())
    assert (usage["runs"], usage["guest_runs"]) == (5, 2)
    # A refunded guest start gives back the guests' share too.
    gate.release("m3", refund=True)
    monkeypatch.setenv(analyze_runs.GUEST_DAILY_CAP_ENV, "3")
    gate.reserve("g5", run_id="g5", guest=True)
    gate.release("g5", refund=True)
    usage = json.loads((tmp_path / "analyze_usage.json").read_text())
    assert (usage["runs"], usage["guest_runs"]) == (4, 2)
    # 0 keeps guests off sponsored Analyze (and the listing says so).
    monkeypatch.setenv(analyze_runs.GUEST_DAILY_CAP_ENV, "0")
    off = gate.capacity_error(guest=True)
    assert off.status == 403 and str(off) == analyze_runs.GUEST_SPONSORED_OFF_MESSAGE
    assert gate.snapshot(guest=True)["available"] is False and gate.snapshot()["available"] is True
    # The share never exceeds the site cap, and a larger pool lends guests more slots.
    monkeypatch.setenv(analyze_runs.GUEST_DAILY_CAP_ENV, "99")
    assert analyze_runs.guest_sponsored_runs_per_day() == 5
    monkeypatch.setenv(analyze_runs.CONCURRENCY_ENV, "4")
    assert analyze_runs.guest_sponsored_concurrency() == 2


def _broker(monkeypatch, issued: list, *, fail: bool = False):
    from agent_monitor import sponsored_kimi_client as broker

    monkeypatch.setattr(broker, "configured", lambda: True)

    def credentials(**kwargs):
        issued.append(kwargs)
        if fail:
            raise broker.SponsoredKimiError("Sponsored Kimi credential attestation failed")
        return {"KIMI_API_KEY": TOKEN, "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
                "KIMI_REASONING_EFFORT": "high", "AGENT_MONITOR_SPONSORED_KIMI": "1",
                "AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610"}

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
    monkeypatch.setenv("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "1")
    return SimpleNamespace(threads=captured, records=records)


GUEST = {"id": 71, "email": "guest+x@public.local", "guest": True}
MEMBER = {"id": 72, "email": "member@example.test", "is_admin": False}


def test_guest_analysis_uses_the_sponsored_math_harness_scope_only(gate, monkeypatch, captured_start):
    issued: list = []
    _broker(monkeypatch, issued)
    # A guest's own Kimi key is never used for Analyze (sponsored only).
    monkeypatch.setattr(jobs, "_user_extra_env",
                        lambda user, *models: {"AGENT_MONITOR_PYTHON": "python", "KIMI_API_KEY": "guest-own-key-123456789"})
    job = jobs.start_job(engine="analyze", run_kind="analyze", source={"url": "https://arxiv.org/abs/2302.05537"},
                         user=GUEST)
    assert job["model"] == "kimi-k3" and job["run_kind"] == "analyze"
    assert job["credential_source"] == "sponsored_kimi" and job["formalize"] is False
    assert issued[0]["engine"] == "math_harness" and issued[0]["client_id"] == GUEST["id"]
    # The broker token must outlive the guest's deadline plus one call.
    assert issued[0]["minimum_ttl_seconds"] >= 2400 + 630
    env = captured_start.threads[0]["kwargs"]["extra_env"]
    assert env["KIMI_API_KEY"] == TOKEN and env["AGENT_MONITOR_SPONSORED_KIMI"] == "1"
    assert env["AGENT_MONITOR_GUEST"] == "1" and env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] == "8192"
    assert "guest-own-key-123456789" not in json.dumps(env)
    assert captured_start.threads[0]["kwargs"]["run_kind"] == "analyze"
    # The request the runner reads: guest role, sponsored route, guest caps.
    workspace = Path(captured_start.records[0]["workspace"])
    req = analysis_request.load(workspace)
    assert (req["role"], req["route"], req["sponsored"], req["model"]) == ("guest", "sponsored_kimi", True, "kimi-k3")
    assert req["source"] == {"kind": "url", "url": "https://arxiv.org/abs/2302.05537"}
    record = captured_start.records[0]
    assert record["run_kind"] == "analyze" and record["analyze"]["source"]["kind"] == "url"
    assert "analysis" not in record or record["analysis"] == {}
    assert record["problem_text"].startswith("Analyze · https://arxiv.org/abs/2302.05537")
    assert gate.holds(job["run_id"]) and gate.usage_today() == 1


def test_own_key_member_analysis_gets_no_sponsor_credential(gate, monkeypatch, captured_start):
    issued: list = []
    _broker(monkeypatch, issued)
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {
        "AGENT_MONITOR_PYTHON": "python", "OPENAI_API_KEY": "sk-member-own-123456789",
        "AGENT_MONITOR_SPONSORED_KIMI": "1"})
    # Subscriptions stay with the administrator (Project 1 policy).
    with pytest.raises(ValueError, match="only for the site administrator"):
        jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, model="gpt-5.6-sol",
                       user=MEMBER, auth_route="codex_subscription")
    job = jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, model="gpt-5.6-sol",
                         user=MEMBER)
    assert issued == [] and job["credential_source"] == "user" and job["auth_route"] == "api_key"
    env = captured_start.threads[0]["kwargs"]["extra_env"]
    assert env["OPENAI_API_KEY"] == "sk-member-own-123456789"
    assert env["AGENT_MONITOR_SPONSORED_KIMI"] == "0" and TOKEN not in json.dumps(env)
    assert "AGENT_MONITOR_SPONSORED_KIMI_URL" not in env
    req = analysis_request.load(Path(captured_start.records[0]["workspace"]))
    assert (req["role"], req["route"], req["sponsored"]) == ("member", "api_key", False)
    assert gate.active() == 0 and gate.usage_today() == 0
    # The runner's child environment is built from it: still no sponsor token.
    child: dict = {}
    jobs._apply_analyze_runner_env(child, env)
    assert child["AGENT_MONITOR_SPONSORED_KIMI"] == "0"


def test_a_member_blank_model_analysis_runs_on_a_model_its_keys_run(gate, monkeypatch, captured_start):
    """The Projects page sends no model ("Default account model").  A new
    account's saved default is Kimi K3; with only an OpenAI key (and no
    sponsored service) the analysis runs on the first OpenAI model, the one
    the console's Analyze form offers."""
    from cryptography.fernet import Fernet

    from agent_monitor import auth

    for name in ("AGENT_MONITOR_PUBLIC", "AGENT_MONITOR_SPONSORED_KIMI", "OPENAI_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    own_key = "sk-member-openai-123456789"
    auth.set_user_env(int(member["id"]), {"OPENAI_API_KEY": own_key})
    job = jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, user=member)
    assert (job["model"], job["credential_source"], job["auth_route"]) == ("gpt-6-astra", "user", "api_key")
    assert captured_start.threads[0]["kwargs"]["extra_env"]["OPENAI_API_KEY"] == own_key
    req = analysis_request.load(Path(captured_start.records[0]["workspace"]))
    assert (req["route"], req["sponsored"], req["model"]) == ("api_key", False, "gpt-6-astra")
    assert gate.usage_today() == 0
    # With no key left, the refusal names the key the saved default needs.
    auth.set_user_env(int(member["id"]), {}, clear=["OPENAI_API_KEY"])
    with pytest.raises(ValueError, match="Kimi K3 requires your own API key"):
        jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, user=member)
    assert len(captured_start.threads) == 1


def test_public_mode_keeps_member_analyses_on_sponsored_kimi(gate, monkeypatch, captured_start):
    """Public mode gives every account the server's provider keys, so a
    member's "own key" analysis would bill the operator (like the own-key CGM
    harness, which public mode removes): members analyze on sponsored Kimi K3."""
    from agent_monitor import account_policy, settings

    issued: list = []
    _broker(monkeypatch, issued)
    operator = {"AGENT_MONITOR_PYTHON": "python", "OPENAI_API_KEY": "sk-operator-shared-0000000000",
                "KIMI_API_KEY": "kimi-operator-shared-0000000000"}
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: dict(operator))
    monkeypatch.setattr(settings, "resolved_user_env", lambda user: {"AGENT_MONITOR_MODEL": "gpt-5"})
    monkeypatch.setenv("AGENT_MONITOR_PUBLIC", "1")
    assert account_policy.member_model_error(MEMBER, "analyze", "gpt-5") == account_policy.ANALYZE_SPONSORED_ONLY_NOTE
    assert account_policy.member_model_error(MEMBER, "analyze", "kimi-k3") is None
    assert account_policy.member_model_error({"id": 1, "is_admin": True}, "analyze", "gpt-5") is None
    with pytest.raises(ValueError, match="included Kimi K3"):
        jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, model="gpt-5", user=MEMBER)
    assert captured_start.threads == [] and gate.usage_today() == 0
    # No model named: Kimi K3 on the sponsored route (not the inherited default,
    # and never the operator's own Kimi key), inside the gate and the daily cap.
    job = jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, user=MEMBER)
    assert job["model"] == "kimi-k3" and job["credential_source"] == "sponsored_kimi"
    env = captured_start.threads[0]["kwargs"]["extra_env"]
    assert env["KIMI_API_KEY"] == TOKEN and "kimi-operator-shared" not in json.dumps(env)
    req = analysis_request.load(Path(captured_start.records[0]["workspace"]))
    assert (req["route"], req["sponsored"]) == ("sponsored_kimi", True)
    assert gate.holds(job["run_id"]) and gate.usage_today() == 1
    policy = analyze_runs.viewer_policy(MEMBER)
    assert policy["routes"] == ["sponsored_kimi"] and policy["caps"]["own_key"] is None
    # Without public mode the member keeps its own-key route.
    monkeypatch.delenv("AGENT_MONITOR_PUBLIC")
    assert account_policy.member_model_error(MEMBER, "analyze", "gpt-5") is None
    assert analyze_runs.viewer_policy(MEMBER)["routes"] == ["sponsored_kimi", "api_key"]


def test_public_mode_refuses_a_members_own_key_recheck(monkeypatch, tmp_path, provider):
    server = provider(default_script())
    workspace = jobs.workspace_dir("analyze_public_recheck_1")
    analysis_workspace(workspace, server)
    write_record("analyze_public_recheck_1", workspace, owner_id=MEMBER["id"])
    monkeypatch.setenv("AGENT_MONITOR_PUBLIC", "1")
    # Public mode: the member's environment carries the server's provider key.
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {
        "AGENT_MONITOR_PYTHON": "python", "DEEPSEEK_API_KEY": "sk-operator-shared-0000000000"})
    launched: list = []
    monkeypatch.setattr(jobs, "_execute_recheck", lambda **kwargs: launched.append(kwargs) or jobs._update_job(
        kwargs["job_id"], status="finished"))
    with pytest.raises(analyze_runs.AnalyzeRequestError, match="members can't use on this site") as refused:
        jobs.start_analysis_recheck("analyze_public_recheck_1", "lem:sum", user=MEMBER)
    assert refused.value.status == 403 and launched == []
    assert not jobs.run_is_active("analyze_public_recheck_1")


def test_a_recheck_or_question_about_an_analysis_on_another_kimi_model_is_refused(gate, monkeypatch, provider):
    """An analysis started on kimi-k2.5 on an own Kimi key, before Analyze
    refused other Kimi models, ran as Kimi K3 under that name (a Kimi
    endpoint is always asked for Kimi K3): a re-check of one of its results,
    or a question about it, would too.  Both are refused before anything is
    reserved, registered or launched."""
    server = provider(default_script())
    workspace = jobs.workspace_dir("analyze_other_kimi_1")
    analysis_workspace(workspace, server)
    write_record("analyze_other_kimi_1", workspace, owner_id=MEMBER["id"], model="kimi-k2.5")
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {
        "AGENT_MONITOR_PYTHON": "python", "KIMI_API_KEY": "sk-own-kimi-123456789"})
    launched: list = []

    def launch(**kwargs):
        launched.append(kwargs)
        jobs._update_job(kwargs["job_id"], status="finished")

    monkeypatch.setattr(jobs, "_execute_recheck", launch)
    monkeypatch.setattr(jobs, "_execute_ask", launch)
    released: list = []
    refusal = "Analyze runs Kimi K3 as its only Kimi model (kimi-k3); this analysis ran on kimi-k2.5"
    with pytest.raises(analyze_runs.AnalyzeRequestError) as rechecked:
        jobs.start_analysis_recheck("analyze_other_kimi_1", "lem:sum", user=MEMBER, on_exit=lambda: released.append(1))
    with pytest.raises(analyze_runs.AnalyzeRequestError) as asked:
        jobs.start_analysis_ask("analyze_other_kimi_1", {"question": "Why?"}, user=MEMBER,
                                on_exit=lambda: released.append(2))
    assert [(str(error.value), error.value.status) for error in (rechecked, asked)] == [(refusal, 400)] * 2
    assert launched == [] and released == [1, 2] and gate.usage_today() == 0
    assert not jobs.run_is_active("analyze_other_kimi_1")
    block = jobs._load_run_record("analyze_other_kimi_1")["analyze"]
    assert "recheck" not in block and "ask" not in block and not block.get("asks")
    # An analysis recorded on Kimi K3 in another case ran on Kimi K3: its re-check asks for kimi-k3.
    write_record("analyze_other_kimi_1", workspace, owner_id=MEMBER["id"], model=" Kimi-K3 ")
    jobs.start_analysis_recheck("analyze_other_kimi_1", "lem:sum", user=MEMBER)
    deadline = time.monotonic() + 30  # the re-check's worker thread
    while (not launched or jobs.run_is_active("analyze_other_kimi_1")) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [(item["model"], item["env"]["AGENT_MONITOR_ANALYZE_MODEL"]) for item in launched] == [
        ("kimi-k3", "kimi-k3")]
    assert not jobs.run_is_active("analyze_other_kimi_1")


def test_member_sponsored_analysis_is_bounded_by_the_broker_limits(gate, monkeypatch, captured_start):
    issued: list = []
    _broker(monkeypatch, issued)
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {"AGENT_MONITOR_PYTHON": "python"})
    jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, user=MEMBER,
                   analyze={"max_nodes": 7, "focus": "Lemma 2.2"})
    req = analysis_request.load(Path(captured_start.records[0]["workspace"]))
    # The effective caps: the member sponsored profile, lowered by max_nodes.
    assert req["caps"] == {"max_nodes": 7, "max_calls": 36, "max_seconds": 3600}
    assert captured_start.records[0]["analyze"]["caps"] == {"max_nodes": 7, "max_calls": 36}
    assert req["focus"] == "Lemma 2.2" and req["route"] == "sponsored_kimi"
    env = captured_start.threads[0]["kwargs"]["extra_env"]
    # The watchdog: member sponsored deadline 3600 s + one 630 s call + 300 s.
    assert env[analyze_runs.WATCHDOG_ENV] == str(3600 + 630 + 300)
    assert jobs._cli_timeout_seconds("analyze", env) == 3600 + 630 + 300


def test_admin_sponsored_analysis_stays_inside_the_broker_token(gate, monkeypatch, captured_start):
    issued: list = []
    _broker(monkeypatch, issued)
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {"AGENT_MONITOR_PYTHON": "python"})
    jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, user={"id": 1, "is_admin": True},
                   model="kimi-k3", auth_route="api_key")
    req = analysis_request.load(Path(captured_start.records[0]["workspace"]))
    assert (req["role"], req["route"]) == ("admin", "sponsored_kimi")
    # 200 calls and 7,200 s per broker token: the admin profile is held below them.
    assert req["caps"] == {"max_nodes": 256, "max_calls": analyze_runs.SPONSORED_MAX_CALLS,
                           "max_seconds": analyze_runs.SPONSORED_MAX_SECONDS}
    env = captured_start.threads[0]["kwargs"]["extra_env"]
    assert jobs._cli_timeout_seconds("analyze", env) == analyze_runs.SPONSORED_MAX_SECONDS + 630 + 300 <= 6600
    assert issued[0]["minimum_ttl_seconds"] <= 7200


def test_capacity_refusals_start_nothing_and_failures_return_the_slot(gate, monkeypatch, captured_start):
    issued: list = []
    _broker(monkeypatch, issued, fail=True)
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {"AGENT_MONITOR_PYTHON": "python"})
    with pytest.raises(Exception, match="attestation failed"):
        jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, user=MEMBER)
    # The broker refused after the slot was taken: slot and daily count return.
    assert issued and gate.active() == 0 and gate.usage_today() == 0 and captured_start.threads == []
    gate.reserve("x", run_id="x")
    gate.reserve("y", run_id="y")
    _broker(monkeypatch, issued)
    with pytest.raises(analyze_runs.AnalyzeCapacityError, match="busy"):
        jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, user=MEMBER)
    assert captured_start.threads == [] and gate.usage_today() == 2


def test_the_worker_releases_the_sponsored_slot_on_every_exit(gate, monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def crash(**_kwargs):
        raise RuntimeError("runner crashed")

    monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", crash)
    for run_id, failure in (("analyze_crash_1", crash), ("analyze_stop_1", None)):
        gate.reserve(run_id, run_id=run_id)
        if failure is None:
            def stopped(**_kwargs):
                raise jobs.StopRequested()

            monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", stopped)
            jobs._write_run({"run_id": run_id, "engine": "analyze", "run_kind": "analyze", "status": "running"})
        jobs._execute_job(job_id=run_id[-1], run_id=run_id, engine="analyze", problem_id="p",
                          problem_text="Analyze · x", problem_path=None, model="kimi-k3", max_iterations=1,
                          workspace=str(workspace), extra_env={"AGENT_MONITOR_SPONSORED_KIMI": "1"},
                          owner_id=None, guest=False, run_kind="analyze")
        assert not gate.holds(run_id), run_id
    record = jobs._load_run_record("analyze_crash_1")
    assert record["status"] == "failed" and record["run_kind"] == "analyze"


def test_cli_watchdog_follows_the_analysis_caps():
    env = {analyze_runs.WATCHDOG_ENV: "3330"}
    assert jobs._cli_timeout_seconds("analyze", env) == 3330
    # An operator timeout for the other engines never cuts an analysis short.
    assert jobs._cli_timeout_seconds("analyze", {**env, "AGENT_MONITOR_CLI_TIMEOUT": "600",
                                                  "AGENT_MONITOR_ANALYZE_TIMEOUT": "60"}) == 3330
    # Sponsored runs stay inside the broker token's lifetime.
    assert jobs._cli_timeout_seconds("analyze", {analyze_runs.WATCHDOG_ENV: "15000",
                                                  "AGENT_MONITOR_SPONSORED_KIMI": "1"}) == 6600
    assert jobs._cli_timeout_seconds("analyze", {}) == 14_400 + 900 + 300


def test_runner_env_carries_the_github_token_and_data_dir_for_analyze_only(monkeypatch):
    from agent_monitor import DATA_DIR
    from agent_monitor.subprocess_env import child_process_env

    monkeypatch.setenv("AGENT_MONITOR_ANALYZE_GITHUB_TOKEN", "github_pat_test_only_1234567890")
    extra = {"AGENT_MONITOR_SPONSORED_KIMI": "1", "KIMI_API_KEY": TOKEN}
    env = child_process_env(extra=extra)
    assert "AGENT_MONITOR_ANALYZE_GITHUB_TOKEN" not in env  # every other child
    jobs._apply_analyze_runner_env(env, extra)
    assert env["AGENT_MONITOR_ANALYZE_GITHUB_TOKEN"] == "github_pat_test_only_1234567890"
    assert env["AGENT_MONITOR_DATA_DIR"] == str(DATA_DIR)
    assert env["AGENT_MONITOR_SPONSORED_KIMI"] == "1" and env["KIMI_API_KEY"] == TOKEN


def test_continuation_and_chat_are_refused_for_analyses(tmp_path):
    workspace = jobs.workspace_dir("analyze_continue_1")
    jobs._write_run({"run_id": "analyze_continue_1", "engine": "analyze", "run_kind": "analyze",
                     "status": "finished", "workspace": str(workspace)})
    with pytest.raises(ValueError, match="can't be continued"):
        jobs.continue_run("analyze_continue_1", user=None)
    with pytest.raises(ValueError) as refused:
        jobs.send_human_message("analyze_continue_1", "keep going", user=None)
    assert str(refused.value) == analyze_runs.CONTINUE_REFUSAL
    assert jobs.list_chat("analyze_continue_1") == []


def test_reconciled_analysis_is_not_offered_continue():
    jobs._write_run({"run_id": "analyze_orphan_1", "engine": "analyze", "run_kind": "analyze",
                     "status": "running", "updated_at": "2000-01-01T00:00:00+00:00"})
    run = jobs.reconcile_run_status("analyze_orphan_1", min_age_seconds=0)
    assert run["status"] == "interrupted" and run["interruption"]["recoverable"] is False
    assert "Start a new analysis" in jobs.list_chat("analyze_orphan_1")[-1]["content"]


# ---------------------------------------------------------------------------
# a full run through jobs (real runner subprocess, fake provider)
# ---------------------------------------------------------------------------


def _wait(predicate, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_full_analysis_through_jobs_publishes_report_graph_and_record(gate, provider, monkeypatch):
    # Cost estimates would fetch LiteLLM's public price list; stay offline.
    monkeypatch.setattr(jobs, "_estimate_cost", lambda *_args, **_kwargs: None)
    server = provider(default_script({"lem:sum": "error"}))
    for name, value in own_key_env(server).items():
        if name.startswith(("DEEPSEEK", "NO_PROXY", "no_proxy")):
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", raising=False)
    job = jobs.start_job(engine="analyze", run_kind="analyze", source={"text": MARKED_PAPER},
                         model="deepseek-test", user=None, auth_route="api_key")
    assert job["problem_preview"] == "Analyze · Sums of evens"
    assert _wait(lambda: jobs.get_job(job["job_id"])["status"] != "running")
    assert jobs.get_job(job["job_id"])["status"] == "finished", jobs.get_job(job["job_id"])
    record = jobs._load_run_record(job["run_id"])
    workspace = Path(record["workspace"])
    assert record["run_kind"] == "analyze" and record["status"] == "finished"
    assert record["problem_text_preview"] == "Analyze · Sums of evens"
    block = record["analyze"]
    assert block["schema"] == "ansatze-analyze-v1" and block["source"]["title"] == "Sums of evens"
    assert block["summary"]["error"] == 1 and block["summary"]["main_closure"] == "error"
    assert block["summary"]["route"] == "api_key" and block["summary"]["calls"] == len(server.seen)
    # run["analysis"] stays the Monitor block.
    assert "summary" not in (record.get("analysis") or {})
    assert (workspace / "analysis" / "report.md").is_file()
    assert (workspace / "analysis" / "analysis_graph.json").is_file()
    assert not (workspace / "proof.md").exists() and not (workspace / "proof.tex").exists()
    # The derived pipeline projected the graph (no model call) and left Lean off.
    assert _wait(lambda: (auto_pipeline.load_state(workspace) or {}).get("status") not in (None, "running"), 30)
    state = auto_pipeline.load_state(workspace)
    assert state["stages"]["informal_dag"]["status"] == "done"
    assert state["stages"]["informal_dag"]["result"]["derived_executor"] == "analysis-projection"
    assert {state["stages"][name]["status"] for name in ("lean", "formal_dag", "coverage")} == {"off"}
    assert proof_graph.load_cached(workspace)["model"] == "analyze-graph"
    assert len(server.seen) == 4  # map + three checks; nothing derived called the model

    # Re-check one node in the background with the run's route.
    server.script = default_script({"lem:sum": "checked"})
    result = jobs.start_analysis_recheck(job["run_id"], "lem:sum", user=None)
    assert result["status"] == "rechecking" and result["node_id"] == "lem:sum"
    assert _wait(lambda: not jobs.run_is_active(job["run_id"]))
    record = jobs._load_run_record(job["run_id"])
    recheck = record["analyze"]["recheck"]
    assert recheck["status"] == "done" and recheck["result"]["verdict"] == "checked"
    assert record["analyze"]["summary"]["error"] == 0 and record["status"] == "finished"
    assert "not a certificate" in jobs.list_chat(job["run_id"])[-1]["content"]
    # The shared snapshot never carries the manuscript beyond its statements.
    snapshot = run_sharing.snapshot(job["run_id"], record)
    blob = json.dumps(snapshot)
    assert SECRET_PROOF not in blob and "pasted.tex" not in blob
    assert snapshot["kind"] == "analysis"
    assert snapshot["proof"].startswith("# Analysis: Sums of evens")
    statuses = {node["id"]: node["status"] for node in snapshot["informal_graph"]["nodes"]}
    assert statuses["lem:sum"] == "checked by a model verifier"
    assert "verified" not in blob.lower().replace("not a certificate", "")
    # And a re-check of something that is not a checkable result is refused.
    with pytest.raises(analyze_runs.AnalyzeRequestError):
        jobs.start_analysis_recheck(job["run_id"], "no-such-node", user=None)


def test_recheck_calls_reach_the_usage_ledger(gate, provider, monkeypatch):
    import sqlite3

    from agent_monitor import usage_ledger

    monkeypatch.setattr(jobs, "_estimate_cost", lambda *_args, **_kwargs: 0.0025)
    server = provider(default_script({"lem:sum": "gap"}))
    for name, value in own_key_env(server).items():
        if name.startswith(("DEEPSEEK", "NO_PROXY", "no_proxy")):
            monkeypatch.setenv(name, value)
    workspace = jobs.workspace_dir("analyze_recheck_ledger_1")
    analysis_workspace(workspace, server)
    write_record("analyze_recheck_ledger_1", workspace, owner_id=None, totals={"input_tokens": 100})
    before = len(server.seen)
    jobs.start_analysis_recheck("analyze_recheck_ledger_1", "lem:sum", user=None)
    assert _wait(lambda: not jobs.run_is_active("analyze_recheck_ledger_1"))
    calls = len(server.seen) - before
    record = jobs._load_run_record("analyze_recheck_ledger_1")
    recheck = record["analyze"]["recheck"]
    assert recheck["status"] == "done" and calls >= 1
    assert recheck["usage"]["calls"] == calls and recheck["usage"]["input_tokens"] > 0
    tally = record["analyze"]["rechecks"]
    assert tally["started"] == 1 and tally["calls"] == calls and tally["cost_usd"] == pytest.approx(0.0025 * calls)
    with sqlite3.connect(usage_ledger.DB_PATH) as conn:
        rows = conn.execute("SELECT stage, calls, input_tokens, output_tokens, cost_usd FROM usage_daily "
                            "WHERE run_id=?", ("analyze_recheck_ledger_1",)).fetchall()
    booked = {row[0]: row[1:] for row in rows}
    assert booked["recheck"][0] == calls and booked["recheck"][1] == recheck["usage"]["input_tokens"]
    assert booked["recheck"][3] == pytest.approx(0.0025 * calls)
    # Booked once, as a re-check: the analysis's engine usage is unchanged.
    assert booked["engine"][1] == 100 and record["totals"]["input_tokens"] == 100


def test_recheck_usage_prices_cache_writes_at_their_own_rate(monkeypatch):
    priced: list[dict] = []
    monkeypatch.setattr(jobs, "_estimate_cost", lambda _model, **kwargs: priced.append(kwargs) or 0.01)

    def book(usage: dict, model: str) -> dict:
        output = json.dumps({"type": "turn.completed", "stage": "check-1", "usage": usage})
        return jobs._book_recheck_usage("analyze_recheck_cache_1", output, model=model)

    # Claude Code reports cache reads and writes apart from its input.
    claude = book({"input_tokens": 10, "cache_read_tokens": 1000, "cached_input_tokens": 1000,
                   "cache_write_tokens": 20000, "output_tokens": 500, "input_excludes_cache": True}, "claude-sonnet-5")
    # Codex counts cached input inside its input and writes no cache.
    codex = book({"input_tokens": 5000, "cached_input_tokens": 4000, "output_tokens": 300}, "gpt-6-sol")
    assert priced == [
        {"input_tokens": 10, "output_tokens": 500, "cache_read": 1000, "cache_write": 20000},
        {"input_tokens": 1000, "output_tokens": 300, "cache_read": 4000, "cache_write": 0},
    ]
    # The tally keeps the input with its cache, as the run's totals do.
    assert (claude["input_tokens"], codex["input_tokens"]) == (21010, 5000)
    assert claude["cost_usd"] == codex["cost_usd"] == 0.01


@pytest.mark.parametrize("node_id", ["lem:sum", "lem:rep"])
def test_the_recheck_watchdog_follows_the_length_of_the_proof(gate, provider, monkeypatch, node_id):
    server = provider(default_script())
    for name, value in own_key_env(server).items():
        if name.startswith(("DEEPSEEK", "NO_PROXY", "no_proxy")):
            monkeypatch.setenv(name, value)
    run_id = "analyze_recheck_watchdog_" + node_id.replace(":", "_")
    workspace = jobs.workspace_dir(run_id)
    analysis_workspace(workspace, server, text=LONG_PAPER)
    write_record(run_id, workspace, owner_id=None)
    launched: list[dict] = []
    monkeypatch.setattr(jobs, "_execute_recheck", lambda **kwargs: launched.append(kwargs)
                        or jobs._update_job(kwargs["job_id"], status="finished"))
    jobs.start_analysis_recheck(run_id, node_id, user=None)
    meta = next(node["meta"] for node in analysis_graph.load(workspace)["nodes"] if node["id"] == node_id)
    parts = meta.get("parts", 1)
    assert (parts > analyze_runner.RECHECK_MAX_CALLS) == (node_id == "lem:rep")
    # The runner's own time limit for this proof, one in-flight call and publication.
    seconds = analyze_runner.RECHECK_SECONDS * max(analyze_runner.RECHECK_MAX_CALLS, parts + 2) \
        // analyze_runner.RECHECK_MAX_CALLS
    assert launched[0]["timeout"] == seconds + jobs._analyze_api_timeout(False) + jobs._RECHECK_GRACE_SECONDS


@pytest.mark.parametrize(("route", "model", "effort", "operator", "expected"), [
    # As in the analysis: Extra high and Ultra calls on a linked account get 30 minutes.
    ("codex_subscription", "gpt-6-sol", "max", {}, ("1800", "1800")),
    ("claude_subscription", "claude-opus-4-8", "xhigh", {}, ("1800", "1800")),
    ("codex_subscription", "gpt-6-sol", "high", {}, (None, None)),
    ("api_key", "deepseek-test", "default", {}, (None, None)),
    # An operator's limit wins, the shared Claude Code one too (the Analyze one would override it).
    ("claude_subscription", "claude-opus-4-8", "max", {"AGENT_MONITOR_CLAUDE_TIMEOUT": "900"}, ("1800", None)),
])
def test_an_extra_high_or_ultra_recheck_keeps_the_long_call_limit(provider, monkeypatch, route, model, effort,
                                                                    operator, expected):
    names = ("AGENT_MONITOR_CODEX_TIMEOUT", "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT")
    for name in (*names, "AGENT_MONITOR_CLAUDE_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in operator.items():
        monkeypatch.setenv(name, value)
    server = provider(default_script())
    run_id = f"analyze_recheck_effort_{route}_{effort}"
    workspace = jobs.workspace_dir(run_id)
    analysis_workspace(workspace, server)
    write_record(run_id, workspace, owner_id=1, auth_route=route, model=model, reasoning_effort=effort,
                 speed_mode="standard")
    real_configure = jobs._configure_auth_route

    def linked_account(env, *, route, user_id):
        # No account is linked here: the route's own flag stands in for one.
        configured = real_configure(env, route="api_key", user_id=None)
        flag = {"codex_subscription": "AGENT_MONITOR_CODEX_SUBSCRIPTION",
                "claude_subscription": "AGENT_MONITOR_CLAUDE_SUBSCRIPTION"}.get(route)
        if flag:
            configured[flag] = "1"
        return configured

    monkeypatch.setattr(jobs, "_configure_auth_route", linked_account)
    monkeypatch.setattr(jobs, "_validate_subscription_model", lambda **_kwargs: None)
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {"DEEPSEEK_API_KEY": "test-key"})
    monkeypatch.setattr(analyze_runs, "availability_error", lambda user: None)
    launched: list[dict] = []
    monkeypatch.setattr(jobs, "_execute_recheck", lambda **kwargs: launched.append(kwargs)
                        or jobs._update_job(kwargs["job_id"], status="finished"))
    jobs.start_analysis_recheck(run_id, "lem:sum", user={"id": 1, "email": "a@example.test", "is_admin": True})
    env = launched[0]["env"]
    assert tuple(env.get(name) for name in names) == expected
    assert env.get("AGENT_MONITOR_CLAUDE_TIMEOUT") == operator.get("AGENT_MONITOR_CLAUDE_TIMEOUT")
    assert env.get("AGENT_MONITOR_RUN_REASONING_EFFORT") == effort
    # The re-check's own watchdog is unchanged: every call stays capped at its deadline.
    assert launched[0]["timeout"] == analyze_runner.RECHECK_SECONDS + jobs._analyze_api_timeout(False) \
        + jobs._RECHECK_GRACE_SECONDS


def test_a_recheck_that_reaches_no_model_gives_its_sponsored_start_back(gate, tmp_path):
    run_id = "analyze_recheck_refund_1"
    jobs._write_run({"run_id": run_id, "engine": "analyze", "run_kind": "analyze", "status": "finished"})
    key = analyze_runs.recheck_key(run_id)
    gate.reserve(key, run_id=run_id)
    assert gate.usage_today() == 1
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "AGENT_MONITOR_DATA_DIR": str(tmp_path / "data"),
           "AGENT_MONITOR_ENV_PATH": str(tmp_path / "server.env")}
    (tmp_path / "server.env").touch()
    (tmp_path / "ws").mkdir()
    # No analysis/request.json: the runner exits 2 (configuration) before any model call.
    jobs._execute_recheck(run_id=run_id, job_id="rf1", node_id="lem:rep", label="Lemma 2.1",
                          workspace=tmp_path / "ws", env=env, timeout=120, gate_key=key, on_exit=None,
                          started_at=jobs._now())
    recheck = jobs._load_run_record(run_id)["analyze"]["recheck"]
    assert recheck["status"] == "failed" and "request.json is missing" in recheck["error"]
    assert not gate.holds(key) and gate.usage_today() == 0


def test_a_guest_recheck_of_a_long_proof_gives_its_sponsored_start_back(gate, provider, tmp_path):
    server = provider(default_script())
    run_id = "analyze_recheck_guest_long_1"
    workspace = jobs.workspace_dir(run_id)
    env = guest_long_proof_analysis(workspace, server)
    write_record(run_id, workspace, owner_id=41, guest=True, credential_source="sponsored_kimi", model="kimi-k3")
    guest = {"id": 41, "guest": True}
    released: list = []
    # Refused at once, as in the analysis: nothing is reserved or counted.
    with pytest.raises(analyze_runs.AnalyzeRequestError, match="Too long for a guest check"):
        jobs.start_analysis_recheck(run_id, "lem:rep", user=guest, on_exit=lambda: released.append(1))
    assert released == [1] and gate.usage_today() == 0
    assert jobs.recheck_count(jobs._load_run_record(run_id)) == 0 and not jobs.run_is_active(run_id)
    # The runner refuses it too, before any call (exit 2): the sponsored start comes back.
    key = analyze_runs.recheck_key(run_id)
    gate.reserve(key, run_id=run_id, guest=True)
    assert (gate.usage_today(), gate.guest_usage_today()) == (1, 1)
    before = len(server.seen)
    (tmp_path / "server.env").touch()
    jobs._execute_recheck(run_id=run_id, job_id="rg1", node_id="lem:rep", label="Lemma 2.1", workspace=workspace,
                          env={**env, "AGENT_MONITOR_DATA_DIR": str(tmp_path / "data"),
                               "AGENT_MONITOR_ENV_PATH": str(tmp_path / "server.env")},
                          timeout=120, gate_key=key, on_exit=None, started_at=jobs._now(), model="kimi-k3")
    recheck = jobs._load_run_record(run_id)["analyze"]["recheck"]
    assert recheck["status"] == "failed" and recheck["error"] == analyze_runner.GUEST_TOO_LONG
    assert len(server.seen) == before
    assert not gate.holds(key) and (gate.usage_today(), gate.guest_usage_today()) == (0, 0)


def test_recheck_refusals(tmp_path, provider):
    server = provider(default_script())
    with pytest.raises(FileNotFoundError):
        jobs.start_analysis_recheck("analyze_missing_1", "lem:sum", user=None)
    jobs._write_run({"run_id": "plain_not_analysis_1", "engine": "plain", "status": "finished"})
    with pytest.raises(analyze_runs.AnalyzeRequestError, match="Only Analyze runs"):
        jobs.start_analysis_recheck("plain_not_analysis_1", "lem:sum", user=None)
    workspace = jobs.workspace_dir("analyze_recheck_ref_1")
    analysis_workspace(workspace, server)
    write_record("analyze_recheck_ref_1", workspace, status="running")
    with pytest.raises(analyze_runs.AnalyzeRequestError) as busy:
        jobs.start_analysis_recheck("analyze_recheck_ref_1", "lem:sum", user=None)
    assert busy.value.status == 409
    write_record("analyze_recheck_ref_1", workspace, status="finished")
    with pytest.raises(analyze_runs.AnalyzeRequestError, match="node_id"):
        jobs.start_analysis_recheck("analyze_recheck_ref_1", "../etc", user=None)
    released: list = []
    with pytest.raises(analyze_runs.AnalyzeRequestError):
        jobs.start_analysis_recheck("analyze_recheck_ref_1", "Not A Node", user=None, on_exit=lambda: released.append(1))
    assert released == [1]


def test_stopping_a_recheck_keeps_the_analysis_status():
    jobs._write_run({"run_id": "analyze_stop_recheck_1", "engine": "analyze", "run_kind": "analyze",
                     "status": "finished"})
    with jobs._LOCK:
        jobs._JOBS["rc1"] = {"job_id": "rc1", "run_id": "analyze_stop_recheck_1", "kind": "recheck",
                             "engine": "analyze", "status": "running"}
    try:
        result = jobs.stop_run("analyze_stop_recheck_1")
    finally:
        with jobs._LOCK:
            jobs._JOBS.pop("rc1", None)
        jobs._stop_event("analyze_stop_recheck_1").clear()
    assert result["recheck"] is True and result["status"] == "stopped"
    assert jobs._load_run_record("analyze_stop_recheck_1")["status"] == "finished"


def _lost_recheck(run_id: str) -> None:
    """A finished analysis whose re-check worker died with the old process."""
    jobs._write_run({"run_id": run_id, "engine": "analyze", "run_kind": "analyze", "status": "finished",
                     "analyze": {"schema": "ansatze-analyze-v1", "summary": {"nodes": 3},
                                 "rechecks": {"started": 1},
                                 "recheck": {"node_id": "lem:sum", "label": "Lemma 2.2", "status": "running",
                                             "job_id": "lostjob001", "started_at": "2026-10-11T07:42:14+00:00"}}})


def test_a_recheck_lost_with_its_worker_is_repaired_on_read_at_startup_and_by_stop():
    # On read (every GET /api/run/<id> reconciles first).
    _lost_recheck("analyze_lost_recheck_1")
    run = jobs.reconcile_run_status("analyze_lost_recheck_1")
    recheck = run["analyze"]["recheck"]
    assert recheck["status"] == "interrupted" and recheck["error"] == jobs.RECHECK_INTERRUPTED
    assert recheck["node_id"] == "lem:sum" and recheck["finished_at"]
    stored = jobs._load_run_record("analyze_lost_recheck_1")
    assert stored["status"] == "finished" and stored["analyze"]["recheck"]["status"] == "interrupted"
    assert stored["analyze"]["rechecks"] == {"started": 1}
    assert "interrupted" in jobs.list_chat("analyze_lost_recheck_1")[-1]["content"]
    # At startup: the manifest flags the analysis although it is not "running".
    _lost_recheck("analyze_lost_recheck_2")
    manifest = json.loads((jobs._cache_dir() / "manifest.json").read_text())
    entry = next(item for item in manifest["runs"] if item["run_id"] == "analyze_lost_recheck_2")
    assert entry["recheck_running"] is True and entry["status"] == "finished"
    jobs.reconcile_stale_running_runs(min_age_seconds=0)
    assert jobs._load_run_record("analyze_lost_recheck_2")["analyze"]["recheck"]["status"] == "interrupted"
    manifest = json.loads((jobs._cache_dir() / "manifest.json").read_text())
    assert next(item for item in manifest["runs"] if item["run_id"] == "analyze_lost_recheck_2")[
        "recheck_running"] is False
    # Stop repairs it too (nothing runs, so it used to answer not_running and keep it).
    _lost_recheck("analyze_lost_recheck_3")
    result = jobs.stop_run("analyze_lost_recheck_3")
    jobs._stop_event("analyze_lost_recheck_3").clear()
    assert result["status"] == "not_running" and result["recheck"] == "interrupted"
    assert jobs._load_run_record("analyze_lost_recheck_3")["analyze"]["recheck"]["status"] == "interrupted"


def test_a_running_recheck_is_left_alone():
    _lost_recheck("analyze_live_recheck_1")
    with jobs._LOCK:
        jobs._JOBS["lostjob001"] = {"job_id": "lostjob001", "run_id": "analyze_live_recheck_1", "kind": "recheck",
                                    "engine": "analyze", "status": "running"}
    try:
        run = jobs.reconcile_run_status("analyze_live_recheck_1")
        jobs.reconcile_stale_running_runs(min_age_seconds=0)
    finally:
        with jobs._LOCK:
            jobs._JOBS.pop("lostjob001", None)
    assert run["analyze"]["recheck"]["status"] == "running"
    assert jobs._load_run_record("analyze_live_recheck_1")["analyze"]["recheck"]["status"] == "running"


# ---------------------------------------------------------------------------
# derived views
# ---------------------------------------------------------------------------


def test_pipeline_formalizes_main_statements_only_when_asked(tmp_path, provider, monkeypatch):
    server = provider(default_script())
    analysis_workspace(tmp_path, server, formalize=True)
    calls: list = []

    def generate(**kwargs):
        calls.append(kwargs)
        return {"status": "incomplete", "status_label": lean_verify.STATEMENTS_ONLY_LABEL, "model": "m"}

    monkeypatch.setattr(lean_verify, "generate", generate)
    monkeypatch.setattr(proof_graph, "generate", lambda **kwargs: {"graph": {"nodes": []}, "model": "lean-parse"})
    record = {"run_id": "analyze_formal_1", "engine": "analyze", "run_kind": "analyze", "status": "finished",
              "formalize": True}
    auto_pipeline._run_batch(tmp_path, record, None, "deepseek-test", "analyze")
    assert len(calls) == 1
    call = calls[0]
    assert call["prove"] is False and call["targets"] == ["thm:main", "lem:rep", "lem:sum"][:len(call["targets"])]
    assert "thm:main" in call["targets"] and "The sum of two even integers is even" in call["problem_override"]
    assert "Informal: <fact_id>" in call["guidance"] and "every" in call["guidance"]
    assert call["source_label"] == "analysis/analysis_graph.json"
    stages = auto_pipeline.load_state(tmp_path)["stages"]
    assert stages["informal_dag"]["result"]["derived_executor"] == "analysis-projection"
    assert stages["lean"]["result"]["scope"] == "statements"
    assert lean_verify.STATEMENTS_ONLY_LABEL in stages["lean"]["detail"]

    # Informal only (the Analyze default): no Lean, the stages say how to formalize.
    calls.clear()
    auto_pipeline._run_batch(tmp_path, {**record, "formalize": False}, None, "deepseek-test", "analyze")
    assert calls == []
    stages = auto_pipeline.load_state(tmp_path)["stages"]
    assert stages["lean"]["status"] == "off" and "select results in the DAG" in stages["lean"]["detail"]
    # A failed analysis is never formalized automatically.
    auto_pipeline._run_batch(tmp_path, {**record, "status": "failed"}, None, "deepseek-test", "analyze")
    assert calls == [] and "did not finish" in auto_pipeline.load_state(tmp_path)["stages"]["lean"]["detail"]


def test_the_pipeline_waits_as_long_as_the_analysis_may_run(tmp_path, monkeypatch):
    # An administrator's own-key analysis may run 14,400 s; the pipeline used
    # to give up after AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT (7,200 s by default)
    # and mark every stage "The analysis published no graph".
    analysis_request.write(tmp_path, source={"kind": "paste", "text": PAPER},
                           caps={"max_nodes": 256, "max_calls": 300, "max_seconds": 14_400}, focus="",
                           formalize=True, role="admin", route="api_key", model="deepseek-test",
                           reasoning_effort="default", sponsored=False)
    record = {"run_id": "analyze_long_1", "engine": "analyze", "run_kind": "analyze", "status": "running",
              "formalize": True}
    monkeypatch.setattr(auto_pipeline, "_run_record", lambda run_id: dict(record))
    monkeypatch.setenv("AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT", "60")
    waits: list = []
    monkeypatch.setattr(auto_pipeline, "_wait_for_source",
                        lambda run_id, workspace, timeout: waits.append(timeout) or ("", dict(record)))
    auto_pipeline._run("analyze_long_1", tmp_path, None, "deepseek-test", "analyze")
    # The watchdog of that analysis: 14,400 s + one call + margin.
    assert waits[0] >= 14_400 + 900 + 300 and waits[0] == auto_pipeline.analysis_wait_seconds(tmp_path)
    # Had it still been running, the stages say so instead of "published no graph".
    stages = auto_pipeline.load_state(tmp_path)["stages"]
    assert {stage["status"] for stage in stages.values()} == {"skipped"}
    assert all("still running" in stage["detail"] and "no graph" not in stage["detail"] for stage in stages.values())
    # A proof run keeps the operator's timeout.
    waits.clear()
    record.update(engine="plain", run_kind="prove")
    auto_pipeline._run("plain_long_1", tmp_path / "plain", None, "m", "plain")
    assert waits == [60.0]


def test_analysis_source_signature_needs_a_finished_published_graph(tmp_path, provider):
    server = provider(default_script())
    record = {"engine": "analyze", "run_kind": "analyze", "status": "running"}
    assert auto_pipeline._source_signature(tmp_path, record) == ""
    analysis_workspace(tmp_path, server)
    assert auto_pipeline._source_signature(tmp_path, record) == ""
    signature = auto_pipeline._source_signature(tmp_path, {**record, "status": "finished"})
    assert signature.startswith("analysis:")
    # A report is never treated as the proof of an analysis.
    (tmp_path / "proof.md").write_text("# Not this\n" + "x" * 300)
    assert auto_pipeline._source_signature(tmp_path, {**record, "status": "finished"}) == signature


def test_an_analysis_dag_is_never_regenerated_from_its_report(tmp_path, provider, monkeypatch):
    server = provider(default_script())
    analysis_workspace(tmp_path, server)
    (tmp_path / "proof_graph.json").unlink()
    monkeypatch.setattr(proof_graph, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model")))
    record = {"engine": "analyze", "run_kind": "analyze", "status": "finished"}
    result = proof_graph.generate(workspace=tmp_path, run_record=record, user=None, kind="informal")
    assert result["model"] == "analyze-graph" and result["derivation"] == "deterministic"
    assert proof_graph.load_or_build_informal(tmp_path, record)["model"] == "analyze-graph"
    # Files a proof agent might write do not make a proof run an analysis.
    assert proof_graph._is_analysis_record({"engine": "plain"}) is False


def test_statement_only_lean_never_proves_or_repairs(tmp_path, monkeypatch):
    class Session:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.status = "failed"
            self.checker_reached = True
            self.lean = "theorem main : True := sorry\n"
            self.check = {"ok": True}
            self.broker_lost = False
            self.lock = None
            self.main_decl = "main"
            self.notes = ""
            self.stop_reason = ""
            self.statement_audit = None
            self.attempts = []
            self.title = "t"
            self.author_model = "m"
            self.citations = []

        def statement_phase(self):
            self.status = "incomplete"

        def statement_audit_phase(self):
            pass

        def lock_statement(self):
            pass

        def prove(self):
            raise AssertionError("prove must not run")

        def _guarded(self, _action, step):
            step()

        def run_repairs(self):
            raise AssertionError("repairs must not run")

        def select_best(self):
            pass

        def final_audit(self):
            return None

        def _statement_flags(self):
            return []

    sessions: list = []
    monkeypatch.setattr(lean_verify, "_FormalSession", lambda **kwargs: sessions.append(Session(**kwargs)) or sessions[-1])
    monkeypatch.setattr(lean_verify, "_llm_environment", lambda user, record: {})
    monkeypatch.setattr(lean_verify, "toolchain_status", lambda: {})
    result = lean_verify.generate(workspace=tmp_path, run_record={"engine": "analyze"}, user=None,
                                  problem_override="State Theorem 1.1", proof_override="Statement: ...",
                                  guidance="BLUEPRINT", prove=False, targets=["thm:main"],
                                  source_label="analysis/analysis_graph.json")
    assert sessions[0].kwargs["problem"] == "State Theorem 1.1" and sessions[0].kwargs["proof"] == "Statement: ..."
    assert result["scope"] == "statements" and result["proofs_attempted"] is False
    assert result["status_label"] == "Statements compiled · proofs not attempted"
    assert result["notes"].startswith("Statements compiled · proofs not attempted")
    assert result["targets"] == ["thm:main"] and result["source"] == "analysis/analysis_graph.json"
    summary = lean_verify.monitor_summary(tmp_path)
    assert summary["scope"] == "statements" and summary["proofs_attempted"] is False
    assert summary["status_label"] == "Statements compiled · proofs not attempted"
    with pytest.raises(ValueError, match="No statements"):
        lean_verify.generate(workspace=tmp_path, run_record=None, user=None, proof_override=" ", prove=False)


STATEMENTS_LEAN = """import Mathlib

/-- Informal: thm:main -/
theorem main (a b : ℤ) (ha : Even a) (hb : Even b) : Even (a + b) := by
  sorry
"""


def _fake_lean_checker(monkeypatch):
    import hashlib

    def check(ldir, uses_mathlib=True, main_decl=None):
        raw = (Path(ldir) / lean_verify.PROOF_FILENAME).read_bytes()
        return {"ok": True, "status": "ok", "exit_code": 0, "stdout": "warning: declaration uses 'sorry'",
                "stderr": "", "duration_s": 0.1, "reports_sorry": True,
                "source_sha256": hashlib.sha256(raw).hexdigest()}

    monkeypatch.setattr(lean_verify, "_run_lean_check", check)


@pytest.mark.parametrize("mode", ["check", "compile"])
def test_checking_statement_only_lean_again_keeps_it_statement_only(tmp_path, monkeypatch, mode):
    _fake_lean_checker(monkeypatch)
    # What generate(..., prove=False) stores for a compiled statements file.
    lean_verify._persist(tmp_path, lean_src=STATEMENTS_LEAN, status="incomplete", uses_mathlib=True, attempts=[],
                         notes="statements", title="Statements", action="verify",
                         extra=lean_verify._statements_only_fields("incomplete", None, ["thm:main"]))
    (tmp_path / "lean").mkdir(exist_ok=True)
    # The owner presses Check (or Compile) in the Lean tab: no statements_only
    # argument, so the stored scope decides.
    after = lean_verify.compile_or_check(workspace=tmp_path, mode=mode)
    assert after["scope"] == "statements" and after["proofs_attempted"] is False
    assert after["status_label"] == lean_verify.STATEMENTS_ONLY_LABEL and after["targets"] == ["thm:main"]
    assert after["notes"].startswith(lean_verify.STATEMENTS_ONLY_LABEL) and "remain at line" not in after["notes"]
    assert "not re-checked by this server" in after["toolchain_note"]
    summary = lean_verify.monitor_summary(tmp_path)
    assert summary["scope"] == "statements" and summary["proofs_attempted"] is False
    assert summary["status_label"] == lean_verify.STATEMENTS_ONLY_LABEL
    # A proof run's Lean is never relabelled.
    proof_ws = tmp_path / "proof-run"
    proof_ws.mkdir()
    lean_verify._persist(proof_ws, lean_src=STATEMENTS_LEAN, status="incomplete", uses_mathlib=True, attempts=[],
                         action="verify")
    proof = lean_verify.compile_or_check(workspace=proof_ws, mode=mode)
    assert "scope" not in proof and lean_verify.monitor_summary(proof_ws)["scope"] == "proof"


def test_an_analysis_lean_check_is_statement_only_even_without_a_stored_result(tmp_path, monkeypatch):
    # The console says so for every analysis (pasted Lean on a fresh analysis too).
    _fake_lean_checker(monkeypatch)
    result = lean_verify.compile_or_check(workspace=tmp_path, lean=STATEMENTS_LEAN, mode="check",
                                          statements_only=True)
    assert result["scope"] == "statements" and result["status_label"] == lean_verify.STATEMENTS_ONLY_LABEL
    assert result["targets"] == [] and result["proofs_attempted"] is False


def test_lean_inputs_state_selected_nodes_with_their_definitions(tmp_path, provider):
    server = provider(default_script())
    analysis_workspace(tmp_path, server)
    graph = analysis_graph.load(tmp_path)
    assert analyze_runs.default_targets(graph) == ["thm:main"]
    inputs = analyze_runs.lean_inputs(graph, ["lem:sum"])
    assert inputs["targets"] == ["lem:sum"] and inputs["target"] == "lem:sum"
    assert "2a+2b=2(a+b)" in inputs["problem"] and "proofs are not attempted" in inputs["problem"]
    assert inputs["proof"].startswith("Statements selected from an existing manuscript")
    with pytest.raises(analyze_runs.AnalyzeRequestError):
        analyze_runs.validate_targets(graph, ["lem:missing"])
    with pytest.raises(analyze_runs.AnalyzeRequestError):
        analyze_runs.validate_targets(graph, [])
    with pytest.raises(analyze_runs.AnalyzeRequestError):
        analyze_runs.validate_targets(graph, ["Bad Id"])
    assert analyze_runs.validate_targets(graph, ["lem:sum", "lem:sum", "thm:main"]) == ["lem:sum", "thm:main"]


# ---------------------------------------------------------------------------
# sharing, projects, memory
# ---------------------------------------------------------------------------


def test_shared_analysis_excludes_source_and_proof_excerpts(tmp_path, provider, monkeypatch):
    server = provider(default_script({"lem:sum": "gap"}))
    run_id = "analyze_share_1"
    workspace = run_sharing.RUNS_DIR / "workspaces" / run_id
    analysis_workspace(workspace, server, text=MARKED_PAPER)
    (workspace / "analysis" / "source" / "notes.txt").write_text("PRIVATE-SOURCE-NOTE")
    record = write_record(run_id, workspace)
    snapshot = run_sharing.snapshot(run_id, record)
    blob = json.dumps(snapshot)
    assert SECRET_PROOF not in blob and "PRIVATE-SOURCE-NOTE" not in blob
    assert snapshot["agents"] == [] and snapshot["kind"] == "analysis" and snapshot["engine"] == "analyze"
    assert snapshot["title"] == "Analyze · Sums of evens"
    assert "not a certificate" in snapshot["notice"]
    nodes = {node["id"]: node for node in snapshot["informal_graph"]["nodes"]}
    assert nodes["lem:sum"]["verdict"] == "gap" and nodes["lem:sum"]["status"] == "gap"
    assert nodes["thm:main"]["closure"] == "gap" and nodes["thm:main"]["main_result"] is True
    assert "2a+2b=2(a+b)" in nodes["lem:sum"]["statement"]
    assert all("proof_excerpt" not in node and "notes" not in node for node in nodes.values())
    assert snapshot["analysis"]["verdict_note"] == "checked by a model verifier — not a certificate"


def test_a_shared_analysis_counts_unusable_verifier_output_as_not_checked(tmp_path, provider):
    # The verifier's reply for Lemma 2.2 cannot be read: no judgement was made.
    server = provider(default_script({"lem:sum": lambda user: "Looks fine to me."}))
    run_id = "analyze_share_unusable_1"
    workspace = run_sharing.RUNS_DIR / "workspaces" / run_id
    analysis_workspace(workspace, server)
    record = write_record(run_id, workspace)
    stored = record["analyze"]["summary"]
    assert stored["unclear"] == 1 and stored["coverage"]["uninterpretable"] == 1
    report = (workspace / "analysis" / "report.md").read_text()
    assert "| unclear | 0 |" in report and "| no usable verifier output | 1 |" in report
    snapshot = run_sharing.snapshot(run_id, record)
    nodes = {node["id"]: node for node in snapshot["informal_graph"]["nodes"]}
    assert nodes["lem:sum"]["verdict"] == "not_checked" and nodes["lem:sum"]["uninterpreted"] is True
    assert nodes["lem:sum"]["status"] == "not checked (no usable verifier output)"
    # The header, the summary and the nodes agree with the report: nothing is "unclear".
    assert "Verdicts: 2 checked by a model verifier, 0 gap, 0 error, 0 unclear, 1 not checked " \
           "(1 with no usable verifier output)" in snapshot["problem"]
    summary = snapshot["analysis"]["summary"]
    assert (summary["unclear"], summary["not_checked"], summary["no_usable_output"]) == (0, 1, 1)
    assert sum(node["verdict"] == "unclear" for node in nodes.values()) == 0


def test_projects_take_the_task_as_the_analysis_focus(monkeypatch):
    monkeypatch.setattr(projects, "get", lambda pid, uid: {"problem": "Project problem text"})
    body = {"project_id": "p1", "project_task": "Check Lemma 2.2", "run_kind": "analyze",
            "source": {"url": "https://arxiv.org/abs/2302.05537"}}
    projects.prepare_run(body, {"id": 3})
    assert body["analyze"] == {"focus": "Check Lemma 2.2"} and body["problem_text"] is None
    assert body["source"] == {"url": "https://arxiv.org/abs/2302.05537"}
    kept = {"project_id": "p1", "project_task": "x", "run_kind": "analyze", "analyze": {"focus": "mine"}}
    projects.prepare_run(kept, {"id": 3})
    assert kept["analyze"] == {"focus": "mine"}
    prove = {"project_id": "p1", "project_task": "t"}
    projects.prepare_run(prove, {"id": 3})
    assert prove["problem_text"].startswith("Project problem text")


def test_an_analysis_never_becomes_shared_memory(monkeypatch):
    monkeypatch.setattr(jobs, "run_owner", lambda run_id: 5)
    monkeypatch.setattr(run_sharing, "record", lambda run_id: {"run_id": run_id, "engine": "analyze",
                                                                "run_kind": "analyze", "status": "finished"})
    with pytest.raises(ValueError, match="can't become shared DAG memory"):
        research.publish_memory({"id": 5}, {"source_run_id": "analyze_mem_1", "publish": True,
                                            "node_id": "lem-sum", "layer": "informal"})


def test_attachments_accept_manuscript_bundles_for_analyze_only():
    import base64
    import io
    import tarfile
    import zipfile

    from agent_monitor import attachments

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.tex", PAPER)
    zipped = base64.b64encode(buffer.getvalue()).decode()
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as archive:
        data = PAPER.encode()
        info = tarfile.TarInfo("main.tex")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    tarred = base64.b64encode(tar_buffer.getvalue()).decode()
    batch = attachments.validate([{"name": "paper.zip", "data": zipped}, {"name": "paper.tar.gz", "data": tarred}],
                                 analyze=True)
    assert [item["type"] for item, _ in batch] == ["application/zip", "application/gzip"]
    assert batch[1][0]["path"].endswith(".tar.gz")
    with pytest.raises(ValueError, match="Unsupported file type"):
        attachments.validate([{"name": "paper.zip", "data": zipped}])
    with pytest.raises(ValueError, match="guests can attach text files only"):
        attachments.validate([{"name": "paper.zip", "data": zipped}], guest=True, analyze=True)
    with pytest.raises(ValueError, match="contents do not match"):
        attachments.validate([{"name": "paper.zip", "data": base64.b64encode(b"not a zip").decode()}], analyze=True)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 10).decode()
    with pytest.raises(ValueError, match="Analyze reads"):
        attachments.validate([{"name": "figure.png", "data": png}], analyze=True)
    assert "zip" in attachments.analyze_extensions(guest=False)
    assert "zip" not in attachments.analyze_extensions(guest=True)
