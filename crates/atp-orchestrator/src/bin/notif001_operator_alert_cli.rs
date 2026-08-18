//! SRS-NOTIF-001 operator binary — drive a REAL operator alert end to end.
//!
//! This is the tool the operator runs to produce the flip evidence. It is
//! deliberately *not* a fixture harness: every layer below the argument parser
//! is the production object.
//!
//! ```text
//!   ExecutionEngine::submit_live_order   (the REAL ERR-2 / SRS-SAFE-003 gate)
//!     -> ConnectivityEvent               (produced BY that gate, not hand-built)
//!     -> ConnectivityNotifierSink        (the REAL detection wiring)
//!     -> OperatorNotifier                (the REAL dispatcher, required email+push)
//!     -> SmtpEmailChannel / PushChannel   (the REAL IF-10 / IF-11 transports)
//!     -> NotificationEventStore          (the REAL durable audit store)
//! ```
//!
//! The connectivity *state* is supplied by the operator rather than probed from a
//! socket, because the point of the run is to exercise the alert path on demand.
//! Everything downstream of that one input is production code — in particular the
//! `ConnectivityEvent` is emitted by the execution engine's own gate, so a
//! regression that stopped the gate emitting it would fail this run rather than
//! be papered over by a hand-constructed event.
//!
//! What is still NOT proven by this binary, and must not be claimed from it:
//! that the *IB Gateway* was genuinely unreachable — the operator asserts the
//! state — and that either channel reached the operator. The two channels fall
//! short in different ways now that they no longer share a relay:
//!
//! * **email (IF-10)** proves hand-off to the `phase1-notification-egress`
//!   relay. Whether the relay's provider then delivered to a mailbox is outside
//!   this run.
//! * **push (IF-11)** proves ntfy *accepted* the publish and returned a message
//!   id. That is a stronger hand-off — there is no relay in between — but
//!   acceptance still is not receipt: it does not prove the operator's phone was
//!   subscribed, online, or that the notification was displayed.
//!
//! The flip needs a real gateway outage, a real inbox, and a real handset.
//!
//! ## Exit codes
//!
//! `0` only when every required channel reached a terminal SUCCESS — delivered,
//! or legitimately suppressed — **and** the event was durably stored. A failed
//! channel, a storage failure, or an un-dispatched alert exits non-zero, so shell
//! automation cannot read a broken alert path as a working one.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

/// Bound on waiting for the dispatch. Comfortably above the dispatcher's own
/// worst case (two required channels x its per-channel deadline), so a timeout
/// here means something is genuinely wedged rather than merely slow.
const FLUSH_TIMEOUT: Duration = Duration::from_secs(90);

use atp_adapters::notification::{PushChannel, PushConfig, SmtpEmailChannel, SmtpRelayConfig};
use atp_execution::{
    BrokerageConnectivity, ExecutionEngine, LiveBrokerageSubmit, MarketDataFreshnessProbe,
    StaleDataEventSink,
};
use atp_notification::{
    ChannelError, DeliveryOutcome, NotificationChannel, OperatorNotifier, SharedChannelClient,
};
use atp_orchestrator::connectivity_notification::{
    ConnectivityAlertOutcome, ConnectivityNotifierSink, SystemAlertClock,
};
use atp_types::{
    AssetClass, ConnectivityState, MarketDataFreshness, OrderErrorCategory, OrderReceipt,
    OrderSide, OrderSubmission, OrderType, StaleDataEvent, StrategyId, StrategyMode,
    StructuredOrderError,
};

const USAGE: &str = "\
notif001_operator_alert_cli — SRS-NOTIF-001 operator alert, end to end

USAGE:
    notif001_operator_alert_cli outage --state <state> --store <dir> \
[--strategy <id>] [--symbol <sym>]

OPTIONS:
    --state <state>    unreachable | scheduled-restart   (required)
    --store <dir>      existing directory for the durable notification store (required)
    --strategy <id>    live strategy id to name in the alert  [default: operator-check]
    --symbol <sym>     symbol to name in the alert            [default: AAPL]

