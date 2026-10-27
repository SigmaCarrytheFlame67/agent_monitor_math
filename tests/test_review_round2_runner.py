"""Regressions from the second review of the 2026-10-27 field-test fixes.

* A follow-up's task lists the session problems as "N. text", each one new
  this turn tagged "← new this turn" (jobs._execute_continue).  A worker
  that copied the whole list was read after the copy only when the list's
  last line alone was tagged: with two problems new this turn, or a problem
  queued for the next turn after the new one, the problem's own "Proof."
  line opened the worker's proof, and the verifier judged the problem's
  tail.  The parser now compares a copy without its tags, on any line.
* A failed but billed API call names the id it requested (_requested_id),
  looked up inside the call's failure handler: an error of that lookup other
  than a TypeError or ValueError (or of its import) escaped the handler, so
  the call's usage was never booked and the provider's failure was hidden.

Nothing here reaches a model API or the network.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_monitor import auto_pipeline, jobs, proof_graph
from agent_monitor.runners import math_harness_runner as runner
from tests.test_cgm_problem_copy import NEW_THIS_TURN, PROOF_TO_COMPLETE, WORKER_PROOF, _theorem
from tests.test_engine_cost_model import _CALL_USAGE, _usage_events
from tests.test_math_harness_runner import (
    PROBLEM,
    _api_run,
    _curator,
    _in_directory,
    _ok,
    _receipt,
    _strategist,
)

ALSO = "Also cover the case where one of the two integers is 0."


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """Private run storage and job registries; a follow-up skips the account
    checks and starts no derived views."""
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    for name, value in (
        ("_JOBS", {}), ("_STOP_EVENTS", {}), ("_PROCS", {}), ("_WORKERS", {}),
        ("_DELETED_RUNS", set()), ("_LAUNCHING_RUNS", set()),
    ):
        monkeypatch.setattr(jobs, name, value)
    monkeypatch.setattr(jobs, "_continuation_auth_environment", lambda **kw: (dict(kw["env"]), "api_key"))
    monkeypatch.setattr(auto_pipeline, "start", lambda **_kw: {})
    return tmp_path


def _follow_up(run_id: str, problems: list[str], new: list[str]) -> tuple[str, str]:
    """The task and session notebook (problem.txt) jobs gives a CGM follow-up
    turn of a session with ``problems``, ``new`` among them added this turn
    (queued feedback)."""
    workspace = jobs.RUNS_DIR / "workspaces" / run_id
    workspace.mkdir(parents=True)
    (workspace / "proof.md").write_text("# Claim\n\nThe first session's proof.\n", encoding="utf-8")
    jobs._write_run({
        "run_id": run_id, "engine": "math_harness", "problem_id": "p", "status": "finished", "owner_id": None,
        "problem_text": problems[0],
        "problems": [{"text": text, "source": "human" if index else "initial"} for index, text in enumerate(problems)],
        "agents": [{"trace_id": f"{run_id}::first", "output": "first session", "status": "finished"}],
        "edges": [],
    })
    seen: dict[str, str] = {}

    def engine(**kwargs):
        seen["task"] = kwargs["problem_text"]
        seen["problem.txt"] = (workspace / "problem.txt").read_text(encoding="utf-8")
        return {"run_id": run_id, "engine": "math_harness", "status": "finished",
                "agents": [{"trace_id": f"{run_id}::t1", "output": "done"}]}

    with patch.object(jobs, "_run_cli_engine_with_openclaude_retry", side_effect=engine):
        jobs._execute_continue(
            job_id=f"{run_id}-turn", run_id=run_id, engine="math_harness", message="\n\n".join(new),
            problem_text=problems[0], model=None, max_iterations=1, workspace=str(workspace),
            problems_recorded=True, new_problems=new,
        )
    return seen["task"], seen["problem.txt"]


# ── a follow-up's whole session list, tagged anywhere ────────────────────


@pytest.mark.parametrize("case", ["two new", "a later problem after the new one"])
def test_a_whole_session_list_tagged_on_any_line_is_read_after_the_copy(runs, case):
    problems, new, listed = {
        "two new": (
            [PROBLEM, ALSO, PROOF_TO_COMPLETE], [ALSO, PROOF_TO_COMPLETE],
            f"1. {PROBLEM}\n2. {ALSO}{NEW_THIS_TURN}\n3. {PROOF_TO_COMPLETE}{NEW_THIS_TURN}",
        ),
        "a later problem after the new one": (
            [PROBLEM, PROOF_TO_COMPLETE, ALSO], [PROOF_TO_COMPLETE],
            f"1. {PROBLEM}\n2. {PROOF_TO_COMPLETE}{NEW_THIS_TURN}\n3. {ALSO}",
        ),
    }[case]
    task, notebook = _follow_up("math_harness_round2_list", problems, new)
    block = task.split("SESSION PROBLEMS:\n", 1)[1].split("\n\n", 1)[0]
    assert block == listed

    # The turn's runner on that task and notebook; the worker copied the whole list.
    with tempfile.TemporaryDirectory() as raw:
        turn = Path(raw)
        (turn / "problem.txt").write_text(notebook, encoding="utf-8")
        code, calls, _events = _api_run(
            turn, [_strategist(1), _theorem(block), _ok(), _curator()], task=task,
            env={"AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS": "1"},
        )
        assert code == 0
        assert _receipt(turn)["verdict"] == "accepted"
        proof_md = (turn / "proof.md").read_text(encoding="utf-8")
    verify = calls[2]["user"]
    for shown in (verify, proof_md):
        assert WORKER_PROOF in shown
        assert shown.count("Complete the proof above.") == 1
        assert NEW_THIS_TURN.strip() not in shown


# ── the requested id of a failed call ────────────────────────────────────

_PREFIXED = runner.Route(provider="api", model="openai:gpt-5.2", transport="api_chat")


@pytest.mark.parametrize(
    "error", [RuntimeError("resolver bug"), KeyError("OPENAI_API_KEY"), AttributeError("x")],
    ids=["RuntimeError", "KeyError", "AttributeError"],
)
def test_the_requested_id_never_raises(monkeypatch, error):
    def resolver(env, model):
        raise error

    monkeypatch.setattr(proof_graph, "_resolve_llm_provider", resolver)
    assert runner._requested_id(_PREFIXED, {}) is None


def test_the_requested_id_survives_its_import_failing(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent_monitor.proof_graph", None)
    assert runner._requested_id(_PREFIXED, {}) is None


def test_a_failed_billed_call_stays_the_failure_when_its_id_cannot_be_resolved(monkeypatch):
    """A worker call on "openai:gpt-5.2" fails after the provider billed it,
    and the lookup of the id it requested breaks: the run still books the
    call's usage and ends on the provider's error (before, the lookup's
    error escaped the runner)."""
    resolve = proof_graph._resolve_llm_provider

    def resolver(env, model):
        # Only the failure handler's lookup breaks; the route resolves as before.
        if sys._getframe(1).f_code.co_name == "_requested_id":
            raise RuntimeError("resolver bug")
        return resolve(env, model)

    monkeypatch.setattr(proof_graph, "_resolve_llm_provider", resolver)
    events: list[dict] = []
    error = runner.APIBackendError(
        "OpenAI returned an empty response for gpt-5.2", usage={"input_tokens": 50, "output_tokens": 8192}
    )
    replies: list[object] = [_strategist(1), error]

    def fake_api(system: str, user: str, *, model: str, env=None, **_kwargs):
        value = replies.pop(0)
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(text=value, usage=dict(_CALL_USAGE), model="gpt-5.2", provider="openai")

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
    assert [(event["stage"], event["usage"]["output_tokens"]) for event in _usage_events(events)] == [
        ("strategist", _CALL_USAGE["output_tokens"]), ("worker-1", 8192)]
    errors = [event["item"]["message"] for event in events if event.get("item", {}).get("type") == "error"]
    assert len(errors) == 1
    assert "worker-1 provider call failed: OpenAI returned an empty response for gpt-5.2" in errors[0]
