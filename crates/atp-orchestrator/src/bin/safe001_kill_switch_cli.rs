//! SRS-SAFE-001 kill-switch activation operator CLI (SyRS SYS-44a; NFR-P3;
//! NFR-SC1; StRS SN-1.11).
//!
//! Drives the REAL `atp-execution` activation gate over a REAL
//! `LiveExecutionState` (validated builders) and a REAL `atp-simulation`
//! `PaperEngineFleet`, measured on a real monotonic clock. The brokerage
//! transport is the deterministic mocked-IB fixture the feature's own
//! verification Step 2 prescribes; the LIVE transport is the deferred
//! SRS-EXE-006 adapter (`kill_switch_activation_contract.deferred[]`).
//!
//! Subcommands:
//!   * `activate` — run ONE activation scenario and print `report:{json}`.
//!     Exit 0 = sequence ran fully clean and every engine is halted;
//!     exit 1 = sequence ran but the report records at least one failure
//!     (the report is still printed — the report is the truth);
//!     exit 2 = the scenario could not run at all (usage/fixture error).
//!   * `perf` — repeat fresh activations (default: the 50-position /
//!     50-resting / 30-engine NFR-SC1 reference shape) and print nearest-rank
//!     p50/p95/p99/p99.9 percentiles (`atp_types::perf::LatencyPercentiles`)
//!     plus `verdict:PASS|FAIL` on the NFR-P3 rule
//!     `max liquidations_submitted_ms <= 5000`. NFR-P3 is a one-shot
//!     deadline, so no `LatencyNfr` catalog variant is added.

use std::process::ExitCode;
use std::time::Instant;

use atp_orchestrator::kill_switch_activation::{
    generated_positions, run_fixture_activation, FixtureActivation, Scenario,
};
use atp_types::perf::{LatencyPercentiles, Percentile};
use atp_types::{
    KillSwitchActivationReport, OrderSide, SideEffectOutcome, KILL_SWITCH_ACTIVATION_BUDGET_MS,
};

const USAGE: &str = "\
SRS-SAFE-001 kill-switch activation CLI (mocked-IB fixture transport; live IB = SRS-EXE-006)

USAGE:
  safe001_kill_switch_cli activate [OPTIONS]
  safe001_kill_switch_cli perf [OPTIONS]
  safe001_kill_switch_cli help

ACTIVATE OPTIONS:
  --activation-id <ID>          activation correlation id (default act-fixture)
  --live-strategy <ID>          live strategy id (default alpha-live)
  --resting <N>                 resting live-strategy orders (default 50)
  --positions <N>               generated open positions (default 50)
  --position <SYM:QTY>          explicit position (repeatable; replaces generated)
  --engines <N>                 paper engines in the fleet (default 30)
  --fail-cancel <ORDER_ID>      inject a cancel failure (repeatable)
  --fail-liquidation <SYM>      inject a liquidation failure (repeatable)
  --fail-disconnect             inject a disconnect failure
  --latency-ms-per-call <N>     fixture transport latency per call

PERF OPTIONS:
  --iterations <N>              fresh activations to run (default 20)
  --resting <N> / --positions <N> / --engines <N>   scenario shape
  --latency-ms-per-call <N>     fixture transport latency per call

EXIT CODES:
  0  activate: report fully clean, every engine halted / perf: verdict PASS
  1  activate: report records failures / perf: verdict FAIL
  2  usage or fixture error (no report produced)
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (subcommand, rest) = match args.split_first() {
        None => {
            eprintln!("safe001_kill_switch_cli: missing subcommand\n\n{USAGE}");
            return ExitCode::from(2);
        }
        Some((first, rest)) => (first.as_str(), rest),
    };
    let outcome = match subcommand {
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        "activate" => cmd_activate(rest),
        "perf" => cmd_perf(rest),
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    };
    match outcome {
        Ok(code) => code,
        Err(error) => {
            eprintln!("safe001_kill_switch_cli: {error}");
            ExitCode::from(2)
        }
    }
}

