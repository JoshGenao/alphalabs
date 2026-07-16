"""L7 domain — the research-embed proxy must never become a control plane.

SRS-RES-001 embeds the Jupyter research environment behind the operator
runtime's reverse-proxy seam. The safety invariants anchored here:

* **One-way boundary (SRS-SEC-004).** Everything under a proxy prefix goes to
  the fixed upstream or fails closed — nothing under it can dispatch into the
  operator REST surface (kill switch, live designation, Hot-Swap). A ``..``
  spelling is refused outright; the kill-switch confirmation guard (UI-4 /
  SRS-SAFE-001) is unchanged by mounting the proxy.
* **Structural shadowing refusal.** A proxy prefix that would enclose (or be
  enclosed by) ``/api/``, ``/dashboard/``, or the WS path is refused at
  registration, so the read-only ``/dashboard`` invariant cannot be bypassed
  by a future registration mistake.
* **Private upstreams only.** A public upstream is refused at registration
  (literal) and at connect time (DNS that resolves publicly) — the request is
  never forwarded.
* **Bind policy unchanged (SRS-SEC-002).** Registering a proxy does not relax
  the loopback/RFC-1918 bind refusal.
* **Independence (SYS-34c).** The research surface round-trips — HTTP and the
  WebSocket kernel tunnel — on a runtime with ZERO strategy or backtest
  handlers registered (kill switch and backtest launch still answer 501
  deferred), i.e. the research environment runs with no live strategy, no
  paper strategy, and no backtest engine present.

SRS trace: SRS-RES-001 / IF-13, SRS-SEC-004 (one-way boundary), SRS-SEC-002
(bind + upstream address policy), UI-4 / SRS-SAFE-001 (confirmation guard),
SyRS SYS-34c (independence).
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from atp_dashboard import ReadinessBackedProvider, mount_dashboard
from atp_runtime import OperatorInterfaceRuntime, ProxyPolicyError
from atp_runtime.errors import BindPolicyError

pytestmark = [pytest.mark.domain, pytest.mark.safety]

_WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _RecordingUpstream(BaseHTTPRequestHandler):
    """Records every request path + whether operator auth crossed (the would-be
    Jupyter side)."""

    seen: list[str] = []
    saw_authorization: list[str] = []
    saw_cookies: list[str] = []

    def _record(self, method: str) -> None:
        type(self).seen.append(f"{method} {self.path}")
        if self.headers.get("Authorization") is not None:
            type(self).saw_authorization.append(f"{method} {self.path}")
        cookie = self.headers.get("Cookie")
        if cookie is not None:
            type(self).saw_cookies.append(cookie)
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._record("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._record("POST")

    def log_message(self, *args: object, **kwargs: object) -> None:
        return


@pytest.fixture()
def recording_upstream() -> Iterator[int]:
    _RecordingUpstream.seen = []
    _RecordingUpstream.saw_authorization = []
    _RecordingUpstream.saw_cookies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def embedded_runtime(recording_upstream: int) -> Iterator[tuple[str, int]]:
    """A dashboard-mounted runtime with the research proxy registered and NO
    strategy/backtest handlers — the SYS-34c independence posture."""

    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    runtime.register_proxy_route("/research/", f"http://127.0.0.1:{recording_upstream}")
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


def _request(
    host: str, port: int, method: str, path: str, body: bytes | None = None
) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body)
        response = conn.getresponse()
        raw = response.read() or b"{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        return response.status, parsed
    finally:
        conn.close()


def _raw_request(host: str, port: int, request: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(request)
        return sock.recv(4096)


# ----- one-way boundary ----- #


def test_proxied_paths_never_dispatch_into_the_operator_surface(embedded_runtime) -> None:
    host, port = embedded_runtime
    # A control-shaped POST under the prefix goes UPSTREAM (recorded by the
    # stub), not into the runtime's registry — the proxy only ever responds.
    status, _ = _request(host, port, "POST", "/research/api/kill-switch")
    assert status == 200
    assert "POST /research/api/kill-switch" in _RecordingUpstream.seen
    # The REAL kill switch is untouched: still confirmation-guarded (428).
    status, body = _request(host, port, "POST", "/api/v1/kill-switch")
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"


def test_dotdot_spelling_is_refused_not_forwarded_not_dispatched(embedded_runtime) -> None:
    host, port = embedded_runtime
    before = list(_RecordingUpstream.seen)
    head = _raw_request(
        host, port, b"POST /research/../api/v1/kill-switch HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    assert b" 400 " in head.split(b"\r\n", 1)[0]
    assert _RecordingUpstream.seen == before  # never reached the upstream


def test_dashboard_read_only_invariant_holds_with_proxy_mounted(embedded_runtime) -> None:
    host, port = embedded_runtime
    assert _request(host, port, "POST", "/dashboard")[0] in (404, 405)
    assert _request(host, port, "POST", "/dashboard/api/system")[0] in (404, 405)
    assert _request(host, port, "PUT", "/dashboard/api/system")[0] in (404, 405)
    assert _request(host, port, "DELETE", "/dashboard")[0] in (404, 405)


def test_mutating_routes_still_confirmation_guarded_with_embed(embedded_runtime) -> None:
    """The browser-side residual mitigation that DOES hold (see SECURITY.md
    § "Residual risk — OPERATOR SIGN-OFF GATE"): with the same-origin embed
    mounted, an UNCONFIRMED call to an operator-mutating route is still refused
    (428). The same-origin vector — notebook JS minting ``confirm=true`` in the
    operator's own browser — is the documented, operator-signed-off residual;
    the enforced SEC-004 boundary is the credential-less, execution-unroutable
    container (proven by tests/domain/test_jupyter_credential_isolation.py and
    the static jupyter_isolation_check), not the browser session."""

    host, port = embedded_runtime
    status, body = _request(host, port, "POST", "/api/v1/kill-switch")
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"


def test_operator_authorization_never_reaches_the_research_upstream(embedded_runtime) -> None:
    """Operator-scoped ``Authorization`` is stripped before the upstream leg
    (Codex finding #2): the token-less Jupyter service never receives dashboard
    auth material (SRS-SEC-004 one-way boundary). The recording upstream sees
    the request but not the header."""

    host, port = embedded_runtime
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("GET", "/research/probe", headers={"Authorization": "Bearer operator-token"})
        response = conn.getresponse()
        response.read()
        assert response.status == 200
    finally:
        conn.close()
    # The upstream recorded the request but the Authorization header never crossed.
    assert any(entry.endswith("/research/probe") for entry in _RecordingUpstream.seen)
    assert _RecordingUpstream.saw_authorization == []


def test_operator_session_cookie_never_reaches_the_research_upstream(embedded_runtime) -> None:
    """A dashboard/operator session cookie is stripped before the upstream leg
    over BOTH the HTTP and WebSocket proxy paths (they share the header filter);
    only Jupyter-owned cookies (``_xsrf`` / ``username-*``) cross the SRS-SEC-004
    boundary. Codex R2 finding."""

    host, port = embedded_runtime
    # Includes a Jupyter-RESEMBLING name (username-operator) that a broad prefix
    # allow-list would have leaked (Codex R3): only the exact reserved _xsrf crosses.
    mixed = "operator_session=secret; username-operator=hijack; _xsrf=jt"

    # HTTP path
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("GET", "/research/probe", headers={"Cookie": mixed})
        conn.getresponse().read()
    finally:
        conn.close()

    # WebSocket path (reuses the same header filter)
    client = socket.create_connection((host, port), timeout=10)
    client.sendall(
        b"GET /research/api/kernels/k/channels HTTP/1.1\r\nHost: x\r\n"
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Key: " + base64.b64encode(b"0123456789abcdef") + b"\r\n"
        b"Sec-WebSocket-Version: 13\r\nCookie: " + mixed.encode() + b"\r\n\r\n"
    )
    try:
        client.recv(256)
    finally:
        client.close()

    # Whatever cookies the upstream saw, NONE carried an operator cookie — not
    # the plain session cookie nor the Jupyter-resembling username- one.
    assert _RecordingUpstream.saw_cookies, "upstream should have seen at least the _xsrf cookie"
    for cookie in _RecordingUpstream.saw_cookies:
        assert "operator_session" not in cookie
        assert "username-operator" not in cookie
        assert cookie.strip() == "_xsrf=jt"


def test_control_surface_prefixes_cannot_be_proxied() -> None:
    runtime = OperatorInterfaceRuntime()
    for prefix in ("/api/", "/api/v1/", "/dashboard/", "/dashboard/jupyter/", "/ws/v1/", "/"):
        with pytest.raises(ProxyPolicyError):
            runtime.register_proxy_route(prefix, "http://127.0.0.1:9")


# ----- upstream address policy ----- #


def test_public_literal_upstream_refused_at_registration() -> None:
    runtime = OperatorInterfaceRuntime()
    with pytest.raises(ProxyPolicyError):
        runtime.register_proxy_route("/research/", "http://8.8.8.8:80")


def test_public_dns_upstream_refused_at_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def selective(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host == "research-upstream.invalid":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", selective)
    runtime = OperatorInterfaceRuntime()
    # A DNS name passes registration (unresolvable there) and MUST be refused
    # at connect time when it resolves publicly — fail closed, nothing sent.
    runtime.register_proxy_route("/research/", "http://research-upstream.invalid:80")
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, body = _request(host, port, "GET", "/research/lab")
        assert status == 502
        assert "non-private" in body["error"]["message"]
    finally:
        runtime.stop()


def test_bind_policy_unchanged_with_proxy_registered() -> None:
    runtime = OperatorInterfaceRuntime()
    runtime.register_proxy_route("/research/", "http://127.0.0.1:9")
    with pytest.raises(BindPolicyError):
        runtime.start(host="8.8.8.8", port=0)


# ----- SYS-34c independence ----- #


def test_research_surface_serves_with_zero_strategies_and_no_backtest_engine(
    embedded_runtime,
) -> None:
    host, port = embedded_runtime
    # No strategy/backtest handler is registered on this runtime: the operator
    # control routes answer 501 deferred...
    status, body = _request(host, port, "POST", "/api/v1/backtests", body=b"{}")
    assert status == 501
    assert body["error"]["category"] == "NOT_IMPLEMENTED"
    # ...while the research environment round-trips regardless (SYS-34c: the
    # research environment operates independently of the backtest engine and
    # of any live/paper strategy).
    status, body = _request(host, port, "GET", "/research/lab")
    assert status == 200
    assert body == {"ok": True}


def test_ws_kernel_tunnel_works_with_zero_strategies() -> None:
    # Raw WS echo upstream (the kernel-channel shape) — independence means the
    # tunnel needs no strategy engine, no backtest engine, no publishers.
    server_sock = socket.socket()
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)

    def serve() -> None:
        try:
            conn, _ = server_sock.accept()
        except OSError:
            return
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        key = next(
            line.split(b": ", 1)[1]
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key")
        )
        accept = base64.b64encode(hashlib.sha1(key + _WS_MAGIC).digest())
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        header = conn.recv(2)
        length = header[1] & 0x7F
        mask = conn.recv(4)
        masked = bytearray(conn.recv(length))
        for i in range(length):
            masked[i] ^= mask[i % 4]
        conn.sendall(bytes([0x81, length]) + bytes(masked))
        conn.recv(1)

    threading.Thread(target=serve, daemon=True).start()
    runtime = OperatorInterfaceRuntime()
    runtime.register_proxy_route("/research/", f"http://127.0.0.1:{server_sock.getsockname()[1]}")
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        client = socket.create_connection((host, port), timeout=10)
        client.sendall(
            b"GET /research/api/kernels/k/channels HTTP/1.1\r\nHost: x\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: " + base64.b64encode(b"0123456789abcdef") + b"\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                break
            head += chunk
        assert b" 101 " in head.split(b"\r\n", 1)[0] + b" "
        mask = b"\x01\x02\x03\x04"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(b"ok"))
        client.sendall(bytes([0x81, 0x80 | 2]) + mask + masked)
        echoed = client.recv(16)
        assert echoed[:2] == bytes([0x81, 2])
        assert echoed[2:4] == b"ok"
        client.close()
    finally:
        runtime.stop()
        server_sock.close()
