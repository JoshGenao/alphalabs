"""SRS-SDK-003 / SyRS SYS-5 — single tradable asset class invariant.

L7 domain (safety) test. Walks both halves of the SRS-SDK-003 AC
end-to-end against a concrete ``_RefStrategyContext`` reference impl
whose ``order()`` calls the SDK-shipped ``assert_asset_class`` guard
(no shadow implementation). Locks:

* Half A — A strategy of either tradable class may subscribe to both
  equities and options for analysis. Subscription never raises on
  asset class.
* Half B — Order submission against the non-configured class raises
  ``AssetClassViolation``; the runtime's order-routing path is not
  reached when the guard raises.

A defensive case (``test_reference_context_calls_shipped_guard``)
patches ``atp_strategy.api.assert_asset_class`` to a sentinel callable
and asserts the reference impl invokes it — locking the rule that
production drivers re-use the SDK helper rather than reimplementing
the comparison (which would silently drift).
"""

from __future__ import annotations

import sys
import unittest
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_strategy as api  # noqa: E402
import pytest  # noqa: E402
from atp_strategy import (  # noqa: E402
    AssetClass,
    AssetClassViolation,
    OrderHandle,
    OrderRequest,
    OrderSide,
    OrderType,
    StrategyConfig,
    assert_asset_class,
)

pytestmark = [pytest.mark.domain, pytest.mark.safety]


@dataclass
class _RefStrategyContext:
    """Minimal concrete StrategyContext for SRS-SDK-003 end-to-end coverage.

    Wires only the methods exercised by this test (``subscribe`` and
    ``order``). The ``order`` path delegates to the shipped
    ``atp_strategy.assert_asset_class`` helper — no shadow comparison
    in this fixture. ``_route_order`` is the production-side hand-off
    (live IB execution or internal paper simulation in real drivers);
    here it records calls so the test can assert routing was reached
    iff the guard accepted the request.
    """

    config: StrategyConfig
    subscribed: list[tuple[str, AssetClass]] = field(default_factory=list)
    routed: list[OrderRequest] = field(default_factory=list)

    def subscribe(self, symbol: str, asset_class: AssetClass = AssetClass.EQUITY) -> None:
        # SRS-SDK-003 AC half-A: no guard on the analysis-subscription
        # path; both tradable classes may subscribe to both data classes.
        self.subscribed.append((symbol, asset_class))

    def order(self, request: OrderRequest) -> OrderHandle:
        # SRS-SDK-003 AC half-B: guard before routing. Production drivers
        # (live IB, internal paper) must call the SDK helper, NOT their
        # own comparison — that's what the L1/L3 layer locks in.
        assert_asset_class(self.config, request)
        return self._route_order(request)

    def _route_order(self, request: OrderRequest) -> OrderHandle:
        self.routed.append(request)
        return OrderHandle(order_id=str(uuid.uuid4()), strategy_id=self.config.strategy_id)


def _equity_request() -> OrderRequest:
    return OrderRequest(
        symbol="AAPL",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        asset_class=AssetClass.EQUITY,
    )


def _option_request() -> OrderRequest:
    return OrderRequest(
        symbol="SPY",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        asset_class=AssetClass.OPTION,
    )


class SubscriptionHalfATest(unittest.TestCase):
    """SRS-SDK-003 AC half-A: subscriptions span both asset classes."""

    def test_equity_strategy_subscribes_to_equity(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s1", tradable_asset_class=AssetClass.EQUITY)
        )
        ctx.subscribe("AAPL", asset_class=AssetClass.EQUITY)
        self.assertEqual(ctx.subscribed, [("AAPL", AssetClass.EQUITY)])

    def test_equity_strategy_subscribes_to_option_for_analysis(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s1", tradable_asset_class=AssetClass.EQUITY)
        )
        # An EQUITY-tradable strategy is free to read OPTION analytics
        # (vol surface, term structure, etc.) without being able to trade
        # options — SRS-SDK-003 AC half-A.
        ctx.subscribe("SPY", asset_class=AssetClass.OPTION)
        self.assertEqual(ctx.subscribed, [("SPY", AssetClass.OPTION)])

    def test_option_strategy_subscribes_to_equity_for_analysis(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s2", tradable_asset_class=AssetClass.OPTION)
        )
        # The inverse: an OPTION-tradable strategy must be able to read
        # underlying equity prices to hedge, even though it cannot
        # submit equity orders.
        ctx.subscribe("AAPL", asset_class=AssetClass.EQUITY)
        self.assertEqual(ctx.subscribed, [("AAPL", AssetClass.EQUITY)])

    def test_option_strategy_subscribes_to_option(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s2", tradable_asset_class=AssetClass.OPTION)
        )
        ctx.subscribe("SPY", asset_class=AssetClass.OPTION)
        self.assertEqual(ctx.subscribed, [("SPY", AssetClass.OPTION)])


