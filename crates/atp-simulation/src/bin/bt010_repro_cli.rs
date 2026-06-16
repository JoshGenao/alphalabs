//! SRS-BT-010 cross-process reproducibility CLI.
//!
//! The cross-process closure of "produce deterministic backtest results for identical inputs"
//! (docs/SRS.md SRS-BT-010; SyRS SYS-62; StRS SN-1.02). The in-process verifier in the
//! `determinism` module ([`verify_reproducible`](atp_simulation::determinism::verify_reproducible))
//! runs two replays sequentially in ONE process; that cannot catch process-seeded nondeterminism
//! that is stable within a process but varies across a restart — the "platform-generated random
//! values" clause. This binary closes that gap: it runs the fixture backtest in a **fresh OS
//! process** and prints a stable fingerprint of the run's inputs and outputs, so an operator (and
//! the `srs_bt_010_cross_process` integration test) can run it twice in two fresh processes and
//! assert byte-identical output. There is no Python<->Rust runtime bridge, so the operator surface
//! is a small Rust binary over the crate's public determinism API — the same precedent as the
//! SRS-BT-009 store CLI.
//!
//! - `digest [--lot N] [--sell-ts T] [--seed N] [--code-version V] [--strategy S] [--full]` — run
//!   the fixture backtest through the real [`BacktestEngine`] once and print, on their own lines:
//!     * `run-manifest:<hex>` — the [`ManifestDigest`] of the run's IMMUTABLE INPUTS (code version,
//!       parameters, data source/symbol/content digest, date range, seed, cost model). Two runs
//!       with the same manifest digest received identical inputs, so a reproducibility check PROVES
//!       the inputs matched rather than assuming the fixture is deterministic.
//!     * `run-digest:<hex>` — the [`RunDigest`] over all three SRS-BT-010 artifacts (trade log,
//!       equity curve, and the SRS-BT-004 metric family).
//!     * with `--full`, the manifest fields and the complete trade log / equity curve / metrics
//!       are also rendered, so an operator can eyeball that two fresh-process runs are identical.
//! - `verify` — run the in-process double-run
//!   ([`verify_reproducible_with_metrics`](atp_simulation::determinism::verify_reproducible_with_metrics))
//!   and print the resulting digest; a convenience wrapper that keeps that surface CLI-reachable.
//!
//! The seed is recorded in the manifest for provenance, but the deterministic engine consumes NO
//! platform randomness, so the `run-digest` is seed-independent: an identical run with a *different*
//! `--seed` produces a different `run-manifest` (the input set differs) but the SAME `run-digest`.
//! That is the "platform-generated random values do not introduce nondeterminism" guarantee made
//! observable. A different `--lot` / `--sell-ts` changes the trades, so the `run-digest` differs.
//!
//! Scope: the REST `POST /api/v1/backtests` + dashboard rendering of this repeated-run workflow
//! (SRS-API-001 / SRS-UI), the end-to-end guarantee under the real Python strategy host binding the
//! strategy code version + seed (SRS-BT-001-runtime), and stamping the [`RunDigest`] onto each
//! persisted SRS-BT-009 record (SRS-BT-009 store integration) remain deferred. The market data and
//! strategy here are deliberate fixtures.

use std::env;
use std::process::ExitCode;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::determinism::{
    digest_manifest, digest_run, verify_reproducible_with_metrics, RunManifest,
};
use atp_simulation::metrics::{
    compute as compute_metrics, Benchmark, MetricsConfig, MetricsError, PerformanceMetrics,
};
use atp_types::StrategyId;

const SYMBOL: &str = "AAPL";
const STARTING_CASH_MINOR: i64 = 1_000_000;
/// The fixed backtest window the fixture bars fall within.
const RANGE: (u64, u64) = (0, 100);

const USAGE: &str = "\
bt010_repro_cli — SRS-BT-010 cross-process reproducibility workflow for backtest results

USAGE:
    bt010_repro_cli digest [--lot <n>] [--sell-ts <ts>] [--seed <n>] [--code-version <v>]
                           [--strategy <id>] [--full]
    bt010_repro_cli verify [--lot <n>] [--sell-ts <ts>] [--strategy <id>]

COMMANDS:
    digest   Run the fixture backtest through the real engine ONCE in this process and print the
             run-manifest:<hex> (a fingerprint of the immutable inputs) and run-digest:<hex> (a
             fingerprint of the trade log + equity curve + metrics). Run it in two FRESH processes
             over identical flags and the two outputs must be byte-identical — the cross-process
             determinism guarantee a same-process double-run cannot establish.
    verify   Run the in-process double-run verifier and print the reproduced run-digest.

