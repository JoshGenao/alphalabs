//! SRS-MD-005 scheduled IB Gateway restart-window operator CLI
//! (SyRS SYS-75 / SYS-45 / SYS-46 / NFR-R2; StRS C-2, SN-2.04, SN-2.05).
//!
//! Drives the REAL production authority chain at an injected instant against a
//! REAL TCP endpoint, and proves the three behaviours SYS-75 asks for:
//!
//! * `prove-suspension` — 60 s before the expected restart, order submission AND
//!   market-data requests are suspended, ZERO IB orders are created, and the
//!   connectivity notification is SUPPRESSED (nothing is sent).
//! * `prove-escalation` — after the window, with the gateway still dead, the
//!   state is UNREACHABLE with `scheduled_restart:false` and the notification is
//!   DISPATCHED — the SYS-45 / SYS-46 escalation.
//! * `prove-resume` — inside the window with the gateway answering again, normal
//!   operations resume: the same order routes through to the broker and the same
//!   subscription is admitted.
//!
//! Each takes an `--inject` that forces the OPPOSITE class, so the proof must
//! fail closed with no proof line. Those are the non-vacuity controls: a suite
//! that only asserts "refused during the window" passes on a gate that refuses
//! always, which would silently disable market data and live trading
//! altogether.
//!
//! It lives in `atp-orchestrator` because that is the one layer allowed to see
//! both the execution gate and the adapter (SRS-ARCH-002): `atp-execution` must
//! not depend on `atp-adapters`, so an execution-crate binary structurally
//! cannot reach the reachability probe.
//!
//! Determinism: the instant arrives as `--now-ns` / `--restart-ns` and nothing
//! reads the wall clock, so a run re-runs byte-identically. The gateway
//! endpoint is a loopback port this binary binds and releases itself — it never
//! touches the shared IB ports, so the tool is safe to run beside other agents.
//!
//! Scope honesty: `transports:FIXTURE` is printed on every path. The email and
//! push transports are the SRS-NOTIF-001 fixtures, so a drill sends nothing and
//! can never be mistaken for a live page; the brokerage transport is the
//! deterministic recording gateway. What IS real here is the whole decision
//! chain — the window arithmetic, the connectivity producer, the TCP probe, the
//! execution gate, the subscription gate, and the notification suppression.

use std::env;
use std::net::{Ipv4Addr, SocketAddr, TcpListener};
use std::process::ExitCode;

use atp_adapters::gateway_reachability::ReachabilityOutcome;
use atp_orchestrator::restart_window_scenario::{
    run_restart_window_scenario, AlertDisposition, LiveOrderOutcome, RestartWindowEvidence,
    RestartWindowScenario, SCENARIO_SYMBOL,
};
use atp_types::{
    ConnectivityState, MarketDataAdmission, RestartPhase, DEFAULT_RESTART_SUSPEND_LEAD_SECONDS,
    DEFAULT_RESTART_WINDOW_SECONDS, NANOS_PER_SECOND,
};

/// The default expected-restart instant: 2026-09-04T03:45:00Z, which is
/// 23:45 America/New_York on 2026-09-03 (EDT) — the SYS-75 restart time.
///
/// A CONSTANT, not a wall-clock read, so the tool is reproducible. Resolving
/// "23:45 ET" for an arbitrary date is a calendar concern and belongs to the
/// DST-aware `atp_strategy.calendar` authority on the Python side; this binary
/// takes the resolved instant as `--restart-ns`.
const DEFAULT_RESTART_NS: i64 = 1_788_493_500 * NANOS_PER_SECOND;

const USAGE: &str = "\
md005_connectivity_restart_window_cli — SRS-MD-005 scheduled IB Gateway restart window

USAGE:
    md005_connectivity_restart_window_cli prove-suspension [RUN FLAGS] [--inject outside-window]
    md005_connectivity_restart_window_cli prove-escalation [RUN FLAGS] [--inject inside-window]
    md005_connectivity_restart_window_cli prove-resume     [RUN FLAGS] [--inject dead-gateway]

COMMANDS:
    prove-suspension   SyRS SYS-75(a)/(b). Evaluate inside the 60 s pre-restart lead and prove the
                       designated-live submission is suspended, ZERO IB orders are created, the
                       market-data subscription is refused, and the connectivity notification is
                       suppressed with nothing sent on any channel.
    prove-escalation   SyRS SYS-75 escalation. Evaluate after the window with the gateway still
                       unreachable and prove the state is UNREACHABLE without the maintenance
                       marker, and that the operator notification is DISPATCHED rather than
                       suppressed.
    prove-resume       SyRS SYS-75(c)/(d). Evaluate inside the window with the gateway answering
                       and prove normal operations resume: the same order routes through to the
                       broker and the same subscription is admitted.

