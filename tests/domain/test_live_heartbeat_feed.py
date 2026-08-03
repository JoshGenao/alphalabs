"""L7 domain — SRS-MD-003: the LIVE feed's freshness verdicts cannot lie.

``test_heartbeat_staleness.py`` pins the monitor's own invariants. This file
pins the ones that only exist once a real producer is in the picture, and they
are all one bug class: **a monitor that has stopped monitoring must never read
as a healthy one.**

The live path adds two ways for that to happen, neither of which the monitor
can defend against by itself:

* the feed daemon dies, leaving behind a snapshot file whose last verdict said
  everything was fresh — and a file nobody is rewriting looks exactly like a
  file somebody is rewriting;
* the daemon runs but its broker probe never gets an answer, so the "is the
  gateway alive" question is answered by our own socket rather than by the
  gateway.

Both are false all-clears on a safety-relevant surface: the operator reads
"fresh" and believes market data is flowing when it is not.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from atp_dashboard.heartbeat import (
    THRESHOLD_MS,
    HeartbeatFreshnessProvider,
    HeartbeatUnavailable,
    SnapshotHeartbeatSource,
)
from atp_logging import LogClass
from atp_logging.persistence import JsonlLogStore

ROOT = Path(__file__).resolve().parents[2]

MAGIC = "atp-md003-snapshot"


def _snapshot(evaluated_at_ns: int, *, market_stale: bool, broker_stale: bool) -> str:
    """A snapshot exactly as ``atp_market_data::live_feed`` renders one."""

    def row(feed_fields: str, stale: bool) -> str:
        return (
            f"status {feed_fields} last_observation_ns={evaluated_at_ns} "
            f"staleness_ms={'20000' if stale else '250'} never_observed=false "
            f"time_stale={'true' if stale else 'false'} gap_stale=false "
            f"stale={'true' if stale else 'false'} threshold_ms={THRESHOLD_MS} "
            f"evaluated_at_ns={evaluated_at_ns}"
        )

    header = (
        f"{MAGIC} schema_version=1 evaluated_at_ns={evaluated_at_ns} "
        f"threshold_ms={THRESHOLD_MS} observed_ticks=3 broker_probe=answered "
        "gap_detection=unavailable degraded=false"
    )
    return "\n".join(
        [
            header,
            row("feed=market_data symbol=AAPL asset_class=equity", market_stale),
            row("feed=broker", broker_stale),
        ]
    )


def _provider(source: SnapshotHeartbeatSource, tmp_path: Path) -> HeartbeatFreshnessProvider:
    return HeartbeatFreshnessProvider(
        source,
        log_store=JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM),
    )


# --------------------------------------------------------------------------- #
# The dead-daemon false all-clear
# --------------------------------------------------------------------------- #


def test_a_dead_feeds_leftover_fresh_snapshot_never_reads_healthy(tmp_path: Path) -> None:
    """The load-bearing one.

    The daemon wrote "everything fresh" and then died. Nothing about the FILE
    changes when that happens — only its age does. If age is not checked, the
    dashboard shows a green heartbeat forever while no market data is arriving
    at all.
    """

    written_at = 1_000_000 * 1_000_000_000
    path = tmp_path / "heartbeat.snapshot"
    path.write_text(_snapshot(written_at, market_stale=False, broker_stale=False))

    # One minute later, with nobody rewriting the file.
    dead_clock = written_at + 60 * 1_000_000_000
    source = SnapshotHeartbeatSource(path, now_ns=lambda: dead_clock)

    with pytest.raises(HeartbeatUnavailable) as exc:
        source.observe()
    assert "old" in str(exc.value)

    health = _provider(source, tmp_path).health_summary()
    assert health["ok"] is False
    assert health["state"] == "UNAVAILABLE"
    assert health["any_stale"] is True, "a dead monitor is NOT proven fresh"


def test_a_snapshot_inside_its_age_budget_is_served(tmp_path: Path) -> None:
    """The other half: a LIVE daemon's verdicts must reach the operator."""

    written_at = 1_000_000 * 1_000_000_000
    path = tmp_path / "heartbeat.snapshot"
    path.write_text(_snapshot(written_at, market_stale=False, broker_stale=False))
    source = SnapshotHeartbeatSource(path, now_ns=lambda: written_at + 900_000_000)

    health = _provider(source, tmp_path).health_summary()
    assert health["ok"] is True
    assert health["state"] == "FRESH"
    assert health["watched_feeds"] == 2
    assert health["data_source"] == "md003_live_feed_cli", (
        "health must name the producer that actually answered, not the fixture CLI"
    )


