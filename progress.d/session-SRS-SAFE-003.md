=== SESSION SRS-SAFE-003 ===
Date: 2026-07-26
Feature: SRS-SAFE-003 — block live order submission when IB Gateway is unreachable
         (docs/SRS.md:246 SRS-5.9; ERR-2 row docs/SRS.md:291; SyRS SYS-45/SYS-64/NFR-R2; StRS SN-2.04)
Outcome: serialized (passes stays false — real ConnectivityState producer + readiness wiring + a
         live IB fault-injection e2e are deferred; see "Resume / next")

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
