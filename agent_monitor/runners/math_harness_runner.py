#!/usr/bin/env python3
"""Bounded, response-only Math Agent Harness running the CGM ("Ansatz") pipeline.

Every model turn is a tool-free chat call through one of three routes
(``api_chat`` for API/Kimi models, ``codex_exec`` and ``claude_code`` for
account subscriptions).  The trusted wrapper alone parses replies and writes
artifacts in the run workspace.  The pipeline is derived from the CGM prompt
pack in ``agent_monitor.cgm`` (Apache-2.0, Copyright 2026 FrenzyMath; see
``agent_monitor/cgm/NOTICE.md``):

1. Scaffold -- validate the workspace and the canonical problem (no calls).
2. Explore -- one strategist call proposes 1..N plans; one independent worker
   per plan (default up to 3) returns delimited lemma blocks, a THEOREM block,
   a self-status and typed findings.  Workers run concurrently.
3. Verify -- candidates are verified solved-first; inside a candidate every
   lemma of the THEOREM's closure, then the THEOREM, gets its own independent
   verifier call in the written topological order, seeing only that
   statement, its proof and the statements of its already-accepted
   predecessors (blocks whose predecessors are accepted are verified
   concurrently; the first block in written order that is not accepted
   decides, as one call at a time would).  A rejection triggers a bounded
   repair driven by the verifier's ``repair_hints`` (default at most 2
   repairs per run); unchanged blocks keep their cached verdicts.
   Verification stops as soon as a THEOREM closure is accepted.
4. Curate -- one curator call scores the exploring-graph frontier; the CGM
   AND/OR propagation is applied deterministically in code.
5. Synthesize -- CGM-format facts for the verified closure, the exploring
   graph (global_memory/*.jsonl), a deterministic ``proof.md`` and a receipt
   binding everything.

Model-verifier acceptance is never a Lean certificate: receipts always carry
``certificate: false`` and ``lean_verified: false``.

Concurrency (``AGENT_MONITOR_CGM_CONCURRENCY``; default 3 on API routes, 2
for guest/public/sponsored runs, 1 on the Codex/Claude subscription routes)
bounds how many provider calls are in flight.  Calls are reserved against the
call budget before they start, and every budget decision is taken from
settled state, so the calls made, the receipt and every artifact depend only
on the replies and the configured concurrency -- never on completion order.
A stop request (SIGTERM) abandons in-flight calls and publishes nothing (the
process exits 143); once the wall-clock budget plus a short grace has elapsed,
in-flight calls are abandoned and what has settled is published.  An
abandoned call can never change an artifact, and a stop request never
interrupts a publication that has started.

Usage: ``math_harness_runner.py`` from a run workspace containing the canonical
``problem.txt``.  A direct operator may provide one argument as additional task
context.  The process emits Codex-shaped JSONL events and exits successfully
only after a substantive root ``proof.md`` has been written.  Library callers
use :func:`run_pipeline`.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import math
import os
import queue
import re
import secrets
import signal
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

if __package__:
    from . import claude_backend as _claude_backend
    from . import codex_backend as _codex_backend
    from .api_backend import APIBackendError, api_chat
    from .claude_backend import (
        ClaudeBackendError,
        claude_exec as claude_code_exec,
        forward_as_codex_event,
        subscription_enabled as claude_subscription_enabled,
    )
    from .codex_backend import (
        CodexBackendError,
        codex_exec,
        subscription_enabled as codex_subscription_enabled,
    )
else:
    # The registry launches this file by absolute path from the run workspace.
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from agent_monitor.runners import claude_backend as _claude_backend
    from agent_monitor.runners import codex_backend as _codex_backend
    from agent_monitor.runners.api_backend import APIBackendError, api_chat
    from agent_monitor.runners.claude_backend import (
        ClaudeBackendError,
        claude_exec as claude_code_exec,
        forward_as_codex_event,
        subscription_enabled as claude_subscription_enabled,
    )
    from agent_monitor.runners.codex_backend import (
        CodexBackendError,
        codex_exec,
        subscription_enabled as codex_subscription_enabled,
    )

from agent_monitor import kimi_k3
from agent_monitor.cgm import closure as cgm_closure
from agent_monitor.cgm import curation as cgm_curation
from agent_monitor.cgm import parsing as cgm_parsing
from agent_monitor.cgm import prompts as cgm_prompts
from agent_monitor.cgm import schema as cgm_schema
from agent_monitor.cgm.parsing import Candidate, ParseError, VerifierReport


ADAPTER_SOURCE = cgm_schema.PIPELINE_SOURCE
AUTHOR = cgm_schema.AUTHOR
# The previous single-critic adapter generation; still recognized so that a
# continuation can retire its artifacts.
LEGACY_ADAPTER_SOURCE = "agent-monitor-math-harness-response-adapter@1"
LEGACY_AUTHOR = "math_harness_model_critic"

MAX_PROBLEM_BYTES = 44_000
MAX_TASK_BYTES = 160_000
MAX_MODEL_PROMPT_BYTES = 120_000
MAX_SYSTEM_PROMPT_BYTES = 12_000
# Per-reply byte caps.  Kimi proofs routinely exceed the old 20,000 B cap;
# engine_graph accepts a proof.md of up to 200,000 B.
MAX_STRATEGIST_BYTES = 48_000
MAX_WORKER_BYTES = 160_000
MAX_VERIFIER_BYTES = 64_000
MAX_CURATOR_BYTES = 48_000
MAX_PROOF_BYTES = 190_000
MAX_FACT_BYTES = 240_000
MAX_RECEIPT_BYTES = 64_000
MAX_MEMORY_FILE_BYTES = 1_000_000
MAX_ATTEMPT_EVIDENCE_BYTES = 60_000
MAX_CANDIDATES = 3
# The Codex response-only contract this runner requires and records.  Version
# 5 is the first whose "no tools" claim holds for every bundled model (earlier
# builds of the adapter could hand "responses lite" models code-mode and
# multi-agent tools); receipts claiming 4 are rejected.
CODEX_RESPONSE_ONLY_ISOLATION_VERSION = 5
API_RESPONSE_ONLY_SCHEMA_VERSION = 1
_CLAUDE_STAGE_TIMEOUT_DEFAULTS = {
    "planner": 600.0,
    "candidate": 1200.0,
    "critic": 600.0,
    "revision": 1200.0,
}
# New stage families map onto the historical timeout families (and env names).
_STAGE_TIMEOUT_FAMILY = {
    "strategist": "planner",
    "planner": "planner",
    "worker": "candidate",
    "candidate": "candidate",
    "verify": "critic",
    "critic": "critic",
    "curator": "critic",
    "repair": "revision",
    "revision": "revision",
}
_MIN_CLAUDE_STAGE_TIMEOUT = 60.0
_MAX_CLAUDE_STAGE_TIMEOUT = 3600.0
DEFAULT_TASK_DIRECTIVE = (
    "Solve the canonical mathematical problem rigorously with the bounded "
    "Math Agent Harness workflow."
)

_SPACE_RE = re.compile(r"\s+")
_SAFE_PROBLEM_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FACT_FILE_RE = re.compile(r"^([0-9a-f]{16})\.md$")
_TEMP_SUFFIX_RE = re.compile(r"^[0-9a-f]{24}$")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_PSEUDO_TOOL_MARKERS = cgm_parsing.PSEUDO_TOOL_MARKERS
_KIMI_EFFORTS = ("low", "high", "max")

# Budget defaults and hard ceilings: (default, minimum, maximum).  Guest,
# public and sponsored runs use the lower guest profile, whose defaults are
# also ceilings.
_BUDGET_PROFILE = {
    "max_calls": (16, 4, 64),
    "max_workers": (3, 1, MAX_CANDIDATES),
    "max_repairs": (2, 0, 4),
    "max_verify_candidates": (2, 1, MAX_CANDIDATES),
    "max_lemmas": (8, 1, 16),
    "max_seconds": (5400, 120, 14_400),
}
_GUEST_BUDGET_PROFILE = {
    "max_calls": (12, 4, 12),
    "max_workers": (2, 1, 2),
    "max_repairs": (1, 0, 1),
    "max_verify_candidates": (2, 1, 2),
    "max_lemmas": (6, 1, 6),
    "max_seconds": (2400, 120, 2400),
}
_BUDGET_ENV = {
    "max_calls": ("AGENT_MONITOR_CGM_MAX_CALLS",),
    "max_workers": (
        "AGENT_MONITOR_CGM_MAX_WORKERS",
        "AGENT_MONITOR_MATH_HARNESS_MAX_WORKERS",
    ),
    "max_repairs": ("AGENT_MONITOR_CGM_MAX_REPAIRS",),
    "max_verify_candidates": ("AGENT_MONITOR_CGM_MAX_VERIFY_CANDIDATES",),
    "max_lemmas": ("AGENT_MONITOR_CGM_MAX_LEMMAS",),
    "max_seconds": ("AGENT_MONITOR_CGM_MAX_SECONDS",),
}
# Provider calls in flight at once: (default, minimum, maximum).  The Codex and
# Claude subscription routes default to 1 unless the variable is set.
_CONCURRENCY_ENV = "AGENT_MONITOR_CGM_CONCURRENCY"
_CONCURRENCY_PROFILE = (3, 1, cgm_schema.MAX_CONCURRENCY)
_GUEST_CONCURRENCY_PROFILE = (
    cgm_schema.GUEST_MAX_CONCURRENCY,
    1,
    cgm_schema.GUEST_MAX_CONCURRENCY,
)
_SUBSCRIPTION_TRANSPORTS = frozenset({"codex_exec", "claude_code"})
# Max mode (see agent_monitor/cgm_max.py): the advisor route answers these call
# families and the worker route the rest.
MAX_ROUTE_MODE_ENV = "AGENT_MONITOR_CGM_ROUTE_MODE"
MAX_CODEX_CALL_HOME_ENV = "AGENT_MONITOR_CODEX_CALL_HOME"
ADVISOR_FAMILIES = cgm_schema.ADVISOR_CALL_FAMILIES
WORKER_FAMILIES = cgm_schema.WORKER_CALL_FAMILIES
# One logical turn makes at most: the call, its low-effort retry, the format
# retry and that retry's low-effort retry.
_MAX_CALLS_PER_TURN = 4
# In-flight calls get this long past the wall-clock budget (their timeouts are
# capped to it) before they are abandoned.
_DEADLINE_GRACE_SECONDS = 15.0
_WAIT_POLL_SECONDS = 0.2
# Sponsored Kimi only: when the provider refused a call before processing it
# (kimi_k3.sponsored_retryable, after the transport retries), the strategist
# call waits and is sent again after each of these pauses in turn, and when
# every worker was refused so, one rescue worker turn starts after a pause.
# No pause may leave the run less than the margin.
_STRATEGIST_BUSY_WAITS = (30.0, 60.0, 120.0)
_RESCUE_WORKER_WAIT = 60.0
_BUSY_WAIT_MARGIN_SECONDS = 300.0
# Claude Code / Codex failure kinds (ClaudeBackendError.kind,
# CodexBackendError.kind) that every later call on the same linked account
# would hit as well; the Analyze runner stops its checks on the same kinds.
_ACCOUNT_STOP_KINDS = frozenset({"auth", "usage_limit", "model"})


class MathHarnessError(RuntimeError):
    """A fail-closed workflow, validation, or artifact error.

    ``failure`` is the sponsored Kimi call failure behind it, if any
    (``kimi_k3.CallFailure``): content-free facts a caller may turn into a
    message with ``kimi_k3.failure_message``.  ``billed`` is ``False`` only
    when no call of the run can have been processed (and billed) by then,
    so such a message may say that the run used no tokens.
    """

    def __init__(
        self, *args: object, failure: object | None = None, billed: bool = True
    ) -> None:
        super().__init__(*args)
        self.failure = failure
        self.billed = billed


class RunCancelled(MathHarnessError):
    """The run was stopped (SIGTERM or a cancel request); nothing was published."""


class ProviderCallError(MathHarnessError):
    """A provider call failed; the model may already have billed for it
    (unless ``failure.retryable``: it was refused before any processing)."""


class EmptyReplyError(ProviderCallError):
    """The provider returned no visible text, even after the allowed retry."""


class PromptBoundError(MathHarnessError):
    """A model prompt built from bounded parts still exceeds its byte bound.

    Raised before any call is made; optional stages degrade instead of
    aborting the run.
    """


class _BudgetExhausted(Exception):
    """No call or wall-clock budget remains for an optional stage."""


class _TurnAbandoned(Exception):
    """The coordinator abandoned this turn (stop request or deadline)."""


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    transport: str


@dataclass(frozen=True)
class Reply:
    text: str
    usage: dict[str, int]
    provider: str
    model: str
    observed_model: str | None = None


@dataclass(frozen=True)
class Budgets:
    """Per-run call and time budgets (see ``Budgets.from_env``)."""

    max_calls: int = 16
    max_workers: int = 3
    max_repairs: int = 2
    max_verify_candidates: int = 2
    max_lemmas: int = 8
    max_seconds: int = 5400
    guest: bool = False
    # Provider calls in flight at once; ``None`` means the route default
    # (see ``resolved_concurrency``).
    concurrency: int | None = None

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> "Budgets":
        """Resolve bounded budgets from ``AGENT_MONITOR_CGM_*`` variables.

        ``AGENT_MONITOR_CGM_MAX_CALLS`` (total provider calls, retries
        included; default 16), ``..._MAX_WORKERS`` (default 3, also bounded by
        ``AGENT_MONITOR_MAX_ITERATIONS``), ``..._MAX_REPAIRS`` (default 2),
        ``..._MAX_VERIFY_CANDIDATES`` (default 2), ``..._MAX_LEMMAS`` (default
        8), ``..._MAX_SECONDS`` (default 5400) and ``..._CONCURRENCY``
        (provider calls in flight, 1..6; unset means the route default: 3 on
        API routes, 1 on subscription routes).  Guest, public and sponsored
        runs use lower defaults that are also ceilings (12 calls, 2 workers, 1
        repair, 6 lemmas, 2400 s, concurrency 2).
        """
        env = source if source is not None else os.environ
        guest = _is_guest_run(env)
        profile = _GUEST_BUDGET_PROFILE if guest else _BUDGET_PROFILE
        values: dict[str, int] = {}
        for name, (default, minimum, maximum) in profile.items():
            values[name] = _bounded_int(
                env,
                _BUDGET_ENV[name],
                default=default,
                minimum=minimum,
                maximum=maximum,
            )
        values["max_workers"] = min(values["max_workers"], candidate_count(env))
        concurrency: int | None = None
        if str(env.get(_CONCURRENCY_ENV, "")).strip():
            default, minimum, maximum = (
                _GUEST_CONCURRENCY_PROFILE if guest else _CONCURRENCY_PROFILE
            )
            concurrency = _bounded_int(
                env,
                (_CONCURRENCY_ENV,),
                default=default,
                minimum=minimum,
                maximum=maximum,
            )
        return cls(guest=guest, concurrency=concurrency, **values)

    def resolved_concurrency(self, route: Route) -> int:
        """Calls in flight for ``route``: explicit value, else the route
        default (1 on subscription routes); guest runs are capped at 2."""
        default, minimum, maximum = (
            _GUEST_CONCURRENCY_PROFILE if self.guest else _CONCURRENCY_PROFILE
        )
        if self.concurrency is None:
            value = 1 if route.transport in _SUBSCRIPTION_TRANSPORTS else default
        elif isinstance(self.concurrency, bool) or not isinstance(self.concurrency, int):
            raise MathHarnessError("concurrency must be an integer")
        else:
            value = self.concurrency
        return max(minimum, min(value, maximum))

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "max_workers": self.max_workers,
            "max_repairs": self.max_repairs,
            "max_verify_candidates": self.max_verify_candidates,
            "max_lemmas": self.max_lemmas,
            "max_seconds": self.max_seconds,
            "guest": self.guest,
            "concurrency": self.concurrency,
        }


def _is_guest_run(env: Mapping[str, str]) -> bool:
    return any(
        str(env.get(name, "")).strip() == "1"
        for name in (
            "AGENT_MONITOR_GUEST",
            "AGENT_MONITOR_PUBLIC_KIMI",
            "AGENT_MONITOR_SPONSORED_KIMI",
        )
    )


# ---------------------------------------------------------------------------
# event emission
# ---------------------------------------------------------------------------

class _RunEmitter:
    """One run's event delivery: one event at a time, in order.

    The lock is per run, so a slow or blocked sink only ever delays its own
    run.  ``sink`` None resolves the module-level :func:`emit` at call time
    (tests and library callers may replace it).  ``admit`` is checked under
    the lock just before delivery; once the run is closed nothing more is
    delivered.
    """

    def __init__(self, sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._sink = sink
        self._lock = threading.RLock()
        self._closed = False

    def deliver(
        self, obj: dict[str, Any], *, admit: Callable[[], bool] | None = None
    ) -> None:
        with self._lock:
            if self._closed or (admit is not None and not admit()):
                return
            (self._sink or emit)(obj)

    def close(self) -> None:
        """Let an event being delivered finish, then deliver nothing more."""
        with self._lock:
            self._closed = True


# run_pipeline installs its run's emitter here; it is context-local so
# concurrent runs in one process never share a sink or a lock, and every turn
# thread runs in a copy of the run's context.
_RUN_EMITTER: contextvars.ContextVar[_RunEmitter | None] = contextvars.ContextVar(
    "math_harness_run_emitter", default=None
)
# The default sink is the process's stdout, which every run in the process
# shares: one JSONL line is written at a time.
_STDOUT_LOCK = threading.Lock()


def emit(obj: dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _STDOUT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def _out(obj: dict[str, Any]) -> None:
    emitter = _RUN_EMITTER.get()
    if emitter is None:
        # Outside a run (for example main()'s final events).
        emit(obj)
    else:
        emitter.deliver(obj)


def emit_item(item: dict[str, Any]) -> None:
    _out({"type": "item.completed", "item": item})


def _emit_backend_event(
    event: dict[str, Any], *, out: Callable[[dict[str, Any]], None] | None = None
) -> None:
    """Forward activity while reserving usage projection for each stage.

    A provider call's own terminal events never end the run: usage is
    projected per stage (``_emit_stage_usage``) and a failed call (Codex
    ``turn.failed`` / ``turn.cancelled``) is shown as a non-terminal error
    item.  The pipeline itself decides the run's outcome (a failed curator or
    verifier call degrades the run; it does not unpublish its proof) and
    reports a fatal error with its own terminal error item.
    """
    event_type = event.get("type")
    if event_type == "turn.completed":
        return
    if event_type in {"turn.failed", "turn.cancelled"}:
        detail = event.get("error") or event.get("message") or event_type
        if isinstance(detail, Mapping):
            detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
        event = {
            "type": "item.completed",
            "item": {
                "type": "error",
                "message": "provider call failed: "
                + " ".join(str(detail).split())[-1200:],
            },
        }
    (out or _out)(event)


def _emit_stage_usage(
    stage: str,
    usage: Mapping[str, int],
    *,
    out: Callable[[dict[str, Any]], None] | None = None,
    input_includes_cache: bool = True,
) -> None:
    """Publish already-incurred usage immediately, even if a later stage fails.

    The run's event stream uses Codex usage semantics (``input_tokens``
    includes cached input). Claude reports cache reads and writes apart from
    ``input_tokens``; that usage is flagged ``input_excludes_cache`` and the
    server's parser folds the cache in (a parser that predates the flag reads
    it as before, without pricing cached input twice).
    """
    projected = dict(usage)
    cache_read = int(usage.get("cache_read_tokens") or 0)
    projected.setdefault("cached_input_tokens", cache_read)
    if not input_includes_cache:
        projected["input_excludes_cache"] = True
    (out or _out)({"type": "turn.completed", "stage": stage, "usage": projected})


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_int(
    source: Mapping[str, str],
    names: tuple[str, ...],
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = next((source[name] for name in names if str(source.get(name, "")).strip()), None)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise MathHarnessError(f"{names[0]} must be an integer") from exc
    return max(minimum, min(parsed, maximum))


def _stage_family(stage: str) -> str:
    base = str(stage or "").split("-", 1)[0]
    return _STAGE_TIMEOUT_FAMILY.get(base, base)


def _claude_stage_timeout(
    stage: str,
    source: Mapping[str, str] | None = None,
) -> float:
    """Return a bounded wall-clock budget for one response-only Claude stage.

    Workers and repairs need more time than the compact JSON strategist,
    verifier and curator stages; they map onto the historical ``candidate`` /
    ``revision`` and ``planner`` / ``critic`` override names.  A Math
    Harness-specific override wins over the shared Claude timeout, and a
    stage-family override wins over both.  There is deliberately no automatic
    timeout retry because the provider may already have performed billable
    inference before the terminal event was lost.
    """
    env = source if source is not None else os.environ
    family = _stage_family(stage)
    default = _CLAUDE_STAGE_TIMEOUT_DEFAULTS.get(family, 600.0)
    stage_name = re.sub(r"[^A-Za-z0-9]+", "_", family).strip("_").upper()
    names = (
        f"AGENT_MONITOR_MATH_HARNESS_CLAUDE_{stage_name}_TIMEOUT",
        "AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT",
        "AGENT_MONITOR_CLAUDE_TIMEOUT",
    )
    raw = next(
        (str(env[name]).strip() for name in names if str(env.get(name, "")).strip()),
        None,
    )
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as exc:
        raise MathHarnessError(f"{names[0]} must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise MathHarnessError(f"{names[0]} must be greater than zero")
    return max(_MIN_CLAUDE_STAGE_TIMEOUT, min(parsed, _MAX_CLAUDE_STAGE_TIMEOUT))


def claude_pipeline_budget_seconds(source: Mapping[str, str] | None = None) -> int:
    """Upper bound on the Claude-route wall clock for one pipeline run.

    Every call is budgeted at the longest stage timeout (workers/repairs),
    capped by the run's own ``max_seconds``, plus a publication margin.
    Intended for the outer process watchdog.
    """
    env = source if source is not None else os.environ
    budgets = Budgets.from_env(env)
    slowest = max(
        _claude_stage_timeout(stage, env)
        for stage in ("strategist", "worker-1", "verify-1", "repair-1", "curator")
    )
    return int(min(budgets.max_calls * slowest, budgets.max_seconds) + 300)


def candidate_count(source: Mapping[str, str] | None = None) -> int:
    """Number of independent workers (bounded by the iteration choice)."""
    env = source if source is not None else os.environ
    guest = _is_guest_run(env)
    default, _minimum, maximum = (
        _GUEST_BUDGET_PROFILE if guest else _BUDGET_PROFILE
    )["max_workers"]
    workers = _bounded_int(
        env,
        _BUDGET_ENV["max_workers"],
        default=default,
        minimum=1,
        maximum=maximum,
    )
    iterations = _bounded_int(
        env,
        ("AGENT_MONITOR_MAX_ITERATIONS", "MATH_HARNESS_MAX_ITERATIONS"),
        default=MAX_CANDIDATES,
        minimum=1,
        maximum=200,
    )
    return max(1, min(MAX_CANDIDATES, workers, iterations))


def _codex_route(env: Mapping[str, str]) -> Route:
    isolation_version = getattr(
        _codex_backend, "RESPONSE_ONLY_TOOL_ISOLATION_VERSION", None
    )
    if (
        isolation_version != CODEX_RESPONSE_ONLY_ISOLATION_VERSION
        or isinstance(isolation_version, bool)
    ):
        raise MathHarnessError(
            "Codex backend does not guarantee response-only tool isolation"
        )
    return Route(
        provider="codex_subscription",
        model=str(env.get("AGENT_MONITOR_CODEX_MODEL") or "gpt-5.6-sol").strip(),
        transport="codex_exec",
    )


def _claude_route(env: Mapping[str, str]) -> Route:
    if (
        getattr(_claude_backend, "RESPONSE_ONLY_TOOL_ISOLATION_VERSION", None)
        != 1
    ):
        raise MathHarnessError(
            "Claude backend does not guarantee response-only tool isolation"
        )
    return Route(
        provider="claude_subscription",
        model=str(env.get("AGENT_MONITOR_CLAUDE_MODEL") or "sonnet").strip(),
        transport="claude_code",
    )


def _max_mode(env: Mapping[str, str]) -> bool:
    return str(env.get(MAX_ROUTE_MODE_ENV) or "").strip().lower() == "max"


def _select_routes(source: Mapping[str, str] | None = None) -> tuple[Route, Route | None]:
    """The run's route, and in Max mode the separate worker route.

    Max: Claude Code (strategist, verifier, curator) leads and Codex (workers,
    repairs) works, each on its own linked account.  Every other run has one
    route and no worker route.
    """
    env = source if source is not None else os.environ
    if not _max_mode(env):
        return _select_route(env), None
    if not (codex_subscription_enabled(env) and claude_subscription_enabled(env)):
        raise MathHarnessError(
            "Max needs both the Claude Code and the Codex account routes"
        )
    return _claude_route(env), _codex_route(env)


def _select_route(source: Mapping[str, str] | None = None) -> Route:
    env = source if source is not None else os.environ
    use_codex = codex_subscription_enabled(env)
    use_claude = claude_subscription_enabled(env)
    if use_codex and use_claude:
        raise MathHarnessError(
            "Codex and Claude account routes cannot both be active"
        )
    if use_codex:
        return _codex_route(env)
    if use_claude:
        return _claude_route(env)
    model = str(
        env.get("MATH_HARNESS_MODEL")
        or env.get("AGENT_MONITOR_SELECTED_MODEL")
        or env.get("AGENT_MONITOR_OPENAI_MODEL")
        or "gpt-5.2"
    ).strip()
    if not model:
        raise MathHarnessError("Math Harness API model is empty")
    return Route(provider="api", model=model, transport="api_chat")


def _response_only_contract(route: Route) -> dict[str, Any]:
    versions = {
        "codex_exec": CODEX_RESPONSE_ONLY_ISOLATION_VERSION,
        "claude_code": 1,
        "api_chat": API_RESPONSE_ONLY_SCHEMA_VERSION,
    }
    version = versions.get(route.transport)
    if version is None:
        raise MathHarnessError("unknown response-only transport contract")
    return {
        "transport": route.transport,
        "isolation_version": version,
    }


def _clean_usage(raw: object) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    usage: dict[str, int] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "reasoning_tokens",
    ):
        try:
            value = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            usage[key] = value
    return usage


compute_fact_id = cgm_schema.compute_fact_id


def _clip_utf8(text: str, limit: int) -> str:
    payload = text.encode("utf-8")
    if len(payload) <= limit:
        return text
    marker = b"\n\n[bounded excerpt omitted]\n\n"
    room = max(0, limit - len(marker))
    head = room // 2
    tail = room - head
    return (
        payload[:head].decode("utf-8", errors="ignore")
        + marker.decode("ascii")
        + payload[-tail:].decode("utf-8", errors="ignore")
    )


# ---------------------------------------------------------------------------
# provider calls
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    """One logical model turn: a call and its bounded retries, on one thread.

    Its calls are reserved against the run budget when the turn opens
    (``limit``); unused reservations return when it closes.  Once abandoned
    (stop request or deadline) the turn makes no further call, emits no event
    and changes no record, so a late reply can never reach an artifact.
    """

    seq: int
    stage: str
    limit: int
    used: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    replies: list[tuple[str, Reply]] = field(default_factory=list)
    closed: bool = False
    abandoned: bool = False
    # Whether a call of this turn may have reached processing (and been
    # billed): a reply came back, or it failed other than as a refusal
    # before processing (kimi_k3.sponsored_retryable).
    processed: bool = False
    gate: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def last_reply(self) -> Reply | None:
        with self.gate:
            return self.replies[-1][1] if self.replies else None


@dataclass
class _Ctx:
    route: Route
    budgets: Budgets
    started: float
    env: Mapping[str, str] = field(default_factory=dict)
    # Max mode: workers and repairs use this route; ``route`` answers the rest.
    worker_route: Route | None = None
    api_timeout: int = 300
    concurrency: int = 1
    cancel: threading.Event = field(default_factory=threading.Event)
    emitter: _RunEmitter = field(default_factory=_RunEmitter)
    turns: list[_Turn] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    verify_seq: int = 0
    repair_seq: int = 0
    used: int = 0
    reserved: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Server-sponsored Kimi: the run's shared cap on transport retries
    # (kimi_k3.RetryAllowance) and the strategist's busy waits still unused.
    sponsored_kimi: bool = False
    kimi_retries: Any = None
    busy_waits: list[float] = field(default_factory=list)
    # Whether any call of the run may have reached processing (_Turn.processed).
    processed: bool = False
    # Routes whose linked account failed in a way every later call would
    # repeat (_ACCOUNT_STOP_KINDS), with why: no later verification, repair
    # or curator call goes to them.
    stopped_routes: dict[Route, str] = field(default_factory=dict)
    # Prefix of the per-call progress notes (other response-only runners that
    # reuse this turn machinery, such as Analyze, name themselves here).
    label: str = "Math Harness"

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Every call record in canonical turn order, never completion order."""
        with self.lock:
            turns = sorted(self.turns, key=lambda turn: turn.seq)
            return [dict(record) for turn in turns for record in turn.calls]

    @property
    def calls_used(self) -> int:
        with self.lock:
            return self.used

    @property
    def deadline(self) -> float:
        return self.started + self.budgets.max_seconds

    def deadline_passed(self) -> bool:
        return time.monotonic() >= self.deadline

    def route_for(self, stage: str) -> Route:
        """The route that answers ``stage`` (always ``route`` outside Max)."""
        if self.worker_route is not None and cgm_schema.call_family(stage) in WORKER_FAMILIES:
            return self.worker_route
        return self.route

    def env_for(self, route: Route) -> Mapping[str, str]:
        """The provider environment for ``route``.

        In Max mode each side sees only its own account: Codex calls never
        receive the Claude configuration directory and use their own home;
        Claude calls never receive the Codex home.
        """
        if self.worker_route is None:
            return self.env
        env = dict(self.env)
        if route.transport == "codex_exec":
            for name in ("CLAUDE_CONFIG_DIR", "AGENT_MONITOR_CLAUDE_SUBSCRIPTION", "AGENT_MONITOR_CLAUDE_EFFORT"):
                env.pop(name, None)
            call_home = str(self.env.get(MAX_CODEX_CALL_HOME_ENV) or "").strip()
            if call_home:
                env["HOME"] = call_home
        else:
            for name in ("CODEX_HOME", "AGENT_MONITOR_CODEX_SUBSCRIPTION"):
                env.pop(name, None)
        env.pop(MAX_CODEX_CALL_HOME_ENV, None)
        return env

    def remaining(self) -> int:
        """Calls neither made nor reserved by an open turn."""
        with self.lock:
            return self.budgets.max_calls - self.used - self.reserved

    def can_call(self, *, reserve: int = 0) -> bool:
        return (
            self.remaining() >= 1 + reserve
            and not self.deadline_passed()
            and not self.cancel.is_set()
        )

    def degrade(self, reason: str) -> None:
        if reason not in self.degraded:
            self.degraded.append(reason)

    def open_turn(self, stage: str, limit: int) -> _Turn:
        """Reserve ``limit`` calls for one turn before it is launched."""
        if self.cancel.is_set():
            raise RunCancelled("the Math Harness run was stopped")
        if limit < 1 or self.deadline_passed():
            raise _BudgetExhausted(f"no budget for {stage}")
        with self.lock:
            if self.used + self.reserved + limit > self.budgets.max_calls:
                raise _BudgetExhausted(f"no budget for {stage}")
            turn = _Turn(seq=len(self.turns) + 1, stage=stage, limit=limit)
            self.turns.append(turn)
            self.reserved += limit
        return turn

    def close_turn(self, turn: _Turn, *, abandon: bool = False) -> None:
        with turn.gate:
            if abandon:
                turn.abandoned = True
            with self.lock:
                if not turn.closed:
                    self.reserved -= turn.limit - turn.used
                    turn.closed = True

    def turn_can_call(self, turn: _Turn) -> bool:
        with turn.gate:
            if turn.abandoned or turn.closed:
                return False
            with self.lock:
                return (
                    turn.used < turn.limit
                    and not self.cancel.is_set()
                    and time.monotonic() < self.deadline
                )

    def begin_call(self, turn: _Turn, record: dict[str, Any]) -> None:
        """Consume one reserved call; the record exists before the call."""
        with turn.gate:
            if turn.abandoned or turn.closed or self.cancel.is_set():
                raise _TurnAbandoned(record["stage"])
            with self.lock:
                if turn.used >= turn.limit or time.monotonic() >= self.deadline:
                    raise _BudgetExhausted(f"no budget for {record['stage']}")
                turn.used += 1
                self.used += 1
                self.reserved -= 1
                turn.calls.append(record)

    def finish_call(
        self, turn: _Turn, record: dict[str, Any], reply: Reply, **updates: Any
    ) -> bool:
        """Settle a call's record; ``False`` if the turn was abandoned."""
        with turn.gate:
            if turn.abandoned:
                return False
            record.update(updates)
            turn.replies.append((str(record["stage"]), reply))
            return True

    def bill_failed_call(
        self, turn: _Turn, record: dict[str, Any], usage: dict[str, int]
    ) -> bool:
        """Record the usage of a failed but billed call (an empty completion,
        for instance); ``False`` if the turn was abandoned."""
        with turn.gate:
            if turn.abandoned:
                return False
            record["usage"] = dict(usage)
            return True

    def note_processed(self, turn: _Turn) -> None:
        """Record that a call of ``turn`` may have reached processing."""
        with turn.gate:
            turn.processed = True
            with self.lock:
                self.processed = True

    def turn_processed(self, turn: _Turn) -> bool:
        with turn.gate:
            return turn.processed

    def run_processed(self) -> bool:
        """Whether any call of the run may have reached processing so far."""
        with self.lock:
            return self.processed

    def turn_live(self, turn: _Turn) -> bool:
        """Whether ``turn`` may still finish the call it has begun."""
        with turn.gate:
            return (
                not turn.abandoned
                and not turn.closed
                and not self.cancel.is_set()
                and time.monotonic() < self.deadline
            )

    def turn_out(self, turn: _Turn, obj: dict[str, Any]) -> None:
        """Emit one event for a live turn; abandoned turns stay silent.

        Whether the turn is live is checked under the run's emit lock just
        before delivery, and the turn's gate is never held while the sink
        runs: a slow sink cannot delay abandonment, and nothing is delivered
        for a turn once it has been abandoned.
        """

        def live() -> bool:
            with turn.gate:
                return not turn.abandoned

        self.emitter.deliver(obj, admit=live)


