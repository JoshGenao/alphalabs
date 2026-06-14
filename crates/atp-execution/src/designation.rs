//! SRS-EXE-001 — live-designation authority (SyRS SYS-1 / SYS-2a / SYS-2c /
//! SYS-2d / AC-15; NFR-S2; StRS SN-1.01 / SN-1.06 / SN-1.11).
//!
//! AGENTS.md core constraint: *"Exactly one strategy may execute against the IB
//! live account at any time."* [`ExecutionEngine::submit_live_order`] decides
//! whether a *Live* submission may reach the broker (ERR-1/2/3 — mode,
//! connectivity, freshness), but it **trusts the caller-passed
//! [`atp_types::StrategyMode`]**: nothing in that gate establishes *which*
//! strategy is the single designated live strategy, requires *explicit
//! confirmation* to designate one (SYS-2d / NFR-S2), or enforces that *at most
//! one* is designated at a time (SYS-2a). This module is that missing authority.
//!
//! [`LiveDesignation`] holds the single designated live [`StrategyId`]. Its
//! [`authority_for`](LiveDesignation::authority_for) decision is the source of
//! truth [`ExecutionEngine::route_order`](crate::ExecutionEngine::route_order)
//! consults **before any broker / connectivity / freshness port**, so only the
//! designated strategy can ever reach IB. Designation requires a
//! [`LiveDesignationConfirmation`] token bound to the specific strategy
//! (SYS-2d), and a second designation is refused until the current live strategy
//! is [`demote`](LiveDesignation::demote)d (SYS-2a).
//!
//! **Scope — in-process designation state machine.** The deterministic
//! invariants (explicit, strategy-bound confirmation required; exactly one
//! designation at a time; only the designated strategy is `Authorized`) are
//! proven offline. The following are DEFERRED to the named runtime owners
//! (`architecture/runtime_services.json` `live_designation_contract.deferred[]`):
//! the operator surface that originates the confirmation (dashboard/CLI/REST,
//! SYS-2c → SRS-API-001); the real IB order dispatch plus the *sole-entry*
//! wiring that makes `submit_live_order` unreachable except through
//! `route_order` (SRS-EXE-006 / SRS-ORCH-*); durable / cross-process single-live
//! state and the operator promote/demote lifecycle that drives
//! `designate`/`demote` (SRS-RESV-002..006 / SRS-ORCH-*); and the NFR-P1 / AC-15
//! live-order latency proof (SRS-PERF-001). SRS-EXE-001 stays `passes:false`
//! until those land.

use atp_types::StrategyId;
use std::fmt;

/// SYS-2d / NFR-S2 explicit-confirmation token, bound to the strategy it
/// confirms. Designating a strategy as the single live strategy is a deliberate
/// operator action that must not be reachable by a default or implicit value:
/// this type has a **private** field, **no `Default`**, and **no public
/// boolean**, so a [`LiveDesignation::designate`] call cannot be satisfied with
/// `true` or a zero value. The only constructor, [`from_operator`], records the
/// operator-supplied acknowledgement captured at the (deferred) operator surface
/// (SYS-2c) and the specific [`StrategyId`] being confirmed — so a token
/// confirmed for strategy A cannot be replayed to designate strategy B
/// ([`LiveDesignationError::ConfirmationMismatch`]).
///
/// [`from_operator`]: LiveDesignationConfirmation::from_operator
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiveDesignationConfirmation {
    strategy_id: StrategyId,
    operator_acknowledgement: String,
}

impl LiveDesignationConfirmation {
    /// Build the explicit-confirmation token for `strategy_id` from the operator
    /// acknowledgement captured at the (deferred SYS-2c) operator surface. The
    /// acknowledgement must be non-empty — an empty acknowledgement is not an
    /// explicit confirmation ([`LiveDesignationError::MissingConfirmation`]).
    pub fn from_operator(
        strategy_id: StrategyId,
        operator_acknowledgement: impl Into<String>,
    ) -> Result<Self, LiveDesignationError> {
        let operator_acknowledgement = operator_acknowledgement.into();
        if operator_acknowledgement.trim().is_empty() {
            return Err(LiveDesignationError::MissingConfirmation);
        }
        Ok(Self {
            strategy_id,
            operator_acknowledgement,
        })
    }

