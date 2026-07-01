=== SESSION SRS-DATA-007 ===
Date: 2026-06-30
Feature: SRS-DATA-007 — provide a unified historical data access interface (docs/SRS.md line 177;
SyRS SYS-27 / SYS-53; StRS SN-1.28 / SN-3.03 / BG-5). Outcome: COMPLETE (passes:true) — the GREEN CLOSE
of the DATA read cluster's keystone. 83 -> 82 remaining.

DISPOSITION (why this closes now, after S70–S76 held it):
- The AC: "Strategy code, backtests, factor jobs, and notebooks query by symbol, date range, and
  resolution WITHOUT specifying the original source provider." SRS verification method = **Contract test**.
- All four named consumers were already wired + tested over prior sessions: strategy + notebook ->
  StoreBackedHistoricalData (S70/S73); backtest -> atp-simulation StoreBarSource in BacktestEngine::run
  (S74); factor jobs -> atp-factor-pipeline store_inputs loaders + run_scheduled_factor_job_over_store
  (S75). Every CONCRETE Codex blocker from S70–S75 (normalization honesty, missing coverage gate, single
  stand-in consumer, bounded-read bug, factor exec-path, forgeable as-of) was fixed in the subsequent
  sessions. The ONLY remaining "blocker" in the metadata was "the Jupyter notebook HOST (SRS-RES-002)".
- KEY SCOPE FINDING: the SRS SEPARATES the two. SRS-DATA-007 (line 177) is the unified *interface*,
  verified by a CONTRACT TEST. SRS-RES-002 (line 209) is *Jupyter access* (the notebook HOST: kernel /
  indicators / plotting / no-live-order isolation), verified by "Test, demonstration". A notebook
  *querying* the interface is plain Python importing the same binding every consumer uses — that DATA
  ACCESS is wired + tested. The Jupyter HOST that *runs* the notebook is RES-002's separate concern.
  Prior sessions conflated the two; this session treats the SRS split as authoritative.
- Operator (AskUserQuestion) chose "Close as complete" given the SRS scope split + all-4-consumers-wired
  evidence. No production logic changed (DATA-007 was already implemented); this session corrects the
  close framing and adds the contract-test verification artifact.

What I did:
- NEW tests/test_data007_unified_interface_close.py (L3 contract test, the SRS verification artifact):
  maps the AC's four named consumers to their concrete surfaces and asserts each reads the PROVIDER-NEUTRAL
  unified path (engine + binding via the 12+12 structural guards; backtest StoreBarSource impl BarSource
  calling query_unified/query_split_adjusted + BacktestDataSource::SystemData; factor store_inputs loaders
  calling query_unified/query_split_adjusted_as_of). Pins the close metadata as COMPLETE and pins the
  RES-002 scope boundary AGAINST THE SRS TEXT (line 177 "Contract test" vs line 209 Jupyter / "demonstration").
  + a cargo-gated behavioural capstone (ingest -> binding read -> source-neutral bars). 11 tests.
- Metadata sweep to a consistent COMPLETE story (no production logic): architecture/runtime_services.json
  (unified_query_runtime_contract + store_history_binding_contract -> COMPLETE; dropped the now-false
  "DATA-007 STAYS passes:false because consumers unwired" clause from coverage_manifest_contract [DATA-011]
  + normalization_modes_contract [DATA-012]; reframed sim_benchmark_contract [BT-005] deferred[0]/[1] from
  "SRS-DATA-007 ... which is not built" -> "interface now COMPLETE; wiring the real benchmark resolver is
  BT-005's work"). Mirrored in tools/{unified_query,store_history,architecture,coverage_manifest,
  normalization_modes}_check.py docstrings/_DEFERRED_OWNERS/summaries; tests/domain/test_store_history_
  consumer.py docstring; crates/atp-simulation/src/benchmark.rs doc-comment. Dropped "srs-data-007" from
  the required-deferred-owner loop in tests/test_coverage_manifest_contract.py (DATA-007 is no longer a
  deferred leg of DATA-011). DATA-011 / DATA-012 / BT-005 stay passes:false for their OWN named reasons.

