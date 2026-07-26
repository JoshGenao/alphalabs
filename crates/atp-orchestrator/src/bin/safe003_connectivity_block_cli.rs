//! SRS-SAFE-003 connectivity-block operator CLI (ERR-2; SyRS SYS-45 / SYS-64 / NFR-R2; StRS SN-2.04).
//!
//! Fault-injects the IB-unreachable / daily-restart connectivity states through the REAL production
//! order-submission authority chain and proves that a Live submission is refused with
//! `CONNECTIVITY_BLOCKED`, creates ZERO IB orders, requests a reconnect, and publishes a structured
//! `ConnectivityEvent` for the dashboard — while a reachable (`Connected`) gate still routes the same
//! order through, and a non-designated paper order never touches IB regardless of connectivity.
//!
//! It lives in `atp-orchestrator` because that is the one layer allowed to see both the execution
//! engine and the IB adapter (SRS-ARCH-002): `atp-execution` must not depend on `atp-adapters`, so
//! the execution-crate CLI structurally cannot reach the brokerage bridge. Every component in the
//! chain is the production one:
//!
//! ```text
//!   ExecutionEngine::dispatch_order         (REAL — the shared order entry; validates then routes)
//!     -> ExecutionEngine::route_order       (REAL — resolves live-ness from the engine-owned
//!                                            LiveDesignation registry, so the single-live invariant
//!                                            is exercised rather than sidestepped by passing
//!                                            StrategyMode::Live)
//!       -> ExecutionEngine::submit_live_order (REAL — the ERR-2/SRS-SAFE-003 connectivity gate)
//!         -> InjectableConnectivity         (FIXTURE — the injected ConnectivityState + reconnect
//!                                            counter, standing in for the deferred real producer)
//!         -> IbBrokerageBridge / RecordingIbGateway (REAL bridge; deterministic mocked transport,
//!                                            the wire-attempt witness: a blocked submission creates
//!                                            ZERO IB orders)
//! ```
//!
//! - `prove-block [--state <unreachable|scheduled-restart>] [--inject connected]` — inject a blocked
//!   connectivity state and prove the designated-live submission is refused with `CONNECTIVITY_BLOCKED`
//!   / `IbGatewayUnreachable`, no IB order is created (`ib-orders-created:0`), exactly one reconnect is
//!   requested, exactly one `ConnectivityEvent` is published (with the correct `scheduled_restart`
//!   flag and the submitting strategy), and a non-designated paper order still simulates. Emits
//!   `connectivity-block-proven:true`.
//!
//! - `routes-when-connected [--inject <unreachable|scheduled-restart>]` — the affirmative positive
//!   control: a `Connected` gate routes the SAME designated-live order through to the broker
//!   (`ib-orders-created:1`, zero reconnects, zero events), proving the block under a blocked state is
//!   genuinely the connectivity gate and not a broken designation. Emits
//!   `connectivity-routes-when-connected:true`.
//!
//! Fail closed / non-vacuity: `prove-block --inject connected` swaps the injected state for a
//! reachable one — the order then routes through, so the block cannot be derived and the proof MUST
//! fail closed with NO proof line. Symmetrically, `routes-when-connected --inject unreachable` (or
//! `scheduled-restart`) forces a block, so "routes through" cannot be derived and MUST fail closed.
//! An unknown subcommand, flag, state, or fault also exits non-zero with no proof line.
//!
//! Scope honesty: this is deterministic FIXTURE verification of the GATE over the real authority
//! chain. No runtime code produces a `ConnectivityState` today — the real producer that maps an IB
//! disconnect (IB 1100 / 2110 / socket loss) onto `Unreachable`, and reconnection + startup readiness
//! back onto `Connected`, is the deferred SRS-EXE-006 / SRS-MD-005 / SRS-EXE-001 connectivity-runtime
//! leg; the "readiness checks pass" half of the acceptance criterion is likewise unenforced (owned by
//! SRS-MD-006 / SRS-ARCH-005 startup readiness). SRS-SAFE-003 stays `passes:false` until that
//! producer, readiness wiring, and a live IB fault-injection e2e land. `transports:FIXTURE` is
//! printed on every path so a drill can never be mistaken for live evidence.