    /// The strategy this confirmation authorizes for live designation.
    pub fn confirmed_strategy(&self) -> &StrategyId {
        &self.strategy_id
    }

    /// The operator acknowledgement phrase carried for audit.
    pub fn operator_acknowledgement(&self) -> &str {
        &self.operator_acknowledgement
    }
}

/// The pure routing-authority decision for a strategy: whether its orders may
/// proceed to the live execution gate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LiveRoutingDecision {
    /// The strategy is the single designated live strategy — its orders may
    /// proceed to the inner ERR-1/2/3 live gate.
    Authorized,
    /// The strategy is not the designated live strategy — every IB-bound attempt
    /// must be rejected synchronously with a structured error (SYS-2d, ERR-1).
    NotDesignated,
}

/// Failure surface for the live-designation authority (SRS-EXE-001).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LiveDesignationError {
    /// SYS-2d / NFR-S2: an explicit confirmation was required but the supplied
    /// operator acknowledgement was empty.
    MissingConfirmation,
    /// SYS-2d: the confirmation token authorizes a *different* strategy than the
    /// one being designated — a confirmation for A cannot designate B.
    ConfirmationMismatch {
        confirmed: StrategyId,
        requested: StrategyId,
    },
    /// SYS-2a: a different strategy is already designated live; it must be
    /// demoted first (the deferred Hot-Swap demotion-before-promotion lifecycle,
    /// SRS-RESV-004).
    AlreadyDesignated {
        current: StrategyId,
        requested: StrategyId,
    },
    /// [`demote`](LiveDesignation::demote) was asked to clear a strategy that is
    /// not the currently designated live strategy (including when nothing is
    /// designated).
    NotDesignated { requested: StrategyId },
}

impl fmt::Display for LiveDesignationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingConfirmation => write!(
                formatter,
                "SRS-EXE-001: live designation requires an explicit operator \
                 confirmation (SyRS SYS-2d / NFR-S2)",
            ),
            Self::ConfirmationMismatch {
                confirmed,
                requested,
            } => write!(
                formatter,
                "SRS-EXE-001: confirmation authorizes strategy `{}` but designation \
                 was requested for `{}` (SyRS SYS-2d)",
                confirmed.as_str(),
                requested.as_str(),
            ),
            Self::AlreadyDesignated { current, requested } => write!(
                formatter,
                "SRS-EXE-001: strategy `{}` is already the designated live strategy; \
                 demote it before designating `{}` (SyRS SYS-2a)",
                current.as_str(),
                requested.as_str(),
            ),
            Self::NotDesignated { requested } => write!(
                formatter,
                "SRS-EXE-001: strategy `{}` is not the currently designated live \
                 strategy and cannot be demoted",
                requested.as_str(),
            ),
        }
    }
}

impl std::error::Error for LiveDesignationError {}

/// The execution-layer live-designation authority. Holds the single designated
/// live [`StrategyId`] (SYS-2a: at most one at a time).
/// [`authority_for`](Self::authority_for) is the pure routing decision
/// [`ExecutionEngine::route_order`](crate::ExecutionEngine::route_order)
/// consults. The empty registry (no designation) is the safe default: until an
/// operator explicitly designates one, **no** strategy is authorized to route to
/// IB.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct LiveDesignation {
    designated: Option<StrategyId>,
}

impl LiveDesignation {
    /// A registry with no designated live strategy.
    pub fn new() -> Self {
        Self { designated: None }
    }

