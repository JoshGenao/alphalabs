//! Integration tests for **SRS-SIM-002** — "simulate fills using live market
//! data and configurable fill models" (SyRS SYS-83 fill simulation, SYS-87
//! realism constraints).
//!
//! Acceptance criterion: *market, limit, stop, and stop-limit simulated fills
//! follow SYS-83 defaults and per-strategy configuration; fill volume
//! constraints are enforced.* These tests drive
//! [`PaperSimulationEngine::evaluate_fill`] through the public crate surface and
//! assert that every order type triggers/fills correctly over a
//! [`MarketSnapshot`], that the SYS-87b volume cap is enforced, that a filled
//! decision flows through the SAME shared cost family the backtest engine uses
//! (so a simulated fill creates no IB API order call — the fill path stays inside
//! the internal simulation engine), and that corrupt market data fails closed.

use atp_simulation::fill_model::{
    BarVolumeBudget, FillDecision, FillModelConfig, FillModelError, LimitFillModel, MarketSnapshot,
    NoFillReason,
};
use atp_simulation::paper_order::{OrderType, Side};
use atp_simulation::sim::PaperSimulationEngine;

fn snapshot(bid: i64, ask: i64, last: i64, volume: i64) -> MarketSnapshot {
    MarketSnapshot {
        bid_minor: bid,
        ask_minor: ask,
        last_minor: last,
        bar_volume: volume,
    }
}

// Every order type with its trigger/limit placed at 10_000, so each crosses on a
// snapshot whose buy touch (ask) or sell touch (bid) sits exactly at 10_000.
const ALL_ORDER_TYPES: [OrderType; 4] = [
    OrderType::Market,
    OrderType::Limit {
        limit_price_minor: 10_000,
    },
    OrderType::Stop {
        stop_price_minor: 10_000,
    },
    OrderType::StopLimit {
        stop_price_minor: 10_000,
        limit_price_minor: 10_000,
    },
];

#[test]
fn every_order_type_resolves_a_decision() {
    // A liquid snapshot where every order type triggers/crosses on both sides:
    // ask at the limit (10_000) for a buy, bid at the limit for a sell, last at
    // the stop for both stops. Each evaluation must yield a deterministic
    // FillDecision (never an error) for well-formed market data.
    let engine = PaperSimulationEngine::new();
    let model = FillModelConfig::syrs_defaults();
    for side in [Side::Buy, Side::Sell] {
        // Place the touch exactly at the limit/stop so every type fills.
        let snap = match side {
            Side::Buy => snapshot(9_990, 10_000, 10_000, 1_000),
            Side::Sell => snapshot(10_000, 10_010, 10_000, 1_000),
        };
        for order_type in ALL_ORDER_TYPES {
            let decision = engine
                .evaluate_fill(&order_type, side, 100, &snap, &model)
                .expect("well-formed market data evaluates without error");
            assert!(
                decision.is_filled(),
                "order type {order_type:?} on side {side:?} should fill on a touching snapshot, got {decision:?}"
            );
        }
    }
}

#[test]
fn limit_and_stop_orders_hold_until_the_market_crosses() {
    let engine = PaperSimulationEngine::new();
    let model = FillModelConfig::syrs_defaults();

    // A buy limit at 10_000 with the ask at 10_010 has not crossed.
    let limit = engine
        .evaluate_fill(
            &OrderType::Limit {
                limit_price_minor: 10_000,
            },
            Side::Buy,
            100,
            &snapshot(9_990, 10_010, 10_000, 1_000),
            &model,
        )
        .expect("evaluated");
    assert_eq!(
        limit,
        FillDecision::NoFill {
            reason: NoFillReason::LimitNotCrossed,
        }
    );

    // A buy stop at 10_000 with the last at 9_900 has not triggered.
    let stop = engine
        .evaluate_fill(
            &OrderType::Stop {
                stop_price_minor: 10_000,
            },
            Side::Buy,
            100,
            &snapshot(9_890, 9_910, 9_900, 1_000),
            &model,
        )
        .expect("evaluated");
    assert_eq!(
        stop,
        FillDecision::NoFill {
            reason: NoFillReason::StopNotTriggered,
        }
    );
}

#[test]
fn limit_fill_models_disagree_on_a_touch() {
    // SRS-SIM-002 requires *configurable* per-strategy fill models. Prove the
    // configuration is behavior-changing: on the SAME touch snapshot (ask exactly
    // at the limit), ImmediateOnCross fills but RequireThroughCross does not.
    let engine = PaperSimulationEngine::new();
    let order = OrderType::Limit {
        limit_price_minor: 10_000,
    };
    let touch = snapshot(9_990, 10_000, 9_995, 1_000);

    let immediate = engine
        .evaluate_fill(
            &order,
            Side::Buy,
            100,
            &touch,
            &FillModelConfig {
                limit_fill: LimitFillModel::ImmediateOnCross,
            },
        )
        .expect("evaluated");
    let through = engine
        .evaluate_fill(
            &order,
            Side::Buy,
            100,
            &touch,
            &FillModelConfig {
                limit_fill: LimitFillModel::RequireThroughCross,
            },
        )
        .expect("evaluated");

    assert_eq!(
        immediate,
        FillDecision::Filled {
            fill_price_minor: 10_000,
            fill_quantity: 100,
        }
    );
    assert_eq!(
        through,
        FillDecision::NoFill {
            reason: NoFillReason::LimitNotCrossed,
        }
    );
    assert_ne!(
        immediate, through,
        "the two configurable fill models must differ on a touch (SRS-SIM-002)"
    );
}

