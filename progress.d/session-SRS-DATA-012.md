=== SESSION SRS-DATA-012 ===
Date: 2026-07-09
Feature: SRS-DATA-012 — support raw, split-adjusted, fully-adjusted, and total-return normalization
modes per security subscription (SyRS SYS-29 / StRS SN-1.15).
Outcome: serialized — the HISTORICAL total-return mode + the four-mode per-query selector are built
and solo-verified; the LIVE per-subscription selection stays deferred (passes:false).

CONTEXT: DATA-011 (CLOSED green) already shipped raw + split-adjusted + fully-adjusted through the
coverage gate (four gated reads over AdjustmentMode {SplitOnly,Full} × AdjustmentBasis
{Frontier,AsOfEnd}) and explicitly deferred total-return + per-subscription selection to DATA-012 as
a "small close." Only dep is SRS-DATA-011 (passes:true). Closing DATA-012 unblocks SRS-DATA-009.

WHAT I DID:
- normalization.rs (crate-internal math): added total_return_record(s) — a sibling of
  fully_adjust_record with the SAME split leg (byte-stable) but the dividend leg INVERTED: it
  reinvests forward (ex_ts <= t, factor prev_close/(prev_close-amount) >= 1) instead of
  back-adjusting (ex_ts > t). volume takes the split factor only. Same i128 / round-half-even /
  overflow / fail-closed discipline (InvalidDividendTerm, BasisCrossingDividend, Overflow,
  UnsupportedKind) — no new error variants. total-return and fully-adjusted are DISTINCT series
  (differ by the constant total-reinvestment factor); continuous across the ex-date (TR(ex-1)==TR(ex)).
  +6 fixed-example unit tests + a third seeded 5000-case property test
  (property_total_return_invariants: no-dividend==split-only, identity, symbol isolation, volume,
  exact composed-rational reinvestment pricing, order-independence, fail-closed).
- coverage.rs (the gate): AdjustmentMode::TotalReturn + two public reads query_total_return[_as_of]
  (reuse the SAME coverage gate, lineage, event surfacing, dividend-event resolution as Full). The
  reinvest leg is basis-invariant by construction (no dividend lookahead: ex_ts <= t <= end_ts),
  the split leg still respects frontier-vs-as-of. +3 gate tests (covered TR distinct from FA;
  uncovered fails closed identically; as-of caps splits while the reinvest leg is basis-invariant).
- data007_query_cli.rs: Normalization::TotalReturn; --normalization total-return routes
  query_total_return (was: parse-reject naming SRS-DATA-012). USAGE + doc updated.
- python/atp_strategy/store_history.py: TOTAL_RETURN added to _NORMALIZATION_LABEL +
  _GATED_NORMALIZATION_LABELS (gate-integrity applies); the defensive not-in-map raise stays (guards
  option-chain / unmapped). All four HISTORICAL modes now served per query.
- Contract lockstep (runtime_services.json): normalization_modes_contract — TOTAL_RETURN moved into
  public/binding/core mode lists, deferred_public_modes -> [], mode_exposure_note + description +
  rust_entry_points + deferred[] rescoped to the LIVE leg only; passes STAYS false.
  store_history_binding_contract.forbidden_normalization_modes -> [] (inert drift) + deferred item
  rescoped; coverage_manifest_contract description reconciled (DATA-011 owner).
- Check lockstep: normalization_modes_check.py (CLI + binding assert SERVE total-return; cargo
  round-trip fails-closed over uncovered for split AND total-return; _DEFERRED_OWNERS fixed),
  coverage_manifest_check.py (the DATA-011 landmine 317-320: reject->serve; deferral text),
  architecture_check.py (drift summaries). test_normalization_modes_contract.py: four mode arrays
  updated + 2 new mutation tests (revert-total-return-to-reject / drop-binding-total-return caught).
- Test lockstep: boundary/test_store_history_binding.py (TR raises -> TR served through gate),
  domain/test_store_history_consumer.py (TR in the gated fail-closed loop),
  domain/test_coverage_gate_domain.py (TR added to the covered/uncovered/past-frontier legs — the
  PAIRED tests/domain diff for the safety-critical coverage.rs change),
  integration/test_data012_split_adjusted.py (TR in the uncovered fail-closed loops),
  integration/test_data011_corporate_actions_e2e.py (+ NEW covered TR reinvestment P&L scenario:
  buy@100 hold across a $1 dividend ex@150 to @300; TR series [10000,10101], raw flat, FA [9900,10000]).

