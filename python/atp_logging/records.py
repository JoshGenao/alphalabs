"""``LogRecord`` schema for SRS-LOG-001.

This module is the cross-language source of truth for the structured log
record that SRS-LOG-001 separates between persistent system logs and user
strategy logs. The :class:`LogRecord` dataclass pins the six AC-named fields
(``timestamp_ns`` / ``severity`` / ``source`` / ``event_type`` / ``message``
/ ``correlation_id``) plus the SRS-LOG-001 separation discriminant
``log_class`` and the optional ``strategy_id`` that the strategy half
requires.

The :class:`Severity` enum is AC-pinned by SyRS SYS-61 (DEBUG, INFO, WARN,
ERROR, CRITICAL). The :class:`LogClass` enum is AC-pinned by SRS-LOG-001
(SYSTEM, STRATEGY). The :class:`Source` enum is AC-pinned by SyRS SYS-61's
verbatim enumeration of system-log emitter components (order routing,
ingestion, container lifecycle, IB Gateway, kill switch, Hot-Swap, resource
monitor, market data) plus the STRATEGY source for the user-strategy half.

``EVENT_TYPES_BY_SOURCE`` is the AC-pinned map of allowed event types per
system source — taken verbatim from the SyRS SYS-61 / SRS-LOG-001 AC
enumerations. Strategy-class records carry user-defined event types and are
not constrained by this map; the dispatcher only enforces the system half.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    """Severity levels enumerated verbatim by SyRS SYS-61.

    The order matters: DEBUG is the least severe, CRITICAL the most. The
    SRS-LOG-001 query interface (``GET /api/v1/logs?severity=...``) treats
    the parameter as a minimum-severity filter, so the value order here is
    the canonical comparison order.
    """

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogClass(StrEnum):
    """SRS-LOG-001's persistent-sink discriminant.

    The AC requires that ``SYSTEM`` and ``STRATEGY`` logs are stored in
    separate persistent sinks. The dispatcher routes on this field; mis-
    classification raises :class:`LogClassError`.
    """

    SYSTEM = "system"
    STRATEGY = "strategy"


class Source(StrEnum):
    """Emitter source components enumerated verbatim by SyRS SYS-61.

    The eight ``SYSTEM`` sources mirror the AC's enumeration of the system
    logs that must be persisted: "order routing outcomes, ingestion job
    lifecycle, container lifecycle, IB Gateway connection state changes,
    kill-switch activations, Hot-Swap events, resource threshold alerts,
    and market data subscription changes". The ``STRATEGY`` source is the
    only one allowed on ``log_class=STRATEGY`` records.
    """

    ORDER_ROUTING = "order_routing"
    INGESTION = "ingestion"
    CONTAINER_LIFECYCLE = "container_lifecycle"
    IB_GATEWAY = "ib_gateway"
    KILL_SWITCH = "kill_switch"
    HOT_SWAP = "hot_swap"
    RESOURCE_MONITOR = "resource_monitor"
    MARKET_DATA = "market_data"
    STRATEGY = "strategy"


SYSTEM_SOURCES: frozenset[Source] = frozenset(
    {
        Source.ORDER_ROUTING,
        Source.INGESTION,
        Source.CONTAINER_LIFECYCLE,
        Source.IB_GATEWAY,
        Source.KILL_SWITCH,
        Source.HOT_SWAP,
        Source.RESOURCE_MONITOR,
        Source.MARKET_DATA,
    }
)
"""Sources allowed on ``LogClass.SYSTEM`` records.

The complement (``{Source.STRATEGY}``) is the only allowed source on
``LogClass.STRATEGY`` records. The dispatcher enforces this invariant; the
contract block re-asserts it so a future enum addition cannot quietly
shift the SYSTEM/STRATEGY boundary.
"""


STRATEGY_SOURCES: frozenset[Source] = frozenset({Source.STRATEGY})
"""Sources allowed on ``LogClass.STRATEGY`` records."""


EVENT_TYPES_BY_SOURCE: dict[Source, tuple[str, ...]] = {
    Source.ORDER_ROUTING: ("ROUTING_DECISION", "ROUTING_OUTCOME"),
    Source.INGESTION: ("JOB_START", "JOB_COMPLETION", "JOB_FAILURE"),
    Source.CONTAINER_LIFECYCLE: (
        "CONTAINER_START",
        "CONTAINER_STOP",
        "CONTAINER_RESTART",
        "OOM_KILL",
    ),
    Source.IB_GATEWAY: ("CONNECT", "DISCONNECT", "RECONNECT"),
    Source.KILL_SWITCH: ("ACTIVATION",),
    Source.HOT_SWAP: ("PROMOTION", "DEMOTION"),
    Source.RESOURCE_MONITOR: ("THRESHOLD_ALERT",),
    Source.MARKET_DATA: ("SUBSCRIPTION_CHANGE", "SEQUENCE_GAP"),
    Source.STRATEGY: (),
}
"""AC-pinned event types per system source.

