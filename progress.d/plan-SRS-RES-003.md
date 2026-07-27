# SRS-RES-003 — Primary dashboard navigation to the embedded Jupyter research environment

## Context

**Feature (claimed by the scheduler):** `SRS-RES-003` — *"The software shall provide primary
dashboard navigation to the embedded Jupyter research environment."*
**AC:** "The operator can open the embedded Jupyter environment from the primary dashboard
workflow without using a direct service URL." Verification: **Demonstration**. P2.
Trace: SyRS **SYS-43**, StRS SN-1.18 / SN-2.01. `docs/SRS.md:210`.

**Prior progress:** none for RES-003 (no `progress.d/session-SRS-RES-003.md`).
Its subject, **SRS-RES-001** (serialized on `main`, `passes:false`), already built:

- `python/atp_runtime/proxy.py` — the fixed-upstream, same-origin reverse-proxy seam
  (`register_proxy_route`), loopback/RFC1918 only.
- `python/atp_dashboard/research.py` — `ResearchEnvironmentProvider`, probe-derived only
  (`not-configured` / `unreachable+reason` / `reachable`+`embed_path`), served at
  `GET /dashboard/api/research`; `/research/` proxy registered iff `ATP_RESEARCH_UPSTREAM` is set.
- SPA "Research — Jupyter" **panel** (`assets/index.html:316`, `assets/app.js:952-1008`) with a
  status line, an *Open research environment* button, and a lazy same-origin iframe.

**The gap RES-003 closes.** SyRS v0.6 explicitly "clarified SYS-43 so it no longer duplicates
SYS-34a": SYS-34a is the *embed* (RES-001, built), SYS-43 is *navigation from the primary
operator workflow* (unbuilt). Today the dashboard is a single 12-panel scroll with **no nav
chrome whatsoever** (`.topbar` = brand + status chips + latency pulse); the Research panel sits
9th (`--i:8`), reachable only by scrolling and hunting, and there is no addressable deep link.
An operator following the primary workflow has no direct navigation to research.

**Outcome:** a persistent, honest **Research** navigation entry in the topbar plus a
`/dashboard#research` deep link that opens the *same-origin* embed in one action — never a
direct Jupyter service URL, never a fabricated "open" when the upstream is not reachable.

**Operator decisions taken during planning:** (1) research-only nav entry, not a full panel
rail — exact SYS-43 scope, no encroachment on UI-1/UI-2/UI-3 panels; (2) the L6 e2e is written
and gated, **not** run this session (no parallel Jupyter/dashboard stack while 3 siblings are live).

---

## Design

Two facts drive the nav, kept deliberately separate so neither can fake the other:

| fact | source | nature |
|---|---|---|
| **routable** — is the same-origin `/research/` prefix registered on *this* runtime? | new `GET /dashboard/api/navigation` (composition fact, **no probe**) | static |
| **reachable** — is the upstream actually answering right now? | existing `GET /dashboard/api/research` poll (live probe) | live |

The nav entry is actionable **only when both hold**, and any degradation on either side
disarms it. This keeps the new route probe-free (no second probe per poll cycle) and leaves
RES-001's snapshot contract byte-unchanged (it is serialized awaiting operator sign-off).

### Files to add

- **`python/atp_dashboard/navigation.py`** — `PrimaryNavigationProvider`.
  - `navigation_snapshot()` → `{generated_at, srs_ref: "SRS-RES-003", entries: [entry]}`; the
    research entry carries `{id, label, panel_anchor, target, state_route, routable, detail}`.
  - **Fail-closed same-origin policy** (`same_origin_target()`): a target must start with a
    single `/`, carry no scheme, no `//`, no backslash, no host — anything else yields
    `routable: false` + a naming `detail`. This is the "without using a direct service URL"
    invariant, enforced in Python and unit-testable.
  - Never echoes `upstream`. Mirrors `research.py`'s honesty docstring conventions.

### Files to change

