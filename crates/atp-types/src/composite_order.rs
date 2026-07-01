//! Multi-leg **options composite** order substrate for **SRS-EXE-004** — "support
//! multi-leg options orders as composite transactions" (docs/SRS.md §5.3
//! SRS-EXE-004; SyRS SYS-4 / SYS-40 / SYS-82; StRS SN-1.24).
//!
//! # What this module owns
//!
//! Two things the rest of the platform has been **deferring to SRS-EXE-004**:
//!
//! 1. [`OptionContractIdentity`] — the canonical option-contract identity
//!    (underlying + expiration + strike + call/put right). Both
//!    [`OrderSubmission::validate`](crate::OrderSubmission::validate) and
//!    [`SecurityKey::new`](crate::SecurityKey::new) currently **fail closed** on
//!    `AssetClass::Option` precisely because this identity did not exist yet —
//!    keying an option by its underlying symbol alone would conflate distinct
//!    contracts (e.g. two different strikes/expirations of AAPL) onto one line.
//!    This is the vendor-neutral model those consumers (and SRS-DATA-004's
//!    option-chain snapshots) will build on. It lives in `atp-types` — the leaf
//!    crate every side depends on — so live, paper, and the data layer share ONE
//!    identity, not copies that can drift.
//! 2. [`CompositeOrderSubmission`] — a multi-leg options order that is submitted
//!    and filled as **one composite transaction** (SYS-4). A composite is two or
//!    more [`CompositeOrderLeg`]s; each leg names a full option contract, a side,
//!    a quantity, and an order type. Because a leg carries an
//!    [`OptionContractIdentity`] *by type*, a composite is **options-only by
//!    construction** — an equity leg is not representable — mirroring the
//!    compile-time safety design of `paper_order::OrderRouting`.
//!
//! # Fail-closed, by construction where possible
//!
//! [`OptionContractIdentity::new`] normalizes the underlying (trim + upper-case)
//! and rejects a blank underlying, a non-positive strike, or an impossible
//! calendar date (leap-year aware). The fields are private, so an
//! `OptionContractIdentity` value is **always** well-formed. The remaining
//! order-level rules — at least two legs (SYS-4), strictly-positive quantities,
//! and strictly-positive trigger/limit prices — are enforced by
//! [`CompositeOrderSubmission::validate`], which every intake MUST call before
//! routing (the same "validate at the intake boundary" contract the single-leg
//! [`OrderSubmission::validate`](crate::OrderSubmission::validate) follows).
//!
//! # Money math: integer minor units, never `f64`
//!
//! The strike ([`OptionContractIdentity`]) and every trigger/limit price (via
//! the leg's [`OrderType`]) are integer **minor units** with the `_minor` suffix,
//! matching the `atp-simulation` `price_minor` convention, so downstream fill and
//! P&L arithmetic stays exact and overflow-safe.
//!
//! # Scope (SRS-EXE-004) — real here vs deferred
//!
//! Real: the identity model, the composite envelope, fail-closed validation, and
//! the adapter composite-submit seam (`BrokerageAdapter::submit_composite_order`
//! over the deterministic in-memory gateway proves "one composite order in IB
//! test mode"). The internal simulation already routes a multi-leg options order
//! as one composite (`PaperOrderRequest::MultiLeg`, SRS-SIM-001), so paper mode's
//! "one composite" half is done.
//!
//! Deferred (so SRS-EXE-004 stays `passes:false`; see
//! `architecture/runtime_services.json#composite_order_contract.deferred`): the
//! **real IB TWS combo-order wire** (operator-gated, SRS-EXE-006 — the
//! deterministic accept/validate/ack works today; the live socket encoding is
//! completed under the operator IB paper-account integration test); the
//! **dashboard composite position** display (SRS-UI-002); wiring the identity
//! into the *single-leg* live option `OrderSubmission` and the option
//! **subscription** key (SRS-EXE-003 / SRS-MD-001 follow-ups — this feature does
//! not change their still-fail-closed behavior, only supplies the type they will
//! consume); the `CompositeOrderSubmission` → `PaperOrderRequest::MultiLeg`
//! sim-engine bridge (the same orchestrator seam the single-leg bridge is
//! deferred to); and the Python multi-leg **authoring** surface
//! (`atp_strategy.api`, the deferred SDK runtime).