#[derive(Debug)]
struct ParsedArgs {
    activation_id: Option<String>,
    live_strategy: Option<String>,
    resting: Option<u32>,
    positions: Option<u32>,
    explicit_positions: Vec<(String, i64)>,
    engines: Option<u32>,
    fail_cancel: Vec<String>,
    fail_liquidation: Vec<String>,
    fail_disconnect: bool,
    latency_ms_per_call: Option<u64>,
    iterations: Option<u32>,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = Self {
            activation_id: None,
            live_strategy: None,
            resting: None,
            positions: None,
            explicit_positions: Vec::new(),
            engines: None,
            fail_cancel: Vec::new(),
            fail_liquidation: Vec::new(),
            fail_disconnect: false,
            latency_ms_per_call: None,
            iterations: None,
        };
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--activation-id" => {
                    set_once(
                        &mut parsed.activation_id,
                        take_value(&mut iter, flag)?,
                        flag,
                    )?;
                }
                "--live-strategy" => {
                    set_once(
                        &mut parsed.live_strategy,
                        take_value(&mut iter, flag)?,
                        flag,
                    )?;
                }
                "--resting" => {
                    set_once(
                        &mut parsed.resting,
                        parse_u32(&take_value(&mut iter, flag)?, flag)?,
                        flag,
                    )?;
                }
                "--positions" => {
                    set_once(
                        &mut parsed.positions,
                        parse_u32(&take_value(&mut iter, flag)?, flag)?,
                        flag,
                    )?;
                }
                "--position" => {
                    parsed
                        .explicit_positions
                        .push(parse_position(&take_value(&mut iter, flag)?)?);
                }
                "--engines" => {
                    set_once(
                        &mut parsed.engines,
                        parse_u32(&take_value(&mut iter, flag)?, flag)?,
                        flag,
                    )?;
                }
                "--fail-cancel" => parsed.fail_cancel.push(take_value(&mut iter, flag)?),
                "--fail-liquidation" => {
                    parsed.fail_liquidation.push(take_value(&mut iter, flag)?);
                }
                "--fail-disconnect" => {
                    if parsed.fail_disconnect {
                        return Err(format!("duplicate flag '{flag}'\n\n{USAGE}"));
                    }
                    parsed.fail_disconnect = true;
                }
                "--latency-ms-per-call" => {
                    set_once(
                        &mut parsed.latency_ms_per_call,
                        parse_u64(&take_value(&mut iter, flag)?, flag)?,
                        flag,
                    )?;
                }
                "--iterations" => {
                    set_once(
                        &mut parsed.iterations,
                        parse_u32(&take_value(&mut iter, flag)?, flag)?,
                        flag,
                    )?;
                }
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn into_scenario(self) -> Scenario {
        let mut scenario = Scenario::reference_baseline();
        if let Some(activation_id) = self.activation_id {
            scenario.activation_id = activation_id;
        }
        if let Some(live_strategy) = self.live_strategy {
            scenario.live_strategy_id = live_strategy;
        }
        if let Some(resting) = self.resting {
            scenario.resting_orders = resting;
        }
        scenario.positions = if self.explicit_positions.is_empty() {
            generated_positions(self.positions.unwrap_or(50))
        } else {
            self.explicit_positions
        };
        if let Some(engines) = self.engines {
            scenario.engines = engines;
        }
        scenario.fail_cancel_order_ids = self.fail_cancel;
        scenario.fail_liquidation_symbols = self.fail_liquidation;
        scenario.fail_disconnect = self.fail_disconnect;
        scenario.latency_ms_per_call = self.latency_ms_per_call;
        scenario
    }
}

fn set_once<T>(slot: &mut Option<T>, value: T, flag: &str) -> Result<(), String> {
    if slot.is_some() {
        return Err(format!("duplicate flag '{flag}'\n\n{USAGE}"));
    }
    *slot = Some(value);
    Ok(())
}

fn take_value(iter: &mut std::slice::Iter<'_, String>, flag: &str) -> Result<String, String> {
    match iter.next() {
        Some(value) if !value.starts_with("--") => Ok(value.clone()),
        _ => Err(format!("flag '{flag}' requires a value\n\n{USAGE}")),
    }
}

fn parse_u32(raw: &str, flag: &str) -> Result<u32, String> {
    raw.parse::<u32>().map_err(|_| {
        format!("flag '{flag}' requires a non-negative integer, got '{raw}'\n\n{USAGE}")
    })
}

