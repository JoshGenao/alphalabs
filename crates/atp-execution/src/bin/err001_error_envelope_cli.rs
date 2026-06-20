//! SRS-ERR-001 structured order-error envelope operator CLI.
//!
//! The operator-facing surface of "every order-submission error is a structured envelope" (docs/SRS.md
//! SRS-ERR-001; SyRS SYS-64; cross-cut with SRS-EXE-001). The acceptance criterion is: "Errors include
//! type, human-readable message, original order parameters, and one of the SyRS-defined error
//! categories when applicable." The envelope type ([`atp_types::StructuredOrderError`]), the SyRS
//! SYS-64 category vocabulary ([`atp_types::OrderErrorCategory`]), and the reject paths that build the
//! envelope are already built and exercised by `tools/error_handling_check.py`, the L3 contract, and
//! the `err_1..err_8` domain tests; this binary makes the envelope *operator-demonstrable* — the same
//! precedent as the SRS-BT-002 / BT-003 / SIM-001 / SIM-002 / SIM-003 CLIs (there is no Python↔Rust
//! strategy host, so the operator workflow is demonstrated over the Rust core).
//!
//! It exercises EVERY production order-submission rejection path that builds a [`StructuredOrderError`]:
//!   * the engine-owned single-live **authority** gate
//!     [`atp_execution::ExecutionEngine::route_order`] — the production order-submission boundary that
//!     derives live-ness from the engine's [`LiveDesignation`] registry and rejects any strategy that
//!     is not the single designated live strategy with a `NotDesignated` structured error;
//!   * the inner mode/connectivity/freshness gate
//!     [`atp_execution::ExecutionEngine::submit_live_order`] — paper non-live, IB unreachable,
//!     scheduled-restart, and stale market-data rejections; and
//!   * the idempotency ledger [`atp_types::OrderLedger::submit`] — a duplicate submission under the
//!     same client correlation id is rejected with the SRS-ERR-001 envelope (the duplicate-submission
//!     rejection SRS-EXE-008 specifies "per SRS-ERR-001").
//!
//! - `categories` — sweep every [`OrderErrorCategory`] variant and assert each `as_str()` maps to a
//!   non-empty, UPPER_SNAKE, and *distinct* SyRS SYS-64 wire string. Emit `all-categories-mapped:true`.
//!   This is the "one of the SyRS-defined error categories" half of the AC.
//!
//! - `envelope [--inject F]` — drive every reject path (authority gate, inner gate, idempotency ledger)
//!   and assert each returns a [`StructuredOrderError`] carrying its expected category, a non-empty
//!   error type, a non-empty human-readable message, AND the original order parameters round-tripped
//!   UNCHANGED. Emit `envelope-complete:true`. This is the "type, message, original order parameters"
//!   half of the AC.
//!
//! - `no-broker [--inject F]` — drive every reject path with a call-counting brokerage stub and assert
//!   the broker is consulted ZERO times. Emit `no-ib-side-effect:true`.
//!
//! - `authority [--inject F]` — drive the production authority gate `route_order` through the
//!   engine-owned [`LiveDesignation`] registry: a non-designated strategy (with NO live strategy, and
//!   again with a DIFFERENT strategy designated live) is rejected with a `NotDesignated` structured
//!   envelope and reaches no broker, while the single designated live strategy IS authorized (routes Ok
//!   and reaches the broker). Emit `authority-enforced:true` — the single-live-strategy invariant
//!   (SRS-EXE-001, AGENTS.md) producing the SRS-ERR-001 envelope on the real submission boundary.
//!
//! Fail closed: an unknown subcommand or flag exits non-zero. `--inject authorized` runs the
//! legitimately authorized path instead (a designated/live, connected, fresh submission that returns
//! `Ok` and reaches the broker). An authorized success is NOT a reject envelope and a legitimate broker
//! call is NOT a side-effect leak, so each reject-proof subcommand must FAIL CLOSED with NO proof line
//! under `--inject authorized`. This makes the proofs non-vacuous.
//!
//! Scope honesty: the envelope is demonstrated for the categories reachable at the *execution
//! boundary*. The remaining SyRS SYS-64 categories (e.g. INVALID_SYMBOL, INSUFFICIENT_BUYING_POWER,
//! RATE_LIMITED, SUBSCRIPTION_LIMIT_REACHED, the ingestion/orchestrator categories) are raised by
//! ADJACENT owners (the data layer, market-data subscription manager, orchestrator) and carry the same
//! envelope — they are "when applicable" per the AC, NOT contexts inside SRS-ERR-001's own criterion.

