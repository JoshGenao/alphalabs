"""L4 boundary — the operator-interface runtime wired over real transports.

Boots :class:`atp_runtime.OperatorInterfaceRuntime` on an ephemeral loopback
port (``127.0.0.1:0`` — never an IB port, never the fixed dashboard port, so
it is parallel-safe) and exercises the three operator surfaces end-to-end over
their real transports:

* REST over a real TCP socket (``http.client``) — runtime-owned 200, the
  confirmation 428 guard, and the deferred 501 envelope;
* WebSocket over a real socket — RFC 6455 handshake, SUBSCRIBE → ACK, a
  publisher fan-out → EVENT, and HEARTBEAT_PING → PONG;
* CLI dispatch through the registry — real exit codes.

SRS trace: SRS-API-001 (operator interface), SRS-SEC-002 (loopback bind),
UI-4 / SRS-SAFE-001 (confirmation guard).
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import socket
import threading
import time
from collections.abc import Iterator

import pytest
from atp_runtime import OperatorInterfaceRuntime
from atp_runtime.rest_server import _WS_OUTBOX_MAXSIZE
from atp_runtime.ws_frames import OpCode, compute_accept_key, decode_frame

pytestmark = pytest.mark.boundary


class _WsReader:
    """Buffered WebSocket frame reader for a client socket (server frames)."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""

    def read(self, timeout: float = 3.0) -> dict:
        self._sock.settimeout(timeout)
        while True:
            frame, self._buf = decode_frame(self._buf, require_mask=False)
            if frame is not None:
                if frame.opcode == OpCode.TEXT:
                    return json.loads(frame.text)
                return {"type": f"OPCODE_{int(frame.opcode)}"}
            chunk = self._sock.recv(65536)
            if not chunk:
                raise EOFError("socket closed")
            self._buf += chunk


@pytest.fixture()
def running_runtime() -> Iterator[tuple[OperatorInterfaceRuntime, str, int]]:
    runtime = OperatorInterfaceRuntime()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield runtime, host, port
    finally:
        runtime.stop()


