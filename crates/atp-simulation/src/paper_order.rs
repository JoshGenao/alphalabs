//! Paper order-intake path for **SRS-SIM-001** — "simulate paper strategy orders
//! locally without routing to any brokerage" (SyRS **SYS-82** local paper order
//! execution, SYS-3 order types, SYS-4 multi-leg composite; StRS SN-1.29 /
//! SN-1.08 / SN-1.24).
//!
//! # The "no IB API order calls" guarantee
//!
//! The acceptance criterion is that *market, limit, stop, stop-limit, equity,
//! option, and multi-leg orders are processed by the simulation engine and
//! create **no IB API order calls***. This module makes that a **compile-time**
//! guarantee rather than a runtime check: [`PaperSimulationEngine::accept_order`]
//! returns an [`OrderRouting`] whose **only** variant is
//! [`OrderRouting::InternalSimulation`]. There is structurally no `Broker` / `Ib`
//! variant to construct, and the `atp-simulation` crate has no dependency on any
//! brokerage adapter (see `Cargo.toml`), so a paper order *cannot* reach a
//! broker. The paired domain test
//! (`tests/domain/test_paper_order_no_broker_route.py`) pins both facts.
//!
//! # What is real here vs deferred
//!
//! This slice is the **order-intake** layer: it accepts a [`PaperOrderRequest`]
//! (a single [`OrderLeg`] or a [`PaperOrderRequest::MultiLeg`] composite),
//! validates every leg, and routes it to the internal simulation engine — for
//! every order type ([`OrderType`]) and asset class ([`AssetClass`]). A multi-leg
//! options order routes as **one composite transaction** (SYS-4): all legs share
//! a single [`OrderRouting::InternalSimulation`] with `composite = true`, so they
//! fill atomically rather than independently.
//!
//! Every context the SRS-SIM-001 acceptance criterion names — every order type
//! ([`OrderType`]), both asset classes ([`AssetClass`]), the multi-leg composite,
//! and the "no IB API order calls" guarantee — is built here, and the operator
//! binary `sim001_paper_order_cli` (the `order_cli` sub-block of
//! `architecture/runtime_services.json#sim_order_contract`) makes that
//! demonstrable: `types` / `assets` / `multileg` / `no-broker` route every shape
//! internally and prove none reaches a brokerage. So `feature_list.json` marks
//! SRS-SIM-001 `passes:true`.
//!
//! The items under `…#sim_order_contract.deferred` are genuinely **adjacent**
//! features — each its own requirement, NOT a context inside SRS-SIM-001's
//! acceptance criterion: the SYS-83 fill *triggering* (when a limit/stop price is
//! crossed, fill probability, the bar-volume cap) and live-market-data-driven
//! fills are SRS-SIM-002 (built, `passes:true`); the full SYS-84 virtual ledger is
//! SRS-SIM-003 (built, `passes:true`); paper-state persistence (SYS-89) is
//! SRS-SIM-004; the orchestrator routing of *all* non-live strategies into this
//! engine is SRS-EXE-002; and the Python strategy runtime that actually submits
//! these orders end to end is the shared SRS-SDK deferral.
//!
//! # Money math
//!
//! Order intake performs no fill arithmetic (that is SRS-SIM-002), but every
//! price an order carries is an **integer minor unit** with the `_minor` suffix
//! (`limit_price_minor`, `stop_price_minor`) — never floating point — so the
//! downstream fill path is exact and overflow-safe. Intake fails closed on a
//! non-positive limit/stop price before the order is ever routed.

use std::fmt;

use crate::sim::PaperSimulationEngine;

