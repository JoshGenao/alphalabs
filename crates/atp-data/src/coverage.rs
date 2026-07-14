//! SRS-DATA-011 — corporate-action **coverage** gate (the keystone that makes an adjusted label
//! honest) plus the corporate-action REFLECTION surface: split/reverse-split and dividend adjustment,
//! symbol-change lineage, and delisting/merger/symbol-change event surfacing.
//!
//! The acceptance criterion (docs/SRS.md SRS-DATA-011): *"Splits, reverse splits, dividends,
//! delistings, mergers, and symbol changes are reflected in historical records so that backtests
//! spanning corporate-action dates produce correct P&L calculations under the selected normalization
//! mode."* This module serves all six action types through ONE coverage-enforcing gate:
//!
//! * **splits / reverse splits** — re-quoted into the served prices (split-adjusted, fully-adjusted,
//!   and total-return modes; the crate-internal SRS-DATA-012 math);
//! * **dividends** — re-quoted into the served prices under the SYS-29 **fully-adjusted** (splits AND
//!   dividends) mode ([`MarketDataStore::query_fully_adjusted`]); volume is never dividend-scaled;
//! * **symbol changes** — resolved as LINEAGE: querying the current symbol returns the predecessor's
//!   bars (relabeled to the queried symbol) for instants before the rename, with adjustments composed
//!   across the hop, so a backtest spanning the rename sees one continuous series;
//! * **delistings / mergers / symbol changes** — surfaced STRUCTURALLY on the result
//!   ([`SplitAdjustedResult::events`]) when their effective instant falls inside the query window, so
//!   a P&L consumer can mark a delisted position final, convert at merger terms, or follow the
//!   lineage hop (position/order REMAPPING itself is SYS-28b/c / SYS-88 — the execution and
//!   simulation layers consume this data; the data layer's job is the honest record).
//!
//! # The gate (the crux)
//!
//! The adjustment math re-quotes a bar at `event_ts = t` by every event with `effective_ts > t`
//! (STRICT). Every in-window bar has `t <= end_ts`, so the deepest requirement for an
//! internally-consistent adjusted `[start_ts, end_ts]` window is that **all corporate actions with
//! `effective_ts <= end_ts` are known**. Define the **coverage frontier** `D = max(complete_through)`
//! over the symbol's [`CorporateActionCoverage`](crate::store::DatasetKind::CorporateActionCoverage)
//! records (`None` if the symbol has no coverage record). Every gated read serves adjusted output
//! **iff a coverage record exists AND `D >= end_ts`**; otherwise it FAILS CLOSED with
//! [`CoverageError::NotCovered`].
//!
//! `D >= end_ts` (not `==`) is correct, and these are the edges an adversarial reviewer probes:
//!
//! * A known event in `(end_ts, D]` has `effective_ts > end_ts >=` every in-window bar, so it applies
//!   *uniformly* to all of them — the series stays internally consistent (this is why the bound is
//!   `>=`, not `==`).
//! * An event exactly at `end_ts` (`effective_ts == end_ts`) leaves the `end_ts` bar unadjusted (the
//!   strict `effective_ts > t` boundary) while adjusting every earlier in-window bar — and it has
//!   `effective_ts = end_ts <= D`, so it is known and handled. The strict math boundary and the `>=`
//!   gate boundary are mutually coherent; there is no off-by-one.
//! * `D < end_ts` (or no record) could leave an unknown event in `(D, end_ts]` that adjusts some
//!   in-window bars — that bar would be silently under-adjusted (a phantom event-drop = wrong P&L),
//!   so it must fail closed.
//!
//! The result is an honest **"as-of-basis adjusted"** series with no phantom event-drops inside the
//! window — the SRS-DATA-011 *"correct P&L for backtests spanning corporate-action dates"* property.
//! Each read's basis is its `adjusted_through` cutoff: the frontier-basis reads apply every event
//! with `effective_ts <= D`, the point-in-time (`_as_of`) reads only events with
//! `effective_ts <= query.end_ts` (no lookahead through a future event). An event with
//! `effective_ts` beyond the cutoff is EXCLUDED even if its record is already in the store: for the
//! frontier basis, coverage guarantees completeness only through `D` (the `(D, ...]` range may hide
//! unknown events, so applying a known later one would silently adjust the series PAST the advertised
//! `coverage_through:D`); for the as-of basis, the event simply has not happened yet at the run's
//! as-of date. Over-claiming a "current-adjusted" series would be the fail-open.
//!
//! # Coverage over a symbol-change lineage (a documented trust decision)
//!
//! Asserting coverage for symbol `S` through `D` asserts that **all corporate actions across `S`'s
//! symbol-change lineage** (its predecessors, via
//! [`CorporateActionSymbolChange`](crate::store::DatasetKind::CorporateActionSymbolChange) records)
//! effective on or before `D` are known — the instrument is ONE continuous entity through a rename,
//! so a per-segment frontier would be a fiction (the operator asserting "AAPLN is complete through D"
//! is asserting knowledge of the instrument's history, which includes its AAPL era). The QUERIED
//! symbol's frontier therefore governs the whole lineage-resolved read. Lineage resolution itself
//! fails closed on inconsistent rename data ([`CoverageError::LineageCycle`] /
//! [`CoverageError::AmbiguousLineage`]): a rename cycle, two predecessors claiming one successor, a
//! predecessor with multiple renames, out-of-order hops, or a bar dated outside its symbol's lineage
//! validity window. A merger does NOT splice the acquired series into the acquirer's — the acquired
//! series terminates and the conversion terms are surfaced as an event.
//!
//! # The gated public entry point (no uncovered capability on ANY surface)
//!
//! The adjustment math (`crate::normalization`) stays **crate-internal** — `split_adjust_records` /
//! `fully_adjust_records` / `total_return_records` / `SplitEvent` / `DividendEvent` are not
//! re-exported. This module is a sibling in the same crate, so it can call those crate-internal
//! functions while no external caller can. This coverage GATE is the **only** public path to adjusted
//! output: it exposes SIX coverage-enforcing reads — [`MarketDataStore::query_split_adjusted`] /
//! [`MarketDataStore::query_fully_adjusted`] / [`MarketDataStore::query_total_return`] (the
//! current-frontier basis, adjusted through `D`) and [`MarketDataStore::query_split_adjusted_as_of`] /
//! [`MarketDataStore::query_fully_adjusted_as_of`] / [`MarketDataStore::query_total_return_as_of`]
//! (the point-in-time basis, adjusted only through `query.end_ts`) — and NONE can return adjusted
//! records without the coverage check passing, so there is no public path to raw-as-adjusted. The
//! query kind is also required to be an equity bar (`DailyEquityBar` / `MinuteEquityBar`) so the
//! math's `UnsupportedKind` path is unreachable at runtime and an adjusted *series* is equity-only by
//! construction. The RAW read (`query_unified`, SRS-DATA-007) is untouched: verbatim storage, no
//! lineage, no adjustment.

use std::collections::BTreeSet;

use crate::normalization::{self, DividendEvent, NormalizationError, SplitEvent};
use crate::query::UnifiedHistoricalQuery;
use crate::store::{successor_symbol, DatasetKind, MarketDataRecord, MarketDataStore, NaturalKey};

/// The hard bound on symbol-change lineage depth. A real instrument renames a handful of times; a
/// chain deeper than this is inconsistent data (or a cycle the visited-set check somehow missed) and
/// fails closed as [`CoverageError::LineageCycle`] rather than walking unbounded.
const MAX_LINEAGE_DEPTH: usize = 32;

/// A structural (non-price) corporate action a gated read SURFACES when its effective instant falls
/// inside the query window `[start_ts, end_ts]`. Splits and dividends are already reflected in the
/// served prices; these are the events a P&L consumer must handle STRUCTURALLY: a delisting marks the
/// position final, a merger converts it at the surfaced terms, a symbol change is the lineage hop the
/// served (already lineage-resolved) series spans. Every surfaced event is within proven coverage
/// (the gate guarantees `D >= end_ts >= event.effective_ts`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CorporateActionEvent {
    /// The symbol's series terminates at `effective_ts` (its last trading instant).
    Delisting {
        /// The delisted symbol (a lineage predecessor's own name, if the hop is upstream).
        symbol: String,
        /// The delisting instant (the record's `event_ts`).
        effective_ts: i64,
    },
    /// The acquired symbol converts into the successor at `effective_ts`: `numerator` successor
    /// shares per `denominator` acquired shares plus `cash_per_share_minor` per acquired share.
    Merger {
        /// The ACQUIRED symbol (this series terminates at the effective instant).
        symbol: String,
        /// The successor (acquirer) symbol.
        successor: String,
        /// Successor shares received per `denominator` acquired shares (>= 0; store-validated).
        numerator: i64,
        /// The acquired-share denominator of the share ratio (> 0; store-validated).
        denominator: i64,
        /// The cash leg per acquired share in integer minor units (>= 0; store-validated).
        cash_per_share_minor: i64,
        /// The merger's effective instant.
        effective_ts: i64,
    },
    /// The instrument was renamed `predecessor` -> `successor` at `effective_ts`. The served series
    /// already spans the hop (predecessor bars are relabeled to the queried symbol).
    SymbolChange {
        /// The old symbol.
        predecessor: String,
        /// The new symbol.
        successor: String,
        /// The rename's effective instant.
        effective_ts: i64,
    },
}

/// One corporate-action FACT — the typed terms an APPLICATION consumer (a position/order adjuster,
/// not a price reader) needs to transform state it owns. Surfaced ONLY by
/// [`MarketDataStore::query_corporate_action_facts`], the coverage-gated fact read, so every fact a
/// consumer acts on sits inside proven coverage. This is the "same corporate-action data source"
/// seam SYS-88 / SRS-DATA-021 (paper virtual positions/orders), SRS-DATA-019 (live resting orders),
/// and SRS-DATA-020 (live positions) share with the backtest price reads: one store, one extraction
/// path (the same crate-internal `normalization` extractors the adjusted reads apply), no parallel
/// parser to drift.
///
/// Unlike [`CorporateActionEvent`] (the structural surface on a price read), a fact carries the
/// split and dividend TERMS — the adjusted price reads fold those into the served prices, but an
/// application consumer holds quantities and cost bases, which prices cannot re-express.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CorporateActionFact {
    /// An `numerator`-for-`denominator` (reverse) split effective at `effective_ts`. The symbol is
    /// the affected lineage segment's own (AS-HELD) name — the symbol a position/order holder held
    /// the instrument under at that instant — NOT the queried symbol: an applier's book reaches the
    /// queried name only when the later rename fact itself is applied, so a retagged pre-rename
    /// split would silently miss the held state.
    Split {
        symbol: String,
        effective_ts: i64,
        numerator: i64,
        denominator: i64,
    },
    /// A cash dividend of `amount_minor` per share, ex at `ex_ts`, with the last raw close strictly
    /// before the ex-date as `prev_close_minor` (the fail-closed sanity reference an applier checks
    /// the amount against). The symbol is the affected segment's own (AS-HELD) name, exactly like
    /// [`Split`](Self::Split).
    Dividend {
        symbol: String,
        ex_ts: i64,
        amount_minor: i64,
        prev_close_minor: i64,
    },
    /// The symbol's series terminates at `effective_ts` (same terms as
    /// [`CorporateActionEvent::Delisting`]; the symbol is the affected segment's own name).
    Delisting { symbol: String, effective_ts: i64 },
    /// The acquired `symbol` converts into `successor` at the surfaced share ratio + cash leg (same
    /// terms as [`CorporateActionEvent::Merger`]).
    Merger {
        symbol: String,
        successor: String,
        numerator: i64,
        denominator: i64,
        cash_per_share_minor: i64,
        effective_ts: i64,
    },
    /// The instrument was renamed `predecessor` -> `successor` at `effective_ts` (same terms as
    /// [`CorporateActionEvent::SymbolChange`]).
    SymbolChange {
        predecessor: String,
        successor: String,
        effective_ts: i64,
    },
}

impl CorporateActionFact {
    /// The instant the fact takes effect (the split/merger/delisting/rename effective instant, or
    /// the dividend ex-instant) — the primary ascending sort key of a fact read's result.
    pub fn effective_ts(&self) -> i64 {
        match self {
            Self::Split { effective_ts, .. }
            | Self::Delisting { effective_ts, .. }
            | Self::Merger { effective_ts, .. }
            | Self::SymbolChange { effective_ts, .. } => *effective_ts,
            Self::Dividend { ex_ts, .. } => *ex_ts,
        }
    }

    /// The deterministic APPLICATION precedence among facts sharing one effective instant — the
    /// secondary sort key. A symbol change orders FIRST: a successor's validity begins AT the
    /// rename instant (inclusive), so a successor-keyed action at that same instant happens after
    /// the rename — an applier must be carried onto the successor before the action can reach its
    /// book (the same-instant predecessor-keyed action is already rejected by the segment checks,
    /// and same-instant chained renames fail closed, so this ordering is unambiguous). A terminal
    /// delisting orders LAST; splits re-express the share count the per-share dividend then
    /// applies to.
    fn same_instant_precedence(&self) -> u8 {
        match self {
            Self::SymbolChange { .. } => 0,
            Self::Split { .. } => 1,
            Self::Dividend { .. } => 2,
            Self::Merger { .. } => 3,
            Self::Delisting { .. } => 4,
        }
    }
}

