//! SRS-DATA-009 transparent cold-read failover operator CLI.
//!
//! The operator-facing workflow that exercises the [`TieredReader`] cold-read path end to end over the
//! two on-disk tiers the SRS-DATA-008 `data008_tier_cli` populates, driven by the same deterministic
//! fixture sources — exactly as the verification step permits ("CLI/API workflows with fixture market
//! data, provider mocks, file reads, and persisted output inspection"):
//!
//! - `query ... --symbol SYM --resolution R --start T0 --end T1 [--kind K] --now T [--hot-days D]
//!   --ssd-capacity C [--cache-share P]` — run the SRS-DATA-007 unified historical query
//!   transparently across the tiers: SSD primary → cold-read cache → (for cold ranges) NAS fallback,
//!   caching NAS-served records on SSD within the configured share. Prints the matched records plus
//!   per-tier provenance (served_from_ssd / _cache / _nas), the cache state (entries vs cap), and
//!   whether the cache is within its cap. Exits NON-ZERO if the cap is ever exceeded (an invariant
//!   breach) so operator automation cannot mistake it for a healthy cache.
//! - `cache-report --ssd S --nas N --ssd-capacity C [--cache-share P]` — inspect the cold-read cache
//!   occupancy against its cap (the "cache does not exceed the configurable SSD share" evidence).
//! - `evict-cache --ssd S --nas N --ssd-capacity C [--cache-share P] --max-entries M` — drain the
//!   cold-read cache down to at most M entries WITHOUT touching hot data (the "evicted before hot
//!   runtime data" primitive the SRS-DATA-010 policy drives).
//!
//! Directories resolve fail-closed: explicit `--ssd` / `--nas` win, else the `ATP_SSD_DATA_DIR` /
//! `ATP_NAS_DATA_DIR` config keys (read here as environment variables — the configuration layer that
//! validates them lives in `python/atp_config`), else an error.
//!
//! `--now` is the deterministic retention-boundary instant (production passes the real instant, e.g.
//! `--now "$(date +%s)"`); it selects the hot/cold boundary, NOT a storage tier. `--hot-days` is
//! floor-enforced at 90 ([`atp_data::tiering::MIN_HOT_RETENTION_DAYS`]). `--cache-share` defaults to
//! 20% ([`atp_data::cold_read::DEFAULT_COLD_READ_CACHE_SHARE_PERCENT`]); `--ssd-capacity` is the SSD
//! capacity (in the store's record unit) the share is taken of.
//!
//! Scope: this is the READ counterpart to `data008_tier_cli` (ingest / archive-cold). All durable
//! cache persistence is owned by the `cold_read` library module (this binary never persists directly —
//! it only reads through [`TieredReader`]), so the cold-read cache write is not an ingestion path. The
//! eviction POLICY (the 80% high-water trigger, inactivity recency, never-evict live-strategy data) is
//! SRS-DATA-010; real SSD/NAS capacity is the NFR-SC2 deployment concern.

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use atp_data::cold_read::{ColdReadConfig, TieredReader, DEFAULT_COLD_READ_CACHE_SHARE_PERCENT};
use atp_data::query::UnifiedHistoricalQuery;
use atp_data::store::DatasetKind;
use atp_data::tiering::{TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};

/// A fixed default instant (NOT a clock read — keeps the demo deterministic). 2023-11-14T22:13:20Z.
const DEFAULT_NOW: i64 = 1_700_000_000;

const USAGE: &str = "\
data009_cold_read_cli — SRS-DATA-009 transparent cold-read failover to NAS + bounded SSD cache

USAGE:
    data009_cold_read_cli query        --ssd <path> --nas <path> --symbol <sym> --resolution <res> \\
                                       --start <ts> --end <ts> [--kind <kind>] [--now <ts>] \\
                                       [--hot-days <days>] --ssd-capacity <records> [--cache-share <pct>]
    data009_cold_read_cli cache-report --ssd <path> --nas <path> --ssd-capacity <records> [--cache-share <pct>]
    data009_cold_read_cli evict-cache  --ssd <path> --nas <path> --ssd-capacity <records> \\
                                       [--cache-share <pct>] --max-entries <n>

