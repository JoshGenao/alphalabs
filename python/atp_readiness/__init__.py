"""Startup readiness gate SDK-surface for ERR-9 / SRS-MD-006.

This package is the cross-language source of truth for the pre-trade hold
state machine. ``ReadinessGate`` consumes the SRS-ARCH-005 validator output
from :mod:`atp_config` and exposes a structured payload shape that the
downstream log sink (SRS-LOG-001), dashboard (SRS-UI-001), and REST/WebSocket
API (SRS-API-001) consume when those features land.

See ``python/atp_readiness/README.md`` for the operator-facing summary and
``architecture/runtime_services.json#startup_readiness_gate_contract`` for the
cross-language contract block.
"""

from .errors import (
    GateTransitionError,
    OverrideAuditError,
    PreTradeHoldError,
    ReadinessGateError,
)
from .gate import GateState, ReadinessGate
from .override import OperatorOverride

__all__ = [
    "GateState",
    "GateTransitionError",
    "OperatorOverride",
    "OverrideAuditError",
    "PreTradeHoldError",
    "ReadinessGate",
    "ReadinessGateError",
]
