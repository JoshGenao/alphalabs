"""L4 boundary — the UI-1 critical-alerts pane over a real transport.

Boots :class:`atp_runtime.OperatorInterfaceRuntime` on an ephemeral loopback
port, mounts the dashboard with the alerts provider, and asserts:

* ``GET /dashboard/api/alerts`` returns the honest deferred snapshot over a real
  TCP socket (``ok:true``, the feed cell names ``SRS-NOTIF-001``, the per-alert
  schema pins the ALERTS-channel contract);
* the route is GET-only;
* a bare mount (no alerts provider) does NOT register the route — the pane
  reports its explicit "not mounted" state rather than pretending a feed exists;
* the served assets carry the pane and its honest awaiting copy — the SPA can
  never render "0 active alerts" while the feed is deferred;
* the production entrypoint (``mount_default_dashboard``) serves the route.

SRS trace: UI-1 (critical alerts), SYS-46 / SYS-58, SRS-SEC-002 (bind).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator

import pytest
from atp_dashboard import (
    ALERT_FIELDS,
    CriticalAlertsProvider,
    ReadinessBackedProvider,
    mount_dashboard,
    mount_default_dashboard,
)
from atp_runtime import OperatorInterfaceRuntime

pytestmark = pytest.mark.boundary


@pytest.fixture()
def running_dashboard() -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    mount_dashboard(runtime, ReadinessBackedProvider({}), alerts=CriticalAlertsProvider())
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        runtime.stop()


def _request(host: str, port: int, method: str, path: str) -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, response.getheader("Content-Type") or "", response.read()
    finally:
        conn.close()


def test_alerts_route_serves_an_honest_deferred_snapshot(running_dashboard) -> None:
    host, port = running_dashboard
    status, ctype, body = _request(host, port, "GET", "/dashboard/api/alerts")
    assert status == 200 and ctype.startswith("application/json")
    snap = json.loads(body)
    assert snap["ok"] is True and snap["srs_ref"] == "UI-1"
    assert snap["feed"] == {"value": None, "data_source": "deferred:SRS-NOTIF-001"}
    assert snap["alerts"] == []  # nothing fabricated
    assert tuple(snap["alert_fields"]) == ALERT_FIELDS


def test_alerts_route_is_read_only(running_dashboard) -> None:
    host, port = running_dashboard
    for method in ("POST", "PUT", "DELETE"):
        assert _request(host, port, method, "/dashboard/api/alerts")[0] in (404, 405)


def test_bare_mount_does_not_serve_the_route() -> None:
    runtime = OperatorInterfaceRuntime()
    mount_dashboard(runtime, ReadinessBackedProvider({}))  # no alerts provider
    assert runtime.dispatch_rest("GET", "/dashboard/api/alerts", b"")[0] == 404


def test_served_assets_carry_the_pane_and_its_honest_awaiting_copy(running_dashboard) -> None:
    host, port = running_dashboard
    _, _, index = _request(host, port, "GET", "/dashboard")
    assert b'data-panel="alerts"' in index
    assert b'id="alerts-summary"' in index and b'id="alerts-table"' in index
    _, _, app_js = _request(host, port, "GET", "/dashboard/app.js")
    # The deferred branch renders an awaiting state naming the producer — the
    # literal "active critical alert" count string is only reachable on the
    # real-feed branch (feed.value is not None).
    assert b"/dashboard/api/alerts" in app_js
    assert b"alert feed awaiting" in app_js
    assert b"SRS-NOTIF-001" in app_js


def test_default_composition_serves_the_route() -> None:
    runtime = OperatorInterfaceRuntime()
    mount_default_dashboard(runtime, {})
    status, snap = runtime.dispatch_rest("GET", "/dashboard/api/alerts")
    assert status == 200
    assert snap["feed"] == {"value": None, "data_source": "deferred:SRS-NOTIF-001"}


def test_default_composition_serves_live_strategy_status_when_configured(tmp_path) -> None:
    """UI-1: the PRODUCTION composition (``python -m atp_dashboard``) exposes
    live strategy status when the ORCH-005 deployment snapshot is configured
    (ATP_DEPLOYMENT_STATE), and honestly serves NO inventory route when it is
    not — never a fabricated inventory."""

    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    binary = root / "target" / "debug" / "orch005_rollback_cli"
    if not binary.exists():
        build = subprocess.run(
            ["cargo", "build", "-q", "-p", "atp-orchestrator", "--bin", "orch005_rollback_cli"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            pytest.skip(f"cannot build orch005_rollback_cli: {build.stderr}")
    state = tmp_path / "deploy.state"
    subprocess.run(
        [
            str(binary),
            "record",
            "--state",
            str(state),
            "--strategy",
            "alpha-1",
            "--hash",
            "sha256:" + "1" * 64,
            "--observed-at",
            "100",
        ],
        check=True,
        capture_output=True,
    )

    configured = OperatorInterfaceRuntime()
    mount_default_dashboard(configured, {"ATP_DEPLOYMENT_STATE": str(state)})
    status, snap = configured.dispatch_rest("GET", "/dashboard/api/strategies")
    assert status == 200
    assert snap["ok"] is True
    assert any(s.get("strategy_id") == "alpha-1" for s in snap["strategies"])

    bare = OperatorInterfaceRuntime()
    mount_default_dashboard(bare, {})
    assert bare.dispatch_rest("GET", "/dashboard/api/strategies")[0] == 404
