"""L7 domain safety test for the SRS-LOG-001 persistent log sinks.

The SDK-surface dispatcher test (``tests/domain/test_log_record_dispatch.py``)
drives the routing boundary through *stub* sinks. This file drives the
*real* persistent sinks (``atp_logging.persistence``) and asserts the
audit-trail safety invariants the AC turns on:

* a safety-critical SYSTEM event (a kill-switch ACTIVATION, an IB
  disconnect) is durably persisted and survives a process restart — the
  audit trail is not lost on crash;
* the SYSTEM and STRATEGY classes are persisted to *physically separate*
  files, with no cross-contamination in either direction, even when a
  caller bypasses the dispatcher and writes straight to a store;
* the reader never *fabricates* a record from a torn (crash-interrupted)
  write, and *fails closed* on a genuinely corrupt complete line rather
  than skipping it;
* every one of the eight AC-named SYSTEM sources round-trips through the
  persistent store with its fields intact.

Scope: these tests exercise the in-process persistence path (a record
dispatched or written directly to the store). The core-runtime event
FORWARDING path — how Rust-owned system events (order routing, IB Gateway,
kill-switch) actually reach this operator store — is deferred (see
``log_persistence_contract.deferred``) and is not asserted here.

Marked ``safety`` + ``domain`` so the deterministic critic recognises this
file as the paired safety-path test for the persistence diff (the sinks
persist kill-switch activations, IB connectivity changes, and the other
safety-relevant system events).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging import (  # noqa: E402
    EVENT_TYPES_BY_SOURCE,
    SYSTEM_SOURCES,
    LogClass,
    LogClassError,
    LogPayloadError,
    LogRecord,
    Severity,
    Source,
)
from atp_logging.persistence import (  # noqa: E402
    JsonlLogStore,
    LogStoreCorruptionError,
    build_separated_log_dispatcher,
    read_records,
)

pytestmark = [pytest.mark.safety, pytest.mark.domain]


def _kill_switch_activation(ts: int = 1_700_000_000_000_000_000) -> LogRecord:
    return LogRecord(
        timestamp_ns=ts,
        severity=Severity.CRITICAL,
        source=Source.KILL_SWITCH,
        event_type="ACTIVATION",
        message="Operator triggered kill switch from dashboard",
        correlation_id="ks-2026-06-30-001",
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )


def test_safety_critical_event_survives_restart(tmp_path: Path) -> None:
    """A kill-switch activation persisted before a crash is readable after."""

    path = tmp_path / "system.jsonl"
    activation = _kill_switch_activation()
    # fsync on (the default) so the record is on disk before the "crash".
    store = JsonlLogStore(path, log_class=LogClass.SYSTEM)
    store.write(activation)
    # Simulate a crash: drop the writer without an orderly close.
    del store

    # A fresh process re-reads the durable trail.
    recovered = read_records(path)
    assert recovered == [activation]
    assert recovered[0].source is Source.KILL_SWITCH
    assert recovered[0].event_type == "ACTIVATION"


def test_system_and_strategy_never_cross_contaminate(tmp_path: Path) -> None:
    dispatcher, system_store, strategy_store = build_separated_log_dispatcher(tmp_path)
    with system_store, strategy_store:
        dispatcher.dispatch(_kill_switch_activation())
        dispatcher.dispatch(
            LogRecord(
                timestamp_ns=1_700_000_000_000_000_001,
                severity=Severity.INFO,
                source=Source.STRATEGY,
                event_type="rebalance_signal",
                message="strategy emitted a rebalance",
                correlation_id="strat-7",
                log_class=LogClass.STRATEGY,
                strategy_id="momentum-v2",
            )
        )
        system_records = system_store.read()
        strategy_records = strategy_store.read()

    # Each store holds ONLY its own class — the core SRS-LOG-001 invariant.
    assert all(r.log_class is LogClass.SYSTEM for r in system_records)
    assert all(r.log_class is LogClass.STRATEGY for r in strategy_records)
    assert len(system_records) == 1 and len(strategy_records) == 1
    assert system_records[0].source is Source.KILL_SWITCH
    assert strategy_records[0].strategy_id == "momentum-v2"
    # No strategy_id ever appears in the system file, and no kill-switch
    # source ever appears in the strategy file.
    assert "momentum-v2" not in (tmp_path / "system.jsonl").read_text()
    assert "kill_switch" not in (tmp_path / "strategy.jsonl").read_text()


def test_direct_write_cannot_bypass_separation(tmp_path: Path) -> None:
    """A caller that bypasses the dispatcher still cannot cross-contaminate."""

    strategy_record = LogRecord(
        timestamp_ns=1,
        severity=Severity.INFO,
        source=Source.STRATEGY,
        event_type="x",
        message="m",
        correlation_id="c",
        log_class=LogClass.STRATEGY,
        strategy_id="s",
    )
    with JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM) as system_store:
        with pytest.raises(LogClassError):
            system_store.write(strategy_record)
    # The misrouted strategy record left no trace in the system trail.
    assert read_records(tmp_path / "system.jsonl") == []


def test_reader_never_fabricates_on_torn_write(tmp_path: Path) -> None:
    path = tmp_path / "system.jsonl"
    good = _kill_switch_activation()
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
        store.write(good)
    # A crash mid-write leaves an unterminated fragment.
    with open(path, "ab") as fh:
        fh.write(b'{"timestamp_ns": 2, "severity": "CRIT')
    recovered = read_records(path)
    # The good record survives; the torn fragment is NOT turned into a record.
    assert recovered == [good]


def test_torn_multibyte_write_never_fabricates(tmp_path: Path) -> None:
    """A crash can tear a write mid multi-byte UTF-8 char; the good audit
    record before it must survive and the torn tail must not be fabricated."""

    path = tmp_path / "system.jsonl"
    good = _kill_switch_activation()
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
        store.write(good)
    with open(path, "ab") as fh:
        fh.write(b'{"message": "tor\xe2\x9c')  # truncated 3-byte char (✓ cut short)
    recovered = read_records(path)
    assert recovered == [good]


def test_reader_fails_closed_on_corruption(tmp_path: Path) -> None:
    path = tmp_path / "system.jsonl"
    # A complete (newline-terminated) but non-JSON line is corruption, not a
    # torn tail — the reader must fail closed rather than skip it.
    path.write_bytes(b"garbage-not-json\n")
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


def test_direct_write_rejects_malformed_audit_record(tmp_path: Path) -> None:
    """A malformed safety event written straight to the store (bypassing the
    dispatcher) must be refused, so the audit trail cannot be corrupted with
    an invalid or mis-attributed kill-switch record."""

    path = tmp_path / "system.jsonl"
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
        tampered = _kill_switch_activation()
        object.__setattr__(tampered, "timestamp_ns", -1)  # impossible audit time
        with pytest.raises(LogPayloadError):
            store.write(tampered)

        mislabelled = _kill_switch_activation()
        object.__setattr__(mislabelled, "strategy_id", "sneaked-in")  # SYSTEM ⇒ no strategy_id
        with pytest.raises(LogClassError):
            store.write(mislabelled)
    # Nothing malformed reached the durable trail.
    assert read_records(path) == []


def test_read_fails_closed_on_tampered_audit_record(tmp_path: Path) -> None:
    """A tampered audit line that is valid JSON but violates the log
    invariants (e.g. a kill-switch SYSTEM record altered to carry a
    strategy_id) must fail closed on read — never served as if genuine."""

    path = tmp_path / "system.jsonl"
    tampered = _kill_switch_activation().as_dict()
    tampered["strategy_id"] = "smuggled-in"  # SYSTEM records must not carry one
    path.write_bytes((json.dumps(tampered) + "\n").encode())
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


def test_every_system_source_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "system.jsonl"
    written: list[LogRecord] = []
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
        for i, source in enumerate(sorted(SYSTEM_SOURCES, key=lambda s: s.value)):
            event_type = EVENT_TYPES_BY_SOURCE[source][0]
            rec = LogRecord(
                timestamp_ns=1_000 + i,
                severity=Severity.INFO,
                source=source,
                event_type=event_type,
                message=f"{source.value} event",
                correlation_id=f"corr-{i}",
                log_class=LogClass.SYSTEM,
                strategy_id=None,
            )
            store.write(rec)
            written.append(rec)
    recovered = read_records(path)
    assert recovered == written
    # All eight AC-named system sources are represented.
    assert {r.source for r in recovered} == set(SYSTEM_SOURCES)