def _is_kimi_route(route: Route) -> bool:
    return route.transport == "api_chat" and "kimi" in route.model.lower()


_SMALL_OUTPUT_CEILING = 8192


def _output_ceiling(env: Mapping[str, str]) -> int | None:
    values = []
    for name in ("AGENT_MONITOR_MAX_OUTPUT_TOKENS", "AGENT_MONITOR_API_MAX_TOKENS"):
        try:
            value = int(str(env.get(name) or "").strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return min(values) if values else None


def _stage_effort(family: str, source: Mapping[str, str] | None = None) -> str:
    """Kimi reasoning effort per stage family.

    Kimi's reasoning tokens count against ``max_tokens``.  The curator always
    uses ``low``; ``AGENT_MONITOR_CGM_REASONING_EFFORT`` overrides every other
    stage; long-output workers and repairs drop to ``low`` under a small
    output ceiling (guest and sponsored runs); otherwise the configured
    ``KIMI_REASONING_EFFORT`` or ``high`` applies.  The effort is passed to
    ``api_chat`` per call; the process environment is never modified.
    """
    env = source if source is not None else os.environ
    if family == "curator":
        return "low"
    explicit = str(env.get("AGENT_MONITOR_CGM_REASONING_EFFORT") or "").strip().lower()
    if explicit in _KIMI_EFFORTS:
        return explicit
    ceiling = _output_ceiling(env)
    if family in {"worker", "repair"} and ceiling is not None and ceiling <= _SMALL_OUTPUT_CEILING:
        return "low"
    configured = str(env.get("KIMI_REASONING_EFFORT") or "").strip().lower()
    return configured if configured in _KIMI_EFFORTS else "high"


def _deadline_timeout(ctx: _Ctx, default: float) -> float:
    """Cap one call's own timeout so it ends by the deadline plus the grace."""
    left = ctx.deadline + _DEADLINE_GRACE_SECONDS - time.monotonic()
    return max(1.0, min(float(default), left))


def _codex_timeout(ctx: _Ctx) -> dict[str, float]:
    raw = str(ctx.env.get("AGENT_MONITOR_CODEX_TIMEOUT") or "600").strip()
    try:
        configured = float(raw)
    except ValueError:
        return {}  # codex_exec reports the invalid setting itself
    if not math.isfinite(configured) or configured <= 0:
        return {}
    return {"timeout": _deadline_timeout(ctx, configured)}


def _not_processed(exc: BaseException) -> bool:
    """Whether a provider failure certainly never reached processing (a
    sponsored Kimi call the transport retries could not get through)."""
    return bool(getattr(getattr(exc, "failure", None), "retryable", False))


def _refused(ctx: _Ctx, turn: _Turn, exc: BaseException) -> bool:
    """Whether ``turn`` failed with ``exc`` and every call it made was
    refused before any processing, so none of them was billed."""
    return _not_processed(exc) and not ctx.turn_processed(turn)


def _stop_route(ctx: _Ctx, stage: str, error: ProviderCallError) -> None:
    """Stop ``stage``'s route after a failure every later call would repeat.

    A linked Claude Code or Codex account that must sign in again, has
    reached its usage limit or cannot use the model fails each further call
    the same way.  Claude Code leaves a sign-in failure to its typed error
    (see claude_backend.forward_as_codex_event), which a stage that degrades
    past it swallows, so that one is shown here, once.
    """
    cause = error.__cause__
    kind = getattr(cause, "kind", None)
    route = ctx.route_for(stage)
    if kind not in _ACCOUNT_STOP_KINDS or route in ctx.stopped_routes:
        return
    ctx.stopped_routes[route] = " ".join(str(cause).split())[:400]
    if kind == "auth" and route.transport == "claude_code":
        emit_item({"type": "error", "message": " ".join(str(error).split())[:1200]})


def _stopped(ctx: _Ctx, stage: str) -> str | None:
    """Why the route answering ``stage`` (a call stage such as ``repair-2``)
    takes no further call this run, if it was stopped."""
    return ctx.stopped_routes.get(ctx.route_for(stage))


def _busy_wait_fits(ctx: _Ctx, seconds: float) -> bool:
    return ctx.deadline - time.monotonic() - seconds >= _BUSY_WAIT_MARGIN_SECONDS


def _wait_for_provider(ctx: _Ctx, seconds: float) -> bool:
    """Pause ``seconds`` for a busy provider; ``False`` if the run was stopped."""
    return not ctx.cancel.wait(max(0.0, seconds))


def _refusal_reason(failure: object) -> str:
    """Who refused a call before processing (this site or Kimi's provider)."""
    return kimi_k3.refusal_reason(
        failure if isinstance(failure, kimi_k3.CallFailure) else None
    )


def _busy_note(
    stage: str | None, failure: object, seconds: float, *, unbilled: bool
) -> str:
    """A progress note for a pause after a refusal before processing: who
    refused, the pause, and "no tokens used" only when ``unbilled`` (no call
    of the run may have been processed so far)."""
    reason = _refusal_reason(failure)
    where = f"{stage} · " if stage else ""
    return (
        f"2 Explore · {where}{reason[:1].upper()}{reason[1:]}; "
        f"retrying in {int(seconds)} s" + (" (no tokens used)" if unbilled else "")
    )


def _next_strategist_wait(ctx: _Ctx, stage: str, exc: BaseException) -> float | None:
    """The pause before sending a refused sponsored strategist call again.

    ``None`` unless the call is the strategist's, on a sponsored Kimi run, and
    was refused before any processing; each pause of
    ``_STRATEGIST_BUSY_WAITS`` is used once per run and only while it leaves
    the run ``_BUSY_WAIT_MARGIN_SECONDS``.
    """
    if (
        not ctx.sponsored_kimi
        or cgm_schema.call_family(stage) != "strategist"
        or not _not_processed(exc)
    ):
        return None
    with ctx.lock:
        if not ctx.busy_waits or not _busy_wait_fits(ctx, ctx.busy_waits[0]):
            return None
        return ctx.busy_waits.pop(0)


def _api_call(
    ctx: _Ctx,
    turn: _Turn,
    route: Route,
    *,
    stage: str,
    system: str,
    user: str,
    effort: str | None,
    env: Mapping[str, str],
    out: Callable[[dict[str, Any]], None],
) -> Any:
    """One ``api_chat`` call.  A sponsored Kimi strategist call the provider
    refused unprocessed is sent again after a deterministic pause (see
    ``_next_strategist_wait``); it stays one call record, since nothing was
    processed or billed."""
    extra: dict[str, Any] = (
        {"retry_allowance": ctx.kimi_retries} if ctx.kimi_retries is not None else {}
    )
    while True:
        try:
            return api_chat(
                system,
                user,
                model=route.model,
                timeout=int(_deadline_timeout(ctx, ctx.api_timeout)),
                effort=effort,
                env=env,
                **extra,
            )
        except APIBackendError as exc:
            pause = _next_strategist_wait(ctx, stage, exc)
            if pause is None:
                raise
            note = _busy_note(stage, exc.failure, pause, unbilled=not ctx.run_processed())
            out(
                {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": f"Math Harness · {note}"},
                }
            )
            if not _wait_for_provider(ctx, pause) or not ctx.turn_live(turn):
                raise _TurnAbandoned(stage) from None


def _provider_call(
    ctx: _Ctx,
    turn: _Turn,
    *,
    stage: str,
    system: str,
    user: str,
    effort: str | None,
) -> Reply:
    """Exactly one billed provider call; records it before any validation.

    Every per-call setting (model, reasoning effort, timeout, environment) is
    passed explicitly; nothing is read from or written to the process
    environment here, so turns can run concurrently.  A sponsored Kimi
    strategist call the provider refused before processing it may be sent
    again under the same record (see ``_api_call``); a failed call's billed
    usage (an empty completion, say) is still recorded.
    """
    route = ctx.route_for(stage)
    call_env = ctx.env_for(route)
    kimi = _is_kimi_route(route)
    claude_timeout = (
        _claude_stage_timeout(stage, ctx.env)
        if route.transport == "claude_code"
        else None
    )
    record: dict[str, Any] = {
        "stage": stage,
        "provider": route.provider,
        "model": route.model,
        "usage": {},
        "response_sha256": None,
        "effort": effort if kimi else None,
    }
    ctx.begin_call(turn, record)

    def out(obj: dict[str, Any]) -> None:
        ctx.turn_out(turn, obj)

    def backend_event(event: dict[str, Any]) -> None:
        _emit_backend_event(event, out=out)

    out({"type": "item.completed", "item": {"type": "reasoning", "text": f"{ctx.label} · {stage}"}})
    observed_model = None
    try:
        if route.transport == "codex_exec":
            result = codex_exec(
                system,
                user,
                model=route.model,
                emit_event=backend_event,
                sandbox="read-only",
                enable_tools=False,
                source_env=call_env,
                **_codex_timeout(ctx),
            )
            provider = route.provider
            model = route.model
        elif route.transport == "claude_code":
            assert claude_timeout is not None
            result = claude_code_exec(
                system,
                user,
                model=route.model,
                emit_event=lambda event: forward_as_codex_event(event, backend_event),
                enable_tools=False,
                max_turns=1,
                timeout=_deadline_timeout(ctx, claude_timeout),
                source_env=call_env,
            )
            provider = route.provider
            model = str(getattr(result, "model", None) or route.model)
            # Session initialization is a requested/configured model. Only
            # the final assistant response supplies authorship evidence.
            for event in reversed(getattr(result, "events", ())):
                message = event.get("message") if event.get("type") == "assistant" else None
                if isinstance(message, dict):
                    actual = message.get("model")
                    observed_model = actual.strip() if isinstance(actual, str) and actual.strip() else None
                    break
        else:
            result = _api_call(
                ctx,
                turn,
                route,
                stage=stage,
                system=system,
                user=user,
                effort=effort if kimi else None,
                env=call_env,
                out=out,
            )
            provider = str(result.provider or "api")
            model = str(result.model or route.model)
            observed_model = getattr(result, "observed_model", None)
            out(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": str(result.text)[:6000]},
                }
            )
    except _TurnAbandoned:
        raise
    except (APIBackendError, ClaudeBackendError, CodexBackendError) as exc:
        if not _not_processed(exc):
            # Only a refusal before processing is certainly free.
            ctx.note_processed(turn)
        # A failed call can still have been billed (an empty completion):
        # book what the provider reported before failing.
        spent = _clean_usage(getattr(exc, "usage", None))
        if spent and ctx.bill_failed_call(turn, record, spent):
            _emit_stage_usage(
                stage,
                spent,
                out=out,
                input_includes_cache=route.transport != "claude_code",
            )
        failure = getattr(exc, "failure", None)
        billed = ctx.run_processed()
        message = str(exc)
        if billed and isinstance(failure, kimi_k3.CallFailure):
            # This text can end up in the run's own error: once any call of
            # the run may have been billed, it never says no tokens were used.
            message = kimi_k3.failure_message(failure, billed=True) or message
        detail = _SPACE_RE.sub(" ", message).strip()[-1200:]
        raise ProviderCallError(
            f"{stage} provider call failed: {detail}", failure=failure, billed=billed
        ) from exc
    except Exception as exc:
        ctx.note_processed(turn)
        # Mocked providers and future backend versions must also fail closed;
        # avoid reflecting an arbitrary exception (which could contain a key).
        raise ProviderCallError(f"{stage} provider call failed") from exc

    ctx.note_processed(turn)
    usage = _clean_usage(getattr(result, "usage", {}))
    text = str(result.text or "").strip()
    reply = Reply(
        text=text,
        usage=usage,
        provider=provider,
        model=model,
        observed_model=observed_model,
    )
    if not ctx.finish_call(
        turn,
        record,
        reply,
        provider=provider,
        model=model,
        usage=usage,
        response_sha256=_sha256(text.encode("utf-8")),
    ):
        raise _TurnAbandoned(stage)
    _emit_stage_usage(
        stage,
        usage,
        out=out,
        input_includes_cache=route.transport != "claude_code",
    )
    return reply


