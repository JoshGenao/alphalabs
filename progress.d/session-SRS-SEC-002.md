=== SESSION SRS-SEC-002 ===
Date: 2026-07-09
Feature: SRS-SEC-002 — by default the dashboard/API service binds only to RFC 1918 / loopback
addresses; publicly-routable binding requires explicit operator configuration + documented
external authentication (SRS-5.9; trace NFR-S3 / StRS SN-2.01; AC-3).
Outcome: serialized (operator-authorized via AskUserQuestion 2026-07-09) — code + SEC-002-attributed
evidence land on main; passes STAYS false. The literal AC-2 "external-host connect against a
non-RFC1918 interface fails" is a DEFERRED live verification (needs a publicly-routable interface,
which a NAT'd host/CI runner does not bind) — captured on the deployed Phase-1 stack, not solo.

WHAT I FOUND (the mechanism already existed + was deliberately converged under API-001, 14 rounds):
- python/atp_runtime/rest_server.py: is_allowed_bind_host / assert_bind_allowed / LoopbackHTTPServer —
  the single listening socket (REST+WS on one port). Uses the EXACT RFC-1918 set (NOT ipaddress.is_private;
  in-code note "Do not switch to is_private"): 0.0.0.0/:: are is_private=True but is_unspecified=True and
  are correctly refused. Default host 127.0.0.1 everywhere (runtime.start, atp_dashboard.__main__).
- errors.py:134 BindPolicyError docstring records the DELIBERATE decision: a public bind "requires
  explicit, documented operator configuration that this runtime intentionally does not provide" → NO
  public-bind opt-in exists by design; external exposure = operator-managed authenticated reverse proxy.
- All docker-compose published ports bind loopback (dashboard 127.0.0.1:8080, jupyter 127.0.0.1:8888,
  ib-gateway ${ATP_IB_HOST:-127.0.0.1}:...); tools/deployment_check.py:67 already enforces the dashboard.
  docs/DEPLOYMENT.md already documented the loopback default + reverse-proxy requirement.

WHAT I DID (feat + 3 fix commits; reuses the converged bind policy, NO product code modified):
- tools/network_binding_check.py (NEW): 6 SEC-002-attributed checks —
  (1) check_dashboard_api_binds_loopback: the dashboard/API service publishes a FIXED loopback/RFC-1918
      host with NO operator-overrideable ${} interpolation (the strong AC-1 claim);
  (2) check_no_service_publishes_all_interfaces: no service publishes a bare PORT:PORT (0.0.0.0); every
      published-port DEFAULT host is loopback/RFC-1918 (honestly scoped to defaults, not overrides);
  (3) check_no_source_binds_all_interfaces: no python/ product module has a literal 0.0.0.0/:: bind;
  (4) check_bind_policy_refuses_public: is_allowed/assert_bind_allowed fail closed on 0.0.0.0/::/link-
      local/CGNAT/public/172.15/172.32;
  (5) check_default_bind_host_is_loopback: runtime.start + atp_dashboard.__main__ + 4 runtime_services.json
      bind constants all loopback;
  (6) check_external_exposure_documented: DEPLOYMENT.md + SECURITY.md carry the external-auth requirement.
  Wired into BOTH CI loops (.github/workflows/ci.yml slug `network_binding` + tools/run_ci_locally.sh
  `network_binding_check`).