/// The result of a covered gated read (split-adjusted OR fully-adjusted; the name predates the
/// fully-adjusted read and is kept for its consumers): the adjusted records (owned, in
/// `event_ts`-ascending order) plus the proven coverage frontier `D`, the instant the series is
/// actually adjusted through — kept SEPARATE so a consumer is never misled about the basis the bars
/// are quoted on (the two coincide for the frontier-basis reads but DIFFER for the point-in-time
/// `_as_of` reads) — and the in-window structural corporate-action [`events`](Self::events).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SplitAdjustedResult {
    /// The adjusted records, owned, in `event_ts`-ascending order (the query result re-quoted; for a
    /// symbol with a rename lineage, predecessor bars are included relabeled to the queried symbol).
    pub records: Vec<MarketDataRecord>,
    /// The proven coverage frontier `D` (the completeness-through instant): every corporate action
    /// effective on or before `D` is known. Always `>= query.end_ts`. This is the COVERAGE proof, NOT
    /// necessarily the adjustment basis — see `adjusted_through`.
    pub coverage_through: i64,
    /// The instant the series is actually ADJUSTED THROUGH — the corporate-action cutoff, i.e. the
    /// "as-of basis" the records are quoted on: events effective on or before this are applied,
    /// later ones are NOT. The frontier-basis reads adjust through `D` (so `adjusted_through ==
    /// coverage_through`, the current basis); the `_as_of` reads adjust through `query.end_ts` (the
    /// point-in-time basis, so `adjusted_through <= coverage_through`). A consumer that needs the
    /// basis the bars are quoted on reads THIS field, never `coverage_through`.
    pub adjusted_through: i64,
    /// The structural corporate actions (delistings, mergers, symbol changes) across the queried
    /// symbol's lineage whose effective instant falls inside `[query.start_ts, query.end_ts]`, in
    /// `effective_ts`-ascending order. Splits and dividends are NOT listed here — they are already
    /// reflected in the served prices. An empty list is the common case, never an error.
    pub events: Vec<CorporateActionEvent>,
}

/// A fail-closed split-adjusted-serving error. Split-adjusted output is served only behind proven
/// coverage; every other condition fails closed rather than emitting raw-as-adjusted output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CoverageError {
    /// Corporate-action coverage for the symbol does not extend through the query end, so a split in
    /// the uncovered tail could be missing — serving split-adjusted would risk raw-as-adjusted output.
    NotCovered {
        /// The symbol whose coverage was insufficient.
        symbol: String,
        /// The coverage frontier `D` the symbol DOES have (`None` if it has no coverage record at all).
        have_through: Option<i64>,
        /// The frontier the query needs (`query.end_ts`): coverage must extend at least this far.
        need_through: i64,
    },
    /// A split-adjusted query named no kind, or a non-equity-bar kind. Split adjustment is an
    /// equity-bar operation; the gate requires an explicit `DailyEquityBar` / `MinuteEquityBar` kind so
    /// the series is equity-only by construction (a kind-agnostic query could otherwise sweep in an
    /// option-chain / fundamental record sharing the symbol + resolution).
    UnsupportedQueryKind {
        /// The (vendor-neutral) kind label the query named, or `"unspecified"` for a kind-agnostic query.
        kind: &'static str,
    },
    /// The adjustment math fails closed (a malformed split/dividend record, a non-positive factor,
    /// an invalid dividend term, a missing dividend reference close, an overflow). Passed through
    /// verbatim so the caller sees the precise money-math reason.
    Normalization(NormalizationError),
    /// Symbol-change lineage resolution found a rename CYCLE (or exceeded the hard depth bound):
    /// following predecessors from the queried symbol revisited a symbol. Inconsistent rename data —
    /// fail closed rather than loop or serve a self-referential history.
    LineageCycle {
        /// The symbol at which the cycle (or the depth bound) was detected.
        symbol: String,
    },
    /// Symbol-change lineage data is AMBIGUOUS or inconsistent: two predecessors claim one successor,
    /// a predecessor has multiple renames, hops are out of chronological order, or a bar is dated
    /// outside its symbol's lineage validity window. Serving a series stitched from ambiguous lineage
    /// could double-count or mis-attribute history — fail closed.
    AmbiguousLineage {
        /// The symbol whose lineage data is ambiguous.
        symbol: String,
        /// What was ambiguous/inconsistent.
        context: &'static str,
    },
}

impl std::fmt::Display for CoverageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotCovered {
                symbol,
                have_through,
                need_through,
            } => {
                let have = match have_through {
                    Some(d) => format!("complete through {d}"),
                    None => "absent (no coverage record)".to_string(),
                };
                write!(
                    f,
                    "split-adjusted refused for {symbol}: corporate-action coverage is {have} but the \
                     query needs coverage through >= {need_through} (SRS-DATA-011); ingest coverage \
                     with data011_coverage_cli"
                )
            }
            Self::UnsupportedQueryKind { kind } => write!(
                f,
                "split-adjusted normalization requires an explicit equity-bar kind \
                 (daily-equity-bar | minute-equity-bar); got '{kind}'"
            ),
            Self::Normalization(err) => write!(f, "{err}"),
            Self::LineageCycle { symbol } => write!(
                f,
                "symbol-change lineage for {symbol} contains a rename cycle (or exceeds the depth \
                 bound): the rename records are inconsistent — refusing to serve a self-referential \
                 history"
            ),
            Self::AmbiguousLineage { symbol, context } => write!(
                f,
                "symbol-change lineage for {symbol} is ambiguous ({context}): refusing to stitch a \
                 series from inconsistent rename data"
            ),
        }
    }
}

impl std::error::Error for CoverageError {}

impl From<NormalizationError> for CoverageError {
    fn from(err: NormalizationError) -> Self {
        Self::Normalization(err)
    }
}

impl MarketDataStore {
    /// The corporate-action coverage frontier `D` for `symbol`: the maximum completeness-through
    /// instant over the symbol's [`CorporateActionCoverage`](DatasetKind::CorporateActionCoverage)
    /// records, or `None` if it has none. The frontier is read from each coverage record's
    /// `event_ts` — the natural-key dedup identity that IS the through-date `D` (see
    /// [`coverage_record`](crate::store::coverage_record)), so advancing the frontier is a new record
    /// and the effective frontier is simply their maximum. Reading `event_ts` is safe even though
    /// `MarketDataRecord::new` is public: store validation requires every coverage record to carry a
    /// single `complete_through` field equal to its `event_ts`, so a forged record asserting a frontier
    /// its key does not carry fails closed before it can enter the store (`upsert` / `restore`).
    pub fn coverage_frontier(&self, symbol: &str) -> Option<i64> {
        self.records()
            .iter()
            .filter(|record| {
                let key = record.key();
                key.kind == DatasetKind::CorporateActionCoverage && key.symbol == symbol
            })
            .map(|record| record.key().event_ts)
            .max()
    }

    /// **The coverage-enforcing split-adjusted read (SRS-DATA-011 / SRS-DATA-012).** Returns the
    /// query's equity bars — lineage-resolved across any symbol changes — re-quoted onto a
    /// split-comparable basis, but ONLY when the queried symbol's corporate-action coverage frontier
    /// extends through the query end.
    ///
    /// This is one of the six coverage-gated public reads (with
    /// [`query_split_adjusted_as_of`](Self::query_split_adjusted_as_of),
    /// [`query_fully_adjusted`](Self::query_fully_adjusted),
    /// [`query_fully_adjusted_as_of`](Self::query_fully_adjusted_as_of),
    /// [`query_total_return`](Self::query_total_return), and
    /// [`query_total_return_as_of`](Self::query_total_return_as_of)); together they are the only
    /// public path to adjusted output (the raw adjustment math stays crate-internal). It fails closed:
    /// * [`CoverageError::UnsupportedQueryKind`] unless `query.kind` is an explicit equity-bar kind, so
    ///   the adjustment math's `UnsupportedKind` path is unreachable at runtime;
    /// * [`CoverageError::NotCovered`] unless a coverage record exists and `frontier >= query.end_ts`
    ///   (see the module docs for why `>=` is the precise, honest condition);
    /// * [`CoverageError::LineageCycle`] / [`CoverageError::AmbiguousLineage`] on inconsistent
    ///   symbol-change data (see the module docs);
    /// * [`CoverageError::Normalization`] if a split record is malformed (passed through verbatim).
    ///
    /// On success the result carries the adjusted records, the `coverage_through` frontier `D`,
    /// `adjusted_through` (here `== coverage_through`, since this method adjusts through the frontier —
    /// the CURRENT basis), and the in-window structural [`events`](SplitAdjustedResult::events). For a
    /// POINT-IN-TIME basis (no future-event leak), use
    /// [`query_split_adjusted_as_of`](Self::query_split_adjusted_as_of), where `adjusted_through` caps at
    /// `query.end_ts`. An empty in-range result is a valid covered result (`records` empty), never an error.
    pub fn query_split_adjusted(
        &self,
        query: &UnifiedHistoricalQuery,
    ) -> Result<SplitAdjustedResult, CoverageError> {
        self.query_adjusted(query, AdjustmentMode::SplitOnly, AdjustmentBasis::Frontier)
    }

    /// Like [`query_split_adjusted`](Self::query_split_adjusted) but POINT-IN-TIME as of the query's
    /// `end_ts` (the run's as-of date): it still requires coverage proven through `end_ts`, but it
    /// applies ONLY corporate actions effective AT OR BEFORE `end_ts`.
    ///
    /// An event effective AFTER `end_ts` — even one within the proven coverage frontier `D` — is NOT
    /// applied: at the run's as-of date it has not happened yet, so re-basing the historical window
    /// onto a FUTURE corporate action would bias a factor / point-in-time backtest (lookahead). So the
    /// served series is adjusted for every event on/before the as-of date and no further; coverage
    /// through `D >= end_ts` still guarantees that on/before-`end_ts` event set is COMPLETE (the
    /// uncovered tail could otherwise hide an in-window-affecting event, so an uncovered query still
    /// fails closed). [`query_split_adjusted`] by contrast adjusts to the as-of-`D`
    /// (coverage-frontier) basis — the "current" basis a LIVE strategy wants, not a point-in-time
    /// historical one.
    pub fn query_split_adjusted_as_of(
        &self,
        query: &UnifiedHistoricalQuery,
    ) -> Result<SplitAdjustedResult, CoverageError> {
        self.query_adjusted(query, AdjustmentMode::SplitOnly, AdjustmentBasis::AsOfEnd)
    }

    /// **The coverage-enforcing FULLY-adjusted read (SRS-DATA-011 / SyRS SYS-29 "fully adjusted =
    /// splits and dividends").** Like [`query_split_adjusted`](Self::query_split_adjusted) — the same
    /// gate, lineage resolution, event surfacing, and frontier basis — but the served prices are
    /// additionally back-adjusted for every cash dividend within the basis: a bar strictly before a
    /// dividend's ex-date is scaled by `(reference close − amount) / reference close`, composed
    /// exactly (one division per field) with the split factors. Volume is NEVER dividend-scaled (a
    /// dividend changes no share count). Each dividend's reference close is the last RAW close in the
    /// lineage series strictly before its ex-date; a dividend with no prior close fails the read
    /// closed ([`NormalizationError::MissingReferenceClose`] via [`CoverageError::Normalization`]) —
    /// never a silent factor of 1 — as does a split effective between a reference close and its
    /// ex-date ([`NormalizationError::BasisCrossingDividend`]: mismatched share bases).
    pub fn query_fully_adjusted(
        &self,
        query: &UnifiedHistoricalQuery,
    ) -> Result<SplitAdjustedResult, CoverageError> {
        self.query_adjusted(query, AdjustmentMode::Full, AdjustmentBasis::Frontier)
    }

    /// Like [`query_fully_adjusted`](Self::query_fully_adjusted) but POINT-IN-TIME as of the query's
    /// `end_ts`: only splits AND dividends effective/ex at or before the as-of date are applied (the
    /// same no-lookahead discipline as
    /// [`query_split_adjusted_as_of`](Self::query_split_adjusted_as_of) — a dividend ex in
    /// `(end_ts, D]` must not bias a point-in-time read).
    pub fn query_fully_adjusted_as_of(
        &self,
        query: &UnifiedHistoricalQuery,
    ) -> Result<SplitAdjustedResult, CoverageError> {
        self.query_adjusted(query, AdjustmentMode::Full, AdjustmentBasis::AsOfEnd)
    }