ENVIRONMENT (the transports; see architecture/runtime_services.json):
    ATP_SMTP_API_KEY    relay credential for IF-10   (required)
    ATP_SMTP_SENDER     envelope sender              (required)
    ATP_OPERATOR_EMAIL  destination mailbox          (required)
    ATP_PUSH_TOKEN      ntfy access token for IF-11  (required, secret)
    ATP_PUSH_TOPIC      ntfy topic                   (required, SECRET — on ntfy
                        the topic alone is enough to publish)
    ATP_SMTP_RELAY_HOST/PORT, ATP_SMTP_RELAY_USER
                        optional; default to the phase1-notification-egress sidecar
    ATP_PUSH_HOST/PORT  optional; default to a loopback ntfy on port 80. Push
                        needs no relay hop — it targets the LAN ntfy directly.

EXIT CODES:
    0  every required channel terminal-succeeded AND the event was stored
    1  the alert path failed (see the printed per-channel detail)
    2  bad invocation / unusable configuration
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(code) => code,
        Err(message) => {
            eprintln!("notif001_operator_alert_cli: {message}");
            eprintln!("{USAGE}");
            ExitCode::from(2)
        }
    }
}

#[derive(Debug)]
struct Options {
    state: ConnectivityState,
    store: PathBuf,
    strategy: String,
    symbol: String,
}

/// Allow-list argument parsing: an unknown flag is a hard error, never ignored.
/// A silently-dropped flag on an operator safety tool means the run proves
/// something other than what the operator asked for.
fn parse(args: &[String]) -> Result<Options, String> {
    let mut command = None;
    let mut state = None;
    let mut store = None;
    let mut strategy = String::from("operator-check");
    let mut symbol = String::from("AAPL");

    let mut index = 0;
    while index < args.len() {
        let arg = args[index].as_str();
        match arg {
            "outage" if command.is_none() => command = Some(arg.to_string()),
            "--help" | "-h" => return Err("usage requested".to_string()),
            "--state" | "--store" | "--strategy" | "--symbol" => {
                let value = args
                    .get(index + 1)
                    .ok_or_else(|| format!("{arg} requires a value"))?;
                if value.starts_with("--") {
                    return Err(format!("{arg} requires a value, got the flag {value}"));
                }
                match arg {
                    "--state" => {
                        state = Some(match value.as_str() {
                            "unreachable" => ConnectivityState::Unreachable,
                            "scheduled-restart" => ConnectivityState::ScheduledRestartWindow,
                            other => {
                                return Err(format!(
                                    "--state must be unreachable | scheduled-restart, got {other:?}"
                                ))
                            }
                        })
                    }
                    "--store" => store = Some(PathBuf::from(value)),
                    "--strategy" => strategy = value.clone(),
                    "--symbol" => symbol = value.clone(),
                    _ => unreachable!("matched above"),
                }
                index += 1;
            }
            other => return Err(format!("unrecognised argument {other:?}")),
        }
        index += 1;
    }

    if command.as_deref() != Some("outage") {
        return Err("the only command is `outage`".to_string());
    }
    let state = state.ok_or("--state is required")?;
    let store = store.ok_or("--store is required")?;
    if !store.is_dir() {
        return Err(format!(
            "--store must be an existing directory: {}",
            store.display()
        ));
    }
    if strategy.trim().is_empty() || symbol.trim().is_empty() {
        return Err("--strategy and --symbol must be non-empty".to_string());
    }

    Ok(Options {
        state,
        store,
        strategy,
        symbol,
    })
}

/// The connectivity port, reporting the operator-asserted state.
struct AssertedConnectivity {
    state: ConnectivityState,
}

impl BrokerageConnectivity for AssertedConnectivity {
    fn state(&self) -> ConnectivityState {
        self.state
    }
    fn request_reconnect(&self) {}
}

