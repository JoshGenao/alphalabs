//! SRS-SAFE-002 kill-switch liquidation-timeout operator CLI (SyRS SYS-44b;
//! StRS SN-1.11).
//!
//! Drives the REAL `atp-execution` timeout gate through the REAL
//! `PollingLiquidationProbe` (the full wait window on a simulated clock — a
//! 30 s drill completes instantly), the REAL SRS-NOTIF-001 `OperatorNotifier`
//! (over fixture email/SMS transports; the concrete SMTP/SMS adapters are the
//! deferred SRS-NOTIF-001 leg) and the REAL `IbGatewayLiquidationCleanup`
//! (over the fixture IB gateway; the live transport binding is the deferred
//! SRS-EXE-006 leg) — the mocked-IB fault-injection workflow the feature's
//! own verification Step 2 prescribes.
//!
//! Subcommand `resolve` runs ONE timeout scenario and prints `outcome:{json}`.
//!
//! EXIT CODES (the outcome line is the truth in every case it is printed):
//!   0  the liquidation filled before the timeout — no SYS-44b action ran
//!   1  the liquidation TIMED OUT — the SYS-44b sequence ran (page + cancel +
//!      disconnect attempted; each outcome is in the JSON, failures included)
//!   3  fail-closed refusal WITHOUT any automated action (probe unavailable
//!      or a probe-inconsistency rejection)
//!   2  usage / scenario error (no outcome produced)

use std::process::ExitCode;

use atp_orchestrator::kill_switch_timeout::{
    run_fixture_timeout, FixtureTimeoutRun, ProbeFault, TimeoutScenario,
};
use atp_types::{OrderErrorCategory, SideEffectOutcome, UnfilledLiquidationOrder};

const USAGE: &str = "\
SRS-SAFE-002 kill-switch liquidation-timeout CLI (mocked-IB fixtures; live IB = SRS-EXE-006,
real SMTP/SMS = SRS-NOTIF-001)

USAGE:
  safe002_liquidation_timeout_cli resolve [OPTIONS]
  safe002_liquidation_timeout_cli help

RESOLVE OPTIONS:
  --live-strategy <ID>          live strategy id (default live-momentum)
  --order-correlation <ID>      liquidation order correlation id (default ks-liq-0001)
  --symbol <SYM>                liquidation symbol (default AAPL)
  --side <BUY|SELL>             liquidation side (default SELL)
  --quantity <N>                liquidation quantity, positive (default 250)
  --timeout-seconds <N>         fill deadline (default 30, the SYS-44b value)
  --broker-order-id <ID>        broker order id bound at submit time (default B-0001)
  --fill-after-seconds <N>      mocked IB fills the order at N s (omit: never fills)
  --probe-error <KIND>          inject a probe fault: connectivity | order-state | probe-timeout
  --premature-timeout-at <N>    inject a LYING probe reporting a timeout at N s (< deadline)
  --fail-email                  email transport fails
  --fail-sms                    SMS transport fails
  --fail-cancel                 IB cancel_order fails
  --fail-disconnect             IB disconnect fails
  --no-broker-binding           simulate a missing domain->broker order-id binding

EXIT CODES:
  0  filled before timeout (no SYS-44b action)
  1  timed out — SYS-44b page + cancel + disconnect ran (outcomes in the JSON)
  3  fail-closed probe refusal (unavailable/inconsistent) — nothing destructive ran
  2  usage or scenario error (no outcome produced)
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (subcommand, rest) = match args.split_first() {
        None => {
            eprintln!("safe002_liquidation_timeout_cli: missing subcommand\n\n{USAGE}");
            return ExitCode::from(2);
        }
        Some((first, rest)) => (first.as_str(), rest),
    };
    let outcome = match subcommand {
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        "resolve" => cmd_resolve(rest),
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    };
    match outcome {
        Ok(code) => code,
        Err(error) => {
            eprintln!("safe002_liquidation_timeout_cli: {error}");
            ExitCode::from(2)
        }
    }
}

