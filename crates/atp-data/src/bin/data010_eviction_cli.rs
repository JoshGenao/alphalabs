//! SRS-DATA-010 SSD storage-eviction-policy operator CLI.
//!
//! The operator-facing workflow that exercises the SRS-DATA-010 eviction policy end to end over the
//! on-disk SSD tier (the primary store + the SRS-DATA-009 cold-read cache) that `data008_tier_cli` /
//! `data009_cold_read_cli` populate — exactly as the verification step permits ("CLI/API workflows
//! with fixture market data, provider mocks, file reads, and persisted output inspection"):
//!
//! - `report   ... ` — inspect SSD usage against the high-water mark: capacity, target, cold/hot usage,
//!   and the pinned (never-evicted) counts by reason. Read-only.
//! - `plan     ... ` — compute the ordered eviction plan (cold-before-hot, non-listed-before-listed,
//!   oldest-first) WITHOUT mutating anything, printing each planned eviction and whether the mark is
//!   reachable. Read-only.
//! - `enforce  ... ` — apply the plan, physically evicting **only** the cold-read cache (the SSD
//!   primary/hot store is never opened for writing, so live / recently-accessed / in-retention data is
//!   structurally safe). Prints the outcome and exits NON-ZERO if the mark cannot be met without
//!   evicting pinned or hot data (the operator-visible fail-safe: SSD is still over the high-water mark).
//!
//! ## Protection inputs (the never-evict + deprioritise sets)
//! `--protection-inputs <file>` is a serde-free line file:
//! ```text
//! live       AAPL          # never evict — traded by the running live strategy (AC-2)
//! active     MSFT          # deprioritise — on the active-strategy list
//! watchlist  TSLA          # deprioritise — on the minute-bar watchlist
//! access     GOOG 1699999900   # symbol accessed by a running job at this epoch-second (AC-3)
//! ```
//! `--use-journal` additionally reads the durable SRS-DATA-010 access journal at
//! `<ssd>/access_journal/` (the real AC-3 producer written by the instrumented backtest / factor read
//! paths), filtered to the running jobs listed in `--running-jobs <file>` (one job id per line); with
//! no running-job file, EVERY in-window access is treated as protected (fail-closed over-protect). A
//! corrupt journal makes the read fail closed (the command errors) so eviction never proceeds against
//! an unreadable recency signal.
//!
//! ## Fail-closed enforcement gate
//! `enforce` REFUSES to run without an explicit protection source (`--protection-inputs` and/or
//! `--use-journal`) unless `--assume-unprotected` is passed — so a real deployment can never silently
//! treat "no live feed wired" as "evict everything". (Even so, `enforce` only ever touches the cache,
//! whose authoritative copy is on NAS, so the gate is defence-in-depth, not the only safety net.)
//!
//! Directories resolve fail-closed: explicit `--ssd` / `--nas` win, else `ATP_SSD_DATA_DIR` /
//! `ATP_NAS_DATA_DIR`. NAS is NOT read by eviction (the policy operates on the SSD tier + cache); it is
//! required only to build the same `TieredReader` the tier CLIs use. `--now` is the deterministic
//! clock (production passes the real instant, e.g. `--now "$(date +%s)"`); `--hot-days` is
//! floor-enforced at 90. Capacity / usage are counted in the store's record unit (the DATA-009 proxy
//! for bytes; the real 1 TB SSD is the NFR-SC2 deployment concern).

use std::collections::BTreeSet;
use std::env;
use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;

use atp_data::access_journal::{AccessJournal, JobId};
use atp_data::cold_read::{ColdReadConfig, TieredReader, DEFAULT_COLD_READ_CACHE_SHARE_PERCENT};
use atp_data::eviction::{
    EvictionEngine, ProtectionInputs, StoragePolicy, DEFAULT_HIGH_WATER_PERCENT,
    DEFAULT_RECENCY_WINDOW_SECS,
};
use atp_data::tiering::{TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};

/// A fixed default instant (NOT a clock read — keeps the demo deterministic). 2023-11-14T22:13:20Z.
const DEFAULT_NOW: i64 = 1_700_000_000;

const USAGE: &str = "\
data010_eviction_cli — SRS-DATA-010 SSD storage eviction policy

USAGE:
    data010_eviction_cli report  --ssd <path> --nas <path> --ssd-capacity <records> \\
                                 [--high-water <pct>] [--recency-secs <s>] [--now <ts>] [--hot-days <d>] \\
                                 [--protection-inputs <file>] [--use-journal] [--running-jobs <file>]
    data010_eviction_cli plan    <same flags as report>
    data010_eviction_cli enforce <same flags> (--protection-inputs <file> | --use-journal | --assume-unprotected)

