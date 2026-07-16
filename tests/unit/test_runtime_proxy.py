"""L1 unit — the operator runtime's reverse-proxy seam (SRS-RES-001 / IF-13).

Exercises ``OperatorInterfaceRuntime.register_proxy_route`` end-to-end against
stub upstreams bound on ``127.0.0.1:0``: verb round-trips, header hygiene in
both directions, the request-smuggling refusals, bounded failure modes (502 /
413 / 400 / 503 — never a hang), registration-time policy refusals, upstream
fixity, and the raw-byte WebSocket tunnel.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from atp_runtime import OperatorInterfaceRuntime, ProxyPolicyError
from atp_runtime.proxy import (
    MAX_PROXY_BODY_BYTES,
    compile_proxy_route,
    match_proxy_route,
)
from atp_runtime.rest_server import is_allowed_bind_host

_WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _EchoUpstream(BaseHTTPRequestHandler):
    """Echoes method/path/headers/body as JSON (the recording stub upstream)."""

    def _echo(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        payload = json.dumps(
            {
                "method": method,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body.decode("utf-8", "replace"),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # Hop-by-hop response header the proxy must strip:
        self.send_header("Keep-Alive", "timeout=5")
        # End-to-end header the proxy must pass through untouched (the
        # Jupyter frame-ancestors case):
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._echo("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._echo("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._echo("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._echo("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._echo("DELETE")

    def log_message(self, *args: object, **kwargs: object) -> None:
        return


@pytest.fixture()
def echo_upstream() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def proxied_runtime(echo_upstream: int) -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    runtime.register_proxy_route("/research/", f"http://127.0.0.1:{echo_upstream}")
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        runtime.stop()


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


# ----- HTTP round-trips ----- #


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_verbs_round_trip_verbatim_path_and_body(proxied_runtime, method: str) -> None:
    host, port = proxied_runtime
    body = b'{"cells": []}' if method in ("POST", "PUT", "PATCH") else None
    status, _, payload = _request(host, port, method, "/research/api/contents/nb.ipynb?type=file", body)
    assert status == 200
    echoed = json.loads(payload)
    assert echoed["method"] == method
    # Upstream fixity: the verbatim path+query arrives; nothing is rewritten.
    assert echoed["path"] == "/research/api/contents/nb.ipynb?type=file"
    if body is not None:
        assert echoed["body"] == body.decode()


def test_bare_prefix_spelling_reaches_upstream(proxied_runtime) -> None:
    host, port = proxied_runtime
    status, _, payload = _request(host, port, "GET", "/research")
    assert status == 200
    assert json.loads(payload)["path"] == "/research"


def test_host_rewritten_and_hop_by_hop_stripped_request_side(
    proxied_runtime, echo_upstream: int
) -> None:
    host, port = proxied_runtime
    status, _, payload = _request(
        host,
        port,
        "GET",
        "/research/lab",
        headers={
            "X-Custom": "carried",
            "Cookie": "sid=1",
            "Origin": f"http://{host}:{port}",
            "Keep-Alive": "timeout=1",
        },
    )
    assert status == 200
    echoed_headers = json.loads(payload)["headers"]
    assert echoed_headers["host"] == f"127.0.0.1:{echo_upstream}"
    assert echoed_headers["x-custom"] == "carried"
    assert echoed_headers["cookie"] == "sid=1"
    # Origin is rewritten to the upstream netloc (Jupyter check_origin sees a
    # self-consistent origin).
    assert echoed_headers["origin"] == f"http://127.0.0.1:{echo_upstream}"
    assert "keep-alive" not in echoed_headers


def test_response_hop_by_hop_stripped_and_csp_passes_through(proxied_runtime) -> None:
    host, port = proxied_runtime
    _, headers, _ = _request(host, port, "GET", "/research/lab")
    assert "keep-alive" not in headers
    # The frame-ancestors CSP must reach the browser untouched: same-origin
    # embedding depends on it being served, not stripped.
    assert headers["content-security-policy"] == "frame-ancestors 'self'"


def test_proxy_does_not_shadow_runtime_surface(proxied_runtime) -> None:
    host, port = proxied_runtime
    status, _, payload = _request(host, port, "GET", "/healthz")
    assert status == 200
    assert json.loads(payload)["status"] == "ok"


# ----- bounded failure modes ----- #


def test_upstream_down_yields_bounded_502(echo_upstream: int) -> None:
    runtime = OperatorInterfaceRuntime()
    # A closed port on loopback: policy-valid, guaranteed refused.
    runtime.register_proxy_route("/research/", "http://127.0.0.1:1")
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        started = time.monotonic()
        status, _, payload = _request(host, port, "GET", "/research/lab")
        elapsed = time.monotonic() - started
        assert status == 502
        assert json.loads(payload)["error"]["category"] == "UPSTREAM_UNAVAILABLE"
        assert elapsed < 8.0  # bounded by the connect deadline, never a hang
    finally:
        runtime.stop()


def test_oversized_body_refused_413_before_forwarding(proxied_runtime) -> None:
    host, port = proxied_runtime
    conn = http.client.HTTPConnection(*proxied_runtime, timeout=10)
    try:
        conn.putrequest("POST", "/research/api/contents")
        conn.putheader("Content-Length", str(MAX_PROXY_BODY_BYTES + 1))
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 413
        assert json.loads(response.read())["error"]["category"] == "PAYLOAD_TOO_LARGE"
    finally:
        conn.close()


def test_chunked_request_body_refused_400(proxied_runtime) -> None:
    host, port = proxied_runtime
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(
            b"POST /research/api/contents HTTP/1.1\r\n"
            b"Host: x\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
        )
        head = sock.recv(4096)
    assert b" 400 " in head.split(b"\r\n", 1)[0]


def test_malformed_content_length_refused_400(proxied_runtime) -> None:
    host, port = proxied_runtime
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(
            b"POST /research/api/contents HTTP/1.1\r\nHost: x\r\nContent-Length: nan\r\n\r\n"
        )
        head = sock.recv(4096)
    assert b" 400 " in head.split(b"\r\n", 1)[0]


def test_dotdot_path_segment_refused_400(proxied_runtime) -> None:
    host, port = proxied_runtime
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(b"GET /research/../api/v1/kill-switch HTTP/1.1\r\nHost: x\r\n\r\n")
        head = sock.recv(4096)
    assert b" 400 " in head.split(b"\r\n", 1)[0]


# ----- streaming ----- #


def test_large_known_length_response_streams_through_intact() -> None:
    blob = bytes(range(256)) * (5 * 4096)  # ~5 MiB

    class _Blob(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def log_message(self, *args: object, **kwargs: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Blob)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    runtime = OperatorInterfaceRuntime()
    runtime.register_proxy_route("/research/", f"http://127.0.0.1:{server.server_address[1]}")
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, headers, payload = _request(host, port, "GET", "/research/files/blob.bin")
        assert status == 200
        assert headers["content-length"] == str(len(blob))
        assert payload == blob
    finally:
        runtime.stop()
        server.shutdown()
        server.server_close()


def test_chunked_upstream_response_buffered_with_computed_length() -> None:
    def upstream(server_sock: socket.socket) -> None:
        conn, _ = server_sock.accept()
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        )
        conn.close()

    server_sock = socket.socket()
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    threading.Thread(target=upstream, args=(server_sock,), daemon=True).start()
    runtime = OperatorInterfaceRuntime()
    runtime.register_proxy_route(
        "/research/", f"http://127.0.0.1:{server_sock.getsockname()[1]}"
    )
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, headers, payload = _request(host, port, "GET", "/research/api/status")
        assert status == 200
        assert payload == b"hello world"
        assert headers["content-length"] == str(len(b"hello world"))
        assert "transfer-encoding" not in headers
    finally:
        runtime.stop()
        server_sock.close()


# ----- registration policy ----- #


@pytest.mark.parametrize(
    ("prefix", "upstream"),
    [
        ("/", "http://127.0.0.1:9"),  # root
        ("/api/", "http://127.0.0.1:9"),  # the REST contract
        ("/api/v1/nested/", "http://127.0.0.1:9"),  # enclosed by /api/
        ("/dashboard/", "http://127.0.0.1:9"),  # the read-only dashboard
        ("/dashboard/jupyter/", "http://127.0.0.1:9"),  # enclosed by /dashboard/
        ("/ws/v1/", "http://127.0.0.1:9"),  # the runtime WS path
        ("/healthz/", "http://127.0.0.1:9"),  # a runtime meta path
        ("/research/", "https://127.0.0.1:9"),  # non-http scheme
        ("/research/", "http://127.0.0.1:9/base"),  # upstream carries a path
        ("/research/", "http://8.8.8.8:80"),  # public literal upstream
        ("/research/", "http://[2001:db8::1]:80"),  # public IPv6 literal
        ("/../research/", "http://127.0.0.1:9"),  # traversal in the prefix
    ],
)
def test_registration_refusals(prefix: str, upstream: str) -> None:
    runtime = OperatorInterfaceRuntime()
    with pytest.raises(ProxyPolicyError):
        runtime.register_proxy_route(prefix, upstream)


def test_overlapping_proxy_prefixes_refused() -> None:
    runtime = OperatorInterfaceRuntime()
    runtime.register_proxy_route("/research/", "http://127.0.0.1:9")
    with pytest.raises(ProxyPolicyError):
        runtime.register_proxy_route("/research/lab/", "http://127.0.0.1:9")
    with pytest.raises(ProxyPolicyError):
        runtime.register_proxy_route("/research", "http://127.0.0.1:9")  # same after normalising
    # Overlap is segment-boundary-based: /res/ does NOT shadow /research/.
    runtime.register_proxy_route("/res", "http://127.0.0.1:9")
    runtime.register_proxy_route("/other/", "http://127.0.0.1:9")


def test_registration_after_start_refused() -> None:
    runtime = OperatorInterfaceRuntime()
    runtime.start(host="127.0.0.1", port=0)
    try:
        with pytest.raises(ProxyPolicyError):
            runtime.register_proxy_route("/research/", "http://127.0.0.1:9")
    finally:
        runtime.stop()


def test_registered_meta_and_asset_paths_are_reserved() -> None:
    runtime = OperatorInterfaceRuntime()
    runtime.register_meta_route("/custom/snapshot", lambda: {})
    runtime.register_asset_routes({"/panel": ("text/html", b"<html></html>")})
    with pytest.raises(ProxyPolicyError):
        runtime.register_proxy_route("/custom/snapshot/", "http://127.0.0.1:9")
    with pytest.raises(ProxyPolicyError):
        runtime.register_proxy_route("/panel/", "http://127.0.0.1:9")


def test_match_proxy_route_longest_prefix_wins() -> None:
    outer = compile_proxy_route(
        "/a/", "http://127.0.0.1:1", reserved=[], allow_host=is_allowed_bind_host
    )
    inner = compile_proxy_route(
        "/a/b/", "http://127.0.0.1:2", reserved=[], allow_host=is_allowed_bind_host
    )
    routes = {outer.prefix: outer, inner.prefix: inner}
    assert match_proxy_route(routes, "/a/b/c").port == 2
    assert match_proxy_route(routes, "/a/x").port == 1
    assert match_proxy_route(routes, "/a").port == 1
    assert match_proxy_route(routes, "/ax") is None


# ----- WebSocket tunnel ----- #


def _ws_echo_upstream() -> tuple[socket.socket, int]:
    """A raw upstream that completes the RFC 6455 handshake then echoes one
    masked client text frame back unmasked."""

    server_sock = socket.socket()
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)

    def serve() -> None:
        try:
            conn, _ = server_sock.accept()
        except OSError:  # fixture teardown closed the listener before a connect
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
        conn.recv(1)  # linger until the tunnel tears down

    threading.Thread(target=serve, daemon=True).start()
    return server_sock, server_sock.getsockname()[1]


def _ws_client_handshake(host: str, port: int, path: str) -> tuple[socket.socket, bytes]:
    sock = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(b"0123456789abcdef")
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n".encode()
        + b"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: "
        + key
        + b"\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    return sock, head


def test_ws_tunnel_round_trips_masked_frame() -> None:
    server_sock, upstream_port = _ws_echo_upstream()
    runtime = OperatorInterfaceRuntime()
    runtime.register_proxy_route("/research/", f"http://127.0.0.1:{upstream_port}")
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        client, head = _ws_client_handshake(host, port, "/research/api/kernels/k/channels")
        assert b" 101 " in head.split(b"\r\n", 1)[0] + b" "
        mask = b"\x01\x02\x03\x04"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(b"hi"))
        client.sendall(bytes([0x81, 0x80 | 2]) + mask + masked)
        echoed = client.recv(16)
        assert echoed[:2] == bytes([0x81, 2])
        assert echoed[2:4] == b"hi"
        client.close()
    finally:
        runtime.stop()
        server_sock.close()


def test_ws_upgrade_to_non_ws_upstream_yields_502(echo_upstream: int) -> None:
    runtime = OperatorInterfaceRuntime()
    runtime.register_proxy_route("/research/", f"http://127.0.0.1:{echo_upstream}")
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        client, head = _ws_client_handshake(host, port, "/research/api/kernels/k/channels")
        assert b" 502 " in head.split(b"\r\n", 1)[0] + b" "
        client.close()
    finally:
        runtime.stop()


def test_ws_tunnel_slots_exhausted_yields_503() -> None:
    server_sock, upstream_port = _ws_echo_upstream()
    runtime = OperatorInterfaceRuntime()
    runtime.register_proxy_route("/research/", f"http://127.0.0.1:{upstream_port}")
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        # Drain the slot semaphore so the next upgrade is refused honestly.
        server = runtime._server  # noqa: SLF001 - deliberate white-box slot drain
        assert server is not None
        drained = 0
        while server.proxy_ws_slots.acquire(blocking=False):
            drained += 1
        try:
            client, head = _ws_client_handshake(host, port, "/research/api/kernels/k/channels")
            assert b" 503 " in head.split(b"\r\n", 1)[0] + b" "
            client.close()
        finally:
            for _ in range(drained):
                server.proxy_ws_slots.release()
    finally:
        runtime.stop()
        server_sock.close()
