=== SESSION SRS-EXE-001 ===
Date: 2026-10-10
Feature: SRS-EXE-001 — route orders to IB only for the designated live strategy
Outcome: serialized (B: landing 1 of 2 integrated; the live-ib run waits on SRS-MD-004)

## What this landing is

The live-designation GATE already existed (`ExecutionEngine::route_order`, a private
non-`Clone` `LiveDesignation`, a strategy-bound confirmation token), but nothing ran
it: only fixture CLIs called `route_order`. Landing 1 builds the production call
site, the **live execution host**, plus the operator designation command. Landing 2
(next session) builds the Python order path and the operator REST/CLI handlers.

Plan, with the operator's decisions: `progress.d/plan-SRS-EXE-001.md`.

## What I built

* **`live_designation_store.rs`** — the durable single-live snapshot
  (`RESV005-LIVE-DESIGNATION-STATE v1`, `ATP_HOT_SWAP_DESIGNATION_STATE`) moved out
  of `resv005_hot_swap_promote_cli.rs` into one shared module. The Hot-Swap CLI,
  the new designation CLI, and the host all read and write it there, under the
  SAME `trigger_config_store::ExclusiveGuard` the swap already held. One change to
  the moved code: `load_designation` bounds its read at 4 KiB (the host calls it on
  every order). The schema registry's writer path now points at the module.
* **`live_host/`** (`mod.rs`, `protocol.rs`, `server.rs`) — per order: the in-process
  mutex, then the designation guard (`acquire_if_parent_exists`), then a FRESH read
  of the snapshot (nothing cached), then `route_order_durably` (authority gate,
  SRS-EXE-009 write-ahead outbox, ERR-2/ERR-3 gates, broker). The guard is held
  until the broker returns, so an order cannot interleave with a swap.
  - One Unix socket per strategy at `<socket_dir>/<id>/order.sock` (directory
    0700, socket 0600; a socket_dir open to group/other is refused). The socket
    IS the identity; the frame cannot name a strategy (`strategy_id` is an
    unknown field).
  - Tab-separated `key=value` frames (no JSON crate in the workspace); fail-closed
    parser; three reply outcomes `ack` / `reject` / `refused` with `durable` and
    `tier` on every reply.
  - Two OS file locks (`File::try_lock`), both released by the kernel if the host
    dies: `<outbox>/host.lock` (one writer per outbox) and `<socket_dir>/host.lock`.
  - `open()` refuses: an unreadable designation, `--outbox-init` over an existing
    directory (atomic `create_dir`), a restart with no outbox, and an outbox holding
    an unbound non-terminal intent (that is SRS-EXE-009 reconciliation).
* **`bin/live_execution_host.rs`** — `serve`. `--transport fixture` uses the
  recording gateway with an optional `--fixture-wire-ledger`. `--transport ib` is
  REFUSED (operator decision): no production freshness producer exists
  (SRS-MD-004), and a fixture "always fresh" probe on real orders would disable
  stale-data blocking.
* **`bin/exe001_live_designation_cli.rs`** — `promote --confirm` / `status`. Empty
  confirmation refused before any lock. A different strategy live → refused,
  pointing at the Hot-Swap. No `demote`: clearing the slot without the RESV-004
  liquidation would orphan open IB positions.
* **Workspace MSRV 1.75 → 1.89** (`File::try_lock`), in its own commit
  (3efb404). Nothing built against 1.75 (scaffold value; toolchain pinned 1.95).
  It enabled 10 clippy hits in 4 unrelated files, all mechanical
  `map_or(true, ..)` → `is_none_or` and `% 2 == 0` → `is_multiple_of` (unsigned
  only). The DATA-007 contract pinned one spelling (`kind_filter_token`) and
  moved with it. Paired domain test: unset query axes match every record.
* **Contract** — `live_designation_contract.live_host` + `deferred[]` rewritten;
  `tools/live_designation_check.py::check_live_host` pins guard→read→route order,
  no `submit_live_order` / `.route_order(`, one owner of the snapshot format,
  socket identity, no `strategy_id` key, and the refusing live-tier arm.
* **Prep commit** — `SAFETY_PATH_RE` gains `live[_-]?host|live[_-]?execution[_-]?host`.
* **Second critic change (operator-approved)** — `.harness/runs/` joins the
  documentation-only carve-outs. This feature's own id is a safety token, so its
  evidence record matched by name and could not be committed, yet `integrate`
  refuses without it. Unit test proves a same-token source path still blocks.

## What I did NOT do, and why

* **Adapter live-account gate** (`TcpIbGateway` serves Paper only): NOT lifted. The
  operator approved lifting it, but with the live tier refused there is no caller,
  and lifting it invalidates SRS-EXE-006's evidence digest for nothing. It lands
  with the change that makes the live tier startable. `interactive_brokers.rs`,
  `wire.rs`, `srs_exe_006_ib_adapter.rs` are untouched.

## What I tested (per step)

Step 1: PASS — `./init.sh` → `✓ Environment ready` (17/17 contract checks).
Step 2: PASS (fixture tier) — real binaries over Unix sockets, wire ledger as the
  outside view. `cargo test -p atp-orchestrator --test srs_exe_001_live_host` →
  16 passed; `pytest tests/domain/test_live_execution_host.py` → 6 passed;
  protocol + server unit tests → 10 passed; `tests/test_live_designation_contract.py`
  → 29 passed (7 new negative tests, one per guard).
