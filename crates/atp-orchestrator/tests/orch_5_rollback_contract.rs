//! SRS-ORCH-005 / SyRS SYS-80 / NFR-S2 — rollback to the previous deployed
//! strategy version.
//!
//! L7 domain (safety) integration test driving the `RetainingVersionRegistry`
//! concrete retention, a fixed `LiveStrategyProbe`, and the
//! `StrategyOrchestrator::rollback` gate.
//!
//! Post-conditions exercised here:
//!   * SYS-80 retention: recording a second version retains the first as
//!     `previous`; a third record drops the first (bounded one-deep — the
//!     requirement names "the previous version"); a same-hash redeploy does
//!     NOT make a version its own rollback target.
//!   * A PAPER (non-live) rollback succeeds WITHOUT a confirmation token
//!     (the AC scopes the confirmation control to the live strategy) — the
//!     non-vacuity check for the live guard.
//!   * A rollback re-records the previous version as current with a fresh
//!     timestamp, so a SECOND rollback returns to the rolled-back-from
//!     version (roll forward).
//!   * Every refusal arm is side-effect free (proven with a write-forbidding
//!     registry wrapper): malformed target hash, never-deployed, no previous
//!     version, target != retained previous (including target == current),
//!     live without confirmation, live with a token bound to ANOTHER
//!     strategy, live-probe failure (fail closed — unprovable live status
//!     never waives NFR-S2), and an empty operator acknowledgement is not a
//!     constructible confirmation at all.
//!   * A registry `record` failure PROPAGATES (`RegistryFailed`) — unlike
//!     `launch`'s best-effort record, the write here IS the rollback.

use atp_orchestrator::{
    DeployedVersionRegistry, DeployedVersionRegistryError, HotSwapSideEffectError,
    LiveStrategyProbe, RetainedDeployedVersionRegistry, RetainingVersionRegistry,
    RollbackConfirmation, RollbackError, StrategyOrchestrator,
};
use atp_types::{DeployedVersion, LiveStrategyState, SourceHash, StrategyId};
use std::cell::Cell;

const HASH_V1: &str = "sha256:1111111111111111111111111111111111111111111111111111111111111111";
const HASH_V2: &str = "sha256:2222222222222222222222222222222222222222222222222222222222222222";
const HASH_V3: &str = "sha256:3333333333333333333333333333333333333333333333333333333333333333";

fn version(hash: &str, at: u64) -> DeployedVersion {
    DeployedVersion::new(SourceHash::new(hash), at)
}

/// A probe with a fixed answer (mirrors the resv_3 fixed-probe idiom).
struct FixedLiveProbe {
    live: Option<&'static str>,
    degraded: bool,
}

impl LiveStrategyProbe for FixedLiveProbe {
    fn current_live(&self) -> Result<Option<LiveStrategyState>, HotSwapSideEffectError> {
        if self.degraded {
            return Err(HotSwapSideEffectError::new("live registry unreachable"));
        }
        Ok(self.live.map(|id| LiveStrategyState {
            strategy_id: StrategyId::new(id),
            drawdown_bps: 0,
        }))
    }
}

const NO_LIVE: FixedLiveProbe = FixedLiveProbe {
    live: None,
    degraded: false,
};

/// A registry wrapper that FORBIDS writes — proves a refusal arm never reaches
/// `record` (the side-effect-free guarantee).
struct WriteForbiddenRegistry<'a> {
    inner: &'a RetainingVersionRegistry,
    write_attempts: Cell<u32>,
}

impl<'a> WriteForbiddenRegistry<'a> {
    fn new(inner: &'a RetainingVersionRegistry) -> Self {
        Self {
            inner,
            write_attempts: Cell::new(0),
        }
    }
}

