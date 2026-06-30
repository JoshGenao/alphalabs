//! Paper-strategy performance-metric accumulator (SRS-BT-004 / SyRS SYS-86).
//!
//! SYS-86 requires the internal simulation engine to **compute and report the same
//! performance metrics for paper strategies** (the SYS-16 family: Sharpe, Sortino,
//! alpha, beta, max drawdown, annualized return, annualized volatility, win rate) as
//! the backtesting engine and the live dashboard, so backtest, paper, and live
//! performance are directly comparable. The [`metrics`](crate::metrics) module ships
//! the *math* over a [`EquityPoint`] curve and a [`Fill`] trade log; this module is the
//! paper-side **producer** of those two primitives. It accumulates, over a paper
//! strategy's life, the mark-to-market net-liquidation equity curve and the trade log
//! from the SYS-84 virtual ledger ([`StrategyLedger`]) and the simulated [`PaperFill`]
//! stream, then feeds them to [`metrics::compute`](crate::metrics::compute) on demand.
//!
//! ## Why this produces the SAME family as the backtest (SYS-86)
//!
//! The backtest engine builds its equity curve as `equity = cash + position * close`
//! (mark-to-market at each bar close, fill-then-mark within a bar) over a starting cash
//! baseline. This accumulator builds the IDENTICAL quantity: `cash` starts at
//! `starting_cash_minor` and accumulates each fill's `cash_delta_minor`
//! (`-(notional) - cost`, the simulator's signed cash impact); at each [`mark`] event
//! it values every open position with [`VirtualPosition::market_value_minor`]
//! (`mark * quantity`) and sets `equity = cash + sum(market_value)`. For one symbol that
//! is exactly `cash + position * close`. So a paper run whose fills and per-bar marks
//! mirror a backtest's trade log and bar closes reproduces the backtest's exact equity
//! curve and trade log -- and therefore the exact same eight metrics. A crate-level
//! integration test (`srs_bt_004_metrics`) drives a real [`BacktestEngine`] and this
//! accumulator from the same activity and asserts the metric families are equal.
//!
//! [`BacktestEngine`]: crate::backtest::BacktestEngine
//! [`VirtualPosition::market_value_minor`]: crate::virtual_ledger::VirtualPosition::market_value_minor
//!
//! ## Fail closed, never fabricate
//!
//! The accumulator's headline hazard is fabricating equity. Two guards prevent it:
//!   * a [`mark`] event MUST supply a strictly-positive mark for EVERY open (non-flat)
//!     position -- a missing mark would silently value a held position at zero and
//!     overstate or understate net-liq, so it is rejected
//!     ([`PaperMetricsError::MissingMark`]); a non-positive mark is rejected by the
//!     ledger primitive ([`PaperMetricsError::Ledger`] wrapping
//!     [`LedgerError::NonPositiveMark`]);
//!   * marks must be strictly increasing in timestamp
//!     ([`PaperMetricsError::NonMonotonicMarkTimestamps`]) so the produced equity curve
//!     is a valid (ordered, non-degenerate) input to
//!     [`metrics::compute`](crate::metrics::compute), and a duplicate symbol within one
//!     mark event is ambiguous and rejected ([`PaperMetricsError::DuplicateMark`]).
//! Fills are validated by the ledger ([`StrategyLedger::apply_fill`]: positive price,
//! non-negative costs, `cash_delta` consistency) before any state changes, so a corrupt
//! fill leaves the accumulator unchanged. The accumulator reads no wall clock, spawns no
//! thread, and uses no RNG, so identical inputs always produce identical metrics
//! (SRS-BT-010 determinism, inherited by [`metrics::compute`](crate::metrics::compute)).
//!
//! ## Deferred (why SRS-BT-004 stays `passes:false`)
//!
//! This module is the deterministic, dependency-free accumulator: it computes the paper
//! metric family from a supplied fill + mark stream and is demonstrable solo over
//! fixtures. The RUNTIME that SUPPLIES those marks at production time -- marking each
//! open position against the live/paper market-data feed (SYS-70 subscription manager)
//! on the strategy's cadence -- and the wiring that surfaces the accumulated family on
//! the live dashboard (SRS-UI / SRS-API, SYS-36 <= 5s) and into the SRS-SIM-004
//! persisted metrics slot are the deferred owners. So `feature_list.json` keeps
//! SRS-BT-004 `passes:false`; this slice closes the in-scope paper-accumulator half.

