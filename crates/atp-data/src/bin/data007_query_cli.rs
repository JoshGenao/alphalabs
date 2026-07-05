//! SRS-DATA-007 unified historical data access operator CLI.
//!
//! The operator-facing workflow that exercises the [`MarketDataStore::query_unified`] read path the
//! SRS-DATA-007 unified historical interface ships. It loads the durable store the SRS-DATA-016
//! ingestion CLI (`data016_ingest_cli`) persists, then answers a query by **symbol, date range, and
//! resolution — without ever naming the original source provider**, exactly as the acceptance
//! criterion requires. There is no Python<->Rust runtime bridge, so this is a small Rust binary in the
//! data crate that demonstrates the SRS-DATA-007 acceptance end to end over the *public* store API:
//!
//! - `query --dir D --symbol S --resolution R --start T0 --end T1 [--kind K]` — load the persisted
//!   store and print every record matching `(symbol, resolution, [start, end])` (optionally narrowed to
//!   one vendor-neutral [`DatasetKind`]), in deterministic `event_ts`-ascending order. The output is
//!   **source-neutral**: it echoes the queried symbol/resolution/range, a `match_count`, and each
//!   record's `event_ts` + integer-minor value fields — there is no provider/source/vendor line, and no
//!   `--provider`/`--source` flag to give one.
//!
//! The store directory is resolved fail-closed: an explicit `--dir` wins, else the `ATP_DATA_STORE_DIR`
//! config key (read here as an environment variable — the configuration layer that validates it lives
//! in `python/atp_config`), else an error. A misconfigured / unmounted directory surfaces as a store
//! error rather than a silently empty catalog.
//!
//! **Transparent cold-read failover (SRS-DATA-009).** When a NAS archival tier is configured — via the
//! `ATP_NAS_DATA_DIR` config key (so an EXISTING deployment auto-tiers with NO change to the query
//! invocation, exactly like `--dir` resolves from `ATP_DATA_STORE_DIR`) or an explicit `--nas` — the
//! SAME `query`, unchanged in its symbol / resolution / range dimensions, is served transparently
//! across the tiers via [`atp_data::cold_read::TieredReader`]: SSD primary → cold-read cache → NAS for
//! data archived off SSD outside the retention window. So an operator reading a record that
//! [`atp_data::tiering::TieredStore::archive_cold`] moved off SSD gets it back from NAS **without
//! changing the query**, and the NAS result is cached on SSD within the configured share (`--cache-share`
//! % — default 20 — of `--ssd-capacity`, which defaults to a bounded value; `--now` defaults to the
//! system clock). Tiered mode serves `raw` only (a `split-adjusted` / `fully-adjusted` query stays
//! single-tier); the adjusted × cold-read interaction is the SRS-DATA-009 × SRS-DATA-011/012
//! follow-up. With no NAS tier configured the query is a single-directory read, byte-identical to
//! before.
//!
//! Read-path scope: a query is a pure READ over the atomically-published on-disk snapshot
//! ([`MarketDataStore::load_from_path`]). It does NOT acquire the single-writer `StoreLock` — a read
//! does not need it, and `save_to_path` publishes via fsync + atomic rename so a reader never observes a
//! half-written store (the cold-read cache write-back is owned by the `cold_read` library, not this
//! binary). Coordinating concurrent reads *during* an active ingestion write is the deferred owner
//! SRS-DATA-017; the eviction POLICY that drives archival is SRS-DATA-010; the real provider network
//! adapters are SRS-DATA-001/003/005/006 (fixture sources stand in); the in-process Python / factor
//! bindings over this engine are downstream consumers of the same interface.

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use atp_data::cold_read::{ColdReadConfig, TieredReader, DEFAULT_COLD_READ_CACHE_SHARE_PERCENT};
use atp_data::store::{DatasetKind, MarketDataRecord, MarketDataStore};
use atp_data::tiering::{TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
use atp_data::{CorporateActionEvent, UnifiedHistoricalQuery};

/// The normalization mode the operator surface serves. `raw` returns stored values verbatim;
/// `split-adjusted` and `fully-adjusted` (splits AND dividends, SyRS SYS-29) route through the
/// coverage-enforcing gate. `total-return` fails closed at parse time (deferred to SRS-DATA-012).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Normalization {
    Raw,
    SplitAdjusted,
    FullyAdjusted,
}

