//! SRS-RESV-003 / SyRS SYS-49a operator CLI — demonstrate that manual and
//! configurable automatic Hot-Swap triggers behave per the acceptance criteria:
//! manual promotion, drawdown-triggered demotion, top-ranked promotion, and
//! highest-momentum promotion are configurable; automatic triggers default to
//! disabled; and all swap triggers are logged.
//!
//! This is the CLI arm of the operator surface named in SYS-49a
//! ("via the dashboard, CLI, or REST API"); the dashboard (SRS-UI-001 / UI-5)
//! and REST (SRS-API-001) arms are deferred. Emits deterministic `key:value`
//! proof lines (repo convention) and fails closed on unknown / duplicate /
//! valueless flags. The trigger layer only proposes + logs — it does NOT execute
//! the swap (that is the SRS-RESV-004 gate).

use atp_orchestrator::{
    HotSwapSideEffectError, HotSwapTriggerLog, LiveStrategyProbe, ReservoirRankingSource,
    StrategyOrchestrator,
};
use atp_types::{
    DrawdownDemotionTrigger, DrawdownThresholdBps, HotSwapTriggerConfig, HotSwapTriggerEvent,
    LiveStrategyState, RankedStrategy, RankingPromotionTrigger, ReservoirRankingSnapshot,
    StrategyId, TriggerRationale,
};
use std::cell::RefCell;
use std::env;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

/// Fixed demonstration observation timestamp (wall-clock time is intentionally
/// not read — the tool is deterministic).
const OBSERVED_AT_SECONDS: u64 = 1_715_000_000;

const USAGE: &str = "\
resv003_hot_swap_trigger_cli — SRS-RESV-003 Hot-Swap trigger configuration + logging

USAGE:
    resv003_hot_swap_trigger_cli <SUBCOMMAND> [FLAGS]

SUBCOMMANDS:
    config      Print the default trigger configuration (proves automatic
                triggers default to disabled; manual is always available).
    evaluate    Evaluate the automatic triggers against fixture inputs and print
                which fired + the logged records.
    manual      Fire the always-available manual promotion and print its log record.
    help        Print this help.

evaluate FLAGS:
    --live <id>                  the current live strategy id (required)
    --live-drawdown <bps>        the live strategy's observed drawdown in bps (default 0)
    --rank <id>:<rank>:<score>:<momentum>   add one reservoir ranking row (repeatable)
    --eval-window <days>         evaluation window in days (default 30)
    --drawdown-threshold <bps>   ENABLE drawdown-demotion at this threshold (1..=10000)
    --top-ranked                 ENABLE top-ranked promotion
    --highest-momentum           ENABLE highest-momentum promotion
    --log <path>                 append each fired trigger to a durable JSONL log (fsynced)
    --inject disabled            non-vacuity: ignore the enable flags and use the
                                 default (all-disabled) config, proving nothing fires

