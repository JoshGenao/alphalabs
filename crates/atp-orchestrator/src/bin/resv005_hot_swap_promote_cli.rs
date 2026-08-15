//! SRS-RESV-005 / SyRS SYS-49d operator CLI — execute a Hot-Swap and demonstrate
//! that a paper strategy is promoted to live **only after a successful demotion**.
//!
//! This is the CLI arm of the SYS-49a operator surface for swap EXECUTION (the
//! trigger-configuration arm is `resv003_hot_swap_trigger_cli`; the REST arm is
//! `python/atp_orchestration/hot_swap_execution.py`, which shells this binary).
//!
//! It drives the **real** [`StrategyOrchestrator::execute_hot_swap`] gate — the
//! same code path the REST route uses — over:
//!
//!   * the **real** SRS-RESV-004 demotion gate (`resolve_demotion`);
//!   * the **real** SRS-EXE-001 live-designation authority
//!     ([`LiveDesignation`]), persisted between invocations in a magic-headed
//!     snapshot (scratch write → fsync → atomic rename), so the single-live
//!     invariant survives the process boundary the REST surface sits behind;
//!   * the **real** SRS-SIM-004 paper-state snapshot for the "preserves prior
//!     paper performance history" clause — the fingerprint is computed from the
//!     candidate's actual `PaperMetricsAccumulator`, not from a flag.
//!
//! # Fixture tier
//!
//! Two inputs are FIXTURES, because their producers are deferred, and the tool
//! says so on every run (`transports:FIXTURE`):
//!
//!   * `--positions` stands in for the IB position feed (deferred SRS-EXE-006);
//!   * `--deployed-version` stands in for the durable deployed-version registry
//!     (deferred SRS-ORCH-004).
//!
//! Emitting the tier label is deliberate: a run of this tool is evidence about
//! the GATE, not about a live IB account, and a reader must not have to infer
//! which.
//!
//! # Scope
//!
//! One demote-then-promote attempt per invocation. A timeout blocks promotion for
//! that attempt; the durable demotion-pending lockout that would also block a
//! later retry is SRS-RESV-004's (see `hot_swap_promotion_contract.deferred[]`).

use atp_execution::designation::{LiveDesignation, LiveDesignationConfirmation};
use atp_orchestrator::cooldown::{ManualCooldownAcknowledgement, SwapCompletion};
use atp_orchestrator::cooldown_store::{self, CompletionOutcome};
use atp_orchestrator::hot_swap_promotion::{
    CooldownControl, CooldownWindowOutcome, HotSwapPromotionEvent, HotSwapPromotionEventSink,
    LivePositionProbe, OpenPosition, PaperHistoryFingerprint, PaperHistorySource, PromotionPorts,
    SwapCompletionSink,
};
use atp_orchestrator::{
    demotion_pending_store::FileDemotionPendingLock, trigger_config_store, DeployedVersionRegistry,
    DeployedVersionRegistryError, HotSwapDemotionEventSink, HotSwapLiquidationProbe,
    HotSwapSideEffectError, OperatorAlertSink, StrategyOrchestrator, UnfilledOrderCanceller,
};
use atp_simulation::paper_state::PaperStateSnapshot;
use atp_types::{
    DeployedVersion, HotSwapDemotionEvent, HotSwapDemotionOutcome, HotSwapDemotionRequest,
    OperatorAlertEvent, SourceHash, StrategyId,
};
use std::cell::{Cell, RefCell};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicU64, Ordering};

/// Read the REAL clock, with `--now` as the only override.
///
/// This used to be `const OBSERVED_AT_SECONDS: u64 = 1_715_000_000`, which was
/// harmless while the timestamp only labelled an audit record. It stopped being
/// harmless when SRS-RESV-006 made this binary the producer of a swap COMPLETION:
/// SYS-49e says the cool-down starts at "the timestamp of the most recent
/// successful swap completion", so a frozen constant would stamp every window with
/// the same 2024 instant and the seven-day window would be born already expired —
/// a cool-down that never suppresses anything (`durable-writes.md` rule 23).
///
/// A pre-epoch clock is an `Err`, never `unwrap_or(0)`: a 0 would make every window
/// read as long expired, which is the same fail-open by another route.
fn wall_clock_seconds() -> Result<u64, String> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|since| since.as_secs())
        .map_err(|_| {
            "system clock reports a time before the Unix epoch; refusing to execute a \
             Hot-Swap whose cool-down window would be stamped against it"
                .to_string()
        })
}

/// SYS-49b default liquidation timeout.
const DEFAULT_TIMEOUT_SECONDS: u64 = 60;

/// Schema version of each promotion-journal line. The version travels per LINE,
/// not in a file header: the journal is append-only under `O_APPEND`, so no writer
/// owns "the start of the file" and a header would race.
const PROMOTION_LOG_SCHEMA_VERSION: u64 = 1;

/// Schema version of the durable live-designation snapshot, embedded in the magic
/// line so an old reader hits a clean version gate rather than "corrupt".
const DESIGNATION_STATE_SCHEMA_VERSION: u64 = 1;

/// Magic header compared for exact equality on load; a foreign or truncated file
/// refuses the whole read rather than reading as "nothing is designated" — that
/// silent empty would let a promotion run over a live strategy.
const STATE_MAGIC: &str = "RESV005-LIVE-DESIGNATION-STATE v1";

const _: () = {
    assert!(DESIGNATION_STATE_SCHEMA_VERSION == 1);
    assert!(matches!(STATE_MAGIC.as_bytes().last(), Some(b'1')));
};

static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

const USAGE: &str = "\
resv005_hot_swap_promote_cli — SRS-RESV-005 Hot-Swap promotion (demote, then promote)

USAGE:
    resv005_hot_swap_promote_cli <SUBCOMMAND> [FLAGS]

SUBCOMMANDS:
    swap        Execute one demote-then-promote attempt.
    status      Print the durable live-designation state.
    help        Print this help.

