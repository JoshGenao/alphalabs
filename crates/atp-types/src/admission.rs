//! SRS-MD-005 / SyRS SYS-75(a): the market-data admission decision, and the
//! proof that it was taken.
//!
//! This module is the ONLY code that can say "admitted". [`AdmissionProof`] has
//! a private field, so outside this module - in any crate, and in every other
//! module of this one - the compiler refuses to build one, and with it
//! [`MarketDataAdmission::Admitted`]. The one place that does is
//! [`RestartWindow::market_data_admission`], the same pure decision the order
//! gate reads. A gate that "always admits" therefore cannot be written: it has
//! no proof to return unless it asks a real window.
//!
//! The registry's only way to add a subscriber consumes a proof
//! (`atp_market_data`'s admitted set), so a write that skipped the decision does
//! not compile either. What used to rest on a source scan now rests on the type
//! system; `tools/connectivity_check.py` pins this file's digest so a second
//! constructor added here is a visible contract change, not a quiet one.
//!
//! What the type system does NOT close: a caller can hand the window a false
//! clock or schedule. That is configuration integrity, not a code path.
//!
//! ```compile_fail,E0451
//! // Outside this module the proof cannot be built: its field is private.
//! // Pinned to E0451 so the test cannot pass on an unrelated compile error.
//! let _forged = atp_types::AdmissionProof { _sealed: () };
//! ```
//!
//! ```compile_fail,E0451
//! // And so `Admitted` cannot be built without asking a window.
//! let _forged = atp_types::MarketDataAdmission::Admitted(atp_types::AdmissionProof { _sealed: () });
//! ```
//!
//! ```
//! // Refusals ARE constructible: forging one can only deny, never admit.
//! let refused = atp_types::MarketDataAdmission::SuspendedForScheduledRestart;
//! assert!(!refused.is_admitted());
//! ```

use crate::{ConnectivityState, RestartWindow};

/// Evidence that a [`RestartWindow`] decided a market-data request may proceed.
///
/// Its only field is private, so it is built in exactly one place:
/// [`RestartWindow::market_data_admission`]. It is deliberately neither `Clone`
/// nor `Copy`: an admission is spent by the write it authorizes, so a proof
/// taken while the window was open cannot be kept and replayed once it closes.
#[derive(Debug, PartialEq, Eq, Hash)]
pub struct AdmissionProof {
    _sealed: (),
}

/// Whether a market-data subscription request may proceed, and the reason when
/// it may not.
///
/// Not `Clone` or `Copy`, because `Admitted` carries the proof. Code that only
/// needs to RECORD the decision stores [`MarketDataAdmission::outcome`], a plain
/// label.
#[derive(Debug, PartialEq, Eq, Hash)]
pub enum MarketDataAdmission {
    /// The request may proceed to the line-limit gate.
    Admitted(AdmissionProof),
    /// SRS-MD-005 / SyRS SYS-75(a): planned maintenance. The identical request
    /// succeeds unchanged once the window closes.
    SuspendedForScheduledRestart,
    /// SyRS SYS-45: the IB Gateway is unreachable. Not maintenance — this is
    /// the state an operator is paged about.
    ConnectivityLost,
}

impl MarketDataAdmission {
    pub const fn is_admitted(&self) -> bool {
        matches!(self, Self::Admitted(_))
    }

    /// The decision as a plain label: copyable, comparable, storable in evidence.
    pub const fn outcome(&self) -> AdmissionOutcome {
        match self {
            Self::Admitted(_) => AdmissionOutcome::Admitted,
            Self::SuspendedForScheduledRestart => AdmissionOutcome::SuspendedForScheduledRestart,
            Self::ConnectivityLost => AdmissionOutcome::ConnectivityLost,
        }
    }

    /// A stable label for operator output and structured records.
    pub const fn as_str(&self) -> &'static str {
        self.outcome().as_str()
    }
}

/// What a market-data admission decided, without the proof: a record, not a
/// capability. Building `AdmissionOutcome::Admitted` admits nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AdmissionOutcome {
    Admitted,
    SuspendedForScheduledRestart,
    ConnectivityLost,
}

impl AdmissionOutcome {
    /// A stable label for operator output and structured records.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Admitted => "ADMITTED",
            Self::SuspendedForScheduledRestart => "SCHEDULED_RESTART_WINDOW",
            Self::ConnectivityLost => "CONNECTIVITY_LOST",
        }
    }
}

impl RestartWindow {
    /// Whether a market-data subscription request may be admitted, and if not,
    /// WHY.
    ///
    /// SYS-75(a) suspends order submission *and market-data requests*
    /// together, so this is derived from the SAME decision the order gate
    /// consults rather than computed alongside it — two predicates over one
    /// requirement drift, and the drift would be invisible.
    ///
    /// It returns a reason rather than a boolean because the two refusals mean
    /// opposite things to an operator. Inside the window, "suspended" means
    /// wait, this is scheduled. After it, the identical refusal is a genuine
    /// outage, and labelling that as maintenance would tell the operator to
    /// wait through an incident.
    ///
    /// Written as an allowlist: only `Connected` admits. A future state
    /// inherits a refusal, which is an under-claim; a denylist would have
    /// inherited admission, which is a fail-open.
    ///
    /// The ONLY constructor of [`AdmissionProof`].
    pub const fn market_data_admission(self, now_ns: i64, reachable: bool) -> MarketDataAdmission {
        match self.connectivity_state(now_ns, reachable) {
            ConnectivityState::Connected => {
                MarketDataAdmission::Admitted(AdmissionProof { _sealed: () })
            }
            ConnectivityState::ScheduledRestartWindow => {
                MarketDataAdmission::SuspendedForScheduledRestart
            }
            ConnectivityState::Unreachable => MarketDataAdmission::ConnectivityLost,
        }
    }

    /// Convenience predicate over [`market_data_admission`](Self::market_data_admission)
    /// for callers that only need the decision.
    pub const fn admits_market_data_requests(self, now_ns: i64, reachable: bool) -> bool {
        self.market_data_admission(now_ns, reachable).is_admitted()
    }
}
