//! SRS-BT-003 shared-cost-family operator CLI.
//!
//! The operator-facing surface of "use the same transaction-cost model family for internal
//! simulation and backtesting unless configured otherwise" (docs/SRS.md SRS-5.6 SRS-BT-003; SyRS
//! SYS-15e / SYS-83d; StRS SN-1.03 / SN-1.29). The acceptance criterion is a *comparison between two
//! engines*: "a paper strategy and backtest using identical cost configuration compute fills and
//! commissions from the same model family". The shared cost-model *family* (the `cost` module,
//! SRS-BT-002) is already consumed by both the runnable [`BacktestEngine`] and the
//! [`PaperSimulationEngine`] (the `sim` module), and the Rust integration test
//! `srs_bt_003_shared_cost_family` already asserts the per-fill decomposition is equal between them.
//! This binary makes that equality *operator-demonstrable* — the same precedent as the SRS-BT-002
//! cost CLI and the SRS-BT-009 / SRS-BT-010 CLIs (there is no Python↔Rust strategy host, so the
//! operator workflow is demonstrated over the Rust core).
//!
//! - `defaults` — print the default cost model of the paper simulation engine and the backtest
//!   engine and prove they are the SAME family: `PaperSimulationEngine::default().cost_config()`
//!   equals `CostConfig::default()` equals `CostConfig::syrs_defaults()` (SYS-15e "same default …
//!   unless explicitly configured otherwise"). This is the "same model family" half made inspectable.
//! - `compare [--commission M] [--slippage M] [--spread M] [--lot N] [--sell-ts T] [--inject F]
//!   [--full]` — build one [`CostConfig`] from the flags (no cost flags ⇒ `CostConfig::default()`,
//!   the shared SyRS baseline), then run the SAME fixture round-trip strategy over the SAME fixture
//!   bars through BOTH engines: the real [`BacktestEngine`] and a paper-strategy replay that drives
//!   the identical [`BacktestStrategy`] decisions through [`PaperSimulationEngine::simulate_fill`]
//!   into a [`PaperLedger`]. It prints, fill by fill, the per-fill cost decomposition produced by
//!   each engine (commission / slippage / spread-impact, in integer minor units) and asserts they
//!   are EQUAL — the headline `cost-family-match:true` is the acceptance criterion, made falsifiable.
//!   Only the cost config is shared; the strategy and bars are identical, so any divergence would be
//!   a real shared-family regression.
//!
//! Override grammar (parsed to the existing enums, identical to bt002_cost_cli):
//!   --commission ib-tiered | per-share:RATE,MIN | per-trade:FEE | none
//!   --slippage   bps:N | none
//!   --spread     observed:FALLBACK | fixed:N | none
//!
//! Fail closed: an unknown subcommand or flag exits non-zero. `--inject <fault>` corrupts a SHARED
//! input (a non-positive price, a negative observed spread, or a misconfigured negative cost
//! parameter); because BOTH engines apply the SAME family, BOTH reject it before any fill, so the
//! CLI prints `inject=<fault>: both engines failed closed` and exits non-zero with NO comparison
//! line — a cash-fabricating fault can never produce a report. If only one engine were to reject a
//! fault the other accepted, that would be a shared-family divergence and the CLI also exits
//! non-zero (a different, louder message).

use std::env;
use std::process::ExitCode;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest, BacktestResult,
    BacktestStrategy, BarSource, DateRange, Fill,
};
use atp_simulation::cost::{CommissionModel, CostConfig, SlippageModel, SpreadImpactModel};
use atp_simulation::sim::{PaperFill, PaperLedger, PaperSimulationEngine};
use atp_types::StrategyId;

const SYMBOL: &str = "AAPL";
const STARTING_CASH_MINOR: i64 = 10_000_000; // $100,000.00

const USAGE: &str = "\
bt003_shared_cost_cli — SRS-BT-003 shared transaction-cost-family operator workflow

USAGE:
    bt003_shared_cost_cli defaults
    bt003_shared_cost_cli compare [--commission <m>] [--slippage <m>] [--spread <m>]
                                  [--lot <n>] [--sell-ts <t>] [--inject <fault>] [--full]