swap FLAGS:
    --state <path>               durable live-designation snapshot (required).
                                 A missing file is an empty designation; an
                                 unreadable or foreign one is an ERROR, never a
                                 silent 'nothing is live'.
    --demoting <id>              the current live strategy to demote (required)
    --candidate <id>             the paper strategy to promote (required)
    --confirm                    explicit operator confirmation (required —
                                 SyRS SYS-2d / NFR-S2). Without it nothing runs.
    --paper-state <dir>          SRS-SIM-004 paper-state store directory (required).
                                 The candidate's performance history is read from
                                 the REAL snapshot: unreadable => refuse, absent
                                 entry => refuse. Neither reads as 'preserved'.
    --timeout <secs>             demotion liquidation timeout (default 60)
    --liquidation <flat|timeout> FIXTURE demotion outcome. REQUIRED; no default
    --positions <spec>           FIXTURE live positions. REQUIRED (SRS-EXE-006):
                                   flat            no open positions
                                   unreadable      the probe cannot be read
                                   <SYM>:<QTY>,... explicit open positions
    --deployed-version <spec>    FIXTURE code identity. REQUIRED (SRS-ORCH-004):
                                   sha256:<64-hex> the candidate's artifact
                                   missing         no recorded version
                                   unreadable      the registry cannot be read
    --inject <drift>             non-vacuity: make the post-condition re-read
                                   disagree with the capture, proving the gate
                                   rolls the designation back
                                   (paper-drift | version-drift)
    --allow-fixture-safety-inputs
                                 REQUIRED to run with FIXTURE values for the two
                                 SAFETY facts below (--positions,
                                 --deployed-version). Without it the swap refuses:
                                 a promotion decided on a fixture flat-account or a
                                 fixture artifact hash is a false green on a live
                                 trading path, and a caller must say out loud that
                                 it is running a drill.
    --demotion-lock <path>       SRS-RESV-004 durable demotion-pending lockout
                                 (REQUIRED). resolve_demotion consults it BEFORE
                                 its probe, so a swap attempted while a previous
                                 demotion is unresolved is refused before any side
                                 effect fires. An unreadable lockout blocks exactly
                                 like a held one.
    --log <path>                 durable JSON-Lines promotion journal (append +
                                 fsync). The record's 1-based ordinal is printed
                                 as `swap-record-ordinal` and is the swap's
                                 identity for the REST surface; without --log the
                                 ordinal is reported as `-`, never invented.
    --cooldown-state <path>      SRS-RESV-006 durable SyRS SYS-49e cool-down
                                 window (REQUIRED). Consulted BEFORE the demotion:
                                 an ACTIVE window — or one that cannot be read —
                                 refuses the swap unless --confirm-cooldown is
                                 given. A SUCCESSFUL swap then RECORDS its
                                 completion here, which starts the next window.
                                 Required rather than defaulted so that 'we could
                                 not tell' is never reachable by omitting a flag.
    --confirm-cooldown           acknowledge the cool-down confirmation warning
                                 and swap anyway. SYS-49e PERMITS a manual swap
                                 during the window; it only requires the operator
                                 to say so. Deliberately separate from --confirm,
                                 which is the SYS-2d transport confirmation — one
                                 flag meaning both would make every ordinary
                                 confirmed swap a silent cool-down override.
    --now <epoch-seconds>        override the clock (default: the real one). This
                                 is the timestamp the cool-down window STARTS at,
                                 so it is read fresh per run and never frozen.

