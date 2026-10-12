"""Total deadlines, typed errors and passive health on the sponsored Kimi path.

Every test talks to loopback stand-ins for the provider (and, for some broker
tests, for the gateway); nothing here contacts a real provider or uses a real
key.  Budgets are a second or two so the deadline paths run quickly.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib import request as urlrequest

from agent_monitor import public_kimi_gateway as gateway_module
from agent_monitor.public_kimi_gateway import (
    DEADLINE_HEADER,
    PublicKimiGateway,
    _CallLog,
    _UpstreamHealth,
    deadline_seconds,
    format_deadline,
)
from agent_monitor.public_kimi_server import (
    PublicKimiApplication,
    PublicKimiHTTPServer,
    PublicLimits,
    gateway_wait_seconds,
)

TOKEN = "local-loopback-token"
SECRET = "real-upstream-secret-value"
PROMPT = "prompt-marker-7d1e"
ANSWER = {
    "choices": [
        {
            "message": {"role": "assistant", "content": "answer-marker-93b2"},
            "finish_reason": "stop",
        }
    ]
}
OPENSSL = shutil.which("openssl")
GATEWAY_LOG = "agent_monitor.public_kimi.gateway"
BROKER_LOG = "agent_monitor.public_kimi.broker"
CHAT = {"model": "kimi-k3", "messages": [{"role": "user", "content": PROMPT}]}


class _Stub(ThreadingHTTPServer):
    """A loopback provider (or gateway) whose behaviour each test picks."""

    daemon_threads = True

    def __init__(self, behaviour: str) -> None:
        super().__init__(("127.0.0.1", 0), _StubHandler)
        self.behaviour = behaviour
        self.stop = threading.Event()
        self.release = threading.Event()
        self.headers_seen: list[dict[str, str]] = []
        self.hung_up: list[float] = []
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> "_Stub":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.headers_seen.append(
            {key.lower(): value for key, value in self.headers.items()}
        )
        self.close_connection = True
        try:
            getattr(self, "_" + self.server.behaviour)()
        except OSError:
            pass  # the caller hung up, as the gateway does at its deadline

    def _json(self, status: int, payload: dict, **headers: str) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def _ok(self) -> None:
        self._json(200, ANSWER)

    def _slow_ok(self) -> None:
        self.server.stop.wait(0.6)
        self._json(200, ANSWER)

    def _slower_ok(self) -> None:
        self.server.stop.wait(1.6)
        self._json(200, ANSWER)

    def _hold(self) -> None:
        # Answers nothing until the caller hangs up, and notes when it did.
        self.connection.settimeout(30)
        try:
            self.connection.recv(1)
        finally:
            self.server.hung_up.append(time.monotonic())

    def _release_ok(self) -> None:
        self.server.release.wait(30)
        self._json(200, ANSWER)

    def _truncated(self) -> None:
        # A 2xx whose body stops short, then the connection closes.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "200")
        self.end_headers()
        self.wfile.write(b'{"choices":[{"message"')

    def _hang_up(self) -> None:
        # Closes without any answer at all.
        self.connection.shutdown(socket.SHUT_RDWR)

    def _fast_503(self) -> None:
        self._json(
            503,
            {"error": {"message": "overloaded; account secret-upstream-detail"}},
            Retry_After="7",
        )

    def _silent(self) -> None:
        self.server.stop.wait(30)

    def _trickle(self) -> None:
        # Headers at once, then one keep-alive byte at a time: urllib's
        # per-operation timeout never fires on this.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        while not self.server.stop.wait(0.25):
            self.wfile.write(b"1\r\n \r\n")
            self.wfile.flush()

    def _sse_trickle(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        while not self.server.stop.wait(0.2):
            self.wfile.write(b"data: {}\n\n")
            self.wfile.flush()


@contextmanager
def _tls_stub(behaviour: str):
    """A provider stand-in behind real TLS, trusted only by a patched opener.

    Production talks HTTPS, where the watchdog must shut an SSLSocket down
    under a reader blocked inside the TLS layer.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
        subprocess.run(
            [
                OPENSSL, "req", "-x509", "-newkey", "ec",
                "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                "-days", "1", "-keyout", str(key), "-out", str(cert),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        stub = _Stub(behaviour)
        stub.socket = server_context.wrap_socket(stub.socket, server_side=True)
        opener = urlrequest.build_opener(
            gateway_module._NoRedirect(),
            gateway_module._WatchedHTTPHandler(),
            gateway_module._WatchedHTTPSHandler(
                context=ssl.create_default_context(cafile=str(cert))
            ),
        )
        with stub, patch.object(gateway_module, "_UPSTREAM_OPENER", opener):
            yield f"https://127.0.0.1:{stub.port}/v1"


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def _post(
    port: int,
    path: str,
    body: dict,
    *,
    token: str = TOKEN,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
) -> tuple[int, dict[str, str], bytes, float]:
    started = time.monotonic()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(body),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        response = connection.getresponse()
        data = response.read()
        return (
            response.status,
            {key.lower(): value for key, value in response.getheaders()},
            data,
            time.monotonic() - started,
        )
    finally:
        connection.close()


def _gateway_port(gateway: PublicKimiGateway) -> int:
    return int(gateway.base_url.removesuffix("/v1").rsplit(":", 1)[1])


def _gateway(base: str, **options) -> PublicKimiGateway:
    settings = {
        "upstream_api_key": SECRET,
        "local_token": TOKEN,
        "max_concurrency": 2,
        "calls_per_hour": 100,
        "upstream_timeout": 2,
    }
    settings.update(options)
    return PublicKimiGateway(upstream_base=base, **settings)


def _error(body: bytes) -> dict:
    value = json.loads(body)
    error = value["error"]
    assert set(error) == {
        "message",
        "type",
        "forwarded",
        "upstream_status",
        "upstream_elapsed_ms",
        "retry_after",
    }, error
    return error


@contextmanager
def _tracebacks(server):
    """Record every exception a server would print as a traceback."""
    seen: list[type] = []
    with patch.object(
        server,
        "handle_error",
        side_effect=lambda _request, _address: seen.append(sys.exc_info()[0]),
    ):
        yield seen


@contextmanager
def _no_proxy():
    with patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
        yield


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _health_after(gateway: PublicKimiGateway, calls: int) -> dict:
    """The health snapshot once ``calls`` outcomes are in (recorded after replying)."""
    _wait_for(lambda: gateway.upstream_health()["recent_calls"] >= calls)
    return gateway.upstream_health()


def _rst_after_request(
    port: int, path: str, body: dict, *, forwarded_to: _Stub, token: str = TOKEN
) -> None:
    """Send one request, then vanish with a reset, like a timed-out client.

    The reset comes only once the request was passed on to ``forwarded_to``,
    so the server under test is waiting for that hop when its caller leaves.
    """
    encoded = json.dumps(body).encode()
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    raw.sendall(
        (
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(encoded)}\r\n\r\n"
        ).encode()
        + encoded
    )
    assert _wait_for(lambda: bool(forwarded_to.headers_seen))
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    raw.close()


class _FakeJobs:
    def list_jobs(self):
        return []

    def delete_run(self, _run_id):
        return None


@contextmanager
def _broker(gateway_base_url: str, *, limits: PublicLimits | None = None, **server_options):
    with tempfile.TemporaryDirectory() as tmp:
        app = PublicKimiApplication(
            jobs=_FakeJobs(),
            data_dir=Path(tmp),
            redactions=(SECRET,),
            limits=limits or PublicLimits(),
            engine_catalog=lambda: [{"id": "plain", "available": True}],
        )
        server = PublicKimiHTTPServer(
            ("127.0.0.1", 0),
            app,
            "",
            gateway_base_url=gateway_base_url,
            gateway_token=TOKEN,
            **server_options,
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            yield server, app
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _sponsor(app: PublicKimiApplication, engine: str = "plain") -> str:
    return app.sponsor_sessions.issue(engine=engine, ip="198.18.0.7")[0]


class GatewayTotalDeadlineTests(unittest.TestCase):
    def test_trickling_provider_is_cut_off_at_the_total_deadline(self) -> None:
        with (
            _no_proxy(),
            _Stub("trickle") as provider,
            _gateway(provider.base, upstream_timeout=2) as gateway,
            _tracebacks(gateway._server) as tracebacks,
            self.assertLogs(GATEWAY_LOG, "INFO") as logs,
        ):
            status, headers, body, elapsed = _post(
                _gateway_port(gateway), "/v1/chat/completions", CHAT
            )
            self.assertTrue(_wait_for(lambda: any("call path=chat" in line for line in logs.output)))
        self.assertEqual(status, 504, body)
        error = _error(body)
        self.assertEqual(error["type"], "upstream_timeout")
        self.assertTrue(error["forwarded"])
        # The status line came at once; the body never finished.
        self.assertEqual(error["upstream_status"], 200)
        self.assertLess(error["upstream_elapsed_ms"], 1500)
        self.assertIsNone(error["retry_after"])
        self.assertNotIn("retry-after", headers)
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 4.5)
        self.assertEqual(tracebacks, [])
        line = next(line for line in logs.output if "call path=chat" in line)
        self.assertIn("status=504 type=upstream_timeout upstream_status=200", line)
        self.assertIn("client=connected", line)
        # The watchdog thread ends with the gateway: nothing leaks.
        self.assertTrue(
            _wait_for(
                lambda: not [
                    thread
                    for thread in threading.enumerate()
                    if thread.name == "public-kimi-upstream-deadline"
                    and thread.is_alive()
                ]
            )
        )

    def test_broker_relays_the_typed_gateway_timeout_unchanged(self) -> None:
        with (
            _no_proxy(),
            _Stub("trickle") as provider,
            _gateway(provider.base, upstream_timeout=2) as gateway,
            _broker(gateway.base_url, limits=PublicLimits(upstream_timeout_seconds=2)) as (
                broker,
                app,
            ),
            _tracebacks(broker) as broker_tracebacks,
            _tracebacks(gateway._server) as gateway_tracebacks,
        ):
            status, _headers, body, elapsed = _post(
                int(broker.server_address[1]),
                "/internal/v1/chat/completions",
                CHAT,
                token=_sponsor(app),
            )
        self.assertEqual(status, 504, body)
        error = _error(body)
        self.assertEqual(error["type"], "upstream_timeout")
        self.assertEqual(error["message"], "Kimi upstream timed out")
        self.assertTrue(error["forwarded"])
        self.assertLess(elapsed, 4.5)
        self.assertEqual((broker_tracebacks, gateway_tracebacks), ([], []))

    def test_silent_provider_times_out_waiting_for_the_status_line(self) -> None:
        with (
            _no_proxy(),
            _Stub("silent") as provider,
            _gateway(provider.base, upstream_timeout=1.5) as gateway,
        ):
            status, _headers, body, elapsed = _post(
                _gateway_port(gateway), "/v1/chat/completions", CHAT
            )
            health = _health_after(gateway, 1)
        self.assertEqual(status, 504, body)
        error = _error(body)
        self.assertEqual(error["type"], "upstream_timeout")
        self.assertTrue(error["forwarded"])
        self.assertIsNone(error["upstream_status"])
        self.assertIsNone(error["upstream_elapsed_ms"])
        self.assertGreaterEqual(elapsed, 1.3)
        self.assertLess(elapsed, 4.0)
        self.assertEqual((health["recent_calls"], health["recent_failures"]), (1, 1))
        self.assertIsNone(health["last_success_age_s"])

    def test_stream_keeps_flowing_but_stops_at_the_total_deadline(self) -> None:
        with (
            _no_proxy(),
            _Stub("sse_trickle") as provider,
            _gateway(provider.base, upstream_timeout=1.5) as gateway,
            self.assertLogs(GATEWAY_LOG, "INFO") as logs,
        ):
            started = time.monotonic()
            connection = http.client.HTTPConnection(
                "127.0.0.1", _gateway_port(gateway), timeout=10
            )
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=json.dumps({**CHAT, "stream": True}),
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            streamed = response.read()
            elapsed = time.monotonic() - started
            connection.close()
            self.assertTrue(_wait_for(lambda: any("call path=chat" in line for line in logs.output)))
        self.assertEqual(response.status, 200)
        self.assertIn(b"data: {}", streamed)
        self.assertGreaterEqual(elapsed, 1.3)
        self.assertLess(elapsed, 4.0)
        line = next(line for line in logs.output if "call path=chat" in line)
        self.assertIn("type=upstream_timeout", line)


@unittest.skipUnless(OPENSSL, "openssl is unavailable")
class GatewayTlsDeadlineTests(unittest.TestCase):
    def test_tls_provider_answers_through_the_watched_connection(self) -> None:
        with _no_proxy(), _tls_stub("ok") as base, _gateway(base) as gateway:
            status, _headers, body, _elapsed = _post(
                _gateway_port(gateway), "/v1/chat/completions", CHAT
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), ANSWER)

    def test_tls_trickle_and_silence_end_at_the_total_deadline(self) -> None:
        for behaviour, upstream_status in (("trickle", 200), ("silent", None)):
            with (
                self.subTest(behaviour=behaviour),
                _no_proxy(),
                _tls_stub(behaviour) as base,
                _gateway(base, upstream_timeout=1.5) as gateway,
                _tracebacks(gateway._server) as tracebacks,
            ):
                status, _headers, body, elapsed = _post(
                    _gateway_port(gateway), "/v1/chat/completions", CHAT
                )
            self.assertEqual(status, 504, body)
            error = _error(body)
            self.assertEqual(
                (error["type"], error["forwarded"], error["upstream_status"]),
                ("upstream_timeout", True, upstream_status),
            )
            self.assertGreaterEqual(elapsed, 1.3)
            self.assertLess(elapsed, 4.0)
            self.assertEqual(tracebacks, [])


