//! SRS-ERR-001 BROKER-SIDE structured order-error envelope operator CLI.
//!
//! The companion to `err001_error_envelope_cli` (atp-execution), which proves the SRS-ERR-001
//! envelope for every reject path reachable at the *execution* boundary. This binary proves the
//! other half of SyRS SYS-64: the **broker-side order-validation** error types — INVALID_SYMBOL,
//! INSUFFICIENT_BUYING_POWER, RATE_LIMITED — arriving inside a [`StructuredOrderError`] when
//! Interactive Brokers rejects a submission.
//!
//! It lives in `atp-orchestrator` because that is the one layer allowed to see both sides
//! (SRS-ARCH-002): `atp-execution` must not depend on `atp-adapters`, so the execution-crate CLI
//! structurally cannot reach the IB adapter. Nothing here is a re-implementation — the whole point
//! is that every component in the chain is the production one:
//!
//! ```text
//!   ExecutionEngine::route_order            (REAL — the PRODUCTION submission boundary; resolves
//!                                            live-ness from the engine-owned LiveDesignation
//!                                            registry, so the single-live invariant is exercised
//!                                            rather than sidestepped by passing StrategyMode::Live)
//!     -> ExecutionEngine::submit_live_order (REAL, the inner mode/connectivity/freshness gate)
//!       -> IbBrokerageBridge                (REAL, LiveBrokerageSubmit port + envelope mapping)
//!         -> InteractiveBrokersBrokerage    (REAL, SRS-EXE-006 adapter)
//!           -> classify_ib_order_error      (REAL, SyRS SYS-64 vendor-code classification)
//!             -> ScriptedIbGateway          (fixture transport: supplies code + message only)
//! ```
//!
//! The ONLY fixture is the transport. A real socket carries a vendor `code` + `message`; the
//! [`ScriptedIbGateway`] supplies exactly that and nothing else, which is precisely the SRS-EXE-006
//! seam (`IbApiError` never crosses the canonical trait boundary). Every classification decision and
//! every envelope field is produced by production code.
//!
//! - `broker-categories [--inject F]` — reject a submission with each vendor code the SRS-EXE-006
//!   classifier maps, and prove the resulting envelope carries the applicable SyRS SYS-64 category, a
//!   non-empty error type, a non-empty human-readable message, and the original order parameters
//!   round-tripped UNCHANGED. Each path also reports `wire-attempts:1` — the witness that the
//!   rejection is genuinely BROKER-side rather than a gate short-circuit that never consulted the
//!   broker. A final `authority-not-designated` path proves the live-designation gate is
//!   load-bearing: the same rejecting transport, submitted by a NON-designated strategy, is refused
//!   with the authority category at `wire-attempts:0` (SRS-EXE-001). Emits
//!   `broker-envelope-complete:true`.
//!
//! - `unmapped [--inject F]` — reject with vendor codes the classifier does NOT map, and prove the
//!   failure is surfaced (never dropped) under `BROKER_REJECTED` while carrying the vendor code and
//!   text in the message — and specifically that it is NOT fabricated as `INVALID_SYMBOL`. The AC
//!   requires a SyRS category only "when applicable"; borrowing an inapplicable one would be a false
//!   claim about the failure. Emits `unmapped-surfaced-not-fabricated:true`.
//!
//! - `parity [--inject F]` — SyRS SYS-64 requires "the error contract shall be identical for live and
//!   paper execution modes". Drive the SAME malformed order down the live arm and the paper arm and
//!   prove both produce byte-identical envelope fields (category, type, message, original order).
//!   Emits `live-paper-parity:true`.
//!
//! Fail closed: an unknown subcommand, flag, or fault exits non-zero. `--inject accepted` swaps the
//! rejecting transport for an ACCEPTING one; an accepted submission is not a rejection, so every
//! proof subcommand MUST fail closed with NO proof line under it. That is what makes these proofs
//! non-vacuous — they cannot pass by simply failing to reject.
//!
//! Scope honesty: this is deterministic fixture verification of the CLASSIFICATION and ENVELOPE over
//! the real adapter. Observing a *real* IB gateway emit these rejections is the operator-gated leg
//! (`ATP_RUN_INTEGRATION=1`, `crates/atp-orchestrator/tests/srs_err_001_broker_envelope_live.rs`);
//! SRS-ERR-001 stays passes:false until the operator runs it. SUBSCRIPTION_LIMIT_REACHED is not an
//! order submission and stays SRS-MD-002's.

