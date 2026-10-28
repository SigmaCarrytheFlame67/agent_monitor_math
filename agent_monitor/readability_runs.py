"""Server side of the console's Readability mode (``run_kind: "readability"``).

The Readability core (``agent_monitor/readability``,
``runners/readability_runner.py``) rewrites one manuscript for a reader; this
module decides who may start which readability run, and what each viewer is
told:

* :func:`normalize_request` validates the Readability part of
  ``POST /api/runs`` with Analyze's source rules by role
  (``analyze_runs.normalize_source``): guests use arXiv, GitHub and
  openai/math links, a paste of at most 20,000 characters or the text-only
  guest uploads; members and administrators also generic HTTPS PDF/TeX
  links, longer pastes and PDF or archive uploads.
* Routes and models, exactly as for Analyze: guests run on sponsored Kimi K3
  only (and so do members in public mode, whose "own" keys would be the
  server's); members on sponsored Kimi K3 or any model of their own API keys;
  administrators, and members given full access (the default), also on the
  Codex or Claude Code account they linked themselves (response-only,
  tool-free; the request's ``own_subscriptions``).  Max and Max (API) are
  refused (``jobs.start_job``, and the runner), and so is another Kimi model
  than Kimi K3 on every route (``analyze_runs.kimi_model_error``).
* Sponsored capacity is Analyze's pool (``analyze_runs.GATE``): one pool for
  manuscript work, with refusals worded for Readability
  (:data:`GATE_MESSAGES`); a run holds the key ``<run id>`` and counts as a
  sponsored start, a question holds ``<run id>#ask`` and counts against the
  day's sponsored questions instead (:data:`ASK_GATE_MESSAGES`), which
  questions about analyses share.
* :func:`viewer_policy` is the per-viewer ``readability`` object of the
  engine listing, so the browser offers exactly what the server accepts.

A readability run reads someone's manuscript, so it is a manuscript run for
every security gate (``analyze_runs.is_manuscript_record``).  Its checks are a
model's judgements: nothing here calls them verified.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from agent_monitor import analyze_runs
from agent_monitor.analyze_runs import AnalyzeRequestError, GateMessages, role_of

RUN_KIND = analyze_runs.READABILITY
ENGINE_ID = analyze_runs.READABILITY
ROLES = analyze_runs.ROLES
MAX_FOCUS_CHARS = 2_000
WATCHDOG_ENV = "AGENT_MONITOR_READABILITY_WATCHDOG_SECONDS"
COMMAND_ENV = "READABILITY_CMD"
KIMI_MODEL = "kimi-k3"
# The question limits Analyze and Readability share (``analyze_runs``): a
# guest's questions per run and per session-hour, an account's per hour (both
# modes' questions together) and the questions a record keeps.
GUEST_ASKS_PER_RUN = analyze_runs.GUEST_ASKS_PER_RUN
GUEST_ASKS_PER_HOUR = analyze_runs.GUEST_ASKS_PER_HOUR
ASKS_PER_HOUR = analyze_runs.ASKS_PER_HOUR
ASKS_KEPT = analyze_runs.ASKS_KEPT

CONTINUE_REFUSAL = (
    "Readability runs can't be continued. Start a new one, or ask about the paper in the Ask panel."
)
DISABLED_MESSAGE = "Readability is turned off on this site."
GUEST_DISABLED_MESSAGE = "Readability needs a signed-in account on this site."
GUEST_LINK_REFUSAL = "Guests can use arXiv and GitHub links; this file needs a member account."
SOURCE_MISSING = "Paste a link or a manuscript, or upload files to make readable"
ONE_SOURCE = "Make one source readable at a time: a link, pasted text or uploaded files"
GUEST_ASK_LIMIT_MESSAGE = "Guests can ask {limit} questions about a run; sign in to ask more"
EDIT_REFUSAL = "A readability run has no editable proof; its readable version is generated from the manuscript"
COMPILE_REFUSAL = "Readability runs are never compiled: their TeX is a third-party manuscript"
CHECK_NOTE = "checked against the original by a model — not a certificate"
# As Analyze's: the way around a full sponsored pool is any model of the
# account's own API keys.
GATE_MESSAGES = GateMessages(
    off="Sponsored Readability is turned off on this site — use your own API key",
    guest_off="Sponsored Readability is turned off on this site — sign in and use your own API key",
    busy=("Sponsored Readability is busy ({active} manuscript runs are using it) — try again in a few minutes "
          "or use your own API key"),
    guest_busy="Sponsored Readability is busy ({active} manuscript runs are using it) — try again in a few minutes",
    guest_share_busy=("Sponsored Readability for guests is busy ({active} running) — try again in a few minutes, "
                      "or sign in"),
    daily="Sponsored Readability is at today's limit — try tomorrow or use your own API key",
    guest_daily="Sponsored Readability is at today's limit — try tomorrow, or sign in and use your own API key",
    guest_share_daily=("Sponsored Readability for guests is at today's limit — try tomorrow, or sign in and use "
                       "your own API key"),
    held="This run already holds a sponsored slot",
)
# A question holds a slot of the same pool while it is answered, but counts
# against the sponsored questions of the day (``analyze_runs.GATE`` with
# ``daily="asks"``, shared with the questions about analyses), never against
# the manuscript runs.
ASK_DAILY = analyze_runs.ASK_DAILY
ASK_GATE_MESSAGES = GateMessages(
    off="Sponsored questions are turned off on this site",
    guest_off="Sponsored questions are turned off for guests on this site — sign in to ask",
    busy="Sponsored Readability is busy ({active} manuscript runs are using it) — ask again in a few minutes",
    guest_busy="Sponsored Readability is busy ({active} manuscript runs are using it) — ask again in a few minutes",
    guest_share_busy=("Sponsored Readability for guests is busy ({active} running) — ask again in a few minutes, "
                      "or sign in"),
    daily="Sponsored questions are at today's limit — ask again tomorrow",
    guest_daily="Sponsored questions are at today's limit — ask again tomorrow",
    guest_share_daily="Sponsored questions for guests are at today's limit — ask again tomorrow, or sign in",
    held="A question about this run is already being answered",
)


def _limits():
    from agent_monitor.readability import limits

    return limits


def is_readability_record(record: Mapping[str, Any] | None) -> bool:
    """Whether a run record (or job) is a readability run."""
    data = record or {}
    return str(data.get("run_kind") or "") == RUN_KIND or str(data.get("engine") or "") == ENGINE_ID


def enabled() -> bool:
    return _limits().enabled()


def command_overridden() -> bool:
    """A ``READABILITY_CMD`` override makes the runner operator-chosen local code."""
    return bool(os.environ.get(COMMAND_ENV, "").strip())


def guests_enabled() -> bool:
    """Guests may start readability runs: the switches (by default as for
    Analyze), no command override, and sponsored Kimi offered to guests."""
    return _limits().guests_enabled() and not command_overridden() and not analyze_runs._guest_kimi_off()


def availability_error(user: Mapping[str, Any] | None) -> AnalyzeRequestError | None:
    """Why ``user`` may not use Readability at all right now, else ``None``."""
    if not enabled():
        return AnalyzeRequestError(DISABLED_MESSAGE, status=403)
    role = role_of(user)
    if role == "guest" and not guests_enabled():
        return AnalyzeRequestError(GUEST_DISABLED_MESSAGE, status=403)
    if role == "member":
        from agent_monitor import account_policy

        error = account_policy.member_engine_error(dict(user or {}), ENGINE_ID)
        if error:
            return AnalyzeRequestError(error, status=403)
    return None


def normalize_request(body: Mapping[str, Any], user: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the Readability fields of a ``POST /api/runs`` body.

    Returns ``{"source", "audience", "focus", "label", "connector",
    "attachments"}`` (see ``analyze_runs.normalize_source``).  Raises
    ``AnalyzeRequestError`` with a plain message.
    """
    from agent_monitor.readability.request import AUDIENCES, DEFAULT_AUDIENCE

    role = role_of(user)
    engine = str(body.get("engine") or ENGINE_ID).strip().lower()
    if engine != ENGINE_ID:
        raise AnalyzeRequestError('Readability runs use the "readability" engine')
    manuscript = analyze_runs.normalize_source(body, role, missing=SOURCE_MISSING, one_source=ONE_SOURCE,
                                               guest_refusal=GUEST_LINK_REFUSAL)
    options = body.get("readability")
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise AnalyzeRequestError("readability must be an object")
    audience = options.get("audience") or DEFAULT_AUDIENCE
    if audience not in AUDIENCES:
        raise AnalyzeRequestError('readability.audience must be "research", "graduate" or "undergraduate"')
    focus = options.get("focus")
    if focus is None:
        focus = ""
    if not isinstance(focus, str):
        raise AnalyzeRequestError("readability.focus must be text")
    focus = focus.strip()
    if len(focus) > MAX_FOCUS_CHARS:
        raise AnalyzeRequestError(f"The focus is limited to {MAX_FOCUS_CHARS:,} characters")
    return {"source": manuscript["source"], "audience": audience, "focus": focus, "label": manuscript["label"],
            "connector": manuscript["connector"], "attachments": manuscript["attachments"]}


