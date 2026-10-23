"""Questions about a finished analysis in the console and in ``jobs``: who
may ask (``POST /api/run/<id>/analysis/ask``), the limits Analyze shares
with Readability's questions (per run, per hour, per network, the sponsored
pool and its daily questions), one question at a time and none during a
re-check, the switch, each viewer's policy, the record's question state
(kept when ``run["analyze"]`` is rebuilt, repaired when its worker is lost),
the deploy check, and a whole sponsored analysis whose questions the real
runner subprocess answers through ``jobs`` against a loopback broker,
including a Stop.

Finished analyses are built by the real runner in-process against the fake
provider of ``test_analyze_runner``; the console tests stub the answer
(``jobs._execute_ask``).  Nothing touches the network.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer
from importlib import util
from pathlib import Path

import pytest

pytest.importorskip("pylatexenc")

from agent_monitor import analyze_runs, jobs, readability_runs, usage_ledger  # noqa: E402
from agent_monitor import console_server as console  # noqa: E402
from agent_monitor.analyze import ask as analyze_ask  # noqa: E402
from agent_monitor.analyze import request as analysis_request  # noqa: E402
from agent_monitor.runners import analyze_runner  # noqa: E402

from test_analyze_console import (  # noqa: E402,F401  (fixture import)
    _admin_cookie,
    _finished_analysis,
    _get,
    _post,
    analyze_console,
    fake_provider,
)
from test_analyze_runner import PAPER, Provider, default_script  # noqa: E402
from test_console_guest_access import (  # noqa: E402,F401  (fixture import)
    _issue_guest,
    guest_console,
    running_server,
)
from test_member_accounts import _member, member_console  # noqa: E402,F401
from test_readability_console import _finished_run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "short-lived-loopback-token-" + "q" * 20
BUSY = "Wait for the analysis (or its re-check, or the question before) to finish"
MEMBER = {"id": 92, "email": "asker@example.test", "is_admin": False}


@pytest.fixture()
def ask_console(analyze_console, monkeypatch):
    for name in (analyze_runs.ASKS_ENV, analyze_runs.ASK_DAILY_CAP_ENV, analyze_runs.GUEST_ASK_DAILY_CAP_ENV,
                 "AGENT_MONITOR_READABILITY_ENABLED", "AGENT_MONITOR_READABILITY_GUESTS", "READABILITY_CMD",
                 "AGENT_MONITOR_PUBLIC"):
        monkeypatch.delenv(name, raising=False)
    windows = (console._ASK_WINDOWS, console._ASK_CLIENT_WINDOWS, console._GUEST_READABILITY_WINDOWS,
               console._GUEST_READABILITY_CLIENT_WINDOWS)
    for window in windows:
        window.clear()
    yield analyze_console
    for window in windows:
        window.clear()


def _fake_ask(monkeypatch, answers: list) -> None:
    """start_manuscript_ask with a stubbed route and answer (no runner): the
    answer lands in the mode's block as ``_execute_ask`` lands it."""
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))

    def answer(**kwargs):
        answers.append(kwargs)
        state = kwargs["state"]
        final = {**state, "status": "done", "answer": "It has a gap: see [[result:lem:sum]].", "refs": ["lem:sum"],
                 "about": [state["node_id"]] if state.get("node_id") else [], "finished_at": jobs._now()}
        jobs._set_ask_state(kwargs["run_id"], final, block=jobs._ASK_MODES[kwargs["kind"]].block)
        # As _execute_ask does, the sponsored slot is given back (here before
        # the job ends, so that a finished job means a free slot).
        if kwargs["gate_key"]:
            analyze_runs.GATE.release(kwargs["gate_key"])
        if kwargs["on_exit"]:
            kwargs["on_exit"]()
        jobs._update_job(kwargs["job_id"], status="finished")

    monkeypatch.setattr(jobs, "_execute_ask", answer)


