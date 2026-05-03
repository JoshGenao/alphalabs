use atp_data::DataLayer;
use atp_types::RuntimeService;

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
