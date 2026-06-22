//! SRS-DATA-017 cross-process Load-test fixture (NOT a product binary).
//!
//! It exists only so the gated `tests/integration` load test can prove, across real OS processes,
//! that reader processes read completed data while an ingestion job GENUINELY holds the single-writer
//! `StoreLock` mid load-modify-save — i.e. the read-during-an-active-write window is real, reads are
//! non-blocking, and the in-progress (uncommitted) write is invisible to readers (snapshot isolation).
//!
//! Unlike the parent-side "the subprocess is alive" heuristic (which brackets process startup +
//! teardown, not the lock-held window), this fixture opens a KNOWN, file-coordinated window: it
//! acquires the lock, performs the in-memory modify, writes a `--ready-file` to signal "I hold the
//! lock and am mid-write", waits for a `--release-file` to appear, and only THEN commits (save) and
//! releases the lock. The wait is bounded so a forgotten release can never hang CI.
//!
//! Usage:
//!     data017_lock_holder --dir <D> --ready-file <R> --release-file <F> [--event-ts <T>]

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use atp_data::store::{fixture_batch, DatasetKind, MarketDataStore, StoreLock};

/// A fixed event timestamp distinct from the test seed (SEED_TS = 1_700_000_000), so the holder's
/// batch is new keys that become visible only after it commits.
const HOLDER_EVENT_TS: i64 = 1_700_000_500;
/// Bound on how long the holder will wait for the release signal before auto-releasing (so a test bug
/// can never leave the lock held forever / hang CI).
const MAX_HOLD: Duration = Duration::from_secs(60);

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data017_lock_holder: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    let mut dir: Option<String> = None;
    let mut ready_file: Option<String> = None;
    let mut release_file: Option<String> = None;
    let mut event_ts: i64 = HOLDER_EVENT_TS;

    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--dir" => dir = args.next(),
            "--ready-file" => ready_file = args.next(),
            "--release-file" => release_file = args.next(),
            "--event-ts" => {
                event_ts = args
                    .next()
                    .and_then(|v| v.parse().ok())
                    .ok_or("--event-ts expects an integer")?;
            }
            other => return Err(format!("unknown flag '{other}'")),
        }
    }
    let dir = PathBuf::from(dir.ok_or("missing --dir")?);
    let ready_file = PathBuf::from(ready_file.ok_or("missing --ready-file")?);
    let release_file = PathBuf::from(release_file.ok_or("missing --release-file")?);

    // Acquire the single-writer lock and BEGIN a load-modify-save: the modify is in memory, the commit
    // (save) is deferred until release, so the lock is genuinely HELD across the whole window below.
    let _lock = StoreLock::acquire(&dir).map_err(|err| err.to_string())?;
    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    for record in fixture_batch(DatasetKind::DailyEquityBar, event_ts) {
        store.upsert(record).map_err(|err| err.to_string())?;
    }

    // Signal readiness: the lock is held and the write is in progress (uncommitted).
    std::fs::write(&ready_file, b"holding\n").map_err(|err| err.to_string())?;

    // Hold the lock until released (bounded — a forgotten release auto-releases rather than hanging).
    let deadline = Instant::now() + MAX_HOLD;
    while !release_file.exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(5));
    }

    // Commit and release (the lock drops at end of scope, AFTER the save).
    store.save_to_path(&dir).map_err(|err| err.to_string())?;
    Ok(())
}
