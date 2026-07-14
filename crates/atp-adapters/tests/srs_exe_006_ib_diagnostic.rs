//! SRS-EXE-006 — operator per-operation live diagnostic (NOT part of the flip
//! evidence; not covered by the paper-account evidence digest).
//!
//! Unlike `paper_account_round_trip` (which stops at the first failing op), this
//! runs each of the six operations independently over a single cached session
//! and prints a per-operation PASS/FAIL report, so the operator can see exactly
//! what works against the current IB Gateway state (e.g. whether historical data
//! succeeds while the Historical Data Farm is yellow/dormant).
//!
//! Operator-gated exactly like the flip test: `#[ignore]` + `ATP_RUN_INTEGRATION=1`
//! + `--features ib-live-transport`, against the paper account on port 4002.
//!
//! ```bash
//! ATP_RUN_INTEGRATION=1 cargo test -p atp-adapters --features ib-live-transport \
//!   --test srs_exe_006_ib_diagnostic -- --ignored --nocapture
//! ```

#![cfg(feature = "ib-live-transport")]

use atp_adapters::interactive_brokers::{
    IbAccountKind, IbConnectionConfig, IbGatewayConnection, TcpIbGateway,
};
use atp_adapters::{
    AssetClass, HistoricalDataRequest, MarketDataChannel, MarketDataSubscription, NormalizationMode,
};
use atp_types::{OrderSide, OrderSubmission, OrderType, StrategyId};

fn order(symbol: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new("diag-1"),
        symbol: symbol.to_string(),
        quantity,
        asset_class: atp_types::AssetClass::Equity,
        side: OrderSide::Buy,
        order_type: OrderType::Market,
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

fn report<T>(
    op: &str,
    result: Result<T, atp_adapters::interactive_brokers::IbApiError>,
    ok: &mut u32,
    detail: impl FnOnce(&T) -> String,
) {
    match result {
        Ok(value) => {
            *ok += 1;
            println!("  PASS  {op:<24} {}", detail(&value));
        }
        Err(err) => {
            println!("  FAIL  {op:<24} code={} msg={}", err.code, err.message);
        }
    }
}

#[test]
#[ignore = "operator per-op diagnostic (ATP_RUN_INTEGRATION=1); binds fixed port 4002"]
fn paper_account_per_operation_diagnostic() {
    assert_eq!(
        std::env::var("ATP_RUN_INTEGRATION").as_deref(),
        Ok("1"),
        "run with ATP_RUN_INTEGRATION=1 against a headless IB paper account (port 4002)",
    );
    let config = IbConnectionConfig::from_env(101).expect("valid ATP_IB_* configuration");
    let gateway = TcpIbGateway::new(config, IbAccountKind::Paper);

    println!("\n=== SRS-EXE-006 IB paper-account per-operation diagnostic ===");
    let mut ok = 0u32;

    let submit = gateway.submit_order(&order("AAPL", 1));
    let broker_order_id = submit.as_ref().ok().map(|r| r.broker_order_id.clone());
    report("submit_order", submit, &mut ok, |r| {
        format!("broker_order_id={}", r.broker_order_id)
    });

    match broker_order_id {
        Some(id) => report("cancel_order", gateway.cancel_order(&id), &mut ok, |_| {
            "cancelled".to_string()
        }),
        None => println!("  SKIP  cancel_order              (no order id from submit)"),
    }

    report(
        "subscribe_market_data",
        gateway.subscribe_market_data(&quotes("AAPL")),
        &mut ok,
        |r| format!("subscription_id={}", r.subscription_id),
    );

    // The op most affected by a yellow/dormant Historical Data Farm.
    report(
        "historical_data (1d)",
        gateway.historical_data(&daily("AAPL")),
        &mut ok,
        |r| format!("bars={}", r.bars.len()),
    );

    report("account_status", gateway.account_status(), &mut ok, |b| {
        format!("records={}", b.records)
    });

    report("positions", gateway.positions(), &mut ok, |b| {
        format!("records={}", b.records)
    });

    println!("=== {ok}/6 operations succeeded ===\n");
    // Diagnostic only — never fails the suite; the flip gate is
    // paper_account_round_trip, which requires ALL operations.
}
