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

---

# === SESSION UI-5 (2) — de-churn, 2026-07-27 ===
Session-2 outcome: partial(blocked-on SRS-API-001 + SRS-RESV-002..006). No code changed.
Still serialized; `passes` stays FALSE. The first `Outcome:` line above is unchanged on
purpose — `serialized_notes()` reads only that one.

## Why this session existed: UI-5 was re-offered, but nothing was left to build

The scheduler handed UI-5 to a fresh session even though the panel above had already
integrated onto main. Verified this session (not taken on faith from the note): the tree is
clean, HEAD == origin/main == d3201aa, and every artefact is present — hotswap.py,
assets/hotswap.js, the panel in index.html/app.js/styles.css, the /dashboard/api/hot-swap
route in server.py, and the unit/boundary/domain tests. So this was churn, not work.

**Two independent root causes, and UI-5 hit both:**

1. **ROOT lags origin/main.** `serialized_notes()` (tools/agent_pool.py:459) reads
   `ROOT/progress.d`, i.e. the primary checkout at
   /Users/joshgenao/Documents/Programming/Python/alphalabs — NOT the worktree and NOT
   origin/main. ROOT sits on branch `chore/mypy-python-clean` at 4c123a8, **12 commits
   behind origin/main**, with an uncommitted mypy cleanup in flight. This very note —
   which does say `Outcome: serialized` — was therefore invisible to the scheduler, so
   UI-5 never entered the awaiting-verification bucket. The same blind spot covers the
   other three notes integrated since 4c123a8: SRS-BT-008, SRS-DATA-010, SRS-SAFE-003.
2. **No recorded dependency edges.** tools/feature_deps.json had *no* UI-5 entry, unlike
   every sibling panel (UI-1 → NOTIF-001/UI-002/UI-003; UI-2 → BT-004/EXE-001;
   UI-3 → API-001; UI-4 → SAFE-001/SAFE-002). A dep-less, flip-blocked feature returns to
   the ready frontier every cycle — the known churn loop.