def _wait(predicate, timeout=120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _answered(run_id: str) -> bool:
    return _wait(lambda: not jobs.run_is_active(run_id), 10)


def _finish_jobs(run_id: str) -> None:
    """End the run's jobs that a stub left running."""
    with jobs._LOCK:
        for job in jobs._JOBS.values():
            if job.get("run_id") == run_id and job.get("status") == "running":
                job["status"] = "finished"


def _ask(port, run_id: str, cookie: str, body: dict, headers=None):
    return _post(port, f"/api/run/{run_id}/analysis/ask", cookie, body, headers=headers)


# ---------------------------------------------------------------------------
# who may ask, and what lands in the record
# ---------------------------------------------------------------------------


def test_the_owner_asks_about_an_analysis_and_the_answer_lands_in_its_record(ask_console, fake_provider,
                                                                              monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    with running_server() as port:
        owner, cookie = _member(port)
        _other, other_cookie = _member(port, "other@example.test")
        workspace = _finished_analysis("analyze_ask_c1", owner["id"], fake_provider)
        asked = _ask(port, "analyze_ask_c1", cookie, {"question": " Why is Lemma 2.2 a gap? ", "node_id": "lem:sum",
                                                       "selection": "Distributivity.", "view": "source"})
        assert _answered("analyze_ask_c1")
        refusals = {
            "foreign": _ask(port, "analyze_ask_c1", other_cookie, {"question": "Why?"}),
            "empty": _ask(port, "analyze_ask_c1", cookie, {"question": " "}),
            "unknown": _ask(port, "analyze_ask_c1", cookie, {"question": "Why?", "node_id": "lem:nope"}),
            "malformed": _ask(port, "analyze_ask_c1", cookie, {"question": "Why?", "node_id": "../x"}),
            "view": _ask(port, "analyze_ask_c1", cookie, {"question": "Why?", "view": "readable"}),
            "long": _ask(port, "analyze_ask_c1", cookie, {"question": "x" * 2001}),
            "mode": _post(port, "/api/run/analyze_ask_c1/readability/ask", cookie, {"question": "Why?"}),
            "path": _post(port, "/api/run/analyze_ask_c1/x/analysis/ask", cookie, {"question": "Why?"}),
        }
        admin = _ask(port, "analyze_ask_c1", _admin_cookie(), {"question": "And for the operator?"})
        assert _answered("analyze_ask_c1")
        _status, run = _get(port, "/api/run/analyze_ask_c1", cookie)
        monkeypatch.setattr(console.research_projects, "can_read_run", lambda run_id, user_id: True)
        _status, shared = _get(port, "/api/run/analyze_ask_c1", other_cookie)
        viewer = _ask(port, "analyze_ask_c1", other_cookie, {"question": "Why?"})
    assert asked[0] == 202, asked
    assert asked[2]["status"] == "asking" and asked[2]["run_id"] == "analyze_ask_c1"
    assert len(asked[2]["ask_id"]) == 12 and asked[2]["job_id"]
    assert refusals["foreign"][0] == 404 and refusals["path"][0] == 404
    assert refusals["empty"][0] == 400 and "Type a question" in refusals["empty"][2]["error"]
    assert refusals["unknown"][2]["error"] == "node_id must be a result of this analysis"
    assert refusals["malformed"][2]["error"] == "node_id must be a result of this analysis"
    assert refusals["view"][2]["error"] == 'view must be "report" or "source"'
    assert "2,000 characters" in refusals["long"][2]["error"]
    assert refusals["mode"][2]["error"] == "Only readability runs answer questions about a paper"
    for name in ("unknown", "malformed", "view", "long", "mode"):
        assert refusals[name][0] == 400, (name, refusals[name])
    assert admin[0] == 202
    # A project member reads the questions and answers, but may not ask.
    assert viewer[0] == 403 and "Only the member who started this run can ask about it" in viewer[2]["error"]
    first = answers[0]
    assert first["kind"] == "analyze" and first["workspace"] == workspace and first["gate_key"] is None
    assert first["model"] == "deepseek-test"
    assert first["timeout"] == (analyze_runner.ASK_SECONDS + jobs._analyze_api_timeout(False)
                                + jobs._ASK_GRACE_SECONDS)
    assert first["env"]["AGENT_MONITOR_SELECTED_MODEL"] == "deepseek-test"
    job = jobs.get_job(first["job_id"])
    assert (job["run_kind"], job["kind"], job["engine"], job["ask_id"]) == ("analyze", "ask", "analyze",
                                                                           asked[2]["ask_id"])
    assert job["problem_preview"] == "Ask · Why is Lemma 2.2 a gap?"
    block = run["analyze"]
    # The analysis is untouched; its questions sit beside it.
    assert block["summary"]["gap"] == 1 and block["schema"] == "ansatze-analyze-v1"
    assert "ask" not in block and block["asks_started"] == 2 and len(block["asks"]) == 2
    entry = block["asks"][0]
    assert entry["ask_id"] == asked[2]["ask_id"] and entry["status"] == "done"
    assert (entry["question"], entry["selection"], entry["view"], entry["node_id"]) == (
        "Why is Lemma 2.2 a gap?", "Distributivity.", "source", "lem:sum")
    assert entry["refs"] == ["lem:sum"] and entry["about"] == ["lem:sum"]
    assert not (run.get("readability") or {}).get("asks")
    assert shared["analyze"]["asks"] == block["asks"]
    assert analyze_ask.STORE.load_request(workspace, entry["ask_id"]) == {
        "question": "Why is Lemma 2.2 a gap?", "selection": "Distributivity.", "view": "source", "node_id": "lem:sum"}
    assert len(answers) == 2
    # Only the questions that were sent spent an hourly slot.
    assert len(console._ASK_WINDOWS[owner["id"]]) == 1


def test_guests_ask_three_questions_about_an_analysis(ask_console, fake_provider, monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        _finished_analysis("analyze_ask_c_guest", guest["id"], fake_provider, guest=True)
        codes = []
        for number in range(4):
            codes.append(_ask(port, "analyze_ask_c_guest", cookie, {"question": f"Q{number}?"}))
            assert _answered("analyze_ask_c_guest")
    limit = analyze_runs.GUEST_ASK_LIMIT_MESSAGE.format(limit=analyze_runs.GUEST_ASKS_PER_RUN)
    assert [code[0] for code in codes] == [202, 202, 202, 403]
    assert codes[3][2] == {"error": limit, "login_required": True}
    assert limit == "Guests can ask 3 questions about an analysis; sign in to ask more"
    assert len(answers) == 3 and len(console._ASK_WINDOWS[guest["id"]]) == 3
    assert jobs.ask_count(jobs._load_run_record("analyze_ask_c_guest")) == 3
    # The limit holds in jobs too, under the launch claim (two tabs at once).
    released: list = []
    with pytest.raises(analyze_runs.AnalyzeRequestError) as refused:
        jobs.start_analysis_ask("analyze_ask_c_guest", {"question": "Q4?"}, user=guest,
                                on_exit=lambda: released.append(1))
    assert refused.value.status == 403 and str(refused.value) == limit and released == [1]
    assert not jobs.run_is_active("analyze_ask_c_guest")


def test_the_hourly_limit_counts_questions_about_readable_versions_and_analyses_together(ask_console,
                                                                                          fake_provider,
                                                                                          monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    monkeypatch.setattr(analyze_runs, "ASKS_PER_HOUR", {"guest": 6, "member": 2, "admin": 120})
    with running_server() as port:
        owner, cookie = _member(port)
        # The analysis first: the readability run scripts the in-process model calls.
        _finished_analysis("analyze_ask_c_hour", owner["id"], fake_provider)
        _finished_run("readability_ask_c_hour", owner["id"], monkeypatch)
        readable = _post(port, "/api/run/readability_ask_c_hour/readability/ask", cookie, {"question": "One?"})
        assert _answered("readability_ask_c_hour")
        analysis = _ask(port, "analyze_ask_c_hour", cookie, {"question": "Two?"})
        assert _answered("analyze_ask_c_hour")
        third = _ask(port, "analyze_ask_c_hour", cookie, {"question": "Three?"})
        fourth = _post(port, "/api/run/readability_ask_c_hour/readability/ask", cookie, {"question": "Four?"})
    assert (readable[0], analysis[0]) == (202, 202)
    for refused in (third, fourth):
        assert refused[0] == 429 and refused[2]["error"] == "You can ask 2 questions per hour; please try again later"
        assert int(refused[1]["retry-after"]) > 0
    assert [answer["kind"] for answer in answers] == ["readability", "analyze"]
    assert jobs._load_run_record("readability_ask_c_hour")["readability"]["asks_started"] == 1
    assert jobs._load_run_record("analyze_ask_c_hour")["analyze"]["asks_started"] == 1


def test_guest_questions_from_one_network_count_both_modes(ask_console, fake_provider, monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    monkeypatch.setattr(console, "GUEST_ASKS_PER_CLIENT_PER_HOUR", 2)
    network = {"X-Forwarded-For": "203.0.113.70"}
    with running_server() as port:
        first, first_cookie, _ = _issue_guest(port)
        second, second_cookie, _ = _issue_guest(port)
        _finished_analysis("analyze_ask_c_net", first["id"], fake_provider, guest=True)
        _finished_run("readability_ask_c_net", second["id"], monkeypatch, guest=True, credential_source="user")
        readable = _post(port, "/api/run/readability_ask_c_net/readability/ask", second_cookie, {"question": "One?"},
                         headers=network)
        assert _answered("readability_ask_c_net")
        analysis = _ask(port, "analyze_ask_c_net", first_cookie, {"question": "Two?"}, headers=network)
        assert _answered("analyze_ask_c_net")
        third = _ask(port, "analyze_ask_c_net", first_cookie, {"question": "Three?"}, headers=network)
        elsewhere = _ask(port, "analyze_ask_c_net", first_cookie, {"question": "Four?"},
                         headers={"X-Forwarded-For": "198.51.100.71"})
        assert _answered("analyze_ask_c_net")
    assert [readable[0], analysis[0], third[0], elsewhere[0]] == [202, 202, 429, 202]
    assert "Too many guest questions from your network" in third[2]["error"]
    # The refused question gave the guest's own hourly slot back.
    assert len(console._ASK_WINDOWS[first["id"]]) == 2 and len(answers) == 3


# ---------------------------------------------------------------------------
# the sponsored pool
# ---------------------------------------------------------------------------


def test_sponsored_questions_about_analyses_count_among_the_days_questions(ask_console, fake_provider,
                                                                            monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    gate = analyze_runs.GATE
    monkeypatch.setenv(analyze_runs.ASK_DAILY_CAP_ENV, "3")
    with running_server() as port:
        owner, cookie = _member(port)
        _finished_analysis("analyze_ask_c_sp", owner["id"], fake_provider, credential_source="sponsored_kimi",
                           model="kimi-k3")
        # A readable version's question counts among the same day's questions.
        _finished_run("readability_ask_c_sp", owner["id"], monkeypatch, credential_source="sponsored_kimi")
        codes = [_post(port, "/api/run/readability_ask_c_sp/readability/ask", cookie, {"question": "Q0?"})]
        assert _answered("readability_ask_c_sp")
        for number in range(1, 4):
            codes.append(_ask(port, "analyze_ask_c_sp", cookie, {"question": f"Q{number}?"}))
            assert _answered("analyze_ask_c_sp")
    assert [code[0] for code in codes] == [202, 202, 202, 429]
    assert codes[3][2]["error"] == analyze_runs.ASK_GATE_MESSAGES.daily
    assert [answer["gate_key"] for answer in answers] == [
        "readability_ask_c_sp#ask", "analyze_ask_c_sp#ask", "analyze_ask_c_sp#ask"]
    assert not gate.holds("analyze_ask_c_sp#ask")
    # The day's manuscript runs are untouched: a sponsored analysis may still start.
    usage = json.loads(gate.usage_path.read_text())
    assert (usage["runs"], usage["asks"]) == (0, 3) and gate.capacity_error() is None
    # The refused question gave its hourly slot back.
    assert len(console._ASK_WINDOWS[owner["id"]]) == 3


def test_a_busy_pool_refuses_a_sponsored_question_in_analyze_words(ask_console, fake_provider, monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    gate = analyze_runs.GATE
    with running_server() as port:
        owner, cookie = _member(port)
        guest, guest_cookie, _ = _issue_guest(port)
        _finished_analysis("analyze_ask_c_busy", owner["id"], fake_provider, credential_source="sponsored_kimi",
                           model="kimi-k3")
        _finished_analysis("analyze_ask_c_gbusy", guest["id"], fake_provider, guest=True,
                           credential_source="sponsored_kimi", model="kimi-k3")
        # A guest's analysis holds the guests' share of the pool.
        gate.reserve("busy-guest", run_id="busy-guest", guest=True)
        guest_busy = _ask(port, "analyze_ask_c_gbusy", guest_cookie, {"question": "Why?"})
        member_ok = _ask(port, "analyze_ask_c_busy", cookie, {"question": "Why?"})
        assert _answered("analyze_ask_c_busy")
        # Every slot is taken.
        gate.reserve("busy-member", run_id="busy-member")
        full = _ask(port, "analyze_ask_c_busy", cookie, {"question": "Why?"})
        gate.release("busy-guest")
        gate.release("busy-member")
        monkeypatch.setenv(analyze_runs.ASK_DAILY_CAP_ENV, "0")
        off = _ask(port, "analyze_ask_c_busy", cookie, {"question": "Why?"})
        guest_off = _ask(port, "analyze_ask_c_gbusy", guest_cookie, {"question": "Why?"})
        # Off for guests only: a guest is told so, and to sign in.
        monkeypatch.setenv(analyze_runs.ASK_DAILY_CAP_ENV, "200")
        monkeypatch.setenv(analyze_runs.GUEST_ASK_DAILY_CAP_ENV, "0")
        guests_only = _ask(port, "analyze_ask_c_gbusy", guest_cookie, {"question": "Why?"})
    assert guest_busy[0] == 429
    assert guest_busy[2]["error"] == analyze_runs.ASK_GATE_MESSAGES.guest_share_busy.format(active=1)
    assert guest_busy[2]["error"].startswith("Sponsored Analyze for guests is busy (1 running)")
    assert member_ok[0] == 202
    assert full[0] == 429 and full[2]["error"] == analyze_runs.ASK_GATE_MESSAGES.busy.format(active=2)
    assert full[2]["error"].startswith("Sponsored Analyze is busy (2 manuscript runs")
    assert off[0] == 403 and off[2]["error"] == analyze_runs.ASK_GATE_MESSAGES.off
    # Off for everyone is not "off for guests": signing in would not help.
    assert guest_off[0] == 403 and guest_off[2]["error"] == analyze_runs.ASK_GATE_MESSAGES.off
    assert guests_only[0] == 403 and guests_only[2]["error"] == analyze_runs.ASK_GATE_MESSAGES.guest_off
    assert analyze_runs.ASK_GATE_MESSAGES.busy != readability_runs.ASK_GATE_MESSAGES.busy
    assert len(answers) == 1 and len(console._ASK_WINDOWS[owner["id"]]) == 1
    assert not console._ASK_WINDOWS.get(guest["id"])
    usage = json.loads(gate.usage_path.read_text())
    assert (usage["asks"], usage.get("guest_asks", 0)) == (1, 0)


def test_a_question_that_cannot_start_gives_its_sponsored_count_back(ask_console, fake_provider, monkeypatch):
    gate = analyze_runs.GATE
    launched: list = []
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: launched.append(kwargs))

    def unconfigured(**_kwargs):
        raise ValueError("The sponsored Kimi route is not configured on this server")

    monkeypatch.setattr(jobs, "_continuation_auth_environment", unconfigured)
    with running_server() as port:
        owner, cookie = _member(port)
        _finished_analysis("analyze_ask_c_refund", owner["id"], fake_provider, credential_source="sponsored_kimi",
                           model="kimi-k3")
        refused = _ask(port, "analyze_ask_c_refund", cookie, {"question": "Why?"})
    assert refused[0] == 400 and refused[2]["error"] == "The sponsored Kimi route is not configured on this server"
    assert launched == [] and not jobs.run_is_active("analyze_ask_c_refund")
    assert not gate.holds("analyze_ask_c_refund#ask") and gate.active() == 0
    usage = json.loads(gate.usage_path.read_text()) if gate.usage_path.exists() else {}
    assert usage.get("asks", 0) == 0 and usage.get("runs", 0) == 0
    assert not console._ASK_WINDOWS.get(owner["id"])
    block = jobs._load_run_record("analyze_ask_c_refund")["analyze"]
    assert not any(key in block for key in jobs.ASK_KEYS)


def test_a_question_whose_worker_cannot_start_is_recorded_as_failed(ask_console, fake_provider, monkeypatch):
    gate = analyze_runs.GATE
    _finished_analysis("analyze_ask_c_thread", MEMBER["id"], fake_provider, credential_source="sponsored_kimi",
                       model="kimi-k3")
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))

    class NoThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    released: list = []
    with monkeypatch.context() as patch, pytest.raises(RuntimeError):
        patch.setattr(jobs.threading, "Thread", NoThread)
        jobs.start_analysis_ask("analyze_ask_c_thread", {"question": "Why?"}, user=MEMBER,
                                on_exit=lambda: released.append(1))
    assert released == [1] and not jobs.run_is_active("analyze_ask_c_thread")
    assert not gate.holds("analyze_ask_c_thread#ask")
    usage = json.loads(gate.usage_path.read_text())
    assert usage["asks"] == 0
    block = jobs._load_run_record("analyze_ask_c_thread")["analyze"]
    assert "ask" not in block and block["asks_started"] == 1
    assert block["asks"][-1]["status"] == "failed" and block["asks"][-1]["error"] == "The question could not be sent"
    assert block["summary"]["gap"] == 1


def test_a_member_cannot_spend_an_own_key_analysis_in_public_mode(ask_console, fake_provider, monkeypatch):
    _finished_analysis("analyze_ask_c_public", MEMBER["id"], fake_provider)
    monkeypatch.setenv("AGENT_MONITOR_PUBLIC", "1")
    launched: list = []
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: launched.append(kwargs))
    with pytest.raises(analyze_runs.AnalyzeRequestError, match="members can't use on this site") as refused:
        jobs.start_analysis_ask("analyze_ask_c_public", {"question": "Why?"}, user=MEMBER)
    assert refused.value.status == 403 and launched == []
    assert str(refused.value) == jobs._ASK_MODES["analyze"].api_key_refusal
    assert not jobs.run_is_active("analyze_ask_c_public")


# ---------------------------------------------------------------------------
# one at a time, a published analysis, the switch
# ---------------------------------------------------------------------------


def test_one_question_at_a_time_and_none_during_a_recheck(ask_console, fake_provider, monkeypatch):
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))
    # Neither the question nor the re-check is answered: their jobs stay "running".
    launched: list = []
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: launched.append(kwargs))
    rechecks: list = []
    monkeypatch.setattr(jobs, "_execute_recheck", lambda **kwargs: rechecks.append(kwargs))
    with running_server() as port:
        owner, cookie = _member(port)
        _finished_analysis("analyze_ask_c_one", owner["id"], fake_provider)
        first = _ask(port, "analyze_ask_c_one", cookie, {"question": "One?"})
        second = _ask(port, "analyze_ask_c_one", cookie, {"question": "Two?"})
        recheck_while_asking = _post(port, "/api/run/analyze_ask_c_one/analysis/recheck", cookie,
                                     {"node_id": "lem:sum"})
        _finish_jobs("analyze_ask_c_one")
        recheck = _post(port, "/api/run/analyze_ask_c_one/analysis/recheck", cookie, {"node_id": "lem:sum"})
        while_rechecking = _ask(port, "analyze_ask_c_one", cookie, {"question": "Three?"})
        _finish_jobs("analyze_ask_c_one")
        # The analysis itself still running.
        record = jobs._load_run_record("analyze_ask_c_one")
        jobs._write_run({**record, "status": "running"})
        running = _ask(port, "analyze_ask_c_one", cookie, {"question": "Four?"})
        jobs._write_run({**record, "status": "finished"})
    assert first[0] == 202
    assert second[0] == 409 and second[2]["error"] == BUSY
    assert recheck_while_asking[0] == 409
    assert recheck_while_asking[2]["error"] == jobs.RECHECK_BUSY
    assert recheck[0] == 200, recheck
    assert while_rechecking[0] == 409 and while_rechecking[2]["error"] == BUSY
    assert running[0] == 409 and running[2]["error"] == BUSY
    assert len(launched) == 1 and len(rechecks) == 1
    # The refused questions gave their hourly slots back.
    assert len(console._ASK_WINDOWS[owner["id"]]) == 1


