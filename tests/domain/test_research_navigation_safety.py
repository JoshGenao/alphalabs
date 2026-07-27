"""L7 domain — primary research navigation must never leak a service URL.

``SRS-RES-003`` / SyRS ``SYS-43`` accepts only if the operator can open the
embedded environment from the primary workflow **without using a direct service
URL**. That is a trust-boundary property, not a rendering detail: the moment
the dashboard hands the browser (or the operator) the upstream's address, the
one-way ``SRS-SEC-004`` posture stops being the only way in and IF-13's
"proxied through the dashboard, not a standalone external endpoint" is broken.

Anchored here:

* **No service URL anywhere.** The upstream authority appears in NO byte the
  runtime serves — not the navigation model, not the SPA, not its JavaScript,
  not the probe snapshot — in the reachable AND unreachable cases (an error
  path that quotes the address leaks just as effectively as a success path).
* **Targets are structurally same-origin.** Every spelling of an absolute,
  protocol-relative, credentialed, or traversing target is refused, and the
  refusal does not echo the rejected value.
* **Navigation is not a control plane.** The model carries destinations only,
  the route answers reads only, and mounting it neither registers a proxyable
  prefix nor relaxes the mutating-route confirmation guard.
* **Nothing stays armed on stale truth.** Routability is a composition fact and
  is never allowed to stand in for liveness — the model refuses to claim an
  environment is reachable, so a navigation control gated on it cannot promise
  an open the system cannot deliver.

SRS trace: ``SRS-RES-003`` / SyRS ``SYS-43``; the environment navigated to is
``SRS-RES-001`` / IF-13 (``SYS-34a``), whose container boundary is
``SRS-SEC-004`` and whose bind policy is ``SRS-SEC-002``.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from atp_dashboard import (
    NAVIGATION_SNAPSHOT_PATH,
    RESEARCH_SNAPSHOT_PATH,
    PrimaryNavigationProvider,
    ReadinessBackedProvider,
    ResearchEnvironmentProvider,
    mount_dashboard,
)
from atp_dashboard.navigation import same_origin_target
from atp_runtime import OperatorInterfaceRuntime, ProxyPolicyError

pytestmark = [pytest.mark.domain, pytest.mark.safety]

_LAB_HTML = b"<!doctype html><html><body>lab</body></html>"

#: Every path the runtime serves to a browser in the primary workflow.
_SERVED_PATHS = (
    "/dashboard",
    "/dashboard/app.js",
    "/dashboard/styles.css",
    NAVIGATION_SNAPSHOT_PATH,
    RESEARCH_SNAPSHOT_PATH,
)

#: The state-bearing routes. The bare-port assertion is confined to these
#: because a five-digit ephemeral port could coincidentally occur inside a
#: 120 KB asset (a flaky test, not a leak); the authority form below is
#: unambiguous and IS asserted against every served path.
_STATE_PATHS = (NAVIGATION_SNAPSHOT_PATH, RESEARCH_SNAPSHOT_PATH)


class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(200)
        self.send_header("Content-Length", str(len(_LAB_HTML)))
        self.end_headers()
        self.wfile.write(_LAB_HTML)

    def log_message(self, *args: object, **kwargs: object) -> None:
        return


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(host: str, port: int, path: str, method: str = "GET") -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@contextlib.contextmanager
def _mounted(upstream_url: str | None) -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        research=ResearchEnvironmentProvider(upstream_url, probe_timeout=0.5),
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


@pytest.fixture()
def reachable_upstream() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# No direct service URL — the acceptance criterion, as a served-bytes property
# --------------------------------------------------------------------------- #


def test_no_served_byte_carries_the_upstream_when_reachable(
    reachable_upstream: int,
) -> None:
    with _mounted(f"http://127.0.0.1:{reachable_upstream}") as (host, port):
        authority = f"127.0.0.1:{reachable_upstream}".encode()
        for path in _SERVED_PATHS:
            status, body = _request(host, port, path)
            assert status == 200, path
            assert authority not in body, path
        for path in _STATE_PATHS:
            assert str(reachable_upstream).encode() not in _request(host, port, path)[1], path


def test_no_served_byte_carries_the_upstream_when_it_is_DOWN() -> None:
    """The failure path leaks as effectively as the success path — so it must not.

    A probe error that quotes the address it could not reach would publish the
    service URL precisely when the operator is most tempted to try it directly.
    """

    dead = _free_port()
    with _mounted(f"http://127.0.0.1:{dead}") as (host, port):
        status, body = _request(host, port, RESEARCH_SNAPSHOT_PATH)
        assert status == 200
        snapshot = json.loads(body)
        assert snapshot["upstream_reachable"] is False  # genuinely down
        for path in _SERVED_PATHS:
            assert f"127.0.0.1:{dead}".encode() not in _request(host, port, path)[1], path
        for path in _STATE_PATHS:
            assert str(dead).encode() not in _request(host, port, path)[1], path


def test_navigation_target_is_served_by_this_origin_not_another(
    reachable_upstream: int,
) -> None:
    """The advertised destination resolves against the dashboard's own listener."""

    with _mounted(f"http://127.0.0.1:{reachable_upstream}") as (host, port):
        _, body = _request(host, port, NAVIGATION_SNAPSHOT_PATH)
        target = json.loads(body)["entries"][0]["target"]
        assert same_origin_target(target) == target
        # Fetching the target from the DASHBOARD port reaches the environment.
        assert _request(host, port, f"{target}lab") == (200, _LAB_HTML)


