"""Full access: an administrator lets a member account link its own Claude
Code and Codex subscriptions and run every engine, including the harnesses
that run local tools (``account_policy.has_full_access``).  It never reaches
the administrator's subscriptions or keys, the member rate limits stay, and
admin pages stay the administrator's."""
from __future__ import annotations

import json

import pytest

from agent_monitor import account_policy, accounts_admin, auth, cgm_max, jobs
from agent_monitor import console_server as console  # noqa: F401  (the fixtures start it)

from test_analyze_console import _admin_cookie, _get, _post  # noqa: E402
from test_console_guest_access import (  # noqa: E402,F401  (fixture import)
    APP_MOUNT,
    guest_console,
    request,
    running_server,
)
from test_member_accounts import _member, member_console  # noqa: E402,F401


def _account(**extra):
    return {"id": 5, "email": "m@example.test", "is_admin": False, "guest": False, **extra}


def test_full_access_lifts_the_engine_and_subscription_rules_only():
    plain, full = _account(), _account(full_access=True)
    assert account_policy.is_member(full) and account_policy.has_full_access(full)
    assert not account_policy.is_restricted(full) and account_policy.is_restricted(plain)
    # Harnesses that run local tools, and the account's own subscriptions.
    assert account_policy.member_engine_error(plain, "codex")
    assert account_policy.member_engine_error(full, "codex") is None
    assert account_policy.member_engine_error(full, "openhands") is None
    for route in ("claude_subscription", "codex_subscription"):
        assert account_policy.member_route_error(plain, route)
        assert account_policy.member_route_error(full, route) is None
        assert account_policy.member_continuation_error(full, route) is None
    assert account_policy.subscription_policy(full) == {"claude": "available", "codex": "available"}
    assert account_policy.subscription_policy(plain) == {"claude": "admin_only", "codex": "admin_only"}
    # The account's default model, or any of its own; never the sponsored Kimi K3 in the CGM harness.
    assert account_policy.member_model_error(full, "math_harness", "") is None
    assert account_policy.member_model_error(full, "math_harness", "claude-opus-5-5") is None
    assert "Kimi K3" in account_policy.member_model_error(full, "math_harness", "kimi-k3")
    assert account_policy.member_run_model(full, "math_harness", None) is None
    # Never for a guest, and an administrator needs none.
    guest = {"id": 9, "guest": True, "full_access": True}
    assert not account_policy.has_full_access(guest) and not account_policy.is_restricted(guest)
    assert not account_policy.has_full_access({"id": 1, "is_admin": True, "full_access": True})


def test_max_runs_on_the_accounts_own_linked_subscriptions(member_console, monkeypatch):
    from agent_monitor import claude_login, codex_login

    ready = {}
    monkeypatch.setattr(claude_login, "account_login_ready", lambda home: ready.get(("claude", str(home)), False))
    monkeypatch.setattr(codex_login, "account_login_ready", lambda home: ready.get(("codex", str(home)), False))
    monkeypatch.setattr(claude_login, "available_models", lambda _uid: ["opus"])
    monkeypatch.setattr(codex_login, "available_models", lambda _uid: ["gpt-6-astra"])
    full, plain = _account(full_access=True), _account()
    assert "limited to the site administrator" in cgm_max.plan_for(plain)["reason"]
    assert "connect Claude Code and Codex" in cgm_max.plan_for(full)["reason"]
    ready[("claude", str(claude_login.account_home(5)))] = True
    ready[("codex", str(codex_login.account_home(5)))] = True
    plan = cgm_max.plan_for(full)
    assert plan["available"] and plan["advisor"]["model"] == "opus"
    assert account_policy.member_model_error(full, "math_harness", "max") is None
    assert account_policy.member_route_error(full, cgm_max.ROUTE) is None


