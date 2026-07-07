"""L7 domain — SRS-UI-001 dashboard safety invariants.

A monitoring surface must never become an unguarded control plane. This anchors
that mounting the dashboard:

* adds **no mutating** endpoint — a POST to a dashboard path is refused, and the
  live-strategy / kill-switch confirmation guard (UI-4 / SRS-SAFE-001) is
  unchanged;
* keeps the loopback / RFC-1918 bind policy (SRS-SEC-002) fail-closed;
* never fabricates a metric value for a producer that is still deferred
  (SRS-BT-004 / SRS-BT-005 / SRS-PERF-001 / SRS-MD-006/007);
* claims exactly the three WebSocket publishers SRS-UI-001 owns — no control
  channel.

SRS trace: SRS-UI-001, SRS-SEC-002 (bind), UI-4 / SRS-SAFE-001 (kill-switch
confirmation), SRS-BT-004/005 · SRS-PERF-001 (deferred producers).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator

import pytest
from atp_dashboard import OWNED_CHANNELS, ReadinessBackedProvider, mount_dashboard
from atp_dashboard.provider import DEFERRED
from atp_runtime import OperatorInterfaceRuntime
from atp_runtime.errors import BindPolicyError
from atp_runtime.rest_server import assert_bind_allowed, is_allowed_bind_host

pytestmark = [pytest.mark.domain, pytest.mark.safety]


@pytest.fixture()
def mounted_runtime() -> Iterator[tuple[OperatorInterfaceRuntime, str, int]]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield runtime, host, port
    finally:
        publisher.stop()
        runtime.stop()


def _request(host: str, port: int, method: str, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        raw = response.read() or b"{}"
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        return response.status, body
    finally:
        conn.close()


def test_dashboard_surfaces_are_read_only(mounted_runtime) -> None:
    _, host, port = mounted_runtime
    # The asset + snapshot paths are GET-only; a POST is not a registered route.
    assert _request(host, port, "POST", "/dashboard")[0] in (404, 405)
    assert _request(host, port, "POST", "/dashboard/api/system")[0] in (404, 405)
    assert _request(host, port, "PUT", "/dashboard/api/system")[0] in (404, 405)
    # No dashboard path is a mutating trading-control route.
    assert _request(host, port, "DELETE", "/dashboard")[0] in (404, 405)


def test_kill_switch_confirmation_guard_is_unchanged(mounted_runtime) -> None:
    _, host, port = mounted_runtime
    # Mounting the dashboard must not weaken the SRS-SAFE-001 confirmation guard.
    status, body = _request(host, port, "POST", "/api/v1/kill-switch")
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"


def test_kill_switch_affordance_uses_only_the_contract_route(mounted_runtime) -> None:
    # SYS-44a: the SRS-SAFE-001 dashboard affordance POSTs to the CONTRACT
    # route on this same runtime — it introduces NO dashboard-namespaced
    # mutation (the read-only assertions above must keep holding) and no
    # second kill path. The client control also cannot bypass the server-side
    # guard: an unwired runtime still refuses its POST target (501 deferred,
    # never a silent success).
    _, host, port = mounted_runtime
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[2] / "python/atp_dashboard/assets/app.js").read_text(
        encoding="utf-8"
    )
    fetch_targets = [line for line in app_js.splitlines() if "fetch(" in line and "api/v1" in line]
    assert all(
        "kill-switch" not in target or "KILL_SWITCH_ROUTE" in target for target in fetch_targets
    )
    assert 'const KILL_SWITCH_ROUTE = "/api/v1/kill-switch?confirm=true";' in app_js, (
        "the affordance must target exactly the contract route with the "
        "confirmation token the transport guard requires"
    )
    # The button's target on THIS (un-wired) runtime stays fail-closed:
    status, body = _request(host, port, "POST", "/api/v1/kill-switch?confirm=true")
    assert status == 501
    assert body["error"]["type"] == "HANDLER_DEFERRED"
    assert body["error"]["detail"]["owner"] == "SRS-SAFE-001"


def test_dashboard_bind_is_loopback_or_rfc1918_only() -> None:
    # SRS-SEC-002: loopback / RFC 1918 accepted; all-interfaces + public refused.
    for allowed in ("127.0.0.1", "10.1.2.3", "192.168.1.9"):
        assert is_allowed_bind_host(allowed)
        assert_bind_allowed(allowed)  # does not raise
    for refused in ("0.0.0.0", "8.8.8.8", "169.254.1.1"):
        assert not is_allowed_bind_host(refused)
        with pytest.raises(BindPolicyError):
            assert_bind_allowed(refused)


def test_start_on_public_host_is_refused_even_with_dashboard_mounted() -> None:
    runtime = OperatorInterfaceRuntime()
    mount_dashboard(runtime, ReadinessBackedProvider({}))
    with pytest.raises(BindPolicyError):
        runtime.start(host="8.8.8.8", port=0)


@pytest.mark.parametrize("channel", OWNED_CHANNELS)
def test_provider_never_fabricates_a_deferred_value(channel: str) -> None:
    payload = ReadinessBackedProvider({}).channel_payload(channel)
    for name, cell in payload.items():
        if isinstance(cell, dict) and str(cell.get("data_source", "")).startswith(DEFERRED):
            assert cell["value"] is None, f"{channel}.{name} fabricated a deferred value"


def test_publisher_claims_only_the_owned_channels() -> None:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    publisher.start()
    try:
        for channel in OWNED_CHANNELS:
            assert runtime.is_publisher_registered(channel)
        # Not a control channel: the dashboard never claims to publish, e.g., a
        # kill-switch or account-mutation stream.
        assert not runtime.is_publisher_registered("ACCOUNT_STATUS")
        # SRS-UI-002 is composition-time OPT-IN: a bare SRS-UI-001 mount never
        # claims the inventory channel (and serves no inventory route) — the
        # dashboard cannot pretend an inventory feed exists that nobody mounted.
        assert not runtime.is_publisher_registered("STRATEGY_STATE")
    finally:
        publisher.stop()
        runtime.stop()


def test_backtest_history_route_is_read_only(mounted_runtime) -> None:
    # UI-3: the backtest panel adds a launch CONTROL, but the /dashboard namespace
    # stays read-only — a mutating verb on the history route is not a registered
    # route (the launch goes to the /api/v1 contract route, asserted below).
    _, host, port = mounted_runtime
    for method in ("POST", "PUT", "DELETE"):
        assert _request(host, port, method, "/dashboard/api/backtests")[0] in (404, 405)


def test_backtest_launch_affordance_uses_only_the_contract_route(mounted_runtime) -> None:
    # SYS-43a: the UI-3 launch affordance POSTs to the CONTRACT route on this same
    # runtime — it introduces NO dashboard-namespaced mutation, and on an un-wired
    # runtime the POST target stays fail-closed (501 deferred, never a silent
    # launch). The live launch handler is deferred (declared owner SRS-BT-001).
    _, host, port = mounted_runtime
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[2] / "python/atp_dashboard/assets/app.js").read_text(
        encoding="utf-8"
    )
    # The affordance targets exactly the contract route via a named constant, and
    # the history route is polled GET-only (never a dashboard-namespaced mutation).
    assert 'const BACKTEST_LAUNCH_ROUTE = "/api/v1/backtests";' in app_js
    assert "fetch(BACKTEST_LAUNCH_ROUTE, {" in app_js
    assert 'fetch(BACKTEST_HISTORY_ROUTE, { cache: "no-store" })' in app_js
    fetch_targets = [line for line in app_js.splitlines() if "fetch(" in line and "api/v1" in line]
    assert all(
        "backtest" not in target or "BACKTEST_LAUNCH_ROUTE" in target for target in fetch_targets
    )
    # The button's target on THIS (un-wired) runtime stays fail-closed.
    status, body = _request(host, port, "POST", "/api/v1/backtests")
    assert status == 501
    assert body["error"]["type"] == "HANDLER_DEFERRED"
    assert body["error"]["detail"]["owner"] == "SRS-BT-001"


def test_backtest_mount_serves_history_route_and_stays_honest() -> None:
    # With the SRS-UI-004 provider mounted the history route is served — and an
    # UNREADABLE store yields an explicit unavailable history (ok:false), never
    # fabricated runs. Without it the route is absent (composition opt-in honesty).
    from atp_dashboard import BacktestHistoryProvider, StoreCliBacktestHistorySource

    bare = OperatorInterfaceRuntime()
    mount_dashboard(bare, ReadinessBackedProvider({}))
    assert bare.dispatch_rest("GET", "/dashboard/api/backtests", b"")[0] == 404

    runtime = OperatorInterfaceRuntime()
    provider = BacktestHistoryProvider(
        StoreCliBacktestHistorySource(
            results_dir="/nonexistent/results",
            binary="/nonexistent/bt009_store_cli",
        )
    )
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}), backtests=provider)
    publisher.start()
    try:
        status, body = runtime.dispatch_rest("GET", "/dashboard/api/backtests", b"")
        assert status == 200
        assert body["ok"] is False and body["backtests"] == []
        # A backtest history provider must NOT claim a WS publisher channel — the
        # history is REST-served (there is no BACKTEST channel).
        assert not runtime.is_publisher_registered("STRATEGY_STATE")
    finally:
        publisher.stop()
        runtime.stop()


def test_inventory_mount_claims_strategy_state_and_stays_honest() -> None:
    # With the SRS-UI-002 provider mounted the publisher claims STRATEGY_STATE
    # too — and an UNREADABLE inventory source publishes an explicit
    # unavailable summary (ok:false + the reason), never fabricated rows.
    from atp_dashboard import RollbackSnapshotInventorySource, StrategyInventoryProvider

    runtime = OperatorInterfaceRuntime()
    inventory = StrategyInventoryProvider(
        RollbackSnapshotInventorySource(
            state_path="/nonexistent/inventory.state",
            binary="/nonexistent/orch005_rollback_cli",
        )
    )
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}), inventory=inventory)
    publisher.start()
    try:
        assert runtime.is_publisher_registered("STRATEGY_STATE")
        events = inventory.strategy_state_events()
        assert len(events) == 1
        assert events[0]["ok"] is False and events[0]["strategy_count"] is None
        status, body = runtime.dispatch_rest("GET", "/dashboard/api/strategies", b"")
        assert status == 200
        assert body["ok"] is False and body["strategies"] == []
    finally:
        publisher.stop()
        runtime.stop()