use std::cell::Cell;
use std::collections::BTreeSet;
use std::env;
use std::process::ExitCode;

use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, ExecutionEngine, LiveBrokerageSubmit,
    LiveDesignationConfirmation, MarketDataFreshnessProbe, StaleDataEventSink,
};
use atp_types::{
    ClientCorrelationId, ConnectivityEvent, ConnectivityState, MarketDataFreshness,
    OrderErrorCategory, OrderLedger, OrderReceipt, OrderSubmission, StaleDataEvent, StrategyId,
    StrategyMode, StructuredOrderError,
};

// Deterministic fixture order parameters. The submission is carried through to the envelope's
// `original_order` unchanged; these only need to be well-formed and stable.
const FIXTURE_SYMBOL: &str = "AAPL";
const FIXTURE_QTY: i64 = 100;
const FIXTURE_STALENESS: u64 = 42;
const LIVE_STRATEGY: &str = "live-1";

/// The engine's result of one submission plus the original (unchanged) submission and the number of
/// times the broker was consulted.
type Outcome = (
    OrderSubmission,
    Result<OrderReceipt, StructuredOrderError>,
    u32,
);

/// Every SyRS SYS-64 category, in a fixed order. Kept in lock-step with [`OrderErrorCategory`]; the
/// `categories` subcommand proves the vocabulary is total + stable over exactly this set.
const ALL_CATEGORIES: [OrderErrorCategory; 17] = [
    OrderErrorCategory::InvalidSymbol,
    OrderErrorCategory::InsufficientBuyingPower,
    OrderErrorCategory::ConnectivityBlocked,
    OrderErrorCategory::RateLimited,
    OrderErrorCategory::MarketDataStale,
    OrderErrorCategory::SubscriptionLimitReached,
    OrderErrorCategory::NonLiveStrategySubmission,
    OrderErrorCategory::IngestionRecordValidationFailed,
    OrderErrorCategory::IngestionPacingBudgetExceeded,
    OrderErrorCategory::StrategyStartupDeadlineExceeded,
    OrderErrorCategory::ResourceProfileInvalid,
    OrderErrorCategory::HostMemorySafetyMarginBreach,
    OrderErrorCategory::DeployedVersionInvalid,
    OrderErrorCategory::HotSwapDemotionTimeout,
    OrderErrorCategory::KillSwitchLiquidationTimeout,
    OrderErrorCategory::KillSwitchLiquidationProbeUnavailable,
    OrderErrorCategory::DuplicateClientCorrelationId,
];

/// A named reject path: a driver that produces a structured rejection, and the category it must carry.
struct RejectPath {
    label: &'static str,
    expected_category: OrderErrorCategory,
    run: fn() -> Outcome,
}

