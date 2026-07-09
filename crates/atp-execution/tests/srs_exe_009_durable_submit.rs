//! SRS-EXE-009 — the durable-submit seam, reached through the authority-gated public
//! entry [`atp_execution::ExecutionEngine::route_order_durably`]: a live order intent
//! is durably committed to the outbox BEFORE the broker is contacted, the broker id
//! is bound on acknowledgement, a synchronous rejection marks the intent terminal so
//! it is never resubmitted, and a NON-designated strategy is rejected before any
//! durable record or broker contact (the single-live-strategy invariant holds for the
//! durable path too).
//!
//! The headline is an ORDERING test: the broker stub asserts the durable outbox
//! snapshot file already exists on disk at the moment `submit_order` is called.

use std::cell::Cell;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use atp_execution::{
    AckFailure, BrokerageConnectivity, ConnectivityEventSink, DurableSubmitError, ExecutionEngine,
    LiveBrokerageSubmit, LiveDesignationConfirmation, MarketDataFreshnessProbe, OrderOutbox,
    OutboxSnapshot, SnapshotCoverage, StaleDataEventSink,
};
use atp_types::{
    AssetClass, ClientCorrelationId, ConnectivityEvent, ConnectivityState, MarketDataFreshness,
    OrderErrorCategory, OrderReceipt, OrderSide, OrderState, OrderSubmission, OrderType,
    StaleDataEvent, StrategyId, StructuredOrderError,
};

const OUTBOX_FILE: &str = "live_order_outbox.snapshot";

static DIR_SEQ: AtomicU64 = AtomicU64::new(0);
fn temp_dir() -> PathBuf {
    let seq = DIR_SEQ.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("atp-exe009-ds-{}-{}", std::process::id(), seq));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn corr(id: &str) -> ClientCorrelationId {
    ClientCorrelationId::new(id).expect("non-empty id")
}

fn confirm(strategy: &str) -> LiveDesignationConfirmation {
    LiveDesignationConfirmation::from_operator(
        StrategyId::new(strategy),
        "operator confirmed live designation",
    )
    .expect("non-empty acknowledgement yields a confirmation token")
}

/// An engine with `live-1` designated as the single live strategy.
fn engine_with_live() -> ExecutionEngine {
    let mut engine = ExecutionEngine::default();
    engine
        .designate(StrategyId::new("live-1"), confirm("live-1"))
        .expect("designation succeeds");
    engine
}

fn submission(symbol: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new("live-1"),
        symbol: symbol.to_string(),
        quantity,
        asset_class: AssetClass::Equity,
        side: OrderSide::Buy,
        order_type: OrderType::Market,
    }
}

fn key_for(correlation: &str) -> atp_types::OrderKey {
    atp_types::OrderKey::new(StrategyId::new("live-1"), corr(correlation))
}

/// A broker that asserts the durable outbox snapshot ALREADY exists on disk when it
/// is called — proving the write-ahead commit was persisted before the broker was
/// reached (the AC's "durably commit ... before submission to IB").
struct OrderingBroker {
    store_dir: PathBuf,
    calls: Cell<u32>,
}

impl LiveBrokerageSubmit for OrderingBroker {
    fn submit_order(
        &self,
        submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        assert!(
            self.store_dir.join(OUTBOX_FILE).exists(),
            "the durable outbox MUST be persisted before broker.submit_order is called"
        );
        self.calls.set(self.calls.get() + 1);
        Ok(OrderReceipt {
            broker_order_id: format!("ib-{}", submission.symbol),
        })
    }
}

/// A broker that ACCEPTS the order (a live order now exists) but first sabotages the
/// store directory so the subsequent post-ACK persist fails — simulating a
/// durability fault AFTER the broker acknowledged.
struct SabotageBroker {
    store_dir: PathBuf,
}