Taken verbatim from the SyRS SYS-61 / SRS-LOG-001 AC enumerations: ingestion
"start/completion/failure", container lifecycle "start, stop, restart, OOM
kill", IB Gateway "connect, disconnect, reconnect", etc. The empty tuple
under ``Source.STRATEGY`` signals that strategy-class event types are
user-defined and not enforced — the AC explicitly leaves strategy event
naming to the strategy author per SN-2.02 ("a logging API that the user
invokes from within their Python strategies").
"""


@dataclass(frozen=True, slots=True)
class LogRecord:
    """Structured log record that SRS-LOG-001's persistent sinks consume.

    Every field is required except ``strategy_id`` which is required when
    ``log_class == LogClass.STRATEGY`` and forbidden when ``log_class ==
    LogClass.SYSTEM`` — the dispatcher enforces the lifecycle invariant.

    Field shape (mirrored by ``required_log_record_fields`` in
    ``architecture/runtime_services.json#log_record_contract``):

    * ``timestamp_ns`` — wall-clock nanoseconds since the Unix epoch;
      non-negative finite ``int`` (``bool`` rejected because Python treats
      it as an ``int`` subclass).
    * ``severity`` — :class:`Severity` enum member (DEBUG / INFO / WARN /
      ERROR / CRITICAL).
    * ``source`` — :class:`Source` enum member; one of ``SYSTEM_SOURCES``
      for ``log_class == SYSTEM`` records, ``Source.STRATEGY`` for
      ``log_class == STRATEGY`` records.
    * ``event_type`` — non-empty ``str``; constrained to
      ``EVENT_TYPES_BY_SOURCE[source]`` for ``log_class == SYSTEM`` records
      and free-form for ``log_class == STRATEGY`` records.
    * ``message`` — non-empty ``str``; free-form human-readable message.
    * ``correlation_id`` — non-empty ``str``; the cross-component trace
      identifier required by SyRS SYS-61 / SRS-LOG-001 ("each log entry
      includes a correlation ID").
    * ``log_class`` — :class:`LogClass` enum member; the SRS-LOG-001
      sink-routing discriminant.
    * ``strategy_id`` — ``str | None``; required non-empty when
      ``log_class == STRATEGY``, must be ``None`` when ``log_class ==
      SYSTEM``. The dispatcher enforces the cross-field invariant.

    The dataclass is frozen so a downstream consumer cannot quietly mutate
    a record between dispatch and persistence (which would invalidate the
    audit trail).
    """

    timestamp_ns: int
    severity: Severity
    source: Source
    event_type: str
    message: str
    correlation_id: str
    log_class: LogClass
    strategy_id: str | None = field(default=None)

    def as_dict(self) -> dict[str, object]:
        """Render the record as a JSON-serialisable dict.

        Used by the SRS-API-001 / SRS-UI-001 / SRS-LOG-001 sinks once they
        land; pinned here so a non-serialisable field cannot slip in
        unnoticed. Enums are rendered as their ``.value`` strings.
        """

        return {
            "timestamp_ns": self.timestamp_ns,
            "severity": self.severity.value,
            "source": self.source.value,
            "event_type": self.event_type,
            "message": self.message,
            "correlation_id": self.correlation_id,
            "log_class": self.log_class.value,
            "strategy_id": self.strategy_id,
        }


def is_finite_non_negative_int(value: object) -> bool:
    """Predicate used by the dispatcher to validate ``timestamp_ns``.

    Rejects ``bool`` (an ``int`` subclass in Python), ``float`` (even when
    finite), ``str``/``None``, negatives, and non-finite values. Exposed so
    the contract check can re-exercise the same predicate that the
    dispatcher uses.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if value < 0:
        return False
    return math.isfinite(value)


__all__ = [
    "EVENT_TYPES_BY_SOURCE",
    "LogClass",
    "LogRecord",
    "STRATEGY_SOURCES",
    "SYSTEM_SOURCES",
    "Severity",
    "Source",
    "is_finite_non_negative_int",
]
