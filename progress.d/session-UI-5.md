=== SESSION UI-5 ===
Date: 2026-07-26
Feature: UI-5 — the dashboard shall provide Hot-Swap controls and status
         (SRS-6 UI-5; traces SRS-RESV-003 through SRS-RESV-006 / SyRS SYS-49a..e)
Outcome: serialized (passes stays FALSE — every live producer is deferred, and
         the browser-e2e acceptance evidence Step 2/3 must be operator-witnessed)

## Context — why serialized

UI-5 is the Hot-Swap sibling of UI-2 (promote-live) and UI-4 (kill-switch): a
control affordance + status pane over a deferred producer. The API contract
already declares the surface — POST /api/v1/hot-swap (candidate_strategy_id,
confirm → swap_id, demotion_state, promotion_state; requires_confirmation) and
GET /api/v1/hot-swap/status (current_live_strategy_id, demotion_pending,
cooldown_expires_at, auto_triggers_enabled) — but EVERY live producer is deferred:
RESV-003 (trigger decision/config) is serialized in Rust against injected ports
with no durable state or HTTP binding; RESV-004/005/006 are unbuilt; the
/api/v1/hot-swap* routes resolve to a DeferredHandler → 501 HANDLER_DEFERRED owner
SRS-RESV-003; there is no durable hot-swap artefact and no HOT_SWAP log writer.
So all four AC facts render as honest deferred cells today. Building the affordance
now with the contract route pinned by e2e makes the flip a cell-swap, not a rebuild.

## What I did

Built the "Changeover Console" panel (design-approved via /frontend-design), 7 files:
- **python/atp_dashboard/hotswap.py** (new) — HotSwapStatusProvider(source=None) +
  the HotSwapStatusSource Protocol (the FLIP SEAM) + HotSwapStatusUnavailable.
  hot_swap_snapshot() serves REAL static SYS-49 schema (TRIGGER_CATALOG with
  automatic_default=disabled, COOLDOWN_DAYS_DEFAULT=7, DEMOTION_TIMEOUT_SECONDS_DEFAULT=60)
  + DEFERRED live cells: current_live_strategy_id→RESV-005, promotion_candidate→RESV-002
  (gates the control), demotion_pending/demotion_detail→RESV-004,
  cooldown{in_effect,started_at,expires_at}→RESV-006, auto_triggers_enabled +
  auto_triggers_live[]→RESV-003, and the 5-rung changeover_sequence. Fail-closed:
  source=None → ok:true all-deferred (producers unbuilt ≠ a fault); a wired-but-
  unreadable source → ok:false + verbatim reason; three source legs fail independently.
  No WS channel (the AsyncAPI contract declares none).
- **python/atp_dashboard/server.py** — HOT_SWAP_SNAPSHOT_PATH="/dashboard/api/hot-swap";
  hot_swap= kwarg on mount_dashboard w/ register_meta_route; hotswap.js in _ASSET_SPEC;
  mount_default_dashboard composes HotSwapStatusProvider() unconditionally (pure builder,
  no env knob — no artefact to read yet; the source Protocol is the flip seam).
