//! SRS-EXE-006 — IB Gateway brokerage adapter, boundary + operator-gated
//! integration coverage.
//!
//! The boundary tests drive the adapter's four AC operations (order submission,
//! cancellation, market-data subscription, historical-data retrieval) end-to-end
//! through the **canonical** `BrokerageAdapter` / `MarketDataAdapter` /
//! `HistoricalDataAdapter` traits over a deterministic in-memory [`FakeIbGateway`]
//! transport — no socket, so they run in the parallel agent pool. They prove the
//! IB-error → SyRS SYS-64 classification surfaces through the common
//! `AdapterError::Brokerage` taxonomy for every broker-validation category and
//! that a failed submission is never silently dropped.
//!
//! The single `#[ignore]` test ([`paper_account_round_trip`]) is the AC's real
//! verification: it drives the live [`TcpIbGateway`] against a **headless IB
//! paper account** (port 4002; SyRS SYS-2e / AC-2, no TWS GUI). It is
//! operator-initiated (`ATP_RUN_INTEGRATION=1` + `--ignored`) because the IB
//! paper account binds a fixed shared port — which is why SRS-EXE-006 lands
//! serialized (`passes:false`) until the operator runs it.

use atp_adapters::interactive_brokers::{
    IbApiError, IbGatewayConnection, InteractiveBrokersBrokerage, IB_CODE_MAX_RATE_EXCEEDED,
    IB_CODE_NOT_CONNECTED, IB_CODE_NO_SECURITY_DEFINITION, IB_CODE_ORDER_REJECTED,
};
// The live socket transport is behind the non-default `ib-live-transport` feature;
// the operator-gated paper-account round-trip is the only test that uses it.
#[cfg(feature = "ib-live-transport")]
use atp_adapters::interactive_brokers::{IbAccountKind, IbConnectionConfig, TcpIbGateway};
use atp_adapters::{
    AdapterError, AssetClass, BrokerageAdapter, DataBatch, HistoricalBar, HistoricalDataAdapter,
    HistoricalDataRequest, HistoricalQueryResult, InteractiveBrokersAdapter, MarketDataAdapter,
    MarketDataChannel, MarketDataSubscription, NormalizationMode, OrderReceipt, OrderSubmission,
    SubscriptionReceipt,
};
use atp_types::{OrderErrorCategory, StrategyId};

/// A deterministic in-memory IB Gateway transport: each operation either yields a
/// programmed success or a programmed [`IbApiError`].
#[derive(Default)]
struct FakeIbGateway {
    submit: Option<Result<OrderReceipt, IbApiError>>,
    cancel: Option<Result<(), IbApiError>>,
    subscribe: Option<Result<SubscriptionReceipt, IbApiError>>,
    historical: Option<Result<HistoricalQueryResult, IbApiError>>,
    account: Option<Result<DataBatch, IbApiError>>,
    positions: Option<Result<DataBatch, IbApiError>>,
}

