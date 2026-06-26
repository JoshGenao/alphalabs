use atp_data::DataLayer;
use atp_types::RuntimeService;

/// Factor analysis & tear-sheet outputs for completed factor-analysis runs (SRS-BT-006 /
/// SyRS SYS-18; StRS SN-1.05). The deterministic, dependency-free core that computes the
/// three deliverables SYS-18 names -- the [`factor_analysis::InformationCoefficient`]
/// (per-period Spearman rank correlation of factor vs forward return), the
/// [`factor_analysis::FactorReturns`] (quantile-sorted mean returns plus the top-minus-bottom
/// long-short spread), and the [`factor_analysis::TurnoverAnalysis`] (quantile membership
/// churn) -- from a [`factor_analysis::FactorPanel`], bundled into one
/// [`factor_analysis::FactorTearSheet`]. Factor scores and returns are dimensionless f64
/// (the factor domain, not a money leak); the work is deterministic (fixed left-to-right
/// folds, total-order ties), an undefined statistic is None (never a fabricated zero), and
/// a non-finite result fails closed. The operator tear-sheet RENDERING surface is realized by
/// the `factor_tear_sheet_cli` binary (this crate's `src/bin/`), which renders the three
/// deliverables of a fixture panel through [`factor_analysis::compute_tear_sheet`] -- the CLI
/// half of the SRS-UI / SRS-API surface -- so SRS-BT-006 is `passes:true`. Not included in THIS
/// CLI (each its own feature): wiring the tear-sheet to consume the scheduled full-universe factor
/// job's real output (the SRS-FAC-001 producer is its own feature) over real SRS-DATA-007 data, and
/// the REST/dashboard rendering half (SRS-UI / SRS-API).
pub mod factor_analysis;

/// The scheduled full-universe factor job that PRODUCES the panel the [`factor_analysis`]
/// tear-sheet consumes (SRS-FAC-001 / SyRS SYS-32, SYS-33, SYS-51, NFR-P7; StRS SN-2.06).
/// [`factor_job::run_factor_job`] resolves its schedule against a [`factor_job::TradingCalendar`]
/// (the same calendar contract strategy scheduling resolves against), screens/ranks/computes a
/// user-defined [`factor_job::FactorModel`] across the full US-equity universe (an 8,000-floor
/// attestation) over both market and fundamental inputs (a security missing either is an
/// auditable skip, never fabricated), and gates on the calendar-resolved, session-aware deadline
/// INSTANT read from an injected [`factor_job::Clock`] (fail-closed on an early start, a late start
/// -- even on a later session -- a late finalization, or a run that scores too few securities).
/// [`factor_job::assemble_regular_panel`] is the producer bridge to SRS-BT-006: it builds a
/// REGULAR [`factor_analysis::FactorPanel`] (a constant calendar-resolved rebalance interval +
/// a non-overlapping forward horizon) -- exactly the regularity the tear-sheet's interval/
/// horizon-dependent means assume but cannot validate. The work is deterministic for a pure model
/// (canonical-key scoring order, total-order ranking, the deadline read from the injected clock --
/// no clock of its own, no parallelism/RNG). The store-backed READ path
/// ([`store_inputs::run_scheduled_factor_job_over_store`], SRS-DATA-007) sources BOTH the market and the
/// fundamental inputs from the unified store and feeds them to the scored core; its data as-of is
/// DERIVED from the calendar ([`factor_job::TradingCalendar::session_as_of_ts`]) for the scheduled
/// session — NOT a caller-supplied timestamp — so a caller cannot pair a session with a future as-of.
/// What stays deferred is the CONCRETE US-equity calendar that provides the real `SessionOrdinal` ↔ epoch
/// mapping (test calendars stand in), the REAL provider network adapters (Databento / Sharadar,
/// SRS-DATA-001/005), the live wall-clock NFR-P7 performance harness over real securities, and the
/// SYS-57 workload-priority admission, so SRS-FAC-001 stays `passes:false`.
pub mod factor_job;

/// The factor job's SRS-DATA-007 store reader: [`store_inputs::load_daily_market_input`] (market) and
/// [`store_inputs::load_fundamental_input`] (fundamental) source a security's dimensionless
/// [`factor_job::MarketFactorInput`] / [`factor_job::FundamentalFactorInput`] from the durable
/// [`atp_data::store::MarketDataStore`] through the source-neutral unified query path
/// ([`atp_data::store::MarketDataStore::query_unified`] raw / `query_split_adjusted` gated) — so factor
/// code queries its inputs by symbol / date range / resolution with NO provider named (the SRS-DATA-007
/// read surface for factor jobs). [`store_inputs::assemble_factor_inputs`] combines both halves into the
/// [`factor_job::SecurityFactorInputs`] cross-section and
/// [`store_inputs::run_scheduled_factor_job_over_store`] runs the full-universe job over it, DERIVING the
/// data as-of from the calendar ([`factor_job::TradingCalendar::session_as_of_ts`]) for the scheduled
/// session — so a caller cannot pair a session with a future as-of. The concrete US-equity calendar that
/// provides the real `SessionOrdinal` ↔ epoch mapping (test calendars stand in), the real
/// Databento/Sharadar network adapters (SRS-DATA-001/005), and the live wall-clock NFR-P7 harness stay
/// deferred, so SRS-FAC-001 stays `passes:false`. Fixture-sourced store data stands in, as the
/// verification step permits.
pub mod store_inputs;

#[derive(Debug)]
pub struct FactorPipelineRuntime {
    data_layer: DataLayer,
}

impl FactorPipelineRuntime {
    pub fn new(data_layer: DataLayer) -> Self {
        Self { data_layer }
    }

    pub fn service(&self) -> RuntimeService {
        RuntimeService::FactorPipelineRuntime
    }

    pub fn data_layer(&self) -> &DataLayer {
        &self.data_layer
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifies_factor_pipeline_runtime() {
        let runtime = FactorPipelineRuntime::new(DataLayer);
        assert_eq!(runtime.service(), RuntimeService::FactorPipelineRuntime);
    }
}
