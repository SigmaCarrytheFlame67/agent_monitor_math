"""Hardened HTTPS GETs of user-supplied links (Analyze manuscript sources).

Generalized from ``news.HTTPSFetcher`` for links that people paste:

* ``https`` on port 443 only, no userinfo, no IP literals (dotted, bracketed
  or numeric forms) and no special-use names (``.local``, ``.internal``, ...);
* every address the name resolves to must be public (``is_global``, not
  multicast, nothing in ``::/8`` but an IPv4-mapped address, and no IPv4
  embedded in a mapped, 6to4, Teredo or NAT64 IPv6 address may be private),
  and the TCP connection is pinned to the checked address while TLS still
  verifies the publisher's hostname (SNI), so a second, rebinding DNS answer
  is never used;
* at most three redirects, each target validated again from scratch;
* ``GET`` only, ``Accept-Encoding: identity``, no cookies; an optional GitHub
  token is sent only when the caller asks for it (``github_api=True``, the
  connector's own REST calls), only to ``api.github.com`` and only on the
  first hop, never after a redirect;
* size caps (25 MiB per file, 30 MiB per bundle by default), a connect timeout
  (10 s), a per-body deadline (30 s), a bundle deadline (120 s) and a hard
  wall-clock limit per hop (connect + TLS + headers + body), enforced by a
  watchdog that shuts the socket down, which also ends a fetch at once when
  the bundle's ``cancel`` event is set (a stopped run);
* the body's kind is sniffed from its bytes (``%PDF-``, gzip, zip, ustar, text),
  never taken from ``Content-Type``.

Error messages are public-safe: they never echo a response body or a token.
The module does no HTML scraping and executes nothing.
"""
from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_BUNDLE_BYTES = 30 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
BODY_TIMEOUT_SECONDS = 30.0
BUNDLE_SECONDS = 120.0
MAX_REDIRECTS = 3
MAX_URL_CHARS = 4096
USER_AGENT = "AnsatzeAnalyze/1.0 (+https://ansatze.ai)"
GITHUB_API_HOST = "api.github.com"
# Outbound fetches in flight per process (resolve preflights and runners).
MAX_CONCURRENT_FETCHES = 4

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_SPECIAL_SUFFIXES = (
    ".localhost", ".local", ".localdomain", ".internal", ".invalid", ".test",
    ".example", ".onion", ".arpa", ".lan", ".home", ".corp", ".intranet",
    ".private",
)
_NUMERIC_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
_HOST_RE = re.compile(r"[a-z0-9.-]+")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# IPv4-compatible (::a.b.c.d), SIIT-translated (::ffff:0:a.b.c.d) and the
# rest of the reserved ::/8; IPv4-mapped addresses are checked instead.
_ZERO_PREFIX = ipaddress.ip_network("::/8")
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_FETCHES)
# Response headers callers may read (lower-cased names).
_KEPT_HEADERS = (
    "content-type", "content-disposition", "etag", "last-modified",
    "x-ratelimit-remaining", "x-ratelimit-reset",
)


