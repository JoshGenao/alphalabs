//! SRS-EXE-004 — the **paper-mode** half of "a four-leg options order is
//! simulated as one composite order" (SyRS SYS-4 / SYS-82).
//!
//! The internal simulation engine already routes a multi-leg options order as ONE
//! composite (`PaperOrderRequest::MultiLeg`, built + green under SRS-SIM-001).
//! This test pins the AC's specific **four-leg** case on the paper path: an
//! iron-condor-shaped four-option-leg order is accepted and routed as one
//! composite (`is_composite() == true`) carrying exactly its four legs, and — by
//! the single-variant [`OrderRouting`] type — reaches no brokerage. The live IB
//! composite half is `crates/atp-adapters/tests/srs_exe_004_composite_order.rs`;
//! both share the SYS-4 "one composite transaction" semantics.

use atp_simulation::paper_order::{
    AssetClass, OrderError, OrderLeg, OrderRouting, OrderType, PaperOrderRequest, Side,
};
use atp_simulation::sim::PaperSimulationEngine;

fn option_leg(symbol: &str, side: Side, order_type: OrderType) -> OrderLeg {
    OrderLeg {
        symbol: symbol.to_string(),
        asset_class: AssetClass::Option,
        side,
        quantity: 1,
        order_type,
    }
}

/// A four-leg iron condor on one underlying — the AC's "four-leg options order".
fn four_option_legs() -> Vec<OrderLeg> {
    vec![
        option_leg("SPY   240621P00480000", Side::Buy, OrderType::Market),
        option_leg("SPY   240621P00490000", Side::Sell, OrderType::Market),
        option_leg("SPY   240621C00520000", Side::Sell, OrderType::Market),
        option_leg("SPY   240621C00530000", Side::Buy, OrderType::Market),
    ]
}

#[test]
fn srs_exe_004_four_leg_options_order_simulates_as_one_composite() {
    let engine = PaperSimulationEngine::new();
    let routing = engine
        .accept_order(&PaperOrderRequest::MultiLeg {
            legs: four_option_legs(),
        })
        .expect("a well-formed four-leg options composite is accepted");

    assert!(
        routing.is_composite(),
        "a four-leg options order must simulate as ONE composite transaction (SYS-4)"
    );
    assert_eq!(
        routing.legs().len(),
        4,
        "the composite carries exactly its four atomic legs"
    );
    // The single-variant OrderRouting is the type-level proof that no leg of the
    // composite reaches a brokerage (no IB API order call).
    let OrderRouting::InternalSimulation { composite, .. } = routing;
    assert!(composite);
}

#[test]
fn srs_exe_004_four_leg_composite_with_a_non_option_leg_fails_closed() {
    // SYS-4 composites are options-only: swapping any leg for an equity leg fails
    // the whole composite closed before routing — no partial spread simulates.
    let engine = PaperSimulationEngine::new();
    let mut legs = four_option_legs();
    legs[2] = OrderLeg {
        symbol: "SPY".to_string(),
        asset_class: AssetClass::Equity,
        side: Side::Sell,
        quantity: 1,
        order_type: OrderType::Market,
    };
    assert_eq!(
        engine.accept_order(&PaperOrderRequest::MultiLeg { legs }),
        Err(OrderError::NonOptionCompositeLeg)
    );
}
