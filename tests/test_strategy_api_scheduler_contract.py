"""Contract tests for SRS-SDK-002 (SyRS SYS-6 / SYS-50 / SYS-51; StRS
SN-1.09 / SN-1.19 / BG-1 / BG-7).

Shells out to ``tools/strategy_api_scheduler_check.py`` for the
positive-evidence path, then mutates a tmpdir copy of
``python/atp_strategy/`` to verify each invariant in the scheduler
contract actually catches a regression: dropped Scheduler Protocol
methods, dropped TradingCalendar Protocol methods, dropped exchange
handles, naive (non-tz-aware) session times, and dropped concrete
class exports.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from strategy_api_scheduler_check import (  # noqa: E402
    StrategyApiSchedulerCheckError,
    assert_strategy_api_scheduler_static,
    load_config,
)


class _MutationRig:
    """Copy ``python/atp_strategy/`` into a tmpdir and run the scheduler check."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "python").mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            ROOT / "python" / "atp_strategy",
            self.root / "python" / "atp_strategy",
        )

    def close(self) -> None:
        self._tmp.cleanup()

    def mutate(self, relpath: str, *, find: str, replace: str) -> None:
        target = self.root / "python" / "atp_strategy" / relpath
        text = target.read_text(encoding="utf-8")
        if find not in text:
            raise AssertionError(f"mutation rig: substring not found in {relpath}: {find!r}")
        target.write_text(text.replace(find, replace, 1), encoding="utf-8")

    def run(self, config: dict) -> list[str]:
        return assert_strategy_api_scheduler_static(config, root=self.root)


class NextFireTimePublicContractTest(unittest.TestCase):
    """``InMemoryScheduler.next_fire_time`` matches its public docstring.

    The docstring promises ``None`` for cancelled/removed handles and a
    typed ``ValueError`` for misconfigured schedules — never silent dispatch
    or unexpected exception types. Locking the contract here prevents drift
    between docstring and implementation (Codex round 6).
    """

    def setUp(self) -> None:
        # Local import to avoid coupling this contract test to the L3
        # mutation rig — it exercises the public package surface only.
        import sys

        python_root = ROOT / "python"
        if str(python_root) not in sys.path:
            sys.path.insert(0, str(python_root))
        from atp_strategy import InMemoryScheduler, UsEquityTradingCalendar  # noqa: PLC0415

        self.cal = UsEquityTradingCalendar.for_exchange("NYSE")
        self.s = InMemoryScheduler(calendar=self.cal)

    def test_cancelled_handle_returns_none(self) -> None:
        import datetime as dt
        import zoneinfo

        eastern = zoneinfo.ZoneInfo("America/New_York")
        h = self.s.at_market_open(lambda _ctx: None)
        h.cancel()
        result = self.s.next_fire_time(h, dt.datetime(2026, 1, 17, 12, 0, tzinfo=eastern))
        self.assertIsNone(result, "cancelled handle must return None per the docstring")

    def test_unresolvable_cron_raises_value_error(self) -> None:
        import datetime as dt
        import zoneinfo

        eastern = zoneinfo.ZoneInfo("America/New_York")
        # 02:00 ET daily fires entirely outside the [04:00, 20:00] window.
        h = self.s.cron("0 2 * * *", lambda _ctx: None)
        with self.assertRaisesRegex(ValueError, "never fires within the pre-market"):
            self.s.next_fire_time(h, dt.datetime(2026, 1, 19, 0, 0, tzinfo=eastern))

    def test_offset_outside_window_raises_value_error(self) -> None:
        import datetime as dt
        import zoneinfo

        eastern = zoneinfo.ZoneInfo("America/New_York")
        # -360 min from session_open 09:30 = 03:30 ET, before 04:00 premarket.
        h = self.s.at_market_open(lambda _ctx: None, offset_minutes=-360)
        with self.assertRaisesRegex(ValueError, r"at_market_open.*outside the.*pre-market"):
            self.s.next_fire_time(h, dt.datetime(2026, 1, 19, 0, 0, tzinfo=eastern))

    def test_cron_registration_is_clock_deterministic(self) -> None:
        """Codex round 10: schedule registration must not read the host clock.

        Backtests inject an "as-of" clock and require that registration
        produce identical handle state regardless of when the test runs.
        ``cron()`` must validate syntax against a fixed sentinel, never
        ``datetime.now()``. A source-level assertion is the cleanest check
        — patching ``datetime.datetime.now`` cross-process is fragile.
        """
        import inspect

        from atp_strategy.scheduler import InMemoryScheduler  # noqa: PLC0415

        src = inspect.getsource(InMemoryScheduler.cron)
        self.assertNotIn(
            "datetime.now",
            src,
            "cron() registration reads host clock — determinism violation",
        )
        self.assertNotIn(
            "_dt.datetime.now",
            src,
            "cron() registration reads host clock — determinism violation",
        )
        # And confirm registration succeeds without any clock side-effect.
        h = self.s.cron("0 9 * * 1-5", lambda _ctx: None)
        self.assertFalse(h.cancelled)


