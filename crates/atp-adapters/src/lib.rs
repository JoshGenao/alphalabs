use atp_types::{RuntimeService, StrategyId};
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AdapterCapability {
    Brokerage,
    MarketData,
    HistoricalData,
    BulkEquityData,
    FundamentalData,
    OptionsData,
    UserParquetImport,
    AlternativeData,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AdapterError {
    NotConfigured {
        adapter: &'static str,
        capability: &'static str,
    },
}

impl fmt::Display for AdapterError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotConfigured {
                adapter,
                capability,
            } => write!(
                formatter,
                "{adapter} adapter capability {capability} is not configured"
            ),
        }
    }
}

impl std::error::Error for AdapterError {}

pub type AdapterResult<T> = Result<T, AdapterError>;

fn not_configured<T>(adapter: &'static str, capability: &'static str) -> AdapterResult<T> {
    Err(AdapterError::NotConfigured {
        adapter,
        capability,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct AdapterVersion {
    pub adapter_version: &'static str,
    pub protocol_version: &'static str,
    pub protocol_label: &'static str,
}

pub const ADAPTER_VERSION_NOT_APPLICABLE: AdapterVersion = AdapterVersion {
    adapter_version: "0.1.0",
    protocol_version: "not-applicable",
    protocol_label: "not-applicable",
};

pub const INTERACTIVE_BROKERS_TWS_API_VERSION: &str = "10.45";
pub const INTERACTIVE_BROKERS_ADAPTER_VERSION: &str = "0.1.0";
pub const INTERACTIVE_BROKERS_PROTOCOL_LABEL: &str = "IB TWS API";

pub trait AdapterBoundary {
    fn provider_name(&self) -> &'static str;

    fn capabilities(&self) -> &'static [AdapterCapability] {
        &[]
    }

    fn version(&self) -> AdapterVersion {
        ADAPTER_VERSION_NOT_APPLICABLE
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderSubmission {
    pub strategy_id: StrategyId,
    pub symbol: String,
    pub quantity: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderReceipt {
    pub broker_order_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MarketDataChannel {
    Trades,
    Quotes,
    Bars,
    OptionChain,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarketDataSubscription {
    pub symbol: String,
    pub channel: MarketDataChannel,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscriptionReceipt {
    pub subscription_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AssetClass {
    Equity,
    Option,
    Future,
    Etf,
    Index,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum NormalizationMode {
    Raw,
    SplitAdjusted,
    FullyAdjusted,
    TotalReturn,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistoricalDataRequest {
    pub symbol: String,
    pub start: String,
    pub end: String,
    pub resolution: String,
    pub asset_class: AssetClass,
    pub normalization_mode: NormalizationMode,
}

#[derive(Debug, Clone, PartialEq)]
pub struct HistoricalBar {
    pub symbol: String,
    pub close: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct HistoricalQueryResult {
    pub symbol: String,
    pub asset_class: AssetClass,
    pub normalization_mode: NormalizationMode,
    pub bars: Vec<HistoricalBar>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UniverseDownloadRequest {
    pub dataset: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackfillRequest {
    pub dataset: String,
    pub years: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IncrementalUpdateRequest {
    pub dataset: String,
    pub date: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DataBatch {
    pub records: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FundamentalsRequest {
    pub symbol: String,
    pub statement: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FundamentalDataSet {
    pub records: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OptionsImportRequest {
    pub underlying: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OptionsDataSet {
    pub contracts: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserParquetImportRequest {
    pub path: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlternativeDataRequest {
    pub dataset: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlternativeDataSet {
    pub dataset: String,
    pub rows: usize,
}

pub trait BrokerageAdapter: AdapterBoundary {
    fn submit_order(&self, _request: OrderSubmission) -> AdapterResult<OrderReceipt> {
        not_configured(self.provider_name(), "submit_order")
    }

    fn cancel_order(&self, _broker_order_id: &str) -> AdapterResult<()> {
        not_configured(self.provider_name(), "cancel_order")
    }

    fn account_status(&self) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "account_status")
    }

    fn positions(&self) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "positions")
    }
}

pub trait MarketDataAdapter: AdapterBoundary {
    fn subscribe_market_data(
        &self,
        _request: MarketDataSubscription,
    ) -> AdapterResult<SubscriptionReceipt> {
        not_configured(self.provider_name(), "subscribe_market_data")
    }
}

pub trait HistoricalDataAdapter: AdapterBoundary {
    fn historical_data(
        &self,
        _request: HistoricalDataRequest,
    ) -> AdapterResult<HistoricalQueryResult> {
        not_configured(self.provider_name(), "historical_data")
    }
}

pub trait DataProviderAdapter: AdapterBoundary {
    fn provider_family(&self) -> &'static str {
        "data-provider"
    }
}

pub trait BulkEquityDataProvider: DataProviderAdapter {
    fn download_full_universe_daily(
        &self,
        _request: UniverseDownloadRequest,
    ) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "download_full_universe_daily")
    }

    fn initial_historical_backfill(&self, _request: BackfillRequest) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "initial_historical_backfill")
    }

    fn incremental_nightly_update(
        &self,
        _request: IncrementalUpdateRequest,
    ) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "incremental_nightly_update")
    }
}

pub trait FundamentalDataProvider: DataProviderAdapter {
    fn ingest_fundamentals(
        &self,
        _request: FundamentalsRequest,
    ) -> AdapterResult<FundamentalDataSet> {
        not_configured(self.provider_name(), "ingest_fundamentals")
    }
}

pub trait OptionsDataProvider: DataProviderAdapter {
    fn import_options(&self, _request: OptionsImportRequest) -> AdapterResult<OptionsDataSet> {
        not_configured(self.provider_name(), "import_options")
    }
}

pub trait UserParquetDataProvider: DataProviderAdapter {
    fn import_user_parquet(&self, _request: UserParquetImportRequest) -> AdapterResult<DataBatch> {
        not_configured(self.provider_name(), "import_user_parquet")
    }
}

pub trait AlternativeDataProvider: DataProviderAdapter {
    fn fetch_alternative_data(
        &self,
        _request: AlternativeDataRequest,
    ) -> AdapterResult<AlternativeDataSet> {
        not_configured(self.provider_name(), "fetch_alternative_data")
    }
}

const INTERACTIVE_BROKERS_CAPABILITIES: &[AdapterCapability] = &[
    AdapterCapability::Brokerage,
    AdapterCapability::MarketData,
    AdapterCapability::HistoricalData,
];

const DATABENTO_CAPABILITIES: &[AdapterCapability] = &[
    AdapterCapability::BulkEquityData,
    AdapterCapability::HistoricalData,
    AdapterCapability::OptionsData,
];

const SHARADAR_CAPABILITIES: &[AdapterCapability] = &[AdapterCapability::FundamentalData];

const USER_PARQUET_CAPABILITIES: &[AdapterCapability] = &[
    AdapterCapability::HistoricalData,
    AdapterCapability::UserParquetImport,
];

const FUTURE_STUB_CAPABILITIES: &[AdapterCapability] = &[AdapterCapability::AlternativeData];

#[derive(Debug, Default, Clone, Copy)]
pub struct AdapterRegistry;

impl AdapterRegistry {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::BrokerAndDataProviderAdapters
    }
}

#[derive(Debug, Default, Clone, Copy)]
pub struct InteractiveBrokersAdapter;

impl AdapterBoundary for InteractiveBrokersAdapter {
    fn provider_name(&self) -> &'static str {
        "interactive_brokers"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        INTERACTIVE_BROKERS_CAPABILITIES
    }

    fn version(&self) -> AdapterVersion {
        AdapterVersion {
            adapter_version: INTERACTIVE_BROKERS_ADAPTER_VERSION,
            protocol_version: INTERACTIVE_BROKERS_TWS_API_VERSION,
            protocol_label: INTERACTIVE_BROKERS_PROTOCOL_LABEL,
        }
    }
}

impl BrokerageAdapter for InteractiveBrokersAdapter {}
impl MarketDataAdapter for InteractiveBrokersAdapter {}
impl HistoricalDataAdapter for InteractiveBrokersAdapter {}

#[derive(Debug, Default, Clone, Copy)]
pub struct DatabentoAdapter;

impl AdapterBoundary for DatabentoAdapter {
    fn provider_name(&self) -> &'static str {
        "databento"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        DATABENTO_CAPABILITIES
    }
}

impl DataProviderAdapter for DatabentoAdapter {}
impl BulkEquityDataProvider for DatabentoAdapter {}
impl OptionsDataProvider for DatabentoAdapter {}
impl HistoricalDataAdapter for DatabentoAdapter {}

#[derive(Debug, Default, Clone, Copy)]
pub struct SharadarAdapter;

impl AdapterBoundary for SharadarAdapter {
    fn provider_name(&self) -> &'static str {
        "sharadar"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        SHARADAR_CAPABILITIES
    }
}

impl DataProviderAdapter for SharadarAdapter {}
impl FundamentalDataProvider for SharadarAdapter {}

#[derive(Debug, Default, Clone, Copy)]
pub struct UserParquetAdapter;

impl AdapterBoundary for UserParquetAdapter {
    fn provider_name(&self) -> &'static str {
        "user_parquet"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        USER_PARQUET_CAPABILITIES
    }
}

impl DataProviderAdapter for UserParquetAdapter {}
impl UserParquetDataProvider for UserParquetAdapter {}
impl HistoricalDataAdapter for UserParquetAdapter {}

#[derive(Debug, Default, Clone, Copy)]
pub struct FutureStubProvider;

impl AdapterBoundary for FutureStubProvider {
    fn provider_name(&self) -> &'static str {
        "future_stub"
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        FUTURE_STUB_CAPABILITIES
    }
}

impl DataProviderAdapter for FutureStubProvider {}
impl AlternativeDataProvider for FutureStubProvider {}

#[cfg(test)]
mod tests {
    use super::*;

    fn brokerage_name<T: BrokerageAdapter>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    fn bulk_provider_name<T: BulkEquityDataProvider>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    fn fundamental_provider_name<T: FundamentalDataProvider>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    fn parquet_provider_name<T: UserParquetDataProvider>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    fn alternative_provider_name<T: AlternativeDataProvider>(adapter: &T) -> &'static str {
        adapter.provider_name()
    }

    #[test]
    fn identifies_adapter_runtime_boundary() {
        let registry = AdapterRegistry;
        assert_eq!(
            registry.service(),
            RuntimeService::BrokerAndDataProviderAdapters
        );
    }

    #[test]
    fn phase_one_stubs_compile_as_public_adapter_implementations() {
        assert_eq!(
            brokerage_name(&InteractiveBrokersAdapter),
            "interactive_brokers"
        );
        assert_eq!(bulk_provider_name(&DatabentoAdapter), "databento");
        assert_eq!(fundamental_provider_name(&SharadarAdapter), "sharadar");
        assert_eq!(parquet_provider_name(&UserParquetAdapter), "user_parquet");
        assert_eq!(
            alternative_provider_name(&FutureStubProvider),
            "future_stub"
        );
    }

    #[test]
    fn stub_operations_return_not_configured() {
        let adapter = InteractiveBrokersAdapter;
        let request = OrderSubmission {
            strategy_id: StrategyId::new("live-1"),
            symbol: "AAPL".to_string(),
            quantity: 10,
        };
        assert_eq!(
            adapter.submit_order(request).unwrap_err(),
            AdapterError::NotConfigured {
                adapter: "interactive_brokers",
                capability: "submit_order",
            }
        );

        let data_provider = DatabentoAdapter;
        assert_eq!(
            data_provider
                .download_full_universe_daily(UniverseDownloadRequest {
                    dataset: "equities-daily".to_string(),
                })
                .unwrap_err(),
            AdapterError::NotConfigured {
                adapter: "databento",
                capability: "download_full_universe_daily",
            }
        );
    }

    #[test]
    fn brokerage_adapter_exposes_required_surface() {
        let adapter = InteractiveBrokersAdapter;

        assert!(matches!(
            adapter.cancel_order("order-1"),
            Err(AdapterError::NotConfigured {
                capability: "cancel_order",
                ..
            })
        ));
        assert!(matches!(
            adapter.account_status(),
            Err(AdapterError::NotConfigured {
                capability: "account_status",
                ..
            })
        ));
        assert!(matches!(
            adapter.positions(),
            Err(AdapterError::NotConfigured {
                capability: "positions",
                ..
            })
        ));
        assert!(matches!(
            adapter.subscribe_market_data(MarketDataSubscription {
                symbol: "AAPL".to_string(),
                channel: MarketDataChannel::Quotes,
            }),
            Err(AdapterError::NotConfigured {
                capability: "subscribe_market_data",
                ..
            })
        ));
        assert!(matches!(
            adapter.historical_data(HistoricalDataRequest {
                symbol: "AAPL".to_string(),
                start: "2026-01-01".to_string(),
                end: "2026-02-01".to_string(),
                resolution: "1m".to_string(),
                asset_class: AssetClass::Equity,
                normalization_mode: NormalizationMode::SplitAdjusted,
            }),
            Err(AdapterError::NotConfigured {
                capability: "historical_data",
                ..
            })
        ));
    }

    #[test]
    fn interactive_brokers_documents_tws_api_version() {
        let adapter = InteractiveBrokersAdapter;
        let version = adapter.version();
        assert_eq!(version.protocol_label, "IB TWS API");
        assert_eq!(
            version.protocol_version,
            INTERACTIVE_BROKERS_TWS_API_VERSION
        );
        assert_eq!(version.protocol_version, "10.45");
        assert!(!version.adapter_version.is_empty());
    }

    #[test]
    fn default_adapter_version_is_not_applicable() {
        let adapter = SharadarAdapter;
        let version = adapter.version();
        assert_eq!(version, ADAPTER_VERSION_NOT_APPLICABLE);
        assert_eq!(version.protocol_label, "not-applicable");
    }

    #[test]
    fn data_provider_traits_expose_required_surface() {
        // Bulk equity download / historical backfill / incremental update
        let bulk = DatabentoAdapter;
        assert!(matches!(
            bulk.download_full_universe_daily(UniverseDownloadRequest {
                dataset: "equities-daily".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "download_full_universe_daily",
                ..
            })
        ));
        assert!(matches!(
            bulk.initial_historical_backfill(BackfillRequest {
                dataset: "equities-daily".to_string(),
                years: 15,
            }),
            Err(AdapterError::NotConfigured {
                capability: "initial_historical_backfill",
                ..
            })
        ));
        assert!(matches!(
            bulk.incremental_nightly_update(IncrementalUpdateRequest {
                dataset: "equities-daily".to_string(),
                date: "2026-05-06".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "incremental_nightly_update",
                ..
            })
        ));

        // Fundamentals ingestion
        let fundamental = SharadarAdapter;
        assert!(matches!(
            fundamental.ingest_fundamentals(FundamentalsRequest {
                symbol: "AAPL".to_string(),
                statement: "income".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "ingest_fundamentals",
                ..
            })
        ));

        // Options import
        let options = DatabentoAdapter;
        assert!(matches!(
            options.import_options(OptionsImportRequest {
                underlying: "AAPL".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "import_options",
                ..
            })
        ));

        // User Parquet import
        let parquet = UserParquetAdapter;
        assert!(matches!(
            parquet.import_user_parquet(UserParquetImportRequest {
                path: "/tmp/data.parquet".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "import_user_parquet",
                ..
            })
        ));

        // Alternative data
        let alt = FutureStubProvider;
        assert!(matches!(
            alt.fetch_alternative_data(AlternativeDataRequest {
                dataset: "altdata".to_string(),
            }),
            Err(AdapterError::NotConfigured {
                capability: "fetch_alternative_data",
                ..
            })
        ));
    }

    #[test]
    fn data_providers_share_data_provider_adapter_base() {
        fn data_family<T: DataProviderAdapter>(adapter: &T) -> &'static str {
            adapter.provider_family()
        }
        assert_eq!(data_family(&DatabentoAdapter), "data-provider");
        assert_eq!(data_family(&SharadarAdapter), "data-provider");
        assert_eq!(data_family(&UserParquetAdapter), "data-provider");
        assert_eq!(data_family(&FutureStubProvider), "data-provider");
    }

    #[test]
    fn unified_historical_data_interface_routes_through_historical_adapter() {
        fn historical_name<T: HistoricalDataAdapter>(adapter: &T) -> &'static str {
            adapter.provider_name()
        }
        // SRS-DATA-007: strategies/backtests/factor jobs query through a
        // single trait without binding to a specific source provider.
        assert_eq!(historical_name(&DatabentoAdapter), "databento");
        assert_eq!(historical_name(&UserParquetAdapter), "user_parquet");
        assert_eq!(
            historical_name(&InteractiveBrokersAdapter),
            "interactive_brokers"
        );

        let request = HistoricalDataRequest {
            symbol: "AAPL".to_string(),
            start: "2026-01-01".to_string(),
            end: "2026-02-01".to_string(),
            resolution: "1d".to_string(),
            asset_class: AssetClass::Equity,
            normalization_mode: NormalizationMode::SplitAdjusted,
        };
        assert!(matches!(
            DatabentoAdapter.historical_data(request.clone()),
            Err(AdapterError::NotConfigured {
                capability: "historical_data",
                ..
            })
        ));
        assert!(matches!(
            UserParquetAdapter.historical_data(request),
            Err(AdapterError::NotConfigured {
                capability: "historical_data",
                ..
            })
        ));
    }

    #[test]
    fn unified_historical_query_carries_asset_class_and_normalization() {
        // API-7: every (asset_class, normalization_mode) pair must round-trip
        // through the unified request shape so strategies, backtests, factor
        // jobs, and research notebooks can express the SRS-DATA-007 +
        // SRS-DATA-012 query knobs without leaking vendor specifics.
        let asset_classes = [
            AssetClass::Equity,
            AssetClass::Option,
            AssetClass::Future,
            AssetClass::Etf,
            AssetClass::Index,
        ];
        let modes = [
            NormalizationMode::Raw,
            NormalizationMode::SplitAdjusted,
            NormalizationMode::FullyAdjusted,
            NormalizationMode::TotalReturn,
        ];
        for asset_class in asset_classes {
            for normalization_mode in modes {
                let request = HistoricalDataRequest {
                    symbol: "AAPL".to_string(),
                    start: "2026-01-01".to_string(),
                    end: "2026-02-01".to_string(),
                    resolution: "1d".to_string(),
                    asset_class,
                    normalization_mode,
                };
                assert_eq!(request.asset_class, asset_class);
                assert_eq!(request.normalization_mode, normalization_mode);
                assert!(matches!(
                    DatabentoAdapter.historical_data(request),
                    Err(AdapterError::NotConfigured {
                        capability: "historical_data",
                        ..
                    })
                ));
            }
        }
    }

    #[test]
    fn historical_query_result_envelope_is_source_neutral() {
        // The envelope must carry symbol + asset_class + normalization_mode
        // + bars and nothing that exposes a vendor source. Constructing the
        // struct exhaustively would not compile if any forbidden field were
        // added; the explicit assertion below guards the field set.
        let result = HistoricalQueryResult {
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Equity,
            normalization_mode: NormalizationMode::FullyAdjusted,
            bars: vec![HistoricalBar {
                symbol: "AAPL".to_string(),
                close: 100.0,
            }],
        };
        assert_eq!(result.symbol, "AAPL");
        assert_eq!(result.asset_class, AssetClass::Equity);
        assert_eq!(result.normalization_mode, NormalizationMode::FullyAdjusted);
        assert_eq!(result.bars.len(), 1);
        // The exhaustive destructure proves there are no other public fields.
        let HistoricalQueryResult {
            symbol: _,
            asset_class: _,
            normalization_mode: _,
            bars: _,
        } = result;
    }

    #[test]
    fn unified_historical_data_interface_supports_phase1_normalizations() {
        // SRS-DATA-012 requires raw, split-adjusted, fully adjusted, and
        // total-return normalization modes per security subscription.
        fn run_through<T: HistoricalDataAdapter>(
            adapter: &T,
            mode: NormalizationMode,
        ) -> AdapterResult<HistoricalQueryResult> {
            adapter.historical_data(HistoricalDataRequest {
                symbol: "AAPL".to_string(),
                start: "2026-01-01".to_string(),
                end: "2026-02-01".to_string(),
                resolution: "1d".to_string(),
                asset_class: AssetClass::Equity,
                normalization_mode: mode,
            })
        }
        for mode in [
            NormalizationMode::Raw,
            NormalizationMode::SplitAdjusted,
            NormalizationMode::FullyAdjusted,
            NormalizationMode::TotalReturn,
        ] {
            assert!(matches!(
                run_through(&DatabentoAdapter, mode),
                Err(AdapterError::NotConfigured {
                    capability: "historical_data",
                    ..
                })
            ));
            assert!(matches!(
                run_through(&UserParquetAdapter, mode),
                Err(AdapterError::NotConfigured {
                    capability: "historical_data",
                    ..
                })
            ));
            assert!(matches!(
                run_through(&InteractiveBrokersAdapter, mode),
                Err(AdapterError::NotConfigured {
                    capability: "historical_data",
                    ..
                })
            ));
        }
    }
}
