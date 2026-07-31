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
    trigger_config_store, HotSwapSideEffectError, HotSwapTriggerLog, LiveStrategyProbe,
    ManualPromotionError, ReservoirRankingSource, StrategyOrchestrator,
};
use atp_types::json_scan::{json_string_value, parse_strict_i64, top_level_json_field};
use atp_types::{
    DrawdownDemotionTrigger, DrawdownThresholdBps, HotSwapTriggerConfig, HotSwapTriggerEvent,
    HotSwapTriggerKind, LiveStrategyState, RankedStrategy, RankingPromotionTrigger,
    ReservoirRankingSnapshot, StrategyId, TriggerRationale,
};
use std::env;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

/// Fixed demonstration observation timestamp (wall-clock time is intentionally
/// not read — the tool is deterministic).
const OBSERVED_AT_SECONDS: u64 = 1_715_000_000;

/// The trigger-event line layout this build WRITES (SRS-DATA-015 / SyRS SYS-66).
/// **Version history:** v1 = `{schema_version, kind, demoting_strategy_id,
/// candidate_strategy_id, rationale, observed_at_seconds}`.
///
/// The version travels per LINE, not in a file header: the log is append-only
/// and `O_APPEND`, so no writer owns "the start of the file" and a header would
/// race. A line written before SRS-DATA-015 carries no `schema_version` key and
/// is read at [`MIN_SUPPORTED_TRIGGER_LOG_SCHEMA_VERSION`] — an existing log
/// stays readable exactly where it lies, with no rewrite.
const TRIGGER_LOG_SCHEMA_VERSION: u64 = 1;

/// The oldest trigger-event line layout this build still READS.
const MIN_SUPPORTED_TRIGGER_LOG_SCHEMA_VERSION: u64 = 1;

const USAGE: &str = "\
resv003_hot_swap_trigger_cli — SRS-RESV-003 Hot-Swap trigger configuration + logging

USAGE:
    resv003_hot_swap_trigger_cli <SUBCOMMAND> [FLAGS]

SUBCOMMANDS:
    config      Show the trigger configuration — the built-in default, or the
                DURABLE one at --state. With --set-* flags, change it durably.
    evaluate    Evaluate the automatic triggers against fixture inputs and print
                which fired + the logged records.
    manual      Fire the always-available manual promotion and print its log record.
    help        Print this help.

config FLAGS:
    --state <path>               read the DURABLE trigger configuration at <path>
                                 (no file => the all-disabled default; an
                                 unreadable file is an error, never a silent
                                 fallback to 'disabled'). Omit to print the
                                 built-in default without touching disk.
    --set-drawdown-threshold <bps>   ENABLE drawdown-demotion at <bps> (1..=10000), persist
    --set-no-drawdown                DISABLE drawdown-demotion, persist
    --set-top-ranked <on|off>        set top-ranked promotion, persist
    --set-highest-momentum <on|off>  set highest-momentum promotion, persist
                                 Every --set-* requires --state and is a
                                 read-modify-write: the other triggers are
                                 preserved, and the result is re-read from disk
                                 before it is reported.

evaluate FLAGS:
    --live <id>                  the current live strategy id (required)
    --live-drawdown <bps>        the live strategy's observed drawdown in bps (default 0)
    --rank <id>:<rank>:<score>:<momentum>   add one reservoir ranking row (repeatable)
    --eval-window <days>         evaluation window in days (default 30)
    --state <path>               take the config from the DURABLE store at <path>
                                 instead of the enable flags below (mutually
                                 exclusive with them — two sources of the same
                                 fact cannot both be authoritative)
    --drawdown-threshold <bps>   ENABLE drawdown-demotion at this threshold (1..=10000)
    --top-ranked                 ENABLE top-ranked promotion
    --highest-momentum           ENABLE highest-momentum promotion
    --log <path>                 durable JSONL audit log (write+flush+fsync). REQUIRED
                                 to log a fired trigger: if any trigger fires without
                                 a sink the pass fails closed (nonzero exit)
    --inject disabled            non-vacuity: ignore the enable flags and use the
                                 default (all-disabled) config, proving nothing fires