impl LiveBrokerageSubmit for SabotageBroker {
    fn submit_order(
        &self,
        submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        assert!(self.store_dir.join(OUTBOX_FILE).exists());
        std::fs::remove_dir_all(&self.store_dir).unwrap();
        std::fs::write(&self.store_dir, b"x").unwrap();
        Ok(OrderReceipt {
            broker_order_id: format!("ib-{}", submission.symbol),
        })
    }
}

/// A broker that panics if it is ever called — proves a rejected submission never
/// reaches the broker.
struct ForbiddenBroker;

impl LiveBrokerageSubmit for ForbiddenBroker {
    fn submit_order(
        &self,
        _submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        panic!("a rejected durable submission must never reach the broker");
    }
}

struct ConnectedProbe;
impl BrokerageConnectivity for ConnectedProbe {
    fn state(&self) -> ConnectivityState {
        ConnectivityState::Connected
    }
    fn request_reconnect(&self) {}
}

/// Connectivity gate closed — the inner ERR-2 gate rejects before the broker.
struct UnreachableProbe;
impl BrokerageConnectivity for UnreachableProbe {
    fn state(&self) -> ConnectivityState {
        ConnectivityState::Unreachable
    }
    fn request_reconnect(&self) {}
}

struct NoopConnectivityEvents;
impl ConnectivityEventSink for NoopConnectivityEvents {
    fn record(&self, _event: ConnectivityEvent) {}
}

struct AlwaysFresh;
impl MarketDataFreshnessProbe for AlwaysFresh {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        MarketDataFreshness::Fresh
    }
    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        0
    }
}

struct NoopStaleEvents;
impl StaleDataEventSink for NoopStaleEvents {
    fn record(&self, _event: StaleDataEvent) {}
}

fn load(dir: &Path) -> OrderOutbox {
    OutboxSnapshot::load_from_path(dir)
        .expect("outbox snapshot present")
        .into_outbox()
}

#[test]
fn srs_exe_009_durable_submit_persists_intent_before_broker() {
    let dir = temp_dir();
    let engine = engine_with_live();
    let mut outbox = OrderOutbox::new();
    let broker = OrderingBroker {
        store_dir: dir.clone(),
        calls: Cell::new(0),
    };

    let receipt = engine
        .route_order_durably(
            &mut outbox,
            &dir,
            corr("c-1"),
            submission("AAPL", 10),
            &broker,
            &ConnectedProbe,
            &NoopConnectivityEvents,
            &AlwaysFresh,
            &NoopStaleEvents,
        )
        .expect("a designated live order submits durably");

    assert_eq!(receipt.broker_order_id, "ib-AAPL");
    assert_eq!(broker.calls.get(), 1);

    let key = key_for("c-1");
    let entry = outbox.entry(&key).expect("intent tracked");
    assert_eq!(entry.state(), OrderState::Acked);
    assert_eq!(entry.broker_order_id(), Some("ib-AAPL"));
    let reloaded = load(&dir);
    assert_eq!(reloaded.broker_order_id(&key), Some("ib-AAPL"));
}

#[test]
fn srs_exe_009_durable_submit_rejects_non_designated_before_any_record() {
    // The single-live-strategy invariant on the durable path: a strategy that is NOT
    // the designated live strategy is rejected BEFORE any outbox mutation or broker
    // contact — it cannot obtain a durable intent, let alone reach IB.
    let dir = temp_dir();
    let engine = ExecutionEngine::default(); // NO live designation
    let mut outbox = OrderOutbox::new();

    let err = engine
        .route_order_durably(
            &mut outbox,
            &dir,
            corr("c-1"),
            submission("AAPL", 10),
            &ForbiddenBroker,
            &ConnectedProbe,
            &NoopConnectivityEvents,
            &AlwaysFresh,
            &NoopStaleEvents,
        )
        .expect_err("a non-designated strategy is rejected");
    match err {
        DurableSubmitError::Rejected(structured) => {
            assert_eq!(
                structured.category,
                OrderErrorCategory::NonLiveStrategySubmission
            );
        }
        other => panic!("expected an authority rejection, got {other:?}"),
    }
    assert!(
        outbox.is_empty(),
        "a non-designated rejection must not commit any durable intent"
    );
    assert!(
        OutboxSnapshot::load_from_path(&dir).is_err(),
        "no durable snapshot must be written for a non-designated rejection"
    );
}

