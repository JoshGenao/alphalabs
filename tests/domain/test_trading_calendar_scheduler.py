"""SRS-SDK-002 — Trading-calendar-aware scheduling (L7 domain / safety).

Anchors the SyRS SYS-6 / SYS-50 / SYS-51 behavioural invariants that
the L3 mutation rig cannot probe — actual NYSE 2026 holiday list, real
DST transitions, early-close hour, multi-exchange parity, and cron
filtration through the calendar. Source of canonical dates is
``architecture/runtime_services.json::strategy_api_scheduler_contract``
so a future calendar-data refresh sees a single source of truth.
"""

from __future__ import annotations

import datetime as dt
import json
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
    UsEquityTradingCalendar,
)

pytestmark = [pytest.mark.domain, pytest.mark.safety]

EASTERN = zoneinfo.ZoneInfo("America/New_York")


def _load_contract() -> dict:
    return json.loads(
        (ROOT / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )["strategy_api_scheduler_contract"]


def _noop(_ctx: object) -> None:
    return None


class CboeAliasDocumentedOverrideTest(unittest.TestCase):
    """CBOE-Options is intentionally aliased to XNYS; the deferral is recorded.

    SRS-SDK-002 names NYSE, NASDAQ, and CBOE. ``exchange_calendars`` ships no
    dedicated CBOE Options calendar (XCBF is CBOE Futures with a 16:15 ET
    close and Chicago tz — wrong asset class). For the strategy SDK's U.S.
    equity-class option workflows, NYSE / NASDAQ / CBOE-Options share the
    SIFMA holiday calendar in practice (Codex round 13: documented override).

    This test locks two assertions so the override cannot silently drift:
    (a) the public ``_EXCHANGE_HANDLES`` mapping still aliases ``CBOE`` to
        ``XNYS``;
    (b) ``strategy_api_scheduler_contract.deferred`` mentions the alias and
        names ``exchange_calendars`` as the reason so a future maintainer
        sees the breadcrumb. If a real CBOE Options handle ships, both
        assertions need updating — that update is the signal that the
        deferral has been honored.
    """

    def test_cboe_alias_and_deferral_string_are_in_sync(self) -> None:
        from atp_strategy.calendar import _EXCHANGE_HANDLES  # noqa: PLC0415

        self.assertEqual(
            _EXCHANGE_HANDLES.get("CBOE"),
            "XNYS",
            "CBOE handle drifted away from XNYS without an explicit "
            "deferred[] update — see Codex round 13 rationale",
        )
        contract = _load_contract()
        joined = " | ".join(contract["deferred"])
        self.assertIn(
            "CBOE Options is aliased to XNYS",
            joined,
            "deferred[] no longer documents the CBOE → XNYS alias rationale",
        )
        self.assertIn(
            "exchange_calendars ships no dedicated CBOE Options calendar",
            joined,
            "deferred[] lost the underlying-library rationale for the alias",
        )


class CanonicalHolidayInvariantTest(unittest.TestCase):
    """Every canonical 2026 NYSE/NASDAQ/CBOE holiday is non-session."""

    def setUp(self) -> None:
        self.contract = _load_contract()
        self.calendars = {
            name: UsEquityTradingCalendar.for_exchange(name)
            for name in self.contract["required_exchange_handles"]
        }

    def test_canonical_holidays_non_session_on_all_exchanges(self) -> None:
        holidays = [dt.date.fromisoformat(s) for s in self.contract["canonical_holidays_2026"]]
        self.assertEqual(len(holidays), 10, "SyRS SYS-50 canonical 2026 list size")
        for holiday in holidays:
            for name, cal in self.calendars.items():
                self.assertFalse(
                    cal.is_session(holiday),
                    f"{name} treated {holiday.isoformat()} as a session — "
                    "SyRS SYS-50 canonical holiday list is broken",
                )


class EarlyCloseInvariantTest(unittest.TestCase):
    """Every canonical 2026 early-close day closes at 13:00 ET."""

    def setUp(self) -> None:
        self.contract = _load_contract()
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")

    def test_canonical_early_closes_have_1pm_close(self) -> None:
        early_closes = [
            dt.date.fromisoformat(s) for s in self.contract["canonical_early_closes_2026"]
        ]
        self.assertEqual(len(early_closes), 2)
        for date in early_closes:
            self.assertTrue(
                self.cal.is_early_close(date),
                f"{date.isoformat()} not detected as early-close",
            )
            s_close = self.cal.session_close(date)
            self.assertEqual(
                (s_close.hour, s_close.minute),
                (13, 0),
                f"{date.isoformat()} close is {s_close.time()} ET; expected 13:00 ET",
            )


class DstTransitionInvariantTest(unittest.TestCase):
    """Session open UTC offset crosses from EST to EDT on the canonical dates."""

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.contract = _load_contract()

    def test_spring_forward_session_offsets(self) -> None:
        # Fri 2026-03-06 is the last EST session before DST→ on Sun 2026-03-08.
        first_edt = dt.date.fromisoformat(
            self.contract["dst_transition_dates_2026"]["first_edt_session_after_spring"]
        )
        before = dt.date(2026, 3, 6)
        open_before = self.cal.session_open(before)
        open_after = self.cal.session_open(first_edt)
        self.assertEqual(
            open_before.utcoffset(),
            dt.timedelta(hours=-5),
            f"EST session before DST→ should be UTC-5 (got {open_before.utcoffset()})",
        )
        self.assertEqual(
            open_after.utcoffset(),
            dt.timedelta(hours=-4),
            f"EDT session after DST→ should be UTC-4 (got {open_after.utcoffset()})",
        )
        # Local clock face stays at 09:30 across the transition.
        self.assertEqual((open_before.hour, open_before.minute), (9, 30))
        self.assertEqual((open_after.hour, open_after.minute), (9, 30))

    def test_fall_back_session_offsets(self) -> None:
        # Fri 2026-10-30 is EDT; first EST session after DST← (Sun 2026-11-01) is Mon 2026-11-02.
        first_est = dt.date.fromisoformat(
            self.contract["dst_transition_dates_2026"]["first_est_session_after_fall"]
        )
        before = dt.date(2026, 10, 30)
        self.assertEqual(self.cal.session_open(before).utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(self.cal.session_open(first_est).utcoffset(), dt.timedelta(hours=-5))


class CronCalendarFiltrationTest(unittest.TestCase):
    """``Scheduler.cron`` resolutions skip non-session days."""

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_cron_skips_mlk_monday(self) -> None:
        # 0 9 * * 1-5 = weekdays at 09:00 ET. Cron alone would fire on
        # 2026-01-19 (MLK Mon); the calendar filter must skip to Tue 2026-01-20.
        h = self.s.cron("0 9 * * 1-5", _noop)
        after = dt.datetime(2026, 1, 18, 0, 0, tzinfo=EASTERN)  # Sun
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(
            fire.date(),
            dt.date(2026, 1, 20),
            f"cron landed on {fire.date()} instead of skipping MLK to 2026-01-20",
        )
        self.assertEqual((fire.hour, fire.minute), (9, 0))


class EarlyCloseDispatchTest(unittest.TestCase):
    """``at_market_close`` fires at 13:00 ET on early-close days."""

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_market_close_on_black_friday(self) -> None:
        h = self.s.at_market_close(_noop)
        # 2026-11-27 09:00 ET — before close.
        after = dt.datetime(2026, 11, 27, 9, 0, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 11, 27))
        self.assertEqual(
            (fire.hour, fire.minute),
            (13, 0),
            f"at_market_close fired at {fire.time()} ET; expected 13:00 ET",
        )

    def test_market_close_on_christmas_eve(self) -> None:
        h = self.s.at_market_close(_noop)
        after = dt.datetime(2026, 12, 24, 9, 0, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 12, 24))
        self.assertEqual((fire.hour, fire.minute), (13, 0))


