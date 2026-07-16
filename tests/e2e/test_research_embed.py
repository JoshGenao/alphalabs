"""L6 e2e — SRS-RES-001: real JupyterLab embedded in the dashboard.

The browser-automation leg of the SRS-RES-001 acceptance evidence (Step 2 /
Step 3): a REAL JupyterLab server (launched locally with
``--ServerApp.base_url=/research/``, token auth disabled — network locality is
the auth boundary, matching the deployed compose posture) is reverse-proxied
by the operator runtime at the same-origin ``/research/`` prefix, and a
headless browser proves:

* the dashboard's Research panel reports the upstream reachable and, on
  demand, renders JupyterLab inside the same-origin iframe — "reachable from
  the dashboard without navigating to a separate service URL" (SYS-34a /
  IF-13; Jupyter's default ``frame-ancestors 'self'`` admits the embed
  precisely because the content is served from the dashboard's own origin);
* a kernel round-trips THROUGH the dashboard port — REST create (with the
  XSRF cookie dance the proxy must pass through) and a
  ``kernel_info_request``/``reply`` over the tunnelled kernel WebSocket;
* independence (SYS-34c): all of the above runs on a runtime with ZERO
  strategy/backtest handlers (backtest launch still answers 501 deferred).

Gated: ``pytest -m "not e2e"`` skips it; runs under ``ATP_RUN_E2E=1`` with
Playwright browsers installed AND ``jupyterlab`` importable. JupyterLab is
installed ad hoc in the worktree venv for this demonstration — it is a
container-image dependency (docker/jupyter.Dockerfile), deliberately NOT
pinned in requirements*.txt.
"""

from __future__ import annotations

import base64
import http.client
import json
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Guard collection: imports must not error when the optional tools are absent —
# the collection-time skip in conftest runs *after* module import.
sync_api = pytest.importorskip("playwright.sync_api")
pytest.importorskip("jupyterlab")

from atp_dashboard import (  # noqa: E402
    ReadinessBackedProvider,
    ResearchEnvironmentProvider,
    mount_dashboard,
)
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402

pytestmark = pytest.mark.e2e

_READY_DEADLINE = 60.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def jupyter_lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    """A real local JupyterLab under base_url=/research/ on an ephemeral port."""

    root = tmp_path_factory.mktemp("jupyterlab")
    for sub in ("runtime", "data", "config", "nb"):
        (root / sub).mkdir()
    port = _free_port()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "JUPYTER_RUNTIME_DIR": str(root / "runtime"),
        "JUPYTER_DATA_DIR": str(root / "data"),
        "JUPYTER_CONFIG_DIR": str(root / "config"),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "jupyter",
            "lab",
            "--ServerApp.ip=127.0.0.1",
            f"--ServerApp.port={port}",
            "--ServerApp.base_url=/research/",
            "--IdentityProvider.token=",
            "--ServerApp.password=",
            "--no-browser",
            f"--notebook-dir={root / 'nb'}",
        ],
        env=env,
        stdout=(root / "jupyterlab.log").open("wb"),
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + _READY_DEADLINE
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/research/api/status")
                status = conn.getresponse().status
                conn.close()
                if status == 200:
                    break
            except OSError:
                time.sleep(0.5)
        else:
            log_tail = (root / "jupyterlab.log").read_text(errors="replace")[-2000:]
            pytest.fail(f"JupyterLab never became ready on :{port}\n{log_tail}")
        yield port
    finally:
        process.terminate()
        process.wait(timeout=15)


@pytest.fixture()
def embedded_dashboard(jupyter_lab: int) -> Iterator[tuple[str, int]]:
    """A dashboard runtime proxying /research/ to the REAL JupyterLab, with
    ZERO strategy/backtest handlers registered (the SYS-34c posture)."""

    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        research=ResearchEnvironmentProvider(f"http://127.0.0.1:{jupyter_lab}"),
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def _xsrf_headers(host: str, port: int) -> dict[str, str]:
    """The XSRF cookie dance a browser performs implicitly — through the proxy."""

    _, headers, _ = _request(host, port, "GET", "/research/lab")
    cookie = headers.get("set-cookie", "")
    token = ""
    if "_xsrf=" in cookie:
        token = cookie.split("_xsrf=")[1].split(";")[0]
    assert token, "JupyterLab did not set an _xsrf cookie through the proxy"
    return {
        "Content-Type": "application/json",
        "Cookie": f"_xsrf={token}",
        "X-XSRFToken": token,
    }


