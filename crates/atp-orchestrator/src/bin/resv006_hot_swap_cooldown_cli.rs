//! SRS-RESV-006 / SyRS SYS-49e operator CLI — the Hot-Swap cool-down window.
//!
//! Four operator concerns: read the window (`status`), set how long it lasts
//! (`configure`), record a swap completion that STARTS it (`record-completion`), and
//! clear an unconfirmed marker an interrupted swap left behind (`clear-provisional`).
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
    clear-provisional   Clear an UNCONFIRMED marker left by an interrupted swap
                        that did NOT complete. Never touches a confirmed window.
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

clear-provisional FLAGS:
    --state <path>      the durable window (REQUIRED)
    --demoted <id>      the interrupted swap's demoted strategy (REQUIRED)
    --promoted <id>     the interrupted swap's promoted strategy (REQUIRED)
    --at <epoch-secs>   the marker's instant, as `status` reported it (REQUIRED)
    --attempt-id <id>   the marker's attempt id, as `status` reported it (REQUIRED).
                        The pair and the instant are not an identity — a retry in the
                        same second leaves all three unchanged — so this is the value
                        that makes the clear a true compare-and-swap against your read
    --confirm           REQUIRED. Clearing a marker retires suppression that may be
                        protecting a strategy that went live before the interruption,
                        so it is an explicit operator act (SyRS SYS-2d / NFR-S2)

    Use this ONLY when you have established the swap did not complete — that the
    promoted strategy is not live. If it DID complete, use record-completion
    instead: that confirms the window rather than retiring it.
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
        "clear-provisional" => cmd_clear_provisional(rest),
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
    // The marker's OWN instant, which `clear-provisional --at` requires (r25). Printed
    // here so an operator reconciles what they read rather than whatever is there when
    // they get around to typing: the clear is a compare-and-swap against this value, and
    // a retry of the same swap between the two moves it.
    if let Ok(Some(record)) = cooldown_store::load(Path::new(&state_path)) {
        if let Some(attempt) = record.provisional {
            let marker = attempt.completion;
            println!(
                "cooldown-provisional-at-seconds:{}",
                marker.completed_at_seconds
            );
            println!(
                "cooldown-provisional-demoted:{}",
                marker.demoted_strategy_id.as_str()
            );
            println!(
                "cooldown-provisional-promoted:{}",
                marker.promoted_strategy_id.as_str()
            );
        }
    }

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

