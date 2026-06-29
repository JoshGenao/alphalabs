//! Deterministic backtest engine surface for **SRS-BT-001** — "backtest Python
//! strategies against stored data and user-uploaded Parquet data over
//! configurable date ranges" (SyRS SYS-14 / SYS-43a; StRS SN-1.02 / SN-1.13 / C-4).
//!
//! # Why this lives in `atp-simulation`
//!
//! The SRS module table lists "Backtesting and Optimization" as a distinct Rust
//! runtime, but **SRS-BT-003** mandates the *same transaction-cost model family*
//! for the internal simulation engine and for backtesting. Co-locating the
//! backtest engine with the [`InternalSimulationEngine`](crate::InternalSimulationEngine)
//! keeps that shared fill/cost model in one crate, which is the operator-directed
//! home for this slice.
//!
//! # What is real here vs deferred
//!
//! This module is a **genuinely runnable** deterministic engine: given a
//! [`BacktestRequest`] (a configurable [`DateRange`], a [`BacktestDataSource`],
//! and a starting cash balance), a [`BarSource`], and a [`BacktestStrategy`], it
//! replays bars in deterministic order, drives the strategy bar-by-bar, applies
//! deterministic fills, and produces a real trade log + equity curve. All money
//! math is in **integer minor units (`i64` cents)** with `i128` intermediates and
//! `checked_*` arithmetic — never floating point — so it is exact and
//! overflow-safe.
//!
//! The pieces required to flip SRS-BT-001 to `passes:true` are deferred behind the
//! [`BarSource`] and [`BacktestStrategy`] ports (see
//! `architecture/runtime_services.json#backtest_contract.deferred`):
//! the real `arrow`/`parquet`-backed reader for uploaded files, the Python
//! strategy execution host, and the REST/dashboard launch surface (SRS-API-001 /
//! SRS-UI). An operator **CLI** launch surface for the *system-data + configurable
//! date-range* halves now exists — the `bt001_backtest_cli` binary launches this
//! engine over a `--start/--end` calendar window via the real
//! [`crate::store_bar_source::StoreBarSource`] (the date binding is
//! [`crate::launch::parse_window`]) — but it consumes fixtures and a fixture
//! [`BacktestStrategy`], so SRS-BT-001 stays `passes:false` until the deferred
//! Parquet reader, Python host, and REST/dashboard surface land. Configurable cost
//! models (SRS-BT-002 / SRS-BT-003) are applied here and now closed; the full metric
//! set (SRS-BT-004) and persisted results (SRS-BT-009) build on this engine. The
//! deterministic-replay guarantee modeled here is the forward seam for SRS-BT-010.

use std::fmt;

use atp_types::StrategyId;

use crate::cost::{CostConfig, CostError};

/// Which catalog a backtest reads its bars from.
///
/// SRS-BT-001 requires a backtest to be "launched with system data **or**
/// uploaded Parquet data". Both flow through the [`BarSource`] port in this
/// slice; the port declares its own identity via [`BarSource::source`] and the
/// engine fails closed ([`BacktestError::DataSourceMismatch`]) when the request's
/// `data_source` disagrees, so the result's provenance is trustworthy. The real
/// per-source readers (the Databento/IB-backed system catalog and the
/// `parquet`-backed uploaded-file reader) are deferred behind the port.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BacktestDataSource {
    /// The platform's stored historical catalog, read via
    /// [`crate::store_bar_source::StoreBarSource`] over the unified `MarketDataStore`
    /// (SRS-DATA-007) — the source-neutral system-catalog reader.
    SystemData,
    /// A user-uploaded Parquet dataset (deferred owner: the `parquet` reader).
    UploadedData,
}

impl BacktestDataSource {
    /// Stable wire token for logs / API payloads.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::SystemData => "system_data",
            Self::UploadedData => "uploaded_data",
        }
    }
}

/// A configurable, inclusive `[start, end]` backtest window.
///
/// `start` and `end` are opaque ordered timestamps (the deterministic time axis);
/// binding them to wall-clock calendar dates is the launch surface's concern.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DateRange {
    pub start: u64,
    pub end: u64,
}