def test_jupyterlab_renders_inside_the_dashboard_iframe(embedded_dashboard) -> None:
    host, port = embedded_dashboard
    dashboard_url = f"http://{host}:{port}/dashboard"
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(dashboard_url)
            panel = page.locator('[data-panel="research"]')
            panel.wait_for(state="visible", timeout=15_000)
            # The poll flips the probe-derived state to reachable and arms the
            # open button (never pre-armed: the state is live, not fabricated).
            open_button = page.locator("#research-open")
            page.wait_for_function(
                "() => !document.getElementById('research-open').disabled",
                timeout=15_000,
            )
            status_text = page.locator("#research-status").inner_text()
            assert "reachable" in status_text
            open_button.click()
            frame_element = page.locator("#research-frame")
            frame_element.wait_for(state="visible", timeout=15_000)
            # SYS-34a: the iframe src is a SAME-ORIGIN path — no separate URL.
            src = frame_element.get_attribute("src")
            assert src == "/research/lab"
            # The embedded document is REAL JupyterLab: its title says so once
            # the lab boots (frame-ancestors 'self' admitted the embed).
            page.wait_for_function(
                "() => {"
                "  const f = document.getElementById('research-frame');"
                "  try { return f.contentDocument"
                "      && /JupyterLab/i.test(f.contentDocument.title); }"
                "  catch (e) { return false; }"
                "}",
                timeout=45_000,
            )
        finally:
            browser.close()


def test_kernel_round_trips_through_the_dashboard_port(embedded_dashboard) -> None:
    host, port = embedded_dashboard
    # Independence (SYS-34c): no backtest engine, no strategies on this
    # runtime — the control routes answer 501 deferred...
    status, _, body = _request(host, port, "POST", "/api/v1/backtests", body=b"{}")
    assert status == 501
    assert json.loads(body)["error"]["category"] == "NOT_IMPLEMENTED"

    # ...while a REAL kernel lives entirely through the dashboard origin.
    headers = _xsrf_headers(host, port)
    status, _, body = _request(
        host,
        port,
        "POST",
        "/research/api/kernels",
        body=json.dumps({"name": "python3"}).encode(),
        headers=headers,
    )
    assert status == 201, body
    kernel_id = json.loads(body)["id"]
    try:
        client = socket.create_connection((host, port), timeout=15)
        try:
            key = base64.b64encode(b"0123456789abcdef").decode()
            cookie = headers["Cookie"]
            client.sendall(
                (
                    f"GET /research/api/kernels/{kernel_id}/channels?session_id=e2e HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                    f"Cookie: {cookie}\r\n\r\n"
                ).encode()
            )
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    break
                head += chunk
            assert b" 101 " in head.split(b"\r\n", 1)[0] + b" ", head[:200]
            # kernel_info_request in the legacy JSON serialisation — the client
            # offers NO subprotocol, so jupyter-server speaks plain JSON.
            message = json.dumps(
                {
                    "header": {
                        "msg_id": "e2e-1",
                        "username": "e2e",
                        "session": "e2e",
                        "msg_type": "kernel_info_request",
                        "version": "5.3",
                        "date": "2026-01-01T00:00:00Z",
                    },
                    "parent_header": {},
                    "metadata": {},
                    "content": {},
                    "channel": "shell",
                }
            ).encode()
            mask = b"\x01\x02\x03\x04"
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(message))
            length = len(message)
            if length < 126:
                frame = bytes([0x81, 0x80 | length]) + mask + masked
            else:
                frame = bytes([0x81, 0x80 | 126]) + struct.pack(">H", length) + mask + masked
            client.sendall(frame)

            client.settimeout(30)
            buffer = head.split(b"\r\n\r\n", 1)[1]
            deadline = time.monotonic() + 30
            found = False
            while time.monotonic() < deadline and not found:
                try:
                    buffer += client.recv(64 << 10)
                except TimeoutError:
                    break
                while len(buffer) >= 2:
                    payload_length = buffer[1] & 0x7F
                    offset = 2
                    if payload_length == 126:
                        if len(buffer) < 4:
                            break
                        payload_length = int.from_bytes(buffer[2:4], "big")
                        offset = 4
                    elif payload_length == 127:
                        if len(buffer) < 10:
                            break
                        payload_length = int.from_bytes(buffer[2:10], "big")
                        offset = 10
                    if len(buffer) < offset + payload_length:
                        break
                    payload = buffer[offset : offset + payload_length]
                    buffer = buffer[offset + payload_length :]
                    if b"kernel_info_reply" in payload:
                        found = True
                        break
            assert found, "no kernel_info_reply arrived through the tunnel"
        finally:
            client.close()
    finally:
        status, _, _ = _request(
            host, port, "DELETE", f"/research/api/kernels/{kernel_id}", headers=headers
        )
        assert status == 204
