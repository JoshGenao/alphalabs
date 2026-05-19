"""L1 unit tests for ``assert_asset_class`` (SRS-SDK-003).

Locks the pure-function contract of the shipped guard helper:
re-export from the package root, two-positional signature
``(config, request)``, silent on matched ``EQUITY/EQUITY`` and
``OPTION/OPTION``, raises ``AssetClassViolation`` (a subclass of
``StrategyAPIError``) on the two mismatched permutations, and message
content that names the offending strategy + class so the structured-
error contract (SyRS SYS-64) reaches user strategy code.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_strategy as _pkg  # noqa: E402
import pytest  # noqa: E402
from atp_strategy import (  # noqa: E402
    AssetClass,
    AssetClassViolation,
    OrderRequest,
    OrderSide,
    OrderType,
    StrategyAPIError,
    StrategyConfig,
    assert_asset_class,
)
from atp_strategy import api as _api_module  # noqa: E402

pytestmark = pytest.mark.unit


def _request(asset_class: AssetClass) -> OrderRequest:
    return OrderRequest(
        symbol="AAPL" if asset_class is AssetClass.EQUITY else "SPY",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        asset_class=asset_class,
    )


class AssertAssetClassExportTest(unittest.TestCase):
    def test_imports_assert_asset_class_from_package_root(self) -> None:
        # Module-level imports (above) are bound at collection time
        # against the real package — verify they resolve and that the
        # symbol participates in the package's documented public surface.
        # We deliberately don't `import atp_strategy` inside the test body
        # because the L3 contract-test mutation rig leaves
        # ``sys.modules['atp_strategy']`` pointing at a (now-deleted)
        # tmpdir copy, which would shadow the real package on subsequent
        # fresh imports within the same pytest session.
        self.assertIs(_pkg.assert_asset_class, assert_asset_class)
        self.assertIn("assert_asset_class", _pkg.__all__)
        # The api module re-exports the same function object — locks the
        # `atp_strategy.api.assert_asset_class` import path that the
        # subscriptions contract check uses for the behavioural exercise.
        self.assertIs(_api_module.assert_asset_class, assert_asset_class)

    def test_signature_is_two_positional(self) -> None:
        sig = inspect.signature(assert_asset_class)
        params = list(sig.parameters)
        self.assertEqual(params, ["config", "request"])


class AssertAssetClassBehaviorTest(unittest.TestCase):
    """SRS-SDK-003 AC half-B: order rejection on the non-configured class."""

    def test_equity_strategy_with_equity_order_is_silent(self) -> None:
        cfg = StrategyConfig(strategy_id="s-eq", tradable_asset_class=AssetClass.EQUITY)
        assert_asset_class(cfg, _request(AssetClass.EQUITY))

    def test_option_strategy_with_option_order_is_silent(self) -> None:
        cfg = StrategyConfig(strategy_id="s-op", tradable_asset_class=AssetClass.OPTION)
        assert_asset_class(cfg, _request(AssetClass.OPTION))

    def test_equity_strategy_with_option_order_raises(self) -> None:
        cfg = StrategyConfig(strategy_id="s-eq", tradable_asset_class=AssetClass.EQUITY)
        with self.assertRaises(AssetClassViolation):
            assert_asset_class(cfg, _request(AssetClass.OPTION))

    def test_option_strategy_with_equity_order_raises(self) -> None:
        cfg = StrategyConfig(strategy_id="s-op", tradable_asset_class=AssetClass.OPTION)
        with self.assertRaises(AssetClassViolation):
            assert_asset_class(cfg, _request(AssetClass.EQUITY))


class AssetClassViolationContractTest(unittest.TestCase):
    """SyRS SYS-64: structured-error contract reaches user code."""

    def test_violation_message_includes_strategy_id_and_offending_class(self) -> None:
        cfg = StrategyConfig(strategy_id="alpha-7", tradable_asset_class=AssetClass.EQUITY)
        with self.assertRaises(AssetClassViolation) as ctx:
            assert_asset_class(cfg, _request(AssetClass.OPTION))
        message = str(ctx.exception)
        self.assertIn("alpha-7", message)
        self.assertIn("EQUITY", message)
        self.assertIn("OPTION", message)

    def test_violation_is_subclass_of_strategy_api_error(self) -> None:
        self.assertTrue(issubclass(AssetClassViolation, StrategyAPIError))


if __name__ == "__main__":
    unittest.main()
