//! Integration tests for **SRS-SIM-001** — "simulate paper strategy orders
//! locally without routing to any brokerage" (SyRS SYS-82 / SYS-3 / SYS-4).
//!
//! Acceptance criterion: *market, limit, stop, stop-limit, equity, option, and
//! multi-leg orders are processed by the simulation engine and create no IB API
//! order calls.* These tests drive [`PaperSimulationEngine::accept_order`]
//! through the public crate surface and assert that every such order routes to
//! the internal simulation engine — the [`OrderRouting`] type has no brokerage
//! variant, so "no IB API order calls" is verified at the type level.

use atp_simulation::paper_order::{
    AssetClass, OrderError, OrderLeg, OrderRouting, OrderType, PaperOrderRequest, Side,
};
use atp_simulation::sim::PaperSimulationEngine;

fn leg(asset_class: AssetClass, side: Side, order_type: OrderType) -> OrderLeg {
    OrderLeg {
        symbol: "AAPL".to_string(),
        asset_class,
        side,
        quantity: 10,
        order_type,
    }
}

/// The "no IB API order calls" invariant: an accepted paper order routes to the
/// internal simulation engine and nowhere else. Because [`OrderRouting`] has a
/// single variant, this exhaustive match is the type-level proof that no broker
/// route exists.
fn assert_routes_internally(routing: &OrderRouting) {
    match routing {
        OrderRouting::InternalSimulation { legs, .. } => {
            assert!(
                !legs.is_empty(),
                "a routed order must carry at least one leg"
            );
        }
    }
}

const ALL_ORDER_TYPES: [OrderType; 4] = [
    OrderType::Market,
    OrderType::Limit {
        limit_price_minor: 9_400,
    },
    OrderType::Stop {
        stop_price_minor: 9_500,
    },
    OrderType::StopLimit {
        stop_price_minor: 9_500,
        limit_price_minor: 9_400,
    },
];

#[test]
fn every_order_type_routes_to_the_internal_simulation_engine() {
    let engine = PaperSimulationEngine::new();
    for order_type in ALL_ORDER_TYPES {
        let routing = engine
            .accept_order(&PaperOrderRequest::Single(leg(
                AssetClass::Equity,
                Side::Buy,
                order_type,
            )))
            .expect("order accepted");
        assert_routes_internally(&routing);
        assert!(!routing.is_composite());
    }
}

#[test]
fn both_asset_classes_route_to_the_internal_simulation_engine() {
    let engine = PaperSimulationEngine::new();
    for asset_class in [AssetClass::Equity, AssetClass::Option] {
        let routing = engine
            .accept_order(&PaperOrderRequest::Single(leg(
                asset_class,
                Side::Sell,
                OrderType::Market,
            )))
            .expect("order accepted");
        assert_routes_internally(&routing);
        assert_eq!(routing.legs()[0].asset_class, asset_class);
    }
}

#[test]
fn multi_leg_option_order_routes_as_one_composite_transaction() {
    // A two-leg vertical option spread (SYS-4): one composite transaction whose
    // legs fill atomically, routed to the internal simulation engine.
    let engine = PaperSimulationEngine::new();
    let routing = engine
        .accept_order(&PaperOrderRequest::MultiLeg {
            legs: vec![
                leg(AssetClass::Option, Side::Buy, OrderType::Market),
                leg(
                    AssetClass::Option,
                    Side::Sell,
                    OrderType::Limit {
                        limit_price_minor: 250,
                    },
                ),
            ],
        })
        .expect("composite order accepted");
    assert_routes_internally(&routing);
    assert!(
        routing.is_composite(),
        "a multi-leg order must route as one composite transaction (SYS-4)"
    );
    assert_eq!(routing.legs().len(), 2);
}