use std::fmt;

use crate::order_type::{OrderSide, OrderType, OrderTypeError};
use crate::StrategyId;

/// Whether an option contract is a call or a put — the fourth component of an
/// option's identity (with underlying, expiration, and strike).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum OptionRight {
    /// The right to **buy** the underlying at the strike.
    Call,
    /// The right to **sell** the underlying at the strike.
    Put,
}

impl OptionRight {
    /// Stable wire string.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Call => "CALL",
            Self::Put => "PUT",
        }
    }

    /// The two right wire strings in declaration order.
    pub const ALL_WIRE: [&'static str; 2] = ["CALL", "PUT"];
}

/// An option **expiration date** with a validated (leap-year-aware) constructor.
/// The fields are private, so an `ExpirationDate` value always names a real
/// calendar day — `2024-02-30` cannot be constructed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ExpirationDate {
    year: i32,
    month: u8,
    day: u8,
}

impl ExpirationDate {
    /// Build an expiration date, failing closed on an impossible calendar date.
    /// The year is bounded to a plausible option-market range (1900..=9999) so a
    /// transposed or garbage field is rejected rather than silently accepted.
    pub fn new(year: i32, month: u8, day: u8) -> Result<Self, OptionContractError> {
        let invalid = |detail: &str| OptionContractError::InvalidExpiration {
            year,
            month,
            day,
            detail: detail.to_string(),
        };
        if !(1900..=9999).contains(&year) {
            return Err(invalid("year is outside the plausible range 1900..=9999"));
        }
        if !(1..=12).contains(&month) {
            return Err(invalid("month must be in 1..=12"));
        }
        let max_day = days_in_month(year, month);
        if day < 1 || day > max_day {
            return Err(invalid("day is out of range for the given month/year"));
        }
        Ok(Self { year, month, day })
    }

    pub const fn year(self) -> i32 {
        self.year
    }

    pub const fn month(self) -> u8 {
        self.month
    }

    pub const fn day(self) -> u8 {
        self.day
    }

    /// The date as a zero-padded `YYYYMMDD` string (the canonical-key component).
    pub fn yyyymmdd(self) -> String {
        format!("{:04}{:02}{:02}", self.year, self.month, self.day)
    }
}

impl fmt::Display for ExpirationDate {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }
}

/// Days in `month` of `year`, leap-year aware (Gregorian). `month` is assumed to
/// be in `1..=12` (the caller validates that first).
const fn days_in_month(year: i32, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap_year(year) => 29,
        2 => 28,
        _ => 0,
    }
}

const fn is_leap_year(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

/// The canonical identity of one option contract: underlying + expiration +
/// strike + call/put right. Two order legs (or two market-data snapshots) name
/// the SAME contract iff their `OptionContractIdentity`s are equal — so this is
/// the dedup / grouping key an option position (or subscription line) is built
/// on. The underlying is normalized (trim + upper-case); the strike is an
/// integer **minor unit**. The fields are private: a value can only be built
/// through [`new`](Self::new), which fails closed on inputs it cannot
/// canonicalize. Carries no broker / vendor identifier.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct OptionContractIdentity {
    underlying: String,
    expiration: ExpirationDate,
    strike_minor: i64,
    right: OptionRight,
}

impl OptionContractIdentity {
    /// Build a canonical option contract identity, normalizing the underlying
    /// (trim + upper-case). Fails closed on a blank underlying, a non-positive
    /// strike, or an impossible expiration date (via [`ExpirationDate::new`]).
    pub fn new(
        underlying: &str,
        expiration: ExpirationDate,
        strike_minor: i64,
        right: OptionRight,
    ) -> Result<Self, OptionContractError> {
        let normalized = underlying.trim().to_uppercase();
        if normalized.is_empty() {
            return Err(OptionContractError::BlankUnderlying);
        }
        if strike_minor <= 0 {
            return Err(OptionContractError::NonPositiveStrike { strike_minor });
        }
        Ok(Self {
            underlying: normalized,
            expiration,
            strike_minor,
            right,
        })
    }

    pub fn underlying(&self) -> &str {
        &self.underlying
    }

    pub fn expiration(&self) -> ExpirationDate {
        self.expiration
    }

    pub fn strike_minor(&self) -> i64 {
        self.strike_minor
    }

