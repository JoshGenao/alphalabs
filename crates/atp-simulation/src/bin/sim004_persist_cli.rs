//! SRS-SIM-004 paper-state persistence operator CLI.
//!
//! The operator-facing, fault-injection surface of "persist paper strategy simulation state"
//! (docs/SRS.md SRS-5.7 SRS-SIM-004; SyRS SYS-89; StRS SN-1.29 / SN-2.05). The acceptance criterion:
//! "Virtual positions, pending simulated orders, accumulated metrics, and user state are persisted
//! every 60 seconds by default and restored within 30 seconds of container restart, excluding
//! warm-up." The verification method is *fault injection, test*.
//!
//! The persistence codec + atomic on-disk store + restore-deadline enforcement live in
//! [`atp_simulation::paper_state`]; this binary makes them *operator-demonstrable* over a
//! deterministic fixture (two reservoir strategies with real priced fills, per-strategy metric
//! accumulators, and per-strategy JSON user-state dictionaries), the same precedent as the
//! SRS-SIM-001/002/003 CLIs. There is no Python strategy host yet, so the operator workflow is
//! demonstrated over the Rust core.
//!
//! - `persist --dir <path>` — capture the fixture (ledger + metrics + user-state) and atomically
//!   persist it to `<path>`; prints `persisted:true`, the per-sub-state strategy counts, the byte
//!   size, and the store path.
//!
//! - `restore --dir <path>` — recover the snapshot previously persisted to `<path>` (measuring the
//!   restore phase and enforcing the SYS-89 30s deadline), then prove it matches the fixture: prints
//!   `restored:true`, the counts, `restore-elapsed-ms`, `restored-within-deadline:true`, and
//!   `state-matches-capture:true`. Running `persist` then `restore` in two SEPARATE processes proves
//!   the state survives process death (the process-level analog of a container restart).
//!
//! - `roundtrip --dir <path> [--inject <fault>]` — persist then restore in one process. With no
//!   fault, prints `state-survived-restart:true`. With `--inject <fault>`, the fault is applied and
//!   MUST be caught fail closed: the CLI prints `inject=<fault>: fault rejected fail-closed (<err>)`
//!   and exits non-zero with NO survival line. Restore-side faults (a corrupt / truncated / tampered
//!   / missing / over-deadline store) fail on recovery; the non-dictionary user-state fault is
//!   rejected at the save_to_path write boundary (so it never overwrites the last-good store). The
//!   faults are `missing-dir`, `corrupt-file`, `truncated`, `tampered-checksum`,
//!   `deadline-exceeded`, and `non-json-user-state`.
//!
//! Fail closed: an unknown subcommand, flag, or fault, or a missing `--dir`, exits non-zero before
//! any work.

use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

use atp_simulation::paper_metrics::PaperMetricsAccumulator;
use atp_simulation::paper_state::{
    recover_from_path, PaperStateSnapshot, PersistenceConfig, PersistenceError,
};
use atp_simulation::sim::PaperSimulationEngine;
use atp_simulation::virtual_ledger::VirtualLedgerBook;
use atp_types::StrategyId;

const STRATEGY_A: &str = "reservoir-a";
const STRATEGY_B: &str = "reservoir-b";

// A well-formed JSON-object user-state dictionary per strategy (the SYS-89 user-state sub-state).
const USER_STATE_A: &str = r#"{"regime":"trend","lookback":20,"enabled":true}"#;
const USER_STATE_B: &str = r#"{"regime":"meanrev","lookback":5,"enabled":false}"#;
// The non-dictionary value injected by `--inject non-json-user-state` (a JSON array, not an object).
const BAD_USER_STATE: &str = r#"["not","a","dictionary"]"#;

const USAGE: &str = "\
sim004_persist_cli — SRS-SIM-004 paper-state persistence operator workflow

USAGE:
    sim004_persist_cli persist   --dir <path>
    sim004_persist_cli restore   --dir <path>
    sim004_persist_cli roundtrip --dir <path> [--inject <fault>]

