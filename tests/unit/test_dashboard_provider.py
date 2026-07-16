"""L1 unit — SRS-UI-001 dashboard provider: honest shapes, no fabrication.

Asserts the provider's channel payloads carry exactly the atp_ws-declared fields,
that deferred producers are surfaced as ``value=None`` tagged ``deferred:<owner>``
(never a fabricated number), that the system snapshot wires the real readiness
payload, and that the readiness snapshot is cached across a ≤5 s poll.
"""

from __future__ import annotations

import pytest
from atp_dashboard import DashboardMetricsProvider, ReadinessBackedProvider
from atp_dashboard.provider import (
    DEFERRED,
    OWNED_CHANNELS,
    REFRESH_BUDGET_MS,
    deferred_field,
)
from atp_ws import EVENT_CHANNELS, MAX_REFRESH_SECONDS

pytestmark = pytest.mark.unit


def _fields_for(channel: str) -> tuple[str, ...]:
    return next(c.payload_fields for c in EVENT_CHANNELS if c.name == channel)


@pytest.fixture()
def provider() -> ReadinessBackedProvider:
    return ReadinessBackedProvider({})


def test_provider_satisfies_protocol(provider: ReadinessBackedProvider) -> None:
    assert isinstance(provider, DashboardMetricsProvider)


@pytest.mark.parametrize("channel", OWNED_CHANNELS)
def test_channel_payload_keys_match_the_ws_contract(
    provider: ReadinessBackedProvider, channel: str
) -> None:
    payload = provider.channel_payload(channel)
    # Every declared field is present (extra meta like as_of is allowed).
    for field in _fields_for(channel):
        assert field in payload, f"{channel} payload missing declared field {field!r}"


@pytest.mark.parametrize("channel", OWNED_CHANNELS)
def test_deferred_fields_never_fabricate_a_value(
    provider: ReadinessBackedProvider, channel: str
) -> None:
    payload = provider.channel_payload(channel)
    for name, cell in payload.items():
        if isinstance(cell, dict) and str(cell.get("data_source", "")).startswith(DEFERRED):
            assert cell["value"] is None, f"{channel}.{name} fabricated a deferred value"


def test_channel_payload_rejects_unowned_channel(provider: ReadinessBackedProvider) -> None:
    with pytest.raises(ValueError):
        provider.channel_payload("ACCOUNT_STATUS")


def test_deferred_field_names_its_owner() -> None:
    cell = deferred_field("benchmark_return")
    assert cell == {"value": None, "data_source": "deferred:SRS-BT-005"}


def test_system_snapshot_is_real_health_plus_deferred_latency(
    provider: ReadinessBackedProvider,
) -> None:
    snap = provider.system_snapshot()
    assert snap["refresh_budget_ms"] == REFRESH_BUDGET_MS == 5_000  # NFR-P2
    assert snap["max_refresh_seconds"] == MAX_REFRESH_SECONDS
    assert snap["srs_ref"] == "SRS-UI-001"

    health = snap["health"]
    assert isinstance(health, dict)
    # The readiness payload is the one real signal today.
    assert health["data_source"] == "live"
    assert set(health) >= {"state", "ok", "errors", "warnings"}

    latency = snap["latency"]
    assert latency["refresh_budget_ms"] == REFRESH_BUDGET_MS
    # Percentiles are deferred to SRS-PERF-001 — no fabricated numbers.
    assert latency["order_signal_to_ack_p95_ms"]["value"] is None
    assert latency["observed_refresh_ms"]["value"] is None  # measured on the client


def test_readiness_snapshot_is_cached_within_ttl(provider: ReadinessBackedProvider) -> None:
    first = provider.system_snapshot()["health"]
    second = provider.system_snapshot()["health"]
    # The outer health dict is a fresh copy per poll (SRS-MD-003 augments it
    # with the live market_data_heartbeat section, which must never mutate
    # the cached readiness payload), so identity is asserted on the CACHED
    # gate evaluation's own nested objects: same errors-list identity =>
    # the gate was not re-evaluated on the second poll.
    assert first is not second
    assert first["errors"] is second["errors"]
    assert first["state"] == second["state"]


def test_health_fails_safe_when_readiness_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from atp_dashboard import provider as prov

    def _boom(_env: object) -> object:
        raise RuntimeError("config incomplete")

    monkeypatch.setattr(prov.ReadinessGate, "from_env", staticmethod(_boom))
    health = ReadinessBackedProvider({}).system_snapshot()["health"]
    # A monitoring surface reports an explicit unavailable state, never crashes.
    assert health["ok"] is False
    assert health["state"] == "UNAVAILABLE"
    assert health["data_source"] == "live"