    pub fn right(&self) -> OptionRight {
        self.right
    }

    /// A deterministic, unambiguous identity string
    /// (`UNDERLYING:YYYYMMDD:C|P:STRIKE_MINOR`, e.g. `AAPL:20240119:C:19000000`).
    /// This is a canonical **dedup key**, not a claim to the OCC 21-character
    /// symbology — that vendor-specific encoding belongs behind the adapter seam.
    pub fn canonical_key(&self) -> String {
        format!(
            "{}:{}:{}:{}",
            self.underlying,
            self.expiration.yyyymmdd(),
            match self.right {
                OptionRight::Call => "C",
                OptionRight::Put => "P",
            },
            self.strike_minor,
        )
    }
}

impl fmt::Display for OptionContractIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.canonical_key())
    }
}

/// Fail-closed errors from building an [`OptionContractIdentity`] /
/// [`ExpirationDate`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OptionContractError {
    /// The underlying symbol was empty or whitespace-only.
    BlankUnderlying,
    /// The strike was not strictly positive (minor units).
    NonPositiveStrike { strike_minor: i64 },
    /// The expiration was not a real calendar date.
    InvalidExpiration {
        year: i32,
        month: u8,
        day: u8,
        detail: String,
    },
}

impl OptionContractError {
    /// A stable discriminator for the specific failure.
    pub const fn error_type(&self) -> &'static str {
        match self {
            Self::BlankUnderlying => "BlankUnderlying",
            Self::NonPositiveStrike { .. } => "NonPositiveStrike",
            Self::InvalidExpiration { .. } => "InvalidExpiration",
        }
    }
}

impl fmt::Display for OptionContractError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BlankUnderlying => write!(f, "option underlying must be non-empty"),
            Self::NonPositiveStrike { strike_minor } => write!(
                f,
                "option strike {strike_minor} minor units must be strictly positive"
            ),
            Self::InvalidExpiration {
                year,
                month,
                day,
                detail,
            } => write!(
                f,
                "option expiration {year:04}-{month:02}-{day:02} is invalid: {detail}"
            ),
        }
    }
}

impl std::error::Error for OptionContractError {}

/// One leg of a multi-leg options composite order: a full option contract, a
/// side, a quantity, and an order type. A leg is options-only **by type** (it
/// carries an [`OptionContractIdentity`]), so an equity leg cannot be
/// represented in a composite (SYS-4 scopes composites to options).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompositeOrderLeg {
    /// The option contract this leg trades.
    pub contract: OptionContractIdentity,
    /// Buy or sell (the leg's direction within the spread).
    pub side: OrderSide,
    /// The leg quantity (contracts; must be `> 0` — direction lives in `side`).
    pub quantity: i64,
    /// The leg's order type and any trigger/limit prices (minor units).
    pub order_type: OrderType,
}

impl CompositeOrderLeg {
    /// Construct a leg. Call [`CompositeOrderSubmission::validate`] before routing
    /// — quantity/price positivity is an intake-boundary check.
    pub fn new(
        contract: OptionContractIdentity,
        side: OrderSide,
        quantity: i64,
        order_type: OrderType,
    ) -> Self {
        Self {
            contract,
            side,
            quantity,
            order_type,
        }
    }
}

/// A multi-leg **options composite** order submitted and filled as one atomic
/// transaction (SYS-4). Carries the owning strategy and two or more
/// [`CompositeOrderLeg`]s. Because it routes as ONE order, an accepting adapter
/// returns exactly one [`OrderReceipt`](crate::OrderReceipt) — one broker order
/// id for the whole composite, never one per leg.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompositeOrderSubmission {
    pub strategy_id: StrategyId,
    pub legs: Vec<CompositeOrderLeg>,
}

impl CompositeOrderSubmission {
    /// Construct a composite submission from its legs. Validate before routing.
    pub fn new(strategy_id: StrategyId, legs: Vec<CompositeOrderLeg>) -> Self {
        Self { strategy_id, legs }
    }

    /// The number of legs.
    pub fn leg_count(&self) -> usize {
        self.legs.len()
    }