#[derive(Debug, Default)]
struct ParsedArgs {
    live_strategy: Option<String>,
    order_correlation: Option<String>,
    symbol: Option<String>,
    side: Option<String>,
    quantity: Option<u64>,
    timeout_seconds: Option<u64>,
    broker_order_id: Option<String>,
    fill_after_seconds: Option<u64>,
    probe_error: Option<ProbeFault>,
    premature_timeout_at: Option<u64>,
    fail_email: bool,
    fail_sms: bool,
    fail_cancel: bool,
    fail_disconnect: bool,
    no_broker_binding: bool,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = Self::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--live-strategy" => {
                    set_once(
                        &mut parsed.live_strategy,
                        take_value(&mut iter, flag)?,
                        flag,
                    )?;
                }
                "--order-correlation" => {
                    set_once(
                        &mut parsed.order_correlation,
                        take_value(&mut iter, flag)?,
                        flag,
                    )?;
                }
                "--symbol" => {
                    set_once(&mut parsed.symbol, take_value(&mut iter, flag)?, flag)?;
                }
                "--side" => {
                    let side = take_value(&mut iter, flag)?;
                    if side != "BUY" && side != "SELL" {
                        return Err(format!(
                            "flag '--side' requires BUY or SELL, got '{side}'\n\n{USAGE}"
                        ));
                    }
                    set_once(&mut parsed.side, side, flag)?;
                }
                "--quantity" => {
                    let quantity = parse_u64(&take_value(&mut iter, flag)?, flag)?;
                    if quantity == 0 {
                        return Err(format!(
                            "flag '--quantity' must be positive (a zero-quantity liquidation \
                             is not an order)\n\n{USAGE}"
                        ));
                    }
                    set_once(&mut parsed.quantity, quantity, flag)?;
                }
                "--timeout-seconds" => {
                    let timeout = parse_u64(&take_value(&mut iter, flag)?, flag)?;
                    if timeout == 0 {
                        return Err(format!(
                            "flag '--timeout-seconds' must be positive\n\n{USAGE}"
                        ));
                    }
                    set_once(&mut parsed.timeout_seconds, timeout, flag)?;
                }
                "--broker-order-id" => {
                    set_once(
                        &mut parsed.broker_order_id,
                        take_value(&mut iter, flag)?,
                        flag,
                    )?;
                }
                "--fill-after-seconds" => {
                    set_once(
                        &mut parsed.fill_after_seconds,
                        parse_u64(&take_value(&mut iter, flag)?, flag)?,
                        flag,
                    )?;
                }
                "--probe-error" => {
                    let kind = take_value(&mut iter, flag)?;
                    let fault = match kind.as_str() {
                        "connectivity" => ProbeFault::Connectivity,
                        "order-state" => ProbeFault::OrderState,
                        "probe-timeout" => ProbeFault::ProbeTimeout,
                        other => {
                            return Err(format!(
                                "flag '--probe-error' requires connectivity | order-state | \
                                 probe-timeout, got '{other}'\n\n{USAGE}"
                            ));
                        }
                    };
                    set_once(&mut parsed.probe_error, fault, flag)?;
                }
                "--premature-timeout-at" => {
                    set_once(
                        &mut parsed.premature_timeout_at,
                        parse_u64(&take_value(&mut iter, flag)?, flag)?,
                        flag,
                    )?;
                }
                "--fail-email" => set_bool_once(&mut parsed.fail_email, flag)?,
                "--fail-sms" => set_bool_once(&mut parsed.fail_sms, flag)?,
                "--fail-cancel" => set_bool_once(&mut parsed.fail_cancel, flag)?,
                "--fail-disconnect" => set_bool_once(&mut parsed.fail_disconnect, flag)?,
                "--no-broker-binding" => set_bool_once(&mut parsed.no_broker_binding, flag)?,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn into_scenario(self) -> Result<TimeoutScenario, String> {
        let mut scenario = TimeoutScenario::reference_unfilled();
        if let Some(live_strategy) = self.live_strategy {
            scenario.live_strategy_id = live_strategy;
        }
        if let Some(order_correlation) = self.order_correlation {
            scenario.order_correlation_id = order_correlation;
        }
        if let Some(symbol) = self.symbol {
            scenario.symbol = symbol;
        }
        if let Some(side) = self.side {
            scenario.side = side;
        }
        if let Some(quantity) = self.quantity {
            scenario.quantity = quantity;
        }
        if let Some(timeout_seconds) = self.timeout_seconds {
            scenario.timeout_seconds = timeout_seconds;
        }
        if let Some(broker_order_id) = self.broker_order_id {
            scenario.broker_order_id = broker_order_id;
        }
        scenario.fill_after_seconds = self.fill_after_seconds;
        scenario.probe_fault = self.probe_error;
        scenario.premature_timeout_at = self.premature_timeout_at;
        if let Some(at) = scenario.premature_timeout_at {
            if at >= scenario.timeout_seconds {
                return Err(format!(
                    "--premature-timeout-at must be BEFORE the deadline ({}s) to model a lying \
                     probe, got {at}\n\n{USAGE}",
                    scenario.timeout_seconds
                ));
            }
            if scenario.probe_fault.is_some() || scenario.fill_after_seconds.is_some() {
                return Err(format!(
                    "--premature-timeout-at replaces the probe; it cannot combine with \
                     --probe-error or --fill-after-seconds\n\n{USAGE}"
                ));
            }
        }
        scenario.fail_email = self.fail_email;
        scenario.fail_sms = self.fail_sms;
        scenario.fail_cancel = self.fail_cancel;
        scenario.fail_disconnect = self.fail_disconnect;
        scenario.bind_broker_order_id = !self.no_broker_binding;
        Ok(scenario)
    }
}

