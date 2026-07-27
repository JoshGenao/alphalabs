=== SESSION SRS-RES-003 ===
Date: 2026-07-27
Feature: SRS-RES-003 — provide primary dashboard navigation to the embedded Jupyter
research environment (SRS §5.6 line 210; SyRS SYS-43; StRS SN-1.18 / SN-2.01;
verification method: Demonstration; P2).
Outcome: serialized (code on main, passes stays false — the Demonstration leg is
         operator-run, and the environment being navigated TO is itself unflipped)

WHAT WAS ALREADY THERE (do not rebuild):
  SRS-RES-001 (serialized, passes:false) built the EMBED — the runtime
  fixed-upstream reverse-proxy seam (atp_runtime/proxy.py), the probe-derived
  GET /dashboard/api/research snapshot, the same-origin /research/ prefix, and the
  "Research — Jupyter" SPA panel with its iframe + Open button.
  SyRS v0.6 explicitly "clarified SYS-43 so it no longer duplicates SYS-34a":
  SYS-34a is the embed (built), SYS-43 is NAVIGATION from the primary operator
  workflow (this feature). Before this session the dashboard had NO nav chrome at
  all — .topbar was brand + status chips + latency pulse — and the Research panel
  sat 9th (--i:8) in a 12-panel scroll with no addressable link. An operator
  following the primary workflow had no direct route to research.

WHAT I DID:
  1. python/atp_dashboard/navigation.py (new) — PrimaryNavigationProvider, served
     at GET /dashboard/api/navigation. Design decision: the control gates on TWO
     facts kept deliberately separate, so neither can stand in for the other:
       routable  — COMPOSITION: is the same-origin /research/ prefix registered on
                   this runtime? Probe-free, so serving the model costs no upstream
                   traffic and cannot masquerade as liveness.
       reachable — LIVENESS: the SRS-RES-001 probe's answer, unchanged.
     `for_research()` derives routability from the SAME condition mount_dashboard
     uses to register the proxy, so the two can never disagree. The provider is
     composed with the same-origin PREFIX and a boolean — it is never handed the
     upstream, so it has none to leak. same_origin_target() is a fail-closed
     allow-list (root-relative only; no scheme/userinfo/protocol-relative/backslash/
     traversal/control chars) and a REJECTED target is never echoed back, because
     echoing it would republish exactly the direct service URL SYS-43 forbids.
  2. server.py — NAVIGATION_SNAPSHOT_PATH, registered inside the existing
     `if research is not None:` block (BEFORE register_proxy_route, so the proxy's
     meta-path shadow guard sees every dashboard route already claimed). No new
     mount_dashboard parameter: navigation to an unmounted embed would be
     navigation to nothing, so a bare SRS-UI-001 dashboard serves no nav route.
     mount_default_dashboard always composes research, so production always has it.
  3. SPA — a persistent topbar entry (index.html + scoped .nav CSS in styles.css):
     an <a href="#research"> on THIS origin (so /dashboard#research is an
     addressable deep link that still navigates with JS disabled), lab-flask inline
     SVG, reticle corners + travelling caret shown ONLY in the armed state so it is
     never mistaken for the passive read-only status chips. app.js renders it from
     one render fn: armed only when routable AND freshly reachable
     (RESEARCH_LIVE_STALE_MS = POLL_MS * 3). Every degraded branch — 404, HTTP
     error, malformed body, stalled fetch, a probe answer aged past its budget —
     disarms it and DELETES the target a click would otherwise find. A click on a
     non-armed control lands the operator on the panel (which carries the probe's
     reason) and fabricates nothing. The #research deep-link intent is ONE-SHOT and
     bounded (3 poll cycles): it opens only off a completed poll proving
     reachability, then gives up honestly rather than opening blind.
  4. Hardening carried by the same diff: the panel button and the nav now open
     through ONE guarded helper (openResearchEmbed), so the same-origin check
     cannot be bypassed by adding a caller; pollResearch gained the bounded
     AbortSignal.timeout(POLL_MS) every other poll already had; the panel's two
     non-reachable branches now clear their stale embedPath.
  NOT touched: research.py's snapshot contract (RES-001 is serialized awaiting
  operator sign-off — its evidence stays valid), atp_runtime/proxy.py, the compose
  stack, runtime_services.json (no cross-language contract here).

