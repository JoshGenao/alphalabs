"""Trading-calendar implementation backed by the ``exchange_calendars`` lib.

SRS trace: ``SRS-SDK-002`` (SyRS ``SYS-6``, ``SYS-50``, ``SYS-51``;
StRS ``SN-1.09``, ``SN-1.19``).

``UsEquityTradingCalendar`` is the concrete ``TradingCalendar`` used by
the Python Strategy SDK. It exposes regular-session open / close in
US-Eastern time (DST-aware via ``zoneinfo("America/New_York")``), the
canonical NYSE / NASDAQ / CBOE U.S. equities holiday list, and the
13:00 ET early-close days. Pre-market (04:00 ET) and after-hours
(20:00 ET) boundaries are platform constants exposed here as well.

All three exchange names (NYSE, NASDAQ, CBOE) currently share the
``XNYS`` underlying calendar — NYSE/NASDAQ aliases are equivalent in
``exchange_calendars``, and the library ships no dedicated CBOE Options
calendar (``XCBF`` is CBOE Futures, with a 16:15 ET close and a
Chicago timezone — wrong for U.S. equity-class strategy hours). Should
the lib gain a real CBOE Options handle, the ``_EXCHANGE_HANDLES`` map
is the single point of update.
"""

from __future__ import annotations

import datetime as _dt
import zoneinfo
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from .api import CalendarHorizonExceeded, NotATradingSession

# Re-export so callers can construct a calendar without separately importing
# the underlying library — keeps SDK code free of vendor-import paths.

EASTERN: Final[zoneinfo.ZoneInfo] = zoneinfo.ZoneInfo("America/New_York")

# Pinned ``exchange_calendars`` horizon. Without explicit ``start`` / ``end``
# the lib uses a host-date-rolling default window (~20y back, ~1y forward),
# which makes session-validity decisions non-deterministic across runs — the
# same query can pass today and raise ``CalendarHorizonExceeded`` next year.
# Pinning to a wide fixed window guarantees deterministic resolution. Mirrored
# in ``architecture/runtime_services.json::strategy_api_scheduler_contract``
# (keys ``calendar_horizon_start`` / ``calendar_horizon_end``) and asserted by
# the contract-check script. Widen these constants together when SRS-BT-*
# needs older history or live scheduling exceeds 2035.
_HORIZON_START: Final[str] = "2000-01-01"
_HORIZON_END: Final[str] = "2035-12-31"

# Regular-session open and close for U.S. equities (NYSE/NASDAQ/CBOE
# equity-class). Early-close days override the close hour to 13:00 ET.
_REGULAR_OPEN_HM: Final[tuple[int, int]] = (9, 30)
_REGULAR_CLOSE_HM: Final[tuple[int, int]] = (16, 0)
_EARLY_CLOSE_HM: Final[tuple[int, int]] = (13, 0)

# Platform-fixed pre-market / after-hours bounds (SyRS SYS-50).
_PREMARKET_OPEN_HM: Final[tuple[int, int]] = (4, 0)
_AFTER_HOURS_CLOSE_HM: Final[tuple[int, int]] = (20, 0)

# Maps the user-facing exchange name to the ``exchange_calendars`` ISO
# handle. All three currently resolve to the same underlying session list.
_EXCHANGE_HANDLES: Final[dict[str, str]] = {
    "NYSE": "XNYS",
    "NASDAQ": "XNAS",
    "CBOE": "XNYS",
}


@lru_cache(maxsize=8)
def _get_underlying(iso_code: str):  # type: ignore[no-untyped-def]
    """Return the cached ``exchange_calendars`` instance for ``iso_code``.

    Pinned to ``[_HORIZON_START, _HORIZON_END]`` so the accepted session
    range is date-deterministic across runs.
    """
    import exchange_calendars  # local import keeps the SDK import-light

    return exchange_calendars.get_calendar(iso_code, start=_HORIZON_START, end=_HORIZON_END)


def _classify_horizon_error(exc: BaseException) -> bool:
    """Return True when ``exc`` is the lib's out-of-bounds signal."""
    name = type(exc).__name__
    return name in {"DateOutOfBounds", "DatetimeIndexError"}