class StrategyApiSchedulerScriptTest(unittest.TestCase):
    def test_script_passes_and_emits_evidence_needles(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/strategy_api_scheduler_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SDK-002 PASS", result.stdout)
        for needle in (
            "Scheduler Protocol declares all 4 required methods",
            "at_market_open, at_market_close, every_n_minutes, cron",
            "TradingCalendar Protocol declares all 7 required methods",
            "is_session, session_open, session_close, is_early_close, premarket_open, afterhours_close, next_session",
            "concrete UsEquityTradingCalendar and InMemoryScheduler classes",
            "for_exchange resolves all 3 required exchange handles",
            "NYSE, NASDAQ, CBOE",
            "tz-aware ET datetimes at 09:30 / 16:00",
            "SYS-50 / SYS-51 DST-aware resolution",
            "horizon pinned to [2000-01-01, 2035-12-31]",
            "date-deterministic across runs",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class SchedulerProtocolMethodMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_at_market_open_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    def at_market_open(\n"
            "        self, callback: ScheduleCallback, *, offset_minutes: int = 0\n"
            "    ) -> ScheduleHandle:",
            replace="    def _removed_at_market_open(\n"
            "        self, callback: ScheduleCallback, *, offset_minutes: int = 0\n"
            "    ) -> ScheduleHandle:",
        )
        with self.assertRaisesRegex(
            StrategyApiSchedulerCheckError,
            "Scheduler Protocol is missing required methods",
        ):
            self.rig.run(self.config)


class CalendarProtocolMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_renaming_is_session_is_caught(self) -> None:
        self.rig.mutate(
            "api.py",
            find="    def is_session(self, date: _dt.date) -> bool:\n"
            '        """Return True if ``date`` is a regular trading session."""',
            replace="    def renamed_is_session(self, date: _dt.date) -> bool:\n"
            '        """Return True if ``date`` is a regular trading session."""',
        )
        with self.assertRaisesRegex(
            StrategyApiSchedulerCheckError,
            "TradingCalendar Protocol is missing required methods",
        ):
            self.rig.run(self.config)

    def test_dropping_premarket_open_is_caught(self) -> None:
        # Both Protocol and StaticTradingCalendar must declare it; renaming
        # the Protocol method while leaving the dataclass alone should still
        # trip the scheduler check via the required_calendar_methods list.
        self.rig.mutate(
            "api.py",
            find="    def premarket_open(self, date: _dt.date) -> _dt.datetime:\n"
            '        """Return the pre-market open for ``date`` in US Eastern time."""',
            replace="    def renamed_premarket_open(self, date: _dt.date) -> _dt.datetime:\n"
            '        """Return the pre-market open for ``date`` in US Eastern time."""',
        )
        with self.assertRaisesRegex(
            StrategyApiSchedulerCheckError,
            "TradingCalendar Protocol is missing required methods",
        ):
            self.rig.run(self.config)


class ConcreteClassExportMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_concrete_calendar_export_is_caught(self) -> None:
        self.rig.mutate(
            "__init__.py",
            find="from .calendar import UsEquityTradingCalendar",
            replace="# from .calendar import UsEquityTradingCalendar",
        )
        with self.assertRaisesRegex(
            StrategyApiSchedulerCheckError,
            "UsEquityTradingCalendar is not re-exported",
        ):
            self.rig.run(self.config)

    def test_dropping_concrete_scheduler_export_is_caught(self) -> None:
        self.rig.mutate(
            "__init__.py",
            find="from .scheduler import InMemoryScheduler",
            replace="# from .scheduler import InMemoryScheduler",
        )
        with self.assertRaisesRegex(
            StrategyApiSchedulerCheckError,
            "InMemoryScheduler is not re-exported",
        ):
            self.rig.run(self.config)


class ExchangeHandleMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_removing_cboe_handle_is_caught(self) -> None:
        self.rig.mutate(
            "calendar.py",
            find='"CBOE": "XNYS",',
            replace="",
        )
        with self.assertRaisesRegex(
            StrategyApiSchedulerCheckError,
            r"for_exchange\('CBOE'\) failed",
        ):
            self.rig.run(self.config)


class TimezoneAwarenessMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_naive_session_open_is_caught(self) -> None:
        # Replace tz-aware constructor with a naive one.
        self.rig.mutate(
            "calendar.py",
            find="return _dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=EASTERN)\n\n    def session_close",
            replace="return _dt.datetime(date.year, date.month, date.day, hour, minute)\n\n    def session_close",
        )
        with self.assertRaisesRegex(
            StrategyApiSchedulerCheckError,
            "naive datetime",
        ):
            self.rig.run(self.config)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
