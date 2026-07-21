//! SRS-EXE-002 — route all non-live strategy orders to the internal simulation
//! engine; paper strategy orders never create IB orders (SyRS SYS-2b / SYS-2e,
//! AC-10; StRS SN-1.06 / SN-1.29 / C-11).
//!
//! L7 domain (safety) acceptance for the ORCHESTRATOR WIRING — the real
//! components behind the routing authority the execution-crate test
//! (`srs_exe_002_order_routing.rs`) proves with panic-on-touch stubs:
//!
//! * the paper side is the REAL SRS-SIM-001 `PaperSimulationEngine` (whose
//!   `OrderRouting` return type has structurally no broker variant) with the
//!   SRS-DATA-021 `VirtualOrderBook` as the single order store;
//! * the live side is the REAL SRS-EXE-006 `InteractiveBrokersBrokerage`
//!   adapter over the deterministic mocked-IB recording transport, so "an IB
//!   order was created" is observable as a wire-level count;
//! * every submission enters through the REAL `ExecutionEngine::dispatch_order`
//!   (the operator verification entry the `exe002_order_routing_cli` bin
//!   drives; the deployed strategy-runtime order path stays deferred to the
//!   SRS-SDK strategy host / SRS-ORCH-* runtime).
//!
//! The IB paper account is NOT touched here: the recording gateway is an
//! in-process double, and the only paper-account surface in the repo stays the
//! operator-initiated SRS-EXE-006 integration test (`ATP_RUN_INTEGRATION=1` +
//! `--ignored`, port 4002) — the SYS-2e boundary.

use atp_execution::{ExecutionEngine, InternalSimulationSubmit, OrderRoutingReceipt};
use atp_orchestrator::order_routing_wiring::{
    run_routing_scenario, RoutingScenario, WiredPaperSimulation, MAX_SCENARIO_PAPER_ORDERS,
    SCENARIO_LIVE_STRATEGY,
};
use atp_types::{
    AssetClass, OrderErrorCategory, OrderSide, OrderSubmission, OrderType, StrategyId,
};

/// A paper order dispatched through the real wiring is accepted by the real
/// simulation engine (resting in the single order store) and creates ZERO
/// order-creating IB wire operations — AC-10 over real components.
#[test]
fn srs_exe_002_paper_order_routes_to_real_simulation_engine_with_zero_ib_wire_ops() {
    let evidence = run_routing_scenario(&RoutingScenario {
        paper_orders: 1,
        designate_live: false,
    })
    .expect("paper-only scenario routes");

    assert_eq!(
        evidence.ib_orders_created, 0,
        "a paper order created an IB order"
    );
    assert_eq!(evidence.simulated_orders_accepted, 1);
    assert_eq!(
        evidence.resting_orders, 1,
        "the accepted order must rest in the book"
    );
    assert_eq!(evidence.orders.len(), 1);
    assert_eq!(evidence.orders[0].route, "internal_simulation");
    assert!(
        evidence.orders[0].receipt.starts_with("paper-"),
        "simulation receipt must be a book id, got {}",
        evidence.orders[0].receipt
    );
    assert_eq!(evidence.designated, None);
}

/// The AC-10 acceptance sweep over REAL components: one designated live
/// strategy among thirty paper strategies — exactly ONE order-creating IB wire
/// operation, all thirty paper orders accepted by the real simulation engine.
#[test]
fn srs_exe_002_one_live_among_thirty_paper_creates_exactly_one_ib_order() {
    let evidence = run_routing_scenario(&RoutingScenario {
        paper_orders: 30,
        designate_live: true,
    })
    .expect("mixed scenario routes");

    assert_eq!(
        evidence.ib_orders_created, 1,
        "exactly the designated live strategy's order may create an IB order"
    );
    assert_eq!(evidence.simulated_orders_accepted, 30);
    assert_eq!(evidence.resting_orders, 30);
    assert_eq!(evidence.designated.as_deref(), Some(SCENARIO_LIVE_STRATEGY));
    assert_eq!(evidence.orders.len(), 31);
    let live_rows: Vec<_> = evidence
        .orders
        .iter()
        .filter(|order| order.route == "live_brokerage")
        .collect();
    assert_eq!(live_rows.len(), 1, "exactly one order routes live");
    assert_eq!(live_rows[0].strategy, SCENARIO_LIVE_STRATEGY);
    assert!(
        live_rows[0].receipt.starts_with("IB-"),
        "the live order's receipt must carry the broker order id, got {}",
        live_rows[0].receipt
    );
    assert!(
        evidence
            .orders
            .iter()
            .filter(|order| order.strategy != SCENARIO_LIVE_STRATEGY)
            .all(|order| order.route == "internal_simulation"),
        "every non-live strategy must route to the internal simulation engine"
    );
}