use std::collections::HashMap;
use std::fmt;

use crate::backtest::{EquityPoint, Fill};
use crate::metrics::{
    compute, Benchmark, BenchmarkPoint, MetricsConfig, MetricsError, PerformanceMetrics,
};
use crate::sim::PaperFill;
use crate::virtual_ledger::{canonical_symbol, LedgerError, StrategyLedger};

/// Accumulates one paper strategy's mark-to-market equity curve and trade log, then
/// computes the SRS-BT-004 / SYS-86 performance-metric family from them.
///
/// Construct with the pre-trade [`new`](Self::new) baseline, drive it with
/// [`apply_fill`](Self::apply_fill) (each simulated fill) and [`mark`](Self::mark) (each
/// mark-to-market instant, e.g. once per bar), then call
/// [`compute_metrics`](Self::compute_metrics). The accumulator owns a [`StrategyLedger`]
/// so the same SYS-84 average-cost accounting the live paper engine uses produces the
/// marked position values -- paper metrics are computed from the SAME ledger state the
/// strategy trades against, independent of the IB account.
#[derive(Debug, Clone)]
pub struct PaperMetricsAccumulator {
    /// The pre-trade baseline equity (cash before any fill), in minor units. This is the
    /// REQUIRED `starting_equity_minor` baseline [`metrics::compute`](crate::metrics::compute)
    /// folds the first period's return from, so it can never be silently omitted.
    starting_cash_minor: i64,
    /// Running cash: `starting_cash_minor` plus the sum of every applied fill's
    /// `cash_delta_minor`. Held in `i128` so a long run cannot overflow before the
    /// per-mark net-liq conversion to `i64` is checked.
    cash_minor: i128,
    /// The SYS-84 virtual position ledger -- the source of marked position values.
    ledger: StrategyLedger,
    /// The trade log, mapped from each [`PaperFill`] to the [`Fill`] shape the win-rate
    /// metric consumes (the simulator's `cash_delta_minor` is dropped; it is folded into
    /// `cash_minor` instead).
    trade_log: Vec<Fill>,
    /// The mark-to-market net-liquidation equity curve, one point per [`mark`](Self::mark).
    equity_curve: Vec<EquityPoint>,
    /// The last mark timestamp, to reject a non-strictly-increasing mark.
    last_mark_ts: Option<u64>,
    /// The last fill timestamp, to reject a backwards (out-of-order) fill.
    last_fill_ts: Option<u64>,
}

impl PaperMetricsAccumulator {
    /// Start an accumulator at the pre-trade `starting_cash_minor` baseline (the cash
    /// before any fill). Fails closed on a non-positive baseline
    /// ([`PaperMetricsError::NonPositiveStartingCash`]): the baseline is the denominator
    /// of the first period's return, so it must be strictly positive (the same invariant
    /// [`metrics::compute`](crate::metrics::compute) enforces on its baseline argument).
    pub fn new(starting_cash_minor: i64) -> Result<Self, PaperMetricsError> {
        if starting_cash_minor <= 0 {
            return Err(PaperMetricsError::NonPositiveStartingCash {
                minor_units: starting_cash_minor,
            });
        }
        Ok(Self {
            starting_cash_minor,
            cash_minor: i128::from(starting_cash_minor),
            ledger: StrategyLedger::new(),
            trade_log: Vec::new(),
            equity_curve: Vec::new(),
            last_mark_ts: None,
            last_fill_ts: None,
        })
    }

