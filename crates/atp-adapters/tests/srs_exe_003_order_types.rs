//! SRS-EXE-003 — market / limit / stop / stop-limit orders for EQUITIES (option
//! orders fail closed pending the contract-identity model, SRS-EXE-004 /
//! SRS-DATA-004), exercised through the **live adapter test mode** (the IB brokerage
//! adapter over a deterministic gateway double, no real IB). Proves the AC
//! facets for each order type on the live path:
//!
//!   * **accepted + acknowledged** — a well-formed order of each type is
//!     forwarded to the gateway and returns an [`OrderReceipt`];
//!   * **validated + fail-closed** — an order whose price is non-positive is
//!     rejected by the adapter with [`AdapterError::InvalidOrder`] and is
//!     **never forwarded to the gateway** (a malformed order can never create a
//!     live broker order — the safety invariant);
//!   * the order type is carried on the **same `OrderSubmission` envelope** the
//!     internal simulation consumes (`paper_order::OrderLeg` shares the identical
//!     `OrderType` / `OrderSide` / `AssetClass`), so live and paper validate by
//!     the one shared rule (`OrderSubmission::validate` → `OrderType::validate_prices`).
//!
//! The internal-simulation half of "both modes" is covered by the paper intake
//! (`paper_order::validate_leg`) + `tests/domain/test_order_type.py`, which pin
//! the SAME `validate_prices` delegation.

use std::cell::Cell;

use atp_adapters::interactive_brokers::{
    IbApiError, IbGatewayConnection, InteractiveBrokersBrokerage,
};
use atp_adapters::{
    AdapterError, BrokerageAdapter, DataBatch, HistoricalDataRequest, HistoricalQueryResult,
    MarketDataSubscription, SubscriptionReceipt,
};
use atp_types::{AssetClass, OrderReceipt, OrderSide, OrderSubmission, OrderType, StrategyId};

/// A gateway double that records how many orders it was asked to submit, so a
/// test can assert that a rejected-before-submission order NEVER reaches it.
#[derive(Default)]
struct RecordingGateway {
    submit_calls: Cell<u32>,
}

impl IbGatewayConnection for RecordingGateway {
    fn submit_order(&self, _order: &OrderSubmission) -> Result<OrderReceipt, IbApiError> {
        self.submit_calls.set(self.submit_calls.get() + 1);
        Ok(OrderReceipt {
            broker_order_id: format!("ib-ord-{}", self.submit_calls.get()),
        })
    }
    fn cancel_order(&self, _broker_order_id: &str) -> Result<(), IbApiError> {
        unimplemented!("not exercised by SRS-EXE-003")
    }
    fn subscribe_market_data(
        &self,
        _request: &MarketDataSubscription,
    ) -> Result<SubscriptionReceipt, IbApiError> {
        unimplemented!("not exercised by SRS-EXE-003")
    }
    fn historical_data(
        &self,
        _request: &HistoricalDataRequest,
    ) -> Result<HistoricalQueryResult, IbApiError> {
        unimplemented!("not exercised by SRS-EXE-003")
    }
    fn account_status(&self) -> Result<DataBatch, IbApiError> {
        unimplemented!("not exercised by SRS-EXE-003")
    }
    fn positions(&self) -> Result<DataBatch, IbApiError> {
        unimplemented!("not exercised by SRS-EXE-003")
    }
}

fn submission(asset_class: AssetClass, order_type: OrderType) -> OrderSubmission {
    OrderSubmission::new(
        StrategyId::new("live-1"),
        "AAPL",
        10,
        asset_class,
        OrderSide::Buy,
        order_type,
    )
}

/// The four order types with well-formed (strictly-positive) prices.
fn well_formed_types() -> [OrderType; 4] {
    [
        OrderType::Market,
        OrderType::Limit {
            limit_price_minor: 15_000,
        },
        OrderType::Stop {
            stop_price_minor: 14_000,
        },
        OrderType::StopLimit {
            stop_price_minor: 14_000,
            limit_price_minor: 13_900,
        },
    ]
}