COMMANDS:
    defaults  Print the default cost model of the paper simulation engine and the backtest engine
              and prove they are the SAME family: PaperSimulationEngine::default().cost_config() ==
              CostConfig::default() == CostConfig::syrs_defaults() (SYS-15e). The 'same model
              family unless configured otherwise' half of the acceptance criterion, made inspectable.
    compare   Build one CostConfig from the flags (no cost flags ⇒ the shared SyRS defaults) and run
              the SAME fixture strategy over the SAME bars through BOTH the real BacktestEngine and a
              PaperSimulationEngine replay, then print each engine's per-fill cost decomposition and
              assert they are EQUAL (headline cost-family-match:true). This is the acceptance
              criterion 'a paper strategy and backtest using identical cost configuration compute
              fills and commissions from the same model family', made falsifiable.

COST MODEL FLAGS (compare):
    --commission ib-tiered | per-share:<rate>,<min> | per-trade:<fee> | none
    --slippage   bps:<n> | none
    --spread     observed:<fallback> | fixed:<n> | none

RUN FLAGS (compare):
    --lot <n>       shares opened on the first bar, then closed at --sell-ts (default 100)
    --sell-ts <t>   bar timestamp at which the position is closed (default 3)
    --inject <f>    corrupt a SHARED input so BOTH engines must fail closed before any fill; one of:
                    nonpositive-price | negative-spread | negative-commission
    --full          also render the backtest final equity and the paper ledger balances

A negative override parameter (e.g. --commission per-share:-1,35) or an injected fault is rejected
by BOTH engines before any fill, so the comparison exits non-zero rather than fabricating cash.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("bt003_shared_cost_cli: {err}");
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
        "compare" => cmd_compare(rest),
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

/// True if any token requests help, so a subcommand can show usage instead of erroring.
fn wants_help(args: &[String]) -> bool {
    args.iter()
        .any(|arg| matches!(arg.as_str(), "help" | "--help" | "-h"))
}

/// Prove the paper engine's default cost family IS the backtest engine's default family.
fn cmd_defaults(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if !rest.is_empty() {
        return Err(format!("`defaults` takes no arguments\n\n{USAGE}"));
    }
    let sim_default = PaperSimulationEngine::default();
    let backtest_default = CostConfig::default();

    println!("sim-default-commission:{}", fmt_commission(sim_default.cost_config().commission));
    println!("sim-default-slippage:{}", fmt_slippage(sim_default.cost_config().slippage));
    println!("sim-default-spread:{}", fmt_spread(sim_default.cost_config().spread_impact));

    // The whole point of SYS-15e: the paper engine's default family IS the backtest default, which
    // IS the published SyRS baseline. Both are shown as booleans an operator can confirm.
    let sim_matches_backtest = sim_default.cost_config() == &backtest_default;
    let backtest_matches_syrs = backtest_default == CostConfig::syrs_defaults();
    println!("sim-default-matches-backtest-default:{sim_matches_backtest}");
    println!("backtest-default-matches-syrs:{backtest_matches_syrs}");
    println!("same-cost-family:{}", sim_matches_backtest && backtest_matches_syrs);
    Ok(())
}

