=== SESSION SRS-UI-004 ===
Date: 2026-07-16
Feature: SRS-UI-004 — display backtest result history and details in the dashboard
(SRS-5.8, P1, method=Demonstration; SyRS SYS-42/SYS-43a)
Outcome: complete

What I did:
- The panel itself was already built by the UI-3 session (2026-07-07, serialized): real
  history over bt009_store_cli + drill-down (equity SVG, trade log, benchmark tiles).
  This session captured the missing browser Demonstration evidence and closed the gaps.
- NEW AC-named e2e (tests/e2e/test_dashboard_refresh.py::
  test_srs_ui_004_backtest_history_lists_ac_fields_and_drills_down): asserts every AC
  clause against a REAL store seeded via bt009_store_cli persist --init — history row
  lists strategy ("momentum"), parameters (lookback=20, threshold=0.5), date range
  (run_window 0–100), metrics (finite Sharpe; pct cells never "—"); drill-down opens
  trade log (2 fills, money columns), equity curve (non-empty SVG path, "5 marks"
  readout), benchmark comparison ("Excess vs SPY" + "Beta vs benchmark" real values).
- FIXED a real rendering bug the screenshot pass exposed: .backtest__detail's
  display:grid overrode the UA [hidden]{display:none}, so an empty bordered drill-down
  box always rendered before a row was selected. One scoped line
  (.backtest__detail[hidden]{display:none}) + a regression pin in the e2e.
  Screenshots pre/post confirm. /frontend-design reviewed: panel meets the bar
  (hover/selected states, gradient equity chart, sign-aware tiles all pre-existing).
- SEPARATE fix commit (c316c76) repairing the red main pytest baseline left by the
  SRS-EXE-006 landing: five literal "10.45" TWS-version anchors in
  tests/test_adapter_contract.py + tests/test_architecture.py synced to the
  golden-pinned 10.19.4; two ruff I001 import sorts (tests/domain/
  test_strategy_container_least_privilege.py, tools/architecture_check.py).
  Without this, pytest was 5-failed RED on origin/main for every sibling.

What I tested (per step):
  Step 1 (init): PASS — ./init.sh → "✓ Environment ready"; dev deps installed manually
    into .venv (init.sh skips requirements-dev.txt); chromium via shared playwright cache.
  Step 2 (exercise, browser + REST): PASS — ATP_RUN_E2E=1 pytest
    tests/e2e/test_dashboard_refresh.py -k backtest → 2 passed (existing UI-3 e2e + new
    AC-named e2e; real store, real provider subprocess, ephemeral loopback port, headless
    chromium). Operator explicitly authorized running the gated e2e with 3 siblings
    leased (binds no shared resource; no load-sensitive NFR assertion). REST leg:
    tests/boundary/test_dashboard_backtests_wiring.py + unit + domain suites → 36 passed.
  Step 3 (AC): PASS — every AC artifact asserted clause-by-clause in the new e2e (see
    above); panel screenshots captured (history + drill-down) as visual evidence.
  Step 4 (evidence → flip): PASS — evidence proves the requirement end to end;
    integrated --mode complete (passes:true via close_feature --verified).
  Gate: full pytest -m "not integration and not e2e" 3611 passed; cargo test
    --workspace green; cargo fmt --check + clippy -D warnings clean; ruff check green.
    KNOWN PRE-EXISTING main baselines (NOT this feature, untouched): ruff format
    --check would reformat 13 files (toolchain-pin drift, owner = the pins PR);
    mypy python/ 66 errors in 16 files (this diff adds no python/ source).

Critic verdicts:
  deterministic: APPROVE — no findings (both commits).
  judgment (adversarial_review.py, reviewer=codex): 3 rounds.
    R1/R2 BLOCK: sole finding = atomicity (baseline repair bundled with the feature
    diff) — resolved by splitting into fix(ci) c316c76 + feat 5db766c; codex raised
    no substantive finding on either change's content in any round.
    R3 (base=c316c76, feature-only diff): APPROVE — no findings.

Resume / next: nothing — SRS-UI-004 is complete. The launch/controls leg is UI-3 /
SRS-BT-001 / SRS-API-001 scope (still blocked, unchanged). UI-3's own flip still needs
the operator (its AC includes initiating backtests; handler 501 owner SRS-API-001).
