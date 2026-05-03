from __future__ import annotations

import inspect
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_strategy  # noqa: E402
from atp_strategy import (  # noqa: E402
    AssetClass,
    AssetClassViolation,
    Bar,
    OrderHandle,
    OrderRequest,
    OrderSide,
    OrderType,
    SMA,
    Strategy,
    StrategyConfig,
    StrategyContext,
)


class StrategyAPIContractTest(unittest.TestCase):
    def test_api_1_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/strategy_api_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("API-1 PASS", result.stdout)

    def test_strategy_context_protocol_shape(self) -> None:
        for method in (
            "subscribe",
            "order",
            "cancel",
            "log",
            "get_state",
            "set_state",
            "indicator",
            "consolidate",
        ):
            self.assertTrue(
                callable(getattr(StrategyContext, method, None)),
                f"StrategyContext.{method} missing",
            )
        sig = inspect.signature(StrategyContext.subscribe)
        self.assertIn("asset_class", sig.parameters)


class IndicatorIncrementalTest(unittest.TestCase):
    def _bar(self, close: float) -> Bar:
        return Bar("X", "t", close, close, close, close, 1)

    def test_sma_incremental_update(self) -> None:
        sma = SMA(period=3)
        self.assertFalse(sma.is_ready)
        sma.update(self._bar(1.0))
        sma.update(self._bar(2.0))
        self.assertFalse(sma.is_ready)
        sma.update(self._bar(3.0))
        self.assertTrue(sma.is_ready)
        self.assertAlmostEqual(sma.value, 2.0)
        sma.update(self._bar(6.0))
        self.assertAlmostEqual(sma.value, (2.0 + 3.0 + 6.0) / 3)


class _FakeContext:
    """Minimal StrategyContext implementation for asset-class enforcement test."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.submitted: list[OrderRequest] = []

    def order(self, request: OrderRequest) -> OrderHandle:
        if request.asset_class != self.config.tradable_asset_class:
            raise AssetClassViolation(
                f"strategy {self.config.strategy_id} cannot trade {request.asset_class}"
            )
        self.submitted.append(request)
        return OrderHandle(order_id="o-1", strategy_id=self.config.strategy_id)


class AssetClassEnforcementTest(unittest.TestCase):
    def test_off_class_order_is_rejected(self) -> None:
        ctx = _FakeContext(
            StrategyConfig(strategy_id="s1", tradable_asset_class=AssetClass.EQUITY)
        )
        ok = OrderRequest("AAPL", 1, OrderSide.BUY, OrderType.MARKET, AssetClass.EQUITY)
        bad = OrderRequest("AAPL", 1, OrderSide.BUY, OrderType.MARKET, AssetClass.OPTION)
        ctx.order(ok)
        with self.assertRaises(AssetClassViolation):
            ctx.order(bad)
        self.assertEqual(len(ctx.submitted), 1)


class PublicDocstringsTest(unittest.TestCase):
    def test_every_public_class_or_function_has_docstring(self) -> None:
        missing: list[str] = []
        for name in atp_strategy.__all__:
            obj = getattr(atp_strategy, name)
            if not (inspect.isclass(obj) or inspect.isfunction(obj)):
                continue
            if not (inspect.getdoc(obj) or "").strip():
                missing.append(name)
        self.assertEqual(missing, [], f"missing docstrings: {missing}")


class StrategyBaseClassTest(unittest.TestCase):
    def test_warmup_bars_class_attribute(self) -> None:
        class Sub(Strategy):
            warmup_bars = 42

        self.assertEqual(Sub().warmup_bars, 42)

    def test_default_callbacks_are_no_ops(self) -> None:
        s = Strategy()
        s.on_start(None)  # type: ignore[arg-type]
        s.on_warmup_complete(None)  # type: ignore[arg-type]
        s.on_schedule(None, "tag")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