- **`python/atp_dashboard/server.py`** — add `NAVIGATION_SNAPSHOT_PATH = "/dashboard/api/navigation"`;
  inside the existing `if research is not None:` block (before the proxy registration, so the
  proxy's meta-path shadow guard sees it) register the nav meta route from a provider derived
  from the research provider. **No new `mount_dashboard` parameter** — nav exists iff the embed
  is mounted; `mount_default_dashboard` always composes research, so production always serves it.
- **`python/atp_dashboard/__init__.py`** — export the new provider (follows existing pattern).
- **`assets/index.html`** — `<nav class="nav" aria-label="Primary">` in `.topbar` with one
  `<a id="nav-research" href="#research">` (inline SVG glyph + label + live state caption);
  `id="research"` on the research `<section>` so the anchor works natively without JS.
- **`assets/app.js`** — new `// ----- SRS-RES-003 primary research navigation -----` block:
  - `pollNavigation()` (bounded `AbortController` fetch, `POLL_MS`); `404` → inert
    "not mounted" state; malformed body → fail closed (guard the mapping/array *before*
    `.get()`, per the UI-5 lesson).
  - `renderResearch()` additionally stamps `researchLive = {reachable, at}`; `renderNav()`
    combines `routable && reachable && fresh(at)`. A stalled/failed/stale research poll
    **disarms** the nav (stale-truth-left-ACTIONABLE is the failure class this feature is most
    exposed to).
  - One shared `openResearchEmbed(path)` used by *both* the panel button and the nav, with a
    client-side same-origin re-check before `frame.src` is assigned (defence in depth).
  - Click when not reachable → scroll to the panel and surface the reason; **never** set `src`,
    never claim an open.
  - Deep link: on load and `hashchange`, `#research` arms a **one-shot** intent consumed by the
    first *completed* research poll (never acts on assumed state).
- **`assets/styles.css`** — `.nav` / `.nav__link` + `[data-state]` tones using existing tokens
  (`--accent`/`--warn`/`--bad`/`--ink-faint`); `.topbar` grid `1fr auto auto` → `1fr auto auto auto`
  and the `@media (max-width: 760px)` rule at `styles.css:279-280`. Load the `frontend-design`
  skill before writing markup/CSS (self-contained, inline SVG, scoped classes).
- **`python/atp_dashboard/README.md`** — one paragraph for the new route/module.

### Explicitly out of scope

No change to `research.py`'s snapshot contract, `atp_runtime/proxy.py`, the compose stack, or
`architecture/runtime_services.json` (no cross-language contract here). No nav for panels owned
by other features.

---

## Tests

| Layer | File | Cases |
|---|---|---|
| **L1 unit** | `tests/unit/test_dashboard_navigation.py` | snapshot shape + `srs_ref`; routable vs not-configured; `detail` names `ATP_RESEARCH_UPSTREAM` + owner; target is always the `/research/` same-origin path; `same_origin_target` rejects `http://host`, `//host`, `research/`, `\host`, `""` → fail closed; snapshot never contains the upstream authority |
| **L4 boundary** | `tests/boundary/test_research_nav_wiring.py` | mounted runtime serves `/dashboard/api/navigation` (routable) over a Jupyterish stub; SPA ships the topbar nav + `href="#research"` + `id="research"` anchor; unconfigured research mount → `routable:false`, no `/research/` prefix; **bare** dashboard (no research provider) → nav route `404` and the SPA still coherent; route is read-only (POST refused) |
| **L7 domain** | `tests/domain/test_research_navigation_safety.py` | **no direct service URL**: the upstream authority appears in *no* served byte (`/dashboard`, `/dashboard/app.js`, nav route, research route); nav target can never escape `/research/` same-origin; mounting nav adds no proxyable/control prefix and no control affordance (no action/POST field); the SEC-004 / bind-policy invariants still hold with nav mounted |
| **L6 e2e** (gated, written not run) | `tests/e2e/test_research_navigation.py` | real JupyterLab (`base_url=/research/`, ephemeral port — reuse the `jupyter_lab` fixture pattern of `tests/e2e/test_research_embed.py:66`): land on `/dashboard`, click the **topbar nav** without scrolling → JupyterLab renders in the same-origin iframe and `page.url` never leaves the dashboard origin; `/dashboard#research` deep link auto-opens; with the upstream **down**, the nav renders unavailable and clicking fabricates nothing |

A `tests/domain/` test is not strictly forced by `SAFETY_PATH_RE` for these paths, but the
"no direct service URL" invariant is a genuine trust-boundary property and belongs at L7.

---

## Verification

```bash
./init.sh                                     # Step 1 — "✓ Environment ready"
pytest -m "not integration and not e2e"       # full solo suite (new L1/L4/L7 included)
cargo test --workspace
ruff check . && ruff format --check python/atp_dashboard tests   # scoped: main is red behind
                                              # whole-repo format gates (not this PR's to fix)
python3 tools/critic_check.py --staged --format text
python3 tools/adversarial_review.py origin/main
tools/run_ci_locally.sh
```

Per-step PASS/FAIL evidence (exact command → observed output) goes into
`progress.d/session-SRS-RES-003.md`.

**Operator leg (not run here):**
`ATP_RUN_E2E=1 pytest tests/e2e/test_research_navigation.py` with `jupyterlab` + Playwright
chromium installed, then the deployed-stack demonstration.

## Expected completeness: **serialized** (`passes` stays `false`)

Honest classification, decided up front:

1. Verification method is **Demonstration** against a deployed dashboard; the L6 browser leg is
   written but operator-run by the decision above.
2. The environment being navigated *to* — **SRS-RES-001** — is itself `passes:false`, pending
   operator sign-off on its documented same-origin browser-vector residual (`SECURITY.md`
   § OPERATOR SIGN-OFF GATE) **and** the deployed compose demonstration (`dashboard-api`'s CMD
   is still a compile-only stub). Flipping RES-003 green would assert an embed that has not been
   accepted.

So: `python3 tools/agent_pool.py integrate "$ATP_FEATURE_ID" --mode serialized`.
No `block` edge is recorded — `--mode serialized` already removes it from the offered frontier,
and a redundant edge is churn; the RES-001 dependency is stated in the session note instead.
`UI-6` (ready frontier, traces to SRS-RES-001 + SRS-RES-003) becomes near-green off this work —
noted as the natural follow-on, not claimed here.

## Order of work

1. `progress.d/plan-SRS-RES-003.md` (persist this plan) + `agent_pool.py heartbeat`.
2. `./init.sh`.
3. `navigation.py` + `server.py` + `__init__.py` + L1/L4/L7 tests → green.
4. SPA (`index.html` / `app.js` / `styles.css`) + L6 gated e2e + README.
5. Full gate → critic (deterministic + adversarial) → `feat` + `chore` commits → integrate serialized.
