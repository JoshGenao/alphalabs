"""In-process Scheduler implementation resolving against a TradingCalendar.

SRS trace: ``SRS-SDK-002`` (SyRS ``SYS-6``, ``SYS-51``).

``InMemoryScheduler`` is the concrete ``Scheduler`` used by the SDK. It
stores ``(handle, kind, params, callback)`` tuples and exposes a pure
``next_fire_time(handle, after)`` helper so tests and the orchestrator
can probe scheduled triggers without a wall-clock fire loop. The actual
fire loop (live wall-clock callback dispatch) is owned by the
orchestrator and execution engine (``SRS-EXE-001`` / ``SRS-ORCH-*``);
this module is responsible for the calendar-aware *resolution* of fire
times.

Cron expressions are parsed via the ``_CronAdapter`` wrapper around the
``croniter`` library — keeping the vendor import isolated inside this
module preserves the SDK boundary the SRS-SDK-001 parity check enforces.
"""

from __future__ import annotations

import datetime as _dt
import itertools
from dataclasses import dataclass, field
from typing import Final

from .api import (
    CalendarHorizonExceeded,
    ScheduleCallback,
    TradingCalendar,
)
from .api import (
    Scheduler as _SchedulerProtocol,  # type: ignore  # noqa: F401
)
from .calendar import EASTERN

_MAX_LOOKAHEAD_DAYS: Final[int] = 14
# Fixed sentinel datetime used solely to validate cron expression syntax at
# registration time. We deliberately do *not* call ``_dt.datetime.now`` here:
# the trading-platform deterministic-time rule (Codex round 10) requires that
# schedule registration produce identical state under an injected backtest
# clock and the live wall clock. croniter only needs a tz-aware base to
# accept the expression — the actual fire-time computation always uses the
# ``after`` argument supplied to ``next_fire_time``.
_CRON_VALIDATION_SENTINEL: Final = "2000-01-01T12:00"
# Cron resolution uses a much larger elapsed-time lookahead so sparse
# expressions resolve correctly: a monthly ``0 9 1 * *`` after Jan 2 must
# walk past Feb 1 and Mar 1 (both 2026 Sundays) to reach Apr 1 ≈ 89 days
# later, and an annual ``0 0 1 1 *`` from mid-year must walk ~6 months to
# the next 1-Jan. 380 days covers every annual cron with leap-year slack;
# leap-year-only crons (``0 0 29 2 *`` — fires every 4 years) are
# explicitly out of scope and surface as ValueError.
_CRON_LOOKAHEAD_DAYS: Final[int] = 380


@dataclass(slots=True)
class _Handle:
    """Concrete ``ScheduleHandle`` returned by every ``InMemoryScheduler`` method."""

    handle_id: int
    scheduler: "InMemoryScheduler"
    _cancelled: bool = False

    def cancel(self) -> None:
        """Cancel this scheduled trigger; idempotent."""
        if self._cancelled:
            return
        self._cancelled = True
        self.scheduler._cancel(self.handle_id)

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@dataclass(frozen=True, slots=True)
class _Entry:
    """Internal scheduler record."""

    handle_id: int
    kind: str  # "market_open" | "market_close" | "every_n_minutes" | "cron"
    callback: ScheduleCallback
    # Per-kind parameters.
    offset_minutes: int = 0
    n_minutes: int = 0
    only_during_session: bool = True
    cron_expression: str = ""


class _CronAdapter:
    """Wrap ``croniter`` so the rest of the SDK never imports it directly.

    Keeps the croniter vendor binding inside this single module so the
    SRS-SDK-001 parity AST scan does not need a vendor-import whitelist
    growth.

    DST handling: ``croniter`` with a ``zoneinfo`` tz-aware base does
    *not* preserve wall-clock time across US Eastern DST transitions —
    e.g., ``0 9 * * 1-5`` from a Friday EST base would drift to 08:00
    EDT on the following Monday (SyRS SYS-50 / SYS-51 violation). The
    same expression with a ``pytz``-aware base does preserve wall-clock
    time. We localize the base via ``pytz`` for the cron walk and
    convert each returned candidate back to ``zoneinfo`` so the public
    API only ever exposes ``zoneinfo``-aware datetimes (``pytz`` is a
    transitive dependency of ``exchange_calendars`` and is already
    installed).
    """

    def __init__(self, expression: str, base: _dt.datetime) -> None:
        try:
            from croniter import CroniterBadCronError, croniter
        except ImportError as exc:  # pragma: no cover - dep is in requirements.txt
            raise RuntimeError("croniter is not installed; add it to requirements.txt") from exc
        try:
            import pytz
        except ImportError as exc:  # pragma: no cover - transitive dep
            raise RuntimeError(
                "pytz is not installed; expected as a transitive dependency of exchange_calendars"
            ) from exc
        if base.tzinfo is None:
            raise ValueError("cron base datetime must be tz-aware")
        # Re-localize the base in pytz so croniter advances wall-clock
        # time (not fixed-offset time) across DST boundaries.
        pytz_eastern = pytz.timezone("America/New_York")
        base_pytz = base.astimezone(pytz_eastern)
        try:
            self._iter = croniter(expression, base_pytz)
        except (CroniterBadCronError, ValueError) as exc:
            raise ValueError(f"invalid cron expression: {expression!r}") from exc

    def next_datetime(self) -> _dt.datetime:
        """Return the next fire time as a tz-aware (zoneinfo) ``datetime``."""
        value = self._iter.get_next(_dt.datetime)
        if value.tzinfo is None:  # defensive — croniter preserves input tz
            value = value.replace(tzinfo=EASTERN)
        return value.astimezone(EASTERN)