/// The broker port must never be reached on a connectivity-blocked submission.
/// Panicking here makes an ERR-2 regression loud instead of silently sending a
/// live order into a gateway the engine just declared unreachable.
struct NeverCalledBroker;

impl LiveBrokerageSubmit for NeverCalledBroker {
    fn submit_order(
        &self,
        _submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        panic!("ERR-2 VIOLATION: the broker port was reached on a connectivity-blocked submission");
    }
}

struct FreshData;

impl MarketDataFreshnessProbe for FreshData {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        MarketDataFreshness::Fresh
    }
    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        0
    }
}

struct IgnoredStaleEvents;

impl StaleDataEventSink for IgnoredStaleEvents {
    fn record(&self, _event: StaleDataEvent) {}
}

/// The literal the catalogue ships as every secret's default, and which
/// `.env.example` tells the operator to LEAVE IN PLACE when the real values are
/// sealed in the SRS-SEC-001 vault.
const PLACEHOLDER_SECRET: &str = "placeholder-set-in-environment";

/// Deployment modes where a placeholder credential is an error, matching
/// `atp_config`'s `PRODUCTION_ENVS`.
const PRODUCTION_ENVS: [&str; 2] = ["staging", "production"];

/// Refuse to build transports from placeholder credentials in staging/production.
///
/// This binary reads the process environment directly, and it CANNOT overlay the
/// vault: the vault is `atp_config.vault`, a Python component, and this is the
/// Rust composition root. That is fine as a division of labour — but it leaves a
/// gap the documented deployment flow walks straight into. `.env.example` tells
/// the operator to seal ATP_PUSH_TOPIC / ATP_PUSH_TOKEN / ATP_SMTP_API_KEY in the
/// vault and LEAVE THE PLACEHOLDERS in `.env`. Without this check, running the
/// operator alert in that (correct) configuration would build transports from the
/// literal string `placeholder-set-in-environment` and try to publish with it.
///
/// So the binary enforces the half of the readiness contract it can: the same
/// placeholder rejection `atp_config` applies, at the same severity, in the same
/// environments. It names the offending keys and never prints their values.
fn reject_placeholder_secrets(read: &impl Fn(&str) -> Option<String>) -> Result<(), String> {
    let env = read("ATP_ENV").unwrap_or_default();
    if !PRODUCTION_ENVS.contains(&env.trim()) {
        return Ok(());
    }
    let offenders: Vec<&str> = ["ATP_SMTP_API_KEY", "ATP_PUSH_TOPIC", "ATP_PUSH_TOKEN"]
        .into_iter()
        .filter(|key| read(key).as_deref() == Some(PLACEHOLDER_SECRET))
        .collect();
    if offenders.is_empty() {
        return Ok(());
    }
    Err(format!(
        "ATP_ENV={env} but {} still hold the catalogue placeholder. The real values \
         are sealed in the SRS-SEC-001 vault, which this Rust binary cannot open — \
         export them into the environment for this run (see docs/DEPLOYMENT.md). \
         Refusing to publish an operator alert with placeholder credentials.",
        offenders.join(", ")
    ))
}

fn build_channels() -> Result<Vec<SharedChannelClient>, ChannelError> {
    let read = |key: &str| std::env::var(key).ok();
    let email = SmtpEmailChannel::new(SmtpRelayConfig::from_env(read)?);
    let push = PushChannel::new(PushConfig::from_env(read)?);
    Ok(vec![
        std::sync::Arc::new(email) as SharedChannelClient,
        std::sync::Arc::new(push) as SharedChannelClient,
    ])
}