fn set_once<T>(slot: &mut Option<T>, value: T, flag: &str) -> Result<(), String> {
    if slot.is_some() {
        return Err(format!("duplicate flag '{flag}'\n\n{USAGE}"));
    }
    *slot = Some(value);
    Ok(())
}

fn set_bool_once(slot: &mut bool, flag: &str) -> Result<(), String> {
    if *slot {
        return Err(format!("duplicate flag '{flag}'\n\n{USAGE}"));
    }
    *slot = true;
    Ok(())
}

fn take_value(iter: &mut std::slice::Iter<'_, String>, flag: &str) -> Result<String, String> {
    match iter.next() {
        Some(value) if !value.starts_with("--") => Ok(value.clone()),
        _ => Err(format!("flag '{flag}' requires a value\n\n{USAGE}")),
    }
}

fn parse_u64(raw: &str, flag: &str) -> Result<u64, String> {
    raw.parse::<u64>().map_err(|_| {
        format!("flag '{flag}' requires a non-negative integer, got '{raw}'\n\n{USAGE}")
    })
}

fn cmd_resolve(rest: &[String]) -> Result<ExitCode, String> {
    let scenario = ParsedArgs::parse(rest)?.into_scenario()?;
    let run = run_fixture_timeout(&scenario)?;
    let (disposition, exit_code) = disposition(&run);
    println!("outcome:{}", run_to_json(&run, disposition));
    if exit_code == 1 {
        eprintln!(
            "safe002_liquidation_timeout_cli: liquidation TIMED OUT — the SYS-44b sequence ran; \
             positions require manual resolution (inspect the outcome line)"
        );
    }
    if exit_code == 3 {
        eprintln!(
            "safe002_liquidation_timeout_cli: fail-closed probe refusal — NO automated \
             cancel/disconnect was taken; resolve the order state manually"
        );
    }
    Ok(ExitCode::from(exit_code))
}

fn disposition(run: &FixtureTimeoutRun) -> (&'static str, u8) {
    match &run.result {
        Ok(_) => ("FILLED_BEFORE_TIMEOUT", 0),
        Err(error) => match error.category {
            OrderErrorCategory::KillSwitchLiquidationTimeout => ("TIMED_OUT_UNFILLED", 1),
            _ if error.error_type == "KillSwitchLiquidationProbeInconsistent" => {
                ("PROBE_INCONSISTENT", 3)
            }
            _ => ("PROBE_UNAVAILABLE", 3),
        },
    }
}

/// JSON string escaping covering the FULL set RFC 8259 requires: quote,
/// backslash, and EVERY C0 control character (U+0000..U+001F). The outcome
/// line is the durable-audit input — an unescaped control byte smuggled in
/// through a symbol / order id / failure reason would make the line
/// unparseable AFTER the SYS-44b side effects already ran, suppressing the
/// LIQUIDATION_TIMEOUT record. Escaping must therefore be total, not
/// best-effort.
fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            control if (control as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", control as u32));
            }
            other => out.push(other),
        }
    }
    out
}

