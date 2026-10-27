"""Regressions from the 2026-10-27 review of the field-test fixes (main 4445254).

Costs:

* Outside Max (API) a call that a refusal fallback passed on was booked as
  one, at the requested model's rates; now each model's share is priced at
  its own rates, as in Max (API), and only Max (API) names the provider.
* A Plain reply naming a dated OpenAI snapshot (which the price table lacks)
  was priced at gpt-4o rates, not at the run's exactly priced model.
* An Analyze re-check or question on a Claude account was priced at the
  run's selector ("opus", which the price lookup reads as Claude 3 Opus).
* A failed but billed call on a provider-prefixed API route
  ("openai:gpt-5.2") named the prefixed id, which no price entry has.

Ansatz CGM follow-ups: a worker that copied its problem from the task's
numbered SESSION PROBLEMS list ("2. text  ← new this turn") had the
problem's own "Proof." line read as its proof's start.

Run control:

* A message a Stop dropped came back as a session problem when the stopped
  follow-up still ended finished (its final write merged its start-of-turn
  list back).
* A member's run start booked by queued feedback stayed booked when a Stop
  dropped that feedback.
* A Stop, then a Continue, before a continuation's worker thread started
  found the run idle: a second session ran beside the stopped one, which
  lost its stop flag.
* A deleted run's identity kept every scalar field of its record (the whole
  problem text) for the life of the process.

Nothing here reaches a model API or the network beyond the loopback console.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_monitor import auth, auto_pipeline, jobs, usage_ledger
from agent_monitor import console_server as console
from agent_monitor.proof_graph import _resolve_llm_provider
from agent_monitor.runners import api_backend, plain_runner
from agent_monitor.runners import math_harness_runner as runner
from tests.test_api_backend_anthropic import _message
from tests.test_cgm_problem_copy import PROOF_TO_COMPLETE, WORKER_PROOF, _theorem
from tests.test_engine_cost_model import (  # noqa: F401  (prices is a fixture)
    _CALL_USAGE,
    _cost,
    _engine_run,
    _event_cost,
    _priced_nodes,
    _usage_events,
    prices,
)
from tests.test_math_harness_runner import (
    PROBLEM,
    _accepted_outputs,
    _api_run,
    _curator,
    _in_directory,
    _ok,
    _receipt,
    _strategist,
)

from test_console_guest_access import (  # noqa: F401  (fixture import)
    APP_MOUNT,
    guest_console,
    request,
    running_server,
)
from test_member_accounts import _member, _member_run, _record_continuations, member_console  # noqa: F401

_TAG = "  ← new this turn"


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


def _seed(run_id: str, *, status: str = "finished", **fields) -> Path:
    workspace = jobs.RUNS_DIR / "workspaces" / run_id
    workspace.mkdir(parents=True)
    (workspace / "problem.txt").write_text("Prove it.", encoding="utf-8")
    (workspace / "proof.md").write_text("# Claim\n\nThe first session's proof.\n", encoding="utf-8")
    record = {
        "run_id": run_id, "engine": "math_harness", "problem_id": "p", "status": status, "owner_id": None,
        "problem_text": "Prove it.", "problems": [{"text": "Prove it.", "source": "initial"}],
        "agents": [{"trace_id": f"{run_id}::first", "output": "first session", "status": "finished"}],
        "edges": [],
    }
    record.update(fields)
    jobs._write_run(record)
    return workspace


def _problems(run_id: str) -> list[str]:
    return [problem["text"] for problem in jobs._load_run_record(run_id)["problems"]]


def _settle(run_id: str, until=lambda: True) -> None:
    """Wait until nothing runs or winds down for ``run_id`` (and ``until``)."""
    deadline = time.monotonic() + 30
    while (jobs.run_is_active(run_id) or not until()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not jobs.run_is_active(run_id) and until()


def _finished(run_id: str, turn: int) -> dict:
    return {"run_id": run_id, "engine": "math_harness", "status": "finished",
            "agents": [{"trace_id": f"{run_id}::t{turn}", "output": "done"}]}


# ── costs ────────────────────────────────────────────────────────────────


def _fallback_reply(text: str, *, declined: tuple[int, int], answered: tuple[int, int], **fields):
    """A Claude API reply that claude-fable-5-1 declined and claude-opus-5-5
    answered (Anthropic's server-side refusal fallback)."""
    usage = {
        "input_tokens": answered[0],
        "output_tokens": answered[1],
        "iterations": [
            {"type": "message", "model": "claude-fable-5-1", "input_tokens": declined[0],
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": declined[1]},
            {"type": "fallback_message", "model": "claude-opus-5-5", "input_tokens": answered[0],
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": answered[1]},
        ],
    }
    return _message(model="claude-opus-5-5", content=[{"type": "text", "text": text}], usage=usage, **fields)


def _claude_api_cgm_run(replies, *, code: int) -> list[dict]:
    """A single-route CGM run on claude-fable-5-1's own API key (not Max
    (API)); only the Anthropic SDK call is faked: ``replies`` in order, or
    a function that answers every call."""
    events: list[dict] = []
    env = {
        "AGENT_MONITOR_SELECTED_MODEL": "claude-fable-5-1",
        "ANTHROPIC_API_KEY": "sk-ant-test-not-a-key-0000",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "2",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
    }
    with tempfile.TemporaryDirectory() as raw:
        with (
            _in_directory(Path(raw)),
            patch.dict(runner.os.environ, env, clear=True),
            patch.object(runner.sys, "argv", ["math_harness_runner.py", PROBLEM]),
            patch.object(runner, "codex_subscription_enabled", return_value=False),
            patch.object(runner, "claude_subscription_enabled", return_value=False),
            patch.object(api_backend, "_anthropic_message", side_effect=replies),
            patch("agent_monitor.settings._http_json") as http,
            patch.object(runner, "emit", side_effect=events.append),
        ):
            assert runner.main() == code
    http.assert_not_called()
    return events


def _declined_notes(events: list[dict]) -> list[str]:
    return [
        event["item"]["text"] for event in events
        if (event.get("item") or {}).get("type") == "reasoning" and "declined first by" in event["item"]["text"]
    ]


def test_a_refusal_fallback_outside_max_api_is_priced_per_model(prices):
    """The strategist's call was declined by fable and answered by opus: its
    usage was one event at fable's rates, opus's 2,000 output tokens too."""
    replies = [_fallback_reply(_strategist(2), declined=(1000, 50), answered=(1000, 2000))]
    replies += [
        _message(model="claude-fable-5-1", content=[{"type": "text", "text": text}],
                 usage={"input_tokens": 10, "output_tokens": 10})
        for text in _accepted_outputs()[1:]
    ]
    events = _claude_api_cgm_run(replies, code=0)
    usage = _usage_events(events)
    # The model that answered first (its share goes on the reply's node),
    # then the model that declined, after a note of its own.
    assert [
        (event["model"], event["usage"]["input_tokens"], event["usage"]["output_tokens"])
        for event in usage if event["stage"] == "strategist"
    ] == [("claude-opus-5-5", 1000, 2000), ("claude-fable-5-1", 1000, 50)]
    assert [note.split(" · ", 1)[1] for note in _declined_notes(events)] == [
        "strategist · declined first by claude-fable-5-1"]
    # Outside Max (API) no usage event names a provider.
    assert {frozenset(event) for event in usage} == {frozenset({"type", "stage", "usage", "model"})}
    assert {event["model"] for event in usage if event["stage"] != "strategist"} == {"claude-fable-5-1"}

    run = _engine_run(events, requested_model="claude-fable-5-1")
    nodes = _priced_nodes(run)
    (answered,) = [node for node in nodes if node["model"] == "claude-opus-5-5"]
    (declined,) = [node for node in nodes if "declined first by" in (node.get("output") or "")]
    assert answered["output"].startswith("{") and answered["output_tokens"] == 2000
    assert answered["cost_usd"] == pytest.approx(_cost("claude-opus-5-5", uncached=1000, output=2000))
    assert declined["model"] == "claude-fable-5-1" and declined["output_tokens"] == 50
    assert declined["cost_usd"] == pytest.approx(_cost("claude-fable-5-1", uncached=1000, output=50))
    assert run["totals"]["cost_usd"] == pytest.approx(sum(_event_cost(event) for event in usage), abs=1e-5)


def test_a_refusal_that_remains_outside_max_api_is_booked_per_model(prices):
    """Declined by both models: the failed call is still billed, each
    model's share at its own rates (it was all at fable's)."""
    refused = _fallback_reply("partial", declined=(1000, 50), answered=(1000, 400), stop_reason="refusal")
    events = _claude_api_cgm_run(lambda _options, _request: refused, code=1)
    usage = _usage_events(events)
    billed = [(event["stage"], event["model"], event["usage"]["output_tokens"]) for event in usage]
    stages = list(dict.fromkeys(stage for stage, _model, _output in billed))
    assert stages[0] == "strategist"
    assert billed == [
        row for stage in stages for row in ((stage, "claude-opus-5-5", 400), (stage, "claude-fable-5-1", 50))
    ]
    assert [note.split(" · ", 1)[1] for note in _declined_notes(events)] == [
        f"{stage} · declined first by claude-fable-5-1" for stage in stages]
    assert {frozenset(event) for event in usage} == {frozenset({"type", "stage", "usage", "model"})}


_SNAPSHOT_REPLY = "# Result\n\nLet $a=2m$ and $b=2n$. Then $a+b=2(m+n)$, which is even. $\\blacksquare$\n"


def test_plain_on_an_openai_snapshot_is_priced_at_the_requested_model(prices, tmp_path, monkeypatch):
    """OpenAI's reply names a dated snapshot of the requested id, which the
    price table lacks: the node was priced at gpt-4o rates."""
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
        patch.dict(plain_runner.os.environ, {"PLAIN_MODEL": "gpt-6-astra", "OPENAI_API_KEY": "sk-test-not-a-key"},
                   clear=True),
        patch.object(plain_runner.sys, "argv", ["plain_runner.py", PROBLEM]),
        patch.object(plain_runner, "subscription_enabled", return_value=False),
        patch.object(plain_runner, "claude_subscription_enabled", return_value=False),
        patch("agent_monitor.settings._http_json", side_effect=fake_http_json),
        patch.object(plain_runner, "emit", side_effect=events.append),
    ):
        assert plain_runner.main() == 0
    assert sent == ["gpt-6-astra"]
    run = _engine_run(events, engine="plain", requested_model="gpt-6-astra")
    (node,) = _priced_nodes(run)
    # The node still says which model wrote the reply; its price is the run's.
    assert node["model"] == "gpt-6-astra-2026-09-03"
    assert node["cost_usd"] == pytest.approx(_cost("gpt-6-astra", uncached=10_000, output=20_000))
    assert node["cost_usd"] != pytest.approx(_cost("gpt-4o", uncached=10_000, output=20_000))


@pytest.mark.parametrize("stage", ["recheck", "ask"])
def test_a_claude_account_recheck_or_question_is_booked_at_the_model_its_calls_name(prices, stage):
    """The re-check and Ask workers get the analysis's selector, "opus" on a
    Claude account; each call's usage names the model Claude Code reported."""
    record = {"engine": "analyze", "auth_route": "claude_subscription", "requested_model": "opus",
              "agents": [{"model": "claude-opus-5-5"}]}
    selector = jobs._continuation_model(record, None)
    assert selector == "opus"
    named = {"type": "turn.completed", "stage": "check-1", "model": "claude-opus-5-5",
             "usage": {"input_tokens": 100_000, "output_tokens": 10_000, "cached_input_tokens": 0,
                       "input_excludes_cache": True}}
    unnamed = {"type": "turn.completed", "stage": "check-2", "usage": {"input_tokens": 1_000, "output_tokens": 100}}
    output = "".join(json.dumps(event) + "\n" for event in (named, unnamed))
    tally = jobs._book_recheck_usage("analyze_booked", output, model=selector, stage=stage)
    at_opus = _cost("claude-opus-5-5", uncached=100_000, output=10_000)
    # A call that names no model: never "opus" read as Claude 3 Opus.
    at_fallback = _cost("gpt-4o", uncached=1_000, output=100)
    assert tally["calls"] == 2
    assert tally["cost_usd"] == pytest.approx(at_opus + at_fallback)
    by_model = {row["key"]: row["cost_usd"] for row in usage_ledger.usage_summary({})["by_model"]}
    assert by_model["claude-opus-5-5"] == pytest.approx(at_opus)


def test_a_failed_billed_call_on_a_prefixed_route_names_the_resolved_id():
    """A worker call on "openai:gpt-5.2" that failed after the provider
    billed it (an empty reply) named the prefixed id; a successful call names
    the id api_chat requested ("gpt-5.2")."""
    events: list[dict] = []
    error = runner.APIBackendError(
        "OpenAI returned an empty response for gpt-5.2", usage={"input_tokens": 50, "output_tokens": 8192}
    )
    replies: list[object] = [_strategist(1), error]

    def fake_api(system: str, user: str, *, model: str, env=None, **_kwargs):
        value = replies.pop(0)
        if isinstance(value, BaseException):
            raise value
        # As api_chat: the result names the id it requested.
        requested = _resolve_llm_provider(dict(env or {}), model)[3]
        return SimpleNamespace(text=value, usage=dict(_CALL_USAGE), model=requested, provider="openai")

    env = {
        "AGENT_MONITOR_SELECTED_MODEL": "openai:gpt-5.2",
        "OPENAI_API_KEY": "sk-test-not-a-key",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
    }
    with tempfile.TemporaryDirectory() as raw:
        with (
            _in_directory(Path(raw)),
            patch.dict(runner.os.environ, env, clear=True),
            patch.object(runner.sys, "argv", ["math_harness_runner.py", PROBLEM]),
            patch.object(runner, "codex_subscription_enabled", return_value=False),
            patch.object(runner, "claude_subscription_enabled", return_value=False),
            patch.object(runner, "api_chat", side_effect=fake_api),
            patch.object(runner, "emit", side_effect=events.append),
        ):
            assert runner.main() == 1  # no worker candidate is left
    assert not replies
    assert [(event["stage"], event["model"], event["usage"]["output_tokens"]) for event in _usage_events(events)] == [
        ("strategist", "gpt-5.2", _CALL_USAGE["output_tokens"]), ("worker-1", "gpt-5.2", 8192)]


# ── a CGM follow-up's copy of its session list ───────────────────────────


@pytest.mark.parametrize("copied", ["line as shown", "line untagged", "whole list"])
def test_a_theorem_copied_from_a_follow_ups_session_list_is_read_after_the_copy(runs, monkeypatch, copied):
    """The follow-up's task (jobs' real builder) lists the session problems as
    "N. text", the new one tagged.  A worker that copied its problem from
    that list had the problem's own "Proof." line taken for its proof's
    start: the verifier and the published proof got the problem's tail and
    the tag as proof text."""
    run_id = "math_harness_follow_up_copy"
    workspace = _seed(run_id, problem_text=PROBLEM, problems=[{"text": PROBLEM, "source": "initial"}])
    seen: dict[str, str] = {}

    def engine(**kwargs):
        seen["task"] = kwargs["problem_text"]
        seen["problem.txt"] = (workspace / "problem.txt").read_text(encoding="utf-8")
        return _finished(run_id, 1)

    monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", engine)
    assert jobs.send_human_message(run_id, PROOF_TO_COMPLETE)["status"] == "continuing"
    _settle(run_id, until=lambda: "task" in seen)
    block = seen["task"].split("SESSION PROBLEMS:\n", 1)[1].split("\n\n", 1)[0]
    assert block == f"1. {PROBLEM}\n2. {PROOF_TO_COMPLETE}{_TAG}"
    statement = {
        "line as shown": block.split("\n", 1)[1],
        "line untagged": block.split("\n", 1)[1].removesuffix(_TAG),
        "whole list": block,
    }[copied]

    # The turn's runner on that task and session notebook.
    with tempfile.TemporaryDirectory() as raw:
        turn = Path(raw)
        (turn / "problem.txt").write_text(seen["problem.txt"], encoding="utf-8")
        code, calls, _events = _api_run(
            turn, [_strategist(1), _theorem(statement), _ok(), _curator()], task=seen["task"],
            env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
        )
        assert code == 0
        assert _receipt(turn)["verdict"] == "accepted"
        proof_md = (turn / "proof.md").read_text(encoding="utf-8")
    verify = calls[2]["user"]
    for shown in (verify, proof_md):
        assert WORKER_PROOF in shown
        assert shown.count("Complete the proof above.") == 1
        assert _TAG.strip() not in shown


# ── run control ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("stop_lands", ["in a published CGM turn", "after the runner finished"])
def test_a_message_a_stop_dropped_stays_out_when_the_turn_still_finishes(runs, monkeypatch, stop_lands):
    """A message sent while a follow-up was being prepared is in that
    follow-up's problem list.  A Stop drops it from the session, but the
    follow-up still ended finished (a CGM turn that had published before the
    Stop, or any turn the Stop reached between its runner's end and its last
    write), and its last write put the message back: the next turn answered
    it as new."""
    engine_name = "math_harness" if stop_lands == "in a published CGM turn" else "codex"
    run_id = f"{engine_name}_dropped_stays_out"
    workspace = _seed(run_id, engine=engine_name)
    in_prep, prepared = threading.Event(), threading.Event()

    def slow_env(**kw):
        if not prepared.is_set():
            in_prep.set()
            assert prepared.wait(30)
        return dict(kw["env"]), "api_key"

    monkeypatch.setattr(jobs, "_continuation_auth_environment", slow_env)
    tasks: list[str] = []
    entered, release = threading.Event(), threading.Event()
    stops: list[dict] = []

    def engine(**kwargs):
        tasks.append(kwargs["problem_text"])
        result = _finished(run_id, len(tasks))
        if len(tasks) == 1 and engine_name == "math_harness":
            entered.set()
            assert release.wait(30)
            result[jobs._PUBLISHED_BEFORE_STOP] = True
        return result

    monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", engine)
    real_merge = jobs._merge_continuation_run

    def merge(previous, current, *, job_id):
        if engine_name == "codex" and not stops:
            stops.append(jobs.stop_run(run_id))  # after the runner, before the last write
        return real_merge(previous, current, job_id=job_id)

    monkeypatch.setattr(jobs, "_merge_continuation_run", merge)

    first: dict = {}
    request_a = threading.Thread(target=lambda: first.update(jobs.continue_run(run_id)), daemon=True)
    request_a.start()
    assert in_prep.wait(30)
    dropped = "Also handle the case n = 0."
    assert jobs.send_human_message(run_id, dropped)["status"] == "queued"
    prepared.set()
    request_a.join(30)
    assert first.get("ok"), first
    if engine_name == "math_harness":
        assert entered.wait(30)
        stops.append(jobs.stop_run(run_id))
        release.set()
    _settle(run_id, until=lambda: bool(stops))
    assert stops[0]["status"] == "stopped"
    assert "Queued feedback (1 message) was removed from the session: the run was stopped before it ran. " \
        "Send it again to have it answered." in [message["content"] for message in jobs.list_chat(run_id)]
    assert jobs._load_run_record(run_id)["status"] == "finished"
    assert _problems(run_id) == ["Prove it."]

    # The next turn, a bare Continue, is not asked to answer it.
    assert jobs.continue_run(run_id)["ok"]
    _settle(run_id, until=lambda: len(tasks) == 2)
    assert dropped not in tasks[1]
    assert dropped not in (workspace / "problem.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("problems", [[{"text": "Prove it.", "source": "initial"}], None])
def test_a_follow_up_keeps_its_own_message(runs, monkeypatch, problems):
    """Guard for the fix above: the turn's own message, not yet in the
    record when the turn reads it, stays a session problem (also on a record
    that predates the problem list)."""
    run_id = "math_harness_own_message"
    _seed(run_id, problems=problems)
    tasks: list[str] = []
    monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry",
                        lambda **kw: tasks.append(kw["problem_text"]) or _finished(run_id, 1))
    assert jobs.continue_run(run_id, message="Also n = 0.")["ok"]
    _settle(run_id, until=lambda: bool(tasks))
    assert _problems(run_id) == ["Prove it.", "Also n = 0."]


def test_a_stop_gives_back_the_member_start_that_dropped_feedback_booked(member_console, monkeypatch, tmp_path):
    """Queued feedback books its author's run start (the follow-up it will
    start); a Stop drops it, so nothing ever starts for it."""
    threads = _record_continuations(monkeypatch)
    monkeypatch.setattr(jobs, "_WORKERS", {})
    monkeypatch.setattr(jobs, "_PROCS", {})
    with running_server() as port:
        user, cookie = _member(port)
        member_id = int(user["id"])
        auth.set_user_env(member_id, {"ANTHROPIC_API_KEY": "sk-ant-member-000000000000"})
        run_id = _member_run(monkeypatch, tmp_path, member_id)
        jobs._JOBS.clear()

        def starts() -> int:
            return len(console._MEMBER_RUN_WINDOWS.get(member_id) or [])

        def post(path: str, body: dict) -> tuple[int, dict]:
            status, _headers, raw = request(port, "POST", f"{APP_MOUNT}/api/runs/{run_id}/{path}", body=body,
                                            headers={"Cookie": cookie})
            return status, json.loads(raw)

        for turn in range(2):
            jobs._JOBS[f"held-{turn}"] = {"job_id": f"held-{turn}", "run_id": run_id, "status": "running",
                                          "owner_id": member_id, "engine": "plain"}
            status, sent = post("chat", {"message": f"Then case {turn}."})
            assert (status, sent["status"], starts()) == (200, "queued", 1)
            status, stopped = post("stop", {})
            assert (status, stopped["status"]) == (200, "stopped")
            assert "dropped_bookings" not in stopped and "dropped_authors" not in stopped
            assert starts() == 0, "the dropped feedback kept the run start it booked"
        assert threads.launched == []

        # A start that was used stays booked: a Stop with nothing queued gives nothing back.
        status, continued = post("continue", {})
        assert (status, continued["status"], starts(), len(threads.launched)) == (200, "continuing", 1, 1)
        status, stopped = post("stop", {})
        assert (status, stopped["status"], starts()) == (200, "stopped", 1)


@pytest.mark.parametrize("first", ["continue", "message"])
def test_a_stop_and_a_continue_before_the_worker_starts_run_one_session(runs, monkeypatch, first):
    """A continuation registers its job before its worker thread starts.  In
    that window (held open here by _CHAT_LOCK, which delete_run holds while it
    removes another run's workspace) a Stop recorded the job stopped, and a
    Continue found nothing running and no worker: it cleared the stop flag
    and started a second session beside the stopped one."""
    run_id = "math_harness_unstarted_worker"
    _seed(run_id)
    calls: list[bool] = []
    lock = threading.Lock()

    def engine(**_kwargs):
        stopped = jobs._stop_event(run_id).is_set()
        with lock:
            calls.append(stopped)
        time.sleep(0.2)
        return {**_finished(run_id, len(calls)), "status": "stopped" if stopped else "finished"}

    monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", engine)
    reached, held = threading.Event(), threading.Event()
    real_write = jobs._write_session_problems

    def write_session_problems(ws, problems):
        real_write(ws, problems)
        if threading.current_thread() is request_a and not held.is_set():
            # A message's own chat line is written: the lock is taken now.
            reached.set()
            assert held.wait(30)

    monkeypatch.setattr(jobs, "_write_session_problems", write_session_problems)
    if first == "continue":
        start = lambda: jobs.continue_run(run_id)  # noqa: E731
    else:
        start = lambda: jobs.send_human_message(run_id, "Also handle the case n = 0.")  # noqa: E731
    results: dict = {}
    request_a = threading.Thread(target=lambda: results.update(first=start()), daemon=True)
    stopper = threading.Thread(target=lambda: results.update(stop=jobs.stop_run(run_id)), daemon=True)
    request_b = threading.Thread(target=lambda: results.update(second=jobs.continue_run(run_id)), daemon=True)
    if first == "continue":
        reached.set()
    else:
        request_a.start()
    assert reached.wait(30)
    jobs._CHAT_LOCK.acquire()  # as delete_run holds it, removing another run's workspace
    held.set()
    try:
        if first == "continue":
            request_a.start()
        deadline = time.monotonic() + 30
        while not jobs._run_is_active(run_id) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert jobs._run_is_active(run_id), "the continuation registered its job"
        stopper.start()  # its chat line waits for the lock; the job reads stopped at once
        while jobs._run_is_active(run_id) and time.monotonic() < deadline:
            time.sleep(0.005)
        results["stopping"] = jobs.run_is_stopping(run_id)
        request_b.start()
        # It answers, or (before the fix) registers a second job and waits for the lock.
        while "second" not in results and not jobs._run_is_active(run_id) and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        jobs._CHAT_LOCK.release()
        for thread in (request_a, stopper, request_b):
            if thread.ident is not None:
                thread.join(30)
    _settle(run_id, until=lambda: bool(calls))
    # One session ran, and it saw its stop flag.
    assert calls == [True]
    assert results["second"] == {"ok": False, "status": "stopping", "run_id": run_id,
                                 "error": jobs.RUN_STOPPING_ERROR}
    assert results["stopping"] is True
    assert jobs._load_run_record(run_id)["status"] == "stopped"


def test_a_deleted_runs_identity_keeps_only_the_fields_a_late_write_needs(runs):
    """Kept in memory for the life of the process, for every deleted run (the
    guest-retention purge deletes them in bulk)."""
    run_id = "math_harness_deleted_identity"
    long_problem = "Let G be a finite group of order n and p a prime dividing n. " * 400
    _seed(run_id, owner_id=7, problem_text=long_problem, problems=[{"text": long_problem, "source": "initial"}],
          model="gpt-6-astra", requested_model="gpt-6-astra", auth_route="api_key", credential_source="user",
          error="x" * 5000, trace_name="session")
    assert len(jobs._load_run_record(run_id)["problem_text"]) > 20_000
    jobs.delete_run(run_id)
    identity = jobs._DELETED_RUN_IDENTITY[run_id]
    assert identity["problem_text"] == long_problem[:2000]
    assert {key: identity.get(key) for key in (
        "owner_id", "problem_id", "model", "requested_model", "auth_route", "credential_source")} == {
        "owner_id": 7, "problem_id": "p", "model": "gpt-6-astra", "requested_model": "gpt-6-astra",
        "auth_route": "api_key", "credential_source": "user"}
    assert identity["problem_text_preview"].startswith("Let G be a finite group")
    assert set(identity) <= {*jobs._RECORD_IDENTITY_KEYS, "problem_text_preview", "problem_text"}
    assert sum(len(str(value)) for value in identity.values()) < 3000
