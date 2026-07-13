//! SRS-DATA-020 — adjust live positions affected by corporate actions.
//!
//! The transform math for every corporate-action class over signed live positions:
//! splits (forward / reverse, long / short, exact-or-review, basis-invariant), cash
//! dividends (additive basis, long / short, P&L-invariant, fail-closed terms),
//! mergers (stock-for-stock remap, cash-leg review, successor validation, collision),
//! symbol changes (relabel), and delistings (freeze + terminal). Companion suite
//! `srs_data_020_corp_action_notify` proves the operator-notification clause.

use atp_execution::corporate_action_positions::{
    plan_position, plan_positions, LivePosition, PositionChangeKind, PositionCorpActionOutcome,
    PositionCorporateAction, PositionReviewReason,
};

fn pos(symbol: &str, quantity: i64, basis: i128) -> LivePosition {
    LivePosition::new(symbol, quantity, basis).expect("valid position")
}

fn expect_adjusted(outcome: &PositionCorpActionOutcome) -> &LivePosition {
    match outcome {
        PositionCorpActionOutcome::Adjusted { after, .. } => after,
        other => panic!("expected Adjusted, got {other:?}"),
    }
}

fn expect_review(outcome: &PositionCorpActionOutcome) -> &PositionReviewReason {
    match outcome {
        PositionCorpActionOutcome::RequiresManualReview { reason, .. } => reason,
        other => panic!("expected RequiresManualReview, got {other:?}"),
    }
}

// --------------------------------------------------------------------------- //
// Splits
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_020_forward_split_scales_quantity_up_and_keeps_basis_invariant() {
    let before = pos("AAPL", 100, 500_000);
    let outcome = plan_position(&before, &PositionCorporateAction::split("AAPL", 4, 1));
    let after = expect_adjusted(&outcome);
    assert_eq!(after.quantity(), 400);
    assert_eq!(
        after.cost_basis_minor(),
        before.cost_basis_minor(),
        "a split never changes the total invested — basis is invariant"
    );
    assert_eq!(after.average_cost_minor(), Some(1_250));
}

#[test]
fn srs_data_020_reverse_split_long_scales_quantity_down_exact() {
    let outcome = plan_position(
        &pos("ZZZ", 100, 500_000),
        &PositionCorporateAction::split("ZZZ", 1, 10),
    );
    let after = expect_adjusted(&outcome);
    assert_eq!(after.quantity(), 10);
    assert_eq!(after.cost_basis_minor(), 500_000);
}

#[test]
fn srs_data_020_reverse_split_of_a_short_yields_negative_quantity_not_a_review() {
    // The load-bearing signed-quantity fix: a short must NOT be sent to review.
    let short = LivePosition::new("ZZZ", -100, -500_000).expect("valid short");
    let outcome = plan_position(&short, &PositionCorporateAction::split("ZZZ", 1, 10));
    let after = expect_adjusted(&outcome);
    assert_eq!(after.quantity(), -10);
    assert_eq!(after.cost_basis_minor(), -500_000);
    assert_eq!(
        after.average_cost_minor(),
        Some(50_000),
        "avg cost stays positive"
    );
}

#[test]
fn srs_data_020_fractional_split_is_review_for_long_and_short() {
    for quantity in [105_i64, -105_i64] {
        let basis = i128::from(quantity) * 1_000;
        let outcome = plan_position(
            &pos("ZZZ", quantity, basis),
            &PositionCorporateAction::split("ZZZ", 1, 10),
        );
        assert_eq!(
            expect_review(&outcome).as_str(),
            "QUANTITY_NOT_INTEGRAL",
            "a fractional share count is cash-in-lieu, never truncated (qty {quantity})"
        );
    }
}

#[test]
fn srs_data_020_one_for_one_split_is_a_no_op() {
    let outcome = plan_position(
        &pos("AAPL", 100, 500_000),
        &PositionCorporateAction::split("AAPL", 1, 1),
    );
    assert!(matches!(
        outcome,
        PositionCorpActionOutcome::Unaffected { .. }
    ));
}

#[test]
fn srs_data_020_non_positive_split_factor_is_review() {
    for (n, m) in [(0, 1), (1, 0), (-2, 1), (1, -2)] {
        let outcome = plan_position(
            &pos("AAPL", 100, 500_000),
            &PositionCorporateAction::split("AAPL", n, m),
        );
        assert_eq!(
            expect_review(&outcome).as_str(),
            "NON_POSITIVE_FACTOR",
            "{n}/{m}"
        );
    }
}

#[test]
fn srs_data_020_split_quantity_overflow_is_review() {
    let outcome = plan_position(
        &pos("AAPL", i64::MAX, i128::from(i64::MAX)),
        &PositionCorporateAction::split("AAPL", 2, 1),
    );
    assert_eq!(expect_review(&outcome).as_str(), "OVERFLOW");
}