fn run(args: &[String]) -> Result<ExitCode, String> {
    let options = parse(args)?;

    reject_placeholder_secrets(&|key: &str| std::env::var(key).ok())?;
    let channels = build_channels().map_err(|err| {
        format!("transport configuration is unusable: {err}. Set the environment above.")
    })?;

    // The REAL dispatcher, the REAL detection wiring, the REAL durable store.
    let sink = ConnectivityNotifierSink::new(OperatorNotifier::new(), channels, SystemAlertClock)
        .with_store_dir(&options.store);

    // Drive the REAL execution gate so the ConnectivityEvent is produced by
    // production code rather than constructed here.
    let engine = ExecutionEngine::default();
    let submission = OrderSubmission::new(
        StrategyId::new(options.strategy.clone()),
        options.symbol.clone(),
        1,
        AssetClass::Equity,
        OrderSide::Buy,
        OrderType::Market,
    );
    let rejection = engine.submit_live_order(
        StrategyMode::Live,
        submission,
        &NeverCalledBroker,
        &AssertedConnectivity {
            state: options.state,
        },
        &sink,
        &FreshData,
        &IgnoredStaleEvents,
    );

    match rejection {
        Err(err) if err.category == OrderErrorCategory::ConnectivityBlocked => {
            println!("gate=CONNECTIVITY_BLOCKED error_type={}", err.error_type);
        }
        Err(err) => {
            return Err(format!(
                "the engine blocked the submission for the wrong reason ({:?}); the \
                 connectivity gate did not fire",
                err.category
            ));
        }
        Ok(_) => {
            return Err(
                "the engine ACCEPTED a submission against a blocked gateway — ERR-2 is not \
                 firing, and no alert path can compensate for that"
                    .to_string(),
            );
        }
    }

    // `record` dispatches off the caller's thread so the execution engine's
    // reconnect is not stuck behind two network sends. This binary DOES need the
    // outcome, so wait for it — bounded, and reported honestly if it overruns.
    if !sink.flush(FLUSH_TIMEOUT) {
        return Err(format!(
            "the alert dispatch did not finish within {}s; the relay is not responding and \
             this run proves nothing",
            FLUSH_TIMEOUT.as_secs()
        ));
    }

    report(&sink, options.state)
}