status FLAGS:
    --state <path>               the snapshot to read (required)
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(true) => ExitCode::SUCCESS,
        // A refused swap is a successful REPORT of a refusal, and a failure of
        // the operation the operator asked for: exit non-zero so automation
        // cannot read a blocked promotion as a completed one.
        Ok(false) => ExitCode::FAILURE,
        Err(err) => {
            eprintln!("resv005_hot_swap_promote_cli: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<bool, String> {
    let (command, rest) = match args.split_first() {
        Some(parts) => parts,
        None => return Err(format!("missing subcommand\n\n{USAGE}")),
    };
    match command.as_str() {
        "swap" => cmd_swap(rest),
        "status" => cmd_status(rest).map(|()| true),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(true)
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// swap
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct SwapArgs {
    state: Option<String>,
    demoting: Option<String>,
    candidate: Option<String>,
    paper_state: Option<String>,
    timeout: Option<u64>,
    liquidation: Option<String>,
    positions: Option<String>,
    deployed_version: Option<String>,
    inject: Option<String>,
    demotion_lock: Option<String>,
    log: Option<String>,
    /// SRS-RESV-006: the durable SyRS SYS-49e window this swap is gated by and
    /// records into. REQUIRED, like `--demotion-lock` and for the same reason.
    cooldown_state: Option<String>,
    now: Option<u64>,
    confirm: bool,
    confirm_cooldown: bool,
    allow_fixture_safety_inputs: bool,
}

/// Allowlist parse in ONE pass: an unknown, duplicated, or value-less flag is an
/// error. A typo that silently fell through to a default would let the tool run a
/// swap the operator did not ask for and report success.
fn parse_swap_args(rest: &[String]) -> Result<SwapArgs, String> {
    let mut parsed = SwapArgs::default();
    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--state" => set_once(&mut parsed.state, &mut iter, flag)?,
            "--demoting" => set_once(&mut parsed.demoting, &mut iter, flag)?,
            "--candidate" => set_once(&mut parsed.candidate, &mut iter, flag)?,
            "--paper-state" => set_once(&mut parsed.paper_state, &mut iter, flag)?,
            "--liquidation" => set_once(&mut parsed.liquidation, &mut iter, flag)?,
            "--positions" => set_once(&mut parsed.positions, &mut iter, flag)?,
            "--deployed-version" => set_once(&mut parsed.deployed_version, &mut iter, flag)?,
            "--inject" => set_once(&mut parsed.inject, &mut iter, flag)?,
            "--log" => set_once(&mut parsed.log, &mut iter, flag)?,
            "--demotion-lock" => set_once(&mut parsed.demotion_lock, &mut iter, flag)?,
            "--cooldown-state" => set_once(&mut parsed.cooldown_state, &mut iter, flag)?,
            "--now" => {
                if parsed.now.is_some() {
                    return Err(dup(flag));
                }
                let raw = take_value(&mut iter, flag)?;
                parsed.now = Some(
                    raw.parse()
                        .map_err(|_| format!("{flag} expects a u64 (got '{raw}')\n\n{USAGE}"))?,
                );
            }
            "--confirm-cooldown" => {
                if parsed.confirm_cooldown {
                    return Err(dup(flag));
                }
                parsed.confirm_cooldown = true;
            }
            "--timeout" => {
                if parsed.timeout.is_some() {
                    return Err(dup(flag));
                }
                let raw = take_value(&mut iter, flag)?;
                parsed.timeout = Some(
                    raw.parse()
                        .map_err(|_| format!("{flag} expects a u64 (got '{raw}')\n\n{USAGE}"))?,
                );
            }
            "--confirm" => {
                if parsed.confirm {
                    return Err(dup(flag));
                }
                parsed.confirm = true;
            }
            "--allow-fixture-safety-inputs" => {
                if parsed.allow_fixture_safety_inputs {
                    return Err(dup(flag));
                }
                parsed.allow_fixture_safety_inputs = true;
            }
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    Ok(parsed)
}

/// The REAL SRS-RESV-006 window writer — the production caller `cooldown_store`
/// named as `deferred-writer:SRS-RESV-005` until this binary existed.
///
/// All the filesystem work stays inside `cooldown_store`, which is the module the
/// SRS-DATA-015 registry names as this entity's writer; this is only the seam.
struct FileSwapCompletions {
    path: PathBuf,
    /// `--now`, when the operator pinned the clock. `None` means read the real one.
    ///
    /// Held here rather than reused from the observation instant because those are
    /// two different moments: the swap is classified against the window when it
    /// STARTS, and the next window opens when it COMPLETES, with the whole SYS-49b
    /// liquidation timeout legitimately in between (adversarial review r5).
    pinned_now: Option<u64>,
}

impl SwapCompletionSink for FileSwapCompletions {
    fn probe_writable(&self) -> Result<(), String> {
        cooldown_store::probe_writable(&self.path).map_err(|error| error.to_string())
    }

    fn completed_at_seconds(&self) -> Result<u64, String> {
        match self.pinned_now {
            // An explicit `--now` pins BOTH instants, so a test or a replay stays
            // reproducible. Deliberate, and the only way the two can coincide.
            Some(now) => Ok(now),
            // Otherwise the REAL clock, read at this moment — after the demotion
            // liquidated, after the promotion designated, after the drift re-reads.
            None => wall_clock_seconds(),
        }
    }

    fn record_swap_completion(&self, completion: &SwapCompletion) -> Result<(), String> {
        match cooldown_store::record_completion(&self.path, completion) {
            Ok(CompletionOutcome::Recorded { .. }) => Ok(()),
            // The store KEPT a newer completion than the one this swap offered. The
            // swap still happened, but the window an operator will observe is not
            // the one this swap opened — only a clock that disagrees with recorded
            // history reaches here, and silently reporting "started" would hide it.
            Ok(CompletionOutcome::KeptNewer { stored, offered }) => Err(format!(
                "a NEWER swap completion is already recorded ({} > {}); the running window \
                 belongs to that swap, not this one — the clock disagrees with recorded \
                 history and both need reconciling",
                stored.completed_at_seconds, offered.completed_at_seconds
            )),
            Err(error) => Err(error.to_string()),
        }
    }
}

fn cmd_swap(rest: &[String]) -> Result<bool, String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(true);
    }
    let args = parse_swap_args(rest)?;

    let state_path = required(&args.state, "--state")?;
    let demoting = parse_strategy_id(&required(&args.demoting, "--demoting")?, "--demoting")?;
    let candidate = parse_strategy_id(&required(&args.candidate, "--candidate")?, "--candidate")?;
    let paper_state_dir = required(&args.paper_state, "--paper-state")?;

    // NFR-S2: the confirmation gate runs BEFORE any state is read or written, so
    // an unconfirmed invocation cannot have touched anything.
    if !args.confirm {
        return Err(
            "--confirm is required: designating a strategy live is an explicit operator \
             action (SyRS SYS-2d / NFR-S2)"
                .to_string(),
        );
    }
    if demoting.as_str() == candidate.as_str() {
        return Err(format!(
            "--demoting and --candidate both name '{}'; a Hot-Swap must name two \
             different strategies",
            demoting.as_str()
        ));
    }
    // FIXTURE-TIER QUARANTINE. Two of this tool's inputs are the SAFETY facts the
    // requirement turns on: whether the live account is flat (--positions) and
    // whether the candidate runs the same artifact (--deployed-version). Their real
    // producers are deferred (SRS-EXE-006 / SRS-ORCH-004), so the values here are
    // FIXTURES — and a promotion decided on a fixture flat-account is a false green
    // on a live trading path, not a demo.
    //
    // Defaulting them silently is what made the served REST route able to report
    // PROMOTED without proving either fact. So the fixture tier is now OPT-IN: a
    // caller has to say out loud that it is running a drill. The REST handler never
    // passes this flag, which is what keeps the served path honest.
    //
    // Checked BEFORE the state file is read or written, so a refused drill leaves
    // nothing behind.
    if !args.allow_fixture_safety_inputs {
        return Err(
            "refusing to promote on FIXTURE safety inputs: --positions (the flat-account \
             fact, real producer deferred to SRS-EXE-006) and --deployed-version (the \
             code-identity fact, real producer deferred to SRS-ORCH-004) have no real \
             source in this build. Pass --allow-fixture-safety-inputs to run this as an \
             explicit drill; a caller that cannot prove the account is flat must not \
             report a promotion"
                .to_string(),
        );
    }

    let inject = match args.inject.as_deref() {
        None => Injection::None,
        Some("paper-drift") => Injection::PaperDrift,
        Some("version-drift") => Injection::VersionDrift,
        Some(other) => {
            return Err(format!(
                "--inject expects 'paper-drift' or 'version-drift' (got '{other}')\n\n{USAGE}"
            ))
        }
    };

    let state_path = PathBuf::from(state_path);
    // SERIALIZE the whole read -> execute -> write sequence. Without this, two
    // concurrent swaps both read the same live strategy, both run the gate against
    // that stale snapshot, both report PROMOTED for different candidates, and the
    // last rename decides durable state — breaking the single-live-strategy
    // invariant that this gate exists to protect. The guard is held for the LIFETIME
    // of the critical section (it is dropped at the end of `cmd_swap`, after the
    // save), not merely acquired: releasing it before the write would reopen the
    // same race.
    let _swap_guard = trigger_config_store::ExclusiveGuard::acquire_creating(&state_path)
        .map_err(|error| format!("cannot serialize the swap against concurrent ones: {error}"))?;
    let mut designation = load_designation(&state_path)?;
    let designated_before = designation
        .designated()
        .map(|id| id.as_str().to_string())
        .unwrap_or_else(|| "none".to_string());

    let request = HotSwapDemotionRequest {
        demoting_strategy_id: demoting.clone(),
        candidate_strategy_id: candidate.clone(),
        timeout_seconds: args.timeout.unwrap_or(DEFAULT_TIMEOUT_SECONDS),
    };

    let probe = FixtureLiquidationProbe(parse_liquidation(args.liquidation.as_deref())?);
    let positions = FixturePositions(parse_positions(args.positions.as_deref())?);
    let paper = RealPaperHistory {
        dir: PathBuf::from(paper_state_dir),
        reads: Cell::new(0),
        drift: matches!(inject, Injection::PaperDrift),
    };
    let versions = FixtureVersions {
        answer: parse_version(args.deployed_version.as_deref())?,
        reads: Cell::new(0),
        drift: matches!(inject, Injection::VersionDrift),
    };
    // SRS-RESV-004's durable lockout. Required, not optional: making it optional
    // would let a caller opt out of the very block that stops a swap running over
    // an unresolved demotion, which is the same silent-default class the fixture
    // safety inputs already had to close.
    let lock = FileDemotionPendingLock::new(required(&args.demotion_lock, "--demotion-lock")?);
    let events = JournalPromotionEvents {
        pending: RefCell::new(None),
        path: args.log.as_ref().map(PathBuf::from),
        ordinal: Cell::new(None),
        append_failed: Cell::new(false),
    };

    let confirmation =
        LiveDesignationConfirmation::from_operator(candidate.clone(), "operator --confirm")
            .map_err(|error| error.to_string())?;

    // SRS-RESV-006 / SYS-49e. The window is resolved through the ONE resolver, so
    // the store read and the `Err -> Unknown` mapping happen in a single place —
    // and an omitted `--cooldown-state` is `Unknown`, which the gate refuses. That
    // is why the flag is required rather than defaulted: "we could not tell" must
    // never be reachable by leaving an argument off.
    let cooldown_state_path = PathBuf::from(required(&args.cooldown_state, "--cooldown-state")?);
    let observed_at_seconds = match args.now {
        Some(now) => now,
        None => wall_clock_seconds()?,
    };
    let cooldown_state = cooldown_store::resolve(Some(&cooldown_state_path), observed_at_seconds);
    let completions = FileSwapCompletions {
        path: cooldown_state_path,
        pinned_now: args.now,
    };
    let acknowledgement = if args.confirm_cooldown {
        ManualCooldownAcknowledgement::Acknowledged
    } else {
        ManualCooldownAcknowledgement::NotAcknowledged
    };

    println!("transports:FIXTURE");
    println!("demoting:{}", demoting.as_str());
    println!("candidate:{}", candidate.as_str());
    println!("designation-before:{designated_before}");
    println!("observed-at-seconds:{observed_at_seconds}");
    println!("cooldown-state:{}", cooldown_state.as_str());
    println!("cooldown-in-effect:{}", !cooldown_state.proven_clear());
    // Printed on EVERY run, not only on the refusal: an operator reading a
    // successful swap needs to know whether they overrode a live window.
    println!("cooldown-confirmed:{}", args.confirm_cooldown);

    let outcome = StrategyOrchestrator.execute_hot_swap(
        request,
        &probe,
        &FixtureCanceller,
        &FixtureAlerts,
        &FixtureDemotionEvents,
        &lock,
        PromotionPorts {
            positions: &positions,
            paper_history: &paper,
            versions: &versions,
            events: &events,
        },
        CooldownControl {
            state: &cooldown_state,
            acknowledgement,
            completions: &completions,
        },
        &mut designation,
        confirmation,
        observed_at_seconds,
    );

    let designated_after = designation
        .designated()
        .map(|id| id.as_str().to_string())
        .unwrap_or_else(|| "none".to_string());

    // Persist the authority BEFORE reporting: a run that printed `PROMOTED` and
    // failed to persist the designation would tell the operator a strategy is
    // live while the next invocation disagrees.
    //
    // Written only when the designation actually MOVED. Two reasons, and the
    // second is the load-bearing one:
    //   * a refusal that changed nothing must leave the durable record — and its
    //     absence, on a first run — exactly as it found it;
    //   * a refusal that DID change it (the post-condition rollback arms release
    //     the demoted strategy and then roll the candidate back) must still be
    //     persisted, or the durable record would disagree with the authority this
    //     process just decided.
    // A pre-publish failure means the durable record is untouched, so it is safe to
    // abort with nothing printed — REST maps that to a non-2xx, which correctly
    // reads as "nothing mutated". A POST-publish failure is the opposite: the live
    // slot has already moved, so the proof lines MUST still be emitted (the swap
    // happened) with the durability caveat carried alongside them.
    let mut published_unsynced: Option<String> = None;
    if designated_after != designated_before {
        match save_designation(&state_path, &designation) {
            PublishOutcome::Durable => {}
            // Nothing was published, so no committed promotion record may exist.
            PublishOutcome::FailedBeforePublish(reason) => return Err(reason),
            PublishOutcome::PublishedNotSynced(reason) => published_unsynced = Some(reason),
        }
    }

    // The durable live state is now settled (published, or provably unchanged), so the
    // audit record can be committed without the risk of outliving a swap that never
    // took effect.
    events.commit();

    match &outcome {
        Ok(promoted) => {
            println!("demotion-outcome:FLAT_CONFIRMED");
            println!("promotion:PROMOTED");
            println!(
                "paper-history:{}:{}:{}",
                promoted.paper_history.equity_points,
                promoted.paper_history.trades,
                promoted.paper_history.digest
            );
            println!(
                "deployed-version:{}",
                promoted.deployed_version.version_identifier()
            );
            println!(
                "demotion-elapsed-seconds:{}",
                promoted.demotion_elapsed_seconds
            );
            // SYS-49e's third clause, evidenced: this swap's completion timestamp IS
            // the next window's start.
            println!("cooldown-window:{}", promoted.cooldown_window.as_str());
            if let CooldownWindowOutcome::Started {
                started_at_seconds, ..
            } = &promoted.cooldown_window
            {
                println!("cooldown-window-started-at-seconds:{started_at_seconds}");
            }
        }
        Err(error) => {
            // Three-valued, not a boolean: NOT_STARTED means no demotion-side port
            // was touched, so nothing mutated and no lockout exists to wait on.
            // Collapsing it into DEMOTION_PENDING told the surface a swap had been
            // accepted and left the dashboard inert awaiting a state that was never
            // created.
            println!("demotion-outcome:{}", error.demotion_outcome().as_str());
            println!("promotion:BLOCKED");
            println!("refusal:{}", error.machine_reason());
            eprintln!("{error}");
        }
    }
    println!("designation-after:{designated_after}");
    // Three states, same discipline as `promotion-recorded`: durable / moved but
    // not crash-durable / not applicable.
    println!(
        "designation-persisted:{}",
        match (&published_unsynced, designated_after == designated_before) {
            (Some(_), _) => "published-unsynced",
            (None, true) => "unchanged",
            (None, false) => "durable",
        }
    );
    if let Some(reason) = &published_unsynced {
        eprintln!(
            "SRS-RESV-005: the live designation was PUBLISHED but not fsynced ({reason}); \
             the slot has moved and a retry would be acting on changed state"
        );
    }
    // Reported LAST and read back from the journal, so it names a record that is
    // already durable. `-` means no journal was configured or the append failed:
    // an explicitly absent id, never a fabricated one.
    println!(
        "swap-record-ordinal:{}",
        events
            .ordinal
            .get()
            .map_or_else(|| "-".to_string(), |n| n.to_string())
    );
    // THREE states, never two. `false` means the append was attempted and failed —
    // the candidate may now be LIVE with no durable record of the swap, which is an
    // operator-reconciliation event. `not-configured` means the caller asked for no
    // journal. Reporting the first as the second would hide an unauditable live
    // state change behind a usage choice.
    let recorded = match (events.ordinal.get(), &events.path) {
        (Some(_), _) => "true",
        (None, Some(_)) => "false",
        (None, None) => "not-configured",
    };
    println!("promotion-recorded:{recorded}");
    // The swap COMPLETED and its SYS-49e window did not start. The automatic
    // triggers are therefore NOT suppressed, which is a fail-open on a safety
    // path — it cannot be undone here (the designation has moved and the book is
    // flat, so rolling back over an unwritable file would be strictly worse), so it
    // is made loud instead: an explicit sentence and a non-zero exit.
    if let Ok(promoted) = &outcome {
        if let CooldownWindowOutcome::NotStarted { reason } = &promoted.cooldown_window {
            eprintln!(
                "SRS-RESV-006: `{}` was promoted live but the SyRS SYS-49e cool-down window \
                 did NOT start ({reason}). THE COOL-DOWN IS NOT IN EFFECT — automatic swap \
                 triggers are not suppressed. Repair the cool-down state file and record the \
                 completion with resv006_hot_swap_cooldown_cli record-completion \
                 --completed-at {observed_at_seconds}, or disable the automatic triggers with \
                 resv003_hot_swap_trigger_cli until you have.",
                candidate.as_str()
            );
            return Ok(false);
        }
    }
    if outcome.is_ok() && recorded == "false" {
        // The promotion HAPPENED — the designation is written and persisted — but
        // its audit record did not land. That is not a clean success, so the exit
        // code must not say it was.
        eprintln!(
            "SRS-RESV-005: `{}` was promoted live but its promotion journal record \
             could NOT be written; the live state change is unauditable and needs \
             operator reconciliation",
            candidate.as_str()
        );
        return Ok(false);
    }
    Ok(outcome.is_ok())
}

fn cmd_status(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let mut state: Option<String> = None;
    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--state" => set_once(&mut state, &mut iter, flag)?,
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    let path = PathBuf::from(required(&state, "--state")?);
    let designation = load_designation(&path)?;
    println!(
        "designated:{}",
        designation
            .designated()
            .map(|id| id.as_str().to_string())
            .unwrap_or_else(|| "none".to_string())
    );
    Ok(())
}

// --------------------------------------------------------------------------- //
// Durable live-designation snapshot
// --------------------------------------------------------------------------- //

/// Read the durable designation.
///
/// Three states, kept apart: **no file** = nothing designated (a first run);
/// **a valid snapshot** = whatever it names; **a foreign, truncated, or malformed
/// file** = an ERROR. Collapsing the third into the first is exactly the failure
/// this gate exists to prevent — it would let a promotion proceed as though no
/// strategy were live.
fn load_designation(path: &Path) -> Result<LiveDesignation, String> {
    let content = match fs::read_to_string(path) {
        Ok(content) => content,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(LiveDesignation::new())
        }
        Err(error) => {
            return Err(format!(
                "cannot read state file {}: {error}",
                path.display()
            ))
        }
    };
    let mut lines = content.lines();
    match lines.next() {
        Some(line) if line == STATE_MAGIC => {}
        _ => {
            return Err(format!(
                "state file {} is not a {STATE_MAGIC} snapshot (refusing a foreign or \
                 truncated file rather than reading it as 'nothing is live')",
                path.display()
            ))
        }
    }
    let mut designation = LiveDesignation::new();
    let mut seen = false;
    for (index, line) in lines.enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let Some(id) = line.strip_prefix("designated\t") else {
            return Err(format!(
                "state file {} line {} is malformed (expected a `designated\\t<id>` line)",
                path.display(),
                index + 2
            ));
        };
        if seen {
            return Err(format!(
                "state file {} names more than one designated strategy; refusing an \
                 ambiguous single-live record (SyRS SYS-2a)",
                path.display()
            ));
        }
        if id.trim().is_empty() {
            return Err(format!(
                "state file {} designates a blank strategy id",
                path.display()
            ));
        }
        let id = StrategyId::new(id.trim());
        let confirmation = LiveDesignationConfirmation::from_operator(
            id.clone(),
            "restored from the durable designation snapshot",
        )
        .map_err(|error| error.to_string())?;
        designation
            .designate(id, confirmation)
            .map_err(|error| error.to_string())?;
        seen = true;
    }
    Ok(designation)
}

