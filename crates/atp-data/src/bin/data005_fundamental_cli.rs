//! SRS-DATA-005 Sharadar fundamental-ingestion operator CLI.
//!
//! The operator-facing workflow that exercises the SRS-DATA-005 fundamental ingestion path end to
//! end over the *public* data-layer API, driven by deterministic vendor-neutral
//! [`atp_types::FundamentalStatements`] fixtures that stand in for the real Sharadar provider adapter
//! (deferred — the `atp-adapters` `FundamentalDataProvider` stays a `not_configured` stub), exactly
//! as the verification step permits ("fixture market data, provider mocks, file reads, and persisted
//! output inspection"):
//!
//! - `ingest   --dir D [--nas N] [--event-ts T] [--available-ts A] [--init]` — build the four canonical
//!   `Fundamental` records (income / balance / cashflow / ratios) for each fixture symbol via
//!   [`atp_data::fundamentals::build_fundamental_records`] and ingest them through the single
//!   validated, tiered write surface `DataLayer::ingest_market_records_tiered` (the ERR-5 validation
//!   gate and idempotent `upsert`, written SSD-first then synced to NAS — SRS-DATA-008), then durably
//!   persist. Loads existing history first (load-modify-save), so ingests accumulate.
//! - `reingest --dir D [--event-ts T] [--available-ts A]` — re-run the SAME ingestion and prove it is
//!   a no-op (no duplicate record, the persisted file byte-for-byte identical). A pure, non-mutating
//!   proof: it NEVER writes to disk, so a failed proof can never become a state-changing ingest.
//! - `inspect  --dir D` — print the total + per-fundamental-resolution record counts and the on-disk
//!   byte length, so an operator can confirm a re-ingest did not grow the store.
//! - `factor-input --dir D --symbol S --as-of TS` — read the point-in-time `fundamental:ratios`
//!   record back through the source-neutral unified query (by symbol / resolution, NO provider named —
//!   the SRS-DATA-007 read surface the factor pipeline uses) and print the derived `earnings_yield` /
//!   `book_to_price`, demonstrating the fundamentals are *available to the factor pipeline*. The
//!   AUTHORITATIVE loader read is `atp_factor_pipeline::store_inputs::load_fundamental_input`, which
//!   the SRS-DATA-005 integration test drives; this command mirrors its selection (latest statement
//!   whose `available_ts <= as_of`) for an operator-runnable demonstration.
//!
//! Stays passes:false: the REAL Sharadar network adapter (live API auth/fetch) and the NFR-P8d
//! overnight-window (16:00→09:30 ET) wall-clock completion proof over the full US-equity universe are
//! deferred; fixture sources + a deterministic clock stand in for the mapping / validation / catalog /
//! availability logic. The SSD store directory resolves fail-closed: explicit `--dir`, else the
//! `ATP_DATA_STORE_DIR` config key, else an error. The NAS archival tier is `--nas`, else
//! `ATP_NAS_DATA_DIR`, else a `nas` subdirectory of the SSD dir (a missing NAS directory degrades the
//! sync — the SSD write still commits); production configures `ATP_NAS_DATA_DIR` to the real mount.

