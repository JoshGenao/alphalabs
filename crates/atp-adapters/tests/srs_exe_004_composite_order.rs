//! SRS-EXE-004 — multi-leg options orders as **composite transactions**,
//! exercised through the **live adapter test mode** (the IB brokerage adapter over
//! a deterministic gateway double, no real IB). Proves the AC facet "a four-leg
//! options order is submitted as one composite order in IB live test mode":
//!
//!   * **one composite → one broker order id** — a well-formed four-leg options
//!     composite is forwarded to the gateway as ONE combo order and returns ONE
//!     [`OrderReceipt`] (the legs fill together or not at all — never one broker
//!     order per leg);
//!   * **validated + fail-closed** — a composite that is empty, single-leg, or
//!     carries a bad leg (non-positive quantity / price) is rejected by the adapter
//!     with [`AdapterError::InvalidOrder`] and is **never forwarded to the gateway**
//!     (no partial spread can reach the live broker — the safety invariant);
//!   * the composite validates by the SAME shared price authority the single-leg
//!     path uses (`CompositeOrderSubmission::validate` → `OrderType::validate_prices`),
//!     so live and paper cannot drift.
//!
//! The internal-simulation "one composite in paper mode" half is covered by
//! `crates/atp-simulation` (`PaperOrderRequest::MultiLeg`, SRS-SIM-001) and the
//! paired `tests/domain/test_composite_order.py`. The REAL IB combo wire is
//! operator-gated (SRS-EXE-006, `ib-live-transport`), so SRS-EXE-004 lands
//! serialized (`passes:false`).

use std::cell::Cell;

use atp_adapters::interactive_brokers::{
    IbApiError, IbGatewayConnection, InteractiveBrokersBrokerage,
};
use atp_adapters::{
    AdapterError, BrokerageAdapter, DataBatch, HistoricalDataRequest, HistoricalQueryResult,
    MarketDataSubscription, SubscriptionReceipt,
};
use atp_types::{
    CompositeOrderLeg, CompositeOrderSubmission, ExpirationDate, OptionContractIdentity,
    OptionRight, OrderReceipt, OrderSide, OrderSubmission, OrderType, StrategyId,
};

/// A gateway double that records how many single-leg and composite orders it was
/// asked to submit, so a test can assert a rejected-before-submission composite
/// NEVER reaches it, and that an accepted composite reaches it exactly once.
#[derive(Default)]
struct RecordingGateway {
    composite_calls: Cell<u32>,
}

impl IbGatewayConnection for RecordingGateway {
    fn submit_order(&self, _order: &OrderSubmission) -> Result<OrderReceipt, IbApiError> {
        unimplemented!("single-leg orders are exercised by SRS-EXE-003")
    }
    fn submit_composite_order(
        &self,
        _order: &CompositeOrderSubmission,
    ) -> Result<OrderReceipt, IbApiError> {
        // ONE combo order id for the whole composite — never one per leg.
        self.composite_calls.set(self.composite_calls.get() + 1);
        Ok(OrderReceipt {
            broker_order_id: format!("ib-combo-{}", self.composite_calls.get()),
        })
    }
    fn cancel_order(&self, _broker_order_id: &str) -> Result<(), IbApiError> {
        unimplemented!("not exercised by SRS-EXE-004")
    }
    fn subscribe_market_data(
        &self,
        _request: &MarketDataSubscription,
    ) -> Result<SubscriptionReceipt, IbApiError> {
        unimplemented!("not exercised by SRS-EXE-004")
    }
    fn historical_data(
        &self,
        _request: &HistoricalDataRequest,
    ) -> Result<HistoricalQueryResult, IbApiError> {
        unimplemented!("not exercised by SRS-EXE-004")
    }
    fn account_status(&self) -> Result<DataBatch, IbApiError> {
        unimplemented!("not exercised by SRS-EXE-004")
    }
    fn positions(&self) -> Result<DataBatch, IbApiError> {
        unimplemented!("not exercised by SRS-EXE-004")
    }
}

fn expiry() -> ExpirationDate {
    ExpirationDate::new(2024, 6, 21).expect("valid expiry")
}

fn leg(
    strike_minor: i64,
    side: OrderSide,
    right: OptionRight,
    order_type: OrderType,
) -> CompositeOrderLeg {
    let contract =
        OptionContractIdentity::new("SPY", expiry(), strike_minor, right).expect("valid contract");
    CompositeOrderLeg::new(contract, side, 1, order_type)
}