COMMANDS:
    persist    Capture the fixture (virtual ledger + accumulated metrics + user-state dictionaries)
               and atomically persist it to <path> (scratch -> fsync -> rename -> dir fsync).
    restore    Recover the snapshot persisted to <path>, enforcing the SYS-89 30s restore deadline,
               and prove the restored state matches the fixture exactly. Run in a SEPARATE process
               from `persist` to prove state survives process death.
    roundtrip  Persist then restore in one process. With --inject, apply a fault between the two and
               prove the restore fails closed.

FLAGS:
    --dir <path>    the store directory (required)
    --inject <f>    (roundtrip only) inject a fault so restore MUST fail closed; one of:
                    missing-dir | corrupt-file | truncated | tampered-checksum |
                    deadline-exceeded | non-json-user-state
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("sim004_persist_cli: {err}");
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
        "persist" => cmd_persist(rest),
        "restore" => cmd_restore(rest),
        "roundtrip" => cmd_roundtrip(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

fn cmd_persist(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest, false)?;
    let snapshot = fixture_snapshot(false)?;
    let bytes = snapshot.serialize().len();
    snapshot
        .save_to_path(&parsed.dir)
        .map_err(|err| format!("persist failed: {err}"))?;

    println!("persisted:true");
    print_counts(&snapshot);
    println!("bytes:{bytes}");
    println!(
        "path:{}",
        PaperStateSnapshot::store_path(&parsed.dir).display()
    );
    Ok(())
}

fn cmd_restore(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest, false)?;
    let outcome = recover_from_path(&parsed.dir).map_err(|err| format!("restore failed: {err}"))?;
    let restored = outcome.snapshot();
    let expected = fixture_snapshot(false)?;

    println!("restored:true");
    print_counts(restored);
    println!(
        "restore-elapsed-ms:{}",
        outcome.restore_elapsed().as_millis()
    );
    // recover_from_path already enforced the SYS-89 deadline (it would have errored otherwise), so
    // reaching here means the restore was within budget.
    println!("restored-within-deadline:true");

    let matches = restored == &expected;
    println!("state-matches-capture:{matches}");
    if !matches {
        return Err(
            "state-matches-capture:false — the restored snapshot differs from the captured fixture \
             (a sub-state was lost, duplicated, or fabricated across the disk round trip)"
                .to_string(),
        );
    }
    Ok(())
}

fn cmd_roundtrip(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest, true)?;
    match parsed.inject {
        Some(fault) => roundtrip_inject(&parsed.dir, fault),
        None => roundtrip_happy(&parsed.dir),
    }
}

/// Persist then restore in one process and prove all three sub-states survived exactly.
fn roundtrip_happy(dir: &Path) -> Result<(), String> {
    let captured = fixture_snapshot(false)?;
    captured
        .save_to_path(dir)
        .map_err(|err| format!("persist failed: {err}"))?;
    let outcome = recover_from_path(dir).map_err(|err| format!("restore failed: {err}"))?;
    let restored = outcome.into_snapshot();
    if restored != captured {
        return Err(
            "state-survived-restart:false — the restored snapshot differs from the captured one"
                .to_string(),
        );
    }
    println!("state-survived-restart:true");
    print_counts(&restored);
    Ok(())
}