class GatewayTypedErrorTests(unittest.TestCase):
    def test_fast_refusal_reports_status_timing_and_retry_hint_but_no_body(self) -> None:
        with (
            _no_proxy(),
            _Stub("fast_503") as provider,
            _gateway(provider.base) as gateway,
        ):
            status, headers, body, _elapsed = _post(
                _gateway_port(gateway), "/v1/chat/completions", CHAT
            )
            health = _health_after(gateway, 1)
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("retry-after"), "7")
        error = _error(body)
        self.assertEqual(
            {key: error[key] for key in ("message", "type", "forwarded", "upstream_status", "retry_after")},
            {
                "message": "Kimi request was rejected",
                "type": "upstream_rejected",
                "forwarded": True,
                "upstream_status": 503,
                "retry_after": 7,
            },
        )
        self.assertIsInstance(error["upstream_elapsed_ms"], int)
        self.assertLessEqual(error["upstream_elapsed_ms"], 2000)
        self.assertNotIn(b"secret-upstream-detail", body)
        self.assertNotIn(SECRET.encode(), body)
        # One fast refusal right now is enough to warn before a run starts.
        self.assertTrue(health["degraded"])
        self.assertEqual((health["recent_calls"], health["recent_failures"]), (1, 1))

    def test_connection_refused_was_never_forwarded(self) -> None:
        with _no_proxy(), _gateway(f"http://127.0.0.1:{_closed_port()}/v1") as gateway:
            status, _headers, body, _elapsed = _post(
                _gateway_port(gateway), "/v1/chat/completions", CHAT
            )
            health = _health_after(gateway, 1)
        self.assertEqual(status, 502)
        error = _error(body)
        self.assertEqual(error["type"], "upstream_unavailable")
        self.assertEqual(error["message"], "Kimi upstream unavailable")
        self.assertFalse(error["forwarded"])
        self.assertIsNone(error["upstream_status"])
        self.assertEqual(health["recent_failures"], 1)

    def test_hourly_cap_and_validation_errors_are_typed_and_not_forwarded(self) -> None:
        with (
            _no_proxy(),
            _Stub("ok") as provider,
            _gateway(provider.base, calls_per_hour=1) as gateway,
        ):
            port = _gateway_port(gateway)
            first = _post(port, "/v1/chat/completions", CHAT)
            capped = _post(port, "/v1/chat/completions", CHAT)
            invalid = _post(port, "/v1/chat/completions", {"model": "kimi-k3", "messages": []})
            unauthorized = _post(port, "/v1/chat/completions", CHAT, token="wrong-token")
            health = _health_after(gateway, 1)
        self.assertEqual(first[0], 200)
        self.assertEqual(json.loads(first[2]), ANSWER)
        self.assertEqual(capped[0], 429)
        error = _error(capped[2])
        self.assertEqual(error["type"], "gateway_quota")
        self.assertEqual(error["message"], "public Kimi call quota reached")
        self.assertFalse(error["forwarded"])
        self.assertIsInstance(error["retry_after"], int)
        self.assertGreater(error["retry_after"], 0)
        self.assertEqual(capped[1].get("retry-after"), str(error["retry_after"]))
        self.assertEqual((invalid[0], _error(invalid[2])["type"]), (400, "bad_request"))
        self.assertEqual(
            (unauthorized[0], _error(unauthorized[2])["type"]), (401, "unauthorized")
        )
        self.assertFalse(_error(unauthorized[2])["forwarded"])
        # Only the call that reached the provider counts towards its health.
        self.assertEqual(health["recent_calls"], 1)
        self.assertIsNotNone(health["last_success_age_s"])
        self.assertFalse(health["degraded"])

    def test_log_lines_are_content_free(self) -> None:
        with (
            _no_proxy(),
            _Stub("ok") as provider,
            _gateway(provider.base) as gateway,
            _broker(gateway.base_url) as (broker, app),
            self.assertLogs("agent_monitor.public_kimi", "INFO") as logs,
        ):
            token = _sponsor(app)
            status, _headers, _body, _elapsed = _post(
                int(broker.server_address[1]),
                "/internal/v1/chat/completions",
                CHAT,
                token=token,
            )
            self.assertTrue(
                _wait_for(lambda: sum("call path=chat" in line for line in logs.output) >= 2)
            )
        self.assertEqual(status, 200)
        text = "\n".join(logs.output)
        for secret in (TOKEN, token, SECRET, PROMPT, "answer-marker-93b2", "198.18."):
            self.assertNotIn(secret, text)
        self.assertIn("public-kimi gateway call path=chat status=200 type=ok", text)
        self.assertIn("public-kimi broker call path=chat engine=plain status=200 type=ok", text)