// The source-neutral order-type vocabulary (`AssetClass` / `Side` / `OrderType`)
// is owned by `atp-types` (the `order_type` module, plus the crate-root
// `AssetClass`). This paper simulation path CONSUMES that shared definition by
// re-exporting it below; the SAME source-neutral type is intended for the future
// live intake (SRS-EXE-006) to consume so the two paths become identical once it
// lands — this slice does NOT yet make the live path consume it (SRS-EXE-003
// stays passes:false). These types were hoisted out of this module (originally
// defined here for SRS-SIM-001) and are re-exported unchanged, so every call site
// keeps working against `paper_order::{AssetClass, Side, OrderType}`.
// `tools/order_type_check.py` pins the re-export so a divergent copy cannot
// reappear. `OrderType` encodes its trigger/limit prices (integer minor units) in
// the variants; price positivity is validated by `OrderType::validate_prices`,
// which `validate_leg` below delegates to.
pub use atp_types::order_type::OrderSide as Side;
pub use atp_types::order_type::OrderType;
pub use atp_types::AssetClass;
// Internal: the shared price-validation error, mapped into OrderError by
// validate_leg so the paper intake delegates to OrderType::validate_prices.
use atp_types::order_type::OrderTypeError;

/// A single order leg: what instrument, which side, how many, and the order
/// type. A leg is the unit of both single and multi-leg requests.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderLeg {
    /// The instrument symbol (must be non-empty / non-whitespace).
    pub symbol: String,
    /// Equity or option.
    pub asset_class: AssetClass,
    /// Buy or sell.
    pub side: Side,
    /// The order quantity (must be `> 0`; the buy/sell direction lives in
    /// [`Side`]).
    pub quantity: i64,
    /// The order type and any trigger/limit prices.
    pub order_type: OrderType,
}

/// A paper order submitted to the simulation engine: either a single leg or a
/// multi-leg **composite** (SYS-4 — multi-leg options orders execute as one
/// composite transaction).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PaperOrderRequest {
    /// A single-leg order (equity or option).
    Single(OrderLeg),
    /// A multi-leg composite order whose legs fill atomically as one
    /// transaction.
    MultiLeg { legs: Vec<OrderLeg> },
}

/// Where an accepted paper order is routed.
///
/// **Critical safety design:** there is exactly ONE variant — the internal
/// simulation engine. No `Broker` / `Ib` variant exists, so it is structurally
/// impossible for [`PaperSimulationEngine::accept_order`] to route a paper order
/// to a brokerage. "Creates no IB API order calls" (SRS-SIM-001) is therefore a
/// compile-time guarantee, not a runtime check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderRouting {
    /// The order was routed to the internal simulation engine. `composite` is
    /// `true` when the legs form one atomic multi-leg transaction (SYS-4).
    InternalSimulation {
        legs: Vec<OrderLeg>,
        composite: bool,
    },
}

impl OrderRouting {
    /// The legs the simulation engine will fill for this routed order.
    pub fn legs(&self) -> &[OrderLeg] {
        match self {
            Self::InternalSimulation { legs, .. } => legs,
        }
    }

    /// Whether the routed order is a single atomic multi-leg composite (SYS-4).
    pub fn is_composite(&self) -> bool {
        match self {
            Self::InternalSimulation { composite, .. } => *composite,
        }
    }
}

/// Fail-closed errors from paper order intake. Carries no broker/vendor
/// identifiers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderError {
    /// A leg symbol was empty / whitespace.
    EmptySymbol,
    /// A leg quantity was not strictly positive.
    NonPositiveQuantity { quantity: i64 },
    /// A limit order carried a non-positive limit price.
    NonPositiveLimitPrice { price_minor: i64 },
    /// A stop / stop-limit order carried a non-positive stop price.
    NonPositiveStopPrice { price_minor: i64 },
    /// A multi-leg composite request carried no legs.
    EmptyMultiLeg,
    /// A multi-leg composite carried only one leg. SYS-4 multi-leg orders are
    /// composites of two or more legs; a single-leg request must be a
    /// [`PaperOrderRequest::Single`].
    SingleLegComposite,
    /// A multi-leg composite carried a non-option leg. SYS-4 / SRS-EXE-004 scope
    /// multi-leg composites to options orders, so every composite leg must be an
    /// [`AssetClass::Option`].
    NonOptionCompositeLeg,
}

