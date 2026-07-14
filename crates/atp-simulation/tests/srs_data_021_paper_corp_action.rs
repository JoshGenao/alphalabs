//! SRS-DATA-021 (L5 integration) — apply corporate actions to paper strategy
//! virtual positions and orders.
//!
//! The acceptance criterion: *"Paper strategy virtual positions and average cost
//! are adjusted for splits, dividends, and mergers; virtual orders for delisted
//! securities are canceled using the same corporate-action data source as live
//! trading and backtesting."* These tests drive the REAL paths end to end:
//! positions built through the ledger's own fill path, orders placed through the
//! intake-validated book, actions applied in place, and — the same-source proof —
//! a fixture `MarketDataStore` whose coverage-gated
//! `query_corporate_action_facts` read feeds `actions_from_facts` feeds
//! `apply_corporate_action`.

use std::cell::RefCell;

use atp_data::store::{coverage_record, delisting_record, dividend_record, DatasetKind};
use atp_data::store::{MarketDataRecord, MarketDataStore, MarketField, NaturalKey};
use atp_data::UnifiedHistoricalQuery;
use atp_simulation::corporate_actions::{
    actions_from_facts, apply_and_emit, apply_corporate_action, PaperAlertError,
    PaperCorpActionAlert, PaperCorpActionAlertSink, PaperCorporateAction, PaperOrderOutcomeKind,
    PaperPositionOutcomeKind, PaperReviewReason,
};
use atp_simulation::paper_order::{AssetClass, OrderLeg, OrderType, Side};
use atp_simulation::sim::PaperFill;
use atp_simulation::virtual_ledger::VirtualLedgerBook;
use atp_simulation::virtual_orders::{VirtualOrderBook, VirtualOrderCancelReason};
use atp_types::StrategyId;

// --------------------------------------------------------------------------- //
// Fixtures (positions through the REAL fill path, so they cannot drift from the
// ledger's invariants)
// --------------------------------------------------------------------------- //

fn fill(symbol: &str, quantity: i64, price_minor: i64) -> PaperFill {
    let notional = i128::from(quantity) * i128::from(price_minor);
    PaperFill {
        ts: 1,
        symbol: symbol.to_string(),
        quantity,
        price_minor,
        commission_minor: 0,
        slippage_minor: 0,
        spread_impact_minor: 0,
        cash_delta_minor: i64::try_from(-notional).expect("fixture fits"),
    }
}

fn costed_fill(symbol: &str, quantity: i64, price_minor: i64, commission: i64) -> PaperFill {
    let notional = i128::from(quantity) * i128::from(price_minor);
    PaperFill {
        ts: 1,
        symbol: symbol.to_string(),
        quantity,
        price_minor,
        commission_minor: commission,
        slippage_minor: 0,
        spread_impact_minor: 0,
        cash_delta_minor: i64::try_from(-notional - i128::from(commission)).expect("fixture fits"),
    }
}

fn strategy(name: &str) -> StrategyId {
    StrategyId::new(name)
}

fn book_with(entries: &[(&str, &str, i64, i64)]) -> VirtualLedgerBook {
    let mut book = VirtualLedgerBook::new();
    for (strat, symbol, quantity, price) in entries {
        book.apply_fill(&strategy(strat), &fill(symbol, *quantity, *price))
            .expect("fixture fill applies");
    }
    book
}

fn leg(symbol: &str, quantity: i64, order_type: OrderType) -> OrderLeg {
    OrderLeg {
        symbol: symbol.to_string(),
        asset_class: AssetClass::Equity,
        side: Side::Buy,
        quantity,
        order_type,
    }
}

// --------------------------------------------------------------------------- //
// Positions: splits, dividends, mergers (the "adjusted for" clause)
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_021_forward_split_adjusts_quantity_and_average_cost() {
    let mut book = book_with(&[("alpha", "AAPL", 100, 5_000)]);
    let mut orders = VirtualOrderBook::new();
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::split("AAPL", 4, 1),
    );
    assert_eq!(report.position_outcomes.len(), 1);
    assert!(matches!(
        report.position_outcomes[0].kind,
        PaperPositionOutcomeKind::Adjusted {
            quantity_before: 100,
            quantity_after: 400,
            cost_basis_before_minor: 500_000,
            cost_basis_after_minor: 500_000,
        }
    ));
    let position = book.position(&strategy("alpha"), "AAPL").expect("held");
    assert_eq!(position.quantity(), 400);
    assert_eq!(position.cost_basis_minor(), 500_000, "basis invariant");
    assert_eq!(
        position.average_cost_minor(),
        Some(1_250),
        "average cost re-derives from the invariant basis"
    );
}

