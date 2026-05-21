"""Structured error types raised by ``atp_readiness``.

Every error carries a :class:`atp_config.ReadinessReport` reference when
applicable so the downstream log sink, dashboard payload, and REST/WebSocket
API can serialise the same structured failure body that drove the hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atp_config import ReadinessReport


class ReadinessGateError(Exception):
    """Base class for every error raised by :mod:`atp_readiness`."""


class PreTradeHoldError(ReadinessGateError):
    """Raised by ``ReadinessGate.assert_ready_or_hold`` when the gate is held.

    The hold state is the system-level signal that the platform must not yet
    accept live or paper order submissions per ERR-9 + SRS-MD-006. Callers
    must catch this error and route the attached :class:`ReadinessReport`
    through the log sink, dashboard, and API surfaces.
    """

    def __init__(self, report: ReadinessReport) -> None:
        error_keys = ", ".join(sorted({f.key for f in report.errors}))
        super().__init__(
            f"system held in pre-trade state: {len(report.errors)} readiness "
            f"error(s) ({error_keys or 'no error-severity failures'})"
        )
        self.report = report


class GateTransitionError(ReadinessGateError):
    """Raised when a forbidden gate state transition is attempted.

    Forbidden transitions are enumerated in
    ``architecture/runtime_services.json#startup_readiness_gate_contract.forbidden_transitions``
    and mirrored in :func:`atp_readiness.gate.ReadinessGate._assert_transition`.
    """


class OverrideAuditError(ReadinessGateError):
    """Raised when ``ReadinessGate.operator_override`` receives an audit-incomplete payload.

    SRS-MD-006 requires that any operator-initiated override of the pre-trade
    hold carries a non-empty actor identifier, a non-empty human-readable
    reason, a non-empty audit-trail reference, and a monotonic timestamp so
    the override is traceable in the SRS-LOG-001 audit log when that surface
    arrives.
    """
