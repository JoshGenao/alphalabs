//! SRS-RESV-006 / SyRS SYS-49e operator CLI — the Hot-Swap cool-down window.
//!
//! Three operator concerns: read the window (`status`), set how long it lasts
//! (`configure`), and record a swap completion that STARTS it (`record-completion`).
//!
//! **Separate from `resv003_hot_swap_trigger_cli` deliberately.** That tool decides which
//! triggers fire; this one can open the window that suppresses them. Folding the two
//! together would give the trigger tool the power to silence itself, and it would put a
//! second writer on a second persisted entity behind one operator surface. The gate itself
//! still lives on RESV-003's entry points — this tool never evaluates a trigger.
//!
//! **The production writer is deferred.** `record-completion` exists so the write path is
//! real, durable and testable today, but the caller that SHOULD invoke it is SRS-RESV-005's
//! promotion — the only code that can observe a swap actually completing. SRS-RESV-004's
//! demotion can finish with no promotion at all (the SYS-49b demotion-pending timeout);
//! that is a failed changeover, not a swap, and must never start a window. Every
//! `record-completion` run says so on its own proof stream (`deferred-writer:SRS-RESV-005`).
//!
//! Emits deterministic `key:value` proof lines (repo convention) and fails closed on
//! unknown / duplicate / valueless flags.

use atp_orchestrator::cooldown::{CooldownPeriodDays, SwapCompletion, COOLDOWN_DAYS_DEFAULT};
use atp_orchestrator::cooldown_store::{self, CompletionOutcome};
use atp_types::StrategyId;
use std::env;
use std::path::Path;
use std::process::ExitCode;

const USAGE: &str = "\
resv006_hot_swap_cooldown_cli — SRS-RESV-006 / SYS-49e Hot-Swap cool-down window

USAGE:
    resv006_hot_swap_cooldown_cli <SUBCOMMAND> [FLAGS]

SUBCOMMANDS:
    status              Report the window at --state as of now (or --now).
    configure           Set the cool-down period durably (--set-days).
    record-completion   Record a swap completion, STARTING the window at its
                        timestamp. The production caller is SRS-RESV-005.
    help                Print this help.

status FLAGS:
    --state <path>      the durable window (REQUIRED). An absent file means no swap has
                        ever completed; an UNREADABLE one is UNKNOWN, never 'no cool-down'
    --now <epoch-secs>  override the real clock (proof runs)

configure FLAGS:
    --state <path>      the durable window (REQUIRED)
    --set-days <n>      1..=365. 0 is REFUSED: a zero-length window would silently defeat
                        SYS-49e. To stop automatic swapping, disable the triggers with
                        resv003_hot_swap_trigger_cli config --set-* instead

record-completion FLAGS:
    --state <path>      the durable window (REQUIRED)
    --demoted <id>      the strategy that went to paper (REQUIRED)
    --promoted <id>     the strategy that went live (REQUIRED)
    --completed-at <s>  the completion instant in epoch seconds. SYS-49e starts the
                        window HERE, not at the write. Defaults to the real clock
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("resv006_hot_swap_cooldown_cli: {err}");
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
        "status" => cmd_status(rest),
        "configure" => cmd_configure(rest),
        "record-completion" => cmd_record_completion(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

/// Read the real clock, refusing a pre-epoch one.
///
/// NOT `unwrap_or(0)`: a zero would place every completion far in the past, so every window
/// would read as long expired and SYS-49e would be silently defeated by a broken clock. A
/// clock this build cannot trust refuses the time-windowed decision instead.
fn wall_clock_seconds() -> Result<u64, String> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|since| since.as_secs())
        .map_err(|_| {
            "system clock reports a time before the Unix epoch; refusing to read or write a \
             Hot-Swap cool-down window against it"
                .to_string()
        })
}

fn cmd_status(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let mut state_path: Option<String> = None;
    let mut now_override: Option<u64> = None;

    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--state" => {
                if state_path.is_some() {
                    return Err(dup(flag));
                }
                state_path = Some(take_value(&mut iter, flag)?);
            }
            "--now" => {
                if now_override.is_some() {
                    return Err(dup(flag));
                }
                now_override = Some(parse_u64(&take_value(&mut iter, flag)?, flag)?);
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    let state_path = state_path.ok_or_else(|| format!("--state <path> is required\n\n{USAGE}"))?;
    let now = match now_override {
        Some(now) => now,
        None => wall_clock_seconds()?,
    };

    let state = cooldown_store::resolve(Some(Path::new(&state_path)), now);
    println!("observed-at-seconds:{now}");
    println!("cooldown-state:{}", state.as_str());
    // The same predicate the gate uses, not a second opinion about it.
    println!("cooldown-in-effect:{}", !state.proven_clear());
    if let Some(started) = state.started_at_seconds() {
        println!("cooldown-started-at-seconds:{started}");
    }
    if let Some(expires) = state.expires_at_seconds() {
        println!("cooldown-expires-at-seconds:{expires}");
    }
    println!("cooldown-days-default:{COOLDOWN_DAYS_DEFAULT}");
    // Adversarial review r13. The two-phase write introduced a THIRD durable
    // condition — a window opened before the swap and never confirmed — and a window
    // an operator cannot distinguish from a completed one is a window they cannot
    // resolve. `unknown` when the store could not be read, never `false`: "this swap
    // completed" is a claim, and an unreadable store supports no claims.
    println!(
        "cooldown-completion-provisional:{}",
        match cooldown_store::completion_is_provisional(Path::new(&state_path)) {
            Some(true) => "true",
            Some(false) => "false",
            None => "unknown",
        }
    );

    // An UNKNOWN window is a failed read, and a shell wrapper must not be able to treat it
    // as an answer — the whole point of the state existing.
    if let Some(reason) = state.degraded_reason() {
        println!("cooldown-unreadable-reason:{}", one_line(reason));
        return Err(format!("cool-down window is not readable: {reason}"));
    }
    Ok(())
}

