# UI-5 — Dashboard Hot-Swap controls & status

## Context — why this change

`UI-5` (P1, `passes:false`) requires the dashboard to *"provide Hot-Swap controls and status"* —
AC: the operator can **(1) trigger manual promotion, (2) inspect demotion-pending state,
(3) view cool-down expiry, (4) see automatic-trigger configuration** — tracing
**SRS-RESV-003 → SRS-RESV-006**. The API contract already declares the exact surface this panel
consumes: `POST /api/v1/hot-swap` (`candidate_strategy_id, confirm` → `swap_id, demotion_state,
promotion_state`, `requires_confirmation=True`) and `GET /api/v1/hot-swap/status`
(`current_live_strategy_id, demotion_pending, cooldown_expires_at, auto_triggers_enabled`) —
UI-5 is the dashboard consumer that contract was written for.

**Backend reality (verified):** every live producer is deferred. RESV-003 (trigger decision/config
layer) is serialized in Rust against injected ports with *no durable state and no HTTP binding*;
RESV-004/005/006 are unbuilt; the `/api/v1/hot-swap*` routes resolve to a `DeferredHandler` →
**501 HANDLER_DEFERRED owner `SRS-RESV-003`**; there is **no** durable hot-swap artefact and **no**
`HOT_SWAP` log writer. So all four AC facts have deferred producers today.

This is the **control-affordance-over-deferred-producer** pattern, already shipped and
operator-approved four times — UI-1 (alerts), UI-2 (promote-live), UI-4 (kill-switch). UI-5 is the
hot-swap sibling. Building it now with the **contract route pinned by e2e** and every cell rendered
honestly (deferred, never fabricated) makes the eventual flip a **cell-swap, not a rebuild**: when
API-001 binds the routes and RESV-003..006/RESV-002/LOG-001 land producers, UI-5 lights up in place.
UI-5's own Step 4 anticipates this: *"leave passes false until browser evidence is captured."*

