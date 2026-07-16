"""Reverse-proxy transport for the operator runtime (``SRS-RES-001`` / IF-13).

A generic, consumer-agnostic seam: a top-layer consumer (e.g. the dashboard's
embedded Jupyter research environment) registers a path *prefix* whose requests
the runtime forwards verbatim to ONE upstream that is fixed at registration
time. IF-13 mandates the research environment is "proxied through dashboard
HTTPS; not a standalone external endpoint" — this module is that proxy's
transport. The HTTPS layer itself stays the operator's documented reverse-proxy
concern (NFR-S3); the runtime speaks plain HTTP on its loopback/RFC-1918 bind.

Policy (fail closed, every rule enforced in code below):

* **Upstream fixity.** The upstream authority comes only from
  :func:`compile_proxy_route`; a request contributes nothing but its verbatim
  path + query. There is no path rewriting, so there is no translation logic
  through which a crafted path could select a different upstream.
* **Private upstreams only.** A literal-IP/localhost upstream is validated at
  registration; a DNS name is resolved at connect time and EVERY resolved
  address must be loopback/RFC 1918 (the ``SRS-SEC-002`` bind set), else the
  request is refused before any byte is forwarded. The socket connects to the
  validated IP, closing the resolve/connect TOCTOU.
* **No operator-surface shadowing.** A prefix that encloses or is enclosed by
  ``/api/``, ``/dashboard/``, the WebSocket path, or any registered meta/asset
  path is refused at registration — the read-only ``/dashboard`` invariant and
  the ``/api/v1`` contract cannot be reached through a proxy spelling.
* **Request-smuggling hardening.** Chunked request bodies are refused (400),
  ``Content-Length`` is validated and read exactly, hop-by-hop headers (plus
  every token the incoming ``Connection`` header names) are stripped in both
  directions, and the upstream leg is parsed by :mod:`http.client`.
* **Bounded everything.** Connect/read deadlines, a request-body cap, a cap on
  buffered unknown-length responses, a bounded WebSocket-handshake read, and a
  server-wide WebSocket tunnel slot limit. Idle kernel-style WS tunnels are
  deliberately NOT killed (research kernels legitimately sit idle for minutes);
  the bound is the slot count, not an idle timer.

Known limitation: an unknown-length (chunked) upstream response is buffered up
to :data:`MAX_BUFFERED_RESPONSE_BYTES` and re-sent with a computed
``Content-Length`` — a Server-Sent-Events upstream would stall. JupyterLab's
core uses WebSockets (tunnelled raw) for live traffic, so this trade keeps the
transport simple without breaking the embed.

SRS trace
---------
``SRS-RES-001`` / IF-13 (dashboard-embedded research environment),
``SRS-SEC-002`` (private-address policy reused for upstreams), ``SRS-SEC-004``
(the one-way dashboard→Jupyter boundary this transport preserves: it only ever
*responds* to browser requests; nothing under a proxy prefix can dispatch into
the operator REST surface).
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import ProxyPolicyError

__all__ = [
    "MAX_BUFFERED_RESPONSE_BYTES",
    "MAX_PROXY_BODY_BYTES",
    "MAX_PROXY_WS_CONNECTIONS",
    "PROXY_CONNECT_TIMEOUT",
    "PROXY_READ_TIMEOUT",
    "ProxyUpstream",
    "compile_proxy_route",
    "filter_request_headers",
    "filter_response_headers",
    "match_proxy_route",
    "open_upstream_response",
    "open_upstream_ws",
    "pump_sockets",
    "resolve_upstream_address",
]

#: RFC 9110 hop-by-hop headers: never forwarded in either direction.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: Request headers the proxy owns itself (rewritten or recomputed per leg).
_PROXY_OWNED_REQUEST_HEADERS = frozenset({"host", "content-length"})

#: Hard ceiling on a proxied request body. Larger than the operator REST
#: ceiling (1 MiB) because notebook saves carry real payloads; still bounded so
#: a hostile client cannot stream unbounded bytes through the runtime.
MAX_PROXY_BODY_BYTES = 32 << 20  # 32 MiB

#: Ceiling on a *buffered* unknown-length upstream response (see module
#: docstring). Known-length responses stream through in chunks instead.
MAX_BUFFERED_RESPONSE_BYTES = 32 << 20  # 32 MiB

#: Upstream TCP connect deadline — an unreachable upstream fails fast to 502.
PROXY_CONNECT_TIMEOUT = 5.0

#: Upstream read deadline per read — a stalled upstream fails closed, never
#: pinning a handler thread forever.
PROXY_READ_TIMEOUT = 30.0

#: Server-wide cap on concurrently tunnelled WebSocket connections (kernel
#: channels). Exceeding it yields an honest 503, not a queue.
MAX_PROXY_WS_CONNECTIONS = 32

#: Bounded read for the upstream's WebSocket handshake response head.
_WS_HANDSHAKE_MAX_BYTES = 16 << 10

#: Streaming copy chunk size for known-length response bodies.
_STREAM_CHUNK_BYTES = 64 << 10

#: WS tunnel pumps poll their ``closed`` event on this cadence — a deliberately
#: short *poll* interval (teardown latency), NOT an idle kill.
_PUMP_POLL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ProxyUpstream:
    """A compiled proxy route: requests under ``prefix`` go to ``host:port``.

    ``prefix`` is normalised to start AND end with ``/``. The upstream
    authority is immutable — see :func:`compile_proxy_route`.
    """

    prefix: str
    host: str
    port: int


def _normalise_prefix(path: str) -> str:
    """Ensure a path starts and ends with ``/`` (for boundary-safe compares)."""

    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path = path + "/"
    return path


def compile_proxy_route(
    prefix: str,
    upstream: str,
    *,
    reserved: Iterable[str],
    allow_host: Callable[[str], bool],
) -> ProxyUpstream:
    """Validate and compile a proxy registration (fail fast, registration time).

    Args:
        prefix: Path prefix to proxy, e.g. ``"/research/"``. Must not overlap
            any ``reserved`` path (either direction) and must not be ``/``.
        upstream: Fixed upstream authority, e.g. ``"http://127.0.0.1:8888"``.
            Scheme must be plain ``http`` with no path/query/fragment.
        reserved: Operator-surface paths the prefix may not shadow.
        allow_host: Address policy (loopback/RFC 1918 — pass
            :func:`~atp_runtime.rest_server.is_allowed_bind_host`). Applied now
            for literal-IP/localhost upstreams; DNS names are resolved and
            re-checked on every connect by :func:`resolve_upstream_address`.

    Raises:
        ProxyPolicyError: On any policy violation.
    """

    if not isinstance(prefix, str) or not prefix.startswith("/") or len(prefix.strip("/")) == 0:
        raise ProxyPolicyError(f"proxy prefix must be a non-root path starting with '/': {prefix!r}")
    if any(ch.isspace() for ch in prefix) or ".." in prefix:
        raise ProxyPolicyError(f"proxy prefix contains forbidden characters: {prefix!r}")
    normalised = _normalise_prefix(prefix)
    for entry in reserved:
        entry_norm = _normalise_prefix(entry)
        if entry_norm == "/":
            continue  # root is refused above; "/" in reserved means exactly root
        if normalised.startswith(entry_norm) or entry_norm.startswith(normalised):
            raise ProxyPolicyError(
                f"proxy prefix {prefix!r} overlaps reserved operator path {entry!r} "
                f"(the /api/v1 contract, /dashboard read-only surface, WS path, and "
                f"registered meta/asset paths cannot be shadowed)"
            )

    split = urlsplit(upstream)
    if split.scheme != "http":
        raise ProxyPolicyError(
            f"proxy upstream must be plain http (the runtime's private-network leg): {upstream!r}"
        )
    if split.path not in ("", "/") or split.query or split.fragment:
        raise ProxyPolicyError(
            f"proxy upstream must be an authority only (no path/query/fragment): {upstream!r} "
            f"— request paths are forwarded verbatim, so the upstream serves under the prefix"
        )
    host = split.hostname
    if not host:
        raise ProxyPolicyError(f"proxy upstream has no host: {upstream!r}")
    port = split.port if split.port is not None else 80
    # A literal IP (or localhost) is checkable now; a DNS name is validated on
    # every connect (resolve_upstream_address), so a public name never receives
    # a byte either way.
    if host == "localhost" or _is_ip_literal(host):
        if not allow_host(host):
            raise ProxyPolicyError(
                f"proxy upstream {host!r} is not loopback/RFC 1918 (SRS-SEC-002 address policy)"
            )
    return ProxyUpstream(prefix=normalised, host=host, port=port)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def match_proxy_route(
    proxy_routes: Mapping[str, ProxyUpstream], path: str
) -> ProxyUpstream | None:
    """Longest-prefix match of a request path against the registered routes.

    Matches ``/research/...`` under prefix ``/research/`` and the bare
    ``/research`` spelling too (so a browser hitting the prefix without the
    trailing slash still reaches the upstream, which issues its own redirect).
    """

    best: ProxyUpstream | None = None
    for prefix, route in proxy_routes.items():
        if path.startswith(prefix) or path == prefix.rstrip("/"):
            if best is None or len(prefix) > len(best.prefix):
                best = route
    return best


def resolve_upstream_address(
    route: ProxyUpstream, allow_host: Callable[[str], bool]
) -> tuple[str, int]:
    """Resolve the fixed upstream and enforce the private-address policy.

    EVERY address the name resolves to must be loopback/RFC 1918, else the
    connect is refused (fail closed — a split-horizon name that also resolves
    publicly is treated as hostile). Returns the first validated address; the
    caller connects to that literal IP, so what was validated is what is dialed.
    """

    try:
        infos = socket.getaddrinfo(route.host, route.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ProxyPolicyError(f"proxy upstream {route.host!r} did not resolve: {exc}") from exc
    if not infos:
        raise ProxyPolicyError(f"proxy upstream {route.host!r} resolved to no addresses")
    validated: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if not allow_host(address):
            raise ProxyPolicyError(
                f"proxy upstream {route.host!r} resolves to non-private address {address!r} "
                f"(SRS-SEC-002 address policy; refusing to forward)"
            )
        validated.append(address)
    return validated[0], route.port


def _assert_clean_header(name: str, value: str) -> None:
    """Belt-and-braces header-injection guard (stdlib also refuses CR/LF)."""

    if any(ch in "\r\n" for ch in name) or any(ch in "\r\n" for ch in value):
        raise ProxyPolicyError(f"refusing header with CR/LF: {name!r}")


def filter_request_headers(
    headers: Mapping[str, str], upstream_netloc: str
) -> list[tuple[str, str]]:
    """Filter browser→upstream headers for one proxied request.

    Strips hop-by-hop headers plus every token the incoming ``Connection``
    header names, and the proxy-owned ``Host``/``Content-Length`` (recomputed
    per leg). Rewrites ``Origin``/``Referer`` to the upstream netloc so the
    upstream's same-origin checks (e.g. Jupyter's ``check_origin`` / XSRF) see
    a self-consistent origin. Everything else — ``Cookie``, ``Authorization``,
    ``X-XSRFToken``, ``Content-Type``, ``Accept-Encoding``, … — forwards as-is.
    """

    connection_tokens = {
        token.strip().lower()
        for token in headers.get("Connection", "").split(",")
        if token.strip()
    }
    dropped = _HOP_BY_HOP | connection_tokens | _PROXY_OWNED_REQUEST_HEADERS
    filtered: list[tuple[str, str]] = []
    for name, value in headers.items():
        lower = name.lower()
        if lower in dropped:
            continue
        if lower == "origin":
            value = f"http://{upstream_netloc}"
        elif lower == "referer":
            parts = urlsplit(value)
            value = f"http://{upstream_netloc}{parts.path}"
            if parts.query:
                value = f"{value}?{parts.query}"
        _assert_clean_header(name, value)
        filtered.append((name, value))
    return filtered


def filter_response_headers(items: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Strip hop-by-hop upstream→browser headers; keep everything else.

    ``Content-Security-Policy`` and ``Set-Cookie`` pass through untouched —
    Jupyter's ``frame-ancestors 'self'`` must reach the browser, and it admits
    the embed precisely because the tunnel serves Jupyter from the dashboard's
    own origin. ``Content-Length`` passes through for known-length bodies (the
    handler recomputes it when it had to buffer an unknown-length body).
    """

    filtered: list[tuple[str, str]] = []
    connection_tokens: set[str] = set()
    materialised = list(items)
    for name, value in materialised:
        if name.lower() == "connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
    dropped = _HOP_BY_HOP | connection_tokens
    for name, value in materialised:
        if name.lower() in dropped:
            continue
        _assert_clean_header(name, value)
        filtered.append((name, value))
    return filtered