# --------------------------------------------------------------------------- #
# Targets are structurally same-origin
# --------------------------------------------------------------------------- #


def test_no_adversarial_prefix_can_become_a_navigation_target() -> None:
    for prefix in (
        "http://8.8.8.8/research/",
        "https://jupyter.example.com/",
        "//jupyter.example.com/",
        "/\\jupyter.example.com/",
        "javascript:alert(1)",
        "data:text/html,<script>1</script>",
        "/research/../api/v1/kill-switch",
        "research/",
    ):
        entry = PrimaryNavigationProvider(
            research_routable=True,
            research_state_route=RESEARCH_SNAPSHOT_PATH,
            research_prefix=prefix,
        ).navigation_snapshot()["entries"][0]
        assert entry["routable"] is False, prefix
        assert entry["target"] is None, prefix
        # The refusal must not republish what it refused.
        assert prefix not in json.dumps(entry), prefix


# --------------------------------------------------------------------------- #
# Navigation is not a control plane
# --------------------------------------------------------------------------- #


def test_navigation_route_answers_reads_only(reachable_upstream: int) -> None:
    with _mounted(f"http://127.0.0.1:{reachable_upstream}") as (host, port):
        assert _request(host, port, NAVIGATION_SNAPSHOT_PATH)[0] == 200
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, _ = _request(host, port, NAVIGATION_SNAPSHOT_PATH, method=method)
            assert status != 200, method


def test_navigation_route_cannot_be_shadowed_by_a_proxy_prefix() -> None:
    """The new route joins the set a future proxy registration cannot enclose."""

    runtime = OperatorInterfaceRuntime()
    mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        research=ResearchEnvironmentProvider(None),
    )
    for prefix in ("/dashboard/", "/dashboard/api/", "/"):
        with pytest.raises(ProxyPolicyError):
            runtime.register_proxy_route(prefix, "http://127.0.0.1:9")


def test_mutating_routes_stay_confirmation_guarded_with_navigation_mounted(
    reachable_upstream: int,
) -> None:
    """Adding navigation chrome must not soften a safety-path guard."""

    with _mounted(f"http://127.0.0.1:{reachable_upstream}") as (host, port):
        status, _ = _request(host, port, "/api/v1/kill-switch", method="POST")
        assert status != 200
        assert status != 404  # the guarded contract route still exists


# --------------------------------------------------------------------------- #
# Composition is never allowed to masquerade as liveness
# --------------------------------------------------------------------------- #


def test_routable_never_claims_reachable() -> None:
    """A registered prefix over a DEAD upstream is still routable — and the
    model says only that. The reachability answer belongs to the probe, so a
    control gated on both facts cannot be armed by composition alone."""

    dead = _free_port()
    with _mounted(f"http://127.0.0.1:{dead}") as (host, port):
        _, nav = _request(host, port, NAVIGATION_SNAPSHOT_PATH)
        entry = json.loads(nav)["entries"][0]
        assert entry["routable"] is True
        # The navigation model makes NO liveness claim at all...
        assert "reachable" not in json.dumps(entry)
        # ...and the probe that does is unambiguous about the truth.
        _, research = _request(host, port, RESEARCH_SNAPSHOT_PATH)
        assert json.loads(research)["upstream_reachable"] is False
        assert json.loads(research)["embed_path"] is None