What I tested (per step):
- Step 1: PASS — ./init.sh -> "✓ Environment ready".
- Step 2: PASS — data016_ingest_cli ingested daily + minute kinds; data007_query_cli query --symbol AAPL
  --resolution 1d --start 0 --end ... returned source-neutral key:value output (no provider/source/vendor/
  feed line); a `--provider ib` flag is REJECTED ("unknown flag '--provider'") — structurally cannot name a source.
- Step 3: PASS — all four named consumers query provider-neutrally: cargo srs_data_007_unified_query (7) +
  store_bar_source (7) + store_market_inputs (7) + srs_fac_001_store_backed_job (14, 1 ignored perf) green;
  pytest test_unified_query_contract + test_store_history_contract + domain/test_store_history_consumer +
  the new test_data007_unified_interface_close (11) green.
- Step 4: PASS — contract evidence: tools/unified_query_check.py + store_history_check.py + architecture_check.py
  PASS; the new L3 contract test is the consolidated four-consumer artifact. cargo test --workspace green (84
  suites, 0 failures); pytest -m "not integration and not e2e" green (only the pre-existing bbands talib flake).

Critic verdicts:
  deterministic: APPROVE — 0 findings (re-run after every edit).
  judgment (tools/codex_review.sh): 7 rounds. R1-R6 findings ALL fixed; converged to a SINGLE residual
  needs-attention that is an OPERATOR-AUTHORIZED SCOPE OVERRIDE (NOT a faked APPROVE) — see below. Each
  round surfaced the next-deeper concern (the non-convergence pattern; a metadata-heavy close):
    R1 [high] prose asserted "(passes:true)" while feature_list.json (source of truth) still reads
      passes:false on the branch -> reframed all 8 flag-claims to "is COMPLETE and closes to passes:true AT
      INTEGRATION (close_feature.py --verified flips it under the lock; this branch does NOT edit
      feature_list.json)" + a reconciliation-invariant test. The flip-at-integrate IS the workflow (Step 7.5).
    R2 [high] file-level sweep miss: the SAME long store_history/normalization/coverage descriptions had
      OTHER stale sentences ("NOT complete ... not yet WIRED ... only a strategy stand-in ... flip reverted")
      -> fixed all 3 + extended the contract test to reject the full stale-phrase set. LESSON: sweep the
      WHOLE block text, not the first match.
    R3 [high] SUBSTANTIVE date-range: the public strategy HistoricalData Protocol exposes only
      get_bars(lookback,end), and metadata framed "promote get_bars_range to the Protocol" as "part of the
      DATA-007 close". OPERATOR (AskUserQuestion) chose "re-scope to SRS-SDK-001": the runtime_checkable
      Protocol is the strategy-AUTHORING surface (SDK-001's domain); the date-range AC is met (engine +
      backtest + factor + the binding's get_bars_range all take explicit [start,end]; get_bars(lookback,end)
      is a bounded date-window query). Re-scoped the 3 passages to SRS-SDK-001 + pinned it in the contract test.
    R4 [high] a stale "DATA-007 consumers are groundwork" deferral survived in the SIBLING SRS-DATA-016 block
      (not a DATA-007-owned one). OPERATOR chose "full sweep + flip": reframed ~45 "deferred SRS-DATA-007"
      shorthand references across sibling features (BT-005 benchmark, BT-009, FAC-001 factor) in JSON + 2
      check tools + 9 rust source/bin/test files (all comment/prose; the interface is complete, the real
      data/resolver/calendar is deferred to DATA-005 / FAC-001 / BT-005). Added a whole-surface guard test
      (scans JSON + all check tools + the swept rust). Grep-clean of all stale markers.
    R5 [high] one MORE contradictory sentence in the DATA-012 normalization block ("does NOT close
      SRS-DATA-007 or SRS-DATA-012 (both STAY passes:false)") -> reconciled (DATA-012 stays false; DATA-007
      is complete) + a proximity-regex guard for "SRS-DATA-007 ... STAYS passes:false".
    R6 [high]x2: (1) the R5 fix was UNSTAGED (working-tree only) while the stale sentence stayed committed
      -> re-committed correctly (verified committed tree grep-clean); (2) the date-range point again.
    R7: the metadata objections are ALL cleared; the SINGLE residual [high] is
      contract:strategy-date-range-missing — Codex holds the runtime_checkable HistoricalData Protocol MUST
      expose an explicit get_bars_range(start,end) or DATA-007 stays open.
  OVERRIDE (operator-authorized, honest — NOT a faked APPROVE): the operator (AskUserQuestion, twice: R3 +
  the final call) chose to close with Codex's date-range needs-attention RECORDED as a scope override. The
  AC's "query by date range" IS met for every named consumer (strategy get_bars(lookback,end) is a bounded
  date-window query; explicit [start,end] is on the engine / backtest StoreBarSource / factor store_inputs /
  the concrete StoreBackedHistoricalData.get_bars_range). Promoting get_bars_range onto the runtime_checkable
  Protocol is an SRS-SDK-001 strategy-AUTHORING-surface change (a module boundary DATA-007 should not cross),
  named + pinned in the contract test. This is the [[feedback_adversarial_loop_nonconvergence]] playbook:
  fix in-scope bugs, scope the rest honestly with a named owner + a pin, get human authorization, record the
  verdict honestly.