    /// Designate `strategy_id` as the single live strategy.
    ///
    /// SYS-2d / NFR-S2: an explicit [`LiveDesignationConfirmation`] is required
    /// and must be bound to `strategy_id` (a confirmation for another strategy
    /// is refused with [`LiveDesignationError::ConfirmationMismatch`]).
    ///
    /// SYS-2a: rejects with [`LiveDesignationError::AlreadyDesignated`] if a
    /// *different* strategy is already designated — the caller must
    /// [`demote`](Self::demote) the current live strategy first. Re-designating
    /// the strategy that is *already* designated is idempotent (`Ok`), so a
    /// retried confirmation does not spuriously fail.
    pub fn designate(
        &mut self,
        strategy_id: StrategyId,
        confirmation: LiveDesignationConfirmation,
    ) -> Result<(), LiveDesignationError> {
        if confirmation.strategy_id != strategy_id {
            return Err(LiveDesignationError::ConfirmationMismatch {
                confirmed: confirmation.strategy_id,
                requested: strategy_id,
            });
        }
        match &self.designated {
            Some(current) if current == &strategy_id => Ok(()),
            Some(current) => Err(LiveDesignationError::AlreadyDesignated {
                current: current.clone(),
                requested: strategy_id,
            }),
            None => {
                self.designated = Some(strategy_id);
                Ok(())
            }
        }
    }

    /// Demote `strategy_id` from live, clearing the designation. Rejects with
    /// [`LiveDesignationError::NotDesignated`] if `strategy_id` is not the
    /// currently designated strategy (including when nothing is designated) —
    /// demoting a strategy that is not live is a caller error, not a silent
    /// no-op.
    pub fn demote(&mut self, strategy_id: &StrategyId) -> Result<(), LiveDesignationError> {
        match &self.designated {
            Some(current) if current == strategy_id => {
                self.designated = None;
                Ok(())
            }
            _ => Err(LiveDesignationError::NotDesignated {
                requested: strategy_id.clone(),
            }),
        }
    }

    /// The currently designated live strategy, if any.
    pub fn designated(&self) -> Option<&StrategyId> {
        self.designated.as_ref()
    }