#[test]
fn srs_data_021_reverse_split_of_a_short_stays_short() {
    // A short: sell 100 @ 5000 through the real fill path.
    let mut book = VirtualLedgerBook::new();
    book.apply_fill(&strategy("alpha"), &fill("ZZZ", -100, 5_000))
        .expect("short fill applies");
    let mut orders = VirtualOrderBook::new();
    apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::split("ZZZ", 1, 10),
    );
    let position = book.position(&strategy("alpha"), "ZZZ").expect("held");
    assert_eq!(position.quantity(), -10, "short scales, never reviews");
    assert_eq!(position.cost_basis_minor(), -500_000);
}

#[test]
fn srs_data_021_dividend_adjusts_basis_additively_for_long_and_short() {
    let mut book = book_with(&[("alpha", "AAPL", 100, 5_000)]);
    book.apply_fill(&strategy("beta"), &fill("AAPL", -100, 5_000))
        .expect("short fill applies");
    let mut orders = VirtualOrderBook::new();
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::dividend("AAPL", 100, 4_000),
    );
    assert_eq!(report.position_outcomes.len(), 2, "both strategies adjust");
    let long = book.position(&strategy("alpha"), "AAPL").expect("held");
    assert_eq!(
        long.cost_basis_minor(),
        490_000,
        "long receives the dividend"
    );
    let short = book.position(&strategy("beta"), "AAPL").expect("held");
    assert_eq!(
        short.cost_basis_minor(),
        -490_000,
        "short pays the dividend"
    );
}

#[test]
fn srs_data_021_merger_remaps_with_basis_and_history_intact() {
    let mut book = VirtualLedgerBook::new();
    book.apply_fill(&strategy("alpha"), &costed_fill("OLD", 200, 4_000, 700))
        .expect("fixture fill applies");
    let mut orders = VirtualOrderBook::new();
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::merger("OLD", "NEW", 3, 2, 0),
    );
    assert!(matches!(
        &report.position_outcomes[0].kind,
        PaperPositionOutcomeKind::Remapped {
            successor,
            quantity_after: 300,
            cost_basis_after_minor: 800_000,
        } if successor == "NEW"
    ));
    assert!(
        book.position(&strategy("alpha"), "OLD").is_none(),
        "the acquired symbol's record is gone"
    );
    let position = book.position(&strategy("alpha"), "NEW").expect("remapped");
    assert_eq!(position.quantity(), 300);
    assert_eq!(position.cost_basis_minor(), 800_000, "basis carried intact");
    assert_eq!(
        position.commission_paid_minor(),
        700,
        "cost history carried intact through the remap"
    );
}

#[test]
fn srs_data_021_mixed_merger_applies_the_cash_leg_additively() {
    // A mixed stock-and-cash merger: 1-for-1 into NEW plus 250 minor per
    // acquired share. The cash leg reduces the basis additively on the
    // PRE-conversion count (the dividend convention): 500_000 − 250·100.
    let mut book = book_with(&[("alpha", "OLD", 100, 5_000)]);
    let mut orders = VirtualOrderBook::new();
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::merger("OLD", "NEW", 1, 1, 250),
    );
    assert!(matches!(
        &report.position_outcomes[0].kind,
        PaperPositionOutcomeKind::Remapped {
            successor,
            quantity_after: 100,
            cost_basis_after_minor: 475_000,
        } if successor == "NEW"
    ));
    let position = book.position(&strategy("alpha"), "NEW").expect("remapped");
    assert_eq!(position.cost_basis_minor(), 475_000);

    // A SHORT pays the cash consideration: basis −500_000 → −475_000.
    let mut book = VirtualLedgerBook::new();
    book.apply_fill(&strategy("alpha"), &fill("OLD", -100, 5_000))
        .expect("short fill applies");
    apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::merger("OLD", "NEW", 1, 1, 250),
    );
    assert_eq!(
        book.position(&strategy("alpha"), "NEW")
            .expect("remapped")
            .cost_basis_minor(),
        -475_000,
        "a short pays the cash leg"
    );

    // A cash leg exceeding the whole basis is a realized-gain event -> review,
    // position untouched.
    let mut book = book_with(&[("alpha", "OLD", 100, 5_000)]);
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::merger("OLD", "NEW", 1, 1, 6_000),
    );
    assert!(matches!(
        report.position_outcomes[0].kind,
        PaperPositionOutcomeKind::RequiresManualReview {
            reason: PaperReviewReason::CashLegCrossesBasis {
                cash_per_share_minor: 6_000
            }
        }
    ));
    assert_eq!(
        book.position(&strategy("alpha"), "OLD")
            .expect("held")
            .cost_basis_minor(),
        500_000,
        "untouched on review"
    );
}

