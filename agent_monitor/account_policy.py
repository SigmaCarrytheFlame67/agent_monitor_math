"""What a self-registered, non-admin account ("member") may run on this host.

Registration can be open to anyone, so a member is treated as untrusted: it
runs only the reviewed engines that never execute tools or local code, and it
never uses an account subscription, subagents or a custom provider endpoint.
The Ansatz CGM harness (``math_harness``) is the same response-only pipeline
as the sponsored ``kimi`` engine; a member runs it only on its own API keys,
never on Kimi K3 (the ``kimi`` engine serves that) or on Max.  Local
coding-agent harnesses (Codex, OpenClaw, OpenHands, DeepAgents, ...) have no
OS sandbox and stay administrator-only.

Ansatz Analyze (``analyze``) is tool-free too: a member runs it on the
sponsored Kimi K3 route or on its own keys, never on a subscription.  Its
availability follows the Analyze switch (``AGENT_MONITOR_ANALYZE_ENABLED``)
rather than ``AGENT_MONITOR_MEMBER_ENGINES``, which lists proof engines; an
``ANALYZE_CMD`` override removes it like the other command overrides.  In
public mode every account inherits the server's provider keys, so a member's
analysis then runs on sponsored Kimi K3 only (see
:func:`member_analyze_sponsored_only`).

Ansatz Readability (``readability``) follows the same rules with its own
switch (``AGENT_MONITOR_READABILITY_ENABLED``) and command override
(``READABILITY_CMD``), and runs Kimi K3 only: sponsored, or on the member's
own Kimi key.
"""
from __future__ import annotations

import os
from typing import Any

# Tool-free engines: the reviewed set guests may use, plus the response-only
# Ansatz CGM harness on the member's own keys, plus Ansatz Analyze and Ansatz
# Readability.
MEMBER_ELIGIBLE_ENGINES = frozenset({"kimi", "plain", "math_harness", "analyze", "readability"})
# Proof engines named by AGENT_MONITOR_MEMBER_ENGINES (its default and ceiling).
MEMBER_PROOF_ENGINES = frozenset({"kimi", "plain", "math_harness"})
ANALYZE_ENGINE = "analyze"
READABILITY_ENGINE = "readability"
# A command override turns these engines into operator-selected local code.
_COMMAND_OVERRIDES = {
    "plain": "PLAIN_CMD",
    "kimi": "KIMI_PROOF_CMD",
    "math_harness": "MATH_HARNESS_CMD",
    "analyze": "ANALYZE_CMD",
    "readability": "READABILITY_CMD",
}
# Engines a member runs only on its own provider keys: never the sponsored
# Kimi route, Max or a subscription.
OWN_KEY_ENGINES = frozenset({"math_harness"})
# Where a member can choose a model from its own keys, as the notes name them.
_OWN_KEY_CHOICES = (("math_harness", "Ansatz CGM"), ("plain", "Plain"))

