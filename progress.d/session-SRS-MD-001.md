=== SESSION SRS-MD-001 ===
Date: 2026-10-14
Feature: SRS-MD-001 - consolidate duplicate real-time market data subscriptions across active strategies
Outcome: serialized (B: the AC's latency half needs the real IB feed measured on a PTP host - SRS-PERF-001 is unbuilt for that leg - and this feature's own IB bridge is the next landing)

## What I did

Built the SRS-MD-001 runtime that the registry deferred: `FanOutRuntime` in
`crates/atp-market-data/src/runtime.rs` (std only, no new dependency).

* One upstream line per security through a vendor-neutral `UpstreamMarketDataFeed`
  port (`open` / `close`). The crate may depend only on `atp-types`, so the IB
  implementation belongs in the composition layer (not built here).
* Opening a line IS a market-data request, so the first subscriber consults the
  SRS-MD-005 `RestartWindowGate` BEFORE `open`, then the limit probe, then `open`,
  then the registry's own gated `subscribe`; a refusal after the open closes the line
  again. The runtime never names the registry's private map, so the SRS-MD-005
  `admission_sites` closure (`tools/connectivity_check.py`) is untouched and green.
* Delivery: one bounded `sync_channel` per strategy; `ingest` wraps a tick once in an
  `Arc` and uses non-blocking `try_send`. Operator policy (plan approval): a full
  queue drops that subscriber's tick, counts it, and publishes one `SubscriberLagEvent`
  per lagging episode; other subscribers never wait.
* A dropped receiver leaves a `(strategy, generation)` departure note that EVERY call
  reaps first, so an idle security's line cannot leak against the IB line limit; the
  generation match stops a stale note from tearing down a re-registration.
* Poisoned lock fails every mutation closed; the SRS-MD-002 probe reports
  `ExceededLimit`. An "impossible" release failure panics (poisons) rather than leaking.
* Port contract written down: no re-entry, runtime lock first; `close` is infallible
  and the bridge must deliver a queued cancel before any later `open`.
* Fixture latency PROBE (`#[ignore]`-gated wall-clock test) uses SRS-PERF-001's
  `LatencyPercentiles`; its report says it is NOT the AC's artifact.
* Fixed along the way: two broken intra-doc links in `live_feed.rs` (rustdoc now 0
  warnings for the crate).

Size: 2,387 added lines (1,113 of them the Rust test file), above the ~1,500 split
guideline. No operator surface (REST/CLI/WS/dashboard) was touched.

## Port note - the worktree was 573 commits stale

`claim_and_work.sh` created this worktree 573 commits behind `origin/main`. The first
implementation was written against that base; I saved it, fast-forwarded to main, and
re-ported it onto main's changes: the registry had moved to `subscriptions.rs` with a
required restart-window port, and SRS-PERF-001's percentile substrate had landed. A
planned CI-wiring change was dropped after the port: `tests/test_subscription_fanout_contract.py`
already runs the fan-out check inside the pytest gate, and `run_ci_locally.sh` already
treats mypy as advisory.

## What I tested (per step)

Recorded in `.harness/runs/SRS-MD-001/` (run via `tools/evidence.py run`):
* Step 1: PASS - `./init.sh` -> "Environment ready".
* Step 2: PASS - `cargo test -p atp-market-data --test srs_md_001_fanout_runtime -- --include-ignored`
  (20 tests + probe) and `pytest tests/domain/test_subscription_fanout_runtime.py
  tests/domain/test_subscription_fanout_dedup.py tests/test_subscription_fanout_contract.py -q`
  (91 passed). Probe on the fixture feed: p50 7.8 us, p95 15.4 us, p99 22.7 us,
  p99.9 35.7 us, max 61.5 us (budget 100 ms) - host monotonic clock, no IB.
* Step 3: NOT RECORDED - dedup half proven solo (one `open` for N subscribers, incl. 8
  concurrent threads); the "<= 100 ms relative to the IB feed" half needs the real
  feed + PTP host. verification_method is `live-ib`, so it also needs an image.
* Step 4: NOT RECORDED - needs the integration + performance run above.
* Full gate: `tools/run_ci_locally.sh` green, every step ran (pytest 5595 passed;
  cargo test --workspace; 85/85 + 23/23 contract checks).
* Mutation: `mutation_verify.py` says all 18 added Python tests go red, but they all
  depend on NEW files (weak, per test-integrity rule 29), so I hand-mutated the
  runtime: M1-M11 each killed a named test (window check, rollback, lag reset, close
  guard, key tracking, reap in lock, generation match, reaped count, fail-open lock,
  poisoned probe, Disconnected path keeps queue).

## Critic verdicts

  deterministic: APPROVE - no findings on every commit and on origin/main..HEAD.
  judgment (tools/adversarial_review.py, claude-fallback - Codex output was
  unparseable every round): 5 rounds, all WARN, no BLOCK.
    r1 idle-line leak on dropped receiver + test + lock-order docs - FIXED
    r2 close contract, poisoned-lock test, read() reaps - FIXED
    r3 no drop-during-ingest test - FIXED (stress test)
    r4 r3's overlap assertion was vacuous; probe ran in default gate; swallowed
       release error; empty-id precedence - FIXED (deterministic in-lock test)
    r5 module doc understated what runs under the lock - FIXED (docs only)
  Override: the recorded judgment verdict is `warn` (round 5). Its one finding was
  documentation and is fixed in 6a3293b; per the workflow, a warn-only doc batch does
  not need another full round. The last substantive round (r5) covered the tree
  except that one doc edit and the live_feed doc-link fix.

## Resume / next

To flip passes:true, in this order:
1. Build the composition-layer bridge (atp-orchestrator, beside
   `live_market_data::IbLiveTickSource`): implement `UpstreamMarketDataFeed` over the IB
   adapter. `MarketDataAdapter` exposes `subscribe_market_data` only, so it needs a
   cancel call and a tick stream that calls `FanOutRuntime::ingest` with the upstream
   receive `Instant`. `interactive_brokers.rs` / `wire.rs` are SHA-pinned by
   SRS-EXE-006 evidence; add new seams in their own module (as SRS-MD-005 did).
2. Re-open every still-held line after an IB Gateway reconnect (the restart drops all
   upstream subscriptions).
3. Cross-container transport from a `SubscriberReceiver` into each strategy container.
4. Operator live run: real feed, PTP host, `LatencyVerificationArtifact` for the
   SRS-MD-001 fan-out leg, plus the step-3 image.