#[test]
fn srs_data_021_pure_cash_merger_and_collision_fail_closed_to_review() {
    // A pure-cash acquisition (no successor shares) is a full disposition ->
    // review, position untouched.
    let mut book = book_with(&[("alpha", "OLD", 100, 5_000)]);
    let mut orders = VirtualOrderBook::new();
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::merger("OLD", "NEW", 0, 1, 5_500),
    );
    assert!(matches!(
        report.position_outcomes[0].kind,
        PaperPositionOutcomeKind::RequiresManualReview {
            reason: PaperReviewReason::CashConsiderationNotSupported {
                cash_per_share_minor: 5_500
            }
        }
    ));
    assert!(
        book.position(&strategy("alpha"), "OLD").is_some(),
        "untouched"
    );

    // Successor already held by the SAME strategy -> collision review.
    let mut book = book_with(&[("alpha", "OLD", 100, 5_000), ("alpha", "NEW", 50, 5_000)]);
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::merger("OLD", "NEW", 1, 1, 0),
    );
    assert!(matches!(
        &report.position_outcomes[0].kind,
        PaperPositionOutcomeKind::RequiresManualReview {
            reason: PaperReviewReason::SuccessorCollision { successor }
        } if successor == "NEW"
    ));

    // A DIFFERENT strategy holding the successor does NOT collide (ledgers are
    // independent — the one-position-per-symbol invariant is per strategy).
    let mut book = book_with(&[("alpha", "OLD", 100, 5_000), ("beta", "NEW", 50, 5_000)]);
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::merger("OLD", "NEW", 1, 1, 0),
    );
    assert!(matches!(
        report.position_outcomes[0].kind,
        PaperPositionOutcomeKind::Remapped { .. }
    ));
    assert_eq!(
        book.position(&strategy("beta"), "NEW")
            .expect("held")
            .quantity(),
        50,
        "the other strategy's position is untouched"
    );
}

#[test]
fn srs_data_021_non_integral_and_sign_crossing_fail_closed_untouched() {
    // 1-for-2 reverse split of an odd lot -> cash-in-lieu review, untouched.
    let mut book = book_with(&[("alpha", "AAPL", 101, 5_000)]);
    let mut orders = VirtualOrderBook::new();
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::split("AAPL", 1, 2),
    );
    assert!(matches!(
        report.position_outcomes[0].kind,
        PaperPositionOutcomeKind::RequiresManualReview {
            reason: PaperReviewReason::QuantityNotIntegral { before: 101, .. }
        }
    ));
    assert_eq!(
        book.position(&strategy("alpha"), "AAPL")
            .expect("held")
            .quantity(),
        101
    );

    // A dividend >= the reference close -> basis-crossing review.
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::dividend("AAPL", 4_000, 4_000),
    );
    assert!(matches!(
        report.position_outcomes[0].kind,
        PaperPositionOutcomeKind::RequiresManualReview {
            reason: PaperReviewReason::BasisCrossingDividend { .. }
        }
    ));
}