/// Clear an UNCONFIRMED marker an interrupted swap left behind.
///
/// Adversarial review r22. Round 21 made `begin_provisional` refuse to displace a
/// different swap's unconfirmed marker — correctly, since that marker may be the only
/// thing suppressing the automatic triggers for a strategy that went live before the
/// interruption — and the refusal told the operator to "clear it if it did not
/// complete". No subcommand did that. The only public write path was
/// `record-completion`, which would have turned a swap that never completed into one
/// that did, so the honest recovery was to hand-edit the file or to lie to the tool.
///
/// This is the recovery surface that refusal promised. It is deliberately narrow:
///
///   * it names the swap, so an operator states WHICH interruption they reconciled;
///   * it requires `--confirm`, because retiring suppression is a safety act, not a
///     cleanup (SyRS SYS-2d / NFR-S2);
///   * it can never touch a CONFIRMED completion — the store refuses, and a swap that
///     really did complete keeps its window;
///   * it reports what it found rather than a bare exit, so "nothing matched" is
///     distinguishable from "cleared", which is the difference between an operator
///     believing they have reconciled something and having actually done so.
fn cmd_clear_provisional(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let mut state_path: Option<String> = None;
    let mut demoted: Option<String> = None;
    let mut promoted: Option<String> = None;
    let mut at_seconds: Option<u64> = None;
    let mut attempt_id: Option<String> = None;
    let mut confirm = false;

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
            "--at" => {
                if at_seconds.is_some() {
                    return Err(dup(flag));
                }
                at_seconds = Some(parse_u64(&take_value(&mut iter, flag)?, flag)?);
            }
            "--attempt-id" => {
                if attempt_id.is_some() {
                    return Err(dup(flag));
                }
                attempt_id = Some(take_value(&mut iter, flag)?);
            }
            "--confirm" => {
                if confirm {
                    return Err(dup(flag));
                }
                confirm = true;
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    let state_path = state_path.ok_or_else(|| format!("--state <path> is required\n\n{USAGE}"))?;
    let demoted = demoted.ok_or_else(|| format!("--demoted <id> is required\n\n{USAGE}"))?;
    let promoted = promoted.ok_or_else(|| format!("--promoted <id> is required\n\n{USAGE}"))?;
    let attempt_id = attempt_id.ok_or_else(|| {
        format!(
            "--attempt-id <id> is required: name the attempt exactly as `status` reported \
             it. The pair and the instant are not an identity — a retry of the same swap \
             in the same second leaves all three unchanged while the ATTEMPT changes — so \
             without it this command would clear whichever attempt is there now rather \
             than the one you inspected (adversarial review r27)\n\n{USAGE}"
        )
    })?;
    let at_seconds = at_seconds.ok_or_else(|| {
        format!(
            "--at <epoch-secs> is required: name the marker's instant exactly as `status` \
             reported it. A retry of the same swap REPLACES the marker, so a request that \
             named only the pair would clear whichever attempt is there now rather than the \
             one you inspected — and that attempt may be the interruption still protecting a \
             strategy that went live (adversarial review r25)\n\n{USAGE}"
        )
    })?;
    if !confirm {
        return Err(format!(
            "--confirm is required: clearing an unconfirmed marker RETIRES the cool-down \
             it was holding, and that marker may be the only thing suppressing the \
             automatic triggers for a strategy that went live before the interruption. \
             Establish that the swap did NOT complete first — if it did, use \
             record-completion instead, which confirms the window rather than retiring \
             it (SyRS SYS-2d / NFR-S2)\n\n{USAGE}"
        ));
    }

    // Validated before anything mutates, same rule as `record-completion`: a value that
    // cannot be represented must not reach the proof stream after the window has moved.
    let demoted_id = parse_strategy_id(&demoted, "--demoted")?;
    let promoted_id = parse_strategy_id(&promoted, "--promoted")?;
    let path = Path::new(&state_path);

    // Read BEFORE, so the report describes what was actually there rather than what the
    // operator asked for. `abandon_provisional` is deliberately idempotent and silent
    // about mismatches — the right shape for a best-effort call on a failure path, and
    // the wrong shape for an operator surface, which must be able to say "nothing here
    // matched what you named".
    let found = cooldown_store::load(path)
        .map_err(|error| format!("the cool-down window could not be read: {error}"))?
        .and_then(|record| record.provisional);
    // FULL equality — pair AND instant. Identity is not provenance (the r18 lesson,
    // here at the operator surface): between an operator reading `status` and running
    // this command, a retry of the SAME swap can replace the marker, and a pair-only
    // match would retire the newer attempt's suppression on the strength of a request
    // about the older one.
    // The ATTEMPT ID is the identity; the pair and the instant are consistency checks
    // on top of it (adversarial review r27). Matching without it and then clearing with
    // the CURRENT marker's id — which is what this did — is not a compare-and-swap at
    // all: it re-reads and clears whatever is there, which is the operation r25 set out
    // to prevent.
    let matched = found.as_ref().is_some_and(|stored| {
        stored.attempt_id == attempt_id
            && stored.completion.demoted_strategy_id.as_str() == demoted_id.as_str()
            && stored.completion.promoted_strategy_id.as_str() == promoted_id.as_str()
            && stored.completion.completed_at_seconds == at_seconds
    });

    match &found {
        Some(stored) => {
            println!("provisional-found:true");
            println!(
                "provisional-demoted:{}",
                stored.completion.demoted_strategy_id.as_str()
            );
            println!(
                "provisional-promoted:{}",
                stored.completion.promoted_strategy_id.as_str()
            );
            println!(
                "provisional-at-seconds:{}",
                stored.completion.completed_at_seconds
            );
            println!("provisional-attempt-id:{}", stored.attempt_id);
        }
        None => println!("provisional-found:false"),
    }

    if !matched {
        println!("provisional-cleared:false");
        return Err(match &found {
            Some(stored)
                if stored.completion.demoted_strategy_id.as_str() == demoted_id.as_str()
                    && stored.completion.promoted_strategy_id.as_str() == promoted_id.as_str() =>
            {
                format!(
                    "the marker for this swap is attempt {:?} at {}s, not the {:?} at {}s \
                     you named — it MOVED between your read and this command, which means \
                     the swap was retried. Re-read it with `status` and reconcile the \
                     attempt that is actually recorded; the one you inspected is gone and \
                     the one here may be protecting a strategy that went live",
                    stored.attempt_id,
                    stored.completion.completed_at_seconds,
                    attempt_id,
                    at_seconds,
                )
            }
            Some(stored) => format!(
                "the unconfirmed marker here belongs to a DIFFERENT swap ({} -> {}); \
                 refusing to clear it on the strength of a request naming {} -> {}. \
                 Reconcile the swap that is actually recorded",
                stored.completion.demoted_strategy_id.as_str(),
                stored.completion.promoted_strategy_id.as_str(),
                demoted_id.as_str(),
                promoted_id.as_str(),
            ),
            None => "there is no unconfirmed marker at this window to clear. A CONFIRMED \
                     completion is not clearable by this command — it is a real cool-down, \
                     and it expires on its own"
                .to_string(),
        });
    }

    let attempt = found.expect("matched implies present");
    cooldown_store::abandon_provisional(path, &attempt.completion, &attempt.attempt_id)
        .map_err(|error| format!("the marker could not be cleared: {error}"))?;

    // Verify the artefact, not the intent (CLAUDE.md rule 5): re-read and say what the
    // window is NOW, because retiring suppression is exactly the moment an operator
    // needs to know whether the automatic triggers are armed again.
    let record = cooldown_store::load(path)
        .map_err(|error| format!("the marker was cleared but the window is unreadable: {error}"))?;
    if record.and_then(|r| r.provisional).is_some() {
        return Err(
            "the marker is still present after the clear; the window on disk is not what \
             this command just wrote"
                .to_string(),
        );
    }
    println!("provisional-cleared:true");
    println!("deferred-writer:SRS-RESV-005");
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
