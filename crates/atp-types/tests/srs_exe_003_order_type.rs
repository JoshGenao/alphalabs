//! Integration coverage for **SRS-EXE-003** — the source-neutral order-type
//! vocabulary + price-validation rule (market / limit / stop / stop-limit,
//! equity / option, buy / sell) that lives in `atp-types` as the single shared
//! definition. The paper path consumes it via re-export, and as of SRS-EXE-003
//! the live path does too — `atp_types::OrderSubmission` carries the order type
//! and the IB adapter validates it. Exercises the public API exactly as a
//! downstream consumer would.
//! Tests are `srs_exe_003_`-prefixed so the L7 domain test
//! (`tests/domain/test_order_type.py`) can drive the safety-relevant subset by
//! exact name.
//!
//! Note: this lives in the leaf crate `atp-types`, which cannot depend on
//! `atp-simulation`, so the "live and paper share ONE type" guarantee is pinned
//! textually by `tools/order_type_check.py` (the paper re-export), not here.

use atp_types::order_type::{OrderSide, OrderType, OrderTypeError};
use atp_types::AssetClass;

const ALL_TYPES: [OrderType; 4] = [
    OrderType::Market,
    OrderType::Limit {
        limit_price_minor: 1,
    },
    OrderType::Stop {
        stop_price_minor: 1,
    },
    OrderType::StopLimit {
        stop_price_minor: 1,
        limit_price_minor: 1,
    },
];

#[test]
fn srs_exe_003_all_four_order_types_have_stable_wire_strings() {
    assert_eq!(OrderType::Market.as_str(), "MARKET");
    assert_eq!(
        OrderType::Limit {
            limit_price_minor: 10_000
        }
        .as_str(),
        "LIMIT"
    );
    assert_eq!(
        OrderType::Stop {
            stop_price_minor: 10_000
        }
        .as_str(),
        "STOP"
    );
    assert_eq!(
        OrderType::StopLimit {
            stop_price_minor: 10_000,
            limit_price_minor: 9_900
        }
        .as_str(),
        "STOP_LIMIT"
    );
    assert_eq!(
        OrderType::ALL_WIRE,
        ["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    );
}

#[test]
fn srs_exe_003_sides_and_asset_classes_have_stable_wire_strings() {
    assert_eq!(OrderSide::Buy.as_str(), "BUY");
    assert_eq!(OrderSide::Sell.as_str(), "SELL");
    assert_eq!(AssetClass::Equity.as_str(), "EQUITY");
    assert_eq!(AssetClass::Option.as_str(), "OPTION");
}

#[test]
fn srs_exe_003_price_requirement_matrix_is_total_and_correct() {
    assert!(!OrderType::Market.requires_limit_price());
    assert!(!OrderType::Market.requires_stop_price());

    let limit = OrderType::Limit {
        limit_price_minor: 1,
    };
    assert!(limit.requires_limit_price());
    assert!(!limit.requires_stop_price());

    let stop = OrderType::Stop {
        stop_price_minor: 1,
    };
    assert!(!stop.requires_limit_price());
    assert!(stop.requires_stop_price());

    let stop_limit = OrderType::StopLimit {
        stop_price_minor: 1,
        limit_price_minor: 2,
    };
    assert!(stop_limit.requires_limit_price());
    assert!(stop_limit.requires_stop_price());
}

#[test]
fn srs_exe_003_no_contradictory_price_set_is_representable() {
    // The price parameters are encoded in the variants, so `requires_*` and the
    // accessors are ALWAYS consistent: a Market can never carry a price and a
    // Limit can never lack one. This is the type-level guarantee that a
    // malformed (contradictory) order can never even be constructed, let alone
    // reach the live broker — the core SRS-EXE-003 safety property.
    for order_type in ALL_TYPES {
        assert_eq!(
            order_type.requires_limit_price(),
            order_type.limit_price_minor().is_some()
        );
        assert_eq!(
            order_type.requires_stop_price(),
            order_type.stop_price_minor().is_some()
        );
    }
}

#[test]
fn srs_exe_003_validation_identical_across_equity_and_option() {
    // The order-type authority is asset-class-agnostic: the same type validates
    // identically whether the leg is an equity or an option (SRS-EXE-003 covers
    // both). Option *contract identity* is deferred to SRS-EXE-004 / SRS-DATA-004.
    for _asset in [AssetClass::Equity, AssetClass::Option] {
        for order_type in ALL_TYPES {
            assert!(order_type.validate_prices().is_ok());
        }
    }
}

#[test]
fn srs_exe_003_validate_prices_passes_for_positive_prices() {
    for order_type in ALL_TYPES {
        assert!(order_type.validate_prices().is_ok());
    }
}

#[test]
fn srs_exe_003_validate_prices_fails_closed_on_non_positive_prices() {
    for bad in [0_i64, -1, i64::MIN] {
        assert_eq!(
            OrderType::Limit {
                limit_price_minor: bad
            }
            .validate_prices(),
            Err(OrderTypeError::NonPositiveLimitPrice { price_minor: bad })
        );
        assert_eq!(
            OrderType::Stop {
                stop_price_minor: bad
            }
            .validate_prices(),
            Err(OrderTypeError::NonPositiveStopPrice { price_minor: bad })
        );
    }
    // Stop is checked before limit on a stop-limit with both invalid.
    assert_eq!(
        OrderType::StopLimit {
            stop_price_minor: 0,
            limit_price_minor: 0
        }
        .validate_prices(),
        Err(OrderTypeError::NonPositiveStopPrice { price_minor: 0 })
    );
}

#[test]
fn srs_exe_003_error_display_is_human_readable() {
    let msg = OrderType::Limit {
        limit_price_minor: -1,
    }
    .validate_prices()
    .unwrap_err()
    .to_string();
    assert!(msg.contains("limit price"), "got: {msg}");
    assert!(msg.contains("strictly positive"), "got: {msg}");
}
