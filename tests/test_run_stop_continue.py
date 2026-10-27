"""Stop, then Continue, while the stopped session's worker winds down.

Production, 2026-10-26: a Continue clicked 0.6 s after Stop (the two buttons
share one spot) was accepted while the stopped runner was still shutting
down, beside it, on the same workspace, record, prompt file and stop flag;
and feedback queued before a Stop waited to start an extra session after the
run's next one.

- Nothing new starts beside a stopped session's worker: Continue is refused
  (409 "still stopping"), a chat message waits and continues the run once
  that worker is done, and the old worker never sees its stop flag cleared.
- A Stop drops the feedback queued for the session it ended, and says so:
  the dropped message is no longer a session problem, and its files stay
  with the run without counting as new.
"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_monitor import auto_pipeline, jobs
from agent_monitor import console_server as console


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """Private run storage and job registries for one test."""
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    for name, value in (
        ("_JOBS", {}), ("_STOP_EVENTS", {}), ("_PROCS", {}), ("_WORKERS", {}),
        ("_DELETED_RUNS", set()), ("_LAUNCHING_RUNS", set()),
    ):
        monkeypatch.setattr(jobs, name, value)
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


class _StoppedFollowUp:
    """A follow-up of ``run_id`` in its own tracked worker thread (as
    _continue_claimed_run starts one) whose engine, once stopped, winds down
    until ``release`` is set, then reads the stop flag one last time."""

    def __init__(self, run_id: str, workspace: Path, monkeypatch) -> None:
        self.run_id, self.job_id = run_id, "job-follow-up"
        self.entered, self.release = threading.Event(), threading.Event()
        self.stop_flag_at_end: bool | None = None
        jobs._JOBS[self.job_id] = {"job_id": self.job_id, "run_id": run_id, "status": "running",
                                   "engine": "math_harness", "kind": "continue"}

        def engine(**_kwargs):
            self.entered.set()
            assert self.release.wait(30)
            self.stop_flag_at_end = jobs._stop_event(run_id).is_set()
            return {"run_id": run_id, "status": "stopped", "agents": []}

        monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", engine)
        self.thread = threading.Thread(target=jobs._execute_continue, daemon=True, kwargs=dict(
            job_id=self.job_id, run_id=run_id, engine="math_harness", message="Add the boundary case.",
            problem_text="Prove it.", model=None, max_iterations=2, workspace=str(workspace), owner_id=7))
        self.thread.start()
        jobs._track_worker(self.job_id, run_id, self.thread)
        assert self.entered.wait(30)

    def end(self) -> None:
        self.release.set()
        self.thread.join(30)
        assert not self.thread.is_alive()


def test_nothing_starts_beside_a_stopped_session_until_its_worker_is_done(runs, monkeypatch):
    run_id = "math_harness_stop_then_continue"
    workspace = _seed(run_id, status="finished")
    real_continue = jobs.continue_run
    launched: list[dict] = []
    # What the ending worker starts (the real continue_run is called directly below).
    monkeypatch.setattr(jobs, "continue_run", lambda rid, **kw: launched.append({"run_id": rid, **kw})
                        or {"ok": True, "status": "continuing", "run_id": rid})
    session = _StoppedFollowUp(run_id, workspace, monkeypatch)

    # Feedback sent while the follow-up runs waits for its end ...
    assert jobs.send_human_message(run_id, "never mind, stop")["status"] == "queued"
    assert jobs.stop_run(run_id)["status"] == "stopped"
    # ... and the Stop drops it, and says so.
    assert not jobs.feedback_pending(run_id)
    chat = _chat(run_id)
    assert "Run stopped by user." in chat
    assert "Queued feedback (1 message) was removed from the session: the run was stopped before it ran. " \
        "Send it again to have it answered." in chat

    # Its record reads stopped at once, but its worker still winds down.
    assert jobs._load_run_record(run_id)["status"] == "stopped"
    assert jobs.run_is_stopping(run_id) and jobs.run_is_active(run_id)
    assert [job.get("stopping") for job in jobs.list_jobs()] == [True]
    # A Continue meanwhile starts nothing and leaves the stop flag set ...
    assert real_continue(run_id) == {"ok": False, "status": "stopping", "run_id": run_id,
                                     "error": jobs.RUN_STOPPING_ERROR}
    assert jobs._stop_event(run_id).is_set() and list(jobs._JOBS) == [session.job_id]
    # ... and a message sent after the Stop waits for the worker.
    assert jobs.send_human_message(run_id, "Instead, prove the case n = 2.")["status"] == "queued"
    assert launched == []

    session.end()
    # The message sent before the Stop is no session problem now; the one
    # sent after it is.
    assert [problem["text"] for problem in jobs._load_run_record(run_id)["problems"]] == [
        "Prove it.", "Add the boundary case.", "Instead, prove the case n = 2."]
    # The old worker never saw its stop flag cleared, and once it was done the
    # message sent after the Stop continued the run (the one sent before it, never).
    assert session.stop_flag_at_end is True
    assert [(item["run_id"], item["message"], item["problems_recorded"]) for item in launched] == [
        (run_id, "Instead, prove the case n = 2.", True)]
    assert not jobs.run_is_stopping(run_id) and not jobs.feedback_pending(run_id)
    assert "stopping" not in jobs.list_jobs()[0]
    # Nothing is left to wait for: a continuation may claim the run now.
    assert jobs._claim_launch(run_id)
    jobs._release_launch(run_id)


# A CGM runner that, once signalled, takes 2 s to wind down (as one ending its
# model call does), then exits as stopped before its publication.
_LINGERING_RUNNER = r"""
import json, signal, sys, time
from pathlib import Path
stopped = []
signal.signal(signal.SIGTERM, lambda *_: stopped.append(True))
Path("held").write_text("")
deadline = time.monotonic() + 60
while not stopped and time.monotonic() < deadline:
    time.sleep(0.02)
