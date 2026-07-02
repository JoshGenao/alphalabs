//! SRS-EXE-005 / SyRS SYS-90, NFR-R3 — durable live-execution-state persistence
//! and restart recovery.
//!
//! Integration coverage over the **public** API of `atp_execution::live_state`:
//! a captured snapshot survives an execution-engine restart (serialize/persist →
//! deserialize/load) reproducing the full enumerated state, the restored ledger
//! rejects a duplicate submission (the AC's "without duplicate submissions"), the
//! warm-up is re-executed on restart, the NFR-R3 restore deadline is enforced,
//! and a corrupt / tampered snapshot fails closed with no partial state.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use atp_execution::live_state::{
    recover, recover_from_path, AccountEquitySnapshot, FillEventRecord, LiveExecutionState,
    LiveStateSnapshot, PersistenceError, RecoveryConfig, RecoveryError, WarmUpError,
    WarmUpReexecutionPort,
};
use atp_types::{
    AssetClass, ClientCorrelationId, OrderErrorCategory, OrderKey, OrderLedger, OrderSide,
    OrderState, OrderSubmission, OrderType, StrategyId,
};

fn corr(id: &str) -> ClientCorrelationId {
    ClientCorrelationId::new(id).expect("non-empty id")
}

fn key(strat: &str, id: &str) -> OrderKey {
    OrderKey::new(StrategyId::new(strat), corr(id))
}

fn submission(strat: &str, symbol: &str, qty: i64, order_type: OrderType) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(strat),
        symbol: symbol.to_string(),
        quantity: qty,
        asset_class: AssetClass::Equity,
        side: OrderSide::Buy,
        order_type,
    }
}

fn sample_ledger() -> OrderLedger {
    let mut ledger = OrderLedger::new();
    ledger
        .submit(
            corr("c-1"),
            &submission("strat-1", "AAPL", 10, OrderType::Market),
        )
        .unwrap();
    ledger
        .transition(&key("strat-1", "c-1"), OrderState::PendingSubmit)
        .unwrap();
    ledger
        .transition(&key("strat-1", "c-1"), OrderState::Acked)
        .unwrap();
    ledger
        .submit(
            corr("c-2"),
            &submission(
                "strat-1",
                "MSFT",
                5,
                OrderType::Limit {
                    limit_price_minor: 30_000,
                },
            ),
        )
        .unwrap();
    ledger
}