impl DateRange {
    pub fn new(start: u64, end: u64) -> Self {
        Self { start, end }
    }

    /// Fail closed on an inverted window (`start > end`). A single-instant range
    /// (`start == end`) is valid.
    pub fn validate(&self) -> Result<(), BacktestError> {
        if self.start > self.end {
            return Err(BacktestError::InvalidDateRange {
                start: self.start,
                end: self.end,
            });
        }
        Ok(())
    }

    /// Whether `ts` falls within the inclusive window.
    pub fn contains(&self, ts: u64) -> bool {
        ts >= self.start && ts <= self.end
    }
}

/// One bar of market data with an **integer minor-unit** close price.
///
/// `close_minor` is the close in the smallest currency unit (e.g. cents) so all
/// downstream money math stays exact. Converting a vendor floating-point close
/// into minor units happens at the (deferred) adapter boundary, never here.
///
/// `spread_minor` is the bar's **observed bid-ask spread** in minor units, when
/// the data source provides quote data (`None` for close-only daily bars). The
/// default spread-impact model (SYS-15c) uses it when present and falls back to a
/// fixed fraction of notional when it is `None`. A negative observed spread is
/// corrupt quote data and the engine fails closed on it before any fill.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BacktestBar {
    pub symbol: String,
    pub ts: u64,
    pub close_minor: i64,
    pub spread_minor: Option<i64>,
}

/// Source of historical bars for a backtest — the deferred Parquet/system-catalog
/// reader seam.
///
/// A real implementation queries stored data (or a user-uploaded Parquet file) for
/// `symbol` within `range`. Implementations may return a superset of the window;
/// the engine authoritatively restricts replay to `range`, so date-range selection
/// is an engine guarantee regardless of source behavior.
pub trait BarSource {
    /// Which catalog this source reads from. The engine validates it against the
    /// request's [`BacktestDataSource`] so a run cannot report the wrong dataset
    /// (e.g. replay the system catalog while marking the result `UploadedData`).
    fn source(&self) -> BacktestDataSource;

    /// Return this source's bars for `symbol` within `range`, reading **at most**
    /// `max_bars` rows. A source whose response would exceed `max_bars` must fail
    /// closed with [`BacktestError::TooManyBars`] rather than materialize an
    /// unbounded `Vec` — so a large uploaded Parquet file cannot exhaust memory
    /// inside the reader. (The fully streaming/bounded cursor that avoids any
    /// materialization is the deferred completion; see `backtest_contract`.)
    fn bars(
        &self,
        symbol: &str,
        range: &DateRange,
        max_bars: usize,
    ) -> Result<Vec<BacktestBar>, BacktestError>;
}

/// The strategy execution boundary — the Rust stand-in for the deferred Python
/// strategy host.
///
/// `on_bar` is handed each bar and the current signed position, and returns the
/// signed quantity delta to trade at this bar's close (`> 0` buy, `< 0` sell,
/// `0` hold). The real boundary marshals these calls to a Python strategy running
/// under the orchestrator, where user code can raise, time out, or fail to
/// marshal — so the port is **fallible**: a failure is surfaced as
/// [`BacktestError::StrategyFailed`] and the engine aborts before applying any
/// fill for that bar, rather than silently treating the failure as a `0` delta.
pub trait BacktestStrategy {
    fn on_bar(&mut self, bar: &BacktestBar, position: i64) -> Result<i64, BacktestError>;
}

/// A backtest launch request.
///
/// `cost_config` carries the per-run transaction-cost model family (SRS-BT-002):
/// it defaults to the SyRS baseline ([`CostConfig::default`]) and an operator
/// overrides it for an individual run (SYS-15d) without changing strategy code —
/// the override lives on the request, not in the strategy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BacktestRequest {
    pub strategy_id: StrategyId,
    pub symbol: String,
    pub data_source: BacktestDataSource,
    pub range: DateRange,
    pub starting_cash_minor: i64,
    pub cost_config: CostConfig,
}

