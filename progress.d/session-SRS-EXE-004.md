=== SESSION SRS-EXE-004 ===
Date: 2026-07-01
Feature: SRS-EXE-004 — support multi-leg options orders as composite transactions
(docs/SRS.md line 148; SyRS SYS-4 / SYS-40 / SYS-82; StRS SN-1.24).
Outcome: SERIALIZED (code merged, passes stays false)

WHY SERIALIZED: the AC names three surfaces — "a four-leg options order is
submitted as one composite order in IB LIVE TEST MODE, simulated as one composite
order in paper mode, and displayed as one composite DASHBOARD position." The IB
live combo wire is operator-gated (SRS-EXE-006, feature ib-live-transport) and
the dashboard is SRS-UI-002 — neither is runnable solo/in-parallel. Step 4 says
leave passes false until proven end-to-end. So this lands the load-bearing
substrate + deterministic demonstrations; passes:false.

What I did (the substrate every option deferral pointed at EXE-004 for):
- NEW crates/atp-types/src/composite_order.rs — the vendor-neutral option-contract
  identity + composite order envelope:
  * OptionContractIdentity (underlying + expiration + strike_minor + call/put
    right; private fields; fail-closed constructor — blank underlying /
    non-positive strike / impossible calendar date via leap-year-aware
    ExpirationDate). canonical_key() = deterministic dedup id.
  * CompositeOrderSubmission + CompositeOrderLeg (options-only BY CONSTRUCTION —
    a leg carries an OptionContractIdentity, so an equity leg is unrepresentable).
    validate() fails closed: <2 legs (SYS-4), non-positive leg quantity, non-positive
    leg price (delegates to the shared OrderType::validate_prices authority so
    live/paper cannot drift). One bad leg rejects the WHOLE composite (atomic).
  * StructuredCompositeOrderError (parallel to StructuredOrderError, carries the
    composite; REUSES OrderErrorCategory — no new SYS-64 variant).
- IB adapter composite seam (crates/atp-adapters): BrokerageAdapter::
  submit_composite_order (default NotConfigured) + IbGatewayConnection::
  submit_composite_order; IB impl validates then submits as ONE combo order ->
  ONE OrderReceipt (never one per leg); TcpIbGateway gated scaffold fails closed
  with LIVE_WIRE_PROTOCOL_PENDING (the operator-gated bit).
- Execution-engine LIVE GATE (crates/atp-execution) — added after Codex R1 flagged
  the adapter seam had no ERR-1/2/3 counterpart: LiveCompositeBrokerageSubmit port
  + ExecutionEngine::submit_live_composite_order (pub(crate)) + route_composite_order
  (pub, the ONLY public live entry — derives live-ness from the engine-owned
  LiveDesignation, so a self-asserted StrategyMode::Live can't reach the broker;
  stricter than single-leg submit_live_order which stays pub for its pinned ERR
  contract). Gates in single-leg precedence: non-live -> connectivity (ERR-2) ->
  per-OPTION-CONTRACT freshness keyed by canonical_key (ERR-3, NOT underlying, so
  distinct contracts on one underlying aren't conflated; atomic -> any stale
  contract blocks) -> validate() -> broker port.
- Paper half reuses the existing PaperSimulationEngine (PaperOrderRequest::MultiLeg,
  SRS-SIM-001) — new crates/atp-simulation/tests/srs_exe_004_paper_composite.rs
  pins the four-leg case (one composite; non-option leg fails closed).
- Metadata: composite_order_contract block (+ execution_gate) in
  architecture/runtime_services.json with honest deferred[]. Reframed now-stale
  "identity does not exist / deferred to EXE-004" prose across atp-types
  (order_type + lib.rs OrderSubmission/SecurityKey docs) + atp-market-data
  (the identity TYPE now exists; single-leg OrderSubmission + SecurityKey +
  MD-001 subscriptions keep their UNCHANGED fail-closed-on-Option behavior until
  they wire it). No production behavior changed outside the new composite path.

