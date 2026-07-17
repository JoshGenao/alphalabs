//! Session-control seam for the SyRS SYS-44a/44b "disconnect from IB" leg —
//! the kill-switch runtime's final safety action (SRS-SAFE-001/002).
//!
//! A SEPARATE trait from [`IbGatewayConnection`](crate::IbGatewayConnection)
//! on purpose, and a SEPARATE module from `interactive_brokers` on purpose:
//!
//! * The wire-operation seam is the pinned SRS-EXE-006 transport contract —
//!   every implementor is a full gateway double, and
//!   `tools/ib_adapter_check.py` binds the operator's paper-account evidence
//!   to that module's exact bytes (`code_digest`). Session teardown is a
//!   control-plane capability only the kill-switch composition consumes, so
//!   it lives outside both the trait and the digest-covered module.
//! * The concrete `TcpIbGateway` binding (dropping the cached wire session so
//!   its `TcpStream` closes) therefore lands with the next operator-gated
//!   SRS-EXE-006 paper-account run — the digest gate REQUIRES that any change
//!   to the transport module be re-proven against the live gateway, which is
//!   exactly the deferred live leg named in
//!   `kill_switch_timeout_contract.deferred[]`. Until then the SYS-44b
//!   scenario drives this seam through the deterministic fixture gateway in
//!   `atp-orchestrator::kill_switch_timeout`.

use crate::AdapterResult;

/// The "disconnect from IB" control-plane capability.
pub trait IbConnectionControl {
    /// Sever the IB Gateway session. Idempotent: disconnecting an unconnected
    /// gateway is `Ok` (the desired state already holds). An `Err` means the
    /// session could not be provably torn down — the kill-switch gate records
    /// it as a `Failed` side effect rather than assuming safety.
    ///
    /// Failures cross the adapter boundary as the canonical
    /// [`AdapterError`](crate::AdapterError) taxonomy (never a raw vendor
    /// error): an implementor maps its vendor failure through
    /// `AdapterError::Brokerage` — carrying the SyRS SYS-64
    /// `OrderErrorCategory` classification (e.g. `CONNECTIVITY_BLOCKED`) via
    /// [`classify_ib_order_error`](crate::classify_ib_order_error) plus the
    /// raw vendor code + message — so the kill-switch cleanup records a
    /// classified, operator-actionable reason on the safety event.
    fn disconnect(&self) -> AdapterResult<()>;
}