impl DeployedVersionRegistry for WriteForbiddenRegistry<'_> {
    fn record(
        &self,
        _strategy_id: &StrategyId,
        _version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError> {
        self.write_attempts.set(self.write_attempts.get() + 1);
        panic!("a refused rollback must never reach the registry write");
    }

    fn lookup(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        self.inner.lookup(strategy_id)
    }
}

impl RetainedDeployedVersionRegistry for WriteForbiddenRegistry<'_> {
    fn previous(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        self.inner.previous(strategy_id)
    }
}

/// A registry whose writes always fail — proves `RegistryFailed` propagates.
struct FailingWriteRegistry<'a> {
    inner: &'a RetainingVersionRegistry,
}

impl DeployedVersionRegistry for FailingWriteRegistry<'_> {
    fn record(
        &self,
        _strategy_id: &StrategyId,
        _version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError> {
        Err(DeployedVersionRegistryError::new("durable store offline"))
    }

    fn lookup(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        self.inner.lookup(strategy_id)
    }
}

impl RetainedDeployedVersionRegistry for FailingWriteRegistry<'_> {
    fn previous(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        self.inner.previous(strategy_id)
    }
}

fn seeded_registry(strategy: &str) -> RetainingVersionRegistry {
    let registry = RetainingVersionRegistry::new();
    let id = StrategyId::new(strategy);
    registry.record(&id, version(HASH_V1, 100)).unwrap();
    registry.record(&id, version(HASH_V2, 200)).unwrap();
    registry
}

#[test]
fn recording_a_second_version_retains_the_first_and_a_third_drops_it() {
    let registry = RetainingVersionRegistry::new();
    let id = StrategyId::new("alpha-1");
    registry.record(&id, version(HASH_V1, 100)).unwrap();
    assert_eq!(
        registry.previous(&id).unwrap(),
        None,
        "first deploy replaced nothing"
    );
    registry.record(&id, version(HASH_V2, 200)).unwrap();
    assert_eq!(registry.lookup(&id).unwrap(), Some(version(HASH_V2, 200)));
    assert_eq!(
        registry.previous(&id).unwrap(),
        Some(version(HASH_V1, 100)),
        "SYS-80: the replaced version is retained"
    );
    registry.record(&id, version(HASH_V3, 300)).unwrap();
    assert_eq!(
        registry.previous(&id).unwrap(),
        Some(version(HASH_V2, 200)),
        "retention is bounded at ONE prior version (the requirement names 'the previous version')"
    );
}

#[test]
fn a_same_hash_redeploy_never_becomes_its_own_rollback_target() {
    let registry = RetainingVersionRegistry::new();
    let id = StrategyId::new("alpha-1");
    registry.record(&id, version(HASH_V1, 100)).unwrap();
    registry.record(&id, version(HASH_V2, 200)).unwrap();
    // Redeploy the SAME v2 hash (e.g. a container restart re-recording).
    registry.record(&id, version(HASH_V2, 250)).unwrap();
    assert_eq!(
        registry.previous(&id).unwrap(),
        Some(version(HASH_V1, 100)),
        "a same-hash redeploy must not overwrite the genuine previous version"
    );
    assert_eq!(registry.lookup(&id).unwrap(), Some(version(HASH_V2, 250)));
}

