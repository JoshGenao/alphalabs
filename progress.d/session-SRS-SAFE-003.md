=== SESSION SRS-SAFE-003 ===
Date: 2026-07-26 (session 1 — build); 2026-07-27 (session 2 — de-churn, see the last section)
Feature: SRS-SAFE-003 — block live order submission when IB Gateway is unreachable
         (docs/SRS.md:246 SRS-5.9; ERR-2 row docs/SRS.md:291; SyRS SYS-45/SYS-64/NFR-R2; StRS SN-2.04)
Outcome: serialized (passes stays false — real ConnectivityState producer + readiness wiring + a
         live IB fault-injection e2e are deferred; see "Resume / next")
Blocked-on (recorded 2026-07-27): SRS-EXE-001, ERR-9, SRS-MD-005 — do NOT claim this feature to
         "build" it again; the code is on main. See "Session 2".

## Context — what already existed vs. the gap this session closed

The ERR-2 connectivity GATE was already built and flipped passes:true by feature ERR-2:
`ExecutionEngine::submit_live_order` (crates/atp-execution/src/lib.rs:562, block arm 638-659) refuses
Live submissions with CONNECTIVITY_BLOCKED on ConnectivityState::{Unreachable,ScheduledRestartWindow},
emits a ConnectivityEvent, requests a reconnect, and never calls the broker. A comprehensive L7
domain test exists (crates/atp-execution/tests/err_2_connectivity_blocked.rs, 4 tests). BUT
SRS-SAFE-003 was never formally addressed (feature_list.json still passes:false, no session note),
and two gaps remained:
  1. ERR-2's test drives `submit_live_order` DIRECTLY (caller-supplied StrategyMode::Live), bypassing
     the `route_order` single-live designation authority — the memory rule "prove via route_order,
     not submit_live_order" was unmet for the connectivity block.
  2. No operator/fault-injection surface drove CONNECTIVITY_BLOCKED (the only wiring scenario,
     run_routing_scenario, always wires HealthyConnectivityFixture = Connected). SAFE-003 Step 2
     asks for a "fault-injection workflow using mocked IB … CLI/API calls, and logs."

## What I did

1. **Wiring (atp-orchestrator/src/order_routing_wiring.rs, additive)** — added
   `InjectableConnectivity` (a BrokerageConnectivity fixture returning a configured ConnectivityState
   + counting request_reconnect), a `CollectingConnectivitySink::events()` accessor, evidence types
   `ConnectivityBlockEvidence` / `LiveConnectivityOutcome`, and `run_connectivity_block_scenario` —
   which designates a single live strategy and routes a designated-live order through the REAL
   `ExecutionEngine::dispatch_order → route_order → submit_live_order` chain with the injected
   connectivity state + real IbBrokerageBridge/RecordingIbGateway, plus a non-designated paper
   contrast. No behavior change to run_routing_scenario.
2. **Operator CLI (atp-orchestrator/src/bin/safe003_connectivity_block_cli.rs, new)** — `prove-block
   [--state unreachable|scheduled-restart] [--inject connected]` and `routes-when-connected
   [--inject <blocked>]`, with opposite-class `--inject` non-vacuity, labeled key:value output,
   `transports:FIXTURE` self-labeling, and fail-closed parsing. Registered as a [[bin]].
3. **Tests** — L4/L5 srs_safe_003_connectivity_block_wiring.rs (4) + srs_safe_003_connectivity_block_cli.rs
   (20), and the mandatory L7 tests/domain/test_safe003_connectivity_block_cli.py (12; behavioral
   cargo-test shell + self-contained source scans proving the CLI drives dispatch_order and NOT
   submit_live_order directly). Required by the critic's SAFETY_PATH_RE (path contains "connectivity"/"safe").

## What I tested (per feature step)

Step 1 (init.sh → "Environment ready"): PASS — `./init.sh` → "✓ Environment ready". (First run tripped
  a TRANSIENT flake in a pre-existing atp-data test store::tests::reingest_through_disk_is_byte_identical
  [SRS-DATA-016, shared $TMPDIR fixed-path temp dir]; it passes deterministically in isolation and on
  re-run — NOT touched, out of scope for SAFE-003. Second init.sh run was clean.)
Step 2 (exercise via fault-injection workflow / CLI / logs): PASS —
  `cargo run -p atp-orchestrator --bin safe003_connectivity_block_cli -- prove-block --state unreachable`
    → outcome:BLOCKED category:CONNECTIVITY_BLOCKED error_type:IbGatewayUnreachable
      witness ib-orders-created:0 reconnects:1 events:1 scheduled_restart:false event-strategy:live-alpha
      → connectivity-block-proven:true (exit 0)
  `… prove-block --state scheduled-restart` → scheduled_restart:true → connectivity-block-proven:true
  `… routes-when-connected` → outcome:ROUTED_THROUGH ib-orders-created:1 reconnects:0 events:0
      → connectivity-routes-when-connected:true (exit 0)
  `… prove-block --inject connected` → exit 1, no proof line (non-vacuity); parse errors all exit 1.
