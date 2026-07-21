# UI-4 — Dashboard kill-switch control with confirmation and status feedback

## Context

`UI-4` (claimed this session, worktree `alphalabs-wt-UI-4`, branch `agent/UI-4`) requires:

> The dashboard shall provide a kill-switch control with confirmation and status feedback.
> **AC:** User can activate kill switch and see **cancellation, liquidation submission, timeout,
> notification, and disconnect** status. (`docs/SRS.md:267`; traces SRS-SAFE-001, SRS-SAFE-002)

**What already exists** (do not rebuild):
- Full activation machinery: `crates/atp-execution/src/kill_switch.rs` (phase order: halt → cancel →
  liquidate → disconnect), `crates/atp-orchestrator/src/bin/safe001_kill_switch_cli.rs` (prints
  `report:{json}`), `safe002_liquidation_timeout_cli.rs` (prints `outcome:{json}`).
- Operator layer `python/atp_safety/`: `handlers.py` (`KillSwitchActivateHandler` /
  `KillSwitchStatusHandler`), `state.py` (durable `kill_switch_last_activation.json`),
  `audit.py` (`ACTIVATION` / `HALTED` / `LIQUIDATION_TIMEOUT` SRS-LOG-001 records),
  `timeout.py`, `wiring.py`.
- Contract route `POST /api/v1/kill-switch` (`python/atp_api/routes.py:183-200`, `requires_confirmation`).
- A **minimal** topbar affordance: `assets/index.html:42-50` + `assets/app.js:1165-1242`
  (arm-then-confirm, POSTs `KILL_SWITCH_ROUTE`, one-line summary).