What I tested (per step):
- Step 1: PASS — ./init.sh -> "✓ Environment ready".
- Step 2: PASS (deterministic, mocked IB via the in-memory gateway double, no real
  IB): cargo test -p atp-adapters --test srs_exe_004_composite_order (4-leg iron
  condor -> ONE broker order id, gateway hit once; empty / single-leg / bad-leg
  composites fail closed, gateway hit ZERO; connectionless adapter -> NotConfigured).
- Step 3: PARTIAL/SERIALIZED — the AC's three surfaces:
  * paper "one composite": PASS — cargo test -p atp-simulation --test
    srs_exe_004_paper_composite (4 option legs -> is_composite()==true, 4 legs, no
    broker route).
  * IB live "one composite": DEMONSTRATED deterministically (adapter over the
    gateway double = "IB test mode") + gated at the execution engine (6 atp-execution
    composite tests: non-live/connectivity/any-contract-stale/malformed/authority
    blocked before the broker; happy path routes once). REAL IB combo wire is
    operator-gated (SRS-EXE-006) -> serialized.
  * dashboard "one composite position": DEFERRED to SRS-UI-002 (dashboard not built).
- Step 4: PASS (evidence recorded; passes stays false). L7 pin
  tests/domain/test_composite_order.py (18 invariants). Full gate:
  cargo test --workspace (1202) + pytest "not integration and not e2e" (2628) green;
  cargo fmt --check + clippy -D warnings + ruff clean; gated ib-live-transport target
  compiles; architecture_check / order_type_check / ib_adapter_check / adapter_check PASS.

Critic verdicts:
  deterministic (critic_check.py --staged / --range): APPROVE — no findings
  (interactive_brokers.rs safety path paired with tests/domain/test_composite_order.py).
  judgment (tools/codex_review.sh origin/main):
    R1 -> needs-attention [high] "composite bypasses live submission gates" (no
      execution-layer ERR-1/2/3 path). FIXED: added submit_live_composite_order +
      route_composite_order + LiveCompositeBrokerageSubmit + 6 gate tests.
    R2 -> needs-attention [high]x2: (a) submit_live_composite_order public ->
      bypasses LiveDesignation; (b) freshness keyed by underlying conflates option
      contracts. FIXED: made inner method pub(crate) (only route_composite_order is
      public + authority-gated); keyed freshness by OptionContractIdentity::
      canonical_key + a contract-level-freshness test (same-underlying, one stale
      strike blocks).
    R3 -> Codex usage limit (resets 12:54 PM) — could not run. FELL BACK to the
      manual fresh-context review per prompts/critic_prompt.md: verified both R2
      fixes complete (grep-confirmed pub(crate) + canonical_key + designation
      consulted + no unwrap/panic in production), checked dependency direction,
      single-live invariant, integer money math, no now()-branching, doc/code
      coherence -> APPROVE. Operator can re-run Codex after the reset if desired.

NOTE (dropped prep commit): I initially added a SAFETY_PATH_RE prep commit
(composite_order tokens) but Codex blocks any critic-gate self-modification pending
human review; operator (AskUserQuestion) chose to DROP it and re-review clean.
interactive_brokers.rs is already a safety path, so the domain-test pairing holds
without it. If future protection of composite_order.rs is wanted, add those tokens
under human review.

Resume / next:
- SRS-EXE-004 stays passes:false. To flip: operator runs the IB paper-account
  integration for a composite combo (--features ib-live-transport, SRS-EXE-006
  wire) AND the dashboard shows the composite as one position (SRS-UI-002). Then
  verified-e2e.
- This UNBLOCKS the option half of SRS-EXE-003 (blocked-on SRS-EXE-004/EXE-008):
  the OptionContractIdentity type now exists for EXE-003 to wire onto the single-leg
  OrderSubmission (still fail-closed on options today) and for SRS-MD-001 /
  SRS-DATA-004 to key option subscriptions/snapshots. EXE-004 itself remaining
  passes:false, EXE-003 stays blocked until EXE-004 flips.
- The CompositeOrderSubmission -> PaperOrderRequest::MultiLeg bridge + the Python
  multi-leg authoring surface are the deferred orchestrator/SDK seams.
