# ATP Phase 1 Deployment

This document satisfies SRS-ARCH-004 by describing how ATP is deployed
as a Docker Compose stack on the Phase 1 Proxmox Ubuntu VM target, and
by enumerating the portability constraints that future cloud
deployments will need to address.

## Phase 1 target: Proxmox Ubuntu VM

The binding Phase 1 deployment target is a Proxmox-hosted Ubuntu VM
(SyRS AC-13). The reference hardware is a single i5-12400 host with
32 GB RAM and a 1 TB primary SSD plus NAS-mounted archive storage
(SyRS reference-baseline §). All ATP runtime services execute on that
single host inside Docker containers managed by Docker Compose
(SyRS AC-12, SyRS SYS-10).

Cloud VPS deployment is a future target outside the release baseline.
StRS SN-2.07 identifies cloud deployment as a target state, and
SRS §10.4 records that cloud VPS deployment is not a release-baseline
software requirement. The Phase 1 stack is therefore designed for the
Proxmox VM first, with portability constraints documented below so
that a future cloud deployment is not precluded.

## Bring-up commands

```bash
cp .env.example .env
# Edit .env to set IB account ports, SSD/NAS paths, and ATP_ENV.
# Development: leave the catalogued secrets as the placeholder value.
docker compose --env-file .env --profile phase1 up
```

**Credentials in staging/production (SRS-SEC-001).** The readiness gate
**rejects** any catalogued secret (`ATP_IB_ACCOUNT`, `ATP_SMTP_API_KEY`,
`ATP_SMS_API_KEY`, `DATABENTO_API_KEY`, `SHARADAR_API_KEY`) supplied as a real
plaintext value when `ATP_ENV` is `staging`/`production` — those credentials
must be encrypted at rest in the credential vault, never edited into `.env`.
Keep the placeholders in `.env` and seal the real secrets instead:

```bash
mkdir -p ./secrets && chmod 700 ./secrets
python -m atp_config.vault generate-key > ./secrets/atp.key && chmod 600 ./secrets/atp.key
ATP_IB_ACCOUNT=U... ATP_SMTP_API_KEY=... ATP_SMS_API_KEY=... \
  DATABENTO_API_KEY=... SHARADAR_API_KEY=... \
  ATP_VAULT_KEY_FILE=./secrets/atp.key \
  python -m atp_config.vault seal ./secrets/atp.vault
# Point the stack at the read-only in-container mount (compose bind-mounts
# ${ATP_SECRETS_DIR} at /run/atp-secrets):
#   ATP_SECRETS_DIR=./secrets
#   ATP_VAULT_FILE=/run/atp-secrets/atp.vault
#   ATP_VAULT_KEY_FILE=/run/atp-secrets/atp.key
```

At startup `load_vault_into_env` decrypts the vault into memory (fail-closed on a
wrong key), so no plaintext credential sits on disk. Development
(`ATP_ENV=development`) keeps plaintext-env flexibility and does not require the
vault.

The `phase1` profile gates the entire deployment stack so the existing
`architecture-check` profile used by SRS-ARCH-001 remains independent.

`./init.sh` includes a static evidence check (`tools/deployment_check.py`)
that verifies the compose file, env template, and this document remain
consistent with the SRS-ARCH-004 metadata block in
`architecture/runtime_services.json`.

## Service inventory

| Compose service | Image | SRS reference |
|---|---|---|
| `phase1-orchestrator` | `docker/core-runtime.Dockerfile` (atp-orchestrator) | SRS-ORCH-001 |
| `phase1-execution-engine` | `docker/core-runtime.Dockerfile` (atp-execution) | SRS-EXE-001 |
| `phase1-strategy-engine` | `docker/core-runtime.Dockerfile` (atp-strategy-engine) | SRS-ARCH-001 |
| `phase1-simulation-engine` | `docker/core-runtime.Dockerfile` (atp-simulation) | SRS-SIM-001 |
| `phase1-market-data` | `docker/core-runtime.Dockerfile` (atp-market-data) | SRS-MD-001 |
| `phase1-data-layer` | `docker/core-runtime.Dockerfile` (atp-data) | SRS-DATA-001 |
| `phase1-factor-pipeline` | `docker/core-runtime.Dockerfile` (atp-factor-pipeline) | SRS-FACT-001 |
| `phase1-notification-dispatcher` | `docker/core-runtime.Dockerfile` (atp-notification) | SRS-NOTIF-001 |
| `phase1-dashboard-api` | `docker/dashboard-api.Dockerfile` | SRS-SEC-002 (loopback bind) |
| `phase1-jupyter` | `docker/jupyter.Dockerfile` | SRS-RES-001, SRS-SEC-004 |
| `phase1-ib-gateway` | `docker/ib-gateway.Dockerfile` (operator-supplied in production) | SRS-EXE-006 |
| `phase1-strategy-runtime` | `docker/strategy-python.Dockerfile` | SRS-ORCH-001, SyRS SYS-11, SRS-SEC-003 (least-privilege) |

