"""Unit tests for the SRS-MD-003 Python bridge (``atp_dashboard.heartbeat``).

The ``CliHeartbeatSource`` kv parsing, the fail-closed unavailable states,
the ``HeartbeatFreshnessProvider``'s channel-payload mapping (exactly the
atp_ws HEARTBEAT ``payload_fields`` plus honest extras), and the once-per-flip
cross-poll LogRecord discipline — all against an injected fake runner (no
cargo build, no subprocess; the real binary is exercised by the Rust CLI
process tests and the L7 domain test).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from atp_dashboard.heartbeat import (
    THRESHOLD_MS,
    CliHeartbeatSource,
    HeartbeatFreshnessProvider,
    HeartbeatUnavailable,
)
from atp_logging import LogClass, Severity, Source
from atp_logging.persistence import JsonlLogStore
from atp_ws import EVENT_CHANNELS, Channel

T0 = 1_700_000_000_000_000_000
NOW = T0 + 15_000_000_001  # 1 ns over the 15 s budget


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["md003_heartbeat_cli", "-"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _stale_cli_output(evaluated_at_ns: int) -> str:
    return (
        f"event kind=HEARTBEAT_STALE feed=market_data symbol=AAPL asset_class=equity "
        f"staleness_ms=15000 last_observation_ns={T0} evaluated_at_ns={evaluated_at_ns} "
        f"threshold_ms=15000\n"
        f"event kind=HEARTBEAT_STALE feed=broker staleness_ms=15000 "
        f"last_observation_ns={T0} evaluated_at_ns={evaluated_at_ns} threshold_ms=15000\n"
        f"status feed=market_data symbol=AAPL asset_class=equity last_observation_ns={T0} "
        f"staleness_ms=15000 never_observed=false time_stale=true gap_stale=false stale=true "
        f"threshold_ms=15000 evaluated_at_ns={evaluated_at_ns}\n"
        f"status feed=broker last_observation_ns={T0} staleness_ms=15000 never_observed=false "
        f"time_stale=true gap_stale=false stale=true threshold_ms=15000 "
        f"evaluated_at_ns={evaluated_at_ns}\n"
    )


def _fresh_cli_output(evaluated_at_ns: int) -> str:
    return (
        f"status feed=market_data symbol=AAPL asset_class=equity last_observation_ns={T0} "
        f"staleness_ms=1000 never_observed=false time_stale=false gap_stale=false stale=false "
        f"threshold_ms=15000 evaluated_at_ns={evaluated_at_ns}\n"
        f"status feed=broker last_observation_ns={T0} staleness_ms=1000 never_observed=false "
        f"time_stale=false gap_stale=false stale=false threshold_ms=15000 "
        f"evaluated_at_ns={evaluated_at_ns}\n"
    )


class _RecordingStore:
    """Minimal in-memory log sink for tests that don't assert log content
    (the provider REQUIRES a sink — SRS-MD-003's 'logged' leg is mandatory)."""

    def __init__(self) -> None:
        self.written: list[object] = []

    def write(self, record: object) -> None:
        self.written.append(record)


@pytest.fixture()
def observations(tmp_path: Path) -> Path:
    script = tmp_path / "observations.txt"
    script.write_text(
        f"watch-security AAPL equity\nwatch-broker\n"
        f"tick AAPL equity 1 {T0}\nbroker-heartbeat {T0}\n",
        encoding="utf-8",
    )
    return script


def _source(
    observations: Path, stdout_for: dict[str, str], now_ns: int = NOW
) -> CliHeartbeatSource:
    """A source whose fake runner returns ``stdout_for['stdout']`` (mutable)."""

    def runner(argv, *, input, timeout):  # noqa: A002 - protocol signature
        assert argv[-1] == "-"
        assert f"evaluate {now_ns}" in input
        return _completed(stdout_for["stdout"])

    return CliHeartbeatSource(observations, runner=runner, now_ns=lambda: now_ns)


def _advancing_source(observations: Path, stdout_fn: dict[str, object]) -> CliHeartbeatSource:
    """A source with an ADVANCING clock (each poll is a strictly newer
    evaluation, like a real wall clock) whose fake runner renders
    ``stdout_fn['fn'](evaluated_at_ns)`` for the instant the poll stamped."""

    clock = {"now": NOW}

    def now_ns() -> int:
        clock["now"] += 1
        return clock["now"]

    def runner(argv, *, input, timeout):  # noqa: A002 - protocol signature
        match = re.search(r"evaluate (\d+)", input)
        assert match is not None
        return _completed(stdout_fn["fn"](int(match.group(1))))

    return CliHeartbeatSource(observations, runner=runner, now_ns=now_ns)


# --------------------------------------------------------------------------- #
# CliHeartbeatSource parsing
# --------------------------------------------------------------------------- #


def test_source_parses_status_and_event_lines(observations: Path) -> None:
    source = _source(observations, {"stdout": _stale_cli_output(NOW)})
    observation = source.observe()
    assert observation["evaluated_at_ns"] == NOW

    statuses = observation["statuses"]
    assert [row["feed"] for row in statuses] == ["market_data:AAPL", "ib_gateway"]
    for row in statuses:
        assert row["stale"] is True and row["time_stale"] is True and row["gap_stale"] is False
        assert row["staleness_ms"] == 15_000 and row["threshold_ms"] == THRESHOLD_MS
        assert row["last_observation_ns"] == T0 and row["never_observed"] is False

    events = observation["events"]
    assert [e["kind"] for e in events] == ["HEARTBEAT_STALE", "HEARTBEAT_STALE"]
    assert events[0]["feed"] == "market_data:AAPL" and events[1]["feed"] == "ib_gateway"


def test_source_parses_never_observed_rows_as_none(observations: Path) -> None:
    stdout = (
        f"status feed=market_data symbol=AAPL asset_class=equity last_observation_ns={T0} "
        f"staleness_ms=0 never_observed=false time_stale=false gap_stale=false stale=false "
        f"threshold_ms=15000 evaluated_at_ns={NOW}\n"
        f"status feed=broker last_observation_ns=none staleness_ms=none never_observed=true "
        f"time_stale=true gap_stale=false stale=true threshold_ms=15000 evaluated_at_ns={NOW}\n"
    )
    source = _source(observations, {"stdout": stdout})
    row = next(r for r in source.observe()["statuses"] if r["feed"] == "ib_gateway")
    assert row["staleness_ms"] is None and row["last_observation_ns"] is None
    assert row["never_observed"] is True and row["stale"] is True


@pytest.mark.parametrize(
    "stdout",
    [
        "garbage line\n",
        f"status feed=weird last_observation_ns=1 staleness_ms=1 never_observed=false "
        f"time_stale=false gap_stale=false stale=false threshold_ms=15000 evaluated_at_ns={NOW}\n",
        f"status feed=broker last_observation_ns=abc staleness_ms=1 never_observed=false "
        f"time_stale=false gap_stale=false stale=false threshold_ms=15000 evaluated_at_ns={NOW}\n",
        f"event kind=MYSTERY feed=broker staleness_ms=1 last_observation_ns=1 "
        f"evaluated_at_ns={NOW} threshold_ms=15000\n",
    ],
)
def test_source_fails_closed_on_grammar_drift(observations: Path, stdout: str) -> None:
    source = _source(observations, {"stdout": stdout})
    with pytest.raises(HeartbeatUnavailable):
        source.observe()


def test_source_fails_closed_when_nothing_is_watched(observations: Path) -> None:
    # Zero status rows = the script watches no feeds. A monitor monitoring
    # nothing must never read as healthy (SYS-39 continuous monitoring).
    source = _source(observations, {"stdout": ""})
    with pytest.raises(HeartbeatUnavailable, match="watches no feeds"):
        source.observe()
    provider = HeartbeatFreshnessProvider(source, log_store=_RecordingStore())
    (event,) = provider.heartbeat_events()
    assert event["is_stale"] is True and event["data_source"] == "unavailable"
    health = provider.health_summary()
    assert health["ok"] is False and health["any_stale"] is True


def test_source_fails_closed_when_broker_is_not_watched(observations: Path) -> None:
    # SYS-39 names the broker API connection as a required monitored feed:
    # a market-data-only script must not present as a healthy monitor.
    stdout = (
        f"status feed=market_data symbol=AAPL asset_class=equity last_observation_ns={T0} "
        f"staleness_ms=1000 never_observed=false time_stale=false gap_stale=false stale=false "
        f"threshold_ms=15000 evaluated_at_ns={NOW}\n"
    )
    source = _source(observations, {"stdout": stdout})
    with pytest.raises(HeartbeatUnavailable, match="does not watch the broker"):
        source.observe()
    health = HeartbeatFreshnessProvider(source, log_store=_RecordingStore()).health_summary()
    assert health["ok"] is False and health["any_stale"] is True
    assert health["state"] == "UNAVAILABLE"


def test_source_fails_closed_when_no_market_data_line_is_watched(
    observations: Path,
) -> None:
    # Codex R8 finding: SYS-39 monitors BOTH feed kinds — a broker-only
    # script must not present as successful market-data monitoring.
    stdout = (
        f"status feed=broker last_observation_ns={T0} staleness_ms=0 never_observed=false "
        f"time_stale=false gap_stale=false stale=false threshold_ms=15000 "
        f"evaluated_at_ns={NOW}\n"
    )
    source = _source(observations, {"stdout": stdout})
    with pytest.raises(HeartbeatUnavailable, match="watches no market-data line"):
        source.observe()
    health = HeartbeatFreshnessProvider(source, log_store=_RecordingStore()).health_summary()
    assert health["ok"] is False and health["any_stale"] is True


def test_source_rejects_foreign_evaluation_instants(observations: Path) -> None:
    # A status row stamped with an evaluate instant other than the one this
    # poll appended means the observation script embedded its own `evaluate`
    # — a historical replay must not masquerade as the current verdict.
    stdout = (
        f"status feed=broker last_observation_ns={T0} staleness_ms=0 never_observed=false "
        f"time_stale=false gap_stale=false stale=false threshold_ms=15000 "
        f"evaluated_at_ns={NOW - 5}\n"
    )
    source = _source(observations, {"stdout": stdout})
    with pytest.raises(HeartbeatUnavailable, match="foreign evaluation"):
        source.observe()


def test_source_surfaces_cli_refusal_and_missing_script(tmp_path: Path, observations: Path) -> None:
    def refusing_runner(argv, *, input, timeout):  # noqa: A002
        return _completed("", returncode=2, stderr="md003_heartbeat_cli: line 1: bad")

    with pytest.raises(HeartbeatUnavailable, match="exit 2"):
        CliHeartbeatSource(observations, runner=refusing_runner, now_ns=lambda: NOW).observe()

    with pytest.raises(HeartbeatUnavailable, match="unreadable"):
        CliHeartbeatSource(
            tmp_path / "missing.txt", runner=refusing_runner, now_ns=lambda: NOW
        ).observe()


# --------------------------------------------------------------------------- #
# HeartbeatFreshnessProvider — channel payload mapping
# --------------------------------------------------------------------------- #


def _heartbeat_payload_fields() -> set[str]:
    return set(next(c.payload_fields for c in EVENT_CHANNELS if c.name == Channel.HEARTBEAT))


def test_heartbeat_events_carry_the_declared_payload_fields(observations: Path) -> None:
    provider = HeartbeatFreshnessProvider(
        _source(observations, {"stdout": _stale_cli_output(NOW)}), log_store=_RecordingStore()
    )
    events = provider.heartbeat_events()
    assert len(events) == 2
    for event in events:
        assert set(event) >= _heartbeat_payload_fields()
        assert event["is_stale"] is True
        assert event["staleness_seconds"] == 15.0
        assert event["threshold_seconds"] == 15.0
        assert isinstance(event["last_tick_at"], str)  # ISO-8601 from last_observation_ns
        assert event["data_source"] == "md003_heartbeat_cli"
    assert {e["feed"] for e in events} == {"market_data:AAPL", "ib_gateway"}


def test_heartbeat_events_fail_closed_when_monitor_unavailable(observations: Path) -> None:
    def broken_runner(argv, *, input, timeout):  # noqa: A002
        raise OSError("binary missing")

    provider = HeartbeatFreshnessProvider(
        CliHeartbeatSource(observations, runner=broken_runner, now_ns=lambda: NOW),
        log_store=_RecordingStore(),
    )
    (event,) = provider.heartbeat_events()
    assert set(event) >= _heartbeat_payload_fields()
    assert event["is_stale"] is True and event["staleness_seconds"] is None
    assert event["data_source"] == "unavailable" and "binary missing" in str(event["reason"])

    snapshot = provider.heartbeat_snapshot()
    assert snapshot["ok"] is False and snapshot["any_stale"] is True
    assert snapshot["state"] == "UNAVAILABLE"

    health = provider.health_summary()
    assert health["ok"] is False and health["any_stale"] is True
    assert health["state"] == "UNAVAILABLE"


def test_health_summary_reports_stale_feeds(observations: Path) -> None:
    provider = HeartbeatFreshnessProvider(
        _source(observations, {"stdout": _stale_cli_output(NOW)}), log_store=_RecordingStore()
    )
    health = provider.health_summary()
    assert health["ok"] is False and health["state"] == "STALE"
    assert health["stale_feeds"] == ["ib_gateway", "market_data:AAPL"]
    assert health["threshold_ms"] == THRESHOLD_MS

    fresh = HeartbeatFreshnessProvider(
        _source(observations, {"stdout": _fresh_cli_output(NOW)}), log_store=_RecordingStore()
    )
    health = fresh.health_summary()
    assert health["ok"] is True and health["state"] == "FRESH" and health["any_stale"] is False


# --------------------------------------------------------------------------- #
# HeartbeatFreshnessProvider — once-per-flip LogRecord discipline
# --------------------------------------------------------------------------- #


def test_transitions_are_logged_once_per_flip_across_polls(
    observations: Path, tmp_path: Path
) -> None:
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    stdout_fn: dict[str, object] = {"fn": _fresh_cli_output}
    provider = HeartbeatFreshnessProvider(
        _advancing_source(observations, stdout_fn), log_store=store
    )

    # Three fresh polls: nothing logged (initially-healthy feeds are baseline).
    for _ in range(3):
        provider.heartbeat_events()
    # Flip stale; three more polls (mixing all three read surfaces): ONE
    # record per feed, not one per poll.
    stdout_fn["fn"] = _stale_cli_output
    provider.heartbeat_events()
    provider.heartbeat_snapshot()
    provider.health_summary()
    # Recover: one HEARTBEAT_RECOVERED per feed.
    stdout_fn["fn"] = _fresh_cli_output
    provider.heartbeat_events()
    store.close()

    records = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM).read()
    kinds = [(r.event_type, r.source, r.severity) for r in records]
    assert kinds == [
        ("HEARTBEAT_STALE", Source.MARKET_DATA, Severity.WARN),
        ("HEARTBEAT_STALE", Source.IB_GATEWAY, Severity.WARN),
        ("HEARTBEAT_RECOVERED", Source.MARKET_DATA, Severity.INFO),
        ("HEARTBEAT_RECOVERED", Source.IB_GATEWAY, Severity.INFO),
    ]
    assert all(r.log_class is LogClass.SYSTEM and r.strategy_id is None for r in records)
    assert all(r.correlation_id.startswith("md003:") for r in records)


