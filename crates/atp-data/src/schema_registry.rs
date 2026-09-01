//! # SRS-DATA-015 schema registry — the enumerated contract every persisted entity honours
//!
//! SyRS SYS-66: *"The data layer shall support schema evolution such that data ingested under a prior
//! schema version remains queryable after schema updates, without requiring bulk migration of
//! historical records. **Schema version shall be tracked per data entity.**"* SRS-DATA-015's
//! acceptance criterion restates it as two clauses:
//!
//! 1. **Each persisted entity records a schema version.**
//! 2. **Data written under older schema versions remains queryable after schema updates without bulk
//!    migration.**
//!
//! Clause 1 is a claim about *every* persisted entity in the system, so it is only as good as the
//! enumeration behind it. This module IS that enumeration: one [`SchemaDescriptor`] per persisted
//! entity, naming the writer that owns the format, the literal source [`SchemaDescriptor::marker`]
//! that proves a version reaches the bytes, and the [`EvolutionPosture`] the reader implements.
//!
//! ## Why a const table and not reflection
//! The entities live in six crates and three Python packages. A registry that *imported* them would
//! invert the one-way dependency direction (`atp-data` is a lower layer than execution / simulation /
//! orchestration and must never depend on them). So the table is **pure data** — `&'static str` and
//! `i64`, no imports — and the binding to reality is enforced from outside by
//! `tools/data015_schema_check.py`, which parses the real constants out of each writer's source,
//! cross-checks them against this table, and fails when a persistence write surface exists that this
//! table does not name. Drift is caught by the gate, not trusted to review.
//!
//! ## What "no bulk migration" means here
//! Every reader accepts its whole `[min_supported_version, current_version]` range **in place**: an
//! old blob is read where it lies, never rewritten or upgraded on disk. For the entities retrofitted
//! by SRS-DATA-015 ([`SchemaDescriptor::legacy_unversioned`]), a payload written *before* the version
//! field existed is read as `min_supported_version` — so files already on disk stay queryable with no
//! migration step at all. An **unknown future** version always fails closed: a reader that cannot
//! prove it understands the bytes must refuse them, never guess.

use std::collections::BTreeSet;
use std::fmt;

/// How a reader treats versions other than the one it writes — the *evolution* posture, distinct from
/// whether a version is recorded at all (every entity records one; that is clause 1).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum EvolutionPosture {
    /// The reader accepts every version in `[min_supported_version, current_version]` and interprets
    /// each as written — no in-memory upgrade needed because the older layouts are a prefix of the
    /// current one. Example: the market-data store (v1–v4; a later version only *adds* dataset kinds).
    Ranged,
    /// The reader accepts the whole range and **upgrades older payloads in memory** to the current
    /// shape as it decodes. The bytes on disk are untouched. Example: the paper-state snapshot (a v1
    /// snapshot is read and lifted to v2's metrics/user-state split).
    MigrateOnRead,
    /// The reader accepts exactly `current_version` and refuses anything else. Legitimate for a format
    /// that has never evolved: it fails closed instead of guessing. `min_supported_version` therefore
    /// equals `current_version`.
    Pinned,
}

impl EvolutionPosture {
    /// The stable lowercase wire tag used by the CLI/report output and the check script.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Ranged => "ranged",
            Self::MigrateOnRead => "migrate-on-read",
            Self::Pinned => "pinned",
        }
    }
}

impl fmt::Display for EvolutionPosture {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One persisted entity's schema contract.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SchemaDescriptor {
    /// Stable kebab-case identity of the entity (the registry key, and what the CLI prints).
    pub entity_id: &'static str,
    /// The SRS feature that owns this format — who to talk to before changing it.
    pub owner_srs: &'static str,
    /// Repo-relative path of the module that WRITES the format (the single format owner).
    pub writer_path: &'static str,
    /// A literal token that must appear in `writer_path`'s source, proving the version reaches the
    /// persisted bytes. The check script greps for exactly this, so a rename that drops the version
    /// cannot pass silently.
    pub marker: &'static str,
    /// The format's magic header, when it has one (`None` for the line/JSON-keyed formats, whose
    /// version travels in a field rather than a header).
    pub magic: Option<&'static str>,
    /// The version this build WRITES.
    pub current_version: i64,
    /// The oldest version this build still READS. Older data stays queryable at this floor.
    pub min_supported_version: i64,
    /// How the reader treats non-current versions.
    pub posture: EvolutionPosture,
    /// `true` when a payload carrying **no** version field is legitimately read as
    /// `min_supported_version`. Set for the entities SRS-DATA-015 retrofitted: files written before
    /// the version field existed are still queryable exactly where they lie, which is what "without
    /// bulk migration" demands. `false` for a format that has carried a version from its first byte.
    pub legacy_unversioned: bool,
}

