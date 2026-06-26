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
/// half of the SRS-UI / SRS-API surface -- so SRS-BT-006 is `passes:true`. Still deferred (each
/// its own feature): the scheduled full-universe factor job that produces the panel
/// (SRS-FAC-001), the SRS-DATA-007 data wiring, and the REST/dashboard rendering half
/// (SRS-UI / SRS-API).
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
/// no clock of its own, no parallelism/RNG). A store-backed MARKET-input loader is AVAILABLE
/// ([`store_inputs::load_daily_market_input`], SRS-DATA-007); wiring it into
/// [`factor_job::run_factor_job`]'s execution path (which still takes caller-supplied inputs), the
/// Sharadar FUNDAMENTAL data wiring (SRS-DATA-005), the live wall-clock performance verification, and the
/// SYS-57 workload-priority admission are deferred, so SRS-FAC-001 stays `passes:false`.
pub mod factor_job;

/// The factor job's SRS-DATA-007 market-input LOADER
/// ([`store_inputs::load_daily_market_input`]). It sources a security's dimensionless
/// [`factor_job::MarketFactorInput`] from the durable [`atp_data::store::MarketDataStore`] through the
/// source-neutral unified query path ([`atp_data::store::MarketDataStore::query_unified`] raw /
/// `query_split_adjusted` gated) — so factor code queries its market inputs by symbol / date range /
/// resolution with NO provider named. This is the market-input PRIMITIVE the factor job will use; it is
/// NOT yet invoked by [`factor_job::run_factor_job`] (which still takes caller-supplied inputs). Wiring it
/// into the factor-job execution path, the Sharadar **fundamental** half (SRS-DATA-005), and the SYS-57
/// workload-priority admission stay deferred — so SRS-FAC-001 (and the SRS-DATA-007 factor-job consumer)
/// stay `passes:false`.
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
