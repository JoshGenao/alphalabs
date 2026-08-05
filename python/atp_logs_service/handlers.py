"""The SRS-LOG-001 log-query handler (REST + CLI on the SRS-API-001 runtime).

One transport-free handler serves both ``GET /api/v1/logs`` and ``admin logs``:
the SDK declares the same query dimensions on both surfaces, so a second
implementation would only be a second place for them to drift.

Read path
---------
The handler owns no state. Every request re-reads the persisted trail through
:func:`atp_logging.persistence.read_records_bounded` — the lock-free, streaming
reader built for exactly this case (a query surface reading a store another
process owns). It reflects records a *different* process appended a millisecond
ago, which is what an operator tailing an audit log expects, and its memory is
bounded by the page size rather than by the size of the trail.

Fail-closed rules this handler owes the audit trail
---------------------------------------------------
* **Separation is re-asserted here.** ``log_class`` selects exactly ONE store —
  the SYSTEM store or the STRATEGY store, never a merged read. The two paths
  are cross-checked at construction (``os.path.samefile``) so a composition
  that aliased them cannot turn this surface into a cross-class read.
* **Corruption is never an empty result.** A ``LogStoreCorruptionError`` from
  the reader becomes a structured ``500`` naming the store. An unreadable audit
  trail rendered as ``{"events": []}`` would be indistinguishable from "nothing
  happened" — the failure mode that makes an audit log worthless.
* **An unparseable parameter is never a silent default.** Every value is
  validated against the enum / format the SDK declares and rejected with a
  ``400`` carrying the offending value. A mistyped ``severity=WARNING`` must not
  quietly widen to "everything".
* **An undeclared parameter is refused, not ignored.** Accepting and dropping
  ``?limit=10`` would report a server-capped result as if the caller's bound had
  been honoured.
* **Reads are bounded in MEMORY, not just in output.** The declared route
  carries no ``limit`` parameter, so the cap is composition-supplied
  (``max_events``) and applied newest-first — and it is applied to the READ.
  An audit trail is append-only and unbounded by default, so materialising it
  to slice the last page would let a large log OOM the operator runtime. The
  response states ``returned`` / ``matched`` / ``truncated`` rather than
  implying the page is the whole trail.
* **No follow flag exists to refuse.** :class:`~atp_runtime.registry.Handler`
  returns one result and cannot stream to stdout, so ``admin logs`` does not
  DECLARE a ``--follow`` option at all: an uncovered capability must not have a
  public surface, and a flag that always errors is still a surface. The ``LOGS``
  WebSocket channel is the event-driven surface (see
  :mod:`atp_logs_service.publisher`), and the command's own summary says so.

SRS trace
---------
``SRS-LOG-001`` (log query surface), ``SYS-38`` / ``SYS-61`` (log record schema,
severity order), ``SRS-API-001`` (the runtime this registers on).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from atp_logging.persistence import (
    LogStoreClassMismatchError,
    LogStoreCorruptionError,
    LogStoreError,
    LogStoreMissingError,
    LogStoreRotationError,
    RecordPosition,
    read_records_bounded,
)
from atp_logging.records import LogClass, LogRecord, Severity, Source
from atp_runtime.errors import ErrorCategory, InterfaceError
from atp_runtime.registry import HandlerResult, Request, Surface

__all__ = [
    "CLI_PARAMS",
    "DEFAULT_MAX_EVENTS",
    "EVENT_FIELDS",
    "LOGS_CLI_OPERATION",
    "LOGS_REST_OPERATION",
    "REST_PARAMS",
    "LogsQueryHandler",
    "render_event",
]

#: The SDK-pinned operation identifiers this handler is registered for.
LOGS_REST_OPERATION = "GET /api/v1/logs"
LOGS_CLI_OPERATION = "admin logs"

#: Query parameters ``GET /api/v1/logs`` declares (``atp_api.routes``), plus the
#: runtime-generic ``confirm`` token the dispatcher passes through on every
#: route. Anything else is refused rather than silently dropped.
REST_PARAMS: frozenset[str] = frozenset(
    {
        "log_class",
        "severity",
        "source",
        "event_type",
        "correlation_id",
        "start_time",
        "end_time",
        "confirm",
    }
)

#: Option destinations ``admin logs`` declares (``atp_cli.commands``). ``json``
#: and ``confirm`` are consumed by the dispatcher and never reach the handler.
CLI_PARAMS: frozenset[str] = frozenset({"log_class", "severity", "source", "since"})

#: Per-event response fields: the six SyRS SYS-61 fields, plus the ``log_class``
#: discriminant so a caller can never mistake which trail an event came from,
#: plus ``strategy_id`` so a strategy line can be ATTRIBUTED.
#:
#: ``strategy_id`` is not optional decoration: ``source`` is always the literal
#: ``strategy`` on strategy-class records, so without it every one of the 30
#: Reservoir strategies produces indistinguishable lines and an operator cannot
#: tell which strategy emitted what. It is ``None`` on system records (where the
#: record schema forbids it) and non-empty on strategy records (where the schema
#: requires it). Keep in lockstep with ``atp_api.routes`` / ``atp_ws.channels``.
EVENT_FIELDS: tuple[str, ...] = (
    "timestamp",
    "severity",
    "source",
    "event_type",
    "message",
    "correlation_id",
    "log_class",
    "strategy_id",
    # An OPAQUE identity for the persisted line. The dashboard merges two feeds
    # for one view (a REST poll and the live LOGS channel) and must tell "the
    # same record, twice" from "two records that happen to look alike" — and
    # audit records CAN look alike: the rendered timestamp is milliseconds, and
    # a retried operation legitimately repeats every other field. Comparing
    # values would silently collapse two real events into one.
    "record_id",
)

#: Unix epoch as an aware datetime — the origin for exact integer ns math.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: Default cap on one response. An append-only audit trail has no natural size
#: bound, so an uncapped read is a memory hazard on the operator's own host.
DEFAULT_MAX_EVENTS = 500


def _iso_from_ns(timestamp_ns: int) -> str:
    """Render an epoch-nanosecond stamp as an ISO-8601 UTC string."""

    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    )


def render_event(record: LogRecord, position: RecordPosition) -> dict[str, object]:
    """Render one record as the declared response event (no extra fields).

    ``position`` is the physical identity of the persisted line; it is published
    as an opaque ``record_id`` so a consumer merging feeds can dedupe on identity
    rather than on values that legitimately repeat.
    """

    return {
        "timestamp": _iso_from_ns(record.timestamp_ns),
        "severity": record.severity.value,
        "source": record.source.value,
        "event_type": record.event_type,
        "message": record.message,
        "correlation_id": record.correlation_id,
        "log_class": record.log_class.value,
        "strategy_id": record.strategy_id,
        "record_id": position.token,
    }


def _bad_request(message: str, *, type: str, detail: dict[str, object]) -> InterfaceError:
    return InterfaceError(ErrorCategory.BAD_REQUEST, message, type=type, detail=detail)


def _parse_log_class(raw: str | None) -> LogClass:
    """``log_class`` → the ONE store to read (default: the CLI's declared default)."""

    if raw is None or raw == "":
        return LogClass.SYSTEM
    try:
        return LogClass(raw)
    except ValueError as exc:
        raise _bad_request(
            f"log_class must be one of {sorted(c.value for c in LogClass)}; got {raw!r}",
            type="LOGS_BAD_LOG_CLASS",
            detail={"parameter": "log_class", "value": raw},
        ) from exc


def _parse_severity(raw: str | None) -> Severity | None:
    """``severity`` → MINIMUM severity (inclusive), per the SYS-61 order."""

    if raw is None or raw == "":
        return None
    try:
        return Severity(raw)
    except ValueError as exc:
        raise _bad_request(
            f"severity must be one of {[s.value for s in Severity]}; got {raw!r}",
            type="LOGS_BAD_SEVERITY",
            detail={"parameter": "severity", "value": raw},
        ) from exc


def _parse_source(raw: str | None, log_class: LogClass) -> Source | None:
    """``source`` → an emitter component valid for the SELECTED class.

    A ``source`` from the other class is a contradiction, not an empty result:
    ``?log_class=strategy&source=kill_switch`` asked for something that cannot
    exist, and answering ``[]`` would read as "no kill-switch activations".
    """

    if raw is None or raw == "":
        return None
    try:
        source = Source(raw)
    except ValueError as exc:
        raise _bad_request(
            f"source must be one of {[s.value for s in Source]}; got {raw!r}",
            type="LOGS_BAD_SOURCE",
            detail={"parameter": "source", "value": raw},
        ) from exc
    is_strategy = source is Source.STRATEGY
    if is_strategy != (log_class is LogClass.STRATEGY):
        raise _bad_request(
            f"source {source.value!r} never appears on {log_class.value!r} records "
            "— the query contradicts the system/strategy separation",
            type="LOGS_SOURCE_CLASS_MISMATCH",
            detail={"parameter": "source", "value": source.value, "log_class": log_class.value},
        )
    return source


def _parse_non_empty(raw: str | None, *, parameter: str) -> str | None:
    """Exact-match text filter; a blank value is a mistake, not "match all"."""

    if raw is None:
        return None
    if not raw.strip():
        raise _bad_request(
            f"{parameter} must be a non-empty string when supplied",
            type="LOGS_BLANK_FILTER",
            detail={"parameter": parameter, "value": raw},
        )
    return raw


def _parse_time_ns(raw: str | None, *, parameter: str) -> int | None:
    """ISO-8601 bound → epoch nanoseconds (naive input is read as UTC).

    HONEST LIMIT: the declared parameter is an ISO-8601 string, which carries
    microsecond resolution at best, while records are stamped in nanoseconds.
    Two records written inside the same microsecond cannot be separated by any
    window this surface can express — they are returned together or not at all.
    """

    if raw is None or raw == "":
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _bad_request(
            f"{parameter} must be an ISO-8601 timestamp; got {raw!r}",
            type="LOGS_BAD_TIME_BOUND",
            detail={"parameter": parameter, "value": raw},
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    # INTEGER arithmetic, deliberately: datetime.timestamp() is a float, and at
    # current epoch values a float64 second carries only ~240 ns of resolution.
    # Scaling that to nanoseconds can land the bound a few hundred ns ABOVE the
    # record it was meant to include, silently dropping an exact-boundary audit
    # entry — the operator asked for "since 12:00:00.000000" and the 12:00:00.000000
    # record vanishes.
    delta = moment - _EPOCH
    epoch_ns = (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000
    if epoch_ns < 0:
        raise _bad_request(
            f"{parameter} predates the Unix epoch; got {raw!r}",
            type="LOGS_BAD_TIME_BOUND",
            detail={"parameter": parameter, "value": raw},
        )
    return epoch_ns


def _reject_unknown(query: Mapping[str, str], allowed: frozenset[str], surface: str) -> None:
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise _bad_request(
            f"unsupported {surface} parameter(s) {unknown}; the declared surface accepts "
            f"{sorted(allowed - {'confirm'})}",
            type="LOGS_UNKNOWN_PARAMETER",
            detail={"unknown": unknown, "accepted": sorted(allowed - {"confirm"})},
        )


class LogsQueryHandler:
    """Registered for ``GET /api/v1/logs`` AND ``admin logs``.

    Args:
        system_store_path: Path of the SYSTEM store's active segment.
        strategy_store_path: Path of the STRATEGY store's active segment. Must
            be a different physical file — the separation this feature exists
            to provide.
        max_events: Cap on events in one response (newest-first). Must be > 0.
        max_files: Rotation depth of the stores being read; must match the
            writer's so no rotated segment is invisible to the query.
    """

    def __init__(
        self,
        *,
        system_store_path: str | os.PathLike[str],
        strategy_store_path: str | os.PathLike[str],
        max_events: int = DEFAULT_MAX_EVENTS,
        max_files: int = 5,
    ) -> None:
        system = Path(system_store_path)
        strategy = Path(strategy_store_path)
        if _same_target(system, strategy):
            raise ValueError(
                "system and strategy store paths resolve to the same file "
                f"({system} / {strategy}); SRS-LOG-001 requires separate persistent sinks"
            )
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
            raise ValueError(f"max_events must be a positive int; got {max_events!r}")
        if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
            raise ValueError(f"max_files must be a positive int; got {max_files!r}")
        self._paths: dict[LogClass, Path] = {
            LogClass.SYSTEM: system,
            LogClass.STRATEGY: strategy,
        }
        self._max_events = max_events
        self._max_files = max_files

    # ----- request handling ----- #

    def handle(self, request: Request) -> HandlerResult:
        """Serve one log query; raises :class:`InterfaceError` for a bad request."""

        if request.surface is Surface.CLI:
            log_class, filters = self._cli_filters(request.query)
        else:
            log_class, filters = self._rest_filters(request.query)
        return HandlerResult(200, self._read(log_class, filters))

    def _rest_filters(self, query: Mapping[str, str]) -> tuple[LogClass, dict[str, object]]:
        _reject_unknown(query, REST_PARAMS, "query")
        log_class = _parse_log_class(query.get("log_class"))
        start_ns = _parse_time_ns(query.get("start_time"), parameter="start_time")
        end_ns = _parse_time_ns(query.get("end_time"), parameter="end_time")
        _reject_inverted_window(start_ns, end_ns)
        return log_class, {
            "log_class": log_class,
            "min_severity": _parse_severity(query.get("severity")),
            "source": _parse_source(query.get("source"), log_class),
            "event_type": _parse_non_empty(query.get("event_type"), parameter="event_type"),
            "correlation_id": _parse_non_empty(
                query.get("correlation_id"), parameter="correlation_id"
            ),
            "start_ns": start_ns,
            "end_ns": end_ns,
        }

    def _cli_filters(self, query: Mapping[str, str]) -> tuple[LogClass, dict[str, object]]:
        _reject_unknown(query, CLI_PARAMS, "option")
        log_class = _parse_log_class(query.get("log_class"))
        return log_class, {
            "log_class": log_class,
            "min_severity": _parse_severity(query.get("severity")),
            "source": _parse_source(query.get("source"), log_class),
            "event_type": None,
            "correlation_id": None,
            "start_ns": _parse_time_ns(query.get("since"), parameter="--since"),
            "end_ns": None,
        }

    # ----- the read itself ----- #

    def _read(self, log_class: LogClass, filters: dict[str, object]) -> dict[str, object]:
        path = self._paths[log_class]
        # Fast path only — the authoritative answer comes from the read below,
        # which is atomic: a deletion landing between this check and the open
        # would otherwise read as a clean empty scan.
        if not path.exists():
            # A CONFIGURED trail that is not there reads as zero records, which
            # is shaped exactly like a healthy quiet system. A deleted store, a
            # mispointed directory, or a writer that never started would answer
            # "200, no events" — an all-clear over missing audit history. Fail
            # closed, with its own error type so it is never confused with
            # corruption (which means present-but-unreadable).
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the configured {log_class.value} log store does not exist: {path}. "
                "Refusing to answer with an empty result — a missing audit trail is "
                "not an empty one",
                type="LOGS_STORE_MISSING",
                detail={"log_class": log_class.value, "store": str(path)},
            )
        try:
            # BOUNDED: the trail is append-only and unbounded by default, so the
            # cap has to apply to the READ, not to a list built from the whole
            # store. Memory is O(max_events) however large the audit log grows;
            # the total match count is still exact (counting is free), so the
            # response can say "100 of 40,000" without holding 40,000 records.
            page, matched = read_records_bounded(
                path,
                limit=self._max_events,
                max_files=self._max_files,
                require_active=True,
                # The store this class selects must contain ONLY that class.
                expect_class=log_class,
                **filters,
            )
        except LogStoreMissingError as error:
            # The trail vanished DURING the read (or between the check and it).
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the configured {log_class.value} log store does not exist: {path}. "
                "Refusing to answer with an empty result — a missing audit trail is "
                "not an empty one",
                type="LOGS_STORE_MISSING",
                detail={"log_class": log_class.value, "store": str(path)},
            ) from error
        except LogStoreClassMismatchError as error:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the {log_class.value} log store contains a record of the other class: "
                f"{error}. Refusing to serve a trail whose separation is broken — "
                "quietly filtering it out would hide the breakage",
                type="LOGS_STORE_CLASS_MISMATCH",
                detail={"log_class": log_class.value, "store": str(path)},
            ) from error
        except LogStoreRotationError as error:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the {log_class.value} log store rotated during every read attempt: "
                f"{error}. Refusing to serve a page that may be missing records — "
                "retry the query",
                type="LOGS_ROTATION_RACE",
                detail={"log_class": log_class.value, "store": str(path)},
            ) from error
        except LogStoreCorruptionError as error:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the {log_class.value} log store is unreadable: {error}. Refusing to "
                "serve an empty result — an unreadable audit trail is not an empty one",
                type="LOGS_STORE_CORRUPT",
                detail={"log_class": log_class.value, "store": str(path)},
            ) from error
        except LogStoreError as error:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the {log_class.value} log store could not be queried: {error}",
                type="LOGS_STORE_UNREADABLE",
                detail={"log_class": log_class.value, "store": str(path)},
            ) from error
        except OSError as error:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"the {log_class.value} log store could not be read: {error}",
                type="LOGS_STORE_UNREADABLE",
                detail={"log_class": log_class.value, "store": str(path)},
            ) from error

        # ``page`` is already newest-first and capped: a truncated page is the
        # most RECENT events, never an arbitrary prefix of an old segment.
        return {
            "events": [render_event(record, position) for position, record in page],
            "log_class": log_class.value,
            "returned": len(page),
            "matched": matched,
            "truncated": matched > len(page),
            "limit": self._max_events,
            # An ABSENT store file reads as zero records, which is also what a
            # present-but-empty trail reads as — and the two mean very different
            # things (nothing written yet, versus a trail that was moved or
            # deleted). The caller is told which, rather than being handed an
            # empty list that looks equally fine either way.
            "store_present": path.exists(),
            "event_fields": list(EVENT_FIELDS),
            "srs_ref": "SRS-LOG-001",
        }


def _reject_inverted_window(start_ns: int | None, end_ns: int | None) -> None:
    """An inverted window can only ever match nothing — say so, don't return []."""

    if start_ns is not None and end_ns is not None and end_ns < start_ns:
        raise _bad_request(
            "end_time precedes start_time — the window can never match a record",
            type="LOGS_INVERTED_WINDOW",
            detail={"start_ns": start_ns, "end_ns": end_ns},
        )


def _same_target(first: Path, second: Path) -> bool:
    """Whether two store paths denote the same physical file (alias-aware)."""

    try:
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except OSError:  # pragma: no cover - stat failure falls back to text compare
        pass
    return os.path.normcase(first.resolve()) == os.path.normcase(second.resolve())