impl FakeIbGateway {
    fn accepting() -> Self {
        Self {
            submit: Some(Ok(OrderReceipt {
                broker_order_id: "ib-ord-1".to_string(),
            })),
            cancel: Some(Ok(())),
            subscribe: Some(Ok(SubscriptionReceipt {
                subscription_id: "ib-sub-1".to_string(),
            })),
            historical: Some(Ok(HistoricalQueryResult {
                symbol: "AAPL".to_string(),
                asset_class: AssetClass::Equity,
                normalization_mode: NormalizationMode::SplitAdjusted,
                bars: vec![HistoricalBar {
                    symbol: "AAPL".to_string(),
                    close: 100.0,
                }],
            })),
            account: Some(Ok(DataBatch { records: 1 })),
            positions: Some(Ok(DataBatch { records: 3 })),
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
    fn submit_order(&self, _order: &OrderSubmission) -> Result<OrderReceipt, IbApiError> {
        self.submit
            .clone()
            .expect("test did not program submit_order")
    }

    fn cancel_order(&self, _broker_order_id: &str) -> Result<(), IbApiError> {
        self.cancel
            .clone()
            .expect("test did not program cancel_order")
    }

    fn subscribe_market_data(
        &self,
        _request: &MarketDataSubscription,
    ) -> Result<SubscriptionReceipt, IbApiError> {
        self.subscribe
            .clone()
            .expect("test did not program subscribe_market_data")
    }

    fn historical_data(
        &self,
        _request: &HistoricalDataRequest,
    ) -> Result<HistoricalQueryResult, IbApiError> {
        self.historical
            .clone()
            .expect("test did not program historical_data")
    }

    fn account_status(&self) -> Result<DataBatch, IbApiError> {
        self.account
            .clone()
            .expect("test did not program account_status")
    }

    fn positions(&self) -> Result<DataBatch, IbApiError> {
        self.positions
            .clone()
            .expect("test did not program positions")
    }
}

fn order(symbol: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new("live-1"),
        symbol: symbol.to_string(),
        quantity,
    }
}

fn quotes(symbol: &str) -> MarketDataSubscription {
    MarketDataSubscription {
        symbol: symbol.to_string(),
        channel: MarketDataChannel::Quotes,
    }
}

fn daily(symbol: &str) -> HistoricalDataRequest {
    HistoricalDataRequest {
        symbol: symbol.to_string(),
        start: "2026-01-01".to_string(),
        end: "2026-02-01".to_string(),
        resolution: "1d".to_string(),
        asset_class: AssetClass::Equity,
        normalization_mode: NormalizationMode::SplitAdjusted,
    }
}

#[test]
fn accepted_order_returns_vendor_neutral_receipt() {
    let adapter = InteractiveBrokersBrokerage::new(FakeIbGateway::accepting());
    let receipt = adapter
        .submit_order(order("AAPL", 10))
        .expect("an accepted order yields a receipt");
    assert_eq!(receipt.broker_order_id, "ib-ord-1");
}

#[test]
fn rejection_maps_to_syrs64_invalid_symbol() {
    let adapter =
        InteractiveBrokersBrokerage::new(FakeIbGateway::rejecting_submit(IbApiError::new(
            IB_CODE_NO_SECURITY_DEFINITION,
            "No security definition found",
        )));
    let err = adapter
        .submit_order(order("ZZZZ", 5))
        .expect_err("an unknown symbol must be rejected");
    assert!(matches!(
        err,
        AdapterError::Brokerage {
            category: Some(OrderErrorCategory::InvalidSymbol),
            ..
        }
    ));
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
        AdapterError::Brokerage {
            category: Some(OrderErrorCategory::InsufficientBuyingPower),
            ..
        }
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
        AdapterError::Brokerage {
            category: Some(OrderErrorCategory::RateLimited),
            ..
        }
    ));
}

#[test]
fn unmapped_rejection_is_surfaced_not_dropped() {
    let adapter =
        InteractiveBrokersBrokerage::new(FakeIbGateway::rejecting_submit(IbApiError::new(
            IB_CODE_ORDER_REJECTED,
            "Order rejected - reason: odd lot not allowed",
        )));
    let err = adapter
        .submit_order(order("AAPL", 3))
        .expect_err("an unmapped rejection must still surface");
    match err {
        AdapterError::Brokerage {
            category,
            code,
            message,
            ..
        } => {
            assert_eq!(category, None);
            assert_eq!(code, IB_CODE_ORDER_REJECTED);
            assert!(message.contains("odd lot"));
        }
        other => panic!("expected AdapterError::Brokerage, got {other:?}"),
    }
}

#[test]
fn cancel_subscribe_historical_round_trip_through_canonical_traits() {
    let adapter = InteractiveBrokersBrokerage::new(FakeIbGateway::accepting());
    adapter.cancel_order("ib-ord-1").expect("cancel succeeds");

    let sub = adapter
        .subscribe_market_data(quotes("AAPL"))
        .expect("subscription succeeds");
    assert_eq!(sub.subscription_id, "ib-sub-1");

    let hist = adapter
        .historical_data(daily("AAPL"))
        .expect("historical retrieval succeeds");
    assert_eq!(hist.symbol, "AAPL");
    assert_eq!(hist.bars.len(), 1);
}

#[test]
fn account_status_and_positions_are_implemented_not_notconfigured() {
    // API-5 traces account status + positions to SRS-EXE-006 — the functional
    // adapter implements them through the transport (NOT inherited NotConfigured).
    let adapter = InteractiveBrokersBrokerage::new(FakeIbGateway::accepting());
    assert_eq!(
        adapter.account_status().expect("account status succeeds"),
        DataBatch { records: 1 }
    );
    assert_eq!(
        adapter.positions().expect("positions succeed"),
        DataBatch { records: 3 }
    );

    // A failure flows through the common AdapterError::Brokerage taxonomy.
    let down = FakeIbGateway {
        account: Some(Err(IbApiError::new(IB_CODE_NOT_CONNECTED, "Not connected"))),
        ..FakeIbGateway::default()
    };
    let adapter = InteractiveBrokersBrokerage::new(down);
    assert!(matches!(
        adapter
            .account_status()
            .expect_err("account status must fail"),
        AdapterError::Brokerage {
            category: Some(OrderErrorCategory::ConnectivityBlocked),
            ..
        }
    ));
}

