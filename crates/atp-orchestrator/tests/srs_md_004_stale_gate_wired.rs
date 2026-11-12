//! SRS-MD-004 contract obligation C4 — the stale-data gate through the real
//! composition, with fixture market data.
//!
//! Every part is the shipped one except the clock and the IB wire: the
//! production probe ([`BoardFreshnessProbe`]) over a real
//! [`LineFreshnessBoard`], the real `ExecutionEngine::dispatch_order`, the real
//! paper destination ([`WiredPaperSimulation`]: `PaperSimulationEngine` plus the
//! `VirtualOrderBook`), and the real IB adapter bridge over a recording
//! gateway. The clock is set by hand so the 15 s threshold is crossed without
//! sleeping; the fixture "feed" is the board's own writer API, which is what a
//! feed loop calls.
//!
//! Acceptance criterion, walked end to end: while subscribed market data is
//! stale, live and paper submissions return MARKET_DATA_STALE; submissions
//! resume when fresh data is received.

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;

use atp_execution::{
    ExecutionEngine, LiveDesignationConfirmation, MarketDataFreshnessProbe, OrderRoutingReceipt,
};
use atp_market_data::{FreshnessClock, GapTracking, LineFreshnessBoard};
use atp_orchestrator::market_data_freshness::BoardFreshnessProbe;
use atp_orchestrator::order_routing_wiring::{
    CollectingConnectivitySink, CollectingStaleDataSink, HealthyConnectivityFixture,
    IbBrokerageBridge, RecordingIbGateway, WiredPaperSimulation,
};
use atp_types::{
    AssetClass, MarketDataFreshness, OrderErrorCategory, OrderSide, OrderSubmission, OrderType,
    SecurityKey, StrategyId,
};

const SECOND_NS: i64 = 1_000_000_000;
const START_NS: i64 = 1_750_000_000 * SECOND_NS;

#[derive(Default)]
struct ManualClock(AtomicI64);

impl ManualClock {
    fn set(&self, now_ns: i64) {
        self.0.store(now_ns, Ordering::SeqCst);
    }
}

impl FreshnessClock for ManualClock {
    fn now_ns(&self) -> Option<i64> {
        Some(self.0.load(Ordering::SeqCst))
    }
}

fn order(strategy: &str, symbol: &str) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(strategy),
        symbol: symbol.to_string(),
        quantity: 10,
        asset_class: AssetClass::Equity,
        side: OrderSide::Buy,
        order_type: OrderType::Market,
    }
}

fn engine_with_live(strategy: &str) -> ExecutionEngine {
    let mut engine = ExecutionEngine::default();
    let confirmation = LiveDesignationConfirmation::from_operator(
        StrategyId::new(strategy),
        "operator confirmed live designation",
    )
    .expect("non-empty acknowledgement yields a confirmation token");
    engine
        .designate(StrategyId::new(strategy), confirmation)
        .expect("designation succeeds");
    engine
}

struct Rig {
    clock: Arc<ManualClock>,
    feed: LineFreshnessBoard,
    probe: BoardFreshnessProbe,
    engine: ExecutionEngine,
    broker: IbBrokerageBridge<RecordingIbGateway>,
    connectivity: HealthyConnectivityFixture,
    connectivity_events: CollectingConnectivitySink,
    stale_events: CollectingStaleDataSink,
    paper: WiredPaperSimulation,
}

impl Rig {
    fn new() -> Self {
        let clock = Arc::new(ManualClock::default());
        clock.set(START_NS);
        let board = LineFreshnessBoard::new(clock.clone(), GapTracking::Unavailable);
        board
            .watch(SecurityKey::new("AAPL", AssetClass::Equity).unwrap())
            .expect("a healthy board accepts the watch");
        Self {
            clock,
            // The feed side and the order side hold separate clones of ONE board.
            feed: board.clone(),
            probe: BoardFreshnessProbe::new(board),
            engine: engine_with_live("live-alpha"),
            broker: IbBrokerageBridge::new(RecordingIbGateway::new()),
            connectivity: HealthyConnectivityFixture,
            connectivity_events: CollectingConnectivitySink::default(),
            stale_events: CollectingStaleDataSink::default(),
            paper: WiredPaperSimulation::new(),
        }
    }

    fn tick(&self, symbol: &str) {
        self.feed
            .observe_security(SecurityKey::new(symbol, AssetClass::Equity).unwrap())
            .expect("a healthy untracked board accepts the observation");
    }