def test_a_live_stale_verdict_reaches_health_as_stale(tmp_path: Path) -> None:
    written_at = 1_000_000 * 1_000_000_000
    path = tmp_path / "heartbeat.snapshot"
    path.write_text(_snapshot(written_at, market_stale=True, broker_stale=False))
    source = SnapshotHeartbeatSource(path, now_ns=lambda: written_at + 500_000_000)

    health = _provider(source, tmp_path).health_summary()
    assert health["ok"] is False
    assert health["state"] == "STALE"
    assert health["stale_feeds"] == ["market_data:AAPL"]


def test_a_future_dated_snapshot_is_refused_not_treated_as_current(tmp_path: Path) -> None:
    """The age guard's blind spot: a negative age is not "older than the limit".

    A snapshot stamped ahead of the reader's clock would sail through a
    greater-than test and pin a fresh verdict. Worse, once served, its
    evaluation instant poisons the provider's monotonic guard — every genuine
    snapshot after it looks older and is discarded as late, so real staleness
    stays invisible until wall time catches up.
    """

    reader_now = 1_000_000 * 1_000_000_000
    path = tmp_path / "heartbeat.snapshot"
    path.write_text(
        _snapshot(reader_now + 3_600 * 1_000_000_000, market_stale=False, broker_stale=False)
    )
    source = SnapshotHeartbeatSource(path, now_ns=lambda: reader_now)

    with pytest.raises(HeartbeatUnavailable) as exc:
        source.observe()
    assert "FUTURE" in str(exc.value)

    health = _provider(source, tmp_path).health_summary()
    assert health["state"] == "UNAVAILABLE"
    assert health["any_stale"] is True


# --------------------------------------------------------------------------- #
# Transitions must survive sampling
# --------------------------------------------------------------------------- #


def test_a_stale_recover_cycle_between_polls_is_still_logged(tmp_path: Path) -> None:
    """The audit leg's sampling hole.

    The daemon evaluates far more often than the dashboard polls. If a feed goes
    stale and recovers entirely between two polls, every row this provider ever
    sees reads fresh — and a real 20-second market-data outage would leave no
    trace in the audit trail at all. The snapshot's transition journal is what
    makes those flips survive, so both records must land even though no sampled
    row was ever stale.
    """

    written_at = 1_000_000 * 1_000_000_000
    went_stale_at = written_at - 8 * 1_000_000_000
    recovered_at = written_at - 3 * 1_000_000_000
    body = _snapshot(written_at, market_stale=False, broker_stale=False)
    body += (
        f"\nevent kind=HEARTBEAT_STALE feed=market_data symbol=AAPL asset_class=equity "
        f"staleness_ms=20000 last_observation_ns={went_stale_at} "
        f"evaluated_at_ns={went_stale_at} threshold_ms={THRESHOLD_MS}"
        f"\nevent kind=HEARTBEAT_RECOVERED feed=market_data symbol=AAPL asset_class=equity "
        f"staleness_ms=120 last_observation_ns={recovered_at} "
        f"evaluated_at_ns={recovered_at} threshold_ms={THRESHOLD_MS}"
    )
    path = tmp_path / "heartbeat.snapshot"
    path.write_text(body)

    store_path = tmp_path / "system.jsonl"
    provider = HeartbeatFreshnessProvider(
        SnapshotHeartbeatSource(path, now_ns=lambda: written_at + 500_000_000),
        log_store=JsonlLogStore(store_path, log_class=LogClass.SYSTEM),
    )

    snapshot = provider.heartbeat_snapshot()
    assert snapshot["ok"] is True, "the current rows are fresh — the incident is history"

    written = store_path.read_text()
    assert "HEARTBEAT_STALE" in written, "the missed outage must reach the audit trail"
    assert "HEARTBEAT_RECOVERED" in written, "so must its recovery"