fn cmd_configure(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let mut state_path: Option<String> = None;
    let mut set_days: Option<u32> = None;

    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--state" => {
                if state_path.is_some() {
                    return Err(dup(flag));
                }
                state_path = Some(take_value(&mut iter, flag)?);
            }
            "--set-days" => {
                if set_days.is_some() {
                    return Err(dup(flag));
                }
                set_days = Some(parse_u32(&take_value(&mut iter, flag)?, flag)?);
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    let state_path = state_path.ok_or_else(|| format!("--state <path> is required\n\n{USAGE}"))?;
    let days = set_days.ok_or_else(|| format!("--set-days <n> is required\n\n{USAGE}"))?;
    // Validated BEFORE the durable write: a period this build's own reader would refuse
    // must never be published over a good window (durable-writes rule 4).
    let period = CooldownPeriodDays::new(days).map_err(|error| error.to_string())?;

    let record = cooldown_store::set_period(Path::new(&state_path), period)
        .map_err(|error| error.to_string())?;
    println!("cooldown-days:{}", record.period.get());
    println!("completion-preserved:{}", record.last_completion.is_some());

    // Re-read from disk rather than echoing what we just held in memory: the proof line an
    // operator acts on should describe the bytes that landed, not the intent that produced
    // them.
    let reread = cooldown_store::load(Path::new(&state_path))
        .map_err(|error| error.to_string())?
        .ok_or_else(|| "cool-down window vanished immediately after it was written".to_string())?;
    println!("reread-cooldown-days:{}", reread.period.get());
    if reread != record {
        return Err("the re-read cool-down window does not match what was written".to_string());
    }
    Ok(())
}

