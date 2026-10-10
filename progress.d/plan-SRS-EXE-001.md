# Plan: SRS-EXE-001 - route orders to IB only for the designated live strategy

## Context

**What the feature must do (MVP acceptance criteria, docs/SRS.md 3.1 on `origin/main`):**
live designation needs explicit operator confirmation; with 1 live and 5 paper strategies
running, only the live strategy can submit to IB; every other IB-bound attempt gets a
structured error; the live order acknowledgement is under 1,000 ms p95 from the strategy
API call to the strategy's acknowledgement callback, timed with the host monotonic clock.
Out of scope (R2): 30 paper strategies, PTP clock.

**What exists on `origin/main`:** the in-process gate is built and tested.
`ExecutionEngine` owns a private, non-`Clone` `LiveDesignation`; `route_order`
(`crates/atp-execution/src/lib.rs:715`) rejects a non-designated strategy with
`NON_LIVE_STRATEGY_SUBMISSION` before touching any port; `LiveDesignationConfirmation`
is a strategy-bound token. The IB adapter (`InteractiveBrokersBrokerage`) reaches the
engine through `IbBrokerageBridge` (`crates/atp-orchestrator/src/order_routing_wiring.rs:214`).

**What is missing - why the feature cannot close today:**
1. No running process hosts `ExecutionEngine` with the real IB adapter. Only test/fixture CLIs call `route_order`.
2. `TcpIbGateway` refuses the live account ("gated on the SRS-EXE-001 execution-engine admission", `interactive_brokers.rs:684-695`, `:776-785`).
3. Designation is in memory only, and nothing lets the operator set it: `promote-live` (REST) and `live promote` (CLI) are declared but answer `501 HANDLER_DEFERRED owner SRS-EXE-001`.
4. A Python strategy has no real order path: `StrategyContext.order()` is a Protocol only, and nothing feeds `deliver_order_event` (`python/atp_strategy/dispatch.py:218`).
5. The latency artifact `LatencyVerificationArtifact::from_samples` refuses a non-PTP clock.

**Operator decisions (2026-10-10):** lift the live-account gate and re-run SRS-EXE-006's
paper-account test in the same live window; land in two serialized landings.

## Design changes found while reading the code (2026-10-10, after approval)

1. **A durable designation snapshot already exists**, private to
   `resv005_hot_swap_promote_cli.rs` (`RESV005-LIVE-DESIGNATION-STATE v1`, named by
   `ATP_HOT_SWAP_DESIGNATION_STATE`), and `cmd_swap` serializes on
   `trigger_config_store::ExclusiveGuard` over that file. The host must use the SAME
   file and lock, or a Hot-Swap could leave the old strategy routing. So: move
   `load_designation` / `save_designation` / `PublishOutcome` verbatim into
   `crates/atp-orchestrator/src/live_designation_store.rs`; the RESV-005 CLI imports
   them. The host takes the guard, reads the snapshot, and routes on EVERY order (no
   cached designation). No new file format, no `flock`.
2. **Designation writes go through a new `exe001_live_designation_cli`**
   (`promote <id> --confirm <ack>`, `demote <id>`, `status`) under the same guard, not
   through host frames. `promote` with another strategy live is refused
   (`AlreadyDesignated`) - that case is a Hot-Swap.
3. **Strategy identity = which socket.** One Unix socket per registered strategy
   (`<dir>/<strategy_id>.sock`), mounted into only that strategy's container. A client
   never states its own strategy id.
4. **Freshness has no production producer** (owner SRS-MD-004, blocked on MD-003).
   Operator decision: the host takes freshness as an injected port; the fixture tier
   uses a labelled fixture; the live IB tier refuses to start until a real producer is
   wired. After Landing 2: `block SRS-EXE-001 --on SRS-MD-004`. Connectivity uses the
   real `ScheduledRestartConnectivity` (MD-005).