# Subscription sign-in stays with the site administrator: the providers do not
# let a third-party site sign in with, or route requests through, members'
# plans.  The Settings cards and the refused login requests say so verbatim,
# naming only the engines this site lets members run on their own keys.
_SIGN_IN_ONLY_FOR_ADMIN = {
    "claude": (
        "Claude subscription sign-in is only for the site administrator, because "
        "Anthropic doesn't let third-party sites sign in with or route requests "
        "through members' Claude plans."
    ),
    "codex": (
        "ChatGPT sign-in for Codex is only for the site administrator, because "
        "OpenAI requires partner approval for hosted sites to use members' "
        "ChatGPT plans."
    ),
}
_USE_YOUR_OWN_KEY = {
    "claude": (
        " To use Claude here, add your own Anthropic API key under API providers, "
        "then choose a Claude model in {engines}."
    ),
    "codex": (
        " To use OpenAI models here, add your own OpenAI API key under API "
        "providers, then choose an OpenAI model in {engines}."
    ),
}
# The notes on a site that lets members run both own-key engines (the default).
CLAUDE_SUBSCRIPTION_NOTE = _SIGN_IN_ONLY_FOR_ADMIN["claude"] + _USE_YOUR_OWN_KEY[
    "claude"
].format(engines="Ansatz CGM or Plain")
CODEX_SUBSCRIPTION_NOTE = _SIGN_IN_ONLY_FOR_ADMIN["codex"] + _USE_YOUR_OWN_KEY[
    "codex"
].format(engines="Ansatz CGM or Plain")
_SUBSCRIPTION_ROUTES = {"claude_subscription": "claude", "codex_subscription": "codex"}
# A run recorded on a subscription or on Max stays with the administrator.
_ADMIN_ONLY_CONTINUATION = {
    "claude_subscription": (
        "This run was billed to a Claude subscription, which only the site "
        "administrator can use here, so only the administrator can continue it. "
        "Start a new run with your own Anthropic API key instead."
    ),
    "codex_subscription": (
        "This run was billed to a ChatGPT (Codex) subscription, which only the "
        "site administrator can use here, so only the administrator can continue "
        "it. Start a new run with your own OpenAI API key instead."
    ),
}
_MAX_CONTINUATION = (
    "This run used Max, which runs on the site administrator's linked Claude "
    "Code and Codex accounts, so only the administrator can continue it. Start "
    "a new run with a model from your own API keys instead."
)
OWN_KEY_MODEL_NOTE = (
    "Members run the Ansatz CGM harness on a model from their own API keys; "
    "Kimi K3 runs on the included Ansatz CGM · Kimi K3 engine"
)
_LOCAL_TOOLS_ERROR = "That engine runs local tools and is limited to the site administrator"

# A member's run feedback is bounded: each message (the length of a problem
# statement), everything added to one run (every continuation prompt repeats
# it), and what waits in the queue for a running run's next turn.
MEMBER_FEEDBACK_MAX_CHARS = 20_000
MEMBER_RUN_FEEDBACK_MAX_CHARS = 100_000
MEMBER_QUEUED_FEEDBACK_MAX_BYTES = 200_000


def is_member(user: dict[str, Any] | None) -> bool:
    """A signed-in account that is neither a guest nor an administrator."""
    return bool(user) and not user.get("guest") and not user.get("is_admin")


def _analyze_enabled() -> bool:
    from agent_monitor.analyze import limits as analyze_limits

    return analyze_limits.enabled()


def _readability_enabled() -> bool:
    from agent_monitor.readability import limits as readability_limits

    return readability_limits.enabled()


def member_engine_allowlist() -> frozenset[str]:
    """Operator-selected member engines, never beyond the tool-free ceiling.

    ``AGENT_MONITOR_MEMBER_ENGINES`` selects the proof engines; Analyze and
    Readability follow their own switches, so a deployment that narrowed the
    proof engines before they existed still offers them
    (``AGENT_MONITOR_ANALYZE_ENABLED=0`` or
    ``AGENT_MONITOR_READABILITY_ENABLED=0`` turns one off for everyone).
    """
    raw = os.environ.get("AGENT_MONITOR_MEMBER_ENGINES")
    requested = (
        set(MEMBER_PROOF_ENGINES)
        if raw is None
        else {part.strip().lower() for part in raw.split(",") if part.strip()}
    )
    allowed = requested & MEMBER_PROOF_ENGINES
    if _analyze_enabled():
        allowed.add(ANALYZE_ENGINE)
    if _readability_enabled():
        allowed.add(READABILITY_ENGINE)
    for engine, override in _COMMAND_OVERRIDES.items():
        if os.environ.get(override, "").strip():
            allowed.discard(engine)
    from agent_monitor.auth import public_mode

    if public_mode():
        # Public mode gives every account the server's provider keys
        # (settings.resolved_user_env), so an "own key" run would bill them.
        allowed -= OWN_KEY_ENGINES
    return frozenset(allowed)