fn cmd_record_completion(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let mut state_path: Option<String> = None;
    let mut demoted: Option<String> = None;
    let mut promoted: Option<String> = None;
    let mut completed_at: Option<u64> = None;

    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--state" => {
                if state_path.is_some() {
                    return Err(dup(flag));
                }
                state_path = Some(take_value(&mut iter, flag)?);
            }
            "--demoted" => {
                if demoted.is_some() {
                    return Err(dup(flag));
                }
                demoted = Some(take_value(&mut iter, flag)?);
            }
            "--promoted" => {
                if promoted.is_some() {
                    return Err(dup(flag));
                }
                promoted = Some(take_value(&mut iter, flag)?);
            }
            "--completed-at" => {
                if completed_at.is_some() {
                    return Err(dup(flag));
                }
                completed_at = Some(parse_u64(&take_value(&mut iter, flag)?, flag)?);
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    let state_path = state_path.ok_or_else(|| format!("--state <path> is required\n\n{USAGE}"))?;
    let demoted = demoted.ok_or_else(|| format!("--demoted <id> is required\n\n{USAGE}"))?;
    let promoted = promoted.ok_or_else(|| format!("--promoted <id> is required\n\n{USAGE}"))?;

    // EVERY value this command will print or persist is validated before it mutates
    // anything (honest-surfaces rule 38): a control character reaching the proof stream
    // after the window had already moved would make the operator read a failure while the
    // disk read a started cool-down.
    let demoted_id = parse_strategy_id(&demoted, "--demoted")?;
    let promoted_id = parse_strategy_id(&promoted, "--promoted")?;
    let completed_at_seconds = match completed_at {
        Some(seconds) => seconds,
        None => wall_clock_seconds()?,
    };

    let completion = SwapCompletion {
        completed_at_seconds,
        demoted_strategy_id: demoted_id,
        promoted_strategy_id: promoted_id,
    };
    let outcome = cooldown_store::record_completion(Path::new(&state_path), &completion).map_err(
        |error| {
            // safety-paths rule 41: a failed durable write leaves NO window, so the swap
            // happened and nothing suppresses. A process that is about to exit cannot hold
            // a fail-closed state in memory, so it says so in as many words instead — and
            // the durable close belongs to SRS-RESV-005, which can refuse to report the
            // swap complete until the window is.
            println!("completion-recorded:false");
            println!("cooldown-window-started:false");
            format!(
                "{error}\n\
                 THE COOL-DOWN WINDOW DID NOT START. The swap completion was not persisted, \
                 so SYS-49e is NOT suppressing automatic triggers. Repair the state file and \
                 re-run, or disable the automatic triggers with resv003_hot_swap_trigger_cli \
                 until it is recorded."
            )
        },
    )?;

    println!("deferred-writer:SRS-RESV-005");
    match &outcome {
        CompletionOutcome::Recorded { previous } => {
            // `cooldown-window-started` is deliberately NOT printed here. The write
            // returning Ok is not the window being readable, and the re-read below can
            // still contradict it — adversarial review r21 found both lines reaching
            // the same stdout, so the proof stream asserted the window had and had not
            // started. This repo's own `parse_trigger_cli_output` refuses contradictory
            // duplicate keys, and this is the command the promote CLI's repair
            // instruction sends operators to. It is emitted ONCE, after the re-read.
            println!("completion-recorded:true");
            println!("completion-at-seconds:{completed_at_seconds}");
            match previous {
                Some(previous) => println!(
                    "previous-completion-at-seconds:{}",
                    previous.completed_at_seconds
                ),
                None => println!("previous-completion:none"),
            }
        }
        CompletionOutcome::KeptNewer { stored, offered } => {
            // Not a write failure — the swap really happened — but the only way to reach
            // here is a clock that disagrees with recorded history, and an operator must
            // not learn that from a zero exit.
            println!("completion-recorded:false");
            println!("cooldown-window-started:false");
            println!(
                "kept-stored-completion-at-seconds:{}",
                stored.completed_at_seconds
            );
            println!(
                "offered-completion-at-seconds:{}",
                offered.completed_at_seconds
            );
            return Err(format!(
                "a completion at {}s is OLDER than the recorded {}s; the stored window was \
                 KEPT (a cool-down only moves forward). The system clock disagrees with \
                 recorded swap history — resolve that before trusting either.",
                offered.completed_at_seconds, stored.completed_at_seconds,
            ));
        }
    }

    // Re-read the published window so the operator sees the durable result, not the intent.
    let state = cooldown_store::resolve(Some(Path::new(&state_path)), completed_at_seconds);
    println!("reread-cooldown-state:{}", state.as_str());
    if let Some(expires) = state.expires_at_seconds() {
        println!("reread-cooldown-expires-at-seconds:{expires}");
    }
    // "The write returned Ok" is not "the window is readable". If the bytes that landed
    // cannot be classified, the swap happened and NOTHING suppresses — the same fail-open
    // as a failed write, reached by a different route, so it gets the same non-zero exit
    // and the same explicit sentence. Verifying the published artefact rather than the
    // intent is CLAUDE.md rule 5.
    if let Some(reason) = state.degraded_reason() {
        println!("cooldown-window-started:false");
        return Err(format!(
            "the completion was written but the window it produced is NOT readable: \
             {reason}\n\
             THE COOL-DOWN IS NOT IN EFFECT. Repair the state file and re-run, or disable \
             the automatic triggers with resv003_hot_swap_trigger_cli until it is."
        ));
    }
    // The single emission, and it is a claim about the VERIFIED window: the completion
    // was written and the bytes that landed classify.
    println!("cooldown-window-started:true");
    Ok(())
}

fn parse_strategy_id(value: &str, flag: &str) -> Result<StrategyId, String> {
    if value.is_empty() {
        return Err(format!("{flag} must not be empty\n\n{USAGE}"));
    }
    if value.chars().any(|c| c.is_control()) {
        return Err(format!(
            "{flag} must not contain control characters\n\n{USAGE}"
        ));
    }
    Ok(StrategyId::new(value))
}

/// Flatten a message onto ONE proof line: the stream is line-oriented `key:value`, so a
/// value carrying a newline would silently become a bogus extra key.
fn one_line(value: &str) -> String {
    value
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn wants_help(rest: &[String]) -> bool {
    rest.iter()
        .any(|arg| arg == "help" || arg == "--help" || arg == "-h")
}

fn dup(flag: &str) -> String {
    format!("{flag} given more than once\n\n{USAGE}")
}

fn parse_u32(value: &str, flag: &str) -> Result<u32, String> {
    value
        .parse()
        .map_err(|_| format!("{flag} expects a u32 (got '{value}')\n\n{USAGE}"))
}

fn parse_u64(value: &str, flag: &str) -> Result<u64, String> {
    value
        .parse()
        .map_err(|_| format!("{flag} expects a u64 (got '{value}')\n\n{USAGE}"))
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .cloned()
        .ok_or_else(|| format!("{flag} expects a value\n\n{USAGE}"))
}