use std::env;
use std::process::ExitCode;

use atp_execution::{ExecutionEngine, LiveDesignationConfirmation};
use atp_orchestrator::order_routing_wiring::{
    CollectingConnectivitySink, CollectingStaleDataSink, FreshMarketDataFixture,
    HealthyConnectivityFixture, IbBrokerageBridge, RecordingIbGateway, ScriptedIbGateway,
    WiredPaperSimulation,
};
use atp_types::{
    AssetClass, OrderErrorCategory, OrderReceipt, OrderSide, OrderSubmission, OrderType,
    StrategyId, StructuredOrderError,
};

// Deterministic fixture order parameters. The submission is carried through to the envelope's
// `original_order` unchanged; these only need to be well-formed and stable.
const FIXTURE_SYMBOL: &str = "AAPL";
const FIXTURE_QTY: i64 = 100;
const LIVE_STRATEGY: &str = "live-1";
const PAPER_STRATEGY: &str = "paper-7";

/// A malformed order both execution arms must reject identically (SyRS SYS-64 live/paper identity).
/// A non-positive quantity fails `OrderSubmission::validate` before any destination is chosen.
const MALFORMED_QTY: i64 = -5;

/// One broker-side reject path: the vendor error a gateway would carry, and the SyRS SYS-64 category
/// the SRS-EXE-006 classifier must derive from it.
struct BrokerRejectPath {
    label: &'static str,
    code: i32,
    vendor_message: &'static str,
    expected_category: OrderErrorCategory,
}

/// The vendor codes `classify_ib_order_error` maps onto SyRS SYS-64 broker-validation categories.
/// Codes and reason text mirror the adapter's own contract
/// (`architecture/runtime_services.json` → `adapter_contract.ib_brokerage_runtime.mapped_categories`).
const MAPPED_PATHS: [BrokerRejectPath; 4] = [
    BrokerRejectPath {
        label: "no-security-definition",
        code: 200,
        vendor_message: "No security definition has been found for the request",
        expected_category: OrderErrorCategory::InvalidSymbol,
    },
    BrokerRejectPath {
        label: "security-not-available",
        code: 203,
        vendor_message: "The security is not available or allowed for this account",
        expected_category: OrderErrorCategory::InvalidSymbol,
    },
    BrokerRejectPath {
        label: "insufficient-buying-power",
        code: 201,
        vendor_message: "Order rejected - reason:Insufficient buying power for this order",
        expected_category: OrderErrorCategory::InsufficientBuyingPower,
    },
    BrokerRejectPath {
        label: "max-rate-exceeded",
        code: 100,
        vendor_message: "Max rate of messages per second has been exceeded",
        expected_category: OrderErrorCategory::RateLimited,
    },
];

/// Vendor rejections the classifier deliberately does NOT map onto a SyRS SYS-64 category: a generic
/// order rejection whose reason text names no known condition, a cancel-side code arriving on the
/// submit path, and a code the adapter has never seen. Each must be surfaced under `BROKER_REJECTED`
/// with the vendor detail intact — never dropped, and never relabelled as an invalid symbol.
const UNMAPPED_PATHS: [BrokerRejectPath; 3] = [
    BrokerRejectPath {
        label: "generic-order-rejection",
        code: 201,
        vendor_message: "Order rejected - reason:Contract is not available for trading",
        expected_category: OrderErrorCategory::BrokerRejected,
    },
    BrokerRejectPath {
        label: "cancel-code-on-submit",
        code: 202,
        vendor_message: "Order cancelled - reason:",
        expected_category: OrderErrorCategory::BrokerRejected,
    },
    BrokerRejectPath {
        label: "unrecognised-vendor-code",
        code: 321,
        vendor_message: "Error validating request:-'bW' : cause - Invalid order type",
        expected_category: OrderErrorCategory::BrokerRejected,
    },
];

const USAGE: &str = "\
err001_broker_envelope_cli — SRS-ERR-001 broker-side structured-error envelope operator workflow

USAGE:
    err001_broker_envelope_cli broker-categories [--inject <fault>]
    err001_broker_envelope_cli unmapped          [--inject <fault>]
    err001_broker_envelope_cli parity            [--inject <fault>]