/// Publish the designation durably: unique scratch file → fsync → atomic rename →
/// parent-directory fsync. The repo's durable-file pattern
/// (`orch005_rollback_cli::save_state`, `atp_simulation::backtest_store`).
/// Outcome of publishing the designation, split by whether the durable state
/// ALREADY CHANGED when the failure happened.
///
/// The distinction is load-bearing for the REST surface: a non-2xx there documents
/// "nothing mutated; retry is allowed". A failure AFTER the atomic rename has
/// already moved the live slot, so reporting it the same way would invite a retry
/// of a swap that already took effect.
enum PublishOutcome {
    /// Written and fsynced.
    Durable,
    /// Failed BEFORE the rename — the durable record is untouched.
    FailedBeforePublish(String),
    /// The rename SUCCEEDED (the next process will read the new designation) but a
    /// later step did not. The live slot has moved; only crash-durability is
    /// uncertain.
    PublishedNotSynced(String),
}

fn save_designation(path: &Path, designation: &LiveDesignation) -> PublishOutcome {
    let mut body = String::from(STATE_MAGIC);
    body.push('\n');
    if let Some(id) = designation.designated() {
        // Write-side validation is a SUPERSET of the loader's, so a successful
        // save can never produce a snapshot the next load refuses.
        if id.as_str().trim().is_empty() || id.as_str().contains(['\t', '\n']) {
            return PublishOutcome::FailedBeforePublish(format!(
                "designated strategy id {:?} would write a snapshot the loader refuses",
                id.as_str()
            ));
        }
        body.push_str(&format!("designated\t{}\n", id.as_str()));
    }
    let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
    let scratch = path.with_extension(format!("tmp.{}.{seq}", std::process::id()));
    {
        let mut file = match fs::File::create(&scratch) {
            Ok(file) => file,
            Err(error) => {
                return PublishOutcome::FailedBeforePublish(format!(
                    "cannot create scratch {}: {error}",
                    scratch.display()
                ))
            }
        };
        if let Err(error) = file
            .write_all(body.as_bytes())
            .and_then(|()| file.sync_all())
        {
            let _ = fs::remove_file(&scratch);
            return PublishOutcome::FailedBeforePublish(format!(
                "cannot write scratch {}: {error}",
                scratch.display()
            ));
        }
    }
    if let Err(error) = fs::rename(&scratch, path) {
        let _ = fs::remove_file(&scratch);
        return PublishOutcome::FailedBeforePublish(format!(
            "cannot publish {} (rename): {error}",
            path.display()
        ));
    }
    // PAST THIS POINT the live slot has moved: the rename is atomic and the next
    // process reads the new designation. A failure here is NOT "nothing happened".
    let parent = path.parent().filter(|p| !p.as_os_str().is_empty());
    match fs::File::open(parent.unwrap_or_else(|| Path::new("."))).and_then(|dir| dir.sync_all()) {
        Ok(()) => PublishOutcome::Durable,
        Err(error) => {
            PublishOutcome::PublishedNotSynced(format!("cannot fsync state directory: {error}"))
        }
    }
}