def test_a_refused_question_leaves_the_one_being_answered_running(ask_console, fake_provider, monkeypatch):
    # Review: any refused request (a second click, a second tab) failed every
    # running question job of the run; the next read then reported the
    # question being answered as interrupted and gave its sponsored slot back,
    # and a re-check or another question could start beside it.
    gate = analyze_runs.GATE
    run_id = "analyze_ask_c_double"
    _finished_analysis(run_id, MEMBER["id"], fake_provider, credential_source="sponsored_kimi", model="kimi-k3")
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: None)  # still being answered
    first = jobs.start_analysis_ask(run_id, {"question": "One?"}, user=MEMBER)
    try:
        for body, status in (({"question": "Two?"}, 409), ({"question": "Two?", "node_id": "lem:nope"}, 400),
                             ({"question": " "}, 400)):
            with pytest.raises(analyze_runs.AnalyzeRequestError) as refused:
                jobs.start_analysis_ask(run_id, body, user=MEMBER)
            assert refused.value.status == status
        assert jobs.get_job(first["job_id"])["status"] == "running" and jobs.run_is_active(run_id)
        record = jobs.reconcile_run_status(run_id)
        assert record["analyze"]["ask"]["ask_id"] == first["ask_id"]
        assert record["analyze"]["ask"]["status"] == "running" and "asks" not in record["analyze"]
        assert gate.holds(analyze_runs.ask_key(run_id))
        with pytest.raises(analyze_runs.AnalyzeRequestError) as busy:
            jobs.start_analysis_recheck(run_id, "lem:sum", user=MEMBER)
        assert busy.value.status == 409
        with pytest.raises(analyze_runs.AnalyzeRequestError) as busy:
            jobs.start_analysis_ask(run_id, {"question": "Three?"}, user=MEMBER)
        assert busy.value.status == 409 and str(busy.value) == BUSY
        assert json.loads(gate.usage_path.read_text())["asks"] == 1
    finally:
        _finish_jobs(run_id)
        gate.release(analyze_runs.ask_key(run_id))