/// Apply `fault` between persist and restore and assert the restore fails closed with the fault's
/// expected error, printing no survival line.
fn roundtrip_inject(dir: &Path, fault: Fault) -> Result<(), String> {
    println!("inject:{}", fault.as_str());
    let store = PaperStateSnapshot::store_path(dir);

    let err: PersistenceError = match fault {
        Fault::MissingDir => {
            // Recover from a directory that was never created: fail-closed Io, never empty state.
            let missing = dir.join("definitely-absent-subdir");
            expect_err(recover_from_path(&missing), "missing-dir")?
        }
        Fault::CorruptFile => {
            fixture_snapshot(false)?
                .save_to_path(dir)
                .map_err(|e| format!("setup persist failed: {e}"))?;
            fs::write(&store, b"totally-not-a-snapshot\n")
                .map_err(|e| format!("could not corrupt the store file: {e}"))?;
            expect_err(recover_from_path(dir), "corrupt-file")?
        }
        Fault::Truncated => {
            fixture_snapshot(false)?
                .save_to_path(dir)
                .map_err(|e| format!("setup persist failed: {e}"))?;
            let contents =
                fs::read(&store).map_err(|e| format!("could not read the store: {e}"))?;
            fs::write(&store, &contents[..contents.len() / 2])
                .map_err(|e| format!("could not truncate the store: {e}"))?;
            expect_err(recover_from_path(dir), "truncated")?
        }
        Fault::TamperedChecksum => {
            fixture_snapshot(false)?
                .save_to_path(dir)
                .map_err(|e| format!("setup persist failed: {e}"))?;
            let contents =
                fs::read_to_string(&store).map_err(|e| format!("could not read the store: {e}"))?;
            // Flip a structurally-valid value to another structurally-valid value (the AAPL cost
            // basis 1_000_000 -> 1_000_001, same length, still positive and sign-consistent), so
            // only the integrity checksum can catch it.
            let tampered = contents.replacen("\n1000000\n", "\n1000001\n", 1);
            if tampered == contents {
                return Err(
                    "tampered-checksum setup did not change the snapshot (fixture drift)"
                        .to_string(),
                );
            }
            fs::write(&store, tampered).map_err(|e| format!("could not tamper the store: {e}"))?;
            expect_err(recover_from_path(dir), "tampered-checksum")?
        }
        Fault::DeadlineExceeded => {
            fixture_snapshot(false)?
                .save_to_path(dir)
                .map_err(|e| format!("setup persist failed: {e}"))?;
            // Load the snapshot, then enforce the deadline over a synthetic over-budget restore
            // duration (the real load is sub-millisecond, so this is how the guard is exercised
            // deterministically): the restore MUST fail closed rather than resume.
            let snapshot = PaperStateSnapshot::load_from_path(dir)
                .map_err(|e| format!("setup load failed: {e}"))?;
            let over_budget = Duration::from_secs(snapshot.config().restore_deadline_secs() + 1);
            expect_err(
                snapshot.config().restore_within_deadline(over_budget),
                "deadline-exceeded",
            )?
        }
        Fault::NonJsonUserState => {
            // The write-boundary poison-pill guard: persisting a snapshot whose user-state value
            // is NOT a JSON object is rejected by save_to_path itself, so a bad caller can never
            // atomically overwrite the last-good store with a file recovery would refuse. (First
            // persist a GOOD snapshot so there IS a prior store; it must remain recoverable.)
            fixture_snapshot(false)?
                .save_to_path(dir)
                .map_err(|e| format!("setup persist failed: {e}"))?;
            let err = expect_err(
                fixture_snapshot(true)?.save_to_path(dir),
                "non-json-user-state",
            )?;
            // The prior good store survived the rejected write.
            recover_from_path(dir)
                .map_err(|e| format!("the last-good store was lost after a rejected write: {e}"))?;
            err
        }
    };

    // Assert the fault produced the EXPECTED fail-closed error class (non-vacuity: it did not
    // merely fail for an unrelated reason).
    assert_expected_error(fault, &err)?;
    Err(format!(
        "inject={}: fault rejected fail-closed ({err})",
        fault.as_str()
    ))
}

// --------------------------------------------------------------------------- //
// Fixture
// --------------------------------------------------------------------------- //