manual FLAGS:
    --demoting <id>              the current live strategy to demote (required)
    --candidate <id>             the reservoir strategy to promote (required)
    --log <path>                 durable JSONL audit log (REQUIRED — manual always
                                 fires; without a sink the command fails closed)
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

/// Show — and with `--set-*`, durably change — the trigger configuration.
///
/// With no `--state <path>` this reads nothing and prints the built-in default,
/// demonstrating the SYS-49a "automatic triggers shall default to disabled"
/// clause and that manual promotion is always available.
///
/// With `--state <path>` it reports the DURABLE configuration
/// ([`trigger_config_store`]), which is what makes "configurable" survive a
/// restart. The three read states stay distinct in the output: no file at all
/// reports `config-source:default` (nothing has been configured, so the default
/// genuinely applies), a readable file reports `config-source:persisted`, and an
/// unreadable one is an ERROR — never a quiet fallback to the default, which
/// would report an unknown configuration as a confident "disabled".
fn cmd_config(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }

    let mut state_path: Option<String> = None;
    let mut set_threshold: Option<u32> = None;
    let mut set_no_drawdown = false;
    let mut set_top_ranked: Option<bool> = None;
    let mut set_highest_momentum: Option<bool> = None;

    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--state" => {
                if state_path.is_some() {
                    return Err(dup(flag));
                }
                state_path = Some(take_value(&mut iter, flag)?);
            }
            "--set-drawdown-threshold" => {
                if set_threshold.is_some() {
                    return Err(dup(flag));
                }
                set_threshold = Some(parse_u32(&take_value(&mut iter, flag)?, flag)?);
            }
            "--set-no-drawdown" => {
                if set_no_drawdown {
                    return Err(dup(flag));
                }
                set_no_drawdown = true;
            }
            "--set-top-ranked" => {
                if set_top_ranked.is_some() {
                    return Err(dup(flag));
                }
                set_top_ranked = Some(parse_on_off(&take_value(&mut iter, flag)?, flag)?);
            }
            "--set-highest-momentum" => {
                if set_highest_momentum.is_some() {
                    return Err(dup(flag));
                }
                set_highest_momentum = Some(parse_on_off(&take_value(&mut iter, flag)?, flag)?);
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }

    if set_threshold.is_some() && set_no_drawdown {
        return Err(format!(
            "--set-drawdown-threshold and --set-no-drawdown contradict each other\n\n{USAGE}"
        ));
    }
    let mutating = set_threshold.is_some()
        || set_no_drawdown
        || set_top_ranked.is_some()
        || set_highest_momentum.is_some();

    let Some(path) = state_path.as_deref().map(Path::new) else {
        if mutating {
            return Err(format!(
                "--set-* requires --state <path>: there is nowhere to persist the change\n\n{USAGE}"
            ));
        }
        print_config(&HotSwapTriggerConfig::default(), None, "default", false);
        return Ok(());
    };

    // A `--set-*` is a read-modify-write over the WHOLE configuration, so two of
    // them running at once (two operator shells, or two threads of the REST
    // server) would both read the old state and both write a full replacement:
    // the second silently discards the first while both callers are told they
    // succeeded. Hold the exclusive guard across read+modify+write so that
    // cannot interleave. Reads take no lock — a save publishes by atomic rename,
    // so a reader sees the old or the new file, never a torn one.
    let _guard = if mutating {
        Some(
            trigger_config_store::ExclusiveGuard::acquire_creating(path)
                .map_err(|e| e.to_string())?,
        )
    } else {
        None
    };

    // Read first, ALWAYS — including on the write path. Starting from the default
    // when the existing file cannot be read would silently discard the operator's
    // other triggers (and could disarm one they believe is on). An unreadable file
    // fails the command instead.
    let persisted = trigger_config_store::load(path).map_err(|error| error.to_string())?;
    let mut config = persisted.unwrap_or_default();

    if let Some(bps) = set_threshold {
        let threshold = DrawdownThresholdBps::new(bps)
            .map_err(|error| format!("invalid --set-drawdown-threshold: {error}"))?;
        config.drawdown_demotion = DrawdownDemotionTrigger::Enabled { threshold };
    }
    if set_no_drawdown {
        config.drawdown_demotion = DrawdownDemotionTrigger::Disabled;
    }
    if let Some(enabled) = set_top_ranked {
        config.top_ranked_promotion = enable_flag(enabled);
    }
    if let Some(enabled) = set_highest_momentum {
        config.highest_momentum_promotion = enable_flag(enabled);
    }

    if mutating {
        trigger_config_store::save(path, &config).map_err(|error| error.to_string())?;
        // Read the configuration BACK from disk and report that, not the value
        // just held in memory: the operator's proof is what a later reader will
        // see, and a write that did not land must not print as if it had.
        let reread = trigger_config_store::load(path)
            .map_err(|error| format!("configuration written but unreadable afterwards: {error}"))?
            .ok_or_else(|| {
                format!(
                    "configuration written to {} but the file is absent on re-read",
                    path.display()
                )
            })?;
        if reread != config {
            return Err(format!(
                "configuration written to {} does not read back as written",
                path.display()
            ));
        }
        print_config(&reread, Some(path), "persisted", true);
        return Ok(());
    }

    let source = if persisted.is_some() {
        "persisted"
    } else {
        "default"
    };
    print_config(&config, Some(path), source, false);
    Ok(())
}

