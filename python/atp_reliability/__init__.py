"""``atp_reliability`` — market-hours availability measurement (SRS-REL-001).

An **offline verification/analysis** tool (not a core runtime service; AC-16
permits non-Rust here, alongside ``atp_readiness`` / ``atp_safety`` / ``atp_cli``)
that produces the objective evidence SRS-REL-001 / SyRS NFR-R1 require: the
availability of the platform during US equity market hours over a rolling 30-day
period, measured only over positively-observed time and refusing to certify
unmeasured spans.

* :mod:`atp_reliability.availability` — the pure, clock-free measurement engine.
* :mod:`atp_reliability.evidence` — calendar + log-store adapters.
* ``python -m atp_reliability`` — the CLI that emits the verification artifact.
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

__all__ = [
    "AvailabilityError",
    "AvailabilityTarget",
    "AvailabilityVerificationArtifact",
    "COUNTING_CAUSES",
    "CoveredSpan",
    "DowntimeInterval",
    "HealthTransition",
    "MARKET_HOURS_BOUNDARY",
    "MarketSessionWindow",
    "OutageCause",
    "Verdict",
    "compute_availability",
    "downtime_from_log_records",
    "market_sessions",
    "reconstruct_downtime",
    "sys75_exclusion_windows",
]