/// Every order-submission reject path that builds a [`StructuredOrderError`] in production: the
/// authority gate (`route_order` NotDesignated), the inner gate (`submit_live_order` paper /
/// unreachable / stale), and the idempotency ledger (`OrderLedger::submit` duplicate correlation id,
/// SRS-EXE-008 "rejected ... with a structured error per SRS-ERR-001"). These are the complete set of
/// `StructuredOrderError` construction sites in the core crates.
const REJECT_PATHS: [RejectPath; 6] = [
    RejectPath {
        label: "authority-not-designated",
        expected_category: OrderErrorCategory::NonLiveStrategySubmission,
        run: run_route_not_designated,
    },
    RejectPath {
        label: "paper-non-live",
        expected_category: OrderErrorCategory::NonLiveStrategySubmission,
        run: run_paper_non_live,
    },
    RejectPath {
        label: "live-unreachable",
        expected_category: OrderErrorCategory::ConnectivityBlocked,
        run: run_live_unreachable,
    },
    RejectPath {
        label: "live-scheduled-restart",
        expected_category: OrderErrorCategory::ConnectivityBlocked,
        run: run_live_scheduled_restart,
    },
    RejectPath {
        label: "live-stale-data",
        expected_category: OrderErrorCategory::MarketDataStale,
        run: run_live_stale,
    },
    RejectPath {
        label: "duplicate-correlation",
        expected_category: OrderErrorCategory::DuplicateClientCorrelationId,
        run: run_duplicate_correlation,
    },
];

const USAGE: &str = "\
err001_error_envelope_cli — SRS-ERR-001 structured order-error envelope operator workflow

USAGE:
    err001_error_envelope_cli categories
    err001_error_envelope_cli envelope  [--inject <fault>]
    err001_error_envelope_cli no-broker [--inject <fault>]
    err001_error_envelope_cli authority [--inject <fault>]

COMMANDS:
    categories  Sweep every SyRS SYS-64 OrderErrorCategory and prove each maps to a distinct,
                non-empty, UPPER_SNAKE wire string (all-categories-mapped:true).
    envelope    Drive every reject path (authority gate + inner gate) and prove each returns a
                structured error carrying category + non-empty type + non-empty message + the unchanged
                original order (envelope-complete:true).
    no-broker   Drive every reject path with a call-counting broker stub and prove the broker is
                consulted zero times — a rejected order never reaches IB (no-ib-side-effect:true).
    authority   Drive route_order through the engine-owned live-designation registry and prove a
                non-designated strategy is rejected (no broker) while the single designated live
                strategy is authorized (reaches the broker) — authority-enforced:true.

RUN FLAGS:
    --inject authorized  run the legitimately authorized path instead; the authorized submission
                         returns Ok and reaches the broker, so the reject-proof MUST fail closed with
                         no proof line. Makes the proofs non-vacuous.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("err001_error_envelope_cli: {err}");
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
        "categories" => cmd_categories(rest),
        "envelope" => cmd_envelope(rest),
        "no-broker" => cmd_no_broker(rest),
        "authority" => cmd_authority(rest),
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

/// True if any token requests help, so a subcommand can show usage instead of erroring.
fn wants_help(args: &[String]) -> bool {
    args.iter()
        .any(|arg| matches!(arg.as_str(), "help" | "--help" | "-h"))
}

/// Prove the SyRS SYS-64 category vocabulary is total + stable: every variant maps to a distinct,
/// non-empty, UPPER_SNAKE wire string.
fn cmd_categories(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    // `categories` has no external input to corrupt (its proof is over the fixed enum), so it takes no
    // flags — a stray token is rejected rather than silently ignored.
    if let Some(flag) = rest.first() {
        return Err(format!("unknown flag '{flag}'\n\n{USAGE}"));
    }

    let mut seen: BTreeSet<&'static str> = BTreeSet::new();
    let mut all_mapped = true;
    for category in ALL_CATEGORIES {
        let wire = category.as_str();
        let upper_snake = !wire.is_empty()
            && wire
                .chars()
                .all(|ch| ch.is_ascii_uppercase() || ch.is_ascii_digit() || ch == '_');
        let distinct = seen.insert(wire);
        let mapped = upper_snake && distinct;
        all_mapped &= mapped;
        println!(
            "category[{category:?}] wire:{wire} upper-snake:{upper_snake} distinct:{distinct}"
        );
    }

    // Non-vacuous: every category mapped AND the distinct set covers exactly the variant count (no
    // two categories collided onto the same wire string).
    if !all_mapped || seen.len() != ALL_CATEGORIES.len() {
        return Err(
            "all-categories-mapped:false — a SyRS SYS-64 category mapped to an empty, \
             non-upper-snake, or colliding wire string (a vocabulary regression)"
                .to_string(),
        );
    }
    println!("all-categories-mapped:true");
    Ok(())
}