const USAGE: &str = "\
data007_query_cli — SRS-DATA-007 unified historical data access operator workflow

USAGE:
    data007_query_cli query --dir <path> --symbol <sym> --resolution <res> --start <ts> --end <ts> [--kind <kind>] [--normalization <mode>]
    data007_query_cli query --dir <ssd> --nas <nas> --ssd-capacity <n> --now <ts> [--cache-share <pct>] [--hot-days <days>] --symbol ... (transparent cold-read failover, SRS-DATA-009)

The store directory is taken from --dir, else the ATP_DATA_STORE_DIR environment variable. A
missing/unmounted directory fails closed rather than masquerading as an empty catalog. The query is a
read-only snapshot load (no single-writer lock); ingest first with data016_ingest_cli.

TRANSPARENT COLD-READ FAILOVER (SRS-DATA-009): when a NAS archival tier is configured, the SAME query
is served across the SSD/NAS tiers — records archived off SSD (outside the retention window) are read
from NAS and cached on SSD, capped at --cache-share % (default 20) of --ssd-capacity records — WITHOUT
changing the query invocation. The NAS tier is taken from the ATP_NAS_DATA_DIR config key (an existing
deployment auto-tiers with NO extra flags) or an explicit --nas; --dir is the SSD primary tier. --now
(the retention-boundary instant) defaults to the system clock; --ssd-capacity defaults to a bounded
value (the real capacity is the NFR-SC2 deployment concern). Tiered mode serves 'raw' only (a
split-adjusted query stays single-tier). Without a NAS tier the query is a single-directory read.

The query names NO source provider — it matches purely on symbol, resolution, and the inclusive
[start, end] event-timestamp range. --kind narrows to one vendor-neutral dataset kind (not a provider).

KINDS (optional --kind disambiguator):
    daily-equity-bar | minute-equity-bar | option-chain | fundamental | corporate-action-split |
    corporate-action-dividend | corporate-action-delisting | corporate-action-merger |
    corporate-action-symbol-change

NORMALIZATION (optional --normalization, default raw):
    raw             stored values verbatim
    split-adjusted  bars re-quoted onto a split-comparable basis (SRS-DATA-012 math), served ONLY when
                    corporate-action COVERAGE for the symbol reaches the query end (SRS-DATA-011); it
                    REQUIRES an equity-bar --kind (daily-equity-bar | minute-equity-bar) and fails
                    closed (naming have/need coverage) when the symbol is not covered through --end, so
                    it never emits raw-as-adjusted output. Ingest coverage with data011_coverage_cli.
    fully-adjusted  splits AND dividends (SyRS SYS-29 'fully adjusted'): the same coverage gate and
                    rules as split-adjusted, with pre-ex-date prices additionally back-adjusted by each
                    dividend's (reference close - amount) / reference close; volume is never
                    dividend-scaled. Fails closed on a dividend with no prior close.
    (total-return is deferred: dividend reinvestment + per-subscription selection, SRS-DATA-012)

Both adjusted modes resolve the symbol's RENAME LINEAGE (a query for the current symbol returns its
predecessors' bars relabeled; coverage asserted for the queried symbol governs its whole lineage) and
add these lines: `coverage_through:<D>` (the proven frontier, always >= --end),
`adjusted_through:<ts>` (the basis the bars are quoted on), `event_count:<n>` and `event.<i>.*`
(in-window delistings / mergers / symbol changes a P&L consumer must handle structurally — mark the
position final, convert at the surfaced terms, follow the lineage hop).

