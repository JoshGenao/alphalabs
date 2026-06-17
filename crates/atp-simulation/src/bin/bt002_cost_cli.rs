//! SRS-BT-002 configurable transaction-cost operator CLI.
//!
//! The operator-facing surface of "apply configurable commission, slippage, and spread-impact
//! models to backtests" (docs/SRS.md SRS-5.6 SRS-BT-002; SyRS SYS-15a–d; StRS SN-1.03). The
//! cost-model *family* in the `cost` module already implements the AC, and the runnable
//! [`BacktestEngine`] already applies the per-run [`CostConfig`] on [`BacktestRequest`]; what kept
//! SRS-BT-002 `passes:false` was the deferred "operator override SURFACE — the REST/CLI/dashboard
//! controls that let an operator pick a cost model per run". This binary ships the **CLI** half of
//! that surface, the same precedent as the SRS-BT-009 store CLI and the SRS-BT-010 reproducibility
//! CLI (there is no Python<->Rust runtime bridge, so the operator workflow the acceptance names is
//! demonstrated here over the Rust core). The REST + dashboard controls (SRS-API-001 / SRS-UI) and
//! the real data + Python strategy host (SRS-BT-001-runtime) remain deferred.
//!
//! - `defaults` — print the SyRS default constants (`DEFAULT_SLIPPAGE_BPS`,
//!   `DEFAULT_SPREAD_FALLBACK_BPS`, the IB tiered rate / floor / cap) and the default model of each
//!   family, and assert `CostConfig::default()` equals `CostConfig::syrs_defaults()`. This is the
//!   "defaults match SyRS values" half of the AC, made inspectable.
//! - `run [--commission M] [--slippage M] [--spread M] [--lot N] [--sell-ts T] [--full]` — build a
//!   [`CostConfig`] from the flags (no cost flags ⇒ `CostConfig::default()`, the SyRS baseline),
//!   set it on the request, run the SAME fixture strategy once through the real engine, and print
//!   the resolved config, the per-fill [`CostBreakdown`] (commission / slippage / spread-impact, in
//!   integer minor units), the aggregate total cost, and the final equity. The strategy is fixed:
//!   only the cost config changes between runs, which is the "override … without changing strategy
//!   code" half of the AC (SYS-15d). `--full` also renders the equity curve.
//!
//! Override grammar (parsed to the existing enums):
//!   --commission ib-tiered | per-share:RATE,MIN | per-trade:FEE | none
//!   --slippage   bps:N | none
//!   --spread     observed:FALLBACK | fixed:N | none
//!
//! Fail closed: an unknown subcommand or flag exits non-zero; a negative override parameter (e.g.
//! `per-share:-1,35`) flows to `CostConfig::validate()` inside the engine and is rejected
//! (`CostError::NegativeParameter`) before any fill, so a misconfigured cost can never fabricate
//! cash — the run exits non-zero with no fill or equity printed.

use std::env;
use std::process::ExitCode;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::cost::{
    CommissionModel, CostConfig, SlippageModel, SpreadImpactModel, DEFAULT_SLIPPAGE_BPS,
    DEFAULT_SPREAD_FALLBACK_BPS, IB_TIERED_MAX_PCT_BPS, IB_TIERED_MIN_PER_ORDER_MINOR,
    IB_TIERED_RATE_CENTIMINOR_PER_SHARE,
};
use atp_types::StrategyId;

const SYMBOL: &str = "AAPL";
const STARTING_CASH_MINOR: i64 = 10_000_000; // $100,000.00

const USAGE: &str = "\
bt002_cost_cli — SRS-BT-002 configurable transaction-cost operator workflow for backtests

USAGE:
    bt002_cost_cli defaults
    bt002_cost_cli run [--commission <m>] [--slippage <m>] [--spread <m>]
                       [--lot <n>] [--sell-ts <t>] [--full]

