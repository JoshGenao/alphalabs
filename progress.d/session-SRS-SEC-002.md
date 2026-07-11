=== SESSION SRS-SEC-002 ===
Date: 2026-07-11
Feature: SRS-SEC-002 — by default the dashboard/API service binds only to RFC 1918 / loopback; public
exposure is via an operator-managed authenticated reverse proxy (no process-level public-bind mode).
Outcome: complete (passes:true) — operator-authorized reframe of AC-2 for the RFC1918-only deployment.

WHY THE FLIP (resume context): the mechanism + inspection check + docs + domain evidence were already on
main (serialized since 2026-07-09). It was held passes:false only because the literal AC-2 test
test_external_host_connect_on_non_rfc1918_interface_is_refused SKIPS unless the host binds a publicly
routable (is_global) interface. The operator clarified the Phase 1 deployment target is an RFC1918-only
network (a 10.0.0.x server; 10.0.0.0/8 is itself RFC 1918) — so NO non-RFC1918 interface exists there
either, and the literal test can never run in the actual target. Gating the flip on an
architecturally-impossible interface was the wrong bar. Operator authorized reinterpreting AC-2 to its
provable, strictly-stronger structural form and flipping complete (AskUserQuestion, 2026-07-11).

WHAT I DID (no product code changed; mechanism unchanged; no public-bind opt-in added):
- tests/domain/test_network_binding.py: removed the always-skipping standalone external-host test + its
  _primary_non_rfc1918_ipv4 / _DEFERRED_EXTERNAL_HOST scaffolding (it asserted nothing in any RFC1918
  environment). Added test_no_non_rfc1918_binding_under_default_config (NON-skipping): the default bind is
  confined to loopback/RFC1918 (never is_global); the runtime refuses fail-closed (BindPolicyError, no
  socket) to bind ANY public v4/v6 address; and RETAINS the literal external-host connect as a
  runs-when-present step (no-op on RFC1918-only hosts, NOT a skip). Kept the local non-loopback refusal
  proxy; rewrote the module docstring to drop the DEFERRED/serialized framing. Strictly stronger than one
  external connect (covers a class of public addresses).
- docs/SRS.md (SRS-SEC-002 row): (a) requirement statement clarified to proxy-only — "exposes no
  process-level publicly-routable bind mode; publicly routable exposure only through operator-managed
  configuration external to the service (an authenticated reverse proxy) with documented external
  authentication" (aligns with the settled SECURITY.md/DEPLOYMENT.md/BindPolicyError design); (b) AC-2
  verification clause reconciled to the structural invariant.
- feature_list.json (SRS-SEC-002 description + Step 3/4) reconciled to match — via operator-authorized
  direct commits to main (6c10157 steps, 22ea01a description); an agent branch cannot edit feature_list.json
  (shared_state_violations). All four requirement sources (feature_list, SRS.md, SECURITY/DEPLOYMENT, test)
  are now consistent: proxy-only, no process public-bind, structural refusal of any non-RFC1918 bind.

WHAT I TESTED (per step, all solo):
  Step 1 (init): PASS — ./init.sh -> "Environment ready".
  Step 2 (inspection): PASS — tools/network_binding_check.py exit 0 (6 evidence lines).
  Step 3 (AC): PASS — (a) default compose loopback/RFC1918 (check); (b) structural AC-2 test
    test_no_non_rfc1918_binding_under_default_config (21 passed, 0 skips); (c) docs external-auth present.
  Step 4 (evidence): PASS — structural invariant is the acceptance evidence for the RFC1918-only target.
  Suite: pytest -m "not integration and not e2e" -> 3231 passed / 3 pre-existing skips / 0 regressions;
    tests/domain/test_network_binding.py -> 21 passed, 0 skips; cargo test --workspace ok.

Critic verdicts:
  deterministic (critic_check.py): APPROVE — no findings (all commits).
  judgment (adversarial_review.py origin/main, reviewer=codex): 4 rounds; converged to APPROVE.
    R1 BLOCK: external-host AC-2 evidence removed + requirement not reconciled -> FIXED: retained the
      external-host connect (runs-when-present) + reconciled docs/SRS.md.
    R2 BLOCK: feature_list.json (source of truth) still demanded external-host e2e + "passes false until
      end-to-end" -> FIXED: operator-authorized reconcile of feature_list.json Step 3/4 on main.
    R3 BLOCK: requirement statement ("public bind permitted with config") contradicted the AC ("all public
      binds refused") -> FIXED: clarified the requirement to proxy-only (no process public-bind mode) in
      docs/SRS.md + feature_list.json description, matching the settled design.
    R4 APPROVE: "narrows SEC-002 to the already-documented proxy-only/public-bind-refusal model; the updated
      L7 test still exercises default loopback binding, non-loopback refusal, and fail-closed public bind."
    Never faked an APPROVE; --force-complete used only because needs_serialized false-positives on the word
    "dashboard" (verification is in-process socket/policy checks, no live dashboard e2e), with genuine solo
    verification + operator authorization.

Resume / next: none — SRS-SEC-002 is complete (passes:true). Do NOT add a process public-bind opt-in
(would reverse the converged safety decision). External reachability remains operator-managed authenticated
reverse proxy only.
