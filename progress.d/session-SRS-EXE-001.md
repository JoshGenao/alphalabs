=== SESSION SRS-EXE-001 ===
Date: 2026-10-10
Feature: SRS-EXE-001 — route orders to IB only for the designated live strategy
Outcome: serialized (B: both landings built; the live-ib run waits on SRS-MD-004)

## What this landing is

The live-designation GATE already existed (`ExecutionEngine::route_order`, a private
non-`Clone` `LiveDesignation`, a strategy-bound confirmation token), but nothing ran
it: only fixture CLIs called `route_order`. Landing 1 builds the production call
site, the **live execution host**, plus the operator designation command. Landing 2
(next session) builds the Python order path and the operator REST/CLI handlers.

Operator decisions (2026-10-10): lift the adapter live-account gate when the live
tier can start (deferred from this landing, see below); two serialized landings;
the live tier refuses until SRS-MD-004; approve both critic changes; integrate
serialized over Codex r3's latent finding. (The plan file was not committed:
`agent_pool.py integrate` refuses any branch-committed `progress.d/plan-*.md`.)

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

## Critic verdicts (landing 1)

deterministic (critic_check.py --staged): APPROVE on every commit (prep cd950aa,
  toolchain 3efb404, feat ec03557, docs b6608c7).
Adversarial rounds: 10
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

## Landing 2 (2026-10-10, same session, re-claimed with `claim --id`)

### What I built

