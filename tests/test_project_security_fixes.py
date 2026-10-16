"""Projects with a member who is not a site administrator run tool-free.

Every run started from a project gets a copy of its shared document, which
any member can edit, so member-written text reaches the agent's prompt.
While such a member belongs to the project, its runs, their follow-ups and
their Formal harness use only the tool-free engines on an API key, without
subagents: a coding agent with a shell, or one signed in to the
administrator's subscription, could be steered into reading the server's
credentials.  Launches and continuations here only record what they would
run; nothing starts a CLI or contacts a provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_monitor import account_policy, auth, cgm_max, jobs, projects
from agent_monitor import console_server as console

from test_console_guest_access import (  # noqa: F401  (fixture import)
    APP_MOUNT,
    guest_console,
    request,
    running_server,
)
from test_member_accounts import _member, _record_continuations, member_console  # noqa: F401

START_JOB = jobs.start_job
ADMIN_EMAIL = "owner@example.test"
PASSWORD = "long-enough-password"
ADMIN_KEY = "sk-ant-admin-000000000000"


def _isolated_jobs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_LAUNCHING_RUNS", set())
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})


def _stored_run(run_id: str, owner: dict, **fields) -> str:
    """A finished run on disk, owned by ``owner``."""
    workspace = jobs.workspace_dir(run_id)
    jobs._write_run({
        "run_id": run_id, "status": "finished", "owner_id": owner["id"], "workspace": str(workspace),
        "problems": [{"text": "Prove it.", "source": "initial"}], "model": "claude-opus-4-8",
        "requested_model": "claude-opus-4-8", "credential_source": "user", **fields,
    })
    return run_id


def _project_with_member(admin: dict, member: dict) -> str:
    project = projects.create(admin, "Joint work", "Prove it.")
    projects.join(projects.invite(project["id"], admin["id"]), member)
    return project["id"]


def test_start_run_decides_at_launch_from_the_projects_current_members():
    admin = auth.register(ADMIN_EMAIL, PASSWORD)
    member = auth.register("member@example.test", PASSWORD)
    assert admin["is_admin"] and not member["is_admin"]
    project = projects.create(admin, "Joint work", "Prove it.")
    decided: list = []

    def launch(tool_free):
        decided.append(tool_free)
        return {"run_id": f"plain_p_{len(decided)}"}

    projects.start_run(project["id"], admin["id"], "", launch)
    projects.join(projects.invite(project["id"], admin["id"]), member)
    projects.start_run(project["id"], admin["id"], "", launch)
    projects.start_run(None, admin["id"], "", launch)
    assert decided == [False, True, False]
    # A project's runs follow its current members.
    assert projects.untrusted_run("plain_p_1") and projects.untrusted_run("plain_p_2")
    projects.remove_member(project["id"], admin["id"], member["id"])
    assert not projects.untrusted_run("plain_p_2") and not projects.untrusted_run("plain_p_3")


def test_start_job_holds_a_collaborator_project_run_to_tool_free_engines(monkeypatch, tmp_path):
    _isolated_jobs(monkeypatch, tmp_path)
    threads = _record_continuations(monkeypatch)
    for name in ("PLAIN_CMD", "KIMI_PROOF_CMD", "MATH_HARNESS_CMD"):
        monkeypatch.delenv(name, raising=False)
    admin = {"id": 1, "is_admin": True, "email": ADMIN_EMAIL}
    # The administrator's account runtime is Claude Code; its Anthropic key is set too.
    monkeypatch.setattr(jobs, "_user_extra_env", lambda *_args: {
        "ANTHROPIC_API_KEY": ADMIN_KEY, "AGENT_MONITOR_ACCOUNT_RUNTIME": "claude",
    })
    refused = (
        {"engine": "claude", "model": "opus"},
        {"engine": "codex"},
        {"engine": "openclaw"},
        {"engine": "plain", "model": "claude-opus-4-8", "auth_route": "claude_subscription"},
        {"engine": "math_harness", "model": cgm_max.MODEL_ID},
    )
    for kwargs in refused:
        with pytest.raises(PermissionError) as refusal:
            jobs.start_job(problem_text="Prove it.", user=admin, tool_free=True, **kwargs)
        assert str(refusal.value) == account_policy.PROJECT_TOOL_FREE_ERROR
    # An operator's command override makes Plain run local code.
    monkeypatch.setenv("PLAIN_CMD", "/bin/true")
    with pytest.raises(PermissionError):
        jobs.start_job(engine="plain", model="claude-opus-4-8", problem_text="Prove it.", user=admin, tool_free=True)
    monkeypatch.delenv("PLAIN_CMD")
    assert threads.launched == []

    job = jobs.start_job(
        engine="plain", model="claude-opus-4-8", problem_text="Prove it.", user=admin,
        tool_free=True, use_subagents=True, subagent_model="sonnet",
    )
    (worker,) = threads.launched
    assert worker["use_subagents"] is False and worker["extra_env"]["AGENT_MONITOR_USE_SUBAGENTS"] == "0"
    assert worker["extra_env"]["AGENT_MONITOR_ACCOUNT_RUNTIME"] == "api"
    record = jobs._load_run_record(job["run_id"])
    assert (record["auth_route"], record["use_subagents"], record["tool_free"]) == ("api_key", False, True)
    # The runner's own record writes keep the restriction.
    jobs._write_run({"run_id": job["run_id"], "status": "finished", "agents": []})
    assert jobs._load_run_record(job["run_id"])["tool_free"] is True


def test_follow_ups_of_a_collaborator_project_run_stay_tool_free(monkeypatch, tmp_path):
    _isolated_jobs(monkeypatch, tmp_path)
    threads = _record_continuations(monkeypatch)
    monkeypatch.setattr(jobs, "_user_extra_env", lambda *_args: {"ANTHROPIC_API_KEY": ADMIN_KEY})
    admin = auth.register(ADMIN_EMAIL, PASSWORD)
    pid = _project_with_member(admin, auth.register("member@example.test", PASSWORD))
    claude_run = _stored_run("claude_p_r1", admin, engine="claude", auth_route="claude_subscription")
    plain_run = _stored_run("plain_p_r2", admin, engine="plain", auth_route="api_key", use_subagents=True)
    for run_id in (claude_run, plain_run):
        projects.attach(pid, admin["id"], run_id, "Find a lemma")

    for follow_up in (
        lambda: jobs.continue_run(claude_run, user=admin),
        lambda: jobs.send_human_message(claude_run, "Keep going.", user=admin),
    ):
        with pytest.raises(PermissionError) as refusal:
            follow_up()
        assert str(refusal.value) == account_policy.PROJECT_TOOL_FREE_ERROR
    # The refused message was not recorded as sent.
    assert jobs.list_chat(claude_run) == []
    # Feedback queued earlier waits instead of starting Claude Code.
    jobs._queue_human_feedback(jobs.RUNS_DIR / "workspaces" / claude_run, "Keep going.", author_id=admin["id"])
    jobs._launch_pending_feedback(claude_run, owner_id=admin["id"], model=None, max_iterations=4)
    assert threads.launched == [] and jobs.feedback_pending(claude_run)

    # A run on a tool-free engine continues, without subagents.
    assert jobs.continue_run(plain_run, user=admin)["status"] == "continuing"
    (follow_up,) = threads.launched
    assert follow_up["engine"] == "plain" and follow_up["extra_env"]["AGENT_MONITOR_USE_SUBAGENTS"] == "0"


def test_console_starts_collaborator_project_runs_only_on_tool_free_engines(member_console, monkeypatch, tmp_path):
    _isolated_jobs(monkeypatch, tmp_path)
    # The real start_job (member_console stubs it); its launch only records.
    monkeypatch.setattr(console.job_manager, "start_job", START_JOB)
    launched: list = []
    monkeypatch.setattr(jobs, "_start_job_launch", lambda **kwargs: launched.append(kwargs) or {
        "run_id": kwargs["run_id"], "job_id": kwargs["job_id"]})
    member_console["env"].write_text(f"ANTHROPIC_API_KEY={ADMIN_KEY}\n", encoding="utf-8")
    _admin, token = auth.login_and_create_session(ADMIN_EMAIL, PASSWORD)
    admin_cookie = {"Cookie": f"{auth.SESSION_COOKIE}={token}"}
    with running_server() as port:
        def post(path: str, body: dict, headers: dict):
            return request(port, "POST", f"{APP_MOUNT}{path}", body=body, headers=headers)

        def project() -> tuple[str, str]:
            pid = json.loads(post("/api/projects", {"title": "Joint work", "problem": "Prove it."}, admin_cookie)[2])["id"]
            invite = json.loads(post(f"/api/projects/{pid}/invite", {}, admin_cookie)[2])["path"].split("/")[-1]
            return pid, invite

        pid, invite = project()
        _first, first_cookie = _member(port, "first@example.test")
        assert post("/api/projects/join", {"token": invite}, {"Cookie": first_cookie})[0] == 200
        claude = post("/api/runs", {"project_id": pid, "engine": "claude", "model": "opus"}, admin_cookie)
        plain = post("/api/runs", {"project_id": pid, "project_task": "Find a lemma", "engine": "plain",
                                   "model": "claude-opus-4-8", "use_subagents": True}, admin_cookie)

        # A member who joins after the request was checked still counts at launch.
        late_pid, late_invite = project()
        late_user, _late_cookie = _member(port, "late@example.test")
        real_prepare = console.research_projects.prepare_run

        def prepare_then_join(body, user):
            real_prepare(body, user)
            projects.join(late_invite, late_user)

        monkeypatch.setattr(console.research_projects, "prepare_run", prepare_then_join)
        raced = post("/api/runs", {"project_id": late_pid, "engine": "claude", "model": "opus"}, admin_cookie)

    for refused in (claude, raced):
        assert refused[0] == 403, refused[2]
        assert json.loads(refused[2])["error"] == account_policy.PROJECT_TOOL_FREE_ERROR
    assert plain[0] == 200, plain[2]
    (run,) = launched
    assert (run["engine"], run["route"], run["use_subagents"], run["tool_free"]) == ("plain", "api_key", False, True)
    assert "Prove it." in run["text"]


def test_console_keeps_follow_ups_and_lean_agents_of_collaborator_project_runs_tool_free(
    member_console, monkeypatch, tmp_path
):
    from agent_monitor import lean_verify

    _isolated_jobs(monkeypatch, tmp_path)
    threads = _record_continuations(monkeypatch)
    harness: list = []
    monkeypatch.setattr(lean_verify, "start_harness", lambda **kwargs: harness.append(kwargs) or {"status": "running"})
    verified: list = []
    monkeypatch.setattr(lean_verify, "generate", lambda **kwargs: verified.append(kwargs) or {"status": "verified"})
    monkeypatch.setattr(lean_verify, "compile_or_check", lambda **_kwargs: {"status": "failed"})
    monkeypatch.setattr("agent_monitor.auto_pipeline.refresh_formal", lambda **_kwargs: None)
    monkeypatch.setattr(console, "_workspace_for_run", lambda run_id: jobs.RUNS_DIR / "workspaces" / run_id)

    def stored_record(run_id):
        try:
            return jobs._load_run_record(run_id)
        except FileNotFoundError:
            return None

    monkeypatch.setattr(console, "_run_record_for", stored_record)
    admin, token = auth.login_and_create_session(ADMIN_EMAIL, PASSWORD)
    cookie = {"Cookie": f"{auth.SESSION_COOKIE}={token}"}
    with running_server() as port:
        member, _member_cookie = _member(port)
        pid = _project_with_member(admin, member)
        claude_run = _stored_run("claude_p_r1", admin, engine="claude", auth_route="claude_subscription")
        plain_run = _stored_run("plain_p_r2", admin, engine="plain", auth_route="api_key")
        for run_id in (claude_run, plain_run):
            projects.attach(pid, admin["id"], run_id, "Find a lemma")
        # Started tool-free in a project its collaborator has since left.
        flagged_run = _stored_run("plain_p_r3", admin, engine="plain", auth_route="api_key", tool_free=True)

        def post(path: str, body: dict):
            return request(port, "POST", f"{APP_MOUNT}{path}", body=body, headers=cookie)

        refused = [
            post(f"/api/runs/{claude_run}/continue", {}),
            post(f"/api/runs/{claude_run}/chat", {"message": "Keep going."}),
            post(f"/api/run/{claude_run}/lean_verify", {"action": "verify"}),
            post(f"/api/run/{plain_run}/lean_verify", {"action": "harness", "engine": "codex", "model": "gpt-5.5"}),
            post(f"/api/run/{flagged_run}/lean_verify", {"action": "harness", "engine": "codex", "model": "gpt-5.5"}),
        ]
        verify = post(f"/api/run/{plain_run}/lean_verify", {"action": "verify"})
        check = post(f"/api/run/{claude_run}/lean_verify", {"action": "check"})

    for status, _headers, raw in refused:
        assert status == 403, raw
        assert json.loads(raw)["error"] == account_policy.PROJECT_TOOL_FREE_ERROR
    assert threads.launched == [] and harness == []
    # Model work on the API route of a tool-free run, and the model-free checker, still run.
    assert verify[0] == 200 and len(verified) == 1
    assert check[0] == 200
