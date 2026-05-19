"""L1 unit tests for ``UsEquityTradingCalendar`` (SRS-SDK-002)."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
import zoneinfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import pytest  # noqa: E402
from atp_strategy import (  # noqa: E402
    CalendarHorizonExceeded,
    NotATradingSession,
    StaticTradingCalendar,
    UsEquityTradingCalendar,
)

pytestmark = pytest.mark.unit

EASTERN = zoneinfo.ZoneInfo("America/New_York")


class ExchangeFactoryTest(unittest.TestCase):
    def test_three_required_exchange_names_resolve(self) -> None:
        for name in ("NYSE", "NASDAQ", "CBOE"):
            cal = UsEquityTradingCalendar.for_exchange(name)
            self.assertEqual(cal.name, name)

    def test_unknown_exchange_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported exchange"):
            UsEquityTradingCalendar.for_exchange("LSE")

    def test_case_insensitive(self) -> None:
        cal = UsEquityTradingCalendar.for_exchange("nyse")
        self.assertEqual(cal.name, "NYSE")


class SessionAndHolidayLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")

    def test_weekend_not_session(self) -> None:
        self.assertFalse(self.cal.is_session(dt.date(2026, 5, 30)))  # Sat
        self.assertFalse(self.cal.is_session(dt.date(2026, 5, 31)))  # Sun

    def test_canonical_2026_holiday_not_session(self) -> None:
        # MLK Day 2026-01-19 (Monday).
        self.assertFalse(self.cal.is_session(dt.date(2026, 1, 19)))

    def test_regular_weekday_is_session(self) -> None:
        self.assertTrue(self.cal.is_session(dt.date(2026, 1, 20)))  # Tue


class SessionTimesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")

    def test_regular_open_is_0930_eastern(self) -> None:
        s_open = self.cal.session_open(dt.date(2026, 1, 20))
        self.assertEqual((s_open.hour, s_open.minute), (9, 30))
        self.assertIsNotNone(s_open.tzinfo)

    def test_regular_close_is_1600_eastern(self) -> None:
        s_close = self.cal.session_close(dt.date(2026, 1, 20))
        self.assertEqual((s_close.hour, s_close.minute), (16, 0))

    def test_early_close_returns_1300_eastern(self) -> None:
        # 2026-11-27 (day after Thanksgiving) is a canonical early-close.
        s_close = self.cal.session_close(dt.date(2026, 11, 27))
        self.assertEqual((s_close.hour, s_close.minute), (13, 0))
        self.assertTrue(self.cal.is_early_close(dt.date(2026, 11, 27)))

    def test_premarket_open_is_0400_eastern(self) -> None:
        pre = self.cal.premarket_open(dt.date(2026, 1, 20))
        self.assertEqual((pre.hour, pre.minute), (4, 0))

    def test_afterhours_close_is_2000_eastern(self) -> None:
        ah = self.cal.afterhours_close(dt.date(2026, 1, 20))
        self.assertEqual((ah.hour, ah.minute), (20, 0))


class BoundaryRejectsNonSessionTest(unittest.TestCase):
    """All four boundary methods refuse to fabricate times on closed days."""

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")

    def test_weekend_boundary_lookups_raise_not_a_trading_session(self) -> None:
        saturday = dt.date(2026, 1, 17)
        for method in ("session_open", "session_close", "premarket_open", "afterhours_close"):
            with self.assertRaises(NotATradingSession):
                getattr(self.cal, method)(saturday)

    def test_holiday_boundary_lookups_raise_not_a_trading_session(self) -> None:
        mlk = dt.date(2026, 1, 19)
        for method in ("session_open", "session_close", "premarket_open", "afterhours_close"):
            with self.assertRaises(NotATradingSession):
                getattr(self.cal, method)(mlk)


class HorizonGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")

    def test_far_future_raises_calendar_horizon_exceeded(self) -> None:
        # exchange_calendars bundles roughly one rolling year of forward
        # data; 2099-01-01 is comfortably outside that horizon.
        with self.assertRaises(CalendarHorizonExceeded):
            self.cal.is_session(dt.date(2099, 1, 1))

    def test_horizon_guard_independent_of_dependency_raise(self) -> None:
        """SDK guard fires even if the lib silently returns False past horizon.

        Defense-in-depth (Codex round 3): a future ``exchange_calendars``
        release that returns ``False`` for an out-of-range date instead of
        raising would otherwise let stale calendar data masquerade as a
        market holiday.
        """
        from unittest.mock import patch

        # Pick a date one day past the bundled last session.
        from atp_strategy.calendar import _get_underlying  # noqa: PLC0415

        underlying = _get_underlying("XNYS")
        far_future = underlying.last_session.date() + dt.timedelta(days=1)

        # Replace the lib's is_session with a stub that always returns False
        # (no exception). The SDK's local horizon guard must still fire.
        with patch.object(type(underlying), "is_session", return_value=False):
            with self.assertRaises(CalendarHorizonExceeded):
                self.cal.is_session(far_future)


class CalendarHorizonPinTest(unittest.TestCase):
    """The exchange_calendars horizon is pinned to a deterministic range.

    Without an explicit ``start`` / ``end`` pin the lib uses a host-date-
    rolling default window — the same session-validity query could pass
    today and raise ``CalendarHorizonExceeded`` next year (Codex round 7).
    The pin must match the constants recorded in
    ``architecture/runtime_services.json::strategy_api_scheduler_contract``.
    """

    def test_pin_matches_contract_block(self) -> None:
        import json

        from atp_strategy.calendar import _get_underlying  # noqa: PLC0415

        contract = json.loads(
            (ROOT / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
        )["strategy_api_scheduler_contract"]
        underlying = _get_underlying("XNYS")
        self.assertEqual(
            underlying.first_session.date(),
            dt.date.fromisoformat(contract["calendar_horizon_start"]) + dt.timedelta(days=2),
            "first_session should be the first business day on/after the "
            "pinned calendar_horizon_start (2000-01-01 was Saturday; first "
            "session is 2000-01-03)",
        )
        self.assertEqual(
            underlying.last_session.date(),
            dt.date.fromisoformat(contract["calendar_horizon_end"]),
            "last_session should match the pinned calendar_horizon_end",
        )


class StaticTradingCalendarProtocolCoverageTest(unittest.TestCase):
    """``StaticTradingCalendar`` is a complete ``TradingCalendar`` Protocol impl.

    The scheduler's cron / market-open / market-close paths call
    ``premarket_open`` and ``afterhours_close`` on whatever calendar the
    user wires in. The minimal in-tree stub must satisfy the full Protocol
    or strategies that compose against `StaticTradingCalendar` will
    AttributeError mid-dispatch (caught by Codex round-2).
    """

    def setUp(self) -> None:
        self.cal = StaticTradingCalendar()

    def test_premarket_open_is_0400_eastern(self) -> None:
        out = self.cal.premarket_open(dt.date(2026, 5, 4))
        self.assertEqual((out.hour, out.minute), (4, 0))
        self.assertIsNotNone(out.tzinfo)

    def test_afterhours_close_is_2000_eastern(self) -> None:
        out = self.cal.afterhours_close(dt.date(2026, 5, 4))
        self.assertEqual((out.hour, out.minute), (20, 0))
        self.assertIsNotNone(out.tzinfo)

    def test_weekend_boundary_methods_raise_not_a_trading_session(self) -> None:
        # Codex round 11: StaticTradingCalendar must not fabricate boundary
        # times on closed days. Saturday 2026-05-09 hits all four boundary
        # methods identically to UsEquityTradingCalendar's contract.
        saturday = dt.date(2026, 5, 9)
        for method in (
            "session_open",
            "session_close",
            "premarket_open",
            "afterhours_close",
        ):
            with self.assertRaises(NotATradingSession):
                getattr(self.cal, method)(saturday)


class NextSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")

    def test_skips_weekend(self) -> None:
        # 2026-01-16 is a Friday; next session is Tue 2026-01-20 (MLK Mon).
        self.assertEqual(
            self.cal.next_session(dt.date(2026, 1, 16)),
            dt.date(2026, 1, 20),
        )

    def test_skips_holiday(self) -> None:
        # From MLK Mon, the next session is Tue 2026-01-20.
        self.assertEqual(
            self.cal.next_session(dt.date(2026, 1, 19)),
            dt.date(2026, 1, 20),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