def test_a_journalled_transition_is_logged_exactly_once(tmp_path: Path) -> None:
    """The journal is re-offered on every snapshot until it ages out."""

    written_at = 1_000_000 * 1_000_000_000
    flipped_at = written_at - 5 * 1_000_000_000
    body = _snapshot(written_at, market_stale=False, broker_stale=False)
    body += (
        f"\nevent kind=HEARTBEAT_STALE feed=broker staleness_ms=20000 "
        f"last_observation_ns={flipped_at} evaluated_at_ns={flipped_at} "
        f"threshold_ms={THRESHOLD_MS}"
    )
    path = tmp_path / "heartbeat.snapshot"
    path.write_text(body)

    store_path = tmp_path / "system.jsonl"
    clock = {"now": written_at + 500_000_000}
    provider = HeartbeatFreshnessProvider(
        SnapshotHeartbeatSource(path, now_ns=lambda: clock["now"]),
        log_store=JsonlLogStore(store_path, log_class=LogClass.SYSTEM),
    )

    provider.heartbeat_snapshot()
    # A later snapshot still carrying the same journal entry.
    later = written_at + 1_000_000_000
    path.write_text(
        _snapshot(later, market_stale=False, broker_stale=False)
        + f"\nevent kind=HEARTBEAT_STALE feed=broker staleness_ms=20000 "
        f"last_observation_ns={flipped_at} evaluated_at_ns={flipped_at} "
        f"threshold_ms={THRESHOLD_MS}"
    )
    clock["now"] = later + 500_000_000
    provider.heartbeat_snapshot()

    assert written_at  # keep the fixture instant meaningful to the reader
    assert store_path.read_text().count("HEARTBEAT_STALE") == 1, (
        "a re-offered journal entry must not duplicate its audit record"
    )


# --------------------------------------------------------------------------- #
# Refusing to half-read a snapshot
# --------------------------------------------------------------------------- #