#[test]
fn paper_rollback_succeeds_without_confirmation_and_swaps_the_pair() {
    // Non-vacuity for the live guard: the confirmation control is scoped to the
    // LIVE strategy; a paper rollback needs no token.
    let registry = seeded_registry("alpha-1");
    let id = StrategyId::new("alpha-1");
    let outcome = StrategyOrchestrator
        .rollback(
            id.clone(),
            SourceHash::new(HASH_V1),
            None,
            &registry,
            &NO_LIVE,
            300,
        )
        .expect("paper rollback needs no confirmation");
    assert_eq!(outcome.rolled_back_from, SourceHash::new(HASH_V2));
    assert_eq!(
        outcome.rolled_back_to,
        version(HASH_V1, 300),
        "fresh timestamp"
    );
    assert!(!outcome.was_live);
    // The retaining record swapped the pair: current is v1 (fresh), previous is v2.
    assert_eq!(registry.lookup(&id).unwrap(), Some(version(HASH_V1, 300)));
    assert_eq!(registry.previous(&id).unwrap(), Some(version(HASH_V2, 200)));

    // A SECOND rollback (naming v2) rolls forward.
    let back = StrategyOrchestrator
        .rollback(
            id.clone(),
            SourceHash::new(HASH_V2),
            None,
            &registry,
            &NO_LIVE,
            400,
        )
        .expect("second rollback returns to the rolled-back-from version");
    assert_eq!(back.rolled_back_to, version(HASH_V2, 400));
    assert_eq!(registry.previous(&id).unwrap(), Some(version(HASH_V1, 300)));
}

#[test]
fn live_rollback_requires_a_strategy_bound_confirmation() {
    let registry = seeded_registry("alpha-1");
    let id = StrategyId::new("alpha-1");
    let live_probe = FixedLiveProbe {
        live: Some("alpha-1"),
        degraded: false,
    };

    // Without a token: refused, no write (write-forbidding wrapper would panic).
    let guarded = WriteForbiddenRegistry::new(&registry);
    let refused = StrategyOrchestrator.rollback(
        id.clone(),
        SourceHash::new(HASH_V1),
        None,
        &guarded,
        &live_probe,
        300,
    );
    assert_eq!(refused.unwrap_err(), RollbackError::MissingConfirmation);
    assert_eq!(guarded.write_attempts.get(), 0);

    // A token bound to ANOTHER strategy: refused (no cross-strategy replay).
    let foreign = RollbackConfirmation::from_operator(
        StrategyId::new("other-9"),
        "operator confirmed rollback of other-9",
    )
    .unwrap();
    let mismatched = StrategyOrchestrator.rollback(
        id.clone(),
        SourceHash::new(HASH_V1),
        Some(foreign),
        &WriteForbiddenRegistry::new(&registry),
        &live_probe,
        300,
    );
    assert!(matches!(
        mismatched.unwrap_err(),
        RollbackError::ConfirmationMismatch { .. }
    ));

    // With the correctly-bound token: the live rollback lands.
    let token = RollbackConfirmation::from_operator(
        id.clone(),
        "operator confirmed rollback of alpha-1 via CLI",
    )
    .unwrap();
    let outcome = StrategyOrchestrator
        .rollback(
            id.clone(),
            SourceHash::new(HASH_V1),
            Some(token),
            &registry,
            &live_probe,
            300,
        )
        .expect("confirmed live rollback lands");
    assert!(outcome.was_live);
    assert_eq!(registry.lookup(&id).unwrap(), Some(version(HASH_V1, 300)));
}

#[test]
fn an_empty_acknowledgement_is_not_a_constructible_confirmation() {
    // NFR-S2 parity with LiveDesignationConfirmation: the token cannot exist
    // without a non-empty operator acknowledgement.
    for empty in ["", "   ", "\t\n"] {
        assert_eq!(
            RollbackConfirmation::from_operator(StrategyId::new("alpha-1"), empty).unwrap_err(),
            RollbackError::MissingConfirmation,
            "acknowledgement {empty:?} must be rejected"
        );
    }
}