    /// The pure routing-authority decision for `strategy_id`:
    /// [`Authorized`](LiveRoutingDecision::Authorized) iff it is the single
    /// designated live strategy, else
    /// [`NotDesignated`](LiveRoutingDecision::NotDesignated). This is the SYS-2d
    /// source of truth `route_order` consults before any broker / connectivity /
    /// freshness port.
    pub fn authority_for(&self, strategy_id: &StrategyId) -> LiveRoutingDecision {
        match &self.designated {
            Some(current) if current == strategy_id => LiveRoutingDecision::Authorized,
            _ => LiveRoutingDecision::NotDesignated,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn confirm(strategy: &str) -> LiveDesignationConfirmation {
        LiveDesignationConfirmation::from_operator(
            StrategyId::new(strategy),
            "operator confirmed live designation",
        )
        .expect("non-empty acknowledgement must yield a confirmation token")
    }

    #[test]
    fn empty_acknowledgement_is_not_an_explicit_confirmation() {
        let err = LiveDesignationConfirmation::from_operator(StrategyId::new("live-a"), "   ")
            .expect_err("an empty/whitespace acknowledgement must be refused");
        assert_eq!(err, LiveDesignationError::MissingConfirmation);
    }

    #[test]
    fn fresh_registry_designates_nobody() {
        let designation = LiveDesignation::new();
        assert!(designation.designated().is_none());
        assert_eq!(
            designation.authority_for(&StrategyId::new("anyone")),
            LiveRoutingDecision::NotDesignated,
        );
        // Default and new() agree.
        assert_eq!(LiveDesignation::default(), designation);
    }

    #[test]
    fn designate_authorizes_only_the_designated_strategy() {
        let mut designation = LiveDesignation::new();
        designation
            .designate(StrategyId::new("live-a"), confirm("live-a"))
            .expect("first designation with a matching confirmation succeeds");

        assert_eq!(
            designation.designated().map(StrategyId::as_str),
            Some("live-a")
        );
        assert_eq!(
            designation.authority_for(&StrategyId::new("live-a")),
            LiveRoutingDecision::Authorized,
        );
        assert_eq!(
            designation.authority_for(&StrategyId::new("paper-7")),
            LiveRoutingDecision::NotDesignated,
        );
    }

    #[test]
    fn confirmation_for_another_strategy_is_refused() {
        let mut designation = LiveDesignation::new();
        let err = designation
            .designate(StrategyId::new("live-b"), confirm("live-a"))
            .expect_err("a confirmation bound to live-a cannot designate live-b");
        assert_eq!(
            err,
            LiveDesignationError::ConfirmationMismatch {
                confirmed: StrategyId::new("live-a"),
                requested: StrategyId::new("live-b"),
            },
        );
        // The failed designation left the registry untouched.
        assert!(designation.designated().is_none());
    }

    #[test]
    fn exactly_one_strategy_may_be_designated_at_a_time() {
        let mut designation = LiveDesignation::new();
        designation
            .designate(StrategyId::new("live-a"), confirm("live-a"))
            .expect("first designation succeeds");

        let err = designation
            .designate(StrategyId::new("live-b"), confirm("live-b"))
            .expect_err("a second concurrent designation must be refused (SYS-2a)");
        assert_eq!(
            err,
            LiveDesignationError::AlreadyDesignated {
                current: StrategyId::new("live-a"),
                requested: StrategyId::new("live-b"),
            },
        );
        // live-a is still the only authorized strategy; live-b never became live.
        assert_eq!(
            designation.authority_for(&StrategyId::new("live-a")),
            LiveRoutingDecision::Authorized,
        );
        assert_eq!(
            designation.authority_for(&StrategyId::new("live-b")),
            LiveRoutingDecision::NotDesignated,
        );
    }

    #[test]
    fn re_designating_the_same_strategy_is_idempotent() {
        let mut designation = LiveDesignation::new();
        designation
            .designate(StrategyId::new("live-a"), confirm("live-a"))
            .expect("first designation succeeds");
        designation
            .designate(StrategyId::new("live-a"), confirm("live-a"))
            .expect("re-designating the already-live strategy is a no-op success");
        assert_eq!(
            designation.designated().map(StrategyId::as_str),
            Some("live-a")
        );
    }

    #[test]
    fn demote_clears_the_designation_and_allows_re_designation() {
        let mut designation = LiveDesignation::new();
        designation
            .designate(StrategyId::new("live-a"), confirm("live-a"))
            .expect("first designation succeeds");

        designation
            .demote(&StrategyId::new("live-a"))
            .expect("demoting the designated strategy succeeds");
        assert!(designation.designated().is_none());
        assert_eq!(
            designation.authority_for(&StrategyId::new("live-a")),
            LiveRoutingDecision::NotDesignated,
        );

        // Demotion-before-promotion: a different strategy can now be designated.
        designation
            .designate(StrategyId::new("live-b"), confirm("live-b"))
            .expect("after demotion a new strategy may be designated");
        assert_eq!(
            designation.authority_for(&StrategyId::new("live-b")),
            LiveRoutingDecision::Authorized,
        );
    }

    #[test]
    fn demoting_a_non_designated_strategy_is_an_error() {
        let mut empty = LiveDesignation::new();
        assert_eq!(
            empty.demote(&StrategyId::new("live-a")),
            Err(LiveDesignationError::NotDesignated {
                requested: StrategyId::new("live-a"),
            }),
        );

        let mut designation = LiveDesignation::new();
        designation
            .designate(StrategyId::new("live-a"), confirm("live-a"))
            .expect("first designation succeeds");
        assert_eq!(
            designation.demote(&StrategyId::new("paper-3")),
            Err(LiveDesignationError::NotDesignated {
                requested: StrategyId::new("paper-3"),
            }),
        );
        // The spurious demote did not clear the real designation.
        assert_eq!(
            designation.designated().map(StrategyId::as_str),
            Some("live-a")
        );
    }
}