// --------------------------------------------------------------------------- //
// Ports
// --------------------------------------------------------------------------- //

enum Injection {
    None,
    PaperDrift,
    VersionDrift,
}

struct FixtureLiquidationProbe(HotSwapDemotionOutcome);
impl HotSwapLiquidationProbe for FixtureLiquidationProbe {
    fn await_flat_or_timeout(&self, _request: &HotSwapDemotionRequest) -> HotSwapDemotionOutcome {
        self.0
    }
}

/// Parse the fixture demotion outcome. NO default: a caller that omits it has not
/// said whether the demotion reached flat, and defaulting to "flat" would decide the
/// requirement's central fact by omission.
fn parse_liquidation(spec: Option<&str>) -> Result<HotSwapDemotionOutcome, String> {
    let spec = spec.ok_or_else(|| {
        format!(
            "--liquidation is required under --allow-fixture-safety-inputs (flat|timeout); \
             omitting it would decide the demotion outcome by default\n\n{USAGE}"
        )
    })?;
    match spec {
        "flat" => Ok(HotSwapDemotionOutcome::FlatBeforeTimeout { elapsed_seconds: 4 }),
        "timeout" => Ok(HotSwapDemotionOutcome::TimedOutDemotionPending {
            elapsed_seconds: DEFAULT_TIMEOUT_SECONDS + 1,
            timeout_seconds: DEFAULT_TIMEOUT_SECONDS,
        }),
        other => Err(format!(
            "--liquidation expects 'flat' or 'timeout' (got '{other}')\n\n{USAGE}"
        )),
    }
}

