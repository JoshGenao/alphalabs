//! SRS-MD-005 / SyRS SYS-75(a): the consolidated subscription set, which only
//! an admission can grow.
//!
//! [`AdmittedSet::admit`] is the ONLY way to add a subscriber, and it consumes
//! an [`AdmissionProof`] - a value only `atp_types::RestartWindow` can make, and
//! only when it decides a request may proceed. The map itself is a private field
//! of this module, so no other code - the registry around it, the crate root, a
//! sibling module, another crate - can reach it. Removing and reading are free:
//! releasing a line is always safe, and reading admits nothing.
//!
//! This is what replaced a source scan that tried to recognise every way Rust
//! can write to a map (`push`, `entry().or_default()`, `Vec::push(..)`,
//! `extend`, whole-map replacement, `mem::swap`, a macro, ...) and kept meeting
//! new ones. The compiler now refuses all of them at once, because none of them
//! can name the map. `tools/connectivity_check.py` pins this file's digest, so
//! a second way in, added here, is a visible contract change.
//!
//! ```compile_fail,E0616
//! // The map is private to this module.
//! use atp_market_data::subscriptions::admitted_set::AdmittedSet;
//! let mut set = AdmittedSet::default();
//! set.lines.clear();
//! ```
//!
//! ```compile_fail,E0451
//! // Adding needs a proof, and no code outside `atp_types::admission` can make one.
//! use atp_market_data::subscriptions::admitted_set::AdmittedSet;
//! use atp_types::{AdmissionProof, AssetClass, SecurityKey, StrategyId};
//! let mut set = AdmittedSet::default();
//! let key = SecurityKey::new("AAPL", AssetClass::Equity).unwrap();
//! set.admit(AdmissionProof { _sealed: () }, key, StrategyId::new("s"));
//! ```

use std::collections::BTreeMap;

use atp_types::{AdmissionProof, SecurityKey, StrategyId};

/// SecurityKey -> subscribers in subscription order. `BTreeMap` gives
/// deterministic key iteration for the line accounting; the per-key `Vec`
/// preserves fan-out order. Kept duplicate-free by the registry's `subscribe`.
#[derive(Debug, Default)]
pub struct AdmittedSet {
    lines: BTreeMap<SecurityKey, Vec<StrategyId>>,
}

/// What [`AdmittedSet::remove`] did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Removal {
    /// The strategy was not a subscriber of that security.
    NotSubscribed,
    /// The strategy left; other subscribers keep the line open.
    Removed,
    /// The last subscriber left, so the upstream line is released.
    Closed,
}

impl AdmittedSet {
    /// Number of distinct upstream lines (securities with a subscriber).
    pub fn line_count(&self) -> usize {
        self.lines.len()
    }

    /// True when `key` has a line (at least one subscriber).
    pub fn contains_key(&self, key: &SecurityKey) -> bool {
        self.lines.contains_key(key)
    }

    /// The subscribers of `key`, in subscription order.
    pub fn subscribers(&self, key: &SecurityKey) -> Option<&[StrategyId]> {
        self.lines.get(key).map(Vec::as_slice)
    }

    /// The ONLY way to add a subscriber. The proof is consumed, so one decision
    /// authorizes one addition and cannot be kept for later.
    pub fn admit(&mut self, proof: AdmissionProof, key: SecurityKey, strategy_id: StrategyId) {
        let _spent = proof;
        self.lines.entry(key).or_default().push(strategy_id);
    }

    /// Remove `strategy_id` from `key`, releasing the line when it was the last.
    pub fn remove(&mut self, key: &SecurityKey, strategy_id: &StrategyId) -> Removal {
        let Some(existing) = self.lines.get_mut(key) else {
            return Removal::NotSubscribed;
        };
        let before = existing.len();
        existing.retain(|s| s != strategy_id);
        if existing.len() == before {
            Removal::NotSubscribed
        } else if existing.is_empty() {
            self.lines.remove(key);
            Removal::Closed
        } else {
            Removal::Removed
        }
    }
}