#[test]
fn srs_exe_009_durable_submit_gate_rejection_is_never_resubmittable() {
    // A designated strategy whose submission passes the authority gate but fails the
    // inner ERR-2 connectivity gate: the intent (write-ahead committed) is durably
    // marked REJECTED so a restart never resubmits it, and the broker is never reached.
    let dir = temp_dir();
    let engine = engine_with_live();
    let mut outbox = OrderOutbox::new();

    let err = engine
        .route_order_durably(
            &mut outbox,
            &dir,
            corr("c-1"),
            submission("AAPL", 10),
            &ForbiddenBroker,
            &UnreachableProbe, // ERR-2 gate closed → rejected before the broker
            &NoopConnectivityEvents,
            &AlwaysFresh,
            &NoopStaleEvents,
        )
        .expect_err("a connectivity-blocked live order is rejected");
    assert!(matches!(err, DurableSubmitError::Rejected(_)));

    let key = key_for("c-1");
    let reloaded = load(&dir);
    assert_eq!(
        reloaded.entry(&key).map(|e| e.state()),
        Some(OrderState::Rejected),
        "the durable snapshot must record REJECTED, not a resubmittable PENDING_SUBMIT"
    );
    let plan = atp_execution::reconcile(
        &reloaded,
        &atp_execution::BrokerOpenOrderSnapshot::new(
            vec![],
            SnapshotCoverage::OpenAndRecentlyCompleted,
        ),
    );
    assert!(
        plan.resubmit.is_empty(),
        "a durably-rejected order must never be resubmitted on restart"
    );
}

#[test]
fn srs_exe_009_durable_submit_rejects_duplicate_correlation_id() {
    let dir = temp_dir();
    let engine = engine_with_live();
    let mut outbox = OrderOutbox::new();
    let broker = OrderingBroker {
        store_dir: dir.clone(),
        calls: Cell::new(0),
    };

    engine
        .route_order_durably(
            &mut outbox,
            &dir,
            corr("c-1"),
            submission("AAPL", 10),
            &broker,
            &ConnectedProbe,
            &NoopConnectivityEvents,
            &AlwaysFresh,
            &NoopStaleEvents,
        )
        .unwrap();

    let err = engine
        .route_order_durably(
            &mut outbox,
            &dir,
            corr("c-1"),
            submission("AAPL", 10),
            &broker,
            &ConnectedProbe,
            &NoopConnectivityEvents,
            &AlwaysFresh,
            &NoopStaleEvents,
        )
        .expect_err("a duplicate correlation id is rejected");
    match err {
        DurableSubmitError::Rejected(structured) => assert_eq!(
            structured.category,
            OrderErrorCategory::DuplicateClientCorrelationId
        ),
        other => panic!("expected a duplicate rejection, got {other:?}"),
    }
    assert_eq!(broker.calls.get(), 1, "the broker is contacted only once");
}

#[test]
fn srs_exe_009_durable_submit_ack_persist_failure_is_distinct_and_carries_receipt() {
    // A durability fault AFTER the broker accepted the order must be AckNotDurable (a
    // live order EXISTS — carry the receipt), NOT the safe pre-broker
    // WriteAheadPersistence — so a caller never blind-retries a live order.
    let dir = temp_dir();
    let engine = engine_with_live();
    let mut outbox = OrderOutbox::new();

    let err = engine
        .route_order_durably(
            &mut outbox,
            &dir,
            corr("c-1"),
            submission("AAPL", 10),
            &SabotageBroker {
                store_dir: dir.clone(),
            },
            &ConnectedProbe,
            &NoopConnectivityEvents,
            &AlwaysFresh,
            &NoopStaleEvents,
        )
        .expect_err("a post-ACK persistence fault surfaces AckNotDurable");
    match err {
        DurableSubmitError::AckNotDurable { receipt, source } => {
            assert_eq!(
                receipt.broker_order_id, "ib-AAPL",
                "the live order id must be preserved for reconciliation"
            );
            assert!(matches!(source, AckFailure::Persistence(_)));
        }
        other => panic!("expected AckNotDurable, got {other:?}"),
    }
}