/// Build the deterministic fixture snapshot: two reservoir strategies with real priced fills in the
/// virtual ledger, a coherent [`PaperMetricsAccumulator`] each, and a JSON user-state dictionary
/// each. Identical across processes, so `persist` in one process and `restore` in another compare
/// byte-for-byte. When `bad_user_state` is set, strategy A's user-state is a non-object value (a
/// JSON array) so persisting it fails closed at the save_to_path write boundary.
fn fixture_snapshot(bad_user_state: bool) -> Result<PaperStateSnapshot, String> {
    let engine = PaperSimulationEngine::new();
    let a = StrategyId::new(STRATEGY_A);
    let b = StrategyId::new(STRATEGY_B);

    // Virtual ledger: A holds an open AAPL long + a closed MSFT round trip; B holds an AAPL short.
    let mut book = VirtualLedgerBook::new();
    apply(&engine, &mut book, &a, 1, "AAPL", 100, 10_000)?;
    apply(&engine, &mut book, &a, 2, "MSFT", 50, 20_000)?;
    apply(&engine, &mut book, &a, 3, "MSFT", -50, 21_000)?;
    apply(&engine, &mut book, &b, 1, "AAPL", -30, 10_500)?;

    // Accumulated metrics: a coherent fill-then-mark sequence per strategy.
    let mut metrics: HashMap<StrategyId, PaperMetricsAccumulator> = HashMap::new();
    metrics.insert(a.clone(), accumulator_a(&engine)?);
    metrics.insert(b.clone(), accumulator_b(&engine)?);

    // User-state dictionaries (opaque JSON objects).
    let mut user_state: HashMap<StrategyId, String> = HashMap::new();
    let a_state = if bad_user_state {
        BAD_USER_STATE
    } else {
        USER_STATE_A
    };
    user_state.insert(a, a_state.to_string());
    user_state.insert(b, USER_STATE_B.to_string());

    Ok(PaperStateSnapshot::capture_full(
        &book,
        &metrics,
        &user_state,
        &PersistenceConfig::default(),
    ))
}

fn accumulator_a(engine: &PaperSimulationEngine) -> Result<PaperMetricsAccumulator, String> {
    let mut acc = PaperMetricsAccumulator::new(1_000_000).map_err(|e| e.to_string())?;
    acc.apply_fill(&sim(engine, 1, "AAPL", 100, 10_000)?)
        .map_err(|e| e.to_string())?;
    acc.mark(1, &[("AAPL".to_string(), 10_100)])
        .map_err(|e| e.to_string())?;
    acc.apply_fill(&sim(engine, 2, "AAPL", -50, 10_200)?)
        .map_err(|e| e.to_string())?;
    acc.mark(2, &[("AAPL".to_string(), 10_300)])
        .map_err(|e| e.to_string())?;
    Ok(acc)
}

