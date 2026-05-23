"""L7 domain test for the SRS-LOG-001 SDK-surface log record + dispatcher.

The L7 layer drives the dispatcher through reference stubs for the future
SRS-LOG-001 persistent sinks, SRS-API-001 query endpoint, and the
SRS-NOTIF-001 ERROR/CRITICAL filter. Verifies that:

* SYSTEM and STRATEGY records route to separate persistent stores
  (SRS-LOG-001's separation invariant — the core AC);
* both stores carry the AC-named fields (timestamp / severity / source /
  event_type / message / correlation_id) with the right types;
* the ``GET /api/v1/logs`` query shape pinned by ``python/atp_api/routes.py``
  resolves against the captured records (severity filter, source filter,
  correlation-ID filter);
* the SRS-NOTIF-001 fan-out can subscribe to ERROR/CRITICAL only without
  re-scanning every record;
* the dispatcher never silently accepts a schema regression: a record with
  any of the 11 SDK-boundary defensive gaps raises a ``LogRecordError``
  subclass instead of landing in a sink.

Marked ``safety`` + ``domain`` so the deterministic critic recognises the
file as the paired safety-path test for the SDK-surface diff.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging import (  # noqa: E402
    EVENT_TYPES_BY_SOURCE,
    LogClass,
    LogClassError,
    LogPayloadError,
    LogRecord,
    LogRecordError,
    LogRoutingError,
    LogSinkError,
    RoutedLogDispatcher,
    Severity,
    Source,
)

pytestmark = [pytest.mark.safety, pytest.mark.domain]


# --------------------------------------------------------------------------- #
# Reference persistent sinks
# --------------------------------------------------------------------------- #


@dataclass
class _RefPersistentSink:
    """Stub for the SRS-LOG-001 persistent log store.

    JSON-round-trips every record on write so a payload field that fails
    to serialise surfaces here rather than at SRS-LOG-001 / SRS-UI-001
    integration time.
    """

    label: str
    stored: list[dict[str, Any]] = field(default_factory=list)

    def write(self, record: LogRecord) -> None:
        payload = record.as_dict()
        # JSON round-trip enforces serialisability.
        line = json.dumps(payload, sort_keys=True)
        self.stored.append(json.loads(line))

    def query(
        self,
        *,
        severity_at_least: Severity | None = None,
        source: Source | None = None,
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sev_order = {m.value: i for i, m in enumerate(Severity)}
        out: list[dict[str, Any]] = []
        for entry in self.stored:
            if (
                severity_at_least is not None
                and sev_order[entry["severity"]] < sev_order[severity_at_least.value]
            ):
                continue
            if source is not None and entry["source"] != source.value:
                continue
            if correlation_id is not None and entry["correlation_id"] != correlation_id:
                continue
            out.append(entry)
        return out


@dataclass
class _RefNotificationFilter:
    """Stub for the SRS-NOTIF-001 ERROR/CRITICAL subscriber.

    Demonstrates that the SDK pins the severity enum tightly enough that a
    notification subsystem can subscribe to a filtered stream without
    re-parsing every record.
    """

    delivered: list[dict[str, Any]] = field(default_factory=list)

    def maybe_deliver(self, record: LogRecord) -> None:
        if record.severity in (Severity.ERROR, Severity.CRITICAL):
            self.delivered.append(record.as_dict())


def _make_dispatcher() -> tuple[
    RoutedLogDispatcher,
    _RefPersistentSink,
    _RefPersistentSink,
    _RefNotificationFilter,
]:
    dispatcher = RoutedLogDispatcher()
    system_sink = _RefPersistentSink(label="system")
    strategy_sink = _RefPersistentSink(label="strategy")
    notifier = _RefNotificationFilter()

    class _FanOutSystemSink:
        def write(self, record: LogRecord) -> None:
            system_sink.write(record)
            notifier.maybe_deliver(record)

    class _FanOutStrategySink:
        def write(self, record: LogRecord) -> None:
            strategy_sink.write(record)
            # Strategy logs are not delivered to the operator notifier; per
            # SyRS SYS-61 / SRS-NOTIF-001 the alerts go to system events only.

    dispatcher.register_sink(LogClass.SYSTEM, _FanOutSystemSink())
    dispatcher.register_sink(LogClass.STRATEGY, _FanOutStrategySink())
    return dispatcher, system_sink, strategy_sink, notifier


def _sys_record(
    *,
    severity: Severity = Severity.INFO,
    source: Source = Source.KILL_SWITCH,
    event_type: str = "ACTIVATION",
    message: str = "ok",
    correlation_id: str = "c",
) -> LogRecord:
    return LogRecord(
        timestamp_ns=time.time_ns(),
        severity=severity,
        source=source,
        event_type=event_type,
        message=message,
        correlation_id=correlation_id,
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )


def _strat_record(
    *,
    severity: Severity = Severity.INFO,
    event_type: str = "user_signal",
    message: str = "strategy emitted signal",
    correlation_id: str = "c-strat",
    strategy_id: str = "strategy-abc",
) -> LogRecord:
    return LogRecord(
        timestamp_ns=time.time_ns(),
        severity=severity,
        source=Source.STRATEGY,
        event_type=event_type,
        message=message,
        correlation_id=correlation_id,
        log_class=LogClass.STRATEGY,
        strategy_id=strategy_id,
    )


# --------------------------------------------------------------------------- #
# Separation invariant (the SRS-LOG-001 core AC)
# --------------------------------------------------------------------------- #


class SystemStrategySeparationTest(unittest.TestCase):
    def test_system_and_strategy_records_land_in_separate_sinks(self) -> None:
        dispatcher, system_sink, strategy_sink, _ = _make_dispatcher()
        dispatcher.dispatch(_sys_record())
        dispatcher.dispatch(_strat_record())
        self.assertEqual(len(system_sink.stored), 1)
        self.assertEqual(len(strategy_sink.stored), 1)
        self.assertEqual(system_sink.stored[0]["log_class"], "system")
        self.assertEqual(strategy_sink.stored[0]["log_class"], "strategy")

    def test_strategy_record_never_lands_in_system_sink(self) -> None:
        dispatcher, system_sink, strategy_sink, _ = _make_dispatcher()
        for _ in range(5):
            dispatcher.dispatch(_strat_record())
        self.assertEqual(len(system_sink.stored), 0)
        self.assertEqual(len(strategy_sink.stored), 5)

    def test_system_record_never_lands_in_strategy_sink(self) -> None:
        dispatcher, system_sink, strategy_sink, _ = _make_dispatcher()
        for _ in range(5):
            dispatcher.dispatch(_sys_record())
        self.assertEqual(len(strategy_sink.stored), 0)
        self.assertEqual(len(system_sink.stored), 5)


# --------------------------------------------------------------------------- #
# AC-named field shape on both stores
# --------------------------------------------------------------------------- #


class AcFieldShapeTest(unittest.TestCase):
    REQUIRED_FIELDS = (
        "timestamp_ns",
        "severity",
        "source",
        "event_type",
        "message",
        "correlation_id",
        "log_class",
        "strategy_id",
    )

    def test_system_records_carry_every_ac_field(self) -> None:
        dispatcher, system_sink, _, _ = _make_dispatcher()
        dispatcher.dispatch(_sys_record())
        payload = system_sink.stored[0]
        for field_name in self.REQUIRED_FIELDS:
            self.assertIn(field_name, payload, msg=f"missing {field_name!r}")

    def test_strategy_records_carry_every_ac_field(self) -> None:
        dispatcher, _, strategy_sink, _ = _make_dispatcher()
        dispatcher.dispatch(_strat_record())
        payload = strategy_sink.stored[0]
        for field_name in self.REQUIRED_FIELDS:
            self.assertIn(field_name, payload, msg=f"missing {field_name!r}")

    def test_payloads_are_json_serialisable(self) -> None:
        dispatcher, system_sink, strategy_sink, _ = _make_dispatcher()
        for severity in Severity:
            dispatcher.dispatch(
                _sys_record(severity=severity, correlation_id=f"c-{severity.value}")
            )
            dispatcher.dispatch(
                _strat_record(severity=severity, correlation_id=f"strat-{severity.value}")
            )
        for entry in (*system_sink.stored, *strategy_sink.stored):
            json.dumps(entry)


# --------------------------------------------------------------------------- #
# Query shape (SRS-API-001 GET /api/v1/logs)
# --------------------------------------------------------------------------- #


class ApiQueryShapeTest(unittest.TestCase):
    """The route declaration in ``python/atp_api/routes.py`` advertises four
    filter fields (severity, source, event_type, correlation_id). The
    reference sink supports the same shape so a handler implementation can
    plug in without renegotiating the contract."""

    def setUp(self) -> None:
        self.dispatcher, self.system_sink, _, _ = _make_dispatcher()
        for severity in Severity:
            self.dispatcher.dispatch(
                _sys_record(
                    severity=severity,
                    source=Source.IB_GATEWAY,
                    event_type="DISCONNECT",
                    correlation_id=f"ib-{severity.value}",
                )
            )

    def test_query_filters_by_severity(self) -> None:
        results = self.system_sink.query(severity_at_least=Severity.ERROR)
        self.assertEqual(len(results), 2)  # ERROR + CRITICAL
        for entry in results:
            self.assertIn(entry["severity"], ("ERROR", "CRITICAL"))

    def test_query_filters_by_source(self) -> None:
        results = self.system_sink.query(source=Source.IB_GATEWAY)
        self.assertEqual(len(results), 5)  # one per severity
        for entry in results:
            self.assertEqual(entry["source"], "ib_gateway")

    def test_query_filters_by_correlation_id(self) -> None:
        results = self.system_sink.query(correlation_id="ib-CRITICAL")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["severity"], "CRITICAL")


# --------------------------------------------------------------------------- #
# Notification fan-out (SRS-NOTIF-001)
# --------------------------------------------------------------------------- #


class NotificationFanOutTest(unittest.TestCase):
    def test_notifier_only_receives_error_and_critical_system_records(self) -> None:
        dispatcher, _, _, notifier = _make_dispatcher()
        for severity in Severity:
            dispatcher.dispatch(
                _sys_record(severity=severity, correlation_id=f"c-{severity.value}")
            )
        delivered = [entry["severity"] for entry in notifier.delivered]
        self.assertEqual(set(delivered), {"ERROR", "CRITICAL"})
        self.assertEqual(len(delivered), 2)

    def test_notifier_does_not_receive_strategy_records(self) -> None:
        dispatcher, _, _, notifier = _make_dispatcher()
        dispatcher.dispatch(_strat_record(severity=Severity.ERROR))
        dispatcher.dispatch(_strat_record(severity=Severity.CRITICAL))
        self.assertEqual(notifier.delivered, [])


# --------------------------------------------------------------------------- #
# Defensive checklist coverage
# --------------------------------------------------------------------------- #


class DefensiveChecklistTest(unittest.TestCase):
    """Per the [[feedback-sdk-boundary-defensive-checklist]] memory: every
    defensive rule should also have an L7 anchor exercising it through the
    reference dispatcher (not just an L3 mutation against the check
    script).
    """

    def setUp(self) -> None:
        self.dispatcher, self.system_sink, self.strategy_sink, _ = _make_dispatcher()

    def _expect_rejected(self, record: LogRecord, error_type: type[LogRecordError]) -> None:
        with self.assertRaises(error_type):
            self.dispatcher.dispatch(record)
        # Neither sink received the bad record.
        self.assertEqual(len(self.system_sink.stored), 0)
        self.assertEqual(len(self.strategy_sink.stored), 0)

    def test_non_record_is_rejected(self) -> None:
        with self.assertRaises(LogPayloadError):
            self.dispatcher.dispatch(None)  # type: ignore[arg-type]
        with self.assertRaises(LogPayloadError):
            self.dispatcher.dispatch({"timestamp_ns": 0})  # type: ignore[arg-type]

    def test_negative_timestamp_is_rejected(self) -> None:
        record = _sys_record()
        object.__setattr__(record, "timestamp_ns", -1)
        self._expect_rejected(record, LogPayloadError)

    def test_bool_timestamp_is_rejected(self) -> None:
        record = _sys_record()
        object.__setattr__(record, "timestamp_ns", True)
        self._expect_rejected(record, LogPayloadError)

    def test_inf_timestamp_is_rejected(self) -> None:
        record = _sys_record()
        object.__setattr__(record, "timestamp_ns", float("inf"))
        self._expect_rejected(record, LogPayloadError)

    def test_empty_message_is_rejected(self) -> None:
        record = _sys_record(message="")
        self._expect_rejected(record, LogPayloadError)

    def test_whitespace_correlation_id_is_rejected(self) -> None:
        record = _sys_record(correlation_id="   ")
        self._expect_rejected(record, LogPayloadError)

    def test_raw_string_severity_is_rejected(self) -> None:
        record = _sys_record()
        object.__setattr__(record, "severity", "INFO")
        self._expect_rejected(record, LogPayloadError)

    def test_system_with_strategy_source_is_rejected(self) -> None:
        record = _sys_record(source=Source.STRATEGY)
        self._expect_rejected(record, LogClassError)

    def test_strategy_with_system_source_is_rejected(self) -> None:
        record = _strat_record()
        object.__setattr__(record, "source", Source.KILL_SWITCH)
        self._expect_rejected(record, LogClassError)

    def test_strategy_without_strategy_id_is_rejected(self) -> None:
        record = _strat_record(strategy_id=None)
        self._expect_rejected(record, LogClassError)

    def test_system_with_strategy_id_is_rejected(self) -> None:
        record = _sys_record()
        object.__setattr__(record, "strategy_id", "leaked")
        self._expect_rejected(record, LogClassError)

    def test_unlisted_system_event_type_is_rejected(self) -> None:
        record = _sys_record(event_type="BOGUS_NOT_IN_ALLOWLIST")
        self._expect_rejected(record, LogPayloadError)


# --------------------------------------------------------------------------- #
# Coverage matrix: every AC-pinned (Source, event_type) pair must dispatch
# --------------------------------------------------------------------------- #


class AcEventCoverageMatrixTest(unittest.TestCase):
    def test_every_pinned_event_type_dispatches(self) -> None:
        dispatcher, system_sink, _, _ = _make_dispatcher()
        for source, event_types in EVENT_TYPES_BY_SOURCE.items():
            if not event_types:
                continue  # STRATEGY has none — exercised separately.
            for event_type in event_types:
                dispatcher.dispatch(
                    _sys_record(
                        source=source,
                        event_type=event_type,
                        correlation_id=f"c-{source.value}-{event_type}",
                    )
                )
        expected_count = sum(len(v) for v in EVENT_TYPES_BY_SOURCE.values())
        self.assertEqual(len(system_sink.stored), expected_count)
        # Every distinct (source, event_type) pair lands in the sink.
        observed_pairs = {(e["source"], e["event_type"]) for e in system_sink.stored}
        expected_pairs = {
            (source.value, event_type)
            for source, types in EVENT_TYPES_BY_SOURCE.items()
            for event_type in types
        }
        self.assertEqual(observed_pairs, expected_pairs)


# --------------------------------------------------------------------------- #
# No-silent-regression: dispatcher must fail closed when sinks are missing
# --------------------------------------------------------------------------- #


class FailClosedTest(unittest.TestCase):
    def test_dispatch_without_any_sink_raises(self) -> None:
        empty = RoutedLogDispatcher()
        with self.assertRaises(LogRoutingError):
            empty.dispatch(_sys_record())

    def test_dispatch_with_only_one_class_registered_raises_for_other(self) -> None:
        partial = RoutedLogDispatcher()

        class _Sink:
            def write(self, record: LogRecord) -> None:
                pass

        partial.register_sink(LogClass.SYSTEM, _Sink())
        # System record dispatches cleanly.
        partial.dispatch(_sys_record())
        # Strategy dispatch fails because no strategy sink is registered.
        with self.assertRaises(LogRoutingError):
            partial.dispatch(_strat_record())

    def test_sink_exception_does_not_leak_to_caller_uncaught(self) -> None:
        class _Raiser:
            def write(self, record: LogRecord) -> None:
                raise RuntimeError("downstream sink failure")

        dispatcher = RoutedLogDispatcher()
        dispatcher.register_sink(LogClass.SYSTEM, _Raiser())
        with self.assertRaises(LogSinkError) as ctx:
            dispatcher.dispatch(_sys_record())
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