use std::env;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use atp_data::fundamentals::{build_fundamental_records, FUNDAMENTAL_RATIOS_RESOLUTION};
use atp_data::query::UnifiedHistoricalQuery;
use atp_data::store::{DatasetKind, MarketDataRecord, MarketDataStore, StoreLock, UpsertOutcome};
use atp_data::tiering::{NasSyncStatus, TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
use atp_data::{DataLayer, MarketIngestError, Sys77RecordValidator};
use atp_types::{AssetClass, FundamentalStatements, SecurityKey};

/// The default fixture fiscal period-end timestamp (a fixed epoch second — NOT a clock read, so a
/// re-ingest is deterministic). 2023-11-14T22:13:20Z.
const DEFAULT_EVENT_TS: i64 = 1_700_000_000;
/// The default availability (filing) offset past the period end — 45 days, a realistic filing lag.
const DEFAULT_AVAILABLE_OFFSET: i64 = 45 * 86_400;

/// The four fundamental statement resolutions, for inspect counts.
const FUNDAMENTAL_RESOLUTIONS: [&str; 4] = [
    "fundamental:income",
    "fundamental:balance",
    "fundamental:cashflow",
    "fundamental:ratios",
];

const USAGE: &str = "\
data005_fundamental_cli — SRS-DATA-005 Sharadar fundamental-ingestion operator workflow

USAGE:
    data005_fundamental_cli ingest       --dir <path> [--nas <path>] [--event-ts <ts>] [--available-ts <ts>] [--init]
    data005_fundamental_cli reingest     --dir <path> [--event-ts <ts>] [--available-ts <ts>]
    data005_fundamental_cli inspect      --dir <path>
    data005_fundamental_cli factor-input --dir <path> --symbol <sym> --as-of <ts>

The SSD store directory is taken from --dir, else the ATP_DATA_STORE_DIR environment variable. A
missing/unmounted directory fails closed (for every command) rather than masquerading as an empty
catalog; pass --init to provision a brand-new SSD + NAS directory. `ingest` writes SSD-first then syncs
to NAS (--nas, else ATP_NAS_DATA_DIR, else a `nas` subdir of --dir); a missing NAS directory degrades
the sync (the SSD write still commits) rather than failing the ingest.

COMMANDS:
    ingest        Ingest the fixture fundamental bundle (income/balance/cashflow/ratios per symbol).
    reingest      Re-run the SAME ingestion and prove it is a no-op (no duplicate, file byte-identical).
    inspect       Print the total + per-fundamental-resolution record counts and on-disk byte length.
    factor-input  Read the point-in-time fundamental:ratios record and print the derived factor inputs.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data005_fundamental_cli: {err}");
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
        "factor-input" => cmd_factor_input(rest),
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

/// Ingest the fixture fundamental bundle, loading any existing history first so an ingest never
/// clobbers previously persisted records. Each record flows through `DataLayer::ingest_market_record`
/// (the ERR-5 validation gate followed by the idempotent `upsert`), so a record already present is
/// silently skipped (the idempotent no-op) rather than duplicated.
fn cmd_ingest(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let ssd = resolve_dir(parsed.dir.as_deref())?;
    let nas = parsed.resolve_nas(&ssd);
    let (event_ts, available_ts) = parsed.timestamps()?;

    // Validate the SSD/NAS tier config up front (distinct dirs, ≥90-day floor) before any write.
    let tier = build_tier(ssd.clone(), nas)?;
    // --init provisions fresh SSD + NAS directories; without it a missing SSD directory fails closed
    // when the writer lock is acquired below (symmetric with load).
    if parsed.init {
        std::fs::create_dir_all(tier.config().ssd_dir())
            .map_err(|err| format!("creating {}: {err}", ssd.display()))?;
        std::fs::create_dir_all(tier.config().nas_dir())
            .map_err(|err| format!("creating {}: {err}", tier.config().nas_dir().display()))?;
    }

    // SSD-FIRST: the durable primary write, serialized behind the single-writer StoreLock held across
    // the WHOLE load-modify-save. Each record flows through the ERR-5 validation gate before it is
    // upserted (`DataLayer::ingest_market_record`).
    let (inserted, duplicates, store_len, count_fundamental) = {
        let _lock = StoreLock::acquire(&ssd).map_err(|err| err.to_string())?;
        let mut store = MarketDataStore::load_from_path(&ssd).map_err(|err| err.to_string())?;
        let (inserted, duplicates) =
            ingest_fixture(&DataLayer, &mut store, event_ts, available_ts)?;
        store.save_to_path(&ssd).map_err(|err| err.to_string())?;
        let count_fundamental = store.count_for_kind(DatasetKind::Fundamental);
        (inserted, duplicates, store.len(), count_fundamental)
        // _lock released on scope exit — the NAS sync below reads the SSD snapshot lock-free.
    };

    // THEN sync the committed SSD snapshot to NAS (the "new data is synced to NAS" half). Best-effort:
    // an unreachable NAS degrades (the SSD write stands), a reachable-but-broken NAS fails — so there
    // is no SSD-only ingest path.
    let nas_sync = tier.sync_ssd_to_nas_best_effort();

    println!("event_ts:{event_ts}");
    println!("available_ts:{available_ts}");
    println!("inserted:{inserted}");
    println!("duplicates_skipped:{duplicates}");
    println!("store_len:{store_len}");
    println!("count_fundamental:{count_fundamental}");
    print_nas_sync(&nas_sync);
    println!("store_file:{}", ssd.join(STORE_FILENAME).display());
    println!(
        "nas_store_file:{}",
        tier.config().nas_dir().join(STORE_FILENAME).display()
    );

    // A reachable-but-broken archive is an INTEGRITY failure: exit non-zero so operator automation
    // cannot mistake it for a clean ingest. A Degraded (NAS-unreachable) outcome stays exit 0.
    if let NasSyncStatus::Failed { reason } = &nas_sync {
        return Err(format!(
            "NAS archival sync FAILED ({reason}): the SSD write committed but the archive is not a \
             superset — investigate the NAS store before relying on indefinite retention"
        ));
    }
    Ok(())
}

/// Re-run the SAME ingestion and prove it is a genuine no-op: no duplicate record is created, and the
/// persisted file is byte-for-byte identical.
///
/// This is a **pure, non-mutating proof**: it NEVER writes to disk. It re-ingests into an in-memory
/// copy of the loaded store and verifies every outcome was an idempotent no-op and that the
/// post-reingest store re-serializes to exactly the on-disk bytes.
fn cmd_reingest(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let (event_ts, available_ts) = parsed.timestamps()?;

    let _lock = StoreLock::acquire(&dir).map_err(|err| err.to_string())?;
    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let store_len_before = store.len();
    let bytes_before = read_store_bytes(&dir)?;

    // Re-ingest into the IN-MEMORY store only — the proof never persists.
    let (inserted, duplicates) = ingest_fixture(&DataLayer, &mut store, event_ts, available_ts)?;
    let store_len_after = store.len();
    let bytes_identical = store.serialize().into_bytes() == bytes_before;

    println!("event_ts:{event_ts}");
    println!("available_ts:{available_ts}");
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

/// Load the persisted store and print the total + per-fundamental-resolution record counts and the
/// on-disk byte length, so an operator can confirm a re-ingest did not grow the store.
fn cmd_inspect(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let bytes = read_store_bytes(&dir)?;

    println!("store_len:{}", store.len());
    println!(
        "count_fundamental:{}",
        store.count_for_kind(DatasetKind::Fundamental)
    );
    for resolution in FUNDAMENTAL_RESOLUTIONS {
        println!(
            "count_{}:{}",
            resolution.replace(':', "_"),
            count_for_resolution(&store, resolution)
        );
    }
    println!("store_bytes:{}", bytes.len());
    println!("store_file:{}", dir.join(STORE_FILENAME).display());
    Ok(())
}

/// Read the point-in-time `fundamental:ratios` record for a symbol as of a date and print the derived
/// factor inputs — the operator-facing demonstration that the ingested fundamentals are *available to
/// the factor pipeline*. Mirrors `load_fundamental_input`'s selection (latest period whose
/// `available_ts <= as_of`); the authoritative read is that loader, driven by the integration test.
fn cmd_factor_input(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let raw_symbol = parsed.require_symbol()?;
    let as_of = parsed.require_as_of()?;

    // Canonicalize the operator symbol through the SAME path as the factor loader
    // (SecurityKey::new -> trim + upper-case), so `--symbol aapl` or `--symbol ' AAPL '` queries the
    // canonical stored identity instead of falsely reporting `available:false` for valid input.
    let symbol = canonical_equity_symbol(&raw_symbol)?;

    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let query = UnifiedHistoricalQuery::new(&symbol, FUNDAMENTAL_RATIOS_RESOLUTION, 0, as_of)
        .with_kind(DatasetKind::Fundamental);
    let result = store.query_unified(&query);

    // Records are event_ts-ascending; select the latest whose availability is at/before as_of.
    let mut chosen: Option<&MarketDataRecord> = None;
    for record in result.records() {
        let event_ts = record.key().event_ts;
        let available_ts = read_field(record, "available_ts")?;
        if available_ts < event_ts {
            return Err(format!(
                "corrupt provenance for {symbol}: available_ts {available_ts} < period end {event_ts}"
            ));
        }
        if available_ts <= as_of {
            chosen = Some(record);
        }
    }

    println!("symbol:{symbol}");
    println!("as_of:{as_of}");
    match chosen {
        None => {
            // An auditable absence — the factor job records a MissingFundamentalData skip.
            println!("available:false");
            Ok(())
        }
        Some(record) => {
            let net_income_minor = read_field(record, "net_income_minor")?;
            let book_equity_minor = read_field(record, "book_equity_minor")?;
            let market_value_minor = read_field(record, "market_value_minor")?;
            if market_value_minor <= 0 {
                return Err(format!(
                    "non-positive market_value_minor ({market_value_minor}) for {symbol}: ratios undefined"
                ));
            }
            let denominator = market_value_minor as f64;
            println!("available:true");
            println!("period_end_ts:{}", record.key().event_ts);
            println!("net_income_minor:{net_income_minor}");
            println!("book_equity_minor:{book_equity_minor}");
            println!("market_value_minor:{market_value_minor}");
            println!("earnings_yield:{}", net_income_minor as f64 / denominator);
            println!("book_to_price:{}", book_equity_minor as f64 / denominator);
            Ok(())
        }
    }
}

// --------------------------------------------------------------------------- //
// Ingestion helpers
// --------------------------------------------------------------------------- //

/// Build the validated SSD/NAS tier at the default (floor-enforced ≥90-day) hot-retention window.
fn build_tier(ssd: PathBuf, nas: PathBuf) -> Result<TieredStore, String> {
    let config =
        TierConfig::new(ssd, nas, DEFAULT_HOT_RETENTION_DAYS).map_err(|err| err.to_string())?;
    Ok(TieredStore::new(config))
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

/// Ingest the deterministic fixture fundamental bundle (all four statements per symbol) into an
/// IN-MEMORY store (the `reingest` idempotency proof), returning (inserted, duplicates_skipped). Each
/// record flows through the UNCHANGED [`DataLayer::ingest_market_record`] (ERR-5 gate + idempotent
/// `upsert`). A conflicting re-ingest fails closed (propagated as an error).
fn ingest_fixture(
    layer: &DataLayer,
    store: &mut MarketDataStore,
    event_ts: i64,
    available_ts: i64,
) -> Result<(usize, usize), String> {
    // The real SRS-DATA-013 SYS-77 validator now gates this operator ingest path (was an accept-all
    // stub). Fundamentals are outside SYS-77's OHLCV/option field rules, so the validator applies only
    // the duplicate check — behaviour-preserving for the well-formed fixtures.
    let validator = Sys77RecordValidator::new();
    let events = NullSink;
    let mut inserted = 0;
    let mut duplicates = 0;
    for statements in fixture_statements(event_ts, available_ts)? {
        let records = build_fundamental_records(&statements).map_err(|err| err.to_string())?;
        for record in records {
            let outcome = layer
                .ingest_market_record(store, record, &validator, &events, observed_at())
                .map_err(format_ingest_error)?;
            match outcome.applied {
                UpsertOutcome::Inserted => inserted += 1,
                UpsertOutcome::UnchangedDuplicate => duplicates += 1,
            }
        }
    }
    Ok((inserted, duplicates))
}

/// Deterministic fixture fundamentals for two symbols with distinct line items so their derived
/// ratios differ. Vendor-neutral (no Sharadar token) — the real Sharadar mapping lives in
/// `atp-adapters` (`SharadarAdapter::map_fundamentals`).
fn fixture_statements(
    event_ts: i64,
    available_ts: i64,
) -> Result<Vec<FundamentalStatements>, String> {
    let rows = [
        // symbol, revenue, net_income, assets, liabilities, book_equity, ncfo, ncfi, ncff, market_value
        (
            "AAPL",
            100_000_000,
            25_000_000,
            80_000_000,
            30_000_000,
            50_000_000,
            30_000_000,
            -10_000_000,
            -5_000_000,
            250_000_000,
        ),
        (
            "MSFT",
            60_000_000,
            18_000_000,
            70_000_000,
            28_000_000,
            42_000_000,
            22_000_000,
            -8_000_000,
            -4_000_000,
            180_000_000,
        ),
    ];
    rows.iter()
        .map(|(sym, rev, ni, assets, liab, eq, ncfo, ncfi, ncff, mv)| {
            FundamentalStatements::new(
                sym,
                event_ts,
                available_ts,
                *rev,
                *ni,
                *assets,
                *liab,
                *eq,
                *ncfo,
                *ncfi,
                *ncff,
                *mv,
            )
            .map_err(|err| err.to_string())
        })
        .collect()
}

fn read_field(record: &MarketDataRecord, name: &str) -> Result<i64, String> {
    record
        .fields()
        .iter()
        .find(|f| f.name == name)
        .map(|f| f.value_minor)
        .ok_or_else(|| format!("fundamental record missing required field '{name}'"))
}

/// Canonicalize an operator-supplied equity symbol through the SAME [`SecurityKey`] path the factor
/// loader uses (trim + upper-case), so a `factor-input` query matches the canonical stored identity
/// (a fundamental record's symbol is normalized at ingest). Fails closed on an empty / unsupported
/// symbol rather than silently querying a non-canonical string and reporting `available:false`.
fn canonical_equity_symbol(raw: &str) -> Result<String, String> {
    SecurityKey::new(raw, AssetClass::Equity)
        .map(|key| key.symbol().to_string())
        .map_err(|err| format!("invalid --symbol '{raw}': {err:?}"))
}

fn count_for_resolution(store: &MarketDataStore, resolution: &str) -> usize {
    // A bounded scan over the catalog — the operator inspect view, not a hot path.
    store
        .records()
        .iter()
        .filter(|r| r.key().kind == DatasetKind::Fundamental && r.key().resolution == resolution)
        .count()
}

/// A no-op validation event sink (the dashboard/notification fan-out is SRS-DATA-014 / SRS-NOTIF-001).
struct NullSink;

impl atp_data::IngestionValidationEventSink for NullSink {
    fn record(&self, _event: atp_types::IngestionValidationEvent) {}
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

const STORE_FILENAME: &str = atp_data::store::STORE_FILENAME;

/// The persisted store bytes, or an empty vec if no file exists yet (a fresh, provisioned directory).
fn read_store_bytes(dir: &Path) -> Result<Vec<u8>, String> {
    let path = dir.join(STORE_FILENAME);
    match std::fs::read(&path) {
        Ok(bytes) => Ok(bytes),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(err) => Err(format!("reading {}: {err}", path.display())),
    }
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
    event_ts: Option<i64>,
    available_ts: Option<i64>,
    symbol: Option<String>,
    as_of: Option<i64>,
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
                "--event-ts" => parsed.event_ts = Some(take_non_negative(&mut iter, flag)?),
                "--available-ts" => parsed.available_ts = Some(take_non_negative(&mut iter, flag)?),
                "--symbol" => parsed.symbol = Some(take_value(&mut iter, flag)?),
                "--as-of" => parsed.as_of = Some(take_non_negative(&mut iter, flag)?),
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

    /// Resolve (event_ts, available_ts) with defaults, failing closed if available_ts < event_ts
    /// (impossible provenance) so a bad operator override is rejected before ingestion.
    fn timestamps(&self) -> Result<(i64, i64), String> {
        let event_ts = self.event_ts.unwrap_or(DEFAULT_EVENT_TS);
        let available_ts = self
            .available_ts
            .unwrap_or(event_ts + DEFAULT_AVAILABLE_OFFSET);
        if available_ts < event_ts {
            return Err(format!(
                "--available-ts ({available_ts}) must be >= --event-ts ({event_ts}): a statement cannot be filed before its period ends"
            ));
        }
        Ok((event_ts, available_ts))
    }

    fn require_symbol(&self) -> Result<String, String> {
        self.symbol
            .clone()
            .ok_or_else(|| "missing required --symbol".to_string())
    }

    fn require_as_of(&self) -> Result<i64, String> {
        self.as_of
            .ok_or_else(|| "missing required --as-of".to_string())
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

fn take_non_negative<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<i64, String> {
    let raw = take_value(iter, flag)?;
    let value = raw
        .parse::<i64>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))?;
    if value < 0 {
        return Err(format!("{flag} must be non-negative"));
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_equity_symbol_matches_the_ingest_normalization() {
        // Fundamental records are stored under the SecurityKey-canonical (trim + upper-case) symbol,
        // so a lowercase / padded operator query must canonicalize identically or `factor-input`
        // would falsely report `available:false` for valid input.
        assert_eq!(canonical_equity_symbol("aapl").unwrap(), "AAPL");
        assert_eq!(canonical_equity_symbol("  aapl  ").unwrap(), "AAPL");
        assert_eq!(canonical_equity_symbol("AAPL").unwrap(), "AAPL");
    }

    #[test]
    fn canonical_equity_symbol_rejects_empty() {
        assert!(canonical_equity_symbol("   ").is_err());
    }
}
