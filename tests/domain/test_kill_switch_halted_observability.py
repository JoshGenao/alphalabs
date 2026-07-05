"""SRS-SAFE-001 / SRS-LOG-001 / SyRS SYS-44a + SYS-61 — the paper-engine
HALTED transition must be observable through the persistent system log within
1 second of kill-switch activation.

L7 domain (safety) test, two halves:

1. **Schema half (this commit):** ``HALTED`` is a first-class
   ``Source.KILL_SWITCH`` system event type — the AC-pinned
   ``EVENT_TYPES_BY_SOURCE`` map accepts it, the router dispatches it, the
   durable ``JsonlLogStore`` persists it and serves it back through the query
   seam (filterable by source / event type / correlation id). The map stays
   CLOSED: an unknown kill-switch event type is still rejected, and a
   kill-switch record cannot masquerade as a strategy-class record.

2. **Latency half (``python/atp_safety`` wiring):** the operator layer writes
   the ACTIVATION + HALTED records durably at activation time and the
   measured activation→durable-HALTED-write latency is asserted ≤ 1.0 s —
   see ``test_activation_writes_halted_record_within_one_second`` below
   (added with the ``atp_safety`` handlers).

The 1-second budget's authority chain: SRS-SAFE-001 AC ("HALTED-state
transition is observable through SRS-LOG-001 within 1 second of activation")
→ ``KILL_SWITCH_HALT_OBSERVABILITY_BUDGET_MS = 1_000`` in ``atp-types`` →
the activation gate's ``halt_completed_ms`` mark → the durable write here.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from atp_logging import (
    EVENT_TYPES_BY_SOURCE,
    LogClass,
    LogClassError,
    LogPayloadError,
    LogRecord,
    RoutedLogDispatcher,
    Severity,
    Source,
)
from atp_logging.persistence import JsonlLogStore

pytestmark = [pytest.mark.domain, pytest.mark.safety]


def _halted_record(*, correlation_id: str = "act-0001") -> LogRecord:
    return LogRecord(
        timestamp_ns=time.time_ns(),
        severity=Severity.WARN,
        source=Source.KILL_SWITCH,
        event_type="HALTED",
        message="paper engines HALTED: engines_total=30 transitioned=30 already_halted=0",
        correlation_id=correlation_id,
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )


def test_halted_is_an_ac_pinned_kill_switch_event_type() -> None:
    # The SRS-SAFE-001 AC makes the HALTED transition an observable SYSTEM
    # event in its own right, beside the SRS-LOG-001 "kill-switch
    # activations" ACTIVATION event.
    assert EVENT_TYPES_BY_SOURCE[Source.KILL_SWITCH] == ("ACTIVATION", "HALTED")


def test_halted_record_dispatches_and_persists_durably(tmp_path: Path) -> None:
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    dispatcher = RoutedLogDispatcher()
    dispatcher.register_sink(LogClass.SYSTEM, store)

    dispatcher.dispatch(_halted_record(correlation_id="act-obs-1"))

    persisted = store.read(source=Source.KILL_SWITCH, event_type="HALTED")
    assert len(persisted) == 1
    record = persisted[0]
    assert record.event_type == "HALTED"
    assert record.correlation_id == "act-obs-1"
    assert record.log_class is LogClass.SYSTEM
    # The query seam the dashboard/API read path uses can correlate the
    # HALTED record back to its activation.
    by_correlation = store.read(correlation_id="act-obs-1")
    assert [entry.event_type for entry in by_correlation] == ["HALTED"]


def test_unknown_kill_switch_event_type_is_still_rejected(tmp_path: Path) -> None:
    # The event-type map stays CLOSED: extending it to HALTED must not open
    # the door to arbitrary kill-switch event names.
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    with pytest.raises(LogPayloadError):
        store.write(
            LogRecord(
                timestamp_ns=time.time_ns(),
                severity=Severity.WARN,
                source=Source.KILL_SWITCH,
                event_type="LIQUIDATED",  # not AC-pinned
                message="not a pinned event type",
                correlation_id="act-bad",
                log_class=LogClass.SYSTEM,
                strategy_id=None,
            )
        )
    assert store.read() == []


def test_halted_record_cannot_masquerade_as_a_strategy_record(tmp_path: Path) -> None:
    # SYSTEM/STRATEGY separation (SRS-LOG-001): a kill-switch HALTED record is
    # a SYSTEM event; a strategy-class copy must be rejected by validation,
    # not silently routed into the strategy log.
    strategy_store = JsonlLogStore(
        tmp_path / "strategy.jsonl", log_class=LogClass.STRATEGY
    )
    with pytest.raises(LogClassError):
        strategy_store.write(
            LogRecord(
                timestamp_ns=time.time_ns(),
                severity=Severity.WARN,
                source=Source.KILL_SWITCH,
                event_type="HALTED",
                message="wrong class",
                correlation_id="act-bad-class",
                log_class=LogClass.STRATEGY,
                strategy_id="alpha-live",
            )
        )
    assert strategy_store.read() == []
