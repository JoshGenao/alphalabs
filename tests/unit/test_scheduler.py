"""L1 unit tests for ``InMemoryScheduler`` (SRS-SDK-002)."""

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
    InMemoryScheduler,
    StaticTradingCalendar,
    UsEquityTradingCalendar,
)

pytestmark = pytest.mark.unit

EASTERN = zoneinfo.ZoneInfo("America/New_York")


def _noop(_ctx: object) -> None:
    return None


class StaticCalendarSchedulerCompatibilityTest(unittest.TestCase):
    """``InMemoryScheduler`` works against any ``TradingCalendar`` Protocol impl.

    Codex round 12: ``_resolve_every_n`` called ``calendar.next_session``
    which lived only on ``UsEquityTradingCalendar``. Strategies that wire
    ``InMemoryScheduler`` against ``StaticTradingCalendar`` (the public
    in-tree stub) would AttributeError on the first post-close
    ``every_n_minutes`` resolution. The Protocol now requires
    ``next_session`` and both implementations satisfy it.
    """

    def setUp(self) -> None:
        self.s = InMemoryScheduler(calendar=StaticTradingCalendar())

    def test_every_n_minutes_after_close_resolves_with_static_calendar(self) -> None:
        # Fri 2026-01-16 16:30 ET. StaticTradingCalendar has no holiday
        # concept, so next session = Mon 2026-01-19 at 09:30 ET (static
        # always opens at 09:30 ET).
        h = self.s.every_n_minutes(60, _noop, only_during_session=True)
        after = dt.datetime(2026, 1, 16, 16, 30, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 1, 19))
        self.assertEqual((fire.hour, fire.minute), (9, 30))


class HandleOwnershipTest(unittest.TestCase):
    """Handles produced by one scheduler are rejected by another.

    Both ``InMemoryScheduler`` instances start their internal counter at 1,
    so a handle from scheduler A and an entry in scheduler B both have
    ``handle_id == 1``. ``next_fire_time`` must compare the handle's
    scheduler reference (``handle.scheduler is self``) before touching
    the entries dict, otherwise it can dispatch the wrong strategy's
    schedule (Codex round 11).
    """

    def setUp(self) -> None:
        cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s1 = InMemoryScheduler(calendar=cal)
        self.s2 = InMemoryScheduler(calendar=cal)

    def test_cross_scheduler_handle_returns_none(self) -> None:
        h1 = self.s1.at_market_open(_noop)
        h2 = self.s2.at_market_open(_noop)
        # Both first handles have handle_id == 1 by construction.
        self.assertEqual(h1.handle_id, h2.handle_id)
        # Resolving h1 against s2 must NOT pick up s2's entry-1.
        after = dt.datetime(2026, 1, 17, 12, 0, tzinfo=EASTERN)
        self.assertIsNone(self.s2.next_fire_time(h1, after))
        self.assertIsNone(self.s1.next_fire_time(h2, after))
        # Each handle still resolves against its own scheduler.
        self.assertIsNotNone(self.s1.next_fire_time(h1, after))
        self.assertIsNotNone(self.s2.next_fire_time(h2, after))


class HandleCancellationTest(unittest.TestCase):
    def setUp(self) -> None:
        cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=cal)

    def test_handle_cancel_is_idempotent(self) -> None:
        h = self.s.at_market_open(_noop)
        self.assertFalse(h.cancelled)
        self.assertEqual(len(self.s), 1)
        h.cancel()
        self.assertTrue(h.cancelled)
        self.assertEqual(len(self.s), 0)
        # Second cancel is a no-op.
        h.cancel()
        self.assertTrue(h.cancelled)

    def test_next_fire_time_after_cancel_returns_none(self) -> None:
        h = self.s.every_n_minutes(15, _noop)
        h.cancel()
        self.assertIsNone(self.s.next_fire_time(h, dt.datetime(2026, 1, 20, 10, 0, tzinfo=EASTERN)))


class InputValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=cal)

    def test_every_n_minutes_rejects_non_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "n > 0"):
            self.s.every_n_minutes(0, _noop)
        with self.assertRaisesRegex(ValueError, "n > 0"):
            self.s.every_n_minutes(-5, _noop)

    def test_cron_rejects_malformed_expression(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid cron"):
            self.s.cron("not a cron", _noop)

    def test_next_fire_time_requires_tz_aware_after(self) -> None:
        h = self.s.at_market_open(_noop)
        with self.assertRaisesRegex(ValueError, "tz-aware"):
            self.s.next_fire_time(h, dt.datetime(2026, 1, 20, 10, 0))


class EveryNMinutesResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=cal)

    def test_advance_during_session(self) -> None:
        h = self.s.every_n_minutes(30, _noop)
        after = dt.datetime(2026, 1, 20, 10, 14, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual((fire.hour, fire.minute), (10, 44))
        self.assertEqual(fire.utcoffset(), dt.timedelta(hours=-5))

    def test_jumps_to_next_session_after_close(self) -> None:
        h = self.s.every_n_minutes(30, _noop)
        after = dt.datetime(2026, 1, 20, 16, 30, tzinfo=EASTERN)  # past close
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 1, 21))
        self.assertEqual((fire.hour, fire.minute), (9, 30))

    def test_only_during_session_false_returns_raw_step(self) -> None:
        h = self.s.every_n_minutes(15, _noop, only_during_session=False)
        after = dt.datetime(2026, 1, 17, 12, 0, tzinfo=EASTERN)  # Saturday
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire, dt.datetime(2026, 1, 17, 12, 15, tzinfo=EASTERN))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