class DeadlineHeaderTests(unittest.TestCase):
    def test_header_values_are_positive_decimal_seconds_of_any_precision(self) -> None:
        for raw, expected in (
            ("12", 12.0),
            (" 1.5 ", 1.5),
            ("630.25", 630.25),
            ("12.", 12.0),
            (".5", 0.5),
            # A remaining time as Python prints a float is still a deadline.
            (str(187.33299994468689), 187.33299994468689),
            ("612.3456789", 612.3456789),
            ("0.0000001", 0.0000001),
            ("12345678", 12345678.0),
            ("", None),
            (None, None),
            ("abc", None),
            ("nan", None),
            ("inf", None),
            ("-1", None),
            ("0", None),
            ("0.0", None),
            ("1e3", None),
            ("0x10", None),
            ("1_000", None),
            ("1.2.3", None),
            (".", None),
            ("1" * 65, None),
            ("9" * 400, None),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(deadline_seconds(raw), expected)
        self.assertEqual(format_deadline(610.0), "610")
        self.assertEqual(format_deadline(1.25), "1.25")
        self.assertEqual(format_deadline(-4), "1")
        # Passed on rounded down, never up.
        self.assertEqual(format_deadline(602.3456789), "602.345")

    def test_headers_only_shorten_each_hop_and_never_lengthen_it(self) -> None:
        limits = PublicLimits()
        self.assertEqual(gateway_wait_seconds(limits), 615.0)
        self.assertEqual(gateway_wait_seconds(limits, "630"), 615.0)
        self.assertEqual(gateway_wait_seconds(limits, "100000"), 615.0)
        self.assertEqual(gateway_wait_seconds(limits, "200"), 195.0)
        self.assertEqual(gateway_wait_seconds(limits, "garbage"), 615.0)
        self.assertEqual(gateway_wait_seconds(limits, "3"), 1.0)
        self.assertAlmostEqual(gateway_wait_seconds(limits, "612.3456789"), 607.3456789)
        self.assertAlmostEqual(
            gateway_wait_seconds(limits, str(187.33299994468689)), 182.33299994468689
        )
        gateway = _gateway("https://kimi.example/v1", upstream_timeout=600)
        try:
            budget = gateway._server.call_budget
            self.assertEqual(budget(None), 600.0)
            self.assertEqual(budget("610"), 600.0)
            self.assertEqual(budget("190"), 190.0)
            self.assertEqual(budget("190.123456789"), 190.123456789)
            self.assertEqual(budget("0.2"), 1.0)
            for garbage in ("nan", "inf", "-5", "0", "1e9", "soon"):
                self.assertEqual(budget(garbage), 600.0, garbage)
        finally:
            gateway._server.server_close()

    def test_broker_forwards_its_own_wait_minus_a_margin(self) -> None:
        with _Stub("ok") as fake_gateway, _no_proxy():
            with _broker(fake_gateway.base) as (broker, app):
                token = _sponsor(app)
                port = int(broker.server_address[1])
                for sent in (None, "100", "garbage", "100000", "3"):
                    headers = {DEADLINE_HEADER: sent} if sent is not None else {}
                    status, _headers, _body, _elapsed = _post(
                        port, "/internal/v1/chat/completions", CHAT, token=token, headers=headers
                    )
                    self.assertEqual(status, 200, sent)
        forwarded = [seen.get(DEADLINE_HEADER.lower()) for seen in fake_gateway.headers_seen]
        self.assertEqual(forwarded, ["610", "90", "610", "610", "1"])

    def test_a_shorter_caller_budget_shortens_the_gateway_deadline(self) -> None:
        with (
            _no_proxy(),
            _Stub("silent") as provider,
            _gateway(provider.base, upstream_timeout=30) as gateway,
        ):
            port = _gateway_port(gateway)
            short = _post(port, "/v1/chat/completions", CHAT, headers={DEADLINE_HEADER: "1.2"})
        self.assertEqual(short[0], 504)
        self.assertEqual(_error(short[2])["type"], "upstream_timeout")
        self.assertGreaterEqual(short[3], 1.0)
        self.assertLess(short[3], 3.5)

    def test_a_float_formatted_budget_still_shortens_every_hop(self) -> None:
        with (
            _no_proxy(),
            _Stub("silent") as provider,
            _gateway(provider.base, upstream_timeout=60) as gateway,
            _broker(gateway.base_url, limits=PublicLimits(upstream_timeout_seconds=60)) as (
                broker,
                app,
            ),
            self.assertLogs("agent_monitor.public_kimi", "INFO") as logs,
        ):
            # Seven decimals: the broker waits 6.5 s and the gateway 1.5 s.
            status, _headers, body, elapsed = _post(
                int(broker.server_address[1]),
                "/internal/v1/chat/completions",
                CHAT,
                token=_sponsor(app),
                headers={DEADLINE_HEADER: "11.5000001"},
                timeout=11.5,
            )
            self.assertTrue(
                _wait_for(lambda: sum("call path=chat" in line for line in logs.output) >= 2)
            )
        self.assertEqual(status, 504, body)
        self.assertEqual(_error(body)["type"], "upstream_timeout")
        self.assertLess(elapsed, 4.0)
        self.assertIn("wait_s=6.5 ", _line(logs, "public-kimi broker call"))
        self.assertIn("budget_s=1.5 ", _line(logs, "public-kimi gateway call"))

    def test_garbage_or_longer_caller_budgets_keep_the_gateway_cap(self) -> None:
        with (
            _no_proxy(),
            _Stub("silent") as provider,
            _gateway(provider.base, upstream_timeout=1.5) as gateway,
        ):
            port = _gateway_port(gateway)
            longer = _post(port, "/v1/chat/completions", CHAT, headers={DEADLINE_HEADER: "50"})
            garbage = _post(port, "/v1/chat/completions", CHAT, headers={DEADLINE_HEADER: "nan"})
        for status, _headers, body, elapsed in (longer, garbage):
            self.assertEqual(status, 504)
            self.assertEqual(_error(body)["type"], "upstream_timeout")
            self.assertGreaterEqual(elapsed, 1.3)
            self.assertLess(elapsed, 4.0)


class BrokerTypedErrorTests(unittest.TestCase):
    def test_broker_only_errors_are_typed(self) -> None:
        with (
            _no_proxy(),
            _Stub("ok") as fake_gateway,
            _broker(fake_gateway.base, limits=PublicLimits(sponsor_calls_per_token=1)) as (
                broker,
                app,
            ),
        ):
            port = int(broker.server_address[1])
            token = _sponsor(app)
            first = _post(port, "/internal/v1/chat/completions", CHAT, token=token)
            quota = _post(port, "/internal/v1/chat/completions", CHAT, token=token)
            unauthorized = _post(port, "/internal/v1/chat/completions", CHAT, token="x" * 64)
            forbidden = _post(port, "/internal/v1/responses", {"input": "x"}, token=_sponsor(app))
        self.assertEqual(first[0], 200)
        self.assertEqual(quota[0], 429)
        self.assertEqual(quota[1].get("retry-after"), "60")
        error = _error(quota[2])
        self.assertEqual(
            (error["type"], error["forwarded"], error["retry_after"]),
            ("sponsor_quota", False, 60),
        )
        self.assertEqual(error["message"], "Sponsored Kimi call quota reached")
        self.assertEqual(
            (unauthorized[0], _error(unauthorized[2])["type"]), (401, "unauthorized")
        )
        self.assertEqual((forbidden[0], _error(forbidden[2])["type"]), (403, "forbidden"))
        self.assertFalse(_error(forbidden[2])["forwarded"])

    def test_unreachable_gateway_was_never_forwarded(self) -> None:
        with _no_proxy(), _broker(f"http://127.0.0.1:{_closed_port()}/v1") as (broker, app):
            status, _headers, body, _elapsed = _post(
                int(broker.server_address[1]),
                "/internal/v1/chat/completions",
                CHAT,
                token=_sponsor(app),
            )
        self.assertEqual(status, 502)
        error = _error(body)
        self.assertEqual(
            (error["type"], error["forwarded"], error["message"]),
            ("gateway_unavailable", False, "Sponsored Kimi gateway unavailable"),
        )

    def test_broker_that_gives_up_on_the_gateway_reports_an_ambiguous_timeout(self) -> None:
        with (
            _no_proxy(),
            _Stub("silent") as fake_gateway,
            _broker(fake_gateway.base) as (broker, app),
        ):
            # A 7-second caller budget leaves the broker 2 s for the gateway.
            status, _headers, body, elapsed = _post(
                int(broker.server_address[1]),
                "/internal/v1/chat/completions",
                CHAT,
                token=_sponsor(app),
                headers={DEADLINE_HEADER: "7"},
            )
        self.assertEqual(status, 504)
        error = _error(body)
        self.assertEqual((error["type"], error["forwarded"]), ("gateway_timeout", True))
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 5.0)