/// Run the same strategy through both engines under one cost config and prove the per-fill cost
/// decomposition is identical.
fn cmd_compare(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let parsed = ParsedArgs::parse(rest)?;
    let cost_config = parsed.injected_cost_config();
    let bars = parsed.fixture_bars();

    // The resolved cost config — the ONE family fed to BOTH engines for this run.
    println!(
        "cost-config: commission={} slippage={} spread={}",
        fmt_commission(cost_config.commission),
        fmt_slippage(cost_config.slippage),
        fmt_spread(cost_config.spread_impact),
    );
    if let Some(fault) = parsed.inject {
        println!("inject:{}", fault.as_str());
    }

    // Run both legs over IDENTICAL inputs. On a fault, both must fail closed; the helper turns the
    // pair of results into either a clean (backtest, paper) success or a fail-closed error.
    let backtest = run_backtest(cost_config, &bars, parsed.lot, parsed.sell_ts);
    let paper = run_paper(cost_config, &bars, parsed.lot, parsed.sell_ts);

    let (backtest, paper) = match reconcile(parsed.inject, backtest, paper)? {
        Reconciled::FailedClosed { fault, backtest_err, paper_err } => {
            // A corrupt SHARED input was rejected by BOTH engines before any fill — print the
            // fail-closed evidence and exit non-zero with NO comparison line (no cash fabricated).
            return Err(format!(
                "inject={fault}: both engines failed closed (backtest: {backtest_err}; paper: {paper_err})"
            ));
        }
        Reconciled::Compared { backtest, paper } => (backtest, paper),
    };

    // Compare fill-for-fill. Both legs drove the SAME strategy over the SAME bars, so the fill
    // sequences line up one-to-one; any mismatch in the cost decomposition is a shared-family
    // regression and fails the comparison.
    if backtest.trade_log.len() != paper.fills.len() {
        return Err(format!(
            "fill-count divergence: backtest produced {} fill(s), paper produced {} — the same \
             strategy over the same bars must produce the same fills",
            backtest.trade_log.len(),
            paper.fills.len()
        ));
    }

    // Refuse to assert a match over ZERO fills: cost-family-match:true is only meaningful if at
    // least one real fill's cost decomposition was actually compared between the two engines.
    if backtest.trade_log.is_empty() {
        return Err("no fills were produced — refusing to assert cost-family-match over zero fills \
                    (a vacuous proof); choose inputs that trade at least once"
            .to_string());
    }

    let mut all_match = true;
    for (index, (bt, pf)) in backtest.trade_log.iter().zip(paper.fills.iter()).enumerate() {
        let matched = fills_agree(bt, pf);
        all_match &= matched;
        println!(
            "fill[{index}] ts={} qty={} price_minor={} | backtest comm={} slip={} spread={} | \
             paper comm={} slip={} spread={} | match={matched}",
            bt.ts,
            bt.quantity,
            bt.price_minor,
            bt.commission_minor,
            bt.slippage_minor,
            bt.spread_impact_minor,
            pf.commission_minor,
            pf.slippage_minor,
            pf.spread_impact_minor,
        );
    }

    println!("fills-compared:{}", backtest.trade_log.len());
    println!("cost-family-match:{all_match}");

    if parsed.full {
        println!("backtest-final-equity-minor:{}", backtest.final_equity_minor);
        println!("paper-ledger-cash-minor:{}", paper.ledger.cash_minor);
        println!("paper-ledger-position:{}", paper.ledger.position);
        println!(
            "paper-ledger-commission-paid-minor:{}",
            paper.ledger.commission_paid_minor
        );
    }

    if !all_match {
        // The acceptance criterion is the MATCH; a divergence is a hard failure, not a warning.
        return Err("cost-family-match:false — the simulation and backtest cost decompositions \
                    diverged for identical config and inputs (SRS-BT-003 regression)"
            .to_string());
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// The two engine legs
// --------------------------------------------------------------------------- //

/// Run the fixture round trip through the real backtest engine.
fn run_backtest(
    cost_config: CostConfig,
    bars: &[BacktestBar],
    lot: i64,
    sell_ts: u64,
) -> Result<BacktestResult, String> {
    let catalog = FixtureCatalog { bars: bars.to_vec() };
    let request = BacktestRequest {
        strategy_id: StrategyId::new("bt003-shared-cost-cli"),
        symbol: SYMBOL.to_string(),
        data_source: BacktestDataSource::SystemData,
        range: DateRange::new(1, sell_ts.max(1)),
        starting_cash_minor: STARTING_CASH_MINOR,
        cost_config,
    };
    let mut strategy = RoundTrip {
        lot,
        sell_ts,
        bought: false,
    };
    BacktestEngine::new()
        .run(&request, &mut strategy, &catalog)
        .map_err(|err| err.to_string())
}

/// The result of replaying the same strategy through the paper simulation engine.
struct PaperRun {
    fills: Vec<PaperFill>,
    ledger: PaperLedger,
}

/// Drive the IDENTICAL strategy decisions over the SAME bars through the paper simulation engine,
/// accumulating each fill into a virtual ledger — the "paper strategy" leg of the acceptance
/// criterion. The bars are replayed in the same order the backtest engine replays them (sorted,
/// in-window), so the two fill sequences are directly comparable.
fn run_paper(
    cost_config: CostConfig,
    bars: &[BacktestBar],
    lot: i64,
    sell_ts: u64,
) -> Result<PaperRun, String> {
    let engine = PaperSimulationEngine::with_cost_config(cost_config);
    let mut ledger = PaperLedger::new(STARTING_CASH_MINOR);
    let mut strategy = RoundTrip {
        lot,
        sell_ts,
        bought: false,
    };

    let mut replay = replay_bars(bars, sell_ts);
    replay.sort_by_key(|bar| bar.ts);

    let mut position: i64 = 0;
    let mut fills = Vec::new();
    for bar in &replay {
        let delta = strategy
            .on_bar(bar, position)
            .map_err(|err| format!("paper strategy failed: {err}"))?;
        if delta == 0 {
            continue;
        }
        let fill = engine
            .simulate_fill(bar.ts, &bar.symbol, delta, bar.close_minor, bar.spread_minor)
            .map_err(|err| err.to_string())?;
        ledger.apply_fill(&fill).map_err(|err| err.to_string())?;
        position = position
            .checked_add(delta)
            .ok_or_else(|| "paper replay position overflowed i64".to_string())?;
        fills.push(fill);
    }
    Ok(PaperRun { fills, ledger })
}

/// The in-window replay set for the paper leg (the backtest engine restricts replay to its
/// `DateRange`; the paper leg mirrors that so both engines see the same bars).
fn replay_bars(bars: &[BacktestBar], sell_ts: u64) -> Vec<BacktestBar> {
    let range = DateRange::new(1, sell_ts.max(1));
    bars.iter()
        .filter(|bar| range.contains(bar.ts))
        .cloned()
        .collect()
}

/// Reconcile the two engine results under the inject contract: with `--inject`, BOTH must fail
/// closed; without it, BOTH must succeed.
enum Reconciled {
    FailedClosed {
        fault: &'static str,
        backtest_err: String,
        paper_err: String,
    },
    Compared {
        backtest: BacktestResult,
        paper: PaperRun,
    },
}

fn reconcile(
    inject: Option<Fault>,
    backtest: Result<BacktestResult, String>,
    paper: Result<PaperRun, String>,
) -> Result<Reconciled, String> {
    match (inject, backtest, paper) {
        // No fault: both must succeed; a stray error propagates.
        (None, Ok(backtest), Ok(paper)) => Ok(Reconciled::Compared { backtest, paper }),
        (None, Err(err), _) => Err(format!("backtest leg failed unexpectedly: {err}")),
        (None, _, Err(err)) => Err(format!("paper leg failed unexpectedly: {err}")),
        // Fault injected: BOTH engines must reject it (the shared fail-closed safety).
        (Some(fault), Err(backtest_err), Err(paper_err)) => Ok(Reconciled::FailedClosed {
            fault: fault.as_str(),
            backtest_err,
            paper_err,
        }),
        // The worst case: a cash-fabricating fault that NEITHER engine rejected. The whole point of
        // the shared family is that this can never happen — surface it as a hard safety failure.
        (Some(fault), Ok(_), Ok(_)) => Err(format!(
            "inject={}: SAFETY FAILURE — both engines ACCEPTED a fault that should fail closed; \
             neither fabrication guard fired",
            fault.as_str()
        )),
        // A fault rejected by only one engine is a shared-family divergence — louder than a plain
        // fail-closed, because it means the two engines do NOT share the same safety.
        (Some(fault), Ok(_), Err(paper_err)) => Err(format!(
            "inject={}: shared-family divergence — the backtest engine ACCEPTED a fault the paper \
             engine rejected ({paper_err})",
            fault.as_str()
        )),
        (Some(fault), Err(backtest_err), Ok(_)) => Err(format!(
            "inject={}: shared-family divergence — the paper engine ACCEPTED a fault the backtest \
             engine rejected ({backtest_err})",
            fault.as_str()
        )),
    }
}

/// Two fills agree iff every cost component matches (the SRS-BT-003 acceptance criterion). The
/// quantity and price are also checked so a mis-aligned comparison can never read as a match.
fn fills_agree(bt: &Fill, pf: &PaperFill) -> bool {
    bt.ts == pf.ts
        && bt.quantity == pf.quantity
        && bt.price_minor == pf.price_minor
        && bt.commission_minor == pf.commission_minor
        && bt.slippage_minor == pf.slippage_minor
        && bt.spread_impact_minor == pf.spread_impact_minor
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

/// A shared-input fault to inject so BOTH engines must fail closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fault {
    NonPositivePrice,
    NegativeSpread,
    NegativeCommission,
}

impl Fault {
    fn parse(spec: &str) -> Result<Self, String> {
        match spec {
            "nonpositive-price" => Ok(Self::NonPositivePrice),
            "negative-spread" => Ok(Self::NegativeSpread),
            "negative-commission" => Ok(Self::NegativeCommission),
            other => Err(format!(
                "unknown fault '{other}' (expected nonpositive-price|negative-spread|negative-commission)"
            )),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::NonPositivePrice => "nonpositive-price",
            Self::NegativeSpread => "negative-spread",
            Self::NegativeCommission => "negative-commission",
        }
    }
}

struct ParsedArgs {
    cost_config: CostConfig,
    lot: i64,
    sell_ts: u64,
    inject: Option<Fault>,
    full: bool,
}

impl Default for ParsedArgs {
    fn default() -> Self {
        Self {
            cost_config: CostConfig::default(),
            lot: 100,
            sell_ts: 3,
            inject: None,
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
                "--inject" => parsed.inject = Some(Fault::parse(&take_value(&mut iter, flag)?)?),
                "--full" => parsed.full = true,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        // A non-positive lot would trade nothing, so both engines would produce ZERO fills and the
        // comparison would vacuously "match" without ever exercising the shared cost family. Reject
        // it at parse time so the proof can never be empty.
        if parsed.lot <= 0 {
            return Err(format!(
                "--lot must be a positive share count (got {}); a non-positive lot would compare \
                 zero fills and prove nothing",
                parsed.lot
            ));
        }
        Ok(parsed)
    }

    /// The cost config for this run, with a negative-commission fault applied if requested.
    fn injected_cost_config(&self) -> CostConfig {
        if self.inject == Some(Fault::NegativeCommission) {
            CostConfig {
                commission: CommissionModel::PerShare {
                    rate_centiminor_per_share: -1,
                    min_per_order_minor: 0,
                },
                ..self.cost_config
            }
        } else {
            self.cost_config
        }
    }

    /// The fixture bars, with an injected close or spread fault applied to the first bar if
    /// requested. Bar 1
    /// carries an observed spread so the default ObservedOrFallbackBps exercises both the
    /// observed-spread and fallback paths within one run.
    fn fixture_bars(&self) -> Vec<BacktestBar> {
        let (close_1, spread_1) = match self.inject {
            Some(Fault::NonPositivePrice) => (0, Some(40)),
            Some(Fault::NegativeSpread) => (10_000, Some(-1)),
            _ => (10_000, Some(40)),
        };
        vec![
            bar(1, close_1, spread_1),
            bar(2, 10_000, None),
            bar(3, 10_000, None),
        ]
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
                Err(format!("unknown slippage model '{spec}' (expected bps:N|none)"))
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
// Deterministic fixture
// --------------------------------------------------------------------------- //

fn bar(ts: u64, close_minor: i64, spread_minor: Option<i64>) -> BacktestBar {
    BacktestBar {
        symbol: SYMBOL.to_string(),
        ts,
        close_minor,
        spread_minor,
    }
}

/// A close-only fixture catalog that honors the requested window (the same shape bt002_cost_cli
/// uses, so the backtest leg sees system data).
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
/// trip, independent of the cost config (only the cost config is shared between the two engines).
/// The SAME struct drives both engines, so the fill *decisions* are guaranteed identical.
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