def test_failed_log_write_is_surfaced_and_retried(observations: Path) -> None:
    class FailingOnceStore:
        def __init__(self) -> None:
            self.calls = 0
            self.written: list[object] = []

        def write(self, record: object) -> None:
            self.calls += 1
            if self.calls == 1:
                raise OSError("disk full")
            self.written.append(record)

    store = FailingOnceStore()
    stdout_fn: dict[str, object] = {"fn": _stale_cli_output}
    provider = HeartbeatFreshnessProvider(
        _advancing_source(observations, stdout_fn), log_store=store
    )

    provider.heartbeat_events()
    # Both feeds flipped stale: two writes attempted, the first (market_data)
    # failed and must NOT advance that feed's baseline; the second landed.
    assert store.calls == 2 and len(store.written) == 1
    # Next poll retries ONLY the lost record (same stale state, no new flip):
    # one more write, and the feed whose record landed is not duplicated.
    provider.heartbeat_events()
    assert store.calls == 3 and len(store.written) == 2
    kinds = [r.event_type for r in store.written]
    assert kinds.count("HEARTBEAT_STALE") == 2
    feeds = {r.correlation_id.split(":", 2)[1] for r in store.written}
    assert feeds == {"market_data", "ib_gateway"}


def test_provider_without_a_log_sink_is_unrepresentable(observations: Path) -> None:
    # Codex R4 finding: SRS-MD-003 makes "logged" mandatory — a provider
    # that monitors and displays while silently dropping its audit trail
    # must not be constructible.
    source = _source(observations, {"stdout": _stale_cli_output(NOW)})
    with pytest.raises(TypeError):
        HeartbeatFreshnessProvider(source)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="requires a durable log_store"):
        HeartbeatFreshnessProvider(source, log_store=None)  # type: ignore[arg-type]