class DisconnectedClientTests(unittest.TestCase):
    def test_gateway_write_after_the_caller_left_logs_instead_of_a_traceback(self) -> None:
        with (
            _no_proxy(),
            _Stub("slow_ok") as provider,
            _gateway(provider.base) as gateway,
            _tracebacks(gateway._server) as tracebacks,
            self.assertLogs(GATEWAY_LOG, "INFO") as logs,
        ):
            _rst_after_request(
                _gateway_port(gateway), "/v1/chat/completions", CHAT, forwarded_to=provider
            )
            self.assertTrue(_wait_for(lambda: any("client=gone" in line for line in logs.output)))
        self.assertEqual(tracebacks, [])
        line = next(line for line in logs.output if "client=gone" in line)
        self.assertIn("status=200 type=ok", line)

    def test_broker_write_after_the_caller_left_logs_instead_of_a_traceback(self) -> None:
        with (
            _no_proxy(),
            _Stub("slow_ok") as fake_gateway,
            _broker(fake_gateway.base) as (broker, app),
            _tracebacks(broker) as tracebacks,
            self.assertLogs(BROKER_LOG, "INFO") as logs,
        ):
            _rst_after_request(
                int(broker.server_address[1]),
                "/internal/v1/chat/completions",
                CHAT,
                forwarded_to=fake_gateway,
                token=_sponsor(app),
            )
            self.assertTrue(_wait_for(lambda: any("client=gone" in line for line in logs.output)))
        self.assertEqual(tracebacks, [])


