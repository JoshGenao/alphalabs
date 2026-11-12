# SRS-MD-004 plan - stale market data blocks live and paper orders

## Context

**Feature:** SRS-MD-004 (P1, MVP, method `solo`). Acceptance criterion: "While subscribed
market data is stale, live and paper order submissions return MARKET_DATA_STALE;
submissions resume when fresh data is received."

**Supposed to:** a real freshness source decides whether an order may go out, on both
the live path and the paper path.

**Actually:**
- The live gate exists (`submit_live_order`, `crates/atp-execution/src/lib.rs:565`), but
  every caller passes a fixture probe that always says "fresh". No production probe exists.
- The paper path (`dispatch_order`, `crates/atp-execution/src/order_routing.rs:250`) has no
  freshness check at all. Its doc comment says that is MD-004's job.
- The live host refuses `--transport ib` because no production probe exists
  (`crates/atp-orchestrator/src/bin/live_execution_host.rs:189-200`).

**Why:** `atp-execution` may not depend on `atp-market-data` (SRS-ARCH-002), so the bridge
from the SRS-MD-003 monitor to the execution port was deferred to MD-004.

**Prior progress:** none. No session note. The contract `docs/contracts/SRS-MD-004.json`
was drafted by the harness commit `42bcec9` and defines C1-C3, I1, S1.

**Dependencies:** only `SRS-MD-004 -> SRS-MD-003`, typed `verification` (high confidence).
It does not block building. It does block `--mode complete`, because MD-003 is not
`passes:true`.

**Playbooks loaded:** adversarial-precheck, test-integrity (always-on), safety-paths,
lifecycle-and-concurrency, scope-and-serialization.

## Expected outcome

**`implemented`** - C1-C3 pass and the independent review approves; `passes` stays false.
Open after this: I1 (real IB paper-gateway window, needs MD-003 live) and S1 (closes once
MD-003 verifies). Class for the note: **B/verification** - waits on SRS-MD-003's
verification, not on code.

## What I will build

### 1. Shared freshness store - `crates/atp-market-data/src/freshness_board.rs` (new)
- `LineFreshnessBoard`: `Arc<Mutex<..>>` over a `HeartbeatFreshnessMonitor` plus an
  explicit `GapTracking` enum chosen at construction:
  - `Unavailable` - the provider has no upstream sequence (IB). Verdict = time freshness.
  - `Tracked(SequenceGapDetector)` - verdict = `combined_line_freshness`.
  This keeps "no gap source" distinct from "empty gap source". An empty detector would
  otherwise read every line as stale.
- The board owns ONE clock (`FreshnessClock` trait). Writers stamp and readers evaluate with
  the same clock, so they cannot drift into different clock domains.
- Writer API: `watch`, `observe_security`, `observe_tick`. Reader API:
  `line_freshness(&SecurityKey) -> LineVerdict { freshness, age: Option<Duration> }`,
  computed at read time from the clock. The board never caches a verdict.
- Fail closed: a poisoned lock reads `Stale` with no age; never-observed and unwatched lines
  read `Stale`; an uncanonicalizable key (option without contract identity) reads `Stale`.
- No I/O and no sink publishing under the lock. The order path does one map lookup, which
  keeps the gate well inside NFR-P1 (safety-paths rule 52).

### 2. Security-aware, age-honest port - `crates/atp-execution/src/lib.rs`
- `freshness(&self, symbol: &str, asset_class: AssetClass)` and
  `staleness_seconds(..) -> Option<u64>`. `runtime_services.json`
  `sequence_gap_contract.deferred[]` assigns this port change to MD-004.
- `StaleDataEvent.staleness_seconds` becomes `Option<u64>` (`crates/atp-types`). No schema
  mirrors this event. The rejection message says "age unknown, never observed" instead of a
  made-up `0s`.
- Update the 4 call sites (single-leg and multi-leg; multi-leg passes `AssetClass::Option`
  plus the contract key, which fails closed) and all 19 impls (6 in src, 13 in tests). The
  change is mechanical.

### 3. Paper gate - `crates/atp-execution/src/order_routing.rs`
- In the `InternalSimulation` arm only, after `validate()` and before `submit_simulated`:
  if the line is stale, record a `StaleDataEvent` and return `MarketDataStale` with the same
  category and `error_type` as live (SYS-64 parity). No connectivity check, because paper
  never touches IB.
- Put the gate in the arm, not before the route match, or live orders get checked twice.
- Rewrite the stale doc text at `:73-75` and `:192-202`.

### 4. Production adapter - `crates/atp-orchestrator/src/market_data_freshness.rs` (new)
- `BoardFreshnessProbe` implements `MarketDataFreshnessProbe` over the board (Send + Sync).
- `MonotonicEpochClock`: epoch reading at start plus `Instant::elapsed`. A wall-clock step
  backwards would otherwise clamp a dead line's age to 0 and read it as fresh. Residual to
  document: the monotonic clock may pause during host sleep.