FLAGS:
    --lot <n>            shares opened on the first bar, then closed (default 10); part of the
                         run's parameter set, so a different lot changes the trades and the digest
    --sell-ts <ts>       bar timestamp at which the position is closed (default 5)
    --seed <n>           platform-RNG seed recorded in the manifest (default 0); the engine uses no
                         platform randomness, so a different seed changes run-manifest but NOT
                         run-digest
    --code-version <v>   strategy code version recorded in the manifest (default sha:deadbeef)
    --strategy <id>      strategy id recorded on the request (default fixture)
    --full               render the manifest fields + the complete trade log / equity curve / metrics
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("bt010_repro_cli: {err}");
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
        "digest" => cmd_digest(rest),
        "verify" => cmd_verify(rest),
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

/// Run the fixture backtest once and print the input-manifest digest and the run digest.
fn cmd_digest(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let request = parsed.request();
    let catalog = fixture_catalog();

    let mut strategy = parsed.strategy_impl();
    let result: BacktestResult = BacktestEngine::new()
        .run(&request, &mut strategy, &catalog)
        .map_err(|err| format!("backtest run failed: {err:?}"))?;

    let metrics = compute_metrics(
        request.starting_cash_minor,
        &result.equity_curve,
        &result.trade_log,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .map_err(|err: MetricsError| format!("metric computation failed: {err:?}"))?;

    let manifest = RunManifest::from_request(
        &request,
        parsed.code_version.clone(),
        parsed.seed,
        parsed.parameters(),
        input_data_digest(&catalog.bars),
    );

    // The two stable fingerprints, each on its own line. Two fresh processes over identical flags
    // must print byte-identical lines (cross-process reproducibility).
    println!("{}", digest_manifest(&manifest));
    println!("{}", digest_run(&result, Some(&metrics)));

    if parsed.full {
        print_full(&manifest, &result, &metrics);
    }
    Ok(())
}

/// Run the in-process double-run verifier and print the reproduced run digest.
fn cmd_verify(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let request = parsed.request();
    let catalog = fixture_catalog();

    let digest = verify_reproducible_with_metrics(
        &BacktestEngine::new(),
        &request,
        || parsed.strategy_impl(),
        &catalog,
        &Benchmark::spy(),
        None,
        &MetricsConfig::default(),
    )
    .map_err(|err| format!("in-process reproducibility check failed: {err}"))?;

    println!("reproducible {digest}");
    Ok(())
}

fn print_full(manifest: &RunManifest, result: &BacktestResult, metrics: &PerformanceMetrics) {
    println!("manifest:");
    println!("    code_version:       {}", manifest.code_version);
    println!("    seed:               {}", manifest.seed);
    println!(
        "    parameters:         [{}]",
        manifest
            .parameters
            .iter()
            .map(|(k, v)| format!("{k}={v}"))
            .collect::<Vec<_>>()
            .join(", ")
    );
    println!("    data_source:        {}", manifest.data_source.as_str());
    println!("    symbol:             {}", manifest.symbol);
    println!(
        "    range:              [{},{}]",
        manifest.range.start, manifest.range.end
    );
    println!("    starting_cash_minor:{}", manifest.starting_cash_minor);
    println!(
        "    input_data_digest:  {:016x}",
        manifest.input_data_digest
    );

    println!("trade_log: {} fill(s)", result.trade_log.len());
    for (index, fill) in result.trade_log.iter().enumerate() {
        println!(
            "    fill[{index}] ts={} symbol={} qty={} price_minor={} commission_minor={} slippage_minor={} spread_impact_minor={}",
            fill.ts,
            fill.symbol,
            fill.quantity,
            fill.price_minor,
            fill.commission_minor,
            fill.slippage_minor,
            fill.spread_impact_minor,
        );
    }
    println!("equity_curve: {} point(s)", result.equity_curve.len());
    for (index, point) in result.equity_curve.iter().enumerate() {
        println!(
            "    equity[{index}] ts={} equity_minor={}",
            point.ts, point.equity_minor
        );
    }
    println!("final_equity_minor: {}", result.final_equity_minor);
    println!(
        "metrics: sharpe={} sortino={} alpha={} beta={} max_drawdown={} ann_return={} ann_vol={} win_rate={} benchmark={}",
        fmt_opt(metrics.sharpe_ratio),
        fmt_opt(metrics.sortino_ratio),
        fmt_opt(metrics.alpha),
        fmt_opt(metrics.beta),
        fmt_opt(metrics.max_drawdown),
        fmt_opt(metrics.annualized_return),
        fmt_opt(metrics.annualized_volatility),
        fmt_opt(metrics.win_rate),
        metrics.benchmark_symbol,
    );
}

fn fmt_opt(value: Option<f64>) -> String {
    value.map_or_else(|| "n/a".to_string(), |v| v.to_string())
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

struct ParsedArgs {
    lot: i64,
    sell_ts: u64,
    seed: u64,
    code_version: String,
    strategy: String,
    full: bool,
}

impl Default for ParsedArgs {
    fn default() -> Self {
        Self {
            lot: 10,
            sell_ts: 5,
            seed: 0,
            code_version: "sha:deadbeef".to_string(),
            strategy: "fixture".to_string(),
            full: false,
        }
    }
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--lot" => parsed.lot = take_i64(&mut iter, flag)?,
                "--sell-ts" => parsed.sell_ts = take_u64(&mut iter, flag)?,
                "--seed" => parsed.seed = take_u64(&mut iter, flag)?,
                "--code-version" => parsed.code_version = take_value(&mut iter, flag)?,
                "--strategy" => parsed.strategy = take_value(&mut iter, flag)?,
                "--full" => parsed.full = true,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn request(&self) -> BacktestRequest {
        BacktestRequest {
            strategy_id: StrategyId::new(self.strategy.clone()),
            symbol: SYMBOL.to_string(),
            data_source: BacktestDataSource::SystemData,
            range: DateRange::new(RANGE.0, RANGE.1),
            starting_cash_minor: STARTING_CASH_MINOR,
            cost_config: CostConfig::default(),
        }
    }

    fn strategy_impl(&self) -> RoundTrip {
        RoundTrip {
            lot: self.lot,
            sell_ts: self.sell_ts,
        }
    }

    /// The run's parameter set, bound into the manifest so a different lot / sell-ts is a different
    /// input identity (and the strategy actually trades differently).
    fn parameters(&self) -> Vec<(String, String)> {
        vec![
            ("lot".to_string(), self.lot.to_string()),
            ("sell_ts".to_string(), self.sell_ts.to_string()),
        ]
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

fn take_i64<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<i64, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<i64>()
        .map_err(|_| format!("{flag} expects an integer, got '{raw}'"))
}

fn take_u64<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<u64, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<u64>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))
}