def _gave_up(port: int, path: str, body: dict, *, timeout: float, token: str = TOKEN) -> float:
    """Call without X-Ansatze-Timeout and give up after ``timeout`` seconds,
    like a client with a short timeout of its own; returns when it left."""
    try:
        _post(port, path, body, token=token, timeout=timeout)
    except TimeoutError:
        return time.monotonic()
    raise AssertionError("the call was answered before the caller gave up")


def _line(logs, *needles: str) -> str:
    return next(line for line in logs.output if all(needle in line for needle in needles))


class CallerGoneTests(unittest.TestCase):
    def test_gateway_stops_the_provider_call_once_its_caller_gives_up(self) -> None:
        with (
            _no_proxy(),
            _Stub("hold") as provider,
            _gateway(provider.base, upstream_timeout=30, max_concurrency=1) as gateway,
            _tracebacks(gateway._server) as tracebacks,
            self.assertLogs(GATEWAY_LOG, "INFO") as logs,
        ):
            port = _gateway_port(gateway)
            left = _gave_up(port, "/v1/chat/completions", CHAT, timeout=1.0)
            self.assertTrue(_wait_for(lambda: bool(provider.hung_up)))
            # The slot is free again: the next call reaches the provider (and
            # ends at its own short deadline) instead of finding it busy.
            status, _headers, body, _elapsed = _post(
                port, "/v1/chat/completions", CHAT, headers={DEADLINE_HEADER: "1"}
            )
            self.assertTrue(
                _wait_for(lambda: sum("call path=chat" in line for line in logs.output) >= 2)
            )
            health = gateway.upstream_health()
        self.assertLess(provider.hung_up[0] - left, 2.5)
        self.assertEqual((status, _error(body)["type"]), (504, "upstream_timeout"))
        line = _line(logs, "type=client_gone")
        self.assertIn("status=- type=client_gone", line)
        self.assertIn("client=gone", line)
        self.assertIn("budget_s=30 ", line)
        self.assertEqual(tracebacks, [])
        # Neither call says anything about the provider's health.
        self.assertEqual((health["recent_calls"], health["degraded"]), (0, False))

    def test_broker_and_gateway_stop_the_call_once_the_broker_caller_gives_up(self) -> None:
        with (
            _no_proxy(),
            _Stub("hold") as provider,
            _gateway(provider.base, upstream_timeout=30) as gateway,
            _broker(gateway.base_url, limits=PublicLimits(upstream_timeout_seconds=30)) as (
                broker,
                app,
            ),
            _tracebacks(broker) as broker_tracebacks,
            _tracebacks(gateway._server) as gateway_tracebacks,
            self.assertLogs("agent_monitor.public_kimi", "INFO") as logs,
        ):
            left = _gave_up(
                int(broker.server_address[1]),
                "/internal/v1/chat/completions",
                CHAT,
                timeout=1.0,
                token=_sponsor(app),
            )
            self.assertTrue(_wait_for(lambda: bool(provider.hung_up)))
            self.assertTrue(
                _wait_for(lambda: sum("type=client_gone" in line for line in logs.output) >= 2)
            )
        # About a second for the broker to notice, and one for the gateway.
        self.assertLess(provider.hung_up[0] - left, 3.5)
        broker_line = _line(logs, "public-kimi broker call", "type=client_gone")
        self.assertIn("status=- type=client_gone", broker_line)
        self.assertIn("wait_s=45 ", broker_line)
        self.assertIn("client=gone", broker_line)
        self.assertIn("client=gone", _line(logs, "public-kimi gateway call", "type=client_gone"))
        self.assertEqual((broker_tracebacks, gateway_tracebacks), ([], []))

    def test_a_caller_that_left_the_queue_is_never_forwarded(self) -> None:
        with (
            _no_proxy(),
            _Stub("release_ok") as provider,
            _gateway(
                provider.base,
                upstream_timeout=120,
                max_concurrency=1,
                queue_seconds=20,
                queue_depth=4,
            ) as gateway,
            self.assertLogs(GATEWAY_LOG, "INFO") as logs,
        ):
            port = _gateway_port(gateway)
            first: list = []
            holder = threading.Thread(
                target=lambda: first.append(_post(port, "/v1/chat/completions", CHAT))
            )
            holder.start()
            self.assertTrue(_wait_for(lambda: len(provider.headers_seen) == 1))
            encoded = json.dumps(CHAT).encode()
            queued = socket.create_connection(("127.0.0.1", port), timeout=5)
            queued.sendall(
                (
                    f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    f"Authorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(encoded)}\r\n\r\n"
                ).encode()
                + encoded
            )
            self.assertTrue(_wait_for(lambda: gateway._server.capacity.waiting == 1))
            queued.close()
            provider.release.set()
            holder.join(timeout=10)
            self.assertTrue(
                _wait_for(lambda: sum("call path=chat" in line for line in logs.output) >= 2)
            )
            admitted = len(gateway._server.call_times)
        self.assertEqual(first[0][0], 200)
        self.assertEqual(len(provider.headers_seen), 1)
        # It took no share of the hourly cap either.
        self.assertEqual(admitted, 1)
        self.assertIn("client=gone", _line(logs, "type=client_gone"))