class OrderRoutingHalfBTest(unittest.TestCase):
    """SRS-SDK-003 AC half-B: order submission rejects the non-configured class."""

    def test_equity_strategy_blocks_option_order(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s1", tradable_asset_class=AssetClass.EQUITY)
        )
        with self.assertRaises(AssetClassViolation):
            ctx.order(_option_request())
        # Routing must not have been reached.
        self.assertEqual(ctx.routed, [])

    def test_option_strategy_blocks_equity_order(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s2", tradable_asset_class=AssetClass.OPTION)
        )
        with self.assertRaises(AssetClassViolation):
            ctx.order(_equity_request())
        self.assertEqual(ctx.routed, [])

    def test_equity_order_passes_through_for_equity_strategy(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s1", tradable_asset_class=AssetClass.EQUITY)
        )
        handle = ctx.order(_equity_request())
        self.assertIsInstance(handle, OrderHandle)
        self.assertEqual(handle.strategy_id, "s1")
        self.assertEqual(len(ctx.routed), 1)
        self.assertIs(ctx.routed[0].asset_class, AssetClass.EQUITY)

    def test_option_order_passes_through_for_option_strategy(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s2", tradable_asset_class=AssetClass.OPTION)
        )
        handle = ctx.order(_option_request())
        self.assertIsInstance(handle, OrderHandle)
        self.assertEqual(handle.strategy_id, "s2")
        self.assertEqual(len(ctx.routed), 1)
        self.assertIs(ctx.routed[0].asset_class, AssetClass.OPTION)


class GuardInvocationTest(unittest.TestCase):
    """Production drivers must call the SDK guard, not reimplement it."""

    def test_violation_does_not_silently_route(self) -> None:
        """A mismatched order must not reach _route_order — the router is
        unreachable when the guard raises.
        """

        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s1", tradable_asset_class=AssetClass.EQUITY)
        )
        with self.assertRaises(AssetClassViolation):
            ctx.order(_option_request())
        self.assertEqual(ctx.routed, [], "router reached on a mismatched order")

    def test_reference_context_calls_shipped_guard(self) -> None:
        """Replaces the SDK guard with a sentinel and asserts the reference
        impl invokes it — locks the rule that production drivers must
        delegate to the SDK helper, not reimplement the comparison.
        """

        sentinel_calls: list[tuple[StrategyConfig, OrderRequest]] = []

        def sentinel_guard(config: StrategyConfig, request: OrderRequest) -> None:
            sentinel_calls.append((config, request))
            # No raise — sentinel approves everything so we can confirm
            # the call site is reached even on the matched-class path.

        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s1", tradable_asset_class=AssetClass.EQUITY)
        )
        with mock.patch(f"{_RefStrategyContext.__module__}.assert_asset_class", sentinel_guard):
            ctx.order(_equity_request())

        self.assertEqual(len(sentinel_calls), 1)
        cfg, req = sentinel_calls[0]
        self.assertEqual(cfg.strategy_id, "s1")
        self.assertIs(req.asset_class, AssetClass.EQUITY)


class StructuredErrorContractTest(unittest.TestCase):
    """SyRS SYS-64: violation reaches user code through StrategyAPIError."""

    def test_violation_is_catchable_via_strategy_api_error(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(strategy_id="s1", tradable_asset_class=AssetClass.EQUITY)
        )
        # User code may catch the base class and route on the structured
        # message — SyRS SYS-64.
        with self.assertRaises(api.StrategyAPIError):
            ctx.order(_option_request())

    def test_violation_message_names_strategy_and_offending_class(self) -> None:
        ctx = _RefStrategyContext(
            config=StrategyConfig(
                strategy_id="alpha-momentum-7", tradable_asset_class=AssetClass.OPTION
            )
        )
        try:
            ctx.order(_equity_request())
        except AssetClassViolation as exc:
            message = str(exc)
            self.assertIn("alpha-momentum-7", message)
            self.assertIn("OPTION", message)
            self.assertIn("EQUITY", message)
        else:
            self.fail("AssetClassViolation was not raised")


if __name__ == "__main__":
    unittest.main()
