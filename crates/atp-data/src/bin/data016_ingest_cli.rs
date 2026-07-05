//! SRS-DATA-016 idempotent-ingestion operator CLI (routed through the SRS-DATA-008 tier).
//!
//! The operator-facing workflow that exercises the durable [`MarketDataStore`] + idempotent
//! [`DataLayer::ingest_market_record`] write path the storage substrate ships. There is no
//! Python<->Rust runtime bridge, so this is a small Rust binary in the data crate that demonstrates
//! the SRS-DATA-016 acceptance end to end, driven by deterministic fixture sources that stand in for
//! the four provider adapters (Databento daily, IB minute, option-chain, Sharadar fundamental) —
//! exactly as the verification step permits ("fixture market data, provider mocks, file reads, and
//! persisted output inspection").
//!
//! **All ingestion is SSD-first, then NAS-synced (SRS-DATA-008).** `ingest` routes through the single
//! validated, tiered write surface ([`DataLayer::ingest_market_records_tiered`]): the batch passes the
//! ERR-5 validation gate read-only, then is durably written to the SSD-primary tier (the `--dir` store
//! directory) BEFORE the NAS archival sync — there is no SSD-only ingest path here. The SRS-DATA-016
//! idempotency property (re-ingest is a no-op, the persisted file byte-identical) is proven over the
//! SSD tier by `reingest`.
//!
//! - `ingest   --dir D --kind K [--nas N] [--event-ts T] [--init]` — validate + ingest a fixture batch
//!   SSD-first through `ingest_market_records_tiered` (ERR-5 gate + idempotent `upsert` + NAS sync)
//!   and durably persist. Loads existing history first (load-modify-save), so ingests accumulate.
//!   Exits non-zero on a NAS archival integrity failure (`NasSyncStatus::Failed`).
//! - `reingest --dir D --kind K [--event-ts T]` — re-run the SAME ingestion for the SAME date against
//!   the SSD tier and prove it is a no-op: no duplicate record, and the persisted SSD file is
//!   byte-for-byte identical. A PURE, non-mutating proof — it never writes to disk.
//! - `inspect  --dir D` — load the persisted SSD store and print the total + per-kind record counts
//!   and on-disk byte length, so an operator can confirm a re-ingest did not grow the store.
//!
//! The SSD store directory is resolved fail-closed: an explicit `--dir` wins, else the
//! `ATP_DATA_STORE_DIR` config key (read here as an environment variable — the configuration layer
//! that validates it lives in `python/atp_config`), else an error. The NAS archival tier is `--nas`,
//! else `ATP_NAS_DATA_DIR`, else a `nas` subdirectory of the SSD dir; a missing NAS directory is an
//! unreachable (unmounted) archival tier, so the SSD write still commits and the sync degrades (the
//! documented recoverable outage a later `data008_tier_cli sync` reconciles). Production configures
//! `ATP_NAS_DATA_DIR` to the real NAS mount (SRS-ARCH-004 bind mount); the full SSD/NAS separation is
//! demonstrated with explicit `--ssd`/`--nas` by `data008_tier_cli`.
//!
//! Scope (SRS-DATA-016 closes the idempotency property; SRS-DATA-008 the tiering): the REAL
//! Databento/IB/Sharadar/option-chain network adapters are deferred (SRS-DATA-001/003/005/006);
//! unified query consumers are SRS-DATA-007; the eviction policy that decides WHEN to archive cold
//! data off SSD is SRS-DATA-010 (exercised via `data008_tier_cli`); the validator rule logic +
//! quarantine alert surface are SRS-DATA-013 / SRS-NOTIF-001. This CLI is a single-logical-writer
//! load-modify-save tool, matching the store's documented contract.

