//! SRS-DATA-008 SSD-primary / NAS-archival tiered-storage operator CLI.
//!
//! The operator-facing workflow that exercises the [`TieredStore`] tier coordinator end to end over
//! two real on-disk store directories, driven by deterministic fixture sources that stand in for the
//! provider adapters (deferred to SRS-DATA-001/003/005/006) — exactly as the verification step
//! permits ("CLI/API workflows with fixture market data, provider mocks, file reads, and persisted
//! output inspection"):
//!
//! - `ingest      --ssd S --nas N --kind K [--event-ts T]` — validate a fixture batch through the
//!   ERR-5 gate, then ingest it SSD-FIRST and sync to NAS via the single validated + tiered write
//!   surface (`DataLayer::ingest_market_records_tiered`). Prints the validated + SSD insert counts and
//!   the NAS sync status (synced / degraded / failed), and the two persisted store-file paths so an
//!   operator can inspect both tiers on disk.
//! - `report      --ssd S --nas N [--now T] [--hot-days D]` — the cross-tier retention report: SSD /
//!   NAS totals, the hot/cold split, any hot datum missing from SSD (a retention breach), any SSD
//!   datum missing from NAS (a sync backlog), and the two acceptance verdicts.
//! - `archive-cold --ssd S --nas N [--now T] [--hot-days D]` — archive cold (older than the hot
//!   window) data off SSD, data-loss-safely (only what is confirmed byte-identical on NAS).
//! - `sync        --ssd S --nas N` — explicitly reconcile NAS to a superset of SSD (recovery after a
//!   degraded ingest); an unreachable NAS is fatal here.
//!
//! Directories resolve fail-closed: explicit `--ssd` / `--nas` win, else the `ATP_SSD_DATA_DIR` /
//! `ATP_NAS_DATA_DIR` config keys (read here as environment variables — the configuration layer that
//! validates them lives in `python/atp_config`), else an error.
//!
//! `--now` / `--hot-days` default to fixed values (NOT a clock read) so the demonstration is
//! deterministic and re-runnable; a production caller supplies the real instant, e.g.
//! `--now "$(date +%s)"`. The hot window is floor-enforced at 90 days
//! ([`atp_data::tiering::MIN_HOT_RETENTION_DAYS`]): a smaller `--hot-days` fails closed.
//!
//! Scope: the tier is provider-agnostic (it stores whatever records it is handed); the real network
//! adapters that FEED it are SRS-DATA-001/003/005/006; transparent cold-read failover from NAS is
//! SRS-DATA-009; the eviction POLICY that decides when to archive is SRS-DATA-010; real SSD/NAS
//! capacity is the NFR-SC2 deployment concern (growth estimates documented in SRS §12.1).

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use atp_data::store::{fixture_batch, DatasetKind, MarketDataRecord};
use atp_data::tiering::{NasSyncStatus, TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
use atp_data::{DataLayer, IngestionValidationEventSink, RecordValidator};
use atp_types::{IngestionValidationEvent, RecordValidationOutcome};

/// A fixed default instant and event timestamp (NOT a clock read — keeps the demo deterministic).
/// 2023-11-14T22:13:20Z.
const DEFAULT_TS: i64 = 1_700_000_000;

const USAGE: &str = "\
data008_tier_cli — SRS-DATA-008 SSD-primary / NAS-archival tiered-storage operator workflow

USAGE:
    data008_tier_cli ingest       --ssd <path> --nas <path> --kind <kind> [--event-ts <ts>]
    data008_tier_cli report       --ssd <path> --nas <path> [--now <ts>] [--hot-days <days>]
    data008_tier_cli archive-cold --ssd <path> --nas <path> [--now <ts>] [--hot-days <days>]
    data008_tier_cli sync         --ssd <path> --nas <path>

Directories come from --ssd/--nas, else ATP_SSD_DATA_DIR / ATP_NAS_DATA_DIR. SSD is provisioned on
first write; an absent NAS directory is treated as an unreachable (unmounted) archival tier:
`ingest` degrades (the SSD write still commits) while `sync` fails closed.

--now / --hot-days default to fixed values for a deterministic demo; production passes the real
instant (e.g. --now \"$(date +%s)\"). --hot-days is floor-enforced at 90.

KINDS:
    daily-equity-bar | minute-equity-bar | option-chain | fundamental | corporate-action-split
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data008_tier_cli: {err}");
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
        "report" => cmd_report(rest),
        "archive-cold" => cmd_archive_cold(rest),
        "sync" => cmd_sync(rest),
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

fn cmd_ingest(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let tier = parsed.tier()?;
    let kind = parsed.require_kind()?;
    let event_ts = parsed.event_ts.unwrap_or(DEFAULT_TS);

    // Route through the SINGLE validated + tiered write surface (DataLayer::ingest_market_records_tiered):
    // every record passes the ERR-5 validation gate read-only BEFORE the tier's SSD-first durable write
    // + NAS sync — so the tier's own operator ingest is validated exactly like data005/data016, not a
    // second unvalidated path.
    let outcome = DataLayer
        .ingest_market_records_tiered(
            &tier,
            fixture_batch(kind, event_ts),
            &AcceptAllValidator,
            &NullSink,
            observed_at(),
        )
        .map_err(|err| err.to_string())?;

    println!("kind:{}", kind.as_str());
    println!("event_ts:{event_ts}");
    println!("validated:{}", outcome.validated);
    println!("ssd_inserted:{}", outcome.tier.ssd_inserted);
    println!("ssd_unchanged:{}", outcome.tier.ssd_unchanged);
    match &outcome.tier.nas_sync {
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
    println!(
        "ssd_store_file:{}",
        tier.config().ssd_dir().join(store_filename()).display()
    );
    println!(
        "nas_store_file:{}",
        tier.config().nas_dir().join(store_filename()).display()
    );

    // A reachable-but-broken archive (corrupt/conflicting NAS store, lock contention, or an SSD
    // alias) is an INTEGRITY failure: the SSD write committed, but NAS is not a superset. Exit
    // NON-ZERO so operator automation that gates on exit status cannot mistake it for a clean ingest.
    // A Degraded (NAS-unreachable) outcome stays exit 0 — it is the documented recoverable outage a
    // later `sync` reconciles, surfaced via the printed status for the SRS-MD-006 readiness check.
    if let NasSyncStatus::Failed { reason } = &outcome.tier.nas_sync {
        return Err(format!(
            "NAS archival sync FAILED ({reason}): the SSD write committed but the archive is not a \
             superset — investigate the NAS store before relying on indefinite retention"
        ));
    }
    Ok(())
}

fn cmd_report(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let tier = parsed.tier()?;
    let now = parsed.now.unwrap_or(DEFAULT_TS);

    let report = tier.retention_report(now).map_err(|err| err.to_string())?;
    println!("now:{}", report.now_ts);
    println!("hot_retention_days:{}", report.hot_retention_days);
    println!("hot_window_start:{}", report.hot_window_start);
    println!("ssd_total:{}", report.ssd_total);
    println!("nas_total:{}", report.nas_total);
    println!("ssd_hot:{}", report.ssd_hot);
    println!("ssd_cold:{}", report.ssd_cold);
    println!("hot_missing_from_ssd:{}", report.hot_missing_from_ssd);
    println!("ssd_missing_from_nas:{}", report.ssd_missing_from_nas);
    println!("nas_reachable:{}", report.nas_reachable);
    // Tri-state verdicts: an unreachable NAS reports `unverified`, never a false `satisfied`.
    println!(
        "ssd_hot_retention:{}",
        report.ssd_hot_retention_verdict().as_str()
    );
    println!("nas_superset:{}", report.nas_superset_verdict().as_str());
    Ok(())
}

fn cmd_archive_cold(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let tier = parsed.tier()?;
    let now = parsed.now.unwrap_or(DEFAULT_TS);

    let outcome = tier.archive_cold(now).map_err(|err| err.to_string())?;
    println!("now:{now}");
    println!("nas_reachable:{}", outcome.nas_reachable);
    println!("archived:{}", outcome.archived);
    println!("retained_unconfirmed:{}", outcome.retained_unconfirmed);
    Ok(())
}

fn cmd_sync(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let tier = parsed.tier()?;
    let added = tier.sync_ssd_to_nas().map_err(|err| err.to_string())?;
    println!("nas_records_added:{added}");
    Ok(())
}

fn store_filename() -> &'static str {
    atp_data::store::STORE_FILENAME
}

/// A fixed observation instant for the ERR-5 envelope (NOT a clock read — keeps the demo
/// deterministic).
fn observed_at() -> u64 {
    DEFAULT_TS as u64
}

/// The DATA-013 validator (deferred) stand-in: accepts every fixture record so the demonstration
/// focuses on the tiering property. The real SYS-77 rule logic is SRS-DATA-013's owner.
struct AcceptAllValidator;

impl RecordValidator for AcceptAllValidator {
    fn validate(&self, _record: &MarketDataRecord) -> RecordValidationOutcome {
        RecordValidationOutcome::Valid
    }
}

/// A no-op validation event sink (the dashboard/notification fan-out is SRS-DATA-014 / SRS-NOTIF-001).
struct NullSink;

impl IngestionValidationEventSink for NullSink {
    fn record(&self, _event: IngestionValidationEvent) {}
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct ParsedArgs {
    ssd: Option<String>,
    nas: Option<String>,
    kind: Option<DatasetKind>,
    event_ts: Option<i64>,
    now: Option<i64>,
    hot_days: Option<u32>,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--ssd" => parsed.ssd = Some(take_value(&mut iter, flag)?),
                "--nas" => parsed.nas = Some(take_value(&mut iter, flag)?),
                "--kind" => {
                    let raw = take_value(&mut iter, flag)?;
                    let kind = DatasetKind::from_label(&raw).ok_or_else(|| {
                        format!(
                            "unknown --kind '{raw}' (expected daily-equity-bar | minute-equity-bar | option-chain | fundamental | corporate-action-split)"
                        )
                    })?;
                    if kind == DatasetKind::CorporateActionCoverage {
                        return Err(
                            "data008_tier_cli does not ingest 'corporate-action-coverage': the \
                             SRS-DATA-011 coverage frontier is a trust assertion, not provider \
                             market data"
                                .to_string(),
                        );
                    }
                    parsed.kind = Some(kind);
                }
                "--event-ts" => parsed.event_ts = Some(parse_ts(&mut iter, flag)?),
                "--now" => parsed.now = Some(parse_ts(&mut iter, flag)?),
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

    /// Build the validated tier from --ssd/--nas (or the env config keys) and --hot-days. The
    /// TierConfig floor-enforces the ≥90-day window, so a too-small --hot-days fails closed here.
    fn tier(&self) -> Result<TieredStore, String> {
        let ssd = resolve_dir(self.ssd.as_deref(), "ATP_SSD_DATA_DIR", "--ssd")?;
        let nas = resolve_dir(self.nas.as_deref(), "ATP_NAS_DATA_DIR", "--nas")?;
        let hot_days = self.hot_days.unwrap_or(DEFAULT_HOT_RETENTION_DAYS);
        let config = TierConfig::new(ssd, nas, hot_days).map_err(|err| err.to_string())?;
        Ok(TieredStore::new(config))
    }

    fn require_kind(&self) -> Result<DatasetKind, String> {
        self.kind
            .ok_or_else(|| "missing required --kind".to_string())
    }
}

fn parse_ts<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<i64, String> {
    let raw = take_value(iter, flag)?;
    let ts = raw
        .parse::<i64>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))?;
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