/// A single deterministic fill recorded in the trade log.
///
/// `price_minor` is the bar close the fill is referenced to; the configured
/// transaction-cost models (SRS-BT-002) are recorded as the separate
/// `commission_minor` / `slippage_minor` / `spread_impact_minor` components (each
/// non-negative) rather than folded into the price, so the cost decomposition is
/// transparent for downstream metrics (SRS-BT-004).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fill {
    pub ts: u64,
    pub symbol: String,
    pub quantity: i64,
    pub price_minor: i64,
    pub commission_minor: i64,
    pub slippage_minor: i64,
    pub spread_impact_minor: i64,
}

/// One point on the equity curve (mark-to-market at a bar close).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EquityPoint {
    pub ts: u64,
    pub equity_minor: i64,
}

/// The completed backtest output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BacktestResult {
    pub data_source: BacktestDataSource,
    pub range: DateRange,
    pub bars_processed: u64,
    pub trade_log: Vec<Fill>,
    pub equity_curve: Vec<EquityPoint>,
    pub final_equity_minor: i64,
}

/// Fail-closed backtest errors. Carries no broker/vendor identifiers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BacktestError {
    /// The request symbol was empty / whitespace.
    EmptySymbol,
    /// The window was inverted (`start > end`).
    InvalidDateRange { start: u64, end: u64 },
    /// The source returned a bar for a different symbol than requested. The
    /// uploaded-Parquet / system-catalog reader is a trust boundary: a source
    /// that mixes symbols must fail closed, never silently trade foreign prices.
    UnexpectedSymbol { expected: String, found: String },
    /// A replayed bar carried a non-positive close price (corrupt market data).
    /// Signed `close_minor` is rejected before any fill so a negative price can
    /// never fabricate cash or equity.
    NonPositivePrice { ts: u64, close_minor: i64 },
    /// Two bars shared the same timestamp for the (single) requested symbol.
    /// Duplicate market records are a known data-quality failure class; the
    /// engine rejects them so replay stays deterministic and never double-fills
    /// a single market instant.
    DuplicateBar { ts: u64 },
    /// No bars fell within the requested window.
    EmptyData,
    /// The strategy failed to produce a decision for a bar (the deferred Python
    /// host raised, timed out, or could not be marshaled). The engine aborts
    /// before applying any fill for that bar.
    StrategyFailed { ts: u64, reason: String },
    /// Money math exceeded `i64` minor-unit range.
    Overflow,
    /// The source returned more bars than the engine will replay in memory.
    /// A fail-closed guard against a pathological / malicious upload exhausting
    /// memory before the deferred streaming reader lands.
    TooManyBars { count: usize, limit: usize },
    /// The provided `BarSource` reads from a different catalog than the request
    /// named, so the result's data-source provenance would be wrong.
    DataSourceMismatch {
        requested: BacktestDataSource,
        actual: BacktestDataSource,
    },
    /// The bar source could not produce bars for the requested window. The stored
    /// system-catalog reader ([`crate::store_bar_source::StoreBarSource`], SRS-DATA-007)
    /// surfaces three fail-closed conditions through this one variant: a split-adjusted
    /// read refused because corporate-action coverage is not proven through the query end
    /// (SRS-DATA-011), a stored bar missing its `close` field, or a window bound that is
    /// not a representable timestamp. Fail-closed: the engine never trades on a source it
    /// could not read, and never silently substitutes raw bars for a refused adjusted read.
    /// `reason` is a source-neutral diagnostic carrying no broker/vendor identifier.
    SourceUnavailable { reason: String },
    /// A replayed bar carried a negative observed bid-ask spread (corrupt quote
    /// data). Rejected before any fill so a negative spread can never produce a
    /// cash-fabricating spread-impact cost (mirrors the `NonPositivePrice` guard).
    NegativeSpread { ts: u64, spread_minor: i64 },
    /// A configured transaction-cost model rejected the run or a fill (SRS-BT-002
    /// cost-model family — e.g. a negative parameter, a negative spread, or
    /// cost-math overflow). Carries the underlying [`CostError`].
    Cost(CostError),
}