/// The `config` subcommand's deterministic proof lines.
///
/// `default-disabled` reports a CONSTANT fact — that
/// `HotSwapTriggerConfig::default()` has every automatic trigger off — because
/// that is the SYS-49a clause it exists to prove. It deliberately does not track
/// the displayed configuration: once a configuration can be persisted, an
/// operator who enabled a trigger must not see the line that proves the default
/// posture flip to `false` and read it as "the default changed".
/// `any-automatic-enabled` is the one that describes what is displayed.
fn print_config(
    config: &HotSwapTriggerConfig,
    path: Option<&Path>,
    source: &str,
    persisted_now: bool,
) {
    if let Some(path) = path {
        println!("config-path:{}", path.display());
    }
    println!("config-source:{source}");
    println!("manual-promotion-available:true");
    println!(
        "drawdown-demotion-enabled:{}",
        config.drawdown_demotion.is_enabled()
    );
    if let Some(threshold) = config.drawdown_demotion.threshold() {
        println!("drawdown-demotion-threshold-bps:{}", threshold.get());
    }
    println!(
        "top-ranked-promotion-enabled:{}",
        config.top_ranked_promotion.is_enabled()
    );
    println!(
        "highest-momentum-promotion-enabled:{}",
        config.highest_momentum_promotion.is_enabled()
    );
    println!("any-automatic-enabled:{}", config.any_automatic_enabled());
    println!(
        "default-disabled:{}",
        !HotSwapTriggerConfig::default().any_automatic_enabled()
    );
    println!("config-persisted:{persisted_now}");
}