// --------------------------------------------------------------------------- //
// Deterministic fixture (mirrors the bt009_store_cli producer)
// --------------------------------------------------------------------------- //

/// A content digest of the input bars, so the manifest binds the DATA by content (not just the
/// data-source label). Deterministic FNV-1a over each bar's `(symbol, ts, close_minor, spread)`,
/// so two fresh processes over the same fixture produce the same digest.
fn input_data_digest(bars: &[BacktestBar]) -> u64 {
    const OFFSET_BASIS: u64 = 0xcbf29ce484222325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut body = String::new();
    for bar in bars {
        body.push_str(&bar.symbol);
        body.push('\n');
        body.push_str(&bar.ts.to_string());
        body.push('\n');
        body.push_str(&bar.close_minor.to_string());
        body.push('\n');
        body.push_str(&bar.spread_minor.map_or("n".to_string(), |s| s.to_string()));
        body.push('\n');
    }
    let mut hash = OFFSET_BASIS;
    for &byte in body.as_bytes() {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

fn fixture_catalog() -> FixtureCatalog {
    FixtureCatalog {
        bars: vec![
            bar(1, 100),
            bar(2, 120),
            bar(3, 90),
            bar(4, 130),
            bar(5, 125),
        ],
    }
}

fn bar(ts: u64, close_minor: i64) -> BacktestBar {
    BacktestBar {
        symbol: SYMBOL.to_string(),
        ts,
        close_minor,
        spread_minor: None,
    }
}

/// A fixture catalog of close-only bars that honors the requested window.
struct FixtureCatalog {
    bars: Vec<BacktestBar>,
}

impl BarSource for FixtureCatalog {
    fn source(&self) -> BacktestDataSource {
        BacktestDataSource::SystemData
    }

    fn bars(
        &self,
        symbol: &str,
        range: &DateRange,
        max_bars: usize,
    ) -> Result<Vec<BacktestBar>, BacktestError> {
        let rows: Vec<BacktestBar> = self
            .bars
            .iter()
            .filter(|bar| bar.symbol == symbol && range.contains(bar.ts))
            .cloned()
            .collect();
        if rows.len() > max_bars {
            return Err(BacktestError::TooManyBars {
                count: rows.len(),
                limit: max_bars,
            });
        }
        Ok(rows)
    }
}

/// Opens `lot` shares on the first bar, then fully closes on `sell_ts` — one round trip.
struct RoundTrip {
    lot: i64,
    sell_ts: u64,
}

impl BacktestStrategy for RoundTrip {
    fn on_bar(&mut self, bar: &BacktestBar, position: i64) -> Result<i64, BacktestError> {
        if bar.ts == self.sell_ts {
            return Ok(-position);
        }
        if position == 0 {
            return Ok(self.lot);
        }
        Ok(0)
    }
}