def test_default_mount_requires_log_dir_when_monitoring_is_configured(
    observations: Path,
) -> None:
    from atp_dashboard.server import mount_default_dashboard
    from atp_runtime import OperatorInterfaceRuntime

    with pytest.raises(ValueError, match="ATP_MD003_LOG_DIR"):
        mount_default_dashboard(
            OperatorInterfaceRuntime(),
            {"ATP_MD003_OBSERVATIONS": str(observations)},
        )


def test_stale_default_timeout_fits_inside_the_channel_cadence() -> None:
    # Codex R3 finding: the subprocess budget must sit BELOW the HEARTBEAT
    # channel's declared refresh cadence, so a wedged CLI resolves to a
    # fail-closed unavailable row within the tick instead of stretching it.
    from atp_dashboard import heartbeat as hb

    cadence_s = next(c.refresh_seconds for c in EVENT_CHANNELS if c.name == Channel.HEARTBEAT)
    assert hb._DEFAULT_TIMEOUT_S < cadence_s


def test_timed_out_cli_reports_unavailable(observations: Path) -> None:
    def wedged_runner(argv, *, input, timeout):  # noqa: A002
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    provider = HeartbeatFreshnessProvider(
        CliHeartbeatSource(observations, runner=wedged_runner, now_ns=lambda: NOW),
        log_store=_RecordingStore(),
    )
    (event,) = provider.heartbeat_events()
    assert event["is_stale"] is True and event["data_source"] == "unavailable"