def test_the_stored_flag_decides_which_subscriptions_a_run_may_use(member_console, monkeypatch, tmp_path):
    from agent_monitor import claude_login

    member = auth.register("m@example.test", "member-password-1")
    owner = next(a for a in accounts_admin.list_accounts() if a["is_admin"])
    homes = {}
    monkeypatch.setattr(claude_login, "account_login_ready", lambda home: True)
    monkeypatch.setattr(claude_login, "account_environment",
                        lambda home, env: homes.setdefault("home", str(home)) and {**env, "HOME": str(home)})
    with pytest.raises(ValueError, match="only for the site administrator"):
        jobs._configure_auth_route({}, route="claude_subscription", user_id=member["id"])
    assert not auth.may_use_own_subscriptions(member["id"])
    accounts_admin.set_full_access(["m@example.test"], on=True)
    assert auth.is_full_access_account(member["id"]) and auth.may_use_own_subscriptions(member["id"])
    env = jobs._configure_auth_route({}, route="claude_subscription", user_id=member["id"])
    # Its own linked account, never the administrator's.
    assert homes["home"] == str(claude_login.account_home(member["id"]))
    assert homes["home"] != str(claude_login.account_home(owner["id"]))
    assert env["AGENT_MONITOR_CLAUDE_SUBSCRIPTION"] == "1"
    # Taken back: refused again.
    accounts_admin.set_full_access(["m@example.test"], on=False)
    with pytest.raises(ValueError):
        jobs._configure_auth_route({}, route="claude_subscription", user_id=member["id"])
    assert auth.may_use_own_subscriptions(owner["id"]) and not auth.is_full_access_account(None)


def test_a_session_sees_the_flag_and_a_follow_up_keeps_it(member_console):
    accounts_admin.create_accounts(accounts_admin.parse_people("m@example.test"), full_access=True)
    member = next(a for a in accounts_admin.list_accounts() if a["email"] == "m@example.test")
    password = accounts_admin.new_password("m@example.test")
    _account_row, token = auth.login_and_create_session("m@example.test", password)
    assert auth.user_for_token(token)["full_access"] is True
    assert jobs._owner_user(member["id"]) == {"id": member["id"], "is_admin": False, "full_access": True}
    accounts_admin.set_full_access(["m@example.test"], on=False)
    # The next request already sees it taken back.
    assert auth.user_for_token(token)["full_access"] is False


def test_accounts_get_full_access_when_made_and_from_the_table(member_console):
    # Whoever registered an address first may not be the person on the list:
    # an existing account is never given full access by a list.
    auth.register("had-one@example.test", "had-one-password")
    people = accounts_admin.parse_people("had-one@example.test\nowner@example.test\nnew@example.test")
    preview = accounts_admin.create_accounts(people, preview=True, full_access=True)
    assert [(r["status"], r.get("full_access_skipped"), r.get("full_access")) for r in preview] == [
        ("exists", True, None), ("exists", None, None), ("new", None, True)]
    results = accounts_admin.create_accounts(people, full_access=True)
    assert [(r["status"], r.get("full_access_skipped"), r.get("full_access")) for r in results] == [
        ("exists", True, None), ("exists", None, None), ("created", None, True)]
    assert all("full_access_granted" not in r for r in preview + results)
    listed = {a["email"]: a["full_access"] for a in accounts_admin.list_accounts()}
    assert listed == {"owner@example.test": True, "had-one@example.test": False, "new@example.test": True}
    accounts_admin.set_full_access(["had-one@example.test"], on=True)
    listed = {a["email"]: a["full_access"] for a in accounts_admin.list_accounts()}
    assert listed == {"owner@example.test": True, "had-one@example.test": True, "new@example.test": True}
    changed = accounts_admin.set_full_access(["new@example.test", "owner@example.test", "nobody@example.test"], on=False)
    assert changed["updated"] == ["new@example.test"]
    assert {item["email"]: item["reason"] for item in changed["skipped"]} == {
        "owner@example.test": "An administrator always has full access",
        "nobody@example.test": "No account uses this email"}
    assert {a["email"]: a["full_access"] for a in accounts_admin.list_accounts()}["new@example.test"] is False


def test_the_admin_api_gives_and_takes_full_access(member_console):
    with running_server() as port:
        admin = _admin_cookie()
        created = _post(port, "/api/admin/accounts", admin, {"text": "a@example.test", "full_access": True})
        listed = _get(port, "/api/admin/accounts", admin)
        off = _post(port, "/api/admin/accounts/full-access", admin, {"emails": ["a@example.test"], "full_access": "off"})
        bad = _post(port, "/api/admin/accounts/full-access", admin, {"emails": ["a@example.test"], "full_access": True})
        _member_user, member_cookie = _member(port)
        refused = _post(port, "/api/admin/accounts/full-access", member_cookie,
                        {"emails": ["member@example.test"], "full_access": "on"})
        after = _get(port, "/api/admin/accounts", admin)
    assert created[0] == 200 and created[2]["results"][0]["full_access"] is True
    assert {a["email"]: a["full_access"] for a in listed[1]["accounts"]}["a@example.test"] is True
    assert off[0] == 200 and off[2]["updated"] == ["a@example.test"]
    assert bad[0] == 400 and refused[0] == 403
    assert {a["email"]: a["full_access"] for a in after[1]["accounts"]} == {
        "owner@example.test": True, "a@example.test": False, "member@example.test": False}