def open_upstream_response(
    route: ProxyUpstream,
    method: str,
    raw_path: str,
    headers: Mapping[str, str],
    body: bytes,
    allow_host: Callable[[str], bool],
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """Forward one HTTP request to the fixed upstream; return (conn, response).

    The caller streams the response and MUST close the returned connection.
    Raises :class:`ProxyPolicyError` on policy refusal; propagates ``OSError``/
    ``http.client.HTTPException`` for the caller to map to an honest 502.
    """

    address, port = resolve_upstream_address(route, allow_host)
    upstream_netloc = f"{route.host}:{route.port}"
    connection = http.client.HTTPConnection(address, port, timeout=PROXY_CONNECT_TIMEOUT)
    try:
        connection.connect()
        if connection.sock is not None:  # pragma: no branch - connected socket exists
            connection.sock.settimeout(PROXY_READ_TIMEOUT)
        connection.putrequest(method, raw_path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", upstream_netloc)
        for name, value in filter_request_headers(headers, upstream_netloc):
            connection.putheader(name, value)
        if body:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body if body else None)
        return connection, connection.getresponse()
    except BaseException:
        connection.close()
        raise


def open_upstream_ws(
    route: ProxyUpstream,
    raw_path: str,
    headers: Mapping[str, str],
    allow_host: Callable[[str], bool],
) -> tuple[socket.socket, bytes]:
    """Open the upstream leg of a WebSocket tunnel.

    Rebuilds the client's handshake against the fixed upstream (Host/Origin
    rewritten, hop-by-hop stripped, ``Sec-WebSocket-*`` and ``Cookie`` carried
    through), reads the upstream's response head bounded by
    :data:`_WS_HANDSHAKE_MAX_BYTES`, and returns ``(connected socket, verbatim
    head bytes)`` — the head may legitimately include early frames past the
    ``\\r\\n\\r\\n`` terminator; the caller relays it verbatim. A non-101
    upstream response raises (the caller maps it to 502).
    """

    address, port = resolve_upstream_address(route, allow_host)
    upstream_netloc = f"{route.host}:{route.port}"
    sock = socket.create_connection((address, port), timeout=PROXY_CONNECT_TIMEOUT)
    try:
        sock.settimeout(PROXY_READ_TIMEOUT)
        lines = [
            f"GET {raw_path} HTTP/1.1",
            f"Host: {upstream_netloc}",
            "Upgrade: websocket",
            "Connection: Upgrade",
        ]
        for name, value in filter_request_headers(headers, upstream_netloc):
            lines.append(f"{name}: {value}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))

        head = b""
        while b"\r\n\r\n" not in head:
            if len(head) > _WS_HANDSHAKE_MAX_BYTES:
                raise ProxyPolicyError(
                    f"upstream WebSocket handshake head exceeded {_WS_HANDSHAKE_MAX_BYTES} bytes"
                )
            chunk = sock.recv(4096)
            if not chunk:
                raise ProxyPolicyError("upstream closed during WebSocket handshake")
            head += chunk
        status_line = head.split(b"\r\n", 1)[0]
        parts = status_line.split(b" ")
        if len(parts) < 2 or parts[1] != b"101":
            raise ProxyPolicyError(
                f"upstream refused WebSocket upgrade: {status_line.decode('latin-1', 'replace')!r}"
            )
        return sock, head
    except BaseException:
        sock.close()
        raise


def pump_sockets(src: socket.socket, dst: socket.socket, closed: threading.Event) -> None:
    """Copy raw bytes ``src → dst`` until EOF/error or ``closed`` is set.

    Pure byte pipe — WebSocket frames pass through untouched (client frames
    stay masked, server frames stay unmasked), so the tunnel is protocol-
    correct without re-framing. The short socket timeout only polls ``closed``
    for deterministic teardown; it is NOT an idle kill (kernels idle legally).
    On exit sets ``closed`` and shuts both sockets down so the peer pump exits.
    """

    try:
        src.settimeout(_PUMP_POLL_SECONDS)
        while not closed.is_set():
            try:
                data = src.recv(_STREAM_CHUNK_BYTES)
            except TimeoutError:
                continue
            except OSError:
                break
            if not data:
                break
            try:
                dst.sendall(data)
            except OSError:
                break
    finally:
        closed.set()
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