/// Parse an explicit `on`/`off` value. A flag that decides whether an automatic
/// swap may fire takes an explicit word, never a bare presence that could be
/// mistyped into the opposite meaning.
fn parse_on_off(value: &str, flag: &str) -> Result<bool, String> {
    match value {
        "on" => Ok(true),
        "off" => Ok(false),
        other => Err(format!(
            "{flag} expects 'on' or 'off' (got '{other}')\n\n{USAGE}"
        )),
    }
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
    let mut state_path: Option<String> = None;
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
            "--state" => {
                if state_path.is_some() {
                    return Err(dup(flag));
                }
                state_path = Some(take_value(&mut iter, flag)?);
            }
            "--rank" => {
                ranked.push(parse_rank_row(&take_value(&mut iter, flag)?)?);
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }

    let live_id = live_id.ok_or_else(|| format!("--live <id> is required\n\n{USAGE}"))?;

    // Two authoritative sources for one fact is a bug waiting to be argued about
    // in an incident review: refuse the combination rather than silently ranking
    // one above the other.
    if state_path.is_some() && (drawdown_threshold.is_some() || top_ranked || highest_momentum) {
        return Err(format!(
            "--state and the inline enable flags (--drawdown-threshold / --top-ranked / \
             --highest-momentum) are mutually exclusive\n\n{USAGE}"
        ));
    }

    // Build the config — unless `--inject disabled` forces the default
    // (all-automatic-disabled) posture to prove it fires nothing.
    //
    // `--inject disabled` deliberately outranks `--state`: it is the non-vacuity
    // control, and it must be able to prove that a config which WOULD fire fires
    // nothing when the default posture is restored.
    let config = if inject_disabled {
        HotSwapTriggerConfig::default()
    } else if let Some(path) = state_path.as_deref().map(Path::new) {
        // The durable configuration drives the real decision, not just the
        // display: an unreadable one must stop the evaluation, because a pass
        // that silently fell back to the default would report "nothing fired"
        // about a configuration nobody could read.
        trigger_config_store::load(path)
            .map_err(|error| error.to_string())?
            .unwrap_or_default()
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
            strategy_id: parse_strategy_id(&live_id, "--live")?,
            drawdown_bps: live_drawdown_bps,
        }),
    };
    let ranking = FixedRanking {
        snapshot: ReservoirRankingSnapshot {
            evaluation_window_days: eval_window_days,
            ranked,
        },
    };
    // Same append-then-count hazard as the manual path, and worse here: one pass can
    // append several records, so a concurrent writer interleaving would make the
    // reported log-file-records describe a mixture of two passes.
    let _guard = match &log_path {
        Some(path) => {
            trigger_config_store::ExclusiveGuard::acquire_if_parent_exists(Path::new(path))
                .map_err(|error| error.to_string())?
        }
        None => None,
    };

    // Same ordering rule as the manual path: validate the existing log before appending to
    // it, so an unreadable log refuses the pass rather than being discovered afterwards.
    let pre_count = match &log_path {
        Some(path) => count_log_records(Path::new(path))?,
        None => 0,
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
    // A fired trigger whose required audit-log record was REJECTED is surfaced
    // here (with the sink's reason) and is never `selected` (fail closed).
    for unlogged in &evaluation.unlogged {
        println!(
            "unlogged:{} candidate:{} reason:{}",
            unlogged.proposal.kind.as_str(),
            unlogged.proposal.candidate_strategy_id.as_str(),
            unlogged.rejection_reason,
        );
    }
    for reason in &evaluation.degraded_inputs {
        println!("degraded-input:{reason}");
    }
    let logged = evaluation.fired.len() - evaluation.unlogged.len();
    println!("fired-count:{}", evaluation.fired.len());
    println!("logged-count:{logged}");
    println!("unlogged-count:{}", evaluation.unlogged.len());
    println!("degraded-count:{}", evaluation.degraded_inputs.len());
    // "all swap triggers are logged": every fired trigger's record was accepted.
    println!("all-triggers-logged:{}", evaluation.unlogged.is_empty());
    println!(
        "selected:{}",
        evaluation
            .selected
            .as_ref()
            .map(|proposal| proposal.kind.as_str())
            .unwrap_or("NONE"),
    );

    if let Some(path) = &log_path {
        // Derived from the pre-count plus what this pass wrote, not from a fresh read: the
        // Derived from the pre-count plus what this pass wrote, not from a fresh read: the
        // guard is still held, so this is exact — and it cannot fail after the bytes are
        // already durable.
        let persisted = pre_count + logged;
        println!("log-persisted:{path}");
        println!("log-file-records:{persisted}");
    }

    // Fail closed at the PROCESS level: if any fired trigger's record was rejected
    // (`unlogged`) or an input port was degraded, exit nonzero so shell automation
    // treats the pass as a failure rather than a clean evaluation.
    if !evaluation.unlogged.is_empty() {
        let reasons: Vec<String> = evaluation
            .unlogged
            .iter()
            .map(|unlogged| {
                format!(
                    "{}: {}",
                    unlogged.proposal.kind.as_str(),
                    unlogged.rejection_reason
                )
            })
            .collect();
        return Err(format!(
            "{} fired trigger(s) could not be logged — the evaluation pass is not \
             actionable (fail closed): {}",
            evaluation.unlogged.len(),
            reasons.join("; ")
        ));
    }
    if !evaluation.degraded_inputs.is_empty() {
        return Err(format!(
            "degraded input port(s): {} (fail closed)",
            evaluation.degraded_inputs.join("; ")
        ));
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
    // Validated BEFORE the guard and the append: a record this build's own reader would
    // refuse must never reach the log.
    let demoting_id = parse_strategy_id(&demoting, "--demoting")?;
    let candidate_id = parse_strategy_id(&candidate, "--candidate")?;

    // The append is atomic on its own (`O_APPEND`), but the ordinal reported below
    // is the log's record COUNT taken afterwards — a separate read. A concurrent
    // fire landing between the two would make this invocation report an ordinal
    // that addresses somebody else's record, and that ordinal is the identity the
    // REST surface hands back for the trigger. Holding the guard across
    // append+count is what makes it address the record this call actually wrote.
    let _guard = match &log_path {
        Some(path) => {
            trigger_config_store::ExclusiveGuard::acquire_if_parent_exists(Path::new(path))
                .map_err(|error| error.to_string())?
        }
        None => None,
    };

    // Read the existing log BEFORE firing. Counting afterwards meant a malformed prior line
    // failed the command *after* the new record had been appended and fsynced: the caller
    // was told the trigger did not fire while the durable log said it did — the exact
    // audit/actionability contradiction this layer exists to prevent. Every fallible read
    // now happens ahead of the append, under the same guard, so a log this build cannot
    // read refuses the fire instead of joining it.
    let pre_count = match &log_path {
        Some(path) => count_log_records(Path::new(path))?,
        None => 0,
    };

    let log = CollectingTriggerLog::new(log_path.as_deref().map(PathBuf::from));
    let outcome = StrategyOrchestrator.request_manual_promotion(
        demoting_id,
        candidate_id,
        &log,
        OBSERVED_AT_SECONDS,
    );

    println!("manual-always-available:true");
    // A refused request never proposed anything, so there is no `fired:` line to print for
    // it — printing one would report a trigger the domain layer declined to create.
    if let Err(ManualPromotionError::SameStrategy { .. }) = &outcome {
        println!("manual-refused:SAME_STRATEGY");
        println!("manual-logged:false");
        return Err(outcome.unwrap_err().to_string());
    }
    // Ok = fired AND logged (safe to hand to the RESV-004 gate); Err = fired but
    // the required audit-log record was rejected (fail closed — not actionable).
    let (proposal, logged) = match &outcome {
        Ok(proposal) => (proposal, true),
        Err(ManualPromotionError::Unlogged(unlogged)) => (&unlogged.proposal, false),
        Err(ManualPromotionError::SameStrategy { .. }) => unreachable!("handled above"),
    };
    println!(
        "fired:{} demoting:{} candidate:{} rationale:{}",
        proposal.kind.as_str(),
        proposal.demoting_strategy_id.as_str(),
        proposal.candidate_strategy_id.as_str(),
        rationale_to_string(&proposal.rationale),
    );
    println!("manual-logged:{logged}");

    if let Some(path) = &log_path {
        // Derived from the pre-count plus what this pass wrote, not from a fresh read: the
        // guard is still held and exactly one record was appended, so this is exact — and
        // it cannot fail after the bytes are already durable.
        let persisted = pre_count + usize::from(logged);
        println!("log-persisted:{path}");
        println!("log-file-records:{persisted}");
        // The 1-based position of the record THIS invocation appended, which is
        // the durable-log count after an append of exactly one record. It is the
        // only identity a fired manual trigger has: a caller can go to that
        // ordinal and read back the very record it caused, so a surface can bind
        // "the trigger fired" to a durable artefact rather than to an exit code.
        // The append and this count are both inside the exclusive guard taken
        // above, so no concurrent fire can land between them and leave this
        // ordinal addressing somebody else's record.
        if logged {
            println!("trigger-record-ordinal:{persisted}");
        }
    }

    // Fail closed at the PROCESS level: a rejected audit-log record must make the
    // command exit nonzero so shell automation cannot treat an unlogged manual
    // Hot-Swap trigger as a successful command.
    match outcome {
        Ok(_) => Ok(()),
        Err(error) => Err(error.to_string()),
    }
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
/// Validate a strategy id before it can reach a durable trigger record.
///
/// The reader this binary ships with ([`validate_trigger_log_line`]) refuses a record whose
/// strategy ids are empty or blank. Writing one anyway would poison the log with a line
/// this very build cannot read back — every later count or fire against that log then fails
/// on a record we wrote ourselves, while the fire that produced it reported success.
///
/// Only emptiness is refused here, deliberately. An id carrying an interior control
/// character (a newline, a quote, a backslash) round-trips correctly — the writer escapes it
/// and `resv_3_control_characters_in_an_id_do_not_poison_the_log` pins that — so rejecting
/// it would remove a working guarantee rather than add one. The stricter no-whitespace rule
/// belongs to the REST arm, which is the layer that correlates a response back to its record
/// through the space-delimited `fired:` line and therefore needs ids to survive that trip.
fn parse_strategy_id(value: &str, flag: &str) -> Result<StrategyId, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(format!(
            "{flag} must name a strategy (got {value:?}); a blank id would write an audit \
             record this build's own reader refuses\n\n{USAGE}"
        ));
    }
    Ok(StrategyId::new(trimmed))
}

