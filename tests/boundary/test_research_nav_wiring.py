"""L4 boundary — SRS-RES-003 primary navigation wired through a real socket.

Mounts the dashboard onto a live ``OperatorInterfaceRuntime`` and proves over
real TCP that the operator's *primary workflow* carries a direct route to the
embedded research environment (SyRS SYS-43):

* the SPA ships a persistent topbar navigation entry whose ``href`` is this
  origin's own ``#research`` anchor — so ``/dashboard#research`` is addressable
  and the entry still navigates with JavaScript disabled;
* ``GET /dashboard/api/navigation`` serves the same-origin navigation model
  alongside the embed it navigates to;
* an unconfigured embed serves an entry that is explicitly NOT routable (and no
  proxy prefix exists to route to);
* a bare SRS-UI-001 dashboard serves no navigation route at all — there is no
  embed to navigate to — and the SPA still ships coherent chrome;
* the route is a strict read: it answers GET and refuses to be a control plane.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from atp_dashboard import (
    NAVIGATION_SNAPSHOT_PATH,
    RESEARCH_SNAPSHOT_PATH,
    ReadinessBackedProvider,
    ResearchEnvironmentProvider,
    mount_dashboard,
)
from atp_dashboard.research import RESEARCH_PREFIX
from atp_runtime import OperatorInterfaceRuntime

_LAB_HTML = b"<!doctype html><html><body data-jupyter='lab-stub'>lab</body></html>"


class _JupyterishUpstream(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_LAB_HTML)))
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(_LAB_HTML)

    def log_message(self, *args: object, **kwargs: object) -> None:
        return


@pytest.fixture()
def upstream() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JupyterishUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def embedded_dashboard(upstream: int) -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        research=ResearchEnvironmentProvider(f"http://127.0.0.1:{upstream}"),
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


def _request(host: str, port: int, path: str, method: str = "GET") -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The primary workflow carries the entry
# --------------------------------------------------------------------------- #


def test_spa_ships_the_primary_navigation_entry(embedded_dashboard) -> None:
    host, port = embedded_dashboard
    status, body = _request(host, port, "/dashboard")
    assert status == 200
    assert b'<nav class="nav" aria-label="Primary">' in body
    assert b'id="nav-research"' in body
    # An anchor on THIS origin: navigable without JS, and the deep-link target.
    assert b'href="#research"' in body
    assert b'id="research"' in body
    # The state caption is a live region so a state change is announced, and it
    # starts as an unknown — never a fabricated "available".
    assert b'id="nav-research-state"' in body
    assert b'aria-live="polite"' in body


def test_navigation_route_serves_a_routable_same_origin_entry(
    embedded_dashboard,
) -> None:
    host, port = embedded_dashboard
    status, body = _request(host, port, NAVIGATION_SNAPSHOT_PATH)
    assert status == 200
    snapshot = json.loads(body)
    assert snapshot["srs_ref"] == "SRS-RES-003"
    entry = snapshot["entries"][0]
    assert entry["id"] == "research"
    assert entry["routable"] is True
    assert entry["target"] == RESEARCH_PREFIX
    assert entry["state_route"] == RESEARCH_SNAPSHOT_PATH
    # The advertised target really is served by this same listener — one
    # origin, no separate service URL.
    assert _request(host, port, f"{entry['target']}lab")[0] == 200


def test_navigation_route_is_a_strict_read(embedded_dashboard) -> None:
    """Navigation describes destinations; it is never a control surface."""

    host, port = embedded_dashboard
    for method in ("POST", "PUT", "DELETE"):
        status, _ = _request(host, port, NAVIGATION_SNAPSHOT_PATH, method=method)
        assert status != 200, method


def test_navigation_model_never_serves_the_upstream(embedded_dashboard, upstream: int) -> None:
    """The operator must never *need* the service URL — nor be handed it."""

    host, port = embedded_dashboard
    _, body = _request(host, port, NAVIGATION_SNAPSHOT_PATH)
    assert str(upstream).encode() not in body
    assert b"127.0.0.1" not in body


# --------------------------------------------------------------------------- #
# Degraded compositions stay honest
# --------------------------------------------------------------------------- #


def test_unconfigured_embed_serves_an_unroutable_entry() -> None:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime, ReadinessBackedProvider({}), research=ResearchEnvironmentProvider(None)
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, body = _request(host, port, NAVIGATION_SNAPSHOT_PATH)
        assert status == 200
        entry = json.loads(body)["entries"][0]
        assert entry["routable"] is False
        assert entry["target"] is None
        assert "ATP_RESEARCH_UPSTREAM" in entry["detail"]
        # ...and there is genuinely nothing to route to.
        assert _request(host, port, f"{RESEARCH_PREFIX}lab")[0] == 404
    finally:
        publisher.stop()
        runtime.stop()


def test_bare_dashboard_serves_no_navigation_route() -> None:
    """No embed mounted -> no navigation to it (the SPA renders not-mounted).

    A bare SRS-UI-001 dashboard must not grow routes it has no producer for.
    """

    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        assert _request(host, port, NAVIGATION_SNAPSHOT_PATH)[0] == 404
        assert _request(host, port, RESEARCH_SNAPSHOT_PATH)[0] == 404
        # The chrome still ships — the entry is present and renders its own
        # explicit "not mounted" state rather than vanishing silently.
        status, body = _request(host, port, "/dashboard")
        assert status == 200
        assert b'id="nav-research"' in body
    finally:
        publisher.stop()
        runtime.stop()


def test_navigation_registration_does_not_disturb_the_embed(
    embedded_dashboard,
) -> None:
    """SRS-RES-001's own contract is untouched by the navigation leg."""

    host, port = embedded_dashboard
    status, body = _request(host, port, RESEARCH_SNAPSHOT_PATH)
    assert status == 200
    snapshot = json.loads(body)
    assert snapshot["srs_ref"] == "SRS-RES-001"
    assert snapshot["configured"] is True
    assert snapshot["upstream_reachable"] is True
    assert snapshot["embed_path"] == f"{RESEARCH_PREFIX}lab"