COMMANDS:
    broker-categories  Reject a live submission with each vendor code the SRS-EXE-006 classifier maps
                       and prove the envelope carries the applicable SyRS SYS-64 category + non-empty
                       type + non-empty message + the unchanged original order
                       (broker-envelope-complete:true).
    unmapped           Reject with vendor codes the classifier does NOT map and prove each is surfaced
                       under BROKER_REJECTED with the vendor detail intact, never dropped and never
                       fabricated as INVALID_SYMBOL (unmapped-surfaced-not-fabricated:true).
    parity             Drive one malformed order down the live arm and the paper arm and prove both
                       produce identical envelope fields — the SyRS SYS-64 'identical for live and
                       paper execution modes' contract (live-paper-parity:true).

RUN FLAGS:
    --inject accepted  swap the rejecting transport for an ACCEPTING one. An accepted submission is
                       not a rejection, so every proof MUST fail closed with no proof line. Makes the
                       proofs non-vacuous.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("err001_broker_envelope_cli: {err}");
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
        "broker-categories" => cmd_broker_categories(rest),
        "unmapped" => cmd_unmapped(rest),
        "parity" => cmd_parity(rest),
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

/// Prove every MAPPED vendor rejection produces a complete SyRS SYS-64 envelope.
fn cmd_broker_categories(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_accepted_fails_closed(fault, "broker-envelope-complete");
    }

    let mut all_complete = true;
    for path in &MAPPED_PATHS {
        let (original, result, attempts) = drive_broker_rejection(path.code, path.vendor_message);
        let err = expect_rejection(&result, path.label)?;
        let original_unchanged = err.original_order == original;
        let message_carries_vendor_detail = err.message.contains(path.vendor_message);
        // The rejection must be genuinely BROKER-side: the order reached the wire exactly once and
        // was refused there. Without this, a gate that short-circuited before the broker could
        // produce a rejection that merely looked like a broker classification.
        let reached_broker = attempts == 1;
        // Non-vacuous: the envelope must carry the path's expected category (derived by the REAL
        // classifier from the vendor code), a non-empty type, a human-readable message that retains
        // the vendor's own text, AND the unchanged original order parameters.
        let complete = err.category == path.expected_category
            && !err.error_type.trim().is_empty()
            && !err.message.trim().is_empty()
            && message_carries_vendor_detail
            && original_unchanged
            && reached_broker;
        all_complete &= complete;
        println!(
            "broker[{}] code:{} category:{} type:{:?} message-nonempty:{} vendor-detail:{} \
             original-order-unchanged:{} wire-attempts:{} complete:{}",
            path.label,
            path.code,
            err.category.as_str(),
            err.error_type,
            !err.message.trim().is_empty(),
            message_carries_vendor_detail,
            original_unchanged,
            attempts,
            complete
        );
    }

    // Every proof above routed through ExecutionEngine::route_order, which resolves live-ness from
    // the engine-owned designation registry. Prove that gate is load-bearing rather than incidental:
    // the SAME rejecting transport, submitted by a NON-designated strategy, must be refused at the
    // authority gate with the authority category and must never reach the wire (SRS-EXE-001).
    let authority_path = &MAPPED_PATHS[0];
    let (rogue_original, rogue_result, rogue_attempts) =
        drive_non_designated_rejection(authority_path.code, authority_path.vendor_message);
    let rogue_err = expect_rejection(&rogue_result, "authority-not-designated")?;
    let gate_holds = rogue_attempts == 0
        && rogue_err.category == OrderErrorCategory::NonLiveStrategySubmission
        && rogue_err.original_order == rogue_original;
    all_complete &= gate_holds;
    println!(
        "broker[authority-not-designated] category:{} type:{:?} wire-attempts:{} \
         original-order-unchanged:{} gate-holds:{}",
        rogue_err.category.as_str(),
        rogue_err.error_type,
        rogue_attempts,
        rogue_err.original_order == rogue_original,
        gate_holds
    );

    if !all_complete {
        return Err(
            "broker-envelope-complete:false — at least one broker rejection produced an envelope \
             missing its SyRS SYS-64 category, type, message, vendor detail, or unchanged original \
             order; never reached the broker; or the live-designation authority gate failed to \
             refuse a non-designated strategy before the wire (an SRS-ERR-001 regression)"
                .to_string(),
        );
    }
    println!("broker-envelope-complete:true");
    Ok(())
}

