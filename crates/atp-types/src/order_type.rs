//! Source-neutral **order-type vocabulary + price-validation authority** for
//! **SRS-EXE-003** — "support market, limit, stop, and stop-limit orders for
//! equities and options in live and paper modes" (docs/SRS.md §5.3; SyRS SYS-3
//! order types / SYS-82 local paper execution; StRS SN-1.08 / BG-1).
//!
//! # Why this lives in `atp-types`
//!
//! The SRS-EXE-003 acceptance criterion is that each order type can be
//! "accepted, validated, state-tracked, and acknowledged in **both** live
//! adapter test mode **and** internal simulation." A *prerequisite* for that
//! AC is that the live execution path ([`atp-execution`], SRS-EXE-001 /
//! SRS-EXE-006) and the paper simulation path ([`atp-simulation`], SRS-SIM-001)
//! share ONE order-type model — not two that can drift. Both sibling crates
//! depend on this leaf crate, so the canonical vocabulary lives here and neither
//! side re-implements it.
//!
//! Status, stated honestly: the paper path **consumes** this definition today
//! (it re-exports the types — see below). The live path does **not** yet — at
//! HEAD `atp-execution` has no order-type intake and `OrderSubmission` carries
//! only `symbol` + `quantity`. So the AC is **not** satisfied by this slice
//! (SRS-EXE-003 stays `passes:false`); this module is the shared seam the live
//! intake (SRS-EXE-006) will consume so the two paths are identical *once it
//! lands*, not a claim that they already are.
//!
//! This module was **hoisted** from `atp-simulation`'s `paper_order` module
//! (which originally defined `OrderType` / `Side` / `AssetClass` for the paper
//! intake path under SRS-SIM-001). `paper_order` now RE-EXPORTS these types: the
//! paper engine consumes this one definition today, and it is the SAME type the
//! future live intake (SRS-EXE-006) will consume — so when the live path is wired
//! the two will be identical by definition (one type, not two copies), but this
//! slice does not yet make the live path consume it.
//! `tools/order_type_check.py` pins the re-export so a divergent copy cannot
//! reappear.
//!
//! # Prices are encoded in the variants
//!
//! A [`OrderType::Limit`] *carries* its `limit_price_minor`; a
//! [`OrderType::Market`] has no price field at all. So the "contradictory price
//! set" bug class — a limit order with no limit price, or a market order with a
//! stray stop price — is impossible to even *represent* (this much IS by
//! construction). The remaining runtime check is **price positivity**, the
//! shared rule [`OrderType::validate_prices`] expresses. NOTE: the variants are
//! `pub`, so a caller *can* construct `Limit { limit_price_minor: -5 }` without
//! calling `validate_prices`; positivity is therefore an **intake-boundary**
//! check, not a construction-time guarantee. The paper intake applies it today
//! (`paper_order::validate_leg`); the live intake (SRS-EXE-006) will apply it
//! when it lands. Restricting the variants to validated constructors / private
//! fields to make positivity unbypassable is an SRS-SIM-001 API change (it would
//! touch every `OrderType::Limit { .. }` match/construction site across the
//! simulation crate) and is deferred to that intake-wiring work — see the
//! contract `deferred[]` list.
//!
//! # Money math: integer minor units, never `f64`
//!
//! Every trigger/limit price is an integer **minor unit** carrying the `_minor`
//! suffix (matching `atp-simulation`'s `price_minor` / `commission_minor`
//! convention and `sim.rs`'s `NonPositivePrice` guard), so downstream fill and
//! P&L arithmetic stays exact and overflow-safe. The Python authoring surface
//! (`atp_strategy.api.OrderRequest`) exposes ergonomic floats; normalizing those
//! to minor units happens at the SDK→core boundary (deferred with the runtime
//! intake). The pinned cross-language parity is the **enum wire strings** + the
//! **price-requirement matrix**, not the float-vs-int representation.
//!
//! # Scope (SRS-EXE-003 SDK-surface) — what is real here vs deferred
//!
//! Real: the four order types + their stable wire strings, the [`OrderSide`]
//! vocabulary, the price-requirement matrix, and fail-closed price-positivity
//! validation — one source of truth across the Rust core, the
//! `order_type_contract` JSON mirror, and the Python SDK.
//!
//! Deferred (so SRS-EXE-003 stays `passes:false`; see
//! `architecture/runtime_services.json#order_type_contract.deferred`): the
//! live-path intake + accept→ack round-trip through the IB adapter (SRS-EXE-006)
//! and the paper accept→ack/fill (SRS-SIM-001/002); order STATE-TRACKING via the
//! SRS-EXE-008 lifecycle machine; the orchestrator routing of non-live orders to
//! the simulation engine (SRS-EXE-002); option **contract identity** + LIVE
//! multi-leg composite submission (SRS-EXE-004 / SRS-DATA-004, mirroring the
//! existing `SecurityKey` option deferral); making price positivity unbypassable
//! *by construction* (validated constructors / private fields — the variants are
//! public today); and enforcing the price matrix on the Python `OrderRequest`
//! authoring surface. The paper intake already DELEGATES price positivity to
//! [`OrderType::validate_prices`] (`paper_order::validate_leg`) — that is done,
//! not deferred.

