//! SRS-EXE-009 / SyRS SYS-90, NFR-R3, NFR-R4 — durable order-intent **outbox** and
//! restart reconciliation.
//!
//! Fault-injection coverage over the **public** API of `atp_execution::outbox`. The
//! restart shape is `commit_intent -> save_to_path -> (process dies) ->
//! load_from_path -> reconcile` against a scripted broker snapshot (the mocked IB),
//! asserting the four acceptance criteria: an intent is durable before submission;
//! an acknowledged (bound) intent is never resubmitted; an unacknowledged intent is
//! adopted (if the broker has it) or resubmitted (only if the broker provably never
//! received it); and entries are retained until a terminal state is observed. Every
//! ambiguous path fails closed toward *not* resubmitting a possibly-live order, and
//! a corrupt / missing snapshot fails closed with no partial state.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use atp_execution::outbox::{
    reconcile, BrokerOpenOrder, BrokerOpenOrderSnapshot, ConflictKind, OrderOutbox, OutboxSnapshot,
    SnapshotCoverage,
};
use atp_types::{
    AssetClass, ClientCorrelationId, OrderErrorCategory, OrderKey, OrderSide, OrderState,
    OrderSubmission, OrderType, StrategyId,
};

static DIR_SEQ: AtomicU64 = AtomicU64::new(0);
fn temp_dir() -> PathBuf {
    let seq = DIR_SEQ.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("atp-exe009-it-{}-{}", std::process::id(), seq));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn corr(id: &str) -> ClientCorrelationId {
    ClientCorrelationId::new(id).expect("non-empty id")
}

fn key(strat: &str, id: &str) -> OrderKey {
    OrderKey::new(StrategyId::new(strat), corr(id))
}

fn submission(strat: &str, symbol: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(strat),
        symbol: symbol.to_string(),
        quantity,
        asset_class: AssetClass::Equity,
        side: OrderSide::Buy,
        order_type: OrderType::Market,
    }
}

fn broker(strat: &str, id: &str, broker_id: &str, state: OrderState) -> BrokerOpenOrder {
    BrokerOpenOrder {
        key: key(strat, id),
        broker_order_id: broker_id.to_string(),
        state,
    }
}

/// Persist an outbox to `dir`, then reload it from disk = the restart boundary.
fn restart(outbox: OrderOutbox, dir: &std::path::Path) -> OrderOutbox {
    OutboxSnapshot::capture(outbox).save_to_path(dir).unwrap();
    OutboxSnapshot::load_from_path(dir)
        .expect("reload after restart")
        .into_outbox()
}

#[test]
fn srs_exe_009_write_ahead_intent_is_durable_before_submission() {
    // AC bullet 1: the intent is durably committed BEFORE any submission/ack.
    let dir = temp_dir();
    let mut outbox = OrderOutbox::new();
    let k = outbox
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap();

    let reloaded = restart(outbox, &dir);
    let entry = reloaded
        .entry(&k)
        .expect("committed intent survived restart");
    assert_eq!(entry.state(), OrderState::PendingSubmit);
    assert!(!entry.is_bound(), "a pre-submit intent must be unbound");
}

#[test]
fn srs_exe_009_bound_intent_not_resubmitted_after_restart() {
    // AC bullet 3: a replayed intent that already has an acknowledged broker id is
    // NOT resubmitted — even when the broker's (open-only) view no longer shows it.
    let dir = temp_dir();
    let mut outbox = OrderOutbox::new();
    let k = outbox
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap();
    outbox.bind_ack(&k, "ib-100").unwrap();

    let reloaded = restart(outbox, &dir);
    let plan = reconcile(
        &reloaded,
        &BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenOnly),
    );
    assert_eq!(plan.skip_bound, vec![k.clone()]);
    assert!(
        plan.resubmit.is_empty(),
        "a bound intent must never be resubmitted"
    );
}