Step 3 (AC: CONNECTIVITY_BLOCKED until reconnection AND readiness checks pass): PARTIAL/serialized —
  the BLOCK half is proven end-to-end through the production authority chain under fixture fault
  injection (CONNECTIVITY_BLOCKED, 0 IB orders via the RecordingIbGateway wire-attempt witness,
  reconnect requested, one ConnectivityEvent); the Connected positive control proves the block is
  selective. The "until reconnection AND readiness checks pass" clearing against a REAL IB signal is
  NOT solo-verifiable: no runtime code produces a ConnectivityState (all fixtures), and the readiness
  half is collapsed into Connected's prose and delegated to the unimplemented BrokerageConnectivity
  port (owned by SRS-MD-006/SRS-ARCH-005). So passes stays false.
Step 4 (record evidence, leave passes false until end-to-end): DONE — evidence above; serialized.

Gate: cargo test --workspace PASS; pytest -m "not integration and not e2e" PASS (4054 passed, 6
  pre-existing skips); tools/run_ci_locally.sh PASS with venv activated (ib_adapter_check clean →
  EXE-006 digest untouched; note "run_ci needs venv" — contract checks use `python3`, needs numpy);
  cargo clippy -p atp-orchestrator --all-targets -D warnings clean; cargo fmt --all --check clean.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (adversarial_review.py origin/main, reviewer=codex): APPROVE — no findings, after two
    fix rounds:
      r1 block: USAGE embedded the exact ':true' success sentinels → removed them from USAGE/error
        text and made fail-closed tests forbid the sentinel substring anywhere in failure output.
      r2 block: `wants_help()` short-circuited to Ok before flag parsing → replaced with `is_sole_help`
        (help honored only as the sole argument), so `prove-block --nope --help` now fails closed;
        added regression tests for mixed help+invalid args.

## Do NOT touch (verified during this session)
  crates/atp-adapters/src/interactive_brokers.rs, interactive_brokers/wire.rs, and
  crates/atp-adapters/tests/srs_exe_006_ib_adapter.rs are SHA-256 pinned by tools/ib_adapter_check.py
  (editing flips closed-green SRS-EXE-006 RED). This session touched none of them; ib_adapter_check
  passes.

## Resume / next (to flip passes:true)
  a. Build the REAL connectivity-runtime producer that maps an IB disconnect (IB 1100/2110/socket
     loss) onto ConnectivityState::Unreachable and reconnection back onto Connected — a runtime
     `impl BrokerageConnectivity` fed by the IB adapter's connectivity signal. Deferred to
     SRS-EXE-006 / SRS-MD-005 / SRS-EXE-001 connectivity-runtime (EXE-001 itself blocked-on PERF-001).
  b. Wire the startup readiness gate into the order path so the "readiness checks pass" half of the AC
     is enforced (not just collapsed into Connected). Owned by SRS-MD-006 / SRS-ARCH-005 / ERR-9
     atp_readiness (progress.txt:230 records this enforcement point "DOES NOT EXIST in the repo yet").
  c. A live IB fault-injection e2e: with (a)+(b) wired, kill the real IB Gateway, assert a live
     submission is refused with CONNECTIVITY_BLOCKED, then reconnection + readiness → the submission
     resumes. Operator-gated (real IB, single-live invariant) — cannot run alongside siblings.
  Where to continue: run_connectivity_block_scenario + safe003_connectivity_block_cli are the shape
  the live leg reuses; swap InjectableConnectivity for the real producer from (a).

=== SESSION 2 — 2026-07-27 (de-churn; NO code change) ===
Outcome: partial(blocked-on SRS-EXE-001, ERR-9, SRS-MD-005) — the feature stays serialized.

## Why this session exists — the scheduler re-offered a feature that is already built

`agent_pool.py claim` handed SRS-SAFE-003 to a fresh session even though session 1's code is on
main (fa8b837 / 7ceaf8b / d8a0a2f). Root cause: `serialized_notes()` (tools/agent_pool.py:460)
decides which serialized features to stop offering by reading **ROOT**/progress.d/session-<id>.md.
ROOT (/Users/joshgenao/Documents/Programming/Python/alphalabs) was at 4c123a8 on branch
`chore/mypy-python-clean` — four sibling integrations behind origin/main — so it did not contain
this note and the scheduler could not see `Outcome: serialized`. SRS-SAFE-003 also had ZERO edges
in tools/feature_deps.json, so nothing else stopped the re-offer. This is the known LOG-001
de-churn pattern (dep-less flip-blocked feature → infinite churn).

OPERATOR ACTION (not performed here — it is your repo/branch): sync ROOT to origin/main, e.g.
`git -C <ROOT> fetch origin` then checkout/pull main once the chore/mypy-python-clean work is
safe. Until then every newly-serialized feature (UI-5, SRS-BT-008, …) will churn the same way.

## What I did