def test_a_foreign_file_is_refused_whole(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.snapshot"
    path.write_text("some other tool's output\nstatus feed=broker stale=false\n")
    source = SnapshotHeartbeatSource(path, now_ns=lambda: 1)

    with pytest.raises(HeartbeatUnavailable) as exc:
        source.observe()
    assert MAGIC in str(exc.value)


def test_an_unknown_schema_version_is_refused_not_guessed(tmp_path: Path) -> None:
    """A future writer's layout must not be parsed as if it were version 1."""

    written_at = 1_000_000 * 1_000_000_000
    path = tmp_path / "heartbeat.snapshot"
    path.write_text(
        _snapshot(written_at, market_stale=False, broker_stale=False).replace(
            "schema_version=1", "schema_version=2"
        )
    )
    source = SnapshotHeartbeatSource(path, now_ns=lambda: written_at)

    with pytest.raises(HeartbeatUnavailable) as exc:
        source.observe()
    assert "schema_version=2" in str(exc.value)


def test_a_missing_snapshot_is_unavailable_not_empty_success(tmp_path: Path) -> None:
    source = SnapshotHeartbeatSource(tmp_path / "never-written", now_ns=lambda: 1)
    with pytest.raises(HeartbeatUnavailable):
        source.observe()


def test_a_broker_only_snapshot_does_not_pass_as_market_data_monitoring(
    tmp_path: Path,
) -> None:
    """SYS-39 names both feed kinds; half of it is not a healthy monitor."""

    written_at = 1_000_000 * 1_000_000_000
    full = _snapshot(written_at, market_stale=False, broker_stale=False)
    broker_only = "\n".join(line for line in full.splitlines() if "feed=market_data" not in line)
    path = tmp_path / "heartbeat.snapshot"
    path.write_text(broker_only)
    source = SnapshotHeartbeatSource(path, now_ns=lambda: written_at)

    with pytest.raises(HeartbeatUnavailable) as exc:
        source.observe()
    assert "market-data" in str(exc.value)


# --------------------------------------------------------------------------- #
# The producer side: only an ANSWER refreshes the broker line
# --------------------------------------------------------------------------- #


def test_a_rebuilt_session_is_observable_so_subscriptions_can_be_replaced() -> None:
    """Runs the Rust invariant behind reconnect recovery.

    ``reqMktData`` state lives in the session that opened it, and the transport
    rebuilds a dropped session transparently. If a subscription holder cannot
    tell that happened, it goes on polling ticker ids nobody publishes to — and
    a feed that is silent because we asked the wrong ids looks exactly like a
    feed that is silent because the market stopped. That is a permanent,
    false staleness alarm, so the generation signal is a safety invariant.
    """

    result = subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "atp-adapters",
            "--features",
            "ib-live-transport",
            "--test",
            "srs_md_003_ib_stream",
            "the_session_generation_advances_on_every_reconnect",
            "--",
            "--exact",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_only_an_answered_round_trip_refreshes_the_broker_line() -> None:
    """Runs the Rust invariant: a failed probe records no observation.

    A half-open TCP socket stays writable long after the peer is gone, so
    "we sent a heartbeat" is not evidence the gateway is alive. Only
    ``currentTime`` coming back is.
    """

    result = subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "atp-market-data",
            "--lib",
            "live_feed::tests::a_failed_round_trip_does_not_refresh_the_broker_line",
            "--",
            "--exact",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


# --- the same invariants, through the composition production actually uses ---
#
# The tests above build the provider by hand. A false all-clear that only the
# hand-built composition defends against is not defended against at all: what
# ships is whatever `mount_default_dashboard` assembles from the environment.


def test_a_dead_daemon_never_reads_healthy_through_the_default_mount(tmp_path: Path) -> None:
    """The headline invariant, end to end through the production mount.

    The daemon dies leaving a snapshot whose last verdict said everything was
    fresh. A file nobody is rewriting looks exactly like a file somebody is
    rewriting, so the age check is the only thing standing between the
    operator and a monitor that has stopped monitoring.
    """
    from atp_dashboard.server import SYSTEM_SNAPSHOT_PATH, mount_default_dashboard
    from atp_runtime import OperatorInterfaceRuntime

    snapshot = tmp_path / "heartbeat.snapshot"
    # Written well over the age limit ago, and it says FRESH.
    dead_at_ns = time.time_ns() - (THRESHOLD_MS + 60_000) * 1_000_000
    snapshot.write_text(
        _snapshot(dead_at_ns, market_stale=False, broker_stale=False), encoding="utf-8"
    )

    runtime = OperatorInterfaceRuntime()
    mount_default_dashboard(
        runtime,
        {"ATP_MD003_SNAPSHOT": str(snapshot), "ATP_MD003_LOG_DIR": str(tmp_path)},
    )

    _status, system = runtime.dispatch_rest("GET", SYSTEM_SNAPSHOT_PATH, b"")
    health = system["health"]["market_data_heartbeat"]
    assert health["state"] == "UNAVAILABLE", "a dead daemon's last FRESH verdict was served"
    assert health["any_stale"] is True, "not proven fresh must never render as fresh"
    assert health["ok"] is False


def test_a_live_mount_cannot_silently_drop_its_audit_trail(tmp_path: Path) -> None:
    """SRS-MD-003 makes "logged" a first-class acceptance leg. A live feed
    mounted without a durable sink would monitor and display while dropping
    every HEARTBEAT_STALE record — a configuration that must be unrepresentable,
    not a degraded mode nobody notices."""
    from atp_dashboard.server import mount_default_dashboard
    from atp_runtime import OperatorInterfaceRuntime

    with pytest.raises(ValueError, match="ATP_MD003_LOG_DIR"):
        mount_default_dashboard(
            OperatorInterfaceRuntime(),
            {"ATP_MD003_SNAPSHOT": str(tmp_path / "heartbeat.snapshot")},
        )


def test_a_fixture_script_cannot_masquerade_as_the_live_feed(tmp_path: Path) -> None:
    """Mounting both producers must fail closed at boot.

    This is a safety-surface attribution bug, not a config nicety: if the
    fixture silently won, an operator reading "FRESH" from a replayed script
    would believe a real market feed was flowing.
    """
    from atp_dashboard.server import mount_default_dashboard
    from atp_runtime import OperatorInterfaceRuntime

    snapshot = tmp_path / "heartbeat.snapshot"
    snapshot.write_text(
        _snapshot(time.time_ns(), market_stale=False, broker_stale=False), encoding="utf-8"
    )
    observations = tmp_path / "observations.txt"
    observations.write_text("watch-security AAPL equity\nwatch-broker\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one producer"):
        mount_default_dashboard(
            OperatorInterfaceRuntime(),
            {
                "ATP_MD003_SNAPSHOT": str(snapshot),
                "ATP_MD003_OBSERVATIONS": str(observations),
                "ATP_MD003_LOG_DIR": str(tmp_path),
            },
        )