def test_older_evaluation_completing_late_cannot_fabricate_transitions(
    observations: Path,
) -> None:
    # Codex R3 finding: WS ticker / REST poll / health poll evaluate outside
    # the provider lock, so a slower OLDER evaluation can commit after a
    # newer one. Its verdicts are history: applying them would fabricate
    # recovered/stale flips that never happened.
    class ScriptedSource:
        def __init__(self, observations_seq) -> None:
            self._seq = list(observations_seq)

        def observe(self):
            return self._seq.pop(0)

    def obs(evaluated_at_ns: int, stale: bool):
        return {
            "statuses": [
                {
                    "feed": "ib_gateway",
                    "last_observation_ns": T0,
                    "staleness_ms": 16_000 if stale else 0,
                    "never_observed": False,
                    "time_stale": stale,
                    "gap_stale": False,
                    "stale": stale,
                    "threshold_ms": 15_000,
                }
            ],
            "events": [],
            "evaluated_at_ns": evaluated_at_ns,
        }

    class RecordingStore:
        def __init__(self) -> None:
            self.written: list[object] = []

        def write(self, record: object) -> None:
            self.written.append(record)

    store = RecordingStore()
    # Poll 1 commits the NEWER evaluation (T+2, stale). Poll 2 is the SLOWER
    # OLDER evaluation (T+1, fresh) finishing late — it must not log a
    # fabricated HEARTBEAT_RECOVERED nor regress the baseline. Poll 3 (T+3,
    # fresh) is a genuine recovery and logs exactly once.
    provider = HeartbeatFreshnessProvider(
        ScriptedSource([obs(NOW + 2, True), obs(NOW + 1, False), obs(NOW + 3, False)]),
        log_store=store,
    )
    provider.heartbeat_events()
    provider.heartbeat_events()
    provider.heartbeat_events()
    kinds = [r.event_type for r in store.written]
    assert kinds == ["HEARTBEAT_STALE", "HEARTBEAT_RECOVERED"], (
        "the late older evaluation must be display-only: no fabricated flip"
    )
    assert store.written[1].timestamp_ns == NOW + 3