use std::env;
use std::process::ExitCode;

use atp_orchestrator::order_routing_wiring::{
    run_connectivity_block_scenario, ConnectivityBlockEvidence, LiveConnectivityOutcome,
    SCENARIO_PAPER_CONTRAST_STRATEGY,
};
use atp_types::ConnectivityState;

const USAGE: &str = "\
safe003_connectivity_block_cli — SRS-SAFE-003 IB-unreachable connectivity-block operator workflow

USAGE:
    safe003_connectivity_block_cli prove-block           [--state <unreachable|scheduled-restart>] [--inject connected]
    safe003_connectivity_block_cli routes-when-connected [--inject <unreachable|scheduled-restart>]

COMMANDS:
    prove-block            Inject a BLOCKED connectivity state (default unreachable) and prove the
                           designated-live submission is refused with CONNECTIVITY_BLOCKED, creates
                           ZERO IB orders, requests one reconnect, and publishes one ConnectivityEvent
                           (emits the connectivity-block-proven success line only when every check
                           holds).
    routes-when-connected  The positive control: a Connected gate routes the SAME order through to the
                           broker with one IB order and zero reconnects/events (emits the
                           connectivity-routes-when-connected success line).

RUN FLAGS:
    --state <s>            prove-block only: which blocked state to inject — unreachable (the ERR-2
                           connectivity loss) or scheduled-restart (the SRS-MD-005 daily-restart
                           window). Default: unreachable.
    --inject <s>           inject the OPPOSITE class to make the proof non-vacuous. On prove-block only
                           `connected` is valid (a reachable gate routes through, so no block); on
                           routes-when-connected only a blocked state is valid (a block, so no route).
                           Either makes the proof fail closed with no proof line.

