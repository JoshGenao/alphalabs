=== SESSION SRS-EXE-002 ===
Date: 2026-07-17
Feature: SRS-EXE-002 — route all non-live strategy orders to the internal
simulation engine (SRS-5.3; SyRS SYS-2b / SYS-2e / AC-10; StRS SN-1.06 /
SN-1.29 / C-11). AC: "Paper strategy orders never create IB orders; the IB
paper account is available only through operator-initiated adapter
integration tests."
Outcome: serialized (blocked-on SRS-SDK-004 — deployed strategy-runtime order
path; codex r2 holds the flip until real strategy-container submissions route
through dispatch_order, or the operator force-completes on the fixture +
inspection evidence)

What I did:
The routing AUTHORITY was already built (Session 54: `OrderRoute`,
`SimulatedOrderReceipt`, `InternalSimulationSubmit`,
`ExecutionEngine::route_destination`/`dispatch_order` in atp-execution, with
panic-on-touch isolation tests + `tools/order_routing_check.py`). What kept
`passes:false` was the COMPOSITION half named in
`order_routing_contract.deferred[]`. This session landed it, on a base
fast-forwarded to origin/main (branch had 0 local commits, 201 behind):

- `crates/atp-orchestrator/src/order_routing_wiring.rs` — the composition the
  execution crate cannot hold (SRS-ARCH-002):
  * `WiredPaperSimulation`: the REAL SRS-SIM-001 `PaperSimulationEngine`
    behind `InternalSimulationSubmit`, mapping the (SRS-EXE-003-enriched)
    `OrderSubmission` field-for-field onto `OrderLeg` and routing every
    accepted order through `VirtualOrderBook::place_accepted` — the book is
    the single order store (the adoption `progress.d/session-SRS-DATA-021.md`
    named for this runtime; orders enter only via the engine's own intake).
    Constructor holds the CONCRETE engine (adversarial-R4 trusted-capability
    binding); port-side rejections map fail-closed onto `StructuredOrderError`
    (category INVALID_SYMBOL + PascalCase error_type discriminators).
  * `IbBrokerageBridge<C: IbGatewayConnection>`: the REAL SRS-EXE-006
    `InteractiveBrokersBrokerage` behind `LiveBrokerageSubmit` (AdapterError →
    StructuredOrderError preserving the SYS-64 category when present).
  * `RecordingIbGateway`: deterministic mocked-IB transport double counting
    every order-creating wire op (submit + composite-submit both counted so no
    order-creating path is invisible); all non-order ops honest fixture errors.
  * `run_routing_scenario`: N paper (+ optional designated live w/ explicit
    operator confirmation) submissions through the REAL
    `ExecutionEngine::dispatch_order`; returns per-order route/receipt rows +
    ib_orders_created + resting_orders. Fail-closed bounds (1..=10000).
- `crates/atp-orchestrator/src/bin/exe002_order_routing_cli.rs` — the operator
  fixture-verification workflow (resolves deferred[1]'s "no Rust binary calls
  dispatch_order"): allowlist fail-closed arg parser, deterministic key:value
  proof lines, and an AC-10 SELF-CHECK (nonzero exit unless paper created 0 IB
  orders and the designated live created exactly 1). Explicitly labeled NOT the
  deployed strategy-runtime order path — real strategy-container submissions
  through dispatch_order stay deferred (owner SRS-SDK runtime / SRS-ORCH-*).
- Contract/doc sweep (mode-flip cluster): `order_routing_contract` gained a
  `wiring` block; resolved deferred[0] (wiring), [1] (production caller), [4]
  (operator IB-paper surface — EXE-006 flipped with the operator-gated
  round-trip; evidence architecture/ib_paper_account_evidence.json), [8] (R4);
  updated the two "dispatch_order is dormant" risk sentences honestly; final
  line now scopes the remaining SDK-host leg as BEYOND this AC. Swept stale
  "deferred wiring" docstrings in order_routing.rs, atp-simulation
  {lib,paper_order,virtual_orders}.rs; updated tools/order_routing_check.py
  messaging ("ROUTING-AUTHORITY PASS" + wiring pointer) + the pinned needles
  in tests/test_order_routing_contract.py. Did NOT touch the digest-pinned
  crates/atp-adapters/src/interactive_brokers* or dispatch_guard-scanned arms.

What I tested (per feature steps[]):
  Step 1: PASS — ./init.sh → "✓ Environment ready".
  Step 2: PASS — mocked-IB CLI workflow (fixtures + CLI + logs):
    `exe002_order_routing_cli route --paper-orders 30` → ib_orders_created:0,
    simulated_orders_accepted:30, resting_orders:30, verdict:PASS (rc=0);
    `route --paper-orders 30 --designate-live` → ib_orders_created:1 (the
    designated strategy only), verdict:PASS (rc=0); `--paper-orders 0` →
    fail-closed rc=1.
  Step 3: PASS — AC clause 1 (tests): cargo test --workspace all green incl.
    new srs_exe_002_routing_wiring (7: zero-wire-ops paper; 1-live-among-30;
    field-for-field mapping ×4 order types; port-side fail-closed; shared-entry
    fail-closed; bounds; live-leg-through-real-adapter) + exe002_cli_fail_closed
    (10) ; pytest -m "not integration and not e2e" → 3878 passed (incl. new
    tests/domain/test_order_routing_wiring.py, 8 tests). AC clause 2
    (inspection): port 4002 / `TcpIbGateway` exist ONLY in the digest-pinned
    EXE-006 adapter module behind the non-default `ib-live-transport` feature;
    the sole caller is the `#[ignore]` + ATP_RUN_INTEGRATION=1 operator
    round-trip (`paper_account_round_trip`); no non-test code dials it (grep);
    vendor-leakage critic keeps IB tokens out of core crates; sim_fill_check
    pins atp-simulation free of adapter/execution deps.
  Step 4: PASS — this note + the contract `wiring` block record the evidence;
    passes stays false (integrate --mode serialized) per the codex r2 AC-scope
    verdict; dependency edge recorded (block --on SRS-SDK-004) so the
    scheduler won't re-offer this feature until the Python order path exists.

Gate results:
  ruff check: PASS. ruff format --check: my files clean; 13 PRE-EXISTING
    reformat candidates on origin/main remain (toolchain-pin owner; none in
    this diff — verified via git status).
  mypy python/: 68 PRE-EXISTING errors in 16 files, none in this diff (no
    python/ source touched).
  cargo fmt --check: PASS. cargo clippy --workspace -D warnings: PASS.
  cargo test --workspace: PASS (0 failures). pytest: 3878 passed / 4
    pre-existing skips.
  tools/order_routing_check.py: PASS. tests/test_order_routing_contract.py:
    20 passed.

Critic verdicts:
  deterministic: WARN — one finding, money:float-arithmetic at
    order_routing.rs:141. OVERRIDE (one line): PRICE_FIELD_RE matches the
    hyphen in the long-standing doc phrase "price-positivity rule" inside a
    /// comment — no arithmetic, no code change; false positive.
  judgment (tools/codex_review.sh origin/main):
    r1: needs-attention — 1 high: "Fixture scenario is overclaimed as
      production routing" (run_routing_scenario is a self-contained fixture
      scenario, not the deployed strategy-runtime order path; docs calling it
      'the production dispatch entry' could let the close hide that real
      strategy submissions remain unwired). REAL — fixed via the reviewer's own
      recommended option: relabeled every 'production dispatch entry/caller'
      claim (module docs, CLI docs, Cargo.toml, test docstrings,
      order_routing_check.py, order_routing.rs scope note,
      order_routing_contract.wiring + deferred[]) as operator fixture
      VERIFICATION, and added an explicit DEPLOYED STRATEGY-RUNTIME ORDER PATH
      deferred[] entry with named owners (SRS-SDK runtime / SRS-ORCH-*).
    r2: needs-attention ("treat as blocking") — 2 high, both REAL:
      (a) AC-scope: closing complete would hide that real strategy-container
        submissions through dispatch_order are still unwired (the CLI is
        fixture-only). ACCEPTED — this is not fixable in-scope (the Python
        host is SRS-SDK's feature): outcome switched to serialized + block
        --on SRS-SDK-004; contract deferred[] final entry restored to an
        explicit "stays passes:false (integrated serialized)" with the
        deployed-path owner named.
      (b) book() mutable-guard bypass: public MutexGuard<VirtualOrderBook> +
        pub place() let callers rest orders around the intake, contradicting
        the intake-only claim. FIXED — book access is now crate-internal
        (book_guard); public surface is read-only (with_book closure over
        &VirtualOrderBook + open_resting_orders); the only mutation path is
        submit_simulated → place_accepted. Tests updated.
    r3: APPROVE — "No material adversarial findings supported by the staged
      diff and required context." (0 findings; parseError null — NOT the
      empty-summary false-approve shape). Deterministic re-run on the same
      staged diff: WARN, the single documented price-positivity doc-comment
      false positive (override above).

Resume / next: SRS-EXE-002 closes complete. Downstream now unblocked:
SRS-DATA-021 (its flip = re-run its scenario suite over this runtime — the
wiring holds the VirtualOrderBook as the single store as its note demanded;
the SIM-002 fill-loop evolution that rests limit/stop until trigger remains
that owner's work and is restated in order_routing_contract.deferred[]).
Remaining named owners unchanged: SRS-SDK Python host, SRS-EXE-008
correlation-id, SRS-EXE-004 live composites, SRS-MD-004 simulated-order
stale gate, sole-entry-bypass (EXE-006/ORCH).