    /// **The coverage-enforcing TOTAL-RETURN read (SRS-DATA-012 / SyRS SYS-29 "total return").** Like
    /// [`query_fully_adjusted`](Self::query_fully_adjusted) — the same gate, lineage resolution, event
    /// surfacing, and frontier basis, and the same coverage requirement (`frontier >= query.end_ts`
    /// else [`CoverageError::NotCovered`]) — but the served prices REINVEST each cash dividend forward
    /// instead of back-adjusting for it: a bar on or after a dividend's ex-date is scaled UP by
    /// `reference close / (reference close − amount)` (the INVERSE of the fully-adjusted factor),
    /// composed exactly with the split factors. Volume is NEVER dividend-scaled. The result is the
    /// growth-of-one-share total-return index anchored at the earliest bar — a genuinely distinct
    /// series from the fully-adjusted (charting) basis, which anchors the latest bar at raw. The same
    /// fail-closed money-math discipline applies (missing reference close, basis-crossing split,
    /// invalid term, overflow).
    ///
    /// Because a reinvested dividend has `ex_ts <= t` and every in-window bar has `t <= query.end_ts`,
    /// this read applies NO dividend ex after the query end — so it is inherently point-in-time over
    /// the dividend leg (no dividend lookahead by construction). The gate therefore resolves (and
    /// validates) dividends only through `query.end_ts`, NOT the coverage frontier `D`: a future
    /// (malformed / basis-crossing / missing-reference) dividend in `(query.end_ts, D]` — which no
    /// returned bar could ever use — cannot fail the read. The frontier vs point-in-time distinction
    /// still governs the SPLIT leg (`effective_ts > t`; a split in `(end_ts, D]` IS back-adjusted into
    /// the in-window bars), so [`query_total_return_as_of`](Self::query_total_return_as_of) is provided
    /// for symmetry: it caps applied splits at `query.end_ts` exactly like the other `_as_of` reads.
    pub fn query_total_return(
        &self,
        query: &UnifiedHistoricalQuery,
    ) -> Result<SplitAdjustedResult, CoverageError> {
        self.query_adjusted(
            query,
            AdjustmentMode::TotalReturn,
            AdjustmentBasis::Frontier,
        )
    }

    /// Like [`query_total_return`](Self::query_total_return) but POINT-IN-TIME as of the query's
    /// `end_ts`: only splits effective at or before the as-of date are applied (the reinvested-dividend
    /// leg is already point-in-time by construction, `ex_ts <= t <= end_ts`, so it is basis-invariant;
    /// the split leg is what the `_as_of` cap governs — a split in `(end_ts, D]` is not applied).
    pub fn query_total_return_as_of(
        &self,
        query: &UnifiedHistoricalQuery,
    ) -> Result<SplitAdjustedResult, CoverageError> {
        self.query_adjusted(query, AdjustmentMode::TotalReturn, AdjustmentBasis::AsOfEnd)
    }

    /// **The coverage-enforcing corporate-action FACT read (SYS-88 / SRS-DATA-021; the shared
    /// application-consumer seam for SRS-DATA-019/020).** Returns every corporate-action fact —
    /// split and dividend TERMS plus the structural delisting / merger / symbol-change events —
    /// across the queried symbol's rename lineage whose effective (ex-) instant falls inside
    /// `[query.start_ts, query.end_ts]`, in `effective_ts`-ascending order.
    ///
    /// This exists because the adjusted PRICE reads fold splits and dividends into the served
    /// prices, which is exactly right for a bar consumer and exactly wrong for an APPLICATION
    /// consumer: a paper/live position or resting order holds a quantity and a cost basis that only
    /// the action's own terms can re-express. Surfacing the terms through this gate keeps one
    /// extraction path (the same crate-internal `normalization` extractors and store-validated
    /// records the price reads apply) instead of every consumer growing a parallel record parser
    /// that can drift.
    ///
    /// The same fail-closed discipline as the six adjusted reads:
    /// * [`CoverageError::UnsupportedQueryKind`] unless `query.kind` is an explicit equity-bar kind
    ///   (the dividend reference close is resolved from that raw bar series);
    /// * [`CoverageError::NotCovered`] unless a coverage record exists and `frontier >=
    ///   query.end_ts` — an uncovered tail could hide an in-window action, so an application
    ///   consumer acting on an incomplete fact list would silently miss an adjustment (the
    ///   fail-open this gate exists to prevent);
    /// * [`CoverageError::LineageCycle`] / [`CoverageError::AmbiguousLineage`] on inconsistent
    ///   symbol-change data;
    /// * [`CoverageError::Normalization`] on a malformed split/dividend record within the basis (a
    ///   missing reference close, a non-positive factor, an invalid term) — never a silently
    ///   dropped fact.
    ///
    /// The basis is POINT-IN-TIME at `query.end_ts` (the `_as_of` discipline): facts effective
    /// after the window end — even inside proven coverage — have not happened yet for a consumer
    /// applying actions as of that instant, so they are not surfaced (no lookahead). EVERY fact —
    /// term and structural alike — carries its lineage segment's own (AS-HELD) symbol, NOT the
    /// queried symbol: an application consumer's book holds state under the historical name until
    /// the rename fact itself is applied in sequence, so the price reads' relabeling would make a
    /// pre-rename split/dividend silently miss the held position or order (the fail-open this
    /// surface must not have). The lineage is walked in BOTH directions: predecessors (the price
    /// reads' backward walk) AND the queried symbol's outgoing renames forward — an in-window
    /// rename carries the applier's book onto the successor, so the successor's later in-window
    /// actions are the same instrument's story and surface too. Apply facts in the returned
    /// `effective_ts` order and the rename hops carry the book exactly as the lineage did.
    ///
    /// ONE query per held instrument: because the read spans the instrument's full rename lineage,
    /// querying any of its names returns the same action story — issue one query per distinct
    /// instrument and never a second one for another name of the same lineage (applying the
    /// returned facts twice would double-adjust). An empty list is the common covered result,
    /// never an error.
    pub fn query_corporate_action_facts(
        &self,
        query: &UnifiedHistoricalQuery,
    ) -> Result<Vec<CorporateActionFact>, CoverageError> {
        // (1) The same equity-bar kind guard as the adjusted reads: the dividend reference close is
        // resolved from the raw bar series of this kind.
        let kind = match query.kind {
            Some(kind @ (DatasetKind::DailyEquityBar | DatasetKind::MinuteEquityBar)) => kind,
            Some(other) => {
                return Err(CoverageError::UnsupportedQueryKind {
                    kind: other.as_str(),
                })
            }
            None => {
                return Err(CoverageError::UnsupportedQueryKind {
                    kind: "unspecified",
                })
            }
        };

        // (2) The same coverage gate: facts are complete only through the proven frontier, and an
        // application consumer must never act on a window whose tail could hide an action.
        let frontier = self.coverage_frontier(&query.symbol);
        if !matches!(frontier, Some(d) if d >= query.end_ts) {
            return Err(CoverageError::NotCovered {
                symbol: query.symbol.clone(),
                have_through: frontier,
                need_through: query.end_ts,
            });
        }

        // (3) Point-in-time basis at the window end (no lookahead), the same cutoff the `_as_of`
        // reads use: lineage, split terms, and dividend terms are resolved (and validated) only
        // through `query.end_ts`. Unlike the price reads (which serve the QUERIED symbol's series
        // and only look BACKWARD through its predecessors), the fact read also follows the queried
        // symbol's outgoing renames FORWARD: an applier holding the predecessor is carried onto
        // the successor by the rename fact itself, so the successor's later in-window actions are
        // part of the same instrument's story and must surface too — stopping at the rename would
        // leave the remapped book silently stale (the fail-open the adversarial review caught).
        let mut lineage = self.resolve_lineage(&query.symbol, query.end_ts)?;
        self.extend_lineage_forward(&mut lineage, &query.symbol, query.end_ts)?;
        let lineage = lineage;
        let series = self.lineage_raw_series(kind, query, &lineage)?;
        let splits = self.lineage_split_events(None, &lineage, query.end_ts)?;
        let dividends = self.lineage_dividend_events(None, &lineage, query.end_ts, &series)?;

        // (4) Window-filter the term facts (the extractors are cutoff-bounded, not start-bounded)
        // and merge with the structural in-window events.
        let mut facts: Vec<CorporateActionFact> = Vec::new();
        for split in splits {
            if split.effective_ts >= query.start_ts {
                facts.push(CorporateActionFact::Split {
                    symbol: split.symbol,
                    effective_ts: split.effective_ts,
                    numerator: split.numerator,
                    denominator: split.denominator,
                });
            }
        }
        for dividend in dividends {
            if dividend.ex_ts >= query.start_ts {
                facts.push(CorporateActionFact::Dividend {
                    symbol: dividend.symbol,
                    ex_ts: dividend.ex_ts,
                    amount_minor: dividend.amount_minor,
                    prev_close_minor: dividend.prev_close_minor,
                });
            }
        }
        for event in self.corporate_events_in_window(query, &lineage)? {
            facts.push(match event {
                CorporateActionEvent::Delisting {
                    symbol,
                    effective_ts,
                } => CorporateActionFact::Delisting {
                    symbol,
                    effective_ts,
                },
                CorporateActionEvent::Merger {
                    symbol,
                    successor,
                    numerator,
                    denominator,
                    cash_per_share_minor,
                    effective_ts,
                } => CorporateActionFact::Merger {
                    symbol,
                    successor,
                    numerator,
                    denominator,
                    cash_per_share_minor,
                    effective_ts,
                },
                CorporateActionEvent::SymbolChange {
                    predecessor,
                    successor,
                    effective_ts,
                } => CorporateActionFact::SymbolChange {
                    predecessor,
                    successor,
                    effective_ts,
                },
            });
        }
        // Ascending by effective instant; facts SHARING an instant order by the deterministic
        // application precedence (rename first — a successor-keyed action at the rename instant
        // happens after the rename that makes it reachable; delisting last).
        facts.sort_by_key(|fact| (fact.effective_ts(), fact.same_instant_precedence()));
        Ok(facts)
    }

    /// The single gated read core every public adjusted read delegates to — one gate, one lineage
    /// resolution, one event-surfacing path, so no mode/basis combination can skip a check.
    fn query_adjusted(
        &self,
        query: &UnifiedHistoricalQuery,
        mode: AdjustmentMode,
        basis: AdjustmentBasis,
    ) -> Result<SplitAdjustedResult, CoverageError> {
        // (1) Equity-bar kind guard. Adjustment is an equity-bar operation; require an explicit
        // DailyEquityBar / MinuteEquityBar kind so a kind-agnostic query cannot sweep in an
        // option-chain / fundamental record (which the math would reject mid-stream as UnsupportedKind)
        // and so the served series is equity-only by construction.
        let kind = match query.kind {
            Some(kind @ (DatasetKind::DailyEquityBar | DatasetKind::MinuteEquityBar)) => kind,
            Some(other) => {
                return Err(CoverageError::UnsupportedQueryKind {
                    kind: other.as_str(),
                })
            }
            None => {
                return Err(CoverageError::UnsupportedQueryKind {
                    kind: "unspecified",
                })
            }
        };

        // (2) Coverage gate on the QUERIED symbol (whose frontier governs its whole lineage — the
        // documented trust decision in the module docs). The frontier must exist AND reach at least
        // the query end, else the uncovered tail could hide an event that adjusts an in-window bar ->
        // fail closed.
        let frontier = self.coverage_frontier(&query.symbol);
        let coverage_through = match frontier {
            Some(d) if d >= query.end_ts => d,
            have => {
                return Err(CoverageError::NotCovered {
                    symbol: query.symbol.clone(),
                    have_through: have,
                    need_through: query.end_ts,
                })
            }
        };

        // (3) The adjustment basis: the cutoff every applied corporate action is bounded to. The
        // frontier basis applies every event with effective_ts <= D -- NOT just the query window
        // (an event in (end_ts, D] legitimately re-bases the in-window bars) and NOT past D (coverage
        // proves completeness only through D, so a known event beyond D would silently adjust the
        // series PAST the advertised coverage_through; a malformed event beyond the cutoff likewise
        // cannot fail the read -- it is out of scope). The as-of basis caps at query.end_ts: an event
        // after the run's as-of date has not happened yet (no lookahead).
        let adjusted_through = match basis {
            AdjustmentBasis::Frontier => coverage_through,
            AdjustmentBasis::AsOfEnd => query.end_ts,
        };

        // (4) Symbol-change LINEAGE: the queried symbol's predecessor segments, bounded to renames
        // effective within the basis. Fails closed on a cycle / ambiguous rename data.
        let lineage = self.resolve_lineage(&query.symbol, adjusted_through)?;

        // (5) The FULL raw lineage series (relabeled to the queried symbol, event_ts-ascending) -- not
        // window-clipped, because a dividend's reference close may precede the window.
        let series = self.lineage_raw_series(kind, query, &lineage)?;

        // (6) The applied corporate actions across the lineage, bounded to the basis cutoff.
        // `split_events_for` / `dividend_events_for` re-filter by kind + symbol and fail closed on a
        // malformed event within the cutoff.
        let splits = self.lineage_split_events(Some(&query.symbol), &lineage, adjusted_through)?;
        let dividends = match mode {
            // Split-adjusted deliberately ignores dividend records entirely (mode semantics: the
            // SYS-29 split-adjusted basis reflects share-count changes only).
            AdjustmentMode::SplitOnly => Vec::new(),
            // Fully-adjusted (back-adjust) applies every dividend with ex_ts > t, so a dividend in
            // (query.end_ts, D] legitimately re-bases the in-window bars -> resolve (and validate)
            // through the frontier cutoff `adjusted_through`.
            AdjustmentMode::Full => self.lineage_dividend_events(
                Some(&query.symbol),
                &lineage,
                adjusted_through,
                &series,
            )?,
            // Total-return (reinvest) applies ONLY dividends with ex_ts <= t, and every in-window bar
            // has t <= query.end_ts, so a dividend ex AFTER the query end is NEVER applied. Resolve
            // (and therefore VALIDATE) dividends only through `query.end_ts`, NOT the coverage frontier:
            // a future (malformed / basis-crossing / missing-reference) dividend in
            // (query.end_ts, D] must not fail a total-return read whose returned bars can never use it.
            // This is the no-dividend-lookahead property enforced at the RESOLUTION boundary, not only
            // in the per-bar application. The SPLIT leg above still uses the frontier cutoff (a split in
            // (end_ts, D] with effective_ts > t IS back-adjusted into the in-window bars), which is what
            // still distinguishes query_total_return from query_total_return_as_of.
            AdjustmentMode::TotalReturn => {
                self.lineage_dividend_events(Some(&query.symbol), &lineage, query.end_ts, &series)?
            }
        };

        // (7) Window-filter the relabeled series and apply the crate-internal math. Every record is
        // the guarded equity-bar kind, so UnsupportedKind is unreachable; an empty match yields an
        // empty (still covered) result.
        let in_window: Vec<&MarketDataRecord> = series
            .iter()
            .filter(|record| {
                let t = record.key().event_ts;
                t >= query.start_ts && t <= query.end_ts
            })
            .collect();
        let adjusted = match mode {
            AdjustmentMode::SplitOnly => normalization::split_adjust_records(&in_window, &splits)?,
            AdjustmentMode::Full => {
                normalization::fully_adjust_records(&in_window, &splits, &dividends)?
            }
            AdjustmentMode::TotalReturn => {
                normalization::total_return_records(&in_window, &splits, &dividends)?
            }
        };

        // (8) Surface the in-window STRUCTURAL events (delistings, mergers, symbol changes) across
        // the lineage -- all provably known (the gate guarantees D >= end_ts >= event.effective_ts).
        let events = self.corporate_events_in_window(query, &lineage)?;

        Ok(SplitAdjustedResult {
            records: adjusted,
            coverage_through,
            adjusted_through,
            events,
        })
    }