fn report<K: atp_orchestrator::connectivity_notification::AlertClock>(
    sink: &ConnectivityNotifierSink<K>,
    state: ConnectivityState,
) -> Result<ExitCode, String> {
    let outcomes = sink.outcomes();
    let Some(outcome) = outcomes.last() else {
        return Err("the connectivity gate fired but produced no alert outcome".to_string());
    };

    match outcome {
        ConnectivityAlertOutcome::Dispatched(event) => {
            println!("dispatched=true");
            println!("dispatch-latency-ms={}", event.dispatch_latency_millis());
            println!("within-sla={}", event.within_dispatch_sla());
            println!("stored=true");

            let mut all_terminal_ok = true;
            for channel in [NotificationChannel::Email, NotificationChannel::Push] {
                match event.delivery_for(channel) {
                    Some(delivery) => {
                        let label = match channel {
                            NotificationChannel::Email => "email",
                            NotificationChannel::Push => "push",
                        };
                        println!(
                            "{label}={:?} detail={}",
                            delivery.outcome(),
                            delivery.detail()
                        );
                        // Suppressed counts as success ONLY for the scheduled
                        // restart window, which is the one case SYS-75 sanctions.
                        let ok = match delivery.outcome() {
                            DeliveryOutcome::Delivered => true,
                            DeliveryOutcome::Suppressed => {
                                state == ConnectivityState::ScheduledRestartWindow
                            }
                            _ => false,
                        };
                        all_terminal_ok &= ok;
                    }
                    None => {
                        println!("{channel:?}=MISSING");
                        all_terminal_ok = false;
                    }
                }
            }

            println!("alert-path-ok={all_terminal_ok}");
            if all_terminal_ok {
                Ok(ExitCode::SUCCESS)
            } else {
                Ok(ExitCode::from(1))
            }
        }
        ConnectivityAlertOutcome::Failed { detail } => {
            println!("dispatched=false");
            println!("alert-path-ok=false");
            eprintln!("alert path FAILED: {detail}");
            Ok(ExitCode::from(1))
        }
        ConnectivityAlertOutcome::Coalesced { since_last_millis } => {
            // A fresh process cannot legitimately coalesce; if it does, the
            // cool-down state is not what this run assumes.
            println!("dispatched=false");
            println!("alert-path-ok=false");
            eprintln!(
                "alert was coalesced {since_last_millis}ms after a previous dispatch — a fresh \
                 run should never coalesce"
            );
            Ok(ExitCode::from(1))
        }
        ConnectivityAlertOutcome::NotAnOutage => {
            println!("dispatched=false");
            println!("alert-path-ok=false");
            eprintln!("the gate reported a healthy state; nothing was alerted");
            Ok(ExitCode::from(1))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|v| v.to_string()).collect()
    }

    #[test]
    fn an_unknown_flag_is_refused_rather_than_ignored() {
        let error = parse(&args(&["outage", "--state", "unreachable", "--danger"]))
            .expect_err("unknown flag must fail");
        assert!(error.contains("unrecognised"), "{error}");
    }

    #[test]
    fn a_flag_consuming_the_next_flag_as_its_value_is_refused() {
        let error = parse(&args(&["outage", "--state", "--store"]))
            .expect_err("a flag as a value must fail");
        assert!(error.contains("requires a value"), "{error}");
    }

    #[test]
    fn an_unknown_state_is_refused() {
        let error = parse(&args(&["outage", "--state", "flaky", "--store", "/tmp"]))
            .expect_err("unknown state must fail");
        assert!(error.contains("--state must be"), "{error}");
    }

    #[test]
    fn a_missing_store_directory_is_refused_before_any_send() {
        let error = parse(&args(&[
            "outage",
            "--state",
            "unreachable",
            "--store",
            "/definitely/not/a/directory",
        ]))
        .expect_err("missing store must fail");
        assert!(error.contains("existing directory"), "{error}");
    }

    #[test]
    fn the_command_is_required() {
        let error = parse(&args(&["--state", "unreachable"])).expect_err("command required");
        assert!(error.contains("only command"), "{error}");
    }

    /// Placeholder credentials must not reach a real publish in staging or
    /// production.
    ///
    /// Found by adversarial review. `.env.example` instructs the operator to
    /// seal the real secrets in the vault and LEAVE the placeholders in `.env`,
    /// and this binary reads the environment directly and cannot open that
    /// vault. Following the documented flow would therefore have published an
    /// operator alert authenticated with the literal string
    /// `placeholder-set-in-environment`.
    #[test]
    fn placeholder_credentials_are_refused_in_staging_and_production() {
        let env_with = |atp_env: &str, placeholder_key: Option<&str>| {
            let atp_env = atp_env.to_string();
            let placeholder_key = placeholder_key.map(str::to_string);
            move |key: &str| -> Option<String> {
                if key == "ATP_ENV" {
                    return Some(atp_env.clone());
                }
                if Some(key.to_string()) == placeholder_key {
                    return Some(PLACEHOLDER_SECRET.to_string());
                }
                Some("a-real-value".to_string())
            }
        };

        for mode in ["staging", "production"] {
            for key in ["ATP_SMTP_API_KEY", "ATP_PUSH_TOPIC", "ATP_PUSH_TOKEN"] {
                let error = reject_placeholder_secrets(&env_with(mode, Some(key)))
                    .expect_err("a placeholder credential must be refused");
                assert!(error.contains(key), "{error}");
                assert!(error.contains(mode), "{error}");
            }
            // All real -> allowed.
            assert!(reject_placeholder_secrets(&env_with(mode, None)).is_ok());
        }

        // Development keeps the placeholder flexibility init.sh relies on.
        for key in ["ATP_SMTP_API_KEY", "ATP_PUSH_TOPIC", "ATP_PUSH_TOKEN"] {
            assert!(reject_placeholder_secrets(&env_with("development", Some(key))).is_ok());
        }
    }
}