def test_older_evaluation_is_not_served_to_operator_surfaces(observations: Path) -> None:
    # Codex R7 finding: a slower OLDER evaluation finishing late must not be
    # PUBLISHED either — an older fresh snapshot would overwrite a newer
    # committed stale one on the HEARTBEAT channel / health surfaces. Late
    # callers get the cached committed observation.
    class ScriptedSource:
        def __init__(self, seq) -> None:
            self._seq = list(seq)

        def observe(self):
            return self._seq.pop(0)

    def obs(evaluated_at_ns: int, stale: bool):
        return {
            "statuses": [
                {
                    "feed": "ib_gateway",
                    "last_observation_ns": T0,
                    "staleness_ms": 16_000 if stale else 0,
                    "never_observed": False,
                    "time_stale": stale,
                    "gap_stale": False,
                    "stale": stale,
                    "threshold_ms": 15_000,
                }
            ],
            "events": [],
            "evaluated_at_ns": evaluated_at_ns,
        }

    provider = HeartbeatFreshnessProvider(
        ScriptedSource([obs(NOW + 2, True), obs(NOW + 1, False)]),
        log_store=_RecordingStore(),
    )
    (newer,) = provider.heartbeat_events()  # commits the NEWER stale verdict
    assert newer["is_stale"] is True
    (late,) = provider.heartbeat_events()  # the OLDER fresh evaluation, late
    assert late["is_stale"] is True, (
        "an older fresh evaluation must not regress the displayed stale state"
    )


