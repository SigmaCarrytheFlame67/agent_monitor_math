"""Analyze caps per role and route, and the Analyze kill switches.

Defaults (DESIGN §1.8):

==================  ============  ===========  ==========  ===========
profile             result checks  model calls  wall clock  in flight
==================  ============  ===========  ==========  ===========
guest               12             16           2,400 s     2
member, sponsored   30             36           3,600 s     2
member, own key     120            140          3,600 s     3
admin, any route    256            300          14,400 s    3
==================  ============  ===========  ==========  ===========

Each value can be changed with ``AGENT_MONITOR_ANALYZE_<PROFILE>_MAX_NODES``,
``..._MAX_CALLS`` and ``..._MAX_SECONDS`` (``PROFILE`` = ``GUEST``,
``MEMBER_SPONSORED``, ``MEMBER_OWN_KEY`` or ``ADMIN``), always within the
absolute ceilings 256 / 300 / 14,400; ``AGENT_MONITOR_ANALYZE_CONCURRENCY``
(1-3) lowers the calls in flight (sponsored and guest runs stay at most 2,
subscription routes at 1).  A request's own ``caps`` can only lower them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Mapping

ENABLED_ENV = "AGENT_MONITOR_ANALYZE_ENABLED"
GUESTS_ENV = "AGENT_MONITOR_ANALYZE_GUESTS"
CONCURRENCY_ENV = "AGENT_MONITOR_ANALYZE_CONCURRENCY"
ROLES = ("guest", "member", "admin")
CEILING_NODES = 256
CEILING_CALLS = 300
CEILING_SECONDS = 14_400
MIN_SECONDS = 120


@dataclass(frozen=True)
class Caps:
    profile: str
    max_nodes: int
    max_calls: int
    max_seconds: int
    concurrency: int
    paste_chars: int

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "max_nodes": self.max_nodes,
            "max_calls": self.max_calls,
            "max_seconds": self.max_seconds,
            "concurrency": self.concurrency,
            "paste_chars": self.paste_chars,
        }


PROFILES = {
    "guest": Caps("guest", 12, 16, 2_400, 2, 20_000),
    "member_sponsored": Caps("member_sponsored", 30, 36, 3_600, 2, 400_000),
    "member_own_key": Caps("member_own_key", 120, 140, 3_600, 3, 400_000),
    "admin": Caps("admin", 256, 300, 14_400, 3, 400_000),
}


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(env.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "off", "no"}


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Analyze is offered at all (default on)."""
    return _flag(os.environ if env is None else env, ENABLED_ENV, True)


def guests_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Anonymous guests may start Analyze runs (default on, within the guest
    caps, one Analyze per guest per hour and the sponsored daily cap;
    ``AGENT_MONITOR_ANALYZE_GUESTS=0`` turns guests off, and
    ``AGENT_MONITOR_ANALYZE_ENABLED=0`` turns Analyze off for everyone)."""
    source = os.environ if env is None else env
    return enabled(source) and _flag(source, GUESTS_ENV, True)


def profile_for(role: str, *, sponsored: bool) -> str:
    if role == "admin":
        return "admin"
    if role == "guest":
        return "guest"
    return "member_sponsored" if sponsored else "member_own_key"


def _bounded(env: Mapping[str, str], name: str, default: int, ceiling: int, floor: int = 1) -> int:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(floor, min(value, ceiling))


def caps_for(
    role: str,
    *,
    sponsored: bool,
    env: Mapping[str, str] | None = None,
    requested: Mapping[str, object] | None = None,
    subscription: bool = False,
) -> Caps:
    """The effective caps for one run (see the module docstring)."""
    source = os.environ if env is None else env
    role = role if role in ROLES else "guest"
    base = PROFILES[profile_for(role, sponsored=sponsored)]
    prefix = f"AGENT_MONITOR_ANALYZE_{base.profile.upper()}_"
    caps = replace(
        base,
        max_nodes=_bounded(source, prefix + "MAX_NODES", base.max_nodes, CEILING_NODES),
        max_calls=_bounded(source, prefix + "MAX_CALLS", base.max_calls, CEILING_CALLS),
        max_seconds=_bounded(source, prefix + "MAX_SECONDS", base.max_seconds, CEILING_SECONDS, MIN_SECONDS),
    )
    concurrency = _bounded(source, CONCURRENCY_ENV, caps.concurrency, 3)
    if sponsored or role == "guest":
        concurrency = min(concurrency, 2)
    if subscription:
        concurrency = 1
    caps = replace(caps, concurrency=min(concurrency, caps.concurrency))
    for key in ("max_nodes", "max_calls", "max_seconds"):
        value = (requested or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            floor = MIN_SECONDS if key == "max_seconds" else 1
            caps = replace(caps, **{key: max(floor, min(getattr(caps, key), value))})
    return caps