fn outcome_to_json(outcome: &SideEffectOutcome) -> String {
    match outcome {
        SideEffectOutcome::NotAttempted => "{\"status\":\"NOT_ATTEMPTED\"}".to_string(),
        SideEffectOutcome::Succeeded => "{\"status\":\"SUCCEEDED\"}".to_string(),
        SideEffectOutcome::Failed { reason } => {
            format!(
                "{{\"status\":\"FAILED\",\"reason\":\"{}\"}}",
                json_escape(reason)
            )
        }
    }
}

fn order_to_json(order: &UnfilledLiquidationOrder) -> String {
    format!(
        "{{\"order_id\":\"{}\",\"symbol\":\"{}\",\"side\":\"{}\",\"quantity\":{}}}",
        json_escape(&order.order_id),
        json_escape(&order.symbol),
        json_escape(&order.side),
        order.quantity,
    )
}

fn run_to_json(run: &FixtureTimeoutRun, disposition: &str) -> String {
    let gateway_calls: Vec<String> = run
        .gateway_calls
        .iter()
        .map(|call| format!("\"{}\"", json_escape(call)))
        .collect();
    let notification = format!(
        "{{\"events\":{},\"email_accepted\":{},\"sms_accepted\":{}}}",
        run.notifications.len(),
        run.email_pages.len(),
        run.sms_pages.len(),
    );
    let common = format!(
        "\"disposition\":\"{disposition}\",\"notification\":{notification},\
         \"gateway_calls\":[{}],\"probe_polls\":{},\"simulated_elapsed_ms\":{}",
        gateway_calls.join(","),
        run.probe_polls,
        run.simulated_elapsed_ms,
    );
    match &run.result {
        Ok(resolved) => format!(
            "{{{common},\"category\":null,\"error_type\":null,\
             \"live_strategy_id\":\"{}\",\"elapsed_seconds\":{},\
             \"manual_resolution_required\":false,\
             \"cleanup\":{{\"operator_alert\":{na},\"liquidation_cancel\":{na},\
             \"ib_disconnect\":{na},\"audit_recorded\":{}}}}}",
            json_escape(resolved.live_strategy_id.as_str()),
            resolved.elapsed_seconds,
            !run.timeout_events.is_empty(),
            na = "{\"status\":\"NOT_ATTEMPTED\"}",
        ),
        Err(error) => {
            let manual_resolution_required =
                error.category == OrderErrorCategory::KillSwitchLiquidationTimeout;
            format!(
                "{{{common},\"category\":\"{}\",\"error_type\":\"{}\",\
                 \"message\":\"{}\",\"unfilled_order\":{},\
                 \"manual_resolution_required\":{manual_resolution_required},\
                 \"cleanup\":{{\"operator_alert\":{},\"liquidation_cancel\":{},\
                 \"ib_disconnect\":{},\"audit_recorded\":{}}}}}",
                json_escape(error.category.as_str()),
                json_escape(&error.error_type),
                json_escape(&error.message),
                order_to_json(&error.original_request.unfilled_order),
                outcome_to_json(&error.cleanup.operator_alert),
                outcome_to_json(&error.cleanup.liquidation_cancel),
                outcome_to_json(&error.cleanup.ib_disconnect),
                error.cleanup.audit_recorded,
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use super::json_escape;

    #[test]
    fn json_escape_covers_every_c0_control_character() {
        // RFC 8259: an unescaped control byte would make the outcome line
        // unparseable AFTER the SYS-44b side effects ran — escaping is total.
        for code in 0u32..0x20 {
            let ch = char::from_u32(code).expect("C0 is valid");
            let escaped = json_escape(&ch.to_string());
            assert!(
                !escaped.chars().any(|c| (c as u32) < 0x20),
                "U+{code:04X} leaked through unescaped: {escaped:?}"
            );
        }
        assert_eq!(json_escape("\u{0001}"), "\\u0001");
        assert_eq!(json_escape("\n"), "\\n");
        assert_eq!(json_escape("\""), "\\\"");
        assert_eq!(json_escape("\\"), "\\\\");
        assert_eq!(json_escape("AA\u{0001}PL"), "AA\\u0001PL");
    }
}