fn sample_state() -> LiveExecutionState {
    LiveExecutionState::new(sample_ledger())
        .with_broker_id(key("strat-1", "c-1"), "IB-8001")
        .unwrap()
        .with_fill(FillEventRecord::new(key("strat-1", "c-1"), 1, 10, 19_050).unwrap())
        .unwrap()
        .with_position("AAPL", 10)
        .unwrap()
        .with_equity(AccountEquitySnapshot::new(1_000_000, 250_000))
        .with_user_state_json(r#"{"phase":"scaling"}"#)
        .unwrap()
}

struct RecordingWarmUp {
    calls: std::cell::RefCell<Vec<String>>,
}
impl WarmUpReexecutionPort for RecordingWarmUp {
    fn reexecute_warmup(&self, strategy: &StrategyId) -> Result<(), WarmUpError> {
        self.calls.borrow_mut().push(strategy.as_str().to_string());
        Ok(())
    }
}

struct FailingWarmUp;
impl WarmUpReexecutionPort for FailingWarmUp {
    fn reexecute_warmup(&self, strategy: &StrategyId) -> Result<(), WarmUpError> {
        Err(WarmUpError::new(strategy.as_str(), "warm-up unavailable"))
    }
}

static DIR_SEQ: AtomicU64 = AtomicU64::new(0);
fn temp_dir() -> PathBuf {
    let seq = DIR_SEQ.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("atp-exe005-it-{}-{}", std::process::id(), seq));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn srs_exe_005_round_trip_reproduces_full_live_state() {
    let snapshot = LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default());
    let serialized = snapshot.serialize();
    let restored = LiveStateSnapshot::deserialize(&serialized).unwrap();
    let state = restored.state();

    assert_eq!(state.orders().len(), 2);
    assert_eq!(
        state.orders().state(&key("strat-1", "c-1")).unwrap(),
        OrderState::Acked
    );
    assert_eq!(state.broker_id(&key("strat-1", "c-1")), Some("IB-8001"));
    assert_eq!(state.fills().len(), 1);
    assert_eq!(state.open_position("AAPL"), Some(10));
    assert_eq!(state.equity().net_liquidation_minor(), 1_250_000);
    assert_eq!(state.user_state_json(), r#"{"phase":"scaling"}"#);
    // Faithful: re-serializing the restored snapshot is byte-identical.
    assert_eq!(restored.serialize(), serialized);
}

#[test]
fn srs_exe_005_serialization_is_deterministic() {
    let a = LiveExecutionState::new(sample_ledger())
        .with_position("AAPL", 10)
        .unwrap()
        .with_position("MSFT", -3)
        .unwrap();
    let b = LiveExecutionState::new(sample_ledger())
        .with_position("MSFT", -3)
        .unwrap()
        .with_position("AAPL", 10)
        .unwrap();
    assert_eq!(
        LiveStateSnapshot::capture(a, RecoveryConfig::default()).serialize(),
        LiveStateSnapshot::capture(b, RecoveryConfig::default()).serialize()
    );
}

#[test]
fn srs_exe_005_no_duplicate_submission_after_restart() {
    // Persist (c-1 ACKED), then "restart": serialize -> deserialize -> resume.
    let snapshot = LiveStateSnapshot::capture(
        LiveExecutionState::new(sample_ledger()),
        RecoveryConfig::default(),
    );
    let restored = LiveStateSnapshot::deserialize(&snapshot.serialize()).unwrap();
    let mut ledger = restored.into_state().into_ledger();

    let err = ledger
        .submit(
            corr("c-1"),
            &submission("strat-1", "AAPL", 10, OrderType::Market),
        )
        .unwrap_err();
    assert_eq!(
        err.category,
        OrderErrorCategory::DuplicateClientCorrelationId
    );
    assert_eq!(
        ledger.state(&key("strat-1", "c-1")).unwrap(),
        OrderState::Acked
    );
    assert_eq!(ledger.len(), 2);
}

#[test]
fn srs_exe_005_warmup_reexecuted_on_restart() {
    let mut ledger = sample_ledger();
    ledger
        .submit(
            corr("z"),
            &submission("strat-2", "TSLA", 1, OrderType::Market),
        )
        .unwrap();
    let snapshot =
        LiveStateSnapshot::capture(LiveExecutionState::new(ledger), RecoveryConfig::default());
    let warmup = RecordingWarmUp {
        calls: std::cell::RefCell::new(Vec::new()),
    };
    let outcome = recover(snapshot, &warmup, Duration::from_secs(1)).unwrap();
    let warmed: Vec<&str> = outcome
        .warmup_reexecuted()
        .iter()
        .map(StrategyId::as_str)
        .collect();
    assert_eq!(warmed, vec!["strat-1", "strat-2"]);
}

#[test]
fn srs_exe_005_restore_deadline_exceeded_fails_closed() {
    let snapshot = LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default());
    let warmup = RecordingWarmUp {
        calls: std::cell::RefCell::new(Vec::new()),
    };
    let err = recover(snapshot, &warmup, Duration::from_secs(61)).unwrap_err();
    assert!(matches!(
        err,
        RecoveryError::RestoreDeadlineExceeded {
            deadline_secs: 60,
            ..
        }
    ));
    assert!(warmup.calls.borrow().is_empty());
}

#[test]
fn srs_exe_005_warmup_failure_aborts_recovery() {
    let snapshot = LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default());
    let err = recover(snapshot, &FailingWarmUp, Duration::from_secs(1)).unwrap_err();
    assert!(matches!(err, RecoveryError::WarmUp(_)));
}

#[test]
fn srs_exe_005_corrupt_snapshot_fails_closed() {
    let serialized =
        LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default()).serialize();
    // A truncated blob restores nothing.
    assert!(LiveStateSnapshot::deserialize(&serialized[..serialized.len() / 2]).is_err());
    // A corrupt magic header is rejected.
    let mut bad_magic = serialized.clone();
    bad_magic.replace_range(0..3, "XXX");
    assert!(matches!(
        LiveStateSnapshot::deserialize(&bad_magic),
        Err(PersistenceError::CorruptSnapshot { .. })
    ));
}

#[test]
fn srs_exe_005_tampered_value_fails_closed() {
    // A structurally-valid byte change inside the body is caught by the checksum.
    let serialized =
        LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default()).serialize();
    let mut bytes = serialized.into_bytes();
    let idx = bytes.len() - 2;
    bytes[idx] = if bytes[idx] == b'0' { b'1' } else { b'0' };
    let tampered = String::from_utf8(bytes).unwrap();
    assert_eq!(
        LiveStateSnapshot::deserialize(&tampered).unwrap_err(),
        PersistenceError::ChecksumMismatch
    );
}

