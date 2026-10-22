"""Model-aware, run-scoped reasoning and speed controls.

The UI treats these values as part of a run's immutable routing contract.  This
module is deliberately provider-credential free so validation can happen
before a workspace, thread, or model request is created.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

REASONING_ENV = "AGENT_MONITOR_RUN_REASONING_EFFORT"
SPEED_ENV = "AGENT_MONITOR_RUN_SPEED_MODE"

DEFAULT_REASONING = "default"
DEFAULT_SPEED = "standard"

_OPENAI_REASONING = ("default", "low", "medium", "high", "xhigh", "max")
# GPT-6 Sol and Luna also accept ``none`` through the Responses API.
_OPENAI_REASONING_WITH_NONE = (
    "default", "none", "low", "medium", "high", "xhigh", "max"
)
_CLAUDE_REASONING = ("default", "low", "medium", "high", "xhigh", "max")
_KIMI_REASONING = ("default", "low", "high", "max")
_SPEEDS = ("standard", "fast")
# Ultrafast is a Responses API service tier that only GPT-6 Astra offers.
_ULTRAFAST_SPEEDS = ("standard", "fast", "ultrafast")
_DEFAULT_PROFILE = ((DEFAULT_REASONING,), (DEFAULT_SPEED,))

# Harnesses that apply a profile to OpenAI models through the Codex CLI (on a
# Codex subscription every one of them does) ...
_OPENAI_TUNING_ENGINES = frozenset(
    {"codex", "deepagents", "hermes", "math_harness", "metaharness", "plain"}
)
# ... and those that send it with an API key to the Responses API.  The Codex
# harness keeps its CLI catalog on either route, and DeepAgents hands an API
# model id to LangChain without a profile, so neither belongs here.
_OPENAI_RESPONSES_ENGINES = frozenset(
    {"hermes", "math_harness", "metaharness", "plain"}
)
# (model prefix, Responses API profile, Codex CLI profile).  API values follow
# the OpenAI model pages; Codex values follow the vetted CLI's bundled catalog
# (``codex debug models --bundled``), so a model it lacks has no Codex profile.
_OPENAI_PROFILES = (
    ("gpt-6-astra", (_OPENAI_REASONING, _ULTRAFAST_SPEEDS), (_OPENAI_REASONING, _SPEEDS)),
    # In the bundled catalog from codex-cli 0.160.0 on; older vetted builds
    # drop its Fast tier, which is why that build is the pinned install.
    ("gpt-6.1-sol", (_OPENAI_REASONING, _SPEEDS), (_OPENAI_REASONING, _SPEEDS)),
    ("gpt-6-sol", (_OPENAI_REASONING_WITH_NONE, _SPEEDS), (_OPENAI_REASONING, _SPEEDS)),
    ("gpt-6-luna", (_OPENAI_REASONING_WITH_NONE, _SPEEDS), (_OPENAI_REASONING, _SPEEDS)),
    ("gpt-5.6", (_OPENAI_REASONING, _SPEEDS), (_OPENAI_REASONING, _SPEEDS)),
)
# Ansatz CGM Max (see agent_monitor/cgm_max.py).
_MAX_ROUTE = "dual_subscription"
_MAX_MODEL = "max"
_KIMI_TUNING_ENGINES = frozenset(
    {
        "codex", "deepagents", "deepseek_harness", "math_harness",
        "metaharness", "openclaw", "plain",
    }
)
_CLAUDE_TUNING_ENGINES = frozenset(
    {"claude", "deepagents", "math_harness", "metaharness", "plain"}
)
# Engines that apply a profile exactly as another engine does.  The Analyze
# and Readability runners reuse the CGM response-only turn machinery of
# ``math_harness``, so they accept the same reasoning and speed options on
# every route.
_ENGINE_ALIASES = {"analyze": "math_harness", "readability": "math_harness"}

_REASONING_LABELS = {
    "default": "Default",
    "none": "None",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Ultra",
}
_SPEED_LABELS = {"standard": "Standard", "fast": "Fast", "ultrafast": "Ultrafast"}
# The ``service_tier`` sent for each speed ("fast" is the current name of the
# former "priority" tier).
_SERVICE_TIERS = {"standard": "default", "fast": "fast", "ultrafast": "ultrafast"}


def _bare_model(model: str | None) -> str:
    value = str(model or "").strip().lower()
    for prefix in (
        "openai:", "openai/", "anthropic:", "anthropic/", "kimi:", "kimi/"
    ):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def _openai_profiles(
    selected: str, route: str
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """Map each harness to the (reasoning, speeds) it can apply to ``selected``."""
    for prefix, api_profile, codex_profile in _OPENAI_PROFILES:
        if selected.startswith(prefix):
            break
    else:
        return {}
    if route == "codex_subscription":
        return dict.fromkeys(_OPENAI_TUNING_ENGINES, codex_profile) if codex_profile else {}
    if route != "api_key":
        return {}
    profiles = dict.fromkeys(_OPENAI_RESPONSES_ENGINES, api_profile)
    if codex_profile:
        profiles["codex"] = codex_profile
    return profiles


def capabilities(
    model: str | None,
    auth_route: str | None,
    engine: str | None = None,
) -> dict[str, Any]:
    """Return only controls that the selected model route actually supports.

    Without ``engine`` the result covers every supported harness, and an
    option that only some of them can apply lists those in ``engines``.
    """
    selected = _bare_model(model)
    route = str(auth_route or "").strip().lower()
    profiles: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}

    if route == _MAX_ROUTE:
        # Ansatz CGM Max always runs both models at maximum effort.
        if selected == _MAX_MODEL:
            profiles = {"math_harness": (("max",), (DEFAULT_SPEED,))}
    elif selected.startswith("kimi-") and route == "api_key":
        profiles = dict.fromkeys(
            _KIMI_TUNING_ENGINES, (_KIMI_REASONING, (DEFAULT_SPEED,))
        )
    elif route == "claude_subscription" and (
        selected.startswith("claude-") or selected in {"sonnet", "opus", "fable"}
    ):
        # Claude Code currently omits --effort for Haiku 4.5.
        if "haiku" not in selected:
            profiles = dict.fromkeys(
                _CLAUDE_TUNING_ENGINES, (_CLAUDE_REASONING, (DEFAULT_SPEED,))
            )
    else:
        profiles = _openai_profiles(selected, route)
    for alias, target in _ENGINE_ALIASES.items():
        if target in profiles:
            profiles[alias] = profiles[target]

    supported_engines = sorted(profiles)
    selected_engine = str(engine or "").strip().lower()
    if selected_engine:
        profiles = {selected_engine: profiles.get(selected_engine, _DEFAULT_PROFILE)}
    elif not profiles:
        profiles = {"": _DEFAULT_PROFILE}

    def options(index: int, labels: dict[str, str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item, label in labels.items():
            engines = sorted(name for name, profile in profiles.items() if item in profile[index])
            if not engines:
                continue
            option: dict[str, Any] = {"id": item, "label": label}
            if len(engines) < len(profiles):
                option["engines"] = engines
            out.append(option)
        return out

    return {
        "reasoning": options(0, _REASONING_LABELS),
        "speed": options(1, _SPEED_LABELS),
        "default_reasoning_effort": DEFAULT_REASONING,
        "default_speed_mode": DEFAULT_SPEED,
        "supported_engines": supported_engines,
    }


def normalize(
    model: str | None,
    auth_route: str | None,
    *,
    reasoning_effort: object = None,
    speed_mode: object = None,
    engine: str | None = None,
) -> dict[str, str]:
    """Validate an explicit UI selection without allowing provider fallthrough."""
    requested_reasoning = str(reasoning_effort or DEFAULT_REASONING).strip().lower()
    requested_speed = str(speed_mode or DEFAULT_SPEED).strip().lower()
    available = capabilities(model, auth_route, engine)
    reasoning_ids = {item["id"] for item in available["reasoning"]}
    speed_ids = {item["id"] for item in available["speed"]}
    if requested_reasoning not in reasoning_ids:
        raise ValueError(
            f"{model or 'This model'} does not support reasoning effort "
            f"{requested_reasoning!r} on {auth_route or 'this route'}"
        )
    if requested_speed not in speed_ids:
        raise ValueError(
            f"{model or 'This model'} does not support speed mode "
            f"{requested_speed!r} on {auth_route or 'this route'}"
        )
    return {
        "reasoning_effort": requested_reasoning,
        "speed_mode": requested_speed,
    }


def apply_environment(
    env: MutableMapping[str, str],
    *,
    model: str | None,
    auth_route: str | None,
    reasoning_effort: object = None,
    speed_mode: object = None,
) -> dict[str, str]:
    """Validate and project a run profile into supported runner variables."""
    tuning = normalize(
        model,
        auth_route,
        reasoning_effort=reasoning_effort,
        speed_mode=speed_mode,
    )
    env[REASONING_ENV] = tuning["reasoning_effort"]
    env[SPEED_ENV] = tuning["speed_mode"]
    selected = _bare_model(model)
    effort = tuning["reasoning_effort"]
    if effort != DEFAULT_REASONING:
        if selected.startswith("kimi-"):
            env["KIMI_REASONING_EFFORT"] = effort
        if auth_route in {"claude_subscription", _MAX_ROUTE}:
            env["AGENT_MONITOR_CLAUDE_EFFORT"] = effort
        if auth_route == "codex_subscription":
            env["OPENHANDS_CODEX_REASONING_EFFORT"] = effort
    return tuning


def from_record(env: MutableMapping[str, str], record: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Apply a persisted profile; legacy records without one remain unchanged."""
    data = record or {}
    if "reasoning_effort" not in data and "speed_mode" not in data:
        return None
    return apply_environment(
        env,
        model=str(data.get("model") or "") or None,
        auth_route=str(data.get("auth_route") or "") or None,
        reasoning_effort=data.get("reasoning_effort"),
        speed_mode=data.get("speed_mode"),
    )