#[test]
fn volume_cap_is_enforced() {
    // SYS-87b: a simulated fill shall not exceed the observed volume for the bar.
    let engine = PaperSimulationEngine::new();
    let model = FillModelConfig::syrs_defaults();

    // Requested 1_000, only 300 traded -> partial fill of 300.
    let partial = engine
        .evaluate_fill(
            &OrderType::Market,
            Side::Buy,
            1_000,
            &snapshot(9_990, 10_010, 10_000, 300),
            &model,
        )
        .expect("evaluated");
    assert_eq!(
        partial,
        FillDecision::Filled {
            fill_price_minor: 10_010,
            fill_quantity: 300,
        }
    );

    // Requested 100, bar volume 1_000 -> exactly 100 (the cap never inflates).
    let full = engine
        .evaluate_fill(
            &OrderType::Market,
            Side::Buy,
            100,
            &snapshot(9_990, 10_010, 10_000, 1_000),
            &model,
        )
        .expect("evaluated");
    assert_eq!(
        full,
        FillDecision::Filled {
            fill_price_minor: 10_010,
            fill_quantity: 100,
        }
    );

    // Zero volume -> nothing fills even though the market order would trigger.
    let none = engine
        .evaluate_fill(
            &OrderType::Market,
            Side::Buy,
            100,
            &snapshot(9_990, 10_010, 10_000, 0),
            &model,
        )
        .expect("evaluated");
    assert_eq!(
        none,
        FillDecision::NoFill {
            reason: NoFillReason::ZeroVolume,
        }
    );
}

#[test]
fn aggregate_volume_cap_holds_across_orders() {
    // SYS-87b "for the bar period": threading ONE BarVolumeBudget through several
    // orders against the same bar caps the AGGREGATE fill at the observed volume,
    // even though each order on its own requests less than the whole bar.
    let engine = PaperSimulationEngine::new();
    let model = FillModelConfig::syrs_defaults();
    let snap = snapshot(9_990, 10_010, 10_000, 1_000);
    let mut budget = BarVolumeBudget::new(snap.bar_volume).expect("budget");

    let mut filled_total = 0;
    for requested in [700, 700, 100] {
        let decision = engine
            .evaluate_fill_against_budget(
                &OrderType::Market,
                Side::Buy,
                requested,
                &snap,
                &model,
                &mut budget,
            )
            .expect("evaluated");
        if let FillDecision::Filled { fill_quantity, .. } = decision {
            filled_total += fill_quantity;
        }
    }
    // 700 + 300 (capped) + 0 (exhausted) = 1_000, never exceeding the bar volume.
    assert_eq!(filled_total, snap.bar_volume);
    assert_eq!(budget.remaining(), 0);
}

#[test]
fn mismatched_budget_cannot_overfill_a_thin_bar() {
    // A budget built for a larger bar must not be usable against a thinner
    // snapshot to fill past its observed volume (SYS-87b): the mismatch fails
    // closed, and a budget bound to the thin bar caps the fill at its volume.
    let engine = PaperSimulationEngine::new();
    let model = FillModelConfig::syrs_defaults();
    let thin = snapshot(9_990, 10_010, 10_000, 100);
    let mut oversized = BarVolumeBudget::new(10_000).expect("budget");
    assert_eq!(
        engine.evaluate_fill_against_budget(
            &OrderType::Market,
            Side::Buy,
            5_000,
            &thin,
            &model,
            &mut oversized,
        ),
        Err(FillModelError::BudgetSnapshotMismatch {
            budget_bar_volume: 10_000,
            snapshot_bar_volume: 100,
        })
    );

    let mut matched = BarVolumeBudget::for_snapshot(&thin).expect("budget");
    let decision = engine
        .evaluate_fill_against_budget(
            &OrderType::Market,
            Side::Buy,
            5_000,
            &thin,
            &model,
            &mut matched,
        )
        .expect("evaluated");
    assert_eq!(
        decision,
        FillDecision::Filled {
            fill_price_minor: 10_010,
            fill_quantity: 100,
        }
    );
}