def test_a_question_refused_for_a_held_slot_gives_back_nobodys_slot(ask_console, fake_provider, monkeypatch):
    # Review: a question refused because the run's question slot was still
    # held (the last answer's job ended, its slot not yet given back) released
    # that slot and refunded its count.
    gate = analyze_runs.GATE
    run_id = "analyze_ask_c_held"
    _finished_analysis(run_id, MEMBER["id"], fake_provider, credential_source="sponsored_kimi", model="kimi-k3")
    launched: list = []
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: launched.append(kwargs))
    key = analyze_runs.ask_key(run_id)
    gate.reserve(key, run_id=run_id, messages=analyze_runs.ASK_GATE_MESSAGES, daily=analyze_runs.ASK_DAILY)
    try:
        with pytest.raises(analyze_runs.AnalyzeCapacityError) as refused:
            jobs.start_analysis_ask(run_id, {"question": "Again?"}, user=MEMBER)
        assert refused.value.status == 409 and str(refused.value) == analyze_runs.ASK_GATE_MESSAGES.held
        assert gate.holds(key) and json.loads(gate.usage_path.read_text())["asks"] == 1
        assert launched == [] and not jobs.run_is_active(run_id)
        assert "asks_started" not in jobs._load_run_record(run_id)["analyze"]
    finally:
        gate.release(key)


def test_questions_need_a_finished_published_analysis(ask_console, monkeypatch):
    launched: list = []
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: launched.append(kwargs))
    with running_server() as port:
        owner, cookie = _member(port)
        jobs.workspace_dir("plain_ask_c1")
        jobs._write_run({"run_id": "plain_ask_c1", "engine": "plain", "status": "finished", "owner_id": owner["id"]})
        not_analysis = _ask(port, "plain_ask_c1", cookie, {"question": "Why?"})
        workspace = jobs.workspace_dir("analyze_ask_c_early")
        analysis_request.write(workspace, source={"kind": "paste", "text": PAPER}, caps={}, focus="",
                               formalize=False, role="member", route="api_key", model="deepseek-test",
                               reasoning_effort="default", sponsored=False)
        base = {"run_id": "analyze_ask_c_early", "engine": "analyze", "run_kind": "analyze",
                "owner_id": owner["id"], "workspace": str(workspace)}
        jobs._write_run({**base, "status": "running"})
        early = _ask(port, "analyze_ask_c_early", cookie, {"question": "Why?"})
        # Stopped (or failed) before its report was published.
        jobs._write_run({**base, "status": "stopped"})
        stopped = _ask(port, "analyze_ask_c_early", cookie, {"question": "Why?"})
    assert not_analysis[0] == 400
    assert not_analysis[2]["error"] == "Only Analyze runs answer questions about an analysis"
    for refused in (early, stopped):
        assert refused[0] == 409 and refused[2]["error"] == "This analysis has no published report to ask about yet"
    assert launched == [] and not console._ASK_WINDOWS.get(owner["id"])
    asks = workspace / "analysis" / "asks"
    assert not asks.exists() or not list(asks.glob("*.json"))


def test_the_questions_switch_turns_off_questions_about_analyses_only(ask_console, fake_provider, monkeypatch):
    answers: list = []
    _fake_ask(monkeypatch, answers)
    monkeypatch.setenv(analyze_runs.ASKS_ENV, "0")
    with running_server() as port:
        owner, cookie = _member(port)
        guest, guest_cookie, _ = _issue_guest(port)
        _finished_analysis("analyze_ask_c_off", owner["id"], fake_provider)
        _finished_run("readability_ask_c_on", owner["id"], monkeypatch)
        refused = _ask(port, "analyze_ask_c_off", cookie, {"question": "Why?"})
        readable = _post(port, "/api/run/readability_ask_c_on/readability/ask", cookie, {"question": "Why?"})
        assert _answered("readability_ask_c_on")
        _s, listing = _get(port, "/api/engines", cookie)
        _s, settings = _get(port, "/api/settings", guest_cookie)
        monkeypatch.setenv(analyze_runs.ASKS_ENV, "1")
        allowed = _ask(port, "analyze_ask_c_off", cookie, {"question": "Why?"})
        assert _answered("analyze_ask_c_off")
        _s, listing_on = _get(port, "/api/engines", cookie)
        _s, settings_on = _get(port, "/api/settings", guest_cookie)
    assert refused[0] == 403 and refused[2]["error"] == analyze_runs.ASKS_DISABLED_MESSAGE
    assert readable[0] == 202 and allowed[0] == 202
    assert [answer["kind"] for answer in answers] == ["readability", "analyze"]
    # The refused question spent no hourly slot.
    assert len(console._ASK_WINDOWS[owner["id"]]) == 2

    def policy(payload, engine):
        return next(item for item in payload["engines"] if item["id"] == engine)[engine]

    assert policy(listing, "analyze")["asks"]["enabled"] is False and policy(listing, "analyze")["enabled"] is True
    assert policy(listing_on, "analyze")["asks"]["enabled"] is True
    assert "asks" in policy(listing, "readability")
    assert settings["guest_policy"]["analyze"]["asks"] is False and settings["guest_policy"]["analyze"]["enabled"]
    assert settings_on["guest_policy"]["analyze"]["asks"] is True
    assert guest["id"] not in console._ASK_WINDOWS


