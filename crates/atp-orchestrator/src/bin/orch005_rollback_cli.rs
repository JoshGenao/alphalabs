//! SRS-ORCH-005 / SyRS SYS-80 / NFR-S2 operator CLI — demonstrate rollback to
//! the previous deployed strategy version: the retaining registry keeps the
//! version each deployment replaced, `rollback` restores exactly that version,
//! and rollback of the LIVE strategy requires the same explicit, strategy-bound
//! confirmation control as live promotion.
//!
//! This is the CLI arm of the operator surface named in SYS-80 ("via the
//! dashboard, CLI, or REST API"); the python/atp_orchestration handler shells
//! this bin for the runtime's CLI + REST lifecycle dispatch, and the dashboard
//! control is the deferred SRS-UI-001 leg. Emits deterministic `key:value`
//! proof lines (repo convention) and fails closed on unknown / duplicate /
//! valueless flags.
//!
//! Honesty notes (the deferred owners named in
//! `architecture/runtime_services.json` `rollback_contract.deferred[]`):
//!   * `--live <id>` wires a FIXED demonstration probe — the real
//!     live-designation source is the deferred SRS-EXE-001 / SRS-RESV-*
//!     runtime. Omitting `--live` means "no strategy is live";
//!     `--degraded-live-probe` simulates an unreadable live registry (the gate
//!     must refuse, fail closed).
//!   * `--state <path>` is this bin's durable demonstration port (a validated
//!     snapshot file, written scratch → fsync → atomic rename): the durable
//!     registry store behind the dashboard / REST readers is deferred.

use atp_orchestrator::{
    DeployedVersionRegistry, HotSwapSideEffectError, LiveStrategyProbe, RetainedVersions,
    RetainingVersionRegistry, RollbackConfirmation, StrategyOrchestrator,
};
use atp_types::{DeployedVersion, LiveStrategyState, SourceHash, StrategyId};
use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

/// Fixed demonstration observation timestamp (wall-clock time is intentionally
/// not read — the tool is deterministic). `--observed-at` overrides it so a
/// multi-invocation walk can keep an ordered audit trail.
const OBSERVED_AT_SECONDS: u64 = 1_715_000_000;

/// Magic header of the state snapshot, so a foreign / truncated file is
/// rejected before any field is parsed (fail closed, never an empty registry).
const STATE_MAGIC: &str = "ORCH005-ROLLBACK-STATE v1";

const USAGE: &str = "\
orch005_rollback_cli — SRS-ORCH-005 rollback to the previous deployed strategy version

USAGE:
    orch005_rollback_cli <SUBCOMMAND> --state <path> [FLAGS]

SUBCOMMANDS:
    record      Record a deployment (the SYS-80 retention write: the replaced
                version becomes the retained previous). Creates the state file
                on first use.
    show        Print a strategy's current + retained previous version.
    rollback    Restore the retained previous version. The target hash must
                NAME that exact version; rollback of the LIVE strategy requires
                --acknowledge (the NFR-S2 confirmation control, matching live
                promotion). Fails closed (nonzero exit, state unchanged) on any
                refusal.
    help        Print this help.

COMMON FLAGS:
    --state <path>          the durable state snapshot (validated on load;
                            written scratch -> fsync -> atomic rename)
    --strategy <id>         the strategy id (required)
    --observed-at <ts>      deployment timestamp override (default fixed,
                            deterministic)

record FLAGS:
    --hash sha256:<64hex>   the deployed source hash (required; validated)