#[test]
fn no_accepted_order_can_reach_a_broker() {
    // Sweep every accepted order shape and assert each routes through the
    // internal simulation engine. There is no OrderRouting variant other than
    // InternalSimulation, so an accepted paper order can never produce an IB API
    // order call (SRS-SIM-001).
    let engine = PaperSimulationEngine::new();
    // Single orders: every asset class, side, order type.
    for asset_class in [AssetClass::Equity, AssetClass::Option] {
        for side in [Side::Buy, Side::Sell] {
            for order_type in ALL_ORDER_TYPES {
                let single = engine
                    .accept_order(&PaperOrderRequest::Single(leg(
                        asset_class,
                        side,
                        order_type,
                    )))
                    .expect("single accepted");
                assert_routes_internally(&single);
            }
        }
    }
    // Multi-leg OPTION composites (SYS-4): two option legs, every side/type pair.
    for side in [Side::Buy, Side::Sell] {
        for order_type in ALL_ORDER_TYPES {
            let multi = engine
                .accept_order(&PaperOrderRequest::MultiLeg {
                    legs: vec![
                        leg(AssetClass::Option, side, order_type),
                        leg(AssetClass::Option, side, order_type),
                    ],
                })
                .expect("composite accepted");
            assert_routes_internally(&multi);
            assert!(multi.is_composite());
        }
    }
}

#[test]
fn intake_fails_closed_on_bad_input() {
    let engine = PaperSimulationEngine::new();

    let mut empty_symbol = leg(AssetClass::Equity, Side::Buy, OrderType::Market);
    empty_symbol.symbol = "  ".to_string();
    assert_eq!(
        engine.accept_order(&PaperOrderRequest::Single(empty_symbol)),
        Err(OrderError::EmptySymbol)
    );

    let mut zero_qty = leg(AssetClass::Equity, Side::Buy, OrderType::Market);
    zero_qty.quantity = 0;
    assert_eq!(
        engine.accept_order(&PaperOrderRequest::Single(zero_qty)),
        Err(OrderError::NonPositiveQuantity { quantity: 0 })
    );

    assert_eq!(
        engine.accept_order(&PaperOrderRequest::Single(leg(
            AssetClass::Equity,
            Side::Buy,
            OrderType::Limit {
                limit_price_minor: 0
            },
        ))),
        Err(OrderError::NonPositiveLimitPrice { price_minor: 0 })
    );

    assert_eq!(
        engine.accept_order(&PaperOrderRequest::Single(leg(
            AssetClass::Equity,
            Side::Buy,
            OrderType::Stop {
                stop_price_minor: -1
            },
        ))),
        Err(OrderError::NonPositiveStopPrice { price_minor: -1 })
    );

    assert_eq!(
        engine.accept_order(&PaperOrderRequest::MultiLeg { legs: vec![] }),
        Err(OrderError::EmptyMultiLeg)
    );

    // A single bad leg fails the whole composite (no partial routing).
    let mut bad = leg(AssetClass::Option, Side::Sell, OrderType::Market);
    bad.quantity = -5;
    assert_eq!(
        engine.accept_order(&PaperOrderRequest::MultiLeg {
            legs: vec![leg(AssetClass::Option, Side::Buy, OrderType::Market), bad],
        }),
        Err(OrderError::NonPositiveQuantity { quantity: -5 })
    );

    // A one-leg composite is not a SYS-4 multi-leg order.
    assert_eq!(
        engine.accept_order(&PaperOrderRequest::MultiLeg {
            legs: vec![leg(AssetClass::Option, Side::Buy, OrderType::Market)],
        }),
        Err(OrderError::SingleLegComposite)
    );

    // Composites are options-only: an equity-only composite fails closed.
    assert_eq!(
        engine.accept_order(&PaperOrderRequest::MultiLeg {
            legs: vec![
                leg(AssetClass::Equity, Side::Buy, OrderType::Market),
                leg(AssetClass::Equity, Side::Sell, OrderType::Market),
            ],
        }),
        Err(OrderError::NonOptionCompositeLeg)
    );

    // ...and a mixed option/equity composite fails closed too.
    assert_eq!(
        engine.accept_order(&PaperOrderRequest::MultiLeg {
            legs: vec![
                leg(AssetClass::Option, Side::Buy, OrderType::Market),
                leg(AssetClass::Equity, Side::Sell, OrderType::Market),
            ],
        }),
        Err(OrderError::NonOptionCompositeLeg)
    );
}

#[test]
fn intake_is_deterministic_for_identical_requests() {
    let engine = PaperSimulationEngine::new();
    let request = PaperOrderRequest::Single(leg(
        AssetClass::Equity,
        Side::Buy,
        OrderType::StopLimit {
            stop_price_minor: 9_500,
            limit_price_minor: 9_400,
        },
    ));
    assert_eq!(engine.accept_order(&request), engine.accept_order(&request));
}