impl fmt::Display for OrderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptySymbol => write!(f, "paper order leg symbol must not be empty"),
            Self::NonPositiveQuantity { quantity } => {
                write!(
                    f,
                    "paper order leg quantity {quantity} must be strictly positive"
                )
            }
            Self::NonPositiveLimitPrice { price_minor } => write!(
                f,
                "paper order limit price {price_minor} minor units must be strictly positive"
            ),
            Self::NonPositiveStopPrice { price_minor } => write!(
                f,
                "paper order stop price {price_minor} minor units must be strictly positive"
            ),
            Self::EmptyMultiLeg => write!(f, "multi-leg paper order must carry at least one leg"),
            Self::SingleLegComposite => write!(
                f,
                "multi-leg composite paper order must carry at least two legs (SYS-4)"
            ),
            Self::NonOptionCompositeLeg => write!(
                f,
                "multi-leg composite paper order legs must all be options (SYS-4)"
            ),
        }
    }
}

impl std::error::Error for OrderError {}

/// Validate a single leg, failing closed before the order can be routed. A bad
/// symbol, a non-positive quantity, or a non-positive trigger/limit price is
/// rejected so a malformed order never reaches the simulation fill path.
fn validate_leg(leg: &OrderLeg) -> Result<(), OrderError> {
    if leg.symbol.trim().is_empty() {
        return Err(OrderError::EmptySymbol);
    }
    if leg.quantity <= 0 {
        return Err(OrderError::NonPositiveQuantity {
            quantity: leg.quantity,
        });
    }
    // Delegate price positivity to the shared SRS-EXE-003 authority so the paper
    // intake and the (future) live intake apply the SAME rule rather than a copy
    // that can drift; map the shared OrderTypeError into this module's
    // fail-closed OrderError. (validate_prices checks the stop price before the
    // limit price, matching the prior behavior.)
    leg.order_type.validate_prices().map_err(|err| match err {
        OrderTypeError::NonPositiveLimitPrice { price_minor } => {
            OrderError::NonPositiveLimitPrice { price_minor }
        }
        OrderTypeError::NonPositiveStopPrice { price_minor } => {
            OrderError::NonPositiveStopPrice { price_minor }
        }
    })
}