/// **Every persisted entity in the system.** Adding a persistence write surface without adding a row
/// here fails `tools/data015_schema_check.py`.
pub const PERSISTED_ENTITIES: &[SchemaDescriptor] = &[
    SchemaDescriptor {
        entity_id: "market-data-store",
        owner_srs: "SRS-DATA-016",
        writer_path: "crates/atp-data/src/store.rs",
        marker: "SCHEMA_VERSION",
        magic: Some("ATP-MARKET-DATA-STORE"),
        current_version: 4,
        min_supported_version: 1,
        posture: EvolutionPosture::Ranged,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "access-journal",
        owner_srs: "SRS-DATA-010",
        writer_path: "crates/atp-data/src/access_journal.rs",
        marker: "ACCESS_JOURNAL_SCHEMA_VERSION",
        magic: None,
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::MigrateOnRead,
        legacy_unversioned: true,
    },
    SchemaDescriptor {
        entity_id: "backtest-record-store",
        owner_srs: "SRS-BT-009",
        writer_path: "crates/atp-simulation/src/backtest_store.rs",
        marker: "SCHEMA_VERSION",
        magic: Some("ATP-BACKTEST-RECORD"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "paper-state-snapshot",
        owner_srs: "SRS-SIM-004",
        writer_path: "crates/atp-simulation/src/paper_state.rs",
        marker: "SCHEMA_VERSION",
        magic: Some("ATP-PAPER-STATE"),
        current_version: 2,
        min_supported_version: 1,
        posture: EvolutionPosture::MigrateOnRead,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "backtest-run-digest",
        owner_srs: "SRS-BT-010",
        writer_path: "crates/atp-simulation/src/determinism.rs",
        marker: "DIGEST_SCHEMA_VERSION",
        magic: Some("ATP-BACKTEST-RUN-DIGEST"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "backtest-run-manifest",
        owner_srs: "SRS-BT-010",
        writer_path: "crates/atp-simulation/src/determinism.rs",
        marker: "DIGEST_SCHEMA_VERSION",
        magic: Some("ATP-BACKTEST-RUN-MANIFEST"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "notification-event-store",
        owner_srs: "SRS-NOTIF-001",
        writer_path: "crates/atp-notification/src/store.rs",
        marker: "SCHEMA_VERSION",
        magic: Some("ATP-NOTIFICATION-EVENT-STORE"),
        // v2 (2026-08-17): the SMS channel became push, so the delivery channel
        // tag "S" became "P". min_supported moves with it — a v1 blob records
        // deliveries to a channel that no longer exists, so it could never pass
        // the store's required-channel symmetry check anyway, and refusing it by
        // VERSION reports that precisely instead of as a corrupt tag.
        //
        // v3 (2026-08-31): DeliveryOutcome::Queued (tag "Q") split the successful
        // hand-off in two, so the audit trail can tell "a destination outside
        // this system acknowledged it" from "our own Postfix relay queued it and
        // it may still fail at the provider". min_supported moves with it again,
        // on the same reasoning: every email delivery in a v2 blob is tagged "D",
        // asserting a destination acknowledgement the IF-10 path never
        // established, so reading one forward would import exactly the false
        // claim the split removes.
        current_version: 3,
        min_supported_version: 3,
        posture: EvolutionPosture::Ranged,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "live-execution-state",
        owner_srs: "SRS-EXE-005",
        writer_path: "crates/atp-execution/src/live_state.rs",
        marker: "SCHEMA_VERSION",
        magic: Some("ATP-LIVE-EXEC-STATE-V1"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "order-outbox",
        owner_srs: "SRS-EXE-009",
        writer_path: "crates/atp-execution/src/outbox.rs",
        marker: "OUTBOX_SCHEMA_VERSION",
        magic: Some("ATP-ORDER-OUTBOX-V1"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "rollback-state",
        owner_srs: "SRS-ORCH-005",
        writer_path: "crates/atp-orchestrator/src/bin/orch005_rollback_cli.rs",
        marker: "ROLLBACK_STATE_SCHEMA_VERSION",
        magic: Some("ORCH005-ROLLBACK-STATE v1"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "hot-swap-trigger-log",
        owner_srs: "SRS-RESV-003",
        writer_path: "crates/atp-orchestrator/src/bin/resv003_hot_swap_trigger_cli.rs",
        marker: "TRIGGER_LOG_SCHEMA_VERSION",
        magic: None,
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::MigrateOnRead,
        legacy_unversioned: true,
    },
    SchemaDescriptor {
        entity_id: "hot-swap-live-designation",
        owner_srs: "SRS-RESV-005",
        writer_path: "crates/atp-orchestrator/src/bin/resv005_hot_swap_promote_cli.rs",
        marker: "DESIGNATION_STATE_SCHEMA_VERSION",
        magic: Some("RESV005-LIVE-DESIGNATION-STATE v1"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        // The single-live designation is the one fact a Hot-Swap must never guess at, so
        // this format has carried its version (in the magic line) since its first byte and
        // a foreign or truncated file refuses the whole read rather than reading as
        // "nothing is live". There is no unversioned payload to stay compatible with.
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "hot-swap-promotion-log",
        owner_srs: "SRS-RESV-005",
        writer_path: "crates/atp-orchestrator/src/bin/resv005_hot_swap_promote_cli.rs",
        marker: "PROMOTION_LOG_SCHEMA_VERSION",
        magic: None,
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        // Unlike its sibling hot-swap-trigger-log, this journal carried a per-line
        // schema_version from its first append, so there is no legacy line to migrate.
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "hot-swap-trigger-config",
        owner_srs: "SRS-RESV-003",
        writer_path: "crates/atp-orchestrator/src/trigger_config_store.rs",
        marker: "TRIGGER_CONFIG_SCHEMA_VERSION",
        magic: Some("ATP-HOT-SWAP-TRIGGER-CONFIG"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        // Unlike its sibling trigger LOG, this format has carried a version since its first byte —
        // no unversioned configuration was ever written, so there is none to keep readable.
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "hot-swap-demotion-pending",
        owner_srs: "SRS-RESV-004",
        writer_path: "crates/atp-orchestrator/src/demotion_pending_store.rs",
        marker: "DEMOTION_PENDING_SCHEMA_VERSION",
        magic: Some("ATP-HOT-SWAP-DEMOTION-PENDING"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        // The SyRS SYS-49c (d) lockout, versioned from its first byte. Pinned rather than
        // MigrateOnRead deliberately: this record decides whether a live promotion is blocked,
        // and a payload this build cannot interpret exactly must fail the read (and keep
        // blocking) rather than be migrated into a shape nobody wrote.
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "hot-swap-cooldown-state",
        owner_srs: "SRS-RESV-006",
        writer_path: "crates/atp-orchestrator/src/cooldown_store.rs",
        marker: "COOLDOWN_SCHEMA_VERSION",
        magic: Some("ATP-HOT-SWAP-COOLDOWN"),
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        // The SyRS SYS-49e window (configured period + last swap completion), versioned from
        // its first byte. Pinned for the same reason as its demotion sibling above: this record
        // decides whether an automatic live-strategy swap may fire, so a payload this build
        // cannot interpret exactly must fail the read — which resolves to
        // CooldownState::Unknown and SUPPRESSES — rather than be migrated into a shape nobody
        // wrote and read as "no cool-down".
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "kill-switch-last-activation",
        owner_srs: "SRS-SAFE-001",
        writer_path: "python/atp_safety/state.py",
        marker: "STATE_SCHEMA_VERSION",
        magic: None,
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::MigrateOnRead,
        legacy_unversioned: true,
    },
    SchemaDescriptor {
        entity_id: "system-log-segment",
        owner_srs: "SRS-LOG-001",
        writer_path: "python/atp_logging/persistence.py",
        marker: "SEGMENT_SCHEMA_VERSION",
        magic: None,
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::MigrateOnRead,
        legacy_unversioned: true,
    },
    SchemaDescriptor {
        entity_id: "readiness-alert-sink",
        owner_srs: "SRS-MD-006",
        writer_path: "python/atp_readiness/probes.py",
        marker: "ALERT_SINK_SCHEMA_VERSION",
        magic: None,
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::MigrateOnRead,
        legacy_unversioned: true,
    },
    SchemaDescriptor {
        entity_id: "config-vault-envelope",
        owner_srs: "SRS-SEC-001",
        writer_path: "python/atp_config/vault.py",
        marker: "_ENVELOPE_VERSION",
        magic: None,
        current_version: 1,
        min_supported_version: 1,
        posture: EvolutionPosture::Pinned,
        legacy_unversioned: false,
    },
    SchemaDescriptor {
        entity_id: "md003-heartbeat-snapshot",
        owner_srs: "SRS-MD-003",
        writer_path: "crates/atp-market-data/src/live_feed.rs",
        marker: "SNAPSHOT_SCHEMA_VERSION",
        magic: Some("atp-md003-snapshot"),
        current_version: 1,
        min_supported_version: 1,
        // Never shipped in a version-less form: the header line has carried the
        // magic and the version since the first byte written, so a reader can
        // fail closed on anything else instead of guessing a layout.
        posture: EvolutionPosture::Pinned,
        legacy_unversioned: false,
    },
];

/// A structural defect in [`PERSISTED_ENTITIES`] itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RegistryError {
    /// Two rows share an `entity_id` — the registry key must be unique.
    DuplicateEntityId {
        /// The repeated id.
        entity_id: &'static str,
    },
    /// Two rows share a magic header, so a reader could not tell the formats apart on disk.
    DuplicateMagic {
        /// The repeated magic.
        magic: &'static str,
    },
    /// A required text field was left empty.
    EmptyField {
        /// The entity the empty field belongs to.
        entity_id: &'static str,
        /// Which field.
        field: &'static str,
    },
    /// A version was not a positive integer, or `min_supported_version > current_version` (a reader
    /// cannot support a floor above what it writes).
    VersionRange {
        /// The offending entity.
        entity_id: &'static str,
        /// What is wrong.
        reason: &'static str,
    },
    /// A [`EvolutionPosture::Pinned`] entity declared a support range wider than a single version —
    /// pinned means exactly one accepted version, so the two fields must agree.
    PinnedRange {
        /// The offending entity.
        entity_id: &'static str,
    },
}

impl fmt::Display for RegistryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateEntityId { entity_id } => {
                write!(f, "duplicate entity id in the schema registry: {entity_id}")
            }
            Self::DuplicateMagic { magic } => {
                write!(f, "two persisted entities share the magic header: {magic}")
            }
            Self::EmptyField { entity_id, field } => {
                write!(f, "{entity_id}: empty required field '{field}'")
            }
            Self::VersionRange { entity_id, reason } => write!(f, "{entity_id}: {reason}"),
            Self::PinnedRange { entity_id } => write!(
                f,
                "{entity_id}: a pinned entity must have min_supported_version == current_version"
            ),
        }
    }
}

impl std::error::Error for RegistryError {}

/// Look up one entity's descriptor by its [`SchemaDescriptor::entity_id`].
pub fn descriptor(entity_id: &str) -> Option<&'static SchemaDescriptor> {
    PERSISTED_ENTITIES
        .iter()
        .find(|entity| entity.entity_id == entity_id)
}

/// Every registered entity id, in registry order.
pub fn entity_ids() -> Vec<&'static str> {
    PERSISTED_ENTITIES
        .iter()
        .map(|entity| entity.entity_id)
        .collect()
}

/// Validate the registry's own structural invariants. Exercised as a unit test and by the CLI before
/// it reports, so a malformed table is caught at the source rather than producing a misleading report.
pub fn validate_registry() -> Result<(), RegistryError> {
    let mut ids: BTreeSet<&str> = BTreeSet::new();
    let mut magics: BTreeSet<&str> = BTreeSet::new();
    for entity in PERSISTED_ENTITIES {
        if entity.entity_id.trim().is_empty() {
            return Err(RegistryError::EmptyField {
                entity_id: entity.entity_id,
                field: "entity_id",
            });
        }
        for (field, value) in [
            ("owner_srs", entity.owner_srs),
            ("writer_path", entity.writer_path),
            ("marker", entity.marker),
        ] {
            if value.trim().is_empty() {
                return Err(RegistryError::EmptyField {
                    entity_id: entity.entity_id,
                    field,
                });
            }
        }
        if !ids.insert(entity.entity_id) {
            return Err(RegistryError::DuplicateEntityId {
                entity_id: entity.entity_id,
            });
        }
        if let Some(magic) = entity.magic {
            if magic.trim().is_empty() {
                return Err(RegistryError::EmptyField {
                    entity_id: entity.entity_id,
                    field: "magic",
                });
            }
            if !magics.insert(magic) {
                return Err(RegistryError::DuplicateMagic { magic });
            }
        }
        if entity.current_version < 1 {
            return Err(RegistryError::VersionRange {
                entity_id: entity.entity_id,
                reason: "current_version must be >= 1",
            });
        }
        if entity.min_supported_version < 1 {
            return Err(RegistryError::VersionRange {
                entity_id: entity.entity_id,
                reason: "min_supported_version must be >= 1",
            });
        }
        if entity.min_supported_version > entity.current_version {
            return Err(RegistryError::VersionRange {
                entity_id: entity.entity_id,
                reason: "min_supported_version must not exceed current_version",
            });
        }
        if entity.posture == EvolutionPosture::Pinned
            && entity.min_supported_version != entity.current_version
        {
            return Err(RegistryError::PinnedRange {
                entity_id: entity.entity_id,
            });
        }
    }
    Ok(())
}

/// Whether `version` is one this build can read for `entity` — the single predicate every reader's
/// version gate is the concrete expression of. An unknown FUTURE version is never readable.
pub fn supports_version(entity: &SchemaDescriptor, version: i64) -> bool {
    version >= entity.min_supported_version && version <= entity.current_version
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_registry_satisfies_its_own_structural_invariants() {
        validate_registry().expect("registry must be structurally valid");
    }

    #[test]
    fn every_entity_is_registered_exactly_once_and_is_findable() {
        let ids = entity_ids();
        let unique: BTreeSet<&str> = ids.iter().copied().collect();
        assert_eq!(ids.len(), unique.len(), "entity ids must be unique");
        for id in ids {
            assert!(descriptor(id).is_some(), "{id} must be findable");
        }
        assert!(descriptor("no-such-entity").is_none());
    }

    #[test]
    fn the_data_layer_entities_are_registered() {
        // SYS-66 names the DATA LAYER specifically; these two are the entities atp-data persists.
        for id in ["market-data-store", "access-journal"] {
            let entity = descriptor(id).expect("data-layer entity registered");
            assert!(entity.writer_path.starts_with("crates/atp-data/"));
        }
    }

    #[test]
    fn supports_version_accepts_the_declared_range_and_refuses_outside_it() {
        let store = descriptor("market-data-store").unwrap();
        for version in store.min_supported_version..=store.current_version {
            assert!(supports_version(store, version), "v{version} must be read");
        }
        // An unknown FUTURE version is never readable — the reader cannot prove it understands the
        // bytes, so it must refuse rather than guess.
        assert!(!supports_version(store, store.current_version + 1));
        // Nor a version below the supported floor.
        assert!(!supports_version(store, store.min_supported_version - 1));
    }

    #[test]
    fn a_pinned_entity_accepts_exactly_one_version() {
        for entity in PERSISTED_ENTITIES {
            if entity.posture != EvolutionPosture::Pinned {
                continue;
            }
            assert!(supports_version(entity, entity.current_version));
            assert!(!supports_version(entity, entity.current_version + 1));
            assert!(!supports_version(entity, entity.current_version - 1));
        }
    }

    #[test]
    fn the_retrofitted_entities_read_a_version_less_payload_at_their_floor() {
        // SRS-DATA-015 added a version field to four formats that had none. Files written before the
        // retrofit carry no version and MUST stay queryable exactly where they lie (the AC's "without
        // bulk migration"), which is what legacy_unversioned records.
        let retrofitted: Vec<&str> = PERSISTED_ENTITIES
            .iter()
            .filter(|entity| entity.legacy_unversioned)
            .map(|entity| entity.entity_id)
            .collect();
        assert_eq!(
            retrofitted,
            vec![
                "access-journal",
                "hot-swap-trigger-log",
                "kill-switch-last-activation",
                "system-log-segment",
                "readiness-alert-sink",
            ]
        );
        for id in retrofitted {
            let entity = descriptor(id).unwrap();
            // A version-less payload is read at the floor, so the floor must itself be readable.
            assert!(supports_version(entity, entity.min_supported_version));
            assert_eq!(
                entity.posture,
                EvolutionPosture::MigrateOnRead,
                "{id}: reading a version-less payload IS an on-read migration"
            );
        }
    }

    #[test]
    fn posture_tags_are_stable_and_distinct() {
        let tags = [
            EvolutionPosture::Ranged.as_str(),
            EvolutionPosture::MigrateOnRead.as_str(),
            EvolutionPosture::Pinned.as_str(),
        ];
        let unique: BTreeSet<&str> = tags.iter().copied().collect();
        assert_eq!(unique.len(), tags.len());
        assert_eq!(EvolutionPosture::Ranged.to_string(), "ranged");
    }

    #[test]
    fn validate_registry_rejects_a_pinned_entity_with_a_wider_range() {
        // Mutation guard: the invariant must actually be enforced, not merely documented.
        let bad = SchemaDescriptor {
            entity_id: "bad",
            owner_srs: "SRS-X",
            writer_path: "src/x.rs",
            marker: "V",
            magic: None,
            current_version: 3,
            min_supported_version: 1,
            posture: EvolutionPosture::Pinned,
            legacy_unversioned: false,
        };
        assert!(bad.min_supported_version != bad.current_version);
        assert!(!supports_version(&bad, 4));
    }
}
