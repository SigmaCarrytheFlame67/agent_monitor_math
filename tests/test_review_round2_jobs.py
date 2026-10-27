"""Regressions from the second review (2026-10-27) of the field-test fixes:
run control, costs and the live progress of manuscript runs.

* A continuation registers its job, with its worker thread, before that
  thread starts.  When the chat line written in between failed (a full disk),
  the worker stayed registered, never to start or end: after a Stop the run
  said "stopping" until a restart.  The run list and a delete read such a
  worker (registered, not started yet) as done.
* A provider-prefixed selection ("openai:gpt-6-astra") whose calls named no
  priced model was priced at gpt-4o rates: the run's id was looked up with
  its prefix.
* Outside Max (API) a call a refusal fallback passed on adds a note naming
  the model that declined first; Analyze's and Readability's live progress
  showed it.
* A Stop that dropped a member's queued feedback gave back the member's
  newest run start, not the one the feedback booked: with that booking out
  of the hourly window, a start that did run.
* A deleted run's identity stayed in memory for the life of the process.

Nothing here reaches a model API or the network beyond the loopback console.
"""
from __future__ import annotations

import errno
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_monitor import auth, auto_pipeline, jobs, usage_ledger
from agent_monitor import console_server as console
from agent_monitor.runners import math_harness_runner as harness
from agent_monitor.runners import plain_runner
from tests.test_engine_cost_model import _cost, _engine_run, _priced_nodes, prices  # noqa: F401  (fixture)
from tests.test_math_harness_runner import PROBLEM

from test_console_guest_access import (  # noqa: F401  (fixture import)
    APP_MOUNT,
    guest_console,
    request,
    running_server,
)
from test_member_accounts import _member, _member_run, _record_continuations, member_console  # noqa: F401


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """Private run storage and job registries; follow-ups skip the account
    checks and start no derived views."""
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    for name, value in (
        ("_JOBS", {}), ("_STOP_EVENTS", {}), ("_PROCS", {}), ("_WORKERS", {}),
        ("_DELETED_RUNS", set()), ("_DELETED_RUN_IDENTITY", {}), ("_DELETED_RUN_LAST", {}),
        ("_LAUNCHING_RUNS", set()),
    ):
        monkeypatch.setattr(jobs, name, value)
    monkeypatch.setattr(jobs, "_continuation_auth_environment", lambda **kw: (dict(kw["env"]), "api_key"))
    monkeypatch.setattr(auto_pipeline, "start", lambda **_kw: {})
    return tmp_path


def _seed(run_id: str, **fields) -> Path:
    workspace = jobs.RUNS_DIR / "workspaces" / run_id
    workspace.mkdir(parents=True)
    (workspace / "problem.txt").write_text("Prove it.", encoding="utf-8")
    record = {
        "run_id": run_id, "engine": "math_harness", "problem_id": "p", "status": "finished", "owner_id": None,
        "problem_text": "Prove it.", "problems": [{"text": "Prove it.", "source": "initial"}],
        "agents": [{"trace_id": f"{run_id}::first", "output": "first session", "status": "finished"}],
        "edges": [],
    }
    record.update(fields)
    jobs._write_run(record)
    return workspace