5. **The adapter live-account gate lift moves out of Landing 1.** With the live tier
   unable to start, lifting it now would turn SRS-EXE-006 red for no benefit. It lands
   with the change that makes the live tier startable.
6. **Protocol is tab-separated `key=value` lines**, not JSON (no JSON library in the
   workspace). Values refuse tabs, newlines, and control bytes.
7. **Outbox:** the host uses `route_order_durably`. Start requires either an existing
   snapshot or an explicit `--outbox-init` (refused if one exists). A loaded outbox
   with a non-terminal entry refuses to serve: restart reconciliation is SRS-EXE-009.

## Step 0 - Refresh the worktree

The branch is 571 commits behind `origin/main`, 0 ahead, clean.
Run `git merge --ff-only origin/main`, then `./init.sh`, then persist this plan to
`progress.d/plan-SRS-EXE-001.md`. Run `python3 tools/verify_queue.py show SRS-EXE-001`
and re-check every file:line below against the refreshed tree before editing.

## Landing 1 - Rust live execution host (~1,300 lines)

**New binary `live_execution_host`** in `crates/atp-orchestrator/src/bin/` (the
orchestrator crate already depends on both atp-execution and atp-adapters; dependency
direction stays one-way). Module code in a new `crates/atp-orchestrator/src/live_host/`.

- **One long-lived IB connection.** The host owns one `ExecutionEngine` wired to
  `IbBrokerageBridge<TcpIbGateway>` (feature `ib-live-transport`) or, in the default
  build, to the in-memory `ScriptedIbGateway`. One connection matters: the gateway serves
  one API client and strands reconnects in `CLOSE_WAIT` (broker-and-live rule 21), so a
  per-order subprocess is not an option.
- **Local socket protocol.** A Unix domain socket under the worktree-local data dir
  (no port, no sibling collision). Newline-delimited JSON frames: `submit`,
  `designate`, `demote`, `status`; replies `ack` (with broker receipt) or the existing
  `StructuredOrderError` envelope. Bounded frame size, fail closed on unknown fields and
  control bytes. Each connection carries a strategy id fixed at handshake, so a client
  cannot submit as another strategy.
- **Durable single-live designation.** The host is the only authority. It takes an
  exclusive `flock` on its state dir (a second host refuses to start) and persists the
  designation with atomic write + fsync + schema version. Missing file = no designation;
  corrupt or unknown version = refuse to start (never "no live strategy"). `designate`
  requires a `LiveDesignationConfirmation` built from the operator's acknowledgement.
- **Orders go through `route_order`** (never `submit_live_order`; broker-and-live rule
  14). If `route_order_durably` (SRS-EXE-009 outbox, `lib.rs:936`) is a drop-in at this
  call site, use it; otherwise use `route_order` and record the gap for SRS-EXE-009.
- **Lift the live-account gate** in `interactive_brokers.rs`: serve `IbAccountKind::Live`
  only for a connection constructed through the host's admission path (a constructor
  that is crate-private to the host wiring, not a public flag). This invalidates
  `architecture/ib_paper_account_evidence.json`; SRS-EXE-006 goes red until the operator
  re-runs `ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py`. Keep the edit to
  the gate only; `wire.rs` and `srs_exe_006_ib_adapter.rs` stay untouched.
- **Reusable fake gateway.** Copy the scripted loopback gateway out of
  `crates/atp-adapters/tests/srs_exe_006_ib_wire.rs:636` into a new test-support file
  (do not edit the hashed test file).

