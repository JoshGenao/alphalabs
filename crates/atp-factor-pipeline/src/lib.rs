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
/// a non-finite result fails closed. The scheduled full-universe factor job that produces
/// the panel (SRS-FAC-001), the SRS-DATA-007 data wiring, and the SRS-UI / SRS-API
/// tear-sheet rendering are deferred, so SRS-BT-006 stays `passes:false`.
pub mod factor_analysis;

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