fn parse_rank_row(spec: &str) -> Result<RankedStrategy, String> {
    let parts: Vec<&str> = spec.split(':').collect();
    if parts.len() != 4 {
        return Err(format!(
            "--rank expects '<id>:<rank>:<score>:<momentum>' (got '{spec}')\n\n{USAGE}"
        ));
    }
    let strategy_id = parse_strategy_id(parts[0], "--rank id")?;
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
        strategy_id,
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

/// Escape a string for a JSON value, covering **every** C0 control character.
///
/// The named escapes alone are not enough. `StrategyId::new` accepts arbitrary text, so an operator
/// id carrying a backspace or form feed would previously be written raw — producing a line that this
/// build's own reader (which refuses raw control characters inside a JSON string, as JSON requires)
/// then rejects as unreadable. That is a poisoned audit log: a record written by this build that
/// this build cannot read, which is precisely what SRS-DATA-015 exists to prevent. Anything below
/// U+0020 without a named escape is emitted as `\u00XX`.
fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            control if control < '\u{20}' => {
                out.push_str(&format!("\\u{:04x}", control as u32));
            }
            other => out.push(other),
        }
    }
    out
}

fn event_to_json(event: &HotSwapTriggerEvent) -> String {
    format!(
        "{{\"schema_version\":{},\"kind\":\"{}\",\"demoting_strategy_id\":\"{}\",\"candidate_strategy_id\":\"{}\",\"rationale\":\"{}\",\"observed_at_seconds\":{}}}",
        TRIGGER_LOG_SCHEMA_VERSION,
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
    // Whether this append CREATES the file decides whether the directory entry also has to
    // be made durable: fsyncing the file's contents does not persist the entry that names
    // it, so a crash right after the first fire could erase a log the caller was already
    // told holds its record (`logged:true` plus an ordinal addressing it).
    let newly_created = !path.exists();
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
        .map_err(|error| format!("cannot append to log file {}: {error}", path.display()))?;
    if newly_created {
        let dir = match path.parent() {
            Some(parent) if !parent.as_os_str().is_empty() => parent.to_path_buf(),
            _ => std::path::PathBuf::from("."),
        };
        let handle = std::fs::File::open(&dir)
            .map_err(|error| format!("cannot open log directory {}: {error}", dir.display()))?;
        handle
            .sync_all()
            .map_err(|error| format!("cannot sync log directory {}: {error}", dir.display()))?;
    }
    Ok(())
}

fn count_log_records(path: &Path) -> Result<usize, String> {
    let content = match std::fs::read_to_string(path) {
        Ok(content) => content,
        // A missing file means zero records were persisted (e.g. every append was
        // rejected) — that is a 0 count, not a read error. Returning it lets the
        // caller's fail-closed check surface the real rejection reason instead.
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(format!("cannot read log file {}: {error}", path.display())),
    };
    let mut counted = 0usize;
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        // SRS-DATA-015: a line this build cannot parse must not be counted as
        // proof that the trigger was logged. The count is the evidence the
        // caller's fail-closed check reads, so counting an unreadable line
        // would manufacture that evidence. A supported schema version is
        // necessary but NOT sufficient — a record this build cannot reconstruct
        // a trigger event from is not evidence of a trigger either.
        validate_trigger_log_line(line).map_err(|reason| {
            format!(
                "log file {} holds a line this build cannot read ({reason})",
                path.display()
            )
        })?;
        counted += 1;
    }
    Ok(counted)
}

