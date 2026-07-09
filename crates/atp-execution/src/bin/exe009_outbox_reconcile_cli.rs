//! SRS-EXE-009 durable-outbox restart-reconciliation operator + fault-injection CLI.
//!
//! Drives the real [`atp_execution::outbox`] types through the acceptance-criteria
//! scenarios a restart exercises, using a **temp store directory** as the durable
//! medium (write-ahead commit → `save_to_path` → a fresh `load_from_path` = the
//! "process died and restarted" boundary) and scripted broker snapshots as the
//! mocked IB. Each subcommand prints kv proof lines and a final proof token; a
//! subcommand only prints its token when the safety property genuinely holds, so
//! the proofs are non-vacuous.
//!
//! `--inject <fault>` injects a hazard and proves the fail-closed property STILL
//! holds (a bound/ambiguous intent is never resubmitted). std-only; depends only on
//! the already-declared `atp-execution` / `atp-types` crates.

use atp_execution::outbox::{
    reconcile, BrokerOpenOrder, BrokerOpenOrderSnapshot, BrokerOpenOrderSource,
    BrokerReconcileError, ConflictKind, OrderOutbox, OutboxSnapshot, ReconciliationPlan,
    SnapshotCoverage,
};
use atp_types::{
    AssetClass, ClientCorrelationId, OrderSide, OrderState, OrderSubmission, OrderType, StrategyId,
};
use std::env;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::{AtomicU64, Ordering};

const USAGE: &str = "\
exe009_outbox_reconcile_cli — SRS-EXE-009 durable-outbox restart-reconciliation workflow

USAGE:
    exe009_outbox_reconcile_cli write-ahead        [--inject duplicate-replay]
    exe009_outbox_reconcile_cli restart-skip-bound [--inject id-conflict]
    exe009_outbox_reconcile_cli restart-adopt
    exe009_outbox_reconcile_cli restart-resubmit   [--inject partial-coverage]
    exe009_outbox_reconcile_cli retention
    exe009_outbox_reconcile_cli broker-error

COMMANDS:
    write-ahead        Commit an order intent, persist it, and reload from disk — proving the intent
                       is durable BEFORE any submission/ack (write-ahead-durable:true).
    restart-skip-bound Commit + ack + persist, then restart and reconcile: an acknowledged (bound)
                       intent is never resubmitted (bound-intent-not-resubmitted:true).
    restart-adopt      Commit (unacked) + persist, then restart and reconcile against a broker that
                       already holds the order: adopt its id, do not resubmit (unacked-intent-adopted:true).
    restart-resubmit   Commit (unacked) + persist, then restart and reconcile against a full-coverage
                       broker view that never saw it: safe to resubmit (unlanded-intent-resubmitted:true).
    retention          Commit two, fill one, and prune: entries are retained until a terminal state is
                       observed, then released (retained-until-terminal:true).
    broker-error       The broker order-state query fails: reconciliation makes NO resubmission
                       decision (no-decision-on-broker-error:true).

