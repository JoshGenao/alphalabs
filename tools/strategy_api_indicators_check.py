#!/usr/bin/env python3
"""Contract evidence script for SRS-SDK-006 (built-in technical indicators).

Verifies that the Python Strategy SDK exposes the six AC-listed
indicators (``SMA``, ``EMA``, ``RSI``, ``MACD``, ``BollingerBands``,
``ATR``) plus the two compound dataclasses (``MACDValue``,
``BollingerValue``) as thin glue wrappers around TA-Lib (canonical
numerical backend) and pandas-ta (documented parity reference) per
SyRS SYS-35 / AC-6 and StRS SN-1.20 / C-9:

* The ``atp_strategy`` package re-exports the six indicators, two
  dataclasses, and the runtime-checkable :class:`Indicator` Protocol.
* Each indicator class implements the Protocol (``value`` / ``is_ready``
  properties; ``update`` method). ``MACD`` / ``BollingerBands`` expose
  the compound reading via a ``reading`` property and the documented
  three-field dataclass.
* ``python/atp_strategy/indicators.py`` imports both ``talib`` and
  ``pandas_ta`` at module top level. AC-6 prohibits custom
  reimplementations: a list of verbatim arithmetic tokens from the
  prior pure-Python implementation MUST NOT appear in the file.
* Behavioural parity on a 300-bar seeded synthetic OHLCV walk
  against TA-Lib (canonical) within the per-indicator
  ``parity_tolerance_abs_vs_talib`` tolerance and against pandas-ta
  (documented seed-bar skip of ``3 * period``) within
  ``parity_tolerance_abs_vs_pandas_ta``.
* NaN -> None: indicators in their warm-up window expose ``.value
  is None``, not a NaN float.
* Incremental updates match batch TA-Lib at every readiness step
  ("support incremental updates on each new bar" half of the AC).
* ``is_ready`` flips at the documented bar count per indicator.
* Backwards compatibility: the existing call sites in
  ``tests/test_strategy_api.py:62-76``,
  ``tests/domain/test_warmup_replay.py:108-128``, and
  ``tests/domain/test_strategy_api_parity.py`` keep passing.

The L7 domain test in ``tests/domain/test_indicators_parity.py`` is
the matching ``safety:paired`` diff that walks the same AC.

Mirrors the PASS/FAIL output style of
``tools/strategy_api_warmup_check.py``.

Invoke:
    python3 tools/strategy_api_indicators_check.py
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StrategyApiIndicatorsCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise StrategyApiIndicatorsCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "strategy_api_indicators_contract" not in config:
        fail("architecture metadata is missing strategy_api_indicators_contract")
    return config["strategy_api_indicators_contract"]


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
    except Exception as exc:
        fail(f"failed to import atp_strategy from {python_root}: {exc!r}")


def _indicators_source_path(root: Path) -> Path:
    path = root / "python" / "atp_strategy" / "indicators.py"
    if not path.is_file():
        fail(f"indicators module missing: {path}")
    return path


def _make_bar(
    api: object, *, close: float, high: float | None = None, low: float | None = None
) -> object:
    h = close if high is None else high
    l = close if low is None else low  # noqa: E741
    return api.Bar("X", "t", close, h, l, close, 1)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_indicator_exports(config: dict, root: Path) -> str:
    block = contract_block(config)
    required = list(block["required_exports"])
    api = _load_sdk_module(root)
    missing = [name for name in required if not hasattr(api, name)]
    if missing:
        fail(
            f"atp_strategy is missing required indicator exports {missing!r} — "
            "strategy authors and the L7 parity test rely on the package-level "
            "surface; AC-6 mandates the wrapper surface as the strategy boundary"
        )
    pkg_all = set(getattr(api, "__all__", ()))
    pkg_missing = [name for name in required if name not in pkg_all]
    if pkg_missing:
        fail(
            f"atp_strategy.__all__ is missing {pkg_missing!r} — exports must be "
            "declared so `from atp_strategy import *` works for strategy authors"
        )
    return f"required exports {sorted(required)} present in atp_strategy.__all__"


def check_indicator_class_shapes(config: dict, root: Path) -> str:
    block = contract_block(config)
    required_classes = list(block["required_indicator_classes"])
    required_methods = list(block["required_indicator_methods"])
    required_props = list(block["required_indicator_properties"])
    compound_props = block["required_compound_indicator_properties"]
    dataclass_fields = block["required_dataclass_fields"]
    api = _load_sdk_module(root)
    for cls_name in required_classes:
        cls = getattr(api, cls_name, None)
        if cls is None:
            fail(f"indicator class {cls_name!r} missing from atp_strategy")
        for method in required_methods:
            attr = getattr(cls, method, None)
            if attr is None or not callable(attr):
                fail(
                    f"{cls_name} is missing required method {method!r} — "
                    f"the Indicator Protocol requires .{method}(bar)"
                )
        for prop in required_props:
            attr = getattr(cls, prop, None)
            if attr is None or not isinstance(attr, property):
                fail(
                    f"{cls_name}.{prop} is not a property — the Indicator "
                    "Protocol requires .value and .is_ready as properties so "
                    "user strategy code can read them without parenthesisation"
                )
    for cls_name, props in compound_props.items():
        cls = getattr(api, cls_name)
        for prop in props:
            attr = getattr(cls, prop, None)
            if attr is None or not isinstance(attr, property):
                fail(
                    f"{cls_name}.{prop} is not a property — compound indicators "
                    f"expose the three-leg reading via .{prop}"
                )
    for dc_name, fields in dataclass_fields.items():
        dc = getattr(api, dc_name, None)
        if dc is None:
            fail(f"dataclass {dc_name!r} missing from atp_strategy")
        actual_fields = list(getattr(dc, "__dataclass_fields__", {}).keys())
        if actual_fields != fields:
            fail(
                f"{dc_name} field order is {actual_fields!r}; expected exactly "
                f"{fields!r} — strategy code unpacks by position via .reading"
            )
    return (
        f"shape locked: {sorted(required_classes)} expose "
        f"{sorted(required_methods + required_props)} as Protocol surface; "
        f"compound classes expose {compound_props}; dataclass fields match"
    )


def check_required_imports(config: dict, root: Path) -> str:
    block = contract_block(config)
    required = list(block["required_imports_in_indicators_module"])
    source = _indicators_source_path(root).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    missing = [name for name in required if name not in imported]
    if missing:
        fail(
            f"indicators.py is missing required module-top-level imports "
            f"{missing!r} — SyRS AC-6 mandates wrapping pandas-ta AND TA-Lib, "
            "so both imports must appear in the source"
        )
    return "talib and pandas_ta imported at module top level — AC-6 deps satisfied"


def check_prohibited_custom_math(config: dict, root: Path) -> str:
    block = contract_block(config)
    prohibited = list(block["prohibited_custom_math_tokens"])
    source = _indicators_source_path(root).read_text(encoding="utf-8")
    found = [token for token in prohibited if token in source]
    if found:
        fail(
            f"indicators.py contains custom-math tokens {found!r} — SyRS AC-6 "
            "prohibits custom reimplementations of indicators available in "
            "pandas-ta and TA-Lib; the wrappers must contain buffering and "
            "type-conversion glue ONLY, with NO arithmetic"
        )
    return (
        f"prohibited custom-math tokens absent ({len(prohibited)} checked) — "
        "AC-6 no-custom-math gate clear"
    )


def check_indicator_protocol_conformance(config: dict, root: Path) -> str:
    api = _load_sdk_module(root)
    cases = (
        ("SMA", {"period": 3}),
        ("EMA", {"period": 3}),
        ("RSI", {"period": 2}),
        ("MACD", {}),
        ("BollingerBands", {"period": 3, "num_std": 2.0}),
        ("ATR", {"period": 2}),
    )
    for name, kwargs in cases:
        cls = getattr(api, name)
        try:
            inst = cls(**kwargs)
        except TypeError as exc:
            fail(
                f"{name}(**{kwargs!r}) raised TypeError — the documented "
                f"constructor kwargs are the public contract for strategy "
                f"authors and existing test sites: {exc}"
            )
        if not isinstance(inst, api.Indicator):
            fail(
                f"{name}{kwargs} does not satisfy the Indicator Protocol — "
                "missing .value / .is_ready / .update at runtime"
            )
    return "all six classes pass isinstance(api.Indicator) runtime-checkable Protocol"


def _seeded_closes(n: int = 300) -> list[float]:
    """Deterministic seeded close-price walk for parity testing.

    Lazy-imports numpy so an L3 mutation that drops numpy from indicators.py
    surfaces in the import check rather than here.
    """
    import numpy as np

    rng = np.random.default_rng(42)
    steps = rng.normal(loc=0.0, scale=0.5, size=n)
    closes = 100.0 + np.cumsum(steps)
    return [float(c) for c in closes]


def _seeded_ohlcv(n: int = 300) -> list[tuple[float, float, float, float]]:
    """Deterministic seeded OHLC walk: returns ``(open, high, low, close)`` per bar."""
    import numpy as np

    closes = _seeded_closes(n)
    rng = np.random.default_rng(7)
    noise = np.abs(rng.normal(loc=0.0, scale=0.3, size=n))
    bars: list[tuple[float, float, float, float]] = []
    prev_close = closes[0]
    for i, close in enumerate(closes):
        open_ = prev_close
        high = close + float(noise[i])
        low = close - float(noise[i])
        bars.append((open_, high, low, close))
        prev_close = close
    return bars


def check_indicator_behavioural_parity(config: dict, root: Path) -> str:
    block = contract_block(config)
    tol_talib = block["parity_tolerance_abs_vs_talib"]
    tol_pta = block["parity_tolerance_abs_vs_pandas_ta"]
    skip_factor = int(block["pandas_ta_seed_skip_bars_factor"])
    api = _load_sdk_module(root)
    import numpy as np
    import pandas as pd
    import pandas_ta as pta
    import talib

    ohlcv = _seeded_ohlcv(300)
    closes = np.asarray([b[3] for b in ohlcv], dtype=np.float64)
    highs = np.asarray([b[1] for b in ohlcv], dtype=np.float64)
    lows = np.asarray([b[2] for b in ohlcv], dtype=np.float64)

    def _drip(ind: object) -> float:
        for o, h, l, c in ohlcv:  # noqa: E741
            bar = api.Bar("X", "t", o, h, l, c, 1)
            ind.update(bar)
        v = ind.value
        if v is None:
            fail(f"{type(ind).__name__} is not ready after 300 bars — buffer or seeding bug")
        return v  # type: ignore[return-value]

    # --- SMA --------------------------------------------------------------- #
    sma_period = 20
    sma = api.SMA(period=sma_period)
    sma_val = _drip(sma)
    sma_talib = float(talib.SMA(closes, timeperiod=sma_period)[-1])
    if abs(sma_val - sma_talib) > tol_talib["SMA"]:
        fail(
            f"SMA({sma_period}) wrapper={sma_val} talib={sma_talib} diff={abs(sma_val - sma_talib)}"
        )
    sma_pta = float(pta.sma(pd.Series(closes), length=sma_period).iloc[-1])
    if abs(sma_val - sma_pta) > tol_pta["SMA"]:
        fail(
            f"SMA({sma_period}) wrapper={sma_val} pandas_ta={sma_pta} diff={abs(sma_val - sma_pta)}"
        )

    # --- EMA --------------------------------------------------------------- #
    ema_period = 20
    ema = api.EMA(period=ema_period)
    ema_val = _drip(ema)
    ema_talib = float(talib.EMA(closes, timeperiod=ema_period)[-1])
    if abs(ema_val - ema_talib) > tol_talib["EMA"]:
        fail(
            f"EMA({ema_period}) wrapper={ema_val} talib={ema_talib} diff={abs(ema_val - ema_talib)}"
        )
    ema_pta = float(pta.ema(pd.Series(closes), length=ema_period).iloc[-1])
    if abs(ema_val - ema_pta) > tol_pta["EMA"]:
        fail(
            f"EMA({ema_period}) wrapper={ema_val} pandas_ta={ema_pta} diff={abs(ema_val - ema_pta)}"
        )

    # --- RSI --------------------------------------------------------------- #
    rsi_period = 14
    rsi = api.RSI(period=rsi_period)
    rsi_val = _drip(rsi)
    rsi_talib = float(talib.RSI(closes, timeperiod=rsi_period)[-1])
    if abs(rsi_val - rsi_talib) > tol_talib["RSI"]:
        fail(
            f"RSI({rsi_period}) wrapper={rsi_val} talib={rsi_talib} diff={abs(rsi_val - rsi_talib)}"
        )
    skip = skip_factor * rsi_period
    rsi_pta = float(pta.rsi(pd.Series(closes[skip:]), length=rsi_period).iloc[-1])
    if abs(rsi_val - rsi_pta) > tol_pta["RSI"]:
        fail(
            f"RSI({rsi_period}) wrapper={rsi_val} pandas_ta (skip={skip})={rsi_pta} diff={abs(rsi_val - rsi_pta)}"
        )

    # --- MACD -------------------------------------------------------------- #
    macd_fast, macd_slow, macd_signal = 12, 26, 9
    macd = api.MACD()
    for o, h, l, c in ohlcv:  # noqa: E741
        macd.update(api.Bar("X", "t", o, h, l, c, 1))
    macd_reading = macd.reading
    if macd_reading is None:
        fail("MACD wrapper not ready after 300 bars — buffer or seeding bug")
    macd_talib, signal_talib, hist_talib = talib.MACD(
        closes, fastperiod=macd_fast, slowperiod=macd_slow, signalperiod=macd_signal
    )
    for leg, wrapper_v, talib_v in (
        ("macd", macd_reading.macd, float(macd_talib[-1])),
        ("signal", macd_reading.signal, float(signal_talib[-1])),
        ("histogram", macd_reading.histogram, float(hist_talib[-1])),
    ):
        if abs(wrapper_v - talib_v) > tol_talib["MACD"]:
            fail(f"MACD.{leg} wrapper={wrapper_v} talib={talib_v} diff={abs(wrapper_v - talib_v)}")
    # pandas-ta MACD parity (with seed-bar skip). pandas-ta returns a DataFrame
    # with columns MACD_{f}_{s}_{sig}, MACDh_{f}_{s}_{sig}, MACDs_{f}_{s}_{sig}.
    macd_skip = skip_factor * macd_slow
    macd_pta_df = pta.macd(
        pd.Series(closes[macd_skip:]),
        fast=macd_fast,
        slow=macd_slow,
        signal=macd_signal,
    )
    macd_pta_cols = list(macd_pta_df.columns)
    macd_col = next(c for c in macd_pta_cols if c.startswith("MACD_"))
    signal_col = next(c for c in macd_pta_cols if c.startswith("MACDs_"))
    hist_col = next(c for c in macd_pta_cols if c.startswith("MACDh_"))
    for leg, wrapper_v, pta_v in (
        ("macd", macd_reading.macd, float(macd_pta_df[macd_col].iloc[-1])),
        ("signal", macd_reading.signal, float(macd_pta_df[signal_col].iloc[-1])),
        ("histogram", macd_reading.histogram, float(macd_pta_df[hist_col].iloc[-1])),
    ):
        if abs(wrapper_v - pta_v) > tol_pta["MACD"]:
            fail(
                f"MACD.{leg} wrapper={wrapper_v} pandas_ta (skip={macd_skip})={pta_v} "
                f"diff={abs(wrapper_v - pta_v)}"
            )

    # --- BollingerBands ---------------------------------------------------- #
    bb_period = 20
    bb_num_std = 2.0
    bb = api.BollingerBands(period=bb_period, num_std=bb_num_std)
    for o, h, l, c in ohlcv:  # noqa: E741
        bb.update(api.Bar("X", "t", o, h, l, c, 1))
    bb_reading = bb.reading
    if bb_reading is None:
        fail("BollingerBands wrapper not ready after 300 bars — buffer or seeding bug")
    upper_talib, middle_talib, lower_talib = talib.BBANDS(
        closes, timeperiod=bb_period, nbdevup=bb_num_std, nbdevdn=bb_num_std, matype=0
    )
    for leg, wrapper_v, talib_v in (
        ("middle", bb_reading.middle, float(middle_talib[-1])),
        ("upper", bb_reading.upper, float(upper_talib[-1])),
        ("lower", bb_reading.lower, float(lower_talib[-1])),
    ):
        if abs(wrapper_v - talib_v) > tol_talib["BollingerBands"]:
            fail(
                f"BollingerBands.{leg} wrapper={wrapper_v} talib={talib_v} diff={abs(wrapper_v - talib_v)}"
            )
    # pandas-ta parity on all three BB legs. The wrapper itself routes through
    # pandas-ta so this is trivially exact, but we still pin the cross-check so
    # a future backend swap is caught.
    bb_pta_df = pta.bbands(pd.Series(closes), length=bb_period, std=bb_num_std)
    bb_pta_cols = list(bb_pta_df.columns)
    bb_upper_col = next(c for c in bb_pta_cols if c.startswith("BBU_"))
    bb_middle_col = next(c for c in bb_pta_cols if c.startswith("BBM_"))
    bb_lower_col = next(c for c in bb_pta_cols if c.startswith("BBL_"))
    for leg, wrapper_v, pta_v in (
        ("middle", bb_reading.middle, float(bb_pta_df[bb_middle_col].iloc[-1])),
        ("upper", bb_reading.upper, float(bb_pta_df[bb_upper_col].iloc[-1])),
        ("lower", bb_reading.lower, float(bb_pta_df[bb_lower_col].iloc[-1])),
    ):
        if abs(wrapper_v - pta_v) > tol_pta["BollingerBands"]:
            fail(
                f"BollingerBands.{leg} wrapper={wrapper_v} pandas_ta={pta_v} "
                f"diff={abs(wrapper_v - pta_v)}"
            )

    # --- ATR --------------------------------------------------------------- #
    atr_period = 14
    atr = api.ATR(period=atr_period)
    for o, h, l, c in ohlcv:  # noqa: E741
        atr.update(api.Bar("X", "t", o, h, l, c, 1))
    atr_val = atr.value
    if atr_val is None:
        fail("ATR wrapper not ready after 300 bars — buffer or seeding bug")
    atr_talib = float(talib.ATR(highs, lows, closes, timeperiod=atr_period)[-1])
    if abs(atr_val - atr_talib) > tol_talib["ATR"]:
        fail(
            f"ATR({atr_period}) wrapper={atr_val} talib={atr_talib} diff={abs(atr_val - atr_talib)}"
        )
    # pandas-ta ATR parity with seed-bar skip (Wilder smoothing seed differs).
    atr_skip = skip_factor * atr_period
    atr_pta = float(
        pta.atr(
            pd.Series(highs[atr_skip:]),
            pd.Series(lows[atr_skip:]),
            pd.Series(closes[atr_skip:]),
            length=atr_period,
        ).iloc[-1]
    )
    if abs(atr_val - atr_pta) > tol_pta["ATR"]:
        fail(
            f"ATR({atr_period}) wrapper={atr_val} pandas_ta (skip={atr_skip})={atr_pta} "
            f"diff={abs(atr_val - atr_pta)}"
        )

    return (
        "SMA / EMA / RSI / MACD / BollingerBands / ATR match TA-Lib within tolerance "
        f"on the 300-bar seeded walk (tolerances {sorted(tol_talib.items())}); "
        "all six indicators also pin pandas-ta parity (with seed skip = "
        f"{skip_factor} * period on Wilder/MACD seeding indicators)"
    )


def check_nan_to_none_convention(config: dict, root: Path) -> str:
    api = _load_sdk_module(root)
    # SMA (pandas-ta backend) — short buffer must expose None.
    sma = api.SMA(period=10)
    for c in (1.0, 2.0, 3.0, 4.0, 5.0):
        sma.update(api.Bar("X", "t", c, c, c, c, 1))
    if sma.value is not None:
        fail(
            f"SMA(period=10) with 5 bars has .value = {sma.value!r}; expected None "
            "— the wrapper MUST surface None during warm-up so user code does "
            "not see NaN floats"
        )
    if sma.is_ready:
        fail("SMA(period=10) with 5 bars reports is_ready=True; expected False")
    # EMA (TA-Lib backend) — same invariant; talib emits NaN for warm-up
    # positions and the wrapper MUST convert to Python None.
    ema = api.EMA(period=10)
    for c in (1.0, 2.0, 3.0, 4.0, 5.0):
        ema.update(api.Bar("X", "t", c, c, c, c, 1))
    if ema.value is not None:
        fail(
            f"EMA(period=10) with 5 bars has .value = {ema.value!r}; expected None "
            "— TA-Lib emits NaN for warm-up positions and the wrapper MUST "
            "convert to Python None so user code does not see NaN floats"
        )
    if ema.is_ready:
        fail("EMA(period=10) with 5 bars reports is_ready=True; expected False")
    # Compound indicator: any leg NaN -> whole reading is None.
    macd = api.MACD()
    for c in (100.0, 101.0, 102.0, 103.0):  # nowhere near slow+signal-1
        macd.update(api.Bar("X", "t", c, c, c, c, 1))
    if macd.reading is not None:
        fail(
            f"MACD with 4 bars has .reading = {macd.reading!r}; expected None — "
            "the compound reading must be None whenever any leg is NaN"
        )
    return (
        "NaN → None convention enforced across both backends: short-buffer "
        "SMA/EMA .value is None, MACD .reading is None when any leg is NaN"
    )


def check_incremental_vs_batch_parity(config: dict, root: Path) -> str:
    """All six indicators: incremental updates match batch at every readiness step.

    Drip-feeds a seeded sequence one bar at a time and compares the wrapper's
    .value (and .reading components for compound indicators) to the equivalent
    batch call on ``arr[:i+1]``. Each indicator type uses its own
    stabilisation window (3 * period for Wilder-smoothed indicators;
    3 * slow for MACD) because the wrapper buffers cap at a multiple of the
    period (see indicators.py docstring on the buffer cap derivation).
    """
    block = contract_block(config)
    tol_talib = block["parity_tolerance_abs_vs_talib"]
    api = _load_sdk_module(root)
    import numpy as np
    import talib

    closes = _seeded_closes(140)
    ohlcv = _seeded_ohlcv(140)
    highs = np.asarray([b[1] for b in ohlcv], dtype=np.float64)
    lows = np.asarray([b[2] for b in ohlcv], dtype=np.float64)
    closes_ohlc = np.asarray([b[3] for b in ohlcv], dtype=np.float64)

    drifts: dict[str, float] = {}

    # --- SMA: exact per-bar match (no stabilisation window) ---
    sma_period = 5
    sma = api.SMA(period=sma_period)
    sma_max_drift = 0.0
    for i, c in enumerate(closes):
        wrapper_v = sma.update(api.Bar("X", "t", c, c, c, c, 1))
        arr = np.asarray(closes[: i + 1], dtype=np.float64)
        batch_raw = float(talib.SMA(arr, timeperiod=sma_period)[-1])
        batch_v = batch_raw if not math.isnan(batch_raw) else None
        if wrapper_v is None and batch_v is None:
            continue
        if wrapper_v is None or batch_v is None:
            fail(
                f"SMA incremental readiness mismatch at bar {i}: wrapper={wrapper_v} batch={batch_v}"
            )
        diff = abs(wrapper_v - batch_v)
        sma_max_drift = max(sma_max_drift, diff)
        if diff > tol_talib["SMA"]:
            fail(
                f"SMA incremental at bar {i} diverges: wrapper={wrapper_v} batch={batch_v} diff={diff}"
            )
    drifts["SMA"] = sma_max_drift

    # --- EMA / RSI / ATR: stabilise after 3 * period bars ---
    for name, period in (("EMA", 10), ("RSI", 14), ("ATR", 14)):
        ind = getattr(api, name)(period=period)
        max_drift = 0.0
        for i, bar in enumerate(ohlcv):
            o, h, l, c = bar  # noqa: E741
            ind.update(api.Bar("X", "t", o, h, l, c, 1))
            if i + 1 < 3 * period:
                continue
            if name == "ATR":
                batch_v = float(
                    talib.ATR(
                        highs[: i + 1], lows[: i + 1], closes_ohlc[: i + 1], timeperiod=period
                    )[-1]
                )
            else:
                fn = getattr(talib, name)
                batch_v = float(fn(closes_ohlc[: i + 1], timeperiod=period)[-1])
            if ind.value is None:
                fail(f"{name}({period}) wrapper not ready at bar {i} (batch finite)")
            diff = abs(float(ind.value) - batch_v)
            max_drift = max(max_drift, diff)
            if diff > tol_talib[name]:
                fail(
                    f"{name}({period}) incremental at bar {i} diverges: wrapper={ind.value} batch={batch_v} diff={diff}"
                )
        drifts[name] = max_drift

    # --- BollingerBands: exact per-bar match (SMA + population stdev) ---
    bb_period = 10
    bb = api.BollingerBands(period=bb_period, num_std=2.0)
    bb_max_drift = 0.0
    for i, c in enumerate(closes):
        bb.update(api.Bar("X", "t", c, c, c, c, 1))
        if i + 1 < bb_period:
            continue
        arr = np.asarray(closes[: i + 1], dtype=np.float64)
        u_t, m_t, l_t = talib.BBANDS(arr, timeperiod=bb_period, nbdevup=2.0, nbdevdn=2.0, matype=0)
        reading = bb.reading
        if reading is None:
            fail(f"BollingerBands wrapper not ready at bar {i}")
        for leg, wrapper_leg, batch_leg in (
            ("middle", reading.middle, float(m_t[-1])),
            ("upper", reading.upper, float(u_t[-1])),
            ("lower", reading.lower, float(l_t[-1])),
        ):
            diff = abs(wrapper_leg - batch_leg)
            bb_max_drift = max(bb_max_drift, diff)
            if diff > tol_talib["BollingerBands"]:
                fail(
                    f"BollingerBands.{leg} incremental at bar {i} diverges: wrapper={wrapper_leg} batch={batch_leg} diff={diff}"
                )
    drifts["BollingerBands"] = bb_max_drift

    # --- MACD: stabilises after 3 * slow bars ---
    macd = api.MACD()
    macd_max_drift = 0.0
    for i, c in enumerate(closes):
        macd.update(api.Bar("X", "t", c, c, c, c, 1))
        if i + 1 < 3 * 26:
            continue
        arr = np.asarray(closes[: i + 1], dtype=np.float64)
        m_t, s_t, h_t = talib.MACD(arr, fastperiod=12, slowperiod=26, signalperiod=9)
        reading = macd.reading
        if reading is None:
            fail(f"MACD wrapper not ready at bar {i}")
        for leg, wrapper_leg, batch_leg in (
            ("macd", reading.macd, float(m_t[-1])),
            ("signal", reading.signal, float(s_t[-1])),
            ("histogram", reading.histogram, float(h_t[-1])),
        ):
            diff = abs(wrapper_leg - batch_leg)
            macd_max_drift = max(macd_max_drift, diff)
            if diff > tol_talib["MACD"]:
                fail(
                    f"MACD.{leg} incremental at bar {i} diverges: wrapper={wrapper_leg} batch={batch_leg} diff={diff}"
                )
    drifts["MACD"] = macd_max_drift

    drift_summary = ", ".join(f"{k}={v:.2e}" for k, v in sorted(drifts.items()))
    return (
        "incremental updates match batch TA-Lib at every step for all six indicators "
        f"(max drifts {drift_summary})"
    )


def check_is_ready_transitions(config: dict, root: Path) -> str:
    api = _load_sdk_module(root)
    # SMA(period=20) flips at bar 20 (not 19, not 21).
    sma = api.SMA(period=20)
    closes = _seeded_closes(25)
    for i, c in enumerate(closes, start=1):
        sma.update(api.Bar("X", "t", c, c, c, c, 1))
        if i < 20 and sma.is_ready:
            fail(f"SMA(20).is_ready flipped True at bar {i}; expected first-True at bar 20")
        if i == 20 and not sma.is_ready:
            fail("SMA(20).is_ready False at bar 20; expected first-True")
    # RSI(period=14) flips at bar 15.
    rsi = api.RSI(period=14)
    closes = _seeded_closes(20)
    for i, c in enumerate(closes, start=1):
        rsi.update(api.Bar("X", "t", c, c, c, c, 1))
        if i < 15 and rsi.is_ready:
            fail(f"RSI(14).is_ready flipped True at bar {i}; expected first-True at bar 15")
        if i == 15 and not rsi.is_ready:
            fail("RSI(14).is_ready False at bar 15; expected first-True")
    # MACD default (12, 26, 9) flips at bar slow+signal-1 = 34.
    macd = api.MACD()
    closes = _seeded_closes(40)
    for i, c in enumerate(closes, start=1):
        macd.update(api.Bar("X", "t", c, c, c, c, 1))
        if i < 34 and macd.is_ready:
            fail(f"MACD(12,26,9).is_ready flipped True at bar {i}; expected first-True at bar 34")
    if not macd.is_ready:
        fail("MACD(12,26,9).is_ready False at bar 40; expected ready")
    # ATR(period=14) flips at bar 15.
    atr = api.ATR(period=14)
    ohlcv = _seeded_ohlcv(20)
    for i, (o, h, l, c) in enumerate(ohlcv, start=1):  # noqa: E741
        atr.update(api.Bar("X", "t", o, h, l, c, 1))
        if i < 15 and atr.is_ready:
            fail(f"ATR(14).is_ready flipped True at bar {i}; expected first-True at bar 15")
        if i == 15 and not atr.is_ready:
            fail("ATR(14).is_ready False at bar 15; expected first-True")
    return "is_ready transitions at documented bar counts: SMA@20, RSI@15, MACD@34, ATR@15"


def check_backwards_compatibility(config: dict, root: Path) -> str:
    """The existing call sites must keep passing through the rewrite."""
    api = _load_sdk_module(root)
    sma = api.SMA(period=3)
    for c in (1.0, 2.0, 3.0):
        sma.update(api.Bar("X", "t", c, c, c, c, 1))
    if sma.value is None or abs(sma.value - 2.0) > 1e-9:
        fail(
            f"SMA(period=3) on (1,2,3) returns {sma.value!r}; expected 2.0 — "
            "tests/test_strategy_api.py:62-76 / tests/domain/test_strategy_api_parity.py:212 "
            "call sites would regress on a public-API change"
        )
    if not sma.is_ready:
        fail("SMA(period=3).is_ready False after 3 bars; expected True")
    # SMA(period=200) on 200 bars must be is_ready (warmup test guarantee).
    sma200 = api.SMA(period=200)
    closes = _seeded_closes(200)
    for c in closes:
        sma200.update(api.Bar("X", "t", c, c, c, c, 1))
    if not sma200.is_ready or sma200.value is None:
        fail(
            "SMA(period=200) after 200 bars is not ready — "
            "tests/domain/test_warmup_replay.py:108-128 would regress"
        )
    return "backwards-compat call sites pass: SMA(3) on (1,2,3) == 2.0; SMA(200)@200 ready"


# --------------------------------------------------------------------------- #
# Public assert helper used by the L3 mutation rig
# --------------------------------------------------------------------------- #


def assert_strategy_api_indicators_static(
    config: dict | None = None, root: Path = ROOT
) -> list[str]:
    """Run every indicator contract check and return evidence strings.

    Raises ``StrategyApiIndicatorsCheckError`` on the first failure.
    """
    config = config if config is not None else load_config(root)
    return [
        check_indicator_exports(config, root),
        check_indicator_class_shapes(config, root),
        check_required_imports(config, root),
        check_prohibited_custom_math(config, root),
        check_indicator_protocol_conformance(config, root),
        check_indicator_behavioural_parity(config, root),
        check_nan_to_none_convention(config, root),
        check_incremental_vs_batch_parity(config, root),
        check_is_ready_transitions(config, root),
        check_backwards_compatibility(config, root),
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
        evidence = assert_strategy_api_indicators_static(root=args.root)
    except StrategyApiIndicatorsCheckError as exc:
        print(f"SRS-SDK-006 FAIL: {exc}", file=sys.stderr)
        return 1
    print("SRS-SDK-006 PASS — Python Strategy API technical indicators")
    for line in evidence:
        print(f"  * {line}")
    print("  * SRS-SDK-006 AC behaviourally locked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
