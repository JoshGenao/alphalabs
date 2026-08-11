//! SRS-RESV-004 / SyRS SYS-49b / SYS-49c Hot-Swap demotion operator CLI.
//!
//! Three subcommands, one per operator question:
//!
//! * `demote` runs a demotion end to end and says what happened. It drives the REAL sequence,
//!   probe, gate, durable lockout and SRS-NOTIF-001 notifier over FIXTURE transports (see
//!   [`atp_orchestrator::hot_swap_demotion_drill`]).
//! * `status` answers "is a demotion pending, and what does it say?". Three-state: no lockout,
//!   a held lockout, or an unreadable one — and the third is an ERROR, never a quiet "nothing
//!   is pending".
//! * `resolve` is the SyRS SYS-49c (d) manual resolution that clears a lockout, requiring an
//!   explicit operator acknowledgement.
//!
//! Output is the repo's deterministic `key:value` proof-line stream, which `python/atp_hotswap`
//! parses. A value that cannot be represented on one line is refused rather than emitted
//! mangled: `StrategyId` validates nothing, so a control character in an id would otherwise
//! split one fact into two proof lines and hand the parser a different id than the system holds.

use std::collections::BTreeMap;
use std::env;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use atp_execution::outbox::BrokerReconcileError;
use atp_orchestrator::demotion_pending_store::{
    read_state, resolve, DemotionPendingRecord, DemotionPendingState,
};
use atp_orchestrator::hot_swap_demotion_drill::{
    outcome_label, run_fixture_demotion, DemotionDrillOutcome, DemotionScenario,
};
use atp_types::{SideEffectOutcome, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS};

const USAGE: &str = "\
resv004_hot_swap_demotion_cli — SRS-RESV-004 Hot-Swap demotion (SyRS SYS-49b / SYS-49c)

USAGE:
    resv004_hot_swap_demotion_cli <SUBCOMMAND> [FLAGS]

SUBCOMMANDS:
    demote      Run a demotion end to end (real sequence + gate + lockout + notifier,
                fixture IB/SMTP/SMS transports) and report the disposition.
    status      Report the DURABLE demotion-pending state at --state. Three-state: no
                lockout, a held one, or an UNREADABLE one (an error — never a silent
                'nothing is pending').
    resolve     SYS-49c (d) manual resolution: clear the lockout so promotion may
                proceed again. Requires --confirm.
    help        Print this help.

demote FLAGS:
    --demoting <id>              the live strategy to demote (required)
    --candidate <id>             the reservoir strategy that would be promoted (required)
    --state <path>               the DURABLE demotion-pending lockout (required — a
                                 demotion whose block cannot outlive the process does
                                 not satisfy SYS-49c (d))
    --expect <disposition>       what this scenario must produce: flat | demotion-pending |
                                 blocked-pending | probe-inconsistent | refused (required).
                                 The run exits non-zero if it produces anything else, so a
                                 drill cannot pass by doing something other than it claims.
    --designated-live <id>       who the live registry names (repeatable). Omit to designate
                                 the demoting strategy, which is the normal case. Use it to
                                 exercise the SyRS SYS-2a refusal: naming someone else, or
                                 naming two strategies, must refuse BEFORE any broker call.
    --no-live-strategy           designate NOBODY live — the account's positions are then not
                                 this request's to liquidate.
    --timeout-seconds <n>        SYS-49b step 4 budget (default 60)
    --position <SYM:qty>         an open position to liquidate, signed (repeatable)
    --resting <SYM>              a resting order that must be cancelled (repeatable)
    --flat-after-seconds <n>     positions go flat this many seconds in. OMIT to never
                                 reach flat, which is what produces the SYS-49c timeout.
    --observed-at <n>            event timestamp, epoch seconds (default 1715000000)
    --position-fault <kind>      make the position view unreadable:
                                 connectivity | stale | timeout | unavailable | malformed
    --fail-signal-halt           SYS-49b (1) fails
    --fail-cancels               SYS-49b (2) fails
    --fail-liquidations          SYS-49b (3) fails
    --fail-unfilled-cancel       SYS-49c (b) fails
    --fail-email                 the SRS-NOTIF-001 email transport fails
    --fail-sms                   the SRS-NOTIF-001 SMS transport fails
    --fail-paper-transition      the container cannot move to paper simulation

