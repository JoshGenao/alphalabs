"""Structured error types raised by :mod:`atp_logging`.

The base :class:`LogRecordError` lets downstream callers catch the family
with one ``except`` clause. The specific subclasses mark the boundary that
rejected the record so the persistent-sink runtime (deferred to
SRS-LOG-001) can distinguish a schema violation from a dispatcher routing
failure when it lands.
"""

from __future__ import annotations


class LogRecordError(Exception):
    """Base class for every error raised by :mod:`atp_logging`."""


class LogPayloadError(LogRecordError):
    """Raised when a :class:`LogRecord` field has an invalid shape, type, or value.

    The dispatcher validates field shape at the ``dispatch`` boundary
    before routing. Covers: bad ``timestamp_ns`` (non-int, negative,
    non-finite, ``bool``), wrong enum type on ``severity`` / ``source`` /
    ``log_class``, empty ``message`` / ``event_type`` / ``correlation_id``,
    and event-type mismatches against ``EVENT_TYPES_BY_SOURCE``.
    """


class LogClassError(LogRecordError):
    """Raised when a record's ``log_class`` is inconsistent with its other fields.

    Covers the SRS-LOG-001 separation invariant: SYSTEM records must not
    carry ``strategy_id``, STRATEGY records must carry a non-empty
    ``strategy_id``, SYSTEM records must use a ``Source`` in
    ``SYSTEM_SOURCES``, and STRATEGY records must use ``Source.STRATEGY``.
    """


class LogRoutingError(LogRecordError):
    """Raised when the dispatcher has no sink registered for a record's log_class.

    Routing is the SRS-LOG-001 "separate persistent sinks" requirement.
    Until SRS-LOG-001 wires the concrete persistent sinks, callers can
    register their own (or in test rigs, a capturing stub). A dispatch
    against a missing sink fails closed.
    """


class LogSinkError(LogRecordError):
    """Raised when a registered :class:`LogSink` rejects a record.

    The dispatcher wraps the underlying sink exception so callers can
    catch the :class:`LogRecordError` family with one ``except`` clause.
    The original exception is preserved on ``__cause__`` for diagnosis.
    """


__all__ = [
    "LogClassError",
    "LogPayloadError",
    "LogRecordError",
    "LogRoutingError",
    "LogSinkError",
]