def _get(host: str, port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        conn.close()


def _request(host: str, port: int, method: str, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        conn.close()


def test_http_runtime_owned_status_is_served_over_a_real_socket(running_runtime):
    _, host, port = running_runtime
    status, body = _get(host, port, "/api/v1/system/status")
    assert status == 200
    assert body["ready"] is False  # domain workflows deferred
    assert len(body["workflows"]) == 8
    assert body["runtime"]["bind_host"] == "127.0.0.1"


def test_http_healthz_and_openapi_meta_endpoints(running_runtime):
    _, host, port = running_runtime
    assert _get(host, port, "/healthz")[1]["status"] == "ok"
    openapi = _get(host, port, "/openapi.json")[1]
    assert openapi["openapi"].startswith("3.")
    assert "/api/v1/kill-switch" in openapi["paths"]


def test_http_confirmation_guard_blocks_before_dispatch(running_runtime):
    _, host, port = running_runtime
    status, body = _request(host, port, "POST", "/api/v1/kill-switch")
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"


def test_http_confirmed_domain_op_returns_deferred_envelope(running_runtime):
    _, host, port = running_runtime
    status, body = _request(host, port, "POST", "/api/v1/kill-switch?confirm=true")
    assert status == 501
    assert body["error"]["type"] == "HANDLER_DEFERRED"
    assert body["error"]["detail"]["owner"] == "SRS-SAFE-001"


def test_http_structured_404_and_405(running_runtime):
    _, host, port = running_runtime
    assert _request(host, port, "GET", "/api/v1/does-not-exist")[0] == 404
    assert _request(host, port, "DELETE", "/api/v1/kill-switch")[0] == 405


# --------------------------------------------------------------------------- #
# WebSocket round-trip
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


def _ws_recv(sock: socket.socket) -> dict:
    frame, _ = decode_frame(sock.recv(65536), require_mask=False)
    assert frame is not None and frame.opcode in (OpCode.TEXT, OpCode.PONG)
    return json.loads(frame.text) if frame.opcode == OpCode.TEXT else {"type": "HEARTBEAT_PONG"}


def test_websocket_subscribe_publish_heartbeat(running_runtime):
    runtime, host, port = running_runtime
    sock = _ws_connect(host, port)
    try:
        _ws_send(sock, {"type": "SUBSCRIBE", "channels": ["LOGS", "NOPE"]})
        time.sleep(0.2)
        ack = _ws_recv(sock)
        assert ack["type"] == "ACK"
        assert ack["subscribed"] == ["LOGS"] and ack["rejected"] == ["NOPE"]

        assert runtime.publish("LOGS", {"message": "hello-operator"}) == 1
        time.sleep(0.2)
        event = _ws_recv(sock)
        assert event["type"] == "EVENT"
        assert event["channel"] == "LOGS"
        # The EVENT envelope carries the body under `data` (declared AsyncAPI shape).
        assert event["data"] == {"message": "hello-operator"}

        _ws_send(sock, {"type": "HEARTBEAT_PING"})
        time.sleep(0.2)
        assert _ws_recv(sock)["type"] == "HEARTBEAT_PONG"
    finally:
        sock.close()


def test_http_oversized_body_is_refused_before_read(running_runtime):
    _, host, port = running_runtime
    sock = socket.create_connection((host, port), timeout=5)
    try:
        # Declare an oversized body but send no body bytes: the server refuses on
        # Content-Length (413) before reading, so this is deterministic.
        oversized = (1 << 20) + 4096
        sock.sendall(
            (
                f"POST /api/v1/kill-switch?confirm=1 HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {oversized}\r\n\r\n"
            ).encode()
        )
        sock.settimeout(5)
        # The server sets Connection: close on refusal, so read to EOF rather
        # than assuming the whole response lands in one TCP segment.
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        response = b"".join(chunks).decode("utf-8", "replace")
        assert response.startswith("HTTP/1.1 413"), response
        assert "PAYLOAD_TOO_LARGE" in response
    finally:
        sock.close()


def test_websocket_slow_consumer_does_not_block_fanout(running_runtime):
    runtime, host, port = running_runtime
    slow = _ws_connect(host, port)  # subscribes, then never reads
    fast = _ws_connect(host, port)  # subscribes and keeps reading
    try:
        _ws_send(slow, {"type": "SUBSCRIBE", "channels": ["LOGS"]})
        _ws_send(fast, {"type": "SUBSCRIBE", "channels": ["LOGS"]})
        fast_reader = _WsReader(fast)
        assert fast_reader.read()["type"] == "ACK"
        time.sleep(0.2)  # let `slow` register before the burst

        # A burst larger than one outbox must complete promptly — proving the
        # stalled `slow` consumer does not block publisher fan-out.
        start = time.monotonic()
        for i in range(_WS_OUTBOX_MAXSIZE + 100):
            runtime.publish("LOGS", {"i": i})
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"fan-out blocked {elapsed:.1f}s by a slow consumer"

        # The fast client still receives events.
        saw_event = False
        for _ in range(_WS_OUTBOX_MAXSIZE + 100):
            if fast_reader.read()["type"] == "EVENT":
                saw_event = True
                break
        assert saw_event
    finally:
        slow.close()
        fast.close()


def test_websocket_writer_thread_terminates_after_full_outbox_close(running_runtime):
    runtime, host, port = running_runtime
    before = {t for t in threading.enumerate() if t.name == "atp-ws-writer"}

    slow = _ws_connect(host, port)
    _ws_send(slow, {"type": "SUBSCRIBE", "channels": ["LOGS"]})
    time.sleep(0.2)
    # Fill the slow consumer's bounded outbox, then close the client.
    for i in range(_WS_OUTBOX_MAXSIZE + 100):
        runtime.publish("LOGS", {"i": i})
    slow.close()

    # The connection's writer thread must terminate — no leaked daemon thread.
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        new_alive = {
            t for t in threading.enumerate() if t.name == "atp-ws-writer" and t.is_alive()
        } - before
        if not new_alive:
            break
        time.sleep(0.1)
    leaked = {
        t for t in threading.enumerate() if t.name == "atp-ws-writer" and t.is_alive()
    } - before
    assert not leaked, "WebSocket writer thread leaked after a full-outbox close"


def test_websocket_fragmented_frame_is_closed(running_runtime):
    _, host, port = running_runtime
    sock = _ws_connect(host, port)
    try:
        # A FIN=0 (fragmented) TEXT frame is a protocol violation for the
        # single-frame control protocol → the server replies with a CLOSE.
        payload = b'{"type": "SUBSCRIBE"}'
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes([0x01, 0x80 | len(payload)]) + mask + masked)  # FIN=0, opcode=TEXT
        time.sleep(0.2)
        frame, _ = decode_frame(sock.recv(4096), require_mask=False)
        assert frame is not None and frame.opcode == OpCode.CLOSE
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# CLI dispatch
# --------------------------------------------------------------------------- #


def _cli(runtime: OperatorInterfaceRuntime, argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    code = runtime.cli_dispatcher().dispatch(argv, stdout=buffer)
    return code, buffer.getvalue()


def test_cli_runtime_owned_commands_return_real_data():
    runtime = OperatorInterfaceRuntime()
    code, out = _cli(runtime, ["admin", "version", "--json"])
    assert code == 0
    assert json.loads(out)["component"] == "atp-operator-interface-runtime"
    assert _cli(runtime, ["admin", "config", "--json"])[0] == 0
    # `readiness check` returns real status data but exits NOT_READY (5) because
    # the runtime reports ready=false while domain workflows are deferred.
    code, out = _cli(runtime, ["readiness", "check", "--json"])
    assert code == 5
    assert json.loads(out)["ready"] is False


def test_start_is_not_reentrant_and_stop_allows_restart():
    runtime = OperatorInterfaceRuntime()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        # A second start must not leak a listener — it fails fast.
        with pytest.raises(RuntimeError):
            runtime.start(host="127.0.0.1", port=0)
        # The first server is still the live one.
        assert runtime.bound_address() == (host, port)
    finally:
        runtime.stop()

    # After stop() the handle is cleared and the old listener is gone.
    with pytest.raises(RuntimeError):
        runtime.bound_address()
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex((host, port)) != 0, "stopped server is still accepting connections"

    # A fresh start works (no leaked state blocking re-bind).
    host2, port2 = runtime.start(host="127.0.0.1", port=0)
    try:
        assert _get(host2, port2, "/healthz")[1]["status"] == "ok"
    finally:
        runtime.stop()


def test_cli_confirmation_guard_and_deferral():
    runtime = OperatorInterfaceRuntime()
    # No --confirm: exit 3 (CONFIRMATION_REQUIRED), never dispatched.
    assert _cli(runtime, ["kill-switch", "activate"])[0] == 3
    # With --confirm: exit 64 (NOT_IMPLEMENTED), structured deferral.
    code, out = _cli(runtime, ["kill-switch", "activate", "--confirm", "--json"])
    assert code == 64
    assert json.loads(out)["error"]["detail"]["owner"] == "SRS-SAFE-001"
    # A non-confirmation deferred command also reports NOT_IMPLEMENTED.
    assert _cli(runtime, ["strategy", "list", "--json"])[0] == 64