def test_each_viewer_is_told_what_it_may_ask(ask_console, monkeypatch):
    guest, member, admin = {"id": 1, "guest": True}, {"id": 2, "email": "m@example.test"}, {"id": 3, "is_admin": True}
    common = {"in_flight": 1, "question_chars": 2000, "selection_chars": 4000, "reason": None, "sponsored": True,
              "sponsored_reason": None}
    assert analyze_runs.viewer_policy(guest)["asks"] == {"enabled": True, "per_hour": 6, "per_run": 3, **common}
    assert analyze_runs.viewer_policy(member)["asks"] == {"enabled": True, "per_hour": 30, "per_run": None, **common}
    assert analyze_runs.viewer_policy(admin)["asks"]["per_hour"] == 120
    # What the policy says is what the server enforces.
    from agent_monitor import manuscript_ask

    assert (common["question_chars"], common["selection_chars"]) == (manuscript_ask.MAX_QUESTION_CHARS,
                                                                      manuscript_ask.MAX_SELECTION_CHARS)
    assert readability_runs.ASKS_PER_HOUR is analyze_runs.ASKS_PER_HOUR
    assert readability_runs.GUEST_ASKS_PER_RUN == analyze_runs.GUEST_ASKS_PER_RUN == 3
    assert readability_runs.ask_key("r1") == analyze_runs.ask_key("r1") == "r1#ask"
    assert console._guest_analyze_summary()["asks"] is True
    assert {key: console._guest_analyze_summary()[key] for key in ("asks_per_run", "asks_per_hour")} == {
        "asks_per_run": 3, "asks_per_hour": 6}
    monkeypatch.setenv(analyze_runs.ASKS_ENV, "0")
    assert not any(analyze_runs.viewer_policy(user)["asks"]["enabled"] for user in (guest, member, admin))
    assert console._guest_analyze_summary()["asks"] is False
    assert str(analyze_runs.ask_error(member)) == analyze_runs.ASKS_DISABLED_MESSAGE
    monkeypatch.delenv(analyze_runs.ASKS_ENV)
    # Without Analyze (for guests) there is nothing to ask about.
    monkeypatch.setenv("AGENT_MONITOR_ANALYZE_GUESTS", "0")
    assert analyze_runs.viewer_policy(guest)["asks"]["enabled"] is False
    assert analyze_runs.viewer_policy(member)["asks"]["enabled"] is True
    assert console._guest_analyze_summary()["asks"] is False
    assert str(analyze_runs.ask_error(guest)) == analyze_runs.GUEST_DISABLED_MESSAGE
    monkeypatch.setenv("AGENT_MONITOR_ANALYZE_ENABLED", "0")
    assert str(analyze_runs.ask_error(member)) == analyze_runs.DISABLED_MESSAGE


# ---------------------------------------------------------------------------
# the record's question state
# ---------------------------------------------------------------------------


def _lost_question(run_id: str) -> None:
    """A finished analysis whose question's worker died with the old process."""
    jobs._write_run({"run_id": run_id, "engine": "analyze", "run_kind": "analyze", "status": "finished",
                     "analyze": {"schema": "ansatze-analyze-v1", "summary": {"nodes": 3}, "asks_started": 2,
                                 "asks": [{"ask_id": "aaaaaaaaaaaa", "status": "done", "question": "First?",
                                           "answer": "Yes."}],
                                 "ask": {"ask_id": "bbbbbbbbbbbb", "status": "running", "job_id": "lostask001",
                                         "question": "Second?", "started_at": "2026-10-22T07:42:14+00:00"}}})


def test_a_lost_question_about_an_analysis_is_repaired_on_read_at_startup_and_by_stop(ask_console):
    gate = analyze_runs.GATE
    # On read (every GET /api/run/<id> reconciles first); its sponsored slot comes back.
    _lost_question("analyze_ask_c_lost_1")
    gate.reserve(analyze_runs.ask_key("analyze_ask_c_lost_1"), run_id="analyze_ask_c_lost_1",
                 messages=analyze_runs.ASK_GATE_MESSAGES, daily=analyze_runs.ASK_DAILY)
    run = jobs.reconcile_run_status("analyze_ask_c_lost_1")
    block = run["analyze"]
    assert "ask" not in block and [entry["ask_id"] for entry in block["asks"]] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    lost = block["asks"][-1]
    assert lost["status"] == "interrupted" and lost["error"] == jobs.ASK_INTERRUPTED and lost["finished_at"]
    assert block["summary"] == {"nodes": 3} and block["asks_started"] == 2
    assert jobs._load_run_record("analyze_ask_c_lost_1")["status"] == "finished"
    assert not gate.holds(analyze_runs.ask_key("analyze_ask_c_lost_1"))
    # At startup: the manifest flags the analysis although it is not "running".
    _lost_question("analyze_ask_c_lost_2")
    manifest = json.loads((jobs._cache_dir() / "manifest.json").read_text())
    entry = next(item for item in manifest["runs"] if item["run_id"] == "analyze_ask_c_lost_2")
    assert entry["ask_running"] is True and entry["status"] == "finished"
    jobs.reconcile_stale_running_runs(min_age_seconds=0)
    assert jobs._load_run_record("analyze_ask_c_lost_2")["analyze"]["asks"][-1]["status"] == "interrupted"
    manifest = json.loads((jobs._cache_dir() / "manifest.json").read_text())
    assert next(item for item in manifest["runs"] if item["run_id"] == "analyze_ask_c_lost_2")["ask_running"] is False
    # Stop repairs it too.
    _lost_question("analyze_ask_c_lost_3")
    result = jobs.stop_run("analyze_ask_c_lost_3")
    jobs._stop_event("analyze_ask_c_lost_3").clear()
    assert result["status"] == "not_running"
    assert jobs._load_run_record("analyze_ask_c_lost_3")["analyze"]["asks"][-1]["status"] == "interrupted"
    # A question whose worker runs here is left alone.
    _lost_question("analyze_ask_c_live")
    with jobs._LOCK:
        jobs._JOBS["lostask001"] = {"job_id": "lostask001", "run_id": "analyze_ask_c_live", "kind": "ask",
                                    "engine": "analyze", "status": "running"}
    try:
        run = jobs.reconcile_run_status("analyze_ask_c_live")
    finally:
        with jobs._LOCK:
            jobs._JOBS.pop("lostask001", None)
    assert run["analyze"]["ask"]["status"] == "running"


def test_a_lost_question_is_repaired_when_the_owner_reads_the_analysis(ask_console):
    with running_server() as port:
        owner, cookie = _member(port)
        _lost_question("analyze_ask_c_lost_get")
        record = jobs._load_run_record("analyze_ask_c_lost_get")
        jobs._write_run({**record, "owner_id": owner["id"]})
        status, run = _get(port, "/api/run/analyze_ask_c_lost_get", cookie)
    assert status == 200 and "ask" not in run["analyze"]
    assert run["analyze"]["asks"][-1]["status"] == "interrupted"