/// Prove every reject path returns a complete structured envelope with the original order unchanged.
fn cmd_envelope(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_authorized_fails_closed_submit(fault, "envelope-complete");
    }

    let mut all_complete = true;
    for path in &REJECT_PATHS {
        let (original, result, _calls) = (path.run)();
        let err = match result {
            Err(err) => err,
            Ok(receipt) => {
                return Err(format!(
                    "envelope[{}] expected a structured rejection but got Ok({receipt:?}) — the \
                     reject path regressed",
                    path.label
                ));
            }
        };
        let original_unchanged = err.original_order == original;
        // Non-vacuous: the envelope must carry the path's expected category, a non-empty type, a
        // non-empty human-readable message, AND the unchanged original order parameters.
        let complete = err.category == path.expected_category
            && !err.error_type.trim().is_empty()
            && !err.message.trim().is_empty()
            && original_unchanged;
        all_complete &= complete;
        println!(
            "envelope[{}] category:{} type:{:?} message-nonempty:{} original-order-unchanged:{} \
             complete:{}",
            path.label,
            err.category.as_str(),
            err.error_type,
            !err.message.trim().is_empty(),
            original_unchanged,
            complete
        );
    }

    if !all_complete {
        return Err(
            "envelope-complete:false — at least one reject path produced an envelope missing its \
             category, type, message, or unchanged original order (an SRS-ERR-001 regression)"
                .to_string(),
        );
    }
    println!("envelope-complete:true");
    Ok(())
}

/// Prove every reject path reaches no brokerage — the runtime witness that a rejected order never
/// produces an IB side effect.
fn cmd_no_broker(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_authorized_fails_closed_submit(fault, "no-ib-side-effect");
    }

    let mut swept: u64 = 0;
    let mut clean: u64 = 0;
    for path in &REJECT_PATHS {
        let (_, result, broker_calls) = (path.run)();
        let rejected = result.is_err();
        // Non-vacuous: each reject path must BOTH return a structured rejection AND consult the broker
        // zero times.
        let clean_path = rejected && broker_calls == 0;
        swept += 1;
        clean += u64::from(clean_path);
        println!(
            "no-broker[{}] rejected:{rejected} broker-calls:{broker_calls} clean:{clean_path}",
            path.label
        );
    }

    println!(
        "no-broker swept:{swept} clean:{clean} leaked:{}",
        swept - clean
    );
    if swept == 0 || clean != swept {
        return Err(format!(
            "no-ib-side-effect:false — swept {swept} reject paths but only {clean} reached no \
             brokerage ({} leaked a broker call)",
            swept - clean
        ));
    }
    println!("no-ib-side-effect:true");
    Ok(())
}