time.sleep(2)
print(json.dumps({"type": "item.completed", "item": {"type": "error", "terminal": True,
                  "message": "the Math Harness run was stopped"}}), flush=True)
Path("exited").write_text("")
sys.exit(143)
"""


def test_a_real_runner_winding_down_keeps_the_run_stopping_until_it_has_exited(runs, monkeypatch):
    """The same, end to end with a real runner process: Stop signals it, and
    for the whole of its wind-down Continue is refused, the stop flag stays
    set and a message waits; once the worker is done, the message continues
    the run and the record says stopped."""
    import sys
    import time

    run_id = "math_harness_lingering_runner"
    workspace = _seed(run_id, status="finished")
    monkeypatch.setattr("agent_monitor.engines_registry.build_cli_command",
                        lambda *_a, **_kw: [sys.executable, "-c", _LINGERING_RUNNER])
    monkeypatch.setattr("agent_monitor.agent_config.persona_preamble", lambda **_kw: "")
    real_continue = jobs.continue_run
    launched: list[str] = []
    monkeypatch.setattr(jobs, "continue_run", lambda rid, **kw: launched.append(kw["message"])
                        or {"ok": True, "status": "continuing", "run_id": rid})
    jobs._JOBS["job-cgm"] = {"job_id": "job-cgm", "run_id": run_id, "status": "running",
                             "engine": "math_harness", "kind": "continue"}
    worker = threading.Thread(target=jobs._execute_continue, daemon=True, kwargs=dict(
        job_id="job-cgm", run_id=run_id, engine="math_harness", message="Add the boundary case.",
        problem_text="Prove it.", model=None, max_iterations=2, workspace=str(workspace), owner_id=7))
    worker.start()
    jobs._track_worker("job-cgm", run_id, worker)
    deadline = time.monotonic() + 30
    while not (workspace / "held").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (workspace / "held").exists(), "the runner never started"

    assert jobs.stop_run(run_id)["status"] == "stopped"
    stopped_at = time.monotonic()
    assert jobs.send_human_message(run_id, "Instead, prove the case n = 2.")["status"] == "queued"
    seen = []
    while time.monotonic() - stopped_at < 1.4:  # well inside the runner's 2 s wind-down
        assert real_continue(run_id)["status"] == "stopping"
        seen.append((jobs.run_is_stopping(run_id), jobs._stop_event(run_id).is_set(), launched == [],
                     not (workspace / "exited").exists()))
        time.sleep(0.2)
    worker.join(30)
    assert not worker.is_alive()
    assert len(seen) >= 5 and all(all(sample) for sample in seen), seen
    assert launched == ["Instead, prove the case n = 2."]
    assert not jobs.run_is_stopping(run_id)
    assert jobs._load_run_record(run_id)["status"] == "stopped"


def test_a_stop_with_nothing_queued_says_nothing_more_and_starts_nothing(runs, monkeypatch):
    run_id = "math_harness_plain_stop"
    workspace = _seed(run_id, status="finished")
    launched: list[dict] = []
    monkeypatch.setattr(jobs, "continue_run", lambda rid, **kw: launched.append(kw) or {"ok": True})
    session = _StoppedFollowUp(run_id, workspace, monkeypatch)
    assert jobs.stop_run(run_id)["status"] == "stopped"
    session.end()
    assert launched == []
    assert not any(line.startswith("Queued feedback") for line in _chat(run_id))
    assert _chat(run_id).count("Run stopped by user.") == 1


class _Turns:
    """Real follow-ups of ``run_id`` (send_human_message, continue_run, the
    queue), each recording the task its engine got; a turn blocks while
    ``hold`` is set, so messages can be queued behind it."""

    def __init__(self, run_id: str, monkeypatch) -> None:
        import time

        self.run_id, self.tasks, self.sources, self.time = run_id, [], [], time
        self.hold, self.entered = threading.Event(), threading.Event()

        def engine(**kwargs):
            from agent_monitor import cgm_sources

            name = (kwargs.get("extra_env") or {}).get(cgm_sources.SOURCES_ENV)
            self.sources.append((Path(kwargs["workspace"]) / name).read_text(encoding="utf-8") if name else "")
            self.tasks.append(kwargs["problem_text"])
            self.entered.set()
            while self.hold.is_set():
                time.sleep(0.01)
            return {"run_id": run_id, "engine": "math_harness", "status": "finished",
                    "agents": [{"trace_id": f"{run_id}::t{len(self.tasks)}", "output": "done"}]}

        monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", engine)
        monkeypatch.setattr(jobs, "_continuation_auth_environment", lambda **kw: (dict(kw["env"]), "api_key"))
        monkeypatch.setattr(auto_pipeline, "start", lambda **_kw: {})

    def wait(self, count: int) -> str:
        deadline = self.time.monotonic() + 30
        while (len(self.tasks) < count or jobs.run_is_active(self.run_id)) and self.time.monotonic() < deadline:
            if self.hold.is_set() and len(self.tasks) >= count:
                break
            self.time.sleep(0.01)
        assert len(self.tasks) >= count, self.tasks
        return self.tasks[count - 1]


def _tagged(task: str) -> list[str]:
    block = task.split("SESSION PROBLEMS:\n", 1)[1].split("\n\n", 1)[0]
    return [line for line in block.splitlines() if line.endswith("  ← new this turn")]


def test_only_what_this_turn_adds_is_tagged_new(runs, monkeypatch):
    """"← new this turn" points a CGM turn at what it must address.  It was
    the last session problem whatever the turn: a bare ▶ Continue (also
    right after a Stop) presented the message the previous turn had already
    answered as new.  Now only what this turn's message added is: a bare
    Continue or a message the session already had tags nothing."""
    run_id = "math_harness_new_this_turn"
    # The local operator's run: its queued feedback continues it as is.
    workspace = _seed(run_id, status="finished", owner_id=None)
    (workspace / "proof.md").write_text("# Claim\n\nx is even, including x = 0.\n", encoding="utf-8")
    turns = _Turns(run_id, monkeypatch)

    assert jobs.send_human_message(run_id, "Also cover x = 0.")["status"] == "continuing"
    assert _tagged(turns.wait(1)) == ["2. Also cover x = 0.  ← new this turn"]
    assert jobs.continue_run(run_id)["ok"]
    assert _tagged(turns.wait(2)) == []
    assert jobs.send_human_message(run_id, "Also cover x = 0.")["status"] == "continuing"
    assert _tagged(turns.wait(3)) == []

    # Queued behind a running turn: a new message, and one the session had.
    turns.hold.set()
    assert jobs.send_human_message(run_id, "Then the case x < 0.")["status"] == "continuing"
    turns.wait(4)
    assert jobs.send_human_message(run_id, "Then the general case.")["status"] == "queued"
    assert jobs.send_human_message(run_id, "Also cover x = 0.")["status"] == "queued"
    turns.hold.clear()
    queued = turns.wait(5)
    assert "HUMAN FEEDBACK — address this now:\nThen the general case.\n\nAlso cover x = 0.\n" in queued
    assert _tagged(queued) == ["4. Then the general case.  ← new this turn"]


def test_a_message_a_stop_drops_leaves_the_session(runs, monkeypatch):
    """A Stop drops the feedback queued for the session it ends, and the chat
    said "Send it again" -- but the message stayed a session problem, so the
    next turn (a bare Continue too) answered it as new, its file came first
    as new, and it kept counting toward a member's feedback cap.  Now it
    leaves the session; its file stays with the run, not as new; a queued
    copy of an earlier message takes nothing away."""
    import base64

    run_id = "math_harness_dropped_feedback"
    workspace = _seed(run_id, status="finished", owner_id=None)
    (workspace / "proof.md").write_text("# Claim\n\nThe first session's proof.\n", encoding="utf-8")
    from agent_monitor import attachments

    attachments.save(workspace, attachments.validate([
        {"name": "notes.md", "data": base64.b64encode(b"Notes: the boundary case is k = 0.").decode()}]))
    turns = _Turns(run_id, monkeypatch)
    turns.hold.set()
    assert jobs.send_human_message(run_id, "Add the boundary case.")["status"] == "continuing"
    turns.wait(1)
    dropped = "Now drop hypothesis H and prove the general statement."
    retracted = {"name": "retracted.md", "data": base64.b64encode(b"Assume H fails; then ...").decode()}
    assert jobs.send_human_message(run_id, dropped, attachments=[retracted])["status"] == "queued"
    assert jobs.send_human_message(run_id, "Prove it.")["status"] == "queued"  # the session has it
    assert jobs.stop_run(run_id)["status"] == "stopped"
    turns.hold.clear()
    deadline = turns.time.monotonic() + 30
    while jobs.run_is_active(run_id) and turns.time.monotonic() < deadline:
        turns.time.sleep(0.01)

    assert "Queued feedback (2 messages) was removed from the session: the run was stopped before they " \
        "ran. Send them again to have them answered. The files attached to them stay with the run." in _chat(run_id)
    record = jobs._load_run_record(run_id)
    texts = ["Prove it.", "Add the boundary case."]
    assert [problem["text"] for problem in record["problems"]] == texts
    assert json.loads((workspace / "problems.json").read_text())[-1]["text"] == "Add the boundary case."
    assert "drop hypothesis H" not in (workspace / "problem.txt").read_text()
    assert jobs._human_feedback_chars(record) == len("Add the boundary case.")
    # The next turn, a bare Continue, neither answers it nor puts its file first.
    assert jobs.continue_run(run_id)["ok"]
    task = turns.wait(2)
    assert "drop hypothesis H" not in task and "← new this turn" not in task
    sections = [line.split(" (", 1)[0] for line in turns.sources[-1].splitlines() if line.startswith("=== File ")]
    assert sections == ['=== File 1 of 2: "notes.md"', '=== File 2 of 2: "retracted.md"']
    assert [item["name"] for item in attachments.list_files(workspace)] == ["notes.md", "retracted.md"]


def test_a_stop_takes_back_only_what_a_dropped_message_added(runs):
    """A dropped message that was already a session problem (sent twice)
    added nothing: the earlier problem stays, and so does any other one."""
    problems = [
        {"text": "Prove it.", "source": "initial"},
        {"text": "Use induction.", "source": "human"},
        {"text": "Use induction.", "source": "human"},
        {"text": "Then the general case.", "source": "human"},
    ]
    dropped = [
        {"content": "Use induction.", "problem_added": True},
        {"content": "Then the general case.", "problem_added": False},
        {"content": "Prove it.", "problem_added": True},  # never an initial problem
    ]
    assert jobs._without_dropped(problems, dropped) == problems[:2] + problems[3:]
    assert jobs._without_dropped(problems, [{"content": "Use induction.", "problem_added": None}]) is None


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_a_queued_message_with_a_unicode_line_separator_stays_one_message(runs, monkeypatch, separator):
    """A queued message is one JSON line, and its own U+2028, U+2029 or
    U+0085 stays raw in it (ensure_ascii=False).  Read with splitlines(),
    the line split into two "messages" of raw JSON, without the author they
    continue as (the run's owner took over) or what a Stop takes back: a
    Stop said two messages were removed, and the one sent stayed a session
    problem."""
    message = f"Use the bound{separator}from Lemma 2."
    workspace = _seed("math_harness_line_separator", status="finished", owner_id=None)
    assert jobs._queue_human_feedback(workspace, message, author_id=7, problem_added=True,
                                      attachment_ids=["a" * 32]) is False
    # Its author's next message joins the follow-up it booked.
    assert jobs._queue_human_feedback(workspace, "Next.", author_id=7) is True
    kept = ("content", "author_id", "problem_added", "attachment_ids")
    assert [{key: entry[key] for key in kept} for entry in jobs._drain_feedback_entries(workspace)] == [
        {"content": message, "author_id": 7, "problem_added": True, "attachment_ids": ["a" * 32]},
        {"content": "Next.", "author_id": 7, "problem_added": None, "attachment_ids": []},
    ]

    # The real flow: queued behind a running turn, then dropped by a Stop.
    run_id = "math_harness_line_separator"
    turns = _Turns(run_id, monkeypatch)
    turns.hold.set()
    assert jobs.send_human_message(run_id, "Add the boundary case.")["status"] == "continuing"
    turns.wait(1)
    assert jobs.send_human_message(run_id, message)["status"] == "queued"
    assert jobs.stop_run(run_id)["status"] == "stopped"
    turns.hold.clear()
    deadline = turns.time.monotonic() + 30
    while jobs.run_is_active(run_id) and turns.time.monotonic() < deadline:
        turns.time.sleep(0.01)
    assert "Queued feedback (1 message) was removed from the session: the run was stopped before it ran. " \
        "Send it again to have it answered." in _chat(run_id)
    assert [problem["text"] for problem in jobs._load_run_record(run_id)["problems"]] == [
        "Prove it.", "Add the boundary case."]


def test_an_ending_worker_starts_the_feedback_queued_for_it(runs, monkeypatch):
    """The ending worker's own thread is never something to wait for: a run
    that finished (not stopped) still continues with its queued feedback."""
    run_id = "math_harness_finished_with_feedback"
    workspace = _seed(run_id, status="finished")
    launched: list[str] = []
    monkeypatch.setattr(jobs, "continue_run", lambda rid, **kw: launched.append(kw["message"]) or {"ok": True})

    def engine(**_kwargs):
        assert jobs.send_human_message(run_id, "Then the general case.")["status"] == "queued"
        return {"run_id": run_id, "status": "finished", "agents": [{"trace_id": f"{run_id}::t", "output": "done"}]}

    monkeypatch.setattr(jobs, "_run_cli_engine_with_openclaude_retry", engine)
    monkeypatch.setattr(auto_pipeline, "start", lambda **_kw: {})
    jobs._JOBS["job-1"] = {"job_id": "job-1", "run_id": run_id, "status": "running", "engine": "math_harness"}
    worker = threading.Thread(target=jobs._execute_continue, daemon=True, kwargs=dict(
        job_id="job-1", run_id=run_id, engine="math_harness", message="Add the boundary case.",
        problem_text="Prove it.", model=None, max_iterations=2, workspace=str(workspace), owner_id=7))
    worker.start()
    jobs._track_worker("job-1", run_id, worker)
    worker.join(30)
    assert launched == ["Then the general case."]


def test_thread_doubles_are_never_waited_for(runs):
    class NoThread:  # embedders' and tests' stand-ins run nothing
        def is_alive(self):
            return True

    jobs._track_worker("job-x", "run-x", NoThread())
    assert jobs._WORKERS == {} and not jobs.run_is_stopping("run-x")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(console, "BASE_PATH", "/test")
    monkeypatch.setattr(console.Handler, "_current_user", lambda self: {"id": 7, "name": "Researcher"})
    monkeypatch.setattr(jobs, "run_owner", lambda rid: 7)
    monkeypatch.setattr(console, "_run_record_for", lambda rid: {"run_id": rid, "engine": "math_harness"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path: str) -> tuple[int, dict]:
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        conn.request("POST", "/test" + path, body="{}", headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        return response.status, payload

    yield post
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_continue_is_refused_with_a_clear_409_while_the_run_is_stopping(api, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(jobs, "run_is_stopping", lambda rid: True)
    monkeypatch.setattr(jobs, "continue_run", lambda rid, **_kw: started.append(rid) or {"ok": True})
    status, payload = api("/api/runs/math_harness_r1/continue")
    assert (status, payload) == (409, {"ok": False, "status": "stopping", "run_id": "math_harness_r1",
                                       "error": "This run is still stopping; try again in a few seconds."})
    assert started == []
    # A Stop that lands between that check and the launch claim answers the same.
    monkeypatch.setattr(jobs, "run_is_stopping", lambda rid: False)
    monkeypatch.setattr(jobs, "run_is_active", lambda rid: False)
    monkeypatch.setattr(jobs, "continue_run", lambda rid, **_kw: {
        "ok": False, "status": "stopping", "run_id": rid, "error": jobs.RUN_STOPPING_ERROR})
    status, payload = api("/api/runs/math_harness_r1/continue")
    assert status == 409 and payload["error"] == jobs.RUN_STOPPING_ERROR