**Tests (Landing 1):**
- L1: frame parser (oversize, unknown field, control byte); designation store (missing / corrupt / version).
- L4 boundary (`crates/atp-orchestrator/tests/srs_exe_001_live_host.rs`): host + scripted gateway; 1 designated + 5 non-designated socket clients submit; exactly one broker side effect, five `NON_LIVE_STRATEGY_SUBMISSION` errors, `wire-attempts:1` and `:0` counters (rule 15); designate without confirmation refused; confirmation for A cannot designate B; second host refused by the lock; restart keeps the designation.
- L7 domain (mandatory, safety path): `tests/domain/test_live_execution_host.py` drives the built binary end to end for the same invariants.
- Update `tools/live_designation_check.py` + the `live_designation_contract` block: move the "durable single-live" and "real IB dispatch" entries out of `deferred[]` only for what the host actually delivers.

Gate, both critics, then `agent_pool.py integrate SRS-EXE-001 --mode serialized`.

## Landing 2 - Python order path and operator surfaces (~1,100 lines)

- **Socket client** `python/atp_strategy/live_client.py`: connect, submit, read ack; bounded timeouts; an unknown or malformed reply is an error, never an ack.
- **Concrete live `StrategyContext.order()`** that sends through the client and, on ack, calls the existing `deliver_order_event` (do not rebuild it; SDK-004 note lines 85-96).
- **Operator handlers** registered on `atp_runtime.HandlerRegistry`, following the kill-switch pattern in `python/atp_safety/wiring.py:60-62`: `POST /api/v1/strategies/{strategy_id}/promote-live` and CLI `live promote <id> --confirm` / `live show`. They forward a confirmed `designate` to the host. The existing 428 confirmation guard stays in front.
- **Latency:** time `perf_counter_ns` from `order()` call to the ack callback; compute p95 with the existing `nfr_p95_cli` / `LatencyPercentiles` (precedent: `tests/domain/test_paper_callback_delivery.py`). Label the result as host-monotonic MVP evidence, distinct from the PTP artifact (measurement playbook rule 8). Do not weaken `LatencyVerificationArtifact`.

**Tests (Landing 2):**
- L1/L3: client framing, reply validation, handler contract (route constants, 428 without confirm, no 501 once registered).
- L7 domain: 1 live + 5 paper Python strategy processes against the host and fake gateway; only the live strategy's orders reach the gateway; the five get the structured error; p95 < 1,000 ms over a fixed sample count; promote-live without confirmation refused.

Gate, both critics, `integrate --mode serialized`.

## Who owns what the AC names

| AC element | Producer | Status |
|---|---|---|
| Operator confirmation surface | SRS-API-001 runtime (built), handler by this feature | handler: this feature |
| IB order transport | SRS-EXE-006 | passes; re-run needed after gate lift |
| Live host + durable designation | this feature | to build |
| Ack callback to strategy | `deliver_order_event` (SDK-004 code on main) | code exists |
| Latency measurement | host monotonic clock (MVP); PERF-001 is R2 | built here, no R2 wait |

No unbuilt MVP feature blocks this. SRS-PERF-001 is R2 and is not waited on.

## Expected outcome: serialized

Steps 1-2 and the solo parts of Step 3 pass against the fake gateway. Step 3's real-IB leg
and Step 4's evidence need a live-ib window. Class A after Landing 2: the operator runs,
in one exclusive window: (1) the SRS-EXE-006 paper-account re-run, (2) the host against
the IB gateway with 1 live + 5 paper strategies, recording p95. Then the operator flips.

## Verification (per landing)

```bash
tools/run_ci_locally.sh
cargo test --workspace            # only one at a time: pgrep -x cargo first
pytest -m "not integration and not e2e"
python3 tools/live_designation_check.py
python3 tools/critic_check.py --staged --format text
tools/codex_review.sh origin/main
```
Mutation-verify each new regression test (remove the gate, see it fail, restore).
Check `git diff origin/main...HEAD --stat` shows no stray deletions before integrating.

## Risks

- The gate lift turns SRS-EXE-006 red until the operator re-runs it. Say so in the note and the verification queue.
- Review may not converge on deferred scope (EXE-005 recovery, ORCH containers). Fix in-scope findings; scope the rest by owner (scope-and-serialization rule 9).