    /// Validate the composite before it is routed to the live broker or the
    /// internal simulation. Fails closed on:
    /// - fewer than two legs — a SYS-4 composite is two or more legs; a single
    ///   leg belongs in the single-leg [`OrderSubmission`](crate::OrderSubmission)
    ///   path;
    /// - any leg with a non-positive quantity;
    /// - any leg whose order type carries a non-positive trigger/limit price
    ///   (delegated to [`OrderType::validate_prices`], the shared price
    ///   authority, so live and paper cannot drift).
    ///
    /// Each leg's option contract is already well-formed by construction (the
    /// private-field [`OptionContractIdentity`]), so no contract re-check is
    /// needed here. Fails on the FIRST bad leg (index in the error) — a composite
    /// is atomic, so one bad leg rejects the whole order; nothing partial routes.
    pub fn validate(&self) -> Result<(), CompositeOrderError> {
        if self.legs.is_empty() {
            return Err(CompositeOrderError::EmptyComposite);
        }
        if self.legs.len() < 2 {
            return Err(CompositeOrderError::SingleLegComposite);
        }
        for (index, leg) in self.legs.iter().enumerate() {
            if leg.quantity <= 0 {
                return Err(CompositeOrderError::NonPositiveLegQuantity {
                    index,
                    quantity: leg.quantity,
                });
            }
            leg.order_type
                .validate_prices()
                .map_err(|source| CompositeOrderError::InvalidLegOrderType { index, source })?;
        }
        Ok(())
    }
}

/// Fail-closed validation errors from [`CompositeOrderSubmission::validate`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompositeOrderError {
    /// The composite carried no legs.
    EmptyComposite,
    /// The composite carried only one leg — a SYS-4 composite is two or more.
    SingleLegComposite,
    /// A leg carried a non-positive quantity.
    NonPositiveLegQuantity { index: usize, quantity: i64 },
    /// A leg's order type carried a non-positive trigger/limit price.
    InvalidLegOrderType {
        index: usize,
        source: OrderTypeError,
    },
}

impl CompositeOrderError {
    /// A stable discriminator for the specific failure (the structured order
    /// error's `error_type` when a composite is rejected).
    pub const fn error_type(&self) -> &'static str {
        match self {
            Self::EmptyComposite => "EmptyComposite",
            Self::SingleLegComposite => "SingleLegComposite",
            Self::NonPositiveLegQuantity { .. } => "NonPositiveLegQuantity",
            Self::InvalidLegOrderType { .. } => "InvalidLegOrderType",
        }
    }
}

impl fmt::Display for CompositeOrderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyComposite => {
                write!(f, "multi-leg composite order must carry at least one leg")
            }
            Self::SingleLegComposite => write!(
                f,
                "multi-leg composite order must carry at least two legs (SYS-4)"
            ),
            Self::NonPositiveLegQuantity { index, quantity } => write!(
                f,
                "composite leg {index} quantity {quantity} must be strictly positive"
            ),
            Self::InvalidLegOrderType { index, source } => {
                write!(
                    f,
                    "composite leg {index} has an invalid order type: {source}"
                )
            }
        }
    }
}

impl std::error::Error for CompositeOrderError {}

#[cfg(test)]
mod tests {
    use super::*;

    fn expiry() -> ExpirationDate {
        ExpirationDate::new(2024, 1, 19).expect("valid expiry")
    }

    fn contract(strike_minor: i64, right: OptionRight) -> OptionContractIdentity {
        OptionContractIdentity::new("AAPL", expiry(), strike_minor, right).expect("valid contract")
    }

    fn leg(strike_minor: i64, side: OrderSide, right: OptionRight) -> CompositeOrderLeg {
        CompositeOrderLeg::new(contract(strike_minor, right), side, 1, OrderType::Market)
    }

    #[test]
    fn option_right_wire_strings_are_stable() {
        assert_eq!(OptionRight::Call.as_str(), "CALL");
        assert_eq!(OptionRight::Put.as_str(), "PUT");
        assert_eq!(OptionRight::ALL_WIRE, ["CALL", "PUT"]);
    }

