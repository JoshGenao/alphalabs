"""Log record SDK-surface for SRS-LOG-001.

This package is the cross-language source of truth for the structured log
record + sink-routing boundary that SRS-LOG-001 requires (separate
persistent sinks for system events and user strategy logs, both rendered
with timestamp / severity / source / event type / message / correlation
ID). The persistent sinks themselves, the dashboard rendering, the live
REST/WebSocket endpoint, and the audit-trail retention policy are
deferred to SRS-LOG-001's runtime half + SRS-UI-001 + SRS-API-001 (the
declared route ``GET /api/v1/logs``, CLI command ``admin logs``, and
WebSocket ``LOGS`` channel already pin the wire format in
``python/atp_api/`` / ``python/atp_cli/`` / ``python/atp_ws/``).

See ``python/atp_logging/README.md`` for the operator-facing summary and
``architecture/runtime_services.json#log_record_contract`` for the cross-
language contract block.
"""

from .dispatcher import LogSink, RoutedLogDispatcher
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

# NOTE: the SRS-SEC-001 redaction layer (SecretRedactor / build_secret_redactor)
# lives in ``atp_logging.redaction`` and is imported from there directly — it is
# deliberately kept OUT of this package ``__all__``, which is pinned to the
# SRS-LOG-001 ``log_record_contract.required_exports`` cross-language contract
# (same convention as the persistence sinks in ``atp_logging.persistence``).

__all__ = [
    "EVENT_TYPES_BY_SOURCE",
    "LogClass",
    "LogClassError",
    "LogPayloadError",
    "LogRecord",
    "LogRecordError",
    "LogRoutingError",
    "LogSink",
    "LogSinkError",
    "RoutedLogDispatcher",
    "STRATEGY_SOURCES",
    "SYSTEM_SOURCES",
    "Severity",
    "Source",
    "is_finite_non_negative_int",
]