def test_questions_survive_every_rebuild_of_the_analysis_block(ask_console, fake_provider):
    run_id = "analyze_ask_c_keep"
    workspace = _finished_analysis(run_id, MEMBER["id"], fake_provider)
    asks = {"asks": [{"ask_id": "aaaaaaaaaaaa", "status": "done", "question": "Why?", "answer": "Because."}],
            "asks_started": 2, "ask_usage": {"calls": 2, "input_tokens": 22, "output_tokens": 14},
            "ask": {"ask_id": "bbbbbbbbbbbb", "status": "running", "job_id": "j", "question": "And?"}}
    record = jobs._load_run_record(run_id)
    record["analyze"].update(asks)
    jobs._write_run(record)

    def kept(block: dict) -> dict:
        return {key: block.get(key) for key in asks}

    # A re-check publishes and refreshes run["analyze"] from the workspace.
    jobs._set_recheck_state(run_id, {"node_id": "lem:sum", "status": "done", "job_id": "r"}, refresh=True,
                            usage={"calls": 1})
    block = jobs._load_run_record(run_id)["analyze"]
    assert kept(block) == asks and block["recheck"]["status"] == "done" and block["rechecks"]["calls"] == 1
    assert block["summary"]["gap"] == 1
    # The worker finalizes the record from the workspace.
    finished = {"run_id": run_id, "engine": "analyze", "status": "finished"}
    jobs._finalize_analyze_record(finished, workspace)
    assert kept(finished["analyze"]) == asks and finished["analyze"]["recheck"]["status"] == "done"
    assert finished["analyze"]["summary"]["gap"] == 1
    # The console completes a stored block without its summary.
    data = {"run_id": run_id, "run_kind": "analyze", "status": "finished", "analyze": dict(asks)}
    console._refresh_analyze_block(data, workspace)
    assert kept(data["analyze"]) == asks and data["analyze"]["summary"]["gap"] == 1
    # A question's state and a re-check's are written under one lock and keep each other.
    jobs._set_ask_state(run_id, {**asks["ask"], "status": "done", "answer": "So."}, usage={"calls": 1},
                        block="analyze")
    block = jobs._load_run_record(run_id)["analyze"]
    assert "ask" not in block and block["asks"][-1]["ask_id"] == "bbbbbbbbbbbb"
    assert block["ask_usage"]["calls"] == 3 and block["recheck"]["status"] == "done"
    assert jobs._ASK_STATE_LOCK is jobs._RECHECK_STATE_LOCK
    with pytest.raises(ValueError):
        jobs._set_ask_state(run_id, asks["ask"], block="analysis")


def test_the_block_of_a_runs_questions_follows_its_kind():
    assert jobs.ask_block_of({"run_kind": "analyze"}) == "analyze"
    assert jobs.ask_block_of({"engine": "analyze"}) == "analyze"
    assert jobs.ask_block_of({"run_kind": "readability"}) == "readability"
    assert jobs.ask_count({"run_kind": "analyze", "analyze": {"asks_started": 2},
                           "readability": {"asks_started": 5}}) == 2
    assert jobs.ask_count({"run_kind": "readability", "readability": {"asks_started": "x"}}) == 0
    assert jobs.ask_count(None) == 0
    assert set(jobs._ASK_MODES) == {"readability", "analyze"}
    analyze = jobs._ASK_MODES["analyze"]
    assert (analyze.block, analyze.runner, analyze.event) == ("analyze", "analyze_runner.py", "analyze.ask")
    output = "\n".join([
        json.dumps({"type": "readability.ask", "result": {"answer": "wrong mode"}}),
        "not json", "[1, 2]",
        json.dumps({"type": "analyze.ask", "result": {"answer": "Yes."}}),
        json.dumps({"type": "item.completed", "item": {"type": "error", "terminal": True, "message": "late"}}),
    ])
    assert jobs._ask_outcome(output, "analyze.ask") == ({"answer": "Yes."}, "late")
    assert jobs._ask_outcome(output) == ({"answer": "wrong mode"}, "late")


def test_deploys_wait_for_a_question_about_an_analysis(tmp_path):
    spec = util.spec_from_file_location("auto_deploy", ROOT / "deploy" / "ansatze" / "auto_deploy.py")
    deploy = util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    running = {"status": "finished", "run_kind": "analyze",
               "analyze": {"summary": {"nodes": 3}, "ask": {"status": "running"}}}
    assert deploy.record_busy(running)
    assert not deploy.record_busy({"status": "finished", "analyze": {"asks": [{"status": "done"}]}})
    # The scan of the data directory finds it in a run record.
    harness = tmp_path / "cache" / "harness"
    harness.mkdir(parents=True)
    (harness / "analyze_ask_deploy.json").write_text(json.dumps(running))
    assert deploy.active_work(tmp_path)
    # The run list flags it as well.
    jobs._write_run({"run_id": "analyze_ask_c_deploy", "engine": "analyze", **running})
    manifest = json.loads((jobs._cache_dir() / "manifest.json").read_text())
    entry = next(item for item in manifest["runs"] if item["run_id"] == "analyze_ask_c_deploy")
    assert entry["ask_running"] is True


# ---------------------------------------------------------------------------
# a sponsored analysis and its questions through jobs (runner subprocesses)
# ---------------------------------------------------------------------------


