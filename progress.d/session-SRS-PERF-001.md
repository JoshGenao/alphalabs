=== SESSION SRS-PERF-001 ===
Date: 2026-07-02
Feature: SRS-PERF-001 — measure latency-sensitive perf metrics against a
PTP-disciplined clock (documented offset bounds) and report p50/p95/p99/p99.9 in
verification artifacts for NFR-P1/P4/P5/P6/P9/P10 + SRS-MD-001 fan-out latency.
Outcome: serialized (code integrated; passes stays false — real end-to-end
verification artifacts need the deferred runtimes + a PTP-disciplined host).

What I did:
- Built crates/atp-types/src/perf.rs — a zero-dep, I/O-free MEASUREMENT SUBSTRATE:
    * nearest-rank percentile engine (LatencyPercentiles: p50/p95/p99/p99.9 over ns
      samples; every reported value is an observed sample, no interpolation;
      fail-closed on empty; resolves_p999() exposes the 1000-sample tail floor).
    * PtpClockDiscipline (Disciplined{max_offset_ns} | Undisciplined) — the
      documented offset bound the SRS requires.
    * LatencyNfr / LATENCY_NFRS catalog binding the 7 AC NFRs to their SyRS/SRS
      measurement boundary + PER-LEG budget(s). Multi-leg NFRs: NFR-P4 = live+paper
      (both p95), NFR-P10 = order_latency (p95) + dashboard_refresh (flat <=5000ms,
      NFR-P2). stated_percentile is a per-LEG field on LatencyThreshold so a flat-max
      leg is never evaluated as p95. Reuses LIVE/PAPER_CALLBACK_LATENCY_P95_MS +
      STRATEGY_STARTUP_DEADLINE_MS; defines ORDER_SIGNAL_TO_ACK/HEARTBEAT_STALENESS/
      CONNECTIVITY_NOTIFICATION/DASHBOARD_REFRESH/SUBSCRIPTION_FANOUT constants.
    * LatencyVerificationArtifact::from_samples — binds samples to a specific leg;
      FAILS CLOSED on non-PTP clock (so the artifact always documents a max clock
      offset), unknown leg, empty/inverted window, i64 window-duration overflow, and
      no samples. Display renders leg + budget + offset + window + all 4 percentiles.
    * NfrVerification::assemble — a multi-leg NFR is verified only when EVERY leg is
      present once; NFR-P10 additionally requires the legs measured SIMULTANEOUSLY
      (overlapping windows); NFR-P4 does NOT (live vs paper are distinct systems).
- Added perf_measurement_contract to architecture/runtime_services.json (spliced
  textually, additively — 0 deletions) + tools/perf_measurement_check.py, which
  PARSES the spec docs and asserts each NFR budget + boundary phrase matches its
  measurement condition: NFR-P1/P4/P5/P6/P9/P10 vs the SyRS §5.1 table
  (docs/SyRS_v0.7.md, `<`/`≤` ms), SRS-MD-001 fan-out vs the SRS requirement row
  (docs/SRS.md, prose "no more than 100 ms"). Also pins Rust↔metadata parity,
  per-leg stated_percentile (via each static array), the fail-closed guards, the
  multi-leg completeness + simultaneity model, offset documentation, and vendor
  isolation. Exercised by tests/test_perf_measurement_contract.py (shell-out + 42
  in-process negative spot-checks) — so BOTH CI paths run the tool via the standard
  pytest gate (mirrors the sequence_gap pattern; NOT added to the generic check
  loops → no ci.yml/run_ci_locally.sh drift).

Key decisions / Codex rounds (all resolved):
- R1 [high]: SRS-MD-001 fan-out modeled as budgetless — but docs/SRS.md carries a
  100 ms fan-out budget (in the SRS requirement row, not the SyRS §5.1 table). FIX:
  100ms (<=) budget + a budget_doc="srs"/match_mode="prose" check against SRS.md.