    /// Extend a resolved (backward) lineage FORWARD through the queried symbol's outgoing renames,
    /// bounded to `cutoff` — the fact read's successor leg (the price reads never call this; they
    /// serve the queried symbol's own series). Each successor gets a validity segment
    /// `[its inbound rename, its own outgoing rename)`, so every downstream validity check applies
    /// to successor actions exactly as it does to predecessor ones. The same fail-closed
    /// discipline as the backward walk: a revisited symbol or a chain past the depth bound is a
    /// [`CoverageError::LineageCycle`]; a successor with two outgoing renames, or hops out of
    /// chronological order, is [`CoverageError::AmbiguousLineage`].
    ///
    /// Coverage note: the gate already proved the QUERIED symbol's frontier through the query end,
    /// and (per the module-docs trust decision) asserting coverage for a symbol asserts knowledge
    /// of its INSTRUMENT's corporate actions — the instrument is one continuous entity through a
    /// rename, forward exactly as backward.
    fn extend_lineage_forward(
        &self,
        lineage: &mut Vec<LineageSegment>,
        symbol: &str,
        cutoff: i64,
    ) -> Result<(), CoverageError> {
        let mut visited: BTreeSet<String> = lineage
            .iter()
            .map(|segment| segment.symbol.clone())
            .collect();
        let mut current = symbol.to_string();
        // The head segment's own retirement instant (set by resolve_lineage when the queried
        // symbol has an outgoing rename within the cutoff) starts the walk.
        let mut current_retired = lineage
            .iter()
            .find(|segment| segment.symbol == current)
            .and_then(|segment| segment.valid_until);
        let mut depth = 0usize;
        while let Some(change_ts) = current_retired {
            depth += 1;
            if depth >= MAX_LINEAGE_DEPTH {
                return Err(CoverageError::LineageCycle { symbol: current });
            }
            // The outgoing rename record retiring `current` at `change_ts` (resolve_lineage /
            // the previous hop proved it exists and is unique within the cutoff).
            let successor = self
                .records()
                .iter()
                .find(|record| {
                    let key = record.key();
                    key.kind == DatasetKind::CorporateActionSymbolChange
                        && key.symbol == current
                        && key.event_ts == change_ts
                })
                .and_then(|record| successor_symbol(record.key()))
                .ok_or_else(|| CoverageError::AmbiguousLineage {
                    symbol: current.clone(),
                    context: "a symbol change record lost its successor",
                })?
                .to_string();
            if !visited.insert(successor.clone()) {
                return Err(CoverageError::LineageCycle { symbol: successor });
            }
            // The successor's own outgoing rename within the cutoff bounds ITS segment — the same
            // single-rename rule as everywhere else, and it must be strictly later than the hop
            // that created the successor.
            let mut outgoing: Vec<i64> = self
                .records()
                .iter()
                .filter(|record| {
                    let key = record.key();
                    key.kind == DatasetKind::CorporateActionSymbolChange
                        && key.symbol == successor
                        && key.event_ts <= cutoff
                })
                .map(|record| record.key().event_ts)
                .collect();
            outgoing.sort_unstable();
            let next_retired = match outgoing.as_slice() {
                [] => None,
                [ts] => Some(*ts),
                _ => {
                    return Err(CoverageError::AmbiguousLineage {
                        symbol: successor,
                        context: "a predecessor has multiple renames",
                    })
                }
            };
            if next_retired.is_some_and(|ts| ts <= change_ts) {
                return Err(CoverageError::AmbiguousLineage {
                    symbol: successor,
                    context: "lineage hops out of chronological order",
                });
            }
            lineage.push(LineageSegment {
                symbol: successor.clone(),
                valid_from: Some(change_ts),
                valid_until: next_retired,
            });
            current = successor;
            current_retired = next_retired;
        }
        Ok(())
    }

    /// Resolve the queried symbol's rename LINEAGE: walk
    /// [`CorporateActionSymbolChange`](DatasetKind::CorporateActionSymbolChange) records whose
    /// successor is the current symbol and whose `event_ts <= cutoff`, newest hop first, producing
    /// the validity segments the series builder enforces. Fails closed on: two predecessors claiming
    /// one successor, a predecessor with multiple renames, hops out of chronological order
    /// ([`CoverageError::AmbiguousLineage`]), a revisited symbol, or a chain deeper than the hard
    /// bound ([`CoverageError::LineageCycle`]).
    fn resolve_lineage(
        &self,
        symbol: &str,
        cutoff: i64,
    ) -> Result<Vec<LineageSegment>, CoverageError> {
        let mut visited: BTreeSet<String> = BTreeSet::new();
        visited.insert(symbol.to_string());
        // The walk collects (predecessor, change_ts) hops, nearest rename first.
        let mut hops: Vec<(String, i64)> = Vec::new();
        let mut current = symbol.to_string();
        loop {
            if hops.len() >= MAX_LINEAGE_DEPTH {
                return Err(CoverageError::LineageCycle { symbol: current });
            }
            let mut candidates: Vec<&MarketDataRecord> = self
                .records()
                .iter()
                .filter(|record| {
                    let key = record.key();
                    key.kind == DatasetKind::CorporateActionSymbolChange
                        && key.event_ts <= cutoff
                        && successor_symbol(key) == Some(current.as_str())
                })
                .collect();
            let hop = match candidates.len() {
                0 => break,
                1 => candidates.remove(0),
                _ => {
                    return Err(CoverageError::AmbiguousLineage {
                        symbol: current,
                        context: "two predecessors claim one successor",
                    })
                }
            };
            let predecessor = hop.key().symbol.clone();
            let change_ts = hop.key().event_ts;
            // The predecessor's ONLY outgoing rename within the cutoff must be the hop just followed:
            // a second one means the same symbol was renamed twice -- inconsistent data.
            let outgoing = self
                .records()
                .iter()
                .filter(|record| {
                    let key = record.key();
                    key.kind == DatasetKind::CorporateActionSymbolChange
                        && key.symbol == predecessor
                        && key.event_ts <= cutoff
                })
                .count();
            if outgoing != 1 {
                return Err(CoverageError::AmbiguousLineage {
                    symbol: predecessor,
                    context: "a predecessor has multiple renames",
                });
            }
            // Each earlier hop must be strictly earlier in time (P renamed to C before C renamed on).
            if let Some((_, prior_ts)) = hops.last() {
                if change_ts >= *prior_ts {
                    return Err(CoverageError::AmbiguousLineage {
                        symbol: predecessor,
                        context: "lineage hops out of chronological order",
                    });
                }
            }
            if !visited.insert(predecessor.clone()) {
                return Err(CoverageError::LineageCycle {
                    symbol: predecessor,
                });
            }
            hops.push((predecessor.clone(), change_ts));
            current = predecessor;
        }

        // The QUERIED symbol's own OUTGOING rename (within the cutoff) RETIRES it: after that
        // instant the symbol no longer exists, so a later bar or corporate action keyed to it is
        // inconsistent data. Bounding the head segment's `valid_until` here makes every downstream
        // validity check catch it — without this, a consumer holding the PREDECESSOR name and
        // querying it directly would be served post-retirement actions as valid facts (the
        // application fail-open the SRS-DATA-021 adversarial review caught). An outgoing rename
        // AFTER the cutoff does not bound the segment (as-of semantics: at the cutoff instant the
        // rename has not happened yet). Two outgoing renames is inconsistent data — fail closed,
        // the same rule the walk applies to every predecessor.
        let mut outgoing_ts: Vec<i64> = self
            .records()
            .iter()
            .filter(|record| {
                let key = record.key();
                key.kind == DatasetKind::CorporateActionSymbolChange
                    && key.symbol == symbol
                    && key.event_ts <= cutoff
            })
            .map(|record| record.key().event_ts)
            .collect();
        outgoing_ts.sort_unstable();
        let head_retired_at = match outgoing_ts.as_slice() {
            [] => None,
            [ts] => Some(*ts),
            _ => {
                return Err(CoverageError::AmbiguousLineage {
                    symbol: symbol.to_string(),
                    context: "a predecessor has multiple renames",
                })
            }
        };

        // Hops -> validity segments: the queried symbol is valid from its (nearest) rename onward
        // until its own outgoing rename (if any) retires it; each predecessor is valid from ITS
        // rename (unbounded for the oldest) until the rename that retired it.
        let mut segments = Vec::with_capacity(hops.len() + 1);
        let mut valid_until: Option<i64> = head_retired_at;
        let mut segment_symbol = symbol.to_string();
        for (predecessor, change_ts) in &hops {
            segments.push(LineageSegment {
                symbol: segment_symbol,
                valid_from: Some(*change_ts),
                valid_until,
            });
            valid_until = Some(*change_ts);
            segment_symbol = predecessor.clone();
        }
        segments.push(LineageSegment {
            symbol: segment_symbol,
            valid_from: None,
            valid_until,
        });
        Ok(segments)
    }

    /// The FULL raw equity-bar series across the lineage segments, relabeled to the queried symbol,
    /// `event_ts`-ascending. Deliberately NOT window-clipped (a dividend reference close may precede
    /// the query window). Fails closed if any bar is dated outside its symbol's validity window — a
    /// predecessor bar on/after its rename (or a successor bar before it) would collide with or
    /// mis-attribute the other segment's history.
    fn lineage_raw_series(
        &self,
        kind: DatasetKind,
        query: &UnifiedHistoricalQuery,
        lineage: &[LineageSegment],
    ) -> Result<Vec<MarketDataRecord>, CoverageError> {
        let mut series: Vec<MarketDataRecord> = Vec::new();
        for segment in lineage {
            for record in self.records() {
                let key = record.key();
                if key.kind != kind
                    || key.symbol != segment.symbol
                    || key.resolution != query.resolution
                {
                    continue;
                }
                let t = key.event_ts;
                if segment.valid_from.is_some_and(|from| t < from)
                    || segment.valid_until.is_some_and(|until| t >= until)
                {
                    return Err(CoverageError::AmbiguousLineage {
                        symbol: segment.symbol.clone(),
                        context: "a bar is dated outside its symbol's lineage validity window",
                    });
                }
                series.push(relabeled(record, &query.symbol));
            }
        }
        // Segments' windows are disjoint, so timestamps are unique; sort for the ascending contract.
        series.sort_by_key(|record| record.key().event_ts);
        Ok(series)
    }