// --------------------------------------------------------------------------- //
// Cash dividends (ADDITIVE basis reduction)
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_020_dividend_reduces_long_basis_by_exact_cash() {
    // basis' = basis - amount*qty = 500000 - 100*100 = 490000
    let outcome = plan_position(
        &pos("AAPL", 100, 500_000),
        &PositionCorporateAction::dividend("AAPL", 100, 4_000),
    );
    let after = expect_adjusted(&outcome);
    assert_eq!(
        after.quantity(),
        100,
        "a cash dividend never changes the share count"
    );
    assert_eq!(after.cost_basis_minor(), 490_000);
}

#[test]
fn srs_data_020_dividend_on_a_short_reduces_proceeds_basis_magnitude() {
    // A short PAYS the dividend: basis' = -500000 - 100*(-100) = -490000 (|basis| falls).
    let short = LivePosition::new("AAPL", -100, -500_000).expect("valid short");
    let after_o = plan_position(
        &short,
        &PositionCorporateAction::dividend("AAPL", 100, 4_000),
    );
    let after = expect_adjusted(&after_o);
    assert_eq!(after.cost_basis_minor(), -490_000);
}

#[test]
fn srs_data_020_dividend_preserves_absolute_pnl_across_the_ex_date() {
    // Value conservation: P&L = mark*q - basis must be invariant when the mark drops by
    // the dividend and the basis is reduced additively.
    let before = pos("AAPL", 100, 500_000);
    let (amount, prev_close) = (100_i64, 4_000_i64);
    let mark_before = i128::from(prev_close);
    let pnl_before = mark_before * i128::from(before.quantity()) - before.cost_basis_minor();

    let after = expect_adjusted(&plan_position(
        &before,
        &PositionCorporateAction::dividend("AAPL", amount, prev_close),
    ))
    .clone();
    let mark_after = i128::from(prev_close - amount);
    let pnl_after = mark_after * i128::from(after.quantity()) - after.cost_basis_minor();

    assert_eq!(
        pnl_before, pnl_after,
        "absolute P&L is invariant across the ex-date"
    );
}

#[test]
fn srs_data_020_invalid_dividend_terms_are_review() {
    for (amount, prev_close) in [(0, 4_000), (-1, 4_000), (100, 0), (100, -1)] {
        let outcome = plan_position(
            &pos("AAPL", 100, 500_000),
            &PositionCorporateAction::dividend("AAPL", amount, prev_close),
        );
        assert_eq!(
            expect_review(&outcome).as_str(),
            "INVALID_DIVIDEND_TERM",
            "{amount}:{prev_close}"
        );
    }
}

#[test]
fn srs_data_020_dividend_at_or_above_reference_close_is_review() {
    let outcome = plan_position(
        &pos("AAPL", 100, 500_000),
        &PositionCorporateAction::dividend("AAPL", 4_000, 4_000),
    );
    assert_eq!(expect_review(&outcome).as_str(), "BASIS_CROSSING_DIVIDEND");
}

#[test]
fn srs_data_020_dividend_that_would_drive_basis_through_zero_is_review() {
    // Deep-in-the-money long: 100 @ avg 1 (basis 100), prev_close 50, dividend 2.
    // amount(2) < prev_close(50) passes the per-share guard, but 2*100 = 200 > basis 100,
    // so the basis would flip sign (negative average cost) — flagged, not fabricated.
    let outcome = plan_position(
        &pos("DEEP", 100, 100),
        &PositionCorporateAction::dividend("DEEP", 2, 50),
    );
    assert_eq!(expect_review(&outcome).as_str(), "BASIS_CROSSING_DIVIDEND");
}

// --------------------------------------------------------------------------- //
// Mergers
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_020_stock_for_stock_merger_remaps_long_and_short_with_basis_intact() {
    // Long: 200 -> 300 successor shares (3-for-2), basis carried.
    match plan_position(
        &pos("OLD", 200, 800_000),
        &PositionCorporateAction::merger("OLD", "NEW", 3, 2, 0),
    ) {
        PositionCorpActionOutcome::Remapped { from_symbol, after } => {
            assert_eq!(from_symbol, "OLD");
            assert_eq!(after.symbol(), "NEW");
            assert_eq!(after.quantity(), 300);
            assert_eq!(after.cost_basis_minor(), 800_000, "basis carries intact");
        }
        other => panic!("expected Remapped, got {other:?}"),
    }
    // Short: -200 -> -300, negative basis carried.
    let short = LivePosition::new("OLD", -200, -800_000).expect("valid short");
    match plan_position(
        &short,
        &PositionCorporateAction::merger("OLD", "NEW", 3, 2, 0),
    ) {
        PositionCorpActionOutcome::Remapped { after, .. } => {
            assert_eq!(after.quantity(), -300);
            assert_eq!(after.cost_basis_minor(), -800_000);
        }
        other => panic!("expected Remapped short, got {other:?}"),
    }
}