def test_a_full_access_member_may_link_subscriptions_but_never_sees_the_servers_keys(member_console, monkeypatch):
    from agent_monitor import claude_login, codex_login, settings

    monkeypatch.setattr(claude_login, "status", lambda uid: {"connected": False, "uid": uid, "real": True})
    monkeypatch.setattr(codex_login, "status", lambda uid: {"connected": False, "uid": uid, "real": True})
    settings.ENV_PATH.write_text("OPENAI_API_KEY=sk-server-secret-key-000000000000\n", encoding="utf-8")
    with running_server() as port:
        member, cookie = _member(port)
        restricted_status = request(port, "GET", f"{APP_MOUNT}/api/settings/claude/status", headers={"Cookie": cookie})
        accounts_admin.set_full_access(["member@example.test"], on=True)
        me = json.loads(request(port, "GET", f"{APP_MOUNT}/api/auth/me", headers={"Cookie": cookie})[2])
        full_status = request(port, "GET", f"{APP_MOUNT}/api/settings/claude/status", headers={"Cookie": cookie})
        codex_status = request(port, "GET", f"{APP_MOUNT}/api/settings/codex/status", headers={"Cookie": cookie})
        raw_settings = request(port, "GET", f"{APP_MOUNT}/api/settings", headers={"Cookie": cookie})[2]
        admin_page = _get(port, "/api/admin/accounts", cookie)
    assert "real" not in json.loads(restricted_status[2])
    assert me["user"]["full_access"] is True
    assert json.loads(full_status[2]) == {"connected": False, "uid": member["id"], "real": True}
    assert json.loads(codex_status[2])["real"] is True
    payload = json.loads(raw_settings)
    assert payload["subscription_policy"] == {"claude": "available", "codex": "available"}
    assert payload["subscription_notes"] == {}
    # Still a member: none of the server's keys, and no admin pages.
    assert "sk-server-secret" not in raw_settings
    assert admin_page[0] == 403