    /// Apply one simulated fill: update the SYS-84 ledger position, fold the fill's
    /// `cash_delta_minor` into running cash, and append it to the trade log.
    ///
    /// Atomic / fail-closed: the prospective new cash is computed with checked
    /// arithmetic and the ledger's own [`apply_fill`](StrategyLedger::apply_fill)
    /// validates the fill (positive price, non-negative costs, `cash_delta`
    /// consistency, non-empty symbol) BEFORE any mutation, so a rejected fill leaves the
    /// accumulator byte-for-byte unchanged -- cash, the ledger, and the trade log all
    /// stay consistent. A fill whose timestamp goes backwards relative to the previous
    /// fill is rejected ([`PaperMetricsError::NonMonotonicFill`]) so the trade log is a
    /// valid time-ordered event stream (equal timestamps are allowed -- several orders
    /// or volume-capped partial fills can fill against one bar).
    ///
    /// CROSS-STREAM coherence: a fill must come strictly AFTER every already-recorded
    /// mark ([`PaperMetricsError::FillBeforeMark`]). A mark at `m` already wrote the
    /// equity point for time `m` from the positions known then; a later fill at `ts <= m`
    /// would retroactively change a position the curve has already valued, producing a
    /// time-incoherent equity curve that [`metrics::compute`](crate::metrics::compute)
    /// could not detect (it sees only a monotonic curve and an in-window fill). The
    /// legitimate within-bar order -- apply a bar's fills, THEN mark that bar -- is
    /// unaffected: at fill time the last recorded mark is the PRIOR bar's, so `ts > m`.
    pub fn apply_fill(&mut self, fill: &PaperFill) -> Result<(), PaperMetricsError> {
        if let Some(previous_ts) = self.last_fill_ts {
            if fill.ts < previous_ts {
                return Err(PaperMetricsError::NonMonotonicFill { ts: fill.ts });
            }
        }
        if let Some(last_mark_ts) = self.last_mark_ts {
            if fill.ts <= last_mark_ts {
                return Err(PaperMetricsError::FillBeforeMark {
                    fill_ts: fill.ts,
                    last_mark_ts,
                });
            }
        }
        // Compute the new cash with checked arithmetic BEFORE mutating the ledger, so a
        // (vanishingly unlikely) cash overflow is caught without leaving the ledger ahead
        // of cash.
        let new_cash = self
            .cash_minor
            .checked_add(i128::from(fill.cash_delta_minor))
            .ok_or(PaperMetricsError::Overflow)?;
        // The ledger validates and applies the fill atomically; on error nothing else has
        // changed yet.
        self.ledger
            .apply_fill(fill)
            .map_err(PaperMetricsError::Ledger)?;
        self.cash_minor = new_cash;
        self.trade_log.push(Fill {
            ts: fill.ts,
            symbol: fill.symbol.clone(),
            quantity: fill.quantity,
            price_minor: fill.price_minor,
            commission_minor: fill.commission_minor,
            slippage_minor: fill.slippage_minor,
            spread_impact_minor: fill.spread_impact_minor,
        });
        self.last_fill_ts = Some(fill.ts);
        Ok(())
    }

