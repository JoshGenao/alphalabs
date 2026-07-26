"""L7 domain (safety) — UI-5 Hot-Swap controls + status invariants.

A Hot-Swap moves REAL money between a paper strategy and the single live slot,
so the dashboard's Changeover Console must never become an unguarded control
plane nor fabricate a swap state. This anchors that mounting the pane:

* adds **no mutating** endpoint — a POST/PUT/DELETE to the ``/dashboard/api/hot-swap``
  read route is refused, and the SYS-49a confirmation guard on the contract
  route ``POST /api/v1/hot-swap`` (SRS-RESV-003) is unchanged;
* drives its manual-promotion affordance to exactly the contract route (never a
  ``/dashboard``-namespaced mutation), and that route stays fail-closed (428
  without the token, 501 HANDLER_DEFERRED owner SRS-RESV-003 with it) on an
  un-wired runtime — the client cannot bypass the server-side guard;
* never fabricates a live Hot-Swap fact: every cell whose ``data_source`` is
  ``deferred:*`` carries ``value: None`` — a swap state the pane cannot observe
  is UNKNOWN, never a green;
* claims no Hot-Swap WebSocket channel (the AsyncAPI contract declares none).

SRS trace: UI-5, SRS-RESV-003..006 (SYS-49a..e), SRS-API-001 (the contract
route owner tag), SRS-UI-001 (dashboard mount seam).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from atp_dashboard import (
    HOT_SWAP_SNAPSHOT_PATH,
    HotSwapStatusProvider,
    ReadinessBackedProvider,
    mount_dashboard,
)
from atp_dashboard.provider import DEFERRED
from atp_runtime import OperatorInterfaceRuntime

pytestmark = [pytest.mark.domain, pytest.mark.safety]

_APP_JS = Path(__file__).resolve().parents[2] / "python/atp_dashboard/assets/app.js"


@pytest.fixture()
def mounted_runtime() -> Iterator[tuple[OperatorInterfaceRuntime, str, int]]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime, ReadinessBackedProvider({}), hot_swap=HotSwapStatusProvider()
    )
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


def test_hot_swap_status_route_is_read_only(mounted_runtime) -> None:
    # A monitoring surface is never a control plane: the swap POST goes to the
    # /api/v1 contract route, never under /dashboard.
    _, host, port = mounted_runtime
    assert _request(host, port, "GET", HOT_SWAP_SNAPSHOT_PATH)[0] == 200
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert _request(host, port, method, HOT_SWAP_SNAPSHOT_PATH)[0] in (404, 405)


def test_hot_swap_confirmation_guard_is_unchanged(mounted_runtime) -> None:
    # SYS-49a: mounting the pane must not weaken the manual-promotion
    # confirmation guard — a POST without the confirmation token never reaches a
    # handler.
    _, host, port = mounted_runtime
    status, body = _request(host, port, "POST", "/api/v1/hot-swap")
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"


def test_hot_swap_affordance_uses_only_the_contract_route(mounted_runtime) -> None:
    # UI-5 / SYS-49a: the manual-promotion affordance POSTs to the CONTRACT
    # route on this same runtime — it introduces NO dashboard-namespaced
    # mutation and no second swap path, and the client control cannot bypass the
    # server-side guard: an un-wired runtime still refuses its POST target (501
    # deferred owner SRS-RESV-003, never a silent success). The confirmation
    # token is sent only after the client's own arm-then-confirm step.
    _, host, port = mounted_runtime
    app_js = _APP_JS.read_text(encoding="utf-8")
    assert 'const HOT_SWAP_ROUTE = "/api/v1/hot-swap?confirm=true";' in app_js, (
        "the affordance must target exactly the contract route with the "
        "confirmation token the transport guard requires"
    )
    # Every /api/v1 fetch that names hot-swap must go through the one route const.
    fetch_targets = [line for line in app_js.splitlines() if "fetch(" in line and "api/v1" in line]
    assert all("hot-swap" not in target or "HOT_SWAP_ROUTE" in target for target in fetch_targets)
    # No hot-swap mutation under the read-only /dashboard namespace, and the read
    # route is the only /dashboard hot-swap path.
    assert "/dashboard/api/hot-swap/promote" not in app_js
    assert app_js.count('"/api/v1/hot-swap?confirm=true"') == 1
    # The affordance's target on THIS (un-wired) runtime stays fail-closed:
    status, body = _request(host, port, "POST", "/api/v1/hot-swap?confirm=true")
    assert status == 501
    assert body["error"]["type"] == "HANDLER_DEFERRED"
    assert body["error"]["detail"]["owner"] == "SRS-RESV-003"


def _iter_cells(node: object) -> Iterator[Mapping[str, object]]:
    """Yield every ``{value, data_source}`` cell reachable in the snapshot."""

    if isinstance(node, Mapping):
        if "data_source" in node and "value" in node:
            yield node
        for value in node.values():
            yield from _iter_cells(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_cells(item)


def test_hot_swap_never_fabricates_a_deferred_fact(mounted_runtime) -> None:
    # The all-deferred snapshot (no producer wired) must carry value: None on
    # every deferred cell — a swap fact the pane cannot observe is UNKNOWN, never
    # a fabricated live value.
    _, host, port = mounted_runtime
    status, body = _request(host, port, "GET", HOT_SWAP_SNAPSHOT_PATH)
    assert status == 200
    cells = list(_iter_cells(body))
    assert cells, "expected the snapshot to carry deferred cells"
    deferred_cells = [c for c in cells if str(c["data_source"]).startswith(DEFERRED)]
    assert deferred_cells, "expected every live fact to be deferred with no producer wired"
    for cell in deferred_cells:
        assert cell["value"] is None, f"deferred cell must not fabricate a value: {cell}"


def test_hot_swap_status_reports_real_sys49_defaults(mounted_runtime) -> None:
    # The REAL static control schema (not a live result) must be present: SYS-49a
    # automatic-default-disabled, and the SYS-49b/e default windows.
    _, host, port = mounted_runtime
    _, body = _request(host, port, "GET", HOT_SWAP_SNAPSHOT_PATH)
    assert body["srs_ref"] == "UI-5"
    assert body["trigger_catalog"]["automatic_default"] == "disabled"
    assert body["cooldown_days_default"] == 7
    assert body["demotion_timeout_seconds_default"] == 60


def test_hot_swap_claims_no_websocket_channel() -> None:
    # There is no Hot-Swap channel in the AsyncAPI contract; publishing on one
    # would be fabrication at the transport layer.
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime, ReadinessBackedProvider({}), hot_swap=HotSwapStatusProvider()
    )
    publisher.start()
    try:
        for channel in ("STRATEGY_STATE", "ACCOUNT_STATUS", "RESERVOIR_RANKING", "ALERTS"):
            assert not runtime.is_publisher_registered(channel)
    finally:
        publisher.stop()
        runtime.stop()
