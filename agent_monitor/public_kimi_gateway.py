"""Loopback credential boundary for the standalone public Kimi service.

The public proof workers receive only a short-lived loopback bearer token.  The
real upstream credential remains in this server process and is substituted only
after a request has passed the path, size, authentication, model, and rate
checks below.

Concurrency: at most ``max_concurrency`` upstream calls run at once.  A call
that arrives while every slot is busy waits for one, first come first served,
for at most ``queue_seconds`` and only while fewer than ``queue_depth`` calls
are already waiting; otherwise (and when the wait runs out) it gets an
immediate 429 without reaching the provider.

Deadline: every call has one total wall-clock budget, ``upstream_timeout`` or
the shorter ``X-Ansatze-Timeout`` its caller sends, measured from the moment
the request arrives.  The queue wait is carved out of that budget, and a
watchdog shuts the upstream connection down when it runs out, even while the
provider is still trickling bytes, so the caller always gets a (typed) answer
within it: the gateway's worst-case response time, which every client timeout
is sized against, is the budget itself.  A caller that hangs up first (its own
timeout fired, or its run was stopped) gets its call cut off too, within about
a second, so no upstream request or slot outlives the caller that asked for it.

Errors: every non-2xx JSON answer has the ``error_body`` shape.  Its ``type``
and ``forwarded`` say what failed and whether the request reached the
provider, and ``upstream_status``/``upstream_elapsed_ms`` tell a fast refusal
from a late failure.  Provider error bodies are never passed on.
"""
from __future__ import annotations

import heapq
import hmac
import http.client
import itertools
import json
import logging
import math
import re
import select
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib import error as urlerror
from urllib import request as urlrequest

from agent_monitor.kimi_responses_proxy import sanitize_responses_payload
from agent_monitor.runners.kimi_chat_completions_proxy import sanitize_chat_payload

