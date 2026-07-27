=== SESSION SRS-EXE-007 ===
Date: 2026-07-27
Feature: SRS-EXE-007 — manage IB TWS API version compatibility for the brokerage adapter
         (docs/SRS.md:151 SRS-5.3; SyRS SYS-65 docs/SyRS_v0.7.md:355; StRS C-2, SN-3.02; API-5)
Outcome: serialized (passes stays false — clause 2's *demonstrated* paper-account upgrade run is
         operator-gated: ATP_RUN_INTEGRATION=1 against port 4002, which cannot run beside siblings)

## Context — what already existed vs. the hole this session closed

Clause 1 ("the adapter documents the supported IB TWS API version") was ALREADY satisfied by
SRS-EXE-006's contract surface: `INTERACTIVE_BROKERS_TWS_API_VERSION = "10.19.4"`
(crates/atp-adapters/src/lib.rs:148) exposed via `AdapterVersion`, mirrored by
`adapter_contract.interactive_brokers.protocol_version` in architecture/runtime_services.json,
prose in architecture/README.md, and cross-checked by tools/adapter_check.py. The distinct
negotiated wire pin (`IB_PINNED_SERVER_VERSION = 176`) is checked by tools/ib_adapter_check.py.

Clause 2 — "API version upgrades are tested against the IB paper trading account BEFORE deployment
to live trading" — had NO enforcement, and it was a fail-open hole:

  The operator paper-account evidence artifact (architecture/ib_paper_account_evidence.json) is
  bound by `code_digest` to the WIRE SOURCES ONLY (interactive_brokers.rs + wire.rs + the EXE-006
  integration test). The declared TWS API version string was bound to nothing a paper run produces.
  So editing the constant from "10.19.4" to any other value + the matching metadata value passed
  adapter_check.py, ib_adapter_check.py and the whole CI mirror — an unvalidated API-version
  upgrade shipped green. tools/ib_adapter_check.py already named this as owed work ("SRS-EXE-007
  version-upgrade regression" must stay enumerated in deferred[]).

## What I did

1. **Validation record — architecture/ib_api_version_support.json (new).** Declares the supported
   TWS API version + negotiated server version and cites the paper-account evidence by
   `evidence_code_digest` + `evidence_generated_at`. Seeded at today's ALREADY-VERIFIED state
   (10.19.4 / server 176, evidence of 2026-07-16T03:11:09Z from the operator-run EXE-006 round
   trip); `provenance` names that origin explicitly rather than implying a run happened here.
2. **The gate — tools/ib_api_version_check.py (new).** Pure inspection (no network, no subprocess,
   no IB), fail-closed on every unreadable shape (missing / unparseable / duplicate-keyed /
   schema-drifted / unknown-key / degenerate-value). Refuses when: the declared constant ≠ the
   validated version (THE upgrade gate); the runtime metadata disagrees; the wire pin disagrees
   across source/metadata/record; the cited digest ≠ the evidence artifact's current `code_digest`
   (a hand-edited citation is not validation); `generated_at` drifts; the evidence records a failed
   run; or the evidence is stale vs the current wire (digest recomputed by REUSING
   `ib_adapter_check._code_digest`, never a forked definition).
3. **Operator path — `--sync`.** Records a NEW paper run into the support record. It refuses when
   the evidence still carries the `generated_at` the record already cites, so bumping the version
   and re-syncing cannot launder an unvalidated upgrade: with no new run there is nothing to sync.
4. **CI wiring (both paths, per the project rule).** `ib_api_version` added to the check loop in
   .github/workflows/ci.yml and `ib_api_version_check` to tools/run_ci_locally.sh.
5. **Docs — architecture/README.md.** Replaced the old "change these two constants" paragraph
   (which described a bump with no validation step) with the ordered upgrade procedure: bump →
   CI is red ON PURPOSE → re-run the paper round trip → `--sync` → green → only then deploy live.
6. **The version↔run binding (added after adversarial round 1).** The operator path
   (`ib_adapter_check._write_evidence`) now records `tws_api_version` — read from the DECLARED
   constant at run time — into the evidence artifact, and the gate requires
   `evidence.tws_api_version == supported_tws_api_version` in both `run` and `--sync`. Without it
   the gate could be defeated: bump the constant + metadata + support record, re-run the paper
   round trip (which the version string does not affect), `--sync`, green — with no run ever
   having exercised the new API version. Codex r1 caught exactly this; it is now the primary
   binding, with the digest/timestamp citation as the secondary one.
   * The existing artifact was backfilled with `"tws_api_version": "10.19.4"`. That is a
     GIT-VERIFIABLE fact, not a hand-authored claim: commit e6f903a (2026-07-14) introduced both
     the constant at "10.19.4" and the evidence artifact, `git log -S` shows no later change to
     that constant, and the recorded run is dated 2026-07-16T03:11:09Z — so the run provably
     happened while 10.19.4 was declared. Every FUTURE artifact gets the field from the operator
     path, never by hand.
   * Verified that this does not flip closed-green SRS-EXE-006 red: `_evidence_is_valid` reads
     per-field with `.get()` (no strict key-set), and `tools/ib_adapter_check.py` still passes.
7. **Operator-run shape, in BOTH paths (added after adversarial round 2).** `--sync` was checking
   less than `run` did, which made it the weak way in. Both now call ONE shared
   `check_operator_run_shape(evidence, runtime)` requiring every field the operator writer stamps:
   the writer's `schema_version` (read from `ib_adapter_check.EVIDENCE_SCHEMA`, not re-declared),
   the operator-gated test name, the gate env var, the paper port (all compared against
   runtime_services.json), and a `result_line` carrying cargo's "test result: ok". A domain test
   asserts the shared checker is called from both paths so they cannot drift apart.

## Trust boundary — stated, not papered over (adversarial round 2 residual)

Codex r2 correctly observed that the evidence artifact is a repo file: the checks above defeat a
partial, careless or accidental edit, but they are integrity checks, NOT a cryptographic
attestation that a paper-account run occurred. Someone who edits the JSON carefully enough can
still satisfy them. What r2 recommended (a nonce/signature that ordinary edits cannot recreate)
cannot be built inside this feature honestly: any key committed to the repo is forgeable by
definition, and an operator-held signing key + a verification path that works without the private
key is a key-management design, not a version gate.

Scope + owner of the residual: this is the REPO-WIDE operator-evidence convention, not something
this feature introduced — SRS-EXE-006 (`ib_adapter_check.check_verified_status`), SRS-MD-006
(`EvidenceFileIbProbe` over the same artifact), SRS-REL-001 and SRS-DATA-017 all treat a committed
artifact as proof of an operator run, and my change strictly NARROWS what this one can bless
(three new required agreements). Designing signed/attested operator evidence spans all of them and
needs operator authorization on the key-management approach. Recorded here rather than silently
accepted; it is NOT claimed as solved.

## What I tested (per feature step)

Step 1 (init.sh → "Environment ready"): PASS — `./init.sh` → "✓ Environment ready".
  Env fix landed en route: the fresh worktree venv had no pytest (init.sh does not install
  requirements-dev.txt); installed it into .venv before testing.
Step 2 (exercise via fault-injection workflow with mocked services + CLI + logs): PASS — driven
  against a hermetic copy of the artifact tree (no IB, no ports):
  [1] baseline mock tree → `python3 tools/ib_api_version_check.py` → exit 0, GATE PASS.
  [2] inject the fault — bump the declared version 10.19.4 → 10.30.1 with no new paper run →
      exit 1, "IB TWS API version upgrade is NOT validated: the adapter declares '10.30.1' … but
      the support record validates '10.19.4'. Re-run the operator paper-account round trip …".
  [3] try to launder it — `--sync` with no new run → exit 1, "nothing to sync: … still records the
      run of 2026-07-16T03:11:09Z that … already cites".
  [4] forge ALL the paperwork — bump lib.rs + runtime metadata + the support record together,
      evidence untouched → exit 1, "the paper-account run recorded in … exercised '10.19.4', but
      … claims '10.30.1' is validated. Only a paper-account round trip run AT the declared
      version validates it".
  [5] a genuinely NEW run but performed at the OLD version (new `generated_at`, tws_api_version
      still 10.19.4) → `--sync` exit 1, "refusing to record an unvalidated upgrade: the
      paper-account run of 2026-08-01T12:00:00Z exercised IB TWS API '10.19.4', but the adapter
      now declares '10.30.1'".
  [6] the honest path — the operator re-runs WITH 10.30.1 declared (the writer records it) →
      `--sync` exit 0, "IB TWS API 10.30.1 recorded as validated … by the run of
      2026-08-01T12:00:00Z" → gate exit 0.
Step 3 (AC): clause 1 PASS (L3 contract test pins constant ↔ metadata ↔ record ↔ evidence, plus the
  documented procedure); clause 2 PARTIAL/serialized — the *policy* is now enforced and proven by
  fault injection, but the AC's literal demonstration ("upgrades ARE tested against the IB paper
  trading account") needs an operator ATP_RUN_INTEGRATION run on port 4002. Not runnable beside
  siblings, and no real API upgrade is pending. So passes stays false.
Step 4 (record evidence, leave passes false): DONE — evidence above; serialized.

Tests added: tests/unit/test_ib_api_version_gate.py (44 cases — every refusal branch, degenerate
  values, duplicate keys, --sync laundering); tests/test_ib_api_version_contract.py (L3, 8 cases —
  live-artifact agreement + CI wiring + documented procedure); tests/domain/
  test_exe007_ib_api_version_gate.py (L7, 8 cases — behavioral / structural non-vacuity / scope
  honesty, mirroring the SRS-EXE-006 domain-test shape). 60 passed.

Gate: tools/run_ci_locally.sh, cargo test --workspace, pytest -m "not integration and not e2e" —
  see the commit trailer for the recorded results.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (adversarial_review.py origin/main, reviewer=codex): r1 BLOCK → fixed (bound the
    evidence artifact to the declared TWS API version; see item 6). r2 BLOCK → fixed (`--sync` was
    checking less than `run`; both now share check_operator_run_shape; see item 7). r3 BLOCK →
    NOT fixed, OPERATOR-AUTHORIZED override, recorded honestly rather than re-run until green:

    r3's finding: the paper-account evidence is committed JSON, so a sufficiently careful
    hand-edit can still bless an upgrade without a real IB paper run. It is a true statement about
    the repo's operator-evidence convention, but (a) it is NOT introduced here — SRS-EXE-006,
    SRS-MD-006's EvidenceFileIbProbe, SRS-REL-001 and SRS-DATA-017 all trust the same class of
    committed artifact; (b) this change strictly NARROWS what the artifact can bless (three new
    required agreements); and (c) the remedy codex asks for — a signed/attested run artifact — is
    key-management design spanning every one of those features, not a version gate.

    OPERATOR AUTHORIZATION (2026-07-27): the operator was shown r3 verbatim with the four options
    (authorize + integrate serialized / build attestation first / land partial + park / drop) and
    chose to authorize the documented residual and integrate serialized. The verdict is recorded
    as BLOCK; it is NOT restated as an approval anywhere. `passes` stays false regardless.
    Named follow-up owner: whoever takes signed operator-evidence attestation (touches
    ib_adapter_check + MD-006 probes + REL-001 + DATA-017 together, plus SRS-SEC-001's vault for
    key custody).

## Do NOT touch
  crates/atp-adapters/src/interactive_brokers.rs, interactive_brokers/wire.rs and
  crates/atp-adapters/tests/srs_exe_006_ib_adapter.rs are SHA-256 pinned by tools/ib_adapter_check.py
  (editing them flips closed-green SRS-EXE-006 RED). This session READS them (version/digest) and
  writes none of them; ib_adapter_check passes.

## Resume / next (to flip passes:true)
  a. On the next real IB TWS API upgrade (or a deliberate rehearsal), follow architecture/README.md:
     bump the constant + metadata → `ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py`
     against the paper account (port 4002) → `python3 tools/ib_api_version_check.py --sync` →
     `python3 tools/ib_api_version_check.py` green. That operator run IS the clause-2 evidence;
     record it and close with `integrate --force-complete` / the verified-e2e label.
  b. DONE in this session (was going to be deferred; codex r1 blocked on it and was right): the
     evidence artifact now carries `tws_api_version` and the gate requires it to match the declared
     constant. Nothing further is owed on the binding itself.