impl From<CostError> for BacktestError {
    fn from(error: CostError) -> Self {
        Self::Cost(error)
    }
}

impl fmt::Display for BacktestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptySymbol => write!(f, "backtest request symbol must not be empty"),
            Self::InvalidDateRange { start, end } => {
                write!(f, "invalid backtest date range: start {start} > end {end}")
            }
            Self::UnexpectedSymbol { expected, found } => write!(
                f,
                "backtest source returned a bar for {found}, expected {expected}"
            ),
            Self::NonPositivePrice { ts, close_minor } => write!(
                f,
                "backtest bar at ts {ts} has a non-positive close {close_minor} minor units"
            ),
            Self::DuplicateBar { ts } => {
                write!(f, "backtest source returned duplicate bars at ts {ts}")
            }
            Self::EmptyData => write!(f, "no bars in the requested backtest date range"),
            Self::StrategyFailed { ts, reason } => {
                write!(f, "backtest strategy failed at ts {ts}: {reason}")
            }
            Self::Overflow => write!(f, "backtest money math overflowed i64 minor units"),
            Self::TooManyBars { count, limit } => write!(
                f,
                "backtest source returned {count} bars, exceeding the {limit}-bar replay limit"
            ),
            Self::DataSourceMismatch { requested, actual } => write!(
                f,
                "backtest source is {} but the request named {}",
                actual.as_str(),
                requested.as_str()
            ),
            Self::SourceUnavailable { reason } => {
                write!(f, "backtest bar source unavailable: {reason}")
            }
            Self::NegativeSpread { ts, spread_minor } => write!(
                f,
                "backtest bar at ts {ts} has a negative observed spread {spread_minor} minor units"
            ),
            Self::Cost(error) => write!(f, "backtest cost model rejected the run: {error}"),
        }
    }
}

impl std::error::Error for BacktestError {}

/// Exact integer notional `quantity * price_minor`, computed in `i128` to detect
/// overflow before narrowing back to `i64` minor units. Money math never uses
/// floating point.
fn checked_notional(quantity: i64, price_minor: i64) -> Result<i64, BacktestError> {
    let product = i128::from(quantity) * i128::from(price_minor);
    i64::try_from(product).map_err(|_| BacktestError::Overflow)
}

/// Default upper bound on the number of bars a single backtest may replay. The
/// engine runs fully in memory; this fail-closed cap stops a pathological or
/// malicious upload from exhausting memory in the sort + equity-curve
/// allocation. The deferred streaming [`BarSource`] (see
/// `backtest_contract.deferred`) supersedes it for the real Parquet reader.
pub const MAX_BACKTEST_BARS: usize = 5_000_000;

/// The deterministic backtest engine.
#[derive(Debug, Clone)]
pub struct BacktestEngine {
    max_bars: usize,
}

impl Default for BacktestEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl BacktestEngine {
    pub fn new() -> Self {
        Self {
            max_bars: MAX_BACKTEST_BARS,
        }
    }

    /// Construct with a custom replay cap (bounded launches / tests).
    pub fn with_max_bars(max_bars: usize) -> Self {
        Self { max_bars }
    }