#[test]
fn fill_flows_through_cost_family() {
    // A triggered fill's (price, quantity) feed simulate_fill, so the simulated
    // fill is charged by the SAME transaction-cost family the backtest engine
    // applies (SRS-BT-003) -- and stays entirely inside the internal simulation
    // engine, producing no IB API order call.
    let engine = PaperSimulationEngine::new();
    let model = FillModelConfig::syrs_defaults();

    let decision = engine
        .evaluate_fill(
            &OrderType::Market,
            Side::Buy,
            100,
            &snapshot(9_990, 10_000, 9_995, 1_000),
            &model,
        )
        .expect("evaluated");
    let FillDecision::Filled {
        fill_price_minor,
        fill_quantity,
    } = decision
    else {
        panic!("expected a fill, got {decision:?}");
    };
    assert_eq!(fill_price_minor, 10_000);
    assert_eq!(fill_quantity, 100);

    // Buy -> positive signed quantity into the shared cost path.
    let fill = engine
        .simulate_fill(1, "AAPL", fill_quantity, fill_price_minor, None)
        .expect("simulated fill");
    assert_eq!(fill.price_minor, 10_000);
    assert_eq!(fill.quantity, 100);
    // The shared SYS-15a/b/c defaults charge a positive cost; a buy pays the
    // notional plus the cost, so the cash delta is strictly negative.
    assert!(fill.total_cost_minor().expect("total") > 0);
    assert!(fill.cash_delta_minor < 0);
}

#[test]
fn fill_model_fails_closed_on_corrupt_data() {
    let engine = PaperSimulationEngine::new();
    let model = FillModelConfig::syrs_defaults();

    // Non-positive quote.
    assert_eq!(
        engine.evaluate_fill(
            &OrderType::Market,
            Side::Buy,
            100,
            &snapshot(9_990, 0, 10_000, 1_000),
            &model,
        ),
        Err(FillModelError::NonPositiveQuote {
            field: "ask",
            price_minor: 0,
        })
    );

    // Crossed book (bid above ask).
    assert_eq!(
        engine.evaluate_fill(
            &OrderType::Market,
            Side::Buy,
            100,
            &snapshot(10_020, 10_000, 10_010, 1_000),
            &model,
        ),
        Err(FillModelError::CrossedBook {
            bid_minor: 10_020,
            ask_minor: 10_000,
        })
    );

    // Negative bar volume.
    assert_eq!(
        engine.evaluate_fill(
            &OrderType::Market,
            Side::Buy,
            100,
            &snapshot(9_990, 10_010, 10_000, -1),
            &model,
        ),
        Err(FillModelError::NegativeVolume { bar_volume: -1 })
    );

    // Non-positive requested quantity.
    assert_eq!(
        engine.evaluate_fill(
            &OrderType::Market,
            Side::Buy,
            0,
            &snapshot(9_990, 10_010, 10_000, 1_000),
            &model,
        ),
        Err(FillModelError::NonPositiveQuantity { quantity: 0 })
    );

    // Non-positive limit price: a sell limit at -1 would otherwise cross a valid
    // bid (bid >= -1) and return a fill AT the negative price. It must fail closed.
    assert_eq!(
        engine.evaluate_fill(
            &OrderType::Limit {
                limit_price_minor: -1,
            },
            Side::Sell,
            100,
            &snapshot(9_990, 10_010, 10_000, 1_000),
            &model,
        ),
        Err(FillModelError::NonPositiveLimitPrice { price_minor: -1 })
    );

    // Non-positive stop price.
    assert_eq!(
        engine.evaluate_fill(
            &OrderType::Stop {
                stop_price_minor: 0,
            },
            Side::Buy,
            100,
            &snapshot(9_990, 10_010, 10_000, 1_000),
            &model,
        ),
        Err(FillModelError::NonPositiveStopPrice { price_minor: 0 })
    );
}

#[test]
fn no_malformed_order_price_can_produce_a_fill() {
    // Regression for the adversarial-review finding: evaluate_fill accepts a raw
    // OrderType, so a non-positive limit/stop price must be rejected rather than
    // crossing a valid snapshot and returning FillDecision::Filled at that price.
    let engine = PaperSimulationEngine::new();
    let model = FillModelConfig::syrs_defaults();
    let snap = snapshot(9_990, 10_010, 10_000, 1_000);
    let malformed = [
        OrderType::Limit {
            limit_price_minor: -1,
        },
        OrderType::Limit {
            limit_price_minor: 0,
        },
        OrderType::Stop {
            stop_price_minor: -5,
        },
        OrderType::Stop {
            stop_price_minor: 0,
        },
        OrderType::StopLimit {
            stop_price_minor: -1,
            limit_price_minor: 10_000,
        },
        OrderType::StopLimit {
            stop_price_minor: 10_000,
            limit_price_minor: 0,
        },
    ];
    for order_type in malformed {
        for side in [Side::Buy, Side::Sell] {
            let result = engine.evaluate_fill(&order_type, side, 100, &snap, &model);
            assert!(
                matches!(
                    result,
                    Err(FillModelError::NonPositiveLimitPrice { .. })
                        | Err(FillModelError::NonPositiveStopPrice { .. })
                ),
                "malformed order {order_type:?} on {side:?} must fail closed, got {result:?}"
            );
        }
    }
}

#[test]
fn evaluate_fill_is_deterministic_for_identical_inputs() {
    let engine = PaperSimulationEngine::new();
    let model = FillModelConfig::syrs_defaults();
    let snap = snapshot(9_990, 10_010, 10_000, 137);
    let first = engine.evaluate_fill(&OrderType::Market, Side::Buy, 200, &snap, &model);
    let second = engine.evaluate_fill(&OrderType::Market, Side::Buy, 200, &snap, &model);
    assert_eq!(first, second);
}
