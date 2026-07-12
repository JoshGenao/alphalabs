"""Evidence adapters that feed the pure availability engine (SRS-REL-001).

This is the only layer in ``atp_reliability`` allowed to touch the trading
calendar and the durable log store; :mod:`atp_reliability.availability` stays
dependency-free and clock-free. Three concerns live here:

1. **Market-hours windows** — :func:`market_sessions` turns a date range into the
   per-trading-day ``[09:30, 16:00)`` ET sessions (13:00 ET on early-close days),
   reusing the real DST/holiday-aware ``UsEquityTradingCalendar`` (SRS-SDK-002 /
   SYS-50). Session bounds are converted to **integer-exact** epoch ns.
2. **NFR-R1 exclusions** — :func:`sys75_exclusion_windows` synthesises the SYS-75
   scheduled IB Gateway restart windows (~23:45 ET daily); operator planned-
   maintenance windows are passed through unchanged. Both are handed to the engine
   as ``excluded_windows`` (carved from numerator + denominator).
3. **Downtime reconstruction** — :func:`reconstruct_downtime` turns a stream of
   health transitions into downtime intervals with a per-source state machine and
   boundary-open / unclosed rules (no-fabrication). :func:`downtime_from_log_records`
   maps the ``atp_logging`` SYS-61 event taxonomy into that stream.

**Honesty boundary (NFR-R1 scope).** A dead host emits no logs, so log records can
never witness the *host-level* outages NFR-R1 includes (hardware failure, kernel
panic) — those are an *absence* of an expected heartbeat, and their positive
observation is the deferred host-liveness feed. IB-Gateway disconnects (NFR-R2) are a
*subsystem* signal, **non-counting** for NFR-R1 host availability, so
:func:`downtime_from_log_records` only ever emits the non-counting
:attr:`OutageCause.IB_CONNECTIVITY`; it is a supplementary audit signal, never the
host-availability oracle. Container-lifecycle records are NOT mapped: SYSTEM records
carry no per-container identifier (``strategy_id`` is ``None`` on SYSTEM records), so
they cannot be paired per container, and a single-container crash is NFR-R5 scope
(non-counting) regardless. The counted ``HOST_UNPLANNED`` intervals come from the
host-liveness feed / operator outage ledger (both deferred — see the module README
and the ``availability_measurement_contract`` ``deferred`` list).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Iterable, Protocol

from atp_logging.records import (
    EVENT_TYPES_BY_SOURCE,
    LogRecord,
    Source,
    is_finite_non_negative_int,
)
from atp_strategy.calendar import EASTERN

from .availability import (
    NS_PER_SECOND,
    DowntimeInterval,
    Interval,
    MarketSessionWindow,
    OutageCause,
)

_EPOCH_UTC = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

# Event types this adapter depends on, pinned against the SYS-61 taxonomy so a
# drift in ``EVENT_TYPES_BY_SOURCE`` fails closed at import rather than silently
# dropping a transition we thought we were mapping.
_IB_DOWN = "DISCONNECT"
_IB_UP = ("CONNECT", "RECONNECT")

# SYS-75: the IB Gateway scheduled daily restart (~23:45 ET for live accounts,
# configurable) with a default 5-minute suppression window. Well outside the
# 09:30-16:00 session, so it never intersects market hours on the compliant path.
_SYS75_RESTART_HM: tuple[int, int] = (23, 45)
_SYS75_DURATION_MINUTES: int = 5


class _SessionCalendar(Protocol):
    """Minimal structural calendar surface used by :func:`market_sessions`.

    Kept local (rather than the ``atp_strategy`` ``TradingCalendar`` Protocol) so the
    frozen ``UsEquityTradingCalendar`` — whose ``name`` is read-only — satisfies it;
    only the three session methods are needed here.
    """

    def is_session(self, date: _dt.date) -> bool: ...
    def session_open(self, date: _dt.date) -> _dt.datetime: ...
    def session_close(self, date: _dt.date) -> _dt.datetime: ...


def _verify_taxonomy(ib_events: tuple[str, ...]) -> None:
    """Fail closed if the SYS-61 taxonomy no longer contains the mapped event types.

    Uses an explicit ``raise`` (NOT ``assert``, which ``python -O`` strips) so a
    renamed/removed upstream event type can never silently cause this adapter to
    drop downtime records and understate the availability evidence.
    """

    missing_ib = [e for e in (_IB_DOWN, *_IB_UP) if e not in ib_events]
    if missing_ib:
        raise RuntimeError(
            f"atp_reliability.evidence: IB_GATEWAY taxonomy drift — {missing_ib} "
            f"no longer in EVENT_TYPES_BY_SOURCE[IB_GATEWAY]={ib_events}"
        )


_verify_taxonomy(EVENT_TYPES_BY_SOURCE[Source.IB_GATEWAY])


def _to_epoch_ns(dt: _dt.datetime) -> int:
    """Convert a timezone-aware ``datetime`` to integer-exact epoch nanoseconds.

    Uses ``timedelta`` arithmetic rather than ``datetime.timestamp() * 1e9`` so
    minute-aligned session bounds convert without floating-point error.
    """

    if dt.tzinfo is None:
        raise ValueError("_to_epoch_ns requires a timezone-aware datetime")
    delta = dt.astimezone(_dt.timezone.utc) - _EPOCH_UTC
    total_seconds = delta.days * 86_400 + delta.seconds
    return total_seconds * NS_PER_SECOND + delta.microseconds * 1_000


def market_sessions(
    calendar: _SessionCalendar,
    start_date: _dt.date,
    end_date: _dt.date,
) -> tuple[int, int, list[MarketSessionWindow]]:
    """Return ``(window_start_ns, window_end_ns, sessions)`` over ``[start, end]``.

    Iterates every calendar date in the inclusive range; for each trading session
    it emits the regular ``[session_open, session_close)`` window (early-close days
    close at 13:00 ET automatically). The analysis window is the full period in
    **UTC-midnight** bounds ``[start_date 00:00 UTC, (end_date + 1) 00:00 UTC)``. UTC
    has no DST, so an N-calendar-day range is *exactly* ``N * 24 h`` elapsed — this is
    what makes the engine's strict elapsed-ns rolling-period gate DST-robust (an
    Eastern-midnight window would be an hour short across spring-forward and wrongly
    fail the gate). Every ET session on a date in ``[start, end]`` falls strictly
    inside this UTC window (09:30 ET is ~13:30-14:30 UTC same day; 16:00 ET is
    ~20:00-21:00 UTC same day), so no session is clipped.

    Raises:
        ValueError: if ``end_date < start_date`` or no session falls in the range.
    """

    if end_date < start_date:
        raise ValueError(f"end_date {end_date} precedes start_date {start_date}")
    sessions: list[MarketSessionWindow] = []
    cursor = start_date
    one_day = _dt.timedelta(days=1)
    while cursor <= end_date:
        if calendar.is_session(cursor):
            open_ns = _to_epoch_ns(calendar.session_open(cursor))
            close_ns = _to_epoch_ns(calendar.session_close(cursor))
            sessions.append(MarketSessionWindow(start_ns=open_ns, end_ns=close_ns))
        cursor += one_day
    if not sessions:
        raise ValueError(f"no trading session in [{start_date}, {end_date}]")
    utc = _dt.timezone.utc
    window_start_ns = _to_epoch_ns(
        _dt.datetime(start_date.year, start_date.month, start_date.day, tzinfo=utc)
    )
    end_next = end_date + one_day
    window_end_ns = _to_epoch_ns(
        _dt.datetime(end_next.year, end_next.month, end_next.day, tzinfo=utc)
    )
    return window_start_ns, window_end_ns, sessions


def sys75_exclusion_windows(
    start_date: _dt.date,
    end_date: _dt.date,
    *,
    restart_hm: tuple[int, int] = _SYS75_RESTART_HM,
    duration_minutes: int = _SYS75_DURATION_MINUTES,
) -> list[Interval]:
    """Synthesise the SYS-75 daily IB-Gateway-restart exclusion windows (ET).

    One ``[restart, restart + duration)`` window per calendar date, in DST-aware
    Eastern time, converted to epoch ns. These are NFR-R1 exclusions; because the
    restart is ~23:45 ET they never intersect the 09:30-16:00 session on the
    compliant path (the engine flags it if they ever do).
    """

    if end_date < start_date:
        raise ValueError(f"end_date {end_date} precedes start_date {start_date}")
    if duration_minutes <= 0:
        raise ValueError(f"duration_minutes must be positive; got {duration_minutes}")
    hour, minute = restart_hm
    windows: list[Interval] = []
    cursor = start_date
    one_day = _dt.timedelta(days=1)
    while cursor <= end_date:
        start_et = _dt.datetime(cursor.year, cursor.month, cursor.day, hour, minute, tzinfo=EASTERN)
        end_et = start_et + _dt.timedelta(minutes=duration_minutes)
        windows.append((_to_epoch_ns(start_et), _to_epoch_ns(end_et)))
        cursor += one_day
    return windows


@dataclass(frozen=True, slots=True)
class HealthTransition:
    """A single health-state edge for one ``source`` at ``timestamp_ns``.

    ``going_down`` opens an outage for that source; ``going_down=False`` closes it.
    The outage cause is derived from ``source`` (see ``source_causes``), so a
    boundary-open outage (no in-window down edge) still has a well-defined cause.
    """

    timestamp_ns: int
    source: str
    going_down: bool


#: Default per-source outage classification. Every entry is a **non-counting**
#: subsystem cause: log/transition evidence cannot witness host death, so it must
#: never be able to fabricate a counted ``HOST_UNPLANNED`` interval.
DEFAULT_SOURCE_CAUSES: dict[str, OutageCause] = {
    "ib_gateway": OutageCause.IB_CONNECTIVITY,
    "container": OutageCause.CONTAINER_CHURN,
}


def _cause_for_source(source: str, source_causes: dict[str, OutageCause]) -> OutageCause:
    """Resolve a source key (exact, then ``prefix:...``) to its outage cause."""

    if source in source_causes:
        return source_causes[source]
    prefix = source.split(":", 1)[0]
    if prefix in source_causes:
        return source_causes[prefix]
    raise KeyError(f"no outage cause mapped for source {source!r}")


def reconstruct_downtime(
    transitions: Iterable[HealthTransition],
    *,
    window_start_ns: int,
    window_end_ns: int,
    source_causes: dict[str, OutageCause] | None = None,
) -> list[DowntimeInterval]:
    """Reconstruct downtime intervals from health transitions (per-source FSM).

    Rules (no-fabrication, fail-closed on ordering):

    * Events are validated (``timestamp_ns`` is a finite non-negative int) and
      **sorted by timestamp** — ``atp_logging.query`` does not sort by time.
    * Per source: a down edge while up opens an outage; consecutive down edges are
      idempotent (earliest start kept); an up edge while down closes it.
    * **Boundary-open:** an up edge with no preceding down edge means the outage
      began before the window → DOWN from ``window_start_ns``.
    * **Unclosed:** an outage still open at the end → DOWN to ``window_end_ns``
      (never assume recovery without evidence — the worst outage must not read as
      zero downtime).

    Emitted intervals are clipped to ``[window_start_ns, window_end_ns]``.
    """

    if window_end_ns <= window_start_ns:
        raise ValueError(f"window is empty/inverted: [{window_start_ns}, {window_end_ns}]")
    causes = source_causes or DEFAULT_SOURCE_CAUSES

    ordered: list[HealthTransition] = []
    for t in transitions:
        if not is_finite_non_negative_int(t.timestamp_ns):
            raise ValueError(f"transition timestamp_ns is not a finite non-negative int: {t!r}")
        ordered.append(t)
    # Order by timestamp, then DOWN edges BEFORE UP edges at the SAME instant
    # (``not going_down`` puts True/down first). A same-timestamp DOWN/UP collision
    # then opens and immediately closes a zero-duration blip rather than fabricating
    # a full-window outage (UP-first would emit boundary-open [start, T] then an
    # unclosed [T, end]).
    ordered.sort(key=lambda t: (t.timestamp_ns, t.source, not t.going_down))

    by_source: dict[str, list[HealthTransition]] = {}
    for t in ordered:
        by_source.setdefault(t.source, []).append(t)

    intervals: list[DowntimeInterval] = []
    for source, events in by_source.items():
        cause = _cause_for_source(source, causes)
        # Explicit per-source state: an outage opens on the first DOWN edge and
        # closes on the next UP edge. ``unknown`` (no edge seen yet) + a first UP
        # means the source was down from the window start until that UP
        # (boundary-open, no-fabrication); once ``up``, further UP edges are no-ops
        # so a stream of UPs cannot each re-emit a boundary-open interval.
        state = "unknown"  # "unknown" | "up" | "down"
        open_start = 0
        for ev in events:
            if ev.going_down:
                if state != "down":
                    open_start = ev.timestamp_ns
                    state = "down"
                # else: already down — idempotent, keep the earliest start.
            elif state == "down":
                _emit(intervals, open_start, ev.timestamp_ns, cause, window_start_ns, window_end_ns)
                state = "up"
            elif state == "unknown":
                _emit(
                    intervals,
                    window_start_ns,
                    ev.timestamp_ns,
                    cause,
                    window_start_ns,
                    window_end_ns,
                )
                state = "up"
            # else state == "up": UP while up — no-op.
        if state == "down":
            _emit(intervals, open_start, window_end_ns, cause, window_start_ns, window_end_ns)
    return intervals


def _emit(
    out: list[DowntimeInterval],
    start_ns: int,
    end_ns: int,
    cause: OutageCause,
    window_start_ns: int,
    window_end_ns: int,
) -> None:
    """Clip ``[start, end)`` to the window and append if non-empty."""

    lo = max(start_ns, window_start_ns)
    hi = min(end_ns, window_end_ns)
    if hi > lo:
        out.append(DowntimeInterval(start_ns=lo, end_ns=hi, cause=cause))


def downtime_from_log_records(
    records: Iterable[LogRecord],
    *,
    window_start_ns: int,
    window_end_ns: int,
) -> list[DowntimeInterval]:
    """Map ``atp_logging`` SYS-61 records to (non-counting) subsystem downtime.

    ONLY IB-Gateway connect/disconnect records become transitions — there is a single
    logical IB Gateway, so a ``DISCONNECT``/``CONNECT`` pair keyed by ``ib_gateway`` is
    unambiguous. Container-lifecycle records are deliberately **not** mapped: SYSTEM log
    records carry no per-container identifier (``strategy_id`` is ``None`` on SYSTEM
    records by the SRS-LOG-001 schema), so one container's ``CONTAINER_START`` would
    close another's ``OOM_KILL`` and mispair the intervals — and a single-container
    crash is NFR-R5 (orchestrator survives it), not host availability, so it is
    non-counting anyway. Kill-switch HALTED, resource alerts, and market-data gaps are
    likewise not mapped (intentional operator action / other-requirement scope).
    """

    transitions: list[HealthTransition] = []
    for rec in records:
        if rec.source is Source.IB_GATEWAY:
            if rec.event_type == _IB_DOWN:
                transitions.append(HealthTransition(rec.timestamp_ns, "ib_gateway", True))
            elif rec.event_type in _IB_UP:
                transitions.append(HealthTransition(rec.timestamp_ns, "ib_gateway", False))
    return reconstruct_downtime(
        transitions,
        window_start_ns=window_start_ns,
        window_end_ns=window_end_ns,
    )


# NOTE: there is deliberately NO public "cover every session" helper. Turning sessions
# into coverage would let a caller synthesise positive coverage from thin air and mint a
# certifying PASS with no host-liveness evidence — the "no-data = up" lie this substrate
# refuses. Coverage must come from real positive observations (the deferred host-liveness
# feed). Tests that model a fully-monitored window build the CoveredSpans themselves.

__all__ = [
    "DEFAULT_SOURCE_CAUSES",
    "HealthTransition",
    "downtime_from_log_records",
    "market_sessions",
    "reconstruct_downtime",
    "sys75_exclusion_windows",
]
