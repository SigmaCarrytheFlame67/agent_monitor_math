"""Which engines run Kimi K3 as their only Kimi model, and which run any.

Plain, the two Ansatz CGM engines, Meta-Harness, Analyze and Readability
make their Kimi calls through ``api_backend.api_chat``, which asks for Kimi
K3 whatever Kimi model is named (``kimi_k3.chat_payload``); DeepSeek Harness
and IMProof run Kimi K3 for ``kimi-k3`` alone (another Kimi name runs a model
that is no Kimi model).  A run of theirs on another Kimi model (kimi-k2.5
typed as a custom model) would be recorded, shown and billed under a name it
never ran on: it is refused, on every route, before anything is projected or
launched, and so is a follow-up of a run recorded on one.  The coding-agent
harnesses (Codex CLI, DeepAgents, OpenHands, OpenClaw, OpenClaude, Hermes)
send the model they are given to an account's own Kimi endpoint, so they run
another Kimi model there exactly as before; only the sponsored service asks
for Kimi K3 whatever any engine names.  (Analyze and Readability:
tests/test_readability_console.py.)  Offline: nothing is launched, no
provider is called.
"""
from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

import pytest

from agent_monitor import account_policy, engines_registry, jobs, kimi_k3, settings
from agent_monitor.runners import api_backend

ADMIN = {"id": 91, "email": "owner@example.test", "is_admin": True}
FULL_ACCESS = {"id": 92, "email": "full@example.test", "is_admin": False, "full_access": True}
MEMBER = {"id": 93, "email": "member@example.test", "is_admin": False}
OWN_KIMI = {"KIMI_API_KEY": "sk-own-kimi-" + "k" * 24}
OWN_OPENAI = {"OPENAI_API_KEY": "sk-own-openai-" + "o" * 24}
OTHER_KIMI_MODELS = ("kimi-k2.5", " Kimi-K2.5 ", "kimi:kimi-k2.5", "KIMI : Kimi-K2-Thinking", "kimi-k3-preview")
# Each engine that runs Kimi K3 alone, and its refusal of another Kimi model.
KIMI_K3_ONLY = {
    "plain": "Plain (no harness) calls Kimi K3 for every Kimi model name; choose kimi-k3",
    "math_harness": "Ansatz CGM harness calls Kimi K3 for every Kimi model name; choose kimi-k3",
    "metaharness": "Meta-Harness calls Kimi K3 for every Kimi model name; choose kimi-k3",
    "deepseek_harness": "DeepSeek Harness runs Kimi K3 as its only Kimi model; choose kimi-k3",
    "improof": "IMProof runs Kimi K3 as its only Kimi model; choose kimi-k3",
}
PASS_THROUGH = ("codex", "deepagents", "openhands", "openclaw", "openclaude", "hermes")
SPONSORED_REFUSAL = ("The sponsored Kimi service calls Kimi K3 for every Kimi model name; choose kimi-k3, "
                     "or add your own Kimi API key in Settings")