class MarketOpenHolidaySkipTest(unittest.TestCase):
    """``at_market_open`` skips holidays and lands on the next session."""

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_market_open_from_friday_skips_mlk_weekend(self) -> None:
        h = self.s.at_market_open(_noop)
        # Fri 2026-01-16 18:00 ET (after Friday's open). Next session is
        # Tue 2026-01-20 (MLK Mon is closed).
        after = dt.datetime(2026, 1, 16, 18, 0, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 1, 20))
        self.assertEqual((fire.hour, fire.minute), (9, 30))


class MarketOpenCloseOffsetWindowTest(unittest.TestCase):
    """``at_market_open`` / ``at_market_close`` offsets that escape the
    same-session [04:00, 20:00] ET window must be rejected.

    SyRS SYS-50 / SYS-51: the calendar-aware scheduler must not silently
    dispatch into a closed-market phase. A misconfigured negative open
    offset that would land before 04:00 ET, or a positive close offset
    that would land after 20:00 ET, is a programming error the resolver
    surfaces immediately.
    """

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_negative_open_offset_before_premarket_raises(self) -> None:
        # session_open = 09:30 ET; -360 min = 03:30 ET, before 04:00 premarket.
        h = self.s.at_market_open(_noop, offset_minutes=-360)
        with self.assertRaisesRegex(ValueError, r"at_market_open.*outside the.*pre-market"):
            self.s.next_fire_time(h, dt.datetime(2026, 1, 19, 0, 0, tzinfo=EASTERN))

    def test_positive_close_offset_after_afterhours_raises(self) -> None:
        # session_close = 16:00 ET; +300 min = 21:00 ET, after 20:00 afterhours.
        h = self.s.at_market_close(_noop, offset_minutes=+300)
        with self.assertRaisesRegex(ValueError, r"at_market_close.*outside the.*pre-market"):
            self.s.next_fire_time(h, dt.datetime(2026, 1, 19, 0, 0, tzinfo=EASTERN))

    def test_safe_negative_open_offset_to_premarket_open_is_allowed(self) -> None:
        # -330 min from 09:30 = 04:00 ET, exactly at premarket boundary.
        h = self.s.at_market_open(_noop, offset_minutes=-330)
        fire = self.s.next_fire_time(h, dt.datetime(2026, 1, 19, 0, 0, tzinfo=EASTERN))
        assert fire is not None
        self.assertEqual((fire.hour, fire.minute), (4, 0))

    def test_safe_positive_close_offset_to_afterhours_close_is_allowed(self) -> None:
        # +240 min from 16:00 = 20:00 ET, exactly at afterhours boundary.
        h = self.s.at_market_close(_noop, offset_minutes=+240)
        fire = self.s.next_fire_time(h, dt.datetime(2026, 1, 19, 0, 0, tzinfo=EASTERN))
        assert fire is not None
        self.assertEqual((fire.hour, fire.minute), (20, 0))