#[test]
fn srs_data_021_only_the_affected_symbol_and_non_flat_records_transform() {
    let mut book = book_with(&[("alpha", "AAPL", 100, 5_000), ("alpha", "MSFT", 10, 40_000)]);
    // A FLAT record: open then fully close BETA's AAPL.
    book.apply_fill(&strategy("beta"), &fill("AAPL", 100, 5_000))
        .expect("open");
    book.apply_fill(&strategy("beta"), &fill("AAPL", -100, 5_000))
        .expect("close");
    let mut orders = VirtualOrderBook::new();
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::split("aapl ", 2, 1), // canonical match
    );
    assert_eq!(
        report.position_outcomes.len(),
        1,
        "one outcome: alpha's open AAPL (not MSFT, not beta's flat record)"
    );
    assert_eq!(report.position_outcomes[0].strategy.as_str(), "alpha");
    assert_eq!(
        book.position(&strategy("alpha"), "MSFT")
            .expect("held")
            .quantity(),
        10,
        "other symbols untouched"
    );
    assert_eq!(
        book.position(&strategy("beta"), "AAPL")
            .expect("record")
            .quantity(),
        0,
        "flat record untouched"
    );
}

// --------------------------------------------------------------------------- //
// Orders: the "virtual orders for delisted securities are canceled" clause
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_021_delisting_cancels_open_orders_on_the_symbol_only() {
    let mut book = book_with(&[("alpha", "DEAD", 100, 5_000)]);
    let mut orders = VirtualOrderBook::new();
    let dead = orders
        .place(&strategy("alpha"), leg("DEAD", 10, OrderType::Market))
        .expect("valid");
    let dead_limit = orders
        .place(
            &strategy("beta"),
            leg(
                " dead",
                20,
                OrderType::Limit {
                    limit_price_minor: 4_900,
                },
            ),
        )
        .expect("valid — canonical match");
    let live = orders
        .place(&strategy("alpha"), leg("LIVE", 10, OrderType::Market))
        .expect("valid");
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::delisting("DEAD"),
    );
    assert_eq!(report.order_outcomes.len(), 2);
    for outcome in &report.order_outcomes {
        assert!(matches!(
            outcome.kind,
            PaperOrderOutcomeKind::Cancelled {
                reason: VirtualOrderCancelReason::Delisting
            }
        ));
    }
    assert!(!orders.order(dead).expect("recorded").is_open());
    assert!(!orders.order(dead_limit).expect("recorded").is_open());
    assert!(orders.order(live).expect("recorded").is_open());

    // Terminal: a second action produces no further outcome for them.
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::delisting("DEAD"),
    );
    assert!(
        report.order_outcomes.is_empty(),
        "cancelled orders are terminal"
    );
}

#[test]
fn srs_data_021_merger_cancels_orders_on_the_acquired_symbol() {
    let mut book = VirtualLedgerBook::new();
    let mut orders = VirtualOrderBook::new();
    orders
        .place(&strategy("alpha"), leg("OLD", 10, OrderType::Market))
        .expect("valid");
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        // Even a CASH merger cancels: the acquired series terminates regardless
        // of the conversion terms.
        &PaperCorporateAction::merger("OLD", "NEW", 1, 1, 250),
    );
    assert!(matches!(
        report.order_outcomes[0].kind,
        PaperOrderOutcomeKind::Cancelled {
            reason: VirtualOrderCancelReason::MergerTermination
        }
    ));
}

#[test]
fn srs_data_021_split_rebases_or_cancels_resting_orders() {
    let mut book = VirtualLedgerBook::new();
    let mut orders = VirtualOrderBook::new();
    let adjustable = orders
        .place(
            &strategy("alpha"),
            leg(
                "AAPL",
                100,
                OrderType::StopLimit {
                    stop_price_minor: 4_100,
                    limit_price_minor: 4_000,
                },
            ),
        )
        .expect("valid");
    let odd_lot = orders
        .place(&strategy("alpha"), leg("AAPL", 3, OrderType::Market))
        .expect("valid");
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::split("AAPL", 1, 2), // 1-for-2 reverse
    );
    assert_eq!(report.order_outcomes.len(), 2);
    let adjusted = orders.order(adjustable).expect("recorded");
    assert!(adjusted.is_open());
    assert_eq!(adjusted.leg().quantity, 50, "quantity 100 * 1/2");
    assert_eq!(
        adjusted.leg().order_type,
        OrderType::StopLimit {
            stop_price_minor: 8_200,
            limit_price_minor: 8_000,
        },
        "prices scale by the inverse factor"
    );
    let cancelled = orders.order(odd_lot).expect("recorded");
    assert!(
        matches!(
            cancelled.status(),
            atp_simulation::virtual_orders::VirtualOrderStatus::Cancelled {
                reason: VirtualOrderCancelReason::QuantityNotIntegral { before: 3, .. }
            }
        ),
        "an odd lot cancels (cash-in-lieu), never truncates"
    );
}

