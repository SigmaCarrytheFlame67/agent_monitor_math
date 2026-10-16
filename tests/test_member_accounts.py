"""Self-registered (member) accounts: open sign-up without host-level power.

Registration may be open to anyone, so a non-admin account runs only the
tool-free engines, never a local harness, subscription or custom endpoint,
and account creation and run starts are rate limited.  The Ansatz CGM harness
runs only on the member's own API keys.  Job creation is stubbed; nothing here
starts a harness or contacts a provider.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from agent_monitor import account_policy, auth
from agent_monitor import console_server as console
from agent_monitor import jobs

from test_console_guest_access import (  # noqa: F401  (fixture import)
    APP_MOUNT,
    _cookie_from,
    guest_console,
    request,
    running_server,
)


@pytest.fixture()
def member_console(guest_console, monkeypatch: pytest.MonkeyPatch):
    for name in (
        "AGENT_MONITOR_MEMBER_ENGINES",
        "AGENT_MONITOR_MEMBER_CONCURRENCY",
        "AGENT_MONITOR_MEMBER_RUNS_PER_HOUR",
        "AGENT_MONITOR_MEMBER_MESSAGES_PER_HOUR",
        "AGENT_MONITOR_SIGNUPS_PER_CLIENT_PER_HOUR",
        "AGENT_MONITOR_SIGNUPS_PER_HOUR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    console._SIGNUP_CLIENT_WINDOWS.clear()
    console._SIGNUP_GLOBAL_WINDOW.clear()
    console._MEMBER_RUN_WINDOWS.clear()
    console._MEMBER_MESSAGE_WINDOWS.clear()
    started: list[dict] = []
    monkeypatch.setattr(
        console.job_manager,
        "start_job",
        lambda **kwargs: started.append(kwargs) or {"run_id": f"kimi_{len(started)}"},
    )
    monkeypatch.setattr(console.job_manager, "list_jobs", lambda **_kwargs: [])
    # The operator already exists, so every sign-up is a member.
    auth.register("owner@example.test", "long-enough-password")
    yield {**guest_console, "started": started}
    console._SIGNUP_CLIENT_WINDOWS.clear()
    console._SIGNUP_GLOBAL_WINDOW.clear()
    console._MEMBER_RUN_WINDOWS.clear()
    console._MEMBER_MESSAGE_WINDOWS.clear()


def _sign_up(port: int, email: str, client: str = "198.51.100.7"):
    return request(
        port,
        "POST",
        f"{APP_MOUNT}/api/auth/register",
        body={"email": email, "password": "long-enough-password"},
        headers={"X-Forwarded-For": client},
    )


def _member(port: int, email: str = "member@example.test") -> tuple[dict, str]:
    status, headers, raw = _sign_up(port, email)
    assert status == 200, raw
    cookie, _token = _cookie_from(headers)
    return json.loads(raw)["user"], cookie


def test_open_sign_up_creates_a_non_admin_member(member_console):
    with running_server() as port:
        user, cookie = _member(port)
        _status, _headers, raw = request(
            port, "GET", f"{APP_MOUNT}/api/auth/me", headers={"Cookie": cookie}
        )

    me = json.loads(raw)
    assert me["registration_open"] is True
    assert me["user"]["email"] == "member@example.test"
    assert user["is_admin"] is False and not user.get("guest")
    assert account_policy.is_member(user)


def test_member_engine_list_marks_local_harnesses_admin_only(member_console):
    with running_server() as port:
        _user, cookie = _member(port)
        _status, _headers, raw = request(
            port, "GET", f"{APP_MOUNT}/api/engines", headers={"Cookie": cookie}
        )
        _admin, token = auth.login_and_create_session("owner@example.test", "long-enough-password")
        _status, _headers, admin_raw = request(
            port, "GET", f"{APP_MOUNT}/api/engines",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={token}"},
        )

    engines = {engine["id"]: engine for engine in json.loads(raw)["engines"]}
    for engine_id in ("codex", "openclaw", "openhands", "deepagents", "claude"):
        assert engines[engine_id]["available"] is False
        assert engines[engine_id]["requires_admin"] is True
    # Member engines bill an API key: their own, or the included Kimi K3.
    for engine_id in ("kimi", "plain", "math_harness"):
        assert not engines[engine_id].get("requires_admin")
        assert engines[engine_id]["auth_modes"] == ["api_key"]
        assert engines[engine_id]["subscription_supported"] is False
        assert engines[engine_id]["claude_subscription_supported"] is False
    assert engines["math_harness"]["own_key_only"] is True
    assert "Kimi K3 runs on the included" in engines["math_harness"]["model_note"]
    assert not engines["plain"].get("own_key_only")
    # The administrator's catalog is unchanged.
    admin = {engine["id"]: engine for engine in json.loads(admin_raw)["engines"]}
    assert admin["math_harness"]["auth_modes"] == ["codex_subscription", "claude_subscription", "api_key"]
    assert "own_key_only" not in admin["math_harness"]
    assert not admin["codex"].get("requires_admin")


def test_operators_can_narrow_the_member_engines(monkeypatch):
    for name in ("AGENT_MONITOR_MEMBER_ENGINES", "AGENT_MONITOR_PUBLIC", "PLAIN_CMD", "KIMI_PROOF_CMD",
                 "MATH_HARNESS_CMD", "ANALYZE_CMD", "AGENT_MONITOR_ANALYZE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    # Ansatz Analyze joins the tool-free member engines; it follows its own
    # switch, not the proof-engine list below.
    assert account_policy.member_engine_allowlist() == {"kimi", "plain", "math_harness", "analyze"}
    # The documented way back to the two sponsored-or-own-key engines.
    monkeypatch.setenv("AGENT_MONITOR_MEMBER_ENGINES", "kimi,plain")
    assert account_policy.member_engine_allowlist() == {"kimi", "plain", "analyze"}
    # The ceiling is fixed: naming a local harness cannot widen it.
    monkeypatch.setenv("AGENT_MONITOR_MEMBER_ENGINES", "kimi,codex,math_harness")
    assert account_policy.member_engine_allowlist() == {"kimi", "math_harness", "analyze"}
    monkeypatch.delenv("AGENT_MONITOR_MEMBER_ENGINES")
    # A command override makes the harness operator-chosen local code.
    monkeypatch.setenv("MATH_HARNESS_CMD", "/bin/true")
    assert account_policy.member_engine_allowlist() == {"kimi", "plain", "analyze"}
    monkeypatch.delenv("MATH_HARNESS_CMD")
    monkeypatch.setenv("ANALYZE_CMD", "/bin/true")
    assert account_policy.member_engine_allowlist() == {"kimi", "plain", "math_harness"}
    monkeypatch.delenv("ANALYZE_CMD")
    # The Analyze kill switch removes it for members too.
    monkeypatch.setenv("AGENT_MONITOR_ANALYZE_ENABLED", "0")
    assert account_policy.member_engine_allowlist() == {"kimi", "plain", "math_harness"}
    assert account_policy.member_engine_error({"id": 9}, "analyze") == "Analyze is turned off on this site"
    monkeypatch.delenv("AGENT_MONITOR_ANALYZE_ENABLED")
    # Public mode gives every account the server's keys, so "own key" fails.
    monkeypatch.setenv("AGENT_MONITOR_PUBLIC", "1")
    assert account_policy.member_engine_allowlist() == {"kimi", "plain", "analyze"}


def test_member_refusals_say_why_each_engine_is_unavailable(monkeypatch):
    for name in ("AGENT_MONITOR_MEMBER_ENGINES", "AGENT_MONITOR_PUBLIC", "PLAIN_CMD", "KIMI_PROOF_CMD", "MATH_HARNESS_CMD"):
        monkeypatch.delenv(name, raising=False)
    member = {"id": 5, "is_admin": False}

    def refusal(engine: str) -> tuple[str | None, str]:
        view = account_policy.member_engine_view({"id": engine, "available": True})
        return account_policy.member_engine_error(member, engine), view.get("hint", "")

    assert refusal("codex") == (
        "That engine runs local tools and is limited to the site administrator",
        "Administrator only on this site: this harness runs local tools",
    )
    assert refusal("math_harness") == (None, "")
    # The response-only CGM harness runs no local tools: narrowing, public
    # mode or a command override each give their own reason.
    monkeypatch.setenv("AGENT_MONITOR_MEMBER_ENGINES", "kimi,plain")
    assert refusal("math_harness") == (
        "The site administrator has not enabled that engine for member accounts",
        "Administrator only on this site: not enabled for member accounts",
    )
    monkeypatch.delenv("AGENT_MONITOR_MEMBER_ENGINES")
    monkeypatch.setenv("AGENT_MONITOR_PUBLIC", "1")
    assert "has not enabled that engine" in refusal("math_harness")[0]
    monkeypatch.delenv("AGENT_MONITOR_PUBLIC")
    monkeypatch.setenv("MATH_HARNESS_CMD", "/bin/true")
    error, hint = refusal("math_harness")
    assert "operator-chosen local code" in error and "site administrator" in error
    assert hint == "Administrator only on this site: this engine runs operator-chosen local code"


def test_subscription_notes_name_only_engines_members_may_use(member_console, monkeypatch):
    monkeypatch.delenv("AGENT_MONITOR_PUBLIC", raising=False)
    monkeypatch.delenv("MATH_HARNESS_CMD", raising=False)
    assert account_policy.subscription_notes() == {
        "claude": account_policy.CLAUDE_SUBSCRIPTION_NOTE,
        "codex": account_policy.CODEX_SUBSCRIPTION_NOTE,
    }
    assert account_policy.CLAUDE_SUBSCRIPTION_NOTE.endswith("choose a Claude model in Ansatz CGM or Plain.")
    # The documented rollback removes the own-key CGM harness: the notes, the
    # Settings view and the refused login all stop pointing at it.
    monkeypatch.setenv("AGENT_MONITOR_MEMBER_ENGINES", "kimi,plain")
    with running_server() as port:
        _user, cookie = _member(port)
        _status, _headers, raw = request(port, "GET", f"{APP_MOUNT}/api/settings", headers={"Cookie": cookie})
        status, _headers, refused = request(port, "POST", f"{APP_MOUNT}/api/settings/claude/login/start",
                                            body={}, headers={"Cookie": cookie})
    notes = json.loads(raw)["subscription_notes"]
    assert notes["claude"].endswith("then choose a Claude model in Plain.")
    assert notes["codex"].endswith("then choose an OpenAI model in Plain.")
    assert "Ansatz CGM" not in notes["claude"] + notes["codex"]
    assert status == 403 and json.loads(refused)["error"] == notes["claude"]
    # With no own-key engine left, only the reason remains.
    monkeypatch.setenv("AGENT_MONITOR_MEMBER_ENGINES", "kimi")
    claude = account_policy.subscription_notes()["claude"]
    assert claude.startswith("Claude subscription sign-in is only for the site administrator")
    assert "API key" not in claude


def test_a_member_harness_run_without_a_model_uses_the_saved_default(member_console):
    with running_server() as port:
        user, cookie = _member(port)

        def start():
            return request(
                port, "POST", f"{APP_MOUNT}/api/runs",
                body={"engine": "math_harness", "problem_text": "Prove it.", "model": None},
                headers={"Cookie": cookie},
            )

        # A new account's default model is the included Kimi K3, which the
        # own-key harness does not run.
        refused = start()
        # The Projects page sends no model: the member's saved default applies.
        auth.set_user_env(int(user["id"]), {"AGENT_MONITOR_MODEL": "claude-opus-4-8"})
        accepted = start()
    assert refused[0] == 403
    assert json.loads(refused[2])["error"] == "Choose a model from your own API keys to run the Ansatz CGM harness"
    assert accepted[0] == 200, accepted[2]
    assert [(call["engine"], call["model"]) for call in member_console["started"]] == [
        ("math_harness", "claude-opus-4-8")
    ]


def test_a_member_blank_model_launch_runs_the_default_the_console_shows(member_console):
    # A new account's saved default is Kimi K3; with only an OpenAI key (and
    # no sponsored service) the console offers the first OpenAI model, and the
    # Projects page's "Default account model" must launch on the same one.
    with running_server() as port:
        user, cookie = _member(port)
        auth.set_user_env(int(user["id"]), {"OPENAI_API_KEY": "sk-member-openai-123456789"})
        _status, _headers, raw = request(port, "GET", f"{APP_MOUNT}/api/settings", headers={"Cookie": cookie})
        launches = [
            request(port, "POST", f"{APP_MOUNT}/api/runs",
                    body={"engine": engine, "problem_text": "Prove it.", "model": ""},
                    headers={"Cookie": cookie})
            for engine in ("plain", "math_harness")
        ]
    shown = json.loads(raw)
    assert (shown["saved_default_model"], shown["default_model"]) == ("kimi-k3", "gpt-6-astra")
    assert [status for status, _headers, _raw in launches] == [200, 200], launches
    assert [(call["engine"], call["model"]) for call in member_console["started"]] == [
        ("plain", "gpt-6-astra"), ("math_harness", "gpt-6-astra")
    ]


def test_member_may_start_tool_free_engines_but_not_local_harnesses(member_console):
    with running_server() as port:
        _user, cookie = _member(port)
        refused = request(
            port, "POST", f"{APP_MOUNT}/api/runs",
            body={"engine": "codex", "problem_text": "Prove it."},
            headers={"Cookie": cookie},
        )
        accepted = request(
            port, "POST", f"{APP_MOUNT}/api/runs",
            body={"engine": "kimi", "problem_text": "Prove it."},
            headers={"Cookie": cookie},
        )

    assert refused[0] == 403, refused[2]
    assert json.loads(refused[2])["allowed_engines"] == ["analyze", "kimi", "math_harness", "plain"]
    assert accepted[0] == 200, accepted[2]
    assert [call["engine"] for call in member_console["started"]] == ["kimi"]


def test_member_runs_the_cgm_harness_only_on_its_own_api_key(member_console):
    with running_server() as port:
        user, cookie = _member(port)

        def start(**extra):
            return request(
                port, "POST", f"{APP_MOUNT}/api/runs",
                body={"engine": "math_harness", "problem_text": "Prove it.", **extra},
                headers={"Cookie": cookie},
            )

        accepted = start(model="claude-opus-4-8", auth_route="api_key")
        refused = {
            "kimi": start(model="kimi-k3", auth_route="api_key"),
            "max": start(model="max", auth_route="dual_subscription"),
            "claude": start(model="sonnet", auth_route="claude_subscription"),
            "codex": start(model="gpt-6-astra", auth_route="codex_subscription"),
            "no model": start(auth_route="api_key"),
        }

    assert accepted[0] == 200, accepted[2]
    assert [
        (call["engine"], call["model"], call["auth_route"])
        for call in member_console["started"]
    ] == [("math_harness", "claude-opus-4-8", "api_key")]
    errors = {}
    for name, (status, _headers, raw) in refused.items():
        assert status == 403, (name, raw)
        errors[name] = json.loads(raw)["error"]
    assert "Kimi K3 runs on the included Ansatz CGM · Kimi K3 engine" in errors["kimi"]
    assert "limited to the site administrator" in errors["max"]
    assert errors["claude"] == account_policy.CLAUDE_SUBSCRIPTION_NOTE
    assert errors["codex"] == account_policy.CODEX_SUBSCRIPTION_NOTE
    assert "Choose a model from your own API keys" in errors["no model"]
    # A refused start reserves nothing: only the accepted one counts.
    assert len(console._MEMBER_RUN_WINDOWS[int(user["id"])]) == 1


def test_a_start_that_fails_returns_the_member_start(member_console, monkeypatch):
    def refuse(**_kwargs):
        raise ValueError("No compatible API key is configured; add and verify one in Settings")

    monkeypatch.setattr(console.job_manager, "start_job", refuse)
    with running_server() as port:
        user, cookie = _member(port)
        status, _headers, raw = request(
            port, "POST", f"{APP_MOUNT}/api/runs",
            body={"engine": "math_harness", "problem_text": "Prove it.", "model": "claude-opus-4-8"},
            headers={"Cookie": cookie},
        )
    assert status == 400 and "No compatible API key" in json.loads(raw)["error"]
    assert not console._MEMBER_RUN_WINDOWS.get(int(user["id"]))


def test_run_start_forwards_the_formalization_choice(member_console):
    with running_server() as port:
        _user, cookie = _member(port)
        for extra in ({"formalize": False}, {}):
            status, _headers, raw = request(
                port, "POST", f"{APP_MOUNT}/api/runs",
                body={"engine": "kimi", "problem_text": "Prove it.", **extra},
                headers={"Cookie": cookie},
            )
            assert status == 200, raw

    # Only an explicit false starts an informal-only run.
    assert [call["formalize"] for call in member_console["started"]] == [False, True]


def test_member_run_starts_are_rate_limited(member_console, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_MEMBER_RUNS_PER_HOUR", "2")
    with running_server() as port:
        _user, cookie = _member(port)
        statuses = [
            request(
                port, "POST", f"{APP_MOUNT}/api/runs",
                body={"engine": "kimi", "problem_text": "Prove it."},
                headers={"Cookie": cookie},
            )[0]
            for _ in range(3)
        ]
    assert statuses == [200, 200, 429]


def test_sign_ups_are_rate_limited_per_client(member_console, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_SIGNUPS_PER_CLIENT_PER_HOUR", "2")
    with running_server() as port:
        statuses = [_sign_up(port, f"user{index}@example.test")[0] for index in range(3)]
        other_client = _sign_up(port, "elsewhere@example.test", client="203.0.113.9")[0]
    assert statuses == [200, 200, 429]
    assert other_client == 200


def test_member_cannot_connect_subscriptions_or_start_the_formal_harness(
    member_console, monkeypatch, tmp_path: Path
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(console, "_workspace_for_run", lambda _run_id: workspace)
    monkeypatch.setattr(console.Handler, "_owns_run", lambda _self, _run_id, _user: True)
    monkeypatch.setattr(console.Handler, "_is_run_owner", lambda _self, _run_id, _user: True)
    with running_server() as port:
        _user, cookie = _member(port)
        codex = request(port, "POST", f"{APP_MOUNT}/api/settings/codex/login/start",
                        body={}, headers={"Cookie": cookie})
        claude = request(port, "POST", f"{APP_MOUNT}/api/settings/claude/login/start",
                         body={}, headers={"Cookie": cookie})
        harness = request(port, "POST", f"{APP_MOUNT}/api/run/kimi_1/lean_verify",
                          body={"action": "harness"}, headers={"Cookie": cookie})
    assert codex[0] == 403 and claude[0] == 403 and harness[0] == 403
    assert "administrator" in json.loads(harness[2])["error"]
    # The refusals explain why, in the words of the member's Settings cards.
    assert json.loads(codex[2])["error"] == account_policy.CODEX_SUBSCRIPTION_NOTE
    assert json.loads(claude[2])["error"] == account_policy.CLAUDE_SUBSCRIPTION_NOTE
    for raw in (codex[2], claude[2]):
        assert "site administrator" in json.loads(raw)["error"]


def test_member_dag_rebuild_never_uses_a_stored_subscription_sign_in(
    member_console, monkeypatch, tmp_path: Path
):
    """Sign-ins and runtimes a member stored before subscriptions became
    administrator-only stay unused by a manual DAG rebuild: not for the model
    "codex", not as a failed API call's fallback, not as a legacy run's runtime."""
    from agent_monitor import proof_graph

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "proof.md").write_text("Assume equality. Therefore one equals one.", encoding="utf-8")
    records: dict[str, dict] = {}
    monkeypatch.setattr(console, "_workspace_for_run", lambda _run_id: workspace)
    monkeypatch.setattr(console.Handler, "_owns_run", lambda _self, _run_id, _user: True)
    # The member started these runs: a rebuild needs the run's owner.
    monkeypatch.setattr(console.Handler, "_is_run_owner", lambda _self, _run_id, _user: True)
    monkeypatch.setattr(console, "_run_record_for", lambda run_id: dict(records[run_id]))
    graph = json.dumps({"nodes": [{"id": "n1", "kind": "conclusion", "label": "done",
                                   "statement": "1=1", "citations": []}], "edges": []})
    launches: list[str] = []
    monkeypatch.setattr(proof_graph, "_run_codex_prompt",
                        lambda *_a, **_k: launches.append("codex") or graph)
    monkeypatch.setattr(proof_graph, "_run_claude_prompt",
                        lambda *_a, **_k: launches.append("claude") or ("sonnet", graph))
    # Both stored sign-ins count as ready: only generate() keeps them unused.
    monkeypatch.setattr(proof_graph, "_codex_account_ready", lambda _user: True)
    monkeypatch.setattr(proof_graph, "_claude_account_ready", lambda _user: True)
    api_models: list = []

    def no_api_key(_env, model, _prompt, **_kwargs):
        api_models.append(model)
        raise ValueError(f"Model {model} requires an API key; add and verify it in Settings")

    monkeypatch.setattr(proof_graph, "_call_llm", no_api_key)
    with running_server() as port:
        user, cookie = _member(port)
        member_id = int(user["id"])
        api_run = {"run_id": "plain_api", "engine": "plain", "owner_id": member_id,
                   "auth_route": "api_key", "model": "gpt-5", "status": "finished"}
        legacy_run = {"run_id": "plain_legacy", "engine": "plain", "owner_id": member_id,
                      "model": "gpt-5", "status": "finished"}
        records.update(plain_api=api_run, plain_legacy=legacy_run)

        def rebuild(run_id: str, body: dict, session: str = cookie) -> tuple[int, str]:
            status, _headers, raw = request(port, "POST", f"{APP_MOUNT}/api/run/{run_id}/proof_graph",
                                            body=body, headers={"Cookie": session})
            return status, json.loads(raw).get("error", "")

        # Asking for "codex" is refused with the note the member's Settings
        # show, before any API call, not as an unknown API model.
        for run_id in ("plain_api", "plain_legacy"):
            assert rebuild(run_id, {"kind": "informal", "model": "codex"}) == (
                400, account_policy.subscription_notes()["codex"],
            )
        assert api_models == []
        for runtime in ("codex", "claude"):  # stored before the policy
            auth.set_user_env(member_id, {"AGENT_MONITOR_ACCOUNT_RUNTIME": runtime})
            assert rebuild("plain_legacy", {"kind": "informal"})[0] == 400
        assert launches == []

        # The administrator's API-key run stays on its key and says so; its
        # legacy run still honours an explicit "codex".
        admin, token = auth.login_and_create_session("owner@example.test", "long-enough-password")
        session = f"{auth.SESSION_COOKIE}={token}"
        records["plain_api"] = {**api_run, "owner_id": admin["id"]}
        records["plain_legacy"] = {**legacy_run, "owner_id": admin["id"]}
        status, error = rebuild("plain_api", {"kind": "informal", "model": "codex"}, session)
        assert status == 400 and error.startswith("This run was recorded on an API key, so its DAG")
        assert rebuild("plain_legacy", {"kind": "informal", "model": "codex"}, session) == (200, "")
    assert launches == ["codex"] and api_models == [None, None]


def test_member_settings_report_the_policy_without_probing_logins(member_console, monkeypatch):
    from agent_monitor import claude_login, codex_login

    probes: list[str] = []
    monkeypatch.setattr(claude_login, "status", lambda _uid: probes.append("claude") or {})
    monkeypatch.setattr(claude_login, "poll_login", lambda _uid: probes.append("claude") or {})
    monkeypatch.setattr(codex_login, "status", lambda _uid: probes.append("codex") or {})
    monkeypatch.setattr(codex_login, "poll_login", lambda _uid: probes.append("codex") or {})
    monkeypatch.setattr(claude_login, "_auth_status", lambda *_a, **_k: probes.append("claude") or {})
    monkeypatch.setattr(codex_login, "account_login_ready", lambda _home: probes.append("codex") or False)
    with running_server() as port:
        _user, cookie = _member(port)
        statuses = {
            path: request(port, "GET", f"{APP_MOUNT}/api/settings/{path}", headers={"Cookie": cookie})
            for path in ("claude/status", "claude/login/poll", "codex/status", "codex/login/poll")
        }
        _status, _headers, raw = request(port, "GET", f"{APP_MOUNT}/api/settings", headers={"Cookie": cookie})

    assert probes == []
    view = json.loads(raw)
    assert view["subscription_policy"] == {"claude": "admin_only", "codex": "admin_only"}
    assert view["subscription_notes"] == account_policy.subscription_notes()
    assert view["subscription_notes"] == {
        "claude": account_policy.CLAUDE_SUBSCRIPTION_NOTE,
        "codex": account_policy.CODEX_SUBSCRIPTION_NOTE,
    }
    assert view["account_runtime"] == "api" and view["auth_mode"] == "api_key"
    for path, (status, _headers, body) in statuses.items():
        state = json.loads(body)
        assert status == 200, path
        assert state["policy"] == "admin_only" and state["connected"] is False
        assert state["error"] == account_policy.subscription_notes()[path.split("/")[0]]


def test_member_login_actions_run_no_cli_and_create_no_account_home(
    member_console, monkeypatch, tmp_path: Path
):
    """A member's cancel, code and logout never reach the login CLIs.

    Logout still removes a sign-in stored before subscriptions became
    administrator-only, which the member's Settings card offers to remove.
    """
    from agent_monitor import claude_login, codex_login

    calls = tmp_path / "cli-calls.log"
    fake = tmp_path / "fake-cli"
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\necho "{{}}"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(claude_login, "claude_binary", lambda: str(fake))
    monkeypatch.setattr(codex_login, "codex_binary", lambda: str(fake))
    monkeypatch.setattr(claude_login, "_auth_cache", {})
    monkeypatch.setenv("AGENT_MONITOR_CLAUDE_ACCOUNT_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("AGENT_MONITOR_CODEX_ACCOUNT_HOME", str(tmp_path / "codex"))
    notes = account_policy.subscription_notes()
    with running_server() as port:
        user, cookie = _member(port)

        def post(path: str, body: dict | None = None):
            return request(port, "POST", f"{APP_MOUNT}/api/settings/{path}",
                           body=body or {}, headers={"Cookie": cookie})

        code = post("claude/login/code", {"code": "one-time-code"})
        answers = {
            path: post(path)
            for path in ("claude/login/cancel", "claude/logout", "codex/login/cancel", "codex/logout")
        }
        homes_created = (tmp_path / "claude").exists() or (tmp_path / "codex").exists()
        stored = [
            claude_login.account_home(int(user["id"])) / ".credentials.json",
            codex_login.account_home(int(user["id"])) / "auth.json",
        ]
        for credentials in stored:
            credentials.parent.mkdir(parents=True, exist_ok=True)
            credentials.write_text("{}", encoding="utf-8")
        removed = [post("claude/logout")[0], post("codex/logout")[0]]
        _status, _headers, raw = request(port, "GET", f"{APP_MOUNT}/api/settings", headers={"Cookie": cookie})

    assert not calls.exists()
    assert not homes_created
    assert code[0] == 403 and json.loads(code[2])["error"] == notes["claude"]
    for path, (status, _headers, body) in answers.items():
        state = json.loads(body)
        assert status == 200, path
        assert state["policy"] == "admin_only" and state["connected"] is False
        assert state["error"] == notes[path.split("/")[0]]
    assert removed == [200, 200]
    assert not any(credentials.exists() for credentials in stored)
    assert json.loads(raw)["subscription_stored"] == {"claude": False, "codex": False}


def test_start_job_refuses_member_harnesses_and_pins_the_api_route(monkeypatch):
    member = {"id": 5, "is_admin": False}
    with pytest.raises(ValueError, match="administrator"):
        jobs.start_job(engine="codex", problem_text="Prove it.", user=member)
    monkeypatch.setenv("PLAIN_CMD", "/bin/true")
    with pytest.raises(ValueError, match="administrator"):
        jobs.start_job(engine="plain", problem_text="Prove it.", user=member)


def test_continuation_refuses_a_member_on_a_local_harness_run(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path)
    (tmp_path / "workspaces" / "codex_run").mkdir(parents=True)
    monkeypatch.setattr(jobs, "_run_is_active", lambda _run_id: None)
    monkeypatch.setattr(jobs, "_load_run_record", lambda _run_id: {"engine": "codex"})
    member = {"id": 5, "is_admin": False}
    with pytest.raises(ValueError, match="administrator"):
        jobs.continue_run("codex_run", user=member)
    with pytest.raises(ValueError, match="administrator"):
        jobs.send_human_message("codex_run", "Keep going.", user=member)


def test_a_retired_danus_run_cannot_be_continued(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path)
    (tmp_path / "workspaces" / "danus_old").mkdir(parents=True)
    monkeypatch.setattr(jobs, "_run_is_active", lambda _run_id: None)
    monkeypatch.setattr(jobs, "_load_run_record", lambda _run_id: {"engine": "danus", "run_id": "danus_old"})
    appended = []
    monkeypatch.setattr(jobs, "_append_chat", lambda *args, **kwargs: appended.append(args))
    admin = {"id": 1, "is_admin": True}
    with pytest.raises(ValueError, match="Danus has been retired"):
        jobs.continue_run("danus_old", user=admin)
    with pytest.raises(ValueError, match="Danus has been retired"):
        jobs.send_human_message("danus_old", "Keep going.", user=admin)
    assert appended == []


def test_member_run_environment_drops_custom_provider_endpoints(monkeypatch):
    stored = {"OPENAI_API_KEY": "sk-member", "OPENAI_BASE_URL": "http://169.254.169.254/v1"}
    monkeypatch.setattr("agent_monitor.settings.resolved_user_env", lambda _user: dict(stored))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr("agent_monitor.settings._read_env_file", lambda: {})
    member_env = jobs._user_extra_env({"id": 5, "is_admin": False}, "gpt-5")
    admin_env = jobs._user_extra_env({"id": 1, "is_admin": True}, "gpt-5")
    assert "OPENAI_BASE_URL" not in member_env
    assert admin_env.get("OPENAI_BASE_URL") == "http://169.254.169.254/v1"


def test_a_member_cannot_save_a_custom_base_url_its_runs_would_ignore(member_console, monkeypatch):
    from agent_monitor import kimi_k3, settings

    for name in settings._account_base_vars():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(settings.OPERATOR_KIMI_BASE_VAR, raising=False)
    # The operator routes OpenAI through its own gateway, which member runs keep.
    settings.ENV_PATH.write_text("OPENAI_BASE_URL=https://gateway.example.test/v1\n", encoding="utf-8")
    key = "sk-member-openai-123456789"
    with running_server() as port:
        user, cookie = _member(port)

        def save(updates):
            status, _headers, raw = request(port, "POST", f"{APP_MOUNT}/api/settings",
                                            body={"updates": updates}, headers={"Cookie": cookie})
            return status, json.loads(raw)

        refused = [
            save({"OPENAI_API_KEY": key, "OPENAI_BASE_URL": "http://127.0.0.1:49971/v1"}),
            save({"ANTHROPIC_BASE_URL": "https://proxy.example.test"}),
            save({"AGENT_MONITOR_BASE_URL": "https://proxy.example.test/v1"}),
        ]
        stored_after_refusals = auth.get_user_env(int(user["id"]))
        # What a Settings form sends: the standard endpoints it shows, or the
        # operator's own, which member runs use.
        standard = save({"OPENAI_API_KEY": key, "OPENAI_BASE_URL": "https://api.openai.com/v1/",
                         "KIMI_API_BASE": kimi_k3.DEFAULT_BASE_URL,
                         "ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
        operator = save({"OPENAI_BASE_URL": "https://gateway.example.test/v1"})
        # A custom base saved before this rule, which runs never used, goes on the next save.
        auth.set_user_env(int(user["id"]), {"GEMINI_API_BASE": "http://127.0.0.1:49971"})
        later = save({"AGENT_MONITOR_MODEL": "gpt-6-astra"})
        cleared = save({"OPENAI_BASE_URL": ""})
        owner, token = auth.login_and_create_session("owner@example.test", "long-enough-password")
        admin = request(port, "POST", f"{APP_MOUNT}/api/settings",
                        body={"updates": {"OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}},
                        headers={"Cookie": f"{auth.SESSION_COOKIE}={token}"})
    for status, body in refused:
        assert status == 400 and body["error"] == (
            "Custom API base URLs are set by the site operator; "
            "your runs use the provider's standard endpoint"
        ), body
    assert stored_after_refusals.get("OPENAI_API_KEY") is None
    assert [standard[0], operator[0], later[0], cleared[0]] == [200, 200, 200, 200]
    stored = auth.get_user_env(int(user["id"]))
    assert stored["OPENAI_API_KEY"] == key and stored["AGENT_MONITOR_MODEL"] == "gpt-6-astra"
    assert "GEMINI_API_BASE" not in stored and "OPENAI_BASE_URL" not in stored
    # The operator keeps choosing endpoints for its own runs.
    assert admin[0] == 200, admin[2]
    assert auth.get_user_env(int(owner["id"]))["OPENAI_BASE_URL"] == "http://127.0.0.1:9/v1"


def test_start_job_holds_the_member_cgm_harness_to_its_own_api_key(monkeypatch):
    for name in ("AGENT_MONITOR_MEMBER_ENGINES", "AGENT_MONITOR_PUBLIC", "MATH_HARNESS_CMD"):
        monkeypatch.delenv(name, raising=False)
    member = {"id": 5, "is_admin": False}
    for kwargs, message in (
        ({"model": "kimi-k3"}, "Kimi K3 runs on the included Ansatz CGM · Kimi K3 engine"),
        ({"model": None}, "Choose a model from your own API keys"),
        ({"model": "max"}, "limited to the site administrator"),
        ({"model": "sonnet", "auth_route": "claude_subscription"}, "Anthropic doesn't let"),
        ({"model": "gpt-6-astra", "auth_route": "codex_subscription"}, "partner approval"),
        ({"model": "claude-opus-4-8", "auth_route": "dual_subscription"}, "limited to the site administrator"),
    ):
        with pytest.raises(ValueError, match=re.escape(message)):
            jobs.start_job(engine="math_harness", problem_text="Prove it.", user=member, **kwargs)
    monkeypatch.setenv("MATH_HARNESS_CMD", "/bin/true")
    with pytest.raises(ValueError, match="administrator"):
        jobs.start_job(
            engine="math_harness", problem_text="Prove it.", model="claude-opus-4-8", user=member
        )


def test_start_job_runs_a_member_blank_model_on_the_members_own_key(monkeypatch, tmp_path):
    from agent_monitor import auto_pipeline

    for name in ("AGENT_MONITOR_PUBLIC", "AGENT_MONITOR_MEMBER_ENGINES", "MATH_HARNESS_CMD",
                 "PLAIN_CMD", "AGENT_MONITOR_SPONSORED_KIMI", "OPENAI_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    own_key = "sk-member-openai-123456789"
    auth.set_user_env(int(member["id"]), {"OPENAI_API_KEY": own_key})
    captured: list[dict] = []

    class FakeThread:
        def __init__(self, *, kwargs=None, **_rest):
            captured.append(kwargs or {})

        def start(self):
            return None

    monkeypatch.setattr(jobs, "workspace_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(jobs, "_write_run", lambda _run: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(jobs.threading, "Thread", FakeThread)
    monkeypatch.setattr(auto_pipeline, "start", lambda **_kwargs: None)

    # The saved default (a new account's Kimi K3) has no key here, so the
    # blank model is the first model of the member's own OpenAI key.
    for engine in ("plain", "math_harness"):
        job = jobs.start_job(engine=engine, problem_text="Prove it.", user=member)
        assert (job["engine"], job["model"], job["auth_route"]) == (engine, "gpt-6-astra", "api_key")
        assert captured[-1]["extra_env"]["OPENAI_API_KEY"] == own_key

    # With no key left, the refusal names what is missing.
    auth.set_user_env(int(member["id"]), {}, clear=["OPENAI_API_KEY"])
    with pytest.raises(ValueError, match="Kimi K3 requires your own API key"):
        jobs.start_job(engine="plain", problem_text="Prove it.", user=member)
    with pytest.raises(ValueError, match="Choose a model from your own API keys"):
        jobs.start_job(engine="math_harness", problem_text="Prove it.", user=member)


def test_member_blank_model_is_the_saved_default_or_a_model_its_keys_run(monkeypatch):
    from agent_monitor import settings, sponsored_kimi_client

    for name in ("AGENT_MONITOR_PUBLIC", "AGENT_MONITOR_MEMBER_ENGINES"):
        monkeypatch.delenv(name, raising=False)
    member = {"id": 5, "is_admin": False}
    env: dict[str, str] = {}
    sponsored = [False]
    monkeypatch.setattr(settings, "resolved_user_env", lambda _user: dict(env))
    monkeypatch.setattr(sponsored_kimi_client, "enabled", lambda: sponsored[0])
    monkeypatch.setattr(sponsored_kimi_client, "available", lambda **_kwargs: sponsored[0])

    def resolve(saved, keys, engine, model=None):
        env.clear()
        env.update({"AGENT_MONITOR_MODEL": saved, **keys} if saved else keys)
        return account_policy.member_run_model(member, engine, model)

    openai = {"OPENAI_API_KEY": "sk-openai"}
    # A saved default the member's keys run wins.
    assert resolve("claude-opus-4-8", {"ANTHROPIC_API_KEY": "sk-ant", **openai}, "plain") == "claude-opus-4-8"
    assert resolve("kimi-k3", {"KIMI_API_KEY": "sk-kimi", **openai}, "plain") == "kimi-k3"
    # Otherwise the first model of the member's keys the engine accepts (never
    # Kimi K3 for the own-key harness, never Max).
    for saved in ("kimi-k3", "claude-opus-4-8", "max", None):
        for engine in ("plain", "math_harness", "analyze"):
            assert resolve(saved, openai, engine) == "gpt-6-astra", (saved, engine)
    assert resolve("kimi-k3", {"KIMI_API_KEY": "sk-kimi", **openai}, "math_harness") == "gpt-6-astra"
    assert resolve("kimi-k3", {"KIMI_API_KEY": "sk-kimi", **openai}, "analyze") == "kimi-k3"
    # Nothing else runs: the saved default stays where the engine takes it,
    # so the refusal names the key it needs.
    assert resolve("kimi-k3", {}, "plain") == "kimi-k3"
    assert resolve("kimi-k3", {}, "analyze") == "kimi-k3"
    assert resolve("claude-opus-4-8", {}, "math_harness") == "claude-opus-4-8"
    assert resolve("kimi-k3", {}, "math_harness") is None
    assert resolve("max", {}, "analyze") is None
    # The sponsored service runs Kimi K3 for Plain and Analyze, never for the
    # own-key harness.
    sponsored[0] = True
    assert resolve("claude-opus-4-8", {}, "plain") == "kimi-k3"
    assert resolve("claude-opus-4-8", {}, "analyze") == "kimi-k3"
    assert resolve("kimi-k3", {}, "math_harness") is None
    assert resolve("kimi-k3", openai, "math_harness") == "gpt-6-astra"
    # A named model, another engine or another account is left alone.
    assert resolve("kimi-k3", openai, "plain", "gpt-5.6-sol") == "gpt-5.6-sol"
    assert resolve("claude-opus-4-8", openai, "kimi") is None
    assert account_policy.member_run_model({"id": 1, "is_admin": True}, "plain", None) is None


def test_member_cgm_harness_run_and_derived_stages_bill_only_the_members_key(
    monkeypatch, tmp_path
):
    """A member's own Anthropic key carries the run and its Formal/DAG/Lean work."""
    from agent_monitor import auto_pipeline, lean_verify, proof_graph, settings, sponsored_kimi_client
    from agent_monitor.subprocess_env import child_process_env

    for name in ("AGENT_MONITOR_PUBLIC", "AGENT_MONITOR_MEMBER_ENGINES", "MATH_HARNESS_CMD",
                 "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "AGENT_MONITOR_DISABLE_AUTO_PIPELINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    # The operator's own keys, in the service environment and its .env file.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-operator-service-openai-0000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-operator-service-0000")
    settings.ENV_PATH.write_text(
        "OPENAI_API_KEY=sk-operator-file-openai-0000\nANTHROPIC_API_KEY=sk-ant-operator-file-0000\n",
        encoding="utf-8",
    )
    # A healthy sponsored broker must still never serve this run.
    monkeypatch.setattr(sponsored_kimi_client, "configured", lambda: True)
    monkeypatch.setattr(
        sponsored_kimi_client, "issue_credentials",
        lambda **_kwargs: pytest.fail("a member's own-key run used the sponsored Kimi broker"),
    )
    auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    assert account_policy.is_member(member)
    own_key = "sk-ant-member-own-key-1234567890"
    auth.set_user_env(int(member["id"]), {
        "ANTHROPIC_API_KEY": own_key,
        "ANTHROPIC_BASE_URL": "http://169.254.169.254/v1",
    })

    captured: dict = {}
    record: dict = {}
    pipeline: list[dict] = []

    class FakeThread:
        def __init__(self, *, kwargs=None, **_rest):
            captured.update(kwargs or {})

        def start(self):
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr(jobs, "workspace_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(jobs, "_write_run", lambda run: record.update(run))
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(jobs.threading, "Thread", FakeThread)
    monkeypatch.setattr(auto_pipeline, "start", lambda **kwargs: pipeline.append(kwargs))

    job = jobs.start_job(
        engine="math_harness", problem_text="Prove it.", model="claude-opus-4-8",
        user=member, auth_route="api_key",
    )

    # (a) The run itself: the member's key, the provider's own endpoint.
    env = captured["extra_env"]
    assert (job["engine"], job["auth_route"], job["credential_source"]) == ("math_harness", "api_key", "user")
    assert env["ANTHROPIC_API_KEY"] == own_key
    assert "OPENAI_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGENT_MONITOR_CODEX_SUBSCRIPTION",
                 "AGENT_MONITOR_CLAUDE_SUBSCRIPTION", "AGENT_MONITOR_SPONSORED_KIMI", "KIMI_API_KEY"):
        assert name not in env
    child = child_process_env(extra=env)  # how the runner process is started
    assert child["ANTHROPIC_API_KEY"] == own_key and "OPENAI_API_KEY" not in child

    # (b) The automatic Formal, DAG and Lean stages for that run.
    assert [(item["owner_id"], item["model"], item["engine"]) for item in pipeline] == [
        (member["id"], "claude-opus-4-8", "math_harness")
    ]
    run_record = {**record, "owner_id": member["id"]}
    derived_user = auto_pipeline._run_user(member["id"], run_record)
    lean_env = lean_verify._llm_environment(derived_user, run_record)
    assert lean_env["ANTHROPIC_API_KEY"] == own_key
    assert "OPENAI_API_KEY" not in lean_env and "ANTHROPIC_BASE_URL" not in lean_env
    assert lean_env.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") != "1"
    assert "KIMI_API_KEY" not in lean_env

    (tmp_path / "proof.md").write_text("# Proof\n\nIt holds because it holds.\n", encoding="utf-8")
    calls: list = []

    def call_llm(env, model, _prompt, **_kwargs):
        calls.append((dict(env), model))
        raise RuntimeError("stopped before any provider call")

    monkeypatch.setattr(proof_graph, "_call_llm", call_llm)
    with pytest.raises(RuntimeError, match="stopped before any provider call"):
        proof_graph.generate(
            workspace=tmp_path, run_record=run_record, user=derived_user,
            model="claude-opus-4-8", kind="informal", allow_codex_fallback=False,
        )
    graph_env, graph_model = calls[0]
    assert graph_model == "claude-opus-4-8"
    assert graph_env["ANTHROPIC_API_KEY"] == own_key
    assert "OPENAI_API_KEY" not in graph_env and "ANTHROPIC_BASE_URL" not in graph_env

    # Public mode would hand members the server's keys: no own-key harness then.
    monkeypatch.setenv("AGENT_MONITOR_PUBLIC", "1")
    with pytest.raises(ValueError, match="administrator"):
        jobs.start_job(
            engine="math_harness", problem_text="Prove it.", model="claude-opus-4-8",
            user=member, auth_route="api_key",
        )


def test_admin_run_derived_stages_keep_the_administrators_keys_and_endpoint(
    monkeypatch, tmp_path
):
    """The automatic Formal, DAG and Lean stages of the administrator's run
    resolve credentials as its manual actions do: the server .env keys, and
    the administrator's own base URL alongside its own key."""
    from agent_monitor import auto_pipeline, lean_verify, proof_graph, settings

    for name in ("AGENT_MONITOR_PUBLIC", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                 "AGENT_MONITOR_BASE_URL", "AGENT_MONITOR_DISABLE_AUTO_PIPELINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    settings.ENV_PATH.write_text("OPENAI_API_KEY=sk-operator-file-openai-0000\n", encoding="utf-8")
    admin = auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    assert admin["is_admin"] and account_policy.is_member(member)
    record = {
        "run_id": "admin_api_run", "owner_id": admin["id"], "engine": "math_harness",
        "model": "gpt-6-astra", "requested_model": "gpt-6-astra", "auth_route": "api_key",
        "credential_source": "user", "status": "finished",
    }
    (tmp_path / "proof.md").write_text("# Proof\n\nIt holds because it holds.\n", encoding="utf-8")
    calls: list = []

    def call_llm(env, model, _prompt, **_kwargs):
        calls.append((dict(env), model))
        raise RuntimeError("stopped before any provider call")

    monkeypatch.setattr(proof_graph, "_call_llm", call_llm)

    derived_user = auto_pipeline._run_user(admin["id"], record)
    assert not account_policy.is_member(derived_user)
    # The administrator keeps no per-account key: the server .env key serves it.
    assert lean_verify._llm_environment(derived_user, record)["OPENAI_API_KEY"] == (
        "sk-operator-file-openai-0000"
    )
    auto_pipeline._informal_worker(tmp_path, record, derived_user, "gpt-6-astra", {}, "math_harness")
    assert calls and calls[-1][0]["OPENAI_API_KEY"] == "sk-operator-file-openai-0000"

    # A gateway key stays with its gateway: never sent to the provider's own endpoint.
    gateway = "https://llm-gateway.example.test/v1"
    auth.set_user_env(int(admin["id"]), {
        "OPENAI_API_KEY": "sk-admin-gateway-key-1234567890", "OPENAI_BASE_URL": gateway,
    })
    lean_env = lean_verify._llm_environment(derived_user, record)
    assert (lean_env["OPENAI_API_KEY"], lean_env.get("OPENAI_BASE_URL")) == (
        "sk-admin-gateway-key-1234567890", gateway,
    )
    auto_pipeline._informal_worker(tmp_path, record, derived_user, "gpt-6-astra", {}, "math_harness")
    graph_env, graph_model = calls[-1]
    assert graph_model == "gpt-6-astra"
    assert (graph_env["OPENAI_API_KEY"], graph_env.get("OPENAI_BASE_URL")) == (
        "sk-admin-gateway-key-1234567890", gateway,
    )

    # A member and a guest run keep their own roles.
    assert account_policy.is_member(
        auto_pipeline._run_user(member["id"], {**record, "owner_id": member["id"]})
    )
    assert auto_pipeline._run_user(admin["id"], {**record, "guest": True}) == {
        "id": admin["id"], "guest": True,
    }
    assert auto_pipeline._run_user(None, record) is None


def test_member_settings_skip_login_probes_and_keep_the_api_runtime(monkeypatch, tmp_path):
    from agent_monitor import claude_login, codex_login, settings

    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("AGENT_MONITOR_CODEX_ACCOUNT_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("AGENT_MONITOR_CLAUDE_ACCOUNT_HOME", str(tmp_path / "claude"))
    probes: list[str] = []
    monkeypatch.setattr(
        claude_login, "_auth_status",
        lambda *_a, **_k: probes.append("claude") or {"loggedIn": False},
    )
    monkeypatch.setattr(codex_login, "account_login_ready", lambda _home: probes.append("codex") or False)
    admin = auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    # A runtime stored before subscriptions became administrator-only.
    auth.set_user_env(int(member["id"]), {"AGENT_MONITOR_ACCOUNT_RUNTIME": "claude"})

    view = settings.get_settings(member)
    assert probes == []
    assert (view["account_runtime"], view["auth_mode"]) == ("api", "api_key")
    assert view["subscription_policy"] == {"claude": "admin_only", "codex": "admin_only"}
    assert view["subscription_notes"] == account_policy.subscription_notes()
    assert view["claude_connected"] is False and view["codex_connected"] is False
    assert view["models_by_runtime"]["claude"] == [] and view["models_by_runtime"]["codex"] == []
    assert view["subscription_stored"] == {"claude": False, "codex": False}
    # Looking for a leftover sign-in creates no account home.
    assert not (tmp_path / "claude").exists() and not (tmp_path / "codex").exists()
    # A sign-in linked before the policy is reported (by its file) so the
    # member can remove it; it is still never probed or used.
    codex_home = codex_login.account_home(int(member["id"]))
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    assert settings.get_settings(member)["subscription_stored"] == {"claude": False, "codex": True}

    for requested in ("claude", "codex"):
        saved = settings.save_settings({"account_runtime": requested}, user=member)
        assert saved["settings"]["account_runtime"] == "api"
        stored = auth.get_user_env(int(member["id"]))
        assert (stored["AGENT_MONITOR_ACCOUNT_RUNTIME"], stored["AGENT_MONITOR_USE_CLAUDE"],
                stored["AGENT_MONITOR_USE_CODEX"]) == ("api", "0", "0")
    assert probes == []

    # The administrator's settings are unchanged: its logins are looked up.
    admin_view = settings.get_settings(admin)
    assert admin_view["subscription_policy"] == {"claude": "available", "codex": "available"}
    assert admin_view["subscription_notes"] == {} and admin_view["subscription_stored"] == {}
    assert {"claude", "codex"} <= set(probes)
    saved = settings.save_settings({"account_runtime": "codex"}, user=admin)
    assert saved["settings"]["account_runtime"] == "codex"


def test_public_mode_member_settings_keep_the_operators_key_and_endpoints_private(monkeypatch, tmp_path):
    from agent_monitor import claude_login, codex_login, settings

    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("AGENT_MONITOR_CODEX_ACCOUNT_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("AGENT_MONITOR_CLAUDE_ACCOUNT_HOME", str(tmp_path / "claude"))
    monkeypatch.setattr(claude_login, "_auth_status", lambda *_a, **_k: {"loggedIn": False})
    monkeypatch.setattr(codex_login, "account_login_ready", lambda _home: False)
    monkeypatch.setenv("AGENT_MONITOR_PUBLIC", "1")
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "AGENT_MONITOR_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    gateway = "https://internal-gw.corp.example/v1"
    settings.ENV_PATH.write_text(
        "OPENAI_API_KEY=sk-secretoperatorkey1234567890\n"
        f"OPENAI_BASE_URL={gateway}\nAGENT_MONITOR_BASE_URL={gateway}\n",
        encoding="utf-8",
    )
    admin = auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")

    def openai(view: dict) -> dict:
        return next(provider for provider in view["providers"] if provider["id"] == "openai")

    view = settings.get_settings(member)
    # The member runs on the server's key without seeing any part of it, or
    # where the operator sends its requests (as for a guest).
    assert openai(view)["api_key_set"] is True
    assert [provider["api_key_masked"] for provider in view["providers"]] == [None] * len(view["providers"])
    assert openai(view)["base_url"] == ""
    assert view["general"]["AGENT_MONITOR_BASE_URL"]["value"] == ""
    assert gateway not in json.dumps(view)
    assert view["runtime_ready"] is True and "openai" in view["configured_providers"]
    assert "gpt-6-astra" in view["api_models"]
    # What the member saved itself still shows.
    own_gateway = "https://member.example/v1"
    auth.set_user_env(int(member["id"]), {"OPENAI_API_KEY": "sk-member-own-key-0000abcd", "OPENAI_BASE_URL": own_gateway})
    assert (openai(settings.get_settings(member))["api_key_masked"], openai(settings.get_settings(member))["base_url"]) == (
        "sk-m••••abcd", own_gateway,
    )
    # The administrator sees the operator's values.
    assert (openai(settings.get_settings(admin))["api_key_masked"], openai(settings.get_settings(admin))["base_url"]) == (
        "sk-s••••7890", gateway,
    )


def test_public_mode_member_settings_hide_operator_endpoints_copied_into_its_rows(monkeypatch, tmp_path):
    from agent_monitor import claude_login, codex_login, settings

    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("AGENT_MONITOR_CODEX_ACCOUNT_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("AGENT_MONITOR_CLAUDE_ACCOUNT_HOME", str(tmp_path / "claude"))
    monkeypatch.setattr(claude_login, "_auth_status", lambda *_a, **_k: {"loggedIn": False})
    monkeypatch.setattr(codex_login, "account_login_ready", lambda _home: False)
    monkeypatch.setenv("AGENT_MONITOR_PUBLIC", "1")
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "AGENT_MONITOR_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    gateway = "https://internal-gw.corp.example/v1"
    anthropic_gateway = "https://claude-gw.corp.example"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", anthropic_gateway)
    settings.ENV_PATH.write_text(
        "OPENAI_API_KEY=sk-secretoperatorkey1234567890\n"
        f"OPENAI_BASE_URL={gateway}\nAGENT_MONITOR_BASE_URL={gateway}\n",
        encoding="utf-8",
    )
    auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    # Saving Settings before the redaction sent every endpoint the cards
    # showed, the operator's included, into the member's own rows.
    auth.set_user_env(int(member["id"]), {
        "OPENAI_BASE_URL": gateway,
        "AGENT_MONITOR_BASE_URL": gateway,
        "ANTHROPIC_BASE_URL": anthropic_gateway + "/",
    })

    view = settings.get_settings(member)
    bases = {provider["id"]: provider["base_url"] for provider in view["providers"]}
    assert (bases["openai"], bases["anthropic"]) == ("", "")
    assert view["general"]["AGENT_MONITOR_BASE_URL"]["value"] == ""
    assert gateway not in json.dumps(view) and anthropic_gateway not in json.dumps(view)
    # Runs still reach the operator's endpoint, as an inherited one would.
    assert jobs._user_extra_env(member, "gpt-6-astra")["OPENAI_BASE_URL"] == gateway


def test_continue_on_a_running_run_uses_no_member_start(member_console, monkeypatch):
    monkeypatch.setattr(console.Handler, "_owns_run", lambda _self, _run_id, _user: True)
    monkeypatch.setattr(console.Handler, "_is_run_owner", lambda _self, _run_id, _user: True)
    calls: list = []
    active = {"value": True}
    monkeypatch.setattr(console.job_manager, "run_is_active", lambda _run_id: active["value"])
    with running_server() as port:
        user, cookie = _member(port)
        window = lambda: list(console._MEMBER_RUN_WINDOWS.get(int(user["id"])) or [])  # noqa: E731

        def post(path: str):
            return request(port, "POST", f"{APP_MOUNT}/api/runs/kimi_1/{path}", body={"message": "Next."}, headers={"Cookie": cookie})

        monkeypatch.setattr(
            console.job_manager, "continue_run",
            lambda *args, **kwargs: calls.append(args) or {"ok": True, "status": "continuing"},
        )
        repeated = [post("continue") for _ in range(4)]
        assert calls == [] and window() == []

        # A continuation that loses the race to another request returns its start.
        active["value"] = False
        monkeypatch.setattr(
            console.job_manager, "continue_run",
            lambda run_id, **_kwargs: {"ok": False, "status": "already_running", "run_id": run_id},
        )
        raced = post("continue")
        assert window() == []

        def refuse(*_args, **_kwargs):
            raise ValueError("That engine runs local tools and is limited to the site administrator")

        monkeypatch.setattr(console.job_manager, "continue_run", refuse)
        refused = post("continue")
        assert window() == []

        monkeypatch.setattr(
            console.job_manager, "continue_run",
            lambda run_id, **_kwargs: {"ok": True, "status": "continuing", "run_id": run_id},
        )
        started = post("continue")
        assert len(window()) == 1

    for status, _headers, raw in repeated:
        assert status == 200
        assert json.loads(raw) == {"ok": False, "status": "already_running", "run_id": "kimi_1"}
    assert raced[0] == 200 and json.loads(raced[2])["status"] == "already_running"
    assert refused[0] == 400
    assert started[0] == 200


def _member_run(monkeypatch, tmp_path: Path, owner_id: int, *, running: bool = True) -> str:
    """A plain API-key run on disk owned by ``owner_id``, its first job running.

    Continuations are prepared for real (policy, credentials, launch claim)
    but their thread only records what it would have run.
    """
    run_id = "plain_p_r1"
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_LAUNCHING_RUNS", set())
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(console.Handler, "_owns_run", lambda _self, _run_id, _user: True)
    workspace = jobs.RUNS_DIR / "workspaces" / run_id
    workspace.mkdir(parents=True)
    jobs._write_run({
        "run_id": run_id, "engine": "plain", "status": "running" if running else "finished",
        "model": "claude-opus-4-8", "requested_model": "claude-opus-4-8", "auth_route": "api_key",
        "credential_source": "user", "owner_id": owner_id, "workspace": str(workspace),
        "problems": [{"text": "Prove it.", "source": "initial"}], "use_subagents": False,
    })
    if running:
        jobs._JOBS["j1"] = {"job_id": "j1", "run_id": run_id, "status": "running", "owner_id": owner_id}
    return run_id


class _RecordedThreads:
    """``jobs.threading`` whose new threads (continuations) only record their work.

    Patched on ``jobs`` alone, so the test's HTTP server keeps real threads.
    """

    def __init__(self):
        self.launched: list[dict] = []

    def __getattr__(self, name):
        return getattr(threading, name)

    def Thread(self, *, kwargs=None, **_rest):  # noqa: N802 - stands in for threading.Thread
        launched = self.launched

        class _Recorded:
            def start(self):
                launched.append(kwargs or {})

        return _Recorded()


def _record_continuations(monkeypatch) -> _RecordedThreads:
    threads = _RecordedThreads()
    monkeypatch.setattr(jobs, "threading", threads)
    return threads


def _finish_running_jobs() -> None:
    with jobs._LOCK:
        for job in jobs._JOBS.values():
            if job.get("status") == "running":
                job["status"] = "finished"


def test_feedback_joining_a_queued_follow_up_uses_no_member_start(member_console, monkeypatch, tmp_path):
    threads = _record_continuations(monkeypatch)
    with running_server() as port:
        user, cookie = _member(port)
        member_id = int(user["id"])
        auth.set_user_env(member_id, {"ANTHROPIC_API_KEY": "sk-ant-member-000000000000"})
        run_id = _member_run(monkeypatch, tmp_path, member_id)
        window = lambda: len(console._MEMBER_RUN_WINDOWS.get(member_id) or [])  # noqa: E731

        def chat(message: str):
            return request(port, "POST", f"{APP_MOUNT}/api/runs/{run_id}/chat", body={"message": message}, headers={"Cookie": cookie})

        # The first message for a running run books its follow-up continuation;
        # later ones join that follow-up.
        replies = [chat("Try induction."), chat("Also check n = 1."), chat("And n = 2.")]
        assert [status for status, _headers, _raw in replies] == [200, 200, 200]
        assert [json.loads(raw)["joined_follow_up"] for _status, _headers, raw in replies] == [False, True, True]
        assert window() == 1
        # The harness finishes: one follow-up carries all three.
        _finish_running_jobs()
        jobs._launch_pending_feedback(run_id, owner_id=member_id, model=None, max_iterations=4)
        assert [item["message"] for item in threads.launched] == [
            "Try induction.\n\nAlso check n = 1.\n\nAnd n = 2."
        ]
        # Feedback for a finished run starts a continuation now.
        _finish_running_jobs()
        status, _headers, raw = chat("Continue.")
        assert status == 200 and json.loads(raw)["status"] == "continuing" and window() == 2
        # A refused message starts nothing.
        assert chat("   ")[0] == 400 and window() == 2


def test_free_feedback_never_starts_or_books_a_continuation_of_its_own(member_console, monkeypatch, tmp_path):
    """The free/paid decision is made where the message is queued, atomically.

    A message checked while feedback waits for a running run, but queued only
    after the run ended (its follow-up drained the queue) or that finds the
    run idle, books or starts a continuation and so pays for it.
    """
    threads = _record_continuations(monkeypatch)
    real_save = jobs.research_attachments.save
    meanwhile: dict = {}

    def save(workspace, batch):
        # send_human_message saves attachments before recording the message:
        # let the harness finish (and its follow-up start) in that window.
        action = meanwhile.pop("action", None)
        if action:
            action()
        return real_save(workspace, batch)

    monkeypatch.setattr(jobs.research_attachments, "save", save)
    with running_server() as port:
        user, cookie = _member(port)
        member_id = int(user["id"])
        auth.set_user_env(member_id, {"ANTHROPIC_API_KEY": "sk-ant-member-000000000000"})
        run_id = _member_run(monkeypatch, tmp_path, member_id)
        window = lambda: len(console._MEMBER_RUN_WINDOWS.get(member_id) or [])  # noqa: E731

        def chat(message: str) -> dict:
            status, _headers, raw = request(port, "POST", f"{APP_MOUNT}/api/runs/{run_id}/chat", body={"message": message}, headers={"Cookie": cookie})
            assert status == 200, raw
            return json.loads(raw)

        assert chat("First correction.")["status"] == "queued" and window() == 1

        # (a) The run ends while the message is in flight, and its follow-up
        # takes the queue: the message books the next follow-up and pays.
        def harness_finishes():
            _finish_running_jobs()
            jobs._launch_pending_feedback(run_id, owner_id=member_id, model=None, max_iterations=4)

        meanwhile["action"] = harness_finishes
        straddler = chat("Straddler.")
        assert straddler["status"] == "queued" and straddler["joined_follow_up"] is False
        assert window() == 2
        _finish_running_jobs()
        jobs._launch_pending_feedback(run_id, owner_id=member_id, model=None, max_iterations=4)
        assert [item["message"] for item in threads.launched] == ["First correction.", "Straddler."]

        # (b) The run ends while the message is in flight and nothing has
        # started yet: the message continues the run now, and pays.
        assert chat("Queued.")["status"] == "queued" and window() == 3
        meanwhile["action"] = _finish_running_jobs
        direct = chat("Direct.")
        assert direct["status"] == "continuing" and window() == 4
        # The finishing job's own follow-up finds the run busy; the feedback
        # it booked waits for the next turn.
        assert jobs._launch_pending_feedback(run_id, owner_id=member_id, model=None, max_iterations=4) is None
        assert jobs.feedback_pending(run_id)
        _finish_running_jobs()
        jobs._launch_pending_feedback(run_id, owner_id=member_id, model=None, max_iterations=4)
    # Every continuation was paid for by exactly one start.
    assert [item["message"] for item in threads.launched][2:] == ["Direct.", "Queued."]
    assert window() == 4


def test_member_feedback_is_rate_limited_and_bounded(member_console, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MONITOR_MEMBER_MESSAGES_PER_HOUR", "4")
    _record_continuations(monkeypatch)
    with running_server() as port:
        user, cookie = _member(port)
        member_id = int(user["id"])
        run_id = _member_run(monkeypatch, tmp_path, member_id)
        workspace = jobs.RUNS_DIR / "workspaces" / run_id

        def chat(message: str):
            status, _headers, raw = request(port, "POST", f"{APP_MOUNT}/api/runs/{run_id}/chat", body={"message": message}, headers={"Cookie": cookie})
            return status, json.loads(raw).get("error")

        too_long = chat("x" * (account_policy.MEMBER_FEEDBACK_MAX_CHARS + 1))
        queued = chat("First.")
        # Feedback already waiting for the running run is bounded...
        pending = workspace / jobs._PENDING_FEEDBACK_FILENAME
        saved = pending.read_text(encoding="utf-8")
        pending.write_text(saved + json.dumps({"content": "y" * account_policy.MEMBER_QUEUED_FEEDBACK_MAX_BYTES}) + "\n", encoding="utf-8")
        queue_full = chat("Second.")
        pending.write_text(saved, encoding="utf-8")
        # ...and so is all the feedback one run carries into every prompt.
        record = jobs._load_run_record(run_id)
        record["problems"] += [{"text": "z" * 20_000, "source": "human"}] * 5
        jobs._write_run(record)
        run_full = chat("Third.")
        limited = chat("Fourth.")

    assert too_long == (400, "Feedback is limited to 20,000 characters; shorten the message")
    assert queued == (200, None)
    assert queue_full[0] == 400 and "already waiting for this run" in queue_full[1]
    assert run_full[0] == 400 and "reached its feedback limit" in run_full[1]
    assert limited[0] == 429 and "hourly limit of run feedback" in limited[1]
    # Refused messages were never recorded or queued, and used no start.
    sent = [
        entry["content"] for entry in jobs.list_chat(run_id)
        if entry.get("role") == "user" and not entry.get("recovered_from_session")
    ]
    assert sent == ["First."]
    assert jobs._drain_human_feedback(workspace) == ["First."]
    assert len(console._MEMBER_RUN_WINDOWS.get(member_id) or []) == 1


def test_feedback_queued_behind_a_continuation_that_fails_still_starts(member_console, monkeypatch, tmp_path):
    """A refused Continue hands the run to feedback queued while it was prepared."""
    threads = _record_continuations(monkeypatch)
    real_route = jobs._continuation_auth_environment
    preparing, release = threading.Event(), threading.Event()

    def slow_route(**kwargs):
        if kwargs.get("model") == "gpt-x":
            preparing.set()
            release.wait(5)
        return real_route(**kwargs)

    monkeypatch.setattr(jobs, "_continuation_auth_environment", slow_route)
    with running_server() as port:
        user, cookie = _member(port)
        member_id = int(user["id"])
        # Two own keys, so an OpenAI model is not taken for one served by
        # the member's only provider.
        auth.set_user_env(member_id, {"ANTHROPIC_API_KEY": "sk-ant-member-000000000000",
                                      "DEEPSEEK_API_KEY": "sk-deepseek-member-0000000000"})
        run_id = _member_run(monkeypatch, tmp_path, member_id, running=False)
        results: dict = {}
        # A Continue on a model none of the member's keys serves is refused,
        # after a slow credential check.
        cont = threading.Thread(target=lambda: results.setdefault("continue", request(
            port, "POST", f"{APP_MOUNT}/api/runs/{run_id}/continue", body={"model": "gpt-x"},
            headers={"Cookie": cookie})))
        cont.start()
        assert preparing.wait(5)
        chat = request(port, "POST", f"{APP_MOUNT}/api/runs/{run_id}/chat",
                       body={"message": "Fix lemma 2."}, headers={"Cookie": cookie})
        release.set()
        cont.join(5)
        chat_log = [entry["content"] for entry in jobs.list_chat(run_id)]

    assert results["continue"][0] == 400
    assert "originally used by this run is unavailable" in json.loads(results["continue"][2])["error"]
    assert json.loads(chat[2])["status"] == "queued"
    # The queued feedback started on the run's recorded model and route.
    assert [(item["message"], item["model"]) for item in threads.launched] == [
        ("Fix lemma 2.", "claude-opus-4-8")
    ]
    assert not jobs.feedback_pending(run_id)
    assert any("now that the other continuation did not start" in line for line in chat_log)
    # The refused Continue gave its start back; the feedback kept its own.
    assert len(console._MEMBER_RUN_WINDOWS.get(member_id) or []) == 1


def test_feedback_queued_behind_a_failed_continuation_says_why_it_waits(monkeypatch, tmp_path):
    threads = _record_continuations(monkeypatch)
    monkeypatch.setattr("agent_monitor.auth.is_operator_account", lambda owner_id: owner_id == 1)
    run_id = _member_run(monkeypatch, tmp_path, 5, running=False)

    def unavailable(**_kwargs):
        raise ValueError("The API connection originally used by this run is unavailable")

    monkeypatch.setattr(jobs, "_continuation_auth_environment", unavailable)
    jobs._queue_human_feedback(jobs.RUNS_DIR / "workspaces" / run_id, "Fix lemma 2.", author_id=5)
    with pytest.raises(ValueError, match="unavailable"):
        jobs.continue_run(run_id, user={"id": 5, "is_admin": False})
    chat_log = [entry["content"] for entry in jobs.list_chat(run_id)]
    assert "Queued feedback could not start yet: The API connection originally used by this run is unavailable" in chat_log
    assert threads.launched == [] and not jobs.run_is_active(run_id)
    assert jobs._drain_human_feedback(jobs.RUNS_DIR / "workspaces" / run_id) == ["Fix lemma 2."]


def test_a_collaborators_queued_feedback_continues_as_that_collaborator(monkeypatch, tmp_path):
    """Queued feedback never runs as the run owner: each author pays for its own."""
    from agent_monitor import projects, settings

    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    for name in ("AGENT_MONITOR_PUBLIC", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    # The operator's key lives only in the server .env (production's layout).
    operator_key = "sk-operator-server-env-key-000000"
    settings.ENV_PATH.write_text(f"OPENAI_API_KEY={operator_key}\n", encoding="utf-8")
    threads = _record_continuations(monkeypatch)
    admin = auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    project = projects.create(admin, "Shared", "Prove it.")
    projects.join(projects.invite(project["id"], admin["id"]), member)
    run_id = _member_run(monkeypatch, tmp_path, int(admin["id"]))
    record = jobs._load_run_record(run_id)
    jobs._write_run({**record, "model": "gpt-6-astra", "requested_model": "gpt-6-astra"})
    projects.attach(project["id"], admin["id"], run_id, "task")
    assert projects.can_read_run(run_id, member["id"])

    # Both write while the administrator's run is running.
    assert jobs.send_human_message(run_id, "Collaborator note.", user=member)["status"] == "queued"
    assert jobs.send_human_message(run_id, "Owner note.", user=admin)["status"] == "queued"
    _finish_running_jobs()
    jobs._launch_pending_feedback(run_id, owner_id=admin["id"], model="gpt-6-astra", max_iterations=4)
    # The collaborator has no OpenAI key, so its feedback cannot start (and
    # never falls back to the operator's); the owner's own feedback can.
    launched = threads.launched
    assert [(item["owner_id"], item["message"]) for item in launched] == [(admin["id"], "Owner note.")]
    assert launched[0]["extra_env"]["OPENAI_API_KEY"] == operator_key
    chat_log = [entry["content"] for entry in jobs.list_chat(run_id)]
    assert "Queued feedback could not start yet: The API connection originally used by this run is unavailable" in chat_log
    assert jobs.feedback_pending(run_id)

    # With its own key, the collaborator's feedback runs on that key, as it.
    member_key = "sk-member-own-openai-key-00000000"
    auth.set_user_env(int(member["id"]), {"OPENAI_API_KEY": member_key})
    _finish_running_jobs()
    jobs._launch_pending_feedback(run_id, owner_id=admin["id"], model="gpt-6-astra", max_iterations=4)
    follow_up = launched[-1]
    assert (follow_up["owner_id"], follow_up["message"]) == (member["id"], "Collaborator note.")
    assert follow_up["extra_env"]["OPENAI_API_KEY"] == member_key
    assert not jobs.feedback_pending(run_id)


def test_queued_feedback_runs_one_author_at_a_time(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path)
    workspace = tmp_path / "workspaces" / "plain_run"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(jobs, "_run_is_active", lambda _run_id: None)
    monkeypatch.setattr(jobs, "_append_chat", lambda *_a, **_k: {})
    monkeypatch.setattr("agent_monitor.auth.is_operator_account", lambda owner_id: owner_id == 1)
    seen: list = []
    refuse = {7}

    def resume(run_id, **kwargs):
        if kwargs["user"]["id"] in refuse:
            raise ValueError("No key")
        seen.append((kwargs["user"], kwargs["message"]))
        return {"ok": True, "status": "continuing"}

    monkeypatch.setattr(jobs, "continue_run", resume)
    # Earlier feedback has no recorded author: it belongs to the job's owner.
    (workspace / jobs._PENDING_FEEDBACK_FILENAME).write_text(
        json.dumps({"content": "Legacy."}) + "\n", encoding="utf-8"
    )
    assert jobs._queue_human_feedback(workspace, "A.", author_id=7) is False
    assert jobs._queue_human_feedback(workspace, "B.", author_id=5) is False
    assert jobs._queue_human_feedback(workspace, "C.", author_id=7) is True
    # The owner's feedback is oldest; then member 7's is refused, so member
    # 5's starts and 7's waits, in order.
    jobs._launch_pending_feedback("plain_run", owner_id=1, model=None, max_iterations=4)
    jobs._launch_pending_feedback("plain_run", owner_id=1, model=None, max_iterations=4)
    assert seen == [({"id": 1, "is_admin": True}, "Legacy."), ({"id": 5, "is_admin": False}, "B.")]
    refuse.clear()
    jobs._launch_pending_feedback("plain_run", owner_id=1, model=None, max_iterations=4)
    assert seen[-1] == ({"id": 7, "is_admin": False}, "A.\n\nC.")
    assert not jobs.feedback_pending("plain_run")


def test_two_simultaneous_continues_start_one_continuation(monkeypatch, tmp_path):
    real_thread = threading.Thread
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path)
    (tmp_path / "workspaces" / "plain_run").mkdir(parents=True)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_LAUNCHING_RUNS", set())
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(jobs, "_load_run_record", lambda _run_id: {
        "run_id": "plain_run", "engine": "plain", "model": "gpt-6-astra", "auth_route": "api_key",
        "owner_id": 1, "problems": [{"text": "Prove it.", "source": "initial"}],
    })
    monkeypatch.setattr(jobs, "_append_chat", lambda *_a, **_k: {})
    monkeypatch.setattr(jobs, "list_chat", lambda _run_id: [])
    monkeypatch.setattr(jobs, "_write_run", lambda _run: None)
    monkeypatch.setattr(jobs, "_user_extra_env", lambda *_a, **_k: {})
    entered, release = threading.Event(), threading.Event()

    def slow_route(**_kwargs):
        # Credential and account checks can take seconds (a Claude status probe).
        entered.set()
        release.wait(5)
        return {}, "api_key"

    monkeypatch.setattr(jobs, "_continuation_auth_environment", slow_route)
    launched: list = []

    class FakeThread:
        def __init__(self, *, kwargs=None, **_rest):
            launched.append(kwargs)

        def start(self):
            return None

    monkeypatch.setattr(jobs.threading, "Thread", FakeThread)
    admin = {"id": 1, "is_admin": True}
    results: list = []
    first = real_thread(target=lambda: results.append(jobs.continue_run("plain_run", user=admin)))
    first.start()
    assert entered.wait(5)
    assert jobs.run_is_active("plain_run")
    second = jobs.continue_run("plain_run", user=admin)
    chat = jobs.send_human_message("plain_run", "Use induction.", user=admin)
    release.set()
    first.join(5)

    assert second == {"ok": False, "status": "already_running", "run_id": "plain_run"}
    # Feedback sent meanwhile is queued for the next turn, not a second run.
    assert chat["status"] == "queued"
    assert results[0]["status"] == "continuing"
    assert len(launched) == 1
    assert jobs.run_is_active("plain_run") and jobs._LAUNCHING_RUNS == set()
    assert jobs.feedback_pending("plain_run")


def test_a_continuation_that_cannot_start_releases_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path)
    (tmp_path / "workspaces" / "plain_run").mkdir(parents=True)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_LAUNCHING_RUNS", set())
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(jobs, "_load_run_record", lambda _run_id: {
        "run_id": "plain_run", "engine": "plain", "model": "gpt-6-astra", "auth_route": "codex_subscription",
        "owner_id": 1, "problems": [{"text": "Prove it.", "source": "initial"}],
    })
    monkeypatch.setattr(jobs, "_user_extra_env", lambda *_a, **_k: {})

    def disconnected(**_kwargs):
        raise ValueError("Codex is not connected for this account; connect it in Settings")

    monkeypatch.setattr(jobs, "_continuation_auth_environment", disconnected)
    with pytest.raises(ValueError, match="not connected"):
        jobs.continue_run("plain_run", user={"id": 1, "is_admin": True})
    assert not jobs.run_is_active("plain_run") and jobs._LAUNCHING_RUNS == set()


def test_member_continuations_keep_an_eligible_model_on_an_api_route():
    member = {"id": 5, "is_admin": False}
    record = {
        "run_id": "math_harness_run", "engine": "math_harness", "model": "claude-opus-4-8",
        "requested_model": "claude-opus-4-8", "auth_route": "api_key", "credential_source": "user",
    }
    pick = jobs._member_continuation_model
    # The chat composer may still hold the other CGM engine's model.
    assert pick(member, "math_harness", record, "kimi-k3") == "claude-opus-4-8"
    assert pick(member, "math_harness", record, "max") == "claude-opus-4-8"
    assert pick(member, "math_harness", record, "gpt-6-astra") == "gpt-6-astra"
    assert pick(member, "math_harness", record, None) == "claude-opus-4-8"
    kimi_record = {**record, "engine": "kimi", "model": "kimi-k3", "requested_model": "kimi-k3",
                   "credential_source": "sponsored_kimi"}
    assert pick(member, "kimi", kimi_record, "claude-opus-4-8") == "kimi-k3"
    # An administrator's explicit choice is unchanged.
    assert pick({"id": 1, "is_admin": True}, "math_harness", record, "kimi-k3") == "kimi-k3"
    # A run recorded on a subscription (an administrator's, shared through a
    # project) or on Max is never continued by a member, and says so.
    for route, message in (
        ("claude_subscription", "billed to a Claude subscription, which only the site administrator"),
        ("codex_subscription", "billed to a ChatGPT (Codex) subscription, which only the site administrator"),
        ("dual_subscription", "This run used Max"),
    ):
        with pytest.raises(ValueError, match=re.escape(message)) as refused:
            pick(member, "plain", {**record, "engine": "plain", "auth_route": route}, None)
        assert "only the administrator can continue it" in str(refused.value)


def test_queued_feedback_continues_with_the_owners_real_role(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path)
    workspace = tmp_path / "workspaces" / "plain_run"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(jobs, "_run_is_active", lambda _run_id: None)
    monkeypatch.setattr(jobs, "_append_chat", lambda *_a, **_k: {})
    monkeypatch.setattr("agent_monitor.auth.is_operator_account", lambda owner_id: owner_id == 1)
    seen: list = []
    monkeypatch.setattr(
        jobs, "continue_run",
        lambda run_id, **kwargs: seen.append(kwargs["user"]) or {"ok": True, "status": "continuing"},
    )
    for owner_id in (1, 5):
        jobs._queue_human_feedback(workspace, "Try induction.")
        jobs._launch_pending_feedback("plain_run", owner_id=owner_id, model=None, max_iterations=4)
    assert seen == [{"id": 1, "is_admin": True}, {"id": 5, "is_admin": False}]
    # Feedback whose follow-up lost the run to another continuation stays queued.
    monkeypatch.setattr(
        jobs, "continue_run",
        lambda run_id, **_kwargs: {"ok": False, "status": "already_running", "run_id": run_id},
    )
    jobs._queue_human_feedback(workspace, "Try induction.")
    jobs._launch_pending_feedback("plain_run", owner_id=5, model=None, max_iterations=4)
    assert jobs._drain_human_feedback(workspace) == ["Try induction."]


_GRAPH_JSON = json.dumps({
    "title": "Identity",
    "nodes": [{"id": "n1", "kind": "conclusion", "label": "identity", "statement": "1 = 1"}],
    "edges": [],
})


def _leftover_sign_ins(monkeypatch, tmp_path, *users) -> None:
    """Each account keeps a ChatGPT and a Claude Code sign-in on the server."""
    from agent_monitor import claude_login, codex_login

    monkeypatch.setenv("AGENT_MONITOR_CODEX_ACCOUNT_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("AGENT_MONITOR_CLAUDE_ACCOUNT_HOME", str(tmp_path / "claude"))
    monkeypatch.setattr(claude_login, "account_login_ready", lambda _home: True)
    for user in users:
        home = codex_login.account_home(int(user["id"]))
        home.mkdir(parents=True)
        (home / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "chatgpt-oauth"}}), encoding="utf-8"
        )


def test_subscription_routes_bind_only_an_administrators_sign_in(monkeypatch, tmp_path):
    admin = auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    _leftover_sign_ins(monkeypatch, tmp_path, admin, member)
    for route in ("codex_subscription", "claude_subscription", "dual_subscription"):
        configured = jobs._configure_auth_route({}, route=route, user_id=admin["id"])
        assert configured["AGENT_MONITOR_AUTH_MODE"] != "api_key"
        # A member's sign-in linked before the policy, and an account that no
        # longer exists, never reach a subscription route, whoever asks.
        for user_id in (member["id"], 999):
            with pytest.raises(ValueError, match="administrator"):
                jobs._configure_auth_route({}, route=route, user_id=user_id)


def test_a_members_leftover_sign_ins_never_carry_its_dag_or_formal_work(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent_monitor import auto_pipeline, codex_login, lean_verify, proof_graph
    from agent_monitor.runners import claude_backend

    admin = auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    _leftover_sign_ins(monkeypatch, tmp_path, admin, member)
    codex_calls: list[dict] = []
    claude_calls: list[dict] = []
    monkeypatch.setattr(
        proof_graph, "_run_codex_prompt_env",
        lambda env, *_args, **_kwargs: codex_calls.append(env) or _GRAPH_JSON,
    )
    monkeypatch.setattr(
        claude_backend, "claude_exec",
        lambda *_args, source_env, **_kwargs: claude_calls.append(source_env)
        or SimpleNamespace(text=_GRAPH_JSON, model="sonnet", usage=None, cost_usd=None),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "proof.md").write_text("Since 1 = 1, the claim holds.", encoding="utf-8")
    api_run = {"run_id": "plain_member", "owner_id": member["id"], "auth_route": "api_key",
               "model": "gpt-6-astra"}

    # Asking for Codex on the member's own API-key run.
    with pytest.raises(ValueError, match="administrator"):
        proof_graph.generate(workspace=workspace, run_record=api_run, user=member, model="codex")

    # A rebuild whose API call fails reports that failure; it never moves to Codex.
    def no_key(*_args, **_kwargs):
        raise ValueError("No API key configured")

    monkeypatch.setattr(proof_graph, "_call_llm", no_key)
    with pytest.raises(ValueError, match="No API key configured"):
        proof_graph.generate(workspace=workspace, run_record=api_run, user=member)

    # Runs recorded on a subscription before the policy, from the member's
    # session and from the automatic pipeline (whose owner has no role flag).
    for route in ("codex_subscription", "claude_subscription"):
        record = {**api_run, "auth_route": route}
        for user in (member, auto_pipeline._run_user(member["id"], record)):
            with pytest.raises(ValueError, match="administrator"):
                proof_graph.generate(workspace=workspace, run_record=record, user=user)
            with pytest.raises(ValueError, match="administrator"):
                lean_verify._llm_environment(user, record)
    # Formal Lean's Codex step (repair, questions and audit when no API answers).
    monkeypatch.setattr(
        "agent_monitor.engines_registry.build_cli_command", lambda *_args, **_kwargs: ["codex", "exec"]
    )
    monkeypatch.setattr(
        lean_verify.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("Formal Lean ran Codex on a member's sign-in"),
    )
    with pytest.raises(ValueError, match="administrator"):
        lean_verify._codex_subscription_interaction(
            workspace=workspace, user=member, run_record={**api_run, "auth_route": "codex_subscription"},
            prompt="Is the proof complete?", lean_source="theorem t : True := trivial", edit=False,
        )
    assert codex_calls == [] and claude_calls == []

    # The administrator's subscription runs, rebuilt the same way, keep their account.
    admin_run = {**api_run, "owner_id": admin["id"], "auth_route": "codex_subscription"}
    admin_owner = auto_pipeline._run_user(admin["id"], admin_run)
    proof_graph.generate(workspace=workspace, run_record=admin_run, user=admin_owner)
    assert codex_calls[0]["CODEX_HOME"] == str(codex_login.account_home(admin["id"]))
    env = lean_verify._llm_environment(admin_owner, admin_run)
    assert env["AGENT_MONITOR_CODEX_SUBSCRIPTION"] == "1"


def test_a_members_stored_runtime_never_selects_a_subscription_for_formal_work(
    monkeypatch, tmp_path
):
    from agent_monitor import lean_verify

    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    _leftover_sign_ins(monkeypatch, tmp_path, member)
    # Records from before runs pinned their route follow the stored runtime.
    unpinned = {"run_id": "plain_old", "owner_id": member["id"], "model": "gpt-6-astra"}
    for runtime in ("codex", "claude"):
        auth.set_user_env(int(member["id"]), {
            "AGENT_MONITOR_ACCOUNT_RUNTIME": runtime, "OPENAI_API_KEY": "sk-member-own-key",
        })
        env = lean_verify._llm_environment(member, unpinned)
        assert env["OPENAI_API_KEY"] == "sk-member-own-key"
        for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGENT_MONITOR_CODEX_SUBSCRIPTION",
                     "AGENT_MONITOR_CLAUDE_SUBSCRIPTION"):
            assert name not in env


def _fake_member_codex(monkeypatch, tmp_path: Path, member_id: int) -> Path:
    """A Codex sign-in the member linked before subscriptions became
    administrator-only, and a fake ``codex`` that only logs its calls."""
    accounts = tmp_path / "codex-accounts"
    home = accounts / f"user_{member_id}"
    home.mkdir(parents=True)
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "fake-not-a-secret"}}))
    monkeypatch.setenv("AGENT_MONITOR_CODEX_ACCOUNT_HOME", str(accounts))
    log = tmp_path / "codex-calls.log"
    fake = tmp_path / "fake-codex"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "CODEX_HOME=$CODEX_HOME $*" >> "{log}"\nexit 1\n')
    fake.chmod(0o755)
    monkeypatch.setenv(
        "CODEX_CMD",
        f"{fake} exec --json --cd {{workspace}} --sandbox danger-full-access "
        "--skip-git-repo-check {model_args} {prompt}",
    )
    return log