/// Prove an UNMAPPED vendor rejection is surfaced honestly rather than relabelled.
fn cmd_unmapped(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_accepted_fails_closed(fault, "unmapped-surfaced-not-fabricated");
    }

    let mut all_honest = true;
    for path in &UNMAPPED_PATHS {
        let (original, result, _attempts) = drive_broker_rejection(path.code, path.vendor_message);
        let err = expect_rejection(&result, path.label)?;
        // Surfaced: the failure became an envelope at all, and the vendor's own code and text
        // survived into the human-readable message (nothing was swallowed).
        let surfaced = err.message.contains(path.vendor_message)
            && err.message.contains(&path.code.to_string());
        // Not fabricated: an unmapped rejection must NOT borrow a SyRS category that does not apply.
        // INVALID_SYMBOL is the specific historical fabrication this proof exists to prevent.
        let not_fabricated = err.category == OrderErrorCategory::BrokerRejected
            && err.category != OrderErrorCategory::InvalidSymbol;
        let honest = surfaced && not_fabricated && err.original_order == original;
        all_honest &= honest;
        println!(
            "unmapped[{}] code:{} category:{} type:{:?} surfaced:{} not-fabricated:{} \
             original-order-unchanged:{} honest:{}",
            path.label,
            path.code,
            err.category.as_str(),
            err.error_type,
            surfaced,
            not_fabricated,
            err.original_order == original,
            honest
        );
    }

    if !all_honest {
        return Err(
            "unmapped-surfaced-not-fabricated:false — an unmapped broker rejection was dropped, \
             lost its vendor detail, or borrowed a SyRS SYS-64 category that does not apply to it \
             (the SRS-ERR-001 acceptance criterion requires a category only 'when applicable')"
                .to_string(),
        );
    }
    println!("unmapped-surfaced-not-fabricated:true");
    Ok(())
}

/// Prove the live and paper arms reject the same malformed order with the same envelope.
fn cmd_parity(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_accepted_fails_closed_parity(fault);
    }

    let (live_original, live_result) = drive_malformed_dispatch(LIVE_STRATEGY, true);
    let (paper_original, paper_result) = drive_malformed_dispatch(PAPER_STRATEGY, false);
    let live = expect_rejection(&live_result, "live")?;
    let paper = expect_rejection(&paper_result, "paper")?;

    for (arm, err, original) in [
        ("live", live, &live_original),
        ("paper", paper, &paper_original),
    ] {
        println!(
            "parity[{}] category:{} type:{:?} message-nonempty:{} original-order-unchanged:{}",
            arm,
            err.category.as_str(),
            err.error_type,
            !err.message.trim().is_empty(),
            &err.original_order == original
        );
    }

    // The contract is IDENTICAL, so compare the fields that define it. `original_order` differs by
    // construction (each arm submits under its own strategy id), so it is checked per-arm above and
    // the identity check covers the classification triple.
    let same_category = live.category == paper.category;
    let same_type = live.error_type == paper.error_type;
    let same_message = live.message == paper.message;
    let originals_unchanged =
        live.original_order == live_original && paper.original_order == paper_original;
    // Non-vacuous: parity on a WRONG category is not parity. Both arms must land on the dedicated
    // invalid-order-parameters category, not on a borrowed one.
    let correct_category = live.category == OrderErrorCategory::OrderParametersInvalid;
    let identical =
        same_category && same_type && same_message && originals_unchanged && correct_category;

    println!(
        "parity[contract] same-category:{same_category} same-type:{same_type} \
         same-message:{same_message} originals-unchanged:{originals_unchanged} \
         correct-category:{correct_category} identical:{identical}"
    );

    if !identical {
        return Err(
            "live-paper-parity:false — the live and paper arms did not reject the same malformed \
             order with the same envelope, or landed on a category other than \
             ORDER_PARAMETERS_INVALID (SyRS SYS-64 requires an identical error contract for live \
             and paper execution modes)"
                .to_string(),
        );
    }
    println!("live-paper-parity:true");
    Ok(())
}

// --------------------------------------------------------------------------- //
// Engine drivers
// --------------------------------------------------------------------------- //

