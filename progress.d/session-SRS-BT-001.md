=== SESSION SRS-BT-001 ===
Date: 2026-06-29
Feature: SRS-BT-001 — backtest Python strategies against stored data and user-uploaded Parquet data over configurable date ranges.
Outcome: partial (blocked-on SRS-API-001, UI-3; PLUS two operator scope decisions: the Parquet crate and the Python strategy host — see Resume/next)

What I did:
Landed the operator **CLI launch surface** for the system-data + configurable-date-range
halves of the AC — the launch surface every sibling BT feature (bt002/bt003/bt009/bt010)
already ships, which SRS-BT-001 (the actual "launch a backtest" feature) conspicuously lacked.
NO new dependencies; NO throwaway code; the engine + StoreBarSource + DateRange substrate was
already on main.

- crates/atp-simulation/src/launch.rs (NEW pub module) — `parse_window(start, end)` binds an
  operator-supplied `YYYY-MM-DD` start/end window to the engine's inclusive epoch-second
  `DateRange`. The backtest module explicitly defers "binding [the opaque u64 axis] to wall-clock
  calendar dates [as] the launch surface's concern"; this is that binding, now real + tested.
  Integer-only, dependency-free (canonical proleptic-Gregorian days_from_civil / civil_from_days),
  fail-closed on malformed / impossible (2024-02-30) / pre-epoch / inverted windows. Reused by the
  deferred REST + dashboard launch surfaces. 10 unit tests.
- crates/atp-simulation/src/bin/bt001_backtest_cli.rs (NEW bin) — `run --start <D> --end <D>
  [--symbol S] [--source system] [--cash N]`. Launches the runnable BacktestEngine over the
  selected window via the **real** StoreBarSource (the SRS-DATA-007 system-data path) over a
  fixture MarketDataStore, restricts replay to the window, prints launched window (dates + epoch) +
  trade log + equity curve + final equity. Fail-closed everywhere: deferred uploaded-Parquet source,
  non-positive --cash, an unseeded --symbol (the fixture is seeded under a FIXED symbol so an absent
  symbol is NOT fabricated — the run returns EmptyData), unknown/duplicate/value-less flags, and
  malformed/inverted dates. 15 integration tests (srs_bt_001_cli, process round-trip).
- Updated crates/atp-simulation/src/backtest.rs module doc + architecture/runtime_services.json
  `backtest_contract` to distinguish the now-LANDED CLI launch surface from the still-deferred
  REST/dashboard + Parquet + Python pieces (Codex contract-drift fix).

What I tested (per step):
- Step 1: PASS — ./init.sh → "✓ Environment ready".
- Step 2: PASS — exercised SRS-BT-001 via the CLI/file workflow over fixture market data: full
  window 2024-01-02..09 → 6 bars; sub-window 2024-01-03..05 → 3 bars + different fill price (10250
  vs 10000) + different final equity (the date selection is load-bearing). Every fail-closed branch
  exits non-zero with no launch output.
- Step 3: PASS (for the buildable halves) — "launched with system data" + "start and end dates are
  selectable" demonstrated at the CLI. "uploaded Parquet data" + "through API and dashboard" remain
  deferred (Resume/next).
- Step 4: passes:false retained — evidence does not yet prove the full AC end to end.
- Gate: cargo test --workspace green (88 ok results, 0 failures); 25 new tests (10 launch unit + 15
  CLI integration); tools/run_ci_locally.sh green (cargo fmt --check, clippy --workspace -D warnings,
  critic APPROVE, all architecture/contract checks); tools/backtest_check.py "SRS-BT-001 SDK-SURFACE
  PASS".

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (tools/codex_review.sh origin/main): APPROVE on round 4.
    R1 [high] unknown/misspelled flag silently ignored → could launch the default system source while
       reporting success (wrong-source launch) → FIXED: single-pass allowlist parser rejecting
       unknown/duplicate/value-less flags + tests.
    R2 [high x2] (a) --cash accepted 0/negative starting cash; (b) --symbol fabricated a fixture
       catalog for any requested symbol → FIXED: reject cash<=0; seed the fixture under a fixed symbol
       so an unseeded symbol fails closed (EmptyData) + tests.
    R3 [medium] contract drift — CLI launch surface shipped while backtest.rs doc + runtime_services.json
       still marked the CLI launch surface deferred → FIXED: updated both docs.
    R4 APPROVE — no material findings.

Resume / next: SRS-BT-001 stays passes:false. The three NAMED AC surfaces still required to flip,
none of which a parallel coding session can build without an operator decision or another feature:
  1. "uploaded Parquet data" → the user-uploaded Apache Parquet BarSource. ** OPERATOR SCOPE DECISION **:
     needs the FIRST third-party Rust crate (arrow/parquet) in a currently zero-dep workspace. The
     CAPABILITY is in SyRS scope (SYS-14, AC-4, IF-5 all mandate Apache Parquet), but introducing the
     first external crate — and its correct home in the DATA LAYER behind the unified interface
     (AC-8 / SYS-27), not atp-simulation — is an architecture call for the operator. The CLI's
     `--source uploaded` already fails closed naming this. Do NOT fake it with a non-Parquet format.
  2. "backtest Python strategies" → the Rust<->Python strategy host (PyO3 or subprocess strategy host
     under the orchestrator). ** OPERATOR SCOPE DECISION ** (new dep, SyRS-gated). The strategy here is
     a fixture BacktestStrategy. The fallible on_bar port (StrategyFailed) is the forward seam.
  3. "selectable through API and dashboard" → SRS-API-001 (live REST POST /api/v1/backtests; currently
     leased by a sibling) + UI-3 (dashboard backtest controls / start-end date pickers). BOTH reuse
     launch::parse_window — wire them to it, don't re-derive the date binding.
Blocked-on: SRS-API-001, UI-3 (the operator-interface features). Items 1+2 are additional non-feature
blockers gated on the two scope decisions above; once a human makes those calls, a future session can
add the Parquet reader (data layer) + Python host, wire API/dashboard to launch::parse_window, then flip.