def problem_text(label: str, focus: str = "") -> str:
    """The run's display problem (first line is the run-list label)."""
    from agent_monitor.analyze.titles import plain_title

    text = f"Readability · {plain_title(label, 200) or 'manuscript'}"
    focus = analyze_runs._printable(focus, MAX_FOCUS_CHARS)
    if focus:
        text += f"\nFocus: {focus}"
    return text


def request_caps(*, sponsored: bool) -> dict[str, int]:
    """What ``readability/request.json`` asks for: only lowerings of the
    role caps (a sponsored run stays inside the broker token's limits)."""
    if sponsored:
        return {"max_calls": analyze_runs.SPONSORED_MAX_CALLS, "max_seconds": analyze_runs.SPONSORED_MAX_SECONDS}
    return {}


def effective_caps(role: str, *, sponsored: bool, subscription: bool = False,
                   requested: Mapping[str, int] | None = None, env: Mapping[str, str] | None = None):
    """The caps the runner will apply (``readability.limits.caps_for``)."""
    return _limits().caps_for(role if role in ROLES else "guest", sponsored=sponsored or role == "guest",
                              env=env, requested=requested, subscription=subscription)


def watchdog_seconds(caps: Any, *, sponsored: bool, api_timeout: int | None = None) -> int:
    """Outer CLI watchdog for one run: its deadline plus one call plus publication."""
    return analyze_runs.watchdog_seconds(caps, sponsored=sponsored, api_timeout=api_timeout)