def _engine_refusal(engine: str) -> tuple[str, str]:
    """Why members may not run ``engine`` here, as (error, catalog hint)."""
    if engine not in MEMBER_ELIGIBLE_ENGINES:
        return (
            _LOCAL_TOOLS_ERROR,
            "Administrator only on this site: this harness runs local tools",
        )
    if engine == ANALYZE_ENGINE and not _analyze_enabled():
        return (
            "Analyze is turned off on this site",
            "Turned off on this site",
        )
    if engine == READABILITY_ENGINE and not _readability_enabled():
        return (
            "Readability is turned off on this site",
            "Turned off on this site",
        )
    override = _COMMAND_OVERRIDES.get(engine)
    if override and os.environ.get(override, "").strip():
        return (
            "That engine runs operator-chosen local code on this site, so it is "
            "limited to the site administrator",
            "Administrator only on this site: this engine runs operator-chosen local code",
        )
    # Narrowed by AGENT_MONITOR_MEMBER_ENGINES, or an own-key engine in
    # public mode.
    return (
        "The site administrator has not enabled that engine for member accounts",
        "Administrator only on this site: not enabled for member accounts",
    )


# In public mode a member's "own key" would be the server's (see above).
ANALYZE_SPONSORED_ONLY_NOTE = "Members analyze on the included Kimi K3 on this site"
READABILITY_KIMI_NOTE = "Readability runs on Kimi K3 for members: the included one or your own Kimi key"


def member_analyze_sponsored_only(user: dict[str, Any] | None) -> bool:
    """Whether ``user``'s analyses must use the sponsored Kimi K3 route.

    Public mode gives every account the server's provider keys
    (``settings.resolved_user_env``), so a member's own-key analysis would
    bill the operator, as the own-key CGM harness would (it is removed from
    the member engines then).  Sponsored Kimi K3 keeps its caps, the gate
    and the daily cap.
    """
    if not is_member(user):
        return False
    from agent_monitor.auth import public_mode

    return public_mode()


def member_engine_error(user: dict[str, Any] | None, engine: str) -> str | None:
    """Why ``user`` may not run ``engine``, or None when it may."""
    if not is_member(user) or engine in member_engine_allowlist():
        return None
    return _engine_refusal(engine)[0]


def member_run_model(
    user: dict[str, Any] | None, engine: str, model: str | None
) -> str | None:
    """The model a member's ``engine`` run uses when the request names none.

    A blank Plain, Ansatz CGM or Analyze request (the Projects page's
    "Default account model") means the default model the console offers: the
    member's saved default, or, when the member's keys (or the sponsored Kimi
    K3 service) cannot run that, the first model of the account's API keys
    they can run.  Only a model the engine accepts, and never Max.  When
    nothing else runs, a saved default the engine accepts stays, so the
    refusal names the key it needs (a new account's default is Kimi K3).
    """
    if engine == READABILITY_ENGINE and is_member(user) and not str(model or "").strip():
        # Readability runs Kimi K3 only (the included one or the member's own key).
        from agent_monitor.kimi_k3 import MODEL_ID

        return MODEL_ID
    if (
        str(model or "").strip()
        or not is_member(user)
        or engine not in {*dict(_OWN_KEY_CHOICES), ANALYZE_ENGINE}
    ):
        return model
    from agent_monitor import cgm_max
    from agent_monitor.jobs import _api_keys_run, _sponsored_broker_serves
    from agent_monitor.settings import get_settings, resolved_user_env

    env = resolved_user_env(user)
    saved = str(env.get("AGENT_MONITOR_MODEL") or "").strip()

    def accepted(candidate: str) -> bool:
        return (
            bool(candidate)
            and not cgm_max.is_max_model(candidate)
            and not member_model_error(user, engine, candidate)
        )

    for candidate in (saved, *get_settings(user)["api_models"]):
        if accepted(candidate) and (
            _api_keys_run(env, candidate) or _sponsored_broker_serves(engine, candidate)
        ):
            return candidate
    return saved if accepted(saved) else model