#[test]
fn srs_exe_009_durable_submit_failed_write_ahead_does_not_poison_outbox() {
    // If the write-ahead durable write fails, the broker must NEVER be consulted AND
    // the caller's in-memory outbox must be left untouched, and nothing durable
    // written.
    let base = temp_dir();
    let file = base.join("not-a-directory");
    std::fs::write(&file, b"x").unwrap();
    let bad_store = file.join("outbox-store"); // parent is a file → create_dir_all fails

    let engine = engine_with_live();
    let mut outbox = OrderOutbox::new();
    let err = engine
        .route_order_durably(
            &mut outbox,
            &bad_store,
            corr("c-1"),
            submission("AAPL", 10),
            &ForbiddenBroker, // must never be reached
            &ConnectedProbe,
            &NoopConnectivityEvents,
            &AlwaysFresh,
            &NoopStaleEvents,
        )
        .expect_err("a failed write-ahead durable write must fail closed");
    assert!(matches!(err, DurableSubmitError::WriteAheadPersistence(_)));
    assert!(
        outbox.is_empty(),
        "a failed write-ahead persist must not poison the in-memory outbox"
    );
    assert!(
        OutboxSnapshot::load_from_path(&bad_store).is_err(),
        "no durable snapshot must exist after a failed write-ahead"
    );
}

#[test]
fn srs_exe_009_durable_submit_invalid_order_cannot_poison_recovery() {
    // An invalid submission must be rejected BEFORE any durable write — otherwise a
    // persisted invalid order would fail the fail-closed restore and brick the WHOLE
    // outbox. Route a VALID order (persisted), then an INVALID one, then reload: the
    // valid entry must still recover and the invalid one leaves no trace.
    let dir = temp_dir();
    let engine = engine_with_live();
    let mut outbox = OrderOutbox::new();

    engine
        .route_order_durably(
            &mut outbox,
            &dir,
            corr("c-good"),
            submission("AAPL", 10),
            &OrderingBroker {
                store_dir: dir.clone(),
                calls: Cell::new(0),
            },
            &ConnectedProbe,
            &NoopConnectivityEvents,
            &AlwaysFresh,
            &NoopStaleEvents,
        )
        .expect("the valid order lands durably");

    // A non-positive quantity is structurally invalid.
    let invalid = OrderSubmission {
        quantity: 0,
        ..submission("MSFT", 1)
    };
    let err = engine
        .route_order_durably(
            &mut outbox,
            &dir,
            corr("c-bad"),
            invalid,
            &ForbiddenBroker, // must never be reached
            &ConnectedProbe,
            &NoopConnectivityEvents,
            &AlwaysFresh,
            &NoopStaleEvents,
        )
        .expect_err("an invalid order is rejected before any durable write");
    assert!(matches!(err, DurableSubmitError::Rejected(_)));
    assert!(
        !outbox.contains(&key_for("c-bad")),
        "an invalid order must leave no durable intent"
    );
    assert_eq!(outbox.len(), 1);

    // The durable snapshot still loads and the valid entry recovers.
    let reloaded = load(&dir);
    assert!(
        reloaded.contains(&key_for("c-good")),
        "the valid entry must still recover after an invalid order was rejected"
    );
    assert!(!reloaded.contains(&key_for("c-bad")));
}