def _invoke(
    ctx: _Ctx,
    turn: _Turn,
    *,
    stage: str,
    system: str,
    user: str,
    effort: str | None = None,
) -> Reply:
    """One logical model call, with a single low-effort retry on empty Kimi output.

    The retry is recorded as ``<stage>-low`` (for a format retry,
    ``<stage>-retry-low``; see ``cgm_schema.CALL_STAGE_RE``).  An empty reply
    that survives the retry raises :class:`EmptyReplyError`.  Retries only
    use calls reserved for this turn.
    """
    if _utf8_size(system) > MAX_SYSTEM_PROMPT_BYTES:
        raise MathHarnessError(f"{stage} system prompt exceeds its bound")
    if _utf8_size(user) > MAX_MODEL_PROMPT_BYTES:
        raise PromptBoundError(f"{stage} model prompt exceeds its bound")
    if not ctx.turn_can_call(turn):
        raise _BudgetExhausted(f"no budget for {stage}")
    kimi = _is_kimi_route(ctx.route)
    empty_detail = f"{stage} returned an empty response"
    try:
        reply = _provider_call(ctx, turn, stage=stage, system=system, user=user, effort=effort)
        if reply.text:
            return reply
    except ProviderCallError as exc:
        if not _is_empty_reply_error(exc):
            raise
        empty_detail = str(exc)
    # Kimi's reasoning tokens count against max_tokens and can starve the
    # visible answer; retry exactly once at low effort when budget allows.
    if kimi and effort != "low" and ctx.turn_can_call(turn):
        retry_stage = f"{stage}-low"
        try:
            reply = _provider_call(
                ctx, turn, stage=retry_stage, system=system, user=user, effort="low"
            )
            if reply.text:
                return reply
            empty_detail = f"{retry_stage} returned an empty response"
        except ProviderCallError as exc:
            if not _is_empty_reply_error(exc):
                raise
            empty_detail = str(exc)
    raise EmptyReplyError(empty_detail)


def _is_empty_reply_error(exc: BaseException) -> bool:
    return "empty response" in str(exc)


def _parse_reply(reply: Reply, *, limit: int, parse: Callable[[str], Any]) -> Any:
    if _utf8_size(reply.text) > limit:
        raise ParseError(f"reply exceeds its {limit}-byte limit")
    if cgm_parsing.has_pseudo_tool_markup(reply.text):
        raise ParseError("reply contains prohibited pseudo-tool markup")
    return parse(reply.text)


def _call_and_parse(
    ctx: _Ctx,
    turn: _Turn,
    *,
    stage: str,
    system: str,
    user: str,
    limit: int,
    parse: Callable[[str], Any],
    family: str,
    resend_reply: bool = False,
) -> tuple[Any, Reply]:
    """Call, parse, and on a malformed reply send one format reminder."""
    effort = _stage_effort(family, ctx.env)
    reply = _invoke(ctx, turn, stage=stage, system=system, user=user, effort=effort)
    try:
        return _parse_reply(reply, limit=limit, parse=parse), reply
    except ParseError as exc:
        first_error = str(exc)
    if not ctx.turn_can_call(turn):
        raise ParseError(first_error)
    reminder = cgm_prompts.FORMAT_REMINDER.format(reason=first_error[:300])
    retry_user = f"{user}\n\n<FORMAT_REMINDER>\n{reminder}\n</FORMAT_REMINDER>"
    if _utf8_size(retry_user) > MAX_MODEL_PROMPT_BYTES:
        # The reminder would push the prompt past its bound; no retry.
        raise ParseError(first_error)
    if resend_reply and reply.text:
        excerpt = _clip_utf8(reply.text, 40_000)
        candidate_user = (
            f"{retry_user}\n\n<PREVIOUS_REPLY>\n{excerpt}\n</PREVIOUS_REPLY>"
        )
        if _utf8_size(candidate_user) <= MAX_MODEL_PROMPT_BYTES:
            retry_user = candidate_user
    retry = _invoke(
        ctx, turn, stage=f"{stage}-retry", system=system, user=retry_user, effort=effort
    )
    return _parse_reply(retry, limit=limit, parse=parse), retry


# ---------------------------------------------------------------------------
# turn scheduling
# ---------------------------------------------------------------------------


@dataclass
class _TurnJob:
    turn: _Turn
    work: Callable[[], Any]
    outcome: Any = None
    error: BaseException | None = None
    abandoned: bool = False


def _turn_limits(count: int, spare: int) -> list[int]:
    """Per-turn call reservations: one call each, then the spare calls handed
    out round-robin (at most ``_MAX_CALLS_PER_TURN`` per turn).  A pure
    function of settled state, so retries never race for budget."""
    limits = [1] * count
    extra = max(0, spare)
    while extra and any(limit < _MAX_CALLS_PER_TURN for limit in limits):
        for position in range(count):
            if extra and limits[position] < _MAX_CALLS_PER_TURN:
                limits[position] += 1
                extra -= 1
    return limits


def _run_turns(ctx: _Ctx, jobs: Sequence[_TurnJob]) -> None:
    """Run already-reserved turns concurrently and wait for every one.

    A wave never holds more than ``ctx.concurrency`` turns.  Each turn runs on
    a daemon thread in a copy of the run's context.  A stop request abandons
    every unfinished turn and raises :class:`RunCancelled`; once the
    wall-clock budget plus a short grace has elapsed, unfinished turns are
    abandoned and the run continues to publication with what has settled.
    Results are read only from finished, non-abandoned turns, in job order.
    """
    if not jobs:
        return
    assert len(jobs) <= max(1, ctx.concurrency), "a wave exceeds the concurrency bound"
    finished: queue.SimpleQueue[int] = queue.SimpleQueue()

    def target(position: int, job: _TurnJob) -> None:
        try:
            job.outcome = job.work()
        except BaseException as exc:  # delivered to the coordinator
            job.error = exc
        finally:
            finished.put(position)

    for position, job in enumerate(jobs):
        context = contextvars.copy_context()
        threading.Thread(
            target=context.run,
            args=(target, position, job),
            name=f"math-harness-{job.turn.stage}",
            daemon=True,
        ).start()
    pending = set(range(len(jobs)))
    try:
        while True:
            # Checked once more after the last turn settles: a turn stopped by
            # the request must surface as a cancellation, not as a failure.
            if ctx.cancel.is_set():
                raise RunCancelled("the Math Harness run was stopped")
            if not pending:
                break
            left = ctx.deadline + _DEADLINE_GRACE_SECONDS - time.monotonic()
            if left <= 0:
                break
            try:
                pending.discard(finished.get(timeout=min(_WAIT_POLL_SECONDS, left)))
            except queue.Empty:
                continue
    finally:
        for position in sorted(pending):
            jobs[position].abandoned = True
            ctx.close_turn(jobs[position].turn, abandon=True)
        for job in jobs:
            ctx.close_turn(job.turn)
    if pending:
        ctx.degrade("deadline-abandoned-calls")


def _run_single(
    ctx: _Ctx,
    stage: str,
    *,
    reserve: int,
    work: Callable[[_Turn], Any],
) -> _TurnJob:
    """Run one turn, keeping ``reserve`` calls for later stages.

    The turn may make up to ``_MAX_CALLS_PER_TURN`` calls but only while
    ``reserve`` calls remain for later stages; its first call only needs one.
    """
    limit = min(_MAX_CALLS_PER_TURN, max(1, ctx.remaining() - reserve))
    turn = ctx.open_turn(stage, limit)
    job = _TurnJob(turn, lambda: work(turn))
    _run_turns(ctx, [job])
    return job


def _job_result(job: _TurnJob) -> Any:
    if job.abandoned or isinstance(job.error, _TurnAbandoned):
        raise _BudgetExhausted(f"{job.turn.stage} was abandoned at the wall-clock deadline")
    if job.error is not None:
        raise job.error
    return job.outcome


def _job_reply(job: _TurnJob) -> Reply | None:
    """The turn's last reply (never from an abandoned turn)."""
    return None if job.abandoned else job.turn.last_reply()


def _emit_stage(ctx: _Ctx, stage_id: str, detail: str = "") -> None:
    if stage_id not in ctx.stages:
        ctx.stages.append(stage_id)
    label = cgm_schema.STAGE_LABELS[stage_id]
    _out({"type": "pipeline.stage", "stage": stage_id, "label": label, "detail": detail})
    text = f"Math Harness · {label}" + (f" · {detail}" if detail else "")
    emit_item({"type": "reasoning", "text": text})


def _note(text: str) -> None:
    emit_item({"type": "reasoning", "text": f"Math Harness · {text}"[:2000]})


# ---------------------------------------------------------------------------
# prompt builders (user messages; all model inputs are delimited data)
# ---------------------------------------------------------------------------


def _task_context(problem: str, task: str, limit: int) -> str:
    if task.strip() == problem.strip():
        return ""
    return (
        "\n\n<UNTRUSTED_TASK_CONTEXT>\n"
        f"{_clip_utf8(task, limit)}\n"
        "</UNTRUSTED_TASK_CONTEXT>"
    )


def _strategist_prompt(problem: str, task: str, count: int) -> str:
    return (
        f"Propose between 1 and {count} materially different decomposition plans "
        f"for this problem (N = {count}).\n\n"
        "<UNTRUSTED_PROBLEM>\n"
        f"{_clip_utf8(problem, 50_000)}\n"
        "</UNTRUSTED_PROBLEM>"
        f"{_task_context(problem, task, 40_000)}"
    )


def _plan_block(plan: cgm_parsing.Plan) -> str:
    subgoals = "\n".join(
        f"- {goal.id}: {goal.statement}"
        + (f" (depends on {', '.join(goal.depends_on)})" if goal.depends_on else "")
        for goal in plan.subgoals
    ) or "- (none given; choose your own lemmas)"
    return (
        "<ASSIGNED_PLAN>\n"
        f"Label: {plan.label}\n"
        f"Technique: {plan.technique}\n"
        f"Approach: {plan.approach}\n"
        f"Subgoals:\n{subgoals}\n"
        f"Known risks: {plan.risks or 'none stated'}\n"
        "</ASSIGNED_PLAN>"
    )


def _strategy_notes(strategy: cgm_parsing.Strategy) -> str:
    lines = [
        f"- Conclusion: {item.statement}"
        + (f" (fragile: {item.fragility_reason or 'yes'})" if item.fragile else "")
        for item in strategy.conclusions
    ]
    lines += [f"- Obstacle: {item}" for item in strategy.obstacles]
    if not lines:
        return ""
    return (
        "\n\n<STRATEGY_NOTES>\n"
        "Unverified awareness from the strategist; never a correctness source.\n"
        + "\n".join(lines)
        + "\n</STRATEGY_NOTES>"
    )


def _worker_prompt(
    problem: str,
    task: str,
    *,
    index: int,
    count: int,
    plan: cgm_parsing.Plan,
    strategy: cgm_parsing.Strategy,
) -> str:
    return (
        f"Carry out assigned plan {index} of {count} independently. You receive "
        "only your assigned plan, not any other worker's work.\n\n"
        "<UNTRUSTED_PROBLEM>\n"
        f"{_clip_utf8(problem, 48_000)}\n"
        "</UNTRUSTED_PROBLEM>"
        f"{_task_context(problem, task, 24_000)}"
        f"{_strategy_notes(strategy)}\n\n"
        f"{_plan_block(plan)}"
    )