class CronDstWallClockPreservationTest(unittest.TestCase):
    """Cron expressions preserve US-Eastern wall-clock across DST transitions.

    SyRS SYS-50 / SYS-51: scheduling resolves against US Eastern with
    daylight-saving transitions. A weekday 09:00 ET cron must fire at
    09:00 EST in winter and 09:00 EDT in summer (different UTC times,
    identical wall-clock). croniter with ``zoneinfo`` drifts across DST
    boundaries; the adapter localizes via ``pytz`` to preserve wall-clock.
    """

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_cron_across_spring_forward_preserves_wall_clock(self) -> None:
        # Spring forward: Sun 2026-03-08. Fri 2026-03-06 is the last EST
        # session before the transition. From Fri 20:01 EST the next
        # "0 9 * * 1-5" must fire at 09:00 EDT on Mon 2026-03-09 — not 08:00.
        h = self.s.cron("0 9 * * 1-5", _noop)
        after = dt.datetime(2026, 3, 6, 20, 1, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 3, 9))
        self.assertEqual((fire.hour, fire.minute), (9, 0))
        self.assertEqual(
            fire.utcoffset(),
            dt.timedelta(hours=-4),
            f"first post-DST session should be EDT (UTC-4); got {fire.utcoffset()}",
        )

    def test_cron_across_fall_back_preserves_wall_clock(self) -> None:
        # Fall back: Sun 2026-11-01. Fri 2026-10-30 is the last EDT
        # session before the transition. From Fri 20:01 EDT the next
        # "0 9 * * 1-5" must fire at 09:00 EST on Mon 2026-11-02 — not 10:00.
        h = self.s.cron("0 9 * * 1-5", _noop)
        after = dt.datetime(2026, 10, 30, 20, 1, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 11, 2))
        self.assertEqual((fire.hour, fire.minute), (9, 0))
        self.assertEqual(
            fire.utcoffset(),
            dt.timedelta(hours=-5),
            f"first post-fall-back session should be EST (UTC-5); got {fire.utcoffset()}",
        )


