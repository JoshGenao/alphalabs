=== SESSION SRS-SEC-003 ===
Date: 2026-07-12
Feature: SRS-SEC-003 — run strategy containers with least-privilege permissions
Outcome: complete (flip passes:true via integrate --mode complete)

AC (docs/SRS.md:251 / SyRS NFR-S5): strategy containers run without privileged
mode, without host network access, and without access to other strategy
containers' filesystems. Verification = "Inspection, security test" (grouped under
the repo's architecture tests → container isolation).

What I did:
- Hardened the `phase1-strategy-runtime` compose template (the declarative source
  the orchestrator clones per strategy — the concrete Docker StrategyContainerRuntime
  is deferred, so there is no live container path to exercise):
    * privileged: false; cap_drop [ALL]; security_opt no-new-privileges:true; no
      cap_add (nothing added back);
    * confined to a dedicated `atp_strategy_net` network (driver: bridge,
      internal: true) → NO host/LAN/internet egress; never network_mode: host;
    * SSD/NAS data tiers mounted READ-ONLY; own writable root layer; no host bind,
      no docker socket, no volumes_from, no credential vault.
- New `tools/container_isolation_check.py`: static compose-template inspection that
  fails closed on every violation. Config-driven via a new
  `architecture/runtime_services.json` container_isolation_contract. Wired into
  `tools/architecture_check.py` (run_checks), `.github/workflows/ci.yml` (Slot A),
  and `tools/run_ci_locally.sh` (Slot B). 18 negative --fixture self-tests.
- Tests: tests/domain/test_strategy_container_least_privilege.py (non-skipping
  structural invariant + 18 parametrized fixture-rejection cases, 25 total). Gated
  L5 tests/integration/test_strategy_container_inspect.py (docker compose create +
  docker inspect: Privileged false, NetworkMode != host, data tiers RO, each
  attached network Internal==true) — excluded from the solo gate, operator's
  effective-config proof.
- Docs: SECURITY.md "Least-privilege strategy containers (SRS-SEC-003)" +
  docs/DEPLOYMENT.md.

Kept the repo's no-YAML-dependency convention: a hard `import yaml` in
architecture_check's import chain would break every worktree that installs only
requirements.txt (init.sh does not install requirements-dev.txt). Instead the text
parser was hardened to parse or fail-closed on the COMPLETE class of Compose
constructs that resolve a value a direct-line read cannot see: flow lists, quoted /
YAML booleans, duplicate keys, long/flow volume syntax, service-level `<<:` merges,
`*` aliases and `${VAR}` interpolation on security keys, and `extends:` inheritance.
The gated docker-inspect test validates the fully-resolved effective config.

What I tested (per step):
  Step 1 (init.sh): PASS — "✓ Environment ready".
  Step 2 (inspection + automated checks): PASS — `python3 tools/container_isolation_check.py`
    → SRS-SEC-003 PASS; all 18 --fixture violations exit 1 (fail closed);
    `python3 tools/architecture_check.py` → SRS-ARCH-001 PASS (runs the new check).
  Step 3 (verify the 3 ACs): PASS — no-privileged / no-host-network(+no egress) /
    no-cross-strategy-filesystem proven by the structural check + domain test.
  Step 4 (evidence): recorded here + in commit messages.
  Regression: full solo suite `pytest -m "not integration and not e2e"` green;
    cargo test --workspace green (no .rs changes); deployment_check +
    network_binding_check still PASS; run_ci_locally.sh green.

Critic verdicts:
  deterministic (critic_check.py): APPROVE — no findings (every commit).
  judgment (adversarial_review.py, reviewer=codex): APPROVE at round 8, after 7
    rounds of substantive fixes — round 1 long/flow-syntax bind bypass; round 2
    confine to internal no-egress network (default bridge had egress); round 3
    strict volume allow-list (extra shared named volume was a cross-strategy
    channel); round 4 flow cap_add / quoted privileged / duplicate keys; round 5
    internal-network direct-child scalar + reject any cap_add; round 6
    merge/alias/interpolation fail-closed; round 7 refuse extends. Each fix has a
    paired negative fixture + domain assertion.

Resume / next: DONE for the autonomous slice. Follow-ups owned elsewhere:
  * Concrete Docker-backed StrategyContainerRuntime that APPLIES this template at
    container-create time, and wires the specific internal services a strategy may
    reach via the SYS-12 interface onto atp_strategy_net (owner SRS-ARCH-004 /
    SRS-ORCH-002). Once it lands, the operator can run
    ATP_RUN_INTEGRATION=1 pytest tests/integration/test_strategy_container_inspect.py
    for the live docker-inspect proof.
  * Future hardening (documented, not applied): read_only root fs + tmpfs scratch.