    /// Run `strategy` over `source`'s bars for `request`, returning the trade log,
    /// equity curve, and final equity. Identical inputs always produce an identical
    /// [`BacktestResult`] (the SRS-BT-010 determinism seam): bars are restricted to
    /// the configurable window and replayed in a stable `ts` order, and every fill
    /// and mark uses exact integer minor-unit arithmetic.
    pub fn run(
        &self,
        request: &BacktestRequest,
        strategy: &mut impl BacktestStrategy,
        source: &impl BarSource,
    ) -> Result<BacktestResult, BacktestError> {
        if request.symbol.trim().is_empty() {
            return Err(BacktestError::EmptySymbol);
        }
        request.range.validate()?;
        // Fail closed on a misconfigured cost model before any data is read: a
        // negative cost parameter would otherwise fabricate cash (SRS-BT-002).
        request.cost_config.validate()?;
        // Provenance: the supplied source must be the catalog the request names,
        // so BacktestResult.data_source is trustworthy (never replay the system
        // catalog while reporting UploadedData, or vice versa).
        if request.data_source != source.source() {
            return Err(BacktestError::DataSourceMismatch {
                requested: request.data_source,
                actual: source.source(),
            });
        }

        // The source bounds its own read to `max_bars` (it fails closed before
        // materializing an unbounded response). The engine re-checks the in-window
        // count below as defense in depth against a source that ignores the cap.
        let mut bars = source.bars(&request.symbol, &request.range, self.max_bars)?;
        // Trust boundary: the source was asked for one symbol. A source (e.g. a
        // user-uploaded Parquet reader) that returns a foreign symbol must fail
        // closed — never silently feed unrelated prices into the strategy.
        if let Some(foreign) = bars.iter().find(|bar| bar.symbol != request.symbol) {
            return Err(BacktestError::UnexpectedSymbol {
                expected: request.symbol.clone(),
                found: foreign.symbol.clone(),
            });
        }
        // The engine owns date-range selection: restrict to the configurable
        // window even if the source returned a superset.
        bars.retain(|bar| request.range.contains(bar.ts));
        // Fail closed if the IN-WINDOW replay set exceeds what this in-memory
        // engine will sort + allocate. The cap counts replayed (in-window) bars,
        // not the raw response, so a source that returns a large out-of-window
        // superset does not fail a valid narrow backtest. Bounding the source's
        // own materialization needs the deferred streaming BarSource.
        if bars.len() > self.max_bars {
            return Err(BacktestError::TooManyBars {
                count: bars.len(),
                limit: self.max_bars,
            });
        }
        // Deterministic replay order: stable sort by timestamp so identical
        // inputs always replay identically, regardless of source iteration order.
        bars.sort_by_key(|bar| bar.ts);
        if bars.is_empty() {
            return Err(BacktestError::EmptyData);
        }
        // Fail closed on duplicate timestamps for the (single) requested symbol:
        // a duplicate market record would double-fill one instant and make replay
        // order-dependent (stable sort preserves source order for equal ts).
        if let Some(pair) = bars.windows(2).find(|pair| pair[0].ts == pair[1].ts) {
            return Err(BacktestError::DuplicateBar { ts: pair[0].ts });
        }
        // Fail closed on corrupt market data before any fill: a non-positive
        // close price would otherwise let a buy fabricate cash (a negative
        // notional flips checked_sub into an addition) and a bogus equity mark.
        if let Some(bad) = bars.iter().find(|bar| bar.close_minor <= 0) {
            return Err(BacktestError::NonPositivePrice {
                ts: bad.ts,
                close_minor: bad.close_minor,
            });
        }
        // Fail closed on a corrupt negative observed spread on ANY bar (even a
        // no-trade bar), before any fill: a negative spread would otherwise drive
        // a cash-fabricating spread-impact cost (SRS-BT-002, mirrors the price
        // guard above).
        if let Some(bad) = bars
            .iter()
            .find(|bar| matches!(bar.spread_minor, Some(spread) if spread < 0))
        {
            return Err(BacktestError::NegativeSpread {
                ts: bad.ts,
                spread_minor: bad.spread_minor.unwrap_or_default(),
            });
        }

        let mut cash_minor = request.starting_cash_minor;
        let mut position: i64 = 0;
        let mut trade_log: Vec<Fill> = Vec::new();
        let mut equity_curve: Vec<EquityPoint> = Vec::with_capacity(bars.len());
        let mut bars_processed: u64 = 0;

        for bar in &bars {
            // A strategy failure aborts the run before any fill is applied.
            let delta = strategy.on_bar(bar, position)?;
            if delta != 0 {
                // Cash decreases by the signed notional of the trade (a buy
                // spends cash; a sell adds it).
                let notional = checked_notional(delta, bar.close_minor)?;
                cash_minor = cash_minor
                    .checked_sub(notional)
                    .ok_or(BacktestError::Overflow)?;
                // Apply the configured transaction-cost models (SRS-BT-002). Each
                // component is non-negative and is always SUBTRACTED from cash —
                // a cost can never fabricate cash, regardless of trade direction.
                let costs =
                    request
                        .cost_config
                        .cost_breakdown(delta, bar.close_minor, bar.spread_minor)?;
                let total_cost_minor = costs.total_minor()?;
                cash_minor = cash_minor
                    .checked_sub(total_cost_minor)
                    .ok_or(BacktestError::Overflow)?;
                position = position.checked_add(delta).ok_or(BacktestError::Overflow)?;
                trade_log.push(Fill {
                    ts: bar.ts,
                    symbol: bar.symbol.clone(),
                    quantity: delta,
                    price_minor: bar.close_minor,
                    commission_minor: costs.commission_minor,
                    slippage_minor: costs.slippage_minor,
                    spread_impact_minor: costs.spread_impact_minor,
                });
            }
            // Mark-to-market: equity = cash + position valued at this close.
            let holdings = checked_notional(position, bar.close_minor)?;
            let equity_minor = cash_minor
                .checked_add(holdings)
                .ok_or(BacktestError::Overflow)?;
            equity_curve.push(EquityPoint {
                ts: bar.ts,
                equity_minor,
            });
            bars_processed += 1;
        }

        let final_equity_minor = equity_curve
            .last()
            .map_or(request.starting_cash_minor, |point| point.equity_minor);

        Ok(BacktestResult {
            data_source: request.data_source,
            range: request.range,
            bars_processed,
            trade_log,
            equity_curve,
            final_equity_minor,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// In-memory system-catalog source that ignores `range` (returns
    /// everything); the engine restricts replay to the window.
    struct VecSource(Vec<BacktestBar>);

    impl BarSource for VecSource {
        fn source(&self) -> BacktestDataSource {
            BacktestDataSource::SystemData
        }

        fn bars(
            &self,
            symbol: &str,
            _range: &DateRange,
            _max_bars: usize,
        ) -> Result<Vec<BacktestBar>, BacktestError> {
            Ok(self
                .0
                .iter()
                .filter(|bar| bar.symbol == symbol)
                .cloned()
                .collect())
        }
    }

    /// A misbehaving system-catalog source that returns its bars verbatim,
    /// ignoring the requested symbol — proves the engine's trust-boundary guards.
    struct PassthroughSource(Vec<BacktestBar>);

    impl BarSource for PassthroughSource {
        fn source(&self) -> BacktestDataSource {
            BacktestDataSource::SystemData
        }

        fn bars(
            &self,
            _symbol: &str,
            _range: &DateRange,
            _max_bars: usize,
        ) -> Result<Vec<BacktestBar>, BacktestError> {
            Ok(self.0.clone())
        }
    }

    /// A source that reports the uploaded-data catalog — used to prove the
    /// engine rejects a request/source provenance mismatch.
    struct UploadedSource(Vec<BacktestBar>);

    impl BarSource for UploadedSource {
        fn source(&self) -> BacktestDataSource {
            BacktestDataSource::UploadedData
        }

        fn bars(
            &self,
            symbol: &str,
            _range: &DateRange,
            _max_bars: usize,
        ) -> Result<Vec<BacktestBar>, BacktestError> {
            Ok(self
                .0
                .iter()
                .filter(|bar| bar.symbol == symbol)
                .cloned()
                .collect())
        }
    }

    /// Buys `lot` shares on the first bar, then holds.
    struct BuyOnceAndHold {
        lot: i64,
        bought: bool,
    }

    impl BacktestStrategy for BuyOnceAndHold {
        fn on_bar(&mut self, _bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
            if self.bought {
                return Ok(0);
            }
            self.bought = true;
            Ok(self.lot)
        }
    }

    /// Always fails — stands in for a Python strategy that raises / times out.
    struct FailingStrategy;

    impl BacktestStrategy for FailingStrategy {
        fn on_bar(&mut self, bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
            Err(BacktestError::StrategyFailed {
                ts: bar.ts,
                reason: "boom".to_string(),
            })
        }
    }

    fn bar(symbol: &str, ts: u64, close_minor: i64) -> BacktestBar {
        BacktestBar {
            symbol: symbol.to_string(),
            ts,
            close_minor,
            spread_minor: None,
        }
    }

    // These BT-001 engine tests assert the raw replay/ledger mechanics, so they
    // run frictionless (CostConfig::zero); the cost-model application is covered
    // by cost.rs unit tests and the SRS-BT-002 integration test.
    fn request(range: DateRange) -> BacktestRequest {
        BacktestRequest {
            strategy_id: StrategyId::new("bt-1"),
            symbol: "AAPL".to_string(),
            data_source: BacktestDataSource::SystemData,
            range,
            starting_cash_minor: 1000,
            cost_config: CostConfig::zero(),
        }
    }

    #[test]
    fn valid_range_passes_and_inverted_range_fails_closed() {
        assert!(DateRange::new(1, 5).validate().is_ok());
        assert!(DateRange::new(7, 7).validate().is_ok());
        assert_eq!(
            DateRange::new(9, 2).validate(),
            Err(BacktestError::InvalidDateRange { start: 9, end: 2 })
        );
    }

    #[test]
    fn checked_notional_detects_overflow() {
        assert_eq!(checked_notional(2, 50), Ok(100));
        assert_eq!(checked_notional(i64::MAX, 2), Err(BacktestError::Overflow));
    }

    #[test]
    fn buy_and_hold_tracks_equity_with_integer_money() {
        // Buy 10 @ 100 on bar ts=1; price rises to 120 by ts=3 (minor units).
        let source = VecSource(vec![
            bar("AAPL", 1, 100),
            bar("AAPL", 2, 110),
            bar("AAPL", 3, 120),
        ]);
        let mut strategy = BuyOnceAndHold {
            lot: 10,
            bought: false,
        };
        let result = BacktestEngine::new()
            .run(&request(DateRange::new(1, 3)), &mut strategy, &source)
            .expect("run");

        assert_eq!(result.bars_processed, 3);
        // One fill: +10 @ 100.
        assert_eq!(result.trade_log.len(), 1);
        assert_eq!(result.trade_log[0].quantity, 10);
        assert_eq!(result.trade_log[0].price_minor, 100);
        // Cash after the buy = 1000 - 10*100 = 0; then equity = position*close.
        assert_eq!(result.equity_curve[0].equity_minor, 1000);
        assert_eq!(result.equity_curve[2].equity_minor, 10 * 120);
        assert_eq!(result.final_equity_minor, 10 * 120);
    }

    #[test]
    fn empty_symbol_fails_closed() {
        let source = VecSource(vec![bar("AAPL", 1, 100)]);
        let mut strategy = BuyOnceAndHold {
            lot: 1,
            bought: false,
        };
        let mut req = request(DateRange::new(1, 3));
        req.symbol = "   ".to_string();
        assert_eq!(
            BacktestEngine::new().run(&req, &mut strategy, &source),
            Err(BacktestError::EmptySymbol)
        );
    }

    #[test]
    fn range_with_no_bars_fails_closed() {
        let source = VecSource(vec![bar("AAPL", 1, 100), bar("AAPL", 2, 110)]);
        let mut strategy = BuyOnceAndHold {
            lot: 1,
            bought: false,
        };
        assert_eq!(
            BacktestEngine::new().run(&request(DateRange::new(50, 60)), &mut strategy, &source),
            Err(BacktestError::EmptyData)
        );
    }

    #[test]
    fn foreign_symbol_from_source_fails_closed() {
        // A source that returns a bar for a different symbol must be rejected,
        // not silently traded — the uploaded-data trust boundary.
        let source = PassthroughSource(vec![bar("MSFT", 1, 100)]);
        let mut strategy = BuyOnceAndHold {
            lot: 1,
            bought: false,
        };
        assert_eq!(
            BacktestEngine::new().run(&request(DateRange::new(1, 3)), &mut strategy, &source),
            Err(BacktestError::UnexpectedSymbol {
                expected: "AAPL".to_string(),
                found: "MSFT".to_string(),
            })
        );
    }

    #[test]
    fn non_positive_price_fails_closed_before_fabricating_cash() {
        // A corrupt negative close would make a buy (negative notional) increase
        // cash via checked_sub; the engine must reject it before any fill.
        let source = VecSource(vec![bar("AAPL", 1, -100)]);
        let mut strategy = BuyOnceAndHold {
            lot: 10,
            bought: false,
        };
        assert_eq!(
            BacktestEngine::new().run(&request(DateRange::new(1, 3)), &mut strategy, &source),
            Err(BacktestError::NonPositivePrice {
                ts: 1,
                close_minor: -100,
            })
        );
    }

    #[test]
    fn strategy_failure_aborts_before_any_fill() {
        let source = VecSource(vec![bar("AAPL", 1, 100), bar("AAPL", 2, 110)]);
        let mut strategy = FailingStrategy;
        assert_eq!(
            BacktestEngine::new().run(&request(DateRange::new(1, 3)), &mut strategy, &source),
            Err(BacktestError::StrategyFailed {
                ts: 1,
                reason: "boom".to_string(),
            })
        );
    }

    #[test]
    fn data_source_mismatch_fails_closed() {
        // request() names SystemData, but this source reads the uploaded catalog:
        // the engine must reject the mismatch so provenance can't be misreported.
        let source = UploadedSource(vec![bar("AAPL", 1, 100)]);
        let mut strategy = BuyOnceAndHold {
            lot: 1,
            bought: false,
        };
        assert_eq!(
            BacktestEngine::new().run(&request(DateRange::new(1, 3)), &mut strategy, &source),
            Err(BacktestError::DataSourceMismatch {
                requested: BacktestDataSource::SystemData,
                actual: BacktestDataSource::UploadedData,
            })
        );
    }

    #[test]
    fn too_many_bars_fails_closed_before_replay() {
        let source = VecSource(vec![
            bar("AAPL", 1, 100),
            bar("AAPL", 2, 110),
            bar("AAPL", 3, 120),
        ]);
        let mut strategy = BuyOnceAndHold {
            lot: 1,
            bought: false,
        };
        assert_eq!(
            BacktestEngine::with_max_bars(2).run(
                &request(DateRange::new(1, 3)),
                &mut strategy,
                &source
            ),
            Err(BacktestError::TooManyBars { count: 3, limit: 2 })
        );
    }

    #[test]
    fn duplicate_timestamp_fails_closed() {
        // Two bars at the same instant would double-fill and make replay
        // order-dependent — reject them.
        let source = VecSource(vec![bar("AAPL", 2, 100), bar("AAPL", 2, 110)]);
        let mut strategy = BuyOnceAndHold {
            lot: 1,
            bought: false,
        };
        assert_eq!(
            BacktestEngine::new().run(&request(DateRange::new(1, 3)), &mut strategy, &source),
            Err(BacktestError::DuplicateBar { ts: 2 })
        );
    }

    #[test]
    fn engine_restricts_replay_to_the_configurable_window() {
        let source = VecSource(vec![
            bar("AAPL", 1, 100),
            bar("AAPL", 2, 110),
            bar("AAPL", 3, 120),
            bar("AAPL", 4, 130),
        ]);
        let mut strategy = BuyOnceAndHold {
            lot: 0,
            bought: false,
        };
        let result = BacktestEngine::new()
            .run(&request(DateRange::new(2, 3)), &mut strategy, &source)
            .expect("run");
        assert_eq!(result.bars_processed, 2);
        assert_eq!(result.equity_curve[0].ts, 2);
        assert_eq!(result.equity_curve[1].ts, 3);
    }
}