def test_member_lean_actions_never_use_a_subscription_or_the_codex_agent(member_console, monkeypatch, tmp_path):
    """A member's own run billed to Codex before subscriptions became
    administrator-only: Lean model actions on it are refused, so a failing
    model call can no longer fall back to the full-access Codex agent."""
    from agent_monitor import lean_verify

    workspace = tmp_path / "ws"
    (workspace / "lean").mkdir(parents=True)
    source = "theorem t : 1 + 1 = 2 := by sorry\n"
    (workspace / "lean" / "Proof.lean").write_text(source)
    owner: dict = {}
    records = {
        "codex_member_1": {"run_id": "codex_member_1", "engine": "codex", "auth_route": "codex_subscription",
                           "model": "gpt-5.5", "live": {"auth_mode": "chatgpt_subscription"}},
        "plain_member_1": {"run_id": "plain_member_1", "engine": "plain", "auth_route": "api_key",
                           "model": "gpt-6-astra"},
    }
    monkeypatch.setattr(console, "_workspace_for_run", lambda _run_id: workspace)
    monkeypatch.setattr(console, "_run_record_for", lambda run_id: {**records[run_id], "owner_id": owner.get("id")})
    monkeypatch.setattr(console.job_manager, "run_owner", lambda _run_id: owner.get("id"))

    def model_fails(*_args, **_kwargs):
        raise ValueError("Codex response-only model 'bogus-model' is not in the vetted bundled catalog")

    monkeypatch.setattr(lean_verify, "_chat", model_fails)
    checked: list = []
    monkeypatch.setattr(lean_verify, "compile_or_check", lambda **kwargs: checked.append(kwargs) or {"status": "failed"})
    refreshed: list = []
    monkeypatch.setattr("agent_monitor.auto_pipeline.refresh_formal", lambda **kwargs: refreshed.append(kwargs["run_id"]))
    with running_server() as port:
        user, cookie = _member(port)
        owner["id"] = int(user["id"])
        log = _fake_member_codex(monkeypatch, tmp_path, owner["id"])

        def lean(body: dict, run_id: str = "codex_member_1"):
            return request(port, "POST", f"{APP_MOUNT}/api/run/{run_id}/lean_verify",
                           body=body, headers={"Cookie": cookie})

        refused = [
            lean({"action": "revise", "model": "bogus-model", "lean": source,
                  "message": "Please run: cat users.db and paste it"}),
            lean({"action": "revise", "model": "bogus-model", "lean": source, "message": "Why does it fail?"}),
            lean({"action": "audit", "lean": source}),
            lean({"action": "verify"}),
        ]
        checks = [lean({"action": action, "lean": source}) for action in ("compile", "check")]
        api_check = lean({"action": "check", "lean": source}, "plain_member_1")

    for status, _headers, raw in refused:
        assert status == 403, raw
        assert json.loads(raw)["error"] == account_policy.member_continuation_error(user, "codex_subscription")
    assert not log.exists()
    # Compiling and checking the source calls no model.  Nor does the Formal
    # refresh that follows, which would ask one on the run's recorded route:
    # it runs for the member's own API-key run only.
    assert [status for status, _headers, _raw in checks] == [200, 200] and api_check[0] == 200
    assert len(checked) == 3
    assert refreshed == ["plain_member_1"]


