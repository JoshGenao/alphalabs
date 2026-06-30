//! SRS-EXE-006 — IB Gateway brokerage adapter, boundary + operator-gated
//! integration coverage.
//!
//! The boundary tests drive the adapter's four AC operations (order submission,
//! cancellation, market-data subscription, historical-data retrieval) end-to-end
//! through a deterministic in-memory [`FakeIbGateway`] transport — no socket, so
//! they run in the parallel agent pool. They prove the IB-error → SyRS SYS-64
//! [`StructuredOrderError`] mapping for every broker-validation category and that
//! a failed submission is never silently dropped.
//!
//! The single `#[ignore]` test ([`paper_account_round_trip`]) is the AC's real
//! verification: it drives the live [`TcpIbGateway`] against a **headless IB
//! paper account** (port 4002; SyRS SYS-2e / AC-2, no TWS GUI). It is
//! operator-initiated (`ATP_RUN_INTEGRATION=1` + `--ignored`) because the IB
//! paper account binds a fixed shared port — which is why SRS-EXE-006 lands
//! serialized (`passes:false`) until the operator runs it.

use atp_adapters::interactive_brokers::{
    IbAccountKind, IbApiError, IbConnectionConfig, IbGatewayConnection, IbOrderSubmitError,
    InteractiveBrokersBrokerage, TcpIbGateway, IB_CODE_MAX_RATE_EXCEEDED,
    IB_CODE_NO_SECURITY_DEFINITION, IB_CODE_ORDER_REJECTED,
};
use atp_types::{OrderErrorCategory, OrderSubmission, StrategyId};
use std::cell::RefCell;

/// A deterministic in-memory IB Gateway transport: each operation either yields a
/// programmed success or a programmed [`IbApiError`]. Records the operations it
/// saw so a test can assert the adapter forwarded the right request.
#[derive(Default)]
struct FakeIbGateway {
    submit: Option<Result<String, IbApiError>>,
    cancel: Option<Result<(), IbApiError>>,
    subscribe: Option<Result<String, IbApiError>>,
    historical: Option<Result<usize, IbApiError>>,
    seen_symbols: RefCell<Vec<String>>,
    seen_cancels: RefCell<Vec<String>>,
}

impl FakeIbGateway {
    fn accepting() -> Self {
        Self {
            submit: Some(Ok("ib-ord-1".to_string())),
            cancel: Some(Ok(())),
            subscribe: Some(Ok("ib-sub-1".to_string())),
            historical: Some(Ok(390)),
            ..Self::default()
        }
    }

    fn rejecting_submit(error: IbApiError) -> Self {
        Self {
            submit: Some(Err(error)),
            ..Self::default()
        }
    }
}

impl IbGatewayConnection for FakeIbGateway {
    fn submit_order(&self, order: &OrderSubmission) -> Result<String, IbApiError> {
        self.seen_symbols.borrow_mut().push(order.symbol.clone());
        self.submit
            .clone()
            .expect("test did not program submit_order")
    }

    fn cancel_order(&self, broker_order_id: &str) -> Result<(), IbApiError> {
        self.seen_cancels
            .borrow_mut()
            .push(broker_order_id.to_string());
        self.cancel
            .clone()
            .expect("test did not program cancel_order")
    }

    fn subscribe_market_data(&self, symbol: &str) -> Result<String, IbApiError> {
        self.seen_symbols.borrow_mut().push(symbol.to_string());
        self.subscribe
            .clone()
            .expect("test did not program subscribe_market_data")
    }

    fn request_historical_data(&self, symbol: &str) -> Result<usize, IbApiError> {
        self.seen_symbols.borrow_mut().push(symbol.to_string());
        self.historical
            .clone()
            .expect("test did not program request_historical_data")
    }
}

fn order(symbol: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new("live-1"),
        symbol: symbol.to_string(),
        quantity,
    }
}

#[test]
fn accepted_order_returns_vendor_neutral_receipt() {
    let adapter = InteractiveBrokersBrokerage::new(FakeIbGateway::accepting());
    let receipt = adapter
        .submit_order(order("AAPL", 10))
        .expect("an accepted order yields a receipt");
    assert_eq!(receipt.broker_order_id, "ib-ord-1");
    assert_eq!(
        adapter.connection().seen_symbols.borrow().as_slice(),
        ["AAPL"]
    );
}

#[test]
fn rejection_maps_to_syrs64_invalid_symbol() {
    let adapter =
        InteractiveBrokersBrokerage::new(FakeIbGateway::rejecting_submit(IbApiError::new(
            IB_CODE_NO_SECURITY_DEFINITION,
            "No security definition found",
        )));
    let submitted = order("ZZZZ", 5);
    let err = adapter
        .submit_order(submitted.clone())
        .expect_err("an unknown symbol must be rejected");
    match err {
        IbOrderSubmitError::Structured(envelope) => {
            assert_eq!(envelope.category, OrderErrorCategory::InvalidSymbol);
            assert_eq!(envelope.original_order, submitted);
        }
        other => panic!("expected a structured INVALID_SYMBOL envelope, got {other:?}"),
    }
}