Step 3: PARTIAL —
  * Explicit confirmation: PASS (empty / whitespace / missing `--confirm` refused,
    snapshot not written).
  * 1 live + 5 paper: PASS on the fixture tier — 3 rounds, 15 paper attempts all
    `NON_LIVE_STRATEGY_SUBMISSION`, wire ledger = `[live-a, live-a, live-a]`.
  * Real IB: NOT RUN — live tier refused until SRS-MD-004.
  * <1,000 ms p95 strategy-API→callback: NOT RUN — needs the Python client
    (landing 2) and then the live run.
Step 4: NOT DONE — evidence record waits for the live-ib run.

Mutation-verified (each restored, suite green after): release the designation lock
before the broker; skip the unresolved-intent refusal; every socket speaks as the
first strategy (caught by 2 Rust + 3 Python tests); drop the outbox lock; recreate
a vanished state dir; drop the snapshot size cap; accept an open socket_dir; leave
a strategy directory 0755; is_some_and in the backtest query predicate.

Size: ~3,300 lines added (estimate was ~1,300; tests are about half).

Flake found and fixed in my own test: `an_oversize_frame_is_refused...` wrote 10 KB
past a host that correctly stops reading at the frame limit and closes, so the tail
write could EPIPE and the `unwrap()` panicked. The write is now allowed to fail.

## Critic verdicts

deterministic (critic_check.py --staged): APPROVE on every commit (prep cd950aa,
  toolchain 3efb404, feat ec03557, docs b6608c7).
Adversarial rounds: 4
judgment (tools/codex_review.sh), 4 rounds:
  r0 (base origin/main): BLOCK meta:critic-self-modification on the SAFETY_PATH_RE
     prep. Operator reviewed and APPROVED the 2-token change on 2026-10-10; later
     rounds use base cd950aa, as the session-50 precedent did.
  r1 [high] socket identity reachable by any process that can open the live
     socket. FIXED host-side: 0700 per-strategy directory, 0600 socket, socket_dir
     open to group/other refused, symlinks refused. The container-mount half has
     no open owner (see Resume / next).
  r2 [high] same mount gap restated (deferred); [high] hygiene: MSRV bump mixed
     into the feature. FIXED: split into chore(toolchain) 3efb404 with its own
     paired domain test; two stale "MSRV 1.75" comments corrected.
  r3 [high] broker call has no host-level timeout while holding the designation
     guard. LATENT (ib tier refused). Recorded as live-tier preconditions in
     deferred[] (b6608c7); a host timeout would reopen the Hot-Swap race.
  Final verdict: needs-attention, every remaining finding is deferred live-tier
  scope. Integrated serialized on operator authorization
  (2026-10-10: "Authorize, integrate serialized"). The two ownerless gaps below
  are noted for a later operator decision, with no dependency edge.

## Resume / next

Landing 2 (SRS-EXE-001, same feature):
1. `python/atp_strategy/live_client.py` — socket client speaking `ATP-LIVE-HOST/1`
   (mirror `live_host/protocol.rs`; refuse to encode a value the parser refuses).
2. Concrete live `StrategyContext.order()` → client → on `ack` call the existing
   `atp_strategy.dispatch.deliver_order_event` (do not rebuild it).
3. Register `POST /api/v1/strategies/{strategy_id}/promote-live` and `live promote
   <id> --confirm` / `live show` on `atp_runtime.HandlerRegistry` (pattern:
   `python/atp_safety/wiring.py:60-62`), shelling `exe001_live_designation_cli`.
4. p95 from `order()` to the ack callback, `perf_counter_ns`, via `nfr_p95_cli` /
   `LatencyPercentiles` (precedent `tests/domain/test_paper_callback_delivery.py`),
   labelled host-monotonic MVP evidence.
Then `block SRS-EXE-001 --on SRS-MD-004`. When MD-004 lands: wire its freshness
producer into the `ib` tier, wire `ScheduledRestartConnectivity`, lift the adapter
gate, and the operator runs, in one live window, the SRS-EXE-006 paper re-run plus
the 1 live + 5 paper run with the p95 recorded.

Known trade: the outbox snapshot is rewritten in full per order and acknowledged
orders stay non-terminal until order-state updates exist (SRS-EXE-008/009), so the
per-order write grows with live order count. Fine at MVP volume; owned there.

Two gaps for the operator, neither owned by an open feature:
* **Container socket mount.** The concrete Docker `StrategyContainerRuntime` must
  bind-mount only `<socket_dir>/<id>/` into container `<id>`. It is listed as
  deferred in `orchestrator_lifecycle_contract` with owners SRS-ARCH-004 /
  SRS-ORCH-002, both `passes:true`. The live-ib AC run ("5 paper strategies
  running") needs real strategy containers, so it needs this.
* **Timeout = UNKNOWN.** A submit that hits `IB_OP_DEADLINE` is marked REJECTED in
  the outbox today (owner SRS-EXE-009, itself blocked on SRS-EXE-001).
