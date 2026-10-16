"""Ansatz CGM "Max": Claude Code leads, Codex works.

A Max run is an Ansatz CGM (``math_harness``) run on two linked subscriptions
at once.  Claude Code, on its most capable model at maximum effort, is the
orchestrator and the advisors: it plans (strategist), checks every lemma
(verifier) and curates the search.  Codex, on its most capable model at
maximum effort, is the worker: it writes and repairs the proof attempts.

Both accounts must be linked, which on this site means the administrator's
account; a Max run is never billed to an API key.  The run records the model
``max`` and the route ``dual_subscription``; ``route_plan`` on the run record
names the model each side used.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MODEL_ID = "max"
ROUTE = "dual_subscription"
ENGINE = "math_harness"
EFFORT = "max"
SPEED = "standard"

# Child-process markers (see jobs._configure_auth_route and the CGM runner).
ROUTE_MODE_ENV = "AGENT_MONITOR_CGM_ROUTE_MODE"
CODEX_CALL_HOME_ENV = "AGENT_MONITOR_CODEX_CALL_HOME"

# Most capable first; the first model the linked account offers is used.
ADVISOR_MODELS = ("fable", "opus", "sonnet")
WORKER_MODELS = ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol")
ADVISOR_ROLES = ("strategist", "verifier", "curator")
WORKER_ROLES = ("worker", "repair")

# Maximum-effort calls are long.  Per-call limits for each side, and the CGM
# pipeline's own wall clock for one Max run (the outer watchdog adds margin).
CODEX_CALL_TIMEOUT = 1800
CLAUDE_CALL_TIMEOUT = 1800
RUN_SECONDS = 10_800

# Lean and graph work after the run uses the worker route.  Those calls have a
# fixed per-call limit, so they run at high rather than maximum effort.
FORMAL_EFFORT = "high"


def is_max_model(model: object) -> bool:
    return str(model or "").strip().lower() == MODEL_ID


def is_max_record(record: Mapping[str, Any] | None) -> bool:
    """Whether a run record belongs to a Max run."""
    data = record or {}
    if str(data.get("auth_route") or "").strip().lower() == ROUTE:
        return True
    live = data.get("live") or {}
    return isinstance(live, Mapping) and str(live.get("auth_mode") or "").strip().lower() == ROUTE


def _side(route: str, model: str | None, roles: tuple[str, ...]) -> dict[str, Any]:
    return {"route": route, "model": model, "effort": EFFORT, "roles": list(roles)}


def plan_for(user: Mapping[str, Any] | None) -> dict[str, Any]:
    """Whether ``user`` can start a Max run, and the models it would use."""
    plan: dict[str, Any] = {
        "available": False,
        "reason": "",
        "advisor": _side("claude_subscription", ADVISOR_MODELS[0], ADVISOR_ROLES),
        "worker": _side("codex_subscription", WORKER_MODELS[0], WORKER_ROLES),
    }
    if not user:
        plan["reason"] = "Max needs a signed-in account with Claude Code and Codex linked"
        return plan
    if user.get("guest"):
        # Signing up gives a member account, which cannot link either one.
        plan["reason"] = (
            "Max runs on the site administrator's linked Claude Code and Codex "
            "subscriptions"
        )
        return plan
    from agent_monitor import account_policy

    if account_policy.is_member(user):
        plan["reason"] = (
            "Max runs on linked Claude Code and Codex subscriptions, which are "
            "limited to the site administrator"
        )
        return plan
    try:
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError):
        plan["reason"] = "Max needs a signed-in account with Claude Code and Codex linked"
        return plan
    from agent_monitor import claude_login, codex_login

    missing = []
    try:
        claude_ready = claude_login.account_login_ready(claude_login.account_home(user_id))
    except (OSError, ValueError):
        claude_ready = False
    try:
        codex_ready = codex_login.account_login_ready(codex_login.account_home(user_id))
    except (OSError, ValueError):
        codex_ready = False
    if not claude_ready:
        missing.append("Claude Code")
    if not codex_ready:
        missing.append("Codex")
    if missing:
        plan["reason"] = (
            "Max needs both Claude Code and Codex linked; connect "
            + " and ".join(missing)
            + " in Settings"
        )
        return plan
    advisor = next(
        (model for model in ADVISOR_MODELS if model in claude_login.available_models(user_id)),
        None,
    )
    worker = next(
        (model for model in WORKER_MODELS if model in codex_login.available_models(user_id)),
        None,
    )
    if advisor is None or worker is None:
        side = "Claude Code" if advisor is None else "Codex"
        plan["reason"] = f"The linked {side} account offers none of the models Max uses"
        return plan
    plan["advisor"]["model"] = advisor
    plan["worker"]["model"] = worker
    plan["available"] = True
    return plan


def record_plan(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """The route plan stored on a Max run, completed with the defaults."""
    stored = (record or {}).get("route_plan")
    stored = stored if isinstance(stored, Mapping) else {}
    plan = {
        "advisor": _side("claude_subscription", ADVISOR_MODELS[0], ADVISOR_ROLES),
        "worker": _side("codex_subscription", WORKER_MODELS[0], WORKER_ROLES),
    }
    for side, default_models in (("advisor", ADVISOR_MODELS), ("worker", WORKER_MODELS)):
        value = stored.get(side)
        if isinstance(value, Mapping):
            model = str(value.get("model") or "").strip()
            if model in default_models:
                plan[side]["model"] = model
    return plan


def apply_models(env: dict[str, str], plan: Mapping[str, Any]) -> dict[str, str]:
    """Select each side's model and the Max time limits in a child env."""
    env["AGENT_MONITOR_SELECTED_MODEL"] = MODEL_ID
    env["AGENT_MONITOR_CLAUDE_MODEL"] = str(plan["advisor"]["model"])
    env["AGENT_MONITOR_CODEX_MODEL"] = str(plan["worker"]["model"])
    env["AGENT_MONITOR_CODEX_TIMEOUT"] = str(CODEX_CALL_TIMEOUT)
    env["AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT"] = str(CLAUDE_CALL_TIMEOUT)
    env["AGENT_MONITOR_CGM_MAX_SECONDS"] = str(RUN_SECONDS)
    return env


