//! # `data018_backup_cli` — SRS-DATA-018 scheduled NAS backup + validated recovery
//!
//! The operator surface over [`atp_data::backup`]. Four subcommands:
//!
//! - `status` — is a backup due under the cadence, and what is the current RPO?
//! - `run` — export every NAS unit to the external target, verify each exported copy, and record
//!   the verified ones in the durable ledger.
//! - `verify` — re-check an existing archive without writing to it.
//! - `restore` — recover from the archive into a destination and verify what landed.
//!
//! ## Why this binary lives in `atp-simulation`, not `atp-data`
//!
//! SYS-59 requires backing up *"backtest results"* alongside market data, and proving a
//! `backtest_results.store` is restorable needs `atp-simulation`'s own codec. `atp-data` is a lower
//! layer and must not depend on `atp-simulation`, so a CLI hosted there could only ever check that
//! family's envelope — and `atp_data::backup` deliberately fails closed (`Unverified`) on evidence
//! that weak. Hosting the operator surface here, one layer up, is the composition root that can
//! inject the real decoder: the backup ENGINE stays in `atp-data`, and only the wiring moves.
//! (`data021_paper_corp_action_cli` sets the same precedent.)
//!
//! ## Exit codes are part of the contract
//!
//! A scheduler must be able to tell "backed up and proven" from "ran without proving anything".
//! `run`/`verify`/`restore` exit **non-zero** on any verdict other than
//! [`BackupVerdict::Verified`], and `status` exits non-zero when the RPO is not proven met. An
//! unverified run is a failure to a caller, not a success with a caveat — the whole point of
//! SYS-60 is that an unproven archive must never read as a good one.

use std::env;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use atp_simulation::backtest_store::BacktestResultStore;

use atp_data::backup::{
    discover_unit_names, due, restore, rpo_report, run_backup_locked, verify_archive, BackupConfig,
    BackupLedger, BackupReport, BackupVerdict, ForeignCodecValidator, RestoreReport,
    SyncDurability, UnitKind, UnitReport, DEFAULT_CADENCE_DAYS, RPO_MAX_DAYS,
};

/// The real backtest-results decoder, injected into `atp_data::backup` so a `backtest_results.store`
/// is verified to the same record-level depth as a market-data blob rather than left `Unverified`.
fn backtest_validator() -> ForeignCodecValidator {
    std::sync::Arc::new(|text: &str| {
        BacktestResultStore::restore(text)
            .map(|_| ())
            .map_err(|err| err.to_string())
    })
}

/// The instant to judge against: `--now` when given, otherwise the **real system clock**.
///
/// Deliberately NOT a fixed default constant. Sibling data CLIs use one for a deterministic demo,
/// but this command is a *scheduled job* whose entire contract is time-driven: with a frozen
/// default, a weekly cron entry that omitted `--now` would record the same timestamp forever, see
/// zero elapsed time on the next run, decide no backup was due, and silently stop backing up —
/// breaching the very RPO it exists to enforce. The library half stays clock-free (every entry
/// point takes `now`), so determinism is preserved where it matters and the process boundary is the
/// only place that reads a clock.
fn resolve_now(explicit: Option<i64>) -> Result<i64, String> {
    if let Some(now) = explicit {
        return Ok(now);
    }
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .map_err(|_| {
            "system clock is before the unix epoch; pass --now <ts> explicitly".to_string()
        })
}

const USAGE: &str = "\
data018_backup_cli — SRS-DATA-018 scheduled backup + validated recovery for NAS-stored data

USAGE:
    data018_backup_cli status  [--nas <path>] --target <path> [--cadence-days <d>] [--now <ts>]
    data018_backup_cli run     [--nas <path>] --target <path> [--cadence-days <d>] [--now <ts>] [--force]
    data018_backup_cli verify  --target <path> [--nas <path>]
    data018_backup_cli restore --target <path> --dest <path>

--nas defaults to $ATP_NAS_DATA_DIR. --target is the EXTERNAL backup target (USB drive, secondary
NAS mount, or cloud-archive mount point); it may not be the NAS root nor nested inside it, because a
copy inside the source's failure domain is not a backup.

--cadence-days defaults to 7 (SYS-59 weekly) and may not exceed the SYS-60 RPO ceiling of 7 days: a
cadence longer than the objective could never satisfy it. `run` is a no-op unless a backup is due,
unless --force is given.