Recorded the honest blocking owners so the board is accurate even when ROOT lags:
  `python3 tools/agent_pool.py block SRS-SAFE-003 --on SRS-EXE-001 ERR-9 SRS-MD-005`
  → ✓ blocked-on ['ERR-9', 'SRS-EXE-001', 'SRS-MD-005'] (no cycles — nothing depends on SAFE-003).

WHERE THAT EDGE LIVES — read this before believing any claim above. State it exactly:

  * IN EFFECT NOW, in the scheduler's runtime state only: `block` wrote the edge under the pool lock
    to `DEPS_FILE = ROOT/tools/feature_deps.json` (tools/agent_pool.py:159) — the file `load_deps()`
    reads when `claim` runs. That is local scheduler state on this machine, and it is the thing that
    actually stops the re-offer. It is NOT observable from this branch diff, and a reader who has
    only the diff cannot confirm it.
  * NOT YET ON origin/main at the time this commit was reviewed. `git show
    origin/main:tools/feature_deps.json` still has no SRS-SAFE-003 key until the integrator runs.
  * WHY THE BRANCH CANNOT CARRY IT: an agent commit touching tools/feature_deps.json is rejected by
    `shared_state_violations` (agent_pool.py:905-920) and aborts integrate with exit 6 — only the
    integrator may write that file. It reaches main in the integrator's marker commit, which calls
    `_sync_deps_into(wt)` (agent_pool.py:1021, partial and complete alike) and then stages the
    INTEGRATE_ALLOWLIST. So a note-only branch diff is the ONLY shape this change can take; the
    reviewer's alternative (commit the graph here) is unimplementable, it fails integrate.
  * TO CONFIRM AFTER integrate (expected, not yet observed when this was written):
    `git show origin/main:tools/feature_deps.json | python3 -c "import json,sys;print(json.load(sys.stdin)['SRS-SAFE-003'])"`
    should print ['ERR-9', 'SRS-EXE-001', 'SRS-MD-005']. If it does not, the de-churn did NOT land
    and this feature will be re-offered again — re-run the `block` command above.
Each edge maps to a named half of the AC, not a dodge:
  * SRS-EXE-001 — the live execution runtime that must BIND a real ConnectivityState producer into
    route_order. Verified this session: every `impl BrokerageConnectivity` in the repo is a
    fixture/test (order_routing_wiring.rs:484 HealthyConnectivityFixture, :813 InjectableConnectivity,
    + test-local stubs). No runtime producer exists.
  * ERR-9 — "Hold system in pre-trade state and expose readiness failure" (docs/SRS.md ERR-9 →
    SRS-ARCH-005, SRS-MD-006) is the owner of the AC's second half, "until reconnection AND
    readiness checks pass". Verified: python/atp_readiness/ (ReadinessGate.assert_ready_or_hold,
    the MD-006 SYS-76 runtime fold) exists and is green, but NO order path consults it — its only
    consumers are atp_dashboard/provider.py, atp_reliability restart evidence, and check tools.
  * SRS-MD-005 — owns the scheduled IB Gateway restart + reconnection semantics ("until
    reconnection", and the ScheduledRestartWindow state this gate already refuses on).

## What I deliberately did NOT build (scope honesty)

  * A Rust TCP-reachability BrokerageConnectivity producer. It would fork the connectivity
    observation vocabulary MD-003's heartbeat watchdog already owns in Python (MD-003 is built and
    awaiting its live feed loop), and it still could not flip passes:true.
  * A readiness precondition on submit_live_order / route_order. That is ERR-9's named scope, and it
    would change the pinned ERR-1/2/3 gate signature across 21 `.submit_live_order(` + 14
    `.route_order(` call sites, including closed-green SRS-EXE-001 / SRS-ERR-001 / SRS-EXE-009 tests.

## What I tested (per feature step)

Step 1 (init.sh → "Environment ready"): PASS — re-run this session.
Steps 2-4: unchanged from session 1 — no code changed, so session 1's evidence stands verbatim.
Gate re-run on the notes/deps diff: see "Critic verdicts" below.

Critic verdicts (session 2):
  deterministic (critic_check.py --staged): APPROVE — no tests/domain/ pairing required: this diff
    touches only progress.d/session-SRS-SAFE-003.md (carved out at critic_check.py:355-358) and
    tools/feature_deps.json (no SAFETY_PATH_RE match). No test weakened or removed.
  judgment (adversarial_review.py origin/main): see the commit trailer for reviewer + verdict.

## Resume / next — UNCHANGED from session 1, plus:
  Do not re-claim SRS-SAFE-003 to build it. When SRS-EXE-001 + ERR-9 + SRS-MD-005 are green, the
  remaining work is (1) bind the real producer into route_order in place of InjectableConnectivity,
  (2) enforce the readiness hold on the live path, (3) the operator live-IB fault-injection e2e
  (kill the gateway → CONNECTIVITY_BLOCKED → reconnect + readiness → submission resumes), then
  `integrate --force-complete` / the verified-e2e label.
