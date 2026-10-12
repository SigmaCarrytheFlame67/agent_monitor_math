"""Offline tests for the hardened link fetcher used by Analyze sources."""
from __future__ import annotations

import gzip
import io
import socket
import tarfile
import zipfile

import pytest

from agent_monitor import safe_fetch
from agent_monitor.safe_fetch import Budget, FetchError, SafeFetcher

PUBLIC_V4 = "93.184.216.34"
TOKEN = "ghp_" + "a" * 36


def public_dns(host, port, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_V4, port))]


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, size):
        data, self.body = self.body[:size], self.body[size:]
        return data


class Connections:
    """A connection factory that hands out scripted responses in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.connections = []

    def __call__(self, host, port, **kwargs):
        parent = self
        response = self.responses.pop(0)

        class Connection:
            sock = None

            def request(self, method, path, headers):
                parent.calls.append({"host": host, "port": port, "method": method,
                                     "path": path, "headers": dict(headers), **kwargs})

            def getresponse(self):
                return response

            def close(self):
                self.closed = True

        connection = Connection()
        self.connections.append(connection)
        return connection


def fetcher(*responses, resolver=public_dns, **kwargs):
    factory = Connections(*responses)
    return SafeFetcher(resolver=resolver, connection_factory=factory, **kwargs), factory


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/paper.pdf",
        "https://127.0.0.1/paper.pdf",
        "https://[::1]/paper.pdf",
        "https://0x7f.1/paper.pdf",
        "https://017.0.0.1/paper.pdf",
        "https://2130706433/paper.pdf",
        "https://localhost/paper.pdf",
        "https://printer.local/paper.pdf",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://example.org:8443/paper.pdf",
        "https://user:secret@example.org/paper.pdf",
        "https://example.org/a b",
        "ftp://example.org/paper.pdf",
        "",
    ],
)
def test_unsafe_urls_are_refused_before_any_network_use(url):
    calls = []

    def resolver(*args, **kwargs):
        calls.append(args)
        return public_dns(*args, **kwargs)

    fetch, factory = fetcher(FakeResponse(body=b"x"), resolver=resolver)
    with pytest.raises(FetchError) as error:
        fetch.get(url)
    assert error.value.kind == "refused"
    assert not calls and not factory.calls


@pytest.mark.parametrize(
    "private",
    ["127.0.0.1", "10.0.0.2", "169.254.169.254", "100.64.0.9", "224.0.0.1", "0.0.0.0",
     "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::",
     "2001:db8::1"],
)
def test_private_or_special_addresses_are_refused_even_mixed_with_public(private):
    def resolver(host, port, **kwargs):
        family = socket.AF_INET6 if ":" in private else socket.AF_INET
        return public_dns(host, port) + [(family, socket.SOCK_STREAM, 6, "", (private, port))]

    fetch, factory = fetcher(FakeResponse(body=b"x"), resolver=resolver)
    with pytest.raises(FetchError, match="non-public"):
        fetch.get("https://example.org/paper.pdf")
    assert not factory.calls


@pytest.mark.parametrize("address", ["::7f00:1", "::a9fe:a9fe", "::ffff:0:a9fe:a9fe", "::8.8.8.8", "::1:2:3",
                                     "64:ff9b:1::a9fe:a9fe"])
def test_ipv4_compatible_translated_and_reserved_ipv6_forms_are_refused(address):
    assert not safe_fetch.is_public_address(address)


def test_public_addresses_including_mapped_and_nat64_public_ones_pass():
    assert safe_fetch.is_public_address(PUBLIC_V4)
    assert safe_fetch.is_public_address("2606:4700::1111")
    assert safe_fetch.is_public_address("::ffff:8.8.8.8")
    assert safe_fetch.is_public_address("64:ff9b::808:808")
    assert not safe_fetch.is_public_address("not-an-address")


def test_connection_is_pinned_to_the_checked_address_against_dns_rebinding(monkeypatch):
    answers = [PUBLIC_V4, "127.0.0.1"]

    def rebinding(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers.pop(0), port))]

    fetch, factory = fetcher(FakeResponse(body=b"plain text body"), resolver=rebinding)
    result = fetch.get("https://example.org/paper.txt")
    assert result.kind == "text"
    # One resolution per hop; the second (rebinding) answer is never used.
    assert answers == ["127.0.0.1"]
    connected = []
    monkeypatch.setattr(
        safe_fetch.socket, "create_connection",
        lambda address, timeout, source=None: connected.append(address) or "sock",
    )
    factory.connections[0]._create_connection(("example.org", 443), 5.0)
    assert connected == [(PUBLIC_V4, 443)]
    assert factory.calls[0]["host"] == "example.org"  # TLS still verifies the name
    assert factory.calls[0]["context"].check_hostname is True


@pytest.mark.parametrize(
    "location",
    [
        "https://10.0.0.1/secret",
        "http://example.org/paper.pdf",
        "https://internal.corp/paper.pdf",
        "https://example.org:444/paper.pdf",
    ],
)
def test_redirects_are_revalidated(location):
    fetch, factory = fetcher(FakeResponse(302, headers={"Location": location}),
                             FakeResponse(body=b"never"))
    with pytest.raises(FetchError) as error:
        fetch.get("https://example.org/paper.pdf")
    assert error.value.kind == "refused"
    assert len(factory.calls) == 1


def test_redirect_to_a_host_that_resolves_privately_is_refused():
    def resolver(host, port, **kwargs):
        address = "192.168.1.5" if host == "evil.example.net" else PUBLIC_V4
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    fetch, factory = fetcher(
        FakeResponse(301, headers={"Location": "https://evil.example.net/x"}),
        FakeResponse(body=b"never"),
        resolver=resolver,
    )
    with pytest.raises(FetchError, match="non-public"):
        fetch.get("https://example.org/paper.pdf")
    assert len(factory.calls) == 1


def test_at_most_three_redirects_and_relative_targets_resolve():
    hops = [FakeResponse(302, headers={"Location": f"/hop{index}"}) for index in range(4)]
    fetch, factory = fetcher(*hops)
    with pytest.raises(FetchError, match="redirects too many times"):
        fetch.get("https://example.org/start")
    assert [call["path"] for call in factory.calls] == ["/start", "/hop0", "/hop1", "/hop2"]
    fetch, factory = fetcher(FakeResponse(302, headers={"Location": "/final"}), FakeResponse(body=b"ok text"))
    assert fetch.get("https://example.org/start").url == "https://example.org/final"


def test_allowed_hosts_bind_every_hop():
    fetch, factory = fetcher(FakeResponse(302, headers={"Location": "https://mirror.example.net/x"}))
    with pytest.raises(FetchError, match="not allowed for this source"):
        fetch.get("https://arxiv.org/e-print/2007.03528", allowed_hosts=("arxiv.org", "export.arxiv.org"))
    with pytest.raises(FetchError, match="not allowed"):
        SafeFetcher(resolver=public_dns, connection_factory=Connections()).get(
            "https://example.org/x", allowed_hosts=("arxiv.org",)
        )


def test_requests_are_get_identity_without_cookies():
    fetch, factory = fetcher(FakeResponse(body=b"%PDF-1.7 body"))
    result = fetch.get("https://example.org/paper.pdf?x=1#frag")
    call = factory.calls[0]
    assert call["method"] == "GET" and call["path"] == "/paper.pdf?x=1"
    assert call["headers"]["Accept-Encoding"] == "identity"
    assert "Cookie" not in call["headers"] and "Authorization" not in call["headers"]
    assert result.kind == "pdf" and result.url == "https://example.org/paper.pdf?x=1"


def test_oversize_by_header_and_by_stream():
    fetch, factory = fetcher(FakeResponse(headers={"Content-Length": "999999"}, body=b"x" * 10), max_bytes=64)
    with pytest.raises(FetchError) as error:
        fetch.get("https://example.org/big.pdf")
    assert error.value.kind == "too_large"
    fetch, _ = fetcher(FakeResponse(body=b"y" * 200), max_bytes=64)
    with pytest.raises(FetchError) as error:
        fetch.get("https://example.org/big.pdf")
    assert error.value.kind == "too_large"


def test_non_identity_encoding_is_refused():
    fetch, _ = fetcher(FakeResponse(headers={"Content-Encoding": "gzip"}, body=gzip.compress(b"text")))
    with pytest.raises(FetchError) as error:
        fetch.get("https://example.org/paper.tex")
    assert error.value.kind == "encoding"


def _tar_bytes() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        data = b"\\documentclass{article}"
        info = tarfile.TarInfo("main.tex")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.tex", "x")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b"%PDF-1.5\n...", "pdf"),
        (gzip.compress(b"x"), "gzip"),
        (_zip_bytes(), "zip"),
        (_tar_bytes(), "tar"),
        ("\\begin{theorem} é \\end{theorem}".encode("utf-8"), "text"),
        ("caf\xe9 \\section{Intro}".encode("latin-1"), "text"),
        (b"<!DOCTYPE html><html><body>hi</body></html>", "html"),
        (b"\x00\x01\x02\x03binary", "binary"),
    ],
)
def test_sniffing_uses_bytes_not_headers(body, kind):
    assert safe_fetch.sniff(body) == kind
    fetch, _ = fetcher(FakeResponse(headers={"Content-Type": "application/octet-stream"}, body=body))
    assert fetch.get("https://example.org/file").kind == kind


def test_kinds_restrict_what_a_source_accepts():
    fetch, _ = fetcher(FakeResponse(body=b"<html><body>landing page</body></html>"))
    with pytest.raises(FetchError, match="a web page") as error:
        fetch.get("https://example.org/paper", kinds={"pdf", "gzip", "zip", "tar", "text"})
    assert error.value.kind == "type"


def test_github_token_only_reaches_the_api_on_the_first_hop():
    fetch, factory = fetcher(
        FakeResponse(body=b'{"sha": "x"}'),
        FakeResponse(body=b"raw"),
        FakeResponse(302, headers={"Location": "https://api.github.com/repositories/1/x"}),
        FakeResponse(body=b'{"moved": true}'),
        github_token=TOKEN,
    )
    fetch.get("https://api.github.com/repos/openai/math/git/trees/abc", github_api=True)
    fetch.get("https://raw.githubusercontent.com/openai/math/abc/README.md", github_api=True)
    fetch.get("https://api.github.com/repos/openai/math/commits/main", github_api=True)
    assert factory.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "Authorization" not in factory.calls[1]["headers"]
    assert factory.calls[2]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    # A redirect never carries the token, even back to the API host.
    assert "Authorization" not in factory.calls[3]["headers"]
    assert TOKEN not in repr(fetch.__dict__.get("github_token"))


def test_a_users_api_link_never_carries_the_token():
    # Only the GitHub connector's own REST calls ask for the token: a link
    # someone pasted (even to api.github.com) is fetched without it.
    fetch, factory = fetcher(FakeResponse(body=b'{"login": "x"}'),
                             FakeResponse(302, headers={"Location": "https://codeload.github.com/o/p/tar.gz/main"}),
                             FakeResponse(body=b"never"), github_token=TOKEN)
    fetch.get("https://api.github.com/user")
    fetch.get("https://api.github.com/repos/o/private/tarball/main")
    assert len(factory.calls) == 3
    assert all("Authorization" not in call["headers"] for call in factory.calls)


def test_malformed_tokens_are_never_sent():
    fetch, factory = fetcher(FakeResponse(body=b"{}"), github_token="bad token\r\nX-Evil: 1")
    fetch.get("https://api.github.com/repos/openai/math")
    assert "Authorization" not in factory.calls[0]["headers"]
    assert not fetch.has_github_token


def test_status_errors_are_typed():
    fetch, _ = fetcher(FakeResponse(404))
    with pytest.raises(FetchError) as error:
        fetch.get("https://raw.githubusercontent.com/o/r/main/missing.tex")
    assert error.value.kind == "not_found" and error.value.status == 404
    fetch, _ = fetcher(FakeResponse(403, headers={"X-RateLimit-Remaining": "0"}))
    with pytest.raises(FetchError) as error:
        fetch.get("https://api.github.com/repos/o/r/git/trees/main")
    assert error.value.kind == "rate_limited"
    fetch, _ = fetcher(FakeResponse(500, body=b"secret upstream detail"))
    with pytest.raises(FetchError) as error:
        fetch.get("https://example.org/x")
    assert error.value.kind == "http" and "secret" not in str(error.value)


def test_bundle_budget_caps_total_bytes_and_time():
    clock = [0.0]
    budget = Budget(max_bytes=100, seconds=10, clock=lambda: clock[0])
    fetch, _ = fetcher(FakeResponse(body=b"a" * 60), FakeResponse(body=b"b" * 60))
    fetch.get("https://example.org/one.tex", budget=budget)
    with pytest.raises(FetchError) as error:
        fetch.get("https://example.org/two.tex", budget=budget)
    assert error.value.kind == "too_large"
    clock[0] = 11.0
    with pytest.raises(FetchError) as error:
        budget.check_time()
    assert error.value.kind == "timeout"


def test_body_deadline_stops_a_slow_stream():
    clock = [0.0]

    class Slow(FakeResponse):
        def read1(self, size):
            clock[0] += 20.0
            return b"x"

    fetch, _ = fetcher(Slow(), body_timeout=30.0, clock=lambda: clock[0])
    with pytest.raises(FetchError) as error:
        fetch.get("https://example.org/slow.tex")
    assert error.value.kind == "timeout"


def test_transport_errors_are_public_safe():
    class Broken(FakeResponse):
        def read1(self, size):
            raise OSError("connection reset by 10.0.0.7 with token abc")

    fetch, _ = fetcher(Broken())
    with pytest.raises(FetchError) as error:
        fetch.get("https://example.org/x.tex")
    assert error.value.kind == "network"
    assert "10.0.0.7" not in str(error.value)


class _Trickler:
    """A loopback server that sends a status line, then one header byte at a
    time for as long as the client listens (a slow-header attack)."""

    def __init__(self):
        import threading

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.server.settimeout(0.2)
        while not self.stop.is_set():
            try:
                client, _ = self.server.accept()
            except OSError:
                continue
            try:
                client.recv(65536)
                client.sendall(b"HTTP/1.1 200 OK\r\nX-Pad: ")
                while not self.stop.is_set():
                    client.sendall(b"a")
                    self.stop.wait(0.2)
            except OSError:
                pass
            finally:
                client.close()

    def close(self):
        self.stop.set()
        self.server.close()
        self.thread.join(5)


@pytest.fixture()
def trickler(monkeypatch):
    import http.client

    server = _Trickler()
    real_pinned = safe_fetch._pinned

    def loopback(_address, watchdog=None):
        create = real_pinned("127.0.0.1", watchdog)
        return lambda _ignored, timeout, source=None: create(None, timeout, source)

    real_connect = socket.create_connection
    monkeypatch.setattr(safe_fetch, "_pinned", loopback)
    monkeypatch.setattr(safe_fetch.socket, "create_connection",
                        lambda address, timeout, source=None: real_connect(("127.0.0.1", server.port), timeout))
    factory = lambda host, port, timeout, context: http.client.HTTPConnection(host, server.port, timeout=timeout)  # noqa: E731
    yield SafeFetcher(resolver=public_dns, connection_factory=factory, connect_timeout=5.0, body_timeout=30.0)
    server.close()


def test_a_server_that_trickles_headers_cannot_outlast_the_budget(trickler):
    import time

    started = time.monotonic()
    with pytest.raises(FetchError) as error:
        trickler.get("https://example.org/slow.pdf", budget=Budget(seconds=1.5))
    assert error.value.kind == "timeout"
    assert time.monotonic() - started < 6


def test_a_stopped_run_ends_a_fetch_at_once(trickler):
    import threading
    import time

    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(FetchError) as error:
        trickler.get("https://example.org/slow.pdf", budget=Budget(seconds=60, cancel=cancel))
    assert error.value.kind == "cancelled"
    assert time.monotonic() - started < 3


def test_tls_context_advertises_http_1_1(monkeypatch):
    # Some CDNs (WordPress.com among them) answer TLS clients that offer no
    # ALPN with 403; the fetcher speaks HTTP/1.1 and says so.
    offered = []

    class Context:
        def set_alpn_protocols(self, protocols):
            offered.append(list(protocols))

    monkeypatch.setattr(safe_fetch.ssl, "create_default_context", Context)
    SafeFetcher(resolver=public_dns)
    assert offered == [["http/1.1"]]