- **python/atp_dashboard/__init__.py** — exports.
- **assets/index.html** — the panel (--i:11): cool-down dial + DEMOTE→PROMOTE slots +
  trigger chips + promote command bar (#hs-btn) + changeover track; hotswap.js <script>.
- **assets/app.js** — HOT_SWAP_ROUTE="/api/v1/hot-swap?confirm=true" + status route;
  renderHotSwap/hsUnknown fail-closed pair; arm-then-confirm machine (in-flight guard,
  candidate gate, AbortSignal.timeout, identity binding on swap_id+promotion_state===PROMOTED,
  result-sticky caption); pollHotSwap/initHotSwap + boot wiring. Deliberately NOT in
  PANEL_FRESH/buildAll fresh-dots (deferred REST pane must not read as an SLA breach).
- **assets/hotswap.js** (new UMD) — pure hotSwapCellValue/hotSwapCellBool (a deferred:*
  cell is never read) + hotSwapCooldown (deferred/expired/active classifier, time injected).
- **assets/styles.css** — scoped .panel--hotswap block (inline-SVG cool-down dial with a
  depleting arc + hatch-when-deferred, hardware-toggle chips, horizontal changeover rail
  reusing the 45° --deferred hatch + diamond fork idiom, CSS-only motion behind
  prefers-reduced-motion). Additive only; brace-balance == 0; light+dark themed.

Honesty pre-fixes (control-affordance + evidence-attribution + safety-pane checklists):
candidate=null → control inert; every degraded branch disarms + clears; identity binding
on swap_id; tri-state demotion_pending/cooldown; deferred cell never draws a resolved rung;
shape-drift refused wholesale; in-flight serialization; cool-down warning fail-closed on
unknown (SYS-49e).

## What I tested (per UI-5 step)
- Step 1 (run ./init.sh + open dashboard in browser automation): PASS (solo) — env ready;
  panel served at /dashboard (panel--hotswap + hotswap.js 200); 5 scratchpad Playwright
  screenshots captured (deferred/rich/armed/light/degraded) — geometry verified in-render.
- Step 2 (navigate the UI-5 workflow / user action): PARTIAL — exercised via e2e
  route-interception (arm-then-confirm, refusals, serialization); a producer-backed
  operator walkthrough REQUIRES the RESV/API producers → serialized.
- Step 3 (verify AC: manual promotion, demotion-pending, cool-down expiry, auto-trigger
  config): PARTIAL — all four surfaced + driven via injected payloads (e2e); every live
  producer is deferred, so real end-to-end verification is operator-witnessed → serialized.
- Step 4 (trace to SRS-RESV-003..006; leave passes false until browser evidence): PASS —
  owners wired per fact; passes stays FALSE by design.

Automated (this session):
- tests/domain/test_dashboard_hot_swap_status.py [domain,safety] — 6 tests PASS
  (read-only, 428/501 guard + owner SRS-RESV-003, no-fabrication walker, SYS-49 defaults,
  no WS channel).
- tests/boundary/test_dashboard_hot_swap_wiring.py [boundary] — 8 tests PASS
  (route-only-when-mounted, read-only, no WS, unreadable→ok:false, NON-mapping source→ok:false
  no-crash, stub source resolves cells, default composition all-deferred).
- tests/unit/test_dashboard_hot_swap.py [unit,node] — 5 tests PASS (deferred-cell never
  read; cool-down deferred/expired/active).
- tests/e2e/test_dashboard_refresh.py [e2e] — 24 UI-5 tests PASS under ATP_RUN_E2E=1 on an
  ephemeral port (parallel-safe): AC coverage, inert-without-candidate, arm-then-confirm one
  POST, refusals honest, success confirmed by durable live strategy, wrong-live-strategy
  MISMATCH, stale-post-swap held pending, candidate-change disarm, out-of-order-poll guard,
  demotion-pending block, partial-outage inert, unknown-live inert, unknown-cooldown inert,
  active-changeover block, degraded-status drops staged confirm, ambiguous-timeout inert,
  non-promoted held, shape-drift wholesale, deferred-cell no resolved rung, serialization via
  held-route, arm-window expiry. Operator re-runs these WITH the real producers wired as the
  flip evidence.
- Regression: 71 existing dashboard domain+boundary tests PASS. ruff check/format clean
  repo-wide. mypy: atp_dashboard clean (the 68 pre-existing errors are in other packages;
  CI marks mypy continue-on-error). No Rust touched.
- Full non-e2e gate: `pytest -m "not integration and not e2e"` → 4060 passed, 4 skipped
  (pre-existing), 129 subtests passed (0:03:33). cargo unaffected (no Rust changes).

## Critic verdicts
  deterministic (critic_check.py --staged): APPROVE — no findings (the mandatory tests/domain/
    hot-swap safety test is paired in the diff; SAFETY_PATH_RE matched via hot[_-]?swap/demotion).
  judgment (adversarial_review.py origin/main, reviewer=codex): APPROVE (round 13). The
    mutating control drew an extended stale-truth-left-ACTIONABLE convergence — 12 real
    fail-closed findings, each fixed + regression-tested (the hot-swap analogue of UI-2's 7
    rounds, longer because the swap is async with a 60s demotion + many safety states):
      r1 candidate-change-while-armed → disarm-on-upsert + arm-time candidate binding
      r2 stale-window after result → clear stale candidate/cool-down + AWAIT re-read + keep disabled;
         success bound to durable current_live_strategy_id (per-call ≠ end state)
      r3 out-of-order polls → monotonic hotPollSeq; mutation bumps it
      r4 partial source outage → require snap.ok===true
      r5 active changeover (rung PENDING/BLOCKED/FAILED) → require clear
      r6 unknown current live strategy → require resolved (know what is demoted)
      r7 unknown cool-down → require hotCooldownActive!==null (dial READY on known-clear)
      r8 stale post-swap snapshot → priorLive separates "not yet reflected" from a real mismatch
      r9 non-PROMOTED 2xx not held → any accepted swap → hotPendingSwap holds inert until correlated
      r10 degraded status left arm intact → hsUnknown now disarms
      r11 timeout < 60s demotion → HOT_FETCH_TIMEOUT_MS=90s + ambiguous-timeout pending guard
      r12 Python provider .get() on a non-mapping source → _normalize_leg fails closed to ok:false
    Net: hotActionable() enables ONLY with the FULL swap-safety picture resolved+clear
    (candidate + known live + not-in-flight + no pending swap + ok + demotion===false +
    cool-down known + no active changeover). r13 APPROVE.

## Resume / next (the flip path)
passes:false is CORRECT. To flip UI-5 (operator):
1. SRS-API-001 binds the /api/v1/hot-swap* handlers (the 428/501 path + identity binding
   is already e2e-pinned) → the manual-promotion control goes live as-is.
2. SRS-RESV-003 persists a durable trigger config → auto_triggers_live chips resolve.
3. SRS-RESV-004/005 land demotion+promotion execution + a durable demotion-pending store →
   the changeover rungs + demotion_pending cell resolve.
4. SRS-RESV-006 writes the cool-down (most-recent-successful-swap timestamp) → the dial
   goes active/expired.
5. SRS-RESV-002 (ranking) supplies promotion_candidate → the control arms in production.
6. SRS-LOG-001 HOT_SWAP records back the durable state.
Implement a concrete HotSwapStatusSource (the Protocol seam), pass it to
HotSwapStatusProvider in mount_default_dashboard (add an ATP_HOT_SWAP_STATE knob), then
re-run the 11 UI-5 e2es in tests/e2e/test_dashboard_refresh.py with the operator witnessing
and flip via the verified-e2e label. Do NOT rebuild the pane — swap the provider cells.