def test_gap_only_staleness_is_not_logged_as_heartbeat_staleness(
    observations: Path,
) -> None:
    # Codex R7 finding: a pure sequence-gap incident (time-fresh ticks, gap
    # in the sequence) is SRS-MD-007's SEQUENCE_GAP audit class. It must
    # still DISPLAY as stale (merged verdict) but must not write a
    # HEARTBEAT_STALE record misclassifying the incident.
    stdout = (
        f"status feed=market_data symbol=AAPL asset_class=equity last_observation_ns={T0} "
        f"staleness_ms=0 never_observed=false time_stale=false gap_stale=true stale=true "
        f"threshold_ms=15000 evaluated_at_ns={NOW}\n"
        f"status feed=broker last_observation_ns={T0} staleness_ms=0 never_observed=false "
        f"time_stale=false gap_stale=false stale=false threshold_ms=15000 "
        f"evaluated_at_ns={NOW}\n"
    )
    store = _RecordingStore()
    provider = HeartbeatFreshnessProvider(
        _source(observations, {"stdout": stdout}), log_store=store
    )
    events = provider.heartbeat_events()
    line = next(e for e in events if e["feed"] == "market_data:AAPL")
    assert line["is_stale"] is True and line["gap_stale"] is True
    assert store.written == [], "gap-only staleness must not file a HEARTBEAT record"
    health = provider.health_summary()
    assert health["any_stale"] is True, "the merged verdict still drives health"