    // Returns exactly what `dispatch_order` returns; the large error is the shared
    // SRS-ERR-001 envelope, and boxing it here would hide the real signature.
    #[allow(clippy::result_large_err)]
    fn submit(
        &self,
        submission: OrderSubmission,
    ) -> Result<OrderRoutingReceipt, atp_types::StructuredOrderError> {
        self.engine.dispatch_order(
            submission,
            &self.broker,
            &self.connectivity,
            &self.connectivity_events,
            &self.probe,
            &self.stale_events,
            &self.paper,
        )
    }

    fn ib_orders(&self) -> u32 {
        self.broker.gateway().orders_created()
    }
}

fn assert_stale_block(result: Result<OrderRoutingReceipt, atp_types::StructuredOrderError>) {
    let err = result.expect_err("a stale line must block the submission");
    assert_eq!(err.category, OrderErrorCategory::MarketDataStale);
    assert_eq!(err.category.as_str(), "MARKET_DATA_STALE");
    assert_eq!(err.error_type, "MarketDataStale");
}

#[test]
fn c4_live_and_paper_block_while_stale_and_resume_when_fresh_data_arrives() {
    let rig = Rig::new();

    // 1. Subscribed but never ticked: stale from the start. Both blocked.
    assert_stale_block(rig.submit(order("live-alpha", "AAPL")));
    assert_stale_block(rig.submit(order("paper-beta", "AAPL")));
    assert_eq!(rig.ib_orders(), 0);
    assert_eq!(rig.paper.open_resting_orders(), 0);
    assert_eq!(rig.stale_events.recorded(), 2);

    // 2. Fresh data arrives: both resume.
    rig.tick("AAPL");
    match rig.submit(order("live-alpha", "AAPL")) {
        Ok(OrderRoutingReceipt::Live(_)) => {}
        other => panic!("the live order must reach IB once fresh, got {other:?}"),
    }
    match rig.submit(order("paper-beta", "AAPL")) {
        Ok(OrderRoutingReceipt::Simulated(_)) => {}
        other => panic!("the paper order must reach the simulator once fresh, got {other:?}"),
    }
    assert_eq!(
        rig.ib_orders(),
        1,
        "exactly one IB order, from the live strategy"
    );
    assert_eq!(
        rig.paper.open_resting_orders(),
        1,
        "the paper order rests in the book"
    );

    // 3. The feed goes silent for more than 15 s: both blocked again, nothing
    //    new reaches IB or the simulator.
    rig.clock.set(START_NS + 15 * SECOND_NS + 1);
    assert_stale_block(rig.submit(order("live-alpha", "AAPL")));
    assert_stale_block(rig.submit(order("paper-beta", "AAPL")));
    assert_eq!(rig.ib_orders(), 1);
    assert_eq!(rig.paper.open_resting_orders(), 1);
    assert_eq!(
        rig.probe.staleness_seconds("AAPL", AssetClass::Equity),
        Some(15),
        "the probe reports the observed age"
    );
    assert_eq!(rig.stale_events.recorded(), 4);

    // 4. A tick newer than the threshold resumes both.
    rig.tick("AAPL");
    assert!(matches!(
        rig.submit(order("live-alpha", "AAPL")),
        Ok(OrderRoutingReceipt::Live(_))
    ));
    assert!(matches!(
        rig.submit(order("paper-beta", "AAPL")),
        Ok(OrderRoutingReceipt::Simulated(_))
    ));
    assert_eq!(rig.ib_orders(), 2);
    assert_eq!(rig.paper.open_resting_orders(), 2);
    assert_eq!(
        rig.connectivity_events.recorded(),
        0,
        "staleness is not a connectivity fault"
    );
}

#[test]
fn c4_one_lines_fresh_data_does_not_admit_orders_on_another_line() {
    let rig = Rig::new();
    rig.tick("AAPL");
    // MSFT was never subscribed or observed.
    assert_stale_block(rig.submit(order("live-alpha", "MSFT")));
    assert_stale_block(rig.submit(order("paper-beta", "MSFT")));
    assert_eq!(rig.ib_orders(), 0);
    assert_eq!(rig.paper.open_resting_orders(), 0);
    assert_eq!(
        rig.probe.freshness("MSFT", AssetClass::Equity),
        MarketDataFreshness::Stale
    );
    assert_eq!(
        rig.probe.staleness_seconds("MSFT", AssetClass::Equity),
        None
    );
}