### 5. Live host message and deferred entries
- Do not wire `LiveFeedLoop` into the board in this feature. `LiveFeedLoop::monitor()` has
  0 callers. Wiring needs the loop's clock and event publishing restructured, which is the
  live host's IB-tier composition (SRS-EXE-001).
- Update the IB-tier refusal text to name the remaining blocker: composing the MD-003 feed
  loop into the board, owned by SRS-EXE-001.
- Re-point `deferred[]` entries in `order_routing_contract`, `sequence_gap_contract` and
  `heartbeat_freshness_contract` (`architecture/runtime_services.json`). Extend
  `freshness_contract` with the paper entry point.

### 6. Static guard - `tools/freshness_check.py`
- Pin the paper gate's Stale branch the way the live branch is pinned, so a removed paper
  gate fails a check, not only a test.

## Tests

| Obligation | Test | What it proves |
|---|---|---|
| C1 | `crates/atp-market-data/tests/srs_md_004_freshness_probe.rs` (new) | Injected clock, no sleeps: never-observed is stale with no age; 15.000 s is fresh and 15 s + 1 ns is stale; a new tick makes it fresh; both `GapTracking` modes (a gap makes a time-fresh line stale); poisoned lock is stale; option key is stale. |
| C2 | `crates/atp-execution/tests/err_3_stale_data_blocked.rs` (extend) | Through `route_order` with a designated live strategy: stale gives MARKET_DATA_STALE and 0 broker calls; fresh gives exactly 1. Plus the durable path the live host really uses (`route_order_durably`): stale gives no broker call and a terminal outbox entry. |
| C3 | `crates/atp-execution/tests/srs_md_004_paper_stale_gate.rs` (new) | Through `dispatch_order`: stale paper order gives MARKET_DATA_STALE, 0 simulation calls, 0 broker calls; after fresh, the next order reaches the simulation port. Non-vacuity: the live order on the same engine still blocks. |
| C4 (new) | `crates/atp-orchestrator/tests/srs_md_004_stale_gate_wired.rs` (new) | Real composition: board + `BoardFreshnessProbe` + `dispatch_order` + `WiredPaperSimulation`. Stale blocks live and paper; a tick resumes both, and the paper order rests in the `VirtualOrderBook`. |
| L7 domain | `tests/domain/test_stale_data_block.py` (extend) | Shells the C3 and C4 tests and confirms they assert (required by the safety-path rule). |
| L3 contract | `tests/test_freshness_contract.py` (extend) | The paper entry point in `freshness_contract`. |

Every new Rust test is mutation-verified by hand (remove the gate, watch it go red, restore).
`mutation_verify.py` covers the Python tests only (test-integrity rule 41).

## Contract amendment - `docs/contracts/SRS-MD-004.json`
- Add the orchestrator adapter, `freshness_board.rs` and `atp-types` to `implementation_paths`.
- C3 criterion: "accepted by the simulation port" instead of "filled by the simulator".
  `WiredPaperSimulation` only accepts and rests orders; the fill loop is deferred to
  SRS-SIM-002/003/004. Record that in `limitations`. The system obligation S1 stays verbatim.
- Add C4 (above).

## Size and surfaces
- About 1,400-1,600 lines (code roughly 650, tests roughly 800). This is near the split line.
  Splitting would leave one half unable to close (a probe with no paper gate, or a gate with
  no producer), so I keep it as one feature.
- Operator surfaces: none new. No REST, CLI, WebSocket or dashboard change. Step 2's "CLI/API
  workflows" is met by the Rust API tests; I add no CLI.

## Verification
1. `./init.sh` until "Environment ready" (evidence step 1).
2. `python3 tools/evidence.py run SRS-MD-004 --obligation C1 -- cargo test -p atp-market-data --test srs_md_004_freshness_probe`, and the same for C2, C3, C4.
3. `python3 tools/evidence.py run SRS-MD-004 --step 2|3 -- <C4 test>` and the domain test.
4. `python3 tools/freshness_check.py`, `python3 tools/contracts.py check`, `python3 tools/evidence.py implemented SRS-MD-004`.
5. Gate: `tools/run_ci_locally.sh`, `cargo test --workspace` (after `pgrep -x cargo` is empty), `pytest -m "not integration and not e2e"`.
6. Critics: `critic_check.py --staged`, `mutation_verify.py origin/main..HEAD`, `adversarial_review.py origin/main`.
7. `python3 tools/agent_pool.py integrate SRS-MD-004 --mode implemented`; session note with `Outcome: implemented (I1 live IB window; S1 waits on SRS-MD-003 verification)`.

First action after approval: save this plan to `progress.d/plan-SRS-MD-004.md` and extend
the lease with `agent_pool.py heartbeat SRS-MD-004`.