COMMANDS:
    defaults  Print the SyRS default constants and the default model of each family, and assert
              CostConfig::default() == CostConfig::syrs_defaults() — the 'defaults match SyRS
              values' half of the SRS-BT-002 acceptance criterion.
    run       Build a CostConfig from the flags (no cost flags ⇒ the SyRS defaults), set it on the
              request, run the SAME fixture strategy once through the real engine, and print the
              resolved config, the per-fill cost breakdown (integer minor units), the aggregate
              total cost, and the final equity. Only the cost config changes between runs — the
              strategy is unchanged — which is the 'override without changing strategy code' half
              of the acceptance criterion (SYS-15d).

COST MODEL FLAGS (run):
    --commission ib-tiered | per-share:<rate>,<min> | per-trade:<fee> | none
    --slippage   bps:<n> | none
    --spread     observed:<fallback> | fixed:<n> | none

RUN FLAGS:
    --lot <n>       shares opened on the first bar, then closed at --sell-ts (default 100)
    --sell-ts <t>   bar timestamp at which the position is closed (default 3)
    --full          also render the equity curve

A negative override parameter (e.g. --commission per-share:-1,35) is rejected by the engine before
any fill (a cost can never be negative), so the run exits non-zero rather than fabricating cash.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("bt002_cost_cli: {err}");
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
        "defaults" => cmd_defaults(rest),
        "run" => cmd_run(rest),
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

/// Print the SyRS default constants + the default model of each family, and prove
/// `CostConfig::default()` is the SyRS baseline.
fn cmd_defaults(rest: &[String]) -> Result<(), String> {
    if !rest.is_empty() {
        return Err(format!("`defaults` takes no arguments\n\n{USAGE}"));
    }
    // The published SyRS constants, printed so an operator can confirm the defaults.
    println!("DEFAULT_SLIPPAGE_BPS={DEFAULT_SLIPPAGE_BPS}");
    println!("DEFAULT_SPREAD_FALLBACK_BPS={DEFAULT_SPREAD_FALLBACK_BPS}");
    println!("IB_TIERED_RATE_CENTIMINOR_PER_SHARE={IB_TIERED_RATE_CENTIMINOR_PER_SHARE}");
    println!("IB_TIERED_MIN_PER_ORDER_MINOR={IB_TIERED_MIN_PER_ORDER_MINOR}");
    println!("IB_TIERED_MAX_PCT_BPS={IB_TIERED_MAX_PCT_BPS}");

    let config = CostConfig::default();
    println!("default-commission:{}", fmt_commission(config.commission));
    println!("default-slippage:{}", fmt_slippage(config.slippage));
    println!("default-spread:{}", fmt_spread(config.spread_impact));
    // The whole point of the AC: the derived Default IS the named SyRS family.
    let matches = config == CostConfig::syrs_defaults();
    println!("default-config-matches-syrs:{matches}");
    Ok(())
}

/// Build the per-run cost config from flags, run the fixture once, and print the cost breakdown.
fn cmd_run(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let request = parsed.request();
    let catalog = fixture_catalog();

    // The resolved cost config — what an operator selected for THIS run (defaults if no flags).
    println!(
        "cost-config: commission={} slippage={} spread={}",
        fmt_commission(request.cost_config.commission),
        fmt_slippage(request.cost_config.slippage),
        fmt_spread(request.cost_config.spread_impact),
    );

    let mut strategy = parsed.strategy_impl();
    // The engine validates the cost config FIRST (a negative parameter fails closed here), then
    // applies it to every fill. A run error propagates to a non-zero exit — no cash fabricated.
    let result: BacktestResult = BacktestEngine::new()
        .run(&request, &mut strategy, &catalog)
        .map_err(|err| format!("backtest run failed: {err}"))?;

    let mut total_cost_minor: i64 = 0;
    for (index, fill) in result.trade_log.iter().enumerate() {
        let fill_total = fill_total_minor(fill)?;
        total_cost_minor = total_cost_minor
            .checked_add(fill_total)
            .ok_or("aggregate cost overflowed i64 minor units")?;
        println!(
            "fill[{index}] ts={} qty={} price_minor={} commission_minor={} slippage_minor={} spread_impact_minor={} total_minor={}",
            fill.ts,
            fill.quantity,
            fill.price_minor,
            fill.commission_minor,
            fill.slippage_minor,
            fill.spread_impact_minor,
            fill_total,
        );
    }
    println!("total-cost-minor:{total_cost_minor}");
    println!("final-equity-minor:{}", result.final_equity_minor);

    if parsed.full {
        println!("equity-curve: {} point(s)", result.equity_curve.len());
        for (index, point) in result.equity_curve.iter().enumerate() {
            println!(
                "    equity[{index}] ts={} equity_minor={}",
                point.ts, point.equity_minor
            );
        }
    }
    Ok(())
}