/// The `OrderSubmission` → `OrderLeg` mapping is field-for-field faithful for
/// every order type: what the strategy submitted is exactly what rests in the
/// simulation engine's book (the live/paper source-neutral invariant).
#[test]
fn srs_exe_002_envelope_maps_to_order_leg_field_for_field() {
    let simulation = WiredPaperSimulation::new();
    let order_types = [
        OrderType::Market,
        OrderType::Limit {
            limit_price_minor: 12345,
        },
        OrderType::Stop {
            stop_price_minor: 9876,
        },
        OrderType::StopLimit {
            stop_price_minor: 9876,
            limit_price_minor: 9700,
        },
    ];

    for (index, order_type) in order_types.into_iter().enumerate() {
        let submission = OrderSubmission::new(
            StrategyId::new(format!("paper-map-{index}")),
            format!("MAP{index}"),
            7 + index as i64,
            AssetClass::Equity,
            OrderSide::Sell,
            order_type,
        );
        let receipt = simulation
            .submit_simulated(submission.clone())
            .expect("valid submission is accepted");
        assert!(receipt.sim_order_id.starts_with("paper-"));

        simulation.with_book(|book| {
            let resting = book
                .orders()
                .iter()
                .find(|order| order.strategy().as_str() == submission.strategy_id.as_str())
                .expect("accepted order rests in the book");
            let leg = resting.leg();
            assert_eq!(leg.symbol, submission.symbol);
            assert_eq!(leg.asset_class, submission.asset_class);
            assert_eq!(leg.side, submission.side);
            assert_eq!(leg.quantity, submission.quantity);
            assert_eq!(leg.order_type, submission.order_type);
        });
    }
}

/// Defense-in-depth on the port itself: a malformed submission handed straight
/// to the wired simulation port (bypassing `dispatch_order`'s shared-entry
/// validation) is rejected by the engine's own fail-closed intake, maps onto
/// the structured SRS-ERR-001 envelope, and rests NOTHING in the book.
#[test]
fn srs_exe_002_port_side_rejection_maps_to_structured_error_and_rests_nothing() {
    let simulation = WiredPaperSimulation::new();
    let submission = OrderSubmission::new(
        StrategyId::new("paper-bad"),
        "BAD",
        -5,
        AssetClass::Equity,
        OrderSide::Buy,
        OrderType::Market,
    );

    let err = simulation
        .submit_simulated(submission)
        .expect_err("a non-positive quantity must fail closed");
    // SRS-ERR-001: a non-positive quantity is an invalid-order-parameters
    // rejection, not an invalid symbol.
    assert_eq!(err.category, OrderErrorCategory::OrderParametersInvalid);
    assert_eq!(err.error_type, "NonPositiveQuantity");
    assert_eq!(
        simulation.open_resting_orders(),
        0,
        "a rejected order must rest nothing"
    );
}