/// A four-leg iron condor on one underlying — the AC's "four-leg options order".
fn iron_condor() -> CompositeOrderSubmission {
    CompositeOrderSubmission::new(
        StrategyId::new("live-1"),
        vec![
            leg(
                48_000_000,
                OrderSide::Buy,
                OptionRight::Put,
                OrderType::Market,
            ),
            leg(
                49_000_000,
                OrderSide::Sell,
                OptionRight::Put,
                OrderType::Market,
            ),
            leg(
                52_000_000,
                OrderSide::Sell,
                OptionRight::Call,
                OrderType::Market,
            ),
            leg(
                53_000_000,
                OrderSide::Buy,
                OptionRight::Call,
                OrderType::Market,
            ),
        ],
    )
}

#[test]
fn srs_exe_004_four_leg_composite_submits_as_one_broker_order() {
    // The AC: "a four-leg options order is submitted as one composite order in IB
    // live test mode." One composite → one broker order id, reaching the gateway
    // exactly once (not four times).
    let adapter = InteractiveBrokersBrokerage::new(RecordingGateway::default());
    let composite = iron_condor();
    assert_eq!(composite.leg_count(), 4);

    let receipt = adapter
        .submit_composite_order(composite)
        .expect("a well-formed four-leg composite is accepted");
    assert!(
        !receipt.broker_order_id.is_empty(),
        "the composite must acknowledge with one broker order id"
    );
    assert_eq!(
        adapter.connection().composite_calls.get(),
        1,
        "the whole composite reaches the gateway exactly ONCE — one combo order, not one per leg"
    );
}

#[test]
fn srs_exe_004_single_leg_composite_fails_closed_before_the_gateway() {
    // A one-leg "composite" is not a SYS-4 multi-leg order — it must be rejected
    // before it can reach the gateway.
    let adapter = InteractiveBrokersBrokerage::new(RecordingGateway::default());
    let single = CompositeOrderSubmission::new(
        StrategyId::new("live-1"),
        vec![leg(
            50_000_000,
            OrderSide::Buy,
            OptionRight::Call,
            OrderType::Market,
        )],
    );
    match adapter.submit_composite_order(single) {
        Err(AdapterError::InvalidOrder { .. }) => {}
        other => panic!("expected a single-leg composite to fail closed, got {other:?}"),
    }
    assert_eq!(
        adapter.connection().composite_calls.get(),
        0,
        "a single-leg composite must NEVER reach the broker gateway"
    );
}

#[test]
fn srs_exe_004_empty_composite_fails_closed_before_the_gateway() {
    let adapter = InteractiveBrokersBrokerage::new(RecordingGateway::default());
    let empty = CompositeOrderSubmission::new(StrategyId::new("live-1"), vec![]);
    match adapter.submit_composite_order(empty) {
        Err(AdapterError::InvalidOrder { .. }) => {}
        other => panic!("expected an empty composite to fail closed, got {other:?}"),
    }
    assert_eq!(adapter.connection().composite_calls.get(), 0);
}

#[test]
fn srs_exe_004_bad_leg_fails_the_whole_composite_before_the_gateway() {
    // One malformed leg (non-positive limit price) rejects the ENTIRE composite —
    // no partial spread reaches the broker.
    let adapter = InteractiveBrokersBrokerage::new(RecordingGateway::default());
    let bad = CompositeOrderSubmission::new(
        StrategyId::new("live-1"),
        vec![
            leg(
                50_000_000,
                OrderSide::Buy,
                OptionRight::Call,
                OrderType::Market,
            ),
            leg(
                51_000_000,
                OrderSide::Sell,
                OptionRight::Call,
                OrderType::Limit {
                    limit_price_minor: 0,
                },
            ),
        ],
    );
    match adapter.submit_composite_order(bad) {
        Err(AdapterError::InvalidOrder { adapter: name, .. }) => assert!(!name.is_empty()),
        other => panic!("expected InvalidOrder, got {other:?}"),
    }
    assert_eq!(
        adapter.connection().composite_calls.get(),
        0,
        "a composite with any invalid leg must NEVER reach the broker gateway"
    );
}

#[test]
fn srs_exe_004_connectionless_adapter_never_fabricates_a_composite() {
    // The zero-config IB discovery handle has no live session, so a composite must
    // fail closed with NotConfigured — a broker adapter with no session must never
    // fabricate an order (mirrors the single-leg NotConfigured guard).
    use atp_adapters::InteractiveBrokersAdapter;
    let adapter = InteractiveBrokersAdapter;
    match adapter.submit_composite_order(iron_condor()) {
        Err(AdapterError::NotConfigured { capability, .. }) => {
            assert_eq!(capability, "submit_composite_order");
        }
        other => panic!("expected NotConfigured, got {other:?}"),
    }
}