**Outcome: integrate `--mode serialized`, `passes:false`.** Zero cells are green today; the flip is
gated on the RESV/API cluster + operator-witnessed browser e2e (which binds the shared dashboard
stack and can't run as a gate while siblings are active).

## Design — "The Changeover Console" (/frontend-design)

Hot-Swap is the highest-drama action in the whole system: swapping which strategy trades **real
money**. The panel should feel **instrument-grade and cinematic** — a hardware throttle-quadrant
crossed with a modern trading terminal — not another flat card. Bold, dark-first, one hot accent,
and an oversized hero instrument you remember.

**Color & atmosphere.** A dominant deep-charcoal field with **depth, not flatness**: a faint
radial **gradient-mesh** glow (two low-opacity `--accent`/`--bad` radials, blurred) behind the hero
dial, plus a ~3% hand-authored CSS **grain** overlay so the surface has texture. Exactly **one**
saturated color at rest — `--accent` = LIVE (used sparingly, so "live money" reads as special);
`--bad` = the DEMOTE/hazard side, surfaced only as a slow-drifting 45° hazard hatch when armed; the
`--deferred` 45° hatch is the UNKNOWN motif **everywhere** (the fail-closed rule rendered in pixels).
Fully re-mapped for light theme via the existing `[data-theme="light"]` tokens.

**Typography (character from treatment — no font files, per SEC-002).** Dramatic scale/weight
contrast: the cool-down readout is a **hero** — `clamp(2.4rem, 6vw, 4rem)`, weight 700,
`tabular-nums`, tight tracking — a glowing `6d 04h 12m`; when deferred it becomes a **dashed em-dash
placeholder at the same scale** so "unknown" is loud, never blank. Instrument labels are `--mono`,
uppercase, `letter-spacing:.16em`, weight 600, `--ink-faint` ("COOL-DOWN / DEMOTE LIVE / PROMOTE
CANDIDATE"). Strategy tokens: mono with a small status pip.

**Signature instrument #1 — the Cool-down Dial** (oversized hero, asymmetric left). Inline-SVG
radial gauge (~200px): an outer **7-tick ring** (one major tick per cool-down day, minor ticks
between), a **depleting progress arc** (`stroke-dasharray`/`stroke-dashoffset`) that empties toward
expiry, a thin inner guide ring, and a center well holding the hero readout + "COOL-DOWN" micro-cap
+ a `started → expires` sub-caption. State lives in geometry: ACTIVE → accent arc + edge glow;
EXPIRED → dim full ring + "READY"; **DEFERRED (today)** → hatch-filled arc track, muted ticks,
dashed readout, a `deferred:SRS-RESV-006` chip.

**Signature instrument #2 — the Changeover Track** (spans full width beneath the dial). A horizontal
directional flow: `LIVE token → [DEMOTE gauntlet: stop-signals → cancel → liquidate → FLAT, with a
fork diamond for the SYS-49b timeout → demotion-pending branch] → handoff gap → [PROMOTE: candidate
→ LIVE flat-start]`. Reuses UI-4's diamond nodes + `[data-status]` rail coloring + branch-fork idiom
but laid out **horizontally, left-to-right, arrowed**. The LIVE token breathes softly when genuinely
live (today: a hatched `deferred:SRS-RESV-005` token); on a future successful swap it **eases along
the track DEMOTE→PROMOTE** — the memorable changeover motion.

**Control strip — trigger chips + command bar** (dense zone, right). Three automatic-trigger chips
(DRAWDOWN DEMOTION / TOP-RANKED / HIGHEST-MOMENTUM) styled as **hardware toggle switches** (track +
knob), each showing `default: OFF` and a hatched live-state (`deferred:SRS-RESV-003`); an always-lit
**MANUAL** chip marks the active path. The promote control (`#hs-btn`) is wrapped by an SVG
**arming countdown ring** (reuse UI-4's 5 s `stroke-dashoffset` sweep); **inert** (dimmed, hatch
cursor, "no candidate — Reservoir ranking deferred") when `promotion_candidate` is null, **armed**
(accent ring ignites + hazard hatch drifts) when a candidate exists.

**Motion (CSS-only, behind `prefers-reduced-motion: reduce`).** Four purposeful moments, nothing
gratuitous: (1) entrance cascade — dial fades+scales in, rail nodes wipe L→R, chips rise (via the
existing `--i:11` stagger); (2) arm — ring sweeps, command-bar border ignites, hazard hatch drifts;
(3) live pulse — LIVE token breathes + a faint shimmer travels the active cool-down arc; (4)
changeover — token eases DEMOTE→PROMOTE on success. Reduced-motion collapses all to static.

**Spatial composition.** Asymmetric two-zone `panel--wide`: the oversized dial hero with generous
negative space (left) against a dense instrument control strip (right), with the changeover track as
full-width connective tissue beneath — grid-breaking, not a symmetric card row.

**Constraints (respected, not leading).** Strictly self-contained (SEC-002/NFR-S3): no CDN, no
remote fonts/images — assets are read once into a fixed path→bytes map. All new CSS is **scoped to
`.panel--hotswap`**; shared `styles.css` changes are **additive only** (so UI-001/002/004 panels +
tests don't regress); after any rebase, assert brace-balance == 0 (the UI-1 lost-`}` gotcha). Verify
self-containment by grepping assets for `http|https|cdn|fonts.googleapis`. **Screenshot every state**
(deferred / armed / inert-no-candidate / degraded + light theme) via a scratchpad Playwright script
on an ephemeral port before committing — the UI-4 ring/rail geometry bugs were invisible in code and
obvious in the render.

## What I'll build (clone the UI-4 kill-switch pattern — 7 files)

1. **`python/atp_dashboard/hotswap.py`** *(new — clone `killswitch.py` + `reservoir.py`)*
   - `HotSwapStatusSource(Protocol)` + `DurableHotSwapSource` (the flip seam; today no artefact
     exists so the default provider takes `source=None`), `HotSwapStatusUnavailable(Exception)`.
   - `HotSwapStatusProvider(source=None)` with `hot_swap_snapshot() -> dict`, importing
     `DEFERRED`/`deferred_field_named` from `.provider` for the exact
     `{"value": null, "data_source": "deferred:<owner>"}` cell.
   - Snapshot = **real static SYS-49 schema** (like `reservoir.py`'s selector config) +
     **deferred live cells**:
     - real: `trigger_catalog` (manual + 3 automatic kinds, `automatic_default:"disabled"` per
       SYS-49a), `cooldown_days_default:7` (SYS-49e), `liquidation_timeout_seconds_default:60`
       (SYS-49b), `srs_ref`.
     - deferred: `current_live_strategy_id`→SRS-RESV-005, `promotion_candidate`→SRS-RESV-002
       (gates the control), `demotion_pending`+`demotion_detail`→SRS-RESV-004,
       `cooldown{in_effect,expires_at,started_at}`→SRS-RESV-006, `auto_triggers_live[]`
       (per-kind enabled)→SRS-RESV-003.
   - Fail-closed: `source=None` → `ok:true, errors:[]`, all live cells deferred; a wired-but-
     unreadable source → `ok:false` + verbatim reason, **never** fabricated. No WS channel.
2. **`python/atp_dashboard/server.py`** — add `HOT_SWAP_SNAPSHOT_PATH = "/dashboard/api/hot-swap"`;
   a keyword-only `hot_swap: HotSwapStatusProvider | None = None` on `mount_dashboard` +
   `register_meta_route(HOT_SWAP_SNAPSHOT_PATH, hot_swap.hot_swap_snapshot)`; in
   `mount_default_dashboard` compose the provider always-on with an **opt-in** source via
   `ATP_HOT_SWAP_STATE` (unset today → `HotSwapStatusProvider(None)`).
3. **`python/atp_dashboard/__init__.py`** — export the provider/source/exception + snapshot path.
4. **`python/atp_dashboard/assets/index.html`** — new
   `<section class="panel panel--wide panel--hotswap" data-panel="hotswap" style="--i:11">`
   (kill-switch is the current last at `--i:10`); body = changeover track + cool-down dial +
   trigger-chip row + the promote command bar (`#hs-btn`) + status/receipt.
5. **`python/atp_dashboard/assets/app.js`** — constants
   `HOT_SWAP_ROUTE = "/api/v1/hot-swap?confirm=true"` (mutate) and
   `HOT_SWAP_STATUS_ROUTE = "/dashboard/api/hot-swap"` (read); `renderHotSwap`/`hsUnknown`
   fail-closed pair; `pollHotSwap`/`initHotSwap`; an **arm-then-confirm state machine**
   (`hotArmTimer`/`hotInFlight`/`hotConfirmedId`) with in-flight guard first, `AbortSignal.timeout`,
   POST body `{candidate_strategy_id, confirm:true}`, identity-bound success; boot-block wiring.
   **Deliberately omit** hot-swap from `PANEL_FRESH`/`buildAll` fresh-dots (deferred REST pane must
   not read as an SLA breach — matches kill-switch).
6. **`python/atp_dashboard/assets/styles.css`** — `.panel--hotswap` block (dial, changeover track,
   trigger chips); reuse `--accent/--ok/--warn/--bad/--deferred` tokens + the 45° hatch. After any
   rebase, assert brace-balance == 0 (the UI-1 lost-`}` gotcha).
7. **`python/atp_dashboard/assets/hotswap.js`** *(optional UMD)* — if the cool-down/demotion cell
   classification is worth a node unit test, extract a pure `hotSwapCell(...)` like `freshness.js`
   (register in `server.py` `_ASSET_SPEC` + `<script defer>` before app.js).

### Honesty pre-fixes baked into pass 1 (so codex converges fast)
Walk all three checklists up front — control-affordance (7 classes), evidence-attribution, and
safety-pane:
- **Unknown truth = no actionable rows:** `promotion_candidate === null` → promote control **inert**
  with "no candidate — Reservoir ranking deferred (SRS-RESV-002)". Every degraded poll branch
  (5xx / 404 / abort / malformed) disarms the control + clears rows, not just the caption.
- **Identity binding:** a 2xx designates a swap **only** if the body carries a non-empty `swap_id`,
  `promotion_state === "PROMOTED"`, and the echoed candidate equals `hotConfirmedId`; a
  misrouted/stale success renders an error naming both ids. No `activation`-style over-claim.
- **Tri-state facts:** `demotion_pending`/`cooldown.in_effect` are true/false/**null**; a deferred
  cell (`value:null`) can **never** draw a resolved rung even if a sibling status string disagrees
  (require `value` present + agreeing). Shape-drift → refuse the whole payload → all UNKNOWN.
- **Cool-down warning (RESV-006):** fail-closed — unknown cool-down does **not** suppress the
  arm-then-confirm; note "cool-down state unknown (deferred:SRS-RESV-006)".
- **In-flight serialization:** one mutate in flight; all sibling triggers inert until it settles.

## Tests (land in the same commit)

- **L7 domain — `tests/domain/test_dashboard_hot_swap_status.py`** `[domain, safety]` **MANDATORY**
  (path matches `hot[_-]?swap` in `SAFETY_PATH_RE`; the critic blocks otherwise). Pins:
  read-only verbs on `/dashboard/api/hot-swap` (POST/PUT/DELETE → 404/405); the exact app.js literal
  `const HOT_SWAP_ROUTE = "/api/v1/hot-swap?confirm=true";` with `app_js.count("/api/v1/hot-swap")`
  bounded; on the un-wired runtime `POST /api/v1/hot-swap` → **428 CONFIRMATION_REQUIRED** and
  `POST …?confirm=true` → **501 HANDLER_DEFERRED owner SRS-RESV-003**; no-fabrication (every
  `data_source` starting `deferred` has `value is None`); provider registers no WS channel.
- **L4 boundary — `tests/boundary/test_dashboard_hot_swap_wiring.py`** `[boundary]` (clone
  `test_dashboard_killswitch_wiring.py`): route served **only** when `hot_swap=` mounted; read-only;
  no WS channel; unreadable source → honest `ok:false`; default composition serves all-deferred with
  no config; opt-in env reads a stub source.
- **L6 e2e — additions to `tests/e2e/test_dashboard_refresh.py`** `[e2e]` (ATP_RUN_E2E-gated;
  **written now, run by the operator** as the serialized flip evidence): arm-then-confirm fires
  exactly **one** POST to the contract route; click-1 arms with no POST + auto-disarm; identity
  binding (200 w/o `swap_id` / mismatched candidate → refused, never "promoted"); shape-drift →
  wholesale refusal; deferred cell never draws a resolved rung; degraded 503/404/`route.abort`
  clears every leg; **in-flight serialization via a held-route** (`held=[]` → append → later
  `fulfill`); control **inert** when `promotion_candidate` null, armable when a candidate is injected
  via `page.route`.
- **L1 unit (optional) — `tests/unit/test_dashboard_hot_swap.py`** node-driven, skip-if-no-node,
  only if the UMD helper is extracted.

Runnable in this session (siblings active): `pytest -m "not integration and not e2e"` (domain +
boundary + unit) and `cargo test --workspace`. The e2e suite is **not** run here.

## Completeness & dependencies

- **Completeness: `serialized`.** All 4 AC facts deferred; Steps 2–3 need operator-witnessed browser
  e2e. `passes` stays **false**.
- **Dependencies:** the build needs no unbuilt prerequisite — the contract routes + runtime
  substrate + `provider.py`/`killswitch.py`/`reservoir.py` patterns all exist. I will **not**
  `block`; it integrates serialized now (like UI-2/UI-4). Flip gate (for the note): API-001 binds
  the routes → RESV-003 durable trigger config → RESV-004/005 execution + durable demotion-pending →
  RESV-006 cool-down store → RESV-002 candidate → LOG-001 `HOT_SWAP` records → operator runs the
  UI-5 e2es.

## Verification (this session)

1. `./init.sh` → "✓ Environment ready" (done).
2. `pytest -m "not integration and not e2e" tests/domain/test_dashboard_hot_swap_status.py
   tests/boundary/test_dashboard_hot_swap_wiring.py [tests/unit/…]` → green.
3. `cargo test --workspace` (unchanged Rust must stay green) + grep assets for
   `http|https|cdn|fonts.googleapis` (SEC-002 self-containment) → none.
4. **Design QA:** scratchpad Playwright screenshot on an **ephemeral port** (`port=0`, parallel-safe
   — same as the e2e fixture; touches no IB/docker/fixed port) of each state
   (deferred / armed / inert-no-candidate / degraded, + light theme), *if* chromium is installed;
   else note deferred to the operator e2e run.
5. Walk every UI-5 `steps[]` entry, recording per-step PASS/FAIL (Steps 2–3 = "requires browser e2e
   → serialized").
6. Critic pass 1 (`critic_check.py --staged`) + pass 2 (`adversarial_review.py origin/main`) → both
   APPROVE, then `run_ci_locally.sh` → `integrate --mode serialized`.
7. Write `progress.d/session-UI-5.md` (resume/flip pointer) as the chore commit.