def _repair_prompt(
    problem: str,
    task: str,
    *,
    plan: cgm_parsing.Plan,
    candidate: Candidate,
    accepted: Sequence[str],
    rejected: str,
    report: VerifierReport,
) -> str:
    report_json = json.dumps(report.as_json(), ensure_ascii=False, indent=1)
    accepted_line = ", ".join(accepted) if accepted else "none yet"
    return (
        f"The independent verifier rejected block {rejected} of your candidate. "
        "Repair the candidate and return the complete revised candidate in the "
        "exact block format.\n\n"
        "<UNTRUSTED_PROBLEM>\n"
        f"{_clip_utf8(problem, 30_000)}\n"
        "</UNTRUSTED_PROBLEM>"
        f"{_task_context(problem, task, 8_000)}\n\n"
        f"{_plan_block(plan)}\n\n"
        "<CURRENT_CANDIDATE>\n"
        f"{_clip_utf8(cgm_parsing.render_candidate(candidate), 60_000)}"
        "</CURRENT_CANDIDATE>\n\n"
        f"<VERIFIER_ACCEPTED>{accepted_line}</VERIFIER_ACCEPTED>\n\n"
        f"<VERIFIER_REPORT block=\"{rejected}\">\n"
        f"{_clip_utf8(report_json, 16_000)}\n"
        "</VERIFIER_REPORT>"
    )


def _verifier_prompt(
    label: str,
    statement: str,
    proof: str,
    predecessors: Sequence[tuple[str, str]],
) -> str:
    # Nothing is clipped: the verifier must judge exactly the statement and
    # proof that would be published.  Parse-time block limits keep this small;
    # a prompt that still exceeds MAX_MODEL_PROMPT_BYTES is not sent.
    if predecessors:
        listed = "\n\n".join(
            f"[{pred_label}] {pred_statement}"
            for pred_label, pred_statement in predecessors
        )
    else:
        listed = "none"
    return (
        f"Verify the candidate fact labelled {label}.\n\n"
        "<PREDECESSOR_STATEMENTS>\n"
        f"{listed}\n"
        "</PREDECESSOR_STATEMENTS>\n\n"
        "<STATEMENT>\n"
        f"{statement}\n"
        "</STATEMENT>\n\n"
        "<PROOF>\n"
        f"{proof}\n"
        "</PROOF>"
    )


# ---------------------------------------------------------------------------
# pipeline state
# ---------------------------------------------------------------------------


@dataclass
class _Verification:
    seq: int
    stage: str
    attempt: int
    label: str
    report: VerifierReport
    response_sha256: str | None
    key: str


@dataclass
class _Attempt:
    index: int
    worker: int
    kind: str  # "worker" | "repair"
    stage: str
    parent: int | None
    candidate: Candidate | None
    error: str | None
    reply: Reply | None
    accepted: bool = False
    rejected_label: str | None = None
    oversized_label: str | None = None
    outcome: str = "unverified"  # accepted|rejected|error|stopped|unverified
    verdicts: dict[str, _Verification] = field(default_factory=dict)
    # The provider failure (kimi_k3.CallFailure) of a failed sponsored call,
    # and whether every call of the turn was refused before any processing.
    failure: Any = None
    refused: bool = False

    @property
    def verified_labels(self) -> list[str]:
        return [
            label
            for label, record in self.verdicts.items()
            if record.report.verdict == "correct"
        ]


@dataclass
class _PipelineResult:
    problem: str
    task: str
    strategy: cgm_parsing.Strategy
    plans: list[cgm_parsing.Plan]
    attempts: list[_Attempt]
    verifications: list[_Verification]
    accepted_attempt: _Attempt | None
    best_attempt: _Attempt


def _verdict_key(
    label: str,
    statement: str,
    proof: str,
    predecessors: Sequence[tuple[str, str]],
) -> str:
    return _sha256(
        cgm_schema.canonical_json(
            [label, statement, proof, [list(item) for item in predecessors]]
        )
    )


_STATUS_RANK = {"solved": 0, "partial": 1, "stuck": 2}


def _strategize(ctx: _Ctx, problem: str, task: str) -> cgm_parsing.Strategy:
    count = ctx.budgets.max_workers
    try:
        job = _run_single(
            ctx,
            "strategist",
            reserve=3,
            work=lambda turn: _call_and_parse(
                ctx,
                turn,
                stage="strategist",
                system=cgm_prompts.STRATEGIST_SYSTEM,
                user=_strategist_prompt(problem, task, count),
                limit=MAX_STRATEGIST_BYTES,
                parse=lambda text: cgm_parsing.parse_strategist(text, max_plans=count),
                family="strategist",
            ),
        )
        strategy, _reply = _job_result(job)
        return strategy
    except ParseError as exc:
        ctx.degrade("strategist-fallback-plan")
        _note(f"2 Explore · strategist reply unusable ({exc}); using one direct plan")
        return cgm_parsing.fallback_strategy()
    except EmptyReplyError as exc:
        # An empty reply is unusable output, not a transport failure: keep
        # going with the direct plan when a worker, a verifier call and the
        # curator still fit.  Any other provider failure aborts the run,
        # except on a sponsored Kimi run (below).
        if not ctx.can_call(reserve=2):
            raise
        ctx.degrade("strategist-fallback-plan")
        _note(f"2 Explore · strategist returned no text ({exc}); using one direct plan")
        return cgm_parsing.fallback_strategy()
    except ProviderCallError as exc:
        # Sponsored Kimi only: a busy provider (its waits used up) or an
        # ambiguous failure such as a timeout falls back to the direct plan,
        # as an empty reply does.  A failure every later call would repeat
        # (an exhausted call budget, an expired token) still aborts, as does
        # any provider failure on another route.
        failure = getattr(exc, "failure", None)
        if (
            not ctx.sponsored_kimi
            or getattr(failure, "permanent", False)
            or not ctx.can_call(reserve=2)
        ):
            raise
        ctx.degrade("strategist-provider-failure")
        ctx.degrade("strategist-fallback-plan")
        if _not_processed(exc):
            why = _refusal_reason(failure)
            # Only when no call was processed, not merely the last one (an
            # earlier, unusable strategist reply may have been billed).
            if not ctx.run_processed():
                why += "; no tokens used"
            reason = f"the strategist call was refused ({why})"
        else:
            reason = "the strategist call failed"
        _note(f"2 Explore · {reason}; using one direct plan")
        return cgm_parsing.fallback_strategy()
    except _BudgetExhausted as exc:
        raise MathHarnessError("no budget for the strategist stage") from exc


def _run_workers(
    ctx: _Ctx,
    problem: str,
    task: str,
    strategy: cgm_parsing.Strategy,
) -> tuple[list[cgm_parsing.Plan], list[_Attempt]]:
    """Run one independent worker per plan, up to ``concurrency`` at a time.

    Each wave reserves one call per worker plus a deterministic share of the
    spare calls for its retries, always keeping one verifier and one curator
    call; results are taken in worker order whatever order they complete in.
    """
    plans = list(strategy.plans[: ctx.budgets.max_workers])
    # Keep one verifier and one curator call in reserve.
    room = ctx.remaining() - 2
    plans = plans[: max(1, min(len(plans), room))]
    attempts: list[_Attempt] = []
    position = 0
    while position < len(plans):
        room = ctx.remaining() - 2
        if position and (room < 1 or ctx.deadline_passed()):
            ctx.degrade("workers-truncated-by-budget")
            break
        width = min(
            ctx.concurrency,
            len(plans) - position,
            max(1, room) if position == 0 else room,
        )
        indices = list(range(position + 1, position + width + 1))
        limits = _turn_limits(width, room - width)
        jobs: list[tuple[int, _TurnJob]] = []
        for index, limit in zip(indices, limits):
            plan = plans[index - 1]
            stage = f"worker-{index}"
            try:
                turn = ctx.open_turn(stage, limit)
            except _BudgetExhausted:
                break
            _note(f"2 Explore · worker {index}/{len(plans)} · {plan.technique} · {plan.label}")
            jobs.append(
                (
                    index,
                    _worker_job(
                        ctx, turn, problem, task, strategy, plans, plan_index=index
                    ),
                )
            )
        _run_turns(ctx, [job for _index, job in jobs])
        truncated = len(jobs) < len(indices)
        for index, job in jobs:
            attempt = _settle_worker(
                ctx, job, index=len(attempts) + 1, stage_index=index, plan_index=index
            )
            if attempt is None:
                truncated = True
                continue
            attempts.append(attempt)
        if truncated:
            ctx.degrade("workers-truncated-by-budget")
            break
        position += width
    if not any(attempt.candidate is not None for attempt in attempts):
        rescued = _rescue_worker(ctx, problem, task, strategy, plans, attempts)
        if rescued is not None:
            attempts.append(rescued)
    if not any(attempt.candidate is not None for attempt in attempts):
        failures = [attempt.error for attempt in attempts if attempt.error]
        detail = f": {failures[-1]}" if failures else ""
        # When every worker failed at the provider, a provider failure
        # explains the run's failure (kimi_runner turns it into a message):
        # the last one that may have been processed, else the last refusal.
        # The run counts as billed if any of its calls may have been.
        explained = None
        if attempts and all(attempt.failure is not None for attempt in attempts):
            explained = next(
                (
                    attempt.failure
                    for attempt in reversed(attempts)
                    if not getattr(attempt.failure, "retryable", False)
                ),
                attempts[-1].failure,
            )
        raise MathHarnessError(
            f"no worker produced a usable candidate{detail}",
            failure=explained,
            billed=ctx.run_processed(),
        )
    return plans, attempts


def _worker_job(
    ctx: _Ctx,
    turn: _Turn,
    problem: str,
    task: str,
    strategy: cgm_parsing.Strategy,
    plans: Sequence[cgm_parsing.Plan],
    *,
    plan_index: int,
) -> _TurnJob:
    """The job of one worker turn carrying out plan ``plan_index`` (1-based)."""
    plan = plans[plan_index - 1]
    user = _worker_prompt(
        problem,
        task,
        index=plan_index,
        count=len(plans),
        plan=plan,
        strategy=strategy,
    )
    return _TurnJob(
        turn,
        _bind_turn(
            _call_and_parse,
            ctx,
            turn,
            stage=turn.stage,
            system=cgm_prompts.worker_system(plan.technique),
            user=user,
            limit=MAX_WORKER_BYTES,
            parse=lambda text: cgm_parsing.parse_worker(
                text, max_lemmas=ctx.budgets.max_lemmas
            ),
            family="worker",
            resend_reply=True,
        ),
    )


def _settle_worker(
    ctx: _Ctx, job: _TurnJob, *, index: int, stage_index: int, plan_index: int
) -> _Attempt | None:
    """The attempt a finished worker turn produced; ``None`` when the turn
    found no budget left (the caller truncates the worker stage)."""
    attempt = _Attempt(
        index=index,
        worker=plan_index,
        kind="worker",
        stage=f"worker-{stage_index}",
        parent=None,
        candidate=None,
        error=None,
        reply=None,
    )
    error = job.error
    if job.abandoned or isinstance(error, _TurnAbandoned):
        attempt.error = "The worker call was abandoned at the wall-clock deadline."
        ctx.degrade("worker-abandoned-at-deadline")
        _note(f"2 Explore · worker {stage_index} · abandoned at the deadline")
    elif error is None:
        candidate, reply = job.outcome
        attempt.candidate = candidate
        attempt.reply = reply
        _note(
            f"2 Explore · worker {stage_index} · status {candidate.status} · "
            f"{len(candidate.lemmas)} lemma(s)"
            + (" + theorem" if candidate.theorem else "")
        )
    elif isinstance(error, ParseError):
        attempt.error = f"The worker reply could not be parsed: {error}"
        ctx.degrade("worker-unparsable")
        _note(f"2 Explore · worker {stage_index} · reply unusable ({error})")
    elif isinstance(error, ProviderCallError):
        attempt.error = str(error)
        attempt.failure = error.failure
        attempt.refused = _refused(ctx, job.turn, error)
        ctx.degrade("worker-provider-failure")
        _note(f"2 Explore · worker {stage_index} · provider call failed")
    elif isinstance(error, PromptBoundError):
        attempt.error = str(error)
        ctx.degrade("worker-prompt-too-large")
        _note(f"2 Explore · worker {stage_index} · prompt exceeds its bound")
    elif isinstance(error, _BudgetExhausted):
        return None
    else:
        raise error
    if attempt.reply is None:
        attempt.reply = _job_reply(job)
    return attempt


def _rescue_worker(
    ctx: _Ctx,
    problem: str,
    task: str,
    strategy: cgm_parsing.Strategy,
    plans: Sequence[cgm_parsing.Plan],
    attempts: Sequence[_Attempt],
) -> _Attempt | None:
    """One more worker turn after the provider refused every worker unprocessed.

    Sponsored Kimi only.  When every call of every worker turn was refused
    before any processing (none of them was billed: a turn whose first reply
    was billed and whose format retry was refused does not count), pause
    ``_RESCUE_WORKER_WAIT`` seconds and carry out plan 1 again as a new
    worker turn (the next worker index), counted in the call budget with the
    verifier and curator calls kept in reserve, and only while the pause
    leaves the run ``_BUSY_WAIT_MARGIN_SECONDS``.  ``None`` when no rescue
    runs or its turn finds no budget.
    """
    if (
        not ctx.sponsored_kimi
        or not plans
        or not attempts
        or not all(attempt.refused for attempt in attempts)
        or ctx.remaining() - 2 < 1
        or not _busy_wait_fits(ctx, _RESCUE_WORKER_WAIT)
    ):
        return None
    ctx.degrade("worker-busy-rescue")
    # "No tokens used" speaks for the run: the strategist was usually billed.
    _note(
        _busy_note(
            None, attempts[-1].failure, _RESCUE_WORKER_WAIT, unbilled=not ctx.run_processed()
        )
    )
    if not _wait_for_provider(ctx, _RESCUE_WORKER_WAIT):
        raise RunCancelled("the Math Harness run was stopped")
    with ctx.lock:
        opened = [cgm_schema.call_turn(turn.stage) for turn in ctx.turns]
    # The next worker index, so the canonical call order still holds.
    stage_index = 1 + max(
        (turn[1] for turn in opened if turn is not None and turn[0] == "worker"),
        default=0,
    )
    room = ctx.remaining() - 2
    try:
        turn = ctx.open_turn(f"worker-{stage_index}", _turn_limits(1, room - 1)[0])
    except _BudgetExhausted:
        return None
    plan = plans[0]
    _note(
        f"2 Explore · worker {stage_index} · plan 1 again · {plan.technique} · {plan.label}"
    )
    job = _worker_job(ctx, turn, problem, task, strategy, plans, plan_index=1)
    _run_turns(ctx, [job])
    return _settle_worker(
        ctx, job, index=len(attempts) + 1, stage_index=stage_index, plan_index=1
    )


