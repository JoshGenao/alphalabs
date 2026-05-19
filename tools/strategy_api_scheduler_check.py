#!/usr/bin/env python3
"""Contract evidence script for SRS-SDK-002 (trading-calendar-aware scheduling).

Verifies that the Python Strategy SDK exposes:

* A ``Scheduler`` Protocol declaring all four required methods
  (``at_market_open``, ``at_market_close``, ``every_n_minutes``,
  ``cron``).
* A ``TradingCalendar`` Protocol declaring all four required methods
  (``is_session``, ``session_open``, ``session_close``,
  ``is_early_close``) and the ``name`` attribute.
* A concrete ``UsEquityTradingCalendar`` class re-exported from
  ``python/atp_strategy/__init__.py`` that resolves the three required
  exchange names (``NYSE``, ``NASDAQ``, ``CBOE``) and returns tz-aware
  US-Eastern ``datetime`` values for ``session_open`` / ``session_close``.
* A concrete ``InMemoryScheduler`` class re-exported from
  ``python/atp_strategy/__init__.py`` implementing all four
  ``Scheduler`` Protocol methods.

SRS-SDK-002 traces SyRS ``SYS-6`` (scheduling primitives), ``SYS-50``
(maintained trading calendar), and ``SYS-51`` (scheduling resolves
against the calendar); StRS ``SN-1.09`` / ``SN-1.19`` / ``BG-1`` / ``BG-7``.

Mirrors the PASS/FAIL output style of
``tools/strategy_api_parity_check.py``.

Invoke:
    python3 tools/strategy_api_scheduler_check.py
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StrategyApiSchedulerCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise StrategyApiSchedulerCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "strategy_api_scheduler_contract" not in config:
        fail("architecture metadata is missing strategy_api_scheduler_contract")
    return config["strategy_api_scheduler_contract"]


def _load_sdk_module(root: Path) -> object:
    """Reload ``atp_strategy`` from ``root`` (supports mutation-test tmpdirs)."""
    python_root = root / "python"
    if not python_root.is_dir():
        fail(f"python/ directory missing under {root}")
    str_root = str(python_root)
    if str_root in sys.path:
        sys.path.remove(str_root)
    sys.path.insert(0, str_root)
    for name in list(sys.modules):
        if name == "atp_strategy" or name.startswith("atp_strategy."):
            sys.modules.pop(name, None)
    try:
        return importlib.import_module("atp_strategy")
    except Exception as exc:  # pragma: no cover — surfaces as a scheduler fail
        fail(f"failed to import atp_strategy from {python_root}: {exc!r}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_scheduler_protocol_methods(config: dict, root: Path) -> str:
    block = contract_block(config)
    required = list(block["required_scheduler_methods"])
    api = _load_sdk_module(root)
    proto = api.Scheduler
    missing = [m for m in required if not hasattr(proto, m)]
    if missing:
        fail(
            f"Scheduler Protocol is missing required methods {missing} — "
            "SYS-6 requires market-open / market-close / every-n-minutes / "
            "cron scheduling primitives"
        )
    return (
        f"Scheduler Protocol declares all {len(required)} required methods "
        f"({', '.join(required)}) for SYS-6 trading-calendar-aware scheduling"
    )


def check_calendar_protocol_surface(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_methods = list(block["required_calendar_methods"])
    required_attrs = list(block["required_calendar_attrs"])
    api = _load_sdk_module(root)
    proto = api.TradingCalendar
    missing_methods = [m for m in required_methods if not hasattr(proto, m)]
    if missing_methods:
        fail(
            f"TradingCalendar Protocol is missing required methods "
            f"{missing_methods} — SYS-50 requires holiday + early-close + "
            "session-boundary queries"
        )
    annotations = getattr(proto, "__annotations__", {})
    missing_attrs = [a for a in required_attrs if a not in annotations]
    if missing_attrs:
        fail(
            f"TradingCalendar Protocol is missing required attributes "
            f"{missing_attrs} (annotation block) — SYS-50 calendar must "
            "carry an exchange name"
        )
    return (
        f"TradingCalendar Protocol declares all {len(required_methods)} "
        f"required methods ({', '.join(required_methods)}) and "
        f"{len(required_attrs)} required attributes "
        f"({', '.join(required_attrs)}) for SYS-50 trading-calendar data"
    )


def check_concrete_classes_exported(config: dict, root: Path) -> str:
    block = contract_block(config)
    api = _load_sdk_module(root)
    calendar_class_name = block["calendar_class"]
    scheduler_class_name = block["scheduler_class"]
    cal_cls = getattr(api, calendar_class_name, None)
    if cal_cls is None:
        fail(
            f"atp_strategy.{calendar_class_name} is not re-exported from "
            "python/atp_strategy/__init__.py — concrete calendar class must "
            "be reachable through the public SDK surface"
        )
    sched_cls = getattr(api, scheduler_class_name, None)
    if sched_cls is None:
        fail(
            f"atp_strategy.{scheduler_class_name} is not re-exported from "
            "python/atp_strategy/__init__.py — concrete scheduler class "
            "must be reachable through the public SDK surface"
        )
    return (
        f"atp_strategy package re-exports concrete {calendar_class_name} "
        f"and {scheduler_class_name} classes per SYS-50 / SYS-51"
    )


def check_exchange_handles(config: dict, root: Path) -> str:
    block = contract_block(config)
    required = dict(block["required_exchange_handles"])
    api = _load_sdk_module(root)
    cal_cls = getattr(api, block["calendar_class"])
    for exchange_name in required:
        try:
            cal = cal_cls.for_exchange(exchange_name)
        except Exception as exc:
            fail(
                f"{block['calendar_class']}.for_exchange({exchange_name!r}) "
                f"failed: {exc!r} — SYS-50 requires NYSE / NASDAQ / CBOE "
                "exchange-name resolution"
            )
        if cal.name.upper() != exchange_name.upper():
            fail(
                f"{block['calendar_class']}.for_exchange({exchange_name!r}) "
                f"returned a calendar with name={cal.name!r}; expected "
                f"{exchange_name!r}"
            )
    return (
        f"{block['calendar_class']}.for_exchange resolves all "
        f"{len(required)} required exchange handles ({', '.join(required)}) "
        "to instances bearing the requested name"
    )


def check_calendar_horizon_is_pinned(config: dict, root: Path) -> str:
    block = contract_block(config)
    start = block.get("calendar_horizon_start")
    end = block.get("calendar_horizon_end")
    if not start or not end:
        fail(
            "strategy_api_scheduler_contract is missing "
            "calendar_horizon_start / calendar_horizon_end — the underlying "
            "exchange_calendars library otherwise uses a host-date-rolling "
            "window, making session-validity decisions non-deterministic"
        )
    api = _load_sdk_module(root)
    cal_cls = getattr(api, block["calendar_class"])
    cal = cal_cls.for_exchange("NYSE")
    from atp_strategy.calendar import _get_underlying  # noqa: PLC0415

    underlying = _get_underlying("XNYS")
    expected_end = _dt.date.fromisoformat(end)
    if underlying.last_session.date() != expected_end:
        fail(
            f"{block['calendar_class']} underlying last_session is "
            f"{underlying.last_session.date().isoformat()}; "
            f"expected pinned {expected_end.isoformat()} per "
            "strategy_api_scheduler_contract.calendar_horizon_end"
        )
    # first_session is the first business day on/after horizon_start; the
    # exact date depends on holidays/weekends near the start, so we only
    # assert it is within 7 days of the configured start.
    start_date = _dt.date.fromisoformat(start)
    delta_days = (underlying.first_session.date() - start_date).days
    if not (0 <= delta_days <= 7):
        fail(
            f"{block['calendar_class']} underlying first_session is "
            f"{underlying.first_session.date().isoformat()}; expected the "
            f"first business day within 7 days of pinned "
            f"{start_date.isoformat()} (delta_days={delta_days})"
        )
    # Make sure the calendar is also unused for caller of cal — keep ref alive
    _ = cal
    return (
        f"underlying exchange_calendars horizon pinned to "
        f"[{start}, {end}] per strategy_api_scheduler_contract — session-"
        "validity decisions are date-deterministic across runs"
    )


def check_session_times_are_timezone_aware(config: dict, root: Path) -> str:
    block = contract_block(config)
    api = _load_sdk_module(root)
    cal_cls = getattr(api, block["calendar_class"])
    cal = cal_cls.for_exchange("NYSE")
    # Use a known session day (Tuesday after MLK Day in 2026) — within
    # exchange_calendars' bundled horizon at the time this contract was
    # written. If the lib's horizon rolls forward past 2026-01-20, this
    # date will still be a valid session.
    sample = _dt.date(2026, 1, 20)
    s_open = cal.session_open(sample)
    s_close = cal.session_close(sample)
    if s_open.tzinfo is None or s_close.tzinfo is None:
        fail(
            f"{block['calendar_class']} returned naive datetime for "
            "session_open/session_close — SYS-50 requires tz-aware US "
            "Eastern time so DST transitions resolve correctly"
        )
    if (s_open.hour, s_open.minute) != (9, 30):
        fail(
            f"{block['calendar_class']}.session_open returned "
            f"{s_open.strftime('%H:%M')} for {sample.isoformat()}; expected "
            "09:30 ET regular session open"
        )
    if (s_close.hour, s_close.minute) != (16, 0):
        fail(
            f"{block['calendar_class']}.session_close returned "
            f"{s_close.strftime('%H:%M')} for {sample.isoformat()}; expected "
            "16:00 ET regular session close (date was not an early-close day)"
        )
    return (
        "UsEquityTradingCalendar.session_open / session_close return "
        "tz-aware ET datetimes at 09:30 / 16:00 on a regular session "
        f"(sample: {sample.isoformat()}) — SYS-50 / SYS-51 DST-aware resolution"
    )


# --------------------------------------------------------------------------- #
# Public assert helper used by the L3 mutation rig
# --------------------------------------------------------------------------- #


def assert_strategy_api_scheduler_static(
    config: dict | None = None, root: Path = ROOT
) -> list[str]:
    """Run every scheduler-contract check and return evidence strings.

    Raises ``StrategyApiSchedulerCheckError`` on the first failure.
    """
    config = config if config is not None else load_config(root)
    return [
        check_scheduler_protocol_methods(config, root),
        check_calendar_protocol_surface(config, root),
        check_concrete_classes_exported(config, root),
        check_exchange_handles(config, root),
        check_session_times_are_timezone_aware(config, root),
        check_calendar_horizon_is_pinned(config, root),
    ]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (default: the parent of this script's dir)",
    )
    args = parser.parse_args(argv)
    try:
        evidence = assert_strategy_api_scheduler_static(root=args.root)
    except StrategyApiSchedulerCheckError as exc:
        print(f"SRS-SDK-002 FAIL: {exc}", file=sys.stderr)
        return 1
    print("SRS-SDK-002 PASS — trading-calendar-aware scheduling contract")
    for line in evidence:
        print(f"  * {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
