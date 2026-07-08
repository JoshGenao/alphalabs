# atp_config — Configuration system (SRS-ARCH-005)

`atp_config` is the declarative source of truth for ATP's required configuration
keys, their validation rules, and the structured readiness-report shape returned
by `load_and_validate`. The catalogue itself is loaded from
`architecture/runtime_services.json` so the JSON file remains the single source
of truth; this package wraps it in dataclasses and validation logic.

This package satisfies SRS-ARCH-005:

> The software shall provide a configuration system for credentials, storage
> paths, IB account settings, market-data line limits, resource limits, and
> notification channels. **AC:** Required configuration keys are documented,
> validated at startup, and reported as structured readiness failures when
> missing or invalid.

It does **not** implement encryption-at-rest for credentials (NFR-S1, NFR-S4 —
that is SRS-SEC-001's job) and it does **not** implement live-trading runtime
readiness (IB connectivity, ingestion freshness — that is SYS-76 / SRS-MD-006).
ARCH-005 is the catalogue + static validator that those features will consume.

## Categories

| Category | Purpose |
|---|---|
| `credentials` | Vendor data-provider API keys (Databento, Sharadar). |
| `storage_paths` | SSD primary tier and NAS archival tier paths. |
| `ib_account` | Interactive Brokers Gateway host, live/paper ports, deployment selector, and the brokerage account identifier (secret; SRS-SEC-001). |
| `market_data_limits` | Operator-configured IB market-data subscription line cap. |
| `resource_limits` | Live and paper strategy memory/CPU caps and the host memory safety margin. |
| `notification_channels` | Email and SMS dispatch credentials. |

## Required keys (19)

| Key | Category | Type | Default | Secret | SRS trace |
|---|---|---|---|---|---|
| `ATP_ENV` | ib_account | enum | `development` | no | SRS-ARCH-005, SRS-ARCH-004 |
| `ATP_IB_HOST` | ib_account | host | `127.0.0.1` | no | SRS-EXE-006 |
| `ATP_IB_LIVE_PORT` | ib_account | int | `4001` | no | SRS-EXE-006, SyRS:AC-15 |
| `ATP_IB_PAPER_PORT` | ib_account | int | `4002` | no | SRS-EXE-006, SyRS:AC-15 |
| `ATP_IB_ACCOUNT` | ib_account | secret | placeholder | yes | SRS-SEC-001, NFR-S1, StRS:C-3 |
| `ATP_MARKET_DATA_LINE_LIMIT` | market_data_limits | int | `100` | no | SRS-MD-002, SyRS:SYS-70 |
| `ATP_SSD_DATA_DIR` | storage_paths | path | `/var/lib/atp/ssd` | no | SRS-DATA-008 |
| `ATP_NAS_DATA_DIR` | storage_paths | path | `/var/lib/atp/nas` | no | SRS-DATA-008/009 |
| `ATP_BACKTEST_RESULTS_DIR` | storage_paths | path | `/var/lib/atp/ssd/backtest_results` | no | SRS-BT-009, SyRS:SYS-79 |
| `ATP_DATA_STORE_DIR` | storage_paths | path | `/var/lib/atp/ssd/market_data` | no | SRS-DATA-016, SyRS:NFR-R4 |
| `ATP_SMTP_API_KEY` | notification_channels | secret | placeholder | yes | SRS-NOTIF-001, NFR-S4 |
| `ATP_SMS_API_KEY` | notification_channels | secret | placeholder | yes | SRS-NOTIF-001, NFR-S4 |
| `DATABENTO_API_KEY` | credentials | secret | placeholder | yes | SRS-DATA-001, NFR-S1 |
| `SHARADAR_API_KEY` | credentials | secret | placeholder | yes | SRS-DATA-005, NFR-S1 |
| `ATP_LIVE_STRATEGY_MEM_MB` | resource_limits | int | `512` | no | SRS-ORCH-002, SyRS:SYS-11/57 |
| `ATP_LIVE_STRATEGY_CPU` | resource_limits | float | `0.25` | no | SRS-ORCH-002, SyRS:SYS-11 |
| `ATP_PAPER_STRATEGY_MEM_MB` | resource_limits | int | `300` | no | SRS-ORCH-002, SyRS:SYS-11 |
| `ATP_PAPER_STRATEGY_CPU` | resource_limits | float | `0.10` | no | SRS-ORCH-002, SyRS:SYS-11 |
| `ATP_HOST_MEMORY_SAFETY_MARGIN_MB` | resource_limits | int | `2048` | no | SRS-ORCH-003, SyRS:SYS-57/58 |

## Validation rules

- `int`: parses as integer; range `min..max` (per key).
- `float`: parses as float; range `min..max` (per key).
- `path`: non-empty string; must be absolute (start with `/`).
- `host`: non-empty string.
- `enum`: must be one of the declared `choices`.
- `secret`: non-empty string. The literal placeholder
  `placeholder-set-in-environment` is a **warning** when `ATP_ENV=development`
  and a hard **error** when `ATP_ENV` is `staging` or `production`. This lets
  the dev-mode `init.sh` defaults pass while still gating real deployments.

## Structured readiness failures

`load_and_validate(env)` returns a `ReadinessReport`:

```python
ReadinessReport(
    failures=[
        ReadinessFailure(
            key="ATP_MARKET_DATA_LINE_LIMIT",
            category=Category.MARKET_DATA_LIMITS,
            severity=Severity.ERROR,
            reason="expected integer, got 'oops'",
            srs_trace=("SRS-MD-002", "SyRS:SYS-70"),
        ),
    ],
    evidence=[
        "SRS-ARCH-005 configuration system evidence:",
        "19 keys catalogued across 6 categories (ATP_ENV='development')",
        "credentials: 2 keys — OK (DATABENTO_API_KEY, SHARADAR_API_KEY)",
        "...",
    ],
)
```

`ReadinessReport.ok` is true only when there are no error-severity failures.
Each `ReadinessFailure` exposes `as_dict()` and `as_json_line()` so the report
can be emitted as JSON-lines for log/dashboard ingestion (SRS-ERR-9: hold the
system in pre-trade state and expose readiness failure through logs, dashboard,
and API).

## Usage

```python
import os
from atp_config import load_and_validate

report = load_and_validate(os.environ)
if not report.ok:
    for failure in report.errors:
        print(failure.as_json_line())
    raise SystemExit(1)
```

For the static contract check used by `init.sh` and `architecture_check.py`,
run:

```
python3 tools/config_check.py
```