use std::fmt;

/// The four supported order types (SYS-3). Trigger/limit prices are integer
/// **minor units** (`_minor` suffix) and are encoded directly in the variants,
/// so a limit order can never lack a limit price and a market order can never
/// carry one. Price **positivity** is enforced by [`OrderType::validate_prices`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OrderType {
    /// Fills at the prevailing market price. Carries no trigger/limit price.
    Market,
    /// Rests and fills only at `limit_price_minor` or better.
    Limit { limit_price_minor: i64 },
    /// Becomes a market order once the market crosses `stop_price_minor`.
    Stop { stop_price_minor: i64 },
    /// Triggered at `stop_price_minor`, then rests as a limit at
    /// `limit_price_minor`.
    StopLimit {
        stop_price_minor: i64,
        limit_price_minor: i64,
    },
}

impl OrderType {
    /// Stable wire string. Matches the Python `atp_strategy.api.OrderType`
    /// member values and the `order_type_contract.order_types[].wire` mirror.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Market => "MARKET",
            Self::Limit { .. } => "LIMIT",
            Self::Stop { .. } => "STOP",
            Self::StopLimit { .. } => "STOP_LIMIT",
        }
    }

    /// The four order-type wire strings in declaration order (the totality
    /// anchor the contract check and tests pin against).
    pub const ALL_WIRE: [&'static str; 4] = ["MARKET", "LIMIT", "STOP", "STOP_LIMIT"];

    /// Whether this order type requires a limit price (`LIMIT`, `STOP_LIMIT`).
    pub const fn requires_limit_price(self) -> bool {
        matches!(self, Self::Limit { .. } | Self::StopLimit { .. })
    }

    /// Whether this order type requires a stop price (`STOP`, `STOP_LIMIT`).
    pub const fn requires_stop_price(self) -> bool {
        matches!(self, Self::Stop { .. } | Self::StopLimit { .. })
    }

    /// The limit price (minor units), or `None` for types that carry none.
    pub const fn limit_price_minor(self) -> Option<i64> {
        match self {
            Self::Limit { limit_price_minor }
            | Self::StopLimit {
                limit_price_minor, ..
            } => Some(limit_price_minor),
            Self::Market | Self::Stop { .. } => None,
        }
    }

    /// The stop price (minor units), or `None` for types that carry none.
    pub const fn stop_price_minor(self) -> Option<i64> {
        match self {
            Self::Stop { stop_price_minor }
            | Self::StopLimit {
                stop_price_minor, ..
            } => Some(stop_price_minor),
            Self::Market | Self::Limit { .. } => None,
        }
    }

    /// The shared price-positivity rule an order-submission **intake** applies
    /// (the SRS-EXE-003 AC "validated"): every price present on the type must be
    /// strictly positive. This expresses the rule once so both paths share it;
    /// it is NOT enforced at construction (the variants are public — see the
    /// module docs), so a consumer MUST call it. The paper intake
    /// (`paper_order::validate_leg`) and the SRS-SIM-002 fill path
    /// (`fill_model::validate_order_type`) both DELEGATE to this method today
    /// (mapping `OrderTypeError` into their own fail-closed errors), so they
    /// cannot drift from it; the live intake (SRS-EXE-006) will too.
    /// `tools/order_type_check.py` pins those delegations in lock-step.
    pub fn validate_prices(self) -> Result<(), OrderTypeError> {
        if let Some(price_minor) = self.stop_price_minor() {
            if price_minor <= 0 {
                return Err(OrderTypeError::NonPositiveStopPrice { price_minor });
            }
        }
        if let Some(price_minor) = self.limit_price_minor() {
            if price_minor <= 0 {
                return Err(OrderTypeError::NonPositiveLimitPrice { price_minor });
            }
        }
        Ok(())
    }
}

/// The side of an order. Hoisted from `atp-simulation`'s `paper_order::Side`
/// (re-exported there as `Side`) so live and paper share one definition.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OrderSide {
    /// Buy / long.
    Buy,
    /// Sell / short.
    Sell,
}

impl OrderSide {
    /// Stable wire string. Matches the Python `atp_strategy.api.OrderSide`
    /// member values and the `order_type_contract.sides` mirror.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Buy => "BUY",
            Self::Sell => "SELL",
        }
    }

    /// The two side wire strings in declaration order.
    pub const ALL_WIRE: [&'static str; 2] = ["BUY", "SELL"];
}