- tests/domain/test_network_binding.py (NEW, domain+safety): default bind is loopback-only (getsockname
  not 0.0.0.0 + loopback HTTP 200); the loopback-bound service REFUSES a connect on this host's real
  non-loopback interface (solo empirical proxy that it is not on 0.0.0.0); public/unspecified bind fails
  closed BEFORE any socket opens; classifier boundary exact; the inspection check passes; and the LITERAL
  AC-2 test test_external_host_connect_on_non_rfc1918_interface_is_refused (requires a publicly-routable
  interface; SKIPS with an explicit DEFERRED reason on a NAT'd host).
- SECURITY.md: new "Network binding (SRS-SEC-002)" section. docs/DEPLOYMENT.md: reconciled to proxy-only
  (the process exposes NO public-bind mode; public reachability is only via an operator-configured
  authenticated reverse proxy = the SRS's "explicit operator configuration + documented external
  authentication"). Removed the contradictory "raw publicly-routable bind is an explicit non-default".
- Did NOT add a public-bind opt-in (would reverse the converged safety decision); did NOT modify
  rest_server.py / runtime.py / atp_readiness/gate.py / the config catalogue; kept the exact RFC-1918 set.

WHAT I TESTED (per step):
  Step 1 (init): PASS — ./init.sh -> "Environment ready" (init.sh skips requirements-dev.txt -> pip
    install -r requirements-dev.txt for pytest).
  Step 2 (inspection/checks): PASS — network_binding_check.py exit 0 (6 evidence lines); deployment_check
    PASS (portability keywords intact after doc edits).
  Step 3 (AC): AC-1 (default compose loopback/RFC-1918) PASS (check, all services); AC-2 solo PROXY PASS
    (default bind loopback-only + local non-loopback interface refused) — the LITERAL non-RFC1918
    external-host refusal is DEFERRED (skips on this NAT'd host); AC-3 (docs external-auth) PASS.
  Step 4: passes:false retained; UI/behavior traces to NFR-S3 / SN-2.01 confirmed.
  Gate: full suite 3180 passed / 3 pre-existing skips / 0 regressions; test_network_binding 20 passed +
    1 deferred-skip; cargo test --workspace ok; ruff check + ruff format clean on my 2 files; mypy python/
    baseline unchanged (no python/ edits; CI runs `mypy python/` only). NOTE: tools/run_ci_locally.sh is
    RED only at `ruff format --check .` on a PRE-EXISTING baseline (tests/domain/test_coverage_gate_domain.py
    + tests/e2e/test_dashboard_refresh.py + tools/deployment_check.py — all unmodified by me, already fail
    on origin/main; toolchain-pin fix is a separate PR). Not a SEC-002 regression; integrate does not run
    that gate.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings (all commits).
  judgment (adversarial_review.py origin/main, reviewer=codex): 4 rounds; converged.
    R1 BLOCK (high): SECURITY.md "no public opt-in / never public" contradicted docs/DEPLOYMENT.md
      "raw publicly-routable bind is an explicit non-default" -> FIXED: reconciled both docs to proxy-only
      (process has no public-bind mode; the authenticated reverse proxy IS the SRS's config+auth).
    R2 BLOCK (high+med): (a) the check resolved ${ATP_IB_HOST:-127.0.0.1} to its default and over-claimed
      safety though an operator can override ATP_IB_HOST=0.0.0.0; (b) the L7 test never did the external-
      host refusal. -> FIXED: split into a FIXED-host dashboard/API claim vs a DEFAULT-host all-services
      guard (honest wording); added a real external-refusal test.
    R3 BLOCK (high): the external test used ANY non-loopback IPv4 (an RFC-1918 LAN addr behind NAT) and
      skipped silently — never proving the LITERAL non-RFC1918 AC. -> FIXED: require _primary_non_rfc1918_ipv4
      (is_global only); on a NAT'd host it SKIPS with an explicit DEFERRED reason; kept the RFC-1918
      interface refusal as a labelled solo proxy; classified SERIALIZED.
    R4 APPROVE: "adds inspection and L7 evidence for SRS-SEC-002 while keeping the feature serialized/
      passes:false for the deferred literal external-host verification." Never faked an APPROVE.

Resume / next (what flips SRS-SEC-002 passes:true — a deferred operator/e2e step, none auto):
  Capture the literal AC-2 evidence: from an EXTERNAL host on a non-RFC1918 (publicly-routable) network,
  connect to the deployed Phase-1 dashboard/API (docker compose --profile phase1 up; dashboard published
  127.0.0.1:8080) and confirm it is REFUSED under the default configuration. On a host that binds a public
  interface, tests/domain/test_network_binding.py::test_external_host_connect_on_non_rfc1918_interface_is_refused
  RUNS (does not skip) and asserts the refusal — so the flip is: run it on the deployed stack (or a host
  with a public interface) + record the pass, then close passes:true. Everything else (mechanism, compose,
  policy, source scan, docs, solo interface-refusal proxy, inspection check) is DONE + green on main.
  DEFERRED OWNER: operator / verified-e2e on the SRS-ARCH-004 Phase-1 deployment. Do NOT add a public-bind
  opt-in and do NOT rebuild the check/tests — only capture the external-host evidence and flip.