#[test]
fn srs_exe_003_each_equity_order_type_accepted_and_acknowledged() {
    // Equity orders of every type are accepted + acknowledged on the live path.
    for order_type in well_formed_types() {
        let adapter = InteractiveBrokersBrokerage::new(RecordingGateway::default());
        let receipt = adapter
            .submit_order(submission(AssetClass::Equity, order_type))
            .unwrap_or_else(|err| panic!("EQUITY {order_type:?} should be accepted, got {err:?}"));
        assert!(
            !receipt.broker_order_id.is_empty(),
            "an accepted order must carry a broker order id (the acknowledgement)"
        );
        assert_eq!(
            adapter.connection().submit_calls.get(),
            1,
            "a well-formed equity order must reach the gateway exactly once"
        );
    }
}

#[test]
fn srs_exe_003_option_orders_fail_closed_pending_contract_identity() {
    // An OPTION order (any type) is rejected fail-closed: option contract
    // identity (underlying + expiration + strike + right) is not on the envelope
    // yet (deferred SRS-EXE-004 / SRS-DATA-004), so an option order with just an
    // underlying must NOT be treated as broker-ready — it never reaches the gateway.
    for order_type in well_formed_types() {
        let adapter = InteractiveBrokersBrokerage::new(RecordingGateway::default());
        match adapter.submit_order(submission(AssetClass::Option, order_type)) {
            Err(AdapterError::InvalidOrder { .. }) => {}
            other => panic!("expected an option order to fail closed, got {other:?}"),
        }
        assert_eq!(
            adapter.connection().submit_calls.get(),
            0,
            "an option order without contract identity must never reach the gateway"
        );
    }
}

#[test]
fn srs_exe_003_non_positive_priced_orders_fail_closed_before_the_gateway() {
    // Every price-carrying type with a non-positive price must be rejected by
    // the adapter BEFORE it reaches the gateway.
    let bad_types = [
        OrderType::Limit {
            limit_price_minor: 0,
        },
        OrderType::Stop {
            stop_price_minor: -1,
        },
        OrderType::StopLimit {
            stop_price_minor: 100,
            limit_price_minor: -5,
        },
    ];
    for asset_class in [AssetClass::Equity, AssetClass::Option] {
        for order_type in bad_types {
            let adapter = InteractiveBrokersBrokerage::new(RecordingGateway::default());
            let result = adapter.submit_order(submission(asset_class, order_type));
            match result {
                Err(AdapterError::InvalidOrder { adapter: name, .. }) => {
                    assert!(!name.is_empty());
                }
                other => panic!("expected InvalidOrder for {order_type:?}, got {other:?}"),
            }
            assert_eq!(
                adapter.connection().submit_calls.get(),
                0,
                "an invalid order must NEVER be forwarded to the broker gateway"
            );
        }
    }
}

#[test]
fn srs_exe_003_blank_symbol_or_bad_quantity_fail_closed_before_the_gateway() {
    // Live/paper validation parity: the live adapter rejects a blank symbol and
    // a non-positive quantity (the same well-formedness the paper intake
    // enforces) BEFORE the gateway, just like a bad price.
    let blank_symbol = OrderSubmission::new(
        StrategyId::new("live-1"),
        "   ",
        10,
        AssetClass::Equity,
        OrderSide::Buy,
        OrderType::Market,
    );
    let bad_quantity = OrderSubmission::new(
        StrategyId::new("live-1"),
        "AAPL",
        0,
        AssetClass::Equity,
        OrderSide::Buy,
        OrderType::Market,
    );
    for order in [blank_symbol, bad_quantity] {
        let adapter = InteractiveBrokersBrokerage::new(RecordingGateway::default());
        match adapter.submit_order(order) {
            Err(AdapterError::InvalidOrder { .. }) => {}
            other => panic!("expected InvalidOrder, got {other:?}"),
        }
        assert_eq!(
            adapter.connection().submit_calls.get(),
            0,
            "a malformed order must NEVER be forwarded to the broker gateway"
        );
    }
}

#[test]
fn srs_exe_003_envelope_carries_the_order_type_for_state_tracking() {
    // The order type is on the same envelope the lifecycle / routing state-track,
    // so a downstream consumer reads it back unchanged (the "state-tracked" facet).
    let sub = submission(
        AssetClass::Equity,
        OrderType::StopLimit {
            stop_price_minor: 14_000,
            limit_price_minor: 13_900,
        },
    );
    assert_eq!(sub.order_type.as_str(), "STOP_LIMIT");
    assert_eq!(sub.side, OrderSide::Buy);
    assert_eq!(sub.asset_class, AssetClass::Equity);
    assert!(sub.validate().is_ok());
}