def test_transient_stale_incident_survives_a_failed_write_and_fast_recovery(
    observations: Path,
) -> None:
    # Codex R6 finding: fresh -> stale (write FAILS) -> fresh again. With a
    # baseline-holdback scheme the next poll sees fresh==fresh and skips —
    # losing BOTH the stale incident and its recovery. The pending-record
    # queue must preserve both, in chronological order.
    class FailingOnceStore:
        def __init__(self) -> None:
            self.calls = 0
            self.written: list[object] = []

        def write(self, record: object) -> None:
            self.calls += 1
            if self.calls == 1:
                raise OSError("disk full")
            self.written.append(record)

    store = FailingOnceStore()

    class ScriptedSource:
        def __init__(self, seq) -> None:
            self._seq = list(seq)

        def observe(self):
            return self._seq.pop(0)

    def obs(evaluated_at_ns: int, stale: bool):
        return {
            "statuses": [
                {
                    "feed": "ib_gateway",
                    "last_observation_ns": T0,
                    "staleness_ms": 16_000 if stale else 0,
                    "never_observed": False,
                    "time_stale": stale,
                    "gap_stale": False,
                    "stale": stale,
                    "threshold_ms": 15_000,
                }
            ],
            "events": [],
            "evaluated_at_ns": evaluated_at_ns,
        }

    provider = HeartbeatFreshnessProvider(
        ScriptedSource([obs(NOW, False), obs(NOW + 1, True), obs(NOW + 2, False)]),
        log_store=store,
    )
    provider.heartbeat_events()  # fresh baseline, nothing to log
    provider.heartbeat_events()  # flips stale; the write FAILS -> queued
    provider.heartbeat_events()  # recovers; queued STALE retried, RECOVERED written
    kinds = [r.event_type for r in store.written]
    assert kinds == ["HEARTBEAT_STALE", "HEARTBEAT_RECOVERED"], (
        "a transient sink failure must lose neither the incident nor the recovery"
    )
    assert store.written[0].timestamp_ns == NOW + 1
    assert store.written[1].timestamp_ns == NOW + 2


def test_pending_log_queue_is_bounded_with_honest_loss_accounting(
    observations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atp_dashboard import heartbeat as hb

    monkeypatch.setattr(hb, "_MAX_PENDING_LOG_RECORDS", 2)

    class AlwaysFailingStore:
        def write(self, record: object) -> None:
            raise OSError("disk full")

    class ScriptedSource:
        def __init__(self) -> None:
            self.n = 0

        def observe(self):
            # Alternate stale/fresh so every poll produces one transition.
            self.n += 1
            stale = self.n % 2 == 1
            return {
                "statuses": [
                    {
                        "feed": "ib_gateway",
                        "last_observation_ns": T0,
                        "staleness_ms": 16_000 if stale else 0,
                        "never_observed": False,
                        "time_stale": stale,
                        "gap_stale": False,
                        "stale": stale,
                        "threshold_ms": 15_000,
                    }
                ],
                "events": [],
                "evaluated_at_ns": NOW + self.n,
            }

    provider = HeartbeatFreshnessProvider(ScriptedSource(), log_store=AlwaysFailingStore())
    for _ in range(5):
        provider.heartbeat_events()
    snapshot = provider.heartbeat_snapshot()  # one more poll (6th transition)
    assert snapshot["log_write_ok"] is False
    assert snapshot["pending_log_records"] == 2, "queue bounded at the ceiling"
    assert snapshot["dropped_log_records"] == 4, "overflow counted, never silent"


def test_one_feeds_failed_write_is_not_masked_by_anothers_success(
    observations: Path,
) -> None:
    # Codex R2 finding: within one poll, a later successful write must not
    # reset the lost-audit flag that an earlier failed write raised.
    class MarketDataFailingStore:
        def __init__(self) -> None:
            self.fail_market_data = True
            self.written: list[object] = []

        def write(self, record: object) -> None:
            if self.fail_market_data and record.source is Source.MARKET_DATA:
                raise OSError("disk full")
            self.written.append(record)

    store = MarketDataFailingStore()
    stdout_fn: dict[str, object] = {"fn": _stale_cli_output}
    provider = HeartbeatFreshnessProvider(
        _advancing_source(observations, stdout_fn), log_store=store
    )

    # This poll: market_data write FAILS, ib_gateway write SUCCEEDS — the
    # snapshot (whose own poll re-attempts and fails again) must still say
    # the audit trail is incomplete.
    provider.heartbeat_events()
    assert provider.heartbeat_snapshot()["log_write_ok"] is False
    # Heal the sink: the retried market_data record lands and the flag
    # recovers on a fully-successful poll.
    store.fail_market_data = False
    assert provider.heartbeat_snapshot()["log_write_ok"] is True
    assert {r.source for r in store.written} == {Source.MARKET_DATA, Source.IB_GATEWAY}