class FetchError(ValueError):
    """A public-safe fetch refusal or failure.

    ``kind`` is one of ``refused`` (URL or address policy), ``not_found``,
    ``rate_limited``, ``http`` (other non-200 status), ``too_large``,
    ``timeout``, ``cancelled`` (the run was stopped), ``type`` (sniffed kind
    not accepted), ``encoding`` or ``network``; ``status`` is the HTTP status
    when one arrived.
    """

    def __init__(self, message: str, *, kind: str = "refused", status: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    url: str
    body: bytes
    kind: str
    content_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        """The body as text: UTF-8 (BOM stripped), else Latin-1."""
        try:
            return self.body.decode("utf-8-sig")
        except UnicodeDecodeError:
            return self.body.decode("latin-1")


def hostname(value: object) -> str:
    """A validated, lower-cased ASCII hostname (never an IP literal)."""
    if not isinstance(value, str):
        raise FetchError("The link has no valid host name")
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise FetchError("The link has no valid host name") from None
    if (
        not host
        or len(host) > 253
        or "." not in host
        or ".." in host
        or not _HOST_RE.fullmatch(host)
        or host.startswith(("-", "."))
        or host.endswith(_SPECIAL_SUFFIXES)
    ):
        raise FetchError("Links to local or special-use host names are not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise FetchError("Links to IP addresses are not allowed; use a host name")
    if all(_NUMERIC_LABEL_RE.fullmatch(label) for label in host.split(".")):
        # 0x7f.1, 017.0.0.1 and similar resolver-dependent IPv4 spellings.
        raise FetchError("Links to IP addresses are not allowed; use a host name")
    return host


def public_https_url(value: object, *, allowed_hosts: Iterable[str] | None = None) -> str:
    """Validate and normalize a public HTTPS URL (fragment dropped)."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_URL_CHARS
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise FetchError("The link is not a valid URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise FetchError("The link is not a valid URL") from None
    if parts.scheme.lower() != "https":
        raise FetchError("Only https:// links are supported")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise FetchError("Links with embedded credentials are not allowed")
    if port not in (None, 443):
        raise FetchError("Only the standard HTTPS port is allowed")
    host = hostname(parts.hostname)
    if allowed_hosts is not None and host not in {str(item).lower() for item in allowed_hosts}:
        raise FetchError("That host is not allowed for this source")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def _embedded_ipv4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    embedded: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        embedded.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        embedded.extend(address.teredo)
    if address in _NAT64:
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return embedded


def is_public_address(value: str) -> bool:
    """Whether a resolved address is a public unicast address."""
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address in _ZERO_PREFIX and address.ipv4_mapped is None and address not in _NAT64:
            return False
        return all(
            embedded.is_global and not embedded.is_multicast
            for embedded in _embedded_ipv4(address)
        )
    return True


def sniff(data: bytes) -> str:
    """The kind of a payload from its first bytes.

    ``pdf``, ``gzip``, ``zip``, ``tar``, ``bzip2``, ``xz``, ``html``,
    ``text`` (UTF-8, or mostly printable single-byte text) or ``binary``.
    """
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data[:2] == b"\x1f\x8b":
        return "gzip"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if len(data) >= 262 and data[257:262] == b"ustar":
        return "tar"
    if data[:3] == b"BZh":
        return "bzip2"
    if data[:6] == b"\xfd7zXZ\x00":
        return "xz"
    head = data[:65536]
    if b"\x00" in head:
        return "binary"
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        if exc.start >= len(head) - 3:
            # A multibyte character cut at the sample boundary.
            text = head[: exc.start].decode("utf-8-sig", errors="replace")
        else:
            controls = sum(1 for byte in head if byte < 32 and byte not in (9, 10, 12, 13))
            return "text" if controls <= max(1, len(head) // 100) else "binary"
    lowered = text.lstrip().lower()
    if lowered.startswith(("<!doctype html", "<html")) or "<html" in lowered[:512]:
        return "html"
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\t\n\f\r")
    return "text" if controls <= max(1, len(text) // 100) else "binary"


class Budget:
    """Byte and wall-clock limits shared by every fetch of one bundle; setting
    ``cancel`` (a stopped run) ends the fetch in progress."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_BUNDLE_BYTES,
        seconds: float = BUNDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        cancel: threading.Event | None = None,
    ) -> None:
        self.max_bytes = int(max_bytes)
        self.clock = clock
        self.deadline = clock() + float(seconds)
        self.used = 0
        self.cancel = cancel
        self._lock = threading.Lock()

    def remaining_bytes(self) -> int:
        with self._lock:
            return max(0, self.max_bytes - self.used)

    def remaining_seconds(self) -> float:
        return self.deadline - self.clock()

    def check_time(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise FetchError("The fetch was stopped", kind="cancelled")
        if self.remaining_seconds() <= 0:
            raise FetchError("Fetching the source took too long", kind="timeout")

    def charge(self, count: int) -> None:
        with self._lock:
            if self.used + count > self.max_bytes:
                raise FetchError("The source exceeds its total size limit", kind="too_large")
            self.used += count


def _pinned(address: str, watchdog: "_Watchdog | None" = None) -> Callable[..., socket.socket]:
    def create(_ignored: object, timeout: float, source_address: object = None) -> socket.socket:
        sock = socket.create_connection((address, 443), timeout, source_address)
        if watchdog is not None:
            watchdog.attach(sock)
        return sock

    return create


class _Watchdog:
    """Ends one hop at its wall-clock deadline, or when ``cancel`` is set, by
    shutting its TCP connection down: a server that trickles a header byte at
    a time (or stalls the TLS handshake) cannot hold a fetch past its limits.

    It shuts down a duplicate of the socket's descriptor, which reaches the
    connection whether or not TLS has wrapped the socket; ``finish`` is
    called before the connection is closed, so a descriptor that the
    process later reuses is never touched.
    """

    def __init__(self, seconds: float, cancel: threading.Event | None, connection: Any = None) -> None:
        self.deadline = time.monotonic() + max(0.0, seconds)
        self.cancel = cancel
        self.connection = connection  # its socket, if it connected without the pinned factory
        self.fired: str | None = None  # "timeout" or "cancelled"
        self._fd: int | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="safe-fetch-watchdog", daemon=True)
        self._thread.start()

    def attach(self, sock: socket.socket) -> None:
        try:
            descriptor = sock.fileno()
        except (AttributeError, OSError):
            return
        with self._lock:
            self._fd = descriptor
            fired = self.fired
        if fired:
            self._shutdown()

    def _watch(self) -> None:
        while not self._done.is_set():
            left = self.deadline - time.monotonic()
            cancelled = self.cancel is not None and self.cancel.is_set()
            if left <= 0 or cancelled:
                with self._lock:
                    if self._done.is_set():
                        return
                    self.fired = "cancelled" if cancelled else "timeout"
                self._shutdown()
                return
            self._done.wait(min(left, 0.2))

    def _shutdown(self) -> None:
        with self._lock:
            if self._done.is_set():
                return
            descriptor = self._fd
            if descriptor is None:
                try:
                    descriptor = self.connection.sock.fileno()
                except (AttributeError, OSError):
                    return
            if descriptor < 0:
                return
            try:
                copy = os.dup(descriptor)
            except OSError:
                return
            try:
                duplicate = socket.socket(fileno=copy)
            except OSError:
                os.close(copy)
                return
        try:
            duplicate.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            duplicate.close()

    def finish(self) -> None:
        with self._lock:
            self._done.set()
            self._fd = None


class SafeFetcher:
    """Bounded GETs of public HTTPS URLs; see the module docstring.

    ``resolver`` and ``connection_factory`` are injectable for tests (the
    defaults are ``socket.getaddrinfo`` and ``http.client.HTTPSConnection``).
    ``github_token`` is sent as a bearer token only for ``get(...,
    github_api=True)``, only to ``api.github.com`` and only on the first hop:
    a user's link to the API never carries it.
    """

    def __init__(
        self,
        *,
        resolver: Callable[..., Any] | None = None,
        connection_factory: Callable[..., Any] | None = None,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        body_timeout: float = BODY_TIMEOUT_SECONDS,
        max_bytes: int = MAX_FILE_BYTES,
        github_token: str | None = None,
        user_agent: str = USER_AGENT,
        clock: Callable[[], float] = time.monotonic,
        slots: Any = None,
    ) -> None:
        self.resolver = resolver or socket.getaddrinfo
        self.connection_factory = connection_factory or http.client.HTTPSConnection
        self.connect_timeout = float(connect_timeout)
        self.body_timeout = float(body_timeout)
        self.max_bytes = int(max_bytes)
        token = str(github_token or "").strip()
        self._github_token = token if re.fullmatch(r"[A-Za-z0-9_.-]{20,255}", token) else ""
        self.user_agent = user_agent
        self.clock = clock
        self.slots = slots if slots is not None else _SLOTS
        self.context = ssl.create_default_context()
        # Advertise the protocol we speak: some CDNs answer TLS clients that
        # offer no ALPN with HTTP 403.
        self.context.set_alpn_protocols(["http/1.1"])

    @property
    def has_github_token(self) -> bool:
        return bool(self._github_token)

    def public_addresses(self, host: str) -> list[str]:
        try:
            rows = self.resolver(host, 443, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            raise FetchError("The link's host name did not resolve", kind="network") from None
        addresses = list(dict.fromkeys(str(row[4][0]) for row in rows))
        if not addresses:
            raise FetchError("The link's host name did not resolve", kind="network")
        if not all(is_public_address(address) for address in addresses):
            raise FetchError("The link's host resolves to a non-public address")
        return addresses

    def get(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str] | None = None,
        accept: str = "*/*",
        max_bytes: int | None = None,
        budget: Budget | None = None,
        kinds: Iterable[str] | None = None,
        headers: Mapping[str, str] | None = None,
        github_api: bool = False,
    ) -> FetchResult:
        """GET ``url``; raise :class:`FetchError` unless a 200 body arrives.

        ``kinds`` restricts the sniffed kind of the body; ``budget`` charges
        the bytes and bounds the time of every fetch of one bundle;
        ``github_api`` marks the connector's own GitHub REST calls (the only
        requests that may carry the operator's token).
        """
        limit = min(self.max_bytes, int(max_bytes or self.max_bytes))
        if budget is not None:
            budget.check_time()
            limit = min(limit, budget.remaining_bytes())
            if limit <= 0:
                raise FetchError("The source exceeds its total size limit", kind="too_large")
        wanted = None if kinds is None else frozenset(kinds)
        hosts = None if allowed_hosts is None else tuple(allowed_hosts)
        current = public_https_url(url, allowed_hosts=hosts)
        wait = self.connect_timeout + self.body_timeout
        if budget is not None:
            wait = min(wait, max(0.0, budget.remaining_seconds()))
        if not self.slots.acquire(timeout=wait):
            raise FetchError("Too many sources are being fetched; try again shortly", kind="timeout")
        try:
            for hop in range(MAX_REDIRECTS + 1):
                result = self._request(
                    current,
                    hop=hop,
                    accept=accept,
                    limit=limit,
                    budget=budget,
                    extra_headers=headers,
                    github_api=github_api,
                )
                if isinstance(result, FetchResult):
                    if wanted is not None and result.kind not in wanted:
                        raise FetchError(
                            f"The link returned {_describe(result.kind)}, which this source does not accept",
                            kind="type",
                        )
                    if budget is not None:
                        budget.charge(len(result.body))
                    return result
                if hop == MAX_REDIRECTS:
                    break
                current = public_https_url(urljoin(current, result), allowed_hosts=hosts)
        finally:
            self.slots.release()
        raise FetchError("The link redirects too many times", kind="http")

    def _request(
        self,
        url: str,
        *,
        hop: int,
        accept: str,
        limit: int,
        budget: Budget | None,
        extra_headers: Mapping[str, str] | None,
        github_api: bool = False,
    ) -> FetchResult | str:
        """One hop: a :class:`FetchResult`, or the redirect target."""
        parts = urlsplit(url)
        host = str(parts.hostname)
        addresses = self.public_addresses(host)
        connect_timeout = self.connect_timeout
        hop_seconds = self.connect_timeout + self.body_timeout
        if budget is not None:
            budget.check_time()
            connect_timeout = max(1.0, min(connect_timeout, budget.remaining_seconds()))
            hop_seconds = min(hop_seconds, budget.remaining_seconds())
        connection = self.connection_factory(
            host, 443, timeout=connect_timeout, context=self.context
        )
        # Pin the checked address for TCP; HTTPSConnection still sends SNI and
        # verifies the certificate for the hostname (no second DNS lookup).
        # The watchdog bounds the whole hop, headers and TLS included.
        watchdog = _Watchdog(hop_seconds, budget.cancel if budget is not None else None, connection)
        connection._create_connection = _pinned(addresses[0], watchdog)
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Encoding": "identity",
        }
        for name, value in (extra_headers or {}).items():
            if (
                isinstance(value, str)
                and len(value) <= 512
                and name.lower() in {"if-none-match", "if-modified-since"}
                and not any(ord(char) < 32 for char in value)
            ):
                request_headers[name] = value
        if github_api and hop == 0 and host == GITHUB_API_HOST and self._github_token:
            request_headers["Authorization"] = f"Bearer {self._github_token}"
            request_headers["X-GitHub-Api-Version"] = "2022-11-28"
        try:
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            connection.request("GET", path, headers=request_headers)
            response = connection.getresponse()
            status = int(response.status)
            if status in _REDIRECTS:
                location = response.getheader("Location")
                if not location:
                    raise FetchError("The link redirects without a target", kind="http", status=status)
                return location
            kept = {
                name: str(response.getheader(name) or "")[:512]
                for name in _KEPT_HEADERS
                if response.getheader(name)
            }
            if status != 200:
                raise _status_error(status, host, kept)
            encoding = str(response.getheader("Content-Encoding") or "identity").strip().lower()
            if encoding not in ("", "identity"):
                raise FetchError("The server sent an unsupported content encoding", kind="encoding")
            length = str(response.getheader("Content-Length") or "").strip()
            if length.isdigit() and int(length) > limit:
                raise FetchError("The file exceeds its size limit", kind="too_large")
            body = self._read_body(connection, response, limit, budget)
        except FetchError:
            raise
        except (TimeoutError, socket.timeout):
            raise FetchError("The source did not answer in time", kind="timeout") from None
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
            if watchdog.fired == "cancelled":
                raise FetchError("The fetch was stopped", kind="cancelled") from None
            if watchdog.fired:
                raise FetchError("The source did not answer in time", kind="timeout") from None
            raise FetchError("The source could not be fetched", kind="network") from None
        finally:
            watchdog.finish()
            connection.close()
        if watchdog.fired:
            # The watchdog cut the body short: never accept a truncated file.
            cancelled = watchdog.fired == "cancelled"
            raise FetchError("The fetch was stopped" if cancelled else "The source did not answer in time",
                             kind="cancelled" if cancelled else "timeout")
        return FetchResult(
            url=url,
            body=body,
            kind=sniff(body),
            content_type=kept.get("content-type", ""),
            headers=kept,
        )

    def _read_body(
        self, connection: Any, response: Any, limit: int, budget: Budget | None
    ) -> bytes:
        seconds = self.body_timeout
        if budget is not None:
            # The budget keeps its own clock; only its remaining time is used.
            seconds = min(seconds, budget.remaining_seconds())
        deadline = self.clock() + seconds
        body = bytearray()
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise FetchError("The source did not finish downloading in time", kind="timeout")
            sock = getattr(connection, "sock", None)
            if sock is not None:
                sock.settimeout(max(0.5, min(self.body_timeout, remaining)))
            chunk = response.read1(min(65536, limit + 1 - len(body)))
            if not chunk:
                return bytes(body)
            body.extend(chunk)
            if len(body) > limit:
                raise FetchError("The file exceeds its size limit", kind="too_large")


def _status_error(status: int, host: str, headers: Mapping[str, str]) -> FetchError:
    if status == 404 or status == 410:
        return FetchError("The link was not found (HTTP 404)", kind="not_found", status=status)
    if host == GITHUB_API_HOST and status in (403, 429) and headers.get("x-ratelimit-remaining") == "0":
        return FetchError(
            "GitHub's API rate limit is reached; try again later or link a single file",
            kind="rate_limited",
            status=status,
        )
    if status == 429:
        return FetchError("The source is rate limiting requests; try again later", kind="rate_limited", status=status)
    return FetchError(f"The source answered HTTP {status}", kind="http", status=status)


def _describe(kind: str) -> str:
    return {
        "html": "a web page",
        "pdf": "a PDF",
        "gzip": "a gzip archive",
        "zip": "a zip archive",
        "tar": "a tar archive",
        "text": "a text file",
    }.get(kind, "an unsupported file type")
