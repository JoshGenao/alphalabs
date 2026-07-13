=== SESSION SRS-SEC-004 ===
Date: 2026-07-13
Feature: SRS-SEC-004 — isolate Jupyter from live trading credentials and execution APIs
Outcome: complete (flip passes:true via integrate --mode complete)

Requirement (SyRS NFR-S6 / StRS SN-1.18, verification method "Security test"):
Jupyter has read-only access to market data + backtest results and cannot submit
live orders or read brokerage credentials. NFR-S6 refines to: no write access to
brokerage credentials, no direct access to the execution engine, no ability to
submit live orders, read-only data-layer access.

What I did (mirrors the SEC-003 static-compose-inspection convention I built):
- HARDENED docker-compose.yml (the change that makes the requirement provable):
  the pre-existing config already blanked secrets (*atp-no-secrets merged first),
  omitted the vault mount, and mounted /ssd,/nas :ro — but phase1-jupyter declared
  NO networks, so it sat on the default bridge shared with phase1-execution-engine
  and phase1-ib-gateway (a real open path to the execution APIs). Added a dedicated
  `atp_research_net` (driver bridge, internal:true) and put phase1-jupyter alone on
  it. Jupyter reads data from the :ro filesystem mounts (no network), so isolation
  costs no functionality.
- NEW tools/jupyter_isolation_check.py: fail-closed static compose inspection,
  config-driven (architecture/runtime_services.json → jupyter_isolation_contract),
  reusing container_isolation_check's hardened parser + deployment_check's credential
  helpers (no PyYAML). Asserts: credential env (x-atp-no-secrets merged FIRST — order,
  not just presence; anchor blanks every catalogued secret; no inline secret
  override), no vault/docker-socket/volumes_from, read-only-only data allow-list, no
  host/shared-namespace networking, and no execution-API network path (dedicated
  internal net, never default bridge, no forbidden peer sharing the network OR the
  network namespace). 19 `--fixture` negatives (one per bypass class).
- WIRED into architecture_check.py (import+call) + ci.yml `for check in` loop
  (`jupyter_isolation`) + run_ci_locally.sh backslash list (`jupyter_isolation_check`).
- TESTS: tests/domain/test_jupyter_credential_isolation.py (L7, non-skipping
  structural invariant + parametrized rejection over all 19 fixtures — 25 tests);
  tests/integration/test_jupyter_isolation_inspect.py (gated L5 docker-inspect,
  ATP_RUN_INTEGRATION=1, excluded from solo gate — operator's live effective-config
  proof: no vault mount, blanked secret env even when host .env is populated, RO
  tiers, internal non-default networks).
- DOCS: SECURITY.md § "Jupyter research-environment isolation (SRS-SEC-004)" (marker
  the check asserts) + docs/DEPLOYMENT.md note.

Key decisions:
- forbidden_network_peers = execution-engine + ib-gateway + dashboard-api. dashboard-api
  hosts SRS-API-001 live-control REST (kill switch / live designation / Hot-Swap), so
  it is execution-capable; the deferred dashboard->Jupyter proxy (IF-13 / SRS-RES-001)
  must preserve a ONE-WAY boundary and must NOT place dashboard-api on atp_research_net.
  The check fails closed if any future edit does.
- Complete (not serialized): all static checks + the non-skipping domain test pass
  solo; the concrete operator-supplied JupyterLab image + dashboard proxy are deferred,
  so the compose template is the authoritative source and static inspection is the
  "Security test" evidence — same convention SEC-003/ARCH-004 flipped complete under.

What I tested (per step):
- Step 1 (./init.sh): PASS — "✓ Environment ready".
- Step 2/3 (exercise + AC): PASS — `python3 tools/jupyter_isolation_check.py` → exit 0
  "SRS-SEC-004 PASS"; all 19 `--fixture <bypass>` → exit 1 "SRS-SEC-004 FAIL"; transitive
  via `python3 tools/architecture_check.py` → exit 0; no regression in deployment_check /
  container_isolation_check / network_binding_check.
- Step 4 (evidence, leave passes false on-branch): domain test 25 passed; broad pytest
  (domain+boundary+unit) 1440 passed, 3 pre-existing skips; full run_ci_locally.sh green
  (critic APPROVE, all architecture/contract checks incl. jupyter_isolation, cargo).

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (adversarial_review.py, reviewer=codex): APPROVE at round 4 after 3
  substantive fail-open fixes:
    r1 — peer sharing Jupyter's network NAMESPACE via network_mode:service:/container:
         (localhost path) → now refused peer-side (Jupyter side already refused). Fixture
         peer-shares-namespace.
    r2 — dashboard-api (SRS-API-001 live-control) documented as future net peer →
         added to forbidden_network_peers; reframed docs to one-way-proxy boundary.
         Fixture dashboard-shares-network.
    r3 — Compose MAP-style peer networks (`networks:\n  atp_research_net: {}`) read as
         "on default" by list-only parser → new _service_networks() parses list+map+flow,
         fail-closed on alias/interp. Fixture peer-map-network.

Gate notes (honest):
- ruff / mypy are dev-only gates NOT installed by init.sh (init.sh skips
  requirements-dev.txt), so the integrate path (run_ci_locally.sh) SKIPS them — that is
  how SEC-003 integrated. origin/main is already ruff+mypy red (pre-existing, unrelated
  files; see project_ci_red_behind_format_gates). I still made my 3 new files ruff
  check+format clean; my domain test's mypy profile matches the SEC-003 sibling (private-
  attr access / untyped fixture params). No net new debt in my files.

Resume / next: none — feature is complete. Operator may run the gated L5 proof:
  ATP_RUN_INTEGRATION=1 pytest tests/integration/test_jupyter_isolation_inspect.py
Deferred (owner SRS-RES-001 / SRS-ARCH-004): the concrete operator-supplied JupyterLab
image + the one-way dashboard->Jupyter proxy that must NOT put dashboard-api on
atp_research_net (the checker enforces this invariant when that wiring lands).