def member_model_error(
    user: dict[str, Any] | None, engine: str, model: str | None
) -> str | None:
    """Why a member may not run ``engine`` on ``model``, or None when it may.

    The ``kimi`` engine runs only Kimi K3.  An own-key engine needs a model
    from the member's own API keys: not Kimi K3, which the sponsored ``kimi``
    engine serves, and not Max, which uses the administrator's subscriptions.
    """
    if not is_member(user):
        return None
    selected = str(model or "").strip().lower()
    if engine == "kimi":
        from agent_monitor.kimi_k3 import MODEL_ID

        if selected and selected != MODEL_ID:
            return f"Ansatz CGM · Kimi K3 runs only {MODEL_ID}"
        return None
    if engine == ANALYZE_ENGINE and member_analyze_sponsored_only(user):
        from agent_monitor.kimi_k3 import MODEL_ID

        if selected and selected != MODEL_ID:
            return ANALYZE_SPONSORED_ONLY_NOTE
        return None
    if engine == READABILITY_ENGINE:
        from agent_monitor.kimi_k3 import MODEL_ID

        if selected and selected != MODEL_ID:
            return READABILITY_KIMI_NOTE
        return None
    if engine not in OWN_KEY_ENGINES:
        return None
    from agent_monitor import cgm_max

    if cgm_max.is_max_model(selected):
        return str(cgm_max.plan_for(user)["reason"])
    if not selected:
        return "Choose a model from your own API keys to run the Ansatz CGM harness"
    if selected.startswith("kimi-"):
        return (
            "Kimi K3 runs on the included Ansatz CGM · Kimi K3 engine; the Ansatz "
            "CGM harness uses a model from your own API keys"
        )
    return None


def member_route_error(
    user: dict[str, Any] | None, auth_route: str | None
) -> str | None:
    """Why a member may not start a run billed to ``auth_route``, or None."""
    if not is_member(user):
        return None
    route = str(auth_route or "").strip().lower()
    from agent_monitor import cgm_max

    if route == cgm_max.ROUTE:
        return str(cgm_max.plan_for(user)["reason"])
    kind = _SUBSCRIPTION_ROUTES.get(route)
    return subscription_notes()[kind] if kind else None


def member_continuation_error(
    user: dict[str, Any] | None, auth_route: str | None
) -> str | None:
    """Why a member may not continue a run recorded on ``auth_route``, or None.

    A run billed to a subscription or to Max (a member's own, started before
    subscriptions became administrator-only) stays the administrator's to
    continue.
    """
    if not is_member(user):
        return None
    route = str(auth_route or "").strip().lower()
    from agent_monitor import cgm_max

    if route == cgm_max.ROUTE:
        return _MAX_CONTINUATION
    return _ADMIN_ONLY_CONTINUATION.get(route)


def member_run_error(
    user: dict[str, Any] | None,
    engine: str,
    *,
    model: str | None = None,
    auth_route: str | None = None,
) -> str | None:
    """Why a member may not start this run, or None when it may."""
    return (
        member_engine_error(user, engine)
        or member_route_error(user, auth_route)
        or member_model_error(user, engine, model)
    )


# Security policy, which the site owner may relax: a run started from a
# project gets a copy of its shared document, which any member can edit.
# Once a member is not an administrator (projects._untrusted: for good, as
# its text stays after it leaves), the project's runs, their follow-ups and
# their Formal harness use only the tool-free engines on an API key, without
# subagents, so text a member wrote never steers a coding agent with a
# shell, or one signed in to the administrator's subscriptions.
PROJECT_TOOL_FREE_ERROR = (
    "Accounts that are not site administrators can write, or have written, in "
    "this project's document, which reaches the agent, so its runs use only "
    "tool-free engines (Ansatz CGM or Plain) on an API key, without subagents. "
    "Coding agents, subscriptions and Max stay off for this project, even "
    "after those members leave; to use them, copy text you have checked into "
    "a new project"
)


