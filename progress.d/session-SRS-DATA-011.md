=== SESSION SRS-DATA-011 ===
Date: 2026-07-05
Feature: SRS-DATA-011 — adjust historical price data for corporate actions (SyRS SYS-28a / StRS SN-1.14)
Outcome: complete (flip candidate passes:true) — all six action types reflected; pending integrate.

CONTEXT: Prior sessions built 2/6 action types (splits/reverse-splits: crate-internal math in
normalization.rs + the SRS-DATA-011 coverage gate in coverage.rs + data011_coverage_cli + an 8-check
coverage_manifest_check.py) and left dividends/delistings/mergers/symbol-changes unbuilt. DATA-009
(cold-read) and DATA-012 (modes) are blocked on this feature. SYS-29 defines "fully adjusted" =
splits AND dividends, so dividends are "reflected" via a fully-adjusted read; position/order
remapping is SYS-28b/c + SYS-88 (execution/simulation consume this data layer's facts).

WHAT I DID (this session — the close of the remaining 4/6):
- store.rs: four v4 corporate-action FACT kinds — CorporateActionDividend (tag 6, one positive
  amount_minor, event_ts = ex-date), CorporateActionDelisting (tag 7, self-describing
  last_trading_ts == event_ts), CorporateActionMerger (tag 8, resolution "merger:<SUCCESSOR>",
  validated terms: cash >= 0, den > 0, num >= 0, non-zero consideration), CorporateActionSymbolChange
  (tag 9, resolution "symbol-change:<SUCCESSOR>", effective_ts == event_ts). SCHEMA_VERSION 3->4
  (MIN_SUPPORTED stays 1; serialize() still writes the MINIMUM version, so a coverage-only store
  stays v3; the forward-compat guard rejects v1-v3 blobs carrying tags 6-9). Successor symbols ride
  in the resolution label (a MarketField value is i64-only; the "fundamental:income" subtype idiom)
  with validate_record enforcing non-empty successor != own symbol (blocks the trivial self-cycle)
  at upsert AND restore. All four are PROVIDER kinds (provider_ingestion_kinds 5->9; deterministic
  fixture_batch arms); CorporateActionCoverage stays the ONLY refused trust kind at every layer.
- normalization.rs (stays crate-private): DividendEvent {symbol, ex_ts, amount_minor,
  prev_close_minor, prev_close_ts} + fully_adjust_record(s) — factor (prev_close - amount)/prev_close
  per dividend with strict ex_ts > t, composed i128-exactly with split factors (one round-half-even
  division per field); volume NEVER dividend-scaled; fail-closed on invalid terms (amount <= 0,
  >= prev_close, bad reference), MissingReferenceClose (no prior close — never a silent factor of 1),
  BasisCrossingDividend (a split effective between a reference close and its ex-date = mismatched
  share bases), overflow. split_adjust_record left byte-identical. Second seeded 5000-case property
  test (property_fully_adjusted_invariants) alongside the split one.
- coverage.rs: ONE private core (query_adjusted: AdjustmentMode SplitOnly|Full x AdjustmentBasis
  Frontier|AsOfEnd) behind FOUR public gated reads — query_split_adjusted[_as_of] (unchanged
  behavior + lineage/events) and NEW query_fully_adjusted[_as_of]. Same gate (frontier D >=
  query.end_ts else NotCovered); applied events bounded to `key.event_ts <= adjusted_through` (D for
  frontier reads, end_ts for _as_of — no dividend/split/rename lookahead; anchor renamed from
  `<= coverage_through`, check + mutation tests updated in lockstep). Symbol-change LINEAGE: querying
  the current symbol returns predecessor bars relabeled, adjustments composed across the hop; the
  QUERIED symbol's frontier governs the whole lineage (documented trust decision); fail-closed
  LineageCycle (visited set + MAX_LINEAGE_DEPTH 32) / AmbiguousLineage (dual predecessors,
  multi-rename predecessor, out-of-order hops, bar outside its validity window). Dividend reference
  closes resolve from the RAW (not adjusted, not window-clipped) lineage series. STRUCTURAL EVENTS:
  SplitAdjustedResult.events = in-window Vec<CorporateActionEvent> (Delisting / Merger with
  numerator/denominator/cash_per_share_minor / SymbolChange) — the facts a P&L consumer needs (mark
  final / convert at terms / follow the hop). Merger does NOT splice the acquired series.
- data007_query_cli: --normalization fully-adjusted served (routes query_fully_adjusted);
  total-return still fails closed naming SRS-DATA-012; adjusted output adds adjusted_through: +
  event_count: + event.<i>.* lines (never record.-prefixed — existing parsers verified tolerant).
  Tiered cold-read guard unchanged (adjusted modes stay single-tier; SRS-DATA-009 follow-up).
  data016_ingest_cli: USAGE + unknown-kind strings only; cmd_ingest byte-identical (sibling
  contracts anchor on it).
- python/atp_strategy/store_history.py: FULLY_ADJUSTED added to the served label map; the
  coverage_through gate-integrity check now covers both adjusted labels; TOTAL_RETURN stays
  NotImplementedError; SPLIT_ADJUSTED map line + Protocol default kept byte-identical (anchors).
- tools/coverage_manifest_check.py: 8 -> 12 static checks (+corporate_action_kinds,
  +gate_applies_dividends, +terminal_events_surfaced, +lineage_bounded); cli_routes_gated +
  gate_condition updated; cargo round-trip extended (fully-adjusted 2475/400000 with dividend@150;
  uncovered fails closed for BOTH adjusted modes; a rename-lineage store relabels + surfaces the
  symbol-change event; a delisting+merger store surfaces both events with exact terms).
- architecture/runtime_services.json (SURGICAL block splice — no whole-file reformat):
  coverage_manifest_contract passes:true, schema_version 4, supported_action_types = all six,
  deferred_action_types [], round_trip extended, deferred -> named owners (TR+selection
  SRS-DATA-012; real provider ingestion SRS-DATA-001/003/006; position/order remapping SYS-28b/c +
  SYS-88; adjusted-over-tier SRS-DATA-009). normalization_modes_contract: FULLY_ADJUSTED moves into
  public/binding/core mode lists; deferred_public_modes [TOTAL_RETURN] (block passes stays false —
  DATA-012 not closed). Stale-prose sweep across unified_query_runtime_contract /
  store_history_binding_contract descriptions + tools/normalization_modes_check.py /
  store_history_check.py / unified_query_check.py / architecture_check.py summaries.
- Tests: store.rs +3 (v4 versioning, per-kind forged-record rejection at new+restore,
  successor_symbol); normalization.rs +10 incl. the second property test; coverage.rs +17 (dividend
  frontier/as-of lookahead pins, raw-series reference close, missing-reference + basis-crossing
  fail-closed, lineage relabel/compose/gate/cycle/ambiguity/validity, event surfacing + windowing +
  empty-window); tests/test_coverage_manifest_contract.py 18->30 (4 new mutation classes; count 12;
  passes True; schema anchor = 4); tests/domain/test_coverage_gate_domain.py +fully-adjusted legs +
  lineage e2e + registered-closed structural; tests/boundary/test_coverage_gate.py +fully-adjusted
  envelope + raw-carries-no-new-lines; tests/boundary/test_store_history_binding.py FULLY_ADJUSTED
  served + gate-integrity + TOTAL_RETURN-only refusal; tests/domain/test_store_history_consumer.py
  uncovered FULLY_ADJUSTED -> CoverageNotProvenError; tests/test_normalization_modes_contract.py
  mode lists + 2 revert-catch mutations; tests/integration/test_data012_split_adjusted.py updated;
  NEW tests/integration/test_data011_corporate_actions_e2e.py — the Step-3 scenario demonstration:
  per action type, CLI-driven fresh store computing buy-before/evaluate-after P&L across the action
  date with exact integers (split 2500/+7500; dividend 2475 vs 2500 = the 25-minor dividend leg;
  delisting mark-final + event; merger 10 MSFT -> 5 AAPL + 5000 cash, P&L -46000 exact; symbol
  change relabeled continuity + coverage-keyed-to-queried-symbol). Reverse splits share the forward
  code path (1-for-N SplitEvent), pinned by crate unit + property tests.

WHAT I TESTED (per AC step):
  Step 1 (init): ./init.sh -> "✓ Environment ready".
  Step 2 (exercise via CLI/fixtures): manual walk — data016 ingest daily-equity-bar@100 --init +
    corporate-action-dividend@150 + corporate-action-split@200; data011 assert-coverage AAPL 200 ->
    frontier:200; data007 query fully-adjusted [0,100] -> exit 0, normalization:fully-adjusted,
    coverage_through:200, adjusted_through:200, event_count:0, close:2475, volume:400000; query end
    250 -> exit 1 naming SRS-DATA-011 have 200 / need 250; total-return -> exit 1 naming SRS-DATA-012.
  Step 3 (acceptance criteria): ATP_RUN_INTEGRATION=1 pytest tests/integration/
    test_data011_corporate_actions_e2e.py + test_data011_coverage_gate.py +
    test_data012_split_adjusted.py -> 10 passed (each of the six action types reflected with exact
    P&L integers under the selected mode; see the per-scenario docstrings).
  Step 4 (objective evidence): cargo test --workspace -> 99 suites ok / 0 failed (atp-data lib 133
    incl. 2x5000-case property tests); pytest -m "not integration and not e2e" -> 2822 passed;
    python3 tools/coverage_manifest_check.py --require-cargo -> PASS (12 static + extended
    round-trip); normalization_modes_check.py --require-cargo -> PASS; .venv store_history_check.py
    --require-cargo -> PASS; .venv architecture_check.py -> PASS; tools/run_ci_locally.sh -> PASS.
Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment: Codex hit its usage limit (codex_review.sh -> "hit your usage limit ... try again Jul 6";
  NOT treated as a verdict). Ran the sanctioned failover from origin/main
  (tools/adversarial_review.py, PR#5): its claude-fallback returned approve but with an EMPTY
  summary/findings — the exact verdict shape memory says to distrust — so I re-ran a FULL
  fresh-context adversarial sub-agent review (prompts/critic_prompt.md + independence system prompt,
  read-only, 36 tool calls, re-ran the whole evidence chain itself): APPROVE with an auditable
  attack summary and 3 LOW findings, none fail-open/lookahead/invariant: (1) two dividends sharing
  one reference bar compose per-factor instead of jointly — pathological for daily sessions,
  documented convention; (2) validity-window enforcement is asymmetric when querying a renamed-away
  predecessor directly (rename still surfaced as an event; no invariant broken); (3) a
  provider-ingested FACT with event_ts <= an existing frontier is served rather than flagged as
  contradicting the operator's assertion — same posture as the pre-existing split design, documented;
  hardening suggestion recorded for the DATA-001/003/006 provider-ingestion close.
Resume / next: integrate --mode complete (flip passes:true). Unblocked follow-ups: SRS-DATA-012
  (total-return + per-subscription selection — small close now), SRS-DATA-009 re-surfaces
  (TieredReader adjusted-over-tier + StoreBarSource/store_inputs cold-read routing). Real provider
  corporate-action ingestion stays with SRS-DATA-001/003/006 (operator-asserted coverage frontier
  stands in); position/order remapping consumes the surfaced events (SYS-28b/c EXE + SYS-88 SIM).