The strategy runtime container is the canonical template the Strategy
Orchestrator clones for each live or paper strategy instance. Resource
profiles match SyRS SYS-11: live container ≤ 512 MB RAM and ≤ 0.25
CPU cores; paper container ≤ 300 MB RAM and ≤ 0.10 CPU cores.

**Least-privilege (SRS-SEC-003 / NFR-S5).** The template runs with no
privileged mode (`privileged: false`, all Linux capabilities dropped,
`no-new-privileges:true`), no host network access (no `network_mode: host`
— it joins only the isolated Compose project bridge), and no access to
other strategy filesystems (own writable root layer, read-only SSD/NAS
tiers, no host Docker socket, no `volumes_from`, no credential-vault mount).
`tools/container_isolation_check.py` enforces this statically in CI. See
`SECURITY.md` § "Least-privilege strategy containers (SRS-SEC-003)".

## Storage tiers

Phase 1 storage uses the SSD-primary, NAS-archive tiering described in
SRS-DATA-008 and SRS-DATA-009. The compose stack bind-mounts both tiers
into every service that needs them:

| Volume | Host path | Container path | Tier |
|---|---|---|---|
| `atp_ssd` | `${ATP_SSD_DATA_DIR}` | `/ssd` | Primary runtime tier |
| `atp_nas` | `${ATP_NAS_DATA_DIR}` | `/nas` | Archive tier |

The Jupyter service (SRS-SEC-004) and the strategy-runtime service
(SRS-SEC-003) mount both paths read-only; the remaining core services
receive read-write mounts. The data layer is the only component that
writes to NAS; other services read through the unified data interface.

## Environment-specific configuration

All ATP services are configured exclusively through environment
variables sourced from `.env` (SRS-ARCH-005). The required keys are:

- `ATP_ENV` — deployment selector (development / staging / production).
- `ATP_IB_HOST`, `ATP_IB_LIVE_PORT`, `ATP_IB_PAPER_PORT` — IB Gateway
  endpoints; live and paper run on separate ports per SyRS AC-15.
- `ATP_IB_ACCOUNT` — IB brokerage account identifier (secret; SRS-SEC-001).
- `ATP_MARKET_DATA_LINE_LIMIT` — IB market-data line cap.
- `ATP_SSD_DATA_DIR`, `ATP_NAS_DATA_DIR` — host-side bind paths for the
  storage tiers.
- `ATP_SMTP_API_KEY`, `ATP_SMS_API_KEY` — notification channel
  credentials.
- `DATABENTO_API_KEY`, `SHARADAR_API_KEY` — vendor data provider
  credentials, isolated behind adapter interfaces (SRS-ARCH-003).

The five secret keys (`ATP_IB_ACCOUNT`, `ATP_SMTP_API_KEY`, `ATP_SMS_API_KEY`,
`DATABENTO_API_KEY`, `SHARADAR_API_KEY`) must be sealed in the encrypted
credential vault for staging/production (see *Bring-up commands* above),
delivered via `ATP_VAULT_FILE` / `ATP_VAULT_KEY_FILE` and the read-only
`/run/atp-secrets` mount — never as plaintext `.env` values (SRS-SEC-001).

The dashboard/API service binds to `127.0.0.1:8080` by default
(SRS-SEC-002) and exposes no process-level public-bind mode — a
non-loopback / non-RFC 1918 host fails closed. Making it reachable
beyond the local network requires the operator to front the loopback
bind with an authenticated reverse proxy (the explicit operator
configuration and documented external authentication SRS-SEC-002
mandates); that proxy is out of scope for the Phase 1 baseline.

## Portability constraints for future deployment

A future cloud VPS deployment must address each of the following
Phase 1 assumptions. They are recorded here so that SRS-ARCH-004's
acceptance criterion is met without precluding a later cloud target.

1. **Local-filesystem storage tiers.** `atp_ssd` and `atp_nas` are
   bind-mounted from host directories. A cloud VPS deployment must
   either preserve attached block storage with comparable IOPS or
   introduce an object-store adapter behind the data layer.
2. **Co-located IB Gateway.** `phase1-ib-gateway` runs on the same
   Docker network as the runtime services. A cloud deployment must
   either co-locate IB Gateway in the same VPC, expose it through a
   tunneled endpoint, or run a managed equivalent. Live trading
   network egress to IB endpoints must be permitted.
