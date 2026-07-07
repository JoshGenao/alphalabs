=== SESSION SRS-UI-002 ===
Date: 2026-07-06
Feature: SRS-UI-002 — display active strategy inventory in the dashboard (SyRS SYS-41 / SYS-79)
Outcome: serialized — the inventory panel + STRATEGY_STATE feed are built and BROWSER-VERIFIED
(headless-chromium e2e run solo this session; no siblings were leased), with the deployed code
version REAL end to end; passes stays false because five of the AC's seven per-strategy fields
render as honest deferred cells until their producer features land (the AC wants the VALUES shown,
not just the columns).

CONTEXT: UI-001's dashboard (built, serialized) owned only PNL/METRICS/HEARTBEAT; STRATEGY_STATE
was a declared-contract channel with NO publisher anywhere, and deployment_version_contract's
deferred[] explicitly assigned "dashboard wiring (SYS-41) that renders the version_identifier on
the active-strategy inventory" to SRS-UI-002. No active-strategy registry exists; the one genuine
per-strategy record is the SRS-ORCH-005 deployment snapshot (strategy id + current/previous
DeployedVersion).

WHAT I DID:
- crates/atp-orchestrator/src/bin/orch005_rollback_cli.rs: NEW `list` subcommand — every recorded
  strategy's current + retained previous version, id-sorted, indexed key:value proof lines
  (strategy_count / strategy.<i>.id|current|previous); missing snapshot fails closed (never an
  empty inventory); refuses a stray --strategy. Bin test added (sorted rows, fail-closed missing,
  flag refusal).
- python/atp_dashboard/inventory.py (NEW): StrategyInventorySource protocol +
  RollbackSnapshotInventorySource (shells `orch005_rollback_cli list` — single format owner, the
  dashboard never parses the snapshot file; injectable runner + timeout; fail-CLOSED parse: count/
  row mismatch or missing id/current = unavailable, never a partial inventory) +
  StrategyInventoryProvider (inventory_snapshot() REST body + strategy_state_events(): one summary
  event — freshness ticks even at zero strategies or unavailable source — plus one event per
  strategy with keys covering the atp_ws STRATEGY_STATE payload_fields). Honesty per the UI-001
  convention: version_identifier/deployment_version_hash/previous are LIVE
  (live:orch005_rollback_cli); mode/asset_class/container_status/lifecycle_state/position_count/
  pnl are {"value":null,"data_source":"deferred:<owner>"} cells (ORCH-004/ORCH-001/SIM-003/BT-004);
  an unreadable snapshot is an explicit ok:false + reason.
- publisher.py: DashboardPublisher gains optional inventory; when mounted it claims STRATEGY_STATE
  (5 s contract cadence) and publishes the summary + per-strategy events per tick. server.py:
  mount_dashboard(..., inventory=None) — composition-time OPT-IN registering
  GET /dashboard/api/strategies; a bare UI-001 mount claims no inventory channel and serves no
  inventory route (pinned in the domain safety test).
- assets: 5th panel (panel--wide) "Strategy Inventory" — a 7-column table (Name / Mode / Asset /
  Container / Deployed version / P&L / Positions) with per-cell deferred rendering (— + owner tag),
  a summary line (count / unavailable / not-mounted states), a panel freshness dot, STRATEGY_STATE
  subscription + WS row upserts keyed by strategy_id, and a /dashboard/api/strategies first-paint
  poll. STRATEGY_STATE is deliberately NOT part of the NFR-P2 gauge (opt-in channel; a bare UI-001
  mount must not read as an SLA breach) — the panel's own dot reports it honestly.
- tests: unit/test_dashboard_inventory.py (7 — real version parse, deferred-cell owners, contract
  payload_fields coverage, unavailable + drifted-CLI fail-closed); domain/test_dashboard_safety.py
  (bare mount never claims STRATEGY_STATE; inventory mount claims it and stays honest over an
  unreadable source); e2e/test_dashboard_refresh.py + inventory scenario (REAL bin, seeded
  snapshot: 2 strategies render with real version identifiers, deferred cells show —, summary
  correct, panel dot reaches fresh ≤ budget).