class CronSparseExpressionLookaheadTest(unittest.TestCase):
    """Sparse cron expressions (monthly, annual) resolve across non-session months.

    SyRS SYS-50 / SYS-51: cron-like expressions resolve against the
    trading calendar. A monthly ``0 9 1 * *`` (1st of month at 09:00 ET)
    must walk past month-starts that land on weekends and find the next
    session-day candidate — even when that requires looking ~90 days out.
    A cron resolver with a too-short elapsed-time bound would falsely
    reject legitimate sparse schedules.
    """

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_month_start_cron_skips_weekend_months_to_first_session(self) -> None:
        # 2026 calendar: Feb 1 = Sunday, Mar 1 = Sunday, Apr 1 = Wednesday.
        # From 2026-01-02 the next 0 9 1 * * fire is Apr 1 09:00 EDT,
        # ~89 days out — past any short-window lookahead but within the
        # documented sparse-cron horizon. This is the exact case Codex
        # round 8 flagged when the lookahead was 45 days.
        h = self.s.cron("0 9 1 * *", _noop)
        after = dt.datetime(2026, 1, 2, 0, 0, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 4, 1))
        self.assertEqual((fire.hour, fire.minute), (9, 0))

    def test_yearly_crons_outside_horizon_raise_value_error(self) -> None:
        # Annual / leap-year crons whose first three calendar-Jan-1s land
        # on non-sessions (Jan 1 2027 = Fri New Year's holiday; Jan 1 2028
        # = Sat) push past the 380-day lookahead. Trading-platform schedules
        # are intraday-to-monthly per SyRS SYS-6; yearly crons surface as
        # ValueError rather than being silently extended into multi-year
        # walks. Locks the documented horizon contract.
        h = self.s.cron("0 9 1 1 *", _noop)
        after = dt.datetime(2026, 7, 15, 0, 0, tzinfo=EASTERN)
        with self.assertRaisesRegex(ValueError, "380-day lookahead"):
            self.s.next_fire_time(h, after)


class CronLongWeekendLookaheadTest(unittest.TestCase):
    """Minute-level cron expressions survive long-weekend session gaps.

    SyRS SYS-50 / SYS-51: cron-like expressions resolve against the
    trading calendar. After the Friday before a long weekend, the next
    valid trigger window may sit four full calendar days away (Friday
    after-hours close → Tuesday pre-market open across MLK / Presidents
    Day / Memorial Day weekends). A cron resolver bounded by candidate
    count instead of elapsed time can falsely reject these schedules.
    """

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_minute_cron_resolves_across_mlk_weekend(self) -> None:
        # 2026-01-16 (Fri) ends at 16:00 ET regular close. MLK Day is
        # Mon 2026-01-19; first session afterward is Tue 2026-01-20.
        # Starting from Fri 2026-01-16 20:01 ET — past the after-hours
        # close — the next valid * * * * * cron candidate is Tue 04:00 ET
        # (~3 days later). Candidate-count bounds would falsely reject;
        # the elapsed-time bound must accept.
        h = self.s.cron("* * * * *", _noop)
        after = dt.datetime(2026, 1, 16, 20, 1, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 1, 20))
        self.assertEqual((fire.hour, fire.minute), (4, 0))