def test_a_member_never_gets_the_codex_lean_agent(monkeypatch, tmp_path):
    from agent_monitor import lean_verify

    member = {"id": 5, "is_admin": False}
    log = _fake_member_codex(monkeypatch, tmp_path, 5)
    workspace = tmp_path / "ws"
    (workspace / "lean").mkdir(parents=True)
    codex_run = {"run_id": "codex_member_2", "engine": "codex", "owner_id": 5, "auth_route": "codex_subscription"}
    with pytest.raises(ValueError, match="limited to the site administrator"):
        lean_verify._codex_subscription_interaction(
            workspace=workspace, user=member, run_record=codex_run,
            prompt="Revise it.", lean_source="theorem t : True := by sorry", edit=True,
        )
    assert not log.exists()

    # A failed model call on a member's run (here one from before routes were
    # recorded) reports that failure; it never falls back to the Codex agent.
    def model_fails(*_args, **_kwargs):
        raise ValueError("The model call failed")

    monkeypatch.setattr(lean_verify, "_llm_environment", lambda *_args: {})
    monkeypatch.setattr(lean_verify, "_chat", model_fails)
    fallbacks: list = []
    monkeypatch.setattr(lean_verify, "_codex_subscription_interaction",
                        lambda **kwargs: fallbacks.append(kwargs) or {"model": "codex", "lean": "", "answer": ""})
    legacy = {"run_id": "plain_member_legacy", "engine": "plain", "owner_id": 5}
    for message in ("Use omega instead.", "Why does it fail?"):
        with pytest.raises(ValueError, match="The model call failed"):
            lean_verify.revise_with_feedback(
                workspace=workspace, user=member, run_record=legacy, message=message,
                lean="theorem t : 1 + 1 = 2 := by sorry\n",
            )
    assert fallbacks == []