WHAT I TESTED (per step):
  Step 1 (./init.sh): PASS — "✓ Environment ready".
    Gotcha for the next session: init.sh does NOT install requirements-dev.txt, so
    the worktree venv has no pytest until `.venv/bin/pip install -r requirements-dev.txt`.
  Step 2 (exercise via browser automation + REST): PARTIAL — solo layers PASS; the
    browser leg is written and gated, operator-run by explicit operator decision
    this session (no parallel Jupyter/dashboard stack while 3 siblings were live):
      L1 tests/unit/test_dashboard_navigation.py — 12 passed. Model shape; routable
        vs explicit deferred cell naming ATP_RESEARCH_UPSTREAM + SRS-RES-001; every
        service-URL spelling refused; a mis-composed prefix/state-route fails closed
        WITHOUT echoing it; probe-free (a dead upstream still answers instantly).
      L1 tests/unit/test_dashboard_navigation_client.py — 10 passed. EXECUTES the
        shipped app.js predicate under node over the SAME adversarial corpus as the
        Python allow-list and demands identical verdicts, so the two mirrors cannot
        drift; malformed nav feeds (including routable:"true" / routable:1) never
        arm; the post-success-then-degraded sequence proves NEITHER surface can
        reopen off a stale path (the Codex R1 regression); static pins on the
        single open-funnel + the disarm branches. Skips cleanly where node is absent.
      L4 tests/boundary/test_research_nav_wiring.py — 7 passed. Over real TCP: SPA
        ships the nav + #research anchor; the route serves a routable same-origin
        entry whose target is really served by the same listener; strict read
        (POST/PUT/DELETE refused); unconfigured embed -> not routable AND no prefix
        to route to; bare dashboard -> 404 and no nav route; RES-001's own snapshot
        contract unchanged.
      L7 tests/domain/test_research_navigation_safety.py — 8 passed. The upstream
        authority appears in NO served byte (SPA, app.js, styles.css, nav route,
        probe route) in the reachable AND the DOWN case (an error path that quotes
        the address leaks just as effectively); no adversarial prefix can become a
        target; nav cannot be shadowed by a proxy prefix; the kill-switch
        confirmation guard is unchanged with nav mounted; routable never claims
        reachable.
      L6 tests/e2e/test_research_navigation.py — WRITTEN, NOT RUN (gated
        ATP_RUN_E2E=1 + playwright + jupyterlab; skips cleanly, verified).
  Step 3 (AC — open the embed from the primary workflow without a direct service
    URL): PASS for the code path (L4 + L7 prove the same-origin route and the
    zero-leak property; L1-node proves the browser guard). The DEMONSTRATION
    itself — a real browser clicking the topbar entry into real JupyterLab — is
    the L6 test, operator-run.
  Step 4 (record evidence, leave passes false): DONE — serialized.
  Solo gate: pytest -m "not integration and not e2e" -> 4204 passed / 5 skipped /
    1 failed. The single failure is tests/property/test_indicators_property.py::
    test_bbands_property_matches_batch_talib — the DOCUMENTED pandas_ta-vs-TA-Lib
    precision main-red (progress.txt lines 3694 / 3810 / 3915 / 4021: "indicators.py
    — NOT touched"); this branch touches no indicator file. cargo test --workspace:
    all suites ok, 0 failed. cargo fmt --check + cargo clippy -D warnings: clean.
    ruff check .: clean. mypy python/: 68 errors in 16 files, ZERO in any file this
    branch touches (and mypy is continue-on-error in ci.yml).
  CI MIRROR (tools/run_ci_locally.sh): run it with the venv on PATH —
    `PATH="$PWD/.venv/bin:$PATH" tools/run_ci_locally.sh` — otherwise it picks up
    the system python and dies on `No module named numpy` in architecture_check.
    With the venv it is green until the BLOCKING whole-repo `ruff format --check .`
    gate, which reports exactly ONE file: tests/domain/test_safe003_connectivity_block_cli.py.
    That file is NOT in my diff and is already unformatted on origin/main (added by
    the SRS-SAFE-003 sibling in fa8b837 — verified by checking out the origin/main
    blob and re-running the formatter on it). Left alone deliberately: the documented
    "CI red behind format gates" rule says a feature PR must not whole-repo-format,
    and rewrapping a sibling's CLI test risks breaking literal contract anchors while
    that sibling may still be working the file. Owner: SRS-SAFE-003 / the format-pin PR.

CRITIC VERDICTS:
  deterministic (critic_check.py --staged): APPROVE — no findings, on every commit.
  judgment (adversarial_review.py, reviewer=codex): THREE rounds, each re-reviewed
    against its OWN follow-up diff (review-the-fix-not-the-whole-feature, so
    already-authorized scope does not re-block).
    R1 (base origin/main) BLOCK, 1 finding — [high] STALE OPEN BUTTON (real bug,
      FIXED): I had deliberately left the panel button to SRS-RES-001, but making
      openResearchEmbed a SHARED funnel pulled it into this feature's surface. On a
      degraded probe (HTTP error / 404 / timeout) pollResearch cleared only the
      navigation liveness fact, so #research-open kept its stale data-embed-path and
      one click could still open an environment last seen alive several polls ago —
      exactly the stale-truth-left-ACTIONABLE class this feature is most exposed to.
      Fix: disarmResearchControls() in all three degraded branches (disable + delete
      the path + state the reason). Proven by executing the SHIPPED source under
      node through the full sequence (armed -> opened -> probe fails -> click), and
      the test was checked to DISCRIMINATE: replaying the pre-fix control flow
      yields reopened=true with the stale path retained.
    R2 (base a2ed570) WARN, 1 finding — [medium] NARROWED L7 COVERAGE (FIXED):
      confining the bare-port assertion to the JSON routes (my own flake-avoidance,
      since a five-digit ephemeral port could coincidentally occur in a 120 KB
      asset) meant a leak spelled localhost:<port> could pass through the assets.
      Fix: _assert_no_service_url() over EVERY served path, matching URL SHAPE —
      any absolute http(s) URL carrying the upstream port or naming any loopback
      authority fails; non-flaky by construction. Verified it catches all five
      variants (localhost / 127.0.0.1 / [::1] / 0.0.0.0 / a non-loopback hostname
      with the port) while the assets' one legitimate absolute URL (the W3C SVG
      namespace) passes. Coverage is now WIDER than before the narrowing.
    R3 (base e36eea0) APPROVE — no findings; summary explicitly confirms the
      direct-service-URL and dashboard-navigation constraints are not violated.
      (Non-empty, specific summary — not the dropped-verdict shape.)

RESUME / NEXT (for the operator to flip passes:true):
  1. SRS-RES-003 cannot honestly flip ahead of SRS-RES-001: its AC names "the
     embedded Jupyter environment", and RES-001 is still passes:false pending
     (a) sign-off on its documented same-origin browser-vector residual
     (SECURITY.md § OPERATOR SIGN-OFF GATE) and (b) the deployed-stack demo.
     No `block` edge was recorded — `--mode serialized` already removes it from the
     offered frontier, and a redundant edge is churn.
  2. The demonstration, once RES-001's stack is up:
       pip install jupyterlab playwright && playwright install chromium
       ATP_RUN_E2E=1 pytest tests/e2e/test_research_navigation.py
     then, on the real compose stack, open /dashboard behind the operator's
     authenticated HTTPS proxy and click the topbar Research entry.
  3. UI-6 (ready frontier — "dashboard provide embedded Jupyter navigation",
     traces to SRS-RES-001 + SRS-RES-003) is now near-green off this work: its AC
     is the same sentence. It was NOT claimed here. Its browser evidence is
     tests/e2e/test_research_navigation.py.
