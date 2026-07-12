# atp_reliability — market-hours availability measurement (SRS-REL-001)

`atp_reliability` produces the objective evidence SRS-REL-001 / SyRS **NFR-R1**
require: the availability of the platform **during US equity market hours**
(09:30–16:00 ET, Mon–Fri, excluding market holidays) over a **rolling 30-day
period**, measured only over positively-observed time and refusing to certify
what it did not measure.

It is an **offline verification/analysis** tool — *not* a core runtime service. It
runs on demand over durable evidence, participates in no trading decision, and is
the availability analog of the `crates/atp-types/src/perf.rs` latency substrate.
Python here is permitted by AC-16 (which constrains only *core runtime services*
to Rust) and consistent with the other Python operator/analysis packages
(`atp_readiness`, `atp_safety`, `atp_cli`). The concrete DST/holiday-aware trading
calendar and the durable log store it consumes are both Python.

## Layout

| Module | Role |
|---|---|
| `availability.py` | The **pure, clock-free engine**. Imports no `atp_*`; integer-ns internally. `compute_availability(...) -> AvailabilityVerificationArtifact`. |
| `evidence.py` | **Adapters** (the only layer that touches the calendar / log store): `market_sessions`, `sys75_exclusion_windows`, `reconstruct_downtime`, `downtime_from_log_records`. |
| `cli.py` / `__main__.py` | `python -m atp_reliability` — emits the artifact + a `verdict:` line, gates the exit code. |

## The NFR-R1 model

Availability is measured over `effective_market_ns = total_market_ns −
excluded_in_session_ns`, where market sessions come from the real
`UsEquityTradingCalendar` (13:00 ET early-close aware, DST-aware). The verdict is
three-valued and **fail-closed**:

- **PASS** — the analysis window is **exactly one 30-calendar-day rolling window**, is
  fully covered, has no in-session exclusions, and downtime ≤ 0.1% (integer per-mille
  gate `(1000 − target)·effective ≥ 1000·downtime`; the exact 0.999 boundary is inclusive).
- **FAIL** — fully measured but downtime breaches the objective.
- **INCONCLUSIVE** — refuses to certify: the window is **not exactly the rolling 30-day
  period** — shorter (a single trading day cannot certify a 30-day requirement) *or*
  longer (NFR-R1 is a rolling metric; a 60-day window could dilute a failing 30-day
  sub-period into a passing average); market time with **no coverage evidence** (no
  "no-data = 100% up" lie); or an exclusion that leaked into market hours.

### What counts

Only `OutageCause.HOST_UNPLANNED` counts — NFR-R1's *included* scope is unplanned
host-level outages (hardware failure, kernel panic). NFR-R1 **excludes** planned
maintenance and the SYS-75 scheduled IB Gateway restart (~23:45 ET). Sub-host
causes (`IB_CONNECTIVITY` → NFR-R2, `CONTAINER_CHURN` → NFR-R5, `KILL_SWITCH_HALT`
→ SRS-SAFE-001) are retained for audit but **do not count** as host availability.

> **Honesty boundary.** A dead host emits no logs, so `downtime_from_log_records`
> can never witness a host-level outage — it only ever emits non-counting subsystem
> signals. The counted host downtime and the positive coverage both come from the
> deferred host-liveness feed (below).

### The "1.17 minutes" reconciliation

The SyRS NFR-R1 parenthetical "≤ 1.17 minutes downtime per trading day on average"
is a **non-binding approximation**: 0.1% of a 6.5-hour (23,400 s) session is
**23.4 s**, not 70.2 s. The machine gate is the ratio (`target_per_mille = 999`);
average downtime per session is reported for information only.

## Usage

The `python/` tree is not pip-installed (repo convention, like `atp_cli` /
`atp_runtime`), so run the CLI with `PYTHONPATH=python` from the repo root:

```bash
# Evidence fixture: period as exchange/start_date/end_date (sessions derived from
# the calendar → always complete) + observed covered/downtime/excluded_windows (ns):
PYTHONPATH=python python -m atp_reliability --fixture evidence.json [--json]

# Calendar-derived window + SYS-75 exclusions, optionally reading a log store
# (start_date/end_date must span exactly 30 calendar days to be certifying):
PYTHONPATH=python python -m atp_reliability --calendar --start 2026-01-05 --end 2026-02-03 \
    [--log-store data/logs/system.jsonl] [--json]
```

`evidence.json` shape (a certifying fixture spans **exactly** 30 calendar days —
`start_date`..`end_date` inclusive — and covers every session):

```json
{ "exchange": "NYSE", "start_date": "2026-01-05", "end_date": "2026-02-03",
  "covered": [[<start_ns>, <end_ns>], ...],
  "downtime": [[<start_ns>, <end_ns>, "host_unplanned"], ...],
  "excluded_windows": [[<start_ns>, <end_ns>], ...] }
```

Exit code: `0` only on `PASS`; `1` on `FAIL`/`INCONCLUSIVE`; `2` on refused input.
Calendar mode has **no coverage oracle** (the host-liveness feed is deferred), so it
is honestly `INCONCLUSIVE` — there is deliberately no flag to synthesise coverage and
certify. A certifying `PASS` requires explicit coverage evidence (`--fixture`).

## Status: serialized (`passes: false`)

The measurement mechanism is complete and fixture-verified. SRS-REL-001 stays
`passes: false` (its own step 4: *"leave passes false until the evidence proves the
requirement end to end"*) — proving ≥ 99.9% over 30 **real** operational days is a
deployment/operator activity that cannot run in the parallel sandbox. Deferred
owners (also in `availability_measurement_contract.deferred`):

1. The real **host-liveness / heartbeat-cadence feed** that produces positive
   coverage and is the only oracle that can witness unplanned host death.
2. The **operator host-outage ledger** (planned vs unplanned classification).
3. The **Rust→operator log-forwarding** path unifying core (Rust) + operator
   (Python) events into one durable store.
4. The **30-real-day operational proof run** (NFR-R1 analysis + system test).