Cause 2 is fixed below and is branch-independent. Cause 1 is the operator's to fix
(see "Operator action" — this session deliberately did not touch ROOT's working tree).

## What I did

Recorded the real blocking edges, derived from the code rather than from prose. hotswap.py
names each deferred cell's owner as a constant (lines 95-108), and each is a real
passes:false feature:

  SRS-RESV-002  HOT_SWAP_CANDIDATE_OWNER   promotion_candidate            → arms the promote control
  SRS-RESV-003  HOT_SWAP_TRIGGER_OWNER     auto_triggers_enabled/_live    → auto-trigger config AC
  SRS-RESV-004  HOT_SWAP_DEMOTION_OWNER    demotion_pending               → demotion-pending AC
  SRS-RESV-005  HOT_SWAP_PROMOTION_OWNER   current_live_strategy_id       → what is being demoted
  SRS-RESV-006  HOT_SWAP_COOLDOWN_OWNER    cooldown.{in_effect,started_at,expires_at} → cool-down AC

plus SRS-API-001, because /api/v1/hot-swap resolves to a DeferredHandler → 501
HANDLER_DEFERRED today, so the manual-promotion AC cannot be exercised at all until
API-001 binds the handler.

  python3 tools/agent_pool.py block UI-5 --on SRS-API-001 SRS-RESV-002 SRS-RESV-003 \
      SRS-RESV-004 SRS-RESV-005 SRS-RESV-006 --reason "<see plan-UI-5.md>"

**Where that edge lives, and why it is NOT in this branch's diff.** `block` writes the
*canonical* deps file, `DEPS_FILE = ROOT/tools/feature_deps.json` (agent_pool.py:159) — the
primary checkout's copy, which is the only one the scheduler reads. This worktree's
`tools/feature_deps.json` is a stale snapshot of origin/main and still reads `UI-5 -> None`;
that is correct and deliberate. A branch commit touching `tools/feature_deps.json` is
rejected outright by `shared_state_violations` (agent_pool.py:905, integrate exit 6) —
only the integrator may write it. The path onto main is `integrate`'s own
`_sync_deps_into(wt)` (agent_pool.py:828), which copies ROOT's canonical file into the
worktree *after* the violation check and stages it into the `[agent-integrate]` marker
commit. So: the scheduler is protected the moment `block` returned; main records it at
integrate. Reviewing this branch's tree alone cannot see the edge — check ROOT, or check
main after the marker commit.

SRS-LOG-001 was deliberately EXCLUDED. It appears at step 6 of the flip path above (HOT_SWAP
records back the durable state), but no AC fact depends on it — over-blocking is as
dishonest as under-blocking, and it would gate UI-5 on a feature its acceptance criteria
never mention.

**Explicitly NOT done: any rebuild or redesign of the pane.** Nothing in UI-5's steps[] is
solo-verifiable that the prior session did not already do; steps 2-3 need an
operator-witnessed browser walkthrough over producers that do not exist. Re-implementing the
panel would have been pure churn on top of churn. The operator was asked whether to run a
/frontend-design pass over the existing Changeover Console this session and chose not to —
the panel already shipped design-approved, and its producers are still deferred.

## What I tested (per UI-5 step)
- Step 1 (./init.sh + dashboard in browser automation): PASS previously (session 1); not
  re-run — no code changed this session, so there was nothing to re-prove.
- Steps 2/3 (workflow walkthrough; the four AC facts): UNCHANGED — still operator-witnessed,
  still gated on the six producers above. This is why passes stays FALSE.
- Step 4 (trace to SRS-RESV-003..006, leave passes false): PASS — the trace is now recorded
  as machine-readable dependency edges in the canonical ROOT deps file (not just prose), so
  the scheduler enforces it. It reaches main via the integrator's marker commit, never via
  this branch — see "Where that edge lives" above.
- Regression evidence that the landed code is still green on current main:
  `.venv/bin/pytest tests/unit/test_dashboard_hot_swap.py
   tests/boundary/test_dashboard_hot_swap_wiring.py
   tests/domain/test_dashboard_hot_swap_status.py -q` → **19 passed in 6.20s**.
- Verified the edge landed in the canonical file: reading
  /Users/joshgenao/Documents/Programming/Python/alphalabs/tools/feature_deps.json (ROOT, the
  copy the scheduler reads) gives
  UI-5 -> ['SRS-API-001','SRS-RESV-002','SRS-RESV-003','SRS-RESV-004','SRS-RESV-005','SRS-RESV-006'],
  and `block` reported no skipped cycles. This worktree's copy still reads UI-5 -> None by
  design; do not "fix" that by staging it.
- Post-integrate confirmation to run: `agent_pool.py status --no-fetch` must list UI-5 under
  **blocked**, not on the ready frontier.

Full gate (session 2), run with `.venv` active — run_ci_locally.sh dies on system python3
(ModuleNotFoundError: numpy) because it shells the ambient interpreter, so `source
.venv/bin/activate` first:
  ruff check .                              PASS
  ruff format --check .                     1 PRE-EXISTING failure, NOT mine (see below);
                                            428/429 formatted, and re-running with that one
                                            file excluded is clean
  cargo fmt --check                         PASS
  cargo clippy --workspace -- -D warnings   PASS (exit 0)
  pytest -m "not integration and not e2e"   4168 passed, 4 skipped, 129 subtests (3:14).
                                            The 4 skips are pre-existing; note that
                                            tests/domain/test_single_live_invariant.py skips
                                            "pending Hot-Swap per SRS-RESV-001..006" — the
                                            same producers UI-5 is now blocked on.
  cargo test --workspace                    exit 0, zero failing results (the
                                            "data008_tier_cli: NAS archival sync FAILED"
                                            string in the log is asserted-on test OUTPUT of a
                                            failure-path test, not a failing test)
  critic_check.py --range origin/main..HEAD APPROVE
  architecture / contract checks            exit 0

**Pre-existing red on main, owner SRS-SAFE-003 (not this session's to fix):**
`ruff format --check` wants to reformat `tests/domain/test_safe003_connectivity_block_cli.py`,
which arrived on main in fa8b837 (feat(SRS-SAFE-003)). Proven not mine: this branch's entire
diff vs origin/main is progress.d/plan-UI-5.md + progress.d/session-UI-5.md — two markdown
files a Python formatter does not read. Deliberately NOT fixed here: the SRS-SAFE-003 agent
still held a live lease while this ran, so reformatting its file would collide with in-flight
work, and formatting a sibling's file inside a feature branch is the known
CI-red-behind-format-gates anti-pattern. Owner should run
`ruff format tests/domain/test_safe003_connectivity_block_cli.py` in the SAFE-003 branch.

## Critic verdicts (session 2; diff is this note only)
  deterministic (critic_check.py --staged): APPROVE — no findings.
  Note: progress.d/ is carved out of the SAFETY_PATH_RE paired-domain-test rule
  (tools/critic_check.py:358), so a notes-only chore does not demand a tests/domain diff.

  judgment (adversarial_review.py origin/main, reviewer=codex): APPROVE at r2.
    r1 BLOCK — "False scheduler dependency handoff" (high, 0.98): the note asserted the
      dependency fix had landed, but `tools/feature_deps.json` in the reviewed tree ends at
      UI-4 with no UI-5 entry, so a reader diffing this branch would conclude the de-churn
      protection was fabricated. The observation was factually correct about the branch tree.
      Root cause was MY PROSE, not the fix: `block` writes the canonical
      ROOT/tools/feature_deps.json, which is genuinely populated, while the branch copy stays
      stale by design. Codex's literal recommendation — "stage the actual
      tools/feature_deps.json update" — must NOT be followed: staging it makes
      `shared_state_violations` reject the integrate (exit 6), because only the integrator
      may write that file. Fixed by taking the other branch of the same recommendation:
      re-scoped the note to say exactly where the edge lives, why it is absent from this
      diff, and how `_sync_deps_into` carries it to main. Same lesson as UI-4's evidence
      attribution round — when a reviewer calls a claim unsupported, correct the claim's
      scope rather than the artefact it points at.
    r2 APPROVE.

## Tooling gap found the hard way: a tracked plan-<id>.md is IMMUTABLE

`progress.d/plan-UI-5.md` is tracked — session 1 left it untracked and the integrator's
`git add -A -- progress.d` swept it into marker commit d3201aa. Once tracked, **no later
session can update it**, because both exits are closed:
  - committing it on the branch → `shared_state_violations` rejects the integrate (exit 6);
    only `progress.d/session-<fid>.md` may come from a branch commit;
  - leaving it dirty for the integrator to sweep → `git rebase` refuses outright
    ("cannot rebase: You have unstaged changes"), and integrate reports that as
    "rebase onto origin/main conflicted", which reads like a content conflict but is not.
    Verified empirically: even a **no-op** rebase (branch already on top of origin/main)
    still refuses with an unstaged tracked file.
So this session's plan lives only in the approved-plan record and in this note, and
progress.d/plan-UI-5.md was restored to its session-1 content untouched. Next session: do
not try to edit a tracked plan-*.md — put the plan in the session note, or the integrate
will fail in a way whose error message points at the wrong thing.

## Operator action (I could not do this — ROOT holds your uncommitted work)

To close cause 1 generally, ROOT's progress.d/ needs to reach origin/main:

    cd /Users/joshgenao/Documents/Programming/Python/alphalabs
    # commit or stash the in-flight chore/mypy-python-clean changes FIRST, then bring
    # progress.d/ up to origin/main so serialized_notes() can see the newest notes.

Until then the scheduler stays blind to the session notes for UI-5, SRS-BT-008,
SRS-DATA-010 and SRS-SAFE-003. UI-5 specifically is now safe either way — the dependency
edges block it independently of the note.

## Resume / next
The flip path in the section above is unchanged and still correct. UI-5 is now
`blocked-on SRS-API-001, SRS-RESV-002..006` and will not be re-offered until those pass.
When they do: implement a concrete HotSwapStatusSource against the Protocol seam, pass it
to HotSwapStatusProvider in mount_default_dashboard, re-run the UI-5 e2es with the operator
witnessing, and flip via the verified-e2e label. Do NOT rebuild the pane.
