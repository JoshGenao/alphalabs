"""L4 boundary — the SRS-UI-001 dashboard mounted over real transports.

Boots :class:`atp_runtime.OperatorInterfaceRuntime` on an ephemeral loopback port
(``127.0.0.1:0`` — parallel-safe, never an IB or the fixed dashboard port), mounts
the dashboard, and exercises it end-to-end:

* the static assets are served with correct content types over a real TCP socket;
* the JSON system snapshot returns the four metric groups' scaffolding;
* a real WebSocket client SUBSCRIBEs and receives a live EVENT within the NFR-P2
  5-second budget (asserted on a fast 1 s channel, never the 5 s METRICS boundary).

SRS trace: SRS-UI-001 (dashboard), SYS-36 / NFR-P2 (≤5 s refresh), SRS-SEC-002
(loopback bind).
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import time
from collections.abc import Iterator

import pytest
from atp_dashboard import OWNED_CHANNELS, ReadinessBackedProvider, mount_dashboard
from atp_dashboard.publisher import DashboardPublisher
from atp_runtime import OperatorInterfaceRuntime
from atp_runtime.ws_frames import OpCode, compute_accept_key, decode_frame

pytestmark = pytest.mark.boundary


@pytest.fixture()
def running_dashboard() -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    publisher: DashboardPublisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


def _get(host: str, port: int, path: str) -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.getheader("Content-Type") or "", response.read()
    finally:
        conn.close()


def test_static_assets_are_served_with_correct_content_types(running_dashboard) -> None:
    host, port = running_dashboard
    status, ctype, body = _get(host, port, "/dashboard")
    assert status == 200 and ctype.startswith("text/html")
    assert b'id="body-pnl"' in body and b"MISSION" in body

    status, ctype, _ = _get(host, port, "/dashboard/styles.css")
    assert status == 200 and ctype.startswith("text/css")

    status, ctype, body = _get(host, port, "/dashboard/app.js")
    assert status == 200 and ctype.startswith("application/javascript")
    assert b"/ws/v1" in body  # the SPA subscribes over the real WS path


def test_system_snapshot_returns_the_four_metric_groups(running_dashboard) -> None:
    host, port = running_dashboard
    status, ctype, body = _get(host, port, "/dashboard/api/system")
    assert status == 200 and ctype.startswith("application/json")
    snap = json.loads(body)
    assert snap["refresh_budget_ms"] == 5_000  # NFR-P2
    assert snap["health"]["data_source"] == "live"
    assert "order_signal_to_ack_p95_ms" in snap["latency"]
    assert snap["srs_ref"] == "SRS-UI-001"


def test_healthz_still_served_after_mounting_the_dashboard(running_dashboard) -> None:
    host, port = running_dashboard
    status, _, body = _get(host, port, "/healthz")
    assert status == 200 and json.loads(body)["status"] == "ok"


# --------------------------------------------------------------------------- #
# WebSocket refresh — a real client receives a live EVENT within 5 s
# --------------------------------------------------------------------------- #


def _ws_connect(host: str, port: int) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            "GET /ws/v1 HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    header = sock.recv(4096).decode()
    assert "101" in header and compute_accept_key(key) in header, header
    return sock


def _ws_send(sock: socket.socket, message: dict) -> None:
    payload = json.dumps(message).encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + masked)


def test_websocket_subscribe_receives_a_live_event_within_5s(running_dashboard) -> None:
    host, port = running_dashboard
    sock = _ws_connect(host, port)
    try:
        _ws_send(sock, {"type": "SUBSCRIBE", "channels": list(OWNED_CHANNELS)})
        deadline = time.monotonic() + 5.0
        start = time.monotonic()
        sock.settimeout(5.0)
        buffer = b""
        event: dict | None = None
        while time.monotonic() < deadline:
            frame, buffer = decode_frame(buffer, require_mask=False)
            if frame is not None:
                if frame.opcode == OpCode.TEXT:
                    message = json.loads(frame.text)
                    if message.get("type") == "EVENT":
                        event = message
                        break
                continue
            try:
                buffer += sock.recv(65536)
            except TimeoutError:
                break
        elapsed = time.monotonic() - start
        assert event is not None, "no EVENT within the 5s refresh budget"
        assert elapsed < 5.0, f"refresh took {elapsed:.2f}s (NFR-P2 budget is 5s)"
        assert event["channel"] in OWNED_CHANNELS
        assert isinstance(event["data"], dict)
    finally:
        sock.close()