Usage/target are in the store's record unit. The high-water target is floor(ssd-capacity * high-water
/ 100) (default high-water 80%). Eviction order: cold-read cache before hot, securities NOT on the
active-strategy/watchlist before those on it, oldest event_ts first. NEVER evicted: live-strategy
symbols, symbols accessed within --recency-secs (default 86400) by a running job, and hot records
inside the DATA-008 90-day retention floor. `enforce` physically evicts ONLY the cold-read cache and
exits NON-ZERO if the mark cannot be met without touching pinned/hot data.

--now defaults to a fixed value for a deterministic demo; production passes the real instant. NAS is
required only to build the tier reader — the eviction policy never reads it.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(code) => code,
        Err(err) => {
            eprintln!("data010_eviction_cli: {err}");
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
        "report" => cmd_report(rest),
        "plan" => cmd_plan(rest),
        "enforce" => cmd_enforce(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(ExitCode::SUCCESS)
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

fn cmd_report(rest: &[String]) -> Result<ExitCode, String> {
    let parsed = ParsedArgs::parse(rest)?;
    let reader = parsed.reader()?;
    let policy = parsed.policy()?;
    let now = parsed.now.unwrap_or(DEFAULT_NOW);
    let (inputs, source) = parsed.protection_inputs(&reader, &policy, now)?;

    let plan = EvictionEngine::new(&reader)
        .plan(&policy, &inputs, now)
        .map_err(|err| err.to_string())?;

    println!("ssd_dir:{}", reader.tier().config().ssd_dir().display());
    println!("cold_cache_dir:{}", reader.cold_cache_dir().display());
    println!("protection_source:{source}");
    println!("now:{now}");
    println!("capacity:{}", plan.capacity);
    println!("high_water_percent:{}", policy.high_water_percent());
    println!("recency_window_secs:{}", policy.recency_window_secs());
    println!("target:{}", plan.target);
    println!("usage_before:{}", plan.usage_before);
    println!("usage_cold:{}", plan.usage_cold);
    println!("usage_hot:{}", plan.usage_hot);
    println!("over_high_water:{}", plan.usage_before > plan.target);
    println!("pinned_live:{}", plan.pinned_live);
    println!("pinned_recent:{}", plan.pinned_recent);
    println!("pinned_retention:{}", plan.pinned_retention);
    println!("evictable:{}", plan.evict.len());
    println!("cold_evictions:{}", plan.cold_evictions());
    println!("hot_evictions:{}", plan.hot_evictions());
    println!("reached_target:{}", plan.reached_target);
    Ok(ExitCode::SUCCESS)
}

fn cmd_plan(rest: &[String]) -> Result<ExitCode, String> {
    let parsed = ParsedArgs::parse(rest)?;
    let reader = parsed.reader()?;
    let policy = parsed.policy()?;
    let now = parsed.now.unwrap_or(DEFAULT_NOW);
    let (inputs, source) = parsed.protection_inputs(&reader, &policy, now)?;

    let plan = EvictionEngine::new(&reader)
        .plan(&policy, &inputs, now)
        .map_err(|err| err.to_string())?;

    println!("protection_source:{source}");
    println!("now:{now}");
    println!("capacity:{}", plan.capacity);
    println!("target:{}", plan.target);
    println!("usage_before:{}", plan.usage_before);
    println!("projected_after:{}", plan.projected_after);
    println!("reached_target:{}", plan.reached_target);
    println!("over_by:{}", plan.over_by);
    println!("cold_evictions:{}", plan.cold_evictions());
    println!("hot_evictions:{}", plan.hot_evictions());
    println!("pinned_live:{}", plan.pinned_live);
    println!("pinned_recent:{}", plan.pinned_recent);
    println!("pinned_retention:{}", plan.pinned_retention);
    for eviction in &plan.evict {
        println!(
            "evict:{}:{}:{}",
            eviction.tier.as_str(),
            eviction.key.symbol,
            eviction.key.event_ts
        );
    }
    Ok(ExitCode::SUCCESS)
}

fn cmd_enforce(rest: &[String]) -> Result<ExitCode, String> {
    let parsed = ParsedArgs::parse(rest)?;
    let reader = parsed.reader()?;
    let policy = parsed.policy()?;
    let now = parsed.now.unwrap_or(DEFAULT_NOW);

    // Fail-closed gate: a destructive enforce requires an explicit protection source, so a real
    // deployment can never silently treat "no live feed wired" as "evict everything".
    let has_source = parsed.protection_inputs_path.is_some() || parsed.use_journal;
    if !has_source && !parsed.assume_unprotected {
        return Err(
            "enforce refuses to run without an explicit protection source: pass --protection-inputs \
             <file> and/or --use-journal, or --assume-unprotected to acknowledge no protections"
                .to_string(),
        );
    }

    let (inputs, source) = parsed.protection_inputs(&reader, &policy, now)?;
    let outcome = EvictionEngine::new(&reader)
        .enforce(&policy, &inputs, now)
        .map_err(|err| err.to_string())?;

    println!("protection_source:{source}");
    println!("now:{now}");
    println!("capacity:{}", outcome.capacity);
    println!("target:{}", outcome.target);
    println!("usage_before:{}", outcome.usage_before);
    println!("cold_evicted:{}", outcome.cold_evicted);
    println!("hot_pressure_deferred:{}", outcome.hot_pressure_deferred);
    println!("usage_after:{}", outcome.usage_after);
    println!("reached_target:{}", outcome.reached_target);
    println!("pinned_retained:{}", outcome.pinned_retained);
    println!("pinned_live:{}", outcome.pinned_live);
    println!("pinned_recent:{}", outcome.pinned_recent);
    println!("pinned_retention:{}", outcome.pinned_retention);

    // The high-water mark is an invariant the operator gates on: if eviction could not bring SSD usage
    // to the target WITHOUT touching pinned or hot data, exit NON-ZERO so operator automation catches
    // that the SSD is still over the mark (resolve via DATA-008 retention archival or added capacity).
    if !outcome.reached_target {
        return Err(format!(
            "SSD still over the high-water mark after eviction: usage {} exceeds target {} \
             ({} hot-tier evictions deferred, {} records pinned) — the policy refuses to evict \
             pinned or hot data",
            outcome.usage_after,
            outcome.target,
            outcome.hot_pressure_deferred,
            outcome.pinned_retained
        ));
    }
    Ok(ExitCode::SUCCESS)
}

// --------------------------------------------------------------------------- //
// Argument parsing (allowlist: unknown flags and value-less flags fail closed)
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct ParsedArgs {
    ssd: Option<String>,
    nas: Option<String>,
    ssd_capacity: Option<u64>,
    cache_share: Option<u32>,
    high_water: Option<u32>,
    recency_secs: Option<i64>,
    now: Option<i64>,
    hot_days: Option<u32>,
    protection_inputs_path: Option<String>,
    running_jobs_path: Option<String>,
    use_journal: bool,
    assume_unprotected: bool,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--ssd" => parsed.ssd = Some(take_value(&mut iter, flag)?),
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
                "--high-water" => {
                    let raw = take_value(&mut iter, flag)?;
                    parsed.high_water = Some(raw.parse::<u32>().map_err(|_| {
                        format!("--high-water expects a percentage 1..=100, got '{raw}'")
                    })?);
                }
                "--recency-secs" => {
                    let raw = take_value(&mut iter, flag)?;
                    parsed.recency_secs = Some(raw.parse::<i64>().map_err(|_| {
                        format!("--recency-secs expects a non-negative integer, got '{raw}'")
                    })?);
                }
                "--now" => parsed.now = Some(parse_ts(&mut iter, flag)?),
                "--hot-days" => {
                    let raw = take_value(&mut iter, flag)?;
                    parsed.hot_days = Some(raw.parse::<u32>().map_err(|_| {
                        format!("--hot-days expects a non-negative integer, got '{raw}'")
                    })?);
                }
                "--protection-inputs" => {
                    parsed.protection_inputs_path = Some(take_value(&mut iter, flag)?)
                }
                "--running-jobs" => parsed.running_jobs_path = Some(take_value(&mut iter, flag)?),
                "--use-journal" => parsed.use_journal = true,
                "--assume-unprotected" => parsed.assume_unprotected = true,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    /// Build the [`TieredReader`] — the SSD/NAS tiers + the cold-read cache config. Eviction reads the
    /// SSD primary + cache and writes only the cache; NAS is required only to build the reader.
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

    /// Build the validated [`StoragePolicy`] from `--high-water` (default 80) and `--recency-secs`
    /// (default 86400).
    fn policy(&self) -> Result<StoragePolicy, String> {
        let high_water = self.high_water.unwrap_or(DEFAULT_HIGH_WATER_PERCENT);
        let recency = self.recency_secs.unwrap_or(DEFAULT_RECENCY_WINDOW_SECS);
        StoragePolicy::new(high_water, recency).map_err(|err| err.to_string())
    }

    /// Assemble the [`ProtectionInputs`] from the optional protection-inputs file and, when
    /// `--use-journal` is set, the durable access journal (filtered to the running-job set). Returns
    /// the inputs and a human-readable description of what sourced them. A corrupt journal fails closed
    /// (the whole command errors). `now` and `policy` bound the recency window read from the journal.
    fn protection_inputs(
        &self,
        reader: &TieredReader,
        policy: &StoragePolicy,
        now: i64,
    ) -> Result<(ProtectionInputs, String), String> {
        let mut inputs = ProtectionInputs::new();
        let mut sources: Vec<String> = Vec::new();

        if let Some(path) = &self.protection_inputs_path {
            let text = fs::read_to_string(path)
                .map_err(|err| format!("cannot read --protection-inputs '{path}': {err}"))?;
            parse_protection_file(&text, &mut inputs)?;
            sources.push(format!("file:{path}"));
        }

        if self.use_journal {
            let journal = AccessJournal::under_ssd(reader.tier().config().ssd_dir());
            // Opting into journal-based recency protection asserts the journal is trustworthy. Because
            // recording fails open (a running job never breaks on a journal-write error), an unwritable
            // journal would silently hold NO recency evidence and read empty — letting eviction remove
            // data a running job is using. Fail CLOSED up front if the journal is not usable.
            journal
                .ensure_usable()
                .map_err(|err| format!("access-journal is not usable (fail-closed): {err}"))?;
            let running = self.running_jobs()?;
            let recent = journal
                .recent(policy.recency_window_secs(), now, running.as_ref())
                .map_err(|err| format!("access-journal read failed closed: {err}"))?;
            let count = recent.len();
            inputs.extend_recent_access(recent);
            sources.push(match &running {
                Some(set) => format!("journal:{count}-symbols:running-jobs={}", set.len()),
                None => format!("journal:{count}-symbols:all-in-window"),
            });
        }

        if sources.is_empty() {
            sources.push(if self.assume_unprotected {
                "assume-unprotected".to_string()
            } else {
                "none".to_string()
            });
        }
        Ok((inputs, sources.join(",")))
    }

    /// The optional running-job set from `--running-jobs <file>` (one job id per line). `None` when the
    /// flag is absent → the journal read over-protects every in-window access (fail-closed default).
    fn running_jobs(&self) -> Result<Option<BTreeSet<JobId>>, String> {
        let Some(path) = &self.running_jobs_path else {
            return Ok(None);
        };
        let text = fs::read_to_string(path)
            .map_err(|err| format!("cannot read --running-jobs '{path}': {err}"))?;
        let mut set = BTreeSet::new();
        for (lineno, raw) in text.lines().enumerate() {
            let line = raw.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let id = JobId::new(line)
                .map_err(|err| format!("--running-jobs line {}: {err}", lineno + 1))?;
            set.insert(id);
        }
        Ok(Some(set))
    }
}

/// Parse the serde-free protection-inputs line file into `inputs`, failing closed on a malformed line.
fn parse_protection_file(text: &str, inputs: &mut ProtectionInputs) -> Result<(), String> {
    for (lineno, raw) in text.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // Allow a trailing `# comment` on a directive line.
        let directive = line.split('#').next().unwrap_or("").trim();
        if directive.is_empty() {
            continue;
        }
        let mut tokens = directive.split_whitespace();
        let kind = tokens.next().unwrap_or("");
        let err_ctx = |what: &str| format!("--protection-inputs line {}: {what}", lineno + 1);
        match kind {
            "live" => {
                let sym = tokens
                    .next()
                    .ok_or_else(|| err_ctx("'live' expects a symbol"))?;
                inputs.add_live_symbol(sym);
            }
            "active" => {
                let sym = tokens
                    .next()
                    .ok_or_else(|| err_ctx("'active' expects a symbol"))?;
                inputs.add_active_symbol(sym);
            }
            "watchlist" => {
                let sym = tokens
                    .next()
                    .ok_or_else(|| err_ctx("'watchlist' expects a symbol"))?;
                inputs.add_watchlist_symbol(sym);
            }
            "access" => {
                let sym = tokens
                    .next()
                    .ok_or_else(|| err_ctx("'access' expects a symbol and a timestamp"))?;
                let ts_raw = tokens
                    .next()
                    .ok_or_else(|| err_ctx("'access' expects a timestamp after the symbol"))?;
                let ts: i64 = ts_raw.parse().map_err(|_| {
                    err_ctx(&format!("'access' timestamp '{ts_raw}' is not an integer"))
                })?;
                inputs.add_recent_access(sym, ts);
            }
            other => {
                return Err(err_ctx(&format!(
                    "unknown directive '{other}' (expected live | active | watchlist | access)"
                )))
            }
        }
        if tokens.next().is_some() {
            return Err(err_ctx("too many tokens for the directive"));
        }
    }
    Ok(())
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
