"""SRS-LOG-001 operator surfaces over the persistent log stores.

``atp_logging`` is the foundational logging SDK: the record schema, the routing
dispatcher, and the durable ``JsonlLogStore`` sinks that keep SYSTEM and
STRATEGY logs in separate physical files. It is forbidden from importing any
interface package (``tools/log_record_check.py`` enforces this at the AST
level), so it cannot register anything on the operator-interface runtime.

This package is that layer — the same relationship ``atp_logging_boot`` has to
credential redaction, and ``atp_safety`` / ``atp_readiness`` have to their own
handlers. It imports both the SDK and ``atp_runtime`` and ships:

* :class:`~atp_logs_service.handlers.LogsQueryHandler` — one transport-free
  handler behind ``GET /api/v1/logs`` and ``admin logs``;
* :class:`~atp_logs_service.publisher.LogEventPublisher` — the event-driven
  ``LOGS`` WebSocket channel publisher;
* :func:`~atp_logs_service.wiring.wire_logs` — the composer entry point.

Nothing here is automatic: a runtime nobody wired keeps returning the structured
``501`` naming ``SRS-LOG-001``, and the ``LOGS`` channel stays unclaimed.

SRS trace
---------
``SRS-LOG-001`` (log query + publication surfaces), ``SYS-38`` / ``SYS-61``,
``SRS-API-001`` (registry / dispatch / publish substrate).
"""

from __future__ import annotations

from .handlers import (
    CLI_PARAMS,
    DEFAULT_MAX_EVENTS,
    EVENT_FIELDS,
    LOGS_CLI_OPERATION,
    LOGS_REST_OPERATION,
    REST_PARAMS,
    LogsQueryHandler,
    render_event,
)
from .publisher import LOGS_CHANNEL, LogEventPublisher
from .wiring import LOGS_OPERATIONS, wire_logs

__all__ = [
    "CLI_PARAMS",
    "DEFAULT_MAX_EVENTS",
    "EVENT_FIELDS",
    "LOGS_CHANNEL",
    "LOGS_CLI_OPERATION",
    "LOGS_OPERATIONS",
    "LOGS_REST_OPERATION",
    "REST_PARAMS",
    "LogEventPublisher",
    "LogsQueryHandler",
    "render_event",
    "wire_logs",
]