def _bind_turn(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[[], Any]:
    return lambda: function(*args, **kwargs)


@dataclass
class _WaveEntry:
    position: int  # in the written order of the THEOREM closure
    label: str
    key: str
    user: str  # the exact verifier prompt
    seq: int = 0
    job: _TurnJob | None = None

    @property
    def stage(self) -> str:
        return f"verify-{self.seq}"


def _run_verification_wave(
    ctx: _Ctx,
    attempt: _Attempt,
    wave: Sequence[_WaveEntry],
    cache: dict[str, _Verification],
    verifications: list[_Verification],
    settled: dict[int, _Verification | str],
) -> None:
    """Verify one wave of independent blocks concurrently.

    Calls are numbered and reserved in written order before any starts
    (always keeping the curator call), and their results are settled in that
    order: a verdict, or why the block could not be verified (``stopped``:
    budget, deadline or provider failure; ``prompt-bound``: its prompt
    exceeded a bound).
    """
    fit = 0 if ctx.deadline_passed() else ctx.remaining() - 1
    launched = wave[: max(0, min(len(wave), fit))]
    jobs: list[_TurnJob] = []
    for entry, limit in zip(launched, _turn_limits(len(launched), fit - len(launched))):
        try:
            turn = ctx.open_turn(f"verify-{ctx.verify_seq + 1}", limit)
        except _BudgetExhausted:
            break
        ctx.verify_seq += 1
        entry.seq = ctx.verify_seq
        entry.job = _TurnJob(
            turn,
            _bind_turn(
                _call_and_parse,
                ctx,
                turn,
                stage=entry.stage,
                system=cgm_prompts.VERIFIER_SYSTEM,
                user=entry.user,
                limit=MAX_VERIFIER_BYTES,
                parse=cgm_parsing.parse_verifier,
                family="verify",
            ),
        )
        jobs.append(entry.job)
    _run_turns(ctx, jobs)

    for entry in wave:
        job = entry.job
        error = job.error if job is not None else None
        if job is None or job.abandoned or isinstance(error, (_BudgetExhausted, _TurnAbandoned)):
            ctx.degrade("verification-stopped-by-budget")
            settled[entry.position] = "stopped"
            continue
        if isinstance(error, PromptBoundError):
            ctx.degrade("verifier-prompt-too-large")
            settled[entry.position] = "prompt-bound"
            continue
        if isinstance(error, ProviderCallError) and not isinstance(error, EmptyReplyError):
            ctx.degrade("verifier-provider-failure")
            _stop_route(ctx, entry.stage, error)
            settled[entry.position] = "stopped"
            continue
        if error is None:
            report, reply = job.outcome
            response_sha = _sha256(reply.text.encode("utf-8"))
        elif isinstance(error, (ParseError, EmptyReplyError)):
            # Malformed or empty verifier output is an "error" verdict for
            # this candidate -- never an acceptance, and not a reason to stop
            # verifying other candidates.
            report = cgm_parsing.error_report(str(error))
            last = _job_reply(job)
            response_sha = _sha256(last.text.encode("utf-8")) if last else None
            ctx.degrade(
                "verifier-unparsable" if isinstance(error, ParseError) else "verifier-empty"
            )
        else:
            raise error
        record = _Verification(
            seq=entry.seq,
            stage=entry.stage,
            attempt=attempt.index,
            label=entry.label,
            report=report,
            response_sha256=response_sha,
            key=entry.key,
        )
        verifications.append(record)
        if report.verdict in {"correct", "wrong"}:
            cache[entry.key] = record
        _note(
            f"3 Verify · worker {attempt.worker} attempt {attempt.index} · "
            f"{entry.label} · {report.verdict}"
        )
        settled[entry.position] = record


def _verify_attempt(
    ctx: _Ctx,
    attempt: _Attempt,
    problem: str,
    cache: dict[str, _Verification],
    verifications: list[_Verification],
) -> str:
    """Verify an attempt's THEOREM closure in its written topological order.

    The order is ``closure_labels`` (the lemmas as written, topologically)
    and then the THEOREM.  Each wave takes, in that order, the blocks whose
    predecessors are all accepted and that come before every block already
    known not to be accepted, up to ``concurrency`` of them needing a
    verifier call; cached verdicts and blocks too large to verify settle
    without a call as they are reached.  The first block in the written order
    that is not accepted decides the outcome.  So one call at a time this is
    exactly the sequential topological verification, and at any concurrency
    the same block decides given the same verdicts (budget permitting);
    concurrency only moves up, or adds, verifier calls for independent
    blocks written after it.

    Returns ``accepted``, ``rejected`` (a definitive ``wrong`` verdict),
    ``error`` (malformed or empty verifier output, or a block whose exact,
    unclipped verifier prompt would exceed ``MAX_MODEL_PROMPT_BYTES``) or
    ``stopped`` (budget, deadline or provider failure).
    """
    candidate = attempt.candidate
    assert candidate is not None and candidate.theorem is not None
    order = [*cgm_parsing.closure_labels(candidate), cgm_parsing.THEOREM_LABEL]
    blocks = {
        label: candidate.theorem
        if label == cgm_parsing.THEOREM_LABEL
        else candidate.lemma(label)
        for label in order
    }
    statements = {block.label: block.statement for block in candidate.lemmas}
    # Position -> its verdict, or why the block could not be verified.
    settled: dict[int, _Verification | str] = {}

    def accepted(label: str) -> bool:
        record = settled.get(order.index(label))
        return isinstance(record, _Verification) and record.report.verdict == "correct"

    def frontier() -> int:
        """The position of the first block known not to be accepted."""
        return min(
            (
                position
                for position, record in settled.items()
                if isinstance(record, str) or record.report.verdict != "correct"
            ),
            default=len(order),
        )

    while True:
        wave: list[_WaveEntry] = []
        for position, label in enumerate(order):
            if len(wave) >= ctx.concurrency or position >= frontier():
                break
            block = blocks[label]
            assert block is not None
            if position in settled or not all(accepted(used) for used in block.uses):
                continue
            statement = problem if label == cgm_parsing.THEOREM_LABEL else block.statement
            predecessors = [(used, statements[used]) for used in block.uses]
            key = _verdict_key(label, statement, block.proof, predecessors)
            cached = cache.get(key)
            if cached is not None:
                settled[position] = cached
                continue
            user = _verifier_prompt(label, statement, block.proof, predecessors)
            if _utf8_size(user) > MAX_MODEL_PROMPT_BYTES:
                # Never clip what the verifier judges; this candidate simply
                # cannot be verified (no call is made).
                settled[position] = "oversized"
                continue
            wave.append(_WaveEntry(position=position, label=label, key=key, user=user))
        if not wave:
            break
        if _stopped(ctx, f"verify-{ctx.verify_seq + 1}"):
            # The verifier's account failed in an earlier wave; these blocks
            # stay unverified (never accepted by default).
            for entry in wave:
                settled[entry.position] = "stopped"
            break
        _run_verification_wave(ctx, attempt, wave, cache, verifications, settled)

    # Verdicts in written order, whatever order the waves settled them in.
    attempt.verdicts = {
        order[position]: record
        for position, record in sorted(settled.items())
        if isinstance(record, _Verification)
    }
    decider = frontier()
    if decider == len(order):
        attempt.accepted = True
        attempt.outcome = "accepted"
        return "accepted"
    label, record = order[decider], settled[decider]
    if record == "oversized":
        ctx.degrade("verifier-prompt-too-large")
        attempt.oversized_label = label
        attempt.outcome = "error"
        _note(
            f"3 Verify · worker {attempt.worker} attempt {attempt.index} · "
            f"{label} · too large to verify"
        )
    elif record == "prompt-bound":
        attempt.oversized_label = label
        attempt.outcome = "error"
    elif record == "stopped":
        attempt.outcome = "stopped"
    else:
        assert isinstance(record, _Verification), record
        attempt.rejected_label = label
        attempt.outcome = "rejected" if record.report.verdict == "wrong" else "error"
    return attempt.outcome


def _repair(
    ctx: _Ctx,
    attempt: _Attempt,
    plan: cgm_parsing.Plan,
    problem: str,
    task: str,
    next_index: int,
) -> _Attempt:
    assert attempt.candidate is not None and attempt.rejected_label is not None
    ctx.repair_seq += 1
    stage = f"repair-{ctx.repair_seq}"
    rejected = attempt.verdicts[attempt.rejected_label]
    _note(f"3 Verify · repair {ctx.repair_seq} of worker {attempt.worker} · {attempt.rejected_label}")
    repaired = _Attempt(
        index=next_index,
        worker=attempt.worker,
        kind="repair",
        stage=stage,
        parent=attempt.index,
        candidate=None,
        error=None,
        reply=None,
    )
    user = _repair_prompt(
        problem,
        task,
        plan=plan,
        candidate=attempt.candidate,
        accepted=attempt.verified_labels,
        rejected=attempt.rejected_label,
        report=rejected.report,
    )
    job: _TurnJob | None = None
    try:
        job = _run_single(
            ctx,
            stage,
            reserve=2,
            work=lambda turn: _call_and_parse(
                ctx,
                turn,
                stage=stage,
                system=cgm_prompts.worker_system(plan.technique),
                user=user,
                limit=MAX_WORKER_BYTES,
                parse=lambda text: cgm_parsing.parse_worker(
                    text, max_lemmas=ctx.budgets.max_lemmas
                ),
                family="repair",
                resend_reply=True,
            ),
        )
        candidate, reply = _job_result(job)
        repaired.candidate = candidate
        repaired.reply = reply
    except ParseError as exc:
        repaired.error = f"The repair reply could not be parsed: {exc}"
        ctx.degrade("repair-unparsable")
    except ProviderCallError as exc:
        repaired.error = str(exc)
        ctx.degrade("repair-provider-failure")
        _stop_route(ctx, stage, exc)
    except PromptBoundError as exc:
        repaired.error = str(exc)
        ctx.degrade("repair-prompt-too-large")
    except _BudgetExhausted:
        repaired.error = "No budget remained for the repair."
        ctx.degrade("repair-stopped-by-budget")
    if repaired.reply is None and job is not None:
        repaired.reply = _job_reply(job)
    return repaired


def _verification_order(attempts: Sequence[_Attempt]) -> list[_Attempt]:
    """Documented policy: solved first, then partial, then worker order."""
    eligible = [
        attempt
        for attempt in attempts
        if attempt.candidate is not None and attempt.candidate.theorem is not None
    ]
    return sorted(
        eligible,
        key=lambda attempt: (
            _STATUS_RANK.get(attempt.candidate.status, 3),  # type: ignore[union-attr]
            attempt.worker,
        ),
    )


def _best_attempt(attempts: Sequence[_Attempt]) -> _Attempt:
    """Most-verified parsed attempt (ties: status, then the latest repair)."""
    parsed = [attempt for attempt in attempts if attempt.candidate is not None]
    assert parsed, "at least one worker candidate is required"

    def rank(attempt: _Attempt) -> tuple[int, int, int, int]:
        candidate = attempt.candidate
        assert candidate is not None
        verified = len(
            [
                label
                for label in attempt.verified_labels
                if label != cgm_parsing.THEOREM_LABEL
            ]
        )
        return (
            -verified,
            0 if candidate.theorem is not None else 1,
            _STATUS_RANK.get(candidate.status, 3),
            -attempt.index,
        )

    return sorted(parsed, key=rank)[0]


def _run_cgm(
    ctx: _Ctx,
    problem: str,
    task: str,
) -> _PipelineResult:
    models = (
        ctx.route.model
        if ctx.worker_route is None
        else f"{ctx.route.model} + {ctx.worker_route.model}"
    )
    _emit_stage(ctx, "scaffold", f"{models} · no tools")
    _emit_stage(ctx, "work", "strategist")
    strategy = _strategize(ctx, problem, task)
    plans, attempts = _run_workers(ctx, problem, task, strategy)

    _emit_stage(ctx, "verify")
    cache: dict[str, _Verification] = {}
    verifications: list[_Verification] = []
    accepted_attempt: _Attempt | None = None
    repairs_used = 0
    verified_candidates = 0
    stop_all = False
    for attempt in _verification_order(attempts):
        if (
            verified_candidates >= ctx.budgets.max_verify_candidates
            or _stopped(ctx, f"verify-{ctx.verify_seq + 1}")
        ):
            break
        verified_candidates += 1
        current = attempt
        while True:
            outcome = _verify_attempt(ctx, current, problem, cache, verifications)
            if outcome == "accepted":
                accepted_attempt = current
                break
            if outcome == "stopped":
                stop_all = True
                break
            if outcome != "rejected":
                break
            # A repair needs its own call, at least one verification and the
            # curator reserve, on accounts that still answer.
            if (
                repairs_used >= ctx.budgets.max_repairs
                or not ctx.can_call(reserve=2)
                or _stopped(ctx, f"repair-{ctx.repair_seq + 1}")
                or _stopped(ctx, f"verify-{ctx.verify_seq + 1}")
            ):
                break
            repairs_used += 1
            plan = plans[current.worker - 1]
            repaired = _repair(ctx, current, plan, problem, task, len(attempts) + 1)
            attempts.append(repaired)
            if repaired.candidate is None or repaired.candidate.theorem is None:
                break
            current = repaired
        if accepted_attempt is not None or stop_all:
            break
    best = accepted_attempt or _best_attempt(attempts)
    return _PipelineResult(
        problem=problem,
        task=task,
        strategy=strategy,
        plans=plans,
        attempts=attempts,
        verifications=verifications,
        accepted_attempt=accepted_attempt,
        best_attempt=best,
    )


# ---------------------------------------------------------------------------
# facts, proof rendering and the exploring graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FactRecord:
    label: str
    fact_id: str
    statement: str
    proof: str
    predecessors: tuple[str, ...]
    payload: bytes
    verification_attempt: int
    is_target: bool

    def as_mapping(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "statement": self.statement,
            "proof": self.proof,
            "predecessors": list(self.predecessors),
        }


def _cited_proof(block: cgm_parsing.Block, fact_ids: Mapping[str, str]) -> str:
    proof = cgm_schema.normalize_text(block.proof)
    if not block.uses:
        return proof
    citations = "; ".join(f"{label} = fact {fact_ids[label]}" for label in block.uses)
    return f"Cited facts: {citations}.\n\n{proof}"


def _build_facts(
    result: _PipelineResult, problem_id: str
) -> tuple[list[_FactRecord], str | None]:
    """Content-addressed facts for the verified part of the selected attempt."""
    attempt = result.best_attempt
    candidate = attempt.candidate
    assert candidate is not None
    accepted = result.accepted_attempt is attempt
    verified = set(attempt.verified_labels)
    labels = [
        label for label in cgm_parsing.closure_labels(candidate) if label in verified
    ]
    if accepted:
        labels.append(cgm_parsing.THEOREM_LABEL)
    fact_ids: dict[str, str] = {}
    records: dict[str, _FactRecord] = {}
    ordered: list[_FactRecord] = []
    for label in labels:
        block = (
            candidate.theorem
            if label == cgm_parsing.THEOREM_LABEL
            else candidate.lemma(label)
        )
        assert block is not None
        if any(used not in fact_ids for used in block.uses):
            continue
        statement = cgm_schema.normalize_text(
            result.problem if label == cgm_parsing.THEOREM_LABEL else block.statement
        )
        proof = _cited_proof(block, fact_ids)
        predecessors = tuple(sorted({fact_ids[used] for used in block.uses}))
        fact_id = cgm_schema.compute_fact_id(
            problem_id=problem_id,
            predecessors=predecessors,
            glossary_introduces={},
            statement=statement,
            proof=proof,
        )
        fact_ids[label] = fact_id
        if fact_id in records:
            continue
        payload = cgm_schema.serialize_fact(
            fact_id=fact_id,
            problem_id=problem_id,
            author=AUTHOR,
            predecessors=predecessors,
            statement=statement,
            proof=proof,
        ).encode("utf-8")
        if len(payload) > MAX_FACT_BYTES:
            raise MathHarnessError("a verified fact exceeds its byte limit")
        record = _FactRecord(
            label=label,
            fact_id=fact_id,
            statement=statement,
            proof=proof,
            predecessors=predecessors,
            payload=payload,
            verification_attempt=attempt.verdicts[label].attempt,
            is_target=label == cgm_parsing.THEOREM_LABEL,
        )
        records[fact_id] = record
        ordered.append(record)
    target_id = fact_ids.get(cgm_parsing.THEOREM_LABEL) if accepted else None
    if target_id is not None:
        order = cgm_closure.closure_order([r.as_mapping() for r in ordered], target_id)
        by_id = {record.fact_id: record for record in ordered}
        ordered = [by_id[fact_id] for fact_id in order]
    return ordered, target_id


def _self_check_facts(
    facts: Sequence[_FactRecord], target_id: str | None, problem: str, proof: str
) -> None:
    """Fail closed unless the published bytes will re-validate exactly."""
    from agent_monitor import engine_graph  # local: engine_graph imports cgm only

    for record in facts:
        parsed = engine_graph._parse_fact(record.payload, record.fact_id, record.fact_id)
        if (
            parsed.statement != record.statement
            or parsed.proof != record.proof
            or tuple(sorted(parsed.predecessors)) != tuple(sorted(record.predecessors))
        ):
            raise MathHarnessError("verified fact does not round-trip its content")
    if target_id is not None:
        rendered = cgm_closure.render_verified_closure(
            [record.as_mapping() for record in facts], target_id
        )
        if rendered != proof:
            raise MathHarnessError("proof.md does not equal the verified closure")
        target = next(record for record in facts if record.fact_id == target_id)
        if cgm_schema.normalize_for_hash(target.statement) != cgm_schema.normalize_for_hash(problem):
            raise MathHarnessError("target statement differs from the problem")


def _self_check_receipt(payload: bytes) -> None:
    """Fail closed unless engine_graph accepts the receipt exactly as written."""
    from agent_monitor import engine_graph  # local: engine_graph imports cgm only

    if engine_graph._cgm_receipt(payload) is None:
        raise MathHarnessError("engine receipt does not satisfy its validator")


def _render_unverified(result: _PipelineResult, reason: str) -> str:
    attempt = result.best_attempt
    candidate = attempt.candidate
    assert candidate is not None
    verified = set(attempt.verified_labels)
    lines = [
        "# Proof candidate (not verifier-accepted)",
        "",
        "> This write-up was NOT accepted by the independent model verifier: "
        f"{reason} Blocks marked verifier-accepted were accepted one at a time; "
        "nothing here is a formal or Lean certificate.",
        "",
    ]

    def section(title: str, block: cgm_parsing.Block, statement: str) -> None:
        lines.extend(
            [
                f"## {title}",
                "",
                f"**Uses:** {', '.join(block.uses) if block.uses else 'none'}.",
                "",
                "**Statement.**",
                "",
                statement,
                "",
                "**Proof.**",
                "",
                block.proof,
                "",
            ]
        )

    for block in candidate.lemmas:
        status = "verifier-accepted" if block.label in verified else "not verifier-accepted"
        section(f"Lemma {block.label} ({status})", block, block.statement)
    if candidate.theorem is not None:
        section(
            "Main theorem (not verifier-accepted)",
            candidate.theorem,
            cgm_schema.normalize_text(result.problem),
        )
    else:
        lines += [
            "## Main theorem",
            "",
            "No proof of the main theorem was produced. The worker reported status "
            f"`{candidate.status}`.",
            "",
        ]
    if candidate.findings:
        lines += ["## Findings (unverified)", ""]
        for finding in candidate.findings:
            lines.append(f"- **{finding.kind}**: {finding.claim}")
        lines.append("")
    rendered = "\n".join(lines).rstrip() + "\n"
    if _utf8_size(rendered) > MAX_PROOF_BYTES:
        rendered = _clip_utf8(rendered, MAX_PROOF_BYTES - 64).rstrip() + "\n"
    return rendered


def _unverified_reason(result: _PipelineResult, ctx: _Ctx) -> str:
    attempt = result.best_attempt
    if attempt.rejected_label and attempt.rejected_label in attempt.verdicts:
        record = attempt.verdicts[attempt.rejected_label]
        issues = [*record.report.critical_errors, *record.report.gaps]
        detail = f" ({issues[0].location}: {issues[0].issue})" if issues else ""
        verdict = "could not be interpreted" if record.report.verdict == "error" else "was rejected"
        return f"block {attempt.rejected_label} {verdict}{_clip_utf8(detail, 600)}."
    if attempt.oversized_label:
        return (
            f"block {attempt.oversized_label} was too large to send to the "
            "verifier unclipped."
        )
    if attempt.candidate is not None and attempt.candidate.theorem is None:
        return "the selected worker produced no proof of the main theorem."
    if "verification-stopped-by-budget" in ctx.degraded:
        return "the verification budget ran out before the theorem was checked."
    stopped = _stopped(ctx, f"verify-{max(ctx.verify_seq, 1)}")
    if stopped:
        return f"verification stopped before the theorem was checked ({stopped.rstrip('. ')})."
    if "verifier-provider-failure" in ctx.degraded:
        return "a verifier call failed before the theorem was checked."
    return "the theorem closure was not verified."


class _Memory:
    """Builds one generation of global_memory rows and typed edges."""

    def __init__(self, timestamp: str) -> None:
        self.timestamp = timestamp
        self.rows: dict[str, list[dict[str, Any]]] = {
            name: [] for name in cgm_schema.ENTRY_FILES
        }
        self.edges: list[dict[str, Any]] = []
        self._edge_keys: set[tuple[str, str, str]] = set()

    def entry(
        self,
        kind: str,
        claim: str,
        evidence: str,
        *,
        status: str,
        fact_id: str | None = None,
        subgoal: str = cgm_schema.GOAL_ROOT,
        predecessors: Sequence[str] = (),
    ) -> str:
        name = f"{kind}.jsonl"
        rows = self.rows[name]
        claim = _clip_utf8(claim.strip() or kind, 3_900)
        evidence = _clip_utf8(evidence.strip() or claim, MAX_ATTEMPT_EVIDENCE_BYTES)
        entry_id = cgm_schema.entry_id(kind, claim, AUTHOR, self.timestamp, len(rows) + 1)
        rows.append(
            {
                "id": entry_id,
                "timestamp_utc": self.timestamp,
                "author": AUTHOR,
                "kind": kind,
                "claim": claim,
                "evidence": evidence,
                "verifiable": cgm_schema.GLOBAL_KINDS[kind],
                "status": status,
                "fact_id": fact_id,
                "links": {"subgoal": subgoal, "predecessors": list(predecessors)},
                "glossary": {},
            }
        )
        return entry_id

    def edge(
        self,
        src: str,
        dst: str,
        edge_type: str,
        rationale: str,
        *,
        technique: str | None = None,
        and_group: str | None = None,
    ) -> None:
        key = (src, dst, edge_type)
        if key in self._edge_keys or src == dst:
            return
        self._edge_keys.add(key)
        self.edges.append(
            {
                "id": cgm_schema.edge_id(
                    src, dst, edge_type, AUTHOR, self.timestamp, len(self.edges) + 1
                ),
                "timestamp_utc": self.timestamp,
                "author": AUTHOR,
                "src": src,
                "dst": dst,
                "type": edge_type,
                "technique": technique,
                "and_group": and_group,
                "rationale": rationale,
            }
        )


def _goal_id(plan_index: int, subgoal_id: str) -> str:
    return f"goal:p{plan_index}-{subgoal_id.lower()}"


@dataclass
class _GraphSnapshot:
    memory: _Memory
    plan_ids: list[str]
    attempt_ids: dict[int, str]
    subgoal_ids: dict[int, list[str]]
    closed_goals: dict[str, list[str]]
    aliases: dict[str, str]
    lines: list[str]
    edge_lines: list[str]
    frontier: list[str]


def _build_memory(
    result: _PipelineResult,
    facts: Sequence[_FactRecord],
    target_id: str | None,
) -> _GraphSnapshot:
    memory = _Memory(_now())
    root = cgm_schema.GOAL_ROOT
    plan_ids: list[str] = []
    subgoal_ids: dict[int, list[str]] = {}
    transforms = {
        "reduction": "reduces-to",
        "reformulation": "equivalent-to",
        "generalization": "generalizes",
    }
    for index, plan in enumerate(result.plans, 1):
        subgoal_text = "\n".join(
            f"- {goal.id}: {goal.statement}" for goal in plan.subgoals
        )
        evidence = (
            f"Technique: {plan.technique}\n\nApproach: {plan.approach}"
            + (f"\n\nSubgoals:\n{subgoal_text}" if subgoal_text else "")
            + f"\n\nKnown risks: {plan.risks or 'none stated'}"
        )
        status = (
            "supported"
            if result.accepted_attempt is not None
            and result.accepted_attempt.worker == index
            else "open"
        )
        plan_id = memory.entry(
            "plan", f"Plan {index}: {plan.label}", evidence, status=status
        )
        plan_ids.append(plan_id)
        memory.edge(plan_id, root, "addresses", "Candidate plan for the root problem.")
        tag = plan.technique if plan.technique in cgm_schema.TECHNIQUES else "plain"
        goals: list[str] = []
        for goal in plan.subgoals:
            goal_node = _goal_id(index, goal.id)
            goals.append(goal_node)
            memory.edge(
                plan_id,
                goal_node,
                "decomposes-into",
                f"{goal.id}: {_clip_utf8(goal.statement, 1_000)}",
                technique=tag,
                and_group=f"plan-{index}",
            )
        for goal in plan.subgoals:
            for dependency in goal.depends_on:
                memory.edge(
                    _goal_id(index, goal.id),
                    _goal_id(index, dependency),
                    "depends-on",
                    "Subgoal ordering declared by the strategist.",
                )
        if plan.technique in transforms and goals:
            edge_type = transforms[plan.technique]
            src, dst = (goals[0], root) if edge_type == "generalizes" else (root, goals[0])
            memory.edge(
                src,
                dst,
                edge_type,
                f"Goal transformation proposed by plan {index} ({plan.technique}); "
                "it closes the root only through a verified bridge fact.",
            )
        subgoal_ids[index] = goals
    for earlier in range(len(plan_ids)):
        for later in range(earlier + 1, len(plan_ids)):
            memory.edge(
                plan_ids[earlier],
                plan_ids[later],
                "alternative-of",
                "Sibling plans for the same goal (OR).",
            )
    for conclusion in result.strategy.conclusions:
        evidence = conclusion.justification or "Stated by the strategist; unverified."
        if conclusion.fragile:
            evidence += f"\n\nFragile: {conclusion.fragility_reason or 'yes'}"
        finding_id = memory.entry(
            "conclusion", conclusion.statement, evidence, status="unverified"
        )
        memory.edge(finding_id, root, "supports", "Immediate conclusion (unverified).")
    for obstacle in result.strategy.obstacles:
        finding_id = memory.entry(
            "obstacle", obstacle, "Identified by the strategist.", status="open"
        )
        memory.edge(finding_id, root, "blocks", "Strategist obstacle (unverified).")

    attempt_ids: dict[int, str] = {}
    for attempt in result.attempts:
        plan = result.plans[attempt.worker - 1]
        candidate = attempt.candidate
        state = candidate.status if candidate is not None else "unparsed"
        claim = (
            f"Proof attempt {attempt.index} ({attempt.kind} {attempt.worker}): "
            f"{plan.label} [{state}, {attempt.outcome}]"
        )
        evidence = (
            cgm_parsing.render_candidate(candidate)
            if candidate is not None
            else (attempt.error or "No usable reply.")
            + (
                "\n\nReply excerpt:\n" + _clip_utf8(attempt.reply.text, 2_000)
                if attempt.reply is not None and attempt.reply.text
                else ""
            )
        )
        predecessors = [attempt_ids[attempt.parent]] if attempt.parent in attempt_ids else []
        attempt_id = memory.entry(
            "proof_attempt",
            claim,
            evidence,
            status="verified" if attempt.accepted else "unverified",
            fact_id=target_id if attempt.accepted else None,
            predecessors=predecessors,
        )
        attempt_ids[attempt.index] = attempt_id
        memory.edge(attempt_id, root, "addresses", "Proof attempt for the root problem.")
        if attempt.parent in attempt_ids:
            memory.edge(
                attempt_ids[attempt.parent],
                attempt_id,
                "refines",
                "Repair driven by the verifier's repair hints.",
            )
        else:
            memory.edge(
                plan_ids[attempt.worker - 1],
                attempt_id,
                "refines",
                "Worker attempt carrying out this plan.",
            )
        if candidate is not None:
            for finding in candidate.findings:
                finding_id = memory.entry(
                    finding.kind,
                    finding.claim,
                    finding.evidence,
                    status="open" if finding.kind in {"dead_end", "obstacle"} else "unverified",
                    predecessors=[attempt_id],
                )
                edge_type = {
                    "dead_end": "blocks",
                    "obstacle": "blocks",
                    "counterexample": "refutes",
                }.get(finding.kind, "supports")
                memory.edge(
                    finding_id,
                    plan_ids[attempt.worker - 1],
                    edge_type,
                    f"Worker {finding.kind.replace('_', ' ')} (unverified).",
                )

    fact_by_attempt_label: dict[tuple[int, str], str] = {}
    best = result.best_attempt
    for record in facts:
        fact_by_attempt_label[(best.index, record.label)] = record.fact_id
    for record in result.verifications:
        verdict = record.report.verdict
        status = {"correct": "supported", "wrong": "challenged"}.get(verdict, "open")
        fact_id = None
        if verdict == "correct":
            written = next((f for f in facts if f.label == record.label), None)
            if (
                written is not None
                and best.verdicts.get(record.label) is record
            ):
                fact_id = written.fact_id
        memory.entry(
            "verification",
            f"Verifier verdict on {record.label} in attempt {record.attempt}: {verdict}",
            json.dumps(record.report.as_json(), ensure_ascii=False, sort_keys=True),
            status=status,
            fact_id=fact_id,
            predecessors=[attempt_ids[record.attempt]],
        )

    closed_goals: dict[str, list[str]] = {}
    for record in facts:
        memory.edge(
            attempt_ids[record.verification_attempt],
            record.fact_id,
            "promoted-to",
            "Accepted by the independent model verifier.",
        )
        if record.is_target:
            memory.edge(record.fact_id, root, "supports", "Verifier-accepted target fact.")
            continue
        block = best.candidate.lemma(record.label) if best.candidate else None
        if block is not None and block.subgoal:
            goal_node = _goal_id(best.worker, block.subgoal)
            if goal_node in subgoal_ids.get(best.worker, []):
                memory.edge(
                    record.fact_id,
                    goal_node,
                    "supports",
                    "Verifier-accepted lemma settling this subgoal.",
                )
                closed_goals.setdefault(goal_node, []).append(record.fact_id)

    # Curator snapshot: aliases for plans and goals (frontier) and their
    # evidence (attempts, findings, facts).
    aliases: dict[str, str] = {}
    lines: list[str] = []
    frontier: list[str] = []

    def alias(node_id: str) -> str:
        for key, value in aliases.items():
            if value == node_id:
                return key
        key = f"N{len(aliases) + 1}"
        aliases[key] = node_id
        return key

    root_alias = alias(root)
    frontier.append(root_alias)
    lines.append(
        f"{root_alias} goal (frontier) root problem; target "
        + ("verifier-accepted" if target_id else "not verifier-accepted")
    )
    for index, plan in enumerate(result.plans, 1):
        plan_alias = alias(plan_ids[index - 1])
        frontier.append(plan_alias)
        lines.append(
            f"{plan_alias} plan (frontier) \"{_clip_utf8(plan.label, 200)}\" "
            f"technique={plan.technique}: {_clip_utf8(plan.approach, 500)}"
        )
        for goal, goal_node in zip(plan.subgoals, subgoal_ids.get(index, [])):
            goal_alias = alias(goal_node)
            frontier.append(goal_alias)
            state = "closed by verified fact" if goal_node in closed_goals else "open"
            lines.append(
                f"{goal_alias} goal (frontier) subgoal {goal.id} of {plan_alias} "
                f"[{state}]: {_clip_utf8(goal.statement, 400)}"
            )
    for attempt in result.attempts:
        attempt_alias = alias(attempt_ids[attempt.index])
        verdicts = ", ".join(
            f"{label} {record.report.verdict}" for label, record in attempt.verdicts.items()
        ) or "not verified"
        status = attempt.candidate.status if attempt.candidate else "unparsed"
        lines.append(
            f"{attempt_alias} proof_attempt of plan "
            f"{alias(plan_ids[attempt.worker - 1])}: self-status {status}; "
            f"verifier: {verdicts}"
        )
    for name in cgm_schema.OPTIONAL_MEMORY_FILES:
        for row in memory.rows[name]:
            lines.append(
                f"{alias(row['id'])} {row['kind']} (unverified): {_clip_utf8(row['claim'], 300)}"
            )
    for record in facts:
        lines.append(
            f"{alias(record.fact_id)} fact (verifier-accepted) {record.label}: "
            f"{_clip_utf8(record.statement, 300)}"
        )
    edge_lines = [
        f"{alias(edge['src'])} {edge['type']} {alias(edge['dst'])}"
        + (
            f" [and_group {edge['and_group']}, technique {edge['technique']}]"
            if edge["type"] == "decomposes-into"
            else ""
        )
        for edge in memory.edges
    ]
    return _GraphSnapshot(
        memory=memory,
        plan_ids=plan_ids,
        attempt_ids=attempt_ids,
        subgoal_ids=subgoal_ids,
        closed_goals=closed_goals,
        aliases=aliases,
        lines=lines,
        edge_lines=edge_lines,
        frontier=frontier,
    )


def _curator_prompt(problem: str, snapshot: _GraphSnapshot) -> str:
    body = (
        "Annotate every frontier node of this exploring graph.\n\n"
        "<PROJECT_TARGET>\n"
        f"{_clip_utf8(problem, 6_000)}\n"
        "</PROJECT_TARGET>\n\n"
        "<NODES>\n" + "\n".join(snapshot.lines) + "\n</NODES>\n\n"
        "<EDGES>\n" + "\n".join(snapshot.edge_lines) + "\n</EDGES>\n\n"
        f"Frontier nodes to annotate: {', '.join(snapshot.frontier)}"
    )
    return _clip_utf8(body, 60_000)


def _curate(
    ctx: _Ctx,
    result: _PipelineResult,
    snapshot: _GraphSnapshot,
    target_id: str | None,
) -> list[dict[str, Any]]:
    _emit_stage(ctx, "curate")
    notes: dict[str, cgm_parsing.CuratorNote] = {}
    if _stopped(ctx, "curator"):
        ctx.degrade("curator-skipped-by-provider-failure")
        _note("4 Curate · curator skipped (its account failed earlier in this run); deterministic rules only")
    elif ctx.can_call():
        try:
            user = _curator_prompt(result.problem, snapshot)
            job = _run_single(
                ctx,
                "curator",
                reserve=0,
                work=lambda turn: _call_and_parse(
                    ctx,
                    turn,
                    stage="curator",
                    system=cgm_prompts.CURATOR_SYSTEM,
                    user=user,
                    limit=MAX_CURATOR_BYTES,
                    parse=lambda text: cgm_parsing.parse_curator(
                        text, aliases=snapshot.frontier, citable=snapshot.aliases
                    ),
                    family="curator",
                ),
            )
            parsed, _reply = _job_result(job)
            for alias, note in parsed.items():
                cited = tuple(
                    snapshot.aliases[item] for item in note.cited if item in snapshot.aliases
                )
                notes[snapshot.aliases[alias]] = cgm_parsing.CuratorNote(
                    note.status, note.score, note.comment, cited
                )
        except (ParseError, ProviderCallError, PromptBoundError, _BudgetExhausted) as exc:
            if isinstance(exc, ProviderCallError):
                _stop_route(ctx, "curator", exc)
            ctx.degrade("curator-deterministic")
            _note(f"4 Curate · curator unavailable ({type(exc).__name__}); deterministic rules only")
    else:
        ctx.degrade("curator-skipped-by-budget")
    plans = [
        cgm_curation.PlanShape(
            node_id=snapshot.plan_ids[index - 1],
            subgoals=tuple(snapshot.subgoal_ids.get(index, [])),
            accepted=result.accepted_attempt is not None
            and result.accepted_attempt.worker == index,
        )
        for index in range(1, len(result.plans) + 1)
    ]
    evidence: dict[str, list[str]] = {}
    for attempt in result.attempts:
        evidence.setdefault(snapshot.plan_ids[attempt.worker - 1], []).append(
            snapshot.attempt_ids[attempt.index]
        )
    annotations = cgm_curation.propagate(
        plans=plans,
        closed_goals=snapshot.closed_goals,
        root_closed_by=target_id,
        notes=notes,
        default_evidence=evidence,
    )
    timestamp = snapshot.memory.timestamp
    return [
        {
            "id": cgm_schema.annotation_id(item.node_id, AUTHOR, timestamp),
            "timestamp_utc": timestamp,
            "author": AUTHOR,
            "node_id": item.node_id,
            "status": item.status,
            "score": item.score,
            "comment": _clip_utf8(item.comment, 3_900),
            "evidence_considered": list(item.evidence),
        }
        for item in annotations
    ]


def _jsonl(rows: list[dict[str, Any]], *, label: str) -> bytes:
    # One physical line per row even for readers that split on Unicode line
    # separators (U+0085, U+2028, U+2029 are escaped inside strings).
    payload = b"".join(cgm_schema.jsonl_line(row).encode("utf-8") for row in rows)
    if len(payload) > MAX_MEMORY_FILE_BYTES:
        raise MathHarnessError(f"{label} exceeds its byte limit")
    return payload


def _memory_payloads(
    snapshot: _GraphSnapshot, annotations: list[dict[str, Any]]
) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for name in cgm_schema.ENTRY_FILES:
        rows = snapshot.memory.rows[name]
        if rows or name in cgm_schema.REQUIRED_MEMORY_FILES:
            payloads[name] = _jsonl(rows, label=name)
    payloads["edges.jsonl"] = _jsonl(snapshot.memory.edges, label="edge memory")
    payloads["annotations.jsonl"] = _jsonl(annotations, label="annotation memory")
    return payloads


# ---------------------------------------------------------------------------
# filesystem safety helpers
# ---------------------------------------------------------------------------


def _safe_components(relative_parts: tuple[str, ...]) -> None:
    if any(
        not part
        or part in {".", ".."}
        or "/" in part
        or (os.altsep is not None and os.altsep in part)
        for part in relative_parts
    ):
        raise MathHarnessError("unsafe artifact path component")


def _open_descendant_directory(
    parent_fd: int,
    relative_parts: tuple[str, ...],
    *,
    create: bool,
) -> int:
    """Open one fixed descendant chain without re-resolving a parent path."""
    _safe_components(relative_parts)
    try:
        current_fd = os.dup(parent_fd)
    except OSError as exc:
        raise MathHarnessError("could not anchor artifact directory") from exc
    try:
        for part in relative_parts:
            try:
                child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise MathHarnessError(
                        f"could not create artifact directory: {part}"
                    ) from exc
                try:
                    child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                except OSError as exc:
                    raise MathHarnessError(
                        f"unsafe artifact directory: {part}"
                    ) from exc
            except OSError as exc:
                raise MathHarnessError(f"unsafe artifact directory: {part}") from exc
            try:
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    raise MathHarnessError(f"unsafe artifact directory: {part}")
            except Exception:
                os.close(child_fd)
                raise
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_rooted_directory(
    root: Path,
    relative_parts: tuple[str, ...],
    *,
    create: bool,
) -> int:
    try:
        root_fd = os.open(os.fspath(root), _DIRECTORY_FLAGS)
    except OSError as exc:
        raise MathHarnessError("workspace is not a safe directory") from exc
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise MathHarnessError("workspace is not a safe directory")
        return _open_descendant_directory(root_fd, relative_parts, create=create)
    finally:
        os.close(root_fd)


def _ensure_secure_directory(root: Path, relative_parts: tuple[str, ...]) -> Path:
    fd = _open_rooted_directory(root, relative_parts, create=True)
    os.close(fd)
    return root.joinpath(*relative_parts)


def _cleanup_temp_directory(
    root: Path,
    relative_parts: tuple[str, ...],
    *,
    allowed_targets: frozenset[str],
    allow_fact_target: bool = False,
) -> None:
    """Remove only interrupted temp files produced by this adapter."""
    try:
        directory_fd = _open_rooted_directory(root, relative_parts, create=False)
    except FileNotFoundError:
        return
    changed = False
    try:
        try:
            iterator = os.scandir(directory_fd)
        except OSError as exc:
            raise MathHarnessError("could not inspect adapter temp directory") from exc
        with iterator:
            for count, entry in enumerate(iterator, 1):
                if count > 1024:
                    raise MathHarnessError("artifact directory exceeds its safety bound")
                name = entry.name
                if not name.startswith(".") or ".tmp-" not in name:
                    continue
                target, suffix = name[1:].rsplit(".tmp-", 1)
                target_allowed = target in allowed_targets or (
                    allow_fact_target and _FACT_FILE_RE.fullmatch(target) is not None
                )
                if not target_allowed or _TEMP_SUFFIX_RE.fullmatch(suffix) is None:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise MathHarnessError("unsafe interrupted adapter artifact") from exc
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise MathHarnessError("unsafe interrupted adapter artifact")
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError as exc:
                    raise MathHarnessError(
                        "could not remove interrupted adapter artifact"
                    ) from exc
                changed = True
        if changed:
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _cleanup_adapter_temps(root: Path) -> None:
    _cleanup_temp_directory(
        root,
        (),
        allowed_targets=frozenset({"proof.md"}),
    )
    _cleanup_temp_directory(
        root,
        ("math_harness", "project"),
        allowed_targets=frozenset({"PROBLEM.md", "TARGET.md", "engine_receipt.json"}),
    )
    _cleanup_temp_directory(
        root,
        ("math_harness", "project", "global_memory"),
        allowed_targets=frozenset(cgm_schema.MEMORY_FILES),
    )
    _cleanup_temp_directory(
        root,
        ("math_harness", "project", "fact_graph", "facts"),
        allowed_targets=frozenset(),
        allow_fact_target=True,
    )


def _preflight_artifacts(root: Path | None = None) -> Path:
    """Reject static path attacks before making any potentially billed call."""
    root = (root or Path.cwd()).resolve(strict=True)
    if not root.is_dir():
        raise MathHarnessError("run workspace is not a directory")

    for name in ("problem.txt", "proof.md"):
        path = root / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MathHarnessError(f"unsafe artifact target: {name}")

    current = root
    for part in ("math_harness", "project"):
        candidate = current / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return root
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MathHarnessError(f"unsafe artifact directory: {part}")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise MathHarnessError(f"artifact directory escapes workspace: {part}") from exc
        current = resolved

    fixed_files = (
        "PROBLEM.md",
        "TARGET.md",
        "engine_receipt.json",
    )
    for name in fixed_files:
        candidate = current / name
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MathHarnessError(f"unsafe artifact target: {name}")

    for branch, file_names in (
        ("global_memory", cgm_schema.MEMORY_FILES),
        ("fact_graph", ()),
    ):
        directory = current / branch
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MathHarnessError(f"unsafe artifact directory: {branch}")
        resolved = directory.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise MathHarnessError(f"artifact directory escapes workspace: {branch}") from exc
        for name in file_names:
            target = resolved / name
            try:
                target_info = target.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISREG(target_info.st_mode):
                raise MathHarnessError(f"unsafe artifact target: {name}")

    facts = current / "fact_graph" / "facts"
    try:
        facts_info = facts.lstat()
    except FileNotFoundError:
        return root
    if stat.S_ISLNK(facts_info.st_mode) or not stat.S_ISDIR(facts_info.st_mode):
        raise MathHarnessError("unsafe artifact directory: facts")
    resolved_facts = facts.resolve(strict=True)
    try:
        resolved_facts.relative_to(root)
    except ValueError as exc:
        raise MathHarnessError("artifact directory escapes workspace: facts") from exc
    try:
        iterator = os.scandir(resolved_facts)
    except OSError as exc:
        raise MathHarnessError("could not inspect existing facts") from exc
    with iterator:
        for count, entry in enumerate(iterator, 1):
            if count > 512:
                raise MathHarnessError("fact directory exceeds its safety bound")
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise MathHarnessError("unsafe existing fact artifact")
            if _FACT_FILE_RE.fullmatch(entry.name) is None:
                raise MathHarnessError("fact directory contains an unexpected artifact")
    return root


def _read_file_at(
    parent_fd: int,
    name: str,
    *,
    limit: int,
    missing_ok: bool = False,
) -> bytes | None:
    _safe_components((name,))
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise MathHarnessError(f"required file is missing: {name}") from None
    except OSError as exc:
        raise MathHarnessError(f"unsafe file: {name}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise MathHarnessError(f"invalid or oversized file: {name}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
        if len(payload) > limit:
            raise MathHarnessError(f"oversized file: {name}")

        def signature(value: os.stat_result) -> tuple[int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
            )

        if signature(before) != signature(after):
            raise MathHarnessError(f"file changed while being read: {name}")
        return payload
    finally:
        os.close(fd)


def _canonical_problem(
    task: str,
    root: Path,
    *,
    require_problem_file: bool = False,
) -> str:
    root_fd = _open_rooted_directory(root, (), create=False)
    try:
        payload = _read_file_at(
            root_fd, "problem.txt", limit=MAX_PROBLEM_BYTES, missing_ok=True
        )
    finally:
        os.close(root_fd)
    if payload is None:
        if require_problem_file:
            raise MathHarnessError(
                "problem.txt is required when no explicit problem argument is supplied"
            )
        if _utf8_size(task) > MAX_PROBLEM_BYTES:
            raise MathHarnessError(
                "task is too large to use as a fallback problem statement"
            )
        problem = task.strip()
    else:
        try:
            problem = payload.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise MathHarnessError("problem.txt is not UTF-8") from exc
    if not problem:
        raise MathHarnessError("problem statement is empty")
    return problem


def _trusted_source_sha256() -> str:
    """Hash this fixed runner file without any model-controlled path input."""
    source = Path(__file__)
    if source.name != "math_harness_runner.py":
        raise MathHarnessError("unexpected adapter source path")
    try:
        fd = os.open(os.fspath(source), _READ_FLAGS)
    except OSError as exc:
        raise MathHarnessError("could not bind the adapter source") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 2_000_000:
            raise MathHarnessError("adapter source is not a bounded regular file")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > 2_000_000:
                raise MathHarnessError("adapter source exceeds its byte limit")
            digest.update(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MathHarnessError("adapter source changed while being hashed")
        return digest.hexdigest()
    finally:
        os.close(fd)


# The previous generation's 13-field binding (kept to validate continuations).
_LEGACY_BINDING_FIELDS = (
    "source_sha256",
    "problem_sha256",
    "task_sha256",
    "proof_sha256",
    "provider",
    "model",
    "verdict",
    "critic_verdict",
    "critic_response_sha256",
    "target_fact_ids",
    "target_sha256",
    "fact_bytes_sha256",
    "certificate",
)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _legacy_binding_digest(receipt: Mapping[str, Any]) -> str:
    binding = {name: receipt.get(name) for name in _LEGACY_BINDING_FIELDS}
    return _sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _problem_from_project_payload(payload: bytes) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MathHarnessError("existing project problem is not UTF-8") from exc
    prefix = "# Problem\n\n"
    if not text.startswith(prefix) or not text.endswith("\n"):
        raise MathHarnessError("existing project problem has an invalid format")
    problem = text[len(prefix) : -1]
    if (
        not problem
        or problem.strip() != problem
        or (prefix + problem + "\n").encode("utf-8") != payload
    ):
        raise MathHarnessError("existing project problem has an invalid format")
    return problem


def _valid_contract(receipt: Mapping[str, Any]) -> bool:
    workflow = receipt.get("workflow")
    transport = str(receipt.get("transport") or "")
    if transport == cgm_schema.HYBRID_TRANSPORT:
        # A Max receipt: Claude Code answered the advisor families and Codex
        # the worker families; see _route_record.
        return (
            isinstance(workflow, dict)
            and workflow.get("tools_enabled") is False
            and receipt.get("schema_version") == cgm_schema.HYBRID_RECEIPT_SCHEMA_VERSION
            and receipt.get("provider") == "claude_subscription"
            and workflow.get("response_only_contract") == cgm_schema.HYBRID_CONTRACT
            and _valid_route_table(workflow.get("routes"))
        )
    return (
        isinstance(workflow, dict)
        and workflow.get("tools_enabled") is False
        and workflow.get("response_only_contract")
        == _response_only_contract(
            Route(
                provider=str(receipt.get("provider") or ""),
                model=str(receipt.get("model") or ""),
                transport=transport,
            )
        )
        if transport in {"codex_exec", "claude_code", "api_chat"}
        else False
    )


def _valid_route_table(routes: object) -> bool:
    """Whether ``routes`` is the advisor/worker table a Max receipt records."""
    sides = {
        "advisor": ("claude_subscription", "claude_code", ADVISOR_FAMILIES),
        "worker": ("codex_subscription", "codex_exec", WORKER_FAMILIES),
    }
    if not isinstance(routes, dict) or set(routes) != set(sides):
        return False
    for side, (provider, transport, families) in sides.items():
        route = routes[side]
        if not isinstance(route, dict) or set(route) != {
            "families", "provider", "model", "transport", "isolation_version", "effort",
        }:
            return False
        model = route["model"]
        version = route["isolation_version"]
        if (
            route["provider"] != provider
            or route["transport"] != transport
            or route["families"] != sorted(families)
            or not isinstance(model, str)
            or not model.strip()
            or len(model) > 240
            or isinstance(version, bool)
            or version != _response_only_contract(Route(provider, model, transport))["isolation_version"]
            or route["effort"] not in cgm_schema.HYBRID_EFFORTS
        ):
            return False
    return True


def _owned_fact_ids_from_receipt(
    receipt: Mapping[str, Any], fact_names: list[str]
) -> tuple[list[str], str | None, str]:
    """Return (fact ids, target id, author) for a valid ownership receipt.

    Schema 3 (this writer), schema 2 (the sequential CGM writer) and the
    legacy schema-1 critic adapter are recognized, so a continuation can
    retire any of them.
    """
    version = receipt.get("schema_version")
    if (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version in cgm_schema.CGM_RECEIPT_SCHEMA_VERSIONS
    ):
        facts = receipt.get("facts")
        ids = (
            [item.get("fact_id") for item in facts if isinstance(item, dict)]
            if isinstance(facts, list)
            else None
        )
        valid = (
            receipt.get("engine") == "math_harness"
            and receipt.get("source") == ADAPTER_SOURCE
            and receipt.get("semantics") == cgm_schema.SEMANTICS
            and receipt.get("certificate") is False
            and receipt.get("lean_verified") is False
            and receipt.get("verdict") in {"accepted", "unattested"}
            and isinstance(ids, list)
            and len(ids) == len(facts)  # type: ignore[arg-type]
            and all(isinstance(item, str) and cgm_schema.FACT_ID_RE.fullmatch(item) for item in ids)
            and sorted(f"{item}.md" for item in ids) == fact_names
            and _is_sha256(receipt.get("binding_sha256"))
            and receipt.get("binding_sha256") == cgm_schema.receipt_binding_digest(receipt)
            and _valid_contract(receipt)
        )
        if not valid:
            raise MathHarnessError("existing facts are not owned by this adapter generation")
        target = receipt.get("target_fact_id")
        if receipt.get("verdict") == "accepted":
            if not isinstance(target, str) or target not in ids:  # type: ignore[operator]
                raise MathHarnessError("existing ownership receipt has no valid target")
        elif target is not None:
            raise MathHarnessError("existing unattested receipt names a target")
        return list(ids), target, AUTHOR  # type: ignore[arg-type]
    if version == 1:
        ids = receipt.get("target_fact_ids")
        valid = (
            receipt.get("engine") == "math_harness"
            and receipt.get("source") == LEGACY_ADAPTER_SOURCE
            and receipt.get("semantics") == "model-critic"
            and receipt.get("verdict") == "accepted"
            and receipt.get("critic_verdict") == "correct"
            and receipt.get("critic") == "response-only-model-critic"
            and receipt.get("certificate") is False
            and isinstance(ids, list)
            and len(ids) == 1
            and all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{16}", item) for item in ids)
            and sorted(f"{item}.md" for item in ids) == fact_names
            and receipt.get("target_fact_id") == ids[0]
            and receipt.get("fact_path") == f"fact_graph/facts/{ids[0]}.md"
            and all(
                _is_sha256(receipt.get(name))
                for name in (
                    "source_sha256", "problem_sha256", "task_sha256", "proof_sha256",
                    "critic_response_sha256", "target_sha256", "fact_bytes_sha256",
                    "binding_sha256",
                )
            )
            and _valid_contract(receipt)
            and receipt["binding_sha256"] == _legacy_binding_digest(receipt)
        )
        if not valid:
            raise MathHarnessError("existing facts are not owned by this adapter generation")
        return list(ids), ids[0], LEGACY_AUTHOR  # type: ignore[index]
    raise MathHarnessError("existing facts are not owned by this adapter generation")


def _prior_generated_fact_ids(root: Path) -> tuple[str, ...]:
    """Fact ids owned by the previous adapter generation (validated)."""
    try:
        project_fd = _open_rooted_directory(
            root, ("math_harness", "project"), create=False
        )
    except FileNotFoundError:
        return ()
    try:
        try:
            facts_fd = _open_descendant_directory(
                project_fd, ("fact_graph", "facts"), create=False
            )
        except FileNotFoundError:
            return ()
        try:
            names: list[str] = []
            try:
                iterator = os.scandir(facts_fd)
            except OSError as exc:
                raise MathHarnessError(
                    "could not inspect existing fact generation"
                ) from exc
            with iterator:
                for count, entry in enumerate(iterator, 1):
                    if count > 512:
                        raise MathHarnessError(
                            "fact directory exceeds its safety bound"
                        )
                    names.append(entry.name)
            fact_names = sorted(name for name in names if _FACT_FILE_RE.fullmatch(name))
            if len(fact_names) != len(names):
                raise MathHarnessError("fact directory contains an unexpected artifact")
            if not fact_names:
                return ()
            for name in fact_names:
                try:
                    info = os.stat(name, dir_fd=facts_fd, follow_symlinks=False)
                except OSError as exc:
                    raise MathHarnessError("unsafe existing fact artifact") from exc
                if not stat.S_ISREG(info.st_mode):
                    raise MathHarnessError("unsafe existing fact artifact")

            receipt_payload = _read_file_at(
                project_fd,
                "engine_receipt.json",
                limit=MAX_RECEIPT_BYTES,
                missing_ok=True,
            )
            if receipt_payload is None:
                raise MathHarnessError("existing facts have no adapter ownership receipt")
            try:
                receipt = json.loads(receipt_payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise MathHarnessError(
                    "existing facts have an invalid ownership receipt"
                ) from exc
            if not isinstance(receipt, dict):
                raise MathHarnessError("existing facts have an invalid ownership receipt")
            ids, target, author = _owned_fact_ids_from_receipt(receipt, fact_names)

            target_payload = _read_file_at(
                project_fd, "TARGET.md", limit=256, missing_ok=True
            )
            if target is not None:
                if (
                    target_payload != f"{target}\n".encode("ascii")
                    or _sha256(target_payload) != receipt.get("target_sha256")
                ):
                    raise MathHarnessError(
                        "existing target does not match its ownership receipt"
                    )
            elif target_payload is not None:
                raise MathHarnessError("existing target does not match its ownership receipt")
            project_problem_payload = _read_file_at(
                project_fd, "PROBLEM.md", limit=MAX_PROBLEM_BYTES + 32, missing_ok=False
            )
            assert project_problem_payload is not None
            project_problem = _problem_from_project_payload(project_problem_payload)
            if _sha256(project_problem.encode("utf-8")) != receipt.get("problem_sha256"):
                raise MathHarnessError("existing problem does not match its ownership receipt")

            root_fd = _open_rooted_directory(root, (), create=False)
            try:
                proof_payload = _read_file_at(
                    root_fd, "proof.md", limit=MAX_PROOF_BYTES + 10_000, missing_ok=False
                )
            finally:
                os.close(root_fd)
            assert proof_payload is not None
            if _sha256(proof_payload) != receipt.get("proof_sha256"):
                raise MathHarnessError("existing proof does not match its ownership receipt")

            listed = {
                item.get("fact_id"): item
                for item in receipt.get("facts") or []
                if isinstance(item, dict)
            }
            for fact_id in ids:
                payload = _read_file_at(facts_fd, f"{fact_id}.md", limit=MAX_FACT_BYTES)
                assert payload is not None
                if receipt.get("schema_version") in cgm_schema.CGM_RECEIPT_SCHEMA_VERSIONS:
                    entry = listed.get(fact_id) or {}
                    expected_sha = entry.get("sha256")
                    expected_bytes = entry.get("bytes")
                else:
                    expected_sha = receipt.get("fact_bytes_sha256")
                    expected_bytes = receipt.get("fact_bytes")
                if (
                    isinstance(expected_bytes, bool)
                    or not isinstance(expected_bytes, int)
                    or expected_bytes != len(payload)
                    or _sha256(payload) != expected_sha
                ):
                    raise MathHarnessError(
                        "existing fact does not match its ownership receipt"
                    )
                header = (
                    payload.split(b"---", 2)[1]
                    if payload.startswith(b"---")
                    else b""
                )
                if (
                    f"fact_id: {fact_id}".encode("utf-8") not in header
                    or f"author: {author}".encode("utf-8") not in header
                ):
                    raise MathHarnessError(
                        "existing fact does not carry adapter ownership"
                    )
            return tuple(ids)
        finally:
            os.close(facts_fd)
    finally:
        os.close(project_fd)


def _atomic_write(root: Path, relative_parts: tuple[str, ...], payload: bytes) -> Path:
    if not relative_parts:
        raise MathHarnessError("artifact path is empty")
    _safe_components(relative_parts)
    name = relative_parts[-1]
    parent_fd = _open_rooted_directory(root, relative_parts[:-1], create=True)
    try:
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise MathHarnessError(f"unsafe artifact target: {name}") from exc
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise MathHarnessError(f"unsafe artifact target: {name}")

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        temporary = ""
        fd = -1
        try:
            for _attempt in range(16):
                candidate = f".{name}.tmp-{secrets.token_hex(12)}"
                try:
                    fd = os.open(candidate, flags, 0o600, dir_fd=parent_fd)
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise MathHarnessError(f"could not allocate artifact {name}")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short artifact write")
                view = view[written:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary = ""
            os.fsync(parent_fd)
        except OSError as exc:
            raise MathHarnessError(f"could not write artifact {name}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except OSError:
                    pass
    finally:
        os.close(parent_fd)
    return root.joinpath(*relative_parts)


def _remove_regular(root: Path, relative_parts: tuple[str, ...]) -> None:
    if not relative_parts:
        raise MathHarnessError("artifact path is empty")
    _safe_components(relative_parts)
    name = relative_parts[-1]
    parent_fd = _open_rooted_directory(root, relative_parts[:-1], create=True)
    try:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise MathHarnessError(f"unsafe stale artifact: {name}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise MathHarnessError(f"unsafe stale artifact: {name}")
        try:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as exc:
            raise MathHarnessError(f"could not remove stale artifact {name}") from exc
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# receipt and publication
# ---------------------------------------------------------------------------


def _call_counts(calls: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {family: 0 for family in cgm_schema.CALL_FAMILIES}
    for record in calls:
        family = cgm_schema.call_family(record["stage"])
        if family is None:
            raise MathHarnessError(f"unexpected call stage: {record['stage']}")
        counts[family] += 1
    counts["total"] = len(calls)
    return counts


def _receipt_payload(
    *,
    ctx: _Ctx,
    result: _PipelineResult,
    problem_id: str,
    proof_payload: bytes,
    facts: Sequence[_FactRecord],
    target_id: str | None,
    target_payload: bytes | None,
    memories: Mapping[str, bytes],
    rows: Mapping[str, int],
) -> bytes:
    # Canonical turn order: the receipt never depends on completion order.
    calls = ctx.calls
    stages = [record["stage"] for record in calls]
    if len(calls) > ctx.budgets.max_calls or not cgm_schema.canonical_call_order(stages):
        raise MathHarnessError("call records violate the bounded workflow")
    answered = [record for record in calls if record["response_sha256"]]
    if ctx.worker_route is None:
        providers = {record["provider"] for record in answered}
        if len(providers) > 1:
            raise MathHarnessError("provider changed during the bounded workflow")
        final_provider = next(iter(providers)) if providers else ctx.route.provider
        final_model = answered[-1]["model"] if answered else ctx.route.model
    else:
        # Max: every call must come from its side's route.  The receipt's
        # provider and model are the advisor's, whose verifier gates facts.
        for record in calls:
            if record["provider"] != ctx.route_for(record["stage"]).provider:
                raise MathHarnessError("provider changed during the bounded workflow")
        advisor_answers = [
            record
            for record in answered
            if cgm_schema.call_family(record["stage"]) in ADVISOR_FAMILIES
        ]
        final_provider = ctx.route.provider
        final_model = advisor_answers[-1]["model"] if advisor_answers else ctx.route.model
    accepted = target_id is not None
    receipt: dict[str, Any] = {
        "schema_version": cgm_schema.RECEIPT_SCHEMA_VERSION,
        "engine": "math_harness",
        "source": ADAPTER_SOURCE,
        "source_sha256": _trusted_source_sha256(),
        "prompt_pack_sha256": cgm_prompts.prompt_pack_sha256(),
        "prompt_pack_version": cgm_prompts.PROMPT_PACK_VERSION,
        "semantics": cgm_schema.SEMANTICS,
        "verifier": cgm_schema.VERIFIER_LABEL,
        "verdict": "accepted" if accepted else "unattested",
        "certificate": False,
        "lean_verified": False,
        "problem_id": problem_id,
        "problem_sha256": _sha256(result.problem.encode("utf-8")),
        "task_sha256": _sha256(result.task.encode("utf-8")),
        "proof_sha256": _sha256(proof_payload),
        "proof_rendering": (
            cgm_schema.PROOF_RENDERING_VERIFIED
            if accepted
            else cgm_schema.PROOF_RENDERING_UNVERIFIED
        ),
        "provider": final_provider,
        "model": final_model,
        "requested_model": ctx.route.model if ctx.worker_route is None else "max",
        "transport": ctx.route.transport if ctx.worker_route is None else cgm_schema.HYBRID_TRANSPORT,
        "target_fact_id": target_id,
        "target_sha256": _sha256(target_payload) if target_payload is not None else None,
        "facts": [
            {
                "fact_id": record.fact_id,
                "label": record.label,
                "bytes": len(record.payload),
                "sha256": _sha256(record.payload),
                "is_target": record.is_target,
            }
            for record in facts
        ],
        "memory": {name: _sha256(payload) for name, payload in sorted(memories.items())},
        "selected_worker": result.best_attempt.worker,
        "selected_attempt": result.best_attempt.index,
        "verdicts": [
            {
                "call": record.stage,
                "attempt": record.attempt,
                "label": record.label,
                "verdict": record.report.verdict,
                "response_sha256": record.response_sha256,
            }
            for record in result.verifications
        ],
        "workflow": {
            "pipeline": cgm_schema.PIPELINE_NAME,
            "verification_policy": cgm_schema.VERIFICATION_POLICY,
            "call_order": cgm_schema.CALL_ORDER,
            "stages": list(ctx.stages),
            "calls": _call_counts(calls),
            "retries": cgm_schema.retry_counts(stages),
            "budgets": replace(ctx.budgets, concurrency=ctx.concurrency).as_dict(),
            "rows": dict(rows),
            "degraded": list(ctx.degraded),
            "tools_enabled": False,
            "response_only_contract": _response_only_contract(ctx.route),
        },
        "calls": calls,
    }
    if ctx.worker_route is not None:
        receipt["schema_version"] = cgm_schema.HYBRID_RECEIPT_SCHEMA_VERSION
        receipt["workflow"]["response_only_contract"] = dict(cgm_schema.HYBRID_CONTRACT)
        receipt["workflow"]["routes"] = {
            "advisor": _route_record(ctx.route, ADVISOR_FAMILIES, ctx.env),
            "worker": _route_record(ctx.worker_route, WORKER_FAMILIES, ctx.env),
        }
    receipt["binding_sha256"] = cgm_schema.receipt_binding_digest(receipt)
    payload = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"
    if len(payload) > MAX_RECEIPT_BYTES:
        raise MathHarnessError("engine receipt exceeds its byte limit")
    return payload


def _route_record(
    route: Route, families: frozenset[str], env: Mapping[str, str]
) -> dict[str, Any]:
    """One side of a Max receipt: who answered which call families, at which effort."""
    if route.transport == "claude_code":
        effort = str(env.get("AGENT_MONITOR_CLAUDE_EFFORT") or "default").strip().lower()
    else:
        effort = str(env.get("AGENT_MONITOR_RUN_REASONING_EFFORT") or "default").strip().lower()
    return {
        "families": sorted(families),
        "provider": route.provider,
        "model": route.model,
        "transport": route.transport,
        "isolation_version": _response_only_contract(route)["isolation_version"],
        "effort": effort if effort in cgm_schema.HYBRID_EFFORTS else "default",
    }


def _safe_problem_id(
    problem: str, source: Mapping[str, str], explicit: str | None = None
) -> str:
    """A frontmatter-safe problem id: ``[A-Za-z0-9._-]``, at most 80 chars.

    ``explicit`` (a caller-supplied id) takes precedence over the environment;
    both go through the same sanitizer, and an id that sanitizes to nothing
    falls back to a hash of the problem.
    """
    raw = str(
        explicit
        or source.get("AGENT_MONITOR_PROBLEM_ID")
        or source.get("MATH_HARNESS_PROBLEM_ID")
        or ""
    ).strip()
    cleaned = _SAFE_PROBLEM_ID_RE.sub("-", raw).strip(".-_")[:80]
    if cleaned:
        return cleaned
    return f"problem-{_sha256(problem.encode('utf-8'))[:12]}"


def _write_artifacts(
    root: Path,
    *,
    ctx: _Ctx,
    result: _PipelineResult,
    problem_id: str,
    prior_fact_ids: tuple[str, ...],
) -> tuple[list[str], str, list[_FactRecord], str | None]:
    """Publish one generation; the receipt is written last and binds it all."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise MathHarnessError("run workspace is not a directory")
    facts, target_id = _build_facts(result, problem_id)
    proof_text: str
    if target_id is not None:
        proof_text = cgm_closure.render_verified_closure(
            [record.as_mapping() for record in facts], target_id
        )
        if _utf8_size(proof_text) > MAX_PROOF_BYTES:
            # Never publish a clipped "verified" proof; fall back honestly.
            ctx.degrade("verified-closure-too-large")
            result.accepted_attempt = None
            result.best_attempt.accepted = False
            facts, target_id = _build_facts(result, problem_id)
    if target_id is None:
        proof_text = _render_unverified(result, _unverified_reason(result, ctx))
    _self_check_facts(facts, target_id, result.problem, proof_text)
    snapshot = _build_memory(result, facts, target_id)
    annotations = _curate(ctx, result, snapshot, target_id)
    _emit_stage(ctx, "finalize")
    memories = _memory_payloads(snapshot, annotations)
    rows = {
        name: payload.count(b"\n") for name, payload in memories.items()
    }
    target_payload = f"{target_id}\n".encode("ascii") if target_id else None
    proof_payload = proof_text.encode("utf-8")
    problem_payload = ("# Problem\n\n" + result.problem.strip() + "\n").encode("utf-8")
    receipt_payload = _receipt_payload(
        ctx=ctx,
        result=result,
        problem_id=problem_id,
        proof_payload=proof_payload,
        facts=facts,
        target_id=target_id,
        target_payload=target_payload,
        memories=memories,
        rows=rows,
    )
    _self_check_receipt(receipt_payload)

    # A stop request is honored up to here.  From the first write on, the
    # generation is published to the end (the receipt last), so a stop can
    # never leave a half-written generation behind.
    if ctx.cancel.is_set():
        raise RunCancelled("the Math Harness run was stopped before publication")

    # Validate every fixed directory before the first write.  Model content is
    # never used as a path component.
    _ensure_secure_directory(root, ("math_harness", "project", "global_memory"))
    _ensure_secure_directory(root, ("math_harness", "project", "fact_graph", "facts"))
    if _prior_generated_fact_ids(root) != prior_fact_ids:
        raise MathHarnessError("existing fact generation changed during the run")

    # Retire the previous generation before publishing any replacement.  The
    # receipt is removed first, so a mid-write failure cannot retain a stale
    # attestation for graph bytes that have changed.
    _remove_regular(root, ("math_harness", "project", "engine_receipt.json"))
    _remove_regular(root, ("math_harness", "project", "TARGET.md"))
    for old_fact_id in prior_fact_ids:
        _remove_regular(
            root,
            ("math_harness", "project", "fact_graph", "facts", f"{old_fact_id}.md"),
        )
    for name in cgm_schema.MEMORY_FILES:
        if name not in memories:
            _remove_regular(root, ("math_harness", "project", "global_memory", name))

    written: list[str] = []
    _atomic_write(root, ("math_harness", "project", "PROBLEM.md"), problem_payload)
    written.append("math_harness/project/PROBLEM.md")
    for name, payload in memories.items():
        _atomic_write(root, ("math_harness", "project", "global_memory", name), payload)
        written.append(f"math_harness/project/global_memory/{name}")
    for record in facts:
        _atomic_write(
            root,
            ("math_harness", "project", "fact_graph", "facts", f"{record.fact_id}.md"),
            record.payload,
        )
        written.append(f"math_harness/project/fact_graph/facts/{record.fact_id}.md")
    if target_payload is not None:
        _atomic_write(root, ("math_harness", "project", "TARGET.md"), target_payload)
        written.append("math_harness/project/TARGET.md")
    _atomic_write(root, ("proof.md",), proof_payload)
    written.append("proof.md")
    # The canonical receipt is last: it binds both graph and root proof bytes.
    _atomic_write(root, ("math_harness", "project", "engine_receipt.json"), receipt_payload)
    written.append("math_harness/project/engine_receipt.json")
    return written, proof_text, facts, target_id


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


# SIGTERM stops every active run in this process.  The handler only sets
# events (no locks), so it is safe at any point of the coordinator thread.
_ACTIVE_RUN_CANCELS: list[threading.Event] = []


def _stop_active_runs(_signum: int | None = None, _frame: object = None) -> None:
    for event in tuple(_ACTIVE_RUN_CANCELS):
        event.set()


@contextmanager
def _stop_on_sigterm(cancel: threading.Event) -> Iterator[None]:
    """Route SIGTERM to ``cancel`` for the duration of one run.

    Installed only from the main thread and only over the default action (or
    this module's own handler), so a caller's own SIGTERM handling is left
    alone; the previous handler is restored afterwards.
    """
    _ACTIVE_RUN_CANCELS.append(cancel)
    previous: Any = None
    installed = False
    try:
        if threading.current_thread() is threading.main_thread():
            try:
                previous = signal.getsignal(signal.SIGTERM)
                if previous in (signal.SIG_DFL, _stop_active_runs):
                    signal.signal(signal.SIGTERM, _stop_active_runs)
                    installed = True
            except (ValueError, OSError):  # not the main interpreter
                installed = False
        yield
    finally:
        if installed:
            signal.signal(signal.SIGTERM, previous)
        _ACTIVE_RUN_CANCELS.remove(cancel)


def run_pipeline(
    workspace: Path,
    problem_text: str,
    route: Route,
    *,
    budgets: Budgets | None = None,
    emit: Callable[[dict[str, Any]], None] | None = None,
    task: str | None = None,
    problem_id: str | None = None,
    worker_route: Route | None = None,
) -> dict[str, Any]:
    """Run the full CGM pipeline in ``workspace`` and publish its artifacts.

    ``problem_text`` is the canonical statement (it becomes PROBLEM.md and the
    target fact's statement).  ``route`` is a response-only transport, for
    example ``Route("api", "kimi-k3", "api_chat")``.  ``budgets`` defaults to
    :meth:`Budgets.from_env` (its ``concurrency`` bounds the provider calls in
    flight; unset means the route default); ``emit`` receives Codex-shaped
    JSON events (default: JSONL on stdout), one at a time and never after
    this call returns or raises (a slow sink delays only its own run);
    ``task`` is optional untrusted context shown to the strategist and
    workers (default: the problem itself); ``problem_id`` defaults to
    ``AGENT_MONITOR_PROBLEM_ID`` or a hash of the problem; either is reduced
    to ``[A-Za-z0-9._-]`` (at most 80 characters) before use.
    ``worker_route`` (Max mode) answers the worker and repair calls while
    ``route`` answers the strategist, verifier and curator calls.

    Writes under ``<workspace>/math_harness/project/``: ``PROBLEM.md``,
    ``global_memory/*.jsonl``, ``fact_graph/facts/<id>.md`` for each verified
    fact, ``TARGET.md`` iff the target was verifier-accepted, and
    ``engine_receipt.json`` (last); plus ``<workspace>/proof.md``.  Given the
    same replies, every artifact is identical whatever order concurrent
    calls complete in.

    Returns ``{"status": "accepted"|"unattested", "accepted": bool,
    "proof_path", "proof_text", "receipt_path", "target_fact_id",
    "fact_ids", "written", "calls", "stages", "verdicts", "degraded",
    "prompt_pack_sha256", "provider", "model", "observed_model"}``.  Every
    failure raises :class:`MathHarnessError` (``ProviderCallError`` for a
    provider failure, :class:`RunCancelled` when a SIGTERM arrives before
    publication starts) and publishes no receipt: an unsafe workspace, a
    failed strategist call (an empty strategist reply degrades to one direct
    plan instead), no usable worker candidate, or an I/O or self-check error
    while publishing.  The process environment is read once and never
    modified; concurrent runs in one process are independent.

    A server-sponsored Kimi run (``AGENT_MONITOR_SPONSORED_KIMI`` or
    ``AGENT_MONITOR_PUBLIC_KIMI``) gives each call 630 s by default and shares
    one cap of ``kimi_k3.SPONSORED_RUN_RETRIES`` transport retries.  A
    strategist call the provider refused before processing it is sent again
    after 30, 60 and then 120 s; when it still fails, or fails ambiguously (a
    timeout, say), the run continues with the direct plan; only a failure
    every later call would repeat (an exhausted call budget, an expired
    token) aborts it.  When the provider refused every call of every worker
    unprocessed, one rescue worker turn runs after 60 s.  No pause leaves the
    run less than ``_BUSY_WAIT_MARGIN_SECONDS``.  A failure's ``failure``
    attribute carries the content-free facts of the provider failure behind
    it, and its ``billed`` attribute whether any call of the run may have
    been processed (and billed).
    """
    cancel = threading.Event()
    emitter = _RunEmitter(emit)
    token = _RUN_EMITTER.set(emitter)
    try:
        with _stop_on_sigterm(cancel):
            return _run_pipeline(
                workspace,
                problem_text,
                route,
                budgets=budgets,
                task=task,
                problem_id=problem_id,
                cancel=cancel,
                emitter=emitter,
                worker_route=worker_route,
            )
    except MathHarnessError:
        raise
    except (OSError, ValueError) as exc:
        # ValueError covers ParseError, ClosureError and EngineGraphError from
        # the publication self-checks.  Never reflect the detail: it may quote
        # model output or paths.
        raise MathHarnessError(f"Math Harness failed closed: {type(exc).__name__}") from exc
    finally:
        # Abandoned calls may still be running: an event already being
        # delivered finishes first, and the sink is never called afterwards.
        emitter.close()
        _RUN_EMITTER.reset(token)


def _run_pipeline(
    workspace: Path,
    problem_text: str,
    route: Route,
    *,
    budgets: Budgets | None,
    task: str | None,
    problem_id: str | None,
    cancel: threading.Event,
    emitter: _RunEmitter,
    worker_route: Route | None = None,
) -> dict[str, Any]:
    root = Path(workspace).resolve(strict=True)
    problem = str(problem_text or "").strip()
    if not problem:
        raise MathHarnessError("problem statement is empty")
    if _utf8_size(problem) > MAX_PROBLEM_BYTES:
        raise MathHarnessError("problem statement exceeds its byte limit")
    task_text = (task or "").strip() or problem
    if _utf8_size(task_text) > MAX_TASK_BYTES:
        raise MathHarnessError("task exceeds its byte limit")
    _cleanup_adapter_temps(root)
    _preflight_artifacts(root)
    prior_fact_ids = _prior_generated_fact_ids(root)
    # One read-only snapshot of the settings for every call of this run.
    env: dict[str, str] = dict(os.environ)
    resolved_budgets = budgets or Budgets.from_env(env)
    concurrency = resolved_budgets.resolved_concurrency(route)
    sponsored_kimi = (
        worker_route is None and _is_kimi_route(route) and kimi_k3.is_sponsored(env)
    )
    api_timeout = _bounded_int(
        env,
        ("AGENT_MONITOR_CGM_API_TIMEOUT",),
        # A sponsored call waits for the broker's own answer (its gateway
        # deadline plus 15 s) instead of abandoning a slow healthy reply.
        default=kimi_k3.SPONSORED_TIMEOUT_SECONDS if sponsored_kimi else 300,
        minimum=60,
        maximum=900,
    )
    if worker_route is not None and (
        route.transport != "claude_code" or worker_route.transport != "codex_exec"
    ):
        raise MathHarnessError("Max pairs a Claude Code advisor with a Codex worker")
    if route.transport == "claude_code":
        # Configuration errors surface before any potentially billed call.
        for stage in ("strategist", "worker-1", "verify-1", "repair-1", "curator"):
            _claude_stage_timeout(stage, env)
    ctx = _Ctx(
        worker_route=worker_route,
        route=route,
        budgets=replace(resolved_budgets, concurrency=concurrency),
        started=time.monotonic(),
        env=env,
        api_timeout=api_timeout,
        concurrency=concurrency,
        cancel=cancel,
        emitter=emitter,
        sponsored_kimi=sponsored_kimi,
        kimi_retries=(
            kimi_k3.RetryAllowance(kimi_k3.SPONSORED_RUN_RETRIES) if sponsored_kimi else None
        ),
        busy_waits=list(_STRATEGIST_BUSY_WAITS) if sponsored_kimi else [],
    )
    _out({"type": "thread.started", "thread_id": "math-harness"})
    emit_item(
        {
            "type": "agent_message",
            "text": (
                "Math Agent Harness · CGM pipeline · up to "
                f"{resolved_budgets.max_workers} worker"
                f"{'s' if resolved_budgets.max_workers != 1 else ''} · "
                f"{resolved_budgets.max_calls} calls · {concurrency} in flight · "
                + (
                    f"{route.model} · no tools"
                    if worker_route is None
                    else (
                        f"Max: Claude Code {route.model} plans, verifies and curates; "
                        f"Codex {worker_route.model} writes and repairs · no tools"
                    )
                )
            ),
        }
    )
    result = _run_cgm(ctx, problem, task_text)
    written, proof_text, facts, target_id = _write_artifacts(
        root,
        ctx=ctx,
        result=result,
        problem_id=_safe_problem_id(problem, env, problem_id),
        prior_fact_ids=prior_fact_ids,
    )
    author = result.best_attempt.reply
    return {
        "status": "accepted" if target_id else "unattested",
        "accepted": target_id is not None,
        "proof_path": str(root / "proof.md"),
        "proof_text": proof_text,
        "receipt_path": str(root / "math_harness" / "project" / "engine_receipt.json"),
        "target_fact_id": target_id,
        "fact_ids": [record.fact_id for record in facts],
        "written": written,
        "calls": ctx.calls_used,
        "stages": list(ctx.stages),
        "verdicts": [
            {
                "call": record.stage,
                "attempt": record.attempt,
                "label": record.label,
                "verdict": record.report.verdict,
            }
            for record in result.verifications
        ],
        "degraded": list(ctx.degraded),
        "prompt_pack_sha256": cgm_prompts.prompt_pack_sha256(),
        "provider": author.provider if author else route.provider,
        "model": author.model if author else route.model,
        "observed_model": getattr(author, "observed_model", None),
    }


def main() -> int:
    explicit_task = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    task = explicit_task or DEFAULT_TASK_DIRECTIVE
    if _utf8_size(task) > MAX_TASK_BYTES:
        emit_item({"type": "error", "message": "task exceeds its byte limit"})
        return 2

    try:
        initial_root = Path.cwd().resolve(strict=True)
        _cleanup_adapter_temps(initial_root)
        root = _preflight_artifacts(initial_root)
        problem = _canonical_problem(
            task,
            root,
            require_problem_file=not bool(explicit_task),
        )
        _prior_generated_fact_ids(root)
        route, worker_route = _select_routes(os.environ)
        budgets = Budgets.from_env(os.environ)
        result = run_pipeline(
            root, problem, route, budgets=budgets, task=task, worker_route=worker_route
        )
    except RunCancelled as exc:
        emit_item({"type": "error", "message": str(exc), "terminal": True})
        return 128 + int(signal.SIGTERM)
    except MathHarnessError as exc:
        emit_item({"type": "error", "message": str(exc), "terminal": True})
        return 1
    except (OSError, ValueError) as exc:
        emit_item(
            {
                "type": "error",
                "message": f"Math Harness failed closed: {type(exc).__name__}",
                "terminal": True,
            }
        )
        return 1

    emit_item(
        {
            "type": "file_change",
            "changes": [{"path": path} for path in result["written"]],
        }
    )
    status = (
        f"accepted by the independent model verifier ({len(result['fact_ids'])} fact"
        f"{'s' if len(result['fact_ids']) != 1 else ''}; not a Lean certificate)"
        if result["accepted"]
        else "not verifier-accepted (unattested candidate)"
    )
    # Attribute the published proof to its author, never the strategist,
    # verifier or curator, nor the routing model when telemetry is absent.
    observed = result.get("observed_model")
    emit_item(
        {
            "type": "agent_message",
            "text": f"Math Harness completed · {status}\n\n{result['proof_text'][:6000]}",
            "model": observed,
            "model_source": "response" if observed else None,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