- R2 [medium]: window_duration_ns() did unchecked i64 subtraction (i64::MIN..MAX
  overflow). FIX: checked_sub at construction + MeasurementWindowOverflow variant +
  stored duration.
- R3 [high]: NFR-P10 omitted the NFR-P2 dashboard-refresh leg (5000ms). FIX: two
  legs (order_latency + dashboard_refresh), match_mode "exact" vs the SyRS row.
- R4 [high]: artifact bound one percentile series to a whole NFR → multi-leg NFRs
  under-specified. FIX: bind each artifact to a threshold LEG + NfrVerification
  completeness bundle (every leg required).
- R5 [high]: NfrVerification didn't enforce NFR-P10's simultaneity. FIX:
  requires_simultaneous_legs() + window-overlap check in assemble (P10 only).
- (post-R5) [high]: NFR-wide stated_percentile mis-evaluated P10's dashboard leg as
  p95. FIX: moved stated_percentile onto per-leg LatencyThreshold; removed the
  NFR-wide method.
- R6: Codex hit its usage limit → per the documented fallback, ran an INDEPENDENT
  fresh-context adversarial reviewer (general-purpose sub-agent, prompts/
  critic_prompt.md criteria) on the final state → APPROVE, no findings.

What I tested (per step):
- Step 1: PASS — ./init.sh → "✓ Environment ready".
- Step 2: PASS — exercised the substrate over generated + fixture samples via
  `cargo test -p atp-types` (22 perf tests: nearest-rank invariants over a 500-case
  seeded-LCG sweep; artifact fail-closed paths; multi-leg completeness + NFR-P10
  simultaneity; per-leg percentile semantics) and `python3 tools/perf_measurement_
  check.py` (PASS — SyRS/SRS budget + boundary parity). There is no standalone CLI:
  SRS-PERF-001 is a measurement SUBSTRATE consumed by the deferred NFR runtimes.
- Step 3: PARTIAL — the harness reports p50/p95/p99/p99.9 for all 7 named metrics,
  documents the max clock offset, and its measurement boundaries are asserted to
  match the SyRS/SRS conditions (BUILT + TESTED). The REAL verification artifacts
  (actual latency samples measured against a PTP-disciplined host under the NFR-SC1
  baseline) are DEFERRED to the runtimes each NFR measures + PTP hardware.
- Step 4: DONE — evidence recorded; passes stays false (serialized).
- Full gate: cargo test --workspace (all suites pass, 0 failed); cargo clippy
  --workspace -D warnings clean; cargo fmt --check clean; ruff clean; pytest -m "not
  integration and not e2e" green (2716+ passed); architecture_check OK.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment: Codex R1–R5 findings all fixed; R6 blocked by Codex usage limit →
    independent fresh-context adversarial reviewer APPROVE (no findings).

Resume / next (to flip SRS-PERF-001 passes:true):
  Wire the deferred runtimes to feed REAL latency samples + a PTP clock offset into
  LatencyVerificationArtifact::from_samples per NFR leg and assemble NfrVerification
  bundles, measured on a PTP-disciplined host under the NFR-SC1 baseline:
    * NFR-P1 / NFR-P10 order latency — SRS-EXE-001 live IB order path.
    * NFR-P4 live/paper callback — SRS-SDK-004 / SRS-SIM-001 dispatchers.
    * NFR-P5 heartbeat staleness — SRS-MD-003.
    * NFR-P6 email+SMS notification — SRS-NOTIF-001.
    * NFR-P9 container startup — SRS-ORCH-001.
    * NFR-P10 dashboard refresh (NFR-P2) — the dashboard (SRS-UI-001 / SRS-API-001).
    * SRS-MD-001 fan-out — the consolidated subscription runtime.
  Then produce the verification artifacts end-to-end and flip via close_feature.py
  --verified. (SRS-PERF-001 has been recorded blocked-on these owners so `claim`
  does not re-offer it by id while they are unbuilt.)