def service_tier(speed_mode: object) -> str | None:
    """The ``service_tier`` request value for a validated speed mode."""
    return _SERVICE_TIERS.get(str(speed_mode or "").strip().lower())


def openai_payload_options(env: Mapping[str, str], model: str | None) -> dict[str, Any]:
    """Translate a validated profile to Responses API request fields."""
    effort = str(env.get(REASONING_ENV) or "").strip().lower()
    speed = str(env.get(SPEED_ENV) or "").strip().lower()
    caps = capabilities(model, "api_key")
    reasoning_ids = {item["id"] for item in caps["reasoning"]}
    speed_ids = {item["id"] for item in caps["speed"]}
    out: dict[str, Any] = {}
    if effort and effort != DEFAULT_REASONING and effort in reasoning_ids:
        out["reasoning"] = {"effort": effort}
    if speed in speed_ids and len(speed_ids) > 1:
        out["service_tier"] = service_tier(speed)
    return out


def codex_config_args(env: Mapping[str, str], model: str | None) -> list[str]:
    """Translate a validated profile to exact Codex CLI config overrides."""
    if _bare_model(model).startswith("kimi-"):
        return []
    effort = str(env.get(REASONING_ENV) or "").strip().lower()
    speed = str(env.get(SPEED_ENV) or "").strip().lower()
    caps = capabilities(model, "codex_subscription")
    reasoning_ids = {item["id"] for item in caps["reasoning"]}
    speed_ids = {item["id"] for item in caps["speed"]}
    args: list[str] = []
    if effort and effort != DEFAULT_REASONING and effort in reasoning_ids:
        args.extend(["-c", f'model_reasoning_effort="{effort}"'])
    if speed in speed_ids and len(speed_ids) > 1:
        args.extend(["-c", f'service_tier="{service_tier(speed)}"'])
    return args