    /// Record one mark-to-market instant at `ts`, appending a net-liquidation equity
    /// point: `equity = cash + sum over open positions of (mark * quantity)`.
    ///
    /// `marks` supplies `(symbol, mark_minor)` for the instant. EVERY currently-open
    /// (non-flat) position MUST have a strictly-positive mark in `marks`, or the call
    /// fails closed:
    ///   * a missing mark for an open position would silently value it at zero and
    ///     fabricate equity -> [`PaperMetricsError::MissingMark`];
    ///   * a non-positive mark is corrupt quote data -> [`PaperMetricsError::Ledger`]
    ///     wrapping [`LedgerError::NonPositiveMark`];
    ///   * a duplicate symbol within one `marks` slice is ambiguous ->
    ///     [`PaperMetricsError::DuplicateMark`].
    ///
    /// Marks for symbols the strategy does NOT hold (e.g. a whole-watchlist quote vector)
    /// do not affect net liquidation, but must still be strictly-positive prices (a
    /// non-positive value is corrupt quote data and fails closed regardless of whether it
    /// values an open position). Flat positions need no mark (their market value is zero).
    ///
    /// Marks must be strictly increasing in timestamp
    /// ([`PaperMetricsError::NonMonotonicMarkTimestamps`]) so the produced equity curve is
    /// the ordered, non-degenerate series [`metrics::compute`](crate::metrics::compute)
    /// requires. Apply a bar's fills (via [`apply_fill`](Self::apply_fill)) BEFORE marking
    /// it, mirroring the backtest engine's fill-then-mark order, so the bar's equity
    /// reflects the post-fill position.
    pub fn mark(&mut self, ts: u64, marks: &[(String, i64)]) -> Result<(), PaperMetricsError> {
        if let Some(previous_ts) = self.last_mark_ts {
            if ts <= previous_ts {
                return Err(PaperMetricsError::NonMonotonicMarkTimestamps { ts });
            }
        }
        // CROSS-STREAM coherence: a mark must be at or after the latest applied fill. A
        // mark at `ts` values every open position as of time `ts`; an applied fill at a
        // LATER time is a future position that must not be folded into this instant's
        // net-liq, so a mark earlier than the last fill fails closed
        // ([`PaperMetricsError::MarkBeforeFill`]) rather than fabricating past equity from
        // a future trade. Equal is allowed (the within-bar fill-then-mark order).
        if let Some(last_fill_ts) = self.last_fill_ts {
            if ts < last_fill_ts {
                return Err(PaperMetricsError::MarkBeforeFill {
                    mark_ts: ts,
                    last_fill_ts,
                });
            }
        }
        // Canonicalize the supplied marks the same way the ledger keys positions, so an
        // `aapl` quote marks the `AAPL` position; reject a duplicate symbol (ambiguous).
        // Every supplied mark must be a strictly-positive price, even for a symbol the
        // strategy does not hold: a non-positive mark is corrupt quote data (a real price
        // is always positive), so it fails closed up front rather than being silently
        // tolerated because it happens not to value any open position this instant.
        let mut mark_by_symbol: HashMap<String, i64> = HashMap::with_capacity(marks.len());
        for (symbol, mark_minor) in marks {
            if *mark_minor <= 0 {
                return Err(PaperMetricsError::Ledger(LedgerError::NonPositiveMark {
                    mark_minor: *mark_minor,
                }));
            }
            let canonical = canonical_symbol(symbol);
            if mark_by_symbol
                .insert(canonical.clone(), *mark_minor)
                .is_some()
            {
                return Err(PaperMetricsError::DuplicateMark { symbol: canonical });
            }
        }
        // Net liquidation = cash + sum of every OPEN position's market value. A flat
        // position contributes zero and needs no mark; a non-flat position with no
        // supplied mark fails closed rather than valuing at zero.
        let mut equity = self.cash_minor;
        for (symbol, position) in self.ledger.positions_iter() {
            if position.quantity() == 0 {
                continue;
            }
            let mark_minor = mark_by_symbol.get(symbol).copied().ok_or_else(|| {
                PaperMetricsError::MissingMark {
                    symbol: symbol.clone(),
                }
            })?;
            let market_value = position
                .market_value_minor(mark_minor)
                .map_err(PaperMetricsError::Ledger)?;
            equity = equity
                .checked_add(market_value)
                .ok_or(PaperMetricsError::Overflow)?;
        }
        let equity_minor = i64::try_from(equity).map_err(|_| PaperMetricsError::Overflow)?;
        self.equity_curve.push(EquityPoint { ts, equity_minor });
        self.last_mark_ts = Some(ts);
        Ok(())
    }

    /// Compute the eight SRS-BT-004 / SYS-16 metrics from the accumulated equity curve and
    /// trade log, against `benchmark` (defaulting to SPY) with optional `benchmark_levels`
    /// (required for alpha/beta) and `config` (annualization + risk-free rate).
    ///
    /// This is the SYS-86 comparability point: it delegates to the SAME
    /// [`metrics::compute`](crate::metrics::compute) the backtest engine uses, passing the
    /// pre-trade [`starting_cash_minor`](Self::starting_cash_minor) baseline, so a paper
    /// strategy reports the identical metric family a backtest of the same activity would.
    /// Surfaces [`metrics::compute`](crate::metrics::compute)'s fail-closed errors (e.g. an
    /// empty curve, or a benchmark misaligned with the equity curve) as
    /// [`PaperMetricsError::Metrics`].
    pub fn compute_metrics(
        &self,
        benchmark: &Benchmark,
        benchmark_levels: Option<&[BenchmarkPoint]>,
        config: &MetricsConfig,
    ) -> Result<PerformanceMetrics, PaperMetricsError> {
        compute(
            self.starting_cash_minor,
            &self.equity_curve,
            &self.trade_log,
            benchmark,
            benchmark_levels,
            config,
        )
        .map_err(PaperMetricsError::Metrics)
    }

    /// The pre-trade baseline equity (cash before any fill), in minor units.
    pub fn starting_cash_minor(&self) -> i64 {
        self.starting_cash_minor
    }

    /// The current running cash in minor units (`starting_cash` plus every fill's
    /// `cash_delta_minor`).
    pub fn cash_minor(&self) -> i128 {
        self.cash_minor
    }

    /// The accumulated mark-to-market net-liquidation equity curve.
    pub fn equity_curve(&self) -> &[EquityPoint] {
        &self.equity_curve
    }

    /// The accumulated trade log (the win-rate source).
    pub fn trade_log(&self) -> &[Fill] {
        &self.trade_log
    }