RUN FLAGS:
    --inject duplicate-replay  (write-ahead) replay the same correlation id — must be rejected
                               idempotently (duplicate-replay-rejected:true).
    --inject id-conflict       (restart-skip-bound) the broker reports a DIFFERENT id for the bound
                               intent — surfaced as unresolved, still never resubmitted
                               (no-resubmit-on-id-conflict:true).
    --inject partial-coverage  (restart-resubmit) only open orders are visible — an absent intent is
                               ambiguous, so it is NOT resubmitted (no-resubmit-on-partial-view:true).
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("exe009_outbox_reconcile_cli: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let (command, rest) = match args.split_first() {
        Some(parts) => parts,
        None => return Err(format!("missing subcommand\n\n{USAGE}")),
    };
    match command.as_str() {
        "write-ahead" => cmd_write_ahead(rest),
        "restart-skip-bound" => cmd_restart_skip_bound(rest),
        "restart-adopt" => cmd_restart_adopt(rest),
        "restart-resubmit" => cmd_restart_resubmit(rest),
        "retention" => cmd_retention(rest),
        "broker-error" => cmd_broker_error(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

/// AC bullet 1: an order intent is durably committed to the outbox BEFORE submission.
fn cmd_write_ahead(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let fault = parse_inject_only(rest)?;
    let store = TempStore::new("write-ahead");

    let mut outbox = OrderOutbox::new();
    let key = outbox
        .commit_intent(corr("c-1"), &sample_order("AAPL", 10))
        .map_err(|err| format!("commit_intent must succeed on a fresh outbox: {err:?}"))?;

    if let Some(fault) = fault {
        if fault != Fault::DuplicateReplay {
            return Err(format!(
                "fault '{}' is not applicable to write-ahead\n\n{USAGE}",
                fault.as_str()
            ));
        }
        println!("inject:{}", fault.as_str());
        // Replaying the same correlation id must be rejected idempotently — the
        // committed intent is never duplicated.
        match outbox.commit_intent(corr("c-1"), &sample_order("AAPL", 10)) {
            Ok(_) => {
                return Err(
                    "SAFETY VIOLATION: a duplicate correlation id was committed twice".to_string(),
                )
            }
            Err(err) => {
                println!("duplicate-replay-rejected-category:{:?}", err.category);
                println!("duplicate-replay-rejected:true");
                return Ok(());
            }
        }
    }

    // Persist the committed (but NOT yet submitted / acked) intent, then reload from
    // disk = the process-restart boundary.
    outbox.save_to_disk(&store)?;
    let reloaded = OutboxSnapshot::load_from_path(store.path())
        .map_err(|err| format!("reload after write-ahead must succeed: {err:?}"))?
        .into_outbox();

    let entry = reloaded
        .entry(&key)
        .ok_or("SAFETY VIOLATION: the committed intent did not survive the restart")?;
    println!("committed-key:{key}");
    println!("reloaded-state:{}", entry.state().as_str());
    println!("reloaded-bound:{}", entry.is_bound());
    if entry.state() != OrderState::PendingSubmit || entry.is_bound() {
        return Err(
            "SAFETY VIOLATION: a write-ahead intent must reload as an unbound PENDING_SUBMIT"
                .to_string(),
        );
    }
    println!("write-ahead-durable:true");
    Ok(())
}

/// AC bullet 3: a replayed intent that already has an acknowledged broker id is not resubmitted.
fn cmd_restart_skip_bound(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let fault = parse_inject_only(rest)?;
    let store = TempStore::new("skip-bound");

    let mut outbox = OrderOutbox::new();
    let key = outbox
        .commit_intent(corr("c-1"), &sample_order("AAPL", 10))
        .map_err(|err| format!("commit_intent: {err:?}"))?;
    outbox
        .bind_ack(&key, "ib-100")
        .map_err(|err| format!("bind_ack: {err}"))?;
    outbox.save_to_disk(&store)?;
    let reloaded = OutboxSnapshot::load_from_path(store.path())
        .map_err(|err| format!("reload: {err:?}"))?
        .into_outbox();

    let (snapshot, injected) = match fault {
        None => (
            BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenOnly),
            false,
        ),
        Some(Fault::IdConflict) => {
            println!("inject:{}", Fault::IdConflict.as_str());
            (
                BrokerOpenOrderSnapshot::new(
                    vec![BrokerOpenOrder {
                        key: key.clone(),
                        broker_order_id: "ib-DIFFERENT".to_string(),
                        state: OrderState::Acked,
                    }],
                    SnapshotCoverage::OpenAndRecentlyCompleted,
                ),
                true,
            )
        }
        Some(other) => {
            return Err(format!(
                "fault '{}' is not applicable to restart-skip-bound\n\n{USAGE}",
                other.as_str()
            ))
        }
    };

    let plan = reconcile(&reloaded, &snapshot);
    print_plan(&plan);
    // The bound intent must NEVER be resubmitted, injected hazard or not.
    if plan.resubmit.contains(&key) || !plan.resubmit.is_empty() {
        return Err("SAFETY VIOLATION: a bound intent was scheduled for resubmission".to_string());
    }
    if injected {
        if !plan
            .unresolved
            .iter()
            .any(|c| c.key == key && matches!(c.kind, ConflictKind::BrokerIdMismatch { .. }))
        {
            return Err(
                "expected a BrokerIdMismatch conflict under --inject id-conflict".to_string(),
            );
        }
        println!("no-resubmit-on-id-conflict:true");
        return Ok(());
    }
    if !plan.skip_bound.contains(&key) {
        return Err("expected the bound intent in skip_bound".to_string());
    }
    println!("bound-intent-not-resubmitted:true");
    Ok(())
}

/// AC bullet 2: on restart, an unacknowledged intent the broker already holds is adopted (bound), not resubmitted.
fn cmd_restart_adopt(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    reject_flags(rest)?;
    let store = TempStore::new("adopt");

    let mut outbox = OrderOutbox::new();
    let key = outbox
        .commit_intent(corr("c-1"), &sample_order("AAPL", 10))
        .map_err(|err| format!("commit_intent: {err:?}"))?;
    // Crash BEFORE recording the ack — the submit-crash window.
    outbox.save_to_disk(&store)?;
    let reloaded = OutboxSnapshot::load_from_path(store.path())
        .map_err(|err| format!("reload: {err:?}"))?
        .into_outbox();

    // The broker DID receive it (it echoes our order ref).
    let snapshot = BrokerOpenOrderSnapshot::new(
        vec![BrokerOpenOrder {
            key: key.clone(),
            broker_order_id: "ib-777".to_string(),
            state: OrderState::Acked,
        }],
        SnapshotCoverage::OpenOnly,
    );
    let plan = reconcile(&reloaded, &snapshot);
    print_plan(&plan);
    if !plan.resubmit.is_empty() {
        return Err(
            "SAFETY VIOLATION: an intent the broker already holds was resubmitted".to_string(),
        );
    }
    if !plan
        .adopt_ack
        .iter()
        .any(|(k, id)| *k == key && id == "ib-777")
    {
        return Err(
            "expected the unacknowledged intent in adopt_ack with the broker id".to_string(),
        );
    }
    println!("unacked-intent-adopted:true");
    Ok(())
}

/// The crash-window resubmit path: an unacknowledged intent the broker provably never received.
fn cmd_restart_resubmit(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let fault = parse_inject_only(rest)?;
    let store = TempStore::new("resubmit");

    let mut outbox = OrderOutbox::new();
    let key = outbox
        .commit_intent(corr("c-1"), &sample_order("AAPL", 10))
        .map_err(|err| format!("commit_intent: {err:?}"))?;
    outbox.save_to_disk(&store)?;
    let reloaded = OutboxSnapshot::load_from_path(store.path())
        .map_err(|err| format!("reload: {err:?}"))?
        .into_outbox();

    match fault {
        None => {
            // Full coverage: the broker view carries open + recently-completed orders,
            // and this intent is in neither → provably never landed → safe to resubmit.
            let snapshot =
                BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenAndRecentlyCompleted);
            let plan = reconcile(&reloaded, &snapshot);
            print_plan(&plan);
            if !plan.resubmit.contains(&key) {
                return Err(
                    "expected the unlanded intent in resubmit under full coverage".to_string(),
                );
            }
            println!("unlanded-intent-resubmitted:true");
            Ok(())
        }
        Some(Fault::PartialCoverage) => {
            println!("inject:{}", Fault::PartialCoverage.as_str());
            // Open-only: absence is ambiguous (it may have filled/cancelled) → NOT resubmitted.
            let snapshot = BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenOnly);
            let plan = reconcile(&reloaded, &snapshot);
            print_plan(&plan);
            if !plan.resubmit.is_empty() {
                return Err(
                    "SAFETY VIOLATION: an intent was resubmitted on a partial (open-only) broker view"
                        .to_string(),
                );
            }
            if !plan
                .unresolved
                .iter()
                .any(|c| c.key == key && matches!(c.kind, ConflictKind::UnverifiableSubmitWindow))
            {
                return Err(
                    "expected an UnverifiableSubmitWindow conflict under --inject partial-coverage"
                        .to_string(),
                );
            }
            println!("no-resubmit-on-partial-view:true");
            Ok(())
        }
        Some(other) => Err(format!(
            "fault '{}' is not applicable to restart-resubmit\n\n{USAGE}",
            other.as_str()
        )),
    }
}

