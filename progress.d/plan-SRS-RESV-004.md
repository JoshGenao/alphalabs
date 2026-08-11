# SRS-RESV-004 — execute Hot-Swap demotion before promotion

## Context

`SRS-RESV-004` (SyRS SYS-49b / SYS-49c, StRS SN-1.25, P1) requires the demotion phase to
run *before* any promotion:

> Current live strategy stops new signals, cancels resting IB orders, submits liquidation
> orders, waits for flat confirmation or the configured timeout (default 60 s), and
> transitions to paper only after live positions are flat; on timeout, the swap enters
> demotion-pending state, dashboard/email/SMS notifications are sent, unfilled liquidation
> orders are canceled, and promotion is blocked until manual resolution.

**No prior session note exists** for this feature. What *does* exist was built by ERR-7's
"SDK-surface contract slice" (commit `82ef06f`) and RESV-003: a `resolve_demotion` gate in
`crates/atp-orchestrator/src/lib.rs:1611` that models only the **binary timeout decision**
over four injected ports, with every concrete producer deferred.

`architecture/runtime_services.json` → `hot_swap_demotion_contract.deferred[]` names the gap
precisely, and three of its seven entries are owned by *this* feature:

1. **The concrete demotion sequence + wait loop** — "stop new signals, cancel resting IB
   orders, submit liquidation orders" and the 60 s flat-confirmation loop. Nothing runs it.
2. **Probe outcome-consistency hardening** — a `TimedOutDemotionPending` reported *before*
   the deadline currently fires the destructive cancel early. Needs a block-**without**-cancel
   rejection distinct from block-with-cancel.
3. **Durable demotion-pending lockout + manual resolution** — `resolve_demotion` is a
   *stateless single-attempt* decision. On timeout it blocks promotion **for that call only**;
   a later retry whose probe reports flat promotes anyway. The AC clause "promotion is blocked
   until manual resolution" is therefore **not enforced today**. This is the safety defect.

The remaining `deferred[]` entries (real IB `cancel_order` transport, real SMTP/SMS) belong to
`atp-adapters` / `SRS-NOTIF-001` and stay deferred.

Intended outcome: the demotion phase actually executes, the demotion-pending lockout is
durable and survives a retry, and the operator can see and clear it — closing SYS-49b/49c.

## Approach: reuse the kill-switch machinery, don't rebuild it

`SRS-SAFE-001/002` already shipped the structurally identical sequence. Reuse, do not clone:

| Need | Existing code to reuse |
|---|---|
| cancel resting → submit liquidations, per-phase `SideEffectOutcome`, deterministic ordering | `crates/atp-execution/src/kill_switch.rs` `activate_kill_switch` (phases 2–3) |
| flat-confirmation poll with a deadline, duplicate/absent broker rows fail closed | `crates/atp-execution/src/kill_switch_probe.rs` `PollingLiquidationProbe` |
| dashboard+email+SMS triad through the **real** `OperatorNotifier` over fixture transports | `crates/atp-orchestrator/src/kill_switch_timeout.rs` `NotifierAlertSink` |
| durable store: MAGIC + version, scratch→fsync→rename→parent-fsync, 3-state read, `ExclusiveGuard` | `crates/atp-orchestrator/src/trigger_config_store.rs` |
| operator CLI shape + `transports:"FIXTURE"` self-label | `src/bin/safe002_liquidation_timeout_cli.rs` |
| Rust bin → Python neutral client → dashboard pane + REST | `python/atp_hotswap/` + `python/atp_safety/timeout.py` |

The dashboard seam already exists and is waiting: `python/atp_dashboard/hotswap.py`
`HotSwapStatusSource.live_state()` feeds `demotion_pending` / `demotion_detail`, rendered
today as `deferred:SRS-RESV-004` cells. This feature supplies that producer — **no new
dashboard panel, no `app.js` change.**

## What I'll build

### Rust — `crates/atp-orchestrator`

- **`src/hot_swap_demotion.rs`** (new) — the SYS-49b sequence, ordered and fail-closed:
  `cease_new_signals` → `cancel_resting_order`* → `submit_market_liquidation`* → poll flat
  or timeout → **transition to paper only on the flat arm**.
  Narrow ports (deliberately *not* `KillSwitchBrokerageControl`, which carries `disconnect`
  — a demotion must never disconnect IB): `SignalHalt`, `DemotionBrokerageControl`
  (cancel + liquidate only), `PaperTransition` (flat arm only). Reuses `atp-execution`'s
  `RestingOrderCancel` / `LiquidationSubmission` record shapes. `ConcreteLiquidationProbe`
  implements the existing `HotSwapLiquidationProbe` over `PollingLiquidationProbe`.
- **`src/demotion_pending_store.rs`** (new) — the durable lockout, modeled line-for-line on
  `trigger_config_store.rs`. Three-state read: absent = no lock; readable = locked;
  **unreadable = fail closed, treated as locked**. `ExclusiveGuard` held across the whole
  read-modify-write.