    /// The underlying SYS-84 virtual position ledger.
    pub fn ledger(&self) -> &StrategyLedger {
        &self.ledger
    }
}

/// Fail-closed errors from the paper-metric accumulator. Carries no broker/vendor
/// identifiers.
#[derive(Debug, Clone, PartialEq)]
pub enum PaperMetricsError {
    /// The pre-trade cash baseline was non-positive (it is the first period return's
    /// denominator, so it must be strictly positive).
    NonPositiveStartingCash { minor_units: i64 },
    /// The underlying virtual ledger rejected a fill or a mark (corrupt fill, non-positive
    /// mark, money-math overflow).
    Ledger(LedgerError),
    /// A fill's timestamp went backwards relative to the previous fill, so the trade log
    /// would not be a valid time-ordered event stream.
    NonMonotonicFill { ts: u64 },
    /// A mark's timestamp was not strictly greater than the previous mark, so the equity
    /// curve would be non-monotonic (and ambiguous for the period returns).
    NonMonotonicMarkTimestamps { ts: u64 },
    /// A fill arrived at or before an already-recorded mark, which would retroactively
    /// change a position the equity curve has already valued (a time-incoherent curve).
    FillBeforeMark { fill_ts: u64, last_mark_ts: u64 },
    /// A mark arrived earlier than an already-applied fill, which would value the instant
    /// using a future position (fabricating past net-liq equity from a later trade).
    MarkBeforeFill { mark_ts: u64, last_fill_ts: u64 },
    /// A `marks` slice carried the same (canonical) symbol twice, so which mark applies is
    /// ambiguous.
    DuplicateMark { symbol: String },
    /// An open (non-flat) position had no mark supplied for this instant, which would
    /// silently value it at zero and fabricate net-liq equity.
    MissingMark { symbol: String },
    /// Cash or net-liquidation money math exceeded the representable range.
    Overflow,
    /// The metric family computation itself failed closed (e.g. an empty equity curve or a
    /// misaligned benchmark); carries the underlying [`MetricsError`].
    Metrics(MetricsError),
}

impl fmt::Display for PaperMetricsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonPositiveStartingCash { minor_units } => write!(
                f,
                "paper metric accumulator starting cash must be strictly positive, got \
                 {minor_units} minor units"
            ),
            Self::Ledger(error) => write!(f, "paper metric accumulator ledger error: {error}"),
            Self::NonMonotonicFill { ts } => write!(
                f,
                "paper metric accumulator fill timestamp {ts} is earlier than a prior fill"
            ),
            Self::NonMonotonicMarkTimestamps { ts } => write!(
                f,
                "paper metric accumulator mark timestamp {ts} is not strictly after the prior mark"
            ),
            Self::FillBeforeMark {
                fill_ts,
                last_mark_ts,
            } => write!(
                f,
                "paper metric accumulator fill timestamp {fill_ts} is at or before an \
                 already-recorded mark at {last_mark_ts}"
            ),
            Self::MarkBeforeFill {
                mark_ts,
                last_fill_ts,
            } => write!(
                f,
                "paper metric accumulator mark timestamp {mark_ts} is earlier than an \
                 already-applied fill at {last_fill_ts}"
            ),
            Self::DuplicateMark { symbol } => write!(
                f,
                "paper metric accumulator received two marks for symbol {symbol} in one instant"
            ),
            Self::MissingMark { symbol } => write!(
                f,
                "paper metric accumulator has an open position in {symbol} with no mark supplied"
            ),
            Self::Overflow => {
                write!(f, "paper metric accumulator money math overflowed")
            }
            Self::Metrics(error) => {
                write!(
                    f,
                    "paper metric accumulator metric computation error: {error:?}"
                )
            }
        }
    }
}

impl std::error::Error for PaperMetricsError {}

#[cfg(test)]
mod tests {
    use super::*;

    /// A well-formed [`PaperFill`] whose `cash_delta_minor` matches the ledger's
    /// `-(notional) - total_cost` invariant, so it passes the ledger's consistency guard.
    fn paper_fill(
        ts: u64,
        symbol: &str,
        quantity: i64,
        price_minor: i64,
        commission_minor: i64,
    ) -> PaperFill {
        let total_cost = i128::from(commission_minor);
        let cash_delta = -(i128::from(quantity) * i128::from(price_minor)) - total_cost;
        PaperFill {
            ts,
            symbol: symbol.to_string(),
            quantity,
            price_minor,
            commission_minor,
            slippage_minor: 0,
            spread_impact_minor: 0,
            cash_delta_minor: i64::try_from(cash_delta).expect("test cash_delta fits i64"),
        }
    }