RUN FLAGS:
    --restart-ns <i64>      Expected restart instant, epoch nanoseconds. Default: the SYS-75
                            23:45 ET reference instant compiled in.
    --now-ns <i64>          Instant to evaluate at. Default: derived from --restart-ns so the
                            subcommand lands in the phase it is proving.
    --lead-seconds <i64>    SyRS SYS-75(a) suspension lead. Default: 60.
    --window-seconds <i64>  SyRS SYS-75(b) window duration. Default: 300.
    --inject <fault>        Force the OPPOSITE class so the proof cannot be derived. On
                            prove-suspension only `outside-window`; on prove-escalation only
                            `inside-window`; on prove-resume only `dead-gateway`. Any of them makes
                            the run fail closed with no proof line.
    --help                  Show this message (only as the sole argument).

EXIT CODES:
    0  the named proof held
    1  the proof could not be derived, an injected fault fired, or the arguments were rejected
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    if is_sole_help(&args) {
        println!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("error: {message}");
            eprintln!();
            eprintln!("{USAGE}");
            ExitCode::FAILURE
        }
    }
}

/// `--help` is honoured ONLY as the sole argument. Accepting it anywhere would
/// let `prove-suspension --help` exit 0 while proving nothing, which is a
/// success exit code attached to no evidence.
fn is_sole_help(args: &[String]) -> bool {
    args.len() == 1 && (args[0] == "--help" || args[0] == "-h")
}

fn run(args: &[String]) -> Result<(), String> {
    let (command, rest) = args
        .split_first()
        .ok_or_else(|| "no subcommand given".to_string())?;
    match command.as_str() {
        "prove-suspension" => cmd_prove_suspension(rest),
        "prove-escalation" => cmd_prove_escalation(rest),
        "prove-resume" => cmd_prove_resume(rest),
        other => Err(format!("unknown subcommand `{other}`")),
    }
}

// --------------------------------------------------------------------------- //
// Arguments
// --------------------------------------------------------------------------- //

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Injection {
    None,
    OutsideWindow,
    InsideWindow,
    DeadGateway,
}

#[derive(Debug, Clone)]
struct RunArgs {
    restart_ns: i64,
    now_ns: Option<i64>,
    lead_ns: i64,
    window_ns: i64,
    injection: Injection,
}

impl RunArgs {
    /// Seed the durations from the SRS-ARCH-005 catalogue keys, falling back to
    /// the SyRS SYS-75 defaults.
    ///
    /// Without this the keys VALIDATED and changed nothing: `.env.example`, the
    /// config README and the catalogue all describe them as controlling the
    /// suspension window, while the binary read compiled-in constants. A
    /// documented knob that moves no behaviour is a lie with a fuse, and this
    /// one would have been discovered during a restart.
    ///
    /// A missing key takes the default; a present but malformed one is a typed
    /// error rather than a silent fallback, because a safety window on a
    /// schedule nobody chose is worse than a refusal the operator can read.
    /// `ATP_IB_RESTART_ET` is deliberately NOT read here — resolving a local
    /// Eastern time needs the DST-aware calendar, which lives on the Python
    /// side; `atp_orchestration.restart_schedule.cli_args` passes the resolved
    /// instant as `--restart-ns`.
    fn from_env(read: impl Fn(&str) -> Option<String>) -> Result<Self, String> {
        Ok(Self {
            restart_ns: DEFAULT_RESTART_NS,
            now_ns: None,
            lead_ns: seconds_from_env(
                &read,
                "ATP_IB_RESTART_SUSPEND_LEAD_SECONDS",
                DEFAULT_RESTART_SUSPEND_LEAD_SECONDS,
            )?,
            window_ns: seconds_from_env(
                &read,
                "ATP_IB_RESTART_WINDOW_SECONDS",
                DEFAULT_RESTART_WINDOW_SECONDS,
            )?,
            injection: Injection::None,
        })
    }
}