#[test]
fn srs_data_020_merger_non_integral_ratio_is_review() {
    // 101 shares at 3-for-2 -> 151.5 -> cash-in-lieu.
    let outcome = plan_position(
        &pos("OLD", 101, 505_000),
        &PositionCorporateAction::merger("OLD", "NEW", 3, 2, 0),
    );
    assert_eq!(expect_review(&outcome).as_str(), "QUANTITY_NOT_INTEGRAL");
}

#[test]
fn srs_data_020_merger_with_any_cash_is_review() {
    // Positive cash leg, pure-cash (N==0), and a negative cash term all fail closed.
    for (n, m, cash) in [(1, 1, 250), (0, 1, 500), (1, 1, -1)] {
        let outcome = plan_position(
            &pos("OLD", 100, 500_000),
            &PositionCorporateAction::merger("OLD", "NEW", n, m, cash),
        );
        assert_eq!(
            expect_review(&outcome).as_str(),
            "CASH_CONSIDERATION_NOT_SUPPORTED",
            "{n}:{m}:{cash}"
        );
    }
}

#[test]
fn srs_data_020_merger_non_positive_denominator_is_review() {
    let outcome = plan_position(
        &pos("OLD", 100, 500_000),
        &PositionCorporateAction::merger("OLD", "NEW", 1, 0, 0),
    );
    assert_eq!(expect_review(&outcome).as_str(), "NON_POSITIVE_FACTOR");
}

#[test]
fn srs_data_020_merger_invalid_successor_is_review() {
    // Blank successor and a self-merger (successor == predecessor, canonical).
    for successor in ["", "old"] {
        let outcome = plan_position(
            &pos("OLD", 100, 500_000),
            &PositionCorporateAction::merger("OLD", successor, 1, 1, 0),
        );
        assert_eq!(
            expect_review(&outcome).as_str(),
            "INVALID_SUCCESSOR",
            "successor '{successor}'"
        );
    }
}

#[test]
fn srs_data_020_merger_onto_a_held_symbol_is_a_collision_review() {
    // OLD merges into NEW, but NEW is already held: merging the two bases is a manual
    // operation, not something the planner fabricates.
    let positions = vec![pos("OLD", 100, 500_000), pos("NEW", 50, 250_000)];
    let outcomes = plan_positions(
        &positions,
        &PositionCorporateAction::merger("OLD", "NEW", 1, 1, 0),
    );
    let old = outcomes
        .iter()
        .find(|o| o.symbol() == "OLD")
        .expect("OLD outcome");
    assert_eq!(expect_review(old).as_str(), "SUCCESSOR_COLLISION");
    // NEW itself is unaffected (the action is on OLD).
    let new = outcomes
        .iter()
        .find(|o| o.symbol() == "NEW")
        .expect("NEW outcome");
    assert!(matches!(new, PositionCorpActionOutcome::Unaffected { .. }));
}

// --------------------------------------------------------------------------- //
// Symbol changes
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_020_symbol_change_relabels_quantity_and_basis_unchanged() {
    match plan_position(
        &pos("OLD", -100, -500_000),
        &PositionCorporateAction::symbol_change("OLD", "NEW"),
    ) {
        PositionCorpActionOutcome::Remapped { from_symbol, after } => {
            assert_eq!(from_symbol, "OLD");
            assert_eq!(after.symbol(), "NEW");
            assert_eq!(after.quantity(), -100);
            assert_eq!(after.cost_basis_minor(), -500_000);
        }
        other => panic!("expected Remapped, got {other:?}"),
    }
}

#[test]
fn srs_data_020_self_relabel_is_a_no_op() {
    let outcome = plan_position(
        &pos("AAPL", 100, 500_000),
        &PositionCorporateAction::symbol_change("AAPL", " aapl "),
    );
    assert!(matches!(
        outcome,
        PositionCorpActionOutcome::Unaffected { .. }
    ));
}

#[test]
fn srs_data_020_symbol_change_collision_is_review() {
    let positions = vec![pos("OLD", 100, 500_000), pos("NEW", 50, 250_000)];
    let outcomes = plan_positions(
        &positions,
        &PositionCorporateAction::symbol_change("OLD", "NEW"),
    );
    let old = outcomes
        .iter()
        .find(|o| o.symbol() == "OLD")
        .expect("OLD outcome");
    assert_eq!(expect_review(old).as_str(), "SUCCESSOR_COLLISION");
}