#[test]
fn srs_exe_009_unacked_intent_adopted_when_broker_has_it() {
    // AC bullet 2: on restart, an unacknowledged intent the broker already holds is
    // adopted (bound to the broker's id), not resubmitted.
    let dir = temp_dir();
    let mut outbox = OrderOutbox::new();
    let k = outbox
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap();

    let reloaded = restart(outbox, &dir);
    let plan = reconcile(
        &reloaded,
        &BrokerOpenOrderSnapshot::new(
            vec![broker("live-1", "c-1", "ib-777", OrderState::Acked)],
            SnapshotCoverage::OpenOnly,
        ),
    );
    assert_eq!(plan.adopt_ack, vec![(k, "ib-777".to_string())]);
    assert!(plan.resubmit.is_empty());
}

#[test]
fn srs_exe_009_resubmit_only_under_full_coverage() {
    // The crash-window decision: an unacknowledged intent absent from the broker view
    // is resubmitted ONLY when the view is complete enough (open + recently-completed)
    // to prove it never landed; an open-only view is ambiguous -> never resubmitted.
    let dir = temp_dir();
    let mut outbox = OrderOutbox::new();
    let k = outbox
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap();
    let reloaded = restart(outbox, &dir);

    let full = reconcile(
        &reloaded,
        &BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenAndRecentlyCompleted),
    );
    assert_eq!(full.resubmit, vec![k.clone()]);
    assert!(full.unresolved.is_empty());

    let open_only = reconcile(
        &reloaded,
        &BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenOnly),
    );
    assert!(
        open_only.resubmit.is_empty(),
        "an open-only view must not trigger a resubmit"
    );
    assert_eq!(open_only.unresolved.len(), 1);
    assert!(matches!(
        open_only.unresolved[0].kind,
        ConflictKind::UnverifiableSubmitWindow
    ));
}

#[test]
fn srs_exe_009_id_conflict_never_resubmits() {
    // A bound intent whose broker id disagrees with the broker's report is surfaced
    // as unresolved and NEVER resubmitted (auto-resubmitting could double a live order).
    let dir = temp_dir();
    let mut outbox = OrderOutbox::new();
    let k = outbox
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap();
    outbox.bind_ack(&k, "ib-1").unwrap();
    let reloaded = restart(outbox, &dir);

    let plan = reconcile(
        &reloaded,
        &BrokerOpenOrderSnapshot::new(
            vec![broker("live-1", "c-1", "ib-DIFFERENT", OrderState::Acked)],
            SnapshotCoverage::OpenAndRecentlyCompleted,
        ),
    );
    assert!(plan.resubmit.is_empty());
    assert!(plan.skip_bound.is_empty());
    assert_eq!(plan.unresolved.len(), 1);
    assert!(matches!(
        plan.unresolved[0].kind,
        ConflictKind::BrokerIdMismatch { .. }
    ));
}

#[test]
fn srs_exe_009_duplicate_broker_rows_never_adopted() {
    // The duplicate-live-order hazard: if the broker reports TWO orders for one
    // correlation key, reconciliation must NOT collapse them into a single
    // adopt/skip/resubmit (which would silently mask a second live order) — it
    // surfaces the duplicate as unresolved for operator resolution.
    let dir = temp_dir();
    let mut outbox = OrderOutbox::new();
    outbox
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap();
    let reloaded = restart(outbox, &dir);

    let plan = reconcile(
        &reloaded,
        &BrokerOpenOrderSnapshot::new(
            vec![
                broker("live-1", "c-1", "ib-A", OrderState::Acked),
                broker("live-1", "c-1", "ib-B", OrderState::Acked),
            ],
            SnapshotCoverage::OpenAndRecentlyCompleted,
        ),
    );
    assert!(plan.adopt_ack.is_empty());
    assert!(plan.resubmit.is_empty());
    assert!(plan.skip_bound.is_empty());
    assert_eq!(plan.unresolved.len(), 1);
    assert!(matches!(
        plan.unresolved[0].kind,
        ConflictKind::DuplicateBrokerRows { .. }
    ));
}

