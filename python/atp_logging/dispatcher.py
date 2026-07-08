"""``RoutedLogDispatcher`` — the SRS-LOG-001 sink-routing boundary.

The dispatcher validates every :class:`LogRecord` against the AC-pinned
schema and routes it to the registered ``LogSink`` for its
:class:`LogClass`. The validation rules mirror SDK-boundary defensive
practice (see ``feedback-sdk-boundary-defensive-checklist`` in the project
memory): payload-shape guard, discriminant validation, type/finite checks,
cross-field invariants, lifecycle consistency, and non-empty contracts on
the string fields.

The :class:`LogSink` protocol declares the single ``write`` method
downstream sinks must implement. The persistent sinks themselves (file,
SQLite, Loki, dashboard publisher) are deferred to SRS-LOG-001 — this
module pins the seam they will plug into.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .errors import (
    LogClassError,
    LogPayloadError,
    LogRecordError,
    LogRoutingError,
    LogSinkError,
)
from .records import (
    EVENT_TYPES_BY_SOURCE,
    STRATEGY_SOURCES,
    SYSTEM_SOURCES,
    LogClass,
    LogRecord,
    Severity,
    Source,
    is_finite_non_negative_int,
)
from .redaction import SecretRedactor


@runtime_checkable
class LogSink(Protocol):
    """The single-method protocol every persistent log sink must implement.

    The dispatcher passes a fully-validated :class:`LogRecord` to ``write``.
    A sink that rejects the record should raise any exception; the
    dispatcher wraps it in :class:`LogSinkError` so callers can handle the
    :class:`LogRecordError` family uniformly.
    """

    def write(self, record: LogRecord) -> None:
        """Persist a validated record.

        Implementations decide their own persistence semantics — file
        append, network publish, in-memory buffer. The contract here is
        only that ``write`` must accept any record the dispatcher passes.
        """

        ...


_STRING_FIELDS: tuple[str, ...] = ("event_type", "message", "correlation_id")
"""Non-empty string fields validated on every record."""


class RoutedLogDispatcher:
    """Validate + route :class:`LogRecord` instances to the registered sinks.

    A single instance owns at most one :class:`LogSink` per
    :class:`LogClass`. ``register_sink`` is idempotent for re-binding to a
    new sink (the previous one is dropped) but raises if asked to route
    to a class that no sink is bound to. ``dispatch`` is single-threaded
    by design: callers must serialise access (or wrap the dispatcher with
    their own lock).
    """

    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        self._sinks: dict[LogClass, LogSink] = {}
        self._redactor = redactor

    def register_sink(self, log_class: LogClass, sink: LogSink) -> None:
        """Bind ``sink`` to ``log_class``.

        The dispatcher does NOT auto-discover sinks; the registration is
        explicit so the SRS-LOG-001 boot path is the single place that
        wires the system + strategy sinks (which under the AC live in
        separate persistent stores).
        """

        if not isinstance(log_class, LogClass):
            raise LogPayloadError(
                f"register_sink expected LogClass; got {type(log_class).__name__}"
            )
        if not isinstance(sink, LogSink):
            raise LogPayloadError(
                f"register_sink expected an object implementing the LogSink "
                f"protocol; got {type(sink).__name__}"
            )
        self._sinks[log_class] = sink

    def sink_for(self, log_class: LogClass) -> LogSink | None:
        """Return the registered sink for ``log_class`` or ``None``.

        Exposed primarily for the contract check + the L3 / L7 test rigs;
        production callers should rely on :meth:`dispatch` for routing.
        """

        return self._sinks.get(log_class)

    def dispatch(self, record: LogRecord) -> None:
        """Validate ``record`` then route it to the sink for its log_class.

        Raises:
            LogPayloadError: record is not a :class:`LogRecord`, a field
                has the wrong type, a numeric field is out of range, or a
                non-empty string field is empty / whitespace-only.
            LogClassError: cross-field invariants between ``log_class``,
                ``source``, and ``strategy_id`` are violated.
            LogRoutingError: no sink is registered for the record's
                ``log_class``.
            LogSinkError: the registered sink raised an exception; the
                original exception is preserved on ``__cause__``.
        """

        validate_log_record(record)

        # SRS-SEC-001: scrub credentials before the record reaches any sink so
        # an IB/SMTP/SMS secret embedded in a message can never be persisted in
        # plaintext. Redaction preserves schema validity (message /
        # correlation_id stay non-empty), so the sink's own validation still
        # holds.
        if self._redactor is not None:
            record = self._redactor.redact_record(record)

        sink = self._sinks.get(record.log_class)
        if sink is None:
            raise LogRoutingError(
                f"no LogSink registered for log_class={record.log_class.value!r}; "
                "register_sink(...) must run before dispatch"
            )
        try:
            sink.write(record)
        except LogRecordError:
            raise
        except Exception as exc:  # noqa: BLE001  (broad-except is intentional here)
            raise LogSinkError(
                f"LogSink for log_class={record.log_class.value!r} raised "
                f"{type(exc).__name__}: {exc}"
            ) from exc


# ---------------------------------------------------------------------- #
# Shared validation — reused by dispatch() and by the persistent sinks
# (atp_logging.persistence) so a record written DIRECTLY to a store, not
# through the dispatcher, is held to the same audit-trail invariants.
# ---------------------------------------------------------------------- #


def validate_log_record(record: LogRecord) -> None:
    """Validate ``record`` against the full SDK schema + log-class invariants.

    The single source of truth for "is this record fit to persist?" —
    :meth:`RoutedLogDispatcher.dispatch` and every concrete
    :class:`LogSink` (``atp_logging.persistence.JsonlLogStore.write``) call
    it, so a record cannot reach a persistent audit trail with an invalid
    timestamp, an empty required field, a forbidden ``strategy_id`` on a
    SYSTEM record, or a source/event-type outside the SyRS SYS-61 taxonomy —
    whether or not it went through the dispatcher.

    Raises:
        LogPayloadError: ``record`` is not a :class:`LogRecord`, a field has
            the wrong type/range, or a non-empty string field is empty.
        LogClassError: the cross-field invariants between ``log_class``,
            ``source``, and ``strategy_id`` are violated.
    """

    if not isinstance(record, LogRecord):
        raise LogPayloadError(
            f"validate_log_record expected a LogRecord; got {type(record).__name__}"
        )
    _assert_payload_shape(record)
    _assert_log_class_invariants(record)


def _assert_payload_shape(record: LogRecord) -> None:
    # Discriminant validation: enums must be exact enum members, not
    # raw strings (StrEnum subclasses str and would equality-match in
    # an `in` check, but the value semantics depend on enum identity).
    if not isinstance(record.severity, Severity):
        raise LogPayloadError(
            f"LogRecord.severity must be a Severity enum member; "
            f"got {type(record.severity).__name__}"
        )
    if not isinstance(record.source, Source):
        raise LogPayloadError(
            f"LogRecord.source must be a Source enum member; got {type(record.source).__name__}"
        )
    if not isinstance(record.log_class, LogClass):
        raise LogPayloadError(
            f"LogRecord.log_class must be a LogClass enum member; "
            f"got {type(record.log_class).__name__}"
        )

    # Numeric type / range / finite guard on timestamp_ns.
    timestamp_ns = record.timestamp_ns
    if not is_finite_non_negative_int(timestamp_ns):
        raise LogPayloadError(
            f"LogRecord.timestamp_ns must be a non-negative finite int "
            f"(bool / float / str / None / negative rejected); "
            f"got {type(timestamp_ns).__name__}={timestamp_ns!r}"
        )

    # Non-empty string-field guard.
    for field_name in _STRING_FIELDS:
        value = getattr(record, field_name, None)
        if not isinstance(value, str):
            raise LogPayloadError(
                f"LogRecord.{field_name} must be a non-empty str; got {type(value).__name__}"
            )
        if not value.strip():
            raise LogPayloadError(
                f"LogRecord.{field_name} must be non-empty (whitespace-only rejected)"
            )


def _assert_log_class_invariants(record: LogRecord) -> None:
    # SRS-LOG-001 separation: SYSTEM records use a SYSTEM source and
    # must NOT carry strategy_id; STRATEGY records use Source.STRATEGY
    # and must carry a non-empty strategy_id.
    if record.log_class is LogClass.SYSTEM:
        if record.source not in SYSTEM_SOURCES:
            raise LogClassError(
                f"LogRecord.log_class=SYSTEM forbids source={record.source.value!r}; "
                f"SYSTEM sources are {sorted(s.value for s in SYSTEM_SOURCES)}"
            )
        if record.strategy_id is not None:
            raise LogClassError(
                "LogRecord.log_class=SYSTEM must not carry strategy_id; "
                f"got strategy_id={record.strategy_id!r}"
            )
        allowed_event_types = EVENT_TYPES_BY_SOURCE[record.source]
        if record.event_type not in allowed_event_types:
            raise LogPayloadError(
                f"LogRecord.event_type={record.event_type!r} not allowed for "
                f"source={record.source.value!r}; "
                f"allowed types: {list(allowed_event_types)}"
            )
    elif record.log_class is LogClass.STRATEGY:
        if record.source not in STRATEGY_SOURCES:
            raise LogClassError(
                f"LogRecord.log_class=STRATEGY forbids source={record.source.value!r}; "
                f"STRATEGY records must use Source.STRATEGY"
            )
        strategy_id = record.strategy_id
        if not isinstance(strategy_id, str):
            raise LogClassError(
                "LogRecord.log_class=STRATEGY requires a non-empty strategy_id; "
                f"got {type(strategy_id).__name__}"
            )
        if not strategy_id.strip():
            raise LogClassError(
                "LogRecord.log_class=STRATEGY requires a non-empty strategy_id; "
                "whitespace-only is rejected"
            )
    else:  # pragma: no cover — defensive: enum exhaustiveness guard
        raise LogClassError(
            f"unknown LogClass member {record.log_class!r}; "
            "the dispatcher must be extended when LogClass gains a variant"
        )


__all__ = [
    "LogSink",
    "RoutedLogDispatcher",
    "validate_log_record",
]
