//! SRS-ERR-001 LIVE broker-rejection gate — operator-initiated only.
//!
//! `err001_broker_envelope_cli` + `srs_err_001_broker_envelope_cli` prove the SyRS SYS-64
//! broker-validation envelope over the REAL adapter, REAL classifier, and REAL execution engine with
//! a scripted transport supplying the vendor `code` + `message` a socket would carry. That is
//! deterministic fixture verification of the CLASSIFICATION and the ENVELOPE.
//!
//! What it does NOT prove is that a real IB Gateway actually emits those codes for these conditions.
//! SRS-EXE-006's operator-attested `paper_account_round_trip` covers an ACCEPTED order round trip; no
//! test has yet observed a real IB *rejection* becoming a `StructuredOrderError`. This file is that
//! observation, and it is the gate that flips SRS-ERR-001 to `passes:true`.
//!
//! Deliberately a NEW file rather than an addition to
//! `crates/atp-adapters/tests/srs_exe_006_ib_adapter.rs`: `tools/ib_adapter_check.py::_code_digest`
//! SHA-256s that file together with the IB adapter module and its wire codec, so editing any of
//! them would invalidate the recorded IB paper-account evidence and flip the closed-green
//! SRS-EXE-006 red.
//!
//! Run it as:
//! ```text
//! ATP_RUN_INTEGRATION=1 cargo test -p atp-orchestrator \
//!     --test srs_err_001_broker_envelope_live --features ib-live-transport -- --ignored
//! ```

#![cfg(feature = "ib-live-transport")]

use atp_adapters::{IbAccountKind, IbConnectionConfig, TcpIbGateway};
use atp_execution::{ExecutionEngine, LiveDesignationConfirmation};
use atp_orchestrator::order_routing_wiring::{
    CollectingConnectivitySink, CollectingStaleDataSink, FreshMarketDataFixture,
    HealthyConnectivityFixture, IbBrokerageBridge,
};
use atp_types::{
    AssetClass, OrderErrorCategory, OrderSide, OrderSubmission, OrderType, StrategyId,
};

/// A syntactically well-formed symbol that has no security definition at IB, so the gateway must
/// reject it with code 200 rather than resting an order. Well-formed on purpose: it has to survive
/// `OrderSubmission::validate` so the rejection comes from the BROKER, not from local validation.
const NONEXISTENT_SYMBOL: &str = "ZZZZQQ";
const LIVE_STRATEGY: &str = "live-1";

fn submission(symbol: &str) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(LIVE_STRATEGY),
        symbol: symbol.to_string(),
        quantity: 1,
        asset_class: AssetClass::Equity,
        side: OrderSide::Buy,
        order_type: OrderType::Market,
    }
}

fn confirm(strategy: &str) -> LiveDesignationConfirmation {
    LiveDesignationConfirmation::from_operator(
        StrategyId::new(strategy),
        "operator confirmed live designation",
    )
    .expect("a non-empty acknowledgement yields a confirmation token")
}

/// AC verification — operator-initiated only. Drives the live [`TcpIbGateway`] against the headless
/// IB **paper** account (port 4002) and asserts a REAL broker rejection arrives as a complete
/// SRS-ERR-001 envelope. Skipped unless `ATP_RUN_INTEGRATION=1` AND run with `--ignored`, because the
/// IB paper account binds a fixed shared port (SyRS SYS-2e) and must not run in the parallel agent
/// pool.
#[test]
#[ignore = "operator-initiated IB paper-account integration (ATP_RUN_INTEGRATION=1); binds fixed port 4002"]
fn live_broker_rejection_carries_the_srs_err_001_envelope() {
    // #[ignore] keeps this out of the default (parallel-agent) run; once the operator explicitly
    // invokes it, it is the SRS-ERR-001 flip gate, so a missing env gate must FAIL CLOSED — never
    // return a vacuous green that looks like a real IB rejection was observed when nothing ran.
    assert_eq!(
        std::env::var("ATP_RUN_INTEGRATION").as_deref(),
        Ok("1"),
        "live_broker_rejection_carries_the_srs_err_001_envelope is the SRS-ERR-001 operator flip \
         gate: run it with ATP_RUN_INTEGRATION=1 against a headless IB paper account (port 4002). \
         Refusing to report success without actually exercising IB.",
    );

    let config = IbConnectionConfig::from_env(102).expect("valid ATP_IB_* configuration");
    let brokerage = IbBrokerageBridge::new(TcpIbGateway::new(config, IbAccountKind::Paper));

    // Route through the PRODUCTION boundary: `route_order` resolves live-ness from the engine-owned
    // designation registry, so this exercises the single-live invariant (SRS-EXE-001) rather than
    // supplying `StrategyMode::Live` as a caller argument and sidestepping it.
    let mut engine = ExecutionEngine::default();
    engine
        .designate(StrategyId::new(LIVE_STRATEGY), confirm(LIVE_STRATEGY))
        .expect("designating a single live strategy on a fresh engine succeeds");

    let original = submission(NONEXISTENT_SYMBOL);
    let result = engine.route_order(
        original.clone(),
        &brokerage,
        &HealthyConnectivityFixture,
        &CollectingConnectivitySink::default(),
        &FreshMarketDataFixture,
        &CollectingStaleDataSink::default(),
    );

    let err = match result {
        Err(err) => err,
        Ok(receipt) => panic!(
            "the IB paper account ACCEPTED an order for a nonexistent symbol \
             ({NONEXISTENT_SYMBOL}, broker_order_id {}); cancel it manually and investigate — the \
             rejection path was not exercised",
            receipt.broker_order_id
        ),
    };

    // The full SRS-ERR-001 acceptance criterion, over a REAL vendor rejection:
    // category + type + human-readable message + the original order parameters unchanged.
    assert_eq!(
        err.category,
        OrderErrorCategory::InvalidSymbol,
        "a real IB 'no security definition' rejection must classify as INVALID_SYMBOL; got {} \
         (message: {})",
        err.category.as_str(),
        err.message
    );
    assert!(
        !err.error_type.trim().is_empty(),
        "the envelope must carry a non-empty error type"
    );
    assert!(
        !err.message.trim().is_empty(),
        "the envelope must carry a human-readable message"
    );
    assert_eq!(
        err.original_order, original,
        "the envelope must round-trip the original order parameters unchanged"
    );
}