@pytest.fixture()
def starts(monkeypatch, tmp_path):
    """start_job with every engine available on its own routes, a healthy
    sponsored service whose credentials are recorded, the account's keys in
    ``state.keys``, and no worker: what was projected, issued and launched
    is kept."""
    from agent_monitor import auto_pipeline, sponsored_kimi_client

    for name in ("AGENT_MONITOR_PUBLIC", "AGENT_MONITOR_MEMBER_ENGINES", "PLAIN_CMD", "MATH_HARNESS_CMD", "KIMI_API_KEY",
                 "OPENAI_API_KEY", "AGENT_MONITOR_KIMI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    state = SimpleNamespace(projected=[], issued=[], threads=[], keys={})
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: state.projected.append(models) or {
        "AGENT_MONITOR_PYTHON": "python", **state.keys})
    monkeypatch.setattr(sponsored_kimi_client, "configured", lambda: True)
    monkeypatch.setattr(sponsored_kimi_client, "endpoint", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(sponsored_kimi_client, "issue_credentials", lambda **kwargs: state.issued.append(kwargs) or {
        "KIMI_API_KEY": "sponsored-session-" + "s" * 20, "KIMI_API_BASE": "http://127.0.0.1:9/internal/v1",
        "AGENT_MONITOR_SPONSORED_KIMI": "1", "AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:9"})
    monkeypatch.setattr(jobs, "workspace_dir", lambda run_id: (tmp_path / run_id).mkdir(exist_ok=True) or tmp_path / run_id)
    monkeypatch.setattr(jobs, "_write_run", lambda _run: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(jobs.threading, "Thread",
                        lambda **kwargs: state.threads.append(kwargs) or SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(auto_pipeline, "start", lambda **_kwargs: None)
    monkeypatch.setattr(engines_registry, "list_engines", lambda: [
        {"id": engine, "label": engine, "available": True, "auth_modes": modes}
        for engine, modes in engines_registry.ENGINE_AUTH_MODES.items()])
    return state


def test_the_engine_listing_says_which_engines_run_kimi_k3_alone():
    """The console's menus offer no other Kimi model for these engines
    (console.html modelsForEngine reads the flag)."""
    flagged = {engine["id"] for engine in engines_registry.list_engines() if engine.get("kimi_k3_only")}
    listed = {engine["id"] for engine in engines_registry.list_engines()}
    assert flagged == engines_registry.KIMI_K3_ONLY_ENGINE_IDS & listed
    assert engines_registry.KIMI_K3_ONLY_ENGINE_IDS == {*KIMI_K3_ONLY, "kimi", "analyze", "readability"}
    assert not engines_registry.KIMI_K3_ONLY_ENGINE_IDS & set(PASS_THROUGH)


@pytest.mark.parametrize("engine", sorted(KIMI_K3_ONLY))
@pytest.mark.parametrize("user", [ADMIN, FULL_ACCESS], ids=["admin", "full-access"])
@pytest.mark.parametrize("keys", [{}, OWN_KIMI], ids=["no-kimi-key", "own-kimi-key"])
def test_another_kimi_model_is_refused_by_the_engines_that_run_kimi_k3_alone(starts, engine, user, keys):
    """On the account's own Kimi key (asked for Kimi K3 under the other name,
    or running another model) and without one (the sponsored route, which
    runs Kimi K3 alone): refused, saying why, before the account's keys are
    projected, a sponsored credential is issued or anything is launched."""
    starts.keys = keys
    for model in OTHER_KIMI_MODELS:
        with pytest.raises(ValueError) as refused:
            jobs.start_job(engine=engine, problem_text="Prove it.", model=model, user=user, auth_route="api_key")
        assert str(refused.value) == KIMI_K3_ONLY[engine], model
    assert (starts.projected, starts.issued, starts.threads) == ([], [], [])


def test_a_restricted_members_plain_run_on_another_kimi_model_is_refused(starts):
    starts.keys = OWN_KIMI
    for model in OTHER_KIMI_MODELS:
        with pytest.raises(ValueError, match=f"^{re.escape(KIMI_K3_ONLY['plain'])}$"):
            jobs.start_job(engine="plain", problem_text="Prove it.", model=model, user=MEMBER)
    assert (starts.projected, starts.issued, starts.threads) == ([], [], [])


def test_kimi_k3_however_it_is_named_still_starts(starts):
    starts.keys = OWN_KIMI
    for model in ("kimi-k3", "Kimi-K3", "kimi:kimi-k3"):
        job = jobs.start_job(engine="plain", problem_text="Prove it.", model=model, user=ADMIN, auth_route="api_key")
        assert job["credential_source"] == "user", model
    starts.keys = {}
    job = jobs.start_job(engine="plain", problem_text="Prove it.", model="kimi-k3", user=ADMIN, auth_route="api_key")
    assert job["credential_source"] == "sponsored_kimi"
    assert len(starts.threads) == 4


# What start_job handed the worker on HEAD 0d7ca50 for each coding-agent
# harness started on kimi-k2.5 with an own Kimi key (and no Kimi endpoint of
# the account's): the run's environment, and what the runner's environment
# names once the selection is applied (jobs._apply_selected_model_env).
HEAD_EXTRA_ENV = {
    "AGENT_MONITOR_ACCOUNT_RUNTIME": "api", "AGENT_MONITOR_AUTH_MODE": "api_key", "AGENT_MONITOR_PYTHON": "python",
    "AGENT_MONITOR_RUN_REASONING_EFFORT": "default", "AGENT_MONITOR_RUN_SPEED_MODE": "standard",
    "AGENT_MONITOR_USE_CLAUDE": "0", "AGENT_MONITOR_USE_CODEX": "0", "AGENT_MONITOR_USE_SUBAGENTS": "1",
    **OWN_KIMI,
}
HEAD_RUNNER_ENV = {
    "AGENT_MONITOR_SELECTED_MODEL": "kimi-k2.5", "AGENT_MONITOR_OPENAI_MODEL": "kimi-k2.5",
    "OPENAI_API_KEY": OWN_KIMI["KIMI_API_KEY"], "LLM_API_KEY": OWN_KIMI["KIMI_API_KEY"],
    "OPENAI_BASE_URL": kimi_k3.DEFAULT_BASE_URL, "OPENAI_API_BASE": kimi_k3.DEFAULT_BASE_URL,
    "AGENT_MONITOR_BASE_URL": kimi_k3.DEFAULT_BASE_URL, "LLM_BASE_URL": kimi_k3.DEFAULT_BASE_URL,
}
HEAD_HARNESS_MODEL = {
    "codex": {}, "hermes": {},
    "deepagents": {"DEEPAGENTS_MODEL": "openai:kimi-k2.5"},
    "openhands": {"AGENT_MONITOR_OPENHANDS_MODEL": "openai/kimi-k2.5", "LLM_MODEL": "openai/kimi-k2.5"},
    "openclaw": {"AGENT_MONITOR_OPENCLAW_MODEL": "openai/kimi-k2.5"},
    "openclaude": {"AGENT_MONITOR_OPENCLAUDE_MODEL": "kimi-k2.5"},
}


@pytest.mark.parametrize("engine", PASS_THROUGH)
@pytest.mark.parametrize("user", [ADMIN, FULL_ACCESS], ids=["admin", "full-access"])
def test_a_coding_agent_harness_starts_another_kimi_model_on_an_own_kimi_key_as_before(starts, engine, user):
    """These harnesses send the model they are given to the account's own
    Kimi endpoint, so kimi-k2.5 runs there as kimi-k2.5: the start is the one
    HEAD made, its record, route and environment unchanged (no refusal, no
    Kimi K3, no operator endpoint put in)."""
    starts.keys = OWN_KIMI
    job = jobs.start_job(engine=engine, problem_text="Prove it.", model="kimi-k2.5", user=user, auth_route="api_key")
    assert (job["model"], job["credential_source"], job["auth_route"], job["engine"]) == (
        "kimi-k2.5", "user", "api_key", engine)
    kwargs = starts.threads[0]["kwargs"]
    assert (kwargs["model"], kwargs["engine"], kwargs["run_kind"]) == ("kimi-k2.5", engine, "prove")
    assert kwargs["extra_env"] == HEAD_EXTRA_ENV
    runner_env = dict(kwargs["extra_env"])
    jobs._apply_selected_model_env(runner_env, engine=engine, model=kwargs["model"])
    assert runner_env == {**HEAD_EXTRA_ENV, **HEAD_RUNNER_ENV, **HEAD_HARNESS_MODEL[engine]}
    assert starts.issued == []


def test_the_sponsored_route_runs_kimi_k3_for_every_engine(starts):
    """Without a Kimi key of its own, a coding-agent harness would reach the
    sponsored service, which asks for Kimi K3 whatever is named: another Kimi
    model is refused there before a credential is issued (an administrator's
    run; a member reaches the service only through the included engines, as
    before)."""
    for engine in ("codex", "deepagents", "openhands", "openclaw", "openclaude"):
        for model in ("kimi-k2.5", "Kimi-K2.5", "kimi-k3-preview"):
            with pytest.raises(ValueError) as refused:
                jobs.start_job(engine=engine, problem_text="Prove it.", model=model, user=ADMIN, auth_route="api_key")
            assert str(refused.value) == SPONSORED_REFUSAL, (engine, model)
        with pytest.raises(ValueError, match="^Kimi K3 runs on the included Ansatz CGM · Kimi K3 engine; for this "
                                             "engine choose a model"):
            jobs.start_job(engine=engine, problem_text="Prove it.", model="kimi-k2.5", user=FULL_ACCESS,
                           auth_route="api_key")
    # Hermes never had the sponsored route.
    with pytest.raises(ValueError, match="^Server-sponsored Kimi K3 is not available with the hermes harness"):
        jobs.start_job(engine="hermes", problem_text="Prove it.", model="kimi-k2.5", user=ADMIN, auth_route="api_key")
    assert (starts.issued, starts.threads) == ([], [])
    job = jobs.start_job(engine="deepagents", problem_text="Prove it.", model="kimi-k3", user=ADMIN,
                         auth_route="api_key")
    assert (job["model"], job["credential_source"]) == ("kimi-k3", "sponsored_kimi")


def _recorded_run(engine: str, model: str, *, sponsored: bool = False) -> str:
    run_id = f"{engine}_kimi_{uuid.uuid4().hex[:8]}"
    workspace = jobs.RUNS_DIR / "workspaces" / run_id
    workspace.mkdir(parents=True)
    (workspace / "proof.md").write_text("# Proof\n", encoding="utf-8")
    jobs._write_run({"run_id": run_id, "engine": engine, "status": "finished", "model": model,
                     "requested_model": model, "owner_id": ADMIN["id"], "auth_route": "api_key",
                     "credential_source": "sponsored_kimi" if sponsored else "user", "workspace": str(workspace),
                     "problem_text": "Prove it."})
    return run_id


def test_a_follow_up_keeps_the_kimi_rule_of_its_engine(monkeypatch):
    """A Plain run started on kimi-k2.5 before such models were refused ran
    as Kimi K3 under that name, and a follow-up would too: refused before the
    message is recorded or anything launched, with the launch claim given
    back.  A follow-up that names another Kimi model for its Kimi K3 run
    stays on Kimi K3.  A coding-agent harness's own-key run continues on the
    Kimi model it names, as before; a sponsored one would run as Kimi K3."""
    monkeypatch.setattr(jobs, "_user_extra_env", lambda user, *models: {"AGENT_MONITOR_PYTHON": "python", **OWN_KIMI})
    launched: list = []
    monkeypatch.setattr(jobs.threading, "Thread",
                        lambda **kwargs: launched.append(kwargs) or SimpleNamespace(start=lambda: None))
    run_id = _recorded_run("plain", "kimi-k2.5")
    refusal = (r"^Plain \(no harness\) calls Kimi K3 for every Kimi model name; this run ran on kimi-k2\.5, "
               r"so it cannot continue: start a new run on kimi-k3$")
    with pytest.raises(ValueError, match=refusal):
        jobs.send_human_message(run_id, "Check step 2.", user=ADMIN)
    for _ in range(2):  # the claim is given back each time
        with pytest.raises(ValueError, match=refusal):
            jobs.continue_run(run_id, message="Check step 2.", user=ADMIN)
    assert jobs.list_chat(run_id) == [] and launched == [] and not jobs.run_is_active(run_id)
    record = jobs._load_run_record(run_id)
    for requested in ("kimi-k2.5", "kimi:kimi-k2.5"):
        assert jobs._member_continuation_model(ADMIN, "plain", {**record, "model": "kimi-k3", "requested_model": "kimi-k3"},
                                               requested) == "kimi-k3"
    # A coding-agent harness: as named, on the own key it started on.
    harness = jobs._load_run_record(_recorded_run("deepagents", "kimi-k2.5"))
    assert jobs._member_continuation_model(ADMIN, "deepagents", harness, None) == "kimi-k2.5"
    assert jobs._member_continuation_model(ADMIN, "deepagents", {**harness, "model": "kimi-k3", "requested_model": "kimi-k3"},
                                           "kimi-k2.5") == "kimi-k2.5"
    # ... but not on the sponsored route.
    sponsored = jobs._load_run_record(_recorded_run("deepagents", "kimi-k2.5", sponsored=True))
    with pytest.raises(ValueError, match="^The sponsored Kimi service calls Kimi K3 for every Kimi model name; "
                                         r"this run ran on kimi-k2\.5, so it cannot continue"):
        jobs._member_continuation_model(ADMIN, "deepagents", sponsored, None)
    assert jobs._member_continuation_model(ADMIN, "deepagents", {**sponsored, "model": "kimi-k3", "requested_model":
                                                                 "kimi-k3"}, "kimi-k2.5") == "kimi-k3"


@pytest.fixture()
def saved_default(starts, monkeypatch):
    """An administrator's (or full-access account's) saved settings: the
    model it saved and the keys it holds, as Settings and the start read
    them."""
    saved: dict = {}

    def save(model: str, keys: dict, *, sponsored: bool = True):
        from agent_monitor import sponsored_kimi_client

        saved.clear()
        saved.update({"AGENT_MONITOR_MODEL": model, "AGENT_MONITOR_ACCOUNT_RUNTIME": "api", **keys})
        starts.keys = dict(keys)
        monkeypatch.setattr(sponsored_kimi_client, "configured", lambda: sponsored)
        monkeypatch.setattr(settings, "sponsored_kimi_available", lambda: sponsored)

    monkeypatch.setattr(settings, "resolved_user_env", lambda _user: dict(saved))
    return save


@pytest.mark.parametrize("engine", ["plain", "metaharness", "deepseek_harness", "improof", "math_harness"])
def test_a_blank_proof_takes_a_saved_other_kimi_model_as_kimi_k3_where_the_account_runs_it(starts, saved_default,
                                                                                         engine):
    """A request without a model (the Projects page's "Default account
    model", or an API caller's) whose saved default is kimi-k2.5, on an
    engine that would run it as Kimi K3: Kimi K3 where the account runs it
    (its own Kimi key, or the sponsored service), else the first model of
    its keys that runs; never a model it cannot run.  (HEAD started it as
    kimi-k2.5, running Kimi K3 under that name; passing the default over
    started Plain with no model, on its built-in gpt-5.2, which a
    Kimi-key-only account cannot run.)"""
    from agent_monitor import sponsored_kimi_client

    started = lambda: (starts.threads[-1]["kwargs"]["model"], starts.threads[-1]["kwargs"]["extra_env"])  # noqa: E731
    # Its own Kimi key: Kimi K3 on it.
    saved_default("kimi-k2.5", OWN_KIMI)
    job = jobs.start_job(engine=engine, problem_text="Prove it.", model=None, user=ADMIN, auth_route="api_key")
    assert (job["model"], job["credential_source"]) == ("kimi-k3", "user")
    assert started()[0] == "kimi-k3" and started()[1]["KIMI_API_KEY"] == OWN_KIMI["KIMI_API_KEY"]
    if sponsored_kimi_client.supports_engine(engine):
        # The sponsored service, while it runs: Kimi K3 there.
        saved_default("kimi-k2.5", OWN_OPENAI)
        job = jobs.start_job(engine=engine, problem_text="Prove it.", model=None, user=ADMIN, auth_route="api_key")
        assert (job["model"], job["credential_source"]) == ("kimi-k3", "sponsored_kimi")
    # Neither: the first model of the account's keys.
    saved_default("kimi-k2.5", OWN_OPENAI, sponsored=False)
    job = jobs.start_job(engine=engine, problem_text="Prove it.", model=None, user=ADMIN, auth_route="api_key")
    assert (job["model"], job["credential_source"]) == ("gpt-6-astra", "user")
    assert started()[0] == "gpt-6-astra"
    # Nothing it runs: refused, saying what is missing; nothing launched.
    saved_default("kimi-k2.5", {}, sponsored=False)
    launched = len(starts.threads)
    with pytest.raises(ValueError, match="^Kimi K3 requires your own API key, or the sponsored Kimi service"):
        jobs.start_job(engine=engine, problem_text="Prove it.", model=None, user=ADMIN, auth_route="api_key")
    assert len(starts.threads) == launched


def test_a_blank_proof_of_a_full_access_account_never_takes_what_it_may_not_run(starts, saved_default):
    """A full-access account's CGM harness never runs a Kimi model
    (account_policy.member_model_error): its saved kimi-k2.5 gives the first
    model of its other keys; its Plain runs Kimi K3 on its own key.  It
    reaches the sponsored Kimi K3 service only through the included engines
    (account_policy.MEMBER_SPONSORED_ENGINES): with that service up and no
    Kimi key of its own, its saved kimi-k2.5 gives Meta-Harness, DeepSeek
    Harness and IMProof the first model of its keys, not the Kimi K3 the
    start would refuse them ("Kimi K3 runs on the included Ansatz CGM · Kimi
    K3 engine; …"), and Plain Kimi K3 there."""
    saved_default("kimi-k2.5", {**OWN_KIMI, **OWN_OPENAI})
    job = jobs.start_job(engine="math_harness", problem_text="Prove it.", model=None, user=FULL_ACCESS,
                         auth_route="api_key")
    assert job["model"] == "gpt-6-astra"
    job = jobs.start_job(engine="plain", problem_text="Prove it.", model=None, user=FULL_ACCESS, auth_route="api_key")
    assert (job["model"], job["credential_source"]) == ("kimi-k3", "user")
    saved_default("kimi-k2.5", OWN_OPENAI)
    for engine in ("metaharness", "deepseek_harness", "improof"):
        job = jobs.start_job(engine=engine, problem_text="Prove it.", model=None, user=FULL_ACCESS,
                             auth_route="api_key")
        assert (job["model"], job["credential_source"]) == ("gpt-6-astra", "user"), engine
        assert starts.threads[-1]["kwargs"]["model"] == "gpt-6-astra", engine
    job = jobs.start_job(engine="plain", problem_text="Prove it.", model=None, user=FULL_ACCESS, auth_route="api_key")
    assert (job["model"], job["credential_source"]) == ("kimi-k3", "sponsored_kimi")


def linked_account_probes(monkeypatch) -> list:
    """Record each probe of a linked Claude Code or Codex account's sign-in
    (Claude Code's ``claude auth status`` contacts Anthropic); every one
    answers signed out."""
    from agent_monitor import claude_login, codex_login

    probes: list = []
    account_state = claude_login._account_state
    monkeypatch.setattr(claude_login, "_auth_cache", {})
    monkeypatch.setattr(claude_login, "_run_auth_status", lambda home: probes.append(("claude auth status", str(home)))
                        or {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"})
    monkeypatch.setattr(claude_login, "_account_state", lambda home, *args, **kwargs: probes.append(
        ("claude", str(home))) or account_state(home, *args, **kwargs))
    monkeypatch.setattr(codex_login, "account_login_ready", lambda home: probes.append(("codex", str(home))) or False)
    return probes


@pytest.mark.parametrize("user", [ADMIN, FULL_ACCESS], ids=["admin", "full-access"])
def test_a_blank_proof_finds_its_model_without_probing_a_linked_account(starts, saved_default, monkeypatch, user):
    """A saved kimi-k2.5 that an engine running Kimi K3 alone cannot run as
    Kimi K3 here (no Kimi key of the account's own, the sponsored service
    down) gives the first model of the account's keys, which the start finds
    from those keys and the sponsored service's health alone
    (settings.account_api_models): it never probes the account's linked
    Claude Code or Codex sign-in for it, as Settings does.  As for Analyze
    and Readability (tests/test_readability_console.py)."""
    probes = linked_account_probes(monkeypatch)
    saved_default("kimi-k2.5", OWN_OPENAI, sponsored=False)
    for engine in ("plain", "metaharness", "deepseek_harness", "improof", "math_harness"):
        job = jobs.start_job(engine=engine, problem_text="Prove it.", model=None, user=user, auth_route="api_key")
        assert (job["model"], job["credential_source"]) == ("gpt-6-astra", "user"), engine
    assert probes == []


def test_a_blank_coding_agent_run_takes_a_saved_other_kimi_model_as_before(starts, saved_default):
    """A coding-agent harness sends a saved kimi-k2.5 as named to the
    account's own Kimi key, as HEAD did; without one the sponsored service
    would run it as Kimi K3, so Kimi K3 is what starts there."""
    saved_default("kimi-k2.5", OWN_KIMI)
    job = jobs.start_job(engine="deepagents", problem_text="Prove it.", model=None, user=ADMIN, auth_route="api_key")
    assert (job["model"], job["credential_source"]) == ("kimi-k2.5", "user")
    saved_default("kimi-k2.5", OWN_OPENAI)
    job = jobs.start_job(engine="deepagents", problem_text="Prove it.", model=None, user=ADMIN, auth_route="api_key")
    assert (job["model"], job["credential_source"]) == ("kimi-k3", "sponsored_kimi")


def test_a_restricted_members_blank_proof_takes_a_saved_other_kimi_model_as_kimi_k3(monkeypatch):
    """As account_policy.member_run_model resolves a member's default: Kimi
    K3 on its own Kimi key, else the first model its keys run."""
    from cryptography.fernet import Fernet

    from agent_monitor import auth

    for name in ("AGENT_MONITOR_PUBLIC", "AGENT_MONITOR_SPONSORED_KIMI", "OPENAI_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    auth.register("owner@example.test", "long-enough-password")
    member = auth.register("member@example.test", "long-enough-password")
    auth.set_user_env(int(member["id"]), {"AGENT_MONITOR_MODEL": "kimi-k2.5", **OWN_OPENAI, **OWN_KIMI})
    assert account_policy.member_run_model(member, "plain", None) == "kimi-k3"
    # The member's CGM harness never takes a Kimi model: its OpenAI key's first.
    assert account_policy.member_run_model(member, "math_harness", None) == "gpt-6-astra"
    auth.set_user_env(int(member["id"]), {}, clear=["KIMI_API_KEY"])
    assert account_policy.member_run_model(member, "plain", None) == "gpt-6-astra"


def test_an_own_kimi_key_of_plain_goes_to_the_operators_kimi_endpoint(starts, monkeypatch):
    """As for the kimi engine and the manuscript engines: an administrator's
    (or full-access account's) own Kimi key that names no endpoint of its own
    goes to AGENT_MONITOR_KIMI_BASE_URL, at the start and in a follow-up; an
    endpoint the account chose stands, and a coding-agent harness keeps its
    own resolution."""
    from agent_monitor.proof_graph import _resolve_llm_provider

    operator = "https://kimi-operator.example/v1"
    monkeypatch.setenv("AGENT_MONITOR_KIMI_BASE_URL", operator)
    starts.keys = OWN_KIMI
    for engine in ("plain", "metaharness", "math_harness", "deepseek_harness", "improof"):
        jobs.start_job(engine=engine, problem_text="Prove it.", model="kimi-k3", user=ADMIN, auth_route="api_key")
        env = starts.threads[-1]["kwargs"]["extra_env"]
        assert env["KIMI_API_BASE"] == operator, engine
        assert _resolve_llm_provider(dict(env), "kimi-k3")[1] == operator
    own = "https://kimi-own.example/v1"
    starts.keys = {**OWN_KIMI, "KIMI_API_BASE": own}
    jobs.start_job(engine="plain", problem_text="Prove it.", model="kimi-k3", user=FULL_ACCESS, auth_route="api_key")
    assert starts.threads[-1]["kwargs"]["extra_env"]["KIMI_API_BASE"] == own
    starts.keys = OWN_KIMI
    jobs.start_job(engine="deepagents", problem_text="Prove it.", model="kimi-k2.5", user=ADMIN, auth_route="api_key")
    assert "KIMI_API_BASE" not in starts.threads[-1]["kwargs"]["extra_env"]
    # A follow-up of an own-key Plain run on Kimi K3.
    record = {"run_id": "plain_kimi_own", "engine": "plain", "status": "finished", "model": "kimi-k3",
              "requested_model": "kimi-k3", "owner_id": ADMIN["id"], "auth_route": "api_key", "credential_source": "user"}
    env, route = jobs._continuation_auth_environment(run_data=record, engine="plain", model="kimi-k3", user=ADMIN,
                                                     env={"AGENT_MONITOR_PYTHON": "python", **OWN_KIMI})
    assert (route, env["KIMI_API_BASE"]) == ("api_key", operator)


def test_api_chat_never_asks_a_kimi_endpoint_under_another_name(monkeypatch):
    """The transport of the engines that run Kimi K3 alone: a call named for
    another Kimi model is refused before anything is sent (an own key's
    refusal every later call would repeat: kind "model"); Kimi K3, however
    named, is asked for as kimi-k3.  A coding-agent harness's audit names its
    model with an OpenAI-compatible prefix and is sent as named."""
    sent: list = []

    def http_json(url, *, headers, method="GET", payload=None, timeout=20):
        sent.append((url.rsplit("/", 1)[-1], payload))
        if url.endswith("/responses"):
            return 200, {"output_text": "ok", "status": "completed", "usage": {"input_tokens": 3, "output_tokens": 1}}
        return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 3, "completion_tokens": 1}}

    monkeypatch.setattr(settings, "_http_json", http_json)
    env = {"KIMI_API_KEY": OWN_KIMI["KIMI_API_KEY"], "KIMI_API_BASE": "http://127.0.0.1:9/v1"}
    for model in OTHER_KIMI_MODELS:
        with pytest.raises(api_backend.APIBackendError,
                           match="^Kimi calls here ask for Kimi K3 whatever Kimi model is named; choose kimi-k3, "
                                 "not ") as caught:
            api_backend.api_chat("system", "user", model=model, env=env)
        assert caught.value.kind == "model", model
    with pytest.raises(api_backend.APIBackendError, match="ask for Kimi K3") as sponsored:
        api_backend.api_chat("system", "user", model="kimi-k2.5", env={**env, "AGENT_MONITOR_SPONSORED_KIMI": "1"})
    assert sponsored.value.kind is None and sent == []
    for model in ("kimi-k3", " Kimi-K3", "kimi:kimi-k3"):
        assert api_backend.api_chat("system", "user", model=model, env=env).text == "ok"
    assert [payload["model"] for _path, payload in sent] == ["kimi-k3"] * 3
    # DeepAgents' audit on kimi-k2.5 (DEEPAGENTS_MODEL "openai:kimi-k2.5", its OpenAI-compatible
    # route pointed at the Kimi key): sent as named.
    sent.clear()
    harness_env = {"OPENAI_API_KEY": OWN_KIMI["KIMI_API_KEY"], "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
    assert api_backend.api_chat("system", "user", model="openai:kimi-k2.5", env=harness_env).text == "ok"
    assert [(path, payload["model"]) for path, payload in sent] == [("responses", "kimi-k2.5")]


_REAL_USER_EXTRA_ENV = jobs._user_extra_env  # the starts fixture replaces it


def test_the_kimi_models_notes_say_what_the_engines_do(starts, saved_default, monkeypatch):
    """OPERATIONS.md's "Kimi models" notes, and the behaviour each sentence
    states, with the account's keys projected as for a real start: Plain
    records Kimi K3 as typed, and runs ``kimi:kimi-k3`` only when a Kimi key
    is the account's only key (subprocess_env reads no ``kimi:`` prefix);
    Ansatz CGM · Kimi K3 refuses any other spelling; an own Kimi key's run
    goes to the operator's endpoint, but the run's derived Lean and DAG calls
    (lean_verify._llm_environment) use the built-in Kimi base."""
    from agent_monitor import ROOT, lean_verify
    from agent_monitor.proof_graph import _resolve_llm_provider

    notes = re.sub(r"\s+", " ", (ROOT / "OPERATIONS.md").read_text(encoding="utf-8"))
    notes = notes[notes.index("- **Kimi models.**"):]
    notes = notes[:notes.index(" - ", 1)]
    for sentence in (
        'Ansatz CGM · Kimi K3 says "Kimi Proof uses kimi-k3", and to a member "Ansatz CGM · Kimi K3 runs only kimi-k3"',
        "Ansatz CGM · Kimi K3 refuses any other spelling. Plain also runs `Kimi-K3` on an own Kimi key, recorded as "
        "typed, and on the sponsored route, recorded as `kimi-k3`; on a linked account it is refused like any API "
        "model. Plain runs `kimi:kimi-k3`, recorded as typed, only when a Kimi key is the account's only key: with "
        'another key beside it, or no key, the start is refused ("No compatible API key is configured; …"), and with '
        "another provider's key alone it starts on that key and the run fails for want of a Kimi key.",
        "An administrator's or a full-access account's own-key Kimi K3 runs of Plain, Ansatz CGM, Meta-Harness, "
        "DeepSeek Harness and IMProof, and their follow-ups, go to the operator's Kimi endpoint "
        "(`AGENT_MONITOR_KIMI_BASE_URL`) unless the account set a `KIMI_API_BASE` of its own, which stands; Ansatz "
        "CGM · Kimi K3 runs and a restricted member's always go to the operator's. The derived Lean and DAG calls of "
        "such a run do not follow this: they still use the account's own `KIMI_API_BASE` or, without one, the "
        "built-in Kimi base, whatever `AGENT_MONITOR_KIMI_BASE_URL` says.",
    ):
        assert sentence in notes, sentence
    assert "starts and follow-ups alike" not in notes and "recorded as named" not in notes

    monkeypatch.setattr(jobs, "_user_extra_env", _REAL_USER_EXTRA_ENV)

    def start(engine, model, keys, user=ADMIN):
        saved_default("", keys)
        try:
            job = jobs.start_job(engine=engine, problem_text="Prove it.", model=model, user=user, auth_route="api_key")
        except ValueError as exc:
            return str(exc)
        return job["model"], job["credential_source"]

    no_key = "No compatible API key is configured; add and verify one in Settings"
    assert start("plain", "Kimi-K3", {}) == ("Kimi-K3", "sponsored_kimi")
    assert start("plain", "Kimi-K3", {**OWN_KIMI, **OWN_OPENAI}) == ("Kimi-K3", "user")
    assert start("plain", "kimi:kimi-k3", OWN_KIMI) == ("kimi:kimi-k3", "user")
    assert start("plain", "kimi:kimi-k3", {**OWN_KIMI, **OWN_OPENAI}) == start("plain", "kimi:kimi-k3", {}) == no_key
    assert start("plain", "kimi:kimi-k3", OWN_OPENAI) == ("kimi:kimi-k3", "user")
    with pytest.raises(ValueError, match="^Model kimi-k3 requires KIMI_API_KEY"):
        _resolve_llm_provider(dict(starts.threads[-1]["kwargs"]["extra_env"]), "kimi:kimi-k3")
    for model in ("Kimi-K3", "kimi:kimi-k3", "kimi-k2.5"):
        assert start("kimi", model, OWN_KIMI) == "Kimi Proof uses kimi-k3", model
    for model in ("kimi:kimi-k3", "kimi-k2.5"):
        assert start("kimi", model, OWN_KIMI, user=MEMBER) == "Ansatz CGM · Kimi K3 runs only kimi-k3", model
        # The console's check of a request, for every member.
        assert account_policy.member_run_error(FULL_ACCESS, "kimi", model=model) == (
            "Ansatz CGM · Kimi K3 runs only kimi-k3")
    # The run goes to the operator's endpoint (test_an_own_kimi_key_of_plain_goes_to_the_operators_kimi_endpoint),
    # its derived calls to the built-in base, or the account's own (a restricted member's: the operator's).
    operator = "https://kimi-operator.example/v1"
    monkeypatch.setenv("AGENT_MONITOR_KIMI_BASE_URL", operator)
    record = {"run_id": "plain_kimi_own", "engine": "plain", "auth_route": "api_key", "model": "kimi-k3"}
    for user, keys, base in ((ADMIN, OWN_KIMI, kimi_k3.DEFAULT_BASE_URL),
                             (FULL_ACCESS, OWN_KIMI, kimi_k3.DEFAULT_BASE_URL),
                             (FULL_ACCESS, {**OWN_KIMI, "KIMI_API_BASE": "https://kimi-own.example/v1"},
                              "https://kimi-own.example/v1"),
                             (MEMBER, OWN_KIMI, operator)):
        saved_default("", keys)
        derived = lean_verify._llm_environment(user, {**record, "owner_id": user["id"]})
        assert _resolve_llm_provider(dict(derived), "kimi-k3")[1] == base, user
    assert operator != kimi_k3.DEFAULT_BASE_URL