    /// The split events across the lineage, bounded to `effective_ts <= adjusted_through`.
    ///
    /// `retag_to` controls the surfaced symbol: the adjusted PRICE reads pass the queried symbol
    /// (the instrument is one continuous entity through a rename, so a predecessor's split applies
    /// to the relabeled series exactly like the current symbol's); the corporate-action FACT read
    /// passes `None` so each split keeps its segment's own (AS-HELD) symbol — an APPLICATION
    /// consumer's book holds state under the historical symbol until the rename action itself is
    /// applied, so retagging would make a pre-rename split silently miss the held position.
    ///
    /// Like [`lineage_raw_series`](Self::lineage_raw_series) does for bars, a within-cutoff split dated
    /// OUTSIDE its symbol's lineage validity window (a predecessor split on/after its rename, or a
    /// successor split before it) FAILS CLOSED ([`CoverageError::AmbiguousLineage`]) — inconsistent
    /// rename data must never be retagged to the queried symbol and silently applied to the wrong bars.
    fn lineage_split_events(
        &self,
        retag_to: Option<&str>,
        lineage: &[LineageSegment],
        adjusted_through: i64,
    ) -> Result<Vec<SplitEvent>, CoverageError> {
        let mut splits: Vec<SplitEvent> = Vec::new();
        for segment in lineage {
            let mut refs: Vec<&MarketDataRecord> = Vec::new();
            for record in self.records() {
                let key = record.key();
                if key.kind != DatasetKind::CorporateActionSplit || key.symbol != segment.symbol {
                    continue;
                }
                // Bounded to the read's basis cutoff (within adjusted_through); a within-cutoff split
                // outside the segment's validity window fails closed.
                if key.event_ts <= adjusted_through {
                    check_action_in_segment_window(segment, key.event_ts)?;
                    refs.push(record);
                }
            }
            splits.extend(
                normalization::split_events_for(&segment.symbol, &refs)?
                    .into_iter()
                    .map(|mut event| {
                        if let Some(symbol) = retag_to {
                            event.symbol = symbol.to_string();
                        }
                        event
                    }),
            );
        }
        Ok(splits)
    }

    /// The dividend events across the lineage, bounded to `ex_ts <= adjusted_through`, surfaced
    /// under `retag_to` (the queried symbol, for the adjusted price reads) or each segment's own
    /// AS-HELD symbol (`None`, for the fact read — see
    /// [`lineage_split_events`](Self::lineage_split_events)). Each dividend's REFERENCE CLOSE is
    /// resolved as the last raw close in the (relabeled, full) lineage `series` strictly before its
    /// ex-date — the RAW series, never the adjusted one, and never window-clipped. A dividend with
    /// no prior close fails the read closed, as does a within-cutoff dividend dated outside its
    /// symbol's lineage validity window (the same fail-closed discipline the split leg and
    /// [`lineage_raw_series`](Self::lineage_raw_series) apply).
    fn lineage_dividend_events(
        &self,
        retag_to: Option<&str>,
        lineage: &[LineageSegment],
        adjusted_through: i64,
        series: &[MarketDataRecord],
    ) -> Result<Vec<DividendEvent>, CoverageError> {
        let prev_close_of = |ex_ts: i64| {
            series
                .iter()
                .rev()
                .filter(|record| record.key().event_ts < ex_ts)
                .find_map(|record| {
                    field_of(record, "close").map(|close| (record.key().event_ts, close))
                })
        };
        let mut dividends: Vec<DividendEvent> = Vec::new();
        for segment in lineage {
            let mut refs: Vec<&MarketDataRecord> = Vec::new();
            for record in self.records() {
                let key = record.key();
                if key.kind != DatasetKind::CorporateActionDividend || key.symbol != segment.symbol
                {
                    continue;
                }
                // Bounded to the read's basis cutoff (within adjusted_through); a within-cutoff
                // dividend outside the segment's validity window fails closed (same discipline as the
                // split leg / raw series: inconsistent rename data).
                if key.event_ts <= adjusted_through {
                    check_action_in_segment_window(segment, key.event_ts)?;
                    refs.push(record);
                }
            }
            dividends.extend(
                normalization::dividend_events_for(&segment.symbol, &refs, prev_close_of)?
                    .into_iter()
                    .map(|mut event| {
                        if let Some(symbol) = retag_to {
                            event.symbol = symbol.to_string();
                        }
                        event
                    }),
            );
        }
        Ok(dividends)
    }

    /// The structural corporate-action events (delistings, mergers, symbol changes) across the
    /// lineage whose effective instant falls inside `[query.start_ts, query.end_ts]`,
    /// `effective_ts`-ascending. Merger terms are read directly off the record — store validation
    /// (`validate_record`) guarantees their presence and sanity, so term extraction is total.
    ///
    /// FAIL-CLOSED on an in-window structural event dated outside its symbol's lineage validity
    /// window (the same [`check_action_in_segment_window`] discipline the split / dividend
    /// collectors apply): a predecessor's delisting or merger on/after its rename — or a
    /// successor's before it — is structurally impossible rename data, and surfacing it would hand
    /// an APPLICATION consumer (the SRS-DATA-021 fact read) an event its book can only no-op
    /// against, silently skipping a required cancel/freeze. The one boundary exception is the
    /// symbol-change record itself: it RETIRES its segment, so it legitimately sits exactly ON the
    /// segment's closing boundary (`event_ts == valid_until`) — the strict check would reject
    /// every legitimate rename.
    fn corporate_events_in_window(
        &self,
        query: &UnifiedHistoricalQuery,
        lineage: &[LineageSegment],
    ) -> Result<Vec<CorporateActionEvent>, CoverageError> {
        let mut events: Vec<CorporateActionEvent> = Vec::new();
        for segment in lineage {
            for record in self.records() {
                let key = record.key();
                if key.symbol != segment.symbol
                    || key.event_ts < query.start_ts
                    || key.event_ts > query.end_ts
                {
                    continue;
                }
                match key.kind {
                    DatasetKind::CorporateActionDelisting => {
                        check_action_in_segment_window(segment, key.event_ts)?;
                        events.push(CorporateActionEvent::Delisting {
                            symbol: key.symbol.clone(),
                            effective_ts: key.event_ts,
                        });
                    }
                    DatasetKind::CorporateActionMerger => {
                        check_action_in_segment_window(segment, key.event_ts)?;
                        events.push(CorporateActionEvent::Merger {
                            symbol: key.symbol.clone(),
                            successor: successor_symbol(key)
                                .expect("store validation guarantees a merger successor")
                                .to_string(),
                            numerator: field_of(record, "numerator")
                                .expect("store validation guarantees merger terms"),
                            denominator: field_of(record, "denominator")
                                .expect("store validation guarantees merger terms"),
                            cash_per_share_minor: field_of(record, "cash_per_share_minor")
                                .expect("store validation guarantees merger terms"),
                            effective_ts: key.event_ts,
                        });
                    }
                    DatasetKind::CorporateActionSymbolChange => {
                        check_symbol_change_in_segment_window(segment, key.event_ts)?;
                        events.push(CorporateActionEvent::SymbolChange {
                            predecessor: key.symbol.clone(),
                            successor: successor_symbol(key)
                                .expect("store validation guarantees a symbol-change successor")
                                .to_string(),
                            effective_ts: key.event_ts,
                        });
                    }
                    _ => {}
                }
            }
        }
        events.sort_by_key(|event| match event {
            CorporateActionEvent::Delisting { effective_ts, .. }
            | CorporateActionEvent::Merger { effective_ts, .. }
            | CorporateActionEvent::SymbolChange { effective_ts, .. } => *effective_ts,
        });
        Ok(events)
    }
}

/// Which corporate actions a gated read re-quotes into the served prices.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AdjustmentMode {
    /// Splits / reverse splits only (the SYS-29 split-adjusted basis).
    SplitOnly,
    /// Splits AND dividends, back-adjusted (the SYS-29 fully-adjusted basis).
    Full,
    /// Splits AND dividends, reinvested forward (the SYS-29 total-return basis, SRS-DATA-012).
    TotalReturn,
}

/// Which cutoff bounds the applied corporate actions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AdjustmentBasis {
    /// Apply every event within the proven coverage frontier `D` (the "current" basis).
    Frontier,
    /// Apply only events effective at or before `query.end_ts` (the point-in-time basis).
    AsOfEnd,
}

/// One symbol's validity window within a rename lineage: bars AND corporate actions of `symbol`
/// legitimately exist only in `[valid_from, valid_until)` (either bound absent = unbounded). Enforced
/// by the series builder (bars) and the split/dividend collectors (actions).
struct LineageSegment {
    symbol: String,
    valid_from: Option<i64>,
    valid_until: Option<i64>,
}

/// Fail closed ([`CoverageError::AmbiguousLineage`]) if a within-cutoff corporate action for `segment`
/// is dated outside its symbol's lineage validity window `[valid_from, valid_until)` — the same
/// discipline [`MarketDataStore::lineage_raw_series`] applies to bars. A predecessor action on/after
/// its rename (or a successor action before it) is inconsistent rename data: retagging it to the
/// queried symbol would silently mis-adjust the wrong bars, so it must fail the read rather than apply.
fn check_action_in_segment_window(
    segment: &LineageSegment,
    event_ts: i64,
) -> Result<(), CoverageError> {
    if segment.valid_from.is_some_and(|from| event_ts < from)
        || segment.valid_until.is_some_and(|until| event_ts >= until)
    {
        return Err(CoverageError::AmbiguousLineage {
            symbol: segment.symbol.clone(),
            context: "a corporate action is dated outside its symbol's lineage validity window",
        });
    }
    Ok(())
}

/// The validity check for a SYMBOL-CHANGE record: like [`check_action_in_segment_window`] but
/// allowing `event_ts == valid_until` — the rename record is what RETIRES its segment, so it
/// legitimately sits exactly on the closing boundary (a split / dividend / delisting / merger at
/// that instant would instead be inconsistent with the rename). A rename before the segment's
/// `valid_from`, or strictly after its `valid_until`, is inconsistent data — fail closed.
fn check_symbol_change_in_segment_window(
    segment: &LineageSegment,
    event_ts: i64,
) -> Result<(), CoverageError> {
    if segment.valid_from.is_some_and(|from| event_ts < from)
        || segment.valid_until.is_some_and(|until| event_ts > until)
    {
        return Err(CoverageError::AmbiguousLineage {
            symbol: segment.symbol.clone(),
            context: "a symbol change is dated outside its symbol's lineage validity window",
        });
    }
    Ok(())
}

/// The queried symbol's relabeling of a lineage bar: the same record with the key symbol swapped
/// (verbatim clone when it already matches). Total: only the symbol changes, so validity holds.
fn relabeled(record: &MarketDataRecord, symbol: &str) -> MarketDataRecord {
    if record.key().symbol == symbol {
        return record.clone();
    }
    let key = NaturalKey {
        symbol: symbol.to_string(),
        ..record.key().clone()
    };
    MarketDataRecord::new(key, record.fields().iter().cloned())
        .expect("relabeling preserves the source record's validity")
}

