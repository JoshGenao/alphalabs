=== SESSION SRS-MD-007 ===
Date: 2026-07-01
Feature: SRS-MD-007 — market-data subscription manager detects tick-sequence gaps
in IB tick streams and reflects gap state in heartbeat/staleness.
Outcome: serialized (code integrated; passes stays false — runtime/dashboard/
order-gate wiring deferred)

What I did:
- Built `SequenceGapDetector` in crates/atp-market-data (per-`SecurityKey` tick-
  sequence tracking + stale-state machine). observe_tick classifies each tick:
    * observed == last+1  → in-sequence; recovers a gap-stale line to Fresh (recovery #1)
    * observed  > last+1  → GAP: marks the line Stale (BEFORE publishing → fail-closed),
      publishes a SequenceGapEvent, surfaces the sink's publish Result on Gap.published
    * observed <= last     → duplicate/backwards: non-monotonic no-op (no gap, no recovery)
  acknowledge_resync() is recovery #2 (Fresh + re-baseline so a post-reconnect
  jump isn't a false gap). freshness() fails CLOSED (unobserved security → Stale).
  stale_since_ns records the FIRST Fresh→Stale onset (preserved across repeated gaps).
- Added atp-types::SequenceGapEvent (4 AC fields: symbol, expected_sequence,
  observed_sequence, observed_at_ns; no broker/vendor/tick leak) + defined
  MarketDataTick.tick_seq as the UPSTREAM PROVIDER sequence (producer contract).
- Made SequenceGapEventSink FALLIBLE (Result<(), SequenceGapPublishError>) so a
  failed SRS-LOG-001/dashboard publication is surfaced, not swallowed (Codex R2/R3).
- Wired SEQUENCE_GAP log event type into log_record_contract JSON + python/atp_logging
  EVENT_TYPES_BY_SOURCE + README (lock-step; log_record_check asserts exact match).
- Added sequence_gap_contract metadata + tools/sequence_gap_check.py static guard
  (fallible-sink + stale-onset guards) exercised by tests/test_sequence_gap_contract.py.

Key decisions / Codex rounds (all resolved, R5 APPROVE):
- R1 [high]: docs overclaimed the deferred MD-004 bridge as a "pure key mapping" —
  the MarketDataFreshnessProbe port is symbol-only while the detector is SecurityKey-
  keyed. FIX: corrected docs; named port security-awareness as a deferred SRS-MD-004/
  ERR-3 change (OrderSubmission already carries asset_class); options fail closed so
  equities (only tradable class) are safe. Did NOT widen the port (~16 sites of other
  features = scope creep).
- R2/R3 [high]: sink was infallible → a lost audit event couldn't be surfaced. FIX:
  made record fallible; Gap.published carries the result; safety (Stale) committed
  before publish (fail-closed proven by gap_stale_state_is_fail_closed_when_publication_fails).
- R3 [high] (real bug): stale_since_ns reset on every gap. FIX: set only on Fresh→Stale
  (proven by repeated_gaps_preserve_the_original_stale_onset_time).
- R4 [high]: tick_seq documented as opaque delivery counter → gaps meaningless. FIX:
  defined tick_seq as the upstream provider sequence + producer contract; ingestion
  adapter (SRS-EXE-006/feed loop) must supply a gap-bearing sequence (deferred owner);
  pinned by producer_contract_a_delivery_renumbered_stream_hides_gaps.

What I tested (per step):
- Step 1: PASS — ./init.sh → "✓ Environment ready".
- Step 2: PASS — exercised the detector over fixture ticks via the Rust integration
  test `cargo test -p atp-market-data --test srs_md_007_sequence_gap` (8 scenarios:
  gap→stale+log, both recoveries, MD-004 freshness value, fail-closed publication,
  repeated-gap onset, per-security isolation, uncanonicalizable fail-closed) and the
  static contract `python3 tools/sequence_gap_check.py` (SEQUENCE-GAP PASS). MD-007 is
  an in-process subscription-manager component — no standalone CLI; there is no
  fixture "provider mock" feed loop yet (that is the deferred SRS-EXE-006 ingestion adapter).
- Step 3: PARTIAL — gap events logged with the 4 AC fields (SEQUENCE_GAP wired into the
  SRS-LOG-001 schema); stale until recovery (both conditions) + fail-closed default: BUILT+TESTED.
  Blocks orders per SRS-MD-004: the detector reports MarketDataFreshness::Stale — the
  exact enum submit_live_order rejects MARKET_DATA_STALE on — but the runtime
  MarketDataFreshnessProbe adapter is DEFERRED (atp-execution must not depend on
  atp-market-data; SRS-ARCH-002). "Visible on the dashboard": DEFERRED (SRS-UI-001/API-001,
  HEARTBEAT channel is contract-only).
- Step 4: DONE — evidence recorded; passes stays false (serialized).
- Full gate: cargo test --workspace (1205 passed, 0 failed suites); pytest not-integration/
  e2e green; cargo clippy 0 warnings; cargo fmt clean; ruff clean;
  sequence_gap/subscription_fanout/log_record/architecture checks PASS.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (tools/codex_review.sh origin/main): APPROVE (round 5) — "No material
    ship-blocking findings; feature remains passes:false; runtime/dashboard/order-gate
    wiring scoped as deferred." (R1–R4 findings all addressed above.)

Resume / next (to flip SRS-MD-007 passes:true):
1. SRS-EXE-006 IB ingestion adapter / feed loop that calls observe_tick per delivered
   tick, populating MarketDataTick.tick_seq with IB's UPSTREAM provider sequence
   (producer contract — see sequence_gap_contract.tick_sequence_source) + a wall clock.
2. The orchestrator-layer MarketDataFreshnessProbe adapter reading freshness()/
   stale_since_ns(); requires making the port security-aware (carry asset_class/
   SecurityKey) on the SRS-MD-004/ERR-3 surface (deferred item).
3. SRS-LOG-001 durable sink for SEQUENCE_GAP + SRS-UI-001/API-001 dashboard HEARTBEAT
   surfacing ("visible on the dashboard").
4. SRS-MD-003 companion time-based heartbeat staleness (merges with gap staleness).
Then re-verify Step 3 end-to-end and flip via close_feature.py --verified.