/// Every trigger kind, so the wire-tag allow-list below is DERIVED from the enum rather than
/// duplicated as literals that could drift from it.
const ALL_TRIGGER_KINDS: [HotSwapTriggerKind; 4] = [
    HotSwapTriggerKind::ManualPromotion,
    HotSwapTriggerKind::DrawdownDemotion,
    HotSwapTriggerKind::TopRankedPromotion,
    HotSwapTriggerKind::HighestMomentumPromotion,
];

/// Accept a persisted trigger-log line ONLY if this build could reconstruct the trigger event it
/// claims to record.
///
/// A supported schema version says the reader understands the record's *layout*; it says nothing
/// about whether the record's fields are actually there. `{"schema_version":1}` declares a layout
/// this build knows and carries no trigger at all — counting it would let an empty object stand in
/// for durable proof that a Hot-Swap trigger was logged, which is the claim
/// [`count_log_records`] backs. So the whole v1 record shape is required: the four value fields the
/// writer emits, with their types and the `kind` enum checked.
///
/// Legacy (pre-SRS-DATA-015) lines carry the same fields minus `schema_version`, so the body
/// requirements are identical for both — only the version field is optional.
fn validate_trigger_log_line(line: &str) -> Result<(), String> {
    // Layout first: an unsupported or malformed version means the field meanings below are not
    // this build's to assume.
    let _version = line_schema_version(line)?;

    let field = |key: &str| -> Result<&str, String> {
        top_level_json_field(line, key)
            .map_err(|_| "line is not a well-formed JSON object".to_string())?
            .ok_or_else(|| format!("record is missing required field '{key}'"))
    };
    let non_empty_string = |key: &str| -> Result<(), String> {
        let raw = field(key)?;
        let value =
            json_string_value(raw).ok_or_else(|| format!("field '{key}' is not a JSON string"))?;
        if value.trim().is_empty() {
            return Err(format!("field '{key}' is empty"));
        }
        Ok(())
    };

    let kind_raw = field("kind")?;
    let kind = json_string_value(kind_raw).ok_or("field 'kind' is not a JSON string")?;
    if !ALL_TRIGGER_KINDS.iter().any(|known| known.as_str() == kind) {
        return Err(format!("field 'kind' has unknown trigger kind {kind:?}"));
    }

    non_empty_string("demoting_strategy_id")?;
    non_empty_string("candidate_strategy_id")?;
    non_empty_string("rationale")?;

    let observed_raw = field("observed_at_seconds")?;
    let observed = parse_strict_i64(observed_raw)
        .ok_or_else(|| "field 'observed_at_seconds' is not a JSON integer".to_string())?;
    if observed < 0 {
        return Err("field 'observed_at_seconds' is negative".to_string());
    }
    Ok(())
}