_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_STREAM_RESPONSE_BYTES = 16 * 1024 * 1024
_CHAT_PATH = "/v1/chat/completions"
_RESPONSES_PATH = "/v1/responses"
# Hard ceilings for the capacity queue, and the upstream time a queued call is
# always left with (the wait never eats into it).
_MAX_QUEUE_SECONDS = 120
_MAX_QUEUE_DEPTH = 128
_MIN_UPSTREAM_SECONDS = 60.0
# Hard ceiling for one call's total budget (PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS).
_MAX_UPSTREAM_SECONDS = 900.0
# A caller's own remaining budget, in decimal seconds (any precision, as a
# float prints).  It can only shorten the gateway's deadline, never lengthen it.
DEADLINE_HEADER = "X-Ansatze-Timeout"
_DEADLINE_RE = re.compile(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+")
_DEADLINE_MAX_CHARS = 64
# How often the watchdog checks that the callers of running calls are still
# connected.
_CALLER_POLL_SECONDS = 1.0
# Passive provider health for /healthz: the outcomes of the last ten minutes.
# A refusal within _FAST_REFUSAL_MS means an overloaded provider, not a slow one.
_HEALTH_WINDOW_SECONDS = 600.0
_HEALTH_MAX_EVENTS = 4096
_FAST_REFUSAL_MS = 2000
_FAST_REFUSAL_RECENT_SECONDS = 120.0
# A call that runs out of this much time (or of the operator's whole cap, if
# that is lower) shows a slow provider.  One cut short by its caller's own
# shorter budget says nothing about the provider.
_SLOW_PROVIDER_SECONDS = 600.0
# Refusals every caller gets alike (with 5xx and refused redirects): an
# overloaded provider, a revoked or unfunded key, or a wrong upstream base.
_PROVIDER_FAILURE_STATUSES = frozenset({401, 402, 403, 404, 429})
# The caller hung up (typically its own timeout fired) before it was answered.
CLIENT_GONE_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# Content-free lines only: never tokens, prompts, outputs, keys or bodies.
logger = logging.getLogger("agent_monitor.public_kimi.gateway")


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Surface an upstream 3xx as its status instead of following it.

    urllib's default opener follows 301/302/303 (even to plain http) and copies
    every header except the body headers, so it would hand the real upstream
    key to whatever host a redirect names.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _shutdown(sock: socket.socket) -> None:
    """Wake every thread blocked on ``sock``; closing it alone would not."""
    try:
        # The base method on purpose: SSLSocket.shutdown would also drop the
        # TLS state under a reader in another thread.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


def _hung_up(sock: socket.socket) -> bool:
    """Whether the peer of ``sock`` has closed it, without reading its data.

    Only a definite end of stream or reset counts: anything else (pending
    bytes, a socket that cannot be peeked at) keeps the caller connected.
    """
    try:
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        if not poller.poll(0):
            return False
        return not sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
    except (BlockingIOError, InterruptedError):
        return False
    except CLIENT_GONE_ERRORS:
        return True
    except (OSError, ValueError):
        return False


class WatchedCall:
    """Deadline and progress of one outgoing request.

    That is a gateway call to the provider, or the sponsored broker's call to
    this gateway.  A ``DeadlineWatchdog`` calls ``expire`` when the deadline
    passes, or when ``caller`` (the socket of whoever asked for this call)
    hangs up first; either shuts the connection down under any blocked read.
    The connection classes below report when the request starts and when it
    has been sent in full: only a failure before that point certainly never
    reached the other end.
    """

    def __init__(self, deadline: float, *, caller: socket.socket | None = None) -> None:
        self.deadline = deadline
        self.caller = caller
        self.started = time.monotonic()
        self.status_at: float | None = None
        self.sent = False
        self.done = False
        self.expired = False
        # The caller hung up first (``expired`` is then True as well).
        self.abandoned = False
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        # A duplicate of the new TCP socket while TLS (or a proxy tunnel) is
        # set up: the TLS socket takes the descriptor over from the original
        # object, so only a duplicate can still shut the connection down.
        self._handshake: socket.socket | None = None

    def remaining(self) -> float:
        return max(0.1, self.deadline - time.monotonic())

    def handshaking(self, sock: socket.socket) -> None:
        """The TCP connection is up; TLS or a proxy tunnel is set up next."""
        try:
            duplicate = sock.dup()
        except OSError:
            return
        with self._lock:
            if not self.expired:
                self._handshake = duplicate
                return
        _shutdown(duplicate)
        duplicate.close()

    def connected(self, sock: socket.socket) -> None:
        """The connection is up; the request is sent next."""
        self.started = time.monotonic()
        with self._lock:
            duplicate, self._handshake = self._handshake, None
            expired = self.expired
            if not expired:
                self._sockets.append(sock)
        if duplicate is not None:
            duplicate.close()
        if expired:
            _shutdown(sock)

    def request_sent(self) -> None:
        self.sent = True

    def status_received(self) -> None:
        self.status_at = time.monotonic()

    def elapsed_ms(self) -> int | None:
        """From just before the request was sent to its status line."""
        return None if self.status_at is None else _ms(self.status_at - self.started)

    def caller_gone(self) -> bool:
        caller = self.caller
        return caller is not None and not self.done and _hung_up(caller)

    def expire(self, *, abandoned: bool = False) -> None:
        """Cut the call off: its deadline passed, or its caller left."""
        with self._lock:
            if self.done or self.expired:
                return
            self.expired = True
            self.abandoned = abandoned
            sockets, self._sockets = self._sockets, []
            duplicate, self._handshake = self._handshake, None
        for sock in sockets:
            _shutdown(sock)
        if duplicate is not None:
            _shutdown(duplicate)
            duplicate.close()

    def finish(self) -> bool:
        """Stop watching; True when the call was cut off first."""
        with self._lock:
            self.done = True
            self.caller = None
            self._sockets = []
            duplicate, self._handshake = self._handshake, None
            expired = self.expired
        if duplicate is not None:
            duplicate.close()
        return expired


class DeadlineWatchdog:
    """One thread that cuts watched calls off at their total deadline, or as
    soon as their caller has hung up.

    urllib's own timeout applies to each socket operation, so a provider that
    sends its headers early and then trickles bytes would never time out, and
    a caller that gave up first would leave its call (and its slot) running.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._calls: list[tuple[float, int, WatchedCall]] = []
        self._order = itertools.count()
        self._thread: threading.Thread | None = None
        self._closed = False

    def watch(self, call: WatchedCall) -> None:
        with self._cond:
            if not self._closed:
                heapq.heappush(self._calls, (call.deadline, next(self._order), call))
                if self._thread is None:
                    self._thread = threading.Thread(
                        target=self._run,
                        name="public-kimi-upstream-deadline",
                        daemon=True,
                    )
                    self._thread.start()
                self._cond.notify()
                return
        # The gateway is shutting down: no new call may outlive it.
        call.expire()

    def close(self) -> None:
        """Stop the thread and cut off every call still in flight."""
        with self._cond:
            self._closed = True
            calls = [call for _deadline, _order, call in self._calls]
            self._calls.clear()
            thread = self._thread
            self._cond.notify()
        for call in calls:
            call.expire()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _run(self) -> None:
        next_poll = 0.0
        while True:
            with self._cond:
                if self._closed:
                    return
                now = time.monotonic()
                due: list[WatchedCall] = []
                # Finished calls leave lazily, once they reach the top.
                while self._calls and (
                    self._calls[0][2].done or self._calls[0][0] <= now
                ):
                    call = heapq.heappop(self._calls)[2]
                    if not call.done:
                        due.append(call)
                callers = [
                    entry[2]
                    for entry in self._calls
                    if entry[2].caller is not None and not entry[2].done
                ]
                polling = bool(callers) and now >= next_poll
                if polling:
                    next_poll = now + _CALLER_POLL_SECONDS
                elif not due:
                    wake = [self._calls[0][0]] if self._calls else []
                    if callers:
                        wake.append(next_poll)
                    self._cond.wait(min(wake) - now if wake else None)
                    continue
            for call in due:
                call.expire()
            if polling:
                for call in callers:
                    if call.caller_gone():
                        call.expire(abandoned=True)


# The call the current handler thread is making.  urllib creates and drives
# its connection on the calling thread, so the connection classes find their
# call here (see ``open_watched``).
_ACTIVE = threading.local()


class _WatchedConnection:
    """Report the connect and send progress of a watched request."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[call-arg]
        # http.client keeps this hook on the instance for replacing.
        self._create_connection = self._watched_create_connection

    def _watched_create_connection(
        self, address: Any, timeout: Any, source_address: Any = None
    ) -> socket.socket:
        call = getattr(_ACTIVE, "call", None)
        if call is None:
            return socket.create_connection(address, timeout, source_address)
        sock = socket.create_connection(address, call.remaining(), source_address)
        try:
            # TLS and any proxy tunnel get only what is left of the budget,
            # and the watchdog can end them at the deadline.
            sock.settimeout(call.remaining())
            call.handshaking(sock)
        except BaseException:
            sock.close()
            raise
        return sock

    def connect(self) -> None:
        super().connect()  # type: ignore[misc]
        call = getattr(_ACTIVE, "call", None)
        if call is not None:
            call.connected(self.sock)  # type: ignore[attr-defined]

    def getresponse(self):  # noqa: ANN201
        # urllib asks for the response only once the whole request was sent.
        call = getattr(_ACTIVE, "call", None)
        if call is not None:
            call.request_sent()
        return super().getresponse()  # type: ignore[misc]


class _WatchedHTTPConnection(_WatchedConnection, http.client.HTTPConnection):
    pass


class _WatchedHTTPSConnection(_WatchedConnection, http.client.HTTPSConnection):
    pass


class _WatchedHTTPHandler(urlrequest.HTTPHandler):
    def http_open(self, req):  # noqa: ANN001, ANN201, D102
        return self.do_open(_WatchedHTTPConnection, req)


class _WatchedHTTPSHandler(urlrequest.HTTPSHandler):
    def https_open(self, req):  # noqa: ANN001, ANN201, D102
        return self.do_open(_WatchedHTTPSConnection, req, context=self._context)


# The only hop that carries the real upstream credential.  Ambient proxy
# settings keep working as before (HTTPS goes through CONNECT, so a proxy
# never sees the key); redirects are never followed.  The watched handlers
# replace urllib's default HTTP(S) handlers and change nothing on the wire.
_UPSTREAM_OPENER = urlrequest.build_opener(
    _NoRedirect(), _WatchedHTTPHandler(), _WatchedHTTPSHandler()
)
# The sponsored broker's loopback hop to this gateway, watched the same way.
# It carries the gateway's token, so it never goes through a proxy and never
# follows a redirect.
LOOPBACK_OPENER = urlrequest.build_opener(
    urlrequest.ProxyHandler({}), _NoRedirect(), _WatchedHTTPHandler()
)


def open_watched(
    opener: urlrequest.OpenerDirector, request: urlrequest.Request, call: WatchedCall
) -> Any:
    """``opener.open`` with ``call`` following the connection it makes."""
    _ACTIVE.call = call
    try:
        return opener.open(request, timeout=call.remaining())
    finally:
        _ACTIVE.call = None


def error_body(
    message: str,
    error_type: str,
    *,
    forwarded: bool = False,
    upstream_status: int | None = None,
    upstream_elapsed_ms: int | None = None,
    retry_after: int | None = None,
) -> dict[str, Any]:
    """The typed error body shared by the gateway and the sponsored broker.

    ``forwarded`` is False only when the request certainly never reached the
    provider, so a caller may send it again without paying twice.
    """
    return {
        "error": {
            "message": message,
            "type": error_type,
            "forwarded": bool(forwarded),
            "upstream_status": upstream_status,
            "upstream_elapsed_ms": upstream_elapsed_ms,
            "retry_after": retry_after,
        }
    }


def deadline_seconds(value: object) -> float | None:
    """A positive decimal ``X-Ansatze-Timeout`` value, else None (ignored)."""
    text = str(value or "").strip()
    if len(text) > _DEADLINE_MAX_CHARS or not _DEADLINE_RE.fullmatch(text):
        return None
    seconds = float(text)
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def format_deadline(seconds: float) -> str:
    """An ``X-Ansatze-Timeout`` value of at least one second.

    Rounded down to the millisecond, so passing it on never lengthens it.
    """
    value = math.floor(max(1.0, float(seconds)) * 1000) / 1000
    return f"{value:.3f}".rstrip("0").rstrip(".")


def response_incomplete(response: Any) -> bool:
    """Whether a Content-Length body ended early (http.client returns it short)."""
    remaining = getattr(response, "length", None)
    return isinstance(remaining, int) and not isinstance(remaining, bool) and remaining > 0


def _bounded_positive(value: object, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _sanitize_payload(
    payload: Mapping[str, Any],
    *,
    reasoning_effort: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Force K3 while retaining bounded OpenAI-compatible tool turns."""
    sanitized, _summary = sanitize_chat_payload(
        payload,
        reasoning_effort=reasoning_effort,
    )
    messages = sanitized.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty array")
    if "stream" in sanitized and not isinstance(sanitized.get("stream"), bool):
        raise ValueError("stream must be a boolean")
    sanitized["max_tokens"] = _bounded_positive(
        sanitized.get("max_tokens"),
        default=max_output_tokens,
        low=1,
        high=max_output_tokens,
    )
    return sanitized


def _sanitize_responses_gateway_payload(
    payload: Mapping[str, Any],
    *,
    reasoning_effort: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Force and cap the Formal-only Responses request sent upstream."""
    sanitized, _summary = sanitize_responses_payload(
        payload,
        reasoning_effort=reasoning_effort,
    )
    if not sanitized.get("input"):
        raise ValueError("input must be nonempty")
    if "stream" in sanitized and not isinstance(sanitized.get("stream"), bool):
        raise ValueError("stream must be a boolean")
    sanitized["model"] = "kimi-k3"
    sanitized["max_output_tokens"] = _bounded_positive(
        sanitized.get("max_output_tokens"),
        default=max_output_tokens,
        low=1,
        high=max_output_tokens,
    )
    return sanitized




class _FairCapacity:
    """Concurrency slots handed out first come, first served.

    ``acquire`` takes a free slot at once when nobody is waiting.  Otherwise
    the caller joins the queue (only while fewer than ``max_waiting`` callers
    are already waiting) and waits at most ``timeout`` seconds for its turn; a
    newcomer never overtakes a waiting call.  ``release`` returns a slot and
    may not be called more often than slots were acquired.
    """

    def __init__(self, slots: int) -> None:
        self._slots = int(slots)
        self._free = int(slots)
        self._cond = threading.Condition(threading.Lock())
        self._queue: deque[object] = deque()

    @property
    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    def acquire(self, *, timeout: float = 0.0, max_waiting: int = 0) -> bool:
        with self._cond:
            if self._free > 0 and not self._queue:
                self._free -= 1
                return True
            if timeout <= 0 or len(self._queue) >= max_waiting:
                return False
            ticket = object()
            self._queue.append(ticket)
            deadline = time.monotonic() + timeout
            try:
                while self._queue[0] is not ticket or self._free <= 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(remaining)
                self._free -= 1
                return True
            finally:
                self._queue.remove(ticket)
                # The next waiter may now be first in line with a free slot.
                self._cond.notify_all()

    def release(self) -> None:
        with self._cond:
            if self._free >= self._slots:
                raise ValueError("gateway capacity released too many times")
            self._free += 1
            self._cond.notify_all()


@dataclass
class _CallLog:
    """Content-free facts about one model call, logged as one line.

    ``status`` 0 means no answer was sent (the caller had already left).
    """

    family: str
    status: int = 0
    error_type: str = "internal_error"
    upstream_status: int | None = None
    upstream_elapsed_ms: int | None = None
    queue_ms: int = 0
    response_bytes: int = 0
    client_connected: bool = True
    # The call's total budget, and whether it was long enough for running out
    # of it to say anything about the provider (see _SLOW_PROVIDER_SECONDS).
    budget_s: float | None = None
    full_budget: bool = True
    # The call's deadline (not its caller leaving) ended it.
    cut_off: bool = False

    def line(self, elapsed: float) -> str:
        def number(value: object) -> str:
            return "-" if value is None else str(value)

        budget = None if self.budget_s is None else format_deadline(self.budget_s)
        return (
            f"public-kimi gateway call path={self.family} status={self.status or '-'} "
            f"type={self.error_type} upstream_status={number(self.upstream_status)} "
            f"upstream_ms={number(self.upstream_elapsed_ms)} queue_ms={self.queue_ms} "
            f"ms={_ms(elapsed)} budget_s={number(budget)} bytes={self.response_bytes} "
            f"client={'connected' if self.client_connected else 'gone'}"
        )


def _provider_failure(status: int) -> bool:
    return status >= 500 or 300 <= status < 400 or status in _PROVIDER_FAILURE_STATUSES


class _UpstreamHealth:
    """Recent upstream outcomes for /healthz; it never calls the provider."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (finished at, failed, fast refusal)
        self._events: deque[tuple[float, bool, bool]] = deque(maxlen=_HEALTH_MAX_EVENTS)
        self._last_success: float | None = None

    def record(
        self,
        *,
        success: bool,
        failed: bool,
        fast_refusal: bool,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._events.append((now, failed, fast_refusal))
            if success:
                self._last_success = now

    def record_call(self, log: _CallLog) -> None:
        if log.error_type == "client_gone" or (log.cut_off and not log.full_budget):
            # Neither a caller that left nor its own shorter budget running
            # out says anything about the provider.
            return
        status = log.upstream_status
        rejected = log.error_type == "upstream_rejected" and status is not None
        failed = log.error_type in {"upstream_timeout", "upstream_unavailable"} or (
            rejected and _provider_failure(status)
        )
        fast = (
            rejected
            and status in {429, 503}
            and log.upstream_elapsed_ms is not None
            and log.upstream_elapsed_ms <= _FAST_REFUSAL_MS
        )
        self.record(success=log.error_type == "ok", failed=failed, fast_refusal=fast)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._events and now - self._events[0][0] > _HEALTH_WINDOW_SECONDS:
                self._events.popleft()
            calls = len(self._events)
            failures = sum(1 for _at, failed, _fast in self._events if failed)
            latest = self._events[-1] if self._events else None
            last_success = self._last_success
        # Conservative: one failure is noise.  A provider that keeps failing,
        # or that has just refused a call at once, is worth a warning.
        degraded = (failures >= 2 and failures * 2 >= calls) or (
            latest is not None
            and latest[2]
            and now - latest[0] <= _FAST_REFUSAL_RECENT_SECONDS
        )
        return {
            "recent_calls": calls,
            "recent_failures": failures,
            "last_success_age_s": (
                None if last_success is None else round(now - last_success, 1)
            ),
            "degraded": bool(degraded),
        }


class _GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        *,
        upstream_base: str,
        upstream_api_key: str,
        local_token: str,
        reasoning_effort: str,
        max_output_tokens: int,
        max_concurrency: int,
        calls_per_hour: int,
        upstream_timeout: float,
        queue_seconds: float = 0.0,
        queue_depth: int = 0,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _GatewayHandler)
        self.upstream_base = upstream_base.rstrip("/")
        self.upstream_authorization = "Bearer " + upstream_api_key
        self.expected_authorization = "Bearer " + local_token
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.upstream_timeout = upstream_timeout
        self.capacity = _FairCapacity(max_concurrency)
        # A queued call always keeps at least _MIN_UPSTREAM_SECONDS (or the
        # whole budget, if smaller) for the upstream request itself.
        self.queue_seconds = max(
            0.0,
            min(float(queue_seconds), upstream_timeout - _MIN_UPSTREAM_SECONDS),
        )
        self.queue_depth = queue_depth if self.queue_seconds > 0 else 0
        self.calls_per_hour = calls_per_hour
        self.call_times: deque[float] = deque()
        self.call_lock = threading.Lock()
        self.watchdog = DeadlineWatchdog()
        self.health = _UpstreamHealth()

    def admit_call(self) -> tuple[bool, int]:
        now = time.monotonic()
        with self.call_lock:
            while self.call_times and now - self.call_times[0] >= 3600:
                self.call_times.popleft()
            if len(self.call_times) >= self.calls_per_hour:
                retry = max(1, int(3600 - (now - self.call_times[0])))
                return False, retry
            self.call_times.append(now)
        return True, 0

    def call_budget(self, requested: object) -> float:
        """One call's total seconds: the cap, or the caller's shorter budget."""
        seconds = deadline_seconds(requested)
        budget = self.upstream_timeout if seconds is None else min(self.upstream_timeout, seconds)
        return max(1.0, budget)

    def full_budget(self, budget: float) -> bool:
        """Whether running out of ``budget`` would show a slow provider."""
        return budget >= min(self.upstream_timeout, _SLOW_PROVIDER_SECONDS)


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except CLIENT_GONE_ERRORS:
            # The caller hung up before it was answered: nothing left to send,
            # and nothing worth a traceback.
            self.close_connection = True
            logger.info("public-kimi gateway: client connection lost")

    @property
    def gateway(self) -> _GatewayServer:
        return self.server  # type: ignore[return-value]

    def _send(self, status: int, headers: list[tuple[str, str]], body: bytes) -> bool:
        """Write one whole response; False when the caller already hung up."""
        try:
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except CLIENT_GONE_ERRORS:
            self.close_connection = True
            return False
        return True

    def _json(self, status: int, payload: Mapping[str, Any], **headers: str) -> bool:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        pairs = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        pairs.extend((key.replace("_", "-"), value) for key, value in headers.items())
        return self._send(status, pairs, body)

    def _fail(
        self,
        log: _CallLog,
        status: int,
        message: str,
        error_type: str,
        *,
        forwarded: bool = False,
        retry_after: int | None = None,
    ) -> None:
        """Answer a model call with a typed error and note it in ``log``."""
        log.status = status
        log.error_type = error_type
        body = error_body(
            message,
            error_type,
            forwarded=forwarded,
            upstream_status=log.upstream_status,
            upstream_elapsed_ms=log.upstream_elapsed_ms,
            retry_after=retry_after,
        )
        headers = {"Retry_After": str(retry_after)} if retry_after is not None else {}
        log.client_connected = self._json(status, body, **headers)

    def _caller_left(self, log: _CallLog) -> None:
        """The caller hung up before it was answered: nobody is left to tell."""
        self.close_connection = True
        log.status, log.error_type, log.client_connected = 0, "client_gone", False

    def _authorized(self) -> bool:
        supplied = str(self.headers.get("Authorization") or "").strip()
        return hmac.compare_digest(supplied, self.gateway.expected_authorization)

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def do_GET(self) -> None:  # noqa: N802
        self._json(405, error_body("method not allowed", "bad_request"))

    def do_POST(self) -> None:  # noqa: N802
        arrived = time.monotonic()
        path = self._path()
        if path not in {_CHAT_PATH, _RESPONSES_PATH}:
            self._json(404, error_body("unsupported gateway path", "not_found"))
            return
        log = _CallLog(family="responses" if path == _RESPONSES_PATH else "chat")
        try:
            self._model_call(path, arrived, log)
        finally:
            logger.info(log.line(time.monotonic() - arrived))

    def _model_call(self, path: str, arrived: float, log: _CallLog) -> None:
        if not self._authorized():
            self._fail(log, 401, "authorization required", "unauthorized")
            return
        media_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json":
            self._fail(log, 415, "application/json required", "bad_request")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 1 or length > _MAX_REQUEST_BYTES:
            self._fail(
                log,
                413 if length > _MAX_REQUEST_BYTES else 400,
                "invalid request size",
                "bad_request",
            )
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            if path == _RESPONSES_PATH:
                sanitized = _sanitize_responses_gateway_payload(
                    payload,
                    reasoning_effort=self.gateway.reasoning_effort,
                    max_output_tokens=self.gateway.max_output_tokens,
                )
            else:
                sanitized = _sanitize_payload(
                    payload,
                    reasoning_effort=self.gateway.reasoning_effort,
                    max_output_tokens=self.gateway.max_output_tokens,
                )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._fail(log, 400, str(exc), "bad_request")
            return
        budget = self.gateway.call_budget(self.headers.get(DEADLINE_HEADER))
        log.budget_s, log.full_budget = budget, self.gateway.full_budget(budget)
        deadline = arrived + budget
        # Queue only while the call would still keep _MIN_UPSTREAM_SECONDS of
        # its own budget for the provider; a shorter budget is refused at once.
        queued = time.monotonic()
        acquired = self.gateway.capacity.acquire(
            timeout=max(
                0.0,
                min(self.gateway.queue_seconds, deadline - queued - _MIN_UPSTREAM_SECONDS),
            ),
            max_waiting=self.gateway.queue_depth,
        )
        log.queue_ms = _ms(time.monotonic() - queued)
        if not acquired:
            # Never forwarded, so the caller may safely try again later.
            self._fail(
                log, 429, "public Kimi call concurrency reached", "gateway_busy", retry_after=2
            )
            return
        try:
            if _hung_up(self.connection):
                # The caller left while it waited for a slot: never forwarded.
                self._caller_left(log)
                return
            admitted, retry_after = self.gateway.admit_call()
            if not admitted:
                self._fail(
                    log,
                    429,
                    "public Kimi call quota reached",
                    "gateway_quota",
                    retry_after=retry_after,
                )
                return
            self._forward(path, sanitized, deadline=deadline, log=log)
        finally:
            self.gateway.capacity.release()

    def _forward(
        self, path: str, payload: Mapping[str, Any], *, deadline: float, log: _CallLog
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = urlrequest.Request(
            self.gateway.upstream_base + path.removeprefix("/v1"),
            data=body,
            headers={
                "Authorization": self.gateway.upstream_authorization,
                "Content-Type": "application/json",
                "Accept": str(self.headers.get("Accept") or "application/json"),
                "User-Agent": "ProvingConsole-Public-Kimi/1",
            },
            method="POST",
        )
        # The provider always gets at least a second, as before the deadline.
        # The call also ends as soon as its caller hangs up.
        call = WatchedCall(max(deadline, time.monotonic() + 1.0), caller=self.connection)
        self.gateway.watchdog.watch(call)
        try:
            self._exchange(request, payload, call, log)
        finally:
            call.finish()
            self.gateway.health.record_call(log)

    def _exchange(
        self,
        request: urlrequest.Request,
        payload: Mapping[str, Any],
        call: WatchedCall,
        log: _CallLog,
    ) -> None:
        try:
            upstream = open_watched(_UPSTREAM_OPENER, request, call)
        except urlerror.HTTPError as exc:
            upstream = exc
        except (OSError, urlerror.URLError, http.client.HTTPException) as exc:
            cut = call.finish()
            if call.abandoned:
                self._caller_left(log)
                return
            log.cut_off = cut or isinstance(exc, TimeoutError) or isinstance(
                getattr(exc, "reason", None), TimeoutError
            )
            if not call.sent:
                # DNS, connect, TLS or send failed: the provider never saw it.
                self._fail(log, 502, "Kimi upstream unavailable", "upstream_unavailable")
            elif log.cut_off:
                self._fail(
                    log, 504, "Kimi upstream timed out", "upstream_timeout", forwarded=True
                )
            else:
                self._fail(
                    log, 502, "Kimi upstream unavailable", "upstream_unavailable", forwarded=True
                )
            return
        call.status_received()
        with upstream:
            status = int(getattr(upstream, "status", upstream.code))
            log.upstream_status = status
            log.upstream_elapsed_ms = call.elapsed_ms()
            if 300 <= status < 400:
                # A redirect was not followed (the key stays with the
                # configured upstream); report it as a failed upstream call.
                self._fail(
                    log, 502, "Kimi request was rejected", "upstream_rejected", forwarded=True
                )
                return
            if status < 200 or status >= 300:
                retry_after = str(upstream.headers.get("Retry-After") or "").strip()
                # Provider error bodies can contain account, billing, or echoed
                # request details.  Preserve only the status, its timing and a
                # safe retry hint.
                self._fail(
                    log,
                    status,
                    "Kimi request was rejected",
                    "upstream_rejected",
                    forwarded=True,
                    retry_after=(
                        min(int(retry_after), 3600)
                        if retry_after.isascii()
                        and retry_after.isdigit()
                        and len(retry_after) <= 6
                        else None
                    ),
                )
                return
            if payload.get("stream") is True:
                self._relay_stream(upstream, status, call, log)
                return
            timed_out = False
            try:
                response_body: bytes | None = upstream.read(_MAX_RESPONSE_BYTES + 1)
            except TimeoutError:
                # A socket timeout can only fire at the deadline itself.
                response_body, timed_out = None, True
            except (OSError, http.client.HTTPException):
                response_body = None
            cut = call.finish()
            if call.abandoned:
                self._caller_left(log)
                return
            if cut or timed_out:
                log.cut_off = True
                self._fail(
                    log, 504, "Kimi upstream timed out", "upstream_timeout", forwarded=True
                )
                return
            if response_body is not None and len(response_body) > _MAX_RESPONSE_BYTES:
                self._fail(
                    log, 502, "Kimi response too large", "response_too_large", forwarded=True
                )
                return
            if response_body is None or response_incomplete(upstream):
                # The provider hung up in the middle of its answer.
                self._fail(
                    log, 502, "Kimi upstream unavailable", "upstream_unavailable", forwarded=True
                )
                return
            headers = [
                (key, str(value))
                for key in ("Content-Type", "Cache-Control", "X-Request-ID")
                if (value := upstream.headers.get(key))
            ]
            headers.append(("Content-Length", str(len(response_body))))
            headers.append(("X-Content-Type-Options", "nosniff"))
            log.status, log.error_type = status, "ok"
            log.response_bytes = len(response_body)
            log.client_connected = self._send(status, headers, response_body)

    def _relay_stream(
        self, upstream: Any, status: int, call: WatchedCall, log: _CallLog
    ) -> None:
        """Pass an event stream through until EOF, the size cap or the deadline."""
        headers = [
            (key, str(value))
            for key in ("Content-Type", "Cache-Control", "X-Request-ID")
            if (value := upstream.headers.get(key))
        ]
        headers.append(("Connection", "close"))
        headers.append(("X-Content-Type-Options", "nosniff"))
        self.close_connection = True
        log.status, log.error_type = status, "ok"
        log.client_connected = self._send(status, headers, b"")
        read = getattr(upstream, "read1", None)
        if not callable(read):
            read = upstream.read
        total = 0
        timed_out = False
        while log.client_connected:
            try:
                chunk = read(16 * 1024)
            except TimeoutError:
                timed_out = True
                break
            except (OSError, http.client.HTTPException):
                log.error_type = "upstream_unavailable"
                break
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_STREAM_RESPONSE_BYTES:
                log.error_type = "response_too_large"
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except CLIENT_GONE_ERRORS:
                log.client_connected = False
                break
            log.response_bytes = total
        cut = call.finish()
        # The headers are out already, so the caller just sees the stream end.
        if call.abandoned or not log.client_connected:
            log.error_type, log.client_connected = "client_gone", False
        elif cut or timed_out:
            log.error_type, log.cut_off = "upstream_timeout", True

    def do_PUT(self) -> None:  # noqa: N802
        self._json(405, error_body("method not allowed", "bad_request"))

    do_DELETE = do_PUT
    do_PATCH = do_PUT


class PublicKimiGateway:
    """Lifecycle for the local-token-to-upstream-key credential boundary."""

    def __init__(
        self,
        *,
        upstream_base: str,
        upstream_api_key: str,
        local_token: str,
        reasoning_effort: str = "max",
        max_output_tokens: int = 8192,
        max_concurrency: int = 4,
        calls_per_hour: int = 180,
        upstream_timeout: float = 900,
        queue_seconds: float = 0,
        queue_depth: int = 0,
    ) -> None:
        self._server = _GatewayServer(
            upstream_base=upstream_base,
            upstream_api_key=upstream_api_key,
            local_token=local_token,
            reasoning_effort=reasoning_effort,
            max_output_tokens=_bounded_positive(
                max_output_tokens, default=8192, low=1024, high=16384
            ),
            max_concurrency=_bounded_positive(
                max_concurrency, default=4, low=1, high=16
            ),
            calls_per_hour=_bounded_positive(
                calls_per_hour, default=180, low=1, high=2000
            ),
            upstream_timeout=max(1.0, min(float(upstream_timeout), _MAX_UPSTREAM_SECONDS)),
            queue_seconds=_bounded_positive(
                queue_seconds, default=0, low=0, high=_MAX_QUEUE_SECONDS
            ),
            queue_depth=_bounded_positive(
                queue_depth, default=0, low=0, high=_MAX_QUEUE_DEPTH
            ),
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="public-kimi-credential-gateway",
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def upstream_health(self) -> dict[str, Any]:
        """Recent provider outcomes for /healthz (never calls the provider)."""
        return self._server.health.snapshot()

    def __enter__(self) -> "PublicKimiGateway":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        # Cut off calls still in flight so their threads end promptly.
        self._server.watchdog.close()
        self._thread.join(timeout=5)
