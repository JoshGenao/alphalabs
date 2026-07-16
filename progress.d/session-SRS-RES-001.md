=== SESSION SRS-RES-001 ===
Date: 2026-07-16
Feature: SRS-RES-001 — embed the Jupyter research environment in the dashboard workflow
Outcome: serialized (code on main, passes:false; operator finishes the deployed-stack
         demonstration AND signs off on the documented browser-vector residual before flip)

What I built (3 feats + 1 harden, all on branch, rebased onto origin/main):
1. atp_runtime reverse-proxy seam (python/atp_runtime/proxy.py + rest_server + runtime):
   runtime.register_proxy_route(prefix, upstream) — generic, consumer-agnostic. Upstream
   FIXED at registration (never request-derived); plain-http loopback/RFC1918 only (DNS
   resolve-then-validate per connect, TOCTOU closed); prefixes that shadow /api/, /dashboard/,
   WS path, or a meta/asset path are structurally unregistrable; hop-by-hop header hygiene both
   ways + Origin/Referer rewrite; chunked-request refusal + exact Content-Length reads
   (smuggling); bounded body/buffer/timeouts (honest 502/413/400/503, never a hang); raw
   byte-pump WebSocket tunnel for kernel channels (slot-bounded 32, no idle kill). do_PATCH
   added (proxy-only). Operator Authorization stripped upstream (Codex R1 #2).
2. Dashboard research panel (python/atp_dashboard/research.py + server + assets):
   ResearchEnvironmentProvider — probe-derived state only (not-configured / unreachable+reason /
   reachable+status), embed_path only after a live probe, never a fabricated "connected".
   mount_dashboard(research=...) serves GET /dashboard/api/research and, iff ATP_RESEARCH_UPSTREAM
   is set, registers the same-origin /research/ proxy. SPA panel with a lazy same-origin iframe.
3. Deployment leg (docker/jupyter.Dockerfile + docker-compose.yml + python/atp_research_proxy +
   jupyter_isolation_check + runtime_services.json + docs): concrete JupyterLab 4.6.1
   (base_url=/research/, non-root, token-less — network path is the auth boundary); NEW
   phase1-research-proxy one-way L4 hop (python -m atp_research_proxy) — the only member of both
   atp_research_net and the NEW internal atp_research_edge_net; dashboard-api joins default+edge,
   NEVER atp_research_net; jupyter's dead port publish removed (IF-13). Checker gains research-proxy
   isolation + no-published-ports assertions + 6 negative fixtures (25 total, all reject).
4. harden: closed the two Codex findings (see Critic verdicts).

Design (the one-way chain):
  browser -> 127.0.0.1:8080 dashboard-api (runtime serves SPA + /research/* proxy)
          -> [atp_research_edge_net, internal] phase1-research-proxy (fixed-upstream L4 hop)
          -> [atp_research_net, internal]       phase1-jupyter (JupyterLab base_url=/research/)
dashboard-api NEVER joins atp_research_net (SEC-004 invariant intact); the hop's fixed upstream
means a connection FROM jupyter only loops back to jupyter.

What I tested (per step):
  Step 1 (./init.sh): PASS — "✓ Environment ready".
  Step 2 (browser automation + REST/WS): PASS solo — ATP_RUN_E2E=1 pytest tests/e2e/
    test_research_embed.py: REAL JupyterLab 4.6.1 reverse-proxied at /research/; Playwright
    headless chromium renders the panel, probe flips reachable, click loads the same-origin
    iframe, iframe inner document is JupyterLab (frame-ancestors 'self' admits it); REST
    POST /research/api/kernels -> 201 through the DASHBOARD port (XSRF cookie dance through the
    proxy); raw WS kernel_info_request -> kernel_info_reply through the tunnel. 2 passed.
  Step 3 (AC — reachable from dashboard w/o separate URL + independent of live/paper/backtest):
    PASS solo for the code path — same-origin /research/ (no separate URL); e2e + domain test run
    with ZERO strategy/backtest handlers (POST /api/v1/backtests still 501 deferred) = SYS-34c
    independence. The DEPLOYED-STACK demonstration (real compose chain, operator HTTPS, dashboard-api
    CMD actually serving `python -m atp_dashboard` rather than its compile-only stub) is operator-gated.
  Step 4 (evidence + leave passes false): DONE — serialized.
  Solo gate: pytest -m "not integration and not e2e" — 3685 passed / 3 skipped after rebase onto
    origin/main c316c76 (the 5 earlier failures were the SRS-EXE-006 red baseline, fixed on main,
    not mine). cargo test --workspace: PASS. Static: jupyter_isolation (positive + 25 fixtures
    reject) / container_isolation / deployment / network_binding / operator_interface_runtime /
    dependency_boundary / architecture — all PASS. ruff check .: PASS.
  KNOWN pre-existing main-red: `ruff format --check .` flags 13 files that are NOT mine
    (atp_reliability/restart* from SRS-REL-002, test_data021_*, container_isolation/deployment
    checkers) — the documented "main red behind format gates" condition (siblings REL-002/UI-004
    integrated through it). ALL 373 other files incl. every file in this feature are format-clean.
    Did NOT whole-repo-format (forbidden in a feature PR); the format-pin PR owns those 13.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings (every commit).
  judgment (codex_review.sh): FOUR rounds, each re-reviewed by its OWN follow-up diff
    (per the review-the-fix-not-the-whole-feature rule so authorized scope doesn't re-block).
    R1 (base origin/main) needs-attention, 2 findings:
      - HIGH (FIXED): proxy forwarded operator Authorization to token-less Jupyter -> stripped
        upstream (_xsrf still forwarded); test proves the upstream never sees Authorization.
      - CRITICAL (operator-authorized serialized): same-origin embed (IF-13) means notebook-
        rendered JS in the OPERATOR'S browser could mint POST /api/v1/kill-switch?confirm=true
        (SRS-API-001 confirm guard is a mintable query flag). The ENFORCED SEC-004 boundary is
        the credential-less, execution-unroutable CONTAINER; the browser-session vector is a
        cross-feature tension (IF-13 same-origin vs API-001 confirm contract). Operator chose
        (AskUserQuestion) "serialized + documented risk"; SECURITY.md carries an explicit
        OPERATOR SIGN-OFF GATE. A browser-enforced fix needs an SRS-API-001 origin-bound confirm
        token OR a separate-origin embed bending IF-13 — both cross-feature, out of scope.
    R2 (base de84f0d) HIGH (FIXED): Cookie still forwarded as-is -> filter to Jupyter-owned only.
    R3 (base 034152f) HIGH (FIXED): username-* prefix too broad -> tightened to exact name _xsrf
      (the only cookie the real e2e proves the token-less API path needs; e2e re-verified green).
    R4 (base 186c6f1) HIGH (CONVERGED / documented residual): a Cookie header has no issuer
      metadata, so exact-name _xsrf can't be told from an operator layer reusing that name. This
      is the IRREDUCIBLE limit of same-origin cookie handling — the only enforced fix (rewrite
      _xsrf into a proxy-owned namespace) breaks JupyterLab's client-side XSRF; a separate-origin
      embed bends IF-13. Scoped, not faked: runtime issues no cookies of its own; SECURITY.md
      reserved-name constraint (external auth must not reuse _xsrf) folded into the sign-off gate;
      regression test pins the edge. This is a strictly NARROWER instance of the R1 same-origin
      browser vector the operator already authorized as serialized — so within existing sign-off.
    Loop stopped at R4 by design (per the adversarial-non-convergence rule: fix in-scope bugs,
    then scope the irreducible residual with docs + human authorization; never fake an APPROVE).
    Integrated --mode serialized: passes stays false; NOT claiming a verified green.

Resume / next (for the operator to flip passes:true):
  1. Sign off on the browser-vector residual (SECURITY.md § "OPERATOR SIGN-OFF GATE"), OR
     require the SRS-API-001 origin-bound confirm token first.
  2. Build the phase1 stack (`docker compose --profile phase1 build`) — dashboard-api's CMD must
     serve `python -m atp_dashboard` (owned by the deployment feature) so ATP_RESEARCH_UPSTREAM is live.
  3. `docker compose --profile phase1 up`, open http://127.0.0.1:8080/dashboard behind the
     operator's authenticated HTTPS reverse proxy (NFR-S3), open the Research panel, confirm
     JupyterLab renders embedded and a kernel runs — with no live/paper strategy and no backtest
     running (SYS-34c). Gated L5 tests/integration/test_jupyter_isolation_inspect.py (ATP_RUN_INTEGRATION=1)
     is the container-config proof.
  Then flip via `close_feature.py --verified` / the verified-e2e label.
  NOTE: JupyterLab is a CONTAINER dependency (docker/jupyter.Dockerfile), deliberately NOT pinned in
  requirements*.txt; the e2e installs it ad hoc in the worktree venv.