/// Drive a broker rejection through the PRODUCTION order-submission boundary:
/// `ExecutionEngine::route_order`, which resolves live-ness from the engine-owned
/// [`LiveDesignation`] registry before delegating to the inner gate and the brokerage port.
///
/// Routing through `route_order` rather than calling `submit_live_order` directly matters for the
/// evidence: `submit_live_order` takes `StrategyMode::Live` as a *caller-supplied* argument, so a
/// proof built on it could reach the bridge with no designated live strategy at all — it would be
/// blind to a regression in how the authority gate propagates broker rejections, and it would
/// sidestep the single-live-strategy invariant (SRS-EXE-001, AGENTS.md) rather than exercise it.
///
/// Returns the original submission, the engine's result, and how many order submissions actually
/// reached the wire — the witness that a rejection is genuinely BROKER-side rather than a gate
/// short-circuit that never consulted the broker at all.
fn drive_broker_rejection(
    code: i32,
    message: &str,
) -> (
    OrderSubmission,
    Result<OrderReceipt, StructuredOrderError>,
    u32,
) {
    let mut engine = ExecutionEngine::default();
    engine
        .designate(StrategyId::new(LIVE_STRATEGY), confirm(LIVE_STRATEGY))
        .expect("designating a single live strategy on a fresh engine succeeds");
    let brokerage = IbBrokerageBridge::new(ScriptedIbGateway::rejecting(code, message));
    let submission = fixture_submission(LIVE_STRATEGY, FIXTURE_QTY);
    let original = submission.clone();
    let result = engine.route_order(
        submission,
        &brokerage,
        &HealthyConnectivityFixture,
        &CollectingConnectivitySink::default(),
        &FreshMarketDataFixture,
        &CollectingStaleDataSink::default(),
    );
    let attempts = brokerage.gateway().attempts();
    (original, result, attempts)
}

/// The authority counterpart: the SAME rejecting transport, but the submitting strategy is NOT the
/// designated live one. `route_order` must reject it at the authority gate BEFORE the bridge is
/// consulted, so the wire attempt count stays 0 and the envelope carries the authority category —
/// never the broker's. Proves the broker proofs above are reached through the gate, not around it.
fn drive_non_designated_rejection(
    code: i32,
    message: &str,
) -> (
    OrderSubmission,
    Result<OrderReceipt, StructuredOrderError>,
    u32,
) {
    let engine = ExecutionEngine::default();
    let brokerage = IbBrokerageBridge::new(ScriptedIbGateway::rejecting(code, message));
    let submission = fixture_submission("rogue-1", FIXTURE_QTY);
    let original = submission.clone();
    let result = engine.route_order(
        submission,
        &brokerage,
        &HealthyConnectivityFixture,
        &CollectingConnectivitySink::default(),
        &FreshMarketDataFixture,
        &CollectingStaleDataSink::default(),
    );
    let attempts = brokerage.gateway().attempts();
    (original, result, attempts)
}

/// Drive a MALFORMED order through `ExecutionEngine::dispatch_order` — the shared entry both arms
/// pass through — designating the strategy live or leaving it to route to the paper simulation.
fn drive_malformed_dispatch(
    strategy: &str,
    designate_live: bool,
) -> (OrderSubmission, Result<OrderReceipt, StructuredOrderError>) {
    let mut engine = ExecutionEngine::default();
    if designate_live {
        engine
            .designate(StrategyId::new(strategy), confirm(strategy))
            .expect("designating a single live strategy on a fresh engine succeeds");
    }
    // An ACCEPTING transport on purpose: parity must hold because the shared validation rejects the
    // order before either destination is reached, not because the broker happened to refuse it.
    let brokerage = IbBrokerageBridge::new(RecordingIbGateway::new());
    let simulation = WiredPaperSimulation::new();
    let submission = fixture_submission(strategy, MALFORMED_QTY);
    let original = submission.clone();
    let result = engine
        .dispatch_order(
            submission,
            &brokerage,
            &HealthyConnectivityFixture,
            &CollectingConnectivitySink::default(),
            &FreshMarketDataFixture,
            &CollectingStaleDataSink::default(),
            &simulation,
        )
        .map(|receipt| OrderReceipt {
            broker_order_id: format!("{receipt:?}"),
        });
    (original, result)
}