/// Fail-closed price-validation errors from the order-type authority,
/// surfaced by [`OrderType::validate_prices`]. Consumers map these into their own
/// fail-closed error types: `atp-simulation`'s `paper_order::validate_leg` and
/// `fill_model::validate_order_type` both delegate to `validate_prices` and map
/// these into `OrderError` / `FillModelError` (pinned by the contract check).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderTypeError {
    /// A `LIMIT` / `STOP_LIMIT` carried a non-positive limit price.
    NonPositiveLimitPrice { price_minor: i64 },
    /// A `STOP` / `STOP_LIMIT` carried a non-positive stop price.
    NonPositiveStopPrice { price_minor: i64 },
}

impl fmt::Display for OrderTypeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonPositiveLimitPrice { price_minor } => write!(
                f,
                "order limit price {price_minor} minor units must be strictly positive"
            ),
            Self::NonPositiveStopPrice { price_minor } => write!(
                f,
                "order stop price {price_minor} minor units must be strictly positive"
            ),
        }
    }
}

impl std::error::Error for OrderTypeError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn order_type_wire_strings_are_stable() {
        assert_eq!(OrderType::Market.as_str(), "MARKET");
        assert_eq!(
            OrderType::Limit {
                limit_price_minor: 1
            }
            .as_str(),
            "LIMIT"
        );
        assert_eq!(
            OrderType::Stop {
                stop_price_minor: 1
            }
            .as_str(),
            "STOP"
        );
        assert_eq!(
            OrderType::StopLimit {
                stop_price_minor: 1,
                limit_price_minor: 1,
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
    fn order_side_wire_strings_are_stable() {
        assert_eq!(OrderSide::Buy.as_str(), "BUY");
        assert_eq!(OrderSide::Sell.as_str(), "SELL");
        assert_eq!(OrderSide::ALL_WIRE, ["BUY", "SELL"]);
    }

    #[test]
    fn price_requirement_matrix_is_total_and_correct() {
        // (order_type, requires_limit, requires_stop)
        let cases = [
            (OrderType::Market, false, false),
            (
                OrderType::Limit {
                    limit_price_minor: 5,
                },
                true,
                false,
            ),
            (
                OrderType::Stop {
                    stop_price_minor: 5,
                },
                false,
                true,
            ),
            (
                OrderType::StopLimit {
                    stop_price_minor: 5,
                    limit_price_minor: 6,
                },
                true,
                true,
            ),
        ];
        for (order_type, wants_limit, wants_stop) in cases {
            assert_eq!(order_type.requires_limit_price(), wants_limit);
            assert_eq!(order_type.requires_stop_price(), wants_stop);
            assert_eq!(order_type.limit_price_minor().is_some(), wants_limit);
            assert_eq!(order_type.stop_price_minor().is_some(), wants_stop);
        }
    }

    #[test]
    fn validate_prices_passes_for_positive_prices() {
        assert!(OrderType::Market.validate_prices().is_ok());
        assert!(OrderType::Limit {
            limit_price_minor: 1
        }
        .validate_prices()
        .is_ok());
        assert!(OrderType::Stop {
            stop_price_minor: 1
        }
        .validate_prices()
        .is_ok());
        assert!(OrderType::StopLimit {
            stop_price_minor: 1,
            limit_price_minor: 1,
        }
        .validate_prices()
        .is_ok());
    }

    #[test]
    fn validate_prices_fails_closed_on_non_positive_limit() {
        for bad in [0, -1, i64::MIN] {
            assert_eq!(
                OrderType::Limit {
                    limit_price_minor: bad
                }
                .validate_prices(),
                Err(OrderTypeError::NonPositiveLimitPrice { price_minor: bad })
            );
        }
    }

    #[test]
    fn validate_prices_fails_closed_on_non_positive_stop() {
        for bad in [0, -1, i64::MIN] {
            assert_eq!(
                OrderType::Stop {
                    stop_price_minor: bad
                }
                .validate_prices(),
                Err(OrderTypeError::NonPositiveStopPrice { price_minor: bad })
            );
        }
    }

    #[test]
    fn stop_limit_checks_stop_before_limit() {
        // Both non-positive: stop is reported first (checked first).
        assert_eq!(
            OrderType::StopLimit {
                stop_price_minor: 0,
                limit_price_minor: 0,
            }
            .validate_prices(),
            Err(OrderTypeError::NonPositiveStopPrice { price_minor: 0 })
        );
        // Stop ok, limit bad: limit is reported.
        assert_eq!(
            OrderType::StopLimit {
                stop_price_minor: 5,
                limit_price_minor: -2,
            }
            .validate_prices(),
            Err(OrderTypeError::NonPositiveLimitPrice { price_minor: -2 })
        );
    }
}