Directories come from --ssd/--nas, else ATP_SSD_DATA_DIR / ATP_NAS_DATA_DIR. The cold-read cache
lives at <ssd>/cold_read_cache. A query reaching cold territory (start < now - hot-days) falls back
to NAS transparently and caches the NAS-served records on SSD, capped at --cache-share % of
--ssd-capacity (default 20%). An unreachable NAS degrades to a resident-only result (no error).

--now defaults to a fixed value for a deterministic demo; production passes the real instant
(e.g. --now \"$(date +%s)\"). --hot-days is floor-enforced at 90.

KINDS:
    daily-equity-bar | minute-equity-bar | option-chain | fundamental | corporate-action-split
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data009_cold_read_cli: {err}");
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
        "cache-report" => cmd_cache_report(rest),
        "evict-cache" => cmd_evict_cache(rest),
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

fn cmd_query(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let reader = parsed.reader()?;
    let now = parsed.now.unwrap_or(DEFAULT_NOW);

    let symbol = parsed.require("--symbol", parsed.symbol.as_deref())?;
    let resolution = parsed.require("--resolution", parsed.resolution.as_deref())?;
    let start = parsed
        .start
        .ok_or_else(|| "missing required --start".to_string())?;
    let end = parsed
        .end
        .ok_or_else(|| "missing required --end".to_string())?;

    let mut query = UnifiedHistoricalQuery::new(symbol, resolution, start, end);
    if let Some(kind) = parsed.kind {
        query = query.with_kind(kind);
    }

    let result = reader.query(&query, now).map_err(|err| err.to_string())?;

    println!("symbol:{}", result.symbol);
    println!("resolution:{}", result.resolution);
    println!("now:{now}");
    println!("records:{}", result.len());
    println!("served_from_ssd:{}", result.served_from_ssd);
    println!("served_from_cache:{}", result.served_from_cache);
    println!("served_from_nas:{}", result.served_from_nas);
    println!("newly_cached:{}", result.newly_cached);
    println!("cache_evicted:{}", result.cache_evicted);
    println!("nas_consulted:{}", result.nas_consulted);
    println!("nas_reachable:{}", result.nas_reachable);
    println!("cold_cache_entries:{}", result.cold_cache_entries);
    println!("cold_cache_capacity:{}", result.cold_cache_capacity);
    println!("cold_cache_within_cap:{}", result.cold_cache_within_cap());
    println!("cold_cache_dir:{}", reader.cold_cache_dir().display());
    for record in result.records() {
        println!(
            "record:{}:{}",
            record.key().event_ts,
            record.key().kind.as_str()
        );
    }

    // The cap is an invariant, not a hope: if the cold-read cache ever exceeds its configured share,
    // exit NON-ZERO so operator automation that gates on exit status catches the breach.
    if !result.cold_cache_within_cap() {
        return Err(format!(
            "cold-read cache invariant BREACH: {} entries exceed the cap of {} \
             (share {}% of capacity)",
            result.cold_cache_entries,
            result.cold_cache_capacity,
            reader.cold_read_config().cache_share_percent()
        ));
    }
    Ok(())
}

fn cmd_cache_report(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let reader = parsed.reader()?;
    let report = reader.cold_cache_report().map_err(|err| err.to_string())?;
    println!("cold_cache_dir:{}", reader.cold_cache_dir().display());
    println!("ssd_capacity_records:{}", report.ssd_capacity_records);
    println!("cache_share_percent:{}", report.share_percent);
    println!("cold_cache_capacity:{}", report.capacity);
    println!("cold_cache_entries:{}", report.entries);
    println!("within_cap:{}", report.within_cap());
    if !report.within_cap() {
        return Err(format!(
            "cold-read cache invariant BREACH: {} entries exceed the cap of {}",
            report.entries, report.capacity
        ));
    }
    Ok(())
}

fn cmd_evict_cache(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let reader = parsed.reader()?;
    let max_entries = parsed
        .max_entries
        .ok_or_else(|| "missing required --max-entries".to_string())?;

    let evicted = reader
        .evict_cold_cache_to(max_entries)
        .map_err(|err| err.to_string())?;
    let report = reader.cold_cache_report().map_err(|err| err.to_string())?;
    println!("max_entries:{max_entries}");
    println!("evicted:{evicted}");
    println!("cold_cache_entries:{}", report.entries);
    println!("within_cap:{}", report.within_cap());
    Ok(())
}

// --------------------------------------------------------------------------- //
// Argument parsing (allowlist: unknown flags and value-less flags fail closed)
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct ParsedArgs {
    ssd: Option<String>,
    nas: Option<String>,
    symbol: Option<String>,
    resolution: Option<String>,
    start: Option<i64>,
    end: Option<i64>,
    kind: Option<DatasetKind>,
    now: Option<i64>,
    hot_days: Option<u32>,
    ssd_capacity: Option<u64>,
    cache_share: Option<u32>,
    max_entries: Option<u64>,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--ssd" => parsed.ssd = Some(take_value(&mut iter, flag)?),
                "--nas" => parsed.nas = Some(take_value(&mut iter, flag)?),
                "--symbol" => parsed.symbol = Some(take_value(&mut iter, flag)?),
                "--resolution" => parsed.resolution = Some(take_value(&mut iter, flag)?),
                "--start" => parsed.start = Some(parse_ts(&mut iter, flag)?),
                "--end" => parsed.end = Some(parse_ts(&mut iter, flag)?),
                "--kind" => {
                    let raw = take_value(&mut iter, flag)?;
                    let kind = DatasetKind::from_label(&raw).ok_or_else(|| {
                        format!(
                            "unknown --kind '{raw}' (expected daily-equity-bar | minute-equity-bar | option-chain | fundamental | corporate-action-split)"
                        )
                    })?;
                    if kind == DatasetKind::CorporateActionCoverage {
                        return Err(
                            "data009_cold_read_cli does not query 'corporate-action-coverage': the \
                             SRS-DATA-011 coverage frontier is a trust assertion, not tiered market data"
                                .to_string(),
                        );
                    }
                    parsed.kind = Some(kind);
                }
                "--now" => parsed.now = Some(parse_ts(&mut iter, flag)?),
                "--hot-days" => {
                    let raw = take_value(&mut iter, flag)?;
                    parsed.hot_days = Some(raw.parse::<u32>().map_err(|_| {
                        format!("--hot-days expects a non-negative integer, got '{raw}'")
                    })?);
                }
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
                "--max-entries" => {
                    let raw = take_value(&mut iter, flag)?;
                    parsed.max_entries = Some(raw.parse::<u64>().map_err(|_| {
                        format!("--max-entries expects a non-negative integer, got '{raw}'")
                    })?);
                }
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    /// Build the [`TieredReader`] from the tier directories, the floor-enforced hot window, and the
    /// cold-read cache config. A too-small --hot-days, an SSD/NAS alias, a zero --ssd-capacity, or a
    /// >100 --cache-share all fail closed here.
    fn reader(&self) -> Result<TieredReader, String> {
        let ssd = resolve_dir(self.ssd.as_deref(), "ATP_SSD_DATA_DIR", "--ssd")?;
        let nas = resolve_dir(self.nas.as_deref(), "ATP_NAS_DATA_DIR", "--nas")?;
        let hot_days = self.hot_days.unwrap_or(DEFAULT_HOT_RETENTION_DAYS);
        let tier_config = TierConfig::new(ssd, nas, hot_days).map_err(|err| err.to_string())?;
        let capacity = self
            .ssd_capacity
            .ok_or_else(|| "missing required --ssd-capacity".to_string())?;
        let share = self
            .cache_share
            .unwrap_or(DEFAULT_COLD_READ_CACHE_SHARE_PERCENT);
        let cold_read = ColdReadConfig::new(capacity, share).map_err(|err| err.to_string())?;
        Ok(TieredReader::new(TieredStore::new(tier_config), cold_read))
    }

    fn require<'a>(&self, flag: &str, value: Option<&'a str>) -> Result<&'a str, String> {
        value.ok_or_else(|| format!("missing required {flag}"))
    }
}

fn parse_ts<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<i64, String> {
    let raw = take_value(iter, flag)?;
    let ts = raw
        .parse::<i64>()
        .map_err(|_| format!("{flag} expects an integer, got '{raw}'"))?;
    if ts < 0 {
        return Err(format!("{flag} must be non-negative"));
    }
    Ok(ts)
}

/// Resolve a tier directory: explicit flag, else the named environment config key, else error.
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