class BrokerAfterSendTypeTests(unittest.TestCase):
    def test_a_gateway_answer_that_never_arrives_in_full_is_not_called_unavailable(self) -> None:
        # gateway_unavailable promises that nothing was forwarded; here the
        # gateway had the call, so the client must not be told no tokens
        # were used.
        for behaviour in ("truncated", "hang_up"):
            with (
                self.subTest(behaviour=behaviour),
                _no_proxy(),
                _Stub(behaviour) as fake_gateway,
                _broker(fake_gateway.base) as (broker, app),
                _tracebacks(broker) as tracebacks,
            ):
                status, headers, body, _elapsed = _post(
                    int(broker.server_address[1]),
                    "/internal/v1/chat/completions",
                    CHAT,
                    token=_sponsor(app),
                )
            self.assertEqual(status, 502, body)
            error = _error(body)
            self.assertEqual(
                (error["type"], error["forwarded"], error["message"]),
                ("gateway_connection_lost", True, "Sponsored Kimi gateway unavailable"),
            )
            self.assertNotIn("retry-after", headers)
            self.assertEqual(tracebacks, [])


@unittest.skipUnless(OPENSSL, "openssl is unavailable")
class GatewayHandshakeDeadlineTests(unittest.TestCase):
    def test_a_slow_connect_and_a_stalled_tls_handshake_end_at_the_deadline(self) -> None:
        # The listener never accepts, so the TLS handshake never gets an answer.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = int(listener.getsockname()[1])
        real_connect = socket.create_connection

        def slow_connect(address, *args, **kwargs):
            if address[1] == port:
                time.sleep(1.2)
            return real_connect(address, *args, **kwargs)

        def open_descriptors() -> int:
            return len(os.listdir("/proc/self/fd"))

        try:
            before = open_descriptors()
            with (
                _no_proxy(),
                patch.object(gateway_module.socket, "create_connection", slow_connect),
                _gateway(f"https://127.0.0.1:{port}/v1", upstream_timeout=2) as gateway,
                _tracebacks(gateway._server) as tracebacks,
            ):
                status, _headers, body, elapsed = _post(
                    _gateway_port(gateway), "/v1/chat/completions", CHAT
                )
        finally:
            listener.close()
        self.assertEqual(status, 502, body)
        error = _error(body)
        self.assertEqual((error["type"], error["forwarded"]), ("upstream_unavailable", False))
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 3.0)
        self.assertEqual(tracebacks, [])
        # Nothing the watchdog used during the handshake stays open.
        self.assertTrue(_wait_for(lambda: open_descriptors() <= before))