use std::env;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use atp_data::store::{fixture_batch, DatasetKind, MarketDataStore, StoreLock, UpsertOutcome};
use atp_data::tiering::{NasSyncStatus, TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
use atp_data::{DataLayer, MarketIngestError, Sys77RecordValidator};

/// The default fixture event timestamp (a fixed epoch second — NOT a clock read, so a re-ingest is
/// deterministic). 2023-11-14T22:13:20Z.
const DEFAULT_EVENT_TS: i64 = 1_700_000_000;

const USAGE: &str = "\
data016_ingest_cli — SRS-DATA-016 idempotent-ingestion operator workflow (SSD-first + NAS sync)

USAGE:
    data016_ingest_cli ingest   --dir <path> --kind <kind> [--nas <path>] [--event-ts <ts>] [--init]
    data016_ingest_cli reingest --dir <path> --kind <kind> [--event-ts <ts>]
    data016_ingest_cli inspect  --dir <path>

The SSD store directory is taken from --dir, else the ATP_DATA_STORE_DIR environment variable. A
missing/unmounted directory fails closed (for every command) rather than masquerading as an empty
catalog; pass --init to provision a brand-new SSD + NAS directory. `ingest` writes SSD-first then
syncs to NAS (--nas, else ATP_NAS_DATA_DIR, else a `nas` subdir of --dir); a missing NAS directory
degrades the sync (the SSD write still commits) rather than failing the ingest.

KINDS:
    daily-equity-bar | minute-equity-bar | option-chain | fundamental | corporate-action-split |
    corporate-action-dividend | corporate-action-delisting | corporate-action-merger |
    corporate-action-symbol-change
    (corporate-action-coverage is NOT ingested here -- the SRS-DATA-011 coverage frontier is a trust
     assertion, asserted only via `data011_coverage_cli assert-coverage --symbol <sym> --through <ts>`)

COMMANDS:
    ingest     Validate + ingest a fixture batch SSD-first, then sync to NAS (load-modify-save).
    reingest   Re-run the SAME ingestion and prove it is a no-op (no duplicate, SSD file byte-identical).
    inspect    Print the total + per-kind record counts and the on-disk byte length.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data016_ingest_cli: {err}");
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
        "ingest" => cmd_ingest(rest),
        "reingest" => cmd_reingest(rest),
        "inspect" => cmd_inspect(rest),
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

/// Validate + ingest a fixture batch SSD-first, then sync to NAS. Routes through the single
/// validated, tiered write surface ([`DataLayer::ingest_market_records_tiered`]): every record flows
/// through the ERR-5 validation gate before the tier's SSD-first durable write and NAS sync, so a
/// record already present is silently skipped (the idempotent no-op) rather than duplicated, and the
/// SSD tier is always committed before the archival tier is touched.
fn cmd_ingest(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let nas = parsed.resolve_nas(&dir);
    let kind = parsed.require_kind()?;
    let event_ts = parsed.event_ts.unwrap_or(DEFAULT_EVENT_TS);

    // Validate the SSD/NAS tier config up front (distinct dirs, ≥90-day floor) before any write.
    let tier = build_tier(dir.clone(), nas)?;
    // --init provisions fresh SSD + NAS directories; without it a missing SSD directory fails closed
    // when the writer lock is acquired below (symmetric with load).
    if parsed.init {
        std::fs::create_dir_all(tier.config().ssd_dir())
            .map_err(|err| format!("creating {}: {err}", dir.display()))?;
        std::fs::create_dir_all(tier.config().nas_dir())
            .map_err(|err| format!("creating {}: {err}", tier.config().nas_dir().display()))?;
    }

    // SSD-FIRST: the durable primary write, serialized behind the single-writer StoreLock held ACROSS
    // the WHOLE load-modify-save (the SRS-DATA-016 idempotency + SRS-DATA-017 writer-serialization
    // contract). Each record flows through the ERR-5 validation gate before it is upserted. The lock is
    // held to the end of this function; the NAS sync below reads the SSD snapshot lock-free.
    let _lock = StoreLock::acquire(&dir).map_err(|err| err.to_string())?;
    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let (inserted, duplicates) = ingest_batch(&store_layer(), &mut store, kind, event_ts)?;
    store.save_to_path(&dir).map_err(|err| err.to_string())?;
    let store_len = store.len();

    // THEN sync the committed SSD snapshot to NAS (the "new data is synced to NAS" half). Best-effort:
    // an unreachable NAS degrades (the SSD write stands, a later `data008_tier_cli sync` reconciles), a
    // reachable-but-broken NAS fails — so there is no SSD-only ingest path.
    let nas_sync = tier.sync_ssd_to_nas_best_effort();

    println!("kind:{}", kind.as_str());
    println!("event_ts:{event_ts}");
    println!("inserted:{inserted}");
    println!("duplicates_skipped:{duplicates}");
    println!("store_len:{store_len}");
    print_nas_sync(&nas_sync);
    println!("store_file:{}", dir.join(store_filename()).display());
    println!(
        "nas_store_file:{}",
        tier.config().nas_dir().join(store_filename()).display()
    );

    // A reachable-but-broken archive (corrupt/conflicting NAS store, lock contention, or an SSD alias)
    // is an INTEGRITY failure: exit NON-ZERO so operator automation gating on exit status cannot
    // mistake it for a clean ingest. A Degraded (NAS-unreachable) outcome stays exit 0 — the
    // documented recoverable outage a later `data008_tier_cli sync` reconciles.
    if let NasSyncStatus::Failed { reason } = &nas_sync {
        return Err(format!(
            "NAS archival sync FAILED ({reason}): the SSD write committed but the archive is not a \
             superset — investigate the NAS store before relying on indefinite retention"
        ));
    }
    Ok(())
}

/// Re-run the SAME ingestion for the SAME date against the SSD tier and prove it is a genuine no-op:
/// no duplicate record is created, and the persisted SSD file is byte-for-byte identical.
///
/// This is a **pure, non-mutating proof**: it NEVER writes to disk. It re-ingests into an in-memory
/// copy of the loaded SSD store and verifies every outcome was an idempotent no-op (`inserted == 0`,
/// the length unchanged) and that the post-reingest store re-serializes to exactly the on-disk bytes.
/// So running `reingest` on the wrong kind/date or a fresh directory fails closed *without* persisting
/// the newly-inserted records — a failed proof can never become a state-changing ingest.
fn cmd_reingest(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let kind = parsed.require_kind()?;
    let event_ts = parsed.event_ts.unwrap_or(DEFAULT_EVENT_TS);

    // Hold the SSD single-writer lock for a consistent snapshot (and fail closed on a missing
    // directory). The proof never saves, so it cannot corrupt the store even though it does not.
    let _lock = StoreLock::acquire(&dir).map_err(|err| err.to_string())?;
    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let store_len_before = store.len();
    let bytes_before = read_store_bytes(&dir)?;

    // Re-ingest into the IN-MEMORY store only — the proof never persists. A genuine no-op leaves the
    // store unchanged, so its serialized form still equals the on-disk bytes; a non-no-op (wrong
    // kind/date, fresh directory) inserts records and is rejected below WITHOUT ever touching disk.
    let (inserted, duplicates) = ingest_batch(&store_layer(), &mut store, kind, event_ts)?;
    let store_len_after = store.len();
    let bytes_identical = store.serialize().into_bytes() == bytes_before;

    println!("kind:{}", kind.as_str());
    println!("event_ts:{event_ts}");
    println!("inserted:{inserted}");
    println!("duplicates_skipped:{duplicates}");
    println!("store_len_before:{store_len_before}");
    println!("store_len_after:{store_len_after}");
    println!("bytes_identical:{bytes_identical}");

    if inserted != 0 || store_len_before != store_len_after || !bytes_identical {
        return Err(format!(
            "re-ingest was NOT idempotent (inserted={inserted}, len {store_len_before}->{store_len_after}, bytes_identical={bytes_identical}); the store was NOT modified"
        ));
    }
    Ok(())
}

/// Load the persisted SSD store and print the total + per-kind record counts and the on-disk byte
/// length, so an operator can confirm a re-ingest did not grow the store.
fn cmd_inspect(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let bytes = read_store_bytes(&dir)?;

    println!("store_len:{}", store.len());
    for kind in DatasetKind::all() {
        println!("count_{}:{}", kind.as_str(), store.count_for_kind(kind));
    }
    println!("store_bytes:{}", bytes.len());
    println!("store_file:{}", dir.join(store_filename()).display());
    Ok(())
}

// --------------------------------------------------------------------------- //
// Ingestion helpers
// --------------------------------------------------------------------------- //

/// Re-ingest the deterministic fixture batch for `kind` on `event_ts` into an IN-MEMORY store (the
/// `reingest` idempotency proof), returning (inserted, duplicates_skipped). Each record flows through
/// the UNCHANGED [`DataLayer::ingest_market_record`] (ERR-5 gate + idempotent `upsert`). A conflicting
/// re-ingest fails closed (propagated as an error).
fn ingest_batch(
    layer: &DataLayer,
    store: &mut MarketDataStore,
    kind: DatasetKind,
    event_ts: i64,
) -> Result<(usize, usize), String> {
    // The real SRS-DATA-013 SYS-77 validator now gates this operator ingest path (was an accept-all
    // stub); a fresh validator per batch keeps its within-batch duplicate detection clean. A record
    // that fails validation is quarantined by the ERR-5 gate and fails the batch closed here — the
    // deterministic fixtures are all well-formed, so this is behaviour-preserving for the demo.
    let validator = Sys77RecordValidator::new();
    let events = NullSink;
    let mut inserted = 0;
    let mut duplicates = 0;
    for record in fixture_batch(kind, event_ts) {
        // The ERR-5 envelope is derived from the record inside ingest_market_record, so validation
        // binds to exactly the record being persisted.
        let outcome = layer
            .ingest_market_record(store, record, &validator, &events, observed_at())
            .map_err(format_ingest_error)?;
        match outcome.applied {
            UpsertOutcome::Inserted => inserted += 1,
            UpsertOutcome::UnchangedDuplicate => duplicates += 1,
        }
    }
    Ok((inserted, duplicates))
}

/// Print the NAS archival sync status of a tiered ingest.
fn print_nas_sync(nas_sync: &NasSyncStatus) {
    match nas_sync {
        NasSyncStatus::Synced { records_added } => {
            println!("nas_sync:synced");
            println!("nas_records_added:{records_added}");
        }
        NasSyncStatus::Degraded { reason } => {
            println!("nas_sync:degraded");
            println!("nas_degraded_reason:{reason}");
        }
        NasSyncStatus::Failed { reason } => {
            println!("nas_sync:failed");
            println!("nas_failed_reason:{reason}");
        }
    }
}

/// Build the validated SSD/NAS tier at the default (floor-enforced ≥90-day) hot-retention window.
fn build_tier(ssd: PathBuf, nas: PathBuf) -> Result<TieredStore, String> {
    let config =
        TierConfig::new(ssd, nas, DEFAULT_HOT_RETENTION_DAYS).map_err(|err| err.to_string())?;
    Ok(TieredStore::new(config))
}

/// A no-op validation event sink (the dashboard/notification fan-out is SRS-DATA-014 / SRS-NOTIF-001).
struct NullSink;

impl atp_data::IngestionValidationEventSink for NullSink {
    fn record(&self, _event: atp_types::IngestionValidationEvent) {}
}

fn store_layer() -> DataLayer {
    DataLayer
}

/// A fixed observation instant for the ERR-5 envelope (NOT a clock read — keeps the demo
/// deterministic).
fn observed_at() -> u64 {
    DEFAULT_EVENT_TS as u64
}

fn format_ingest_error(err: MarketIngestError) -> String {
    err.to_string()
}

// --------------------------------------------------------------------------- //
// Store directory + file helpers
// --------------------------------------------------------------------------- //

/// The persisted store bytes, or an empty vec if no file exists yet (a fresh, provisioned
/// directory). A real I/O failure propagates.
fn read_store_bytes(dir: &Path) -> Result<Vec<u8>, String> {
    let path = dir.join(store_filename());
    match std::fs::read(&path) {
        Ok(bytes) => Ok(bytes),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(err) => Err(format!("reading {}: {err}", path.display())),
    }
}

fn store_filename() -> &'static str {
    atp_data::store::STORE_FILENAME
}

