#!/usr/bin/env python3
"""Contract evidence script for feature API-1.

Introspects the public ``atp_strategy`` package and confirms that the
Python Strategy API exposes every capability bucket required by API-1's
description, tracing each to ``SRS-SDK-001``..``SRS-SDK-009`` in
``docs/SRS.md`` §5.2.

Mirrors the PASS/FAIL output style of ``tools/architecture_check.py``.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import sys
import typing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
README_PATH = ROOT / "python" / "atp_strategy" / "README.md"


class ContractCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ContractCheckError(message)


def _load() -> object:
    if str(PYTHON_ROOT) not in sys.path:
        sys.path.insert(0, str(PYTHON_ROOT))
    return importlib.import_module("atp_strategy")


def _proto_methods(proto: type) -> set[str]:
    return {
        name
        for name in dir(proto)
        if not name.startswith("_") and callable(getattr(proto, name, None))
    }


def _proto_signature_params(proto: type, method: str) -> list[str]:
    func = getattr(proto, method)
    return list(inspect.signature(func).parameters)


def check_sdk_001(api: object) -> str:
    ctx = api.StrategyContext
    if not isinstance(ctx, type) or not issubclass(ctx, typing.Protocol):  # type: ignore[arg-type]
        fail("SDK-001: StrategyContext must be a typing.Protocol")
    forbidden = {"execution_mode", "is_paper", "is_live", "mode"}
    for method in ("subscribe", "order", "cancel", "log", "get_state", "set_state"):
        if not hasattr(ctx, method):
            fail(f"SDK-001: StrategyContext missing required method {method!r}")
        params = _proto_signature_params(ctx, method)
        leak = forbidden.intersection(params)
        if leak:
            fail(
                f"SDK-001: StrategyContext.{method} leaks execution-mode parameter "
                f"{sorted(leak)}; same surface must serve live and paper."
            )
    return "SDK-001: identical context surface across live and paper (no execution-mode leakage)"


def check_sdk_002(api: object) -> str:
    expected_scheduler = {"at_market_open", "at_market_close", "every_n_minutes", "cron"}
    methods = _proto_methods(api.Scheduler)
    missing = expected_scheduler - methods
    if missing:
        fail(f"SDK-002: Scheduler missing methods: {sorted(missing)}")
    if not hasattr(api.ScheduleHandle, "cancel"):
        fail("SDK-002: ScheduleHandle must expose cancel()")
    cal_methods = {"is_session", "session_open", "session_close", "is_early_close"}
    if not cal_methods.issubset(_proto_methods(api.TradingCalendar)):
        fail(f"SDK-002: TradingCalendar missing {sorted(cal_methods - _proto_methods(api.TradingCalendar))}")
    instance = api.StaticTradingCalendar()
    if instance.name != "NYSE":
        fail("SDK-002: StaticTradingCalendar.name must default to 'NYSE'")
    return "SDK-002: schedule.{at_market_open, at_market_close, every_n_minutes, cron} + TradingCalendar"


def check_sdk_003(api: object) -> str:
    if {"EQUITY", "OPTION"} != {m.name for m in api.AssetClass}:
        fail("SDK-003: AssetClass must contain EQUITY and OPTION")
    cfg_fields = {f.name for f in dataclasses.fields(api.StrategyConfig)}
    if "tradable_asset_class" not in cfg_fields:
        fail("SDK-003: StrategyConfig.tradable_asset_class field is required")
    sub = inspect.signature(api.StrategyContext.subscribe)
    if "asset_class" not in sub.parameters:
        fail("SDK-003: StrategyContext.subscribe must accept asset_class")
    if not issubclass(api.AssetClassViolation, api.StrategyAPIError):
        fail("SDK-003: AssetClassViolation must derive from StrategyAPIError")
    return "SDK-003: AssetClass{EQUITY,OPTION}; subscribe(asset_class=...); AssetClassViolation"


def check_sdk_004(api: object) -> str:
    expected = {"FILL", "PARTIAL_FILL", "CANCELLED", "REJECTED"}
    have = {m.name for m in api.OrderEventType}
    missing = expected - have
    if missing:
        fail(f"SDK-004: OrderEventType missing {sorted(missing)}")
    fields = {f.name for f in dataclasses.fields(api.OrderEvent)}
    required = {
        "event_type",
        "order_id",
        "client_order_id",
        "symbol",
        "fill_price",
        "fill_quantity",
        "commission",
    }
    if not required.issubset(fields):
        fail(f"SDK-004: OrderEvent missing fields {sorted(required - fields)}")
    return "SDK-004: OrderEventType{FILL,PARTIAL_FILL,CANCELLED,REJECTED}; OrderEvent fields complete"


def check_sdk_005(api: object) -> str:
    cfg_fields = {f.name for f in dataclasses.fields(api.StrategyConfig)}
    if "warmup_bars" not in cfg_fields:
        fail("SDK-005: StrategyConfig.warmup_bars field is required")
    if not callable(getattr(api.Strategy, "on_warmup_complete", None)):
        fail("SDK-005: Strategy.on_warmup_complete callback is required")
    if not issubclass(api.WarmupNotComplete, api.StrategyAPIError):
        fail("SDK-005: WarmupNotComplete must derive from StrategyAPIError")
    return "SDK-005: StrategyConfig.warmup_bars; Strategy.on_warmup_complete; WarmupNotComplete"


def check_sdk_006(api: object) -> str:
    indicators = importlib.import_module("atp_strategy.indicators")
    expected = ["SMA", "EMA", "RSI", "MACD", "BollingerBands", "ATR"]
    for name in expected:
        cls = getattr(indicators, name, None)
        if cls is None:
            fail(f"SDK-006: indicators.{name} missing")
        for member in ("update", "value", "is_ready"):
            if not hasattr(cls, member):
                fail(f"SDK-006: {name} does not implement Indicator.{member}")
    return "SDK-006: SMA, EMA, RSI, MACD, BollingerBands, ATR implement Indicator"


def check_sdk_007(api: object) -> str:
    if not hasattr(api.BarConsolidator, "consolidate"):
        fail("SDK-007: BarConsolidator must expose consolidate()")
    if not callable(getattr(api.StrategyContext, "consolidate", None)):
        fail("SDK-007: StrategyContext.consolidate is required")
    return "SDK-007: BarConsolidator + StrategyContext.consolidate"


def check_sdk_008(api: object) -> str:
    if not hasattr(api.RenkoBuilder, "update"):
        fail("SDK-008: RenkoBuilder must expose update()")
    if not hasattr(api.RangeBarBuilder, "update"):
        fail("SDK-008: RangeBarBuilder must expose update()")
    return "SDK-008: RenkoBuilder + RangeBarBuilder Protocols (P3)"


def check_sdk_009(api: object) -> str:
    if not README_PATH.exists():
        fail("SDK-009: python/atp_strategy/README.md is required")
    text = README_PATH.read_text(encoding="utf-8")
    if "Example" not in text:
        fail("SDK-009: README.md must contain at least one 'Example' section")
    missing_docs: list[str] = []
    for name in api.__all__:
        obj = getattr(api, name)
        if not (inspect.isclass(obj) or inspect.isfunction(obj)):
            continue
        doc = inspect.getdoc(obj) or ""
        if not doc.strip():
            missing_docs.append(name)
    if missing_docs:
        fail(f"SDK-009: missing docstrings on {missing_docs}")
    return f"SDK-009: README.md present with examples; {len(api.__all__)} public names documented"


def run_checks() -> list[str]:
    api = _load()
    checks = [
        check_sdk_001,
        check_sdk_002,
        check_sdk_003,
        check_sdk_004,
        check_sdk_005,
        check_sdk_006,
        check_sdk_007,
        check_sdk_008,
        check_sdk_009,
    ]
    return [check(api) for check in checks]


def main() -> int:
    try:
        evidence = run_checks()
    except ContractCheckError as error:
        print(f"API-1 FAIL: {error}", file=sys.stderr)
        return 1

    print("API-1 PASS")
    for line in evidence:
        print(f"- {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