class CronWindowEnforcementTest(unittest.TestCase):
    """Cron expressions outside [04:00, 20:00] ET refuse to resolve.

    SyRS SYS-50 / SYS-51: scheduling resolves against pre-market and
    after-hours session boundaries; a cron whose fire times fall entirely
    outside the platform [04:00, 20:00] ET window has no valid trading-
    day phase to dispatch in.
    """

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_overnight_cron_at_2am_raises_value_error(self) -> None:
        h = self.s.cron("0 2 * * *", _noop)
        with self.assertRaisesRegex(ValueError, "never fires within the pre-market"):
            self.s.next_fire_time(h, dt.datetime(2026, 1, 19, 0, 0, tzinfo=EASTERN))

    def test_late_evening_cron_at_2100_raises_value_error(self) -> None:
        # 21:00 ET is after the platform 20:00 ET after-hours close.
        h = self.s.cron("0 21 * * *", _noop)
        with self.assertRaises(ValueError):
            self.s.next_fire_time(h, dt.datetime(2026, 1, 19, 0, 0, tzinfo=EASTERN))

    def test_premarket_0430_cron_fires_inside_window(self) -> None:
        # 04:30 ET sits inside the [04:00, 20:00] window.
        h = self.s.cron("30 4 * * 1-5", _noop)
        fire = self.s.next_fire_time(h, dt.datetime(2026, 1, 18, 0, 0, tzinfo=EASTERN))
        assert fire is not None
        self.assertEqual((fire.hour, fire.minute), (4, 30))
        # MLK 2026-01-19 is non-session — must skip to Tue 2026-01-20.
        self.assertEqual(fire.date(), dt.date(2026, 1, 20))

    def test_after_hours_1900_cron_fires_inside_window(self) -> None:
        h = self.s.cron("0 19 * * 1-5", _noop)
        fire = self.s.next_fire_time(h, dt.datetime(2026, 1, 18, 0, 0, tzinfo=EASTERN))
        assert fire is not None
        self.assertEqual((fire.hour, fire.minute), (19, 0))
        self.assertEqual(fire.date(), dt.date(2026, 1, 20))


class EveryNMinutesPostCloseSnapTest(unittest.TestCase):
    """``every_n_minutes`` candidates past session_close snap to next session open.

    Codex round 9: ``every_n_minutes(1440, only_during_session=True)`` starting
    at Fri 16:01 ET previously advanced by exactly 24h each iteration, so it
    stayed past close on every subsequent day and eventually exhausted the
    iteration bound. The resolver now snaps post-close candidates to the
    next session's open, so daily / weekly intervals resolve correctly.
    """

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_daily_interval_after_close_snaps_to_next_session_open(self) -> None:
        h = self.s.every_n_minutes(1440, _noop, only_during_session=True)
        # Fri 2026-01-16 16:01 ET (1 minute past regular close).
        after = dt.datetime(2026, 1, 16, 16, 1, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        # Sat/Sun/MLK Mon are non-sessions; next session is Tue 2026-01-20.
        self.assertEqual(fire.date(), dt.date(2026, 1, 20))
        self.assertEqual((fire.hour, fire.minute), (9, 30))

    def test_intraday_interval_at_close_returns_close_then_next_open(self) -> None:
        # every_n_minutes(15) starting at 15:46 → 16:01 (past close) →
        # snap to next session open.
        h = self.s.every_n_minutes(15, _noop, only_during_session=True)
        after = dt.datetime(2026, 1, 16, 15, 46, tzinfo=EASTERN)
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        self.assertEqual(fire.date(), dt.date(2026, 1, 20))
        self.assertEqual((fire.hour, fire.minute), (9, 30))


class EveryNMinutesHolidaySkipTest(unittest.TestCase):
    """``every_n_minutes(only_during_session=True)`` skips entire holiday days."""

    def setUp(self) -> None:
        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_every_30min_skips_mlk_monday(self) -> None:
        h = self.s.every_n_minutes(30, _noop, only_during_session=True)
        after = dt.datetime(2026, 1, 16, 16, 0, tzinfo=EASTERN)  # Fri close
        fire = self.s.next_fire_time(h, after)
        assert fire is not None
        # The next in-session minute is Tue 2026-01-20 09:30 ET — MLK Mon must skip.
        self.assertEqual(fire.date(), dt.date(2026, 1, 20))
        self.assertEqual((fire.hour, fire.minute), (9, 30))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