#[test]
fn non_order_operation_failure_maps_to_classified_boundary_error() {
    // A connectivity fault on cancel/subscribe/historical surfaces through the
    // common AdapterError::Brokerage taxonomy, classified CONNECTIVITY_BLOCKED.
    let gw = FakeIbGateway {
        cancel: Some(Err(IbApiError::new(IB_CODE_NOT_CONNECTED, "Not connected"))),
        subscribe: Some(Err(IbApiError::new(IB_CODE_NOT_CONNECTED, "Not connected"))),
        historical: Some(Err(IbApiError::new(IB_CODE_NOT_CONNECTED, "Not connected"))),
        ..FakeIbGateway::default()
    };
    let adapter = InteractiveBrokersBrokerage::new(gw);
    for err in [
        adapter
            .cancel_order("ib-ord-9")
            .expect_err("cancel must fail"),
        adapter
            .subscribe_market_data(quotes("AAPL"))
            .map(|_| ())
            .expect_err("subscribe must fail"),
        adapter
            .historical_data(daily("AAPL"))
            .map(|_| ())
            .expect_err("historical must fail"),
    ] {
        assert!(matches!(
            err,
            AdapterError::Brokerage {
                category: Some(OrderErrorCategory::ConnectivityBlocked),
                ..
            }
        ));
    }
}

#[test]
fn documented_provider_bridges_to_functional_runtime() {
    // The documented zero-config provider (InteractiveBrokersAdapter, named in
    // adapter_contract) bridges to the FUNCTIONAL runtime via with_gateway — and the
    // functional path returns a real receipt, NOT AdapterError::NotConfigured.
    let adapter = InteractiveBrokersAdapter.with_gateway(FakeIbGateway::accepting());
    let receipt = adapter
        .submit_order(order("AAPL", 1))
        .expect("the wired runtime submits for real, not NotConfigured");
    assert_eq!(receipt.broker_order_id, "ib-ord-1");
}

#[test]
fn connectionless_provider_is_not_configured_by_design() {
    // The connectionless handle MUST NOT fabricate trading operations — with no live
    // session, NotConfigured is the safe answer (trading requires a transport).
    let err = InteractiveBrokersAdapter
        .submit_order(order("AAPL", 1))
        .expect_err("a connectionless IB adapter cannot submit");
    assert!(matches!(err, AdapterError::NotConfigured { .. }));
}

/// AC verification — operator-initiated only. Drives the live [`TcpIbGateway`]
/// against the headless IB **paper** account (port 4002; no TWS GUI). Skipped
/// unless `ATP_RUN_INTEGRATION=1` AND run with `--ignored`, because the IB paper
/// account binds a fixed shared port (SyRS SYS-2e) and must not run in the
/// parallel agent pool. This is the gate that flips SRS-EXE-006 to `passes:true`.
#[cfg(feature = "ib-live-transport")]
#[test]
#[ignore = "operator-initiated IB paper-account integration (ATP_RUN_INTEGRATION=1); binds fixed port 4002"]
fn paper_account_round_trip() {
    // #[ignore] keeps this out of the default (parallel-agent) run; once the operator
    // explicitly invokes it (--ignored / by name) it is the SRS-EXE-006 flip gate, so
    // a missing env gate must FAIL CLOSED — never return a vacuous green that looks
    // like the IB paper account was exercised when nothing ran.
    assert_eq!(
        std::env::var("ATP_RUN_INTEGRATION").as_deref(),
        Ok("1"),
        "paper_account_round_trip is the SRS-EXE-006 operator flip gate: run it with \
         ATP_RUN_INTEGRATION=1 against a headless IB paper account (port 4002). Refusing \
         to report success without actually exercising IB.",
    );
    let config = IbConnectionConfig::from_env(101).expect("valid ATP_IB_* configuration");
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
        .subscribe_market_data(quotes("AAPL"))
        .expect("paper account confirms a market-data subscription");
    let hist = adapter
        .historical_data(daily("AAPL"))
        .expect("paper account returns historical bars");
    assert!(
        !hist.bars.is_empty(),
        "historical retrieval returned no bars"
    );
    adapter
        .account_status()
        .expect("paper account returns account status");
    adapter
        .positions()
        .expect("paper account returns positions");
}
