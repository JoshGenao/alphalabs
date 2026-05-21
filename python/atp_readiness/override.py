"""Operator override dataclass for the startup readiness gate.

``OperatorOverride`` carries the audit-trail fields SRS-MD-006 requires when
an operator manually releases the pre-trade hold (e.g. to start paper-only
strategies while IB credentials are intentionally unset in a development
deployment). The four required fields are enforced at construction time by
the gate, not by the dataclass itself, so the gate can surface a precise
:class:`atp_readiness.errors.OverrideAuditError` per field.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class OperatorOverride:
    """One operator-initiated release of the pre-trade hold.

    Required fields (SRS-MD-006 audit-trail contract):

    * ``actor`` — non-empty identifier of the operator (e.g. an email or
      operator-id string).
    * ``reason`` — non-empty human-readable justification surfaced through
      the SRS-LOG-001 audit log + SRS-NOTIF-001 alert dispatch.
    * ``audit_trail_id`` — non-empty cross-reference to the persistent
      operator audit log entry. The downstream SRS-LOG-001 + SRS-NOTIF-001
      surfaces consume this id to thread the override across systems.
    * ``timestamp_ns`` — wall-clock nanoseconds since the Unix epoch when the
      override was issued. Must be a non-negative integer; ``bool`` is
      rejected (Python treats ``True`` / ``False`` as ints).
    """

    actor: str
    reason: str
    audit_trail_id: str
    timestamp_ns: int

    def as_dict(self) -> dict[str, Any]:
        """Return the override as a JSON-serialisable mapping."""

        return asdict(self)


__all__ = ["OperatorOverride"]