COMMANDS:
    query    Print every record matching symbol + resolution + [start, end] (event_ts-ascending).
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data007_query_cli: {err}");
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
        "query" => cmd_query(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Subcommand
// --------------------------------------------------------------------------- //

/// Load the persisted store (read-only snapshot) and print every record matching the unified query.
/// The output is source-neutral: symbol/resolution/range header, a match count, then each record's
/// event_ts + integer-minor value fields — never the provider a record came from.
fn cmd_query(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let symbol = parsed.require_symbol()?;
    let resolution = parsed.require_resolution()?;
    let start = parsed.require_start()?;
    let end = parsed.require_end()?;

    // A READ does not acquire the single-writer StoreLock. load_from_path reads the atomically-published
    // whole-file snapshot, so a reader never observes a half-written store; concurrent-read-DURING-write
    // coordination is the separate deferred SRS-DATA-017.
    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;

    let mut query = UnifiedHistoricalQuery::new(symbol.clone(), resolution.clone(), start, end);
    if let Some(kind) = parsed.kind {
        query = query.with_kind(kind);
    }

    // SRS-DATA-009 transparent cold-read failover: when a NAS archival tier is configured, the SAME
    // query (unchanged symbol/resolution/range) is served across SSD -> cold-read cache -> NAS, so an
    // operator reading a record archived off SSD gets it back from NAS transparently. The NAS tier is
    // resolved from the ENVIRONMENT (ATP_NAS_DATA_DIR) exactly like --dir resolves from
    // ATP_DATA_STORE_DIR, so an existing deployment auto-tiers with NO change to the query invocation;
    // an explicit --nas overrides. --dir is the SSD primary tier here. Adjusted reads are not tiered
    // (the adjusted × cold-read interaction is the SRS-DATA-009 × SRS-DATA-011/012 follow-up), so a
    // split-adjusted / fully-adjusted query with a NAS configured falls through to the single-tier SSD
    // path unchanged.
    if let Some(nas) = resolve_nas(&parsed) {
        if parsed.normalization == Normalization::Raw {
            return cmd_query_tiered(&parsed, dir, nas, &query, &symbol, &resolution, start, end);
        }
    }

    // Resolve the records + printed normalization label + the gated result metadata (the coverage
    // frontier, the adjustment basis, and the surfaced structural events) per mode.
    type GatedMeta = (i64, i64, Vec<CorporateActionEvent>);
    let (records, normalization_label, gated): (Vec<MarketDataRecord>, &str, Option<GatedMeta>) =
        match parsed.normalization {
            // RAW: stored values verbatim over the atomically-published snapshot.
            Normalization::Raw => {
                let matched = store.query_unified(&query);
                let records = matched
                    .records()
                    .iter()
                    .map(|record| (*record).clone())
                    .collect();
                (records, "raw", None)
            }
            // SPLIT-ADJUSTED / FULLY-ADJUSTED: route through the SINGLE coverage-enforcing gate
            // (MarketDataStore::query_split_adjusted / query_fully_adjusted). It fails closed (exit
            // non-zero) on NotCovered (coverage for the symbol does not reach --end), on a
            // missing/non-equity --kind, on inconsistent rename-lineage data, and on a malformed
            // split/dividend -- so this surface never emits raw-as-adjusted output. There is no
            // CLI-side adjustment math; the gate is the only path to adjusted output.
            Normalization::SplitAdjusted => {
                let adjusted = store
                    .query_split_adjusted(&query)
                    .map_err(|err| err.to_string())?;
                (
                    adjusted.records,
                    "split-adjusted",
                    Some((
                        adjusted.coverage_through,
                        adjusted.adjusted_through,
                        adjusted.events,
                    )),
                )
            }
            Normalization::FullyAdjusted => {
                let adjusted = store
                    .query_fully_adjusted(&query)
                    .map_err(|err| err.to_string())?;
                (
                    adjusted.records,
                    "fully-adjusted",
                    Some((
                        adjusted.coverage_through,
                        adjusted.adjusted_through,
                        adjusted.events,
                    )),
                )
            }
        };

    println!("symbol:{symbol}");
    println!("resolution:{resolution}");
    println!("start:{start}");
    println!("end:{end}");
    println!("kind:{}", parsed.kind.map_or("any", |kind| kind.as_str()));
    println!("normalization:{normalization_label}");
    if let Some((coverage_through, adjusted_through, events)) = gated {
        println!("coverage_through:{coverage_through}");
        println!("adjusted_through:{adjusted_through}");
        print_events(&events);
    }
    println!("match_count:{}", records.len());
    print_records(&records);
    Ok(())
}

/// Print the structural corporate-action events a gated read surfaced for the query window — the
/// facts a P&L consumer must handle structurally (mark a delisted position final, convert at the
/// merger terms, follow the lineage hop). Lines never start with `record.`, so record parsers are
/// unaffected; merger-only term lines are omitted for the other kinds.
fn print_events(events: &[CorporateActionEvent]) {
    println!("event_count:{}", events.len());
    for (index, event) in events.iter().enumerate() {
        match event {
            CorporateActionEvent::Delisting {
                symbol,
                effective_ts,
            } => {
                println!("event.{index}.kind:delisting");
                println!("event.{index}.symbol:{symbol}");
                println!("event.{index}.successor:-");
                println!("event.{index}.effective_ts:{effective_ts}");
            }
            CorporateActionEvent::Merger {
                symbol,
                successor,
                numerator,
                denominator,
                cash_per_share_minor,
                effective_ts,
            } => {
                println!("event.{index}.kind:merger");
                println!("event.{index}.symbol:{symbol}");
                println!("event.{index}.successor:{successor}");
                println!("event.{index}.effective_ts:{effective_ts}");
                println!("event.{index}.numerator:{numerator}");
                println!("event.{index}.denominator:{denominator}");
                println!("event.{index}.cash_per_share_minor:{cash_per_share_minor}");
            }
            CorporateActionEvent::SymbolChange {
                predecessor,
                successor,
                effective_ts,
            } => {
                println!("event.{index}.kind:symbol-change");
                println!("event.{index}.symbol:{predecessor}");
                println!("event.{index}.successor:{successor}");
                println!("event.{index}.effective_ts:{effective_ts}");
            }
        }
    }
}

/// SRS-DATA-009 transparent cold-read failover: serve the SAME `query` across the SSD/NAS tiers via
/// [`TieredReader`], reading archived-off records back from NAS and caching them on SSD within the
/// configured share — the record output format is identical to the single-tier path (so a consumer's
/// query shape and result shape are unchanged), plus per-tier provenance lines as inspectable evidence.
/// Tiered mode serves `raw` only (the split-adjusted × cold-read interaction is the SRS-DATA-009 ×
/// SRS-DATA-011/012 follow-up). Durable cache persistence is owned by the `cold_read` library, so this
/// binary itself takes no store lock and never persists directly.
#[allow(clippy::too_many_arguments)]
fn cmd_query_tiered(
    parsed: &ParsedArgs,
    ssd_dir: PathBuf,
    nas_dir: PathBuf,
    query: &UnifiedHistoricalQuery,
    symbol: &str,
    resolution: &str,
    start: i64,
    end: i64,
) -> Result<(), String> {
    // Tier config resolves from flags-or-defaults so the transparent path needs NO extra invocation
    // flags: capacity defaults to a bounded value (the real 1 TB capacity is the NFR-SC2 deployment
    // concern), the share defaults to 20%, and `now` (the retention-boundary instant) defaults to the
    // wall clock — read HERE at the composition root, so the cold_read library stays clock-free.
    let capacity = parsed.ssd_capacity.unwrap_or(DEFAULT_SSD_CAPACITY_RECORDS);
    let now = resolve_now(parsed.now)?;
    let hot_days = parsed.hot_days.unwrap_or(DEFAULT_HOT_RETENTION_DAYS);
    let share = parsed
        .cache_share
        .unwrap_or(DEFAULT_COLD_READ_CACHE_SHARE_PERCENT);

    let tier_config = TierConfig::new(ssd_dir, nas_dir, hot_days).map_err(|err| err.to_string())?;
    let cold_read = ColdReadConfig::new(capacity, share).map_err(|err| err.to_string())?;
    let reader = TieredReader::new(TieredStore::new(tier_config), cold_read);
    let result = reader.query(query, now).map_err(|err| err.to_string())?;

    println!("symbol:{symbol}");
    println!("resolution:{resolution}");
    println!("start:{start}");
    println!("end:{end}");
    println!("kind:{}", parsed.kind.map_or("any", |kind| kind.as_str()));
    println!("normalization:raw");
    println!("tier:cold-read");
    println!("now:{now}");
    println!("served_from_ssd:{}", result.served_from_ssd);
    println!("served_from_cache:{}", result.served_from_cache);
    println!("served_from_nas:{}", result.served_from_nas);
    println!("nas_reachable:{}", result.nas_reachable);
    println!("cold_cache_entries:{}", result.cold_cache_entries);
    println!("cold_cache_capacity:{}", result.cold_cache_capacity);
    println!("cold_cache_within_cap:{}", result.cold_cache_within_cap());
    println!("match_count:{}", result.len());
    print_records(result.records());

    // The cap is an invariant: exit non-zero if the cold-read cache ever exceeds its configured share.
    if !result.cold_cache_within_cap() {
        return Err(format!(
            "cold-read cache invariant BREACH: {} entries exceed the cap of {}",
            result.cold_cache_entries, result.cold_cache_capacity
        ));
    }
    Ok(())
}

/// Print the source-neutral record report shared by the single-tier and tiered read paths: each
/// record's `event_ts`, optional option contract, and integer-minor value fields — never a provider.
fn print_records(records: &[MarketDataRecord]) {
    for (index, record) in records.iter().enumerate() {
        let key = record.key();
        println!("record.{index}.event_ts:{}", key.event_ts);
        println!(
            "record.{index}.option_contract:{}",
            key.option_contract.as_deref().unwrap_or("-")
        );
        for value in record.fields() {
            println!("record.{index}.field.{}:{}", value.name, value.value_minor);
        }
    }
}

// --------------------------------------------------------------------------- //
// Store directory helper
// --------------------------------------------------------------------------- //

/// Default SSD capacity (in the store's record unit) the cold-read cache cap is a share of, when
/// neither `--ssd-capacity` nor a deployment config supplies one. The real 1 TB SSD capacity is the
/// NFR-SC2 deployment concern; this bounded default keeps transparent tiering working with zero
/// query-invocation changes (the cache is still capped at `--cache-share` % — default 20 — of it).
const DEFAULT_SSD_CAPACITY_RECORDS: u64 = 1_000_000;

/// Resolve the store directory: explicit `--dir`, else `ATP_DATA_STORE_DIR`, else error.
fn resolve_dir(explicit: Option<&str>) -> Result<PathBuf, String> {
    if let Some(dir) = explicit {
        return Ok(PathBuf::from(dir));
    }
    match env::var("ATP_DATA_STORE_DIR") {
        Ok(dir) if !dir.trim().is_empty() => Ok(PathBuf::from(dir)),
        _ => Err("no store directory: pass --dir <path> or set ATP_DATA_STORE_DIR".to_string()),
    }
}

/// Resolve the NAS archival tier for transparent cold-read failover (SRS-DATA-009): an explicit
/// `--nas <dir>`, else a non-empty `ATP_NAS_DATA_DIR` config key — resolved exactly like `--dir` /
/// `ATP_DATA_STORE_DIR`, so an EXISTING deployment auto-tiers with no change to the query invocation.
///
/// A **configured** NAS tier engages tiering even if the mount is currently **absent/unreachable**:
/// that surfaces as a DEGRADED cold read (`nas_reachable:false`) via [`TieredReader`]'s NAS
/// classification, NOT a silent single-tier read that would hide the NAS outage (an archived-off cold
/// record would then look like an empty result instead of a degraded-mode alert). Presence of the
/// directory is deliberately NOT checked here — reachability is the tier's runtime concern. Returns
/// `None` only when no NAS tier is configured at all (the genuine single-tier read path).
fn resolve_nas(parsed: &ParsedArgs) -> Option<PathBuf> {
    if let Some(nas) = parsed.nas.as_deref() {
        return Some(PathBuf::from(nas));
    }
    match env::var("ATP_NAS_DATA_DIR") {
        Ok(dir) if !dir.trim().is_empty() => Some(PathBuf::from(dir)),
        _ => None,
    }
}

/// Resolve the retention-boundary instant `now_ts`: an explicit `--now`, else the wall clock read HERE
/// at the composition root (the `cold_read` library itself never reads a clock — it is a pure function
/// of the caller-supplied `now_ts`). Fails closed if the system clock is before the Unix epoch.
fn resolve_now(explicit: Option<i64>) -> Result<i64, String> {
    if let Some(now) = explicit {
        return Ok(now);
    }
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .map_err(|_| "system clock is before the Unix epoch".to_string())
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

struct ParsedArgs {
    dir: Option<String>,
    symbol: Option<String>,
    resolution: Option<String>,
    start: Option<i64>,
    end: Option<i64>,
    kind: Option<DatasetKind>,
    normalization: Normalization,
    // SRS-DATA-009 transparent cold-read failover (optional): a configured NAS tier makes --dir the
    // SSD primary and serves the same query across SSD -> cold-read cache -> NAS.
    nas: Option<String>,
    ssd_capacity: Option<u64>,
    cache_share: Option<u32>,
    now: Option<i64>,
    hot_days: Option<u32>,
}

impl Default for ParsedArgs {
    fn default() -> Self {
        Self {
            dir: None,
            symbol: None,
            resolution: None,
            start: None,
            end: None,
            kind: None,
            normalization: Normalization::Raw,
            nas: None,
            ssd_capacity: None,
            cache_share: None,
            now: None,
            hot_days: None,
        }
    }
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--dir" => parsed.dir = Some(take_value(&mut iter, flag)?),
                "--symbol" => parsed.symbol = Some(take_value(&mut iter, flag)?),
                "--resolution" => parsed.resolution = Some(take_value(&mut iter, flag)?),
                "--start" => parsed.start = Some(parse_ts(&take_value(&mut iter, flag)?, flag)?),
                "--end" => parsed.end = Some(parse_ts(&take_value(&mut iter, flag)?, flag)?),
                "--kind" => {
                    let raw = take_value(&mut iter, flag)?;
                    let kind = DatasetKind::from_label(&raw).ok_or_else(|| {
                        format!(
                            "unknown --kind '{raw}' (expected daily-equity-bar | minute-equity-bar | option-chain | fundamental | corporate-action-split | corporate-action-dividend | corporate-action-delisting | corporate-action-merger | corporate-action-symbol-change)"
                        )
                    })?;
                    parsed.kind = Some(kind);
                }
                "--normalization" => {
                    parsed.normalization = parse_normalization(&take_value(&mut iter, flag)?)?;
                }
                // SRS-DATA-009 transparent cold-read failover flags (optional).
                "--nas" => parsed.nas = Some(take_value(&mut iter, flag)?),
                "--ssd-capacity" => {
                    let raw = take_value(&mut iter, flag)?;
                    parsed.ssd_capacity = Some(raw.parse::<u64>().map_err(|_| {
                        format!("--ssd-capacity expects a non-negative integer, got '{raw}'")
                    })?);
                }
                "--cache-share" => {
                    let raw = take_value(&mut iter, flag)?;
                    parsed.cache_share = Some(raw.parse::<u32>().map_err(|_| {
                        format!("--cache-share expects a percentage 0..=100, got '{raw}'")
                    })?);
                }
                "--now" => parsed.now = Some(parse_ts(&take_value(&mut iter, flag)?, flag)?),
                "--hot-days" => {
                    let raw = take_value(&mut iter, flag)?;
                    parsed.hot_days = Some(raw.parse::<u32>().map_err(|_| {
                        format!("--hot-days expects a non-negative integer, got '{raw}'")
                    })?);
                }
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn require_symbol(&self) -> Result<String, String> {
        self.symbol
            .clone()
            .ok_or_else(|| "missing required --symbol".to_string())
    }

    fn require_resolution(&self) -> Result<String, String> {
        self.resolution
            .clone()
            .ok_or_else(|| "missing required --resolution".to_string())
    }

    fn require_start(&self) -> Result<i64, String> {
        self.start
            .ok_or_else(|| "missing required --start".to_string())
    }

    fn require_end(&self) -> Result<i64, String> {
        self.end.ok_or_else(|| "missing required --end".to_string())
    }
}

/// Parse the `--normalization` value. `raw` returns stored values verbatim; `split-adjusted` and
/// `fully-adjusted` route through the coverage-enforcing gate
/// ([`MarketDataStore::query_split_adjusted`] / [`MarketDataStore::query_fully_adjusted`]) — the
/// values are ACCEPTED here, and the gate itself fails closed when the symbol is not covered through
/// `--end` (so an adjusted label is served only when coverage makes it honest, never
/// raw-as-adjusted). `total-return` is rejected as DEFERRED (dividend reinvestment and the
/// per-subscription mode selection, SRS-DATA-012). An unknown value fails closed.
fn parse_normalization(raw: &str) -> Result<Normalization, String> {
    match raw {
        "raw" => Ok(Normalization::Raw),
        "split-adjusted" => Ok(Normalization::SplitAdjusted),
        "fully-adjusted" => Ok(Normalization::FullyAdjusted),
        "total-return" => Err(format!(
            "--normalization '{raw}' is deferred to SRS-DATA-012 (total-return needs dividend \
             reinvestment and per-subscription mode selection); this surface serves 'raw', \
             'split-adjusted', and 'fully-adjusted'"
        )),
        other => Err(format!(
            "unknown --normalization '{other}' (this operator surface serves 'raw' | 'split-adjusted' | 'fully-adjusted')"
        )),
    }
}

fn parse_ts(raw: &str, flag: &str) -> Result<i64, String> {
    let ts = raw
        .parse::<i64>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))?;
    if ts < 0 {
        return Err(format!("{flag} must be non-negative"));
    }
    Ok(ts)
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}
