=== SESSION SRS-SDK-004 ===
Date: 2026-07-03
Feature: SRS-SDK-004 — deliver order event callbacks to Python strategy code
Outcome: serialized (passes stays false)

What I did:
Built the SDK-owned production delivery seam for order-event callbacks — the piece
that was genuinely missing. Prior sessions had built the prerequisites: the Rust
source-neutral category authority (atp-types OrderEventCategory + OrderLedger::
transition_with_event), the Python OrderEvent payload + assert_order_event_payload
guard + p95 budget constants, the scattered fill VALUES (fill_model/cost/
virtual_ledger, integer minor units), and the PERF-001 percentile substrate
(perf.rs: LatencyPercentiles / LatencyVerificationArtifact, LatencyNfr::
OrderEventCallback == NFR-P4, legs live/paper). The only end-to-end dispatch path
was a TEST-ONLY _RefDispatcher (atp-simulation/src/lib.rs:58: "no callback runtime
yet"), and there was NO real paper p95 measurement. With SRS-SIM-001/002 green,
the paper side became buildable.

- python/atp_strategy/dispatch.py (NEW) — the reusable delivery seam the
  runtime_services.json deferred[] note says "production dispatchers re-use":
  * SimulatedFill — the atp-simulation -> SDK boundary descriptor (mirrors the
    Rust fill field homes; integer minor units).
  * build_order_event — descriptor -> OrderEvent; minor-units -> float via
    MINOR_UNITS_PER_UNIT=100 (cents; reuses cost.rs convention); round-trips to
    the nearest minor unit; category carried through from the engine (NOT
    re-derived — the Rust authority owns derivation).
  * deliver_order_event — the shared guard->invoke->sample seam every dispatcher
    reuses: assert_order_event_payload (fail-closed, never deliver malformed) ->
    Strategy.on_order_event -> monotonic ns latency sample. Fetches the callback
    once (None/missing/non-callable sink fails closed).
  * deliver_simulated_fill — paper-path entry (build + deliver).
  Host binding, imported explicitly from the submodule — NOT re-exported on the
  author-facing atp_strategy __all__ (store_history.py precedent). Rust core never
  imports it (boundary is a DTO, not FFI).
- crates/atp-types/src/bin/nfr_p95_cli.rs (NEW) + Cargo.toml [[bin]] — drives the
  PERF-001 percentile engine over caller-supplied ns samples so the Python test
  evaluates p95 through ONE authoritative percentile path (no re-implemented
  math). Default path = percentile computation (honest: NOT a PTP-disciplined
  NFR-P4 artifact); --ptp-offset-ns + --window-*-ns opt into the full
  LatencyVerificationArtifact for an operator on a PTP host. Fail-closed on
  unknown leg/flag, partial PTP set, empty/unparseable samples. 11 Rust unit tests.

Design decision — did NOT touch architecture/runtime_services.json or its check
script: the needle "NFR-P4 p95 latency proof deferred to SRS-EXE-001 + SRS-SIM-001"
stays ACCURATE (the full PTP-disciplined proof is still deferred; my seam is the
reusable helper, not the full runtime), and the check does hasattr on the
top-level package. Registering the seam would force __init__ re-exports + risk the
documentation contract test. The seam is pinned by the new domain test + its
module docstring instead. Avoids CI-mirror / shared-file churn.

What I tested (per step):
- Step 1 (init.sh -> Environment ready): PASS — ./init.sh -> "✓ Environment ready".
- Step 2 (exercise via documented API/CLI + fixtures): PASS (paper) — production
  seam driven via deliver_simulated_fill / deliver_order_event into a recording
  strategy, and nfr_p95_cli over real samples. Live path DEFERRED (needs IB).
- Step 3 (AC): (a) fill/partial/cancel/reject carry fill price/qty/commission/order
  IDs — PASS through the production seam (tests/domain/test_paper_callback_delivery
  .py FourCategoryProductionDeliveryTest) + existing contract/unit tests. (b) paper
  p95 <100ms from simulated fill — PASS for the SDK delivery seam:
  nfr_p95_cli paper -> verdict:PASS over ~2000 samples, plus a binary-free
  max<100ms guard; the engine-inclusive PTP-disciplined proof is DEFERRED to
  SRS-SIM-001. (c) live p95 <1000ms from broker fill ack — DEFERRED (needs IB
  Gateway; SRS-EXE-001/EXE-006).
- Step 4 (record evidence, leave passes false end-to-end): PASS — evidence
  recorded; integrated --mode serialized, passes stays false.
- Full gates: cargo fmt --check clean (formatted ONLY the new bin file, never the
  crate); cargo clippy -p atp-types --all-targets -D warnings clean; cargo test
  --workspace 0 failures (incl. 11 CLI unit tests); pytest "not integration and not
  e2e" 2851 passed / 4 pre-existing skips. NOTE: run_ci_locally.sh dies at the
  mypy step because main is pre-existing mypy-red (66 errors in files I did not
  touch); my dispatch.py is mypy-clean. integrate does not re-run mypy.

Critic verdicts:
  deterministic (critic_check.py --staged): WARN — 3x money:float-arithmetic on
    price-named fields. OVERRIDE: the OrderEvent fill_price/commission float schema
    is pre-existing and SDK-001-locked; canonical exact money stays the integer
    minor unit on the descriptor; the minor->float conversion round-trips to the
    nearest minor unit (property-tested). No Decimal migration (would break the
    SDK-001 OrderEvent float contract). critic_check.py --range: APPROVE.
  judgment (adversarial_review.py, reviewer=claude-fallback): APPROVE. First pass
    warned "nfr_p95_cli fail-closed parser untested"; fixed by adding 11 Rust unit
    tests (parse_args/read_samples) + a Python subprocess NfrCliFailClosedTest;
    re-run -> APPROVE, no findings.

Resume / next (to flip passes:true):
- LIVE leg (blocking, needs IB): SRS-EXE-001 / SRS-EXE-006 build the live IB
  dispatcher (broker fill ack -> OrderEvent -> deliver_order_event) and measure
  live callback p95 <1000ms on a PTP-disciplined host.
- PAPER engine-inclusive leg: SRS-SIM-001 wires the running simulation engine to
  EMIT SimulatedFill descriptors (fill produced -> descriptor) and measures the
  full paper p95 <100ms from simulated fill through the engine on a PTP host
  (nfr_p95_cli --ptp-offset-ns ... paper produces the artifact).
- State-preserving events (cancel rejection): SRS-EXE-006 / SRS-SIM-002 event-kind
  API (out of scope for the transition-derived authority).
- The reusable seam (deliver_order_event) and nfr_p95_cli are the substrate both
  legs consume — do NOT rebuild; wire the descriptor source + run on a PTP host.

=== FOLLOW-UP 2026-07-04 (Codex re-review after adversarial_review.py fix 3880988) ===
The original judgment pass ran as the CLAUDE FALLBACK because adversarial_review.py
was dropping Codex's verdict (the `--json` envelope nests it under `result`;
extract_json saw no top-level `verdict` → "unparseable" → 100% fallback). Commit
3880988 (`_verdict_from_envelope`) fixes that. Re-running the now-fixed reviewer
against this feature reached CODEX, which found two real fail-opens the fallback
missed (both now fixed, commit fa9adef):
- [high] negative commission delivered as a negative fee → P&L corruption. Fixed at
  TWO layers: `_minor_to_units` rejects negative minor units, AND
  `assert_order_event_payload` now enforces non-negative commission (symmetric with
  its fill_price rule) so the SHARED deliver_order_event seam protects the live
  dispatcher's pre-built OrderEvents too, not just the SimulatedFill builder path.
- [medium] `deliver_order_event` could return a negative NFR-P4 latency sample for a
  future / wrong-clock-domain fill stamp. Fixed: reject `fill_at_ns > now` before
  delivery; sample is now guaranteed >= 0.
New L7 tests cover both (incl. a direct pre-built OrderEvent with negative
commission). Verdicts: deterministic APPROVE; judgment (reviewer=CODEX) APPROVE.
Still serialized (passes:false) — the live + engine-inclusive p95 legs are unchanged.
