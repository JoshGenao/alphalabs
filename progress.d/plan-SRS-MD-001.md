# SRS-MD-001 - fan-out runtime (thread-safe, gated upstream binding, bounded delivery)

## Context

**Feature:** SRS-MD-001 (P1, MVP, `verification_method: live-ib`). AC: *Multiple strategies
subscribing to the same security consume one IB subscription; each subscriber receives fan-out
data with no more than 100 ms additional latency relative to the IB feed.*

Already on main: `ConsolidatedSubscriptionRegistry` (`crates/atp-market-data/src/subscriptions.rs`)
- single-threaded dedup, routing, line accounting, change events, gated by SRS-MD-005's
`RestartWindowGate`. Deferred: the "SRS-MD-001-runtime" item in
`architecture/runtime_services.json` -> `subscription_fanout_contract.deferred[]`.

Operator decisions (plan approval, 2026-10-14):
- Build the runtime and integrate `--mode serialized` (passes stays false). The real IB feed and
  PTP-clock evidence stay with the operator / SRS-PERF-001.
- Slow subscriber policy: bounded per-subscriber queue; on overflow drop that subscriber's tick,
  count it, publish one lag event per episode. Never block other subscribers.

## What was built

`crates/atp-market-data/src/runtime.rs` (new, `pub mod runtime` in `lib.rs`), `std` only:
- `UpstreamMarketDataFeed` port (`open` / `close`) - vendor-neutral; the crate may depend only on
  `atp-types`, so the IB bridge belongs in the composition layer (deferred).
- `FanOutRuntime<F, W, C, L>`: one `Mutex` around the registry + per-subscriber queues.
  - `subscribe`: first subscriber -> `window.admission()` (opening a line is a market-data request,
    SYS-75(a)) -> limit probe -> `feed.open` -> registry's gated `subscribe`; close the line again
    if the registry refuses. Joining an existing line sends nothing upstream (registry still gates).
  - `unsubscribe` closes on `Closed` only; never gated (registry's own exemption).
  - `ingest`: `Arc<FanOutDelivery>` + non-blocking `try_send`; `Full` -> drop + count + one
    `SubscriberLagEvent` per episode; `Disconnected` -> deregister + release sole-held lines.
  - Poisoned lock -> every mutation fails closed; SRS-MD-002 probe reports `ExceededLimit`.
  - Tracks each subscriber's keys itself, so it never names the registry's private map (the
    SRS-MD-005 `admission_sites` closure in `tools/connectivity_check.py` is untouched).

## Tests

- `crates/atp-market-data/tests/srs_md_001_fanout_runtime.rs`: 14 boundary/domain tests over a
  fake feed + controllable window gate; plus an `#[ignore]`-gated wall-clock latency PROBE that
  uses SRS-PERF-001's `LatencyPercentiles` and writes
  `target/verification/srs_md_001_fanout_latency_probe.json` (labelled as NOT the AC artifact).
- `tests/domain/test_subscription_fanout_runtime.py` (L7): shells each Rust test, runs the probe
  with `--ignored`, asserts the report's honesty labels, pins `passes:false`.
- `tests/test_subscription_fanout_contract.py` (L3): 11 new in-memory mutation tests for the new
  `tools/subscription_fanout_check.py` runtime checks.

## Completeness

`serialized` (class C/B mix): steps 1-2 solo PASS; step 3's dedup half PASS solo; the latency
half needs the real IB feed + PTP host (`LatencyVerificationArtifact` refuses an undisciplined
clock). Remaining owners: the composition-layer IB bridge (adapter needs cancel + tick stream),
reconnect re-open, cross-container transport, operator live run.

## Port note

The worktree was created 573 commits behind `origin/main`. The first implementation was written
against the stale base, saved, and re-ported after fast-forwarding: the registry had moved to
`subscriptions.rs` and gained the restart-window port, and SRS-PERF-001's percentile substrate had
landed. A planned CI-wiring change was dropped: `tests/test_subscription_fanout_contract.py`
already runs the check inside the pytest gate.
