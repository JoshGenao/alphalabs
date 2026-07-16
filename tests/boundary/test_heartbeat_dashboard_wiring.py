"""L4 boundary test — the SRS-MD-003 heartbeat provider wired into the real
operator-interface runtime through :func:`atp_dashboard.server.mount_dashboard`.

Asserts, over the real runtime + publisher objects (stub CLI runner, no
cargo/subprocess):

* mounting the provider claims the HEARTBEAT publisher and serves the
  ``GET /dashboard/api/heartbeat`` snapshot route;
* one publish tick emits one HEARTBEAT event PER FEED, exactly once (the
  channel is moved OFF the main ticker when the provider is mounted — no
  double publish from the deferred provider payload);
* the system snapshot's health section reflects ``any_stale`` (the AC's
  "reflected in system health status" leg);
* staleness transitions land in the JsonlLogStore and are queryable by
  source + event_type (the "logged" leg, end to end through the wiring);
* a bare mount (no provider) keeps the honest deferred HEARTBEAT cells on
  the main ticker — the pre-MD-003 behavior is unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from atp_dashboard.heartbeat import CliHeartbeatSource, HeartbeatFreshnessProvider
from atp_dashboard.provider import ReadinessBackedProvider
from atp_dashboard.server import HEARTBEAT_SNAPSHOT_PATH, SYSTEM_SNAPSHOT_PATH, mount_dashboard
from atp_logging import LogClass, Severity, Source
from atp_logging.persistence import JsonlLogStore
from atp_runtime import OperatorInterfaceRuntime

T0 = 1_700_000_000_000_000_000
NOW = T0 + 16_000_000_000  # 16 s after the last observation -> stale


def _stale_cli_output(evaluated_at_ns: int) -> str:
    return (
        f"event kind=HEARTBEAT_STALE feed=market_data symbol=AAPL asset_class=equity "
        f"staleness_ms=16000 last_observation_ns={T0} evaluated_at_ns={evaluated_at_ns} "
        f"threshold_ms=15000\n"
        f"event kind=HEARTBEAT_STALE feed=broker staleness_ms=16000 "
        f"last_observation_ns={T0} evaluated_at_ns={evaluated_at_ns} threshold_ms=15000\n"
        f"status feed=market_data symbol=AAPL asset_class=equity last_observation_ns={T0} "
        f"staleness_ms=16000 never_observed=false time_stale=true gap_stale=false stale=true "
        f"threshold_ms=15000 evaluated_at_ns={evaluated_at_ns}\n"
        f"status feed=broker last_observation_ns={T0} staleness_ms=16000 never_observed=false "
        f"time_stale=true gap_stale=false stale=true threshold_ms=15000 "
        f"evaluated_at_ns={evaluated_at_ns}\n"
    )


@pytest.fixture()
def stale_heartbeat(tmp_path: Path) -> HeartbeatFreshnessProvider:
    observations = tmp_path / "observations.txt"
    observations.write_text(
        f"watch-security AAPL equity\nwatch-broker\n"
        f"tick AAPL equity 1 {T0}\nbroker-heartbeat {T0}\n",
        encoding="utf-8",
    )

    def runner(argv, *, input, timeout):  # noqa: A002 - protocol signature
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=_stale_cli_output(NOW), stderr=""
        )

    source = CliHeartbeatSource(observations, runner=runner, now_ns=lambda: NOW)
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    return HeartbeatFreshnessProvider(source, log_store=store)


class _PublishSpy:
    """Counts runtime.publish calls per channel without a live WS session."""

    def __init__(self, runtime: OperatorInterfaceRuntime) -> None:
        self.calls: dict[str, list[object]] = {}
        self._inner = runtime.publish
        runtime.publish = self  # type: ignore[method-assign]

    def __call__(self, channel: str, payload: object) -> int:
        self.calls.setdefault(str(channel), []).append(payload)
        return self._inner(channel, payload)


def test_mounted_heartbeat_provider_wires_channel_route_health_and_logs(
    stale_heartbeat: HeartbeatFreshnessProvider, tmp_path: Path
) -> None:
    runtime = OperatorInterfaceRuntime()
    provider = ReadinessBackedProvider({}, heartbeat=stale_heartbeat)
    publisher = mount_dashboard(runtime, provider, heartbeat=stale_heartbeat)
    spy = _PublishSpy(runtime)

    # The publisher claims HEARTBEAT (via start()) like every owned channel.
    assert not runtime.is_publisher_registered("HEARTBEAT")
    publisher.start()
    try:
        assert runtime.is_publisher_registered("HEARTBEAT")
    finally:
        publisher.stop()

    # One manual tick: one event per feed, exactly once — the deferred
    # provider payload must NOT also publish on HEARTBEAT (no double publish).
    spy.calls.clear()
    publisher.publish_once()
    heartbeat_payloads = spy.calls.get("HEARTBEAT", [])
    assert len(heartbeat_payloads) == 2
    assert {p["feed"] for p in heartbeat_payloads} == {"market_data:AAPL", "ib_gateway"}
    assert all(p["is_stale"] is True for p in heartbeat_payloads)
    assert all(p["staleness_seconds"] == 16.0 for p in heartbeat_payloads)

    # REST: the heartbeat snapshot route is served with the stale verdict.
    status, snapshot = runtime.dispatch_rest("GET", HEARTBEAT_SNAPSHOT_PATH, b"")
    assert status == 200
    assert snapshot["ok"] is True and snapshot["any_stale"] is True
    assert snapshot["threshold_ms"] == 15_000
    assert [f["feed"] for f in snapshot["feeds"]] == ["market_data:AAPL", "ib_gateway"]

    # System health reflection (the AC's fourth leg).
    status, system = runtime.dispatch_rest("GET", SYSTEM_SNAPSHOT_PATH, b"")
    assert status == 200
    heartbeat_health = system["health"]["market_data_heartbeat"]
    assert heartbeat_health["any_stale"] is True and heartbeat_health["state"] == "STALE"
    assert heartbeat_health["stale_feeds"] == ["ib_gateway", "market_data:AAPL"]

    # Logged: the two stale transitions are persisted once (the polls above
    # all saw the same stale state) and queryable by source + event_type.
    records = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM).read()
    stale_records = [r for r in records if r.event_type == "HEARTBEAT_STALE"]
    assert len(stale_records) == 2
    assert {r.source for r in stale_records} == {Source.MARKET_DATA, Source.IB_GATEWAY}
    assert all(r.severity is Severity.WARN for r in stale_records)


def test_bare_mount_keeps_deferred_heartbeat_cells_on_the_main_ticker() -> None:
    runtime = OperatorInterfaceRuntime()
    provider = ReadinessBackedProvider({})
    publisher = mount_dashboard(runtime, provider)
    spy = _PublishSpy(runtime)

    publisher.publish_once()
    heartbeat_payloads = spy.calls.get("HEARTBEAT", [])
    assert len(heartbeat_payloads) == 1, "unmounted: one deferred payload on the main ticker"
    (payload,) = heartbeat_payloads
    assert payload["is_stale"]["value"] is None
    assert payload["is_stale"]["data_source"].startswith("deferred:")

    # No heartbeat REST route on a bare mount.
    status, _body = runtime.dispatch_rest("GET", HEARTBEAT_SNAPSHOT_PATH, b"")
    assert status == 404

    # And the system health section is an honest deferred cell.
    _status, system = runtime.dispatch_rest("GET", SYSTEM_SNAPSHOT_PATH, b"")
    section = system["health"]["market_data_heartbeat"]
    assert section["value"] is None and section["data_source"].startswith("deferred:")