Integrity is validated ON COMPLETION by re-reading the EXPORTED bytes through the owning codec
(magic header + checksum, plus a full record-set comparison for market-data blobs) — never by
trusting the copy. Verdicts are tri-state: verified / corrupt / unverified. `unverified` means the
check could not run (unreachable target, or no units found) and is NOT a pass.

`verify` re-reads the archive WITHOUT writing; pass --nas so it can also catch units missing from
the archive entirely. `run` re-verifies the existing archive before honouring the cadence, so a
wiped or rotted target does not hide behind a not-due ledger.

--now defaults to the REAL system clock. Pass it only to pin a deterministic instant in tests: a
frozen default would make a scheduled job see zero elapsed time forever and stop backing up.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(code) => code,
        Err(err) => {
            eprintln!("data018_backup_cli: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<ExitCode, String> {
    let (command, rest) = match args.split_first() {
        Some(parts) => parts,
        None => return Err(format!("missing subcommand\n\n{USAGE}")),
    };
    match command.as_str() {
        "status" => cmd_status(rest),
        "run" => cmd_run(rest),
        "verify" => cmd_verify(rest),
        "restore" => cmd_restore(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(ExitCode::SUCCESS)
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Parsed flags
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct Flags {
    nas: Option<String>,
    target: Option<String>,
    dest: Option<String>,
    cadence_days: Option<u32>,
    now: Option<i64>,
    force: bool,
}

/// Allow-list flag parsing: an unknown flag is an error rather than being ignored, so a typo in a
/// scheduled job's arguments can never silently change what the backup does.
fn parse_flags(rest: &[String]) -> Result<Flags, String> {
    let mut flags = Flags::default();
    let mut iter = rest.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--nas" => flags.nas = Some(take_value(&mut iter, "--nas")?),
            "--target" => flags.target = Some(take_value(&mut iter, "--target")?),
            "--dest" => flags.dest = Some(take_value(&mut iter, "--dest")?),
            "--cadence-days" => {
                let raw = take_value(&mut iter, "--cadence-days")?;
                flags.cadence_days = Some(
                    raw.parse::<u32>()
                        .map_err(|_| format!("--cadence-days expects an integer, got '{raw}'"))?,
                );
            }
            "--now" => {
                let raw = take_value(&mut iter, "--now")?;
                flags.now = Some(
                    raw.parse::<i64>()
                        .map_err(|_| format!("--now expects a unix timestamp, got '{raw}'"))?,
                );
            }
            "--force" => flags.force = true,
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    Ok(flags)
}

fn build_config(flags: &Flags) -> Result<BackupConfig, String> {
    let nas = resolve_dir(flags.nas.as_deref(), "ATP_NAS_DATA_DIR", "--nas")?;
    let target = flags
        .target
        .as_deref()
        .map(PathBuf::from)
        .ok_or_else(|| "no backup target: pass --target <path>".to_string())?;
    let cadence = flags.cadence_days.unwrap_or(DEFAULT_CADENCE_DAYS);
    Ok(BackupConfig::new(nas, target, cadence)
        .map_err(|err| err.to_string())?
        .with_backtest_validator(backtest_validator()))
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

fn cmd_status(rest: &[String]) -> Result<ExitCode, String> {
    let flags = parse_flags(rest)?;
    let config = build_config(&flags)?;
    let now = resolve_now(flags.now)?;
    let ledger = BackupLedger::load(config.target_dir()).map_err(|err| err.to_string())?;
    // Judge against what is on the NAS NOW: a unit that has never been backed up is invisible to
    // the ledger, so a ledger-only assessment would report green while that unit is unprotected.
    let current = discover_unit_names(config.nas_dir()).map_err(|err| err.to_string())?;
    let rpo = rpo_report(&ledger, &current, now);
    let ledger_due = due(&ledger, &config, &current, now);
    // The ledger says a backup happened once; the ARCHIVE is what a recovery would actually read.
    // Media can be wiped, unmounted or rot while backup_ledger.log survives, so reporting "within
    // rpo" on ledger freshness alone would certify a recovery point that no longer exists. Both
    // must hold. A target that is entirely unreachable is reported, not treated as fine.
    let archive = match verify_archive(config.target_dir(), &current, Some(&backtest_validator())) {
        Ok(report) => Some(report),
        Err(err) => {
            eprintln!("data018_backup_cli: archive could not be verified: {err}");
            None
        }
    };
    let archive_ok = archive
        .as_ref()
        .is_some_and(|report| report.verdict().is_verified());

    println!("SRS-DATA-018 backup status (SyRS SYS-59 / SYS-60)");
    println!("  nas source      : {}", config.nas_dir().display());
    println!("  external target : {}", config.target_dir().display());
    println!("  cadence         : {} day(s)", config.cadence_days());
    println!("  rpo ceiling     : {RPO_MAX_DAYS} day(s)");
    // `verified_at` is None for two DIFFERENT reasons and conflating them would misreport the
    // state: nothing has ever been backed up, versus some units are fine but at least one has no
    // recovery point at all (so the archive as a whole has none either).
    match (rpo.verified_at, rpo.age_days, rpo.unbacked_units.is_empty()) {
        (Some(ts), Some(age), _) => println!("  last verified   : {ts} ({age} day(s) ago)"),
        (Some(ts), None, _) => {
            println!("  last verified   : {ts} (AHEAD of --now; clock disagreement)")
        }
        (None, _, true) => {
            println!("  last verified   : never — no unit has ever been proven backed up")
        }
        (None, _, false) => println!(
            "  last verified   : undefined — {} unit(s) below have no verified backup, so the \
             archive has no recovery point",
            rpo.unbacked_units.len()
        ),
    }
    if rpo.unbacked_units.is_empty() {
        println!(
            "  unbacked units  : none ({} unit(s) on the NAS)",
            current.len()
        );
    } else {
        // Name them: "some unit is unprotected" is the single most actionable thing this command
        // can say, and a bare count would leave the operator guessing which data is at risk.
        println!(
            "  unbacked units  : {} NEVER backed up — {}",
            rpo.unbacked_units.len(),
            rpo.unbacked_units.join(", ")
        );
    }
    println!(
        "  ledger freshness: {}",
        if rpo.within_rpo {
            "within rpo"
        } else {
            "STALE"
        }
    );
    println!(
        "  archive verifies: {}",
        match &archive {
            Some(report) => report.verdict().label(),
            None => "unreadable",
        }
    );
    // The headline the operator (and any scheduler gating on this) reads: BOTH halves must hold.
    println!(
        "  within rpo      : {}",
        if rpo.within_rpo && archive_ok {
            "yes"
        } else {
            "NO"
        }
    );
    // A backup is due if the cadence says so OR the archive no longer verifies — reporting "no"
    // while the media is wiped would contradict what `run` actually does in that state, and would
    // read as "nothing to do" at precisely the moment there is everything to do.
    let is_due = ledger_due || !archive_ok;
    println!(
        "  backup due      : {}{}",
        if is_due { "yes" } else { "no" },
        if is_due && !ledger_due {
            " (cadence not reached, but the archive does not verify)"
        } else {
            ""
        }
    );
    if let Some(report) = &archive {
        if !report.verdict().is_verified() {
            println!("  archive detail  :");
            print_units(&report.units);
        }
    }

    Ok(if rpo.within_rpo && archive_ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    })
}

fn cmd_run(rest: &[String]) -> Result<ExitCode, String> {
    let flags = parse_flags(rest)?;
    let config = build_config(&flags)?;
    let now = resolve_now(flags.now)?;
    let ledger = BackupLedger::load(config.target_dir()).map_err(|err| err.to_string())?;

    let current = discover_unit_names(config.nas_dir()).map_err(|err| err.to_string())?;
    if !flags.force && !due(&ledger, &config, &current, now) {
        // The ledger says a backup happened; that is NOT evidence the archive is still there. Media
        // can be wiped, unmounted or rot between runs, and skipping a run on a stale ledger while
        // the archive is empty gives an infinite RPO behind a green status. Re-read the media.
        let archive = verify_archive(config.target_dir(), &current, Some(&backtest_validator()))
            .map_err(|err| err.to_string())?;
        if archive.verdict().is_verified() {
            println!(
                "backup not due under the {} day cadence, and the existing archive re-verified \
                 ({} unit(s)) — pass --force to run anyway",
                config.cadence_days(),
                archive.units.len()
            );
            return Ok(ExitCode::SUCCESS);
        }
        println!("the existing archive no longer verifies — backing up again despite the cadence:");
        print_units(&archive.units);
    }

    // Export + verify + ledger advancement under ONE exclusive lock on the target: two concurrent
    // runs must not interleave such that an older run publishes staler bytes over a newer archive
    // while the newer ledger timestamp survives.
    let (report, _ledger) = run_backup_locked(&config, now).map_err(|err| err.to_string())?;
    print_units(&report.units);
    let verdict = report.verdict();
    print_verdict("backup", verdict, &report);

    Ok(if verdict.is_verified() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    })
}

fn cmd_verify(rest: &[String]) -> Result<ExitCode, String> {
    let flags = parse_flags(rest)?;
    let target = flags
        .target
        .as_deref()
        .map(PathBuf::from)
        .ok_or_else(|| "no backup target: pass --target <path>".to_string())?;
    // Expected contents: the NAS unit set when one is given (the stronger check — it catches units
    // missing from the archive entirely), otherwise whatever the archive itself holds.
    let expected = match flags.nas.as_deref() {
        Some(nas) => discover_unit_names(Path::new(nas)).map_err(|err| err.to_string())?,
        None => discover_unit_names(&target).map_err(|err| err.to_string())?,
    };
    let report = verify_archive(&target, &expected, Some(&backtest_validator()))
        .map_err(|err| err.to_string())?;
    print_units(&report.units);
    let verdict = report.verdict();
    println!("verify verdict: {}", verdict.label());
    Ok(if verdict.is_verified() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    })
}

fn cmd_restore(rest: &[String]) -> Result<ExitCode, String> {
    let flags = parse_flags(rest)?;
    let target = flags
        .target
        .as_deref()
        .map(PathBuf::from)
        .ok_or_else(|| "no backup target: pass --target <path>".to_string())?;
    let dest = flags
        .dest
        .as_deref()
        .map(PathBuf::from)
        .ok_or_else(|| "no restore destination: pass --dest <path>".to_string())?;
    let report: RestoreReport =
        restore(&target, &dest, Some(&backtest_validator())).map_err(|err| err.to_string())?;
    print_units(&report.units);
    let verdict = report.verdict();
    println!("restore verdict: {}", verdict.label());
    Ok(if verdict.is_verified() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    })
}

// --------------------------------------------------------------------------- //
// Rendering + helpers
// --------------------------------------------------------------------------- //

fn print_units(units: &[UnitReport]) {
    if units.is_empty() {
        println!("  (no backup units found — this is UNVERIFIED, not an empty success)");
        return;
    }
    for unit in units {
        print!(
            "  {:<28} {:<17} {:<9} {:<11} {:<13}",
            unit.unit,
            unit.kind.label(),
            unit.status.label(),
            unit.verdict.label(),
            // Show HOW the verdict was reached: a backtest-results blob can only be checked to its
            // envelope, and rendering that identically to a full record-level decode would overstate
            // the evidence behind it.
            unit.verification.label()
        );
        // Distinguish "this codec does not expose a record count" from "we never got far enough to
        // count" — collapsing them would let a failed verification read like a codec limitation.
        match (unit.records, unit.kind) {
            (Some(n), _) => print!(" ({n} records)"),
            (None, UnitKind::BacktestResults) => {
                print!(" (record count not exposed by this codec)")
            }
            (None, UnitKind::MarketData) => {
                print!(" (record count unavailable — verification did not complete)")
            }
        }
        if unit.detail.is_empty() {
            println!();
        } else {
            println!(" — {}", unit.detail);
        }
    }
}

fn print_verdict(label: &str, verdict: BackupVerdict, report: &BackupReport) {
    println!(
        "{label} verdict: {} ({} of {} unit(s) proven)",
        verdict.label(),
        report.verified_units().len(),
        report.units.len()
    );
    // Report the sync barrier SEPARATELY from the verdict, and only when a write happened. The two
    // are different guarantees: the verdict says the exported bytes were read back and matched the
    // source, which no `fsync` return value can tell you; the barrier says the filesystem promised
    // to have flushed them. A target that cannot offer the barrier still yields a real, verified
    // archive — but the operator must be told, because it is their power-loss risk to carry.
    match report.durability {
        Some(SyncDurability::FullSync) => println!("{label} sync    : full-sync"),
        Some(SyncDurability::TargetUnsupported) => println!(
            "{label} sync    : unsupported-by-target — this filesystem implements no sync \
             barrier (typical of SMB/NFS/cloud mounts), so the exported bytes were verified by \
             re-reading them but never explicitly flushed; an OS or power failure during the run \
             could still lose them"
        ),
        None => {}
    }
}

fn resolve_dir(explicit: Option<&str>, env_key: &str, flag: &str) -> Result<PathBuf, String> {
    if let Some(dir) = explicit {
        return Ok(PathBuf::from(dir));
    }
    match env::var(env_key) {
        Ok(dir) if !dir.trim().is_empty() => Ok(PathBuf::from(dir)),
        _ => Err(format!("no directory: pass {flag} <path> or set {env_key}")),
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
