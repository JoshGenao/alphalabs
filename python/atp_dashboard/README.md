# atp_dashboard — SRS-UI-001 web dashboard

A self-contained web dashboard showing **live performance**, **system health**,
**latency**, and **benchmark-relative metrics**, refreshing within the **NFR-P2
5-second** budget. It is the top layer: it imports only `atp_runtime`,
`atp_readiness`, and `atp_ws` (never a core trading engine or a vendor SDK).

## Architecture

The dashboard is built on the `SRS-API-001` operator-interface runtime
(`atp_runtime.OperatorInterfaceRuntime`) — it does **not** run its own server.

```
ReadinessBackedProvider ──payloads──▶ DashboardPublisher ──runtime.publish()──▶ WS  /ws/v1
        │                                                                        (PNL·METRICS·HEARTBEAT)
        └──system_snapshot()──▶ runtime.register_meta_route ──▶ GET /dashboard/api/system
   assets/{index,styles,app} ──▶ runtime.register_asset_routes ──▶ GET /dashboard[/*]
```

* **`provider.py`** — assembles the four metric groups. Only the readiness
  snapshot (`atp_readiness.ReadinessGate.as_dashboard_payload`) is real today;
  metric values owned by not-yet-built features are surfaced as honest
  `{"value": null, "data_source": "deferred:<owner>"}` placeholders — **never
  fabricated**.
* **`publisher.py`** — a daemon ticker that claims the `PNL`/`METRICS`/`HEARTBEAT`
  publishers and pushes each channel at its declared `refresh_seconds` (≤5 s).
* **`assets/`** — a dependency-free, single-page dashboard (no external CDN/fonts).
* **`server.py`** — `mount_dashboard(runtime, provider)` wires the routes; `serve()`
  is the blocking process entrypoint.

## Run

```bash
python -m atp_dashboard        # binds 127.0.0.1:$ATP_DASHBOARD_PORT|$ATP_DEV_PORT|8080
# then open http://127.0.0.1:<port>/dashboard
```

Binding is loopback / RFC-1918 only (`SRS-SEC-002`), enforced fail-closed by
`runtime.start`. The dashboard is a **read-only** monitoring surface — it exposes
no order/kill-switch mutation; those remain behind the runtime's confirmation guard.

## Deferred producers (why metric values show "—")

| Group | Channel / field | Owning feature |
|-------|-----------------|----------------|
| Live performance | `PNL` (daily/cumulative/unrealized) | `SRS-BT-004` |
| Benchmark-relative | `METRICS` sharpe/sortino/alpha/beta/drawdown | `SRS-BT-004` |
| Benchmark-relative | `METRICS` benchmark_return (vs SPY) | `SRS-BT-005` |
| System health | `HEARTBEAT` feed/staleness | `SRS-MD-007` |
| System health | readiness probes (IB/SSD/NAS) | `SRS-MD-006` |
| Latency | order/pipeline p95 percentiles | `SRS-PERF-001` |

The client measures its **own** observed refresh latency (real) and renders it
against the 5,000 ms budget — the live proof of the ≤5 s SLA.

## Verification

* `pytest tests/unit/test_dashboard_provider.py tests/unit/test_dashboard_publisher.py`
  — provider honesty + publisher cadence/lifecycle (L1).
* `pytest tests/boundary/test_dashboard_wiring.py` — real HTTP/WS round-trip,
  ≤5 s refresh (L4).
* `pytest tests/domain/test_dashboard_safety.py` — read-only / loopback / no
  fabrication (L7).
* `ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_refresh.py` — browser demonstration
  (L6, Playwright; deferred). Passing this plus the 30-paper baseline-load NFR-P2
  performance run flips `SRS-UI-001` to `passes:true` (`verified-e2e`).