/// Prove the engine-owned single-live authority gate (`route_order`) enforces SRS-EXE-001: a
/// non-designated strategy is rejected with a structured envelope and reaches no broker, while the
/// single designated live strategy is authorized and reaches the broker.
fn cmd_authority(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_authorized_fails_closed_authority(fault);
    }

    let mut enforced = true;

    // (a) No live strategy designated: every strategy is non-designated, so route_order rejects.
    let no_live = ExecutionEngine::default();
    let (orig_a, res_a, calls_a) = route_order_for(&no_live, "rogue-1");
    enforced &= report_authority_rejection("none-designated", &orig_a, &res_a, calls_a);

    // (b) A DIFFERENT strategy is designated live: a non-designated strategy is still rejected (the
    // single-live-strategy invariant — only the one designated strategy may route).
    let mut other_live = ExecutionEngine::default();
    other_live
        .designate(StrategyId::new(LIVE_STRATEGY), confirm(LIVE_STRATEGY))
        .map_err(|err| format!("authority: could not designate the live strategy: {err}"))?;
    let (orig_b, res_b, calls_b) = route_order_for(&other_live, "paper-7");
    enforced &= report_authority_rejection("other-strategy-live", &orig_b, &res_b, calls_b);

    // (c) The single designated live strategy IS authorized: route_order reaches the broker and
    // returns Ok. The authorized contrast proving the gate is real (not vacuously rejecting all).
    let (_, res_c, calls_c) = route_order_for(&other_live, LIVE_STRATEGY);
    let authorized = res_c.is_ok() && calls_c == 1;
    enforced &= authorized;
    println!(
        "authority[designated-live] authorized:{} broker-calls:{calls_c}",
        res_c.is_ok()
    );

    if !enforced {
        return Err(
            "authority-enforced:false — route_order did not reject a non-designated strategy with a \
             clean structured envelope, or did not authorize the single designated live strategy \
             (SRS-EXE-001 single-live invariant regression)"
                .to_string(),
        );
    }
    println!("authority-enforced:true");
    Ok(())
}

/// Report and check one authority-gate rejection: a structured `NonLiveStrategySubmission` envelope
/// with the unchanged original order and zero broker calls.
fn report_authority_rejection(
    label: &str,
    original: &OrderSubmission,
    result: &Result<OrderReceipt, StructuredOrderError>,
    broker_calls: u32,
) -> bool {
    match result {
        Err(err) => {
            let original_unchanged = &err.original_order == original;
            let ok = err.category == OrderErrorCategory::NonLiveStrategySubmission
                && !err.error_type.trim().is_empty()
                && !err.message.trim().is_empty()
                && original_unchanged
                && broker_calls == 0;
            println!(
                "authority[{label}] category:{} type:{:?} original-order-unchanged:{} \
                 broker-calls:{broker_calls} rejected:{ok}",
                err.category.as_str(),
                err.error_type,
                original_unchanged
            );
            ok
        }
        Ok(receipt) => {
            println!("authority[{label}] UNEXPECTED Ok({receipt:?}) — expected a NotDesignated rejection");
            false
        }
    }
}

// --------------------------------------------------------------------------- //
// Fail-closed injection (non-vacuity)
// --------------------------------------------------------------------------- //

/// `--inject authorized` for `envelope` / `no-broker`: the inner gate's authorized path returns `Ok`
/// and reaches the broker, so the reject proof cannot be made. Always returns Err with no proof line.
fn inject_authorized_fails_closed_submit(fault: Fault, proof: &str) -> Result<(), String> {
    let Fault::Authorized = fault;
    let (_, result, broker_calls) = run_authorized_submit();
    match result {
        Ok(receipt) => Err(format!(
            "inject=authorized: submit_live_order returned Ok({receipt:?}) on the Live+Connected+\
             Fresh path and reached the broker (broker-calls:{broker_calls}); an authorized \
             submission is not a rejection, so {proof} cannot be proven — no proof asserted"
        )),
        Err(err) => Err(format!(
            "inject=authorized: the authorized Live+Connected+Fresh path unexpectedly rejected \
             ({err}) — a regression; no {proof} asserted"
        )),
    }
}