/// The value of `record`'s field named `name`, if present.
fn field_of(record: &MarketDataRecord, name: &str) -> Option<i64> {
    record
        .fields()
        .iter()
        .find(|field| field.name == name)
        .map(|field| field.value_minor)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{coverage_record, MarketField, NaturalKey};

    fn field(name: &str, value_minor: i64) -> MarketField {
        MarketField {
            name: name.to_string(),
            value_minor,
        }
    }

    fn daily_bar(symbol: &str, event_ts: i64, close: i64, volume: i64) -> MarketDataRecord {
        MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::DailyEquityBar,
                symbol: symbol.to_string(),
                resolution: "1d".to_string(),
                event_ts,
                option_contract: None,
            },
            [field("close", close), field("volume", volume)],
        )
        .expect("well-formed daily bar")
    }

    fn split(
        symbol: &str,
        effective_ts: i64,
        numerator: i64,
        denominator: i64,
    ) -> MarketDataRecord {
        MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::CorporateActionSplit,
                symbol: symbol.to_string(),
                resolution: "split".to_string(),
                event_ts: effective_ts,
                option_contract: None,
            },
            [
                field("denominator", denominator),
                field("numerator", numerator),
            ],
        )
        .expect("well-formed split record")
    }

    fn store_of(records: impl IntoIterator<Item = MarketDataRecord>) -> MarketDataStore {
        let mut store = MarketDataStore::new();
        for record in records {
            store.upsert(record).expect("fixture upsert");
        }
        store
    }

    fn daily_query(symbol: &str, start: i64, end: i64) -> UnifiedHistoricalQuery {
        UnifiedHistoricalQuery::new(symbol, "1d", start, end).with_kind(DatasetKind::DailyEquityBar)
    }

    fn close_of(record: &MarketDataRecord, name: &str) -> i64 {
        record
            .fields()
            .iter()
            .find(|f| f.name == name)
            .expect("field present")
            .value_minor
    }

    #[test]
    fn covered_query_returns_split_adjusted_bars() {
        // AAPL bar @100 (close 10000, vol 100000), a 4-for-1 split @200, coverage through 200.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 200, 4, 1),
            coverage_record(200, "AAPL"),
        ]);
        // Query [0,100]: frontier 200 >= 100 -> served; bar@100 is pre-split (200 > 100) -> adjusted.
        let result = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert_eq!(result.coverage_through, 200);
        assert_eq!(result.records.len(), 1);
        assert_eq!(close_of(&result.records[0], "close"), 2_500); // 10000 / 4
        assert_eq!(close_of(&result.records[0], "volume"), 400_000); // 100000 * 4
    }

    #[test]
    fn as_of_caps_splits_at_window_end_not_coverage_frontier() {
        // Split @200 is AFTER the window end (100) but within coverage (300). query_split_adjusted
        // applies it (as-of-D basis); query_split_adjusted_as_of does NOT (point-in-time as of the
        // window end) -- so a future split cannot bias a historical read.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 200, 4, 1),
            coverage_record(300, "AAPL"),
        ]);
        let q = daily_query("AAPL", 0, 100);
        // as-of-D applies the future split (10000 / 4 = 2500)...
        assert_eq!(
            close_of(&store.query_split_adjusted(&q).unwrap().records[0], "close"),
            2_500
        );
        // ...but the point-in-time as-of read leaves the bar on its then-current basis (no lookahead).
        let as_of = store.query_split_adjusted_as_of(&q).unwrap();
        assert_eq!(
            close_of(&as_of.records[0], "close"),
            10_000,
            "future split must not be applied"
        );
        assert_eq!(close_of(&as_of.records[0], "volume"), 100_000);
        // The result keeps the proven frontier (300) SEPARATE from the actual adjustment basis (the
        // as-of date, 100) -- so a consumer is never misled that the bars are adjusted through 300.
        assert_eq!(
            as_of.coverage_through, 300,
            "coverage proven through the frontier D"
        );
        assert_eq!(
            as_of.adjusted_through, 100,
            "but adjusted only through the as-of date (query.end_ts)"
        );
        // The frontier method, by contrast, adjusts through D, so the two coincide.
        let d_basis = store.query_split_adjusted(&q).unwrap();
        assert_eq!(d_basis.coverage_through, 300);
        assert_eq!(d_basis.adjusted_through, 300);
    }

    #[test]
    fn as_of_applies_in_window_splits_and_fails_closed_when_uncovered() {
        // A split @50 (<= the window end 100) IS applied to the pre-split bar @40 (within the as-of
        // window); the post-split bar @100 is already on the new basis.
        let store = store_of([
            daily_bar("AAPL", 40, 10_000, 100_000),
            daily_bar("AAPL", 100, 3_000, 100_000),
            split("AAPL", 50, 4, 1),
            coverage_record(300, "AAPL"),
        ]);
        let result = store
            .query_split_adjusted_as_of(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert_eq!(close_of(&result.records[0], "close"), 2_500); // pre-split @40 re-quoted 10000/4
        assert_eq!(close_of(&result.records[1], "close"), 3_000); // post-split @100 unchanged

        // The coverage gate is unchanged: an uncovered query fails closed, same as query_split_adjusted.
        let bare = store_of([
            daily_bar("AAPL", 40, 10_000, 100_000),
            split("AAPL", 50, 4, 1),
        ]);
        assert!(matches!(
            bare.query_split_adjusted_as_of(&daily_query("AAPL", 0, 100))
                .unwrap_err(),
            CoverageError::NotCovered { .. }
        ));
    }

    #[test]
    fn gate_passes_at_frontier_equal_to_end_and_above() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(150, "AAPL"),
        ]);
        // D == end_ts (150 == 150) passes (boundary).
        assert!(store
            .query_split_adjusted(&daily_query("AAPL", 0, 150))
            .is_ok());
        // D > end_ts (150 > 100) passes.
        assert!(store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .is_ok());
    }

    #[test]
    fn gate_fails_closed_one_short_of_the_end() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(149, "AAPL"),
        ]);
        // D == end_ts - 1 (149 < 150) fails closed.
        let err = store
            .query_split_adjusted(&daily_query("AAPL", 0, 150))
            .unwrap_err();
        assert_eq!(
            err,
            CoverageError::NotCovered {
                symbol: "AAPL".to_string(),
                have_through: Some(149),
                need_through: 150,
            }
        );
    }

    #[test]
    fn no_coverage_record_fails_closed() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 200, 4, 1),
        ]);
        let err = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap_err();
        assert_eq!(
            err,
            CoverageError::NotCovered {
                symbol: "AAPL".to_string(),
                have_through: None,
                need_through: 100,
            }
        );
    }

    #[test]
    fn split_in_the_covered_tail_above_end_still_adjusts_in_window_bars() {
        // Coverage through 300; query [0,100]; a split @200 is in (end=100, D=300]. It must still
        // adjust the in-window bar@100 (200 > 100), which is exactly why the gate is `D >= end`, not
        // a window-bounded split scan.
        let store = store_of([
            daily_bar("AAPL", 100, 8_000, 50_000),
            split("AAPL", 200, 2, 1),
            coverage_record(300, "AAPL"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert_eq!(close_of(&result.records[0], "close"), 4_000); // 8000 / 2
        assert_eq!(close_of(&result.records[0], "volume"), 100_000); // 50000 * 2
    }

    #[test]
    fn split_exactly_at_end_leaves_the_end_bar_unadjusted_but_adjusts_earlier_bars() {
        // Split effective exactly at end_ts (200). The bar AT 200 is already post-split (strict
        // boundary) -> unadjusted; the bar @100 is pre-split -> adjusted. The split has
        // effective_ts == end_ts <= D, so it is known and handled by a `D >= end_ts` gate.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            daily_bar("AAPL", 200, 4_000, 40_000),
            split("AAPL", 200, 4, 1),
            coverage_record(200, "AAPL"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("AAPL", 0, 200))
            .unwrap();
        assert_eq!(result.records.len(), 2);
        // bar@100 adjusted (10000/4 = 2500), bar@200 unadjusted (4000).
        assert_eq!(close_of(&result.records[0], "close"), 2_500);
        assert_eq!(close_of(&result.records[1], "close"), 4_000);
    }

    #[test]
    fn split_beyond_the_frontier_is_not_applied_as_of_d() {
        // as-of-D: a split with effective_ts > D must NOT adjust the result, even though its record is
        // present in the store. Coverage through 150, query [0,100], a split @200 (> 150): the bar@100
        // must come back UNADJUSTED (close 10000), labeled coverage_through:150 -- not silently
        // re-based past the advertised frontier.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 200, 4, 1),
            coverage_record(150, "AAPL"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert_eq!(result.coverage_through, 150);
        assert_eq!(
            close_of(&result.records[0], "close"),
            10_000,
            "split@200 > D=150 must not apply"
        );
        assert_eq!(close_of(&result.records[0], "volume"), 100_000);
    }

    #[test]
    fn split_at_exactly_the_frontier_is_applied() {
        // Boundary: a split with effective_ts == D is within the proven-complete frontier (coverage
        // through D is inclusive), so it IS applied to earlier in-window bars.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 150, 4, 1),
            coverage_record(150, "AAPL"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert_eq!(result.coverage_through, 150);
        assert_eq!(
            close_of(&result.records[0], "close"),
            2_500,
            "split@150 == D=150 applies"
        );
    }

    #[test]
    fn malformed_split_beyond_the_frontier_does_not_fail_an_as_of_d_query() {
        // A malformed (non-positive) split beyond the frontier is out of the as-of-D scope, so it must
        // NOT fail closed an otherwise-covered query (only malformed splits WITHIN the frontier do).
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 300, 0, 1), // malformed, but effective_ts 300 > D=200 -> excluded
            coverage_record(200, "AAPL"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert_eq!(close_of(&result.records[0], "close"), 10_000);
    }

    #[test]
    fn frontier_is_the_max_over_multiple_coverage_records() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(100, "AAPL"),
            coverage_record(300, "AAPL"),
            coverage_record(200, "AAPL"),
        ]);
        assert_eq!(store.coverage_frontier("AAPL"), Some(300));
        // A query needing through 250 passes (max frontier 300 >= 250).
        assert!(store
            .query_split_adjusted(&daily_query("AAPL", 0, 250))
            .is_ok());
    }

    #[test]
    fn advancing_the_frontier_never_conflicts() {
        // Append-only, idempotent: advancing is an Insert, re-asserting the same D is a no-op, and a
        // ConflictingContent is structurally impossible (event_ts = D, so a different D is a new key).
        use crate::store::UpsertOutcome;
        let mut store = MarketDataStore::new();
        assert_eq!(
            store.upsert(coverage_record(100, "AAPL")).unwrap(),
            UpsertOutcome::Inserted
        );
        assert_eq!(
            store.upsert(coverage_record(200, "AAPL")).unwrap(),
            UpsertOutcome::Inserted
        );
        assert_eq!(
            store.upsert(coverage_record(200, "AAPL")).unwrap(),
            UpsertOutcome::UnchangedDuplicate
        );
        assert_eq!(store.coverage_frontier("AAPL"), Some(200));
    }

    #[test]
    fn coverage_is_per_symbol() {
        // MSFT coverage does not cover an AAPL query.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(500, "MSFT"),
        ]);
        assert_eq!(store.coverage_frontier("AAPL"), None);
        let err = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap_err();
        assert!(matches!(
            err,
            CoverageError::NotCovered {
                have_through: None,
                ..
            }
        ));
    }

    #[test]
    fn unspecified_or_non_equity_query_kind_fails_closed() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            coverage_record(200, "AAPL"),
        ]);
        // Kind-agnostic query (no kind) is rejected before any coverage/math.
        let agnostic = UnifiedHistoricalQuery::new("AAPL", "1d", 0, 100);
        assert_eq!(
            store.query_split_adjusted(&agnostic).unwrap_err(),
            CoverageError::UnsupportedQueryKind {
                kind: "unspecified"
            }
        );
        // A non-equity kind (fundamental) is rejected.
        let fundamental =
            UnifiedHistoricalQuery::new("AAPL", "1d", 0, 100).with_kind(DatasetKind::Fundamental);
        assert_eq!(
            store.query_split_adjusted(&fundamental).unwrap_err(),
            CoverageError::UnsupportedQueryKind {
                kind: "fundamental"
            }
        );
    }

    #[test]
    fn covered_but_empty_in_range_is_a_value_not_an_error() {
        // Coverage exists and reaches the end, but no bar falls in the range -> a covered empty result.
        let store = store_of([
            daily_bar("AAPL", 500, 10_000, 100_000),
            coverage_record(100, "AAPL"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert!(result.records.is_empty());
        assert_eq!(result.coverage_through, 100);
    }

    #[test]
    fn a_forged_coverage_frontier_cannot_enter_the_store_so_the_gate_holds() {
        // The gate trusts the coverage record's event_ts as the frontier. MarketDataRecord::new is
        // public, so a producer could TRY to forge a coverage record whose key event_ts (999) asserts a
        // frontier its complete_through field (200) does not carry. Store validation rejects it, so it
        // can never enter the store -> the gate sees no coverage -> NotCovered (it cannot be fooled).
        let forged = MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::CorporateActionCoverage,
                symbol: "AAPL".to_string(),
                resolution: "coverage".to_string(),
                event_ts: 999,
                option_contract: None,
            },
            [field("complete_through", 200)],
        );
        assert!(
            forged.is_err(),
            "a coverage record with a forged frontier must fail validation"
        );

        // A store built only from the honest constructor has a trustworthy frontier; a bare-bones store
        // with no coverage record fails the gate closed.
        let store = store_of([daily_bar("AAPL", 100, 10_000, 100_000)]);
        assert_eq!(store.coverage_frontier("AAPL"), None);
        assert!(matches!(
            store.query_split_adjusted(&daily_query("AAPL", 0, 100)),
            Err(CoverageError::NotCovered {
                have_through: None,
                ..
            })
        ));
    }

    #[test]
    fn malformed_split_fails_closed_through_the_gate() {
        // A non-positive split factor for the symbol surfaces as a Normalization error (not a panic).
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 200, 0, 1), // zero numerator
            coverage_record(200, "AAPL"),
        ]);
        let err = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap_err();
        assert!(matches!(err, CoverageError::Normalization(_)));
    }

    #[test]
    fn unadjusted_when_no_split_but_covered() {
        // Covered, but no split for the symbol -> the bars come back verbatim (identity), correctly
        // labeled split-adjusted as-of-D (not a masquerade: there genuinely is no split to apply).
        let store = store_of([
            daily_bar("AAPL", 100, 10_003, 100_001),
            coverage_record(200, "AAPL"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert_eq!(close_of(&result.records[0], "close"), 10_003);
        assert_eq!(close_of(&result.records[0], "volume"), 100_001);
    }

    // ----------------------------------------------------------------------- //
    // Fully-adjusted (splits AND dividends) through the gate.
    // ----------------------------------------------------------------------- //

    use crate::store::{delisting_record, dividend_record, merger_record, symbol_change_record};

    #[test]
    fn covered_fully_adjusted_applies_dividends_and_splits_but_split_adjusted_ignores_dividends() {
        // Bar @100 (close 10000, vol 100000); $1.00 dividend ex @150 (reference close = the bar
        // itself); 4-for-1 split @200; coverage through 200.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            dividend_record(150, "AAPL", 100),
            split("AAPL", 200, 4, 1),
            coverage_record(200, "AAPL"),
        ]);
        let q = daily_query("AAPL", 0, 100);
        // Fully-adjusted: 10000 · (1·9900)/(4·10000) = 2475; volume takes the SPLIT factor only.
        let full = store.query_fully_adjusted(&q).unwrap();
        assert_eq!(full.coverage_through, 200);
        assert_eq!(full.adjusted_through, 200);
        assert_eq!(close_of(&full.records[0], "close"), 2_475);
        assert_eq!(close_of(&full.records[0], "volume"), 400_000);
        // Split-adjusted over the SAME store ignores the dividend record entirely: 10000/4 = 2500.
        let split_only = store.query_split_adjusted(&q).unwrap();
        assert_eq!(close_of(&split_only.records[0], "close"), 2_500);
        assert_eq!(close_of(&split_only.records[0], "volume"), 400_000);
    }

    #[test]
    fn fully_adjusted_fails_closed_when_uncovered_exactly_like_split_adjusted() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            dividend_record(150, "AAPL", 100),
        ]);
        for query_fn in [
            MarketDataStore::query_fully_adjusted,
            MarketDataStore::query_fully_adjusted_as_of,
        ] {
            let err = query_fn(&store, &daily_query("AAPL", 0, 100)).unwrap_err();
            assert_eq!(
                err,
                CoverageError::NotCovered {
                    symbol: "AAPL".to_string(),
                    have_through: None,
                    need_through: 100,
                }
            );
        }
        // The kind guard is identical too.
        assert_eq!(
            store
                .query_fully_adjusted(&UnifiedHistoricalQuery::new("AAPL", "1d", 0, 100))
                .unwrap_err(),
            CoverageError::UnsupportedQueryKind {
                kind: "unspecified"
            }
        );
    }

    #[test]
    fn dividend_in_the_covered_tail_applies_on_the_frontier_basis_but_not_as_of() {
        // Dividend ex @200 is AFTER the window end (100) but within coverage (300): the frontier
        // basis applies it; the point-in-time basis must NOT (dividend lookahead).
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            dividend_record(200, "AAPL", 100),
            coverage_record(300, "AAPL"),
        ]);
        let q = daily_query("AAPL", 0, 100);
        let frontier = store.query_fully_adjusted(&q).unwrap();
        assert_eq!(close_of(&frontier.records[0], "close"), 9_900);
        assert_eq!(frontier.adjusted_through, 300);
        let as_of = store.query_fully_adjusted_as_of(&q).unwrap();
        assert_eq!(
            close_of(&as_of.records[0], "close"),
            10_000,
            "a future dividend must not bias a point-in-time read"
        );
        assert_eq!(as_of.coverage_through, 300);
        assert_eq!(as_of.adjusted_through, 100);
    }

    #[test]
    fn dividend_beyond_the_frontier_is_excluded_even_if_malformed() {
        // A dividend past D is out of the as-of-D scope: not applied, and a malformed one (missing
        // reference close -- no bar before ex 400 beyond D) cannot fail an otherwise-covered query.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            dividend_record(250, "AAPL", 100), // beyond D=200
            coverage_record(200, "AAPL"),
        ]);
        let result = store
            .query_fully_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert_eq!(close_of(&result.records[0], "close"), 10_000);
    }

    #[test]
    fn dividend_reference_close_comes_from_the_raw_series_before_the_window() {
        // The reference close for a dividend is the last RAW close strictly before its ex-date --
        // even when that bar precedes the query window (the series is resolved store-wide, not
        // window-clipped) and even when a split later re-quotes the served bars.
        let store = store_of([
            daily_bar("AAPL", 50, 20_000, 100_000), // the reference bar, OUTSIDE the window
            daily_bar("AAPL", 100, 10_000, 100_000),
            dividend_record(60, "AAPL", 200), // ex @60: reference = close 20000 @50 -> factor 99/100
            coverage_record(200, "AAPL"),
        ]);
        // Window [100, 100]: only the bar @100, which is ON/after ex 60 -> dividend does not apply to
        // it; but the RESOLUTION of the reference close must not fail (bar @50 exists store-wide).
        let result = store
            .query_fully_adjusted(&daily_query("AAPL", 100, 100))
            .unwrap();
        assert_eq!(close_of(&result.records[0], "close"), 10_000);
        // Window [0, 100]: the bar @50 IS pre-ex -> adjusted by (20000-200)/20000 = 99/100.
        let both = store
            .query_fully_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap();
        assert_eq!(close_of(&both.records[0], "close"), 19_800);
        assert_eq!(close_of(&both.records[1], "close"), 10_000);
    }

    #[test]
    fn dividend_with_no_prior_close_fails_the_read_closed() {
        // No bar exists before the ex-date -> the factor cannot be computed -> the WHOLE read fails
        // closed (never a silent factor of 1 dressed as fully-adjusted).
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            dividend_record(50, "AAPL", 100), // ex @50, but the first bar is @100
            coverage_record(200, "AAPL"),
        ]);
        let err = store
            .query_fully_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap_err();
        assert!(matches!(
            err,
            CoverageError::Normalization(NormalizationError::MissingReferenceClose {
                ex_ts: 50,
                ..
            })
        ));
    }

    #[test]
    fn basis_crossing_split_between_reference_and_ex_date_fails_closed_through_the_gate() {
        // A split effective between the dividend's reference close (@100) and its ex-date (@160)
        // puts the amount and the reference on different share bases -> fail closed.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 150, 4, 1),
            dividend_record(160, "AAPL", 25),
            coverage_record(200, "AAPL"),
        ]);
        let err = store
            .query_fully_adjusted(&daily_query("AAPL", 0, 100))
            .unwrap_err();
        assert!(matches!(
            err,
            CoverageError::Normalization(NormalizationError::BasisCrossingDividend { .. })
        ));
        // The split-adjusted read over the same store is untouched by the dividend (mode semantics).
        assert!(store
            .query_split_adjusted(&daily_query("AAPL", 0, 100))
            .is_ok());
    }

    // ----------------------------------------------------------------------- //
    // Total-return (reinvested dividends, SRS-DATA-012) through the gate.
    // ----------------------------------------------------------------------- //

    #[test]
    fn covered_total_return_reinvests_dividends_distinct_from_fully_adjusted() {
        // Reference close 10000 @100; $1.00 dividend ex @150; post-ex bar @200 (dropped to 9900);
        // coverage through 300.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            dividend_record(150, "AAPL", 100),
            daily_bar("AAPL", 200, 9_900, 100_000),
            coverage_record(300, "AAPL"),
        ]);
        let q = daily_query("AAPL", 0, 200);
        // TOTAL-RETURN reinvests forward: the pre-ex bar @100 stays raw (10000), the post-ex bar @200
        // is grossed UP by 10000/9900 -> 10000. A continuous, reinvested series.
        let tr = store.query_total_return(&q).unwrap();
        assert_eq!(tr.coverage_through, 300);
        assert_eq!(tr.adjusted_through, 300);
        assert_eq!(close_of(&tr.records[0], "close"), 10_000); // bar@100 raw (ex 150 > 100)
        assert_eq!(close_of(&tr.records[1], "close"), 10_000); // bar@200 reinvested 9900·10000/9900
        assert_eq!(close_of(&tr.records[1], "volume"), 100_000); // dividend never scales volume

        // FULLY-ADJUSTED back-adjusts the OTHER way: pre-ex bar @100 scaled DOWN to 9900, post-ex
        // bar @200 stays raw (9900). DISTINCT series from total-return (both flat here, different level).
        let fa = store.query_fully_adjusted(&q).unwrap();
        assert_eq!(close_of(&fa.records[0], "close"), 9_900);
        assert_eq!(close_of(&fa.records[1], "close"), 9_900);
        assert_ne!(
            close_of(&tr.records[1], "close"),
            close_of(&fa.records[1], "close"),
            "total-return and fully-adjusted are distinct series"
        );
        // SPLIT-ADJUSTED ignores the dividend entirely (mode semantics): both bars verbatim.
        let sa = store.query_split_adjusted(&q).unwrap();
        assert_eq!(close_of(&sa.records[0], "close"), 10_000);
        assert_eq!(close_of(&sa.records[1], "close"), 9_900);
    }

    #[test]
    fn total_return_fails_closed_when_uncovered_exactly_like_the_other_adjusted_reads() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            dividend_record(150, "AAPL", 100),
        ]);
        for query_fn in [
            MarketDataStore::query_total_return,
            MarketDataStore::query_total_return_as_of,
        ] {
            let err = query_fn(&store, &daily_query("AAPL", 0, 200)).unwrap_err();
            assert_eq!(
                err,
                CoverageError::NotCovered {
                    symbol: "AAPL".to_string(),
                    have_through: None,
                    need_through: 200,
                }
            );
        }
        // The equity-kind guard is identical too.
        assert_eq!(
            store
                .query_total_return(&UnifiedHistoricalQuery::new("AAPL", "1d", 0, 200))
                .unwrap_err(),
            CoverageError::UnsupportedQueryKind {
                kind: "unspecified"
            }
        );
    }

    #[test]
    fn total_return_as_of_caps_splits_at_window_end_while_the_reinvest_leg_is_basis_invariant() {
        // Reference close 10000 @100; dividend ex @120; queried post-ex bar @150 (dropped to 9900);
        // a 4-for-1 split @200 AFTER the window end (150) but within coverage (300).
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            dividend_record(120, "AAPL", 100),
            daily_bar("AAPL", 150, 9_900, 100_000),
            split("AAPL", 200, 4, 1),
            coverage_record(300, "AAPL"),
        ]);
        let q = daily_query("AAPL", 0, 150);
        // Frontier basis applies the future split (÷4) AND the reinvested dividend: bar@150 =
        // 9900·(1·10000)/(4·9900) = 2500.
        let frontier = store.query_total_return(&q).unwrap();
        assert_eq!(frontier.adjusted_through, 300);
        assert_eq!(close_of(&frontier.records[1], "close"), 2_500);
        // Point-in-time basis EXCLUDES the future split (effective_ts 200 > 150), but the dividend
        // reinvestment is unchanged (ex 120 <= 150, basis-invariant): bar@150 = 9900·10000/9900 = 10000.
        let as_of = store.query_total_return_as_of(&q).unwrap();
        assert_eq!(as_of.adjusted_through, 150);
        assert_eq!(as_of.coverage_through, 300);
        assert_eq!(close_of(&as_of.records[1], "close"), 10_000);
        // The pre-ex bar @100 is raw under as-of (no split, ex 120 > 100), split-only under frontier.
        assert_eq!(close_of(&as_of.records[0], "close"), 10_000);
        assert_eq!(close_of(&frontier.records[0], "close"), 2_500); // 10000/4 (split only, ex 120 > 100)
    }

    #[test]
    fn total_return_frontier_ignores_an_invalid_future_dividend_beyond_the_query_end() {
        // REGRESSION (adversarial review): a dividend ex @250 in (query.end_ts=200, D=300] with an
        // INVALID term (amount 10000 >= its reference close 10000 @200). Total-return can never apply
        // it (reinvest needs ex_ts <= t <= 200), so it MUST NOT fail the read -- the no-dividend-
        // lookahead property enforced at the resolution boundary, not just the per-bar application.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            daily_bar("AAPL", 200, 10_000, 100_000),
            dividend_record(250, "AAPL", 10_000), // ex @250: amount 10000 >= reference close 10000
            coverage_record(300, "AAPL"),
        ]);
        let q = daily_query("AAPL", 0, 200);
        // total-return succeeds and returns the raw bars (no dividend ex <= 200 applies).
        let tr = store.query_total_return(&q).unwrap();
        assert_eq!(close_of(&tr.records[0], "close"), 10_000);
        assert_eq!(close_of(&tr.records[1], "close"), 10_000);
        // CONTRAST: fully-adjusted DOES apply an ex @250 > t dividend on the frontier basis, so it
        // legitimately validates it and FAILS closed on the invalid term (the two modes differ here).
        assert!(matches!(
            store.query_fully_adjusted(&q),
            Err(CoverageError::Normalization(
                NormalizationError::InvalidDividendTerm { .. }
            ))
        ));
    }

    #[test]
    fn total_return_frontier_ignores_a_future_basis_crossing_dividend_beyond_the_query_end() {
        // REGRESSION (adversarial review): a dividend ex @300 in (query.end_ts=250, D=400] whose
        // reference close (@200) is separated from its ex-date by a split @260 -> a BasisCrossingDividend
        // for fully-adjusted. Total-return never applies the ex @300 dividend (ex > 250 >= t), so the
        // future basis-crossing must NOT fail the total-return read.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            daily_bar("AAPL", 200, 10_000, 100_000),
            split("AAPL", 260, 4, 1),
            dividend_record(300, "AAPL", 100), // ex @300, reference close @200; split @260 in (200, 300]
            coverage_record(400, "AAPL"),
        ]);
        let q = daily_query("AAPL", 0, 250);
        // total-return succeeds: the future dividend @300 is out of scope (its basis-crossing is never
        // checked because it is never resolved), the split @260 still back-adjusts the in-window bars.
        assert!(store.query_total_return(&q).is_ok());
        // CONTRAST: fully-adjusted resolves the ex @300 dividend on the frontier basis and FAILS closed
        // on the basis-crossing split.
        assert!(matches!(
            store.query_fully_adjusted(&q),
            Err(CoverageError::Normalization(
                NormalizationError::BasisCrossingDividend { .. }
            ))
        ));
    }

    // ----------------------------------------------------------------------- //
    // Symbol-change lineage through the gate.
    // ----------------------------------------------------------------------- //

    #[test]
    fn querying_the_successor_returns_the_predecessor_history_relabeled() {
        // AAPL bars @100/@200, renamed AAPL->AAPLN @300, AAPLN bar @400; coverage on the QUERIED
        // symbol (AAPLN) through 500. The lineage read returns one continuous series, every record
        // relabeled to the queried symbol.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            daily_bar("AAPL", 200, 11_000, 100_000),
            symbol_change_record(300, "AAPL", "AAPLN"),
            daily_bar("AAPLN", 400, 12_000, 100_000),
            coverage_record(500, "AAPLN"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("AAPLN", 0, 500))
            .unwrap();
        assert_eq!(result.records.len(), 3);
        for (record, (ts, close)) in
            result
                .records
                .iter()
                .zip([(100, 10_000), (200, 11_000), (400, 12_000)])
        {
            assert_eq!(
                record.key().symbol,
                "AAPLN",
                "every bar is relabeled to the queried symbol"
            );
            assert_eq!(record.key().event_ts, ts);
            assert_eq!(close_of(record, "close"), close);
        }
        // The rename is surfaced as an in-window structural event.
        assert_eq!(
            result.events,
            vec![CorporateActionEvent::SymbolChange {
                predecessor: "AAPL".to_string(),
                successor: "AAPLN".to_string(),
                effective_ts: 300,
            }]
        );
    }

    #[test]
    fn adjustments_compose_across_the_lineage_hop() {
        // A PREDECESSOR-era split (AAPL 2-for-1 @150) and a SUCCESSOR-era dividend (AAPLN $1.00 ex
        // @450, reference close 12000 @400) both apply to the relabeled series: the instrument is one
        // continuous entity through the rename.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 150, 2, 1),
            symbol_change_record(300, "AAPL", "AAPLN"),
            daily_bar("AAPLN", 400, 12_000, 100_000),
            dividend_record(450, "AAPLN", 120),
            coverage_record(500, "AAPLN"),
        ]);
        let result = store
            .query_fully_adjusted(&daily_query("AAPLN", 0, 500))
            .unwrap();
        // Bar @100: split (÷2) AND dividend ((12000-120)/12000 = 99/100) -> 10000·(1·11880)/(2·12000)
        // = 4950; volume ×2.
        assert_eq!(close_of(&result.records[0], "close"), 4_950);
        assert_eq!(close_of(&result.records[0], "volume"), 200_000);
        // Bar @400 (post-split, pre-ex): dividend only -> 12000·99/100 = 11880; volume untouched.
        assert_eq!(close_of(&result.records[1], "close"), 11_880);
        assert_eq!(close_of(&result.records[1], "volume"), 100_000);
    }

    #[test]
    fn lineage_is_gated_by_the_queried_symbols_coverage_not_the_predecessors() {
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            symbol_change_record(300, "AAPL", "AAPLN"),
            coverage_record(500, "AAPL"), // coverage on the PREDECESSOR only
        ]);
        let err = store
            .query_split_adjusted(&daily_query("AAPLN", 0, 400))
            .unwrap_err();
        assert!(
            matches!(err, CoverageError::NotCovered { ref symbol, .. } if symbol == "AAPLN"),
            "the QUERIED symbol's frontier governs the lineage read: {err:?}"
        );
    }

    #[test]
    fn a_rename_cycle_fails_closed() {
        // A->B @200 and B->A @100 (chronologically consistent hops) form a cycle: B's lineage walks
        // to A (hop @200), then A's predecessor is B again -> revisited -> fail closed.
        let store = store_of([
            symbol_change_record(200, "A", "B"),
            symbol_change_record(100, "B", "A"),
            coverage_record(500, "B"),
        ]);
        let err = store
            .query_split_adjusted(&daily_query("B", 0, 400))
            .unwrap_err();
        assert!(matches!(err, CoverageError::LineageCycle { .. }), "{err:?}");
    }

    #[test]
    fn two_predecessors_claiming_one_successor_fail_closed() {
        let store = store_of([
            symbol_change_record(200, "A", "C"),
            symbol_change_record(300, "B", "C"),
            coverage_record(500, "C"),
        ]);
        let err = store
            .query_split_adjusted(&daily_query("C", 0, 400))
            .unwrap_err();
        assert!(
            matches!(
                err,
                CoverageError::AmbiguousLineage {
                    context: "two predecessors claim one successor",
                    ..
                }
            ),
            "{err:?}"
        );
    }

    #[test]
    fn a_predecessor_with_multiple_renames_fails_closed() {
        // A renamed to B @200 but ALSO to C @300: A's history cannot belong to both successors.
        let store = store_of([
            symbol_change_record(200, "A", "B"),
            symbol_change_record(300, "A", "C"),
            coverage_record(500, "B"),
        ]);
        let err = store
            .query_split_adjusted(&daily_query("B", 0, 400))
            .unwrap_err();
        assert!(
            matches!(
                err,
                CoverageError::AmbiguousLineage {
                    context: "a predecessor has multiple renames",
                    ..
                }
            ),
            "{err:?}"
        );
    }

    #[test]
    fn a_bar_outside_its_lineage_validity_window_fails_closed() {
        // A predecessor bar dated ON the rename (300) collides with the successor era -> fail closed
        // rather than double-count or mis-attribute it.
        let store = store_of([
            daily_bar("AAPL", 300, 10_000, 100_000), // AAPL bar AT the rename instant
            symbol_change_record(300, "AAPL", "AAPLN"),
            coverage_record(500, "AAPLN"),
        ]);
        let err = store
            .query_split_adjusted(&daily_query("AAPLN", 0, 400))
            .unwrap_err();
        assert!(
            matches!(
                err,
                CoverageError::AmbiguousLineage {
                    context: "a bar is dated outside its symbol's lineage validity window",
                    ..
                }
            ),
            "{err:?}"
        );
    }

    #[test]
    fn a_corporate_action_outside_its_lineage_validity_window_fails_closed() {
        // REGRESSION (adversarial review): a within-cutoff split/dividend dated OUTSIDE its symbol's
        // lineage validity window is inconsistent rename data. It must FAIL CLOSED (the same discipline
        // lineage_raw_series applies to bars), never be retagged to the queried symbol and silently
        // applied to the wrong bars. Rename AAPL -> AAPLN @300: AAPL valid [.., 300); AAPLN valid [300, ..).
        let action_ctx = "a corporate action is dated outside its symbol's lineage validity window";

        // (1) A PREDECESSOR split dated AFTER the rename (AAPL @350 >= AAPL's valid_until 300).
        let bad_split = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            symbol_change_record(300, "AAPL", "AAPLN"),
            daily_bar("AAPLN", 400, 12_000, 100_000),
            split("AAPL", 350, 2, 1),
            coverage_record(500, "AAPLN"),
        ]);
        assert!(matches!(
            bad_split.query_split_adjusted(&daily_query("AAPLN", 0, 500)),
            Err(CoverageError::AmbiguousLineage { context, .. }) if context == action_ctx
        ));

        // (2) A SUCCESSOR dividend dated BEFORE the rename (AAPLN @250 < AAPLN's valid_from 300). The
        // fully-adjusted AND total-return reads both fail closed (they share the dividend collector).
        let bad_div = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            daily_bar("AAPL", 200, 10_000, 100_000),
            symbol_change_record(300, "AAPL", "AAPLN"),
            daily_bar("AAPLN", 400, 12_000, 100_000),
            dividend_record(250, "AAPLN", 100),
            coverage_record(500, "AAPLN"),
        ]);
        for read in [
            MarketDataStore::query_fully_adjusted,
            MarketDataStore::query_total_return,
        ] {
            assert!(matches!(
                read(&bad_div, &daily_query("AAPLN", 0, 500)),
                Err(CoverageError::AmbiguousLineage { context, .. }) if context == action_ctx
            ));
        }

        // Non-vacuity: a WITHIN-window predecessor split (AAPL @150 < 300) over the same lineage SUCCEEDS.
        let ok = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            split("AAPL", 150, 2, 1),
            symbol_change_record(300, "AAPL", "AAPLN"),
            daily_bar("AAPLN", 400, 12_000, 100_000),
            coverage_record(500, "AAPLN"),
        ]);
        assert!(ok
            .query_split_adjusted(&daily_query("AAPLN", 0, 500))
            .is_ok());
    }

    #[test]
    fn a_rename_in_the_covered_tail_resolves_on_the_frontier_basis_but_not_as_of() {
        // The rename @300 is AFTER the as-of date (200) but within coverage (500): the frontier basis
        // resolves the lineage (serving AAPL's bars under AAPLN); the point-in-time basis does not --
        // at the as-of date the rename has not happened, so AAPLN has no history yet.
        let store = store_of([
            daily_bar("AAPL", 100, 10_000, 100_000),
            symbol_change_record(300, "AAPL", "AAPLN"),
            coverage_record(500, "AAPLN"),
        ]);
        let q = daily_query("AAPLN", 0, 200);
        let frontier = store.query_split_adjusted(&q).unwrap();
        assert_eq!(
            frontier.records.len(),
            1,
            "frontier basis resolves the rename lineage"
        );
        let as_of = store.query_split_adjusted_as_of(&q).unwrap();
        assert!(
            as_of.records.is_empty(),
            "as of 200 the rename has not happened: no lookahead through a future rename"
        );
    }

    // ----------------------------------------------------------------------- //
    // Structural event surfacing (delistings, mergers, symbol changes).
    // ----------------------------------------------------------------------- //

    #[test]
    fn delisting_and_merger_events_are_surfaced_in_window_with_exact_terms() {
        let store = store_of([
            daily_bar("MSFT", 100, 8_000, 50_000),
            delisting_record(150, "MSFT"),
            merger_record(180, "MSFT", "AAPL", 1, 2, 500),
            coverage_record(200, "MSFT"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("MSFT", 0, 200))
            .unwrap();
        assert_eq!(
            result.events,
            vec![
                CorporateActionEvent::Delisting {
                    symbol: "MSFT".to_string(),
                    effective_ts: 150,
                },
                CorporateActionEvent::Merger {
                    symbol: "MSFT".to_string(),
                    successor: "AAPL".to_string(),
                    numerator: 1,
                    denominator: 2,
                    cash_per_share_minor: 500,
                    effective_ts: 180,
                },
            ],
            "events are surfaced effective_ts-ascending with their exact stored terms"
        );
        // The bars themselves are untouched by structural events (no price re-quote).
        assert_eq!(close_of(&result.records[0], "close"), 8_000);
    }

    #[test]
    fn events_outside_the_window_are_not_surfaced() {
        let store = store_of([
            daily_bar("MSFT", 100, 8_000, 50_000),
            delisting_record(150, "MSFT"),
            coverage_record(300, "MSFT"),
        ]);
        // Window [0, 149]: the delisting @150 is outside -> not surfaced.
        let before = store
            .query_split_adjusted(&daily_query("MSFT", 0, 149))
            .unwrap();
        assert!(before.events.is_empty());
        // Window [200, 300]: also outside (the window starts after it).
        let after = store
            .query_split_adjusted(&daily_query("MSFT", 200, 300))
            .unwrap();
        assert!(after.events.is_empty());
    }

    #[test]
    fn covered_empty_window_still_surfaces_its_events() {
        // No bar falls in [140, 160] but the delisting @150 does: a covered empty-record result still
        // carries the structural event (a backtest holding through the window needs it).
        let store = store_of([
            daily_bar("MSFT", 100, 8_000, 50_000),
            delisting_record(150, "MSFT"),
            coverage_record(300, "MSFT"),
        ]);
        let result = store
            .query_split_adjusted(&daily_query("MSFT", 140, 160))
            .unwrap();
        assert!(result.records.is_empty());
        assert_eq!(
            result.events,
            vec![CorporateActionEvent::Delisting {
                symbol: "MSFT".to_string(),
                effective_ts: 150,
            }]
        );
    }
}
