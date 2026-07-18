# UI-2 — Dashboard Strategy Management View

## Context

UI-2 (docs/SRS.md:265, P1, traces SRS-ORCH-004 + SRS-EXE-001): "The dashboard shall provide a strategy management view." AC: view active strategies, deployed code version, mode, asset class, container status, key metrics; **live designation requires explicit confirmation** (NFR-S2, SYS-2c, AC-15).

Exploration established:
- The **viewing half already exists** (SRS-UI-002, serialized): the Strategy Inventory panel renders all AC columns; deployed version is REAL via `orch005_rollback_cli list`; mode/asset/container/P&L/positions are honest deferred cells.
- **No deferred cell can flip live**: ORCH-001/002/003/004 + SIM-003 are `passes:true` but landed as in-process gates/demo CLIs with **no queryable read surface** (verified: no container-status CLI, no persisted mode/asset-class registry, `sim003_ledger_cli` is fixture-only, `LiveDesignation` is in-memory and Clone-forbidden). Several owner strings now name `passes:true` features — misleading to the operator.
- The **management leg is the genuinely-unbuilt named surface**: no live-designation control exists anywhere. The contract route `POST /api/v1/strategies/{strategy_id}/promote-live` already exists (`python/atp_api/routes.py:172-182`, `requires_confirmation=True`); the runtime already answers **428 CONFIRMATION_REQUIRED** without `confirm` (`python/atp_runtime/rest_server.py:278`, pinned in `tests/domain/test_operator_interface_runtime.py:51`) and **501 HANDLER_DEFERRED owner SRS-EXE-001** with it (`python/atp_runtime/contract.py:42`).
- Precedents to mirror: kill-switch arm-then-fire affordance (`app.js:929-1006`, domain pins `tests/domain/test_dashboard_safety.py:73,:81`) and UI-1's umbrella e2e over the production composition `mount_default_dashboard` (`tests/e2e/test_dashboard_refresh.py:450-521`).

**Completeness expectation: serialized.** Real designation/mode/metrics values require SRS-EXE-001 + SRS-BT-004 (unbuilt). After integrating, record `block UI-2 --on SRS-EXE-001 SRS-BT-004`. `passes` stays false.

## What to build

### 1. Live-designation affordance (per-row, arm-then-fire) — the new surface
- `python/atp_dashboard/assets/index.html`: retitle panel heading "Strategy Inventory" → **"Strategy Management"** (nothing pins the old title); add a **Manage** column `<th>`; add a **designation status line** (`#designation-status`, `aria-live="assertive"`) whose default reads *"live designation state — awaits SRS-EXE-001"* (deferred-styled), **never** "no live strategy" (all-clear-shaped while the producer is deferred). Update the panel comment block.
- `python/atp_dashboard/assets/app.js` (mirror kill-switch exactly):
  - `PROMOTE_LIVE_ROUTE(id)` = `"/api/v1/strategies/" + encodeURIComponent(id) + "/promote-live?confirm=true"` (contract route only; encoded id — ids come from a CLI).
  - Per-row **Promote live** button rendered by `renderInventoryRow`. Click 1 = arm: `data-armed=true`, text "CONFIRM LIVE: <id>?", status line "armed — click again within 5s", 5s auto-disarm timer. Click 2 within window = POST (bounded `AbortSignal.timeout`), response rendered **verbatim**: 501 → "deferred — owner SRS-EXE-001"; 428/5xx → refusal by error type; network fail → FAILED; 200 → render `strategy_id`/`is_live`/`promoted_at` from the body only. **Never** mark a row/Mode cell live from a POST attempt — `is_live` appears in the status line only, from a real response field.
  - **One armed row at a time** (arming row B disarms row A); **disarm-on-upsert** (row rebuild in `renderInventoryRow` always renders disarmed — an armed button must not survive a data refresh).
### 1b. Design pass (`/frontend-design` skill — loaded; central to this feature)
The dashboard has a committed aesthetic: **"ATP · Mission Control"** — dark-primary instrument console, monospace tabular readouts, one signature chartreuse accent (`--accent: #b6ff3a`) carrying status meaning, dual theme via `[data-theme]` + `prefers-color-scheme`, self-contained assets (no external fonts/CDN — SEC-002 loopback posture). The management view gets a full design execution at that bar, not a minimal bolt-on:

- **The panel becomes the console's command deck**: retitled "Strategy Management", promoted visually — layered panel depth (inset gradient + `--shadow`), a subtle scanline/grid texture behind the table (CSS-only, theme-aware), staggered row reveal on first paint (`animation-delay` per row index, respecting `prefers-reduced-motion`).
- **Table as instrument readout**: monospace tabular figures for version/metric cells, refined column rhythm, hover state that lifts the row with a hairline accent edge, selected/armed row gets a full-width accent inset border. Deferred cells keep the em-dash + owner tag but styled as engraved/dimmed chips so real values visibly outrank them the day producers land.
- **Promote-live as a deliberate two-stage instrument**: resting state = quiet outlined control; armed state = the row locks focus — button swells to "CONFIRM LIVE: `<id>`?", danger choreography (amber→red pulse ring animation on `[data-armed="true"]`, mirroring but distinct from the kill-switch pulse), the rest of the table dims slightly (sibling rows drop opacity) so exactly one candidate reads as staged; a thin countdown bar animates the 5s auto-disarm window (pure CSS `scaleX` transition, honest to `KILL_ARM_WINDOW_MS`-style constant).
- **Designation status line as a proper readout**: beacon dot + `aria-live` caption in the panel head area, dashed-border "deferred" framing (the established awaiting-producer visual convention from the alerts beacon), tones for armed/refused/deferred/error — never all-clear-shaped.
- All additions are **scoped `.manage__*` / `.inventory` extensions appended to styles.css** using the existing custom-property vocabulary; test-pinned IDs/classes (`#inventory-table`, `#inventory-rows`, `#inventory-summary`, `.metric__value`, `.srctag`, `data-panel="strategies"`) are preserved so existing e2e/boundary pins keep passing. After any rebase conflict: verify brace balance == 0 and origin/main's sheet is a verbatim prefix (known keep-both hazard).
- Evidence: before/after screenshots (dark + light theme) captured in the e2e run as part of the browser evidence.