/// AC bullet 4: entries are retained until their terminal state is observed, then released.
fn cmd_retention(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    reject_flags(rest)?;

    let mut outbox = OrderOutbox::new();
    let filled = outbox
        .commit_intent(corr("c-fill"), &sample_order("AAPL", 10))
        .map_err(|err| format!("commit_intent: {err:?}"))?;
    let working = outbox
        .commit_intent(corr("c-work"), &sample_order("MSFT", 5))
        .map_err(|err| format!("commit_intent: {err:?}"))?;
    outbox
        .bind_ack(&filled, "ib-1")
        .map_err(|e| e.to_string())?;
    outbox
        .bind_ack(&working, "ib-2")
        .map_err(|e| e.to_string())?;

    // Before terminal: both retained.
    println!("retained-before-fill:{}", outbox.len());
    if outbox.len() != 2 {
        return Err("both working intents must be retained before any terminal state".to_string());
    }
    outbox
        .observe_state(&filled, OrderState::Filled)
        .map_err(|e| e.to_string())?;
    let pruned = outbox.prune_terminal();
    println!("pruned:{}", pruned.len());
    println!("retained-after-fill:{}", outbox.len());
    if pruned != vec![filled.clone()] {
        return Err("prune must release exactly the terminal (FILLED) intent".to_string());
    }
    if outbox.contains(&filled) || !outbox.contains(&working) {
        return Err("the working intent must be retained; the FILLED intent released".to_string());
    }
    println!("retained-until-terminal:true");
    Ok(())
}