WHAT I TESTED (per AC step):
  Step 1 (init): ./init.sh -> "✓ Environment ready".
  Step 2 (exercise via CLI/fixtures): manual walk — data016 ingest daily-equity-bar@100 + @300 +
    corporate-action-dividend@150; data011 assert-coverage AAPL 300; data007 query [0,300] ->
    raw [10000,10000], split-adjusted [10000,10000] (no split), fully-adjusted [9900,10000],
    total-return [10000,10101] (10000*10000/9900 round-half-even). options->adjusted fails closed
    (equity-kind guard); covered TR exit 0; uncovered TR exit 1 naming SRS-DATA-011; unknown mode
    exit 1. All four modes selectable per query.
  Step 3 (acceptance criteria): the coverage-gate DOMAIN test (cargo, no integration marker) exercises
    total-return served (2500) behind coverage and fail-closed (uncovered / past-frontier) end-to-end
    over the real CLIs; the reinvestment-P&L INTEGRATION test is written (serialized-verification step).
    options-request-raw holds structurally (adjusted reads reject non-equity kinds); indicators-request-
    adjusted holds (all adjusted modes selectable). The LIVE-subscription selection is DEFERRED.
  Step 4 (objective evidence): cargo test --workspace -> 0 failed (atp-data lib 157, incl. the 3
    5000-case property tests + the lineage-window + future-dividend regressions); pytest -m "not
    integration and not e2e" -> 3030 passed / 3 pre-existing skips; cargo fmt --check + cargo clippy
    --workspace -D warnings -> clean (hand-formatted new lines, no whole-crate fmt);
    normalization_modes_check.py / coverage_manifest_check.py / store_history_check.py /
    architecture_check.py / unified_query_check.py --require-cargo -> PASS.
    run_ci_locally.sh halts only at the PRE-EXISTING main mypy failure (67 errors in files I did not
    touch; store_history.py is mypy-clean; integrate skips mypy).

Critic verdicts:
  deterministic (critic_check.py --staged): WARN — 2 findings, money:float-arithmetic on
    test_data011_corporate_actions_e2e.py close-field subtraction. OVERRIDE: false positive — these are
    INTEGER minor-unit values (_parse does int(value)); integer subtraction is exact, the same
    money-safe pattern the sibling dividend test uses. No float, no rounding. (Subsequent fix commits
    all APPROVE deterministically.)
  judgment (adversarial_review.py, reviewer=codex): APPROVE after 5 block-rounds, all resolved:
    R1 [high]: total-return frontier reads validated FUTURE dividends in (end_ts, D] that no bar can
      reinvest -> could wrongly fail a read. FIX: resolve TR dividends only through query.end_ts
      (no-lookahead at the resolution boundary). +2 regression tests.
    R2 [high, honesty]: contract over-claimed 'only LIVE deferred' while the binding rejects all
      OPTION access. FIX: scoped option-chain binding access as a deferred passes:false contributor.
    R3 [high]: repo-wide doc-drift — 'four reads'/'TWO property tests'/'total-return deferred' across
      coverage_manifest + normalization_modes checks + Rust core docstrings. FIX: swept to six reads /
      three property tests / total-return served; PASS line renamed 'SRS-DATA-012 NORMALIZATION MODES'.
    R4 [high]: unified_query_runtime_contract + unified_query_check still listed TOTAL_RETURN deferred.
      FIX: rescoped to LIVE + option-chain. Verified every TOTAL_RETURN ref across all four contract
      blocks classifies clean.
    R5 [high x2]: (a) REAL fail-open — lineage_split_events/lineage_dividend_events didn't bound
      corporate actions to their symbol's rename validity window (unlike lineage_raw_series for bars),
      so an out-of-window action could be retagged + silently mis-adjust. FIX: shared
      check_action_in_segment_window fails closed (AmbiguousLineage) + a crate gate test + an e2e
      domain test. (b) [honesty] reworded 'options can request raw' as PARTIALLY served (mode half done
      at the CLI; strategy-facing option DATA deferred, owner SRS-DATA-006) -- not claimed closed.
    R6: APPROVE — "no material ship-blocking findings; remaining scope gaps kept as passes:false/
      deferred rather than claimed complete."

COMPLETENESS: serialized. passes STAYS false for TWO deferred scopes (NOT any normalization mode):
(1) the LIVE subscription mode selection — the Market Data Subscription Manager (atp-market-data) is
unbuilt (SubscriptionRequest/MarketDataSubscription carry no normalization field,
subscribe_market_data is a NotConfigured stub, every SRS-MD-* + SRS-EXE-006 is passes:false; owner
SRS-MD-001); and (2) option-chain bar ACCESS through the strategy binding — the equity binding serves
OHLCV bars only and rejects OPTION (the Bar shape doesn't fit option chains; owner SRS-DATA-006). The
'options can request raw' AC clause is PARTIALLY served: the raw MODE is selectable and the operator
CLI serves raw option-chain (adjusted refused on non-equity), but the strategy-facing option DATA path
is deferred. Every HISTORICAL normalization mode (raw/split/fully/total-return) is built + solo-verified.

Resume / next: the operator finishes the LIVE-leg verification (manually or via verified-e2e) once the
Market Data Subscription Manager exists. Deferral owner: SRS-MD-001 (-> SRS-EXE-006, SRS-PERF-001).
DATA-012's only formal dep stays SRS-DATA-011 (no block edge added). SRS-DATA-009 is now unblockable.
Don't rebuild the total-return math/gate/CLI/binding — wire the LIVE selector onto the subscription
manager when it lands.
