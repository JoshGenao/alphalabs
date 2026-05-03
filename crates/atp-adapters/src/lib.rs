use atp_types::RuntimeService;

pub trait AdapterBoundary {
    fn provider_name(&self) -> &'static str;
}

#[derive(Debug, Default)]
pub struct AdapterRegistry;

impl AdapterRegistry {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::BrokerAndDataProviderAdapters
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifies_adapter_runtime_boundary() {
        let registry = AdapterRegistry;
        assert_eq!(registry.service(), RuntimeService::BrokerAndDataProviderAdapters);
    }
}