/// `--inject authorized` for `authority`: a designated strategy routed through `route_order` is
/// authorized (Ok + broker reached), so the NotDesignated rejection proof cannot be made. Always Err.
fn inject_authorized_fails_closed_authority(fault: Fault) -> Result<(), String> {
    let Fault::Authorized = fault;
    let mut engine = ExecutionEngine::default();
    if let Err(err) = engine.designate(StrategyId::new("rogue-1"), confirm("rogue-1")) {
        return Err(format!(
            "inject=authorized: could not designate the strategy ({err}); no authority proof asserted"
        ));
    }
    let (_, result, broker_calls) = route_order_for(&engine, "rogue-1");
    match result {
        Ok(_) => Err(format!(
            "inject=authorized: route_order authorized a DESIGNATED strategy (broker-calls:\
             {broker_calls}); a NotDesignated rejection cannot be proven when the strategy IS the \
             designated live strategy — no authority proof asserted"
        )),
        Err(err) => Err(format!(
            "inject=authorized: the designated live strategy unexpectedly rejected ({err}) — a \
             regression; no authority proof asserted"
        )),
    }
}

// --------------------------------------------------------------------------- //
// Engine drivers and stubs
// --------------------------------------------------------------------------- //

/// Drive `submit_live_order` (the inner gate) with the given mode/connectivity/freshness.
fn drive_submit(
    mode: StrategyMode,
    conn_state: ConnectivityState,
    freshness: MarketDataFreshness,
    strategy: &str,
) -> Outcome {
    let engine = ExecutionEngine::default();
    let broker = CountingBroker::new();
    let connectivity = StubConnectivity { state: conn_state };
    let events = NoopConnectivityEvents;
    let probe = StubFreshness {
        freshness,
        staleness: FIXTURE_STALENESS,
    };
    let stale_events = NoopStaleEvents;
    let submission = fixture_submission(strategy);
    let original = submission.clone();
    let result = engine.submit_live_order(
        mode,
        submission,
        &broker,
        &connectivity,
        &events,
        &probe,
        &stale_events,
    );
    (original, result, broker.calls())
}

/// Drive `route_order` (the production authority gate) on `engine` for `strategy`, connected + fresh.
fn route_order_for(engine: &ExecutionEngine, strategy: &str) -> Outcome {
    let broker = CountingBroker::new();
    let connectivity = StubConnectivity {
        state: ConnectivityState::Connected,
    };
    let events = NoopConnectivityEvents;
    let probe = StubFreshness {
        freshness: MarketDataFreshness::Fresh,
        staleness: FIXTURE_STALENESS,
    };
    let stale_events = NoopStaleEvents;
    let submission = fixture_submission(strategy);
    let original = submission.clone();
    let result = engine.route_order(
        submission,
        &broker,
        &connectivity,
        &events,
        &probe,
        &stale_events,
    );
    (original, result, broker.calls())
}

// Reject-path drivers (function pointers for REJECT_PATHS).

/// The production authority gate rejects a non-designated strategy (no live strategy designated).
fn run_route_not_designated() -> Outcome {
    let engine = ExecutionEngine::default();
    route_order_for(&engine, "rogue-1")
}

fn run_paper_non_live() -> Outcome {
    drive_submit(
        StrategyMode::Paper,
        ConnectivityState::Connected,
        MarketDataFreshness::Fresh,
        "paper-7",
    )
}

fn run_live_unreachable() -> Outcome {
    drive_submit(
        StrategyMode::Live,
        ConnectivityState::Unreachable,
        MarketDataFreshness::Fresh,
        LIVE_STRATEGY,
    )
}

/// The connectivity arm rejects BOTH `Unreachable` and `ScheduledRestartWindow` (SRS-MD-005) with the
/// same ConnectivityBlocked envelope; this path drives the scheduled-restart state so a regression in
/// that state cannot slip through the envelope / no-broker proofs.
fn run_live_scheduled_restart() -> Outcome {
    drive_submit(
        StrategyMode::Live,
        ConnectivityState::ScheduledRestartWindow,
        MarketDataFreshness::Fresh,
        LIVE_STRATEGY,
    )
}

fn run_live_stale() -> Outcome {
    drive_submit(
        StrategyMode::Live,
        ConnectivityState::Connected,
        MarketDataFreshness::Stale,
        LIVE_STRATEGY,
    )
}