#[test]
fn srs_exe_005_end_to_end_disk_restart_no_duplicate() {
    // The fault-injection shape: persist -> (process dies) -> recover_from_path.
    let dir = temp_dir();
    LiveStateSnapshot::capture(
        LiveExecutionState::new(sample_ledger()),
        RecoveryConfig::default(),
    )
    .save_to_path(&dir)
    .unwrap();

    let warmup = RecordingWarmUp {
        calls: std::cell::RefCell::new(Vec::new()),
    };
    let outcome = recover_from_path(&dir, &warmup).unwrap();
    assert!(outcome.restore_elapsed() <= Duration::from_secs(60));
    assert!(!warmup.calls.borrow().is_empty());

    let mut ledger = outcome.into_snapshot().into_state().into_ledger();
    let err = ledger
        .submit(
            corr("c-1"),
            &submission("strat-1", "AAPL", 10, OrderType::Market),
        )
        .unwrap_err();
    assert_eq!(
        err.category,
        OrderErrorCategory::DuplicateClientCorrelationId
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn srs_exe_005_recovery_fails_closed_on_a_missing_snapshot() {
    // Both a missing directory AND an existing directory with no snapshot file
    // fail closed — recovery never silently restores empty state (which would drop
    // the ledger and could allow duplicate submissions after a lost file).
    let absent = std::env::temp_dir().join(format!("atp-exe005-absent-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&absent);
    assert!(matches!(
        LiveStateSnapshot::load_from_path(&absent),
        Err(PersistenceError::Io { .. })
    ));

    let empty_dir = temp_dir();
    assert!(matches!(
        LiveStateSnapshot::load_from_path(&empty_dir),
        Err(PersistenceError::Io { .. })
    ));
    // recover_from_path composes load_from_path, so it fails closed too.
    let warmup = RecordingWarmUp {
        calls: std::cell::RefCell::new(Vec::new()),
    };
    assert!(matches!(
        recover_from_path(&empty_dir, &warmup),
        Err(RecoveryError::Persistence(PersistenceError::Io { .. }))
    ));
    let _ = std::fs::remove_dir_all(&empty_dir);
}

#[test]
fn srs_exe_005_warmup_reexecutes_for_a_registered_strategy_without_orders() {
    // A live strategy with recovered positions but no active order is still warmed
    // up on restart (SRS-SDK-005), so it never resumes on cold indicators.
    let dir = temp_dir();
    let state = LiveExecutionState::new(OrderLedger::new())
        .with_live_strategy(&StrategyId::new("cold-strat"))
        .unwrap()
        .with_position("AAPL", 5)
        .unwrap();
    LiveStateSnapshot::capture(state, RecoveryConfig::default())
        .save_to_path(&dir)
        .unwrap();

    let warmup = RecordingWarmUp {
        calls: std::cell::RefCell::new(Vec::new()),
    };
    let outcome = recover_from_path(&dir, &warmup).unwrap();
    assert_eq!(outcome.restored_order_count(), 0);
    assert_eq!(*warmup.calls.borrow(), vec!["cold-strat"]);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn srs_exe_005_duplicate_fill_fails_closed() {
    // Two fills with the same (order, sequence) identity would double-count an
    // execution — the builder (and thus deserialize) rejects the duplicate.
    let state = LiveExecutionState::new(sample_ledger())
        .with_fill(FillEventRecord::new(key("strat-1", "c-1"), 1, 10, 19_050).unwrap())
        .unwrap();
    assert!(matches!(
        state.with_fill(FillEventRecord::new(key("strat-1", "c-1"), 1, 10, 19_050).unwrap()),
        Err(PersistenceError::DuplicateRecord { .. })
    ));
}

#[test]
fn srs_exe_005_user_state_must_be_a_json_object() {
    // The AC's "JSON-serializable state dictionary" is validated as a JSON object;
    // a non-JSON or non-object value fails closed rather than deferring the failure
    // to strategy restart.
    assert!(LiveExecutionState::new(sample_ledger())
        .with_user_state_json("not json")
        .is_err());
    assert!(LiveExecutionState::new(sample_ledger())
        .with_user_state_json("[1,2,3]")
        .is_err());
    assert!(LiveExecutionState::new(sample_ledger())
        .with_user_state_json(r#"{"positions":{"AAPL":10},"phase":"live"}"#)
        .is_ok());
}