#[test]
fn srs_data_021_dividend_leaves_orders_resting_and_symbol_change_relabels() {
    let mut book = VirtualLedgerBook::new();
    let mut orders = VirtualOrderBook::new();
    let id = orders
        .place(
            &strategy("alpha"),
            leg(
                "OLD",
                10,
                OrderType::Limit {
                    limit_price_minor: 4_000,
                },
            ),
        )
        .expect("valid");
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::dividend("OLD", 100, 4_000),
    );
    assert!(
        report.order_outcomes.is_empty(),
        "a dividend affects no order"
    );

    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::symbol_change("OLD", "NEW"),
    );
    assert!(matches!(
        report.order_outcomes[0].kind,
        PaperOrderOutcomeKind::Adjusted { .. }
    ));
    let order = orders.order(id).expect("recorded");
    assert!(order.is_open());
    assert_eq!(order.leg().symbol, "NEW", "relabeled to the successor");
    assert_eq!(order.leg().quantity, 10, "quantity and prices unchanged");
}

#[test]
fn srs_data_021_equity_action_never_touches_an_option_order() {
    let mut book = VirtualLedgerBook::new();
    let mut orders = VirtualOrderBook::new();
    let option = orders
        .place(
            &strategy("alpha"),
            OrderLeg {
                symbol: "AAPL  240119C00190000".to_string(),
                asset_class: AssetClass::Option,
                side: Side::Buy,
                quantity: 2,
                order_type: OrderType::Market,
            },
        )
        .expect("valid");
    let report = apply_corporate_action(
        &mut book,
        &mut orders,
        &PaperCorporateAction::delisting("AAPL"),
    );
    assert!(report.order_outcomes.is_empty());
    assert!(
        orders.order(option).expect("recorded").is_open(),
        "an OCC contract is a different canonical symbol from its underlying"
    );
}

// --------------------------------------------------------------------------- //
// Alerts: the fallible sink (missed pages surfaced, never swallowed)
// --------------------------------------------------------------------------- //

struct RecordingSink {
    alerts: RefCell<Vec<PaperCorpActionAlert>>,
    fail_on_symbol: Option<String>,
}

impl PaperCorpActionAlertSink for RecordingSink {
    fn dispatch(&self, alert: PaperCorpActionAlert) -> Result<(), PaperAlertError> {
        if self.fail_on_symbol.as_deref() == Some(alert.symbol.as_str()) {
            return Err(PaperAlertError::new("transport down"));
        }
        self.alerts.borrow_mut().push(alert);
        Ok(())
    }
}

#[test]
fn srs_data_021_apply_and_emit_pages_holds_reviews_and_cancels() {
    let mut book = book_with(&[("alpha", "DEAD", 100, 5_000)]);
    let mut orders = VirtualOrderBook::new();
    orders
        .place(&strategy("alpha"), leg("DEAD", 10, OrderType::Market))
        .expect("valid");
    let sink = RecordingSink {
        alerts: RefCell::new(Vec::new()),
        fail_on_symbol: None,
    };
    let report = apply_and_emit(
        &mut book,
        &mut orders,
        &PaperCorporateAction::delisting("DEAD"),
        &sink,
    );
    assert!(report.alert_failures.is_empty());
    let alerts = sink.alerts.borrow();
    assert_eq!(alerts.len(), 2);
    assert_eq!(alerts[0].reason.kind_str(), "DELISTED_HOLD");
    assert_eq!(alerts[1].reason.kind_str(), "ORDER_CANCELLED");
    assert!(alerts[0].operator_summary().contains("delisted"));
    // A routine adjust pages nobody.
    let mut book = book_with(&[("alpha", "AAPL", 100, 5_000)]);
    let report = apply_and_emit(
        &mut book,
        &mut orders,
        &PaperCorporateAction::split("AAPL", 2, 1),
        &sink,
    );
    assert!(report.alerts().is_empty());
}