ask_key = analyze_runs.ask_key


def _caps_view(caps: Any) -> dict[str, int]:
    return {"max_parts": int(caps.max_parts), "max_calls": int(caps.max_calls), "max_seconds": int(caps.max_seconds)}


def viewer_policy(user: Mapping[str, Any] | None, *, guest_runs_per_hour: int = 1) -> dict[str, Any]:
    """The ``readability`` object of the engine listing for one viewer."""
    from agent_monitor import account_policy
    from agent_monitor.readability.prompts import AUDIENCE_LABELS
    from agent_monitor.readability.request import DEFAULT_AUDIENCE

    role = role_of(user)
    refusal = availability_error(user)
    sponsored_only = role == "guest" or account_policy.member_analyze_sponsored_only(dict(user or {}))
    sponsored_caps = effective_caps(role, sponsored=True, requested=request_caps(sponsored=True))
    policy: dict[str, Any] = {
        "enabled": refusal is None,
        "reason": str(refusal) if refusal is not None else None,
        "role": role,
        # An account given full access may use its own linked subscriptions.
        "routes": (["sponsored_kimi"] if sponsored_only
                   else ["sponsored_kimi", "api_key"] if account_policy.is_restricted(dict(user or {}))
                   else ["sponsored_kimi", "api_key", "codex_subscription", "claude_subscription"]),
        # The API models this viewer may name, as for Analyze: Kimi K3 alone
        # where the sponsored route is the only one (guests, members in public
        # mode), and the console's menu then offers just that; otherwise
        # ``None``, no list of its own: any model of the account's own API keys
        # (Settings' ``api_models``, with Kimi K3 where it is sponsored).  A
        # linked account runs its own models.
        "api_models": [KIMI_MODEL] if sponsored_only else None,
        "sponsored": analyze_runs.GATE.snapshot(guest=role == "guest"),
        "caps": {
            "sponsored": _caps_view(sponsored_caps),
            "own_key": None if sponsored_only else _caps_view(effective_caps(role, sponsored=False)),
        },
        "sources": {
            "links": sorted(analyze_runs.GUEST_CONNECTORS) + ([] if role == "guest" else ["https"]),
            "paste_chars": analyze_runs.paste_limit(role),
            "uploads": "text" if role == "guest" else "manuscript",
            "upload_extensions": analyze_runs._upload_extensions(role),
        },
        "audiences": [{"id": key, "label": label} for key, label in AUDIENCE_LABELS.items()],
        "default_audience": DEFAULT_AUDIENCE,
        "focus_chars": MAX_FOCUS_CHARS,
        "asks": analyze_runs.asks_policy(refusal, role, mode=RUN_KIND),
        "check_note": CHECK_NOTE,
        "continue": False,
    }
    if role == "guest":
        policy["runs_per_hour"] = guest_runs_per_hour
    return policy