- architecture/runtime_services.json: deployment_version_contract deferred leg updated — the
  SYS-41 dashboard rendering LANDED via SRS-UI-002; the remaining leg (production in-process
  DeployedVersionRegistry caller) stays with SRS-API-001. README + package exports updated.

WHAT I TESTED (per AC step):
  Step 1 (init): ./init.sh -> "✓ Environment ready".
  Step 2 (browser automation + REST/WS): ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_refresh.py
    -> 2 passed in a REAL headless chromium (run solo — the pool showed no sibling leases): the
    UI-001 regression e2e stays green with the 5th panel present, and the new inventory e2e proves
    both seeded strategies render with their real version identifiers, deferred cells display as
    explicit placeholders, the summary reads "2 strategies", and the panel freshness dot reaches
    "fresh" within budget. REST: GET /dashboard/api/strategies serves ok:true + rows (boundary/
    domain tests + the loopback e2e); WS: STRATEGY_STATE events delivered (client renders them).
  Step 3 (AC fields): name (= strategy id, real) + deployed code version (REAL, SYS-79 identifier)
    are populated; mode / asset class / container status / P&L / position count are DISPLAYED as
    columns with honest deferred cells naming ORCH-004 / ORCH-001 / SIM-003 / BT-004 — the reason
    passes stays false (serialized): the AC wants those values shown, and their producers are
    unbuilt features.
  Step 4 (evidence): pytest -m "not integration and not e2e" -> 3022 passed; dashboard suites
    (unit+boundary+domain) 42 passed; cargo orchestrator suites green incl. the new list test;
    ruff + node --check clean.
Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings (both commits).
  judgment (fresh-context sub-agent critic; Codex on usage cooldown): ROUND 1 = BLOCK with two
  empirically-confirmed defects + one warn — (1) unguarded int() in _parse_rows let a non-integer
  strategy_count escape as ValueError and KILL the shared publisher ticker (starving
  PNL/METRICS/HEARTBEAT silently); (2) the bin accepted \r in strategy ids, which forged whole
  proof lines through Python's str.splitlines(); (3) the inventory subprocess on the shared ticker
  could starve the 1s channels up to its 10s timeout. ALL FIXED in d9cb0f7: guarded parse
  (ValueError -> InventoryUnavailable; negative count refused; '\n'-only split), bin refuses
  control/U+2028/U+2029 chars at parse AND save (write side = strict superset of every downstream
  splitter; snapshot byte-identical on refusal), inventory isolated on its own guarded ticker
  thread + every tick exception-guarded, with empirical regression tests for each. ROUND 2 =
  APPROVE, zero findings — the reviewer re-probed everything (extended fuzz: unicode-digit
  indices, whitespace/signed/underscore counts, ALL splitlines separators incl. FS/GS/RS/NEL/LS/PS,
  NUL, over-refusal of legit ids; measured PNL inter-tick gaps ~1.0s under an 8s-sleeping
  inventory; confirmed the guard swallows only Exception per-tick with the freshness dot + REST
  ok:false surfacing failures honestly) and could not construct a fabrication, escaped exception,
  thread kill, starvation, or forged proof line.
Resume / next: the flip needs the five deferred field producers wired into
  atp_dashboard.inventory's rows: mode/asset_class/lifecycle (SRS-ORCH-004 registry),
  container_status (SRS-ORCH-001/ARCH-004), position_count (SRS-SIM-003 live wiring), pnl
  (SRS-BT-004 SYS-70 feed — join via the per-strategy PNL channel; STRATEGY_STATE deliberately
  carries no pnl field). Each is a provider-cell swap, not a rebuild (the UI-001 extension
  contract). Then re-run the inventory e2e for the flip evidence.