/// The schema version a persisted trigger-event line declares.
///
/// Only a line that genuinely carries NO `schema_version` key reads as legacy
/// ([`MIN_SUPPORTED_TRIGGER_LOG_SCHEMA_VERSION`]). A key that is present but
/// unparseable — a float, a quoted number, a version outside the supported range
/// — is an ERROR, never quietly downgraded to "legacy": that downgrade would let
/// a record written by a newer build be counted as valid audit evidence by a
/// reader that cannot actually parse its fields.
///
/// The object is scanned STRUCTURALLY rather than by a leading-prefix match, so
/// the key is found wherever a writer placed it, while a `"schema_version"`
/// occurring inside the operator-supplied `rationale` string can never spoof one.
fn line_schema_version(line: &str) -> Result<u64, String> {
    let raw = match top_level_json_field(line, "schema_version") {
        Err(_) => return Err("line is not a well-formed JSON object".to_string()),
        Ok(None) => return Ok(MIN_SUPPORTED_TRIGGER_LOG_SCHEMA_VERSION),
        Ok(Some(raw)) => raw,
    };
    let parsed = parse_strict_i64(raw)
        .ok_or_else(|| format!("schema_version {raw} is not a JSON integer"))?;
    let version =
        u64::try_from(parsed).map_err(|_| format!("schema_version {parsed} is negative"))?;
    if !(MIN_SUPPORTED_TRIGGER_LOG_SCHEMA_VERSION..=TRIGGER_LOG_SCHEMA_VERSION).contains(&version) {
        return Err(format!(
            "schema_version {version} is outside the supported range \
             [{MIN_SUPPORTED_TRIGGER_LOG_SCHEMA_VERSION}, {TRIGGER_LOG_SCHEMA_VERSION}]"
        ));
    }
    Ok(version)
}