#[test]
fn rejection_maps_to_syrs64_insufficient_buying_power() {
    let adapter =
        InteractiveBrokersBrokerage::new(FakeIbGateway::rejecting_submit(IbApiError::new(
            IB_CODE_ORDER_REJECTED,
            "Order rejected - reason: Insufficient buying power for this order",
        )));
    let err = adapter
        .submit_order(order("AAPL", 1_000_000))
        .expect_err("an over-leveraged order must be rejected");
    assert!(matches!(
        err,
        IbOrderSubmitError::Structured(e) if e.category == OrderErrorCategory::InsufficientBuyingPower
    ));
}

#[test]
fn rejection_maps_to_syrs64_rate_limited() {
    let adapter =
        InteractiveBrokersBrokerage::new(FakeIbGateway::rejecting_submit(IbApiError::new(
            IB_CODE_MAX_RATE_EXCEEDED,
            "Max rate of messages per second exceeded",
        )));
    let err = adapter
        .submit_order(order("AAPL", 1))
        .expect_err("a throttled submission must be rejected");
    assert!(matches!(
        err,
        IbOrderSubmitError::Structured(e) if e.category == OrderErrorCategory::RateLimited
    ));
}

#[test]
fn unmapped_rejection_is_surfaced_not_dropped() {
    let adapter =
        InteractiveBrokersBrokerage::new(FakeIbGateway::rejecting_submit(IbApiError::new(
            IB_CODE_ORDER_REJECTED,
            "Order rejected - reason: odd lot not allowed",
        )));
    let submitted = order("AAPL", 3);
    let err = adapter
        .submit_order(submitted.clone())
        .expect_err("an unmapped rejection must still surface");
    match err {
        IbOrderSubmitError::Unmapped {
            code,
            message,
            original_order,
        } => {
            assert_eq!(code, IB_CODE_ORDER_REJECTED);
            assert!(message.contains("odd lot"));
            assert_eq!(original_order, submitted);
        }
        other => panic!("expected an unmapped failure, got {other:?}"),
    }
}

#[test]
fn cancel_subscribe_historical_round_trip_through_transport() {
    let adapter = InteractiveBrokersBrokerage::new(FakeIbGateway::accepting());
    adapter.cancel_order("ib-ord-1").expect("cancel succeeds");
    assert_eq!(
        adapter.connection().seen_cancels.borrow().as_slice(),
        ["ib-ord-1"]
    );

    let sub = adapter
        .subscribe_market_data("AAPL")
        .expect("subscription succeeds");
    assert_eq!(sub.symbol, "AAPL");
    assert_eq!(sub.subscription_id, "ib-sub-1");

    let hist = adapter
        .request_historical_data("AAPL")
        .expect("historical retrieval succeeds");
    assert_eq!(hist.symbol, "AAPL");
    assert_eq!(hist.bar_count, 390);
}

/// AC verification — operator-initiated only. Drives the live [`TcpIbGateway`]
/// against the headless IB **paper** account (port 4002; no TWS GUI). Skipped
/// unless `ATP_RUN_INTEGRATION=1` AND run with `--ignored`, because the IB paper
/// account binds a fixed shared port (SyRS SYS-2e) and must not run in the
/// parallel agent pool. This is the gate that flips SRS-EXE-006 to `passes:true`.
#[test]
#[ignore = "operator-initiated IB paper-account integration (ATP_RUN_INTEGRATION=1); binds fixed port 4002"]
fn paper_account_round_trip() {
    if std::env::var("ATP_RUN_INTEGRATION").as_deref() != Ok("1") {
        eprintln!("skipping: set ATP_RUN_INTEGRATION=1 to run the IB paper-account integration");
        return;
    }
    let config = IbConnectionConfig::from_env(101);
    let adapter = InteractiveBrokersBrokerage::new(TcpIbGateway::new(config, IbAccountKind::Paper));

    // Order submission, cancellation, market-data subscription, and historical-data
    // retrieval against the IB paper account, without the TWS GUI (AC-2). The
    // operator completes the TWS wire encoding in TcpIbGateway before this passes.
    let receipt = adapter
        .submit_order(order("AAPL", 1))
        .expect("paper account accepts a 1-share AAPL order");
    adapter
        .cancel_order(&receipt.broker_order_id)
        .expect("paper account cancels the resting order");
    adapter
        .subscribe_market_data("AAPL")
        .expect("paper account confirms a market-data subscription");
    let hist = adapter
        .request_historical_data("AAPL")
        .expect("paper account returns historical bars");
    assert!(hist.bar_count > 0, "historical retrieval returned no bars");
}