@dataclass(slots=True)
class InMemoryScheduler:
    """Concrete ``Scheduler`` resolving fire times against a ``TradingCalendar``.

    The four ``Scheduler`` Protocol methods (``at_market_open``,
    ``at_market_close``, ``every_n_minutes``, ``cron``) all return a
    ``_Handle`` whose ``cancel()`` is idempotent. ``next_fire_time`` is
    exposed so the orchestrator's fire loop (and the L7 domain tests)
    can resolve a handle to its next scheduled trigger without dispatch.

    Example:
        >>> import datetime as dt
        >>> from atp_strategy import UsEquityTradingCalendar
        >>> cal = UsEquityTradingCalendar.for_exchange("NYSE")
        >>> s = InMemoryScheduler(calendar=cal)
        >>> h = s.at_market_open(lambda ctx: None)
        >>> h.cancelled
        False
    """

    calendar: TradingCalendar
    _entries: dict[int, _Entry] = field(default_factory=dict)
    _counter: itertools.count = field(default_factory=lambda: itertools.count(1))

    # -- Scheduler Protocol methods -------------------------------------- #

    def at_market_open(self, callback: ScheduleCallback, *, offset_minutes: int = 0) -> _Handle:
        """Fire ``callback`` at the regular session open, plus an optional offset.

        Resolves against the bound ``TradingCalendar`` so holidays,
        early closes, and DST transitions are handled centrally.
        ``offset_minutes`` outside the session's
        ``[premarket_open, afterhours_close]`` window raises
        ``ValueError`` at resolution time.
        """
        return self._register(
            _Entry(
                handle_id=next(self._counter),
                kind="market_open",
                callback=callback,
                offset_minutes=offset_minutes,
            )
        )

    def at_market_close(self, callback: ScheduleCallback, *, offset_minutes: int = 0) -> _Handle:
        """Fire ``callback`` at the regular (or early) session close + offset.

        On early-close sessions (e.g. day after Thanksgiving) the
        anchor is the 13:00 ET close, so ``offset_minutes=-5`` fires
        at 12:55 ET on those days. Same out-of-window guard as
        ``at_market_open``.
        """
        return self._register(
            _Entry(
                handle_id=next(self._counter),
                kind="market_close",
                callback=callback,
                offset_minutes=offset_minutes,
            )
        )

    def every_n_minutes(
        self,
        n: int,
        callback: ScheduleCallback,
        *,
        only_during_session: bool = True,
    ) -> _Handle:
        """Fire ``callback`` every ``n`` minutes, optionally only during a session.

        ``only_during_session=True`` (default) suppresses ticks
        outside ``[session_open, session_close]`` and snaps the next
        candidate to the next session's open. Set ``False`` for 24/7
        ticks. ``n`` must be a positive integer.
        """
        if n <= 0:
            raise ValueError(f"every_n_minutes requires n > 0; got {n}")
        return self._register(
            _Entry(
                handle_id=next(self._counter),
                kind="every_n_minutes",
                callback=callback,
                n_minutes=n,
                only_during_session=only_during_session,
            )
        )

    def cron(self, expression: str, callback: ScheduleCallback) -> _Handle:
        """Fire ``callback`` on a cron-like schedule expression (calendar-filtered).

        Standard 5-field cron syntax (``"min hr dom mon dow"``).
        Resolution walks against the bound trading calendar and
        skips candidates that land off-session or outside
        ``[04:00, 20:00]`` ET. Malformed expressions raise
        ``ValueError`` at registration time.
        """
        # Validate eagerly so the caller sees malformed expressions at
        # registration time, but use a fixed sentinel base — never the host
        # wall clock — so registration produces identical state under an
        # injected backtest clock and the live wall clock. The real fire-
        # time computation always uses the ``after`` argument supplied to
        # ``next_fire_time`` (Codex round 10).
        sentinel = _dt.datetime.fromisoformat(_CRON_VALIDATION_SENTINEL).replace(tzinfo=EASTERN)
        _CronAdapter(expression, sentinel)
        return self._register(
            _Entry(
                handle_id=next(self._counter),
                kind="cron",
                callback=callback,
                cron_expression=expression,
            )
        )

    # -- Inspection / resolution ----------------------------------------- #

    def next_fire_time(self, handle: _Handle, after: _dt.datetime) -> _dt.datetime | None:
        """Resolve the next fire time for ``handle`` strictly after ``after``.

        Return value:
            ``None`` if the handle has been cancelled or removed from the
            scheduler (``handle.cancel()`` already called, or the handle
            was never registered with this scheduler).

        Raises:
            ValueError: if ``after`` is not tz-aware; if a cron expression
                never fires within the ``[04:00, 20:00]`` ET pre-market /
                after-hours window within the scheduler's bundled
                ``_CRON_LOOKAHEAD_DAYS`` calendar horizon; or if an
                ``at_market_open`` / ``at_market_close`` offset escapes
                the same-session pre-market / after-hours window. These
                are programming errors (misconfigured schedule), not
                "no trigger right now" outcomes — they surface as typed
                exceptions so the operator can fix the schedule rather
                than silently dispatching at the wrong time.
        """
        # Reject handles owned by another scheduler instance. Without this
        # guard, two InMemoryScheduler instances both starting their counter
        # at 1 could let a handle from scheduler A resolve against
        # scheduler B's entry (handle_id collision) — Codex round 11.
        if handle.scheduler is not self:
            return None
        if handle.cancelled or handle.handle_id not in self._entries:
            return None
        if after.tzinfo is None:
            raise ValueError("next_fire_time requires a tz-aware 'after' datetime")
        entry = self._entries[handle.handle_id]
        after_et = after.astimezone(EASTERN)
        if entry.kind == "market_open":
            return self._resolve_market_open(entry, after_et)
        if entry.kind == "market_close":
            return self._resolve_market_close(entry, after_et)
        if entry.kind == "every_n_minutes":
            return self._resolve_every_n(entry, after_et)
        if entry.kind == "cron":
            return self._resolve_cron(entry, after_et)
        raise AssertionError(f"unknown schedule kind: {entry.kind!r}")  # pragma: no cover

    def __len__(self) -> int:
        """Number of active (non-cancelled) schedule entries."""
        return len(self._entries)

    # -- Internals ------------------------------------------------------- #

    def _register(self, entry: _Entry) -> _Handle:
        self._entries[entry.handle_id] = entry
        return _Handle(handle_id=entry.handle_id, scheduler=self)

    def _cancel(self, handle_id: int) -> None:
        self._entries.pop(handle_id, None)

    def _resolve_market_open(self, entry: _Entry, after_et: _dt.datetime) -> _dt.datetime:
        cursor = after_et.date()
        for _ in range(_MAX_LOOKAHEAD_DAYS + 1):
            if self.calendar.is_session(cursor):
                fire = self.calendar.session_open(cursor) + _dt.timedelta(
                    minutes=entry.offset_minutes
                )
                self._require_within_session_window(
                    fire, cursor, kind="at_market_open", offset=entry.offset_minutes
                )
                if fire > after_et:
                    return fire
            cursor = cursor + _dt.timedelta(days=1)
        raise AssertionError("no market_open within 15-day window")  # pragma: no cover

    def _resolve_market_close(self, entry: _Entry, after_et: _dt.datetime) -> _dt.datetime:
        cursor = after_et.date()
        for _ in range(_MAX_LOOKAHEAD_DAYS + 1):
            if self.calendar.is_session(cursor):
                fire = self.calendar.session_close(cursor) + _dt.timedelta(
                    minutes=entry.offset_minutes
                )
                self._require_within_session_window(
                    fire, cursor, kind="at_market_close", offset=entry.offset_minutes
                )
                if fire > after_et:
                    return fire
            cursor = cursor + _dt.timedelta(days=1)
        raise AssertionError("no market_close within 15-day window")  # pragma: no cover

    def _require_within_session_window(
        self,
        fire: _dt.datetime,
        session_date: _dt.date,
        *,
        kind: str,
        offset: int,
    ) -> None:
        """Reject offsets that escape the same session's pre/after-hours window.

        SyRS SYS-50 / SYS-51: ``at_market_open`` and ``at_market_close``
        must dispatch within the same trading day's [04:00, 20:00] ET
        platform window. A large negative open offset can resolve into the
        prior holiday; a large positive close offset can resolve after the
        after-hours close. Both are misconfigurations the calendar-aware
        scheduler must refuse rather than silently fire into a closed phase.
        """
        pre = self.calendar.premarket_open(session_date)
        post = self.calendar.afterhours_close(session_date)
        if not (pre <= fire <= post):
            raise ValueError(
                f"{kind}(offset_minutes={offset}) on session "
                f"{session_date.isoformat()} resolves to {fire.isoformat()} "
                f"outside the [{pre.isoformat()}, {post.isoformat()}] "
                "pre-market / after-hours window — offset is too large"
            )

    def _resolve_every_n(self, entry: _Entry, after_et: _dt.datetime) -> _dt.datetime:
        candidate = after_et + _dt.timedelta(minutes=entry.n_minutes)
        if not entry.only_during_session:
            return candidate
        # Walk forward until we land inside [session_open, session_close] of a
        # session day. On a non-session day OR a session day past close, snap
        # to the NEXT session day's open — advancing by ``n_minutes`` alone
        # would keep daily-interval candidates stuck past close indefinitely
        # (Codex round 9). Bound by elapsed wall-clock time, not iteration
        # count, so daily / weekly intervals across long-weekend gaps resolve.
        deadline = after_et + _dt.timedelta(days=_MAX_LOOKAHEAD_DAYS)
        while candidate <= deadline:
            cdate = candidate.date()
            try:
                on_session = self.calendar.is_session(cdate)
            except CalendarHorizonExceeded as exc:
                raise ValueError(
                    f"every_n_minutes(n={entry.n_minutes}) cannot resolve "
                    f"within the bundled calendar horizon ({exc})"
                ) from exc
            if on_session:
                s_open = self.calendar.session_open(cdate)
                s_close = self.calendar.session_close(cdate)
                if s_open <= candidate <= s_close:
                    return candidate
                if candidate < s_open:
                    candidate = s_open
                    continue
                # candidate > s_close — fall through to next-session snap.
            try:
                next_date = self.calendar.next_session(cdate)
            except CalendarHorizonExceeded as exc:
                raise ValueError(
                    f"every_n_minutes(n={entry.n_minutes}) cannot resolve "
                    f"within the bundled calendar horizon ({exc})"
                ) from exc
            candidate = self.calendar.session_open(next_date)
        raise ValueError(
            f"every_n_minutes(n={entry.n_minutes}) did not find a fire time "
            f"within the {_MAX_LOOKAHEAD_DAYS}-day lookahead"
        )

    def _resolve_cron(self, entry: _Entry, after_et: _dt.datetime) -> _dt.datetime:
        adapter = _CronAdapter(entry.cron_expression, after_et)
        # Walk cron candidates; accept the first one that lands on a regular
        # session AND inside the platform pre-market / after-hours window
        # ([04:00, 20:00] ET). SyRS SYS-50 / SYS-51: cron-like expressions
        # resolve against the trading-calendar boundaries, so a 02:00 ET
        # cron that would fire outside any trading-day phase is rejected.
        # Bound by *elapsed wall-clock time* — not by candidate count — so a
        # minute-level cron survives long-weekend gaps (e.g., Friday after-
        # hours close to Tuesday pre-market open across MLK weekend) without
        # being prematurely rejected.
        deadline = after_et + _dt.timedelta(days=_CRON_LOOKAHEAD_DAYS)
        while True:
            candidate = adapter.next_datetime()
            if candidate > deadline:
                raise ValueError(
                    f"cron expression {entry.cron_expression!r} never fires "
                    "within the pre-market / after-hours session window "
                    f"([04:00, 20:00] ET on a trading day) within the "
                    f"{_CRON_LOOKAHEAD_DAYS}-day lookahead — schedule cannot resolve"
                )
            cdate = candidate.date()
            try:
                if not self.calendar.is_session(cdate):
                    continue
                pre = self.calendar.premarket_open(cdate)
                post = self.calendar.afterhours_close(cdate)
            except CalendarHorizonExceeded as exc:
                # Pathological cron whose candidates never land in-window
                # eventually walks past the bundled calendar horizon — that
                # is just a slow path to the same answer.
                raise ValueError(
                    f"cron expression {entry.cron_expression!r} never fires "
                    "within the pre-market / after-hours session window "
                    "([04:00, 20:00] ET on a trading day) — schedule cannot "
                    f"resolve within the bundled calendar horizon ({exc})"
                ) from exc
            if pre <= candidate <= post:
                return candidate
