# atp_readiness — Startup readiness gate (ERR-9 SDK-surface)

`atp_readiness` is the cross-language source of truth for ATP's **pre-trade
hold** state machine. It consumes the SRS-ARCH-005 validator output and
exposes the structured payload shape that the downstream log sink, dashboard,
and REST/WebSocket API will read when those surfaces land.

This package satisfies the **SDK-surface half** of ERR-9:

> Missing or invalid startup configuration: hold system in pre-trade state
> and expose readiness failure through logs, dashboard, and API.

It does **not** ship:

- the live REST/WebSocket endpoint serving `as_dashboard_payload`
  (deferred to SRS-API-001);
- the dashboard rendering (deferred to SRS-UI-001);
- the concrete log sink that consumes `as_log_records`
  (deferred to SRS-LOG-001);
- the runtime readiness probes for IB connectivity, IB account
  authentication, SSD data layer access, ingestion freshness within one
  trading day, system service health, and NAS reachability (deferred to
  SRS-MD-006);
- the persistent operator-override audit log + notification dispatch
  (deferred to SRS-LOG-001 + SRS-NOTIF-001).

Because those four surfaces are not yet built, `ERR-9` stays
`passes:false` in `feature_list.json`. The contract here is what they will
consume when they arrive.

## States

| State | Meaning |
|---|---|
| `initializing` | gate has not yet evaluated readiness |
| `pre_trade_blocked` | at least one error-severity readiness failure is present; live and paper order submission must be held |
| `ready` | configuration validation passes; the SRS-MD-006 runtime half still gates live trading when it lands |
| `overridden` | an operator manually released the pre-trade hold with a fully-audited `OperatorOverride` |

## Allowed transitions

```
initializing       -> {pre_trade_blocked, ready}
pre_trade_blocked  -> {pre_trade_blocked, ready, overridden}
ready              -> {pre_trade_blocked, ready}
overridden         -> {pre_trade_blocked, ready}
```

Forbidden transitions raise
`atp_readiness.errors.GateTransitionError`. The full forbidden set is
enumerated in
`architecture/runtime_services.json#startup_readiness_gate_contract.forbidden_transitions`
and re-asserted by the L3 contract test.

## Operator override (SRS-MD-006 audit trail)

Every operator-initiated release of the pre-trade hold must carry four
audit-trail fields. The gate raises
`atp_readiness.errors.OverrideAuditError` when any are missing, empty, or
of the wrong type.

| Field | Type | Required content |
|---|---|---|
| `actor` | non-empty `str` | operator identifier (e.g. an email or operator-id) |
| `reason` | non-empty `str` | human-readable justification (surfaced through SRS-LOG-001 + SRS-NOTIF-001 when they land) |
| `audit_trail_id` | non-empty `str` | cross-reference to the persistent operator audit log entry |
| `timestamp_ns` | non-negative `int` | wall-clock nanoseconds since the Unix epoch; `bool` is rejected |

## Usage

```python
import os
from atp_readiness import (
    OperatorOverride,
    PreTradeHoldError,
    ReadinessGate,
)

gate = ReadinessGate.from_env(os.environ)

try:
    gate.assert_ready_or_hold()
except PreTradeHoldError as hold:
    # Surface for SRS-LOG-001 (log sink), SRS-UI-001 (dashboard),
    # and SRS-API-001 (REST/WebSocket).
    for record in gate.as_log_records():
        log.error(record)
    payload = gate.as_dashboard_payload()
    publish_to_dashboard(payload)
    # If the operator chooses to release the hold:
    gate.operator_override(
        OperatorOverride(
            actor="operator@example.com",
            reason="paper-only Reservoir warm-up; IB creds intentionally unset",
            audit_trail_id="audit-12345",
            timestamp_ns=time.time_ns(),
        )
    )
```

## Contract metadata

The single source of truth for the gate's state machine, override audit
fields, payload field set, and SDK-surface boundary lives in
`architecture/runtime_services.json#startup_readiness_gate_contract`. The
companion check script
`python3 tools/startup_readiness_gate_check.py` runs at every boot
(via `init.sh`) and on CI to enforce that the Python implementation, the
contract block, and the test rig stay in parity.