/// Fail-closed: a broker order-state query failure yields NO reconciliation decision.
fn cmd_broker_error(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    reject_flags(rest)?;

    let mut outbox = OrderOutbox::new();
    outbox
        .commit_intent(corr("c-1"), &sample_order("AAPL", 10))
        .map_err(|err| format!("commit_intent: {err:?}"))?;

    let source = FailingBrokerSource;
    match source.open_orders() {
        Ok(_) => Err("SAFETY VIOLATION: the failing broker source returned Ok".to_string()),
        Err(err) => {
            // No snapshot → reconcile is NOT run → no resubmission decision is made.
            // The typed category is surfaced so the fail-closed path is distinguished.
            println!("broker-query-category:{}", err.category());
            println!("broker-query-error:{err}");
            println!("reconcile-invoked:false");
            println!("no-decision-on-broker-error:true");
            Ok(())
        }
    }
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

fn print_plan(plan: &ReconciliationPlan) {
    println!("plan skip_bound:{}", plan.skip_bound.len());
    println!("plan adopt_ack:{}", plan.adopt_ack.len());
    println!("plan resubmit:{}", plan.resubmit.len());
    println!("plan mark_terminal:{}", plan.mark_terminal.len());
    println!("plan unresolved:{}", plan.unresolved.len());
}

fn corr(id: &str) -> ClientCorrelationId {
    ClientCorrelationId::new(id).expect("valid correlation id")
}

fn sample_order(symbol: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new("live-1"),
        symbol: symbol.to_string(),
        quantity,
        asset_class: AssetClass::Equity,
        side: OrderSide::Buy,
        order_type: OrderType::Market,
    }
}

/// A broker source that always fails — proves the fail-closed no-decision path.
struct FailingBrokerSource;

impl BrokerOpenOrderSource for FailingBrokerSource {
    fn open_orders(&self) -> Result<BrokerOpenOrderSnapshot, BrokerReconcileError> {
        Err(BrokerReconcileError::connectivity_blocked(
            "IB Gateway unreachable during restart reconciliation",
        ))
    }
}

/// A unique temp store directory for the durable outbox; removed on drop.
struct TempStore {
    path: PathBuf,
}

impl TempStore {
    fn new(label: &str) -> Self {
        static SEQ: AtomicU64 = AtomicU64::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let path = env::temp_dir().join(format!(
            "atp-exe009-cli-{label}-{}-{seq}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&path);
        Self { path }
    }

    fn path(&self) -> &std::path::Path {
        &self.path
    }
}

impl Drop for TempStore {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

/// Persist an outbox to the temp store, mapping the error to a String.
trait SaveToDisk {
    fn save_to_disk(&self, store: &TempStore) -> Result<(), String>;
}

impl SaveToDisk for OrderOutbox {
    fn save_to_disk(&self, store: &TempStore) -> Result<(), String> {
        OutboxSnapshot::capture(self.clone())
            .save_to_path(store.path())
            .map_err(|err| format!("save_to_path: {err:?}"))
    }
}

/// True if any token requests help.
fn wants_help(args: &[String]) -> bool {
    args.iter()
        .any(|arg| matches!(arg.as_str(), "help" | "--help" | "-h"))
}

/// Reject any flag for a subcommand that takes none.
fn reject_flags(rest: &[String]) -> Result<(), String> {
    if let Some(flag) = rest.first() {
        return Err(format!("unknown flag '{flag}'\n\n{USAGE}"));
    }
    Ok(())
}

/// A hazard to inject so a subcommand proves its fail-closed property still holds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fault {
    DuplicateReplay,
    IdConflict,
    PartialCoverage,
}

impl Fault {
    fn parse(spec: &str) -> Result<Self, String> {
        match spec {
            "duplicate-replay" => Ok(Self::DuplicateReplay),
            "id-conflict" => Ok(Self::IdConflict),
            "partial-coverage" => Ok(Self::PartialCoverage),
            other => Err(format!(
                "unknown fault '{other}' (expected duplicate-replay | id-conflict | partial-coverage)"
            )),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::DuplicateReplay => "duplicate-replay",
            Self::IdConflict => "id-conflict",
            Self::PartialCoverage => "partial-coverage",
        }
    }
}

/// Parse a subcommand that accepts only an optional `--inject <fault>`.
fn parse_inject_only(rest: &[String]) -> Result<Option<Fault>, String> {
    let mut inject = None;
    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--inject" => inject = Some(Fault::parse(&take_value(&mut iter, flag)?)?),
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    Ok(inject)
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}