    fn marks(pairs: &[(&str, i64)]) -> Vec<(String, i64)> {
        pairs.iter().map(|&(s, m)| (s.to_string(), m)).collect()
    }

    #[test]
    fn new_rejects_non_positive_starting_cash() {
        assert_eq!(
            PaperMetricsAccumulator::new(0).unwrap_err(),
            PaperMetricsError::NonPositiveStartingCash { minor_units: 0 }
        );
        assert!(matches!(
            PaperMetricsAccumulator::new(-1).unwrap_err(),
            PaperMetricsError::NonPositiveStartingCash { .. }
        ));
        assert!(PaperMetricsAccumulator::new(1_000).is_ok());
    }

    #[test]
    fn net_liq_equity_is_cash_plus_marked_positions() {
        // Start with 1000 cash. Buy 10 @ 100 (cost 0): cash -> 0, position 10.
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        assert_eq!(acc.cash_minor(), 0);
        // Mark @ 120: equity = 0 + 10*120 = 1200.
        acc.mark(1, &marks(&[("AAPL", 120)])).unwrap();
        assert_eq!(
            acc.equity_curve(),
            &[EquityPoint {
                ts: 1,
                equity_minor: 1_200
            }]
        );
        // Sell 10 @ 120 (cost 0): cash -> 1200, position flat.
        acc.apply_fill(&paper_fill(2, "AAPL", -10, 120, 0)).unwrap();
        assert_eq!(acc.cash_minor(), 1_200);
        // A flat position needs no mark; equity is just cash.
        acc.mark(2, &[]).unwrap();
        assert_eq!(
            acc.equity_curve()[1],
            EquityPoint {
                ts: 2,
                equity_minor: 1_200
            }
        );
    }

    #[test]
    fn short_position_marks_to_negative_market_value() {
        // Start 1000, short 5 @ 200 (cost 0): cash -> 1000 + 1000 = 2000, position -5.
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", -5, 200, 0)).unwrap();
        assert_eq!(acc.cash_minor(), 2_000);
        // Mark @ 210 (price rose -> the short lost): equity = 2000 + (-5*210) = 950.
        acc.mark(1, &marks(&[("AAPL", 210)])).unwrap();
        assert_eq!(acc.equity_curve()[0].equity_minor, 950);
    }

