"""``atp_reliability`` — offline reliability measurement substrates (SRS-REL-001 / SRS-REL-002).

**Offline verification/analysis** tools (not core runtime services; AC-16 permits non-Rust here,
alongside ``atp_readiness`` / ``atp_safety`` / ``atp_cli``) that produce the objective evidence the
SyRS reliability NFRs require, each measuring only positively-observed evidence and refusing to
certify what it did not observe:

* :mod:`atp_reliability.availability` — SRS-REL-001 / NFR-R1: platform availability during US
  equity market hours over a rolling 30-day period (pure, clock-free engine).
* :mod:`atp_reliability.evidence` — calendar + log-store adapters for the availability engine.
* ``python -m atp_reliability`` — the availability verification CLI.
* :mod:`atp_reliability.restart` — SRS-REL-002 / NFR-R6: full-system-restart recovery time
  (restore to trade-ready within 10 minutes; pure, clock-free engine).
* :mod:`atp_reliability.boot_evidence` — host boot-telemetry adapters for the restart engine.
* ``python -m atp_reliability.restart_cli`` — the restart-recovery verification CLI.
"""

from __future__ import annotations

from .availability import (
    COUNTING_CAUSES,
    MARKET_HOURS_BOUNDARY,
    AvailabilityError,
    AvailabilityTarget,
    AvailabilityVerificationArtifact,
    CoveredSpan,
    DowntimeInterval,
    MarketSessionWindow,
    OutageCause,
    Verdict,
    compute_availability,
)
from .evidence import (
    HealthTransition,
    downtime_from_log_records,
    market_sessions,
    reconstruct_downtime,
    sys75_exclusion_windows,
)
from .restart import (
    DEFAULT_RTO_BUDGET_NS,
    DEFAULT_RTO_SECONDS,
    REQUIRED_PHASES,
    REQUIRED_SUBCHECKS,
    SRS_REL_002,
    TRADE_READY_STATES,
    GateOutcome,
    ObservedPhase,
    ReadinessOutcome,
    RestartError,
    RestartPhase,
    RestartRecoveryArtifact,
    RestartRecoveryTarget,
    SubCheck,
    SubCheckResult,
    SubCheckStatus,
    compute_restart_recovery,
)

__all__ = [
    "AvailabilityError",
    "AvailabilityTarget",
    "AvailabilityVerificationArtifact",
    "COUNTING_CAUSES",
    "CoveredSpan",
    "DEFAULT_RTO_BUDGET_NS",
    "DEFAULT_RTO_SECONDS",
    "DowntimeInterval",
    "GateOutcome",
    "HealthTransition",
    "MARKET_HOURS_BOUNDARY",
    "MarketSessionWindow",
    "ObservedPhase",
    "OutageCause",
    "REQUIRED_PHASES",
    "REQUIRED_SUBCHECKS",
    "ReadinessOutcome",
    "RestartError",
    "RestartPhase",
    "RestartRecoveryArtifact",
    "RestartRecoveryTarget",
    "SRS_REL_002",
    "SubCheck",
    "SubCheckResult",
    "SubCheckStatus",
    "TRADE_READY_STATES",
    "Verdict",
    "compute_availability",
    "compute_restart_recovery",
    "downtime_from_log_records",
    "market_sessions",
    "reconstruct_downtime",
    "sys75_exclusion_windows",
]