fn accumulator_b(engine: &PaperSimulationEngine) -> Result<PaperMetricsAccumulator, String> {
    let mut acc = PaperMetricsAccumulator::new(500_000).map_err(|e| e.to_string())?;
    acc.apply_fill(&sim(engine, 1, "AAPL", -30, 10_500)?)
        .map_err(|e| e.to_string())?;
    acc.mark(1, &[("AAPL".to_string(), 10_400)])
        .map_err(|e| e.to_string())?;
    Ok(acc)
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

fn sim(
    engine: &PaperSimulationEngine,
    ts: u64,
    symbol: &str,
    quantity: i64,
    price_minor: i64,
) -> Result<atp_simulation::sim::PaperFill, String> {
    engine
        .simulate_fill(ts, symbol, quantity, price_minor, None)
        .map_err(|err| err.to_string())
}

fn apply(
    engine: &PaperSimulationEngine,
    book: &mut VirtualLedgerBook,
    strategy: &StrategyId,
    ts: u64,
    symbol: &str,
    quantity: i64,
    price_minor: i64,
) -> Result<(), String> {
    let fill = sim(engine, ts, symbol, quantity, price_minor)?;
    book.apply_fill(strategy, &fill).map_err(|e| e.to_string())
}

fn print_counts(snapshot: &PaperStateSnapshot) {
    println!("strategies:{}", snapshot.book().strategy_count());
    println!("metrics-strategies:{}", snapshot.metrics().len());
    println!("user-state-strategies:{}", snapshot.user_state().len());
}

/// Turn a `Result` that must be an `Err` into that error, or a descriptive failure if the fault was
/// NOT rejected (a non-vacuity guard: the fault must actually be caught fail-closed).
fn expect_err(
    result: Result<impl std::fmt::Debug, PersistenceError>,
    fault: &str,
) -> Result<PersistenceError, String> {
    match result {
        Err(err) => Ok(err),
        Ok(value) => Err(format!(
            "inject={fault}: expected the operation to fail closed, but it succeeded ({value:?}) — \
             the fault was not rejected (a vacuous fault-injection proof)"
        )),
    }
}

/// Assert the observed error is the fault's expected fail-closed class.
fn assert_expected_error(fault: Fault, err: &PersistenceError) -> Result<(), String> {
    let ok = match fault {
        Fault::MissingDir => matches!(err, PersistenceError::Io { .. }),
        Fault::CorruptFile => matches!(
            err,
            PersistenceError::CorruptSnapshot { .. } | PersistenceError::ChecksumMismatch
        ),
        Fault::Truncated => matches!(
            err,
            PersistenceError::CorruptSnapshot { .. } | PersistenceError::ChecksumMismatch
        ),
        Fault::TamperedChecksum => matches!(err, PersistenceError::ChecksumMismatch),
        Fault::DeadlineExceeded => matches!(err, PersistenceError::RestoreDeadlineExceeded { .. }),
        Fault::NonJsonUserState => matches!(err, PersistenceError::InconsistentField { .. }),
    };
    if ok {
        Ok(())
    } else {
        Err(format!(
            "inject={}: restore failed, but with an unexpected error {err:?} (not the fault's \
             fail-closed class)",
            fault.as_str()
        ))
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fault {
    MissingDir,
    CorruptFile,
    Truncated,
    TamperedChecksum,
    DeadlineExceeded,
    NonJsonUserState,
}

impl Fault {
    fn parse(spec: &str) -> Result<Self, String> {
        match spec {
            "missing-dir" => Ok(Self::MissingDir),
            "corrupt-file" => Ok(Self::CorruptFile),
            "truncated" => Ok(Self::Truncated),
            "tampered-checksum" => Ok(Self::TamperedChecksum),
            "deadline-exceeded" => Ok(Self::DeadlineExceeded),
            "non-json-user-state" => Ok(Self::NonJsonUserState),
            other => Err(format!(
                "unknown fault '{other}' (expected missing-dir|corrupt-file|truncated|\
                 tampered-checksum|deadline-exceeded|non-json-user-state)"
            )),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::MissingDir => "missing-dir",
            Self::CorruptFile => "corrupt-file",
            Self::Truncated => "truncated",
            Self::TamperedChecksum => "tampered-checksum",
            Self::DeadlineExceeded => "deadline-exceeded",
            Self::NonJsonUserState => "non-json-user-state",
        }
    }
}

struct ParsedArgs {
    dir: PathBuf,
    inject: Option<Fault>,
}

impl ParsedArgs {
    fn parse(rest: &[String], allow_inject: bool) -> Result<Self, String> {
        let mut dir: Option<PathBuf> = None;
        let mut inject: Option<Fault> = None;
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--dir" => dir = Some(PathBuf::from(take_value(&mut iter, flag)?)),
                "--inject" if allow_inject => {
                    inject = Some(Fault::parse(&take_value(&mut iter, flag)?)?)
                }
                "--inject" => {
                    return Err(format!("--inject is only valid for `roundtrip`\n\n{USAGE}"))
                }
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        let dir = dir.ok_or_else(|| format!("--dir <path> is required\n\n{USAGE}"))?;
        Ok(Self { dir, inject })
    }
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}