def formal_view(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A run as Lean and graph work after the run sees it.

    Derived calls run on one route; for Max that is the linked Codex account
    with the worker model.  Derived Codex calls have a fixed per-call limit
    (240 s for a DAG or Lean response), so like Max they run at no more than
    high effort: a Codex-subscription run made at extra-high or maximum
    effort derives its views at high.  Other records are returned unchanged.
    """
    if record is None:
        return record  # type: ignore[return-value]
    if not is_max_record(record):
        live = record.get("live") or {}
        route = str(record.get("auth_route") or "").strip().lower() or (
            "codex_subscription"
            if isinstance(live, Mapping)
            and str(live.get("auth_mode") or "").strip().lower()
            in {"codex_subscription", "chatgpt_subscription"}
            else ""
        )
        effort = str(record.get("reasoning_effort") or "").strip().lower()
        if route == "codex_subscription" and effort in {"xhigh", "max"}:
            view = dict(record)
            view["reasoning_effort"] = FORMAL_EFFORT
            return view
        return record  # type: ignore[return-value]
    worker = record_plan(record)["worker"]
    model = str(worker["model"])
    view = dict(record)
    view["auth_route"] = "codex_subscription"
    view["model"] = model
    view["requested_model"] = model
    view["reasoning_effort"] = FORMAL_EFFORT
    view["speed_mode"] = SPEED
    live = dict(view.get("live") or {})
    live.update(auth_mode="chatgpt_subscription", model=model, requested_model=model)
    view["live"] = live
    return view


def formal_model(record: Mapping[str, Any] | None, fallback: str | None) -> str | None:
    """The model derived work should use for ``record``."""
    if is_max_record(record):
        return str(record_plan(record)["worker"]["model"])
    return fallback