/// The legitimately ACCEPTED path: the same live submission over an accepting transport. Returns Ok
/// and mints a broker order id, so no rejection proof can be derived from it.
// StructuredOrderError is the intentionally rich SRS-ERR-001 envelope; boxing the Err variant here
// would diverge from `ExecutionEngine::submit_live_order`'s own signature, which this mirrors.
#[allow(clippy::result_large_err)]
fn drive_accepted_submission() -> Result<OrderReceipt, StructuredOrderError> {
    let mut engine = ExecutionEngine::default();
    engine
        .designate(StrategyId::new(LIVE_STRATEGY), confirm(LIVE_STRATEGY))
        .expect("designating a single live strategy on a fresh engine succeeds");
    let brokerage = IbBrokerageBridge::new(RecordingIbGateway::new());
    let submission = fixture_submission(LIVE_STRATEGY, FIXTURE_QTY);
    // Same production boundary as the reject paths (route_order, authority-gated), so the
    // fail-closed comparison is like-for-like.
    engine.route_order(
        submission,
        &brokerage,
        &HealthyConnectivityFixture,
        &CollectingConnectivitySink::default(),
        &FreshMarketDataFixture,
        &CollectingStaleDataSink::default(),
    )
}

/// A rejection is REQUIRED for every proof; an `Ok` means the reject path regressed, so surface it as
/// an error rather than skipping the path (fail closed, never vacuous).
fn expect_rejection<'a>(
    result: &'a Result<OrderReceipt, StructuredOrderError>,
    label: &str,
) -> Result<&'a StructuredOrderError, String> {
    match result {
        Err(err) => Ok(err),
        Ok(receipt) => Err(format!(
            "[{label}] expected a structured rejection but got Ok({receipt:?}) — the reject path \
             regressed"
        )),
    }
}

fn fixture_submission(strategy: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(strategy),
        symbol: FIXTURE_SYMBOL.to_string(),
        quantity,
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

// --------------------------------------------------------------------------- //
// Fail-closed fault injection
// --------------------------------------------------------------------------- //

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fault {
    /// Run the transport that ACCEPTS the submission, which is not a rejection.
    Accepted,
}

impl Fault {
    fn parse(spec: &str) -> Result<Self, String> {
        match spec {
            "accepted" => Ok(Self::Accepted),
            other => Err(format!("unknown fault '{other}' (expected accepted)")),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Accepted => "accepted",
        }
    }
}

/// Under `--inject accepted` the broker ACCEPTS, so there is no rejection to build a proof from.
/// Always returns `Err` — no proof line may ever be printed on this path.
fn inject_accepted_fails_closed(fault: Fault, proof: &str) -> Result<(), String> {
    match fault {
        Fault::Accepted => match drive_accepted_submission() {
            Ok(receipt) => Err(format!(
                "inject=accepted: submit_live_order returned Ok({receipt:?}) over an accepting \
                 transport; an accepted submission is not a broker rejection, so {proof} cannot be \
                 proven — no proof asserted"
            )),
            Err(err) => Err(format!(
                "inject=accepted: the accepted Live+Connected+Fresh path unexpectedly rejected \
                 ({err}) — a regression; no {proof} asserted"
            )),
        },
    }
}

/// The parity proof needs a REJECTION on both arms; an accepting transport supplies neither.
fn inject_accepted_fails_closed_parity(fault: Fault) -> Result<(), String> {
    match fault {
        Fault::Accepted => {
            // A WELL-FORMED order over an accepting transport: the shared validation has nothing to
            // reject, so neither arm produces an envelope and parity cannot be demonstrated.
            let engine = ExecutionEngine::default();
            let brokerage = IbBrokerageBridge::new(RecordingIbGateway::new());
            let submission = fixture_submission(PAPER_STRATEGY, FIXTURE_QTY);
            let simulation = WiredPaperSimulation::new();
            match engine.dispatch_order(
                submission,
                &brokerage,
                &HealthyConnectivityFixture,
                &CollectingConnectivitySink::default(),
                &FreshMarketDataFixture,
                &CollectingStaleDataSink::default(),
                &simulation,
            ) {
                Ok(receipt) => Err(format!(
                    "inject=accepted: dispatch_order returned Ok({receipt:?}) for a well-formed \
                     order; with nothing rejected there is no envelope on either arm, so \
                     live-paper-parity cannot be proven — no proof asserted"
                )),
                Err(err) => Err(format!(
                    "inject=accepted: a well-formed order over an accepting transport unexpectedly \
                     rejected ({err}) — a regression; no live-paper-parity asserted"
                )),
            }
        }
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

/// Parse a subcommand that accepts only an optional `--inject <fault>`. Anything else fails closed.
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
        .cloned()
        .ok_or_else(|| format!("{flag} requires a value\n\n{USAGE}"))
}