// --------------------------------------------------------------------------- //
// Delisting
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_020_delisting_freezes_long_and_short_basis() {
    for quantity in [100_i64, -100_i64] {
        let basis = i128::from(quantity) * 5_000;
        let outcome = plan_position(
            &pos("DEAD", quantity, basis),
            &PositionCorporateAction::delisting("DEAD"),
        );
        match outcome {
            PositionCorpActionOutcome::Delisted { position } => {
                assert!(position.is_delisted());
                assert_eq!(position.quantity(), quantity, "quantity frozen");
                assert_eq!(
                    position.cost_basis_minor(),
                    basis,
                    "basis frozen (never fabricated)"
                );
            }
            other => panic!("expected Delisted, got {other:?}"),
        }
    }
}

#[test]
fn srs_data_020_already_delisted_position_is_terminal() {
    let delisted = LivePosition::delisted("DEAD", 100, 500_000).expect("valid delisted");
    // No further corporate action reaches a delisted position.
    for action in [
        PositionCorporateAction::split("DEAD", 2, 1),
        PositionCorporateAction::dividend("DEAD", 100, 4_000),
        PositionCorporateAction::merger("DEAD", "NEW", 1, 1, 0),
        PositionCorporateAction::delisting("DEAD"),
    ] {
        let outcome = plan_position(&delisted, &action);
        assert!(
            matches!(outcome, PositionCorpActionOutcome::Unaffected { .. }),
            "{action:?}"
        );
    }
}

// --------------------------------------------------------------------------- //
// Matching, validation, determinism
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_020_symbol_match_is_case_and_whitespace_insensitive() {
    // A position on ` aapl ` is affected by an AAPL action.
    let outcome = plan_position(
        &pos(" aapl ", 100, 500_000),
        &PositionCorporateAction::split("AAPL", 2, 1),
    );
    assert_eq!(expect_adjusted(&outcome).quantity(), 200);
}

#[test]
fn srs_data_020_other_symbol_position_is_unaffected() {
    let outcome = plan_position(
        &pos("MSFT", 100, 500_000),
        &PositionCorporateAction::split("AAPL", 2, 1),
    );
    assert!(matches!(
        outcome,
        PositionCorpActionOutcome::Unaffected { .. }
    ));
}

#[test]
fn srs_data_020_constructor_fails_closed_on_bad_inputs() {
    assert!(LivePosition::new("AAPL", 0, 0).is_err(), "flat position");
    assert!(
        LivePosition::new("   ", 100, 500_000).is_err(),
        "blank symbol"
    );
    assert!(
        LivePosition::new("AAPL", 100, -1).is_err(),
        "sign-inconsistent basis (negative average cost)"
    );
    assert!(
        LivePosition::new("AAPL", -100, 1).is_err(),
        "sign-inconsistent short basis"
    );
    // A zero-cost spinoff (non-flat, zero basis) is allowed.
    assert!(LivePosition::new("SPIN", 100, 0).is_ok());
}

#[test]
fn srs_data_020_outcomes_are_sorted_by_canonical_symbol() {
    let positions = vec![
        pos("ZZZ", 10, 50_000),
        pos("AAA", 10, 50_000),
        pos("MMM", 10, 50_000),
    ];
    let outcomes = plan_positions(&positions, &PositionCorporateAction::delisting("QQQ"));
    let symbols: Vec<&str> = outcomes.iter().map(|o| o.symbol()).collect();
    assert_eq!(
        symbols,
        vec!["AAA", "MMM", "ZZZ"],
        "deterministic canonical-symbol order"
    );
}

#[test]
fn srs_data_020_strategy_callback_covers_adjusted_remapped_delisted_only() {
    let split = plan_position(
        &pos("AAPL", 100, 500_000),
        &PositionCorporateAction::split("AAPL", 2, 1),
    );
    assert_eq!(
        split.strategy_callback().unwrap().kind,
        PositionChangeKind::Adjusted
    );

    let remap = plan_position(
        &pos("OLD", 100, 500_000),
        &PositionCorporateAction::symbol_change("OLD", "NEW"),
    );
    assert_eq!(
        remap.strategy_callback().unwrap().kind,
        PositionChangeKind::Remapped
    );

    let delist = plan_position(
        &pos("DEAD", 100, 500_000),
        &PositionCorporateAction::delisting("DEAD"),
    );
    assert_eq!(
        delist.strategy_callback().unwrap().kind,
        PositionChangeKind::Delisted
    );

    let review = plan_position(
        &pos("AAPL", 100, 500_000),
        &PositionCorporateAction::split("AAPL", 0, 1),
    );
    assert!(
        review.strategy_callback().is_none(),
        "review is not a strategy change event"
    );

    let unaffected = plan_position(
        &pos("MSFT", 100, 500_000),
        &PositionCorporateAction::delisting("AAPL"),
    );
    assert!(unaffected.strategy_callback().is_none());
}