#[test]
fn refusal_arms_are_side_effect_free() {
    let registry = seeded_registry("alpha-1");
    let id = StrategyId::new("alpha-1");

    // Malformed target hash (validated before any registry read).
    let malformed = StrategyOrchestrator.rollback(
        id.clone(),
        SourceHash::new("md5:nope"),
        None,
        &WriteForbiddenRegistry::new(&registry),
        &NO_LIVE,
        300,
    );
    assert!(matches!(
        malformed.unwrap_err(),
        RollbackError::TargetHashInvalid(_)
    ));

    // Never deployed.
    let never = StrategyOrchestrator.rollback(
        StrategyId::new("ghost-7"),
        SourceHash::new(HASH_V1),
        None,
        &WriteForbiddenRegistry::new(&registry),
        &NO_LIVE,
        300,
    );
    assert!(matches!(
        never.unwrap_err(),
        RollbackError::NeverDeployed { .. }
    ));

    // Deployed once: no previous version to roll back to (inert).
    let single = RetainingVersionRegistry::new();
    single
        .record(&StrategyId::new("solo-1"), version(HASH_V1, 100))
        .unwrap();
    let inert = StrategyOrchestrator.rollback(
        StrategyId::new("solo-1"),
        SourceHash::new(HASH_V1),
        None,
        &WriteForbiddenRegistry::new(&single),
        &NO_LIVE,
        300,
    );
    assert!(matches!(
        inert.unwrap_err(),
        RollbackError::NoPreviousVersion { .. }
    ));

    // Target mismatch — including naming the CURRENT version (not a rollback).
    for wrong in [HASH_V3, HASH_V2] {
        let mismatch = StrategyOrchestrator.rollback(
            id.clone(),
            SourceHash::new(wrong),
            None,
            &WriteForbiddenRegistry::new(&registry),
            &NO_LIVE,
            300,
        );
        match mismatch.unwrap_err() {
            RollbackError::TargetMismatch {
                requested,
                retained_previous,
            } => {
                assert_eq!(requested, SourceHash::new(wrong));
                assert_eq!(
                    retained_previous,
                    SourceHash::new(HASH_V1),
                    "the refusal names the retained hash so the operator can retry correctly"
                );
            }
            other => panic!("expected TargetMismatch, got {other:?}"),
        }
    }

    // Degraded live probe: unprovable live status refuses (fail closed) — even
    // WITH a valid confirmation token, because the gate cannot bind the token
    // to a proven live identity.
    let degraded = FixedLiveProbe {
        live: None,
        degraded: true,
    };
    let token =
        RollbackConfirmation::from_operator(id.clone(), "operator confirmed rollback").unwrap();
    let unprovable = StrategyOrchestrator.rollback(
        id.clone(),
        SourceHash::new(HASH_V1),
        Some(token),
        &WriteForbiddenRegistry::new(&registry),
        &degraded,
        300,
    );
    assert!(matches!(
        unprovable.unwrap_err(),
        RollbackError::LiveStatusUnavailable { .. }
    ));
}

#[test]
fn a_registry_write_failure_propagates_as_registry_failed() {
    // Unlike launch's best-effort record: the write IS the rollback.
    let registry = seeded_registry("alpha-1");
    let failing = FailingWriteRegistry { inner: &registry };
    let failed = StrategyOrchestrator.rollback(
        StrategyId::new("alpha-1"),
        SourceHash::new(HASH_V1),
        None,
        &failing,
        &NO_LIVE,
        300,
    );
    assert!(matches!(
        failed.unwrap_err(),
        RollbackError::RegistryFailed(_)
    ));
    // The inner registry is untouched — the failed write changed nothing.
    assert_eq!(
        registry.lookup(&StrategyId::new("alpha-1")).unwrap(),
        Some(version(HASH_V2, 200))
    );
}

#[test]
fn a_rollback_of_a_different_live_strategy_needs_no_confirmation() {
    // SOME strategy is live, but not the one being rolled back: the AC scopes
    // the confirmation control to "rollback of the live strategy".
    let registry = seeded_registry("alpha-1");
    let other_live = FixedLiveProbe {
        live: Some("other-9"),
        degraded: false,
    };
    let outcome = StrategyOrchestrator
        .rollback(
            StrategyId::new("alpha-1"),
            SourceHash::new(HASH_V1),
            None,
            &registry,
            &other_live,
            300,
        )
        .expect("a non-live strategy's rollback needs no confirmation");
    assert!(!outcome.was_live);
}
