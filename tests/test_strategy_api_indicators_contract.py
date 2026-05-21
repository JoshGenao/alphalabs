"""Contract tests for SRS-SDK-006 (SyRS SYS-35 / AC-6; StRS SN-1.20 / C-9).

Shells out to ``tools/strategy_api_indicators_check.py`` for the
positive-evidence path, then mutates a tmpdir copy of
``python/atp_strategy/`` to verify each invariant in the indicator
contract actually catches a regression:

* Dropping ``import talib`` or ``import pandas_ta`` from ``indicators.py``
  (AC-6 dependency-direction gate).
* Reintroducing custom math tokens prohibited by AC-6 (e.g.
  ``self._alpha``, ``self._avg_gain`` from the prior pure-Python file).
* Renaming the public ``period`` kwarg or dropping a class from the
  package ``__all__`` (backwards-compat gate).
* Demoting ``.value`` from property to method, or dropping ``.update``
  (Indicator Protocol shape).
* Reordering ``MACDValue`` fields so positional unpacking via
  ``.reading`` would break for strategy authors.
* Adding a constant offset to the SMA wrapper output, so the parity
  tolerance check fires.
* Inverting the NaN -> None convention so user code would see NaN floats.
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

from strategy_api_indicators_check import (  # noqa: E402
    StrategyApiIndicatorsCheckError,
    assert_strategy_api_indicators_static,
    load_config,
)


class _MutationRig:
    """Copy ``python/atp_strategy/`` into a tmpdir and run the indicators check."""

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
        return assert_strategy_api_indicators_static(config, root=self.root)


class StrategyApiIndicatorsScriptTest(unittest.TestCase):
    """Positive evidence: the CLI emits the required evidence needles."""

    def test_script_passes_and_emits_evidence_needles(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/strategy_api_indicators_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SDK-006 PASS", result.stdout)
        for needle in (
            "required exports",
            "shape locked",
            "talib and pandas_ta imported at module top level",
            "prohibited custom-math tokens absent",
            "all six classes pass isinstance(api.Indicator)",
            "SMA / EMA / RSI / MACD / BollingerBands / ATR match TA-Lib within tolerance",
            "NaN → None convention enforced",
            "incremental updates match batch TA-Lib at every step",
            "is_ready transitions at documented bar counts",
            "backwards-compat call sites pass",
            "SRS-SDK-006 AC behaviourally locked",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class IndicatorImportsMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_talib_import_is_caught(self) -> None:
        self.rig.mutate(
            "indicators.py",
            find="import talib  # noqa: E402",
            replace="# import talib  # mutation: removed talib import",
        )
        with self.assertRaisesRegex(
            StrategyApiIndicatorsCheckError, r"required module-top-level imports"
        ):
            self.rig.run(self.config)

    def test_dropping_pandas_ta_import_is_caught(self) -> None:
        self.rig.mutate(
            "indicators.py",
            find="import pandas_ta  # noqa: E402",
            replace="# import pandas_ta  # mutation: removed",
        )
        with self.assertRaisesRegex(
            StrategyApiIndicatorsCheckError, r"required module-top-level imports"
        ):
            self.rig.run(self.config)


class IndicatorCustomMathMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_reintroducing_alpha_token_is_caught(self) -> None:
        self.rig.mutate(
            "indicators.py",
            find="class EMA(_IndicatorBase):",
            replace=(
                "_AC6_VIOLATION_PLACEHOLDER = None  # references prohibited token below\n"
                "_SCRATCH_ALPHA = lambda period: None  # noqa: E731\n"
                "# self._alpha = 2.0 / (period + 1.0)\n"
                "class EMA(_IndicatorBase):"
            ),
        )
        with self.assertRaisesRegex(StrategyApiIndicatorsCheckError, r"custom-math tokens"):
            self.rig.run(self.config)

    def test_reintroducing_avg_gain_token_is_caught(self) -> None:
        self.rig.mutate(
            "indicators.py",
            find="class RSI(_IndicatorBase):",
            replace=("# self._avg_gain = 0.0\nclass RSI(_IndicatorBase):"),
        )
        with self.assertRaisesRegex(StrategyApiIndicatorsCheckError, r"custom-math tokens"):
            self.rig.run(self.config)


class IndicatorProtocolMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_update_method_is_caught(self) -> None:
        # Rename SMA.update so the Protocol no longer matches. SMA routes via
        # pandas-ta so the mutation target body references pandas_ta.sma.
        self.rig.mutate(
            "indicators.py",
            find=(
                "    def update(self, bar: Bar) -> float | None:\n"
                "        # SMA is routed through pandas-ta"
            ),
            replace=(
                "    def _update(self, bar: Bar) -> float | None:\n"
                "        # SMA is routed through pandas-ta"
            ),
        )
        with self.assertRaises(StrategyApiIndicatorsCheckError):
            self.rig.run(self.config)

    def test_demoting_value_property_is_caught(self) -> None:
        # Strip the @property decorator on the base value accessor.
        self.rig.mutate(
            "indicators.py",
            find='    @property\n    def value(self) -> float | None:\n        """Latest indicator value, or ``None`` if the warm-up window is incomplete."""\n        return self._value',
            replace='    def value(self) -> float | None:\n        """Latest indicator value, or ``None`` if the warm-up window is incomplete."""\n        return self._value',
        )
        with self.assertRaisesRegex(StrategyApiIndicatorsCheckError, r"\.value is not a property"):
            self.rig.run(self.config)


class IndicatorExportsMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_dropping_sma_from_package_all_is_caught(self) -> None:
        self.rig.mutate(
            "__init__.py",
            find='"SMA",\n',
            replace="",
        )
        with self.assertRaisesRegex(
            StrategyApiIndicatorsCheckError, r"required indicator exports|__all__"
        ):
            self.rig.run(self.config)

    def test_reordering_macd_value_fields_is_caught(self) -> None:
        self.rig.mutate(
            "indicators.py",
            find="    macd: float\n    signal: float\n    histogram: float",
            replace="    signal: float\n    macd: float\n    histogram: float",
        )
        with self.assertRaisesRegex(StrategyApiIndicatorsCheckError, r"MACDValue field order"):
            self.rig.run(self.config)


class IndicatorParityMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_adding_offset_to_sma_breaks_parity_check(self) -> None:
        # Bias the SMA wrapper output by a constant; parity tolerance is 1e-9
        # so any non-zero offset trips the behavioural check. SMA is routed
        # through pandas_ta.sma per SyRS AC-6 (so the library genuinely wraps
        # both backends), so the mutation target is the pandas-ta call.
        self.rig.mutate(
            "indicators.py",
            find=(
                "        out = pandas_ta.sma(ser, length=self.period)\n"
                "        v = _nan_to_none(float(out.iloc[-1]))"
            ),
            replace=(
                "        out = pandas_ta.sma(ser, length=self.period)\n"
                "        v = _nan_to_none(float(out.iloc[-1]) + 1.0)"
            ),
        )
        with self.assertRaisesRegex(StrategyApiIndicatorsCheckError, r"SMA.*wrapper="):
            self.rig.run(self.config)

    def test_dropping_short_buffer_guard_and_nan_to_none_leaks_nan(self) -> None:
        # Drop both defensive layers in EMA (TA-Lib backend): the early-return
        # guard for short buffers AND the _nan_to_none conversion. With both
        # gone, TA-Lib's NaN at short-buffer positions leaks to .value as a
        # NaN float, which the check_nan_to_none_convention step catches via
        # its EMA(period=10) + 5 bars probe.
        self.rig.mutate(
            "indicators.py",
            find=(
                "        self._closes.append(bar.close)\n"
                "        if len(self._closes) < self.period:\n"
                "            return None\n"
                "        arr = np.asarray(self._closes, dtype=_FLOAT64)\n"
                "        out = talib.EMA(arr, timeperiod=self.period)\n"
                "        v = _nan_to_none(float(out[-1]))\n"
                "        if v is None:\n"
                "            return None\n"
                "        self._value = v\n"
                "        self._ready = True\n"
                "        return v"
            ),
            replace=(
                "        self._closes.append(bar.close)\n"
                "        arr = np.asarray(self._closes, dtype=_FLOAT64)\n"
                "        out = talib.EMA(arr, timeperiod=self.period)\n"
                "        v = float(out[-1])\n"
                "        self._value = v\n"
                "        self._ready = True\n"
                "        return v"
            ),
        )
        with self.assertRaisesRegex(StrategyApiIndicatorsCheckError, r"None|NaN|expected None"):
            self.rig.run(self.config)


class IndicatorBackwardsCompatibilityMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _MutationRig()
        self.config = load_config()

    def tearDown(self) -> None:
        self.rig.close()

    def test_renaming_sma_period_kwarg_is_caught(self) -> None:
        # The public SMA(period=...) kwarg is part of the backwards-compat
        # contract. Renaming to `window` would break existing call sites
        # (tests/test_strategy_api.py:62-76, tests/domain/test_warmup_replay.py:108-128).
        # The protocol-conformance check rebuilds SMA(period=3) so the
        # rename surfaces as a contract failure rather than a raw TypeError.
        self.rig.mutate(
            "indicators.py",
            find=(
                "    def __init__(self, period: int) -> None:\n"
                "        super().__init__()\n"
                "        if period < 1:\n"
                '            raise ValueError("period must be >= 1")\n'
                "        self.period = period\n"
                "        self._closes: deque[float] = deque(maxlen=period)"
            ),
            replace=(
                "    def __init__(self, window: int) -> None:\n"
                "        super().__init__()\n"
                "        if window < 1:\n"
                '            raise ValueError("period must be >= 1")\n'
                "        self.period = window\n"
                "        self._closes: deque[float] = deque(maxlen=window)"
            ),
        )
        with self.assertRaisesRegex(StrategyApiIndicatorsCheckError, r"TypeError|period|window"):
            self.rig.run(self.config)


if __name__ == "__main__":
    unittest.main()