def _loopback_broker(monkeypatch, issued: list, base: str) -> None:
    from agent_monitor import sponsored_kimi_client as broker

    monkeypatch.setattr(broker, "configured", lambda: True)

    def credentials(**kwargs):
        issued.append(kwargs)
        return {"KIMI_API_KEY": TOKEN, "KIMI_API_BASE": base, "KIMI_REASONING_EFFORT": "high",
                "AGENT_MONITOR_SPONSORED_KIMI": "1", "AGENT_MONITOR_SPONSORED_KIMI_URL": base.rsplit("/internal", 1)[0]}

    monkeypatch.setattr(broker, "issue_credentials", credentials)


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """The sponsored pool alone (``jobs.start_job`` runs for real here)."""
    for name in (analyze_runs.DAILY_CAP_ENV, analyze_runs.CONCURRENCY_ENV, analyze_runs.GUEST_DAILY_CAP_ENV,
                 analyze_runs.GUEST_CONCURRENCY_ENV, analyze_runs.ASK_DAILY_CAP_ENV,
                 analyze_runs.GUEST_ASK_DAILY_CAP_ENV, analyze_runs.ASKS_ENV, "AGENT_MONITOR_ANALYZE_ENABLED",
                 "AGENT_MONITOR_ANALYZE_GUESTS", "ANALYZE_CMD", "AGENT_MONITOR_CLI_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    # The derived pipeline (a graph projection, no model call) is not the point here.
    monkeypatch.setenv("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", "1")
    monkeypatch.setattr(analyze_runs.GATE, "usage_path", tmp_path / "analyze_usage.json")
    analyze_runs.GATE.reset()
    yield analyze_runs.GATE
    analyze_runs.GATE.reset()


def test_a_question_that_reaches_no_model_gives_its_sponsored_start_back(gate, tmp_path):
    run_id = "analyze_ask_c_unsent"
    state = {"ask_id": "cccccccccccc", "status": "running", "question": "Why?", "selection": "", "view": "report",
             "node_id": None, "job_id": "unsent01", "started_at": jobs._now()}
    jobs._write_run({"run_id": run_id, "engine": "analyze", "run_kind": "analyze", "status": "finished",
                     "analyze": {"summary": {"nodes": 3}, "asks_started": 1, "ask": state}})
    key = analyze_runs.ask_key(run_id)
    gate.reserve(key, run_id=run_id, messages=analyze_runs.ASK_GATE_MESSAGES, daily=analyze_runs.ASK_DAILY)
    assert json.loads(gate.usage_path.read_text())["asks"] == 1
    env = {"PATH": "/usr/bin:/bin", "AGENT_MONITOR_DATA_DIR": str(tmp_path / "data"),
           "AGENT_MONITOR_ENV_PATH": str(tmp_path / "server.env")}
    (tmp_path / "server.env").touch()
    (tmp_path / "ws").mkdir()
    exits: list = []
    # No analysis/request.json: the Analyze runner exits 2 (configuration) before any model call.
    jobs._execute_ask(run_id=run_id, job_id="unsent01", state=state, workspace=tmp_path / "ws", env=env,
                      timeout=120, gate_key=key, on_exit=lambda: exits.append(1), model="kimi-k3", kind="analyze")
    block = jobs._load_run_record(run_id)["analyze"]
    assert "ask" not in block and block["summary"] == {"nodes": 3}
    entry = block["asks"][-1]
    assert entry["status"] == "failed" and entry["error"] == "analysis/request.json is missing"
    assert "answer" not in entry and "usage" not in entry and exits == [1]
    assert not gate.holds(key) and json.loads(gate.usage_path.read_text())["asks"] == 0


def test_a_sponsored_analysis_and_its_questions_through_jobs(gate, monkeypatch):
    from agent_monitor.analyze import prompts

    monkeypatch.setattr(jobs, "_estimate_cost", lambda *_args, **_kwargs: 0.0005)
    stall, arrived = threading.Event(), threading.Event()
    base = default_script({"lem:sum": "gap"})

    def script(kind, node, user, headers):
        if kind != "other":
            return base(kind, node, user, headers)
        if "Stop me" in user.split("</QUESTION>", 1)[0]:
            arrived.set()
            stall.wait(30)
        return "Lemma 2.2 has a gap: see [[result:lem:sum]] and Lemma~\\ref{lem:rep}."

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    server.seen, server.lock, server.script = [], threading.Lock(), script
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    run_id = ""
    try:
        issued: list = []
        _loopback_broker(monkeypatch, issued, f"http://127.0.0.1:{server.server_address[1]}/internal/v1")
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(name, "*")
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        job = jobs.start_job(engine="analyze", run_kind="analyze", source={"text": PAPER}, user=None,
                             model="kimi-k3", auth_route="api_key")
        run_id = job["run_id"]
        assert job["credential_source"] == "sponsored_kimi"
        assert _wait(lambda: jobs.get_job(job["job_id"])["status"] != "running")
        assert jobs.get_job(job["job_id"])["status"] == "finished", jobs.get_job(job["job_id"])
        assert _wait(lambda: not gate.holds(run_id))
        record = jobs._load_run_record(run_id)
        workspace = Path(record["workspace"])
        folder = workspace / "analysis"
        published = {name: (folder / name).read_bytes() for name in ("analysis_graph.json", "receipt.json",
                                                                      "report.md")}
        assert record["status"] == "finished" and record["analyze"]["summary"]["gap"] == 1

        # A question about Lemma 2.2, answered by the runner on the analysis's sponsored route.
        result = jobs.start_analysis_ask(run_id, {"question": "Why is there a gap?", "node_id": "lem:sum"}, user=None)
        assert result["status"] == "asking"
        assert _wait(lambda: not jobs.run_is_active(run_id) and not gate.holds(analyze_runs.ask_key(run_id)))
        block = jobs._load_run_record(run_id)["analyze"]
        entry = block["asks"][-1]
        assert entry["status"] == "done", entry
        assert entry["answer"] == "Lemma 2.2 has a gap: see [[result:lem:sum]] and Lemma 2.1."
        assert entry["refs"] == ["lem:sum"] and entry["about"] == ["lem:sum"] and entry["model"] == "kimi-k3"
        assert entry["usage"]["calls"] == 1 and entry["usage"]["cost_usd"] == pytest.approx(0.0005)
        assert block["ask_usage"]["calls"] == 1 and block["asks_started"] == 1 and "ask" not in block
        asked = [seen for seen in server.seen if seen["kind"] == "other"]
        assert len(asked) == 1 and asked[0]["path"] == "/internal/v1/chat/completions"
        assert asked[0]["auth"] == f"Bearer {TOKEN}" and "<ABOUT_RESULT>\n[lem:sum] Lemma 2.2" in asked[0]["user"]
        assert next(message["content"] for message in asked[0]["payload"]["messages"]
                    if message["role"] == "system") == prompts.ANALYZE_ASK_SYSTEM
        answer = analyze_ask.STORE.load_answer(workspace, entry["ask_id"])
        assert answer["route"] == "sponsored_kimi" and answer["carried"] is False

        # A follow-up carries the subject and reads the first exchange.
        jobs.start_analysis_ask(run_id, {"question": "Why does that matter?"}, user=None)
        assert _wait(lambda: not jobs.run_is_active(run_id) and not gate.holds(analyze_runs.ask_key(run_id)))
        entry = jobs._load_run_record(run_id)["analyze"]["asks"][-1]
        assert entry["status"] == "done" and entry["about"] == ["lem:sum"]
        prompt = [seen for seen in server.seen if seen["kind"] == "other"][-1]["user"]
        assert "<EARLIER>\nQuestion: Why is there a gap?" in prompt and prompts.CARRIED_NOTE in prompt

        # Both questions are booked as questions, apart from the analysis.
        with sqlite3.connect(usage_ledger.DB_PATH) as connection:
            rows = connection.execute("SELECT stage, calls, cost_usd FROM usage_daily WHERE run_id=?",
                                      (run_id,)).fetchall()
        booked = {row[0]: row[1:] for row in rows}
        assert booked["ask"][0] == 2 and booked["ask"][1] == pytest.approx(0.001)
        # One sponsored start today (the analysis); its questions count among the day's questions.
        usage = json.loads(gate.usage_path.read_text())
        assert (usage["runs"], usage["asks"]) == (1, 2)

        # Stop during a question: the runner settles, and the analysis is as it was.
        jobs.start_analysis_ask(run_id, {"question": "Stop me before I answer"}, user=None)
        assert arrived.wait(60), "the question never reached the provider"
        stopped = jobs.stop_run(run_id)
        assert stopped["status"] == "stopped" and stopped["ask"] is True
        assert _wait(lambda: not gate.holds(analyze_runs.ask_key(run_id))
                     and (jobs._load_run_record(run_id)["analyze"].get("asks") or [{}])[-1].get("status") != "done"
                     and "ask" not in jobs._load_run_record(run_id)["analyze"], 60)
        record = jobs._load_run_record(run_id)
        entry = record["analyze"]["asks"][-1]
        assert entry["question"] == "Stop me before I answer"
        assert entry["status"] == "stopped" and entry["error"] == "The question was stopped"
        assert record["status"] == "finished" and record["analyze"]["summary"]["gap"] == 1
        assert published == {name: (folder / name).read_bytes() for name in published}
        assert record["analyze"]["asks_started"] == 3
        # The stopped question reached a model: its start is spent.
        assert json.loads(gate.usage_path.read_text())["asks"] == 3
    finally:
        stall.set()
        if run_id:
            jobs._stop_event(run_id).clear()
        server.shutdown()
        server.server_close()



def test_a_refused_recheck_leaves_the_one_running(ask_console, fake_provider, monkeypatch):
    # Review: a refused re-check (a second click, a second tab) failed every
    # running re-check job of the analysis, so its question or another
    # re-check could start beside the one still running.
    run_id = "analyze_ask_c_recheck_twice"
    _finished_analysis(run_id, MEMBER["id"], fake_provider)
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))
    monkeypatch.setattr(jobs, "_execute_recheck", lambda **kwargs: None)  # still checking
    first = jobs.start_analysis_recheck(run_id, "lem:sum", user=MEMBER)
    try:
        for node_id, status in (("lem:sum", 409), ("lem:nope", 400)):
            with pytest.raises(analyze_runs.AnalyzeRequestError) as refused:
                jobs.start_analysis_recheck(run_id, node_id, user=MEMBER)
            assert refused.value.status == status
        assert jobs.get_job(first["job_id"])["status"] == "running" and jobs.run_is_active(run_id)
        record = jobs.reconcile_run_status(run_id)
        assert record["analyze"]["recheck"]["status"] == "running"
        with pytest.raises(analyze_runs.AnalyzeRequestError) as busy:
            jobs.start_analysis_ask(run_id, {"question": "Now?"}, user=MEMBER)
        assert busy.value.status == 409 and str(busy.value) == BUSY
    finally:
        _finish_jobs(run_id)


def test_a_watcher_forgets_only_its_own_runner():
    # Review: the watcher of a stopped question exited after the next
    # question's runner had registered, and unregistered that one, so a
    # later Stop could not signal it.
    old, new = object(), object()
    jobs._register_proc("analyze_ask_c_procs", old)
    jobs._register_proc("analyze_ask_c_procs", new)
    try:
        jobs._unregister_proc("analyze_ask_c_procs", old)
        assert jobs._PROCS.get("analyze_ask_c_procs") is new
        jobs._unregister_proc("analyze_ask_c_procs", new)
        assert "analyze_ask_c_procs" not in jobs._PROCS
    finally:
        jobs._PROCS.pop("analyze_ask_c_procs", None)
        jobs._stop_event("analyze_ask_c_procs").clear()


