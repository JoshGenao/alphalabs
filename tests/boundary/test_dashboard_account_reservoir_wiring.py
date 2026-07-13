"""L4 boundary — the SRS-UI-003 account + Reservoir panels over real transports.

Boots :class:`atp_runtime.OperatorInterfaceRuntime` on an ephemeral loopback
port, mounts the dashboard with the account + Reservoir providers, and asserts:

* ``GET /dashboard/api/account`` and ``/dashboard/api/reservoir`` return honest
  deferred snapshots over a real TCP socket (``ok:true`` + deferred cells; the
  Reservoir snapshot carries the real SYS-48 window config);
* a bare mount (no account/reservoir provider) does NOT register the routes —
  the panels report their explicit "not mounted" state rather than pretending a
  feed exists;
* a real WebSocket client SUBSCRIBEs to ``ACCOUNT_STATUS`` / ``RESERVOIR_RANKING``
  and receives a live EVENT within the NFR-P2 5-second budget;
* the served assets carry both panels + subscribe to both channels, and the two
  REST routes are GET-only (a mutating verb is not a registered route);
* the production entrypoint (``mount_default_dashboard``) serves both routes and
  registers both publishers.

SRS trace: SRS-UI-003 (account + Reservoir), SYS-43b / SYS-48, SRS-SEC-002 (bind).
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
from atp_dashboard import (
    AccountStatusProvider,
    ReadinessBackedProvider,
    ReservoirRankingProvider,
    mount_dashboard,
    mount_default_dashboard,
)
from atp_dashboard.publisher import DashboardPublisher
from atp_runtime import OperatorInterfaceRuntime
from atp_runtime.ws_frames import OpCode, compute_accept_key, decode_frame

pytestmark = pytest.mark.boundary

_UI003_CHANNELS = ("ACCOUNT_STATUS", "RESERVOIR_RANKING")


@pytest.fixture()
def running_dashboard() -> Iterator[tuple[str, int, DashboardPublisher]]:
    runtime = OperatorInterfaceRuntime()
    # Returned UN-started: the WS test starts the publisher AFTER a client
    # subscribes so the immediate first tick deterministically reaches it.
    publisher = mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        account=AccountStatusProvider(),
        reservoir=ReservoirRankingProvider(),
    )
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port, publisher
    finally:
        publisher.stop()
        runtime.stop()


def _request(host: str, port: int, method: str, path: str) -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, response.getheader("Content-Type") or "", response.read()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# REST poll routes (first paint) — honest deferred snapshots
# --------------------------------------------------------------------------- #


def test_account_route_serves_an_honest_deferred_snapshot(running_dashboard) -> None:
    host, port, _ = running_dashboard
    status, ctype, body = _request(host, port, "GET", "/dashboard/api/account")
    assert status == 200 and ctype.startswith("application/json")
    snap = json.loads(body)
    assert snap["ok"] is True and snap["srs_ref"] == "SRS-UI-003"
    for field in (
        "equity",
        "daily_pnl",
        "cumulative_pnl",
        "margin_usage",
        "buying_power",
        "ib_connection_state",
    ):
        assert snap[field] == {"value": None, "data_source": "deferred:SRS-EXE-006"}


def test_reservoir_route_serves_deferred_ranking_plus_real_window_config(running_dashboard) -> None:
    host, port, _ = running_dashboard
    status, ctype, body = _request(host, port, "GET", "/dashboard/api/reservoir")
    assert status == 200 and ctype.startswith("application/json")
    snap = json.loads(body)
    assert snap["ok"] is True and snap["srs_ref"] == "SRS-UI-003"
    assert snap["allowed_windows"] == [1, 7, 15, 30, 60, 90] and snap["default_window"] == 30
    assert snap["rankings"] == {"value": None, "data_source": "deferred:SRS-RESV-002"}


def test_routes_are_read_only(running_dashboard) -> None:
    host, port, _ = running_dashboard
    for path in ("/dashboard/api/account", "/dashboard/api/reservoir"):
        for method in ("POST", "PUT", "DELETE"):
            assert _request(host, port, method, path)[0] in (404, 405)


def test_bare_mount_does_not_serve_the_routes() -> None:
    runtime = OperatorInterfaceRuntime()
    mount_dashboard(runtime, ReadinessBackedProvider({}))  # no account/reservoir
    for path in ("/dashboard/api/account", "/dashboard/api/reservoir"):
        assert runtime.dispatch_rest("GET", path, b"")[0] == 404


def test_served_assets_carry_both_panels_and_subscribe_to_both_channels(running_dashboard) -> None:
    host, port, _ = running_dashboard
    _, _, index = _request(host, port, "GET", "/dashboard")
    assert b'data-panel="account"' in index and b'data-panel="reservoir"' in index
    assert b'id="resv-window"' in index  # the SYS-48 window control
    _, _, app_js = _request(host, port, "GET", "/dashboard/app.js")
    for channel in _UI003_CHANNELS:
        assert channel.encode() in app_js  # the SPA subscribes to both channels
    # The panels introduce NO /dashboard-namespaced mutation (read-only surface).
    assert b"/dashboard/api/account" in app_js and b"/dashboard/api/reservoir" in app_js


# --------------------------------------------------------------------------- #
# WebSocket refresh — a real client receives EVENTs within the 5 s budget
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


def test_ui003_channels_refresh_within_budget(running_dashboard) -> None:
    host, port, publisher = running_dashboard
    sock = _ws_connect(host, port)
    try:
        _ws_send(sock, {"type": "SUBSCRIBE", "channels": list(_UI003_CHANNELS)})
        time.sleep(0.2)  # let the SUBSCRIBE register before the first publish
        start = time.monotonic()
        publisher.start()

        first_seen: dict[str, float] = {}
        sock.settimeout(4.0)
        buffer = b""
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline and set(first_seen) < set(_UI003_CHANNELS):
            frame, buffer = decode_frame(buffer, require_mask=False)
            if frame is not None:
                if frame.opcode == OpCode.TEXT:
                    message = json.loads(frame.text)
                    if message.get("type") == "EVENT" and message["channel"] in _UI003_CHANNELS:
                        first_seen.setdefault(message["channel"], time.monotonic() - start)
                        assert isinstance(message["data"], dict)
                continue
            try:
                buffer += sock.recv(65536)
            except TimeoutError:
                break

        missing = set(_UI003_CHANNELS) - set(first_seen)
        assert not missing, f"channels never refreshed: {missing}"
        for channel, elapsed in first_seen.items():
            assert elapsed < 5.0, f"{channel} first refresh took {elapsed:.2f}s (>5s NFR-P2 budget)"
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# Production composition (mount_default_dashboard)
# --------------------------------------------------------------------------- #


def test_default_composition_serves_both_routes_and_registers_both_publishers() -> None:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(runtime, {})
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        assert runtime.is_publisher_registered("ACCOUNT_STATUS")
        assert runtime.is_publisher_registered("RESERVOIR_RANKING")
        assert _request(host, port, "GET", "/dashboard/api/account")[0] == 200
        assert _request(host, port, "GET", "/dashboard/api/reservoir")[0] == 200
    finally:
        publisher.stop()
        runtime.stop()