    #[test]
    fn expiration_rejects_impossible_dates() {
        assert!(ExpirationDate::new(2024, 2, 30).is_err(), "Feb 30");
        assert!(ExpirationDate::new(2023, 2, 29).is_err(), "Feb 29 non-leap");
        assert!(ExpirationDate::new(2024, 2, 29).is_ok(), "Feb 29 leap");
        assert!(ExpirationDate::new(2024, 13, 1).is_err(), "month 13");
        assert!(ExpirationDate::new(2024, 0, 1).is_err(), "month 0");
        assert!(ExpirationDate::new(2024, 4, 31).is_err(), "Apr 31");
        assert!(ExpirationDate::new(1800, 1, 1).is_err(), "year too old");
        assert_eq!(expiry().yyyymmdd(), "20240119");
    }

    #[test]
    fn contract_identity_normalizes_and_fails_closed() {
        let c = OptionContractIdentity::new("  aapl ", expiry(), 19_000_000, OptionRight::Call)
            .expect("valid");
        assert_eq!(c.underlying(), "AAPL");
        assert_eq!(c.canonical_key(), "AAPL:20240119:C:19000000");
        assert_eq!(
            OptionContractIdentity::new("", expiry(), 1, OptionRight::Call),
            Err(OptionContractError::BlankUnderlying)
        );
        assert_eq!(
            OptionContractIdentity::new("AAPL", expiry(), 0, OptionRight::Call),
            Err(OptionContractError::NonPositiveStrike { strike_minor: 0 })
        );
    }

    #[test]
    fn distinct_contracts_have_distinct_identities() {
        // Same underlying, different strike/right/expiration => different contract.
        let call_190 = contract(19_000_000, OptionRight::Call);
        let call_200 = contract(20_000_000, OptionRight::Call);
        let put_190 = contract(19_000_000, OptionRight::Put);
        assert_ne!(call_190, call_200);
        assert_ne!(call_190, put_190);
        assert_ne!(call_190.canonical_key(), call_200.canonical_key());
    }

    #[test]
    fn four_leg_composite_validates() {
        // An iron condor: four option legs on one underlying.
        let submission = CompositeOrderSubmission::new(
            StrategyId::new("live-1"),
            vec![
                leg(18_000_000, OrderSide::Sell, OptionRight::Put),
                leg(17_000_000, OrderSide::Buy, OptionRight::Put),
                leg(20_000_000, OrderSide::Sell, OptionRight::Call),
                leg(21_000_000, OrderSide::Buy, OptionRight::Call),
            ],
        );
        assert_eq!(submission.leg_count(), 4);
        assert!(submission.validate().is_ok());
    }

    #[test]
    fn empty_and_single_leg_composites_fail_closed() {
        assert_eq!(
            CompositeOrderSubmission::new(StrategyId::new("s"), vec![]).validate(),
            Err(CompositeOrderError::EmptyComposite)
        );
        assert_eq!(
            CompositeOrderSubmission::new(
                StrategyId::new("s"),
                vec![leg(19_000_000, OrderSide::Buy, OptionRight::Call)],
            )
            .validate(),
            Err(CompositeOrderError::SingleLegComposite)
        );
    }

    #[test]
    fn one_bad_leg_fails_the_whole_composite() {
        // A non-positive quantity on the second leg rejects the entire composite.
        let mut bad = leg(19_000_000, OrderSide::Sell, OptionRight::Call);
        bad.quantity = 0;
        let submission = CompositeOrderSubmission::new(
            StrategyId::new("s"),
            vec![leg(19_000_000, OrderSide::Buy, OptionRight::Call), bad],
        );
        assert_eq!(
            submission.validate(),
            Err(CompositeOrderError::NonPositiveLegQuantity {
                index: 1,
                quantity: 0
            })
        );
    }

    #[test]
    fn bad_leg_price_fails_closed_via_shared_authority() {
        let bad_price_leg = CompositeOrderLeg::new(
            contract(19_000_000, OptionRight::Call),
            OrderSide::Buy,
            1,
            OrderType::Limit {
                limit_price_minor: 0,
            },
        );
        let submission = CompositeOrderSubmission::new(
            StrategyId::new("s"),
            vec![
                leg(19_000_000, OrderSide::Buy, OptionRight::Call),
                bad_price_leg,
            ],
        );
        match submission.validate() {
            Err(CompositeOrderError::InvalidLegOrderType { index: 1, source }) => {
                assert_eq!(
                    source,
                    OrderTypeError::NonPositiveLimitPrice { price_minor: 0 }
                );
            }
            other => panic!("expected InvalidLegOrderType, got {other:?}"),
        }
    }
}