status FLAGS:
    --state <path>               the DURABLE demotion-pending lockout (required)

resolve FLAGS:
    --state <path>               the DURABLE demotion-pending lockout (required)
    --confirm <acknowledgement>  the operator's statement that the unfilled positions
                                 have been inspected and resolved (required, non-blank).
                                 This is the control between an automated retry and a
                                 live position nobody looked at.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("resv004_hot_swap_demotion_cli: {err}");
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
        "demote" => cmd_demote(rest),
        "status" => cmd_status(rest),
        "resolve" => cmd_resolve(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

fn wants_help(args: &[String]) -> bool {
    args.iter()
        .any(|arg| matches!(arg.as_str(), "help" | "--help" | "-h"))
}

// --------------------------------------------------------------------------- //
// Proof-line emission
// --------------------------------------------------------------------------- //

/// Emit one `key:value` proof line, refusing a value that cannot survive the stream.
///
/// The parser splits on the FIRST colon and on newlines, so a value carrying a newline (or any
/// other C0 control) would silently become two lines — one of them a fabricated key. `StrategyId`
/// validates nothing, so this is reachable from real data, and the fail-closed answer is to
/// refuse the whole command: a status that cannot be stated exactly must read as unavailable, not
/// as a different status.
fn proof_representable(key: &str, value: &str) -> Result<(), String> {
    if let Some(bad) = value.chars().find(|c| (*c as u32) < 0x20 || *c == '\u{7f}') {
        return Err(format!(
            "refusing to emit '{key}': the value carries control character U+{:04X}, which \
             would split one fact across two proof lines",
            bad as u32
        ));
    }
    Ok(())
}

fn proof(key: &str, value: &str) -> Result<(), String> {
    proof_representable(key, value)?;
    println!("{key}:{value}");
    Ok(())
}

/// Every value `emit_record` would print, checked without printing any of them.
///
/// Used to prove a resolution is REPORTABLE before the lockout is removed — see `cmd_resolve`.
fn record_is_representable(record: &DemotionPendingRecord) -> Result<(), String> {
    proof_representable("demoting-strategy-id", record.demoting_strategy_id.as_str())?;
    proof_representable(
        "candidate-strategy-id",
        record.candidate_strategy_id.as_str(),
    )?;
    for (key, outcome) in [
        ("liquidation-cancel", &record.liquidation_cancel),
        ("operator-alert", &record.operator_alert),
    ] {
        if let SideEffectOutcome::Failed { reason } = outcome {
            proof_representable(&format!("{key}-reason"), reason)?;
        }
    }
    Ok(())
}

fn proof_u64(key: &str, value: u64) -> Result<(), String> {
    proof(key, &value.to_string())
}

fn proof_bool(key: &str, value: bool) -> Result<(), String> {
    proof(key, if value { "true" } else { "false" })
}

/// Print a side-effect outcome as a label plus, only when it failed, its reason — the same
/// present-iff-meaningful shape the durable record uses.
fn proof_outcome(key: &str, outcome: &SideEffectOutcome) -> Result<(), String> {
    proof(key, outcome_label(outcome))?;
    if let SideEffectOutcome::Failed { reason } = outcome {
        proof(&format!("{key}-reason"), reason)?;
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// Argument parsing — allowlist, single pass
// --------------------------------------------------------------------------- //

/// One pass over the arguments, refusing anything not explicitly allowed.
///
/// A scan-for-known-flags parser silently drops a typo (`--stat`) and falls back to a default
/// the operator never chose, then reports success. Unknown flags, duplicates, missing values, and
/// a value that is itself a flag are all refused here.
struct Args {
    values: BTreeMap<String, String>,
    repeated: BTreeMap<String, Vec<String>>,
    flags: Vec<String>,
}

impl Args {
    fn parse(
        args: &[String],
        valued: &[&str],
        repeatable: &[&str],
        boolean: &[&str],
    ) -> Result<Self, String> {
        let mut values: BTreeMap<String, String> = BTreeMap::new();
        let mut repeated: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut flags: Vec<String> = Vec::new();
        let mut index = 0;
        while index < args.len() {
            let token = args[index].as_str();
            if !token.starts_with("--") {
                return Err(format!("unexpected argument '{token}'\n\n{USAGE}"));
            }
            if boolean.contains(&token) {
                if flags.iter().any(|flag| flag == token) {
                    return Err(format!("duplicate flag '{token}'\n\n{USAGE}"));
                }
                flags.push(token.to_string());
                index += 1;
                continue;
            }
            let is_valued = valued.contains(&token);
            let is_repeatable = repeatable.contains(&token);
            if !is_valued && !is_repeatable {
                return Err(format!("unknown flag '{token}'\n\n{USAGE}"));
            }
            let value = match args.get(index + 1) {
                // A value that is itself a flag means the operator omitted one; consuming it
                // would silently apply the NEXT flag's name as this flag's value.
                Some(value) if !value.starts_with("--") => value.clone(),
                _ => return Err(format!("flag '{token}' requires a value\n\n{USAGE}")),
            };
            if is_repeatable {
                repeated.entry(token.to_string()).or_default().push(value);
            } else {
                if values.contains_key(token) {
                    return Err(format!("duplicate flag '{token}'\n\n{USAGE}"));
                }
                values.insert(token.to_string(), value);
            }
            index += 2;
        }
        Ok(Self {
            values,
            repeated,
            flags,
        })
    }

    fn get(&self, flag: &str) -> Option<&str> {
        self.values.get(flag).map(String::as_str)
    }

    fn require(&self, flag: &str) -> Result<&str, String> {
        self.get(flag)
            .ok_or_else(|| format!("flag '{flag}' is required\n\n{USAGE}"))
    }

    fn list(&self, flag: &str) -> &[String] {
        self.repeated.get(flag).map(Vec::as_slice).unwrap_or(&[])
    }

    fn is_set(&self, flag: &str) -> bool {
        self.flags.iter().any(|set| set == flag)
    }

    fn u64(&self, flag: &str, default: u64) -> Result<u64, String> {
        match self.get(flag) {
            None => Ok(default),
            Some(raw) => raw
                .parse::<u64>()
                .map_err(|_| format!("flag '{flag}' requires a non-negative integer, got '{raw}'")),
        }
    }
}

fn state_path(args: &Args) -> Result<PathBuf, String> {
    Ok(PathBuf::from(args.require("--state")?))
}

// --------------------------------------------------------------------------- //
// status
// --------------------------------------------------------------------------- //

fn cmd_status(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let args = Args::parse(rest, &["--state"], &[], &[])?;
    let path = state_path(&args)?;
    emit_status(&path)
}

/// Print the three-state lockout status.
///
/// `Clear` and `Pending` are both answers. `Unreadable` is NOT: it exits non-zero so the caller —
/// the Python client, and through it the UI-5 pane — renders *unknown* rather than a confident
/// "no demotion is pending", which is the exact false all-clear the lockout exists to prevent.
fn emit_status(path: &Path) -> Result<(), String> {
    match read_state(path) {
        DemotionPendingState::Clear => {
            proof("state-source", "clear")?;
            proof_bool("demotion-pending", false)?;
            proof_bool("promotion-blocked", false)?;
            proof("demotion-detail", "no demotion is pending")?;
            Ok(())
        }
        DemotionPendingState::Pending(record) => {
            proof("state-source", "pending")?;
            proof_bool("demotion-pending", true)?;
            proof_bool("promotion-blocked", true)?;
            emit_record(&record)?;
            proof(
                "demotion-detail",
                &DemotionPendingState::Pending(record.clone()).reason(),
            )
        }
        DemotionPendingState::Unreadable { reason } => Err(format!(
            "demotion-pending lockout at {} is UNREADABLE ({reason}); refusing to report a \
             demotion state that cannot be evidenced — promotion stays blocked",
            path.display()
        )),
    }
}

fn emit_record(record: &DemotionPendingRecord) -> Result<(), String> {
    proof("demoting-strategy-id", record.demoting_strategy_id.as_str())?;
    proof(
        "candidate-strategy-id",
        record.candidate_strategy_id.as_str(),
    )?;
    proof_u64("elapsed-seconds", record.elapsed_seconds)?;
    proof_u64("timeout-seconds", record.timeout_seconds)?;
    proof_u64("observed-at-seconds", record.observed_at_seconds)?;
    proof_outcome("liquidation-cancel", &record.liquidation_cancel)?;
    proof_outcome("operator-alert", &record.operator_alert)
}

// --------------------------------------------------------------------------- //
// resolve
// --------------------------------------------------------------------------- //

fn cmd_resolve(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let args = Args::parse(rest, &["--state", "--confirm"], &[], &[])?;
    let path = state_path(&args)?;
    let acknowledgement = args.require("--confirm")?;

    // Prove the resolution can be REPORTED before it is performed. `resolve` deletes the
    // lockout; if a proof line were rejected afterwards the command would exit non-zero having
    // already unblocked promotion, leaving no valid record that a manual resolution happened —
    // the operator would read a failure and the system would read "clear". Do every fallible
    // check before the destructive write, not after it.
    // `(found by /codex:adversarial-review, SRS-RESV-004 r4 [critical])`
    proof_representable("operator-acknowledgement", acknowledgement)?;
    // Not-pending / unreadable need no check here: `resolve` refuses both on its own, with a
    // better message than anything this could produce.
    if let DemotionPendingState::Pending(held) = read_state(&path) {
        record_is_representable(&held)?;
    }

    let cleared = resolve(&path, acknowledgement).map_err(|error| error.to_string())?;

    proof("resolved", "true")?;
    emit_record(&cleared)?;
    proof("operator-acknowledgement", acknowledgement)?;
    // Read back from disk rather than asserting from the call: the operator surface reports the
    // END STATE, and a per-call success is not one.
    match read_state(&path) {
        DemotionPendingState::Clear => {
            proof_bool("promotion-blocked", false)?;
            proof("state-source", "clear")
        }
        other => Err(format!(
            "resolve reported success but the lockout still blocks promotion: {}",
            other.reason()
        )),
    }
}

// --------------------------------------------------------------------------- //
// demote
// --------------------------------------------------------------------------- //

const DISPOSITIONS: [&str; 5] = [
    "flat",
    "demotion-pending",
    "blocked-pending",
    "probe-inconsistent",
    "refused",
];

fn cmd_demote(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let args = Args::parse(
        rest,
        &[
            "--demoting",
            "--candidate",
            "--state",
            "--expect",
            "--timeout-seconds",
            "--flat-after-seconds",
            "--observed-at",
            "--position-fault",
        ],
        &["--position", "--resting", "--designated-live"],
        &[
            "--fail-signal-halt",
            "--fail-cancels",
            "--fail-liquidations",
            "--fail-unfilled-cancel",
            "--fail-email",
            "--fail-sms",
            "--fail-paper-transition",
            "--no-live-strategy",
        ],
    )?;

    let expected = args.require("--expect")?.to_string();
    if !DISPOSITIONS.contains(&expected.as_str()) {
        return Err(format!(
            "--expect must be one of {}, got '{expected}'\n\n{USAGE}",
            DISPOSITIONS.join(" | ")
        ));
    }

    let mut positions = BTreeMap::new();
    for raw in args.list("--position") {
        let (symbol, quantity) = raw
            .split_once(':')
            .ok_or_else(|| format!("--position expects <SYMBOL>:<signed quantity>, got '{raw}'"))?;
        if symbol.trim().is_empty() {
            return Err(format!("--position '{raw}' names no symbol"));
        }
        let quantity: i64 = quantity
            .parse()
            .map_err(|_| format!("--position '{raw}' has a non-integer quantity"))?;
        if positions.insert(symbol.to_string(), quantity).is_some() {
            // Two statements about one symbol's net position, and they may disagree.
            return Err(format!("--position names {symbol} more than once"));
        }
    }

    let scenario = DemotionScenario {
        demoting_strategy_id: args.require("--demoting")?.to_string(),
        candidate_strategy_id: args.require("--candidate")?.to_string(),
        timeout_seconds: args.u64("--timeout-seconds", HOT_SWAP_DEMOTION_TIMEOUT_SECONDS)?,
        positions,
        resting_orders: args.list("--resting").to_vec(),
        flat_after_seconds: match args.get("--flat-after-seconds") {
            None => None,
            Some(_) => Some(args.u64("--flat-after-seconds", 0)?),
        },
        state_path: state_path(&args)?,
        observed_at_seconds: args.u64("--observed-at", 1_715_000_000)?,
        fail_signal_halt: args.is_set("--fail-signal-halt"),
        fail_cancels: args.is_set("--fail-cancels"),
        fail_liquidations: args.is_set("--fail-liquidations"),
        fail_unfilled_cancel: args.is_set("--fail-unfilled-cancel"),
        fail_email: args.is_set("--fail-email"),
        fail_sms: args.is_set("--fail-sms"),
        fail_paper_transition: args.is_set("--fail-paper-transition"),
        designated_live: match (
            args.is_set("--no-live-strategy"),
            args.list("--designated-live"),
        ) {
            (true, ids) if !ids.is_empty() => {
                return Err(
                    "--no-live-strategy and --designated-live contradict each other".to_string(),
                )
            }
            // An explicitly EMPTY live registry, so the SyRS SYS-2a "nothing to demote" refusal
            // is reachable — distinct from the default, which designates the demoting strategy.
            (true, _) => Some(Vec::new()),
            (false, []) => None,
            (false, ids) => Some(ids.to_vec()),
        },
        position_fault: match args.get("--position-fault") {
            None => None,
            Some("connectivity") => Some(BrokerReconcileError::connectivity_blocked(
                "fixture: IB gateway unreachable",
            )),
            Some("stale") => Some(BrokerReconcileError::stale_data(
                "fixture: position view too stale to trust",
            )),
            Some("timeout") => Some(BrokerReconcileError::timeout(
                "fixture: position query deadline elapsed",
            )),
            Some("unavailable") => Some(BrokerReconcileError::unavailable(
                "fixture: position service down",
            )),
            Some("malformed") => Some(BrokerReconcileError::malformed_snapshot(
                "fixture: position snapshot internally inconsistent",
            )),
            Some(other) => {
                return Err(format!(
                    "--position-fault expects connectivity|stale|timeout|unavailable|malformed, \
                     got '{other}'"
                ))
            }
        },
    };

    let outcome = run_fixture_demotion(&scenario)?;
    emit_drill(&outcome)?;

    // The run is only a pass if it did what the scenario claimed it would. Without this a
    // scenario configured to time out could quietly go flat — or vice versa — and the exit code
    // would still say success, which is how a drill starts proving nothing.
    if outcome.disposition != expected {
        return Err(format!(
            "scenario expected disposition '{expected}' but the demotion produced \
             '{}'{}",
            outcome.disposition,
            outcome
                .error_message
                .as_deref()
                .map(|message| format!(" ({message})"))
                .unwrap_or_default(),
        ));
    }
    Ok(())
}

fn emit_drill(outcome: &DemotionDrillOutcome) -> Result<(), String> {
    proof("disposition", &outcome.disposition)?;
    // The tier travels into any record made from this run: the gate, probe, lockout and
    // notifier are real, the IB socket and the SMTP/SMS transports are not.
    proof("transports", outcome.transports)?;
    proof_bool("promotion-blocked", outcome.promotion_blocked)?;
    // Emitted ONLY when there is a block to describe. On the flat path there is none, and a
    // bare `promotion-block-is-durable:false` there reads as a degradation — the same shape a
    // failed lockout write produces — when the truth is that the question does not apply.
    if outcome.promotion_blocked {
        proof_bool(
            "promotion-block-is-durable",
            outcome.promotion_block_is_durable,
        )?;
    }

    // SYS-49b steps 1-3. A swap refused before it started never ran them, and an empty
    // sequence reports `fully_clean() == true` — "nothing failed" is the same shape as
    // "nothing happened". `sequence-ran` keeps them apart, and the cleanliness verdicts are
    // only emitted when there is a run to describe.
    let sequence_ran = outcome.sequence.signal_halt != SideEffectOutcome::NotAttempted;
    proof_bool("sequence-ran", sequence_ran)?;
    proof_outcome("signal-halt", &outcome.sequence.signal_halt)?;
    proof_u64(
        "resting-orders-cancelled",
        outcome.sequence.resting_order_cancels.len() as u64,
    )?;
    proof_u64(
        "resting-order-cancel-failures",
        outcome
            .sequence
            .resting_order_cancels
            .iter()
            .filter(|cancel| cancel.outcome.is_failed())
            .count() as u64,
    )?;
    proof_u64(
        "liquidations-submitted",
        outcome.sequence.liquidations.len() as u64,
    )?;
    proof_u64(
        "liquidation-failures",
        outcome
            .sequence
            .liquidations
            .iter()
            .filter(|liquidation| liquidation.outcome.is_failed())
            .count() as u64,
    )?;
    if sequence_ran {
        proof_bool("sequence-fully-clean", outcome.sequence.fully_clean())?;
        proof_bool(
            "sequence-safe-to-accept-flat",
            outcome.sequence.safe_to_accept_flat(),
        )?;
        if let Some(reason) = outcome.sequence.degradation_reason() {
            proof("sequence-degradation", &reason)?;
        }
    }

    // SYS-49c side effects.
    proof_u64(
        "unfilled-order-cancels",
        outcome.unfilled_cancels.len() as u64,
    )?;
    proof_u64("operator-pages", outcome.notifications.len() as u64)?;
    for event in &outcome.notifications {
        // Every required channel's delivery, individually — "the dispatcher was called" is not
        // "the operator was paged".
        for delivery in event.deliveries() {
            proof_bool(
                &format!(
                    "operator-page-delivered-{}",
                    delivery.channel().as_str().to_lowercase()
                ),
                delivery.outcome().is_delivered(),
            )?;
        }
    }
    if let Some(degradation) = &outcome.probe_degradation {
        proof("probe-degradation", degradation)?;
    }

    // The audit events the deferred SRS-LOG-001 sink will consume.
    proof_u64("demotion-events", outcome.demotion_events.len() as u64)?;
    for event in &outcome.demotion_events {
        proof_outcome("event-liquidation-cancel", &event.liquidation_cancel)?;
        proof_outcome("event-operator-alert", &event.operator_alert)?;
        proof_outcome("event-demotion-pending", &event.demotion_pending)?;
    }

    // SYS-49b closing clause.
    proof_u64("paper-transitions", outcome.paper_moved.len() as u64)?;
    if let Some(completed) = &outcome.completed {
        proof(
            "completed-demoting-strategy-id",
            completed.demoting_strategy_id.as_str(),
        )?;
        proof(
            "completed-candidate-strategy-id",
            completed.candidate_strategy_id.as_str(),
        )?;
        proof_u64("completed-elapsed-seconds", completed.elapsed_seconds)?;
    }
    if let Some(error_type) = &outcome.error_type {
        proof("error-type", error_type)?;
    }
    if let Some(message) = &outcome.error_message {
        proof("error-message", message)?;
    }
    Ok(())
}