#[test]
fn srs_exe_009_terminal_state_synced_from_broker() {
    // A bound intent the broker reports FILLED (ACKED -> FILLED is a legal edge) is
    // synced via mark_terminal, so retention can then release it.
    let dir = temp_dir();
    let mut outbox = OrderOutbox::new();
    let k = outbox
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap();
    outbox.bind_ack(&k, "ib-1").unwrap();
    let reloaded = restart(outbox, &dir);

    let plan = reconcile(
        &reloaded,
        &BrokerOpenOrderSnapshot::new(
            vec![broker("live-1", "c-1", "ib-1", OrderState::Filled)],
            SnapshotCoverage::OpenAndRecentlyCompleted,
        ),
    );
    assert_eq!(plan.skip_bound, vec![k.clone()]);
    assert_eq!(plan.mark_terminal, vec![(k, OrderState::Filled)]);
}

#[test]
fn srs_exe_009_retained_until_terminal() {
    // AC bullet 4: entries are retained until a terminal state is observed, then
    // released — across a restart.
    let dir = temp_dir();
    let mut outbox = OrderOutbox::new();
    let filled = outbox
        .commit_intent(corr("c-fill"), &submission("live-1", "AAPL", 10))
        .unwrap();
    let working = outbox
        .commit_intent(corr("c-work"), &submission("live-1", "MSFT", 5))
        .unwrap();
    outbox.bind_ack(&filled, "ib-1").unwrap();
    outbox.bind_ack(&working, "ib-2").unwrap();
    outbox.observe_state(&filled, OrderState::Filled).unwrap();

    // Both survive the restart (retention holds across persistence)...
    let mut reloaded = restart(outbox, &dir);
    assert_eq!(reloaded.len(), 2);
    // ...then prune releases exactly the terminal one.
    let pruned = reloaded.prune_terminal();
    assert_eq!(pruned, vec![filled.clone()]);
    assert!(!reloaded.contains(&filled));
    assert!(reloaded.contains(&working));
}

#[test]
fn srs_exe_009_no_duplicate_commit_after_restart() {
    // The idempotency spine mirrors SRS-EXE-005: after a restart the reloaded outbox
    // rejects a re-committed correlation id as a duplicate.
    let dir = temp_dir();
    let mut outbox = OrderOutbox::new();
    outbox
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap();
    let mut reloaded = restart(outbox, &dir);

    let err = reloaded
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap_err();
    assert_eq!(
        err.category,
        OrderErrorCategory::DuplicateClientCorrelationId
    );
    assert_eq!(reloaded.len(), 1);
}

#[test]
fn srs_exe_009_corrupt_snapshot_fails_closed() {
    // A corrupt-magic or tampered snapshot is rejected whole — no partial restore.
    let mut outbox = OrderOutbox::new();
    outbox
        .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
        .unwrap();
    let good = OutboxSnapshot::capture(outbox).serialize();

    let bad_magic = good.replacen("ATP-ORDER-OUTBOX-V1", "NOPE", 1);
    assert!(OutboxSnapshot::deserialize(&bad_magic).is_err());

    // Flip a structurally-valid byte (the quantity) -> checksum catches it.
    let tampered = good.replacen("\n10\n", "\n11\n", 1);
    assert_ne!(tampered, good);
    assert!(OutboxSnapshot::deserialize(&tampered).is_err());

    assert!(OutboxSnapshot::deserialize("").is_err());
}

#[test]
fn srs_exe_009_missing_snapshot_fails_closed() {
    // A missing store directory, and an existing directory with no snapshot file,
    // both fail closed — recovery never silently restores an empty outbox (which
    // would drop pending intents and could allow duplicate submissions).
    let missing = std::env::temp_dir().join(format!(
        "atp-exe009-absent-{}-{}",
        std::process::id(),
        DIR_SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = std::fs::remove_dir_all(&missing);
    assert!(OutboxSnapshot::load_from_path(&missing).is_err());

    let empty = temp_dir(); // exists, but no snapshot written
    assert!(OutboxSnapshot::load_from_path(&empty).is_err());
}