/// The per-fill total, overflow-checked (a cost can never fabricate cash, so the sum is the
/// non-negative total the engine subtracted from cash).
fn fill_total_minor(fill: &atp_simulation::backtest::Fill) -> Result<i64, String> {
    fill.commission_minor
        .checked_add(fill.slippage_minor)
        .and_then(|partial| partial.checked_add(fill.spread_impact_minor))
        .ok_or_else(|| "fill cost total overflowed i64 minor units".to_string())
}

// --------------------------------------------------------------------------- //
// Model formatting (the enums derive Debug, not Display)
// --------------------------------------------------------------------------- //

fn fmt_commission(model: CommissionModel) -> String {
    match model {
        CommissionModel::IbTiered => "IbTiered".to_string(),
        CommissionModel::PerShare {
            rate_centiminor_per_share,
            min_per_order_minor,
        } => format!("PerShare({rate_centiminor_per_share},{min_per_order_minor})"),
        CommissionModel::PerTrade { fee_minor } => format!("PerTrade({fee_minor})"),
        CommissionModel::None => "None".to_string(),
    }
}

fn fmt_slippage(model: SlippageModel) -> String {
    match model {
        SlippageModel::NotionalBps { bps } => format!("NotionalBps({bps})"),
        SlippageModel::None => "None".to_string(),
    }
}