- **`resolve_demotion` gains a `DemotionPendingLock` port** — consulted **before** accepting
  any `FlatBeforeTimeout`, and written **before** the timeout error is returned (fallible
  work before the durable write; durable write before the caller is told). Plus the
  block-without-cancel rejection for an inconsistent probe outcome (`deferred[]` #2).
- **`src/bin/resv004_hot_swap_demotion_cli.rs`** (new) — `demote` (drives the REAL sequence
  over fixture transports, self-labels `transports:"FIXTURE"`), `status --state <path>`
  (three-state), `resolve --state <path> --confirm <ack>` (manual resolution).

### Python

- **`python/atp_hotswap/`** — a `CliHotSwapDemotionSource` producing a real `live_state()`
  (`demotion_pending` / `demotion_detail`), raising the **same** `HotSwapStatusUnavailable`
  object the pane already catches, plus a composing source that merges it with the existing
  trigger config source.
- **`python/atp_dashboard/server.py`** — compose it behind `ATP_HOT_SWAP_DEMOTION_STATE`,
  mirroring `ATP_HOT_SWAP_TRIGGER_STATE`; half-configured fails at boot (no silent
  no-lockout mode).

### Contract + gates

- `architecture/runtime_services.json` `hot_swap_demotion_contract`: add the sequence /
  lockout / paper-transition blocks; **delete the `deferred[]` entries this feature ships**
  (stale "deferred" prose after the surface lands is a re-found defect class — LOG-001
  r11/r19/r29/r33).
- `tools/hot_swap_demotion_check.py`: extend to a **static collector** over the whole class
  (ordering, lock-consulted-before-flat, no promotion path on any blocked arm), not one
  assertion per instance.

## Tests

| Layer | File | Cases |
|---|---|---|
| **L7 domain** (mandatory — safety path) | `tests/domain/test_hot_swap_demotion_sequence.py` | SYS-49b ordering proven by a shared call log; **timeout → lock persisted → a LATER attempt with a flat probe is still blocked** (the real regression); manual resolve clears it; unreadable store blocks. Driven through the shipped CLI, crossing the seam. |
| Rust | `crates/atp-orchestrator/tests/resv_4_demotion_sequence.rs` | Same invariants at the gate; paper transition never on the timeout arm; inconsistent probe blocks **without** cancelling. |
| L4 boundary | `tests/boundary/test_hot_swap_demotion_backend.py` | Client fail-closed: binary absent, timeout, unparseable, unknown status. |
| L6 e2e | `tests/e2e/test_hot_swap_demotion.py` | Playwright on an **ephemeral port** through the shipped `mount_default_dashboard`: pending renders after a real drill; resolve clears it; corrupt store renders **unknown**, not "none pending". |

All mutation-verified with the **venv** interpreter (`tools/mutation_verify.py`), Rust cases
by function-body mutation (a `git checkout` of the source removes co-located Rust tests and
goes vacuously green).

## Explicitly NOT in scope (stated refusals, not gaps)

- **No new REST routes** (operator decision). The surface is CLI + the existing dashboard
  pane. `routes.py` / `openapi.json` / `rest_api_check.py` are untouched, so there is no
  rebase collision with the sibling on `SRS-RESV-005`. Step 2's "REST or WebSocket checks
  *where applicable*" is not applicable here — the session note will say so plainly rather
  than imply a REST leg was verified.
- **`POST /api/v1/hot-swap` stays 501.** Its declared response carries `promotion_state`;
  promotion is `SRS-RESV-005`, held by a sibling right now. Wiring it would tell an operator
  a strategy was promoted when only a demotion ran.
- **Real IB cancel/liquidation transport** → deferred `atp-adapters` leg. **Real SMTP/SMS**
  → `SRS-NOTIF-001`. Both drive the REAL gate and REAL `OperatorNotifier`, over FIXTURE
  transports, self-labeled — the SAFE-002 precedent.

## Completeness

**Target `complete`, fixture-tier** (operator decision). The feature is
`verification_method: e2e`, not `live-ib`; `SRS-RESV-003` closed `complete` on exactly this
basis. The real gate, real probe, real `OperatorNotifier` and real durable store all execute;
only the IB socket and the SMTP/SMS transports are fixtures, and the outcome payload
self-labels `transports:"FIXTURE"` so the tier travels into the record.

Honesty conditions — if any of these fails I integrate `--mode serialized`, not `complete`:

- the Playwright e2e must pass **solo, on a clean re-run** (the integrator re-runs recorded
  commands and compares exit codes);
- every step must be recorded with `evidence.py run` (not `record`);
- both critic passes must be a real `approve` — a TIMEOUT or an empty-summary fallback is not
  a verdict.

## Verification (feature `steps[]`, through `tools/evidence.py run`)

1. `./init.sh` → "✓ Environment ready".
2. `pytest tests/e2e/test_hot_swap_demotion.py` under `ATP_RUN_E2E=1` — the browser leg.
3. `pytest tests/domain/test_hot_swap_demotion_sequence.py` + `cargo test -p atp-orchestrator`
   — every AC clause.
4. `python3 tools/hot_swap_demotion_check.py` — contract evidence.

Each recorded command must be a single argv with no shell metacharacters, or it cannot close
the feature on machine evidence. `.harness/runs/SRS-RESV-004/evidence.json` is committed with
the feature work.

Then: `mutation_verify` (venv interpreter) → `critic_check.py --staged` →
`adversarial_review.py origin/main` → `run_ci_locally.sh` (after
`pip install -r requirements-dev.txt`) → `agent_pool.py integrate SRS-RESV-004 --mode complete`.

Session note + any new playbook rules land in the `chore` commit.