/// Resolve the SSD store directory: explicit `--dir`, else `ATP_DATA_STORE_DIR`, else error.
fn resolve_dir(explicit: Option<&str>) -> Result<PathBuf, String> {
    if let Some(dir) = explicit {
        return Ok(PathBuf::from(dir));
    }
    match env::var("ATP_DATA_STORE_DIR") {
        Ok(dir) if !dir.trim().is_empty() => Ok(PathBuf::from(dir)),
        _ => Err("no store directory: pass --dir <path> or set ATP_DATA_STORE_DIR".to_string()),
    }
}

/// The default NAS archival directory when none is configured: a `nas` subdirectory of the SSD dir.
/// Distinct from the SSD store dir (so the tier's alias guard is satisfied) and co-located so a bare
/// invocation still exercises the tiered write path (an absent dir simply degrades the sync).
fn default_nas_dir(ssd: &Path) -> PathBuf {
    ssd.join("nas")
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct ParsedArgs {
    dir: Option<String>,
    nas: Option<String>,
    kind: Option<DatasetKind>,
    event_ts: Option<i64>,
    init: bool,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--dir" => parsed.dir = Some(take_value(&mut iter, flag)?),
                "--nas" => parsed.nas = Some(take_value(&mut iter, flag)?),
                "--kind" => {
                    let raw = take_value(&mut iter, flag)?;
                    let kind = DatasetKind::from_label(&raw).ok_or_else(|| {
                        format!(
                            "unknown --kind '{raw}' (expected daily-equity-bar | minute-equity-bar | option-chain | fundamental | corporate-action-split | corporate-action-dividend | corporate-action-delisting | corporate-action-merger | corporate-action-symbol-change)"
                        )
                    })?;
                    // Corporate-action COVERAGE is a trust assertion (the SRS-DATA-011 frontier the
                    // split-adjusted gate reads), NOT a market-data fixture. It must be asserted ONLY
                    // through its dedicated operator surface (data011_coverage_cli assert-coverage, with
                    // an explicit --symbol / --through), so this generic market-data ingest CLI refuses
                    // it rather than offering a second, fixture-shaped path to grant coverage.
                    if kind == DatasetKind::CorporateActionCoverage {
                        return Err(
                            "data016_ingest_cli does not ingest 'corporate-action-coverage': the \
                             SRS-DATA-011 coverage frontier is a trust assertion, asserted only via \
                             `data011_coverage_cli assert-coverage --symbol <sym> --through <ts>`"
                                .to_string(),
                        );
                    }
                    parsed.kind = Some(kind);
                }
                "--event-ts" => {
                    let raw = take_value(&mut iter, flag)?;
                    let ts = raw.parse::<i64>().map_err(|_| {
                        format!("--event-ts expects a non-negative integer, got '{raw}'")
                    })?;
                    if ts < 0 {
                        return Err("--event-ts must be non-negative".to_string());
                    }
                    parsed.event_ts = Some(ts);
                }
                "--init" => parsed.init = true,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    /// Resolve the NAS archival tier: --nas, else ATP_NAS_DATA_DIR, else a `nas` subdir of the SSD dir.
    fn resolve_nas(&self, ssd: &Path) -> PathBuf {
        if let Some(nas) = self.nas.as_deref() {
            return PathBuf::from(nas);
        }
        match env::var("ATP_NAS_DATA_DIR") {
            Ok(dir) if !dir.trim().is_empty() => PathBuf::from(dir),
            _ => default_nas_dir(ssd),
        }
    }

    fn require_kind(&self) -> Result<DatasetKind, String> {
        self.kind
            .ok_or_else(|| "missing required --kind".to_string())
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
