=== SESSION SRS-EXE-006 ===
Date: 2026-06-30
Feature: SRS-EXE-006 — implement the initial brokerage adapter for headless IB Gateway
  (docs/SRS.md:150 SRS-5.3; SyRS SYS-52/SYS-65/AC-2/SYS-2e; StRS C-2, SN-3.02; API-5).
Outcome: complete (2026-07-15 operator-run paper_account_round_trip GREEN — TWS v176 wire protocol implemented + live-verified; flip via integrate --mode complete --force-complete).

SELECTION: this session was launched on SRS-ERR-001. I diagnosed that ERR-001, SRS-EXE-001,
and SRS-EXE-002 (the three the scheduler offered in id-order) are all already-built SDK-surfaces
on main that stay passes:false, blocked on the SAME unbuilt keystone — the SRS-EXE-006 IB adapter
(+ orchestrator/Python runtime + SRS-PERF-001 latency). I recorded the missing dependency edges
(ERR-001->EXE-006; EXE-001->EXE-006,PERF-001; EXE-002->EXE-006) so the scheduler stops mis-offering
them, then surfaced the routing-vs-foundational conflict (AskUserQuestion). Operator chose
"Build SRS-EXE-006 keystone". Acquired EXE-006 collision-safely via agent_pool's locked primitives
(targeted claim — claim can't target; EXE-003/004/005 sort before EXE-006).

What I built (default build — fully tested solo, no network):
- crates/atp-adapters/src/interactive_brokers.rs (NEW):
  * classify_ib_order_error: deterministic IB TWS error-code -> SyRS SYS-64 OrderErrorCategory
    (100->RATE_LIMITED, 200/203->INVALID_SYMBOL, 201+reason->INSUFFICIENT_BUYING_POWER,
    502/504/1100/2110->CONNECTIVITY_BLOCKED). THE concrete artifact SRS-ERR-001's broker
    categories were vocabulary-only without. brokerage_error -> AdapterError::Brokerage is the
    single IB-error -> common-taxonomy crossing (carries category + raw code/message; never dropped).
  * IbGatewayConnection transport seam (6 ops: submit/cancel/subscribe/historical/account_status/
    positions) returning canonical DTOs; raw IbApiError confined to the seam.
  * InteractiveBrokersBrokerage<C> implements the CANONICAL BrokerageAdapter/MarketDataAdapter/
    HistoricalDataAdapter traits (SYS-52) so callers use the documented interface + failures flow
    through AdapterError. InteractiveBrokersAdapter::with_gateway bridges the documented zero-config
    provider to the functional runtime (connectionless = NotConfigured by design).
  * IbConnectionConfig: literal-IP host (no DNS — can't hang outside the connect deadline),
    fail-closed on malformed/non-Unicode/zero ATP_IB_* (IbConnectionConfigError).
- crates/atp-adapters/Cargo.toml: ib-live-transport feature (NON-DEFAULT) gates TcpIbGateway
  (the live socket scaffold) so the default public surface never advertises a half-built live path.
- crates/atp-adapters/tests/srs_exe_006_ib_adapter.rs (NEW): 10 solo boundary tests over a
  deterministic FakeIbGateway + 1 #[ignore]+feature-gated paper_account_round_trip (the operator
  flip gate; fails closed without ATP_RUN_INTEGRATION=1; binds fixed port 4002).
- tools/ib_adapter_check.py (NEW, 10 checks): transport seam, classifier, canonical boundary,
  provider bridge, config fail-closed, live fail-closed (timeout+no-DNS+feature-gate), integration
  harness (fails-closed gate), serialized status, cargo smoke (boundary suite + --features --no-run
  compile of the gated test). Wired into init.sh + run_ci_locally.sh + ci.yml.
- tests/domain/test_ib_adapter_envelope.py (NEW, L7 safety-paired, 16 tests): behavioral + 11
  mutation non-vacuity + scope honesty (serialized, operator-gated, passes:false).
- architecture/runtime_services.json: adapter_contract.ib_brokerage_runtime sub-block.
- tools/critic_check.py (prep): SAFETY_PATH_RE += /interactive_brokers.rs, ib-adapter,
  brokerage-runtime, srs-exe-006 (deliberately NOT ib-gateway — avoids docker/ib-gateway.Dockerfile).
- No new crate dependency (atp-types only); no vendor SDK.

What I tested (per step):
- Step 1: ./init.sh -> "Environment ready" (new IB adapter runtime check gate green). PASS.
- Step 2: drove the adapter over the FakeIbGateway through the canonical traits (every SYS-64
  category, never-drop, account/positions, connectivity boundary); config fail-closed cases;
  live-transport (feature-on) fails loud + the operator gate fails closed without the env. PASS.
- Step 3 (AC: passes IB paper-account tests for submit/cancel/market-data/historical without the
  TWS GUI): the operator-gated paper_account_round_trip is the AC's real verification — it requires
  a real headless IB paper account (port 4002) + --features ib-live-transport + ATP_RUN_INTEGRATION=1.
  Cannot run in the parallel agent pool -> SERIALIZED (passes:false).
- cargo test --workspace green (0 failed); default + feature clippy clean; rustfmt + ruff clean;
  pytest 'not integration and not e2e' 2545 passed; ib_adapter/adapter/dependency_boundary/
  architecture/adapter_isolation checks PASS.

Critic verdicts:
  deterministic (tools/critic_check.py --staged): APPROVE — 0 findings on prep + feat + R1..R7.
  judgment (tools/codex_review.sh --base 0cc4483 prep): NEEDS-ATTENTION across 7 rounds, GENUINELY
    CONVERGING (each round's prior findings were addressed and did NOT recur):
    R1 explicit connect timeout + confine raw IbApiError (FIXED); R2 canonical AdapterResult
    boundary + fail-closed config (FIXED); R3 cargo-smoke fail-closed + connect tries all addrs
    (FIXED); R4 bridge documented provider -> functional runtime (FIXED); R5 bound DNS (literal-IP)
    + fail-closed operator gate (FIXED); R6 feature-gate the live scaffold + non-Unicode env
    (FIXED); R7 implement account_status/positions + compile gated test target (FIXED). R8
    (completed post-integration once Codex's rate limit reset, at operator request): needs-attention
    with EXACTLY ONE finding, the IRREDUCIBLE serialized boundary — "TcpIbGateway returns the pending
    sentinel for every op, so it cannot satisfy the paper-account path; implement the TWS wire protocol
    [cannot be done/verified solo] OR keep the live transport off the exposed surface [DONE — it is
    feature-gated OFF by default behind ib-live-transport]". The R1-R7 fixable findings did NOT recur;
    the review CONVERGED to this single by-design operator-gated remainder.
    Codex never reached a clean APPROVE; the recurring theme is the live TWS wire protocol, which
    is the IRREDUCIBLE serialized boundary (it cannot be verified solo — exactly why the AC requires
    operator-initiated IB paper-account integration). Recorded honestly; NEVER faked APPROVE. The
    commit ships a SERIALIZED substrate and does NOT claim passes:true (S65 ERR-001 precedent: a
    serialized surface with a needs-attention verdict is no over-claim). Committed on the operator's
    standing authorization to build this keystone (AskUserQuestion) + the deterministic APPROVE.

Resume / next (to flip passes:true):
- The operator runs the gated integration: `ATP_RUN_INTEGRATION=1 cargo test -p atp-adapters
  --features ib-live-transport --test srs_exe_006_ib_adapter -- --ignored paper_account_round_trip`
  against a real headless IB paper account (port 4002), after completing the TWS wire encoding in
  TcpIbGateway (the only deferred piece; see ib_brokerage_runtime.deferred[]). Then re-run Codex
  for a final clean pass and integrate --mode complete.
- DOWNSTREAM UNBLOCK (the point of this keystone): SRS-ERR-001 can now wire the broker-validation
  categories (classify_ib_order_error / AdapterError::Brokerage) into its envelope CLI; SRS-EXE-001
  /002 can wire the IB adapter into the live/sim dispatch; ERR-8 (kill-switch) gains the IB cancel/
  disconnect path. The dependency edges are recorded in tools/feature_deps.json.

=== FOLLOW-UP (2026-07-15): TWS v176 wire protocol implemented + operator paper-account run GREEN — COMPLETE ===
- Operator session (keystone-unblock directive: EXE-006 gates ~34 dependents). Implemented the deferred TWS wire protocol that was the single irreducible R8 boundary, then ran the AC's real verification against the operator's live headless IB Gateway paper account.
- EVIDENCE: paper_account_round_trip GREEN — `ATP_RUN_INTEGRATION=1 ATP_IB_HOST=127.0.0.1 ATP_IB_PAPER_PORT=4002 cargo test -p atp-adapters --features ib-live-transport --test srs_exe_006_ib_adapter -- --ignored paper_account_round_trip` → `test result: ok. 1 passed` (1.27s), driving submit → cancel → market-data subscribe → historical → account_status → positions against real account DU5302722. The gateway's own logs independently confirm the wire: `Server version is 176`, `Start API message, ClientID=101`, `OrderFactory - transmitting order: ... AAPL Stock (NASDAQ.NMS), size=1 ... type=MKT`, `Order Canceled`, `NetLiquidation ... 30101.80`.
- BUILT: `crates/atp-adapters/src/interactive_brokers/wire.rs` — v100+ framing (4-byte BE length + NUL fields, MAX_FRAME ceiling; handshake payload is RAW/no-NUL per ibapi `comm.make_msg`, a real-gateway requirement discovered live), handshake pinned to server version 176 (fails closed on any other), startApi + nextValidId (reqIds nudge fallback), and all six ops: placeOrder / cancelOrder / reqMktData(delayed) / reqHistoricalData(1d TRADES) / reqAccountSummary / reqPositions. Bounded per-op deadlines (IB_CODE_WIRE_TIMEOUT — a mute gateway FAILS, never hangs). `TcpIbGateway` now caches an IbSession behind a Mutex, dropped on transport fault.
- GOLDEN-PINNED: `crates/atp-adapters/tests/srs_exe_006_ib_wire.rs` (27 tests, ephemeral-loopback fake gateway, parallel-safe) asserts every outbound frame byte-for-byte against vectors generated from the official `ibapi==10.19.4` EClient at serverVersion 176 (placeOrder = 115 fields), plus the fail-closed edges.
- FAIL-CLOSED / SAFETY (adversarial-review-driven, 8 rounds, every finding real + fixed with a test): Inactive/unknown order status ≠ ACK; PendingCancel ≠ cancel success (terminal Cancelled/ApiCancelled or code 202 only); openOrder echo ≠ acceptance; historical refuses non-Equity + non-SplitAdjusted (IB daily TRADES bars are split-adjusted only); impossible civil dates (2026-02-31) fail closed; SRS-EXE-003 validate() at the transport seam; the LIVE IB account is HARD-GATED before any socket on SRS-EXE-001 admission (session + composite paths) — the adapter alone can never place a real live-account order; 10197 "no market data during competing live session" confirms the subscribe (registered, ticks withheld — pre-market/contended) while a real 354 still fails.
- CONTRACT / HONESTY: `architecture/runtime_services.json` ib_brokerage_runtime status serialized→verified (+pinned_server_version 176, wire_tests). `tools/ib_adapter_check.py` now (a) RUNS the fake-gateway wire suite, (b) under ATP_RUN_INTEGRATION=1 EXECUTES paper_account_round_trip itself (evidence not metadata), (c) refuses status=verified unless this note records `paper_account_round_trip GREEN` (fail-closed both directions). `tests/domain/test_ib_adapter_envelope.py` updated in lock-step (live-account gate, composite gate, evidence-required status, operator-mode). Only the SRS-EXE-004 composite (combo/BAG) wire stays operator-gated pending (still fails closed via IB_CODE_LIVE_WIRE_PROTOCOL_PENDING) — kept in deferred[].
- GATES: fmt + clippy -D warnings clean; cargo -p atp-adapters (all suites incl. wire) green; ib_adapter_check PASS; adversarial review vs origin/main converged (the last finding was the by-design "verified without recorded evidence" state — self-resolves with THIS note). Live-run gotcha: IB Gateway's API listener wedges after the first client disconnects (silent handshake even to an independent Python client — proved it was gateway state, not the adapter); a full Gateway restart clears it and the round trip then passes in one session.
- Flip: `integrate SRS-EXE-006 --mode complete --force-complete` (honesty guard trips on IB keywords by design; the flag records the operator attestation above). Unblocks the ~34-dependent cluster (SRS-ERR-001, SRS-EXE-001/002/004/005/008/009, SRS-MD-001/003/005/006/007, SRS-NOTIF-001, SRS-SAFE-001, SRS-DATA-019/020, ERR-8, ...).

=== FOLLOW-UP (2026-07-16): adversarial hardening (5 real findings) + operator-authorized flip ===
- After the live paper-account round trip went GREEN, the adversarial review (vs origin/main) found FIVE distinct, real defects across successive rounds — each fixed with a regression test; none recurred (genuine convergence, not churn):
  1. NUL/control-byte wire injection: a strategy-supplied symbol with an embedded \0 could shift NUL-delimited TWS fields. `encode_frame` now rejects any field with a byte < 0x20 before the socket write; fake-gateway test covers order/subscribe/historical.
  2. 10197 "no market data during competing live session" was reported as subscribe SUCCESS — it means IB is WITHHOLDING the stream, so subscribe now fails closed (the live run empirically confirmed subscribe still passes via a clean protocol ack, so nothing was lost).
  3. Verified-status gate trusted hand-editable markdown → replaced with a machine-checkable, code-bound evidence artifact (architecture/ib_paper_account_evidence.json): written ONLY by the operator run (ATP_RUN_INTEGRATION=1, check_cargo_smoke) from the observed `test result: ok`, and validated against a SHA-256 of wire.rs + interactive_brokers.rs + the integration test, so any wire change staleness-invalidates it. Forged/stale/absent all fail closed (tested).
  4. Public protocol-version contract drift: version() advertised IB_TWS_API_VERSION="10.45" while the wire pins server version 176. Reconciled to the actual package generation "10.19.4" (distinct field) and bound the metadata pinned_server_version=176 to the Rust IB_PINNED_SERVER_VERSION const (ib_adapter_check enforces equality — can't drift).
  5. Connectivity-loss codes (1100/2110) did not drop the cached session → next op could reuse a dead socket. `is_transport_fault` now DERIVES the connectivity set from classify_ib_order_error (shared is_connectivity_fault helper), and is_informational_notice no longer masks 2110 as a benign farm notice. Fake-gateway test proves 1100 AND 2110 → fail + fresh reconnect handshake.
- IRREDUCIBLE boundary (the one finding that did NOT converge): "verified status can be self-attested by committed JSON." This is the operator-attestation boundary — CI has no IB Gateway, so the live paper leg cannot be re-proven in the verifying context, and any committed evidence is author-controlled. Per the repo's operator-gated (SyRS SYS-2e) pattern + AGENTS.md, this resolves via HUMAN AUTHORIZATION (`integrate --force-complete`), never a faked APPROVE. The AUTHORITATIVE verification did run: ATP_RUN_INTEGRATION=1 actually connected to the operator's IB paper account and ran the six-op round trip (many times; latest digest a7591ba, 1.65s), which cannot be self-attested.
- OPERATOR DIAGNOSTIC EVIDENCE (2026-07-16, operator-requested): added `crates/atp-adapters/tests/srs_exe_006_ib_diagnostic.rs` (per-op live health check, ignore+ATP_RUN_INTEGRATION-gated, NOT in the evidence digest). Ran it against the live gateway: 7/7 runs, 6/6 operations each (submit broker_order_id=14, cancel, subscribe ib-md-9000, historical bars=32 — served on demand despite the Historical Data Farm showing yellow, account records=3, positions records=0), zero CLOSE_WAIT accumulation even under 4 rapid no-gap reconnections. Characterized the earlier "wedge": IB Gateway leaves a prior API connection in CLOSE_WAIT and serves ONE client at a time, so a connection made while the gateway is not-fully-ready leaves a socket that blocks the single API slot until restart — gateway-side (the client can't force the server to reap it), and irrelevant to real usage where the execution engine holds one long-lived session. NOT conflated with the 1100/2110 in-session connectivity fix.
- FLIP: operator explicitly authorized the flip (2026-07-16) after reviewing the per-op diagnostic. `integrate SRS-EXE-006 --mode complete --force-complete` (honesty guard trips on IB keywords by design; --force-complete records this operator attestation). Unblocks the ~34-dependent cluster (SRS-ERR-001, SRS-EXE-001/002/004/005/008/009, SRS-MD-001/003/005/006/007, SRS-NOTIF-001, SRS-SAFE-001, SRS-DATA-019/020, ERR-8, ...).
