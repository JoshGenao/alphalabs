=== SESSION SRS-EXE-003 ===
Date: 2026-06-30
Feature: SRS-EXE-003 — support market/limit/stop/stop-limit orders for equities and options
  in live and paper modes.
Outcome: serialized (passes:false) — the live order envelope now carries the order type and the
  IB adapter validates+acknowledges each type; the real-IB e2e + the sim-engine OrderLeg bridge stay deferred.

What I did:
- Landed the OrderSubmission ENRICHMENT the codebase explicitly deferred to SRS-EXE-003 (the
  order_routing.rs docstring + order_type_contract.deferred[] named it): atp_types::OrderSubmission
  gained asset_class / side / order_type (matching the sim's OrderLeg) + OrderSubmission::new +
  OrderSubmission::validate (delegates to OrderType::validate_prices — the SAME rule paper_order +
  fill_model apply, so live↔paper cannot drift). Updated all ~28 construction sites across 4 crates.
- atp-adapters: InteractiveBrokersBrokerage::submit_order now VALIDATES the order before submission,
  failing closed with the new AdapterError::InvalidOrder so a malformed order NEVER reaches the
  gateway (a malformed order can never create a live broker order — the safety invariant). This
  closes the S53-promised "the live intake (SRS-EXE-006) will delegate to validate_prices".
- Tests: crates/atp-adapters/tests/srs_exe_003_order_types.rs (live-adapter-test-mode over a
  deterministic recording gateway — all 4 types accepted+acked for equity+option; non-positive
  prices fail closed without reaching the gateway; envelope round-trips the order type). L7
  tests/domain/test_order_type.py extended with 2 live-adapter safety tests (the mandatory
  safety pairing for the order_routing/order_lifecycle/order_type paths).
- Contract reconciliation: order_type_contract.description + deferred[] + the order_routing.rs
  docstring updated — BOTH paths now consume the order type. STILL deferred (why passes:false):
  the SRS-EXE-008 ClientCorrelationId key; the OrderSubmission → PaperOrderRequest/OrderLeg bridge
  the real PaperSimulationEngine::accept_order needs (SRS-ORCH-*/SRS-SIM-001 seam); the real-IB
  wire (operator-gated, SRS-EXE-006).
- StructuredOrderError (SRS-ERR-001 audit envelope, carries the unchanged original order) crossed
  clippy's result_large_err threshold once OrderSubmission grew; added a crate-level allow in
  atp-types + atp-execution with the audit-payload rationale (cold error path carries the full
  order by contract — NOT Boxed away from it). [If Codex prefers Box<OrderSubmission>, that's the
  alternative — ~17 sites: 8 constructors + ~9 `*err.original_order` comparison readers.]

What I tested (per AC step):
- Step 1 (./init.sh → ready): PASS.
- Step 2/3 (each order type accepted/validated/state-tracked/acknowledged in live adapter test mode
  + internal simulation): PARTIAL/serialized — the LIVE-adapter-test-mode is proven over a
  deterministic gateway (srs_exe_003_order_types.rs); the SIM side shares the SAME validate_prices
  rule (paper_order::validate_leg, pinned by order_type_check.py + the existing srs_exe_003 tests).
  The full end-to-end needs the real-IB wire + the sim-engine OrderLeg bridge → passes:false.
- Step 4 (objective evidence, leave passes:false): DONE.
- Commands: cargo check/clippy -D warnings/fmt --check PASS; cargo test --workspace green (91 ok
  result lines incl. the new test + 28 enriched sites); tools/order_type_check.py (venv) PASS;
  tests/domain/test_order_type.py → 7 passed.

Critic verdicts:
  deterministic (tools/critic_check.py --staged): WARN (3) — OVERRIDE: all flagged "price" fields
    are i64 minor units (limit_price_minor / stop_price_minor), never f64; two are doc comments,
    one a test literal `0`. No float money arithmetic introduced.
  judgment (tools/codex_review.sh origin/main):
    r1 -> needs-attention [high] public order-type docs contradict the new live-path behavior
      (order_type.rs module docs + order_type_check.py + the srs_exe_003 test docstring + the domain
      test docstring still said "live path does NOT consume / OrderSubmission carries only symbol+quantity").
      FIXED: swept ALL four stale docs to "both paths consume the order type; passes:false is for the
      deferred end-to-end halves (real-IB wire + OrderSubmission->OrderLeg bridge), not a missing vocabulary."
      (Lesson confirmed: sweep the .rs MODULE doc + the check docstring + the test docstrings, not just
      runtime_services.json, when a deferred surface lands.)
    r2 -> needs-attention: [high] OrderSubmission::validate only checked PRICES, so a blank symbol or
      non-positive quantity could still reach the gateway (breaking live/paper parity) -> FIXED: added
      OrderSubmissionError {BlankSymbol, NonPositiveQuantity, InvalidOrderType} and validate() now
      enforces non-blank symbol + quantity>0 + price positivity (the SAME rules paper_order::validate_leg
      applies); adapter + domain tests prove a blank-symbol/zero-qty order never reaches the gateway.
      [medium] more stale order_type_contract.deferred entries ("atp-execution has no order-type intake /
      OrderSubmission carries only symbol+quantity") -> FIXED (reconciled; real-IB wire + NFR-P1 + Python
      authoring + sim OrderLeg bridge remain the genuine deferrals).
    r3 -> needs-attention: [high] ExecutionEngine::dispatch_order routed to the sim port WITHOUT
      validating (parity left to the port impl) -> FIXED: dispatch_order now validates at the SHARED
      entry before routing, so a malformed order reaches neither the broker nor the sim port (Rust unit
      test + L7 domain test prove zero sim-port calls). NOTE: mapped to OrderErrorCategory::InvalidSymbol
      (the order-rejection bucket) with the precise reason in error_type — a dedicated invalid-order-params
      category is a 111-ref cross-cutting SRS-ERR-001 taxonomy change, deferred. [medium] one more stale
      order_type.rs module-doc line -> FIXED (both intakes now apply validate_prices).
    r4 -> needs-attention: [high] route_order/submit_live_order (public live entries) sent the order to
      broker.submit_order WITHOUT validating (only dispatch_order + the adapter did) -> FIXED: submit_live_order
      now validates on the live path immediately before broker.submit_order (AFTER the ERR-2/3 connectivity+
      freshness gates take precedence, BEFORE the broker call), so a malformed order never reaches the broker
      even via a direct route_order/submit_live_order call. Rust unit + L7 domain test prove broker.calls==0.
      GOTCHA: initial placement (before the mode/connectivity match) broke 3 ERR property tests (a malformed
      Paper/disconnected/stale fuzz order got InvalidSymbol instead of NonLive/Connectivity/Stale) -> moved
      into the connected+fresh arm so reachability errors keep precedence.
    r5 -> needs-attention: BOTH [high] were pure DOC-DRIFT (no code bugs — the validation code converged):
      a stale evidence STRING in order_type_check.py + a second deferred-list passage in order_type.rs ->
      FIXED. Then did a THOROUGH residual sweep and fixed ~6 more scattered "live intake will / (future)
      live / carries only symbol+quantity / live consumption deferred" references across order_type.rs,
      order_type_check.py, and 5 runtime_services.json blocks (order_type_contract description trailing
      clause + 7544/7550/7653 notes). Added an L7 contract-coherence test asserting the check evidence
      says "BOTH paths consume" (not the stale claim). Final sweep clean.
    r6 -> needs-attention: [high] validate() ACCEPTED AssetClass::Option orders with just an underlying
      symbol — but options need full contract identity (underlying+expiration+strike+right); the repo's
      SecurityKey already fails closed on Option. -> FIXED (honest scope): OrderSubmission::validate now
      REJECTS AssetClass::Option fail-closed (OptionContractIdentityUnsupported) pending SRS-EXE-004/
      DATA-004; option orders never reach the broker/gateway. Updated adapter+domain tests (option ->
      rejected, equity -> accepted) and reconciled the "equity AND option" doc/contract claims to
      "equity; option fail-closed pending contract identity". The order TYPES themselves are
      asset-agnostic + proven for equities.
  CONVERGENCE: 6 Codex rounds. The CODE converged at r5 (that round was pure doc-drift); r6 surfaced the
    one remaining real code gap (option identity), now fixed. Did NOT run r7 — code is sound, doc-drift
    thoroughly swept; the remaining deferrals (real-IB wire, sim OrderLeg bridge, option contract
    identity, EXE-008 idempotency) are named owners. Integrated serialized on the operator's "continue
    Codex adversarial review" directive + the recorded honest verdicts (never faked APPROVE).

Known issues / notes for next agent:
- SRS-EXE-003 stays passes:false. To FLIP: (1) the OrderSubmission → OrderLeg bridge so the real
  PaperSimulationEngine consumes the enriched envelope (SRS-ORCH-*/SRS-SIM-001), (2) the SRS-EXE-008
  ClientCorrelationId idempotency key, (3) the operator-run real-IB paper integration (SRS-EXE-006,
  --features ib-live-transport). Then each order type is proven end-to-end in BOTH modes.
- The order-type AUTHORITY (validate_prices) is the single shared rule; do NOT add a second copy.
  Both the adapter (live) and paper_order (sim) delegate to OrderType::validate_prices.
Resume / next: wire the OrderSubmission→OrderLeg sim bridge, or pick another execution feature.
