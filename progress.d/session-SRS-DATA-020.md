=== SESSION SRS-DATA-020 ===
Date: 2026-07-13
Feature: SRS-DATA-020 — adjust live positions affected by corporate actions
Outcome: serialized (code complete + fully tested solo; passes stays false)

AC (docs/SRS.md:190 / SYS-28c / StRS SN-1.14): "Live position quantities and average
cost basis are adjusted for splits, reverse splits, and dividends; mergers and symbol
changes remap positions to successor securities; delistings mark positions as delisted
and notify the operator." Verification = scenario test.

What I did
----------
Built a PURE, fail-closed corporate-action planner over LIVE positions — the
live-position sibling of DATA-019 (resting orders) and DATA-011 (the corp-action
substrate). Key structural finding: no rich live position ledger existed
(LiveExecutionState.open_positions is quantity-only, no cost basis), so this
INTRODUCES the live position-with-cost-basis + delisted-status value model, then a
planner over it (mirroring how corporate_action_orders.rs is self-contained).

- crates/atp-execution/src/corporate_action_positions.rs (new; re-exported from lib.rs):
  * LivePosition { symbol (canonical), signed quantity i64, signed cost_basis_minor
    i128, status Active|Delisted }. Average cost derived = basis/qty; constructor fails
    closed on blank symbol, flat quantity, and sign-inconsistent basis (negative avg cost).
  * PositionCorporateAction { Split, Dividend, Merger, SymbolChange, Delisting } (local
    input; composition root maps atp_data::CorporateActionEvent onto it at the deferred seam).
  * PositionCorpActionOutcome { Adjusted, Remapped, Delisted, RequiresManualReview,
    Unaffected } + PositionReviewReason taxonomy. outcome.alert() -> operator page (Delisted
    + Review only); outcome.strategy_callback() -> PositionChangeEvent (Adjusted/Remapped/
    Delisted — distinct audience).
  * plan_position / plan_positions (successor-collision guard) / plan_and_emit (fallible
    PositionCorpActionAlertSink, continue-to-safety, alert_failures surfaced).
- crates/atp-execution/src/bin/data020_position_corp_action_cli.rs (+ Cargo [[bin]]):
  std-only fixture-driven CLI, per-position JSON + notification + callback intents,
  fail-closed exit 2.

Money-math decisions (documented in the module; POST red-team by a Plan sub-agent)
----------------------------------------------------------------------------------
- ALL transforms are EXACT integer or fail-closed-to-review — NO rounding anywhere;
  basis conservation is exact and provable. (Dropped div_round_half_even entirely.)
- Split: quantity x N/M exact (else review — cash-in-lieu never truncated); total
  cost_basis_minor INVARIANT (a split re-expresses the per-unit average only).
- Cash dividend = ADDITIVE `cost_basis - amount_minor*quantity` (checked i128). The
  originally-sketched multiplicative price-ratio factor was WRONG (leaks ~2.5% of P&L
  per ex-date); additive conserves value exactly (P&L invariant across the ex-date,
  numerically verified), matches the actual dividend cash, and is correct for shorts.
  Reclassifies the dividend income -> basis reduction (cap-gain-at-close) — the correct
  choice for a position-only representation with no cash channel.
- Signed-safe quantity scaling: a short reverse-split yields a negative quantity, NEVER
  a spurious review (the load-bearing porting fix vs. DATA-019's positive-only guard).
- Merger: pure stock-for-stock remaps (basis intact); ANY cash leg -> review. Successor
  validated (blank / self-merger / collision-with-held -> review; one position per symbol).

What I tested (per step)
------------------------
Step 1: PASS — ./init.sh -> "✓ Environment ready".
Step 2: PASS — data020 CLI exercised over every action class (fixtures + --positions-file):
  forward/reverse split (long+short), fractional->MANUAL_REVIEW, additive dividend,
  stock-for-stock merger remap, merger-with-cash->review, symbol-change relabel,
  delisting->DELISTED+notify+callback; fail-closed exit 2 (unknown flag / no action /
  two actions / bad basis / sign-inconsistent position).
Step 3: PASS — cargo test -p atp-execution --test srs_data_020_position_corp_action (29)
  --test srs_data_020_corp_action_notify (5); pytest tests/domain/test_data020_live_
  position_corp_action.py (12) + tests/property (2); cargo test --workspace (all green);
  pytest -m "not integration and not e2e" (3401 passed, 3 pre-existing skips).
Step 4: PASS (honest) — classified SERIALIZED; passes stays false.

Gate: cargo fmt --check clean (formatted only my own new files); clippy clean;
dependency_boundary_check.py PASS (atp-execution ∌ atp-notification); architecture_check.py
PASS. NOTE: tools/run_ci_locally.sh ruff step trips ONLY on 2 PRE-EXISTING files
(tests/domain/test_strategy_container_least_privilege.py + tools/architecture_check.py,
both from SEC-003 f1bc789, verified byte-identical to origin/main — the documented
"CI red behind format gates" debt). Ruff is skipped in the canonical init.sh flow
(requirements-dev.txt not installed); my own new .py files are ruff check + format clean.
Out of scope for DATA-020 (shared files; separate toolchain-pin concern) — not touched.

Critic verdicts
---------------
  deterministic (critic_check.py --staged): APPROVE — no findings (pre-commit hook also APPROVE).
  judgment (adversarial_review.py, reviewer=codex): APPROVE (round 2).
    Round 1 BLOCK (2 findings, both fixed in commit 2b5f7eb):
      - HIGH: duplicate canonical input positions bypassed the collision guard
        (fail-open: two records for one symbol each remapped) → plan_positions now
        pre-scans and fails closed to RequiresManualReview{DuplicatePosition}.
      - MEDIUM: json_escape did not escape C0 control chars (U+0000..=U+001F) →
        now \uXXXX-escaped; bin unit test + a CLI parse-back test.
    Round 2 APPROVE: "No material ship-blocking finding … explicitly scoped as a
    serialized fixture/planner slice with passes:false, matching the DATA-019 pattern."

Resume / next (to flip passes:true, operator)
---------------------------------------------
Run the real end-to-end once the live position feed carrying cost basis exists and
notify/callback are live:
  - SRS-EXE-006 / API-5: brokerage adapter account/positions sync (live positions WITH
    cost basis; today's open_positions is quantity-only).
  - SRS-EXE-001: live execution runtime that holds/applies the position ledger.
  - SRS-NOTIF-001: real operator email/SMS for the delisting page.
  - SRS-SDK-004: a position-change strategy callback surface (the SDK's deliver_order_event
    seam is order-event-specific; a position-change callback is deferred to that owner).
Bind PositionCorpActionAlertSink to the real notify fan-out at the composition root;
feed CorporateActionEvent + split/dividend facts from atp-data. Siblings: SRS-DATA-021
(paper virtual positions, atp-simulation — do NOT overlap). block --on
SRS-EXE-001 SRS-EXE-006 SRS-NOTIF-001 SRS-SDK-004.