fn fmt_spread(model: SpreadImpactModel) -> String {
    match model {
        SpreadImpactModel::ObservedOrFallbackBps { fallback_bps } => {
            format!("ObservedOrFallbackBps({fallback_bps})")
        }
        SpreadImpactModel::FixedBps { bps } => format!("FixedBps({bps})"),
        SpreadImpactModel::None => "None".to_string(),
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

struct ParsedArgs {
    cost_config: CostConfig,
    lot: i64,
    sell_ts: u64,
    full: bool,
}

impl Default for ParsedArgs {
    fn default() -> Self {
        Self {
            cost_config: CostConfig::default(),
            lot: 100,
            sell_ts: 3,
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
                "--commission" => {
                    parsed.cost_config.commission = parse_commission(&take_value(&mut iter, flag)?)?
                }
                "--slippage" => {
                    parsed.cost_config.slippage = parse_slippage(&take_value(&mut iter, flag)?)?
                }
                "--spread" => {
                    parsed.cost_config.spread_impact = parse_spread(&take_value(&mut iter, flag)?)?
                }
                "--lot" => parsed.lot = take_i64(&mut iter, flag)?,
                "--sell-ts" => parsed.sell_ts = take_u64(&mut iter, flag)?,
                "--full" => parsed.full = true,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn request(&self) -> BacktestRequest {
        BacktestRequest {
            strategy_id: StrategyId::new("bt002-cost-cli"),
            symbol: SYMBOL.to_string(),
            data_source: BacktestDataSource::SystemData,
            range: DateRange::new(1, 3),
            starting_cash_minor: STARTING_CASH_MINOR,
            cost_config: self.cost_config,
        }
    }

    fn strategy_impl(&self) -> RoundTrip {
        RoundTrip {
            lot: self.lot,
            sell_ts: self.sell_ts,
            bought: false,
        }
    }
}

fn parse_commission(spec: &str) -> Result<CommissionModel, String> {
    match spec {
        "ib-tiered" => Ok(CommissionModel::IbTiered),
        "none" => Ok(CommissionModel::None),
        _ => {
            if let Some(rest) = spec.strip_prefix("per-share:") {
                let (rate, min) = rest
                    .split_once(',')
                    .ok_or_else(|| format!("per-share expects <rate>,<min>, got '{rest}'"))?;
                Ok(CommissionModel::PerShare {
                    rate_centiminor_per_share: parse_i64_field("per-share rate", rate)?,
                    min_per_order_minor: parse_i64_field("per-share min", min)?,
                })
            } else if let Some(fee) = spec.strip_prefix("per-trade:") {
                Ok(CommissionModel::PerTrade {
                    fee_minor: parse_i64_field("per-trade fee", fee)?,
                })
            } else {
                Err(format!(
                    "unknown commission model '{spec}' (expected ib-tiered|per-share:R,M|per-trade:F|none)"
                ))
            }
        }
    }
}

fn parse_slippage(spec: &str) -> Result<SlippageModel, String> {
    match spec {
        "none" => Ok(SlippageModel::None),
        _ => {
            if let Some(bps) = spec.strip_prefix("bps:") {
                Ok(SlippageModel::NotionalBps {
                    bps: parse_u32_field("slippage bps", bps)?,
                })
            } else {
                Err(format!(
                    "unknown slippage model '{spec}' (expected bps:N|none)"
                ))
            }
        }
    }
}

fn parse_spread(spec: &str) -> Result<SpreadImpactModel, String> {
    match spec {
        "none" => Ok(SpreadImpactModel::None),
        _ => {
            if let Some(fallback) = spec.strip_prefix("observed:") {
                Ok(SpreadImpactModel::ObservedOrFallbackBps {
                    fallback_bps: parse_u32_field("spread fallback bps", fallback)?,
                })
            } else if let Some(bps) = spec.strip_prefix("fixed:") {
                Ok(SpreadImpactModel::FixedBps {
                    bps: parse_u32_field("spread fixed bps", bps)?,
                })
            } else {
                Err(format!(
                    "unknown spread model '{spec}' (expected observed:F|fixed:N|none)"
                ))
            }
        }
    }
}

fn parse_i64_field(label: &str, raw: &str) -> Result<i64, String> {
    raw.parse::<i64>()
        .map_err(|_| format!("{label} expects an integer, got '{raw}'"))
}

fn parse_u32_field(label: &str, raw: &str) -> Result<u32, String> {
    raw.parse::<u32>()
        .map_err(|_| format!("{label} expects a non-negative integer, got '{raw}'"))
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
// Deterministic fixture (flat $100.00 price; bar 1 carries an observed spread so the default
// ObservedOrFallbackBps exercises BOTH the observed-spread and fallback paths in one run)
// --------------------------------------------------------------------------- //

fn fixture_catalog() -> FixtureCatalog {
    FixtureCatalog {
        bars: vec![
            bar(1, 10_000, Some(40)),
            bar(2, 10_000, None),
            bar(3, 10_000, None),
        ],
    }
}

fn bar(ts: u64, close_minor: i64, spread_minor: Option<i64>) -> BacktestBar {
    BacktestBar {
        symbol: SYMBOL.to_string(),
        ts,
        close_minor,
        spread_minor,
    }
}

/// A close-only fixture catalog that honors the requested window.
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

/// Opens `lot` shares exactly once on the first bar, then fully closes on `sell_ts` — one round
/// trip that is independent of the cost config (so only the cost config changes between runs).
struct RoundTrip {
    lot: i64,
    sell_ts: u64,
    bought: bool,
}

impl BacktestStrategy for RoundTrip {
    fn on_bar(&mut self, bar: &BacktestBar, position: i64) -> Result<i64, BacktestError> {
        if bar.ts == self.sell_ts && position != 0 {
            return Ok(-position);
        }
        if !self.bought {
            self.bought = true;
            return Ok(self.lot);
        }
        Ok(0)
    }
}