* **`python/atp_strategy/live_client.py`** — `encode_submit` / `parse_reply` mirror
  `live_host/protocol.rs`; the encoder refuses whatever the host would (control
  characters, blank symbol, quantity <= 0, a price that is not a whole number of minor
  units, never rounded). `socket_path` validates the strategy id with the host's
  alphabet (a traversal id would reach another strategy's socket). `LiveHostClient`
  never re-sends: a written frame with no valid reply is `LiveOrderOutcomeUnknown`.
  `LiveOrderRouter` is the `order()` leg of a live `StrategyContext`: warm-up and
  asset-class guards, then the host, then ONE queued `ACK` or `REJECTED` event per
  order, delivered through `dispatch.deliver_order_event`; a `refused` reply raises.
  It is the order leg only, not a whole live context or the in-container host process.
* **`python/atp_orchestration/live_designation.py`** — promote-live (REST), `live
  promote` / `live show` (CLI) over `exe001_live_designation_cli`. `promoted_at` is
  null when nothing moved (the snapshot stores who, not when); `warning` carries
  published-not-synced. Composed in `serve()` behind `ATP_LIVE_DESIGNATION_ROUTES=1`
  (requires `ATP_HOT_SWAP_DESIGNATION_STATE`) and in a new composed CLI,
  **`python -m atp_orchestration`**. The LIVE_DESIGNATION workflow is fully_served
  when mounted; a bare runtime still answers the structured 501.
* **`exe001_live_designation_cli` exit codes**: 0 ok, 2 refused input, 3 published
  not synced, 4 state error (lock/read/pre-publish write), 5 another strategy live.
  The handler maps codes, never stderr text.
* **`nfr_p1_ack_cli`** (atp-types) — NFR-P1 p95 over host-monotonic samples using
  the catalog's own budget and `LatencyPercentiles`; `--tier` is required and printed.
* **Contract**: route field types, strict body, `served_by`; CLI exit codes the
  handlers reach; `Command.served_entrypoint` (required with `served_by`; also set for
  SRS-LOG-001's `admin logs`); manual/openapi regenerated; `operator_surface` +
  `strategy_order_leg` blocks pinned by `check_operator_and_strategy_legs`.
* **Flake fix (operator-approved to ride along)**: `test_bbands_property_matches_batch_talib`
  bound now includes TA-Lib's running-sum cancellation error, sqrt(eps) * max|close|;
  the falsifying case is pinned.

### What I tested (per step)

Step 1: PASS — `./init.sh` → `✓ Environment ready`.
Step 2: PASS (fixture tier) — L1 `tests/unit/test_live_client.py` (51); L4
  `tests/boundary/test_live_designation_surface.py` (13, incl. subprocess runs of the
  shipped `python -m atp_orchestration`); L7 `tests/domain/test_live_execution_host.py`
  (12).
Step 3:
  * Explicit confirmation: PASS on REST (428, nothing written) and CLI (exit 3).
  * 1 live + 5 paper through the Strategy API: PASS on the fixture tier — operator
    promotes via the REST handler; 3 orders each; live gets ACK x3 (`IB-*` ids), each
    paper strategy REJECTED x3 with `NON_LIVE_STRATEGY_SUBMISSION/NotDesignatedLiveStrategy`;
    wire ledger = live only. Also: a designation made with the shipped CLI is what the
    host routes on.
  * < 1,000 ms p95, host monotonic clock: PASS on the fixture tier —
    `nfr:NFR-P1 clock:host-monotonic tier:FIXTURE samples:1000 p95_ms:28.04
    budget_ms:1000 verdict:PASS` (p50 25.07, p99.9 34.14). Most of each order is the
    durable outbox write before the broker.
  * Real IB (`--tier LIVE_IB`): NOT RUN — the live tier refuses until SRS-MD-004.
Step 4: NOT DONE — waits for the live-ib run.

Mutation-verified: rounding a price; delivering inside `order()`; a reject mapped as
an ack; always stamping `promoted_at`; dropping the quantity guard; reporting a
corrupt snapshot as a refusal (binary) and as a bad request (handler); the served-
entrypoint and contract guards (negative tests each).

### Critic verdicts (landing 2)

deterministic: APPROVE on every commit.
judgment (codex_review.sh, base origin/main), 6 rounds:
  r1 [high] CLI commands unmounted in any shipped entrypoint → FIXED (`python -m
     atp_orchestration` + subprocess tests + domain test).
  r2 [medium] negative quantity made an undeliverable REJECTED event → FIXED (encoder
     guard). [low] flake fix rides along → operator: keep.
  r3 [high] manual named `python -m atp_cli` (the stub) for served commands → FIXED
     for the class (`served_entrypoint`, incl. `admin logs`). [medium] flake → kept.
  r4 [high] corrupt snapshot reported as bad input → FIXED (distinct exit codes).
  r5 [high] socket path from an unvalidated strategy id → FIXED; [medium] `live show`
     USAGE_ERROR undeclared → FIXED for the class (test requires it on every served
     command); [medium] flake → kept.
  r6 no verdict: the run stopped mid-analysis, then the retry hit Codex's usage limit.
  The harness failover then refused to review: **ROUND BUDGET EXHAUSTED for
  SRS-EXE-001: 9 BLOCK rounds (budget 8, 0 prior authorization(s))**. Options it gives
  the operator: split; close honestly at serialized; or
  `tools/adversarial_review.py --authorize-continue "<why>"` (operator only).
  Every finding of r1-r5 was in scope and is fixed; none repeated a class.
  Integration decision: operator chose "Close at serialized now" (2026-10-10), with the
  budget exhausted, CI green (5,560 passed, every step ran) and mypy clean.

## Resume / next

1. `block SRS-EXE-001 --on SRS-MD-004` (the live tier's stale-data producer).
2. When SRS-MD-004 lands: wire its freshness producer and `ScheduledRestartConnectivity`
   into the `ib` tier; meet the two live-tier preconditions in
   `live_designation_contract.deferred[]` (deadline-bounded transport; a submit timeout
   recorded as UNKNOWN, owner SRS-EXE-009); lift the adapter live-account gate.
3. Operator live window: SRS-EXE-006 paper re-run, then 1 live + 5 paper real strategy
   containers through the host, and `nfr_p1_ack_cli --tier LIVE_IB` over the live
   strategy's samples.

Known trade: the outbox snapshot is rewritten in full per order and acknowledged
orders stay non-terminal until order-state updates exist (SRS-EXE-008/009), so the
per-order write grows with live order count (~25 ms/order at 1,000 orders on this
Mac). Fine at MVP volume; owned there.

Two gaps for the operator, neither owned by an open feature:
* **Container socket mount.** The concrete Docker `StrategyContainerRuntime` must
  bind-mount only `<socket_dir>/<id>/` into container `<id>`. It is listed as
  deferred in `orchestrator_lifecycle_contract` with owners SRS-ARCH-004 /
  SRS-ORCH-002, both `passes:true`. The live-ib AC run ("5 paper strategies
  running") needs real strategy containers, so it needs this.
* **Timeout = UNKNOWN.** A submit that hits `IB_OP_DEADLINE` is marked REJECTED in
  the outbox today (owner SRS-EXE-009, itself blocked on SRS-EXE-001).
