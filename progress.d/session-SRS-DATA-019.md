=== SESSION SRS-DATA-019 ===
Date: 2026-07-12
Feature: SRS-DATA-019 — adjust or cancel live resting orders affected by corporate actions (SyRS SYS-28b, StRS SN-1.14; Scenario test)
Outcome: serialized (code merges, passes stays false)

What I did:
- Built `crates/atp-execution/src/corporate_action_orders.rs` — a pure, fail-closed
  planner over resting (non-terminal) orders:
    * `plan_resting_order(key, submission, action)` / `plan_resting_orders(ledger, action)`
      / `plan_and_emit(ledger, action, sink)` -> `RestingOrderOutcome::{Adjusted, Cancelled{reason}, Unaffected}`.
    * Split scaling mirrors atp-data byte-stable: quantity × NUM/DEN (EXACT), price × DEN/NUM
      (round-half-to-even via a byte-stable `div_round_half_even`), i128 intermediates, checked_mul.
    * Adjustment-not-possible ALWAYS fails closed to CANCEL with a structured
      `RestingOrderCancelReason`: Delisting, NonPositiveFactor, PriceRoundedNonPositive,
      QuantityNotIntegral (fractional reverse split — never truncate), Overflow.
    * Adjust is applied as cancel-then-new via `OrderLedger::cancel_replace` (no in-place mutation).
    * Neutral emission: `RestingOrderCorpActionAlert` (operator_summary/callback_reason) +
      `RestingOrderCorpActionAlertSink` port (mirrors ConnectivityEventSink/KillSwitchOperatorAlertSink);
      `RestingOrderOutcome::resting_order_cancel()` reuses atp_types::RestingOrderCancel.
- Added `data019_order_lifecycle_corp_action_cli` scenario CLI (fixture --order specs + --orders-file
  reads; per-order adjust/cancel JSON incl. notification + callback intents; fail-closed exit 2).
- Tests: `srs_data_019_resting_order_corp_action` (L1/L2 transform + ledger cancel_replace),
  `srs_data_019_corp_action_notify` (emission-through-port), `tests/domain/test_data019_...` (L7:
  shells both Rust suites, drives the CLI, and proves the strategy-callback clause end-to-end
  through the real SRS-SDK-004 `deliver_order_event` seam).

Key decision — the notify AC clause & the ARCH-002 boundary:
- `atp-execution` MUST NOT depend on `atp-notification` (tools/dependency_boundary_check.py enforces it;
  an initial dev-dependency approach was caught and reverted). Following the established neutral-port
  pattern, the engine EMITS a neutral alert; the composition-root binding onto
  NotificationTrigger::critical_failure -> OperatorNotifier::dispatch (proven by SRS-NOTIF-001's own
  tests) and onto deliver_order_event (SRS-SDK-004) is DEFERRED. Notification is scoped to the CANCEL
  path only (the AC attaches "operator is notified" to "when adjustment is not possible ... canceled").

What I tested (per step):
- Step 1: PASS — ./init.sh -> "Environment ready".
- Step 2: PASS — CLI over fixtures: forward 4:1 (qty×4/limit÷4), reverse 1:10 exact (qty 100->10) vs
  fractional (qty 5 -> CANCELLED/QUANTITY_NOT_INTEGRAL), delisting -> CANCELLED/DELISTING, --orders-file
  read, unknown flag -> exit 2. notification + callback intents present on every CANCELLED line.
- Step 3: PASS (solo, over fixtures) — adjust qty + limit/stop/stop-limit; cancel on delisting /
  fractional / price-rounds-to-0 / non-positive factor / overflow; cancel -> emitted alert (notify) +
  OrderEvent(CANCELLED, reason) delivered to a recording Strategy.on_order_event (callback).
  cargo test -p atp-execution (15 DATA-019 tests) + tests/domain/test_data019 (8) all green.
- Step 4: passes stays FALSE (serialized) — end-to-end needs the deferred live/notify halves below.
- Gate: cargo fmt --check clean; cargo test --workspace ok; pytest -m "not integration and not e2e"
  = 3268 passed / 0 failed (incl. ARCH-002 boundary); run_ci_locally mirror.

State routing (fail-closed, added over adversarial rounds — see below):
  Acked -> adjust or cancel (full known working quantity). PartiallyFilled (affected) -> cancel
  fail-closed (PartiallyFilledNotAdjustable; remaining qty unknown here). New/PendingSubmit (affected)
  -> cancel fail-closed (UnacknowledgedNotAdjustable; pre-ack race — can still ack OR fill at stale
  basis). CancelPending / terminal / unaffected-symbol -> Unaffected. Symbol match is canonical
  (trim+upper, byte-stable with SecurityKey::new). Alert sink is FALLIBLE (dispatch -> Result); a
  failed operator page is surfaced in RestingOrderCorpActionReport.alert_failures, never swallowed.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings (reworded 3 "price/quantity" doc
    phrases that tripped the money:float-arithmetic heuristic; the module has zero floats).
  judgment (adversarial_review.py, reviewer=codex): 4 rounds. Rounds 1-3 found 5 GENUINE in-scope
    bugs, ALL fixed + regression-tested:
      R1: raw symbol match (case/whitespace miss) -> canonical match; over-broad "non-terminal"
          state filter -> per-state routing.
      R2: PartiallyFilled silently ignored -> cancel fail-closed; infallible alert sink -> fallible
          dispatch()->Result with surfaced failures.
      R3: affected New/PendingSubmit left Unaffected (pre-ack race) -> cancel fail-closed.
    R4: BLOCK on the DEFERRED live path only ("the diff only adds a pure fixture planner/CLI and
        explicitly defers live order-state feed + broker cancel wiring") — NOT a code bug; the
        reviewer's own fix option 1 is "serialized groundwork, no completed-feature claim" =
        integrate --mode serialized. This is the intended serialized scope (feature Step 4). The live
        IB/notification path cannot be built or exercised solo (unbuilt EXE-001/006, NOTIF-001,
        SDK-004 + single-live-IB invariant), so the adversarial loop cannot converge to APPROVE this
        session. OPERATOR-AUTHORIZED (AskUserQuestion) to land serialized over the R4 deferred-scope
        block — recorded verbatim, NOT a faked approve.

Deferred (why passes:false), with owners:
- Live resting-order STATE fed from live broker order events + routing the cancel/cancel-replace to the
  real IB adapter (BrokerageAdapter::cancel_order fails closed LIVE_WIRE_PROTOCOL_PENDING) -> SRS-EXE-001 / SRS-EXE-006.
- Real operator email/SMS delivery -> SRS-NOTIF-001.
- Live in-container Python callback delivery -> SRS-SDK-004.
- Composition-root feed of corp-action facts (atp-data coverage/normalization) into execution -> orchestrator.

Resume / next: to FLIP passes:true, an operator runs the end-to-end scenario once EXE-001/EXE-006 +
NOTIF-001 + SDK-004 are live (real resting order adjusted/cancelled at IB -> real email/SMS + real
Python callback). Do NOT rebuild the planner — wire the RestingOrderCorpActionAlertSink to the real
notification/callback fan-out at the composition root, and feed CorporateActionEvent::Delisting +
split facts from atp-data. After integrate, this feature is `block`ed-on SRS-EXE-001 SRS-EXE-006
SRS-NOTIF-001 SRS-SDK-004 to stop re-offer churn.