3. **Docker daemon and cgroup-based isolation.** The Strategy
   Orchestrator drives strategy lifecycle through the host Docker
   daemon and depends on Linux cgroup-based resource enforcement
   (SyRS SYS-11). A managed-container target (Kubernetes, ECS, Cloud
   Run) would require replacing the orchestrator's direct Docker
   integration with the platform's native API while preserving the
   single-live-strategy invariant.
4. **Reference-hardware resource profiles.** Live and paper resource
   limits are tuned to the reference Proxmox VM. Cloud VPS instance
   sizing must be re-derived from measured runtime resource use, not
   copied verbatim.
5. **Loopback-only network exposure.** SRS-SEC-002 requires the
   dashboard/API to bind to RFC 1918 or loopback addresses by default.
   The dashboard/API process itself provides no public-bind mode (a
   non-loopback / non-RFC 1918 host fails closed with `BindPolicyError`);
   publicly-routable reachability is possible only by the operator
   placing an authenticated reverse proxy in front of that loopback /
   RFC 1918 bind — the explicit operator configuration and documented
   external authentication SRS-SEC-002 requires.
6. **Single-host log and time assumptions.** Phase 1 logs and clock
   sources are local. Cloud deployment will need centralised log
   aggregation and confirmed clock skew bounds before live trading
   timestamps can be relied upon for reconciliation.

These constraints are validated by `tools/deployment_check.py`, which
fails if this document loses any of the keywords that anchor the
portability discussion.

## Configuration system (SRS-ARCH-005)

The configuration system is the declarative catalogue of every required
deployment variable plus a startup validator that surfaces structured
readiness failures. The catalogue lives in the `configuration` block of
`architecture/runtime_services.json`; the validator lives in
`python/atp_config`. Nineteen keys are catalogued across six categories:

| Category | Keys |
|---|---|
| `credentials` | `DATABENTO_API_KEY`, `SHARADAR_API_KEY` |
| `storage_paths` | `ATP_SSD_DATA_DIR`, `ATP_NAS_DATA_DIR`, `ATP_BACKTEST_RESULTS_DIR`, `ATP_DATA_STORE_DIR` |
| `ib_account` | `ATP_ENV`, `ATP_IB_HOST`, `ATP_IB_LIVE_PORT`, `ATP_IB_PAPER_PORT`, `ATP_IB_ACCOUNT` |
| `market_data_limits` | `ATP_MARKET_DATA_LINE_LIMIT` |
| `resource_limits` | `ATP_LIVE_STRATEGY_MEM_MB`, `ATP_LIVE_STRATEGY_CPU`, `ATP_PAPER_STRATEGY_MEM_MB`, `ATP_PAPER_STRATEGY_CPU`, `ATP_HOST_MEMORY_SAFETY_MARGIN_MB` |
| `notification_channels` | `ATP_SMTP_API_KEY`, `ATP_SMS_API_KEY` |

Every key is documented with a type (`int`, `float`, `path`, `host`,
`enum`, or `secret`), a validator (range bounds, absolute-path,
non-empty, enum membership), a default suitable for `init.sh` development
mode, and an SRS trace. Resource-limit defaults match the SRS-ORCH-002
profiles (live ≤ 512 MB / 0.25 CPU; paper ≤ 300 MB / 0.10 CPU) and the
SyRS SYS-57 host memory safety margin (2 GB). They drive the
`x-atp-env` anchor in `docker-compose.yml` for orchestrator consumption;
the strategy-runtime service's static `deploy.resources.limits` block
remains the template default and is *not* substituted from these
variables, because Compose's `memory:` field requires a unit suffix that
a raw integer value does not provide.

Secret keys default to the literal sentinel `placeholder-set-in-environment`.
The validator treats this as a non-blocking warning when
`ATP_ENV=development` and as a hard error when `ATP_ENV` is `staging` or
`production`, so dev shells continue to pass without leaking real
credentials and real deployments cannot start with placeholders.

Every readiness failure is structured:

```json
{
  "key": "ATP_MARKET_DATA_LINE_LIMIT",
  "category": "market_data_limits",
  "severity": "error",
  "reason": "expected integer, got 'oops'",
  "srs_trace": ["SRS-MD-002", "SyRS:SYS-70"]
}
```

`init.sh` chains `tools/config_check.py` after the deployment check, so
`✓ Environment ready` requires that every catalogued key parses, every
range bound holds, and `.env.example` lists every key. The same check is
aggregated into `tools/architecture_check.py` so the `SRS-ARCH-001 PASS`
output now includes a `SRS-ARCH-005 configuration system evidence:`
bullet group with one line per category.

Encryption-at-rest for credentials (NFR-S1, NFR-S4) and the live-trading
runtime readiness check (SyRS SYS-76, traced to SRS-MD-006) consume this
catalogue but are out of scope for SRS-ARCH-005 itself.
