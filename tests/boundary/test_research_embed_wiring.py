"""L4 boundary — SRS-RES-001 research embed wired through a real socket.

Mounts the dashboard with a research provider onto a live
``OperatorInterfaceRuntime`` and proves, over real TCP against the SAME
listener that serves ``/dashboard`` and ``/ws/v1``:

* the SPA ships the research panel (``data-panel="research"``);
* ``GET /dashboard/api/research`` serves the probe-derived snapshot;
* ``GET /research/lab`` passes the upstream body AND its
  ``Content-Security-Policy: frame-ancestors 'self'`` header through untouched
  (the same-origin embed depends on that header being served, not stripped);
* a WebSocket upgrade under ``/research/`` tunnels to the upstream on the same
  port the dashboard is served from — one origin, no separate service URL
  (SYS-34a / IF-13).
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
from atp_dashboard import (
    RESEARCH_SNAPSHOT_PATH,
    ReadinessBackedProvider,
    ResearchEnvironmentProvider,
    mount_dashboard,
)
from atp_runtime import OperatorInterfaceRuntime

_WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_LAB_HTML = b"<!doctype html><html><body data-jupyter='lab-stub'>lab</body></html>"


class _JupyterishUpstream(BaseHTTPRequestHandler):
    """Serves a lab page with Jupyter's CSP header (the stub research side)."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_LAB_HTML)))
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(_LAB_HTML)

    def log_message(self, *args: object, **kwargs: object) -> None:
        return


def _ws_echo_listener() -> tuple[socket.socket, int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            if b"Upgrade: websocket" not in request and b"upgrade: websocket" not in request:
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                    b"Content-Security-Policy: frame-ancestors 'self'\r\n"
                    b"Content-Type: text/html\r\n\r\n" % len(_LAB_HTML)
                    + _LAB_HTML
                )
                conn.close()
                continue
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
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener, listener.getsockname()[1]


@pytest.fixture()
def embedded_dashboard() -> Iterator[tuple[str, int]]:
    listener, upstream_port = _ws_echo_listener()
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        research=ResearchEnvironmentProvider(f"http://127.0.0.1:{upstream_port}"),
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()
        listener.close()


def _get(host: str, port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def test_spa_ships_the_research_panel(embedded_dashboard) -> None:
    host, port = embedded_dashboard
    status, _, body = _get(host, port, "/dashboard")
    assert status == 200
    assert b'data-panel="research"' in body
    assert b"research-frame" in body


def test_research_snapshot_route_serves_probe_state(embedded_dashboard) -> None:
    host, port = embedded_dashboard
    status, _, body = _get(host, port, RESEARCH_SNAPSHOT_PATH)
    assert status == 200
    snapshot = json.loads(body)
    assert snapshot["configured"] is True
    assert snapshot["upstream_reachable"] is True
    assert snapshot["embed_path"] == "/research/lab"


def test_research_content_and_csp_pass_through_same_origin(embedded_dashboard) -> None:
    host, port = embedded_dashboard
    status, headers, body = _get(host, port, "/research/lab")
    assert status == 200
    assert body == _LAB_HTML
    # Jupyter's frame-ancestors CSP must reach the browser: the same-origin
    # iframe is admitted by 'self' precisely because this content is served
    # from the dashboard's own origin.
    assert headers["content-security-policy"] == "frame-ancestors 'self'"


def test_ws_upgrade_under_research_tunnels_on_the_dashboard_port(embedded_dashboard) -> None:
    host, port = embedded_dashboard
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


def test_unconfigured_mount_registers_no_proxy() -> None:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime, ReadinessBackedProvider({}), research=ResearchEnvironmentProvider(None)
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, _, body = _get(host, port, RESEARCH_SNAPSHOT_PATH)
        assert status == 200
        assert json.loads(body)["configured"] is False
        # No upstream configured -> the /research/ prefix must NOT exist.
        status, _, body = _get(host, port, "/research/lab")
        assert status == 404
    finally:
        publisher.stop()
        runtime.stop()