fn parse_u64(raw: &str, flag: &str) -> Result<u64, String> {
    raw.parse::<u64>().map_err(|_| {
        format!("flag '{flag}' requires a non-negative integer, got '{raw}'\n\n{USAGE}")
    })
}

/// `SYM:QTY` with a non-zero signed quantity (e.g. `AAPL:100`, `MSFT:-50`).
fn parse_position(raw: &str) -> Result<(String, i64), String> {
    let (symbol, quantity_raw) = raw
        .split_once(':')
        .ok_or_else(|| format!("--position requires SYM:QTY, got '{raw}'\n\n{USAGE}"))?;
    if symbol.trim().is_empty() {
        return Err(format!("--position has a blank symbol: '{raw}'\n\n{USAGE}"));
    }
    let quantity: i64 = quantity_raw.parse().map_err(|_| {
        format!("--position quantity must be a signed integer, got '{raw}'\n\n{USAGE}")
    })?;
    if quantity == 0 {
        return Err(format!(
            "--position quantity must be non-zero (a flat symbol is not an open position): '{raw}'\n\n{USAGE}"
        ));
    }
    Ok((symbol.to_string(), quantity))
}

fn cmd_activate(rest: &[String]) -> Result<ExitCode, String> {
    let parsed = ParsedArgs::parse(rest)?;
    if parsed.iterations.is_some() {
        return Err(format!("--iterations is a perf-mode flag\n\n{USAGE}"));
    }
    let scenario = parsed.into_scenario();
    let outcome = run_fixture_activation(&scenario)?;
    println!("report:{}", activation_to_json(&outcome));
    if outcome.report.fully_clean() && outcome.all_engines_halted {
        Ok(ExitCode::SUCCESS)
    } else {
        eprintln!(
            "safe001_kill_switch_cli: activation completed WITH FAILURES — inspect the report \
             (fully_clean={}, all_engines_halted={})",
            outcome.report.fully_clean(),
            outcome.all_engines_halted,
        );
        Ok(ExitCode::from(1))
    }
}

fn cmd_perf(rest: &[String]) -> Result<ExitCode, String> {
    let parsed = ParsedArgs::parse(rest)?;
    if !parsed.explicit_positions.is_empty()
        || !parsed.fail_cancel.is_empty()
        || !parsed.fail_liquidation.is_empty()
        || parsed.fail_disconnect
    {
        return Err(format!(
            "perf mode measures the clean reference shape; fault flags belong to activate\n\n{USAGE}"
        ));
    }
    let iterations = parsed.iterations.unwrap_or(20).max(1);
    let scenario_template = parsed.into_scenario();

    let mut activation_ns_samples: Vec<u64> = Vec::with_capacity(iterations as usize);
    let mut max_liquidations_submitted_ms: u64 = 0;
    let mut any_unclean = false;
    for iteration in 0..iterations {
        let mut scenario = scenario_template.clone();
        scenario.activation_id = format!("perf-{iteration:04}");
        let started = Instant::now();
        let outcome = run_fixture_activation(&scenario)?;
        let elapsed_ns = u64::try_from(started.elapsed().as_nanos()).unwrap_or(u64::MAX);
        activation_ns_samples.push(elapsed_ns);
        max_liquidations_submitted_ms =
            max_liquidations_submitted_ms.max(outcome.report.timings.liquidations_submitted_ms);
        if !(outcome.report.fully_clean() && outcome.all_engines_halted) {
            any_unclean = true;
        }
    }

    let percentiles = LatencyPercentiles::from_samples(&activation_ns_samples)
        .map_err(|error| format!("perf percentile computation failed: {error:?}"))?;

    println!(
        "shape: positions:{} resting:{} engines:{}",
        scenario_template.positions.len(),
        scenario_template.resting_orders,
        scenario_template.engines,
    );
    println!("iterations:{iterations}");
    println!(
        "activation_total p50_ms:{:.3} p95_ms:{:.3} p99_ms:{:.3} p999_ms:{:.3} (nearest-rank over {} samples)",
        percentiles.get_millis_f64(Percentile::P50),
        percentiles.get_millis_f64(Percentile::P95),
        percentiles.get_millis_f64(Percentile::P99),
        percentiles.get_millis_f64(Percentile::P999),
        percentiles.sample_count(),
    );
    println!("max_liquidations_submitted_ms:{max_liquidations_submitted_ms}");
    println!("budget_ms:{KILL_SWITCH_ACTIVATION_BUDGET_MS}");

    // NFR-P3 verdict rule (kill_switch_activation_contract): every
    // activation's cancel+liquidation-submission mark within 5 000 ms — and
    // a run that recorded ANY failure cannot claim a clean perf pass.
    let pass = max_liquidations_submitted_ms <= KILL_SWITCH_ACTIVATION_BUDGET_MS && !any_unclean;
    println!("verdict:{}", if pass { "PASS" } else { "FAIL" });
    Ok(if pass {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    })
}

fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
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

fn side_to_str(side: OrderSide) -> &'static str {
    match side {
        OrderSide::Buy => "BUY",
        OrderSide::Sell => "SELL",
    }
}

fn report_to_json(report: &KillSwitchActivationReport) -> String {
    let cancels: Vec<String> = report
        .resting_order_cancels
        .iter()
        .map(|cancel| {
            let broker = match &cancel.order.broker_order_id {
                Some(broker_order_id) => format!("\"{}\"", json_escape(broker_order_id)),
                None => "null".to_string(),
            };
            format!(
                "{{\"order_id\":\"{}\",\"symbol\":\"{}\",\"broker_order_id\":{},\"outcome\":{}}}",
                json_escape(&cancel.order.order_id),
                json_escape(&cancel.order.symbol),
                broker,
                outcome_to_json(&cancel.outcome),
            )
        })
        .collect();
    let liquidations: Vec<String> = report
        .liquidations
        .iter()
        .map(|liquidation| {
            format!(
                "{{\"symbol\":\"{}\",\"side\":\"{}\",\"quantity\":{},\"outcome\":{}}}",
                json_escape(&liquidation.symbol),
                side_to_str(liquidation.side),
                liquidation.quantity,
                outcome_to_json(&liquidation.outcome),
            )
        })
        .collect();
    let paper_halt_summary = match &report.paper_halt_summary {
        Some(summary) => format!(
            "{{\"engines_total\":{},\"transitioned\":{},\"already_halted\":{}}}",
            summary.engines_total, summary.transitioned, summary.already_halted,
        ),
        None => "null".to_string(),
    };
    format!(
        "{{\"activation_id\":\"{}\",\"live_strategy_id\":\"{}\",\"activated_at_epoch_ms\":{},\
         \"paper_halt\":{},\"paper_halt_summary\":{},\"resting_order_cancels\":[{}],\
         \"liquidations\":[{}],\"ib_disconnect\":{},\
         \"timings\":{{\"halt_completed_ms\":{},\"cancels_completed_ms\":{},\
         \"liquidations_submitted_ms\":{},\"disconnect_completed_ms\":{}}},\
         \"fully_clean\":{},\"within_nfr_p3\":{}}}",
        json_escape(&report.activation_id),
        json_escape(report.live_strategy_id.as_str()),
        report.activated_at_epoch_ms,
        outcome_to_json(&report.paper_halt),
        paper_halt_summary,
        cancels.join(","),
        liquidations.join(","),
        outcome_to_json(&report.ib_disconnect),
        report.timings.halt_completed_ms,
        report.timings.cancels_completed_ms,
        report.timings.liquidations_submitted_ms,
        report.timings.disconnect_completed_ms,
        report.fully_clean(),
        report.within_nfr_p3(),
    )
}

fn activation_to_json(outcome: &FixtureActivation) -> String {
    // The composition-level fact the report cannot know: whether every REAL
    // engine gate ended up HALTED (asserted over the fleet, not the summary).
    let report_json = report_to_json(&outcome.report);
    let core = report_json
        .strip_suffix('}')
        .expect("report JSON is a single object");
    format!(
        "{core},\"all_engines_halted\":{},\"events_recorded\":{}}}",
        outcome.all_engines_halted, outcome.events_recorded,
    )
}