#[test]
fn srs_data_021_failed_dispatch_is_surfaced_and_does_not_abort() {
    let mut book = book_with(&[("alpha", "DEAD", 100, 5_000)]);
    let mut orders = VirtualOrderBook::new();
    orders
        .place(&strategy("alpha"), leg("DEAD", 10, OrderType::Market))
        .expect("valid");
    let sink = RecordingSink {
        alerts: RefCell::new(Vec::new()),
        fail_on_symbol: Some("DEAD".to_string()),
    };
    let report = apply_and_emit(
        &mut book,
        &mut orders,
        &PaperCorporateAction::delisting("DEAD"),
        &sink,
    );
    assert_eq!(
        report.alert_failures.len(),
        2,
        "every missed page is surfaced, and one failure does not abort the rest"
    );
    assert_eq!(report.alert_failures[0].error.reason, "transport down");
    // The BOOK mutation already happened — a paging failure never rolls back the
    // fail-closed cancel itself.
    assert_eq!(orders.open_count(), 0);
}

// --------------------------------------------------------------------------- //
// The same-data-source binding (SYS-88's "using the same corporate-action data
// source as live trading and backtesting")
// --------------------------------------------------------------------------- //

fn field(name: &str, value_minor: i64) -> MarketField {
    MarketField {
        name: name.to_string(),
        value_minor,
    }
}

fn daily_bar(symbol: &str, event_ts: i64, close: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        },
        [field("close", close), field("volume", 1_000)],
    )
    .expect("well-formed daily bar")
}

fn split_record(
    symbol: &str,
    effective_ts: i64,
    numerator: i64,
    denominator: i64,
) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionSplit,
            symbol: symbol.to_string(),
            resolution: "split".to_string(),
            event_ts: effective_ts,
            option_contract: None,
        },
        [
            field("denominator", denominator),
            field("numerator", numerator),
        ],
    )
    .expect("well-formed split record")
}

#[test]
fn srs_data_021_store_facts_drive_the_paper_application_end_to_end() {
    // ONE store holds the corporate-action records (the SRS-DATA-011 data source
    // backtests read through their gated bar reads); the paper application
    // consumes the SAME store through the coverage-gated fact read.
    let mut store = MarketDataStore::new();
    for record in [
        daily_bar("AAPL", 100, 4_000),
        split_record("AAPL", 200, 4, 1),
        dividend_record(300, "AAPL", 100),
        delisting_record(400, "DEAD"),
        coverage_record(500, "AAPL"),
        coverage_record(500, "DEAD"),
    ] {
        store.upsert(record).expect("fixture upsert");
    }

    let mut book = book_with(&[("alpha", "AAPL", 100, 5_000), ("alpha", "DEAD", 10, 1_000)]);
    let mut orders = VirtualOrderBook::new();
    orders
        .place(&strategy("alpha"), leg("DEAD", 5, OrderType::Market))
        .expect("valid");

    // AAPL's in-window facts: the split then the dividend, in event order.
    let facts = store
        .query_corporate_action_facts(
            &UnifiedHistoricalQuery::new("AAPL", "1d", 0, 500)
                .with_kind(DatasetKind::DailyEquityBar),
        )
        .expect("covered");
    for action in actions_from_facts(&facts) {
        apply_corporate_action(&mut book, &mut orders, &action);
    }
    // DEAD's facts: the delisting.
    let facts = store
        .query_corporate_action_facts(
            &UnifiedHistoricalQuery::new("DEAD", "1d", 0, 500)
                .with_kind(DatasetKind::DailyEquityBar),
        )
        .expect("covered");
    for action in actions_from_facts(&facts) {
        apply_corporate_action(&mut book, &mut orders, &action);
    }

    // Split (x4, basis invariant) then dividend (additive 100 * 400 shares).
    let position = book.position(&strategy("alpha"), "AAPL").expect("held");
    assert_eq!(position.quantity(), 400);
    assert_eq!(position.cost_basis_minor(), 500_000 - 100 * 400);
    // The delisted security's order is cancelled; the position still held.
    assert_eq!(orders.open_count(), 0);
    assert_eq!(
        book.position(&strategy("alpha"), "DEAD")
            .expect("held")
            .quantity(),
        10
    );
}