Every path prints transports:FIXTURE. The real IB-disconnect -> Unreachable producer + readiness
wiring + a live fault-injection e2e are deferred; SRS-SAFE-003 stays passes:false until they land.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("safe003_connectivity_block_cli: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    // A sole help token prints usage and exits zero. Any OTHER combination — including a help token
    // mixed with other arguments — falls through to normal, fail-closed parsing, so a malformed proof
    // invocation can never exit successfully just because it also carried `--help`.
    if is_sole_help(args) {
        print!("{USAGE}");
        return Ok(());
    }
    let (command, rest) = match args.split_first() {
        Some(parts) => parts,
        None => return Err(format!("missing subcommand\n\n{USAGE}")),
    };
    match command.as_str() {
        "prove-block" => cmd_prove_block(rest),
        "routes-when-connected" => cmd_routes_when_connected(rest),
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

/// True if `args` is EXACTLY one help token. This is the ONLY form that shows usage and exits zero;
/// a help token mixed with anything else must fall through to fail-closed parsing, so a malformed
/// invocation (e.g. `prove-block --nope --help`) can never be rescued into a false success.
fn is_sole_help(args: &[String]) -> bool {
    matches!(args, [only] if matches!(only.as_str(), "help" | "--help" | "-h"))
}

/// Prove the connectivity gate BLOCKS a designated-live submission under an injected blocked state.
fn cmd_prove_block(rest: &[String]) -> Result<(), String> {
    if is_sole_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let (state, inject) = parse_prove_block_args(rest)?;
    if let Some(injected) = inject {
        // --inject connected: a reachable gate routes the order through, so no block can be derived.
        println!("inject:{}", state_label(injected));
        return inject_connected_fails_closed(injected);
    }

    let evidence =
        run_connectivity_block_scenario(state).map_err(|err| format!("scenario error: {err}"))?;
    report_block(state, &evidence)
}

/// The affirmative positive control: a `Connected` gate routes the same order through to the broker.
fn cmd_routes_when_connected(rest: &[String]) -> Result<(), String> {
    if is_sole_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if let Some(injected) = parse_routes_args(rest)? {
        // --inject <blocked>: a blocked gate refuses the order, so "routes through" cannot be derived.
        println!("inject:{}", state_label(injected));
        return inject_blocked_fails_closed(injected);
    }

    let evidence = run_connectivity_block_scenario(ConnectivityState::Connected)
        .map_err(|err| format!("scenario error: {err}"))?;
    report_routes_through(&evidence)
}

// --------------------------------------------------------------------------- //
// Evidence reporting
// --------------------------------------------------------------------------- //

/// Print the block evidence and assert every SRS-SAFE-003 post-condition; refuse the proof (Err, no
/// `:true` line) if any check fails.
fn report_block(state: ConnectivityState, ev: &ConnectivityBlockEvidence) -> Result<(), String> {
    println!(
        "srs:SRS-SAFE-003 proof:connectivity-block state:{} transports:FIXTURE designated:{}",
        state_label(state),
        ev.designated
    );

    let (category, error_type, message) = match &ev.live_outcome {
        LiveConnectivityOutcome::Blocked {
            category,
            error_type,
            message,
        } => (category.as_str(), error_type.as_str(), message.as_str()),
        LiveConnectivityOutcome::RoutedThrough { broker_order_id } => {
            println!("outcome:ROUTED_THROUGH broker-order-id:{broker_order_id}");
            return Err(format!(
                "connectivity-block-proven:false — the designated-live order routed through under \
                 the injected {} state instead of being blocked (broker order id {broker_order_id}); \
                 the SRS-SAFE-003 connectivity gate did not fire",
                state_label(state)
            ));
        }
    };

    let message_nonempty = !message.trim().is_empty();
    // The message must trace SRS-SAFE-003 and name the submitting strategy (SRS-ERR-001 envelope).
    let traces_safe003 = message.contains("SRS-SAFE-003") && message.contains(&ev.designated);
    println!(
        "outcome:BLOCKED category:{category} error_type:{error_type} \
         message-nonempty:{message_nonempty} traces-safe003:{traces_safe003}"
    );

    let expected_scheduled = state == ConnectivityState::ScheduledRestartWindow;
    let scheduled_ok = ev.event_scheduled_restart == Some(expected_scheduled);
    let event_strategy_ok = ev.event_strategy.as_deref() == Some(ev.designated.as_str());
    println!(
        "witness ib-orders-created:{} reconnects:{} events:{} scheduled_restart:{} event-strategy:{}",
        ev.ib_orders_created,
        ev.reconnects,
        ev.events_recorded,
        ev.event_scheduled_restart
            .map(|flag| flag.to_string())
            .unwrap_or_else(|| "none".to_string()),
        ev.event_strategy.as_deref().unwrap_or("none"),
    );

    let contrast_ok = ev.non_designated_sim_receipt.starts_with("paper-");
    println!(
        "contrast[non-designated] strategy:{} route:internal_simulation sim-receipt:{}",
        SCENARIO_PAPER_CONTRAST_STRATEGY, ev.non_designated_sim_receipt
    );

    // Non-vacuous: EVERY post-condition of the SRS-SAFE-003 acceptance criterion must hold.
    let proven = category == "CONNECTIVITY_BLOCKED"
        && error_type == "IbGatewayUnreachable"
        && message_nonempty
        && traces_safe003
        && ev.ib_orders_created == 0
        && ev.reconnects == 1
        && ev.events_recorded == 1
        && scheduled_ok
        && event_strategy_ok
        && contrast_ok;
    if !proven {
        return Err(
            "connectivity-block-proven:false — a blocked live submission did not produce the full \
             SRS-SAFE-003 post-condition (CONNECTIVITY_BLOCKED / IbGatewayUnreachable, zero IB \
             orders, exactly one reconnect, exactly one ConnectivityEvent with the correct \
             scheduled_restart flag and submitting strategy, and a non-designated paper order still \
             simulating)"
                .to_string(),
        );
    }
    println!("connectivity-block-proven:true");
    Ok(())
}

/// Print the positive-control evidence and assert the `Connected` gate routes the order through.
fn report_routes_through(ev: &ConnectivityBlockEvidence) -> Result<(), String> {
    println!(
        "srs:SRS-SAFE-003 proof:routes-when-connected state:connected transports:FIXTURE \
         designated:{}",
        ev.designated
    );

    let broker_order_id = match &ev.live_outcome {
        LiveConnectivityOutcome::RoutedThrough { broker_order_id } => broker_order_id.as_str(),
        LiveConnectivityOutcome::Blocked { category, .. } => {
            println!("outcome:BLOCKED category:{category}");
            return Err(format!(
                "connectivity-routes-when-connected:false — a Connected gate blocked the live order \
                 ({category}); the block must be SELECTIVE to the unreachable state, else the gate \
                 would disable the live path even when IB is healthy"
            ));
        }
    };

    let broker_id_nonempty = !broker_order_id.trim().is_empty();
    println!(
        "outcome:ROUTED_THROUGH broker-order-id:{broker_order_id} broker-id-nonempty:{broker_id_nonempty}"
    );
    println!(
        "witness ib-orders-created:{} reconnects:{} events:{}",
        ev.ib_orders_created, ev.reconnects, ev.events_recorded
    );

    let contrast_ok = ev.non_designated_sim_receipt.starts_with("paper-");
    println!(
        "contrast[non-designated] strategy:{} route:internal_simulation sim-receipt:{}",
        SCENARIO_PAPER_CONTRAST_STRATEGY, ev.non_designated_sim_receipt
    );

    let proven = broker_id_nonempty
        && ev.ib_orders_created == 1
        && ev.reconnects == 0
        && ev.events_recorded == 0
        && ev.event_scheduled_restart.is_none()
        && contrast_ok;
    if !proven {
        return Err(
            "connectivity-routes-when-connected:false — a Connected live submission did not route \
             cleanly through to the broker (exactly one IB order created, zero reconnects, zero \
             connectivity events, and a non-designated paper order still simulating)"
                .to_string(),
        );
    }
    println!("connectivity-routes-when-connected:true");
    Ok(())
}

// --------------------------------------------------------------------------- //
// Fail-closed fault injection (non-vacuity)
// --------------------------------------------------------------------------- //

/// Under `--inject connected` the gate is reachable, so the live order routes through and no block
/// can be derived. Always returns `Err` — no proof line may ever be printed on this path.
fn inject_connected_fails_closed(injected: ConnectivityState) -> Result<(), String> {
    let ev = run_connectivity_block_scenario(injected)
        .map_err(|err| format!("scenario error: {err}"))?;
    match ev.live_outcome {
        LiveConnectivityOutcome::RoutedThrough { broker_order_id } => Err(format!(
            "inject=connected: the live order routed through over a reachable gate (broker order id \
             {broker_order_id}); a routed order is not a block, so connectivity-block cannot be \
             proven — no proof asserted"
        )),
        LiveConnectivityOutcome::Blocked { category, .. } => Err(format!(
            "inject=connected: a Connected gate unexpectedly blocked the live order ({category}) — a \
             regression; no connectivity-block asserted"
        )),
    }
}

/// Under `--inject <blocked>` the gate refuses the order, so "routes through" cannot be demonstrated.
/// Always returns `Err` — no proof line may ever be printed on this path.
fn inject_blocked_fails_closed(injected: ConnectivityState) -> Result<(), String> {
    let ev = run_connectivity_block_scenario(injected)
        .map_err(|err| format!("scenario error: {err}"))?;
    match ev.live_outcome {
        LiveConnectivityOutcome::Blocked { category, .. } => Err(format!(
            "inject={}: the live order was blocked ({category}) over an unreachable gate; a blocked \
             order is not a routed one, so routes-when-connected cannot be proven — no proof asserted",
            state_label(injected)
        )),
        LiveConnectivityOutcome::RoutedThrough { broker_order_id } => Err(format!(
            "inject={}: a blocked gate unexpectedly routed the live order through (broker order id \
             {broker_order_id}) — a regression; no routes-when-connected asserted",
            state_label(injected)
        )),
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing (fail-closed)
// --------------------------------------------------------------------------- //

/// Parse `prove-block`'s flags: an optional `--state` (a blocked state, default unreachable) and an
/// optional `--inject connected` (the opposite-class non-vacuity fault). Anything else fails closed.
fn parse_prove_block_args(
    rest: &[String],
) -> Result<(ConnectivityState, Option<ConnectivityState>), String> {
    let mut state: Option<ConnectivityState> = None;
    let mut inject: Option<ConnectivityState> = None;
    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--state" => {
                let value = take_value(&mut iter, flag)?;
                let parsed = parse_state(&value)?;
                if !parsed.is_blocked() {
                    return Err(format!(
                        "prove-block proves a BLOCKED connectivity state; '{value}' is not blocked \
                         — use `routes-when-connected` to prove the Connected control\n\n{USAGE}"
                    ));
                }
                if state.is_some() {
                    return Err(format!("--state given more than once\n\n{USAGE}"));
                }
                state = Some(parsed);
            }
            "--inject" => {
                let value = take_value(&mut iter, flag)?;
                let parsed = parse_state(&value)?;
                if parsed != ConnectivityState::Connected {
                    return Err(format!(
                        "prove-block's non-vacuity fault is `--inject connected` (the opposite \
                         class); '{value}' is not a valid injection here\n\n{USAGE}"
                    ));
                }
                if inject.is_some() {
                    return Err(format!("--inject given more than once\n\n{USAGE}"));
                }
                inject = Some(parsed);
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    Ok((state.unwrap_or(ConnectivityState::Unreachable), inject))
}

/// Parse `routes-when-connected`'s flags: an optional `--inject <blocked>` (the opposite-class
/// non-vacuity fault). Anything else fails closed.
fn parse_routes_args(rest: &[String]) -> Result<Option<ConnectivityState>, String> {
    let mut inject: Option<ConnectivityState> = None;
    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--inject" => {
                let value = take_value(&mut iter, flag)?;
                let parsed = parse_state(&value)?;
                if !parsed.is_blocked() {
                    return Err(format!(
                        "routes-when-connected's non-vacuity fault is `--inject \
                         unreachable|scheduled-restart` (a blocked state); '{value}' is not a \
                         blocked state\n\n{USAGE}"
                    ));
                }
                if inject.is_some() {
                    return Err(format!("--inject given more than once\n\n{USAGE}"));
                }
                inject = Some(parsed);
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    Ok(inject)
}

/// Parse a connectivity-state spec. Fail closed on anything unrecognised.
fn parse_state(spec: &str) -> Result<ConnectivityState, String> {
    match spec {
        "unreachable" => Ok(ConnectivityState::Unreachable),
        "scheduled-restart" => Ok(ConnectivityState::ScheduledRestartWindow),
        "connected" => Ok(ConnectivityState::Connected),
        other => Err(format!(
            "unknown connectivity state '{other}' (expected unreachable | scheduled-restart | \
             connected)\n\n{USAGE}"
        )),
    }
}

/// The wire spelling of a connectivity state (stable CLI vocabulary).
fn state_label(state: ConnectivityState) -> &'static str {
    match state {
        ConnectivityState::Unreachable => "unreachable",
        ConnectivityState::ScheduledRestartWindow => "scheduled-restart",
        ConnectivityState::Connected => "connected",
    }
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .cloned()
        .ok_or_else(|| format!("{flag} requires a value\n\n{USAGE}"))
}