/// Read a positive second count from the environment, in nanoseconds.
///
/// Absent takes the default. Present-but-empty, non-numeric or non-positive is
/// a typed error: an empty value is usually a variable that expanded to
/// nothing, and applying the default there hides exactly the deployment mistake
/// that leaves a safety window on a schedule nobody chose.
fn seconds_from_env(
    read: &impl Fn(&str) -> Option<String>,
    key: &str,
    default_seconds: i64,
) -> Result<i64, String> {
    let Some(raw) = read(key) else {
        return Ok(default_seconds * NANOS_PER_SECOND);
    };
    if raw.trim().is_empty() {
        return Err(format!(
            "{key} is set but empty; unset it to accept the default ({default_seconds}s)"
        ));
    }
    let seconds: i64 = raw
        .trim()
        .parse()
        .map_err(|_| format!("{key}=`{raw}` is not an integer"))?;
    if seconds <= 0 {
        return Err(format!("{key}={seconds} must be positive (SyRS SYS-75)"));
    }
    seconds
        .checked_mul(NANOS_PER_SECOND)
        .ok_or_else(|| format!("{key}={seconds} overflows nanoseconds"))
}

fn parse_args(args: &[String], allowed_injection: Injection) -> Result<RunArgs, String> {
    // Environment first, explicit flags second: a flag is the operator saying
    // it right now, and must win over the deployment's standing configuration.
    let mut parsed = RunArgs::from_env(|key| match env::var(key) {
        Ok(value) => Some(value),
        // NotUnicode is NOT absent. Treating it as unset would silently apply
        // the default for a value the operator did set (EXE-006 rule 4).
        Err(env::VarError::NotPresent) => None,
        Err(env::VarError::NotUnicode(_)) => Some(String::from("\u{fffd}")),
    })?;
    let mut index = 0;
    while index < args.len() {
        let flag = args[index].as_str();
        match flag {
            "--restart-ns" => {
                parsed.restart_ns = parse_i64(take_value(args, &mut index, flag)?, flag)?;
            }
            "--now-ns" => {
                parsed.now_ns = Some(parse_i64(take_value(args, &mut index, flag)?, flag)?);
            }
            "--lead-seconds" => {
                let seconds = parse_i64(take_value(args, &mut index, flag)?, flag)?;
                parsed.lead_ns = seconds
                    .checked_mul(NANOS_PER_SECOND)
                    .ok_or_else(|| format!("{flag} value {seconds} overflows nanoseconds"))?;
            }
            "--window-seconds" => {
                let seconds = parse_i64(take_value(args, &mut index, flag)?, flag)?;
                parsed.window_ns = seconds
                    .checked_mul(NANOS_PER_SECOND)
                    .ok_or_else(|| format!("{flag} value {seconds} overflows nanoseconds"))?;
            }
            "--inject" => {
                let value = take_value(args, &mut index, flag)?;
                let injection = parse_injection(value)?;
                if injection != allowed_injection {
                    return Err(format!(
                        "`--inject {value}` is not the opposite class for this subcommand; \
                         only `{}` is accepted here",
                        injection_label(allowed_injection)
                    ));
                }
                parsed.injection = injection;
            }
            other => return Err(format!("unexpected argument `{other}`")),
        }
        index += 1;
    }
    Ok(parsed)
}

fn parse_injection(value: &str) -> Result<Injection, String> {
    match value {
        "outside-window" => Ok(Injection::OutsideWindow),
        "inside-window" => Ok(Injection::InsideWindow),
        "dead-gateway" => Ok(Injection::DeadGateway),
        other => Err(format!("unknown fault `{other}`")),
    }
}

const fn injection_label(injection: Injection) -> &'static str {
    match injection {
        Injection::None => "none",
        Injection::OutsideWindow => "outside-window",
        Injection::InsideWindow => "inside-window",
        Injection::DeadGateway => "dead-gateway",
    }
}

fn take_value<'a>(args: &'a [String], index: &mut usize, flag: &str) -> Result<&'a str, String> {
    *index += 1;
    args.get(*index)
        .map(String::as_str)
        .ok_or_else(|| format!("{flag} requires a value"))
}

fn parse_i64(value: &str, flag: &str) -> Result<i64, String> {
    value
        .parse::<i64>()
        .map_err(|_| format!("{flag} value `{value}` is not an integer"))
}

// --------------------------------------------------------------------------- //
// Endpoints
// --------------------------------------------------------------------------- //

