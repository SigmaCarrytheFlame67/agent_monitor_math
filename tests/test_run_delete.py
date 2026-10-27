"""Deleting a run that is still running.

A run deleted during its session would come back: its worker's last writes
recreated the record, the manifest entry and the workspace.  A run deleted
while it runs now stays deleted (no record, manifest entry, chat or workspace
comes back), and the usage it still reports is booked under its owner.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from agent_monitor import jobs, usage_ledger


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """Private run storage and job registries for one test."""
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    for name, value in (
        ("_JOBS", {}), ("_STOP_EVENTS", {}), ("_PROCS", {}), ("_WORKERS", {}),
        ("_DELETED_RUNS", set()), ("_DELETED_RUN_IDENTITY", {}), ("_DELETED_RUN_LAST", {}),
        ("_LAUNCHING_RUNS", set()),
    ):
        monkeypatch.setattr(jobs, name, value)
    monkeypatch.delenv("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", raising=False)
    return tmp_path


def _seed(run_id: str, *, status: str, **fields) -> Path:
    workspace = jobs.RUNS_DIR / "workspaces" / run_id
    workspace.mkdir(parents=True)
    (workspace / "problem.txt").write_text("Prove it.", encoding="utf-8")
    jobs._write_run({
        "run_id": run_id, "engine": "math_harness", "problem_id": "p", "status": status, "owner_id": 7,
        "problem_text": "Prove it.", "problems": [{"text": "Prove it.", "source": "initial"}],
        "agents": [{"trace_id": f"{run_id}::first", "output": "first session", "status": "finished"}],
        "edges": [], **fields,
    })
    return workspace


def _chat(run_id: str) -> list[str]:
    return [message["content"] for message in jobs.list_chat(run_id)]


def _manifest_ids() -> set[str]:
    path = jobs.CACHE_DIR / "harness" / "manifest.json"
    if not path.exists():
        return set()
    return {entry.get("run_id") for entry in json.loads(path.read_text(encoding="utf-8")).get("runs") or []}


def test_a_run_deleted_while_it_runs_stays_deleted(runs, monkeypatch):
    run_id = "math_harness_deleted_live"
    workspace = _seed(run_id, status="running", credential_source="user", auth_route="api_key", model="gpt-test")
    jobs._JOBS["job-live"] = {"job_id": "job-live", "run_id": run_id, "status": "running", "engine": "math_harness"}
    assert run_id in _manifest_ids()

    def engine(**_kwargs):
        jobs.delete_run(run_id)
        # The runner's last heartbeat after the kill, as _run_cli_engine's _flush writes it.
        flushed = {"run_id": run_id, "engine": "math_harness", "status": "stopped",
                   "agents": [{"trace_id": f"{run_id}::turn-1", "output": "partial", "status": "stopped"}],
                   "totals": {"input_tokens": 1200, "output_tokens": 300, "cost_usd": 0.02}}
        jobs._write_run(dict(flushed))
        return flushed

    monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", engine)
    launched: list = []
    monkeypatch.setattr(jobs, "continue_run", lambda *a, **kw: launched.append(kw))
    jobs._execute_job(job_id="job-live", run_id=run_id, engine="math_harness", problem_id="p",
                      problem_text="Prove it.", problem_path=None, model=None, max_iterations=1,
                      workspace=str(workspace), owner_id=7, guest=False)

    # Neither record copy, nor its manifest entry, nor its chat or workspace came back.
    with pytest.raises(FileNotFoundError):
        jobs._load_run_record(run_id)
    assert not (jobs.RUNS_DIR / f"{run_id}.json").exists()
    assert run_id not in _manifest_ids()
    assert not workspace.exists()
    assert jobs.list_jobs() == [] and launched == []
    # The usage it reported after the delete is booked, under its owner and route.
    with usage_ledger._connect() as conn:
        row = conn.execute("SELECT owner_id, billing, deleted_at FROM runs_index WHERE run_id=?", (run_id,)).fetchone()
        spent = conn.execute("SELECT SUM(input_tokens), SUM(output_tokens), MIN(owner_id) FROM usage_daily "
                             "WHERE run_id=? AND stage='engine'", (run_id,)).fetchone()
    assert (row["owner_id"], row["billing"]) == (7, "api") and row["deleted_at"]
    assert tuple(spent) == (1200, 300, 7)


def _engine_usage(run_id: str) -> tuple:
    with usage_ledger._connect() as conn:
        return tuple(conn.execute(
            "SELECT SUM(input_tokens), SUM(output_tokens), MIN(owner_id), MAX(owner_id), MIN(billing), MAX(billing) "
            "FROM usage_daily WHERE run_id=? AND stage='engine'", (run_id,)).fetchone())


def test_a_follow_up_deleted_while_it_runs_still_books_its_usage(runs, monkeypatch):
    """A follow-up's live records hold only its own turns and usage, merged
    onto the run's earlier sessions as it stops.  Deleted meanwhile, it merges
    them all the same (in memory: the record stays deleted) and books its
    usage, as the resurrected record used to."""
    run_id = "math_harness_deleted_follow_up"
    workspace = _seed(run_id, status="finished", credential_source="user", auth_route="api_key", model="gpt-test",
                      totals={"input_tokens": 5000, "output_tokens": 1000, "cost_usd": 0.5})
    assert _engine_usage(run_id) == (5000, 1000, 7, 7, "api", "api")
    in_engine, deleted = threading.Event(), threading.Event()

    def engine(**_kwargs):
        # This follow-up's live record: its own turn and usage only.
        jobs._write_run({"run_id": run_id, "engine": "math_harness", "status": "running",
                         "agents": [{"trace_id": f"{run_id}::turn-1", "output": "partial", "status": "running"}],
                         "totals": {"input_tokens": 1000, "output_tokens": 200}})
        in_engine.set()
        assert deleted.wait(30)
        # Killed by the delete: the runner's last record, as _run_cli_engine flushes it.
        flushed = {"run_id": run_id, "engine": "math_harness", "status": "stopped",
                   "agents": [{"trace_id": f"{run_id}::turn-1", "output": "partial", "status": "stopped"}],
                   "totals": {"input_tokens": 1200, "output_tokens": 300}}
        jobs._write_run(dict(flushed))
        return flushed

    monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", engine)
    jobs._JOBS["job-f"] = {"job_id": "job-f", "run_id": run_id, "status": "running", "engine": "math_harness",
                           "kind": "continue"}
    worker = threading.Thread(target=jobs._execute_continue, daemon=True, kwargs=dict(
        job_id="job-f", run_id=run_id, engine="math_harness", message="Add the boundary case.",
        problem_text="Prove it.", model=None, max_iterations=2, workspace=str(workspace), owner_id=7))
    worker.start()
    jobs._track_worker("job-f", run_id, worker)
    assert in_engine.wait(30)
    jobs.delete_run(run_id)
    deleted.set()
    worker.join(30)
    assert not worker.is_alive()
    # Nothing came back ...
    with pytest.raises(FileNotFoundError):
        jobs._load_run_record(run_id)
    assert run_id not in _manifest_ids() and not workspace.exists()
    # ... the follow-up's usage is booked on top of the earlier session's, under the run's owner ...
    assert _engine_usage(run_id) == (6200, 1300, 7, 7, "api", "api")
    # ... and the record's stand-in went with the worker.
    assert jobs._DELETED_RUN_LAST == {}


def test_a_second_delete_keeps_the_owner_of_the_usage_booked_after_it(runs):
    run_id = "math_harness_deleted_twice"
    _seed(run_id, status="running", credential_source="user", auth_route="api_key")
    jobs.delete_run(run_id)
    jobs.delete_run(run_id)  # an administrator's second click: the record is gone by now
    # A worker still winding down reports its usage.
    jobs._write_run({"run_id": run_id, "engine": "math_harness", "status": "stopped", "agents": [],
                     "totals": {"input_tokens": 100, "output_tokens": 10}})
    assert _engine_usage(run_id) == (100, 10, 7, 7, "api", "api")


def test_a_follow_up_whose_run_was_deleted_as_it_started_brings_no_workspace_back(runs, monkeypatch):
    """A delete during a follow-up's preparation (after _continue_claimed_run
    cleared the stop flag and saw the workspace): its worker starts no runner,
    whose start (copying the profile's skills into the workspace) would bring
    the workspace back, with no record, for good."""
    run_id = "math_harness_deleted_as_it_started"
    workspace = _seed(run_id, status="finished")
    started = jobs.RUNS_DIR / "runner-started"
    monkeypatch.setattr("agent_monitor.engines_registry.build_cli_command", lambda *_a, **_kw: [
        sys.executable, "-c", f"open({str(started)!r}, 'w').close()"])
    jobs._stop_event(run_id).clear()
    jobs.delete_run(run_id)
    jobs._JOBS["job-p"] = {"job_id": "job-p", "run_id": run_id, "status": "running", "engine": "math_harness"}
    jobs._execute_continue(job_id="job-p", run_id=run_id, engine="math_harness", message="Add the boundary case.",
                           problem_text="Prove it.", model=None, max_iterations=2, workspace=str(workspace), owner_id=7)
    assert not workspace.exists() and not started.exists()
    with pytest.raises(FileNotFoundError):
        jobs._load_run_record(run_id)
    # The profile does have skills to copy (a live run gets them): the guard kept the workspace away.
    live = jobs.workspace_dir("math_harness_alive_with_skills")
    from agent_monitor.agent_config import materialize_enabled_skills

    assert materialize_enabled_skills(live) and (live / "_agent" / "skills").is_dir()


def test_normal_runs_keep_writing_their_record_and_chat(runs):
    run_id = "math_harness_alive"
    workspace = _seed(run_id, status="running")
    jobs._append_chat(run_id, "system", "still here")
    jobs._write_run({"run_id": run_id, "status": "finished", "agents": []})
    assert jobs._load_run_record(run_id)["owner_id"] == 7  # merged from the stored record, as before
    assert _chat(run_id) == ["still here"] and (workspace / "human_chat.jsonl").exists()