def test_a_stopped_question_holds_the_run_until_its_runner_exits(ask_console, fake_provider, monkeypatch):
    # Review: Stop marked the question's job stopped at once, so while its
    # runner was still winding down a new question could start, and a read
    # called the question "interrupted (the server restarted)".
    run_id = "analyze_ask_c_stop_wait"
    _finished_analysis(run_id, MEMBER["id"], fake_provider)
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: None)  # its runner winds down
    terminated: list = []
    monkeypatch.setattr(jobs, "_terminate_registered_proc", lambda proc, force=True: terminated.append(force))
    first = jobs.start_analysis_ask(run_id, {"question": "One?"}, user=MEMBER)
    runner_proc = object()
    jobs._register_proc(run_id, runner_proc)
    try:
        stopped = jobs.stop_run(run_id)
        assert stopped["status"] == "stopped" and stopped.get("ask") is True and terminated == [False]
        # The runner has not exited: the job still runs, nothing starts beside it,
        # and the question is not read as lost.
        assert jobs.get_job(first["job_id"])["status"] == "running"
        with pytest.raises(analyze_runs.AnalyzeRequestError) as busy:
            jobs.start_analysis_ask(run_id, {"question": "Two?"}, user=MEMBER)
        assert busy.value.status == 409
        record = jobs.reconcile_run_status(run_id)
        assert record["analyze"]["ask"]["status"] == "running"
    finally:
        jobs._unregister_proc(run_id, runner_proc)
        _finish_jobs(run_id)
        jobs._stop_event(run_id).clear()


def test_a_carried_answer_says_so_in_the_record(ask_console, fake_provider, monkeypatch):
    run_id = "analyze_ask_c_carried"
    _finished_analysis(run_id, MEMBER["id"], fake_provider)
    workspace = jobs.workspace_dir(run_id)
    state = {"ask_id": "0123456789ab", "status": "running", "question": "Why?", "selection": "", "view": "report",
             "node_id": None, "job_id": "carriedjob", "started_at": jobs._now()}
    jobs._set_ask_state(run_id, state, started=True, block="analyze")
    result = {"answer": "Because.", "refs": [], "about": ["lem:sum"], "carried": True, "model": "m"}
    monkeypatch.setattr(jobs, "_watch_runner", lambda *args, **kwargs: (
        json.dumps({"type": "analyze.ask", "result": result}) + "\n", 0, ""))
    monkeypatch.setattr(jobs, "_book_recheck_usage", lambda *args, **kwargs: {})
    jobs._execute_ask(run_id=run_id, job_id="carriedjob", state=state, workspace=workspace, env={}, timeout=60,
                      gate_key=None, on_exit=None, model="m", kind="analyze")
    entry = jobs._load_run_record(run_id)["analyze"]["asks"][-1]
    assert entry["status"] == "done" and entry["about"] == ["lem:sum"] and entry["carried"] is True


def test_sponsored_questions_stop_when_sponsored_runs_are_off(ask_console, fake_provider, monkeypatch):
    # Review: turning sponsored Analyze off (a daily cap of 0) refused
    # re-checks and formalizing on a sponsored analysis but not its questions,
    # and the console was told it could ask.
    _fake_ask(monkeypatch, [])
    run_id = "analyze_ask_c_sponsored_off"
    _finished_analysis(run_id, MEMBER["id"], fake_provider, credential_source="sponsored_kimi", model="kimi-k3")
    monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "0")
    with pytest.raises(analyze_runs.AnalyzeRequestError) as refused:
        jobs.start_analysis_ask(run_id, {"question": "Why?"}, user=MEMBER)
    assert refused.value.status == 403 and str(refused.value) == analyze_runs.SPONSORED_ASKS_OFF["analyze"]
    assert not analyze_runs.GATE.holds(analyze_runs.ask_key(run_id))
    member = analyze_runs.viewer_policy(MEMBER)["asks"]
    assert member["enabled"] is True and member["sponsored"] is False
    assert member["sponsored_reason"] == analyze_runs.SPONSORED_ASKS_OFF["analyze"]
    guest = analyze_runs.viewer_policy({"id": 93, "guest": True})["asks"]
    assert guest["enabled"] is False and guest["reason"] == analyze_runs.SPONSORED_ASKS_OFF["analyze"]
    readable = readability_runs.viewer_policy({"id": 93, "guest": True})["asks"]
    assert readable["enabled"] is False and "Sponsored Readability is turned off" in readable["reason"]
    # The questions' own cap at 0 for guests: a guest is told to sign in.
    monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "40")
    monkeypatch.setenv(analyze_runs.GUEST_ASK_DAILY_CAP_ENV, "0")
    guest = analyze_runs.viewer_policy({"id": 93, "guest": True})["asks"]
    assert guest["enabled"] is False and guest["reason"] == analyze_runs.ASK_GATE_MESSAGES.guest_off
    assert analyze_runs.viewer_policy(MEMBER)["asks"]["sponsored"] is True
    # An own-key analysis is not sponsored: its questions go on.
    monkeypatch.setenv(analyze_runs.DAILY_CAP_ENV, "0")
    _finished_analysis("analyze_ask_c_own_key_on", MEMBER["id"], fake_provider)
    assert jobs.start_analysis_ask("analyze_ask_c_own_key_on", {"question": "Why?"}, user=MEMBER)["status"] == "asking"


def test_a_recheck_refused_during_a_question_spends_no_guest_allowance(ask_console, fake_provider, monkeypatch):
    # Review: a guest's re-check refused because a question was answered
    # still spent a guest Formal/DAG action (three hourly windows).
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: None)  # still being answered
    with running_server() as port:
        guest, cookie, _ = _issue_guest(port)
        run_id = "analyze_ask_c_guest_busy"
        _finished_analysis(run_id, guest["id"], fake_provider, guest=True)
        try:
            asked = _ask(port, run_id, cookie, {"question": "Why?"})
            assert asked[0] == 202, asked
            refused = _post(port, f"/api/run/{run_id}/analysis/recheck", cookie, {"node_id": "lem:sum"})
        finally:
            _finish_jobs(run_id)
    assert refused[0] == 409 and refused[2]["error"] == jobs.RECHECK_BUSY
    assert not console._GUEST_DERIVED_WINDOWS.get(guest["id"])
    assert not console._GUEST_RECHECK_WINDOWS.get(guest["id"])


def test_a_repeated_stop_leaves_a_question_to_its_worker(ask_console, fake_provider, monkeypatch):
    # Review: a Stop that found no registered runner (before it registers, or
    # after it exits while its worker books the calls) marked the job
    # stopped with its worker alive.
    run_id = "analyze_ask_c_stop_twice"
    _finished_analysis(run_id, MEMBER["id"], fake_provider)
    monkeypatch.setattr(jobs, "_continuation_auth_environment",
                        lambda **kwargs: ({"AGENT_MONITOR_PYTHON": "python"}, "api_key"))
    monkeypatch.setattr(jobs, "_execute_ask", lambda **kwargs: None)  # its worker is still at work
    first = jobs.start_analysis_ask(run_id, {"question": "One?"}, user=MEMBER)
    try:
        for _ in range(2):
            stopped = jobs.stop_run(run_id)
            assert stopped["status"] == "stopped"
            assert jobs.get_job(first["job_id"])["status"] == "running" and jobs.run_is_active(run_id)
        record = jobs.reconcile_run_status(run_id)
        assert record["analyze"]["ask"]["status"] == "running" and "asks" not in record["analyze"]
    finally:
        _finish_jobs(run_id)
        jobs._stop_event(run_id).clear()


def test_a_guest_is_told_when_sponsored_runs_are_off_for_guests_only(ask_console, monkeypatch):
    monkeypatch.setenv(analyze_runs.GUEST_DAILY_CAP_ENV, "0")
    guest = analyze_runs.viewer_policy({"id": 94, "guest": True})["asks"]
    assert guest["enabled"] is False and guest["reason"] == analyze_runs.GUEST_SPONSORED_ASKS_OFF["analyze"]
    assert "for guests" in guest["reason"]
    assert analyze_runs.viewer_policy(MEMBER)["asks"]["sponsored"] is True
