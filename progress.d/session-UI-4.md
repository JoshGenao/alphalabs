=== SESSION UI-4 ===
Date: 2026-07-21
Feature: UI-4 — dashboard kill-switch control with confirmation and status feedback
         (SRS-6 UI-4; traces SRS-SAFE-001 / SYS-44a and SRS-SAFE-002 / SYS-44b)
Outcome: serialized (passes stays false); blocked-on SRS-SAFE-001 + SRS-SAFE-002

## What I did

The genuinely-unbuilt surface (named by the contract's own deferred[] entry) was the
STATUS FEEDBACK half: there was no kill-switch read surface at all, no timeout or
notification leg anywhere in the UI, and the existing topbar affordance had no
in-flight guard, no fetch timeout and no identity binding.

1. **`python/atp_safety/audit.py`** — added `parse_liquidation_timeout_message`, the
   strict inverse of `build_liquidation_timeout_record`'s message, beside its writer
   (`LIQUIDATION_TIMEOUT_FIELDS` is the single shared vocabulary). All-or-nothing:
   anchored whole-string grammar, single non-space tokens, an ambiguity check against
   `<field>=` look-alikes, and closed vocabularies for every field the pane reasons
   about. Any drift → `None` (UNKNOWN), never a partial dict.

2. **`python/atp_dashboard/killswitch.py`** (new) — `KillSwitchStatusProvider` +
   `DurableKillSwitchStatusSource`, reading two durable artefacts the SAFE-001/002
   layer already writes: the `kill_switch_last_activation.json` record (via
   `atp_safety.state`) and the newest SRS-LOG-001 `LIQUIDATION_TIMEOUT` record. Emits
   a six-rung `sequence` (halt → cancellation → liquidation → [timeout, notification]
   → disconnect) plus a receipt and an order table. The two sources fail independently.

3. **`python/atp_dashboard/server.py`** — `KILL_SWITCH_SNAPSHOT_PATH =
   "/dashboard/api/kill-switch"`, opt-in `kill_switch=` kwarg on `mount_dashboard`,
   always composed in `mount_default_dashboard` with an opt-in SOURCE
   (`ATP_KILL_SWITCH_STATE` / `ATP_KILL_SWITCH_LOG_DIR`). GET-only; no WS channel
   (the AsyncAPI contract declares none, and publishing on an undeclared channel
   would be fabrication at the transport layer).

4. **Front-end** (`assets/index.html` + `styles.css` + `app.js`, `/frontend-design`) —
   a "Kill Switch — Liquidate Sequence" panel built entirely from the existing console
   design tokens (no external asset; light theme and reduced-motion come free). The
   load-bearing idea is the SEQUENCE RAIL: the rail segment and diamond node between
   rungs carry the phase state, and UNKNOWN is drawn with the dashboard's established
   45° deferred HATCH — so an unobservable leg is visually impossible to misread as
   green. The confirm control drains a pill-shaped countdown stroke over the 5 s arm
   window; FIXTURE evidence wears a hazard-striped drill badge. Screenshot-verified in
   observed / armed / unconfigured / degraded states and in the light theme.

5. **Control hardening** (UI-2 pattern): both triggers (topbar `#killswitch-btn` and
   the panel's `#ks-btn`) drive ONE state machine — `killInFlight` guard checked
   first, `AbortSignal.timeout`, the staged confirmation consumed on fire, auto-disarm
   restoring the resting caption, and identity binding (a 2xx designates an activation
   only when the runtime echoes a non-empty `activation_id`; a later snapshot naming a
   different one renders a MISMATCH naming both). `KILL_SWITCH_ROUTE` stays the only
   `/api/v1/kill-switch` target — no dashboard-namespaced mutation was added.

### Key decisions (fail-closed, all pinned by tests)

* **Unknown ≠ never-activated.** `activated` is tri-state; `False` only when a readable
  state dir genuinely holds no record. Corrupt/unreadable/unconfigured → `None`.
* **A record must substantiate an activation.** `load_last_activation` only proves the
  file parsed as an object; `_validated_activation` additionally requires a real
  `activation_id` and three-way identity agreement across record/report/response, so
  one activation's evidence can never appear under another's receipt.
* **The SYS-44b legs cannot resolve, by construction.** The timeout record correlates
  by ORDER id and the activation report carries no id for the liquidations it
  submitted — no key links them. The pane shows the record verbatim, explicitly
  labelled "NOT correlated to this activation", and leaves both rungs UNKNOWN.
  Resolving them is SRS-SAFE-002's to enable (carry an activation id into the record).
* **Disconnect needs its pinned proof.** A SUCCEEDED disconnect CALL without a strict
  boolean `ib_gateway_disconnected` is UNKNOWN, not "IB gateway disconnected".
* **Client-side too.** A rung renders resolved only when `leg.value === leg.status`;
  shape drift, 404, non-OK, malformed JSON, abort/timeout all route to one clear that
  blanks the whole rail. The server cannot talk the client into a green leg.

## What I tested (per feature_list step)

* **Step 1** — `./init.sh` → "✓ Environment ready" (worktree-local, ATP_DEV_PORT=3010).
  Browser automation: PASS locally via Playwright over an ephemeral-port runtime
  (12 UI-4 e2e tests). NOT operator-witnessed → see Step 4.
* **Step 2** (navigate the UI-4 workflow) — PASS.
  `ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_refresh.py -k ui_4` → 12 passed.
  Covers: every AC surface rendered, unconfigured pane never all-clear, confirmation
  required (no POST on the first click), arm-window expiry restoring the resting
  caption, 501 refusal rendered with its owner, partial failure never dressed as
  success, a 200 without an activation_id refused, degraded/404/shape-drift/deferred-cell
  payloads each clearing every leg, and activations serialized behind a held route.
* **Step 3** (activate and see cancellation / liquidation / timeout / notification /
  disconnect status) — **PARTIAL, by design.** The four SRS-SAFE-001 legs render from a
  real durable record; the two SRS-SAFE-002 legs render the record's content but stay
  UNKNOWN because nothing correlates it to an activation. And no composition wires the
  activate handler, so on a real runtime the POST is the honest 501 HANDLER_DEFERRED
  (owner SRS-SAFE-001). A genuine end-to-end activation is not reachable this session.
* **Step 4** (trace to SRS-SAFE-001/002, leave passes false until browser evidence) —
  PASS. `architecture/runtime_services.json`'s deferred[] entry now states exactly what
  landed and the two things that remain (live evidence; SYS-44b leg correlation).
  UI-4 stays `passes:false`.

Supporting: `pytest -m "not integration and not e2e"` → 4023 passed, 4 skipped.
`cargo test --workspace` → green. `cargo fmt --check` / `cargo clippy -D warnings` → clean.
`kill_switch_check` / `kill_switch_timeout_check` / `architecture_check` / `rest_api_check` /
`cli_check` / `operator_interface_runtime_check` / `network_binding_check` /
`log_record_check` → all PASS. mypy: 0 errors in the files this session touched (68
pre-existing errors elsewhere, unchanged). `ruff check` clean; `ruff format` clean on
every file touched here.

**CI-mirror caveat (pre-existing, not owned by UI-4):** `tools/run_ci_locally.sh` aborts
at `ruff format --check .` because 13 files are already unformatted on `origin/main`
(`python/atp_reliability/*`, `tools/deployment_check.py`, `tests/domain/test_restart_recovery.py`,
…) — verified against `git show origin/main:<path>`. Only this session's own files were
formatted; the repo-wide reformat belongs to the toolchain-pin change, not a feature PR.
Every remaining CI step was run individually and is green.

## Critic verdicts

* deterministic (`tools/critic_check.py --staged`): **APPROVE** — no findings.
* judgment (`tools/adversarial_review.py origin/main`, reviewer=**codex**): **APPROVE**
  at round 6. Rounds 1–5 each found a real defect, all fixed with regression tests:
  1. disconnect leg resolved SUCCEEDED without the pinned gateway flag;
     + unrelated whole-file JSON reformat churn (reverted to a 2-line surgical edit).
  2. a readable-but-drifted `{}` record read as `activated: true`.
  3. identity validation was partial (report id unchecked, response id optional).
  4. the SYS-44b legs were resolved from an order-correlated record that cannot be
     tied to the displayed activation.
  5. the timeout-log parser accepted a trailing tail, which would have suppressed the
     MANUAL RESOLUTION REQUIRED warning (fail-open on the loudest SYS-44b signal).

## Resume / next

Blocked-on **SRS-SAFE-001** (blocked-on SRS-EXE-001 + SRS-EXE-002) and **SRS-SAFE-002**.
To flip UI-4:

1. Compose `atp_safety.wire_kill_switch` onto the operator runtime (a real backend, not
   the fixture CLI) and set `ATP_KILL_SWITCH_STATE` / `ATP_KILL_SWITCH_LOG_DIR` in the
   dashboard's environment. The 200 path is already pinned by e2e including the
   identity binding — no client change needed.
2. SRS-SAFE-002: carry an activation id into `build_liquidation_timeout_record` (extend
   `LIQUIDATION_TIMEOUT_FIELDS` — writer and reader are one module, and the round-trip
   test will hold you to it). Then `_timeout_leg` / `_notification_leg` can resolve
   against the displayed activation instead of rendering uncorrelated evidence; drop
   `_UNCORRELATED` and re-point the two rungs at `SOURCE_TIMEOUT_RECORD`.
3. SRS-NOTIF-001 for live email/SMS transports (the `FIXTURE`→`LIVE` tier badge flips
   itself off the record).
4. Re-run the 12 UI-4 e2es in `tests/e2e/test_dashboard_refresh.py` with the operator
   witnessing the browser session, then close with `--verified`.

Do NOT rebuild the pane or the control — wire the producers and swap the cells.