class HealthzExposureTests(unittest.TestCase):
    def test_upstream_health_is_only_for_local_callers_not_the_public_edge(self) -> None:
        reported = {
            "recent_calls": 2,
            "recent_failures": 0,
            "last_success_age_s": 3.0,
            "degraded": False,
        }
        with _broker("", upstream_health=lambda: reported) as (broker, _app):
            answers = []
            for headers in ({}, {"X-Public-Client-IP": "203.0.113.9"}):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", int(broker.server_address[1]), timeout=5
                )
                connection.request("GET", "/healthz", headers=headers)
                response = connection.getresponse()
                answers.append((response.status, json.loads(response.read())))
                connection.close()
        local, edge = answers
        self.assertEqual(local, (200, {"ok": True, "service": "public-kimi", "upstream": reported}))
        self.assertEqual(edge, (200, {"ok": True, "service": "public-kimi"}))


class UpstreamHealthTests(unittest.TestCase):
    @staticmethod
    def _log(
        error_type: str,
        status: int | None = None,
        elapsed: int | None = None,
        *,
        full_budget: bool = True,
        cut_off: bool = False,
    ) -> _CallLog:
        return _CallLog(
            family="chat",
            error_type=error_type,
            upstream_status=status,
            upstream_elapsed_ms=elapsed,
            full_budget=full_budget,
            cut_off=cut_off,
        )

    def test_quiet_provider_is_not_degraded(self) -> None:
        health = _UpstreamHealth()
        self.assertEqual(
            health.snapshot(now=100.0),
            {
                "recent_calls": 0,
                "recent_failures": 0,
                "last_success_age_s": None,
                "degraded": False,
            },
        )

    def test_degraded_needs_two_failures_and_at_least_half_of_recent_calls(self) -> None:
        health = _UpstreamHealth()
        health.record(success=True, failed=False, fast_refusal=False, now=0.0)
        health.record(success=False, failed=True, fast_refusal=False, now=1.0)
        self.assertFalse(health.snapshot(now=2.0)["degraded"])  # one failure is noise
        health.record(success=False, failed=True, fast_refusal=False, now=3.0)
        snapshot = health.snapshot(now=4.0)
        self.assertEqual((snapshot["recent_calls"], snapshot["recent_failures"]), (3, 2))
        self.assertTrue(snapshot["degraded"])
        for moment in (5.0, 6.0):
            health.record(success=True, failed=False, fast_refusal=False, now=moment)
        snapshot = health.snapshot(now=7.0)
        self.assertEqual((snapshot["recent_calls"], snapshot["recent_failures"]), (5, 2))
        self.assertFalse(snapshot["degraded"])  # 2 of 5 is under half
        self.assertEqual(snapshot["last_success_age_s"], 1.0)
        # Ten minutes later the window is empty again; the last success stays.
        later = health.snapshot(now=6.0 + 601)
        self.assertEqual((later["recent_calls"], later["recent_failures"]), (0, 0))
        self.assertFalse(later["degraded"])
        self.assertEqual(later["last_success_age_s"], 601.0)

    def test_a_fresh_fast_refusal_alone_is_degraded_for_two_minutes(self) -> None:
        health = _UpstreamHealth()
        for moment in range(5):
            health.record(success=True, failed=False, fast_refusal=False, now=float(moment))
        health.record(success=False, failed=True, fast_refusal=True, now=10.0)
        self.assertTrue(health.snapshot(now=11.0)["degraded"])
        self.assertTrue(health.snapshot(now=129.0)["degraded"])
        self.assertFalse(health.snapshot(now=131.0)["degraded"])
        # A success after the refusal clears it at once.
        health.record(success=True, failed=False, fast_refusal=False, now=12.0)
        self.assertFalse(health.snapshot(now=13.0)["degraded"])

    def test_call_outcomes_map_onto_failures_and_fast_refusals(self) -> None:
        cases = (
            (self._log("ok", 200, 900), (False, False)),
            (self._log("upstream_rejected", 503, 300), (True, True)),
            (self._log("upstream_rejected", 429, 1999), (True, True)),
            (self._log("upstream_rejected", 503, 2500), (True, False)),
            (self._log("upstream_rejected", 500, 100), (True, False)),
            # A revoked or unfunded key, or a wrong upstream base, fails
            # every sponsored call just the same.
            (self._log("upstream_rejected", 401, 100), (True, False)),
            (self._log("upstream_rejected", 402, 100), (True, False)),
            (self._log("upstream_rejected", 403, 100), (True, False)),
            (self._log("upstream_rejected", 404, 100), (True, False)),
            (self._log("upstream_rejected", 302, 100), (True, False)),
            # A request the provider found invalid says nothing about it.
            (self._log("upstream_rejected", 400, 100), (False, False)),
            (self._log("upstream_rejected", 413, 100), (False, False)),
            (self._log("upstream_timeout", 200, 100, cut_off=True), (True, False)),
            (self._log("upstream_timeout", cut_off=True), (True, False)),
            (self._log("upstream_unavailable"), (True, False)),
            (self._log("response_too_large", 200, 100), (False, False)),
        )
        for log, expected in cases:
            with self.subTest(log=log), patch.object(_UpstreamHealth, "record") as record:
                _UpstreamHealth().record_call(log)
            kwargs = record.call_args.kwargs
            self.assertEqual((kwargs["failed"], kwargs["fast_refusal"]), expected)
            self.assertEqual(kwargs["success"], log.error_type == "ok")

    def test_calls_the_caller_cut_short_say_nothing_about_the_provider(self) -> None:
        for log in (
            self._log("client_gone"),
            self._log("client_gone", 200, 300),
            # The caller's own shorter X-Ansatze-Timeout ran out.
            self._log("upstream_timeout", full_budget=False, cut_off=True),
            self._log("upstream_timeout", 200, 300, full_budget=False, cut_off=True),
            self._log("upstream_unavailable", full_budget=False, cut_off=True),
        ):
            with self.subTest(log=log), patch.object(_UpstreamHealth, "record") as record:
                _UpstreamHealth().record_call(log)
            record.assert_not_called()
        # A real transport failure still counts, whatever the budget.
        with patch.object(_UpstreamHealth, "record") as record:
            _UpstreamHealth().record_call(self._log("upstream_unavailable", full_budget=False))
        self.assertTrue(record.call_args.kwargs["failed"])

    def test_only_a_budget_near_the_cap_judges_a_slow_provider(self) -> None:
        for cap, budget, full in (
            (600, 600.0, True),
            (600, 590.0, False),
            (600, 190.0, False),
            (300, 300.0, True),
            (300, 299.0, False),
            # Sponsored clients cap a 900 s gateway at about 620 s.
            (900, 620.0, True),
            (900, 599.0, False),
        ):
            gateway = _gateway("https://kimi.example/v1", upstream_timeout=cap)
            try:
                with self.subTest(cap=cap, budget=budget):
                    self.assertIs(gateway._server.full_budget(budget), full)
            finally:
                gateway._server.server_close()

    def test_short_caller_budgets_cannot_mark_a_healthy_provider_degraded(self) -> None:
        with (
            _no_proxy(),
            _Stub("slower_ok") as provider,
            _gateway(provider.base, upstream_timeout=30) as gateway,
            self.assertLogs(GATEWAY_LOG, "INFO") as logs,
        ):
            port = _gateway_port(gateway)
            answered = _post(port, "/v1/chat/completions", CHAT)
            # Counted, two of three calls failing would mark it degraded.
            short = [
                _post(port, "/v1/chat/completions", CHAT, headers={DEADLINE_HEADER: "0.3"})
                for _ in range(2)
            ]
            self.assertTrue(
                _wait_for(lambda: sum("call path=chat" in line for line in logs.output) >= 3)
            )
            health = gateway.upstream_health()
        self.assertEqual(answered[0], 200)
        for status, _headers, body, _elapsed in short:
            self.assertEqual((status, _error(body)["type"]), (504, "upstream_timeout"))
        self.assertEqual(
            (health["recent_calls"], health["recent_failures"], health["degraded"]),
            (1, 0, False),
        )
        # Each line names the budget the call had, and how long it took.
        line = [line for line in logs.output if "type=upstream_timeout" in line][0]
        self.assertIn("budget_s=1 ", line)
        self.assertRegex(line, r" ms=\d+ ")

    def test_broker_healthz_keeps_liveness_keys_and_adds_passive_upstream_health(self) -> None:
        reported = {
            "recent_calls": 4,
            "recent_failures": 3,
            "last_success_age_s": 42.5,
            "degraded": True,
        }

        def broken() -> dict:
            raise RuntimeError("health unavailable")

        for source, expected in (
            (lambda: reported, reported),
            (
                None,
                {
                    "recent_calls": 0,
                    "recent_failures": 0,
                    "last_success_age_s": None,
                    "degraded": False,
                },
            ),
            (
                broken,
                {
                    "recent_calls": 0,
                    "recent_failures": 0,
                    "last_success_age_s": None,
                    "degraded": False,
                },
            ),
        ):
            with self.subTest(source=source), _broker("", upstream_health=source) as (broker, _app):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", int(broker.server_address[1]), timeout=5
                )
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                value = json.loads(response.read())
                connection.close()
            self.assertEqual(response.status, 200)
            self.assertIs(value["ok"], True)
            self.assertEqual(value["service"], "public-kimi")
            self.assertEqual(value["upstream"], expected)


class UpstreamTimeoutKnobTests(unittest.TestCase):
    def test_knob_default_floor_ceiling_and_invalid_values(self) -> None:
        self.assertEqual(PublicLimits.from_env({}).upstream_timeout_seconds, 600)
        variable = "PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS"
        for raw, expected in (("300", 300), ("10", 60), ("0", 60), ("5000", 900), (" 720 ", 720)):
            with self.subTest(raw=raw):
                self.assertEqual(
                    PublicLimits.from_env({variable: raw}).upstream_timeout_seconds, expected
                )
        for raw in ("12.5", "-1", "1e3", "ten"):
            with self.subTest(raw=raw), self.assertRaisesRegex(RuntimeError, variable):
                PublicLimits.from_env({variable: raw})

    def test_gateway_clamps_its_budget_and_the_queue_keeps_provider_time(self) -> None:
        gateway = _gateway("https://kimi.example/v1", upstream_timeout=10**6, queue_seconds=60)
        try:
            self.assertEqual(gateway._server.upstream_timeout, 900.0)
            self.assertEqual(gateway._server.queue_seconds, 60)
        finally:
            gateway._server.server_close()


if __name__ == "__main__":
    unittest.main()