@dataclass(frozen=True, slots=True)
class UsEquityTradingCalendar:
    """Real NYSE / NASDAQ / CBOE U.S.-equity-class trading calendar.

    Implements the ``TradingCalendar`` Protocol defined in
    ``python/atp_strategy/api.py``. Holidays, early closes, and DST
    transitions resolve through the ``exchange_calendars`` library;
    open / close times are synthesized as 9:30 ET / 16:00 ET (or
    13:00 ET on early-close days) in DST-aware US Eastern time.

    Example:
        >>> import datetime as dt
        >>> cal = UsEquityTradingCalendar.for_exchange("NYSE")
        >>> cal.is_session(dt.date(2026, 1, 19))  # MLK Day
        False
        >>> cal.is_session(dt.date(2026, 1, 20))  # Tue after MLK
        True
        >>> cal.is_early_close(dt.date(2026, 11, 27))  # Day after Thanksgiving
        True
    """

    name: str
    _iso_code: str

    @classmethod
    def for_exchange(cls, name: str) -> "UsEquityTradingCalendar":
        """Construct a calendar for ``name`` (``"NYSE"``, ``"NASDAQ"``, ``"CBOE"``).

        Raises:
            ValueError: if ``name`` is not a supported exchange.
        """
        upper = name.upper()
        try:
            iso = _EXCHANGE_HANDLES[upper]
        except KeyError as exc:
            raise ValueError(
                f"unsupported exchange: {name!r}; supported: {sorted(_EXCHANGE_HANDLES)}"
            ) from exc
        return cls(name=upper, _iso_code=iso)

    # -- TradingCalendar Protocol surface --------------------------------- #

    def is_session(self, date: _dt.date) -> bool:
        """Return True if ``date`` is a regular trading session.

        Enforces the SDK-local horizon guard *before* delegating to the
        underlying ``exchange_calendars`` lib so stale calendar data
        cannot be silently misclassified as a market holiday if a future
        lib version returns ``False`` (instead of raising) for an
        out-of-range date. The library exception handler below is a
        backstop for edge cases where ``first_session`` / ``last_session``
        and ``is_session`` disagree on the accepted range.
        """
        self._ensure_within_horizon(date)
        cal = _get_underlying(self._iso_code)
        try:
            return bool(cal.is_session(date.isoformat()))
        except Exception as exc:
            if _classify_horizon_error(exc):
                raise CalendarHorizonExceeded(
                    f"{date.isoformat()} is past the bundled calendar horizon "
                    f"(last session: {cal.last_session.date().isoformat()})"
                ) from exc
            raise

    def session_open(self, date: _dt.date) -> _dt.datetime:
        """Return the regular session open for ``date`` in US Eastern time.

        Raises ``NotATradingSession`` if ``date`` is a weekend or holiday —
        fabricating an open on a closed-market day would silently leak into
        scheduled order submission.
        """
        self._require_session(date)
        hour, minute = _REGULAR_OPEN_HM
        return _dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=EASTERN)

    def session_close(self, date: _dt.date) -> _dt.datetime:
        """Return the regular (or early) session close for ``date`` in ET.

        Raises ``NotATradingSession`` if ``date`` is a weekend or holiday.
        """
        self._require_session(date)
        hour, minute = _EARLY_CLOSE_HM if self.is_early_close(date) else _REGULAR_CLOSE_HM
        return _dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=EASTERN)

    def is_early_close(self, date: _dt.date) -> bool:
        """Return True if ``date`` is a session with an early (13:00 ET) close."""
        if not self.is_session(date):
            return False
        cal = _get_underlying(self._iso_code)
        close_ts = cal.session_close(date.isoformat())
        close_et = close_ts.tz_convert(EASTERN)
        return (close_et.hour, close_et.minute) < _REGULAR_CLOSE_HM

    # -- Platform-fixed pre/after-market boundaries ----------------------- #

    def premarket_open(self, date: _dt.date) -> _dt.datetime:
        """Return the platform-fixed pre-market open (04:00 ET) for ``date``.

        Raises ``NotATradingSession`` if ``date`` is a weekend or holiday —
        there is no pre-market on a closed-market day.
        """
        self._require_session(date)
        hour, minute = _PREMARKET_OPEN_HM
        return _dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=EASTERN)

    def afterhours_close(self, date: _dt.date) -> _dt.datetime:
        """Return the platform-fixed after-hours close (20:00 ET) for ``date``.

        Raises ``NotATradingSession`` if ``date`` is a weekend or holiday.
        """
        self._require_session(date)
        hour, minute = _AFTER_HOURS_CLOSE_HM
        return _dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=EASTERN)

    # -- Helpers for the scheduler --------------------------------------- #

    def next_session(self, after: _dt.date) -> _dt.date:
        """Return the first session date strictly after ``after``.

        Walks up to 14 calendar days forward; raises
        ``CalendarHorizonExceeded`` if no session is found within that
        window (which would only happen near the calendar horizon).
        """
        cursor = after + _dt.timedelta(days=1)
        for _ in range(14):
            if self.is_session(cursor):
                return cursor
            cursor += _dt.timedelta(days=1)
        raise CalendarHorizonExceeded(
            f"no trading session within 14 days after {after.isoformat()}"
        )

    def _ensure_within_horizon(self, date: _dt.date) -> None:
        """Raise ``CalendarHorizonExceeded`` if ``date`` is past lib bounds."""
        cal = _get_underlying(self._iso_code)
        first = cal.first_session.date()
        last = cal.last_session.date()
        if date < first or date > last:
            raise CalendarHorizonExceeded(
                f"{date.isoformat()} outside bundled calendar horizon "
                f"({first.isoformat()}..{last.isoformat()})"
            )

    def _require_session(self, date: _dt.date) -> None:
        """Raise ``NotATradingSession`` for weekends / holidays.

        Boundary methods (``session_open`` / ``session_close`` /
        ``premarket_open`` / ``afterhours_close``) must not fabricate
        hypothetical trading times on closed-market days — the resulting
        ``datetime`` would silently propagate into the scheduler and risk
        an order submission against an actually-closed exchange.
        ``is_session`` itself first runs the horizon guard, so this also
        raises ``CalendarHorizonExceeded`` for past-bounds dates.
        """
        if not self.is_session(date):
            raise NotATradingSession(
                f"{date.isoformat()} is not a regular {self.name} trading "
                "session (weekend or holiday) — call is_session() first or "
                "use next_session() to walk forward"
            )