/// A malformed submission dispatched through the shared entry is rejected
/// BEFORE either port: no IB wire operation, nothing resting in the book.
#[test]
fn srs_exe_002_malformed_dispatch_fails_closed_before_both_ports() {
    use atp_orchestrator::order_routing_wiring::{
        CollectingConnectivitySink, CollectingStaleDataSink, FreshMarketDataFixture,
        HealthyConnectivityFixture, IbBrokerageBridge, RecordingIbGateway,
    };

    let engine = ExecutionEngine::default();
    let simulation = WiredPaperSimulation::new();
    let brokerage = IbBrokerageBridge::new(RecordingIbGateway::new());
    let submission = OrderSubmission::new(
        StrategyId::new("paper-blank"),
        "   ",
        10,
        AssetClass::Equity,
        OrderSide::Buy,
        OrderType::Market,
    );

    let err = engine
        .dispatch_order(
            submission,
            &brokerage,
            &HealthyConnectivityFixture,
            &CollectingConnectivitySink::default(),
            &FreshMarketDataFixture,
            &CollectingStaleDataSink::default(),
            &simulation,
        )
        .expect_err("a blank symbol must fail closed at the shared entry");
    // SRS-ERR-001: a blank symbol is a malformed order parameter — the broker was
    // never asked, so INVALID_SYMBOL (which means "the broker says this symbol
    // does not exist") would be a fabricated claim.
    assert_eq!(err.category, OrderErrorCategory::OrderParametersInvalid);
    assert_eq!(brokerage.gateway().orders_created(), 0);
    assert_eq!(simulation.open_resting_orders(), 0);
}

/// Fail-closed scenario bounds: zero paper orders and a degenerate oversized
/// sweep are both refused before any dispatch.
#[test]
fn srs_exe_002_scenario_bounds_fail_closed() {
    assert!(run_routing_scenario(&RoutingScenario {
        paper_orders: 0,
        designate_live: false,
    })
    .is_err());
    assert!(run_routing_scenario(&RoutingScenario {
        paper_orders: MAX_SCENARIO_PAPER_ORDERS + 1,
        designate_live: true,
    })
    .is_err());
}

/// The dispatched receipt for a live order carries the broker order id minted
/// by the wire transport through the REAL adapter — the live leg of the
/// wiring is the same SRS-EXE-006 adapter path the operator-gated
/// paper-account test proves against the real gateway.
#[test]
fn srs_exe_002_live_leg_routes_through_the_real_adapter() {
    use atp_execution::LiveDesignationConfirmation;
    use atp_orchestrator::order_routing_wiring::{
        CollectingConnectivitySink, CollectingStaleDataSink, FreshMarketDataFixture,
        HealthyConnectivityFixture, IbBrokerageBridge, RecordingIbGateway,
    };

    let mut engine = ExecutionEngine::default();
    let live = StrategyId::new("live-alpha");
    engine
        .designate(
            live.clone(),
            LiveDesignationConfirmation::from_operator(live.clone(), "operator confirms")
                .expect("confirmation"),
        )
        .expect("designate");

    let simulation = WiredPaperSimulation::new();
    let brokerage = IbBrokerageBridge::new(RecordingIbGateway::new());
    let receipt = engine
        .dispatch_order(
            OrderSubmission::new(
                live,
                "LIVE001",
                10,
                AssetClass::Equity,
                OrderSide::Buy,
                OrderType::Market,
            ),
            &brokerage,
            &HealthyConnectivityFixture,
            &CollectingConnectivitySink::default(),
            &FreshMarketDataFixture,
            &CollectingStaleDataSink::default(),
            &simulation,
        )
        .expect("designated live order routes");

    match receipt {
        OrderRoutingReceipt::Live(receipt) => {
            assert_eq!(receipt.broker_order_id, "IB-1");
        }
        OrderRoutingReceipt::Simulated(receipt) => {
            panic!(
                "the designated live strategy must not route to simulation (sim order id {})",
                receipt.sim_order_id
            );
        }
    }
    assert_eq!(brokerage.gateway().orders_created(), 1);
    assert_eq!(
        brokerage.gateway().recorded_calls(),
        vec!["submit:live-alpha:LIVE001".to_string()]
    );
    assert_eq!(
        simulation.open_resting_orders(),
        0,
        "the sim port must stay untouched"
    );
}