struct FixtureCanceller;
impl UnfilledOrderCanceller for FixtureCanceller {
    fn cancel_unfilled_liquidation_orders(
        &self,
        _request: &HotSwapDemotionRequest,
    ) -> Result<(), HotSwapSideEffectError> {
        println!("liquidation-cancel:ATTEMPTED");
        Ok(())
    }
}

struct FixtureAlerts;
impl OperatorAlertSink for FixtureAlerts {
    fn dispatch(&self, event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError> {
        let channels: Vec<&str> = event.channels.iter().map(|c| c.as_str()).collect();
        println!("operator-alert:{}", channels.join("+"));
        Ok(())
    }
}

struct FixtureDemotionEvents;
impl HotSwapDemotionEventSink for FixtureDemotionEvents {
    fn record(&self, event: HotSwapDemotionEvent) -> Result<(), HotSwapSideEffectError> {
        println!("demotion-promotion-blocked:{}", event.promotion_blocked);
        Ok(())
    }
}

enum PositionAnswer {
    Flat,
    Open(Vec<OpenPosition>),
    Unreadable,
}

struct FixturePositions(PositionAnswer);
impl LivePositionProbe for FixturePositions {
    fn open_positions(&self) -> Result<Vec<OpenPosition>, HotSwapSideEffectError> {
        match &self.0 {
            PositionAnswer::Flat => Ok(Vec::new()),
            PositionAnswer::Open(held) => Ok(held.clone()),
            PositionAnswer::Unreadable => Err(HotSwapSideEffectError::new(
                "FIXTURE: the live position probe reported it could not be read",
            )),
        }
    }
}

/// Parse the fixture position answer. NO default: an unstated account is not a flat
/// account, and defaulting to "flat" is precisely the silent success this gate exists
/// to refuse.
fn parse_positions(spec: Option<&str>) -> Result<PositionAnswer, String> {
    let spec = spec.ok_or_else(|| {
        format!(
            "--positions is required under --allow-fixture-safety-inputs \
             (flat|unreadable|<SYM>:<QTY>,...); an unstated account is not a flat one\n\n{USAGE}"
        )
    })?;
    match spec {
        "flat" => Ok(PositionAnswer::Flat),
        "unreadable" => Ok(PositionAnswer::Unreadable),
        raw => {
            let mut held = Vec::new();
            for entry in raw.split(',') {
                let (symbol, quantity) = entry.split_once(':').ok_or_else(|| {
                    format!("--positions expects '<SYM>:<QTY>,...' (got '{entry}')\n\n{USAGE}")
                })?;
                if symbol.trim().is_empty() {
                    return Err(format!("--positions entry '{entry}' has a blank symbol"));
                }
                held.push(OpenPosition {
                    symbol: symbol.trim().to_string(),
                    quantity: quantity.trim().parse().map_err(|_| {
                        format!("--positions quantity must be an i64 (got '{quantity}')")
                    })?,
                });
            }
            Ok(PositionAnswer::Open(held))
        }
    }
}

/// The REAL SRS-SIM-004 paper-history source: reads the persisted snapshot and
/// fingerprints the candidate's accumulated performance.
struct RealPaperHistory {
    dir: PathBuf,
    reads: Cell<u64>,
    drift: bool,
}

impl PaperHistorySource for RealPaperHistory {
    fn fingerprint(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<Option<PaperHistoryFingerprint>, HotSwapSideEffectError> {
        let read = self.reads.get();
        self.reads.set(read + 1);
        let snapshot = PaperStateSnapshot::load_from_path(&self.dir).map_err(|error| {
            HotSwapSideEffectError::new(format!("paper-state store unreadable: {error}"))
        })?;
        let Some(accumulator) = snapshot.metrics().get(strategy_id) else {
            return Ok(None);
        };
        let mut fingerprint = PaperHistoryFingerprint {
            equity_points: accumulator.equity_curve().len() as u64,
            trades: accumulator.trade_log().len() as u64,
            digest: digest_of(accumulator),
        };
        // Non-vacuity: make the post-condition RE-READ disagree with the capture,
        // which cannot happen naturally inside one process. Without this the
        // rollback arm would never be exercised by the operator walk.
        if self.drift && read > 0 {
            fingerprint.digest.push_str("-INJECTED-DRIFT");
        }
        Ok(Some(fingerprint))
    }
}

/// Deterministic FNV-1a digest over the accumulated history's explicit fields.
///
/// Built from the named fields rather than a `Debug` rendering so the value is
/// stable by construction: a same-length but rewritten history changes the digest.
fn digest_of(accumulator: &atp_simulation::paper_metrics::PaperMetricsAccumulator) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    let mut fold = |bytes: &[u8]| {
        for byte in bytes {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
    };
    fold(&accumulator.starting_cash_minor().to_le_bytes());
    fold(&accumulator.cash_minor().to_le_bytes());
    for point in accumulator.equity_curve() {
        fold(&point.ts.to_le_bytes());
        fold(&point.equity_minor.to_le_bytes());
    }
    for fill in accumulator.trade_log() {
        fold(&fill.ts.to_le_bytes());
        fold(fill.symbol.as_bytes());
        fold(&fill.quantity.to_le_bytes());
        fold(&fill.price_minor.to_le_bytes());
        fold(&fill.commission_minor.to_le_bytes());
        fold(&fill.slippage_minor.to_le_bytes());
        fold(&fill.spread_impact_minor.to_le_bytes());
    }
    format!("{hash:016x}")
}

enum VersionAnswer {
    Recorded(DeployedVersion),
    Missing,
    Unreadable,
}

struct FixtureVersions {
    answer: VersionAnswer,
    reads: Cell<u64>,
    drift: bool,
}

impl DeployedVersionRegistry for FixtureVersions {
    fn record(
        &self,
        _strategy_id: &StrategyId,
        _version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError> {
        Err(DeployedVersionRegistryError::new(
            "the SRS-RESV-005 promotion gate never writes a deployed version",
        ))
    }

    fn lookup(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        let read = self.reads.get();
        self.reads.set(read + 1);
        match &self.answer {
            VersionAnswer::Missing => Ok(None),
            VersionAnswer::Unreadable => Err(DeployedVersionRegistryError::new(
                "FIXTURE: the deployed-version registry reported it could not be read",
            )),
            VersionAnswer::Recorded(version) => {
                if self.drift && read > 0 {
                    return Ok(Some(DeployedVersion::new(
                        SourceHash::new(
                            "sha256:\
                             bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        ),
                        version.deployed_at_seconds,
                    )));
                }
                Ok(Some(version.clone()))
            }
        }
    }
}

/// Parse the fixture code identity. NO default: a dummy artifact hash handed out by
/// omission would satisfy "uses the same strategy code" without anyone stating what
/// the code is.
fn parse_version(spec: Option<&str>) -> Result<VersionAnswer, String> {
    let spec = spec.ok_or_else(|| {
        format!(
            "--deployed-version is required under --allow-fixture-safety-inputs \
             (sha256:<64-hex>|missing|unreadable)\n\n{USAGE}"
        )
    })?;
    match spec {
        "missing" => Ok(VersionAnswer::Missing),
        "unreadable" => Ok(VersionAnswer::Unreadable),
        raw => {
            let hash = SourceHash::new(raw);
            hash.validate().map_err(|error| {
                format!("--deployed-version is not a valid source hash: {error}\n\n{USAGE}")
            })?;
            Ok(VersionAnswer::Recorded(DeployedVersion::new(
                hash,
                1_700_000_000,
            )))
        }
    }
}

/// The SYS-61 `hot_swap` PROMOTION audit sink, backed by a durable append-only
/// JSON-Lines journal when `--log` names one.
///
/// The record's ORDINAL is the swap's identity — the position a later reader can
/// go to and find this very record — which is what the REST surface hands back as
/// `swap_id`. Binding the id to a durable artefact rather than to the fact that a
/// call returned is the same rule the SRS-RESV-003 trigger surface follows: a
/// per-call outcome is not proof of an end state.
///
/// With no `--log` the sink only prints, and the ordinal is reported as `-` — an
/// explicitly absent id, never a fabricated one.
struct JournalPromotionEvents {
    /// The accepted-but-not-yet-appended event (see `record`).
    pending: RefCell<Option<HotSwapPromotionEvent>>,
    path: Option<PathBuf>,
    ordinal: Cell<Option<u64>>,
    /// Whether an append was ATTEMPTED and failed. Kept apart from "no journal was
    /// configured": a promotion whose audit record could not be written is an
    /// operator-reconciliation event, while an unconfigured journal is a CLI usage
    /// choice. Collapsing them would report the first as the second.
    append_failed: Cell<bool>,
}

impl HotSwapPromotionEventSink for JournalPromotionEvents {
    /// ACCEPTS the event; it does not append it yet.
    ///
    /// The gate calls this while the live designation is still only in memory. If the
    /// append happened here and the durable publish then failed BEFORE its rename, the
    /// journal would claim `promoted:true` for a swap the durable authority never
    /// accepted — and recovery tooling would reconcile from a false audit record for a
    /// live-trading state change. So the event is buffered and [`commit`] appends it
    /// once the designation is published (or once the attempt is known to have changed
    /// nothing). Raised by /codex adversarial review r6 [high].
    fn record(&self, event: HotSwapPromotionEvent) -> Result<(), HotSwapSideEffectError> {
        println!("promotion-event-promoted:{}", event.promoted);
        println!("promotion-event-refusal:{}", event.refusal.unwrap_or("-"));
        *self.pending.borrow_mut() = Some(event);
        Ok(())
    }
}

impl JournalPromotionEvents {
    /// Append the buffered event, AFTER the durable live-state publish. Called only on
    /// paths where the designation is known: published, or provably unchanged.
    fn commit(&self) {
        let Some(event) = self.pending.borrow_mut().take() else {
            return;
        };
        let Some(path) = &self.path else {
            return;
        };
        let line = format!(
            "{{\"schema_version\":{PROMOTION_LOG_SCHEMA_VERSION},\
             \"event_type\":\"PROMOTION\",\
             \"demoting_strategy_id\":{},\
             \"candidate_strategy_id\":{},\
             \"promoted\":{},\
             \"refusal\":{},\
             \"flat_confirmed\":{},\
             \"paper_history_preserved\":{},\
             \"deployed_version\":{},\
             \"observed_at_seconds\":{}}}\n",
            json_string(event.demoting_strategy_id.as_str()),
            json_string(event.candidate_strategy_id.as_str()),
            event.promoted,
            event.refusal.map_or("null".to_string(), json_string),
            event.flat_confirmed,
            event.paper_history_preserved,
            event
                .deployed_version
                .as_deref()
                .map_or("null".to_string(), json_string),
            event.observed_at_seconds,
        );
        match append_journal_line(path, &line) {
            Ok(ordinal) => self.ordinal.set(Some(ordinal)),
            Err(reason) => {
                self.append_failed.set(true);
                eprintln!("SRS-RESV-005: promotion journal append failed: {reason}");
            }
        }
    }
}

/// Append `line` under `O_APPEND`, flush, fsync, and return the record's 1-based
/// ordinal (the journal's record count after the append).
fn append_journal_line(path: &Path, line: &str) -> Result<u64, String> {
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| format!("cannot open promotion journal {}: {error}", path.display()))?;
    file.write_all(line.as_bytes())
        .and_then(|()| file.sync_all())
        .map_err(|error| format!("cannot append to promotion journal: {error}"))?;
    let content = fs::read_to_string(path)
        .map_err(|error| format!("cannot re-read promotion journal: {error}"))?;
    Ok(content.lines().filter(|l| !l.trim().is_empty()).count() as u64)
}

/// Minimal JSON string escaper with TOTAL C0 escaping.
///
/// Every control character below 0x20 becomes `\u00XX`, not just the five with
/// short forms — a raw control byte inside a strategy id would otherwise produce
/// a journal line that no strict reader accepts, and the corruption would land
/// AFTER the swap already happened, suppressing its own audit record.
fn json_string(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len() + 2);
    out.push('"');
    for ch in raw.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

// --------------------------------------------------------------------------- //
// Arg helpers
// --------------------------------------------------------------------------- //

fn wants_help(args: &[String]) -> bool {
    args.iter()
        .any(|arg| matches!(arg.as_str(), "help" | "--help" | "-h"))
}

fn set_once<'a>(
    slot: &mut Option<String>,
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<(), String> {
    if slot.is_some() {
        return Err(dup(flag));
    }
    *slot = Some(take_value(iter, flag)?);
    Ok(())
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    let value = iter
        .next()
        .ok_or_else(|| format!("{flag} expects a value\n\n{USAGE}"))?;
    // A value that is itself a flag means the operator omitted one: consuming it
    // would silently bind the NEXT flag's name as this flag's value.
    if value.starts_with("--") {
        return Err(format!(
            "{flag} expects a value but got the flag '{value}'\n\n{USAGE}"
        ));
    }
    Ok(value.to_string())
}

fn dup(flag: &str) -> String {
    format!("duplicate flag '{flag}'\n\n{USAGE}")
}

fn required(slot: &Option<String>, flag: &str) -> Result<String, String> {
    slot.clone()
        .ok_or_else(|| format!("{flag} is required\n\n{USAGE}"))
}

fn parse_strategy_id(value: &str, flag: &str) -> Result<StrategyId, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(format!(
            "{flag} must name a strategy (got {value:?})\n\n{USAGE}"
        ));
    }
    if trimmed.contains(['\t', '\n']) {
        return Err(format!(
            "{flag} must not contain a tab or newline (got {value:?}); it would write a \
             designation snapshot this build's own reader refuses\n\n{USAGE}"
        ));
    }
    Ok(StrategyId::new(trimmed))
}