/// A loopback address with nothing listening: bind an ephemeral port, read it
/// back, then release it.
///
/// This IS the fault injection the acceptance criterion is verified by, and it
/// needs no gateway — which is why SRS-MD-005's verification method is
/// `integration` and not `live-ib`. It never touches 4001/4002, so the tool is
/// safe to run while other agents hold the shared IB ports.
fn dead_endpoint() -> Result<SocketAddr, String> {
    let listener = TcpListener::bind(SocketAddr::from((Ipv4Addr::LOCALHOST, 0)))
        .map_err(|err| format!("could not bind an ephemeral loopback port: {err}"))?;
    let addr = listener
        .local_addr()
        .map_err(|err| format!("could not read the ephemeral port: {err}"))?;
    drop(listener);
    Ok(addr)
}

/// A loopback listener that stays bound for the caller's lifetime — the stand-in
/// for a gateway that has finished restarting.
fn live_endpoint() -> Result<(TcpListener, SocketAddr), String> {
    let listener = TcpListener::bind(SocketAddr::from((Ipv4Addr::LOCALHOST, 0)))
        .map_err(|err| format!("could not bind an ephemeral loopback port: {err}"))?;
    let addr = listener
        .local_addr()
        .map_err(|err| format!("could not read the ephemeral port: {err}"))?;
    Ok((listener, addr))
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

fn cmd_prove_suspension(args: &[String]) -> Result<(), String> {
    let parsed = parse_args(args, Injection::OutsideWindow)?;
    // The FIRST instant of the lead, which is inside it for every legal lead.
    // The earlier `restart_ns - lead_ns / 2` integer-divided to `restart_ns`
    // itself at the catalogue-legal `lead_seconds = 1`, so the default run
    // landed in `Restarting` and printed the SYS-75(a) proof for a lead it
    // never entered. The injection moves the instant outside the window
    // entirely, where nothing is suspended.
    let default_now = parsed.restart_ns - parsed.lead_ns;
    let now_ns = match parsed.injection {
        Injection::OutsideWindow => parsed.restart_ns - parsed.lead_ns - NANOS_PER_SECOND,
        _ => parsed.now_ns.unwrap_or(default_now),
    };
    let endpoint = dead_endpoint()?;
    let evidence = evaluate(&parsed, now_ns, endpoint)?;

    report_header("suspension", &parsed, now_ns, &evidence);
    report_common(&evidence);

    // The PHASE first. Every other check below is also satisfied inside the
    // restart window itself, so without this the proof would print for
    // SYS-75(a) — the 60-second PRE-restart lead — while evaluating somewhere
    // the lead never covered. A proof line must name what it proved.
    require(
        evidence.phase == RestartPhase::Suspending,
        format!(
            "SyRS SYS-75(a) is the PRE-restart lead; this instant is in {:?}",
            evidence.phase
        ),
    )?;
    require(
        evidence.connectivity_state == ConnectivityState::ScheduledRestartWindow,
        format!(
            "SyRS SYS-75(a): expected the scheduled-restart state, observed {:?}",
            evidence.connectivity_state
        ),
    )?;
    let LiveOrderOutcome::Blocked {
        category,
        error_type,
        ..
    } = &evidence.live_outcome
    else {
        return Err(
            "SyRS SYS-75(a): the designated-live submission routed through during the \
             suspension window"
                .to_string(),
        );
    };
    require(
        category == "CONNECTIVITY_BLOCKED",
        format!("expected CONNECTIVITY_BLOCKED, observed {category}"),
    )?;
    require(
        error_type == "IbGatewayUnreachable",
        format!("expected the ERR-2 envelope error_type, observed {error_type}"),
    )?;
    require(
        evidence.ib_orders_created == 0,
        format!(
            "a suspended submission must create ZERO IB orders; observed {}",
            evidence.ib_orders_created
        ),
    )?;
    require(
        evidence.event_scheduled_restart == Some(true),
        "the published ConnectivityEvent must carry the maintenance marker".to_string(),
    )?;
    require(
        !evidence.market_data_admitted,
        "SyRS SYS-75(a): market-data requests must be suspended alongside order submission"
            .to_string(),
    )?;
    require(
        evidence.market_data_refusal.as_deref() == Some("ScheduledRestartWindow"),
        format!(
            "the subscription refusal must name the restart window; observed {:?}",
            evidence.market_data_refusal
        ),
    )?;
    require(
        evidence.market_data_admission == MarketDataAdmission::SuspendedForScheduledRestart,
        format!(
            "the market-data admission must read as planned maintenance, observed {}",
            evidence.market_data_admission.as_str()
        ),
    )?;
    require(
        evidence.alert_disposition == AlertDisposition::Suppressed,
        format!(
            "SyRS SYS-75(b): expected a suppressed notification, observed {}",
            evidence.alert_disposition.as_str()
        ),
    )?;
    require(
        evidence.channel_messages_sent == 0,
        format!(
            "a suppressed notification must send nothing; {} message(s) reached a transport",
            evidence.channel_messages_sent
        ),
    )?;
    require(
        evidence.reconnects >= 1,
        "SyRS SYS-75(c): the gate must request a reconnection attempt".to_string(),
    )?;

    println!("restart-window-suspension-proven:true");
    Ok(())
}

fn cmd_prove_escalation(args: &[String]) -> Result<(), String> {
    let parsed = parse_args(args, Injection::InsideWindow)?;
    // One second past the window by default; the injection moves the instant
    // back INSIDE it, where the same dead gateway is planned maintenance.
    let default_now = parsed.restart_ns + parsed.window_ns + NANOS_PER_SECOND;
    let now_ns = match parsed.injection {
        Injection::InsideWindow => parsed.restart_ns + parsed.window_ns / 2,
        _ => parsed.now_ns.unwrap_or(default_now),
    };
    let endpoint = dead_endpoint()?;
    let evidence = evaluate(&parsed, now_ns, endpoint)?;

    report_header("escalation", &parsed, now_ns, &evidence);
    report_common(&evidence);

    require(
        evidence.connectivity_state == ConnectivityState::Unreachable,
        format!(
            "SyRS SYS-75 escalation: expected UNREACHABLE after the window, observed {:?}",
            evidence.connectivity_state
        ),
    )?;
    require(
        evidence.phase == RestartPhase::Elapsed,
        format!("expected the elapsed phase, observed {:?}", evidence.phase),
    )?;
    require(
        matches!(evidence.live_outcome, LiveOrderOutcome::Blocked { .. }),
        "an unreachable gateway must still block the live submission (SyRS SYS-45)".to_string(),
    )?;
    require(
        evidence.event_scheduled_restart == Some(false),
        "the escalated ConnectivityEvent must NOT carry the maintenance marker — that flag \
         is what silences the operator page"
            .to_string(),
    )?;
    require(
        evidence.alert_disposition == AlertDisposition::Dispatched,
        format!(
            "SyRS SYS-46: expected a dispatched notification after the window, observed {}",
            evidence.alert_disposition.as_str()
        ),
    )?;
    require(
        evidence.channel_messages_sent > 0,
        "an escalated notification must actually reach the transports".to_string(),
    )?;
    require(
        evidence.ib_orders_created == 0,
        format!(
            "a connectivity-blocked submission must create ZERO IB orders; observed {}",
            evidence.ib_orders_created
        ),
    )?;
    require(
        evidence.market_data_admission == MarketDataAdmission::ConnectivityLost,
        format!(
            "after the window a market-data refusal must read as an OUTAGE, not as \
             maintenance; observed {}",
            evidence.market_data_admission.as_str()
        ),
    )?;
    require(
        evidence.market_data_refusal.as_deref() == Some("IbGatewayUnreachable"),
        format!(
            "the escalated subscription refusal must name the outage; observed {:?}",
            evidence.market_data_refusal
        ),
    )?;

    println!("restart-window-escalation-proven:true");
    Ok(())
}

fn cmd_prove_resume(args: &[String]) -> Result<(), String> {
    let parsed = parse_args(args, Injection::DeadGateway)?;
    // Halfway through the window by default: the gateway has come back before
    // the window closed, so SYS-75(c)/(d) says resume.
    let default_now = parsed.restart_ns + parsed.window_ns / 2;
    let now_ns = parsed.now_ns.unwrap_or(default_now);

    // Hold the listener for the whole run so the endpoint stays answering.
    let (listener, endpoint) = live_endpoint()?;
    let endpoint = match parsed.injection {
        Injection::DeadGateway => {
            drop(listener);
            dead_endpoint()?
        }
        _ => endpoint,
    };
    let evidence = evaluate(&parsed, now_ns, endpoint)?;

    report_header("resume", &parsed, now_ns, &evidence);
    report_common(&evidence);

    // Same rule as the other two proofs: name the phase you are in. A resume
    // proof that passed outside the window would be describing an ordinary
    // healthy gateway, not SYS-75(c)/(d).
    require(
        evidence.phase == RestartPhase::Restarting,
        format!(
            "SyRS SYS-75(c)/(d) is about the restart WINDOW; this instant is in {:?}",
            evidence.phase
        ),
    )?;
    require(
        evidence.connectivity_state == ConnectivityState::Connected,
        format!(
            "SyRS SYS-75(c)/(d): expected CONNECTED once the gateway answers, observed {:?}",
            evidence.connectivity_state
        ),
    )?;
    let LiveOrderOutcome::RoutedThrough { broker_order_id } = &evidence.live_outcome else {
        return Err(
            "SyRS SYS-75(d): the designated-live submission must route through once operations \
             resume"
                .to_string(),
        );
    };
    require(
        !broker_order_id.is_empty(),
        "a routed submission must carry a broker order id".to_string(),
    )?;
    require(
        evidence.ib_orders_created == 1,
        format!(
            "a resumed submission must create exactly one IB order; observed {}",
            evidence.ib_orders_created
        ),
    )?;
    require(
        evidence.market_data_admitted,
        "SyRS SYS-75(d): market-data requests must resume with order submission".to_string(),
    )?;
    require(
        evidence.alert_disposition == AlertDisposition::NoEvent,
        format!(
            "a healthy gateway must publish no connectivity event, observed {}",
            evidence.alert_disposition.as_str()
        ),
    )?;
    require(
        evidence.reconnects == 0,
        format!(
            "a resumed path must request no reconnect; observed {}",
            evidence.reconnects
        ),
    )?;

    println!("restart-window-resume-proven:true");
    Ok(())
}

// --------------------------------------------------------------------------- //
// Reporting
// --------------------------------------------------------------------------- //

fn evaluate(
    parsed: &RunArgs,
    now_ns: i64,
    endpoint: SocketAddr,
) -> Result<RestartWindowEvidence, String> {
    run_restart_window_scenario(&RestartWindowScenario {
        now_ns,
        expected_restart_ns: parsed.restart_ns,
        lead_ns: parsed.lead_ns,
        window_ns: parsed.window_ns,
        gateway_endpoint: endpoint,
    })
}

fn report_header(proof: &str, parsed: &RunArgs, now_ns: i64, evidence: &RestartWindowEvidence) {
    println!(
        "srs:SRS-MD-005 proof:restart-window-{proof} transports:FIXTURE designated:{} symbol:{}",
        evidence.designated, SCENARIO_SYMBOL
    );
    println!(
        "window restart_ns:{} lead_ns:{} window_ns:{} now_ns:{now_ns} phase:{:?}",
        parsed.restart_ns, parsed.lead_ns, parsed.window_ns, evidence.phase
    );
}

fn report_common(evidence: &RestartWindowEvidence) {
    println!(
        "gateway reachability:{} state:{:?} scheduled_restart:{}",
        evidence
            .reachability
            .as_ref()
            .map_or("NOT_PROBED", ReachabilityOutcome::as_str),
        evidence.connectivity_state,
        evidence
            .event_scheduled_restart
            .map_or_else(|| "none".to_string(), |flag| flag.to_string()),
    );
    let (outcome, detail) = match &evidence.live_outcome {
        LiveOrderOutcome::Blocked {
            category,
            error_type,
            ..
        } => ("BLOCKED", format!("{category}/{error_type}")),
        LiveOrderOutcome::RoutedThrough { broker_order_id } => {
            ("ROUTED_THROUGH", broker_order_id.clone())
        }
    };
    println!("live-order outcome:{outcome} detail:{detail}");
    println!(
        "witness ib-orders-created:{} reconnects:{} events:{}",
        evidence.ib_orders_created, evidence.reconnects, evidence.events_recorded
    );
    println!(
        "market-data admitted:{} admission:{} refusal:{}",
        evidence.market_data_admitted,
        evidence.market_data_admission.as_str(),
        evidence.market_data_refusal.as_deref().unwrap_or("none"),
    );
    println!(
        "alerts disposition:{} messages-sent:{}",
        evidence.alert_disposition.as_str(),
        evidence.channel_messages_sent
    );
    println!(
        "contrast[non-designated] route:internal_simulation sim-receipt:{}",
        evidence.non_designated_sim_receipt
    );
}

/// Fail closed on a broken post-condition. Returning `Err` is what keeps the
/// proof line unprinted — the success sentinel is emitted only after every
/// check below it has held.
fn require(condition: bool, message: String) -> Result<(), String> {
    if condition {
        Ok(())
    } else {
        Err(message)
    }
}
