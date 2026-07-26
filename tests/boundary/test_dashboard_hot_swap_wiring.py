"""L4 boundary — mounting the UI-5 Hot-Swap status pane on the runtime.

The pane is a composition-time opt-in READ: mounting it serves exactly one new
GET route, claims no WebSocket channel, and adds no mutating surface. Without it
the route does not exist at all (a dashboard cannot imply a Hot-Swap feed nobody
mounted). A wired-but-unreadable source is an explicit ``ok: False`` snapshot,
never a crash or a fabricated fact; a readable source resolves the cells.

SRS trace: ``UI-5``, ``SRS-RESV-003``..``006`` (the observed Hot-Swap state),
``SRS-UI-001`` (dashboard mount seam).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator, Mapping

import pytest
from atp_dashboard import (
    HOT_SWAP_SNAPSHOT_PATH,
    HotSwapStatusProvider,
    HotSwapStatusUnavailable,
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


def _mounted(provider: HotSwapStatusProvider | None) -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}), hot_swap=provider)
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


class _StubSource:
    """A readable source resolving a live swap state (the future flip seam)."""

    def live_state(self) -> Mapping[str, object] | None:
        return {
            "current_live_strategy_id": "momentum-v3",
            "demotion_pending": False,
            "cooldown": {
                "in_effect": True,
                "started_at": "2026-07-22T00:00:00Z",
                "expires_at": "2026-07-29T00:00:00Z",
            },
            "sequence": {
                "demote_signals": {"status": "DONE", "detail": "signals stopped"},
            },
        }

    def trigger_config(self) -> Mapping[str, object] | None:
        return {
            "any_enabled": True,
            "drawdown_demotion": {"enabled": True},
            "top_ranked_promotion": {"enabled": False},
            "highest_momentum_promotion": {"enabled": False},
        }

    def promotion_candidate(self) -> Mapping[str, object] | None:
        return {"candidate_strategy_id": "meanrev-v7"}


class _FailingSource:
    """A wired source whose legs cannot be read — must fail closed, not crash."""

    def live_state(self) -> Mapping[str, object] | None:
        raise HotSwapStatusUnavailable("hot-swap state unreadable (fixture)")

    def trigger_config(self) -> Mapping[str, object] | None:
        raise HotSwapStatusUnavailable("trigger config unreadable (fixture)")

    def promotion_candidate(self) -> Mapping[str, object] | None:
        raise HotSwapStatusUnavailable("candidate source unreadable (fixture)")


class _MalformedSource:
    """A wired source returning NON-mapping values (version skew / corrupt state).
    The provider must fail closed (ok:false + deferred), never raise on ``.get()``."""

    def live_state(self):  # type: ignore[no-untyped-def]
        return ["not", "a", "mapping"]

    def trigger_config(self):  # type: ignore[no-untyped-def]
        return "corrupt"

    def promotion_candidate(self):  # type: ignore[no-untyped-def]
        return 42


def test_mounting_serves_the_snapshot_route() -> None:
    for host, port in _mounted(HotSwapStatusProvider()):
        status, body = _request(host, port, "GET", HOT_SWAP_SNAPSHOT_PATH)

        assert status == 200
        assert body["srs_ref"] == "UI-5"
        assert len(body["changeover_sequence"]) == 5
        # No source: a well-formed all-deferred pane (producers unbuilt is not a
        # fault), never a fabricated swap state.
        assert body["ok"] is True
        assert body["current_live_strategy_id"]["value"] is None
        assert body["promotion_candidate"]["value"] is None


def test_without_the_provider_the_route_does_not_exist() -> None:
    # Composition opt-in honesty: a bare SRS-UI-001 mount serves no Hot-Swap feed
    # rather than an empty-looking one.
    for host, port in _mounted(None):
        assert _request(host, port, "GET", HOT_SWAP_SNAPSHOT_PATH)[0] == 404


def test_the_snapshot_route_is_read_only() -> None:
    for host, port in _mounted(HotSwapStatusProvider()):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert _request(host, port, method, HOT_SWAP_SNAPSHOT_PATH)[0] in (404, 405)


def test_mounting_claims_no_websocket_channel() -> None:
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


def test_a_wired_but_unreadable_source_serves_an_explicit_unavailable() -> None:
    provider = HotSwapStatusProvider(_FailingSource())
    for host, port in _mounted(provider):
        status, body = _request(host, port, "GET", HOT_SWAP_SNAPSHOT_PATH)

        # 200 with an honest unavailable body — the pane must render the failure,
        # not lose the route, and never fabricate a fact.
        assert status == 200
        assert body["ok"] is False
        assert body["errors"]
        assert body["current_live_strategy_id"]["value"] is None


def test_a_non_mapping_source_return_fails_closed_not_crashes() -> None:
    # Shape drift (a source returning a non-mapping) must not crash the route: the
    # pane still emits its fail-closed ok:false snapshot with the leg deferred.
    provider = HotSwapStatusProvider(_MalformedSource())
    for host, port in _mounted(provider):
        status, body = _request(host, port, "GET", HOT_SWAP_SNAPSHOT_PATH)

        assert status == 200
        assert body["ok"] is False
        assert body["errors"]
        assert body["current_live_strategy_id"]["value"] is None
        assert body["promotion_candidate"]["value"] is None
        assert len(body["changeover_sequence"]) == 5
        assert all(leg["status"] == "UNKNOWN" for leg in body["changeover_sequence"])


def test_a_readable_source_resolves_the_live_cells() -> None:
    # The flip seam: a concrete source (RESV-004/005/006 + LOG-001, RESV-002)
    # swaps every deferred cell to a real value with no change to the pane.
    provider = HotSwapStatusProvider(_StubSource())
    for host, port in _mounted(provider):
        status, body = _request(host, port, "GET", HOT_SWAP_SNAPSHOT_PATH)

        assert status == 200
        assert body["ok"] is True
        assert body["current_live_strategy_id"]["value"] == "momentum-v3"
        assert body["promotion_candidate"]["value"] == "meanrev-v7"
        assert body["demotion_pending"]["value"] is False
        assert body["cooldown"]["in_effect"]["value"] is True
        assert body["cooldown"]["expires_at"]["value"] == "2026-07-29T00:00:00Z"
        live = {t["kind"]: t["enabled"]["value"] for t in body["auto_triggers_live"]}
        assert live["drawdown_demotion"] is True
        assert live["top_ranked_promotion"] is False
        first = body["changeover_sequence"][0]
        assert first["phase"] == "demote_signals"
        assert first["status"] == "DONE"
        assert first["value"] == "DONE"


def test_default_composition_serves_the_route_without_configuration() -> None:
    # The production entrypoint always serves the pane, all-deferred — an
    # unconfigured dashboard must never fabricate a swap state.
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(runtime, {})
    publisher.start()
    try:
        status, body = runtime.dispatch_rest("GET", HOT_SWAP_SNAPSHOT_PATH, b"")

        assert status == 200
        assert body["ok"] is True
        assert body["promotion_candidate"]["value"] is None
        assert all(leg["status"] == "UNKNOWN" for leg in body["changeover_sequence"])
    finally:
        publisher.stop()
        runtime.stop()