// --------------------------------------------------------------------------- //
// Concrete demonstration ports
// --------------------------------------------------------------------------- //

struct FixedLiveProbe {
    state: Option<LiveStrategyState>,
}

impl LiveStrategyProbe for FixedLiveProbe {
    fn current_live(&self) -> Result<Option<LiveStrategyState>, HotSwapSideEffectError> {
        Ok(self.state.clone())
    }
}

struct FixedRanking {
    snapshot: ReservoirRankingSnapshot,
}

impl ReservoirRankingSource for FixedRanking {
    fn snapshot(&self) -> Result<ReservoirRankingSnapshot, HotSwapSideEffectError> {
        Ok(self.snapshot.clone())
    }
}

/// The CLI's concrete swap-trigger log sink. When a `--log` path is given it
/// durably appends each fired trigger to a JSONL file (write + flush + fsync);
/// a file-write failure is surfaced as `Err` (a REJECTED record → the trigger is
/// kept out of the actionable path, fail closed).
///
/// With NO `--log` path there is no real audit destination, so a record is
/// REJECTED (`Err`) rather than silently accepted — a firing CLI command without
/// a sink must not claim a trigger was logged when nothing was persisted. A
/// concrete deployment wires the deferred SRS-LOG-001 durable store here; until
/// then `--log` is required to log (and therefore to act on) a fired trigger.
struct CollectingTriggerLog {
    sink_path: Option<PathBuf>,
}

impl CollectingTriggerLog {
    fn new(sink_path: Option<PathBuf>) -> Self {
        Self { sink_path }
    }
}

impl HotSwapTriggerLog for CollectingTriggerLog {
    fn record(&self, event: HotSwapTriggerEvent) -> Result<(), HotSwapSideEffectError> {
        match &self.sink_path {
            Some(path) => append_event_line(path, &event).map_err(HotSwapSideEffectError::new),
            None => Err(HotSwapSideEffectError::new(
                "no audit-log sink configured — pass --log <path> to log a fired trigger",
            )),
        }
    }
}