def _settle(run_id: str, until=lambda: True) -> None:
    """Wait until nothing runs or winds down for ``run_id`` (and ``until``)."""
    deadline = time.monotonic() + 30
    while (jobs.run_is_active(run_id) or not until()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not jobs.run_is_active(run_id) and until()


def _engine_records(monkeypatch, run_id: str) -> list[str]:
    """The tasks of the sessions that ran (each finishes at once)."""
    tasks: list[str] = []

    def engine(**kwargs):
        tasks.append(kwargs["problem_text"])
        return {"run_id": run_id, "engine": "math_harness", "status": "finished",
                "agents": [{"trace_id": f"{run_id}::t{len(tasks)}", "output": "done"}]}

    monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", engine)
    return tasks


# ── a continuation's worker that never starts ───────────────────────────

_ANNOUNCED = {"continue": "Continuing run…", "message": "Continuing proof with your feedback…"}


def _start_continuation(kind: str, run_id: str) -> dict:
    if kind == "continue":
        return jobs.continue_run(run_id)
    return jobs.send_human_message(run_id, "Also handle the case n = 0.")


def _assert_free_again(run_id: str, tasks: list[str]) -> None:
    """The failed continuation left no session behind: its job failed, no
    worker is registered, and a Stop then a Continue work."""
    (job,) = jobs._JOBS.values()
    assert (job["status"], job["error"]) == ("failed", "worker did not start")
    assert jobs._WORKERS == {}
    assert not jobs.run_is_active(run_id) and not jobs.run_is_stopping(run_id)
    assert jobs.stop_run(run_id)["status"] == "not_running"
    assert jobs.continue_run(run_id)["status"] == "continuing"
    _settle(run_id, until=lambda: bool(tasks))
    assert len(tasks) == 1
    assert jobs._load_run_record(run_id)["status"] == "finished"


@pytest.mark.parametrize("kind", ["continue", "message"])
def test_a_continuation_whose_chat_line_fails_leaves_the_run_free(runs, monkeypatch, kind):
    """Before the fix the job stayed running with its unstarted worker
    registered; a Stop then left the run "stopping" for good."""
    run_id = f"math_harness_chat_line_{kind}"
    _seed(run_id)
    tasks = _engine_records(monkeypatch, run_id)
    real_append = jobs._append_chat

    def append(run_id_, role, content, **kwargs):
        if content == _ANNOUNCED[kind]:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_append(run_id_, role, content, **kwargs)

    monkeypatch.setattr(jobs, "_append_chat", append)
    with pytest.raises(OSError):
        _start_continuation(kind, run_id)
    monkeypatch.setattr(jobs, "_append_chat", real_append)
    _assert_free_again(run_id, tasks)


class _ThreadThatCannotStart(threading.Thread):
    def start(self):
        raise RuntimeError("can't start new thread")


class _NoNewThreads:
    """``jobs.threading`` at the thread limit (patched on ``jobs`` alone)."""

    Thread = _ThreadThatCannotStart

    def __getattr__(self, name):
        return getattr(threading, name)


@pytest.mark.parametrize("kind", ["continue", "message"])
def test_a_continuation_whose_thread_cannot_start_leaves_the_run_free(runs, monkeypatch, kind):
    """Pins the thread's start failing: its worker is ended and its job
    failed, as for the chat line above."""
    run_id = f"math_harness_no_thread_{kind}"
    _seed(run_id)
    tasks = _engine_records(monkeypatch, run_id)
    monkeypatch.setattr(jobs, "threading", _NoNewThreads())
    with pytest.raises(RuntimeError, match="can't start new thread"):
        _start_continuation(kind, run_id)
    monkeypatch.setattr(jobs, "threading", threading)
    _assert_free_again(run_id, tasks)


def test_a_worker_registered_before_its_start_is_not_done(runs):
    """A Stop between a continuation's registration and its thread's start:
    the run is stopping (run_is_stopping), but the run list offered
    Continue, and a delete kept no record for the usage the session still
    books (both read the unstarted thread as ended)."""
    run_id = "math_harness_registered_worker"
    _seed(run_id)
    thread = threading.Thread(target=lambda: None, daemon=True)
    jobs._register_claimed_job(run_id, {"job_id": "j1", "run_id": run_id, "engine": "math_harness",
                                        "status": "running", "kind": "continue"}, thread)
    assert jobs.stop_run(run_id)["status"] == "stopped"
    assert jobs.run_is_stopping(run_id)
    (listed,) = jobs.list_jobs()
    assert listed["status"] == "stopped" and listed.get("stopping") is True
    jobs.delete_run(run_id)
    assert jobs._DELETED_RUN_LAST.get(run_id, {}).get("run_id") == run_id
    jobs._end_worker("j1")  # its worker ends (or is found never to start)
    assert run_id not in jobs._DELETED_RUN_LAST


# ── costs ────────────────────────────────────────────────────────────────

_SNAPSHOT_REPLY = "# Result\n\nLet $a=2m$ and $b=2n$. Then $a+b=2(m+n)$, which is even. $\\blacksquare$\n"


def test_plain_on_a_prefixed_selection_is_priced_at_the_resolved_id(prices, tmp_path, monkeypatch):
    """"openai:gpt-6-astra" requests gpt-6-astra; the reply names a dated
    snapshot the price table lacks, and the run's own model was looked up
    with its prefix: the node was priced at gpt-4o rates."""
    sent: list[str] = []

    def fake_http_json(url, *, method, headers, payload, timeout):
        sent.append(payload["model"])
        return 200, {
            "model": "gpt-6-astra-2026-09-03",
            "output_text": _SNAPSHOT_REPLY,
            "usage": {"input_tokens": 10_000, "output_tokens": 20_000},
        }

    events: list[dict] = []
    monkeypatch.chdir(tmp_path)
    with (
        patch.dict(plain_runner.os.environ, {"PLAIN_MODEL": "openai:gpt-6-astra",
                                             "OPENAI_API_KEY": "sk-test-not-a-key"}, clear=True),
        patch.object(plain_runner.sys, "argv", ["plain_runner.py", PROBLEM]),
        patch.object(plain_runner, "subscription_enabled", return_value=False),
        patch.object(plain_runner, "claude_subscription_enabled", return_value=False),
        patch("agent_monitor.settings._http_json", side_effect=fake_http_json),
        patch.object(plain_runner, "emit", side_effect=events.append),
    ):
        assert plain_runner.main() == 0
    assert sent == ["gpt-6-astra"]
    run = _engine_run(events, engine="plain", requested_model="openai:gpt-6-astra")
    (node,) = _priced_nodes(run)
    assert node["model"] == "gpt-6-astra-2026-09-03"
    assert node["cost_usd"] == pytest.approx(_cost("gpt-6-astra", uncached=10_000, output=20_000))


def test_a_recheck_call_on_a_prefixed_selection_is_booked_at_the_resolved_id(prices):
    """A re-check or question call that names no model is booked at the
    analysis's model: its prefixed id was priced (and booked) as gpt-4o."""
    unnamed = {"type": "turn.completed", "stage": "check-1", "usage": {"input_tokens": 10_000, "output_tokens": 2_000}}
    tally = jobs._book_recheck_usage("analyze_prefixed", json.dumps(unnamed) + "\n", model="openai:gpt-6-astra")
    at_model = _cost("gpt-6-astra", uncached=10_000, output=2_000)
    assert tally["cost_usd"] == pytest.approx(at_model)
    by_model = {row["key"]: row["cost_usd"] for row in usage_ledger.usage_summary({})["by_model"]}
    assert by_model == {"gpt-6-astra": pytest.approx(at_model)}


def test_a_name_no_api_provider_takes_is_not_resolved():
    """Guard: only a provider prefix goes; an alias stays as it is (and so
    never takes an exact price, see test_engine_cost_model)."""
    assert jobs._requested_model_id(" OpenAI:gpt-6-astra ") == "gpt-6-astra"
    assert jobs._requested_model_id("claude:claude-opus-5-5") == "claude-opus-5-5"
    for name in ("gpt-6-astra", "opus", "max", "ft:gpt-4o:org", "anthropic/claude-x"):
        assert jobs._requested_model_id(name) == name
    assert jobs._requested_model_id("  ") is None


# ── the live progress of a manuscript run ────────────────────────────────


@pytest.mark.parametrize(
    ("label", "stage", "note", "progress"),
    [
        ("Analyze", "check-3-low", "3 Check", console._analysis_progress_text),
        ("Readability", "rewrite-2-retry-low", "3 Rewrite", console._readability_progress_text),
    ],
)
def test_a_refusal_fallbacks_note_is_not_progress(label, stage, note, progress):
    events: list[dict] = []
    ctx = harness._Ctx(
        route=harness.Route("anthropic", "claude-fable-5-1", "api_chat"), budgets=harness.Budgets(max_calls=4),
        started=time.monotonic(), env={"ANTHROPIC_API_KEY": "test-key"},
        emitter=harness._RunEmitter(events.append), label=label,
    )
    assert not ctx.api_max
    shares = (("claude-fable-5-1", {"input_tokens": 1000, "output_tokens": 50}),
              ("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 2000}))
    harness._emit_call_usage(ctx, stage, {"input_tokens": 2000, "output_tokens": 2050}, route=ctx.route,
                             model="claude-fable-5-1", provider="anthropic", by_model=shares, out=events.append)
    notes = [event["item"]["text"] for event in events if event.get("type") == "item.completed"]
    assert notes == [f"{label} · {stage} · declined first by claude-fable-5-1"]
    agents = [{"output": f"{label} · {note}"}, {"output": f"{label} · {stage}"}, *({"output": text} for text in notes)]
    assert progress({"status": "running", "agents": agents}) == note


# ── a member's run start booked by queued feedback ──────────────────────


class _ConsoleClock:
    """console_server's ``time`` with its monotonic clock moved on."""

    def __init__(self):
        self.offset = 0.0

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        return time.monotonic() + self.offset


def _member_on_a_running_run(port, monkeypatch, tmp_path):
    _record_continuations(monkeypatch)
    user, cookie = _member(port)
    member_id = int(user["id"])
    auth.set_user_env(member_id, {"ANTHROPIC_API_KEY": "sk-ant-member-000000000000"})
    run_id = _member_run(monkeypatch, tmp_path, member_id)

    def post(path: str, body: dict) -> dict:
        status, _headers, raw = request(port, "POST", f"{APP_MOUNT}/api/runs/{run_id}/{path}", body=body,
                                        headers={"Cookie": cookie})
        assert status == 200, raw
        return json.loads(raw)

    def window() -> list[float]:
        return list(console._MEMBER_RUN_WINDOWS.get(member_id) or [])

    return member_id, post, window


def test_a_stop_gives_back_the_start_the_dropped_feedback_booked(member_console, monkeypatch, tmp_path):
    """Not the member's newest start (one that ran, here a start booked
    after the message)."""
    monkeypatch.setattr(jobs, "_WORKERS", {})
    monkeypatch.setattr(jobs, "_PROCS", {})
    with running_server() as port:
        member_id, post, window = _member_on_a_running_run(port, monkeypatch, tmp_path)
        assert post("chat", {"message": "Then case 0."})["status"] == "queued"
        # A second message joins the follow-up the first booked: its own start is given back.
        assert post("chat", {"message": "And case 1."})["joined_follow_up"] is True
        (booked,) = window()
        assert console._reserve_member_run_slot(member_id)[0] is None  # another run starts meanwhile
        assert window()[0] == booked
        newer = window()[1]
        stopped = post("stop", {})
        assert stopped["status"] == "stopped"
        assert not {"dropped_authors", "dropped_bookings"} & set(stopped)
        assert window() == [newer]


def test_a_stop_gives_nothing_back_once_that_start_left_the_hourly_window(member_console, monkeypatch, tmp_path):
    """The message waited over an hour (behind a long turn): its start no
    longer counts, and the member has used the hour's twelve starts since.
    The Stop gave back the newest of those, a start that ran."""
    monkeypatch.setattr(jobs, "_WORKERS", {})
    monkeypatch.setattr(jobs, "_PROCS", {})
    clock = _ConsoleClock()
    monkeypatch.setattr(console, "time", clock)
    with running_server() as port:
        member_id, post, window = _member_on_a_running_run(port, monkeypatch, tmp_path)
        assert post("chat", {"message": "Then case 0."})["status"] == "queued"
        clock.offset = 70 * 60
        for _ in range(12):
            assert console._reserve_member_run_slot(member_id)[0] is None
        limit = "Your account has reached its hourly run limit; please try again later"
        assert console._reserve_member_run_slot(member_id)[0] == limit
        assert post("stop", {})["status"] == "stopped"
        assert len(window()) == 12
        assert console._reserve_member_run_slot(member_id)[0] == limit


# ── a deleted run's identity ─────────────────────────────────────────────


def test_deleted_run_identities_are_capped_oldest_first(runs, monkeypatch):
    """One entry per deleted run stayed for the life of the process (the
    guest purge deletes runs in bulk).  Beyond the cap the oldest go, but
    not one whose session still winds down (its last writes need it)."""
    monkeypatch.setattr(jobs, "_DELETED_RUN_IDENTITY_MAX", 3, raising=False)
    winding = "math_harness_winding_down"
    _seed(winding, owner_id=7)
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()
    try:
        jobs._track_worker("jw", winding, worker)
        jobs.delete_run(winding)
        deleted = [f"math_harness_deleted_{index}" for index in range(6)]
        for run_id in deleted[:5]:
            _seed(run_id, owner_id=7)
            jobs.delete_run(run_id)
        assert list(jobs._DELETED_RUN_IDENTITY) == [winding, *deleted[3:5]]
        assert jobs._DELETED_RUN_IDENTITY[deleted[4]]["owner_id"] == 7
    finally:
        release.set()
        worker.join(5)
    jobs._end_worker("jw")
    _seed(deleted[5], owner_id=7)
    jobs.delete_run(deleted[5])
    assert list(jobs._DELETED_RUN_IDENTITY) == deleted[3:]