Known issues / notes for next agent:
- DATA-007 is CLOSED (passes:true). Do NOT re-add "notebook HOST is the remaining unwired DATA-007 consumer"
  — the Jupyter HOST is the SEPARATE SRS-RES-002 feature (line 209), distinct from DATA-007's contract-test
  interface (line 177). RES-002 (Jupyter host: kernel/plotting/no-live-order isolation) is still READY/unbuilt.
- SRS-DATA-011 (dividend/delisting/merger/symbol-change math), SRS-DATA-012 (FULLY_ADJUSTED/TOTAL_RETURN +
  live-subscription), SRS-FAC-001 (NFR-P7 wall-clock perf + concrete-calendar mapping), and SRS-BT-005
  (web-dashboard benchmark rendering + the real benchmark resolver wiring) all remain passes:false for their
  OWN named reasons — DATA-007 closing does NOT auto-close them.
- SRS-SDK-001 FOLLOW-UP (the recorded Codex override): promote an explicit get_bars_range(symbol, *, start,
  end, frequency, ...) onto the runtime_checkable HistoricalData Protocol so a Protocol-TYPED ctx.history
  (not only the concrete StoreBackedHistoricalData) exposes explicit-[start,end] date-range access. It needs
  every implementer to add it — the warm-up / parity / harness stubs (tests/domain/test_strategy_api_parity,
  test_warmup_replay, python/atp_strategy/examples/_harness.py) — which is why S70 deferred it to SRS-SDK-001.
  DATA-007's date-range AC does NOT depend on it (see the OVERRIDE above); this is a strategy-authoring-surface
  ergonomic. tests/test_data007_unified_interface_close.py pins the SDK-001 attribution.
- Pre-existing (NOT mine): tests/property/test_indicators_property.py::test_bbands_property_matches_batch_talib
  flakes on a clean tree (talib/pandas-ta drift). The worktree .venv needed `pip install -r requirements-dev.txt`
  to get pytest (init.sh installs only requirements.txt). run_ci_locally.sh's `mypy python/` step reports 49
  PRE-EXISTING errors in python/atp_strategy/examples/{_harness,sma_crossover}.py (byte-identical to the fork
  point; I touched NO python/ files) — repo-wide debt, not this change. cmd_integrate does not run the CI mirror;
  my change's real gates (cargo --workspace, pytest not-integration/e2e, ruff on my files, deterministic critic,
  Codex) are all green/converged.