def test_full_access_keeps_the_member_run_limit(member_console, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_MEMBER_RUNS_PER_HOUR", "1")
    with running_server() as port:
        _member_user, cookie = _member(port)
        accounts_admin.set_full_access(["member@example.test"], on=True)
        statuses = []
        for _ in range(2):
            status, _headers, raw = request(port, "POST", f"{APP_MOUNT}/api/runs", headers={"Cookie": cookie},
                                            body={"problem": "Show that 1 + 1 = 2.", "engine": "kimi"})
            statuses.append(status)
    assert statuses[0] == 200, raw
    assert statuses[1] == 429


def test_the_cli_gives_and_takes_full_access(member_console, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(accounts_admin, "_load_site_environment", lambda: None)
    path = tmp_path / "people.txt"
    path.write_text("Ada - ada@example.org\n", encoding="utf-8")
    assert accounts_admin.main(["create", str(path), "--full-access"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["full_access"] is True
    assert accounts_admin.main(["list"]) == 0
    assert "ada@example.org  Ada  (full access)" in capsys.readouterr().out
    assert accounts_admin.main(["full-access", "off", "ada@example.org"]) == 0
    assert accounts_admin.main(["full-access", "on", "owner@example.test"]) == 1
    capsys.readouterr()
    assert {a["email"]: a["full_access"] for a in accounts_admin.list_accounts()}["ada@example.org"] is False


def test_hermes_stays_with_the_administrator_even_with_full_access(member_console):
    full, plain = _account(full_access=True), _account()
    assert "inside the console process" in account_policy.member_engine_error(full, "hermes")
    assert account_policy.member_engine_error(plain, "hermes")
    assert account_policy.member_engine_error({"id": 1, "is_admin": True}, "hermes") is None
    with running_server() as port:
        _member_user, cookie = _member(port)
        accounts_admin.set_full_access(["member@example.test"], on=True)
        status, _headers, raw = request(port, "GET", f"{APP_MOUNT}/api/engines", headers={"Cookie": cookie})
    assert status == 200
    engines = {engine["id"]: engine for engine in json.loads(raw)["engines"]}
    assert engines["hermes"]["requires_admin"] is True and engines["hermes"]["health"] == "admin_only"
    assert "inside the console" in engines["hermes"]["hint"]
    # The coding harnesses are its to run (whether set up on this host or not).
    for engine_id in ("codex", "claude", "openhands"):
        if engine_id in engines:
            assert not engines[engine_id].get("requires_admin"), engines[engine_id]


def test_a_full_access_member_never_reaches_sponsored_kimi_outside_the_included_engines(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from agent_monitor import auto_pipeline, sponsored_kimi_client

    for name in ("AGENT_MONITOR_PUBLIC", "AGENT_MONITOR_MEMBER_ENGINES", "MATH_HARNESS_CMD", "PLAIN_CMD",
                 "OPENAI_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    accounts_admin.set_full_access(["member@example.test"], on=True)
    user = {**member, "full_access": True}
    # A healthy sponsored broker, and a new account's saved default (Kimi K3).
    monkeypatch.setattr(sponsored_kimi_client, "supports_engine", lambda _engine: True)
    monkeypatch.setattr(sponsored_kimi_client, "configured", lambda: True)
    monkeypatch.setattr(sponsored_kimi_client, "endpoint", lambda: "http://127.0.0.1:9/v1")
    monkeypatch.setattr(sponsored_kimi_client, "issue_credentials",
                        lambda **_kwargs: {"KIMI_API_KEY": "sponsored-session", "KIMI_BASE_URL": "http://127.0.0.1:9/v1"})
    monkeypatch.setattr(jobs, "workspace_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(jobs, "_write_run", lambda _run: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    started = []

    class FakeThread:
        def __init__(self, *, kwargs=None, **_rest):
            started.append(kwargs or {})

        def start(self):
            return None

    monkeypatch.setattr(jobs.threading, "Thread", FakeThread)
    monkeypatch.setattr(auto_pipeline, "start", lambda **_kwargs: None)
    from agent_monitor import engines_registry

    monkeypatch.setattr(engines_registry, "list_engines", lambda: [
        {"id": engine, "available": True, "auth_modes": ["api_key", "codex_subscription", "claude_subscription"]}
        for engine in ("math_harness", "codex", "kimi", "plain")])
    for engine, model in (("math_harness", None), ("codex", "kimi-k3"), ("math_harness", "kimi-k3")):
        with pytest.raises(ValueError, match="Kimi K3 runs on the included"):
            jobs.start_job(engine=engine, problem_text="Prove it.", model=model, user=user)
    assert started == []
    # The included engine still serves it, as it serves every member.
    job = jobs.start_job(engine="kimi", problem_text="Prove it.", user=user)
    assert job["engine"] == "kimi" and started[-1]["extra_env"].get("AGENT_MONITOR_SPONSORED_KIMI") == "1"


def test_a_full_access_member_keeps_and_verifies_its_own_endpoint(member_console, monkeypatch):
    from agent_monitor import settings

    for name in settings._account_base_vars():
        monkeypatch.delenv(name, raising=False)
    urls = []
    monkeypatch.setattr(settings, "_http_json",
                        lambda url, **_kwargs: urls.append(url) or (200, {"data": [{"id": "gpt-6-astra"}]}))
    full = auth.register("full@example.test", "full-password-1")
    plain = auth.register("plain@example.test", "plain-password-1")
    accounts_admin.set_full_access(["full@example.test"], on=True)
    endpoint = {"OPENAI_API_KEY": "sk-own-openai-123456789", "OPENAI_BASE_URL": "https://gateway.example.test/v1"}
    for account in (full, plain):
        auth.set_user_env(int(account["id"]), endpoint)
    full_user, plain_user = {**full, "full_access": True}, dict(plain)
    assert settings.restrict_account_base_urls(dict(endpoint), full_user)["OPENAI_BASE_URL"] == endpoint["OPENAI_BASE_URL"]
    assert "OPENAI_BASE_URL" not in settings.restrict_account_base_urls(dict(endpoint), plain_user)
    assert settings.verify_provider("openai", user=full_user)["ok"] is True
    assert urls == ["https://gateway.example.test/v1/models"]
    assert "only by the operator" in settings.verify_provider("openai", user=plain_user)["error"]


def test_analyze_and_readability_offer_a_full_access_account_its_own_subscriptions(member_console):
    from agent_monitor import analyze_runs, readability_runs

    full, plain = _account(full_access=True), _account()
    for policy in (analyze_runs.viewer_policy, readability_runs.viewer_policy):
        assert "claude_subscription" in policy(full)["routes"] and "codex_subscription" in policy(full)["routes"]
        assert policy(plain)["routes"] == ["sponsored_kimi", "api_key"]


# ── On by default ────────────────────────────────────────────────────────────

def _full(email):
    return {a["email"]: a["full_access"] for a in accounts_admin.list_accounts()}[email]


def test_every_new_member_account_starts_with_full_access(member_console, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_FULL_ACCESS_DEFAULT", "1")
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    assert auth.full_access_by_default()
    member = auth.register("signup@example.test", "signup-password-1")
    assert member["full_access"] is True and auth.may_use_own_subscriptions(member["id"])
    guest, _token = auth.create_guest_session()
    assert guest["full_access"] is False and not auth.is_full_access_account(guest["id"])
    # Taken back from the table, it stays taken back.
    accounts_admin.set_full_access(["signup@example.test"], on=False)
    assert not auth.is_full_access_account(member["id"])
    # Off: new accounts start restricted again; nobody loses it.
    monkeypatch.setenv("AGENT_MONITOR_FULL_ACCESS_DEFAULT", "0")
    assert not auth.full_access_by_default()
    assert auth.register("later@example.test", "later-password-1")["full_access"] is False


def test_a_google_sign_up_starts_with_full_access(member_console, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm
    import jwt
    import time

    monkeypatch.setenv("AGENT_MONITOR_FULL_ACCESS_DEFAULT", "1")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    monkeypatch.setattr(auth, "_google_jwks", lambda: {"keys": [jwk]})
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-123.apps.googleusercontent.com")
    now = int(time.time())
    token = jwt.encode({"iss": "https://accounts.google.com", "aud": "client-123.apps.googleusercontent.com",
                        "sub": "g-1", "email": "g@example.test", "email_verified": True, "name": "G",
                        "iat": now, "exp": now + 300}, key, algorithm="RS256", headers={"kid": "test-key"})
    assert auth.login_with_google(token)["full_access"] is True


def test_existing_members_get_full_access_once(member_console, monkeypatch):
    # Accounts made before this release (default off) get it at the first
    # start with the default on, and only then.
    member = auth.register("old@example.test", "old-password-1")
    assert member["full_access"] is False
    guest, _token = auth.create_guest_session()
    monkeypatch.setenv("AGENT_MONITOR_FULL_ACCESS_DEFAULT", "1")
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    assert auth.is_full_access_account(member["id"]) and not auth.is_full_access_account(guest["id"])
    accounts_admin.set_full_access(["old@example.test"], on=False)
    monkeypatch.setattr(auth, "_INITIALIZED", False)  # the next start
    assert not auth.is_full_access_account(member["id"])


def test_adding_accounts_follows_the_box_and_otherwise_the_default(member_console, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_FULL_ACCESS_DEFAULT", "1")
    off = accounts_admin.create_accounts(accounts_admin.parse_people("off@example.test"), full_access=False)
    assert off[0]["status"] == "created" and not off[0].get("full_access") and _full("off@example.test") is False
    default = accounts_admin.create_accounts(accounts_admin.parse_people("d@example.test"))
    assert default[0].get("full_access") is True and _full("d@example.test") is True
    preview = accounts_admin.create_accounts(accounts_admin.parse_people("p@example.test"), preview=True)
    assert preview[0]["status"] == "new" and preview[0].get("full_access") is True
    with running_server() as port:
        admin = _admin_cookie()
        _post(port, "/api/admin/accounts", admin, {"text": "api-off@example.test", "full_access": False})
        _post(port, "/api/admin/accounts", admin, {"text": "api-default@example.test"})
    assert _full("api-off@example.test") is False and _full("api-default@example.test") is True