impl PaperSimulationEngine {
    /// Accept a paper order and route it to the **internal simulation engine**
    /// (SRS-SIM-001).
    ///
    /// Every leg is validated (fail closed on an empty symbol, a non-positive
    /// quantity, or a non-positive trigger/limit price) before the order is
    /// routed. A [`PaperOrderRequest::MultiLeg`] routes as one composite
    /// transaction (SYS-4). The return type is [`OrderRouting`], whose only
    /// variant is [`OrderRouting::InternalSimulation`] — so an accepted paper
    /// order can never reach a brokerage (no IB API order call).
    pub fn accept_order(&self, request: &PaperOrderRequest) -> Result<OrderRouting, OrderError> {
        match request {
            PaperOrderRequest::Single(leg) => {
                validate_leg(leg)?;
                Ok(OrderRouting::InternalSimulation {
                    legs: vec![leg.clone()],
                    composite: false,
                })
            }
            PaperOrderRequest::MultiLeg { legs } => {
                if legs.is_empty() {
                    return Err(OrderError::EmptyMultiLeg);
                }
                // SYS-4 / SRS-EXE-004 scope multi-leg to OPTIONS composites: a
                // composite is two or more option legs that fill atomically. A
                // single-leg request must be a `Single`, and an equity/mixed
                // composite is out of scope — fail closed on both before routing.
                if legs.len() < 2 {
                    return Err(OrderError::SingleLegComposite);
                }
                for leg in legs {
                    validate_leg(leg)?;
                    if leg.asset_class != AssetClass::Option {
                        return Err(OrderError::NonOptionCompositeLeg);
                    }
                }
                Ok(OrderRouting::InternalSimulation {
                    legs: legs.clone(),
                    composite: true,
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn equity_leg(order_type: OrderType) -> OrderLeg {
        OrderLeg {
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Equity,
            side: Side::Buy,
            quantity: 100,
            order_type,
        }
    }

    fn option_leg(side: Side, order_type: OrderType) -> OrderLeg {
        OrderLeg {
            symbol: "AAPL  240119C00190000".to_string(),
            asset_class: AssetClass::Option,
            side,
            quantity: 2,
            order_type,
        }
    }

    #[test]
    fn single_equity_market_order_routes_to_internal_sim() {
        let engine = PaperSimulationEngine::new();
        let routing = engine
            .accept_order(&PaperOrderRequest::Single(equity_leg(OrderType::Market)))
            .expect("accepted");
        assert!(!routing.is_composite());
        assert_eq!(routing.legs().len(), 1);
        assert_eq!(routing.legs()[0].asset_class, AssetClass::Equity);
        // The only routing variant is the internal simulation engine.
        let OrderRouting::InternalSimulation { .. } = routing;
    }

    #[test]
    fn single_option_limit_order_routes_to_internal_sim() {
        let engine = PaperSimulationEngine::new();
        let routing = engine
            .accept_order(&PaperOrderRequest::Single(option_leg(
                Side::Buy,
                OrderType::Limit {
                    limit_price_minor: 250,
                },
            )))
            .expect("accepted");
        assert!(!routing.is_composite());
        assert_eq!(routing.legs()[0].asset_class, AssetClass::Option);
    }

    #[test]
    fn stop_and_stop_limit_orders_route_to_internal_sim() {
        let engine = PaperSimulationEngine::new();
        for order_type in [
            OrderType::Stop {
                stop_price_minor: 9_500,
            },
            OrderType::StopLimit {
                stop_price_minor: 9_500,
                limit_price_minor: 9_400,
            },
        ] {
            let routing = engine
                .accept_order(&PaperOrderRequest::Single(equity_leg(order_type)))
                .expect("accepted");
            assert_eq!(routing.legs().len(), 1);
        }
    }

    #[test]
    fn multi_leg_order_routes_as_one_composite_transaction() {
        // A vertical option spread: buy one call, sell another (SYS-4).
        let engine = PaperSimulationEngine::new();
        let routing = engine
            .accept_order(&PaperOrderRequest::MultiLeg {
                legs: vec![
                    option_leg(Side::Buy, OrderType::Market),
                    option_leg(Side::Sell, OrderType::Market),
                ],
            })
            .expect("accepted");
        assert!(
            routing.is_composite(),
            "multi-leg must route as one composite transaction"
        );
        assert_eq!(routing.legs().len(), 2);
        assert_eq!(routing.legs()[0].side, Side::Buy);
        assert_eq!(routing.legs()[1].side, Side::Sell);
    }

    #[test]
    fn single_order_is_not_composite() {
        let engine = PaperSimulationEngine::new();
        let routing = engine
            .accept_order(&PaperOrderRequest::Single(equity_leg(OrderType::Market)))
            .expect("accepted");
        assert!(!routing.is_composite());
    }

    #[test]
    fn empty_symbol_fails_closed() {
        let engine = PaperSimulationEngine::new();
        let mut leg = equity_leg(OrderType::Market);
        leg.symbol = "   ".to_string();
        assert_eq!(
            engine.accept_order(&PaperOrderRequest::Single(leg)),
            Err(OrderError::EmptySymbol)
        );
    }

    #[test]
    fn non_positive_quantity_fails_closed() {
        let engine = PaperSimulationEngine::new();
        let mut leg = equity_leg(OrderType::Market);
        leg.quantity = 0;
        assert_eq!(
            engine.accept_order(&PaperOrderRequest::Single(leg)),
            Err(OrderError::NonPositiveQuantity { quantity: 0 })
        );
    }

    #[test]
    fn non_positive_limit_price_fails_closed() {
        let engine = PaperSimulationEngine::new();
        let leg = equity_leg(OrderType::Limit {
            limit_price_minor: 0,
        });
        assert_eq!(
            engine.accept_order(&PaperOrderRequest::Single(leg)),
            Err(OrderError::NonPositiveLimitPrice { price_minor: 0 })
        );
    }

    #[test]
    fn non_positive_stop_price_fails_closed() {
        let engine = PaperSimulationEngine::new();
        let leg = equity_leg(OrderType::Stop {
            stop_price_minor: -1,
        });
        assert_eq!(
            engine.accept_order(&PaperOrderRequest::Single(leg)),
            Err(OrderError::NonPositiveStopPrice { price_minor: -1 })
        );
    }

    #[test]
    fn stop_limit_validates_both_prices() {
        let engine = PaperSimulationEngine::new();
        // Bad stop, good limit -> stop error first.
        let leg = equity_leg(OrderType::StopLimit {
            stop_price_minor: 0,
            limit_price_minor: 9_400,
        });
        assert_eq!(
            engine.accept_order(&PaperOrderRequest::Single(leg)),
            Err(OrderError::NonPositiveStopPrice { price_minor: 0 })
        );
        // Good stop, bad limit -> limit error.
        let leg = equity_leg(OrderType::StopLimit {
            stop_price_minor: 9_500,
            limit_price_minor: -5,
        });
        assert_eq!(
            engine.accept_order(&PaperOrderRequest::Single(leg)),
            Err(OrderError::NonPositiveLimitPrice { price_minor: -5 })
        );
    }

    #[test]
    fn empty_multi_leg_fails_closed() {
        let engine = PaperSimulationEngine::new();
        assert_eq!(
            engine.accept_order(&PaperOrderRequest::MultiLeg { legs: vec![] }),
            Err(OrderError::EmptyMultiLeg)
        );
    }

    #[test]
    fn multi_leg_with_one_bad_leg_fails_closed() {
        let engine = PaperSimulationEngine::new();
        let mut bad = option_leg(Side::Sell, OrderType::Market);
        bad.quantity = -3;
        let routing = engine.accept_order(&PaperOrderRequest::MultiLeg {
            legs: vec![option_leg(Side::Buy, OrderType::Market), bad],
        });
        assert_eq!(
            routing,
            Err(OrderError::NonPositiveQuantity { quantity: -3 })
        );
    }

    #[test]
    fn single_leg_composite_fails_closed() {
        // A composite must carry two or more legs (SYS-4); a one-leg request
        // belongs in a `Single`.
        let engine = PaperSimulationEngine::new();
        assert_eq!(
            engine.accept_order(&PaperOrderRequest::MultiLeg {
                legs: vec![option_leg(Side::Buy, OrderType::Market)],
            }),
            Err(OrderError::SingleLegComposite)
        );
    }

    #[test]
    fn non_option_composite_leg_fails_closed() {
        // SYS-4 multi-leg composites are options-only; an equity leg in a
        // composite fails closed before routing.
        let engine = PaperSimulationEngine::new();
        assert_eq!(
            engine.accept_order(&PaperOrderRequest::MultiLeg {
                legs: vec![
                    option_leg(Side::Buy, OrderType::Market),
                    equity_leg(OrderType::Market),
                ],
            }),
            Err(OrderError::NonOptionCompositeLeg)
        );
    }

    #[test]
    fn deterministic_for_identical_requests() {
        let engine = PaperSimulationEngine::new();
        let request = PaperOrderRequest::Single(equity_leg(OrderType::Limit {
            limit_price_minor: 9_973,
        }));
        assert_eq!(engine.accept_order(&request), engine.accept_order(&request));
    }
}