#[test]
fn srs_data_021_pre_rename_split_reaches_the_position_held_under_the_old_symbol() {
    // The adversarial-review regression: OLD splits 2-for-1 @200, then renames
    // to NEW @300. The paper book holds OLD. Facts queried for NEW must carry
    // the split under its AS-HELD symbol (OLD) so that, applied in event order,
    // the split hits the held position FIRST and the rename fact then carries
    // the book onto NEW. (Retagging the split to NEW — the price reads'
    // relabeling — would silently skip it and remap an unadjusted position.)
    let mut store = MarketDataStore::new();
    for record in [
        atp_data::store::symbol_change_record(300, "OLD", "NEW"),
        split_record("OLD", 200, 2, 1),
        coverage_record(400, "NEW"),
    ] {
        store.upsert(record).expect("fixture upsert");
    }
    let mut book = book_with(&[("alpha", "OLD", 100, 5_000)]);
    let mut orders = VirtualOrderBook::new();
    let order = orders
        .place(
            &strategy("alpha"),
            leg(
                "OLD",
                100,
                OrderType::Limit {
                    limit_price_minor: 4_000,
                },
            ),
        )
        .expect("valid order");

    let facts = store
        .query_corporate_action_facts(
            &UnifiedHistoricalQuery::new("NEW", "1d", 0, 400)
                .with_kind(DatasetKind::DailyEquityBar),
        )
        .expect("covered");
    for action in actions_from_facts(&facts) {
        apply_corporate_action(&mut book, &mut orders, &action);
    }

    // The split applied to the OLD-held position (100 -> 200, basis invariant),
    // THEN the rename carried it onto NEW.
    assert!(book.position(&strategy("alpha"), "OLD").is_none());
    let position = book.position(&strategy("alpha"), "NEW").expect("remapped");
    assert_eq!(
        position.quantity(),
        200,
        "the pre-rename split was NOT skipped"
    );
    assert_eq!(position.cost_basis_minor(), 500_000);
    // The resting order took the same journey: split-rebased, then relabeled.
    let resting = orders.order(order).expect("recorded");
    assert!(resting.is_open());
    assert_eq!(resting.leg().symbol, "NEW");
    assert_eq!(resting.leg().quantity, 200);
    assert_eq!(
        resting.leg().order_type,
        OrderType::Limit {
            limit_price_minor: 2_000
        },
        "limit price halved by the 2-for-1 split before the relabel"
    );
}

#[test]
fn srs_data_021_uncovered_store_refuses_the_fact_read() {
    // The application path inherits the coverage gate: no coverage record, no
    // facts — a paper adjuster can never act on a window that could hide an
    // action.
    let mut store = MarketDataStore::new();
    store
        .upsert(split_record("AAPL", 200, 4, 1))
        .expect("fixture upsert");
    assert!(store
        .query_corporate_action_facts(
            &UnifiedHistoricalQuery::new("AAPL", "1d", 0, 500)
                .with_kind(DatasetKind::DailyEquityBar),
        )
        .is_err());
}

#[test]
fn srs_data_021_facts_map_totally_onto_paper_actions() {
    use atp_data::CorporateActionFact;
    let facts = vec![
        CorporateActionFact::Split {
            symbol: "A".into(),
            effective_ts: 1,
            numerator: 2,
            denominator: 1,
        },
        CorporateActionFact::Dividend {
            symbol: "A".into(),
            ex_ts: 2,
            amount_minor: 100,
            prev_close_minor: 4_000,
        },
        CorporateActionFact::Merger {
            symbol: "A".into(),
            successor: "B".into(),
            numerator: 3,
            denominator: 2,
            cash_per_share_minor: 0,
            effective_ts: 3,
        },
        CorporateActionFact::SymbolChange {
            predecessor: "B".into(),
            successor: "C".into(),
            effective_ts: 4,
        },
        CorporateActionFact::Delisting {
            symbol: "C".into(),
            effective_ts: 5,
        },
    ];
    let actions = actions_from_facts(&facts);
    assert_eq!(
        actions.len(),
        5,
        "every fact maps (total, order-preserving)"
    );
    assert_eq!(actions[0], PaperCorporateAction::split("A", 2, 1));
    assert_eq!(actions[1], PaperCorporateAction::dividend("A", 100, 4_000));
    assert_eq!(actions[2], PaperCorporateAction::merger("A", "B", 3, 2, 0));
    assert_eq!(actions[3], PaperCorporateAction::symbol_change("B", "C"));
    assert_eq!(actions[4], PaperCorporateAction::delisting("C"));
}