/// The idempotency ledger rejects a duplicate submission under the same client correlation id with the
/// SRS-ERR-001 envelope (SRS-EXE-008). The ledger never touches a broker, so broker calls stay 0.
fn run_duplicate_correlation() -> Outcome {
    let mut ledger = OrderLedger::new();
    let submission = fixture_submission("paper-7");
    let original = submission.clone();
    let correlation_id =
        ClientCorrelationId::new("dup-1").expect("a non-empty correlation id is valid");
    // The first submission under a fresh correlation id is admitted (borrow ends at `.is_ok()`).
    let first_admitted = ledger.submit(correlation_id.clone(), &submission).is_ok();
    // The duplicate submission under the SAME correlation id is rejected with the envelope. Any other
    // shape (first not admitted, or the duplicate unexpectedly admitted) is surfaced as Ok so the
    // reject proof fails closed rather than passing vacuously.
    let result = match ledger.submit(correlation_id, &submission) {
        Err(err) if first_admitted => Err(err),
        _ => Ok(OrderReceipt {
            broker_order_id: "UNEXPECTED-DUPLICATE-OUTCOME".to_string(),
        }),
    };
    (original, result, 0)
}

/// The legitimately authorized inner-gate path (Live+Connected+Fresh): returns Ok and reaches broker.
fn run_authorized_submit() -> Outcome {
    drive_submit(
        StrategyMode::Live,
        ConnectivityState::Connected,
        MarketDataFreshness::Fresh,
        LIVE_STRATEGY,
    )
}

fn fixture_submission(strategy: &str) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(strategy),
        symbol: FIXTURE_SYMBOL.to_string(),
        quantity: FIXTURE_QTY,
    }
}

fn confirm(strategy: &str) -> LiveDesignationConfirmation {
    LiveDesignationConfirmation::from_operator(
        StrategyId::new(strategy),
        "operator confirmed live designation",
    )
    .expect("a non-empty acknowledgement yields a confirmation token")
}

/// A brokerage port that counts how many times the engine pushes an order to it. On a reject path the
/// count stays 0; on the authorized path it becomes 1 (the runtime witness behind `no-ib-side-effect`).
struct CountingBroker {
    calls: Cell<u32>,
}

impl CountingBroker {
    fn new() -> Self {
        Self {
            calls: Cell::new(0),
        }
    }

    fn calls(&self) -> u32 {
        self.calls.get()
    }
}

impl LiveBrokerageSubmit for CountingBroker {
    fn submit_order(
        &self,
        submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        self.calls.set(self.calls.get() + 1);
        Ok(OrderReceipt {
            broker_order_id: format!("ib-{}", submission.symbol),
        })
    }
}

struct StubConnectivity {
    state: ConnectivityState,
}

impl BrokerageConnectivity for StubConnectivity {
    fn state(&self) -> ConnectivityState {
        self.state
    }

    fn request_reconnect(&self) {}
}

struct NoopConnectivityEvents;

impl ConnectivityEventSink for NoopConnectivityEvents {
    fn record(&self, _event: ConnectivityEvent) {}
}

struct StubFreshness {
    freshness: MarketDataFreshness,
    staleness: u64,
}

impl MarketDataFreshnessProbe for StubFreshness {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        self.freshness
    }

    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        self.staleness
    }
}

struct NoopStaleEvents;

impl StaleDataEventSink for NoopStaleEvents {
    fn record(&self, _event: StaleDataEvent) {}
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

/// A fault to inject so a reject-proof subcommand must fail closed (print no proof line).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fault {
    /// Run the legitimately authorized path, which is not a rejection.
    Authorized,
}

impl Fault {
    fn parse(spec: &str) -> Result<Self, String> {
        match spec {
            "authorized" => Ok(Self::Authorized),
            other => Err(format!("unknown fault '{other}' (expected authorized)")),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Authorized => "authorized",
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