rollback FLAGS:
    --target sha256:<64hex> the version to roll back to — must equal the
                            retained previous version's hash (required)
    --live <id>             demonstration live-strategy id (omitted = none is
                            live; the real source is deferred SRS-EXE-001/RESV)
    --degraded-live-probe   simulate an unreadable live registry (must refuse)
    --acknowledge <phrase>  the operator acknowledgement minted into the
                            strategy-bound RollbackConfirmation (required when
                            the strategy is live)
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("orch005_rollback_cli: {err}");
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
        "record" => cmd_record(rest),
        "show" => cmd_show(rest),
        "rollback" => cmd_rollback(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

fn wants_help(args: &[String]) -> bool {
    args.iter()
        .any(|arg| matches!(arg.as_str(), "help" | "--help" | "-h"))
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

fn cmd_record(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let parsed = ParsedArgs::parse(rest)?;
    let state_path = parsed.require_state()?;
    let strategy = parsed.require_strategy()?;
    let hash = parsed
        .hash
        .clone()
        .ok_or("missing required --hash".to_string())?;
    SourceHash::validate_str(&hash).map_err(|violation| {
        format!("--hash is not a valid source hash: {}", violation.as_str())
    })?;

    // `record` creates the state file on first use; every other subcommand
    // requires it to exist (a missing snapshot is data absence, fail closed).
    let registry = if state_path.exists() {
        load_state(&state_path)?
    } else {
        RetainingVersionRegistry::new()
    };
    let id = StrategyId::new(strategy.clone());
    let version = DeployedVersion::new(SourceHash::new(hash), parsed.observed_at);
    registry
        .record(&id, version)
        .map_err(|error| error.to_string())?;
    save_state(&state_path, &registry)?;

    let retained = registry
        .retained(&id)
        .map_err(|error| error.to_string())?
        .expect("the record just inserted this strategy");
    println!("strategy:{strategy}");
    println!("current:{}", retained.current.version_identifier());
    println!("previous:{}", previous_identifier(&retained));
    println!("retained-previous:{}", retained.previous.is_some());
    Ok(())
}

fn cmd_show(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let parsed = ParsedArgs::parse(rest)?;
    let state_path = parsed.require_state()?;
    let strategy = parsed.require_strategy()?;
    let registry = load_state(&state_path)?;
    let retained = registry
        .retained(&StrategyId::new(strategy.clone()))
        .map_err(|error| error.to_string())?
        .ok_or_else(|| format!("strategy '{strategy}' has no recorded deployment"))?;
    println!("strategy:{strategy}");
    println!("current:{}", retained.current.version_identifier());
    println!("previous:{}", previous_identifier(&retained));
    Ok(())
}

fn cmd_rollback(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let parsed = ParsedArgs::parse(rest)?;
    let state_path = parsed.require_state()?;
    let strategy = parsed.require_strategy()?;
    let target = parsed
        .target
        .clone()
        .ok_or("missing required --target".to_string())?;

    let registry = load_state(&state_path)?;
    let id = StrategyId::new(strategy.clone());

    // The demonstration live probe (see the module honesty notes).
    let probe = FixedLiveProbe {
        live: parsed.live.clone(),
        degraded: parsed.degraded_live_probe,
    };

    // The operator acknowledgement — required by the GATE only when the
    // strategy is live; passing it for a paper rollback is harmless (ignored).
    let confirmation = match parsed.acknowledge.as_deref() {
        None => None,
        Some(phrase) => Some(
            RollbackConfirmation::from_operator(id.clone(), phrase)
                .map_err(|error| error.to_string())?,
        ),
    };

    let outcome = StrategyOrchestrator
        .rollback(
            id,
            SourceHash::new(target),
            confirmation,
            &registry,
            &probe,
            parsed.observed_at,
        )
        .map_err(|error| error.to_string())?;
    // The gate's write landed in the in-memory registry; persist the snapshot.
    // A persist failure after a successful gate write must still fail the
    // command (the state file IS the durable record).
    save_state(&state_path, &registry)?;

    println!("strategy:{}", outcome.strategy_id.as_str());
    println!("rolled-back-from:{}", outcome.rolled_back_from.as_str());
    println!(
        "rolled-back-to:{}",
        outcome.rolled_back_to.version_identifier()
    );
    println!("was-live:{}", outcome.was_live);
    Ok(())
}

fn previous_identifier(retained: &RetainedVersions) -> String {
    retained
        .previous
        .as_ref()
        .map(DeployedVersion::version_identifier)
        .unwrap_or_else(|| "-".to_string())
}

// --------------------------------------------------------------------------- //
// Durable state snapshot (the bin's demonstration port)
// --------------------------------------------------------------------------- //
//
// Format (deterministic, strategy-id-sorted; one strategy per line):
//   ORCH005-ROLLBACK-STATE v1
//   <strategy_id>\t<current_hash>\t<current_ts>\t<prev_hash|->\t<prev_ts|->
//
// Load is FAIL CLOSED: a missing magic, wrong field count, malformed hash,
// non-numeric timestamp, duplicate strategy line, or an empty strategy id
// refuses the whole file — a tampered snapshot must never silently read as
// "no previous version" (that would let a rollback misfire).

fn load_state(path: &Path) -> Result<RetainingVersionRegistry, String> {
    let content = fs::read_to_string(path)
        .map_err(|error| format!("cannot read state file {}: {error}", path.display()))?;
    let mut lines = content.lines();
    match lines.next() {
        Some(line) if line == STATE_MAGIC => {}
        _ => {
            return Err(format!(
            "state file {} is not an {STATE_MAGIC} snapshot (refusing a foreign/truncated file)",
            path.display()
        ))
        }
    }
    let registry = RetainingVersionRegistry::new();
    let mut seen: Vec<String> = Vec::new();
    for (index, line) in lines.enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let fields: Vec<&str> = line.split('\t').collect();
        let [strategy, current_hash, current_ts, prev_hash, prev_ts] = fields.as_slice() else {
            return Err(format!(
                "state file {} line {} is malformed (expected 5 tab-separated fields)",
                path.display(),
                index + 2
            ));
        };
        if strategy.trim().is_empty() {
            return Err(format!(
                "state file {} line {} has an empty strategy id",
                path.display(),
                index + 2
            ));
        }
        if seen.contains(&strategy.to_string()) {
            return Err(format!(
                "state file {} carries strategy '{strategy}' twice (refusing an ambiguous snapshot)",
                path.display()
            ));
        }
        seen.push(strategy.to_string());
        let current = parse_version(current_hash, current_ts, path, index)?;
        let previous = if *prev_hash == "-" && *prev_ts == "-" {
            None
        } else {
            Some(parse_version(prev_hash, prev_ts, path, index)?)
        };
        registry
            .seed(
                &StrategyId::new(*strategy),
                RetainedVersions { current, previous },
            )
            .map_err(|error| error.to_string())?;
    }
    Ok(registry)
}

fn parse_version(
    hash: &str,
    ts: &str,
    path: &Path,
    index: usize,
) -> Result<DeployedVersion, String> {
    SourceHash::validate_str(hash).map_err(|violation| {
        format!(
            "state file {} line {} carries an invalid source hash: {}",
            path.display(),
            index + 2,
            violation.as_str()
        )
    })?;
    let deployed_at = ts.parse::<u64>().map_err(|_| {
        format!(
            "state file {} line {} carries a non-numeric timestamp '{ts}'",
            path.display(),
            index + 2
        )
    })?;
    Ok(DeployedVersion::new(SourceHash::new(hash), deployed_at))
}

/// Process-local scratch-file disambiguator (combined with the pid for
/// cross-process uniqueness). Affects only the scratch NAME, never the
/// persisted bytes — the tool stays deterministic.
static SCRATCH_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// Persist the snapshot durably: a UNIQUE scratch file (`<state>.tmp.<pid>.<seq>`,
/// so two concurrent invocations can never create/truncate each other's scratch),
/// fsynced, then atomically renamed onto the state path, then a parent-directory
/// fsync (the rename is a directory-entry change; a crash right after it must not
/// roll the publish back). This is the repo's durable-file pattern
/// (crates/atp-simulation/src/backtest_store.rs::save_to_path). Guarantee
/// scope: last-publish-wins between genuinely concurrent writers — the
/// single-logical-writer coordination (a lock) belongs to the deferred durable
/// registry store (rollback_contract.deferred[]).
fn save_state(path: &Path, registry: &RetainingVersionRegistry) -> Result<(), String> {
    let mut body = String::from(STATE_MAGIC);
    body.push('\n');
    for (strategy, retained) in registry
        .entries_sorted()
        .map_err(|error| error.to_string())?
    {
        // Write-side validation is a SUPERSET of what the loader refuses, so an
        // exit-0 save can never produce a snapshot a later load rejects (a
        // success-acknowledged write must not brick the durable record).
        if strategy.trim().is_empty() {
            return Err(
                "strategy id is empty; refusing to write a snapshot the loader would refuse"
                    .to_string(),
            );
        }
        if strategy.contains('\t') || strategy.contains('\n') {
            return Err(format!(
                "strategy id {strategy:?} contains a field separator; refusing to write an \
                 unparseable snapshot"
            ));
        }
        let (prev_hash, prev_ts) = match &retained.previous {
            None => ("-".to_string(), "-".to_string()),
            Some(previous) => (
                previous.source_hash.as_str().to_string(),
                previous.deployed_at_seconds.to_string(),
            ),
        };
        body.push_str(&format!(
            "{strategy}\t{}\t{}\t{prev_hash}\t{prev_ts}\n",
            retained.current.source_hash.as_str(),
            retained.current.deployed_at_seconds,
        ));
    }
    let seq = SCRATCH_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let scratch = path.with_extension(format!("tmp.{}.{seq}", std::process::id()));
    {
        let mut file = fs::File::create(&scratch).map_err(|error| {
            format!("cannot create scratch file {}: {error}", scratch.display())
        })?;
        if let Err(error) = file
            .write_all(body.as_bytes())
            .and_then(|()| file.sync_all())
        {
            let _ = fs::remove_file(&scratch);
            return Err(format!(
                "cannot write scratch file {}: {error}",
                scratch.display()
            ));
        }
    }
    fs::rename(&scratch, path).map_err(|error| {
        let _ = fs::remove_file(&scratch);
        format!(
            "cannot publish state file {} (rename from scratch): {error}",
            path.display()
        )
    })?;
    // fsync the parent directory so the rename itself is durable.
    let parent = path.parent().filter(|p| !p.as_os_str().is_empty());
    let dir_handle = fs::File::open(parent.unwrap_or_else(|| Path::new(".")))
        .map_err(|error| format!("cannot open state directory for fsync: {error}"))?;
    dir_handle
        .sync_all()
        .map_err(|error| format!("cannot fsync state directory: {error}"))
}

// --------------------------------------------------------------------------- //
// Concrete demonstration ports
// --------------------------------------------------------------------------- //

struct FixedLiveProbe {
    live: Option<String>,
    degraded: bool,
}

impl LiveStrategyProbe for FixedLiveProbe {
    fn current_live(&self) -> Result<Option<LiveStrategyState>, HotSwapSideEffectError> {
        if self.degraded {
            return Err(HotSwapSideEffectError::new(
                "live registry unreachable (simulated via --degraded-live-probe)",
            ));
        }
        Ok(self.live.as_ref().map(|id| LiveStrategyState {
            strategy_id: StrategyId::new(id.clone()),
            drawdown_bps: 0,
        }))
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing (fail closed: unknown / duplicate / valueless flags)
// --------------------------------------------------------------------------- //

struct ParsedArgs {
    state: Option<PathBuf>,
    strategy: Option<String>,
    hash: Option<String>,
    target: Option<String>,
    live: Option<String>,
    degraded_live_probe: bool,
    acknowledge: Option<String>,
    observed_at: u64,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs {
            state: None,
            strategy: None,
            hash: None,
            target: None,
            live: None,
            degraded_live_probe: false,
            acknowledge: None,
            observed_at: OBSERVED_AT_SECONDS,
        };
        let mut observed_at_set = false;
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--state" => set_once_path(&mut parsed.state, take_value(&mut iter, flag)?, flag)?,
                "--strategy" => set_once(&mut parsed.strategy, take_value(&mut iter, flag)?, flag)?,
                "--hash" => set_once(&mut parsed.hash, take_value(&mut iter, flag)?, flag)?,
                "--target" => set_once(&mut parsed.target, take_value(&mut iter, flag)?, flag)?,
                "--live" => set_once(&mut parsed.live, take_value(&mut iter, flag)?, flag)?,
                "--degraded-live-probe" => {
                    if parsed.degraded_live_probe {
                        return Err(format!("duplicate flag '{flag}'"));
                    }
                    parsed.degraded_live_probe = true;
                }
                "--acknowledge" => {
                    set_once(&mut parsed.acknowledge, take_value(&mut iter, flag)?, flag)?
                }
                "--observed-at" => {
                    if observed_at_set {
                        return Err(format!("duplicate flag '{flag}'"));
                    }
                    observed_at_set = true;
                    let raw = take_value(&mut iter, flag)?;
                    parsed.observed_at = raw.parse::<u64>().map_err(|_| {
                        format!("{flag} expects a non-negative integer, got '{raw}'")
                    })?;
                }
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn require_state(&self) -> Result<PathBuf, String> {
        self.state
            .clone()
            .ok_or("missing required --state".to_string())
    }

    fn require_strategy(&self) -> Result<String, String> {
        // Reject an empty/whitespace id AT PARSE, mirroring the loader's refusal —
        // an exit-0 command must never write (or address) a snapshot entry the
        // loader would refuse.
        match self.strategy.as_deref().map(str::trim) {
            None => Err("missing required --strategy".to_string()),
            Some("") => Err("--strategy must not be empty".to_string()),
            Some(_) => Ok(self.strategy.clone().expect("checked above")),
        }
    }
}

fn set_once(slot: &mut Option<String>, value: String, flag: &str) -> Result<(), String> {
    if slot.is_some() {
        return Err(format!("duplicate flag '{flag}'"));
    }
    *slot = Some(value);
    Ok(())
}

fn set_once_path(slot: &mut Option<PathBuf>, value: String, flag: &str) -> Result<(), String> {
    if slot.is_some() {
        return Err(format!("duplicate flag '{flag}'"));
    }
    *slot = Some(PathBuf::from(value));
    Ok(())
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    match iter.next() {
        Some(value) if !value.starts_with("--") => Ok(value.clone()),
        _ => Err(format!("flag '{flag}' expects a value")),
    }
}
