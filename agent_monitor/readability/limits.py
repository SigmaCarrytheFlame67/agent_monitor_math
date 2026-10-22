"""Readability caps per role and route, and the Readability switches.

Defaults:

==================  =====  ===========  ==========  =========
profile             parts  model calls  wall clock  in flight
==================  =====  ===========  ==========  =========
guest               6      16           2,400 s     2
member, sponsored   12     30           3,600 s     2
member, own key     40     90           3,600 s     3
admin, any route    120    300          14,400 s    3
==================  =====  ===========  ==========  =========

Each value can be changed with ``AGENT_MONITOR_READABILITY_<PROFILE>_MAX_PARTS``,
``..._MAX_CALLS`` and ``..._MAX_SECONDS`` (``PROFILE`` = ``GUEST``,
``MEMBER_SPONSORED``, ``MEMBER_OWN_KEY`` or ``ADMIN``), always within the
ceilings 120 / 300 / 14,400; ``AGENT_MONITOR_READABILITY_CONCURRENCY`` (1-3)
lowers the calls in flight (sponsored and guest runs stay at most 2,
subscription routes at 1).  A request's own ``caps`` can only lower them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Mapping

from agent_monitor.analyze import limits as analyze_limits

ENABLED_ENV = "AGENT_MONITOR_READABILITY_ENABLED"
GUESTS_ENV = "AGENT_MONITOR_READABILITY_GUESTS"
CONCURRENCY_ENV = "AGENT_MONITOR_READABILITY_CONCURRENCY"
ROLES = ("guest", "member", "admin")
CEILING_PARTS = 120
CEILING_CALLS = 300
CEILING_SECONDS = 14_400
MIN_SECONDS = analyze_limits.MIN_SECONDS


@dataclass(frozen=True)
class Caps:
    profile: str
    max_parts: int
    max_calls: int
    max_seconds: int
    concurrency: int
    paste_chars: int

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "max_parts": self.max_parts,
            "max_calls": self.max_calls,
            "max_seconds": self.max_seconds,
            "concurrency": self.concurrency,
            "paste_chars": self.paste_chars,
        }


PROFILES = {
    "guest": Caps("guest", 6, 16, 2_400, 2, 20_000),
    "member_sponsored": Caps("member_sponsored", 12, 30, 3_600, 2, 400_000),
    "member_own_key": Caps("member_own_key", 40, 90, 3_600, 3, 400_000),
    "admin": Caps("admin", 120, 300, 14_400, 3, 400_000),
}


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Readability is offered at all (default on)."""
    return analyze_limits._flag(os.environ if env is None else env, ENABLED_ENV, True)


def guests_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Anonymous guests may start readability runs.  Unset, the switch
    follows Analyze's guest switch (``AGENT_MONITOR_ANALYZE_GUESTS``, default
    on): both modes spend the same sponsored pool."""
    source = os.environ if env is None else env
    default = analyze_limits._flag(source, analyze_limits.GUESTS_ENV, True)
    return enabled(source) and analyze_limits._flag(source, GUESTS_ENV, default)


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
    base = PROFILES[analyze_limits.profile_for(role, sponsored=sponsored)]
    prefix = f"AGENT_MONITOR_READABILITY_{base.profile.upper()}_"
    bounded = analyze_limits._bounded
    caps = replace(
        base,
        max_parts=bounded(source, prefix + "MAX_PARTS", base.max_parts, CEILING_PARTS),
        max_calls=bounded(source, prefix + "MAX_CALLS", base.max_calls, CEILING_CALLS),
        max_seconds=bounded(source, prefix + "MAX_SECONDS", base.max_seconds, CEILING_SECONDS, MIN_SECONDS),
    )
    concurrency = bounded(source, CONCURRENCY_ENV, caps.concurrency, 3)
    if sponsored or role == "guest":
        concurrency = min(concurrency, 2)
    if subscription:
        concurrency = 1
    caps = replace(caps, concurrency=min(concurrency, caps.concurrency))
    for key in ("max_parts", "max_calls", "max_seconds"):
        value = (requested or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            floor = MIN_SECONDS if key == "max_seconds" else 1
            caps = replace(caps, **{key: max(floor, min(getattr(caps, key), value))})
    return caps