manual FLAGS:
    --demoting <id>              the current live strategy to demote (required)
    --candidate <id>             the reservoir strategy to promote (required)
    --log <path>                 append the trigger to a durable JSONL log (fsynced)
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("resv003_hot_swap_trigger_cli: {err}");
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
        "config" => cmd_config(rest),
        "evaluate" => cmd_evaluate(rest),
        "manual" => cmd_manual(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

/// True if any token requests help, so a subcommand can show usage instead of erroring.
fn wants_help(args: &[String]) -> bool {
    args.iter()
        .any(|arg| matches!(arg.as_str(), "help" | "--help" | "-h"))
}

/// Print the default (all-automatic-disabled) configuration. Demonstrates the
/// SYS-49a "automatic triggers shall default to disabled" clause and that manual
/// promotion is always available.
fn cmd_config(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if let Some(flag) = rest.first() {
        return Err(format!("unknown flag '{flag}'\n\n{USAGE}"));
    }

    let config = HotSwapTriggerConfig::default();
    println!("manual-promotion-available:true");
    println!(
        "drawdown-demotion-enabled:{}",
        config.drawdown_demotion.is_enabled()
    );
    println!(
        "top-ranked-promotion-enabled:{}",
        config.top_ranked_promotion.is_enabled()
    );
    println!(
        "highest-momentum-promotion-enabled:{}",
        config.highest_momentum_promotion.is_enabled()
    );
    println!("any-automatic-enabled:{}", config.any_automatic_enabled());
    println!("default-disabled:{}", !config.any_automatic_enabled());
    Ok(())
}

/// Evaluate the automatic triggers against fixture inputs and print which fired
/// plus the logged records. With `--inject disabled` the enable flags are
/// ignored and the default (all-disabled) config is used, proving nothing fires
/// even when the inputs would otherwise trigger — the non-vacuity control.
fn cmd_evaluate(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }

    let mut live_id: Option<String> = None;
    let mut live_drawdown_bps: u32 = 0;
    let mut live_drawdown_seen = false;
    let mut eval_window_days: u32 = 30;
    let mut eval_window_seen = false;
    let mut drawdown_threshold: Option<u32> = None;
    let mut top_ranked = false;
    let mut highest_momentum = false;
    let mut inject_disabled = false;
    let mut log_path: Option<String> = None;
    let mut ranked: Vec<RankedStrategy> = Vec::new();

    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--live" => {
                if live_id.is_some() {
                    return Err(dup(flag));
                }
                live_id = Some(take_value(&mut iter, flag)?);
            }
            "--live-drawdown" => {
                if live_drawdown_seen {
                    return Err(dup(flag));
                }
                live_drawdown_seen = true;
                live_drawdown_bps = parse_u32(&take_value(&mut iter, flag)?, flag)?;
            }
            "--eval-window" => {
                if eval_window_seen {
                    return Err(dup(flag));
                }
                eval_window_seen = true;
                eval_window_days = parse_u32(&take_value(&mut iter, flag)?, flag)?;
            }
            "--drawdown-threshold" => {
                if drawdown_threshold.is_some() {
                    return Err(dup(flag));
                }
                drawdown_threshold = Some(parse_u32(&take_value(&mut iter, flag)?, flag)?);
            }
            "--top-ranked" => {
                if top_ranked {
                    return Err(dup(flag));
                }
                top_ranked = true;
            }
            "--highest-momentum" => {
                if highest_momentum {
                    return Err(dup(flag));
                }
                highest_momentum = true;
            }
            "--inject" => {
                if inject_disabled {
                    return Err(dup(flag));
                }
                let value = take_value(&mut iter, flag)?;
                if value != "disabled" {
                    return Err(format!(
                        "--inject expects 'disabled' (got '{value}')\n\n{USAGE}"
                    ));
                }
                inject_disabled = true;
            }
            "--log" => {
                if log_path.is_some() {
                    return Err(dup(flag));
                }
                log_path = Some(take_value(&mut iter, flag)?);
            }
            "--rank" => {
                ranked.push(parse_rank_row(&take_value(&mut iter, flag)?)?);
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }

    let live_id = live_id.ok_or_else(|| format!("--live <id> is required\n\n{USAGE}"))?;

    // Build the config from the enable flags — unless `--inject disabled` forces
    // the default (all-automatic-disabled) posture to prove it fires nothing.
    let config = if inject_disabled {
        HotSwapTriggerConfig::default()
    } else {
        let drawdown_demotion = match drawdown_threshold {
            Some(bps) => DrawdownDemotionTrigger::Enabled {
                threshold: DrawdownThresholdBps::new(bps)
                    .map_err(|error| format!("invalid --drawdown-threshold: {error}"))?,
            },
            None => DrawdownDemotionTrigger::Disabled,
        };
        HotSwapTriggerConfig {
            drawdown_demotion,
            top_ranked_promotion: enable_flag(top_ranked),
            highest_momentum_promotion: enable_flag(highest_momentum),
        }
    };

    let live = FixedLiveProbe {
        state: Some(LiveStrategyState {
            strategy_id: StrategyId::new(&live_id),
            drawdown_bps: live_drawdown_bps,
        }),
    };
    let ranking = FixedRanking {
        snapshot: ReservoirRankingSnapshot {
            evaluation_window_days: eval_window_days,
            ranked,
        },
    };
    let log = CollectingTriggerLog::new(log_path.as_deref().map(PathBuf::from));

    let evaluation = StrategyOrchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    println!("inject-disabled:{inject_disabled}");
    println!("any-automatic-enabled:{}", config.any_automatic_enabled());
    for proposal in &evaluation.fired {
        println!(
            "fired:{} demoting:{} candidate:{} rationale:{}",
            proposal.kind.as_str(),
            proposal.demoting_strategy_id.as_str(),
            proposal.candidate_strategy_id.as_str(),
            rationale_to_string(&proposal.rationale),
        );
    }
    println!("fired-count:{}", evaluation.fired.len());
    println!(
        "selected:{}",
        evaluation
            .selected
            .as_ref()
            .map(|proposal| proposal.kind.as_str())
            .unwrap_or("NONE"),
    );
    let logged = log.events.borrow().len();
    println!("logged-count:{logged}");
    // Mechanical guarantee of "all swap triggers are logged": one log record per
    // fired trigger, recorded in the same path that builds the proposal.
    println!("all-triggers-logged:{}", logged == evaluation.fired.len());

    if let Some(path) = &log_path {
        let persisted = count_log_records(Path::new(path))?;
        println!("log-persisted:{path}");
        println!("log-file-records:{persisted}");
    }
    Ok(())
}

