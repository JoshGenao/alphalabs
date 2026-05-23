# atp_logging — Log record schema + sink routing (SRS-LOG-001 SDK-surface)

`atp_logging` is the cross-language source of truth for ATP's structured
**log record schema** and the **system-vs-strategy sink routing** boundary
SRS-LOG-001 requires. The package consumes nothing — it is upstream of the
persistent log sinks (deferred to SRS-LOG-001's runtime half), the
dashboard log pane (deferred to SRS-UI-001), and the live `GET /api/v1/logs`
endpoint + `LOGS` WebSocket publisher (deferred to SRS-API-001).

This package satisfies the **SDK-surface half** of SRS-LOG-001:

> The software shall separate persistent system logs from user strategy logs.
> System events and user strategy logs are stored with timestamp, severity,
> source, event type, message, and correlation ID; system logs include order
> routing outcomes, ingestion job lifecycle, container lifecycle, IB Gateway
> connection state changes, kill-switch activations, Hot-Swap events,
> resource threshold alerts, and market data subscription changes; both log
> classes are viewable from the dashboard.

It does **not** ship:

- the concrete persistent sinks (file / SQLite / Loki / etc.) — deferred to
  SRS-LOG-001's runtime half;
- the `GET /api/v1/logs` REST handler body — deferred to SRS-API-001;
- the `LOGS` WebSocket channel publisher — deferred to SRS-API-001;
- the dashboard log pane rendering — deferred to SRS-UI-001;
- the `admin logs` CLI runner — deferred to SRS-API-001's operator-
  interface-runtime;
- the audit-log retention + rotation policy — deferred to SRS-LOG-001.

Because those five surfaces are not yet built, `SRS-LOG-001` stays
`passes:false` in `feature_list.json`. The contract here is what they
consume when they arrive.

## Schema

| Field | Type | Required | Description |
|---|---|---|---|
| `timestamp_ns` | non-negative `int` | yes | wall-clock nanoseconds since the Unix epoch; `bool`, `float`, negative, and non-finite values are rejected |
| `severity` | `Severity` | yes | one of `DEBUG / INFO / WARN / ERROR / CRITICAL` (SyRS SYS-61 verbatim) |
| `source` | `Source` | yes | one of the nine source components (eight SYSTEM + STRATEGY) |
| `event_type` | non-empty `str` | yes | for SYSTEM records constrained to `EVENT_TYPES_BY_SOURCE[source]`; for STRATEGY records free-form |
| `message` | non-empty `str` | yes | free-form human-readable message |
| `correlation_id` | non-empty `str` | yes | cross-component trace identifier per SyRS SYS-61 |
| `log_class` | `LogClass` | yes | `SYSTEM` or `STRATEGY` — the sink-routing discriminant |
| `strategy_id` | `str \| None` | conditional | required non-empty when `log_class == STRATEGY`; must be `None` when `log_class == SYSTEM` |

`LogRecord` is a frozen dataclass — once dispatched a record cannot be
mutated en route to the persistent sink (which would invalidate the audit
trail).

## Sources and event types

`SYSTEM_SOURCES` (the eight system-log emitter components from SyRS SYS-61):

| Source | Allowed event types |
|---|---|
| `ORDER_ROUTING` | `ROUTING_DECISION`, `ROUTING_OUTCOME` |
| `INGESTION` | `JOB_START`, `JOB_COMPLETION`, `JOB_FAILURE` |
| `CONTAINER_LIFECYCLE` | `CONTAINER_START`, `CONTAINER_STOP`, `CONTAINER_RESTART`, `OOM_KILL` |
| `IB_GATEWAY` | `CONNECT`, `DISCONNECT`, `RECONNECT` |
| `KILL_SWITCH` | `ACTIVATION` |
| `HOT_SWAP` | `PROMOTION`, `DEMOTION` |
| `RESOURCE_MONITOR` | `THRESHOLD_ALERT` |
| `MARKET_DATA` | `SUBSCRIPTION_CHANGE` |

`STRATEGY_SOURCES` is `{Source.STRATEGY}` only. Strategy-class event types
are not enforced — the AC explicitly leaves strategy event naming to the
strategy author per SN-2.02 ("a logging API that the user invokes from
within their Python strategies").

## Dispatcher contract

`RoutedLogDispatcher` validates every record at the dispatch boundary
before routing to the bound `LogSink` for its `log_class`:

```python
from atp_logging import (
    LogClass,
    LogRecord,
    RoutedLogDispatcher,
    Severity,
    Source,
)

dispatcher = RoutedLogDispatcher()
dispatcher.register_sink(LogClass.SYSTEM, system_sink)
dispatcher.register_sink(LogClass.STRATEGY, strategy_sink)

dispatcher.dispatch(
    LogRecord(
        timestamp_ns=time.time_ns(),
        severity=Severity.INFO,
        source=Source.KILL_SWITCH,
        event_type="ACTIVATION",
        message="Operator triggered kill switch from dashboard",
        correlation_id="ks-2026-05-22-001",
        log_class=LogClass.SYSTEM,
    )
)
```

The dispatcher raises one of the `LogRecordError` subclasses on rejection:

| Error | Raised when |
|---|---|
| `LogPayloadError` | type / range / non-empty guard fails on any field |
| `LogClassError` | cross-field invariant between `log_class`, `source`, and `strategy_id` fails |
| `LogRoutingError` | no sink is registered for the record's `log_class` |
| `LogSinkError` | the registered sink raised an exception (original on `__cause__`) |

All four subclass `LogRecordError`, so a downstream caller can catch the
family with a single `except LogRecordError` clause.

## Contract metadata

The single source of truth for the schema, enum variants, allowed event
types, sink protocol, and SDK-surface boundary lives in
`architecture/runtime_services.json#log_record_contract`. The companion
check script `python3 tools/log_record_check.py` runs at every boot (via
`init.sh`) and on CI to enforce that the Python implementation, the
contract block, and the test rig stay in parity.