    #[test]
    fn mark_requires_a_mark_for_every_open_position() {
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 1, 100, 0)).unwrap();
        acc.apply_fill(&paper_fill(1, "MSFT", 1, 100, 0)).unwrap();
        // Only AAPL marked; the open MSFT position has no mark -> fail closed (never
        // value it at zero).
        assert_eq!(
            acc.mark(1, &marks(&[("AAPL", 110)])).unwrap_err(),
            PaperMetricsError::MissingMark {
                symbol: "MSFT".to_string()
            }
        );
        // No equity point was appended on the failed mark.
        assert!(acc.equity_curve().is_empty());
    }

    #[test]
    fn mark_ignores_quotes_for_unheld_symbols() {
        // A whole-watchlist quote vector may carry symbols the strategy does not hold;
        // those marks are ignored (they do not affect net liquidation).
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        acc.mark(1, &marks(&[("AAPL", 120), ("TSLA", 9_999), ("MSFT", 1)]))
            .unwrap();
        assert_eq!(acc.equity_curve()[0].equity_minor, 1_200);
    }

    #[test]
    fn mark_rejects_duplicate_symbol() {
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        assert_eq!(
            acc.mark(1, &marks(&[("AAPL", 120), ("aapl", 130)]))
                .unwrap_err(),
            PaperMetricsError::DuplicateMark {
                symbol: "AAPL".to_string()
            }
        );
    }

    #[test]
    fn mark_rejects_non_positive_mark() {
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        assert_eq!(
            acc.mark(1, &marks(&[("AAPL", 0)])).unwrap_err(),
            PaperMetricsError::Ledger(LedgerError::NonPositiveMark { mark_minor: 0 })
        );
    }

    #[test]
    fn mark_rejects_non_positive_mark_even_for_unheld_symbol() {
        // A corrupt (non-positive) quote is rejected even when it values no open position
        // this instant -- a real price is always positive, so it fails closed rather than
        // being silently tolerated.
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        assert_eq!(
            acc.mark(1, &marks(&[("AAPL", 110), ("TSLA", -1)]))
                .unwrap_err(),
            PaperMetricsError::Ledger(LedgerError::NonPositiveMark { mark_minor: -1 })
        );
        // The failed mark appended no equity point.
        assert!(acc.equity_curve().is_empty());
    }

    #[test]
    fn compute_metrics_rejects_a_fill_outside_the_marked_window() {
        // The trade log and the equity curve must describe the SAME run: a fill after the
        // last mark (a curve/log mismatch) is surfaced by metrics::compute's run-window
        // coherence guard rather than silently producing disagreeing metrics.
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        acc.mark(1, &marks(&[("AAPL", 100)])).unwrap();
        acc.mark(2, &marks(&[("AAPL", 110)])).unwrap();
        // A fill at ts 9 is past the last mark (ts 2): the curve window is [1, 2].
        acc.apply_fill(&paper_fill(9, "AAPL", -10, 110, 0)).unwrap();
        assert_eq!(
            acc.compute_metrics(&Benchmark::spy(), None, &MetricsConfig::default())
                .unwrap_err(),
            PaperMetricsError::Metrics(MetricsError::TradeLogOutsideRun {
                ts: 9,
                run_start: 1,
                run_end: 2,
            })
        );
    }

    #[test]
    fn mark_rejects_non_increasing_timestamp() {
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        acc.mark(5, &marks(&[("AAPL", 110)])).unwrap();
        // Equal timestamp is rejected (the equity curve must be STRICTLY increasing).
        assert_eq!(
            acc.mark(5, &marks(&[("AAPL", 120)])).unwrap_err(),
            PaperMetricsError::NonMonotonicMarkTimestamps { ts: 5 }
        );
        // A backwards timestamp is rejected too.
        assert_eq!(
            acc.mark(3, &marks(&[("AAPL", 120)])).unwrap_err(),
            PaperMetricsError::NonMonotonicMarkTimestamps { ts: 3 }
        );
    }

    #[test]
    fn apply_fill_rejects_backwards_fill() {
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(5, "AAPL", 1, 100, 0)).unwrap();
        assert_eq!(
            acc.apply_fill(&paper_fill(2, "AAPL", 1, 100, 0))
                .unwrap_err(),
            PaperMetricsError::NonMonotonicFill { ts: 2 }
        );
    }

    #[test]
    fn mark_then_same_timestamp_fill_is_rejected() {
        // A fill at the SAME timestamp as an already-recorded mark is too late: the mark
        // already wrote that instant's equity without this fill, so accepting it would
        // make the curve and the trade log disagree about what was held at that time.
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.mark(10, &[]).unwrap();
        assert_eq!(
            acc.apply_fill(&paper_fill(10, "AAPL", 1, 100, 0))
                .unwrap_err(),
            PaperMetricsError::FillBeforeMark {
                fill_ts: 10,
                last_mark_ts: 10
            }
        );
    }

    #[test]
    fn fill_then_earlier_mark_is_rejected() {
        // A mark in the PAST relative to an already-applied fill would value that earlier
        // instant using the future position -- fabricating past net-liq from a later trade.
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(10, "AAPL", 1, 100, 0)).unwrap();
        assert_eq!(
            acc.mark(5, &marks(&[("AAPL", 110)])).unwrap_err(),
            PaperMetricsError::MarkBeforeFill {
                mark_ts: 5,
                last_fill_ts: 10
            }
        );
    }

    #[test]
    fn within_bar_fill_then_mark_at_same_timestamp_is_allowed() {
        // The legitimate runtime order: apply a bar's fills, THEN mark that bar at the
        // same timestamp. Both cross-stream guards permit equality, so a coherent
        // single-instant run is accepted.
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(7, "AAPL", 10, 100, 0)).unwrap();
        acc.mark(7, &marks(&[("AAPL", 100)])).unwrap();
        assert_eq!(
            acc.equity_curve(),
            &[EquityPoint {
                ts: 7,
                equity_minor: 1_000
            }]
        );
        // And a later bar (fill then mark) still advances cleanly.
        acc.apply_fill(&paper_fill(8, "AAPL", -10, 110, 0)).unwrap();
        acc.mark(8, &[]).unwrap();
        assert_eq!(acc.equity_curve().len(), 2);
    }

    #[test]
    fn apply_fill_same_timestamp_is_allowed() {
        // Several orders / partial fills can fill against one bar (equal timestamps).
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 5, 100, 0)).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 5, 100, 0)).unwrap();
        assert_eq!(acc.trade_log().len(), 2);
        assert_eq!(acc.ledger().position("AAPL").unwrap().quantity(), 10);
    }

    #[test]
    fn rejected_fill_leaves_accumulator_unchanged() {
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        let cash_before = acc.cash_minor();
        // A non-positive price is rejected by the ledger; nothing else may change.
        let bad = paper_fill(2, "AAPL", 10, 0, 0);
        assert!(matches!(
            acc.apply_fill(&bad).unwrap_err(),
            PaperMetricsError::Ledger(_)
        ));
        assert_eq!(acc.cash_minor(), cash_before);
        assert_eq!(acc.trade_log().len(), 1);
        assert_eq!(acc.ledger().position("AAPL").unwrap().quantity(), 10);
    }

    #[test]
    fn trade_log_maps_paper_fill_costs() {
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 5, 100, 7)).unwrap();
        let logged = &acc.trade_log()[0];
        assert_eq!(logged.ts, 1);
        assert_eq!(logged.symbol, "AAPL");
        assert_eq!(logged.quantity, 5);
        assert_eq!(logged.price_minor, 100);
        assert_eq!(logged.commission_minor, 7);
    }

    #[test]
    fn compute_metrics_produces_the_family_and_is_deterministic() {
        // A round trip with a marked curve: buy, mark up, mark down, sell flat.
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        acc.mark(1, &marks(&[("AAPL", 100)])).unwrap();
        acc.mark(2, &marks(&[("AAPL", 120)])).unwrap();
        acc.mark(3, &marks(&[("AAPL", 90)])).unwrap();
        acc.apply_fill(&paper_fill(4, "AAPL", -10, 110, 0)).unwrap();
        acc.mark(4, &[]).unwrap();
        let config = MetricsConfig::default();
        let benchmark = Benchmark::spy();
        let first = acc.compute_metrics(&benchmark, None, &config).unwrap();
        let second = acc.compute_metrics(&benchmark, None, &config).unwrap();
        assert_eq!(first, second);
        assert_eq!(first.benchmark_symbol, "SPY");
        // One completed round trip (bought 100, sold 110) -> a win -> win rate 1.0.
        assert_eq!(first.win_rate, Some(1.0));
        // The curve had a drawdown (120 -> 90), so max drawdown is defined and positive.
        assert!(first.max_drawdown.unwrap() > 0.0);
    }

    #[test]
    fn compute_metrics_on_empty_curve_fails_closed() {
        let acc = PaperMetricsAccumulator::new(1_000).unwrap();
        assert_eq!(
            acc.compute_metrics(&Benchmark::spy(), None, &MetricsConfig::default())
                .unwrap_err(),
            PaperMetricsError::Metrics(MetricsError::EmptyEquityCurve)
        );
    }

    #[test]
    fn compute_metrics_with_benchmark_yields_alpha_beta() {
        // Strategy and benchmark move together over a 3-mark curve; alpha/beta become
        // defined once a benchmark level series (baseline + per-mark) is supplied.
        let mut acc = PaperMetricsAccumulator::new(1_000).unwrap();
        acc.apply_fill(&paper_fill(1, "AAPL", 10, 100, 0)).unwrap();
        acc.mark(1, &marks(&[("AAPL", 100)])).unwrap();
        acc.mark(2, &marks(&[("AAPL", 110)])).unwrap();
        acc.mark(3, &marks(&[("AAPL", 121)])).unwrap();
        // Benchmark carries its baseline first (curve.len() + 1 = 4 levels).
        let levels = vec![
            BenchmarkPoint {
                ts: 0,
                level_minor: 1_000,
            },
            BenchmarkPoint {
                ts: 1,
                level_minor: 1_000,
            },
            BenchmarkPoint {
                ts: 2,
                level_minor: 1_100,
            },
            BenchmarkPoint {
                ts: 3,
                level_minor: 1_210,
            },
        ];
        let metrics = acc
            .compute_metrics(&Benchmark::spy(), Some(&levels), &MetricsConfig::default())
            .unwrap();
        assert!(metrics.beta.is_some());
        assert!(metrics.alpha.is_some());
    }
}