/// Fire the always-available manual promotion (SYS-49a(a)) and print its record.
/// Manual selection is not gated by the automatic-trigger config.
fn cmd_manual(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }

    let mut demoting: Option<String> = None;
    let mut candidate: Option<String> = None;
    let mut log_path: Option<String> = None;

    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--demoting" => {
                if demoting.is_some() {
                    return Err(dup(flag));
                }
                demoting = Some(take_value(&mut iter, flag)?);
            }
            "--candidate" => {
                if candidate.is_some() {
                    return Err(dup(flag));
                }
                candidate = Some(take_value(&mut iter, flag)?);
            }
            "--log" => {
                if log_path.is_some() {
                    return Err(dup(flag));
                }
                log_path = Some(take_value(&mut iter, flag)?);
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }

    let demoting = demoting.ok_or_else(|| format!("--demoting <id> is required\n\n{USAGE}"))?;
    let candidate = candidate.ok_or_else(|| format!("--candidate <id> is required\n\n{USAGE}"))?;

    let log = CollectingTriggerLog::new(log_path.as_deref().map(PathBuf::from));
    let proposal = StrategyOrchestrator.request_manual_promotion(
        StrategyId::new(&demoting),
        StrategyId::new(&candidate),
        &log,
        OBSERVED_AT_SECONDS,
    );

    println!("manual-always-available:true");
    println!(
        "fired:{} demoting:{} candidate:{} rationale:{}",
        proposal.kind.as_str(),
        proposal.demoting_strategy_id.as_str(),
        proposal.candidate_strategy_id.as_str(),
        rationale_to_string(&proposal.rationale),
    );
    println!("logged-count:{}", log.events.borrow().len());

    if let Some(path) = &log_path {
        let persisted = count_log_records(Path::new(path))?;
        println!("log-persisted:{path}");
        println!("log-file-records:{persisted}");
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

fn enable_flag(enabled: bool) -> RankingPromotionTrigger {
    if enabled {
        RankingPromotionTrigger::Enabled
    } else {
        RankingPromotionTrigger::Disabled
    }
}

fn dup(flag: &str) -> String {
    format!("duplicate flag '{flag}'\n\n{USAGE}")
}

fn parse_u32(value: &str, flag: &str) -> Result<u32, String> {
    value
        .parse()
        .map_err(|_| format!("{flag} expects a u32 (got '{value}')\n\n{USAGE}"))
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}

/// Parse a `--rank <id>:<rank>:<score>:<momentum>` ranking row. Non-finite
/// scores are rejected at the input boundary (fail closed).
fn parse_rank_row(spec: &str) -> Result<RankedStrategy, String> {
    let parts: Vec<&str> = spec.split(':').collect();
    if parts.len() != 4 {
        return Err(format!(
            "--rank expects '<id>:<rank>:<score>:<momentum>' (got '{spec}')\n\n{USAGE}"
        ));
    }
    let id = parts[0];
    if id.is_empty() {
        return Err(format!("--rank id must be non-empty (got '{spec}')"));
    }
    let rank: u32 = parts[1]
        .parse()
        .map_err(|_| format!("--rank rank must be a u32 (got '{}')", parts[1]))?;
    let score: f64 = parts[2]
        .parse()
        .map_err(|_| format!("--rank score must be a number (got '{}')", parts[2]))?;
    let momentum: f64 = parts[3]
        .parse()
        .map_err(|_| format!("--rank momentum must be a number (got '{}')", parts[3]))?;
    if !score.is_finite() || !momentum.is_finite() {
        return Err(format!(
            "--rank score/momentum must be finite (got '{spec}')"
        ));
    }
    Ok(RankedStrategy {
        strategy_id: StrategyId::new(id),
        rank,
        risk_adjusted_score: score,
        momentum_score: momentum,
    })
}

fn rationale_to_string(rationale: &TriggerRationale) -> String {
    match rationale {
        TriggerRationale::ManualSelection => "manual-selection".to_string(),
        TriggerRationale::DrawdownBreached {
            observed_bps,
            threshold_bps,
        } => {
            format!("drawdown-breached(observed_bps={observed_bps},threshold_bps={threshold_bps})")
        }
        TriggerRationale::TopRanked { rank, score } => {
            format!("top-ranked(rank={rank},score={score})")
        }
        TriggerRationale::HighestMomentum { momentum_score } => {
            format!("highest-momentum(momentum_score={momentum_score})")
        }
    }
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

fn event_to_json(event: &HotSwapTriggerEvent) -> String {
    format!(
        "{{\"kind\":\"{}\",\"demoting_strategy_id\":\"{}\",\"candidate_strategy_id\":\"{}\",\"rationale\":\"{}\",\"observed_at_seconds\":{}}}",
        event.kind.as_str(),
        json_escape(event.demoting_strategy_id.as_str()),
        json_escape(event.candidate_strategy_id.as_str()),
        json_escape(&rationale_to_string(&event.rationale)),
        event.observed_at_seconds,
    )
}

/// Durably append one trigger event as a JSON line: write + flush + fsync so the
/// record survives a crash. This is the RESV-003 demonstration of the "all swap
/// triggers are logged" clause; the durable, queryable, dashboard-viewable SYS-61
/// system-log store is the deferred SRS-LOG-001 sink.
fn append_event_line(path: &Path, event: &HotSwapTriggerEvent) -> Result<(), String> {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| format!("cannot open log file {}: {error}", path.display()))?;
    let mut line = event_to_json(event);
    line.push('\n');
    file.write_all(line.as_bytes())
        .and_then(|()| file.flush())
        .and_then(|()| file.sync_all())
        .map_err(|error| format!("cannot append to log file {}: {error}", path.display()))
}

fn count_log_records(path: &Path) -> Result<usize, String> {
    let content = std::fs::read_to_string(path)
        .map_err(|error| format!("cannot read log file {}: {error}", path.display()))?;
    Ok(content
        .lines()
        .filter(|line| !line.trim().is_empty())
        .count())
}

// --------------------------------------------------------------------------- //
// Concrete demonstration ports
// --------------------------------------------------------------------------- //

struct FixedLiveProbe {
    state: Option<LiveStrategyState>,
}

impl LiveStrategyProbe for FixedLiveProbe {
    fn current_live(&self) -> Option<LiveStrategyState> {
        self.state.clone()
    }
}

struct FixedRanking {
    snapshot: ReservoirRankingSnapshot,
}

impl ReservoirRankingSource for FixedRanking {
    fn snapshot(&self) -> ReservoirRankingSnapshot {
        self.snapshot.clone()
    }
}

/// Collects every trigger event in memory AND, when a `--log` path is given,
/// durably appends it to a JSONL file. Best-effort: a file-write failure is
/// surfaced as `Err` but the in-memory record still stands (the evaluator treats
/// the sink as best-effort and never un-fires a trigger).
struct CollectingTriggerLog {
    events: RefCell<Vec<HotSwapTriggerEvent>>,
    sink_path: Option<PathBuf>,
}

impl CollectingTriggerLog {
    fn new(sink_path: Option<PathBuf>) -> Self {
        Self {
            events: RefCell::new(Vec::new()),
            sink_path,
        }
    }
}

impl HotSwapTriggerLog for CollectingTriggerLog {
    fn record(&self, event: HotSwapTriggerEvent) -> Result<(), HotSwapSideEffectError> {
        self.events.borrow_mut().push(event.clone());
        if let Some(path) = &self.sink_path {
            append_event_line(path, &event).map_err(HotSwapSideEffectError::new)?;
        }
        Ok(())
    }
}