**What is genuinely unbuilt** — and is exactly UI-4, per the contract's own deferred entry
(`architecture/runtime_services.json → kill_switch_activation_contract.deferred[4]`:
*"the full control with cancellation / liquidation-submission / timeout / notification / disconnect
status feedback is UI-4"*):
1. **No status read surface.** `KillSwitchStatusHandler` is CLI-only; there is no
   `/dashboard/api/kill-switch`. Control feedback today is ephemeral (a page reload loses it).
2. **No timeout or notification legs anywhere in the UI** — the two SRS-SAFE-002 AC legs.
3. **The control lacks the UI-2-grade hardening**: no in-flight guard, no fetch timeout, no
   identity binding of the 200 to the returned `activation_id`.

Nothing composes `wire_kill_switch` in production, so on the real runtime the POST target is
`501 HANDLER_DEFERRED owner SRS-SAFE-001`. That refusal must render honestly, and the status pane
must **never** show an all-clear for unknown state — this is the highest-stakes false-all-clear
surface in the system (a fabricated "IB DISCONNECTED ✓" is a lie about a liquidation).

**Expected outcome:** `--mode serialized`, `passes:false`, `block UI-4 --on SRS-SAFE-001 SRS-SAFE-002`.
The AC needs a *real* activation (SAFE-001 handler over a live IB path — blocked on EXE-001/EXE-002)
and live timeout transports (SAFE-002 + NOTIF-001), plus browser evidence (step 1/4 explicitly say
"leave passes false until browser evidence is captured"). Never fake the green.

---

## Implementation

### 1. Symmetric reader for the SYS-44b durable record — `python/atp_safety/audit.py`

`build_liquidation_timeout_record` packs the timeout/notification facts into `LogRecord.message`
as `k=v` pairs (`audit.py:139-158`). Add its **inverse next to it** (read↔write symmetry — a
reader that drifts from its writer is the classic fail-open):

```python
LIQUIDATION_TIMEOUT_FIELDS = ("disposition","transports","order_id","symbol","side",
                              "quantity","operator_alert","liquidation_cancel",
                              "ib_disconnect","manual_resolution_required")

def parse_liquidation_timeout_message(message: str) -> dict[str, str] | None:
    """Strict inverse of build_liquidation_timeout_record's message.
    Returns None (never a partial dict) if ANY field is missing/duplicated —
    an unparseable record is UNKNOWN, never an all-clear."""
```

Pinned by a round-trip unit test: writer → reader → every field recovered verbatim.

### 2. New dashboard provider — `python/atp_dashboard/killswitch.py`

Follows the `alerts.py` (pure REST pane) + `inventory.py` (source Protocol + `Unavailable`) shapes.

- `KILL_SWITCH_STATUS_OWNER = "SRS-SAFE-001"`, `KILL_SWITCH_TIMEOUT_OWNER = "SRS-SAFE-002"`,
  `KILL_SWITCH_NOTIFY_OWNER = "SRS-NOTIF-001"`.
- `KillSwitchStatusUnavailable(Exception)`.
- `KillSwitchStatusSource` Protocol → `status_snapshot() -> dict`.
- `DurableKillSwitchStatusSource(state_dir=..., log_path=None)`:
  - `atp_safety.state.load_last_activation(state_dir)` — `LastActivationCorruptError` →
    `KillSwitchStatusUnavailable` (**fail closed**: corrupt ≠ never-activated, mirroring
    `handlers._load_guard`).
  - `atp_logging.persistence.read_records(log_path, source=Source.KILL_SWITCH,
    event_type="LIQUIDATION_TIMEOUT", newest_first=True, limit=1)` → `parse_liquidation_timeout_message`.
- `KillSwitchStatusProvider.kill_switch_snapshot() -> dict`, five legs + identity:

```
ok: bool                     # false on any unavailable/unreadable/corrupt path
reason: str | null           # verbatim why, when ok:false
activated: true | false | null      # null when the source cannot be read — NEVER false
activation_id / activated_at        # deferred cell when unknown
cancellation      : {value, data_source, status, orders: [...] | null}
liquidation       : {value, data_source, status, orders: [...] | null, within_nfr_p3, budget_ms}
timeout           : {value, data_source, disposition, transports, manual_resolution_required}
notification      : {value, data_source, operator_alert, email, sms}
disconnect        : {value, data_source, status}
halt              : {value, data_source, engines_halted, audit_recorded, halted_log_latency_ms,
                     halt_observability_budget_ms}
```

Honesty rules (each pinned by a test):
- Unknown → `value: null` + `data_source: "deferred:<owner>"`. Never `[]` where `null` means
  unknown (`orders: null`, not `[]` — the UI-1 alerts lesson).
- `ran_clean` / `within_nfr_p3` / `ib_gateway_disconnected` are surfaced **only** when strictly
  `bool`; anything else → `UNKNOWN`. A real `false` renders as a loud failure, not a blank.
- `transports == "FIXTURE"` is carried through verbatim and rendered as a drill badge — fixture
  evidence never masquerades as live SYS-44b history.
- No state dir configured → `ok:false` + reason, all legs deferred, `activated: null`.

Export from `python/atp_dashboard/__init__.py`.

### 3. Route + composition — `python/atp_dashboard/server.py`

- `KILL_SWITCH_PATH = "/dashboard/api/kill-switch"` alongside the existing path constants (:57-92).
- `mount_dashboard(..., kill_switch: KillSwitchStatusProvider | None = None)` →
  `runtime.register_meta_route(KILL_SWITCH_PATH, kill_switch.kill_switch_snapshot)` (GET-only).
- `mount_default_dashboard`: **always composed** (like `alerts`), reading optional
  `ATP_KILL_SWITCH_STATE` (state dir) and `ATP_KILL_SWITCH_LOG_DIR`; unset → honest `ok:false`.
- **No WS channel.** `atp_ws.channels.Channel` has exactly 8 members and no kill-switch channel;
  `runtime.register_publisher` rejects unknown channels. REST-poll only, per the `alerts.py`
  precedent. `DashboardPublisher` is untouched.

### 4. Front-end — design (`/frontend-design`)

**Aesthetic direction: "abort-sequence telemetry."** The dashboard already commits to a dark
instrument console (`styles.css:1-101`) — lime `--accent: #b6ff3a` as the healthy signature, `--mono`
tabular readouts, film-grain overlay, radial-gradient atmosphere, staggered `rise` panel entrance via
`--i`, hatched `.swatch--deferred` for unknown, `[data-theme]` + `prefers-color-scheme` light variant.
This panel does **not** introduce a second aesthetic; it is that console at its most severe — the one
surface where the lime signature is deliberately absent and red/hazard owns the frame. Everything is
built from the existing CSS variables (self-contained: **no external fonts, no CDN, no images** —
SEC-002 loopback posture), so the light theme comes free.

**The one memorable thing — the sequence spine.** The five AC legs are not a list; they are the real
SRS-SAFE-001 phase order rendered as a numbered ladder down a vertical rail, with the SRS-SAFE-002
escalation branching off it:

```
 ①━ HALT PAPER ENGINES      12 / 12 transitioned        ✓ 0.4s / 1.0s budget
 ┃
 ②━ CANCELLATION            4 resting orders            ✓ SUCCEEDED
 ┃
 ③━ LIQUIDATION SUBMISSION  6 market orders             ✓ 1 842 ms / 5 000 ms  ▓▓▓▓▓▓░░░░
 ┣╌╌ ④ TIMEOUT              ▨ UNKNOWN — deferred:SRS-SAFE-002
 ┃    ⑤ NOTIFICATION        ▨ UNKNOWN — deferred:SRS-NOTIF-001
 ④━ DISCONNECT              ▨ UNKNOWN — deferred:SRS-SAFE-001
```

- Nodes are rotated-square diamonds (`transform: rotate(45deg)`), not dots — distinct from the
  existing `.chip__dot` circles so the rail reads as a *sequence*, not a status cluster.
- **The rail segment between nodes carries the state**: solid `--ok` when resolved clean, `--bad`
  when FAILED, and the established 45° `repeating-linear-gradient` **hatch** (the system's existing
  "deferred/unknown" idiom, `styles.css:262`) when unknown. Hatching makes unknown visually
  *impossible* to misread as green — the fail-closed rule enforced in pixels, not just in the DOM.
- The SAFE-002 legs branch off the rail with a dashed connector, so their separate ownership is
  legible at a glance.

**The countdown ring.** The panel-level confirm reuses the existing `.pulse` SVG ring motif
(`index.html:56-62`) at small scale: arming starts a 5 s `stroke-dashoffset` drain around the CONFIRM
button, so the operator *sees* the arm window closing. One inline SVG, one `@keyframes`, CSS-driven —
it reads as the same instrument family, not a new widget.

**Hazard framing.** Resting: the panel is quiet, `--line` border, muted. Armed: a hazard-striped top
rail (45° `repeating-linear-gradient` in `--bad`) animates in, the panel border goes
`color-mix(in srgb, var(--bad) 55%, transparent)`, and a single slow sweep gradient crosses the
header. Fired: the border latches red and the activation id sets in oversized tabular mono
(`font-variant-numeric: tabular-nums`, wide tracking) as the panel's display element — the receipt.
All motion is one `@keyframes` each and is already neutralised by the existing
`prefers-reduced-motion` block (`styles.css:286-289`).

**Tier badge.** `FIXTURE DRILL` renders as a hazard-striped pill; `LIVE` as a solid `--bad` pill.
Fixture evidence can never be mistaken for live SYS-44b history — the honesty rule made visual.

**Restraint where it counts.** The per-order table (`#ks-orders`) reuses the existing `.inventory`
table styling verbatim (`styles.css:319-327`) so the data grid is identical across panels; the
latency-vs-budget meter reuses the established pill/tabular-figure vocabulary rather than inventing a
new chart palette. One system, not two.

**Markup/CSS placement**
- `assets/index.html` — new `<section class="panel panel--wide" data-panel="killswitch" style="--i:N">`
  titled "Kill Switch — Liquidate Sequence", with the rail (`#ks-rail`), the identity/receipt block,
  the confirm control + ring, the tier badge, and `#ks-orders`. The topbar affordance stays exactly
  where it is (contract-pinned).
- `assets/styles.css` — one scoped `.ks-*` block **appended** after the existing `.killswitch` block
  (:293-311). *Rebase note: append, never interleave — a keep-both merge in this file has silently
  dropped a closing `}` before (UI-1).*
- Accessibility: `role="group"` + `aria-live="assertive"` on the status readout (matching the existing
  affordance), `:focus-visible` inherits the global accent outline (:291), every state distinguished
  by **glyph + text**, never by colour alone (the hatch/diamond/✓/▨ vocabulary carries it).
- Responsive: rail left / table right at wide, collapsing to a single column at the existing 760px
  breakpoint (`styles.css:279-284`).

`assets/app.js` — upgrade the block at :1165-1242, mirroring the UI-2 promote-live pattern
(:524-638):
- **Keep the literal** `const KILL_SWITCH_ROUTE = "/api/v1/kill-switch?confirm=true";` — pinned by
  `tests/domain/test_dashboard_safety.py:98` and `tests/boundary/test_kill_switch_wiring.py:322`.
- `killInFlight` guard (checked *first* in the click handler; the button and any re-fire inert
  until it settles) + `AbortSignal.timeout(KILL_FETCH_TIMEOUT_MS)`.
- **Identity binding**: a 200 designates the pane only when `typeof body.activation_id === "string"`
  and it is non-empty; the legs are then filled from that response and stamped with that id.
  A mismatch against a subsequently polled snapshot renders an error naming both ids.
- Refusals verbatim: `REFUSED <status> <err.type>` + `err.detail.owner` (501 → `SRS-SAFE-001`).
- Auto-disarm restores the **resting** caption (no orphan "armed" caption).
- `pollKillSwitch()` on the existing `POLL_MS` ticker → `renderKillSwitch(snap)`.
  **Every** degraded branch — non-OK, 404, malformed JSON, `ok:false`, missing legs, abort/timeout —
  clears all five legs to `UNKNOWN` and marks the pane degraded. No branch may leave a stale
  green leg on screen.

### 5. Contract prose — `architecture/runtime_services.json`

Amend `kill_switch_activation_contract.deferred[4]`'s `what` to state that the rich control + status
pane landed and that what remains deferred is *live activation evidence* (SAFE-001 handler wiring,
SAFE-002 transports, browser sign-off). **Keep the literal token `UI-4`** — `tools/kill_switch_check.py:317-325`
requires `deferred[]` to name it, and UI-4 stays `passes:false`.

---

## Tests

| Layer | File | Cases |
|---|---|---|
| L1 unit | `tests/unit/test_dashboard_killswitch.py` (new) | no state dir → `ok:false`/`activated:null`/all-deferred; no activation → `activated:false`, `orders:null`; real record → five legs from the report; **corrupt record → `ok:false`, never "never activated"**; non-bool `within_nfr_p3`/`ib_gateway_disconnected` → `UNKNOWN`; timeout record present → disposition + `transports:"FIXTURE"` badge; unparseable message → `UNKNOWN`, not all-clear |
| L1 unit | `tests/unit/test_kill_switch_audit.py` (extend, or new) | `build_liquidation_timeout_record` → `parse_liquidation_timeout_message` round-trip; missing/duplicated field → `None` |
| L4 boundary | `tests/boundary/test_dashboard_killswitch_wiring.py` (new) | mount serves `GET /dashboard/api/kill-switch` 200; POST/PUT/DELETE → 404/405; **absent provider → 404** (composition opt-in honesty); no new WS publisher claimed; unreadable state dir → 200 with `ok:false` |
| **L7 domain** *(mandatory — `kill_switch` + `safety` path)* | `tests/domain/test_dashboard_kill_switch_status.py` (new) | pane never renders/serves an all-clear for unknown state (every deferred cell `value is None`); `orders`/`activated` are `null` not `[]`/`false` when unreadable; the `/dashboard` namespace stays read-only with the pane mounted; the app.js control still targets **only** `KILL_SWITCH_ROUTE`; the 428 confirmation guard and the 501 `HANDLER_DEFERRED owner SRS-SAFE-001` are unchanged. Existing `tests/domain/test_dashboard_safety.py` must stay green untouched. |
| L6 e2e | `tests/e2e/test_dashboard_refresh.py` (extend; `ATP_RUN_E2E=1`, **not run in this session**) | `test_ui_4_kill_switch_control_covers_every_ac_surface` (all five legs rendered); `..._requires_explicit_confirmation` (no POST until the 2nd click — `page.on("request")` sniffing); `..._renders_refusals_honestly` (route-fulfilled 501/428/504); `..._partial_failure_is_never_dressed_as_success` (200 with a `FAILED` outcome); `..._degraded_status_route_clears_every_leg` (`route.abort()` + malformed body); `..._activations_are_serialized` (held route pins the in-flight window open) |

Runnable in parallel: `pytest -m "not integration and not e2e"`, `cargo test --workspace`.

---

## Verification

1. `./init.sh` → "✓ Environment ready" (worktree-local, binds `ATP_DEV_PORT=3010`).
2. `pytest tests/unit/test_dashboard_killswitch.py tests/boundary/test_dashboard_killswitch_wiring.py tests/domain/ -q`
3. `pytest -m "not integration and not e2e"` + `cargo test --workspace` (no regressions).
4. `python3 tools/kill_switch_check.py`, `tools/kill_switch_timeout_check.py`,
   `tools/architecture_check.py`, `tools/rest_api_check.py`, `tools/cli_check.py`,
   `tools/operator_interface_runtime_check.py` — the contract/route pins must all stay green.
5. Manual read-surface proof (no browser, no shared port):
   `python3 -c "…OperatorInterfaceRuntime + mount_default_dashboard(env) …; dispatch_rest('GET','/dashboard/api/kill-switch')"`
   → assert `ok:false` + every leg `value: None` with an unset state dir; then seed a
   `kill_switch_last_activation.json` via `persist_last_activation` and re-read → legs populate.
6. Design-fidelity checks (cheap, no browser):
   - `grep -nE "https?://|@import|cdn|fonts\.googleapis" python/atp_dashboard/assets/*` → **no hits**
     outside the existing `data:` URIs (SEC-002 self-contained posture).
   - New CSS uses only existing custom properties / `color-mix` — no raw hex except the hazard-stripe
     alpha, so the `[data-theme="light"]` and `prefers-color-scheme` variants work unchanged.
   - `python3 -c "import re,pathlib; css=pathlib.Path('python/atp_dashboard/assets/styles.css').read_text(); print(css.count('{'), css.count('}'))"`
     → braces balanced (the UI-1 keep-both-merge trap).
   - Screenshot the panel in all four states (resting / armed / fired-clean / degraded-unknown) during
     the e2e pass when the operator runs it, for the browser evidence Step 4 requires.
7. `tools/run_ci_locally.sh` (venv-activated).
8. Critic pass 1 `python3 tools/critic_check.py --staged`; pass 2
   `python3 tools/adversarial_review.py origin/main` — both must APPROVE, record the reviewer.
9. `git commit` prep(none)/feat/chore, then
   `python3 tools/agent_pool.py block UI-4 --on SRS-SAFE-001 SRS-SAFE-002 --reason "…"` and
   `python3 tools/agent_pool.py integrate UI-4 --mode serialized`.

**Step-by-step against `feature_list.json` steps[]:** Step 1 dashboard-in-browser and Step 3
"user can activate … and see" require e2e + a wired SAFE-001 handler → **serialized**, honestly
recorded per-step in `progress.d/session-UI-4.md`.

## Notes
- `progress.d/plan-UI-4.md` is written after approval but **must not be `git add`ed** —
  only `session-<id>.md` is allowed in `progress.d/` by the critic.
- Never hand-edit `feature_list.json` / `progress.txt`; the flip path is `integrate` only.
- Pre-walk `feedback_control_affordance_checklist` (7 UI-2 codex rounds) + the UI-1 false-all-clear
  list in the **first** implementation pass so the adversarial review converges in 1–2 rounds.