### 2. Honest owner-string corrections
`python/atp_dashboard/inventory.py` `INVENTORY_FIELD_OWNERS` (:81-88): every owner must name a **still-`passes:false`** feature (rule, verified against `feature_list.json` + `architecture/runtime_services.json` deferred entries at implementation time): mode → SRS-EXE-001 (durable designation); container_status → the deferred Docker `StrategyContainerRuntime` owner (SRS-ORCH-002/SRS-ARCH-004 per `resource_profile_contract.deferred`); position_count → the SIM-003 runtime-feed owner (SRS-EXE-005/EXE-002 per feature notes); lifecycle_state → SRS-ORCH-005; asset_class → the strategy-registry owner named in runtime_services; pnl stays SRS-BT-004. Sync the module docstring/comments.

### 3. Tests (same commit; touches live_mode path → domain test mandatory)
- **L7 `tests/domain/test_dashboard_safety.py`**:
  - `test_promote_live_confirmation_guard_is_unchanged` — mounted runtime, POST promote-live without confirm → 428 CONFIRMATION_REQUIRED (mirror of :73).
  - `test_promote_live_affordance_uses_only_the_contract_route` — grep served app.js for the exact route constant + no `/dashboard/*` mutation; POST with confirm on the unwired runtime → 501 HANDLER_DEFERRED owner SRS-EXE-001 (mirror of :81).
  - Existing `test_dashboard_surfaces_are_read_only` must keep passing unchanged.
- **L1 `tests/unit/test_dashboard_inventory.py`**: new guard `test_every_deferred_owner_names_an_unbuilt_feature` — each `INVENTORY_FIELD_OWNERS` owner exists in `feature_list.json` with `passes:false` (the de-churn guard: a future producer flip forces the cell swap instead of leaving a stale owner tag). Existing owner-map test adapts automatically.
- **L4 `tests/boundary/test_dashboard_wiring.py`**: served assets carry the management affordance (route const in app.js, Manage column + designation status line in index.html) — mirrors the alerts served-assets pin.
- **L6 `tests/e2e/test_dashboard_refresh.py`** (ATP_RUN_E2E=1, ephemeral loopback + headless chromium, real cargo-built CLI seeded state — binds no shared resource, safe with siblings; plan approval covers running these per UI-1 precedent):
  - `test_ui_2_strategy_management_view_covers_every_ac_surface` — over `mount_default_dashboard` (production composition): all AC columns, real version identifiers, deferred owner tags, per-row control present, designation status line never all-clear-shaped.
  - `test_ui_2_promote_live_requires_explicit_confirmation` — `page.route` interception: single click arms (names the id) and fires **no** request; auto-disarm works; double-click fires exactly one POST to the encoded contract path with confirm; real runtime 501 rendered as deferred/owner, row never marked live.
  - `test_ui_2_promote_live_renders_refusals_and_success_honestly` — fulfilled 428 → refusal shown; fake 200 `{strategy_id,is_live,promoted_at}` → rendered verbatim in the status line while the Mode cell **stays deferred** (no fabrication from a response); fulfilled 503/network-abort → FAILED, control re-enabled, no stale armed state.

## Files
`python/atp_dashboard/assets/{index.html,app.js,styles.css}`, `python/atp_dashboard/inventory.py`, `tests/domain/test_dashboard_safety.py`, `tests/unit/test_dashboard_inventory.py`, `tests/boundary/test_dashboard_wiring.py`, `tests/e2e/test_dashboard_refresh.py`, `progress.d/session-UI-2.md` (chore).

No Python server/publisher changes needed (no new provider, no new channel, no PANEL_FRESH entry — the control lives inside the existing strategies panel; freshness semantics unchanged).

## Verification & gate
1. `./init.sh`; first action after approval: persist this plan to `progress.d/plan-UI-2.md`; `agent_pool.py heartbeat UI-2`.
2. `pytest tests/unit/test_dashboard_inventory.py tests/domain/test_dashboard_safety.py tests/boundary/ -q`; `ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_refresh.py -k "ui_2 or inventory or ui_1" -q` (regression: UI-1 umbrella + inventory e2es stay green with the new column); screenshots for evidence.
3. Walk the safety-pane checklist (false-all-clear class) before review: unknown ≠ all-clear; every degraded branch clears armed/status state; fail-closed parsing; bounded fetch.
4. `critic_check.py --staged` + `adversarial_review.py origin/main` (both APPROVE; record reviewer).
5. `tools/run_ci_locally.sh`, `cargo test --workspace`, `pytest -m "not integration and not e2e"`.
6. `block UI-2 --on SRS-EXE-001 SRS-BT-004 --reason "designation enforcement + key-metrics producers unbuilt"`, then `agent_pool.py integrate UI-2 --mode serialized`; park & take next.