class ProjectPolicyError(PermissionError):
    """A run or follow-up refused by the project rule above (HTTP 403)."""


def tool_free_engines() -> frozenset[str]:
    """The tool-free proof engines, less any an operator command replaces."""
    return frozenset(
        engine
        for engine in MEMBER_PROOF_ENGINES
        if not os.environ.get(_COMMAND_OVERRIDES[engine], "").strip()
    )


def project_run_error(
    engine: str, *, model: str | None = None, auth_route: str | None = None
) -> str | None:
    """Why a run that carries a non-administrator's project text may not use
    this engine, model and route, or None when it may."""
    from agent_monitor import cgm_max

    route = str(auth_route or "").strip().lower()
    if (
        engine in tool_free_engines()
        and route in {"", "api_key"}
        and not cgm_max.is_max_model(model)
    ):
        return None
    return PROJECT_TOOL_FREE_ERROR


def member_feedback_error(
    message: str, *, run_feedback_chars: int, queued_bytes: int
) -> str | None:
    """Why a member's run feedback is refused, or None when it fits.

    ``run_feedback_chars`` is the human feedback the run already carries and
    ``queued_bytes`` the feedback waiting for its next turn.
    """
    if len(message) > MEMBER_FEEDBACK_MAX_CHARS:
        return (
            f"Feedback is limited to {MEMBER_FEEDBACK_MAX_CHARS:,} characters; "
            "shorten the message"
        )
    if run_feedback_chars + len(message) > MEMBER_RUN_FEEDBACK_MAX_CHARS:
        return "This run has reached its feedback limit; start a new run to keep going"
    if queued_bytes + len(message.encode("utf-8")) > MEMBER_QUEUED_FEEDBACK_MAX_BYTES:
        return (
            "Enough feedback is already waiting for this run; send more after the "
            "current harness finishes"
        )
    return None


def admin_only_engine_view(engine: dict[str, Any]) -> dict[str, Any]:
    """``engine`` as the catalog shows it to an account that may not run it."""
    hint = _engine_refusal(str(engine.get("id") or ""))[1]
    return {
        **engine,
        "available": False,
        "requires_admin": True,
        "health": "admin_only",
        "health_detail": hint,
        "hint": hint,
    }


def member_engine_view(engine: dict[str, Any]) -> dict[str, Any]:
    """How the engine catalog presents ``engine`` to a member."""
    if engine.get("id") not in member_engine_allowlist():
        return admin_only_engine_view(engine)
    # Member runs bill an API key (their own, or the included Kimi K3).
    view = {
        **engine,
        "auth_modes": [mode for mode in engine.get("auth_modes") or [] if mode == "api_key"],
        "subscription_supported": False,
        "claude_subscription_supported": False,
    }
    if engine.get("id") in OWN_KEY_ENGINES:
        view["own_key_only"] = True
        view["model_note"] = OWN_KEY_MODEL_NOTE
    return view


def subscription_policy(user: dict[str, Any] | None) -> dict[str, str]:
    """Who may sign in with each account subscription, for the Settings UI."""
    if user is not None and user.get("guest"):
        state = "sign_in"
    elif is_member(user):
        state = "admin_only"
    else:
        state = "available"
    return {"claude": state, "codex": state}


def subscription_notes() -> dict[str, str]:
    """Why a member cannot sign in with each subscription, and what works instead.

    The way forward names only the engines this site lets members run on
    their own keys, so narrowing ``AGENT_MONITOR_MEMBER_ENGINES`` (or a
    command override, or public mode) never points a member at one it removed.
    """
    allowed = member_engine_allowlist()
    engines = " or ".join(name for engine, name in _OWN_KEY_CHOICES if engine in allowed)
    return {
        kind: note + (_USE_YOUR_OWN_KEY[kind].format(engines=engines) if engines else "")
        for kind, note in _SIGN_IN_ONLY_FOR_ADMIN.items()
    }
