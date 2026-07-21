"""L4 boundary — mounting the UI-4 kill-switch status pane on the runtime.

The pane is a composition-time opt-in READ: mounting it serves exactly one new
GET route, claims no WebSocket channel, and adds no mutating surface. Without
it the route does not exist at all (a dashboard cannot imply a kill-switch feed
nobody mounted).

SRS trace: ``UI-4``, ``SRS-SAFE-001`` / ``SRS-SAFE-002`` (the observed
sequence), ``SRS-UI-001`` (dashboard mount seam).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from atp_dashboard import (
    KILL_SWITCH_SNAPSHOT_PATH,
    DurableKillSwitchStatusSource,
    KillSwitchStatusProvider,
    ReadinessBackedProvider,
    mount_dashboard,
    mount_default_dashboard,
)
from atp_runtime import OperatorInterfaceRuntime

pytestmark = pytest.mark.boundary


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


def _mounted(provider: KillSwitchStatusProvider | None) -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}), kill_switch=provider)
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


def test_mounting_serves_the_snapshot_route() -> None:
    for host, port in _mounted(KillSwitchStatusProvider()):
        status, body = _request(host, port, "GET", KILL_SWITCH_SNAPSHOT_PATH)

        assert status == 200
        assert body["srs_ref"] == "UI-4"
        assert len(body["sequence"]) == 6
        # Unconfigured: honest unavailable, never "not activated".
        assert body["ok"] is False
        assert body["activated"] is None


def test_without_the_provider_the_route_does_not_exist() -> None:
    # Composition opt-in honesty: a bare SRS-UI-001 mount serves no kill-switch
    # feed rather than an empty-looking one.
    for host, port in _mounted(None):
        assert _request(host, port, "GET", KILL_SWITCH_SNAPSHOT_PATH)[0] == 404


def test_the_snapshot_route_is_read_only() -> None:
    # A monitoring surface is never a control plane: the activation POST goes to
    # the /api/v1 contract route, never under /dashboard.
    for host, port in _mounted(KillSwitchStatusProvider()):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert _request(host, port, method, KILL_SWITCH_SNAPSHOT_PATH)[0] in (404, 405)


def test_mounting_claims_no_websocket_channel() -> None:
    # There is no kill-switch channel in the AsyncAPI contract; publishing on
    # one would be fabrication at the transport layer.
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime, ReadinessBackedProvider({}), kill_switch=KillSwitchStatusProvider()
    )
    publisher.start()
    try:
        for channel in ("STRATEGY_STATE", "ACCOUNT_STATUS", "RESERVOIR_RANKING", "ALERTS"):
            assert not runtime.is_publisher_registered(channel)
    finally:
        publisher.stop()
        runtime.stop()


def test_an_unreadable_state_dir_serves_an_explicit_unavailable(tmp_path: Path) -> None:
    corrupt = tmp_path / "state"
    corrupt.mkdir()
    (corrupt / "kill_switch_last_activation.json").write_text("{", encoding="utf-8")
    provider = KillSwitchStatusProvider(
        DurableKillSwitchStatusSource(state_dir=corrupt, log_dir=tmp_path)
    )

    for host, port in _mounted(provider):
        status, body = _request(host, port, "GET", KILL_SWITCH_SNAPSHOT_PATH)

        # 200 with an honest unavailable body — the pane must be able to render
        # the failure, not lose the route.
        assert status == 200
        assert body["ok"] is False
        assert body["activated"] is None
        assert any("corrupt" in error for error in body["errors"])


def test_default_composition_serves_the_route_without_configuration() -> None:
    # The production entrypoint always serves the pane. With no state directory
    # configured it renders UNKNOWN — an unconfigured dashboard must never state
    # that the kill switch has not been activated, because it cannot know.
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(runtime, {})
    publisher.start()
    try:
        status, body = runtime.dispatch_rest("GET", KILL_SWITCH_SNAPSHOT_PATH, b"")

        assert status == 200
        assert body["ok"] is False
        assert body["activated"] is None
        assert all(leg["status"] == "UNKNOWN" for leg in body["sequence"])
    finally:
        publisher.stop()
        runtime.stop()


def test_default_composition_reads_a_configured_state_dir(tmp_path: Path) -> None:
    from atp_safety.state import persist_last_activation

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    persist_last_activation(
        state_dir,
        {
            "activation_id": "act-xyz",
            "response": {
                "activation_id": "act-xyz",
                "activated_at": "2026-07-21T00:00:00.000+00:00",
                "cancelled_orders": [],
                "liquidation_orders": [],
                "paper_engines_halted": 0,
                "ib_gateway_disconnected": True,
            },
            "report": {
                "activation_id": "act-xyz",
                "activated_at_epoch_ms": 1,
                "paper_halt": {"status": "SUCCEEDED"},
                "paper_halt_summary": {
                    "engines_total": 0,
                    "transitioned": 0,
                    "already_halted": 0,
                },
                "resting_order_cancels": [],
                "liquidations": [],
                "ib_disconnect": {"status": "SUCCEEDED"},
                "timings": {"liquidations_submitted_ms": 12},
                "within_nfr_p3": True,
                "all_engines_halted": True,
            },
            "ran_clean": True,
            "audit_recorded": True,
            "halted_log_latency_ms": 40.0,
            "persisted_at_ns": 1,
        },
    )

    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(
        runtime,
        {"ATP_KILL_SWITCH_STATE": str(state_dir), "ATP_KILL_SWITCH_LOG_DIR": str(tmp_path)},
    )
    publisher.start()
    try:
        status, body = runtime.dispatch_rest("GET", KILL_SWITCH_SNAPSHOT_PATH, b"")

        assert status == 200
        assert body["activated"] is True
        assert body["activation_id"] == "act-xyz"
        legs = {leg["phase"]: leg for leg in body["sequence"]}
        assert legs["disconnect"]["status"] == "SUCCEEDED"
        # The SYS-44b legs stay UNKNOWN — no timeout record was written.
        assert legs["timeout"]["status"] == "UNKNOWN"
        assert legs["notification"]["status"] == "UNKNOWN"
    finally:
        publisher.stop()
        runtime.stop()
