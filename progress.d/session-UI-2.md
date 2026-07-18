=== SESSION UI-2 ===
Date: 2026-07-18
Feature: UI-2 — dashboard strategy management view (SRS-6 UI-2, P1; traces SRS-ORCH-004,
SRS-EXE-001; SYS-2c/2d, NFR-S2, AC-15)
Outcome: serialized — the management view is built and browser-verified; passes stays false
(blocked-on SRS-EXE-001 + SRS-BT-004: the real designation handler and the key-metrics
producers are unbuilt, so the AC's live-designation effect and metric VALUES cannot exist yet).

CONTEXT (what exploration established before building):
- The viewing half already existed (SRS-UI-002, serialized): inventory panel, deployed version
  REAL via orch005_rollback_cli list, other AC columns honest deferred cells.
- NO deferred cell could flip live: ORCH-001/002/003/004 + SIM-003 are passes:true but landed
  as in-process gates/types/demo CLIs with no queryable read surface (no container-status CLI,
  no persisted mode/asset registry, sim003_ledger_cli is fixture-only, LiveDesignation is
  in-memory + Clone-forbidden). The only real producer remains orch005_rollback_cli list.
- The genuinely-unbuilt named surface was the MANAGEMENT leg: live designation with explicit
  confirmation. The contract route POST /api/v1/strategies/{id}/promote-live already existed
  (requires_confirmation=True; runtime 428 without confirm at rest_server.py; 501
  HANDLER_DEFERRED owner SRS-EXE-001 via contract.py) — no new server mutation surface needed.

WHAT I BUILT (feat c32c27a + adversarial fixes 0849845/442d5b1/3bb1904/6e747a9/c825e6c/f88cefc):
- assets/index.html: panel retitled "Strategy Management"; Manage column; designation readout
  (#designation-state/#designation-status, aria-live) defaulting to "live designation state —
  awaits SRS-EXE-001" (dashed deferred framing; never all-clear-shaped).
- assets/app.js: per-row arm-then-confirm PROMOTE LIVE control mirroring the kill-switch flow —
  promoteLiveRoute(id) = contract route with encodeURIComponent(id) + ?confirm=true; 5s arm
  window w/ auto-disarm; one staged candidate at a time; disarm-on-upsert; bounded fetch
  (AbortSignal.timeout); verbatim honest rendering (501 → REFUSED + owner SRS-EXE-001; 428/5xx
  → refusal by type; network fail → FAILED outcome-unknown; 200 designates ONLY when is_live
  === true AND body.strategy_id === the confirmed id); global in-flight guard serializes
  designation requests (AC-15). Inventory row lifecycle: per-source (WS vs REST poll) burst
  state + one global monotonic generation stamp — completed bursts sweep only older rows;
  zero/unavailable/malformed summaries clear rows immediately; a source contradicting its own
  summary fails closed; every degraded poll branch (404 / non-OK / malformed / unreachable)
  clears actionable rows.
- assets/styles.css: /frontend-design pass on the "ATP · Mission Control" system — command-deck
  panel, staggered row reveal (reduced-motion safe), armed-row focus choreography (siblings
  recede via filter — NOT opacity, the rise animation's forwards fill owns opacity), amber→red
  armed pulse, honest 5s countdown bar, designation beacon chip. All scoped .manage__*
  appends; test-pinned IDs/classes preserved.
- inventory.py: INVENTORY_FIELD_OWNERS retagged so every deferred cell names still-deferred
  work (mode→SRS-EXE-001, asset_class→SRS-API-001, container_status→SRS-ORCH-002,
  lifecycle_state→SRS-ORCH-005, position_count→SRS-SIM-004, pnl→SRS-BT-004); docstring synced.
- Tests: domain (promote-live 428 guard unchanged; affordance uses ONLY the contract route +
  501 owner pin), unit (owner-honesty guard: each owner passes:false OR named in a
  runtime_services.json deferred leg — flips force the cell swap), boundary (served assets
  carry the affordance), e2e ×7 new (AC umbrella over mount_default_dashboard; explicit-
  confirmation arm/fire/428/501; refusals+success honesty incl. mismatched strategy_id;
  removed-strategy loses its control (REAL state-file shrink); degraded-poll clearing;
  malformed WS summary via route_web_socket; cross-source interleaving keeps healthy rows;
  serialized requests via held route).

WHAT I TESTED (per step):
  Step 1 (init): PASS — ./init.sh → "✓ Environment ready" (dev deps manual: pip install -r
    requirements-dev.txt into .venv; init.sh skips it).
  Step 2 (browser automation): PASS — ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_refresh.py
    → 17 passed (headless chromium, ephemeral loopback, real cargo-built CLIs, seeded state;
    binds no shared resource — run with siblings leased per UI-1 precedent, authorized via plan
    approval). Screenshots captured (resting deck + armed choreography, dark console).
  Step 3 (AC): PARTIAL by producer availability — active strategies + deployed code version
    REAL; mode/asset/container/key-metrics displayed as honest deferred cells naming
    still-deferred owners (their values need SRS-EXE-001/API-001/ORCH-002/SIM-004/BT-004);
    live designation REQUIRES explicit confirmation end-to-end (client arm-then-confirm +
    server 428) and fails closed at 501 while SRS-EXE-001 is unbuilt → passes stays false.
  Step 4 (trace + serialized): PASS — UI behavior traces SRS-ORCH-004 (version rendering) and
    SRS-EXE-001 (designation contract); browser evidence captured; integrated serialized.
  Gate: pytest -m "not integration and not e2e" 3816 passed (full) / 1778 fast suites re-run
    after every fix round; cargo test --workspace green; tools/run_ci_locally.sh green;
    node --check + ruff + css-brace-balance clean.

Critic verdicts:
  deterministic: APPROVE — no findings (every commit).
  judgment (adversarial_review.py, reviewer=codex): 7 rounds. r1 stale rows keep promote
  controls after strategy removal → generation reconciliation + shrink e2e; r2 degraded poll
  branches fail open (+ latent malformed-snapshot false "no strategies deployed") → all
  branches clear rows; r3 malformed WS summary treated healthy → fail-closed summary
  validation + route_web_socket e2e; r4 success not bound to confirmed strategy_id →
  identity check + mismatch e2e; r5 row resurrection after zero/error summary + unserialized
  POSTs → open-summary gating + global in-flight guard; r6 shared counters made cross-source
  interleaving read as corruption → per-source burst state + interleaving e2e; r7 APPROVE.
  (Every finding was one class: stale-or-unknown truth left ACTIONABLE, the control-surface
  sibling of UI-1's false-all-clear class.)

Resume / next (flip path — wire producers, don't rebuild):
- SRS-EXE-001 lands the real promote-live handler + durable designation state → the control's
  200 path goes live as-is (response contract already pinned by e2e incl. identity binding);
  swap the mode cell + designation readout to the real designation source; re-run the UI-2
  e2es. SRS-BT-004 → P&L/positions cell swaps (SRS-UI-002 extension contract).
- The owner-honesty unit guard (test_every_deferred_owner_names_work_that_is_actually_still_
  deferred) will trip when any named owner flips — that trip IS the cell-swap reminder.
- blocked-on recorded: SRS-EXE-001, SRS-BT-004.
