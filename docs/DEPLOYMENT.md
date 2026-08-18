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
# Load .env into THIS shell (compose reads it via --env-file, but the mkdir below
# needs it in the shell), then provision the storage-tier host directories the
# compose stack bind-mounts — they must exist before `up`, or the volume mount fails:
set -a; . ./.env; set +a
mkdir -p "${ATP_SSD_DATA_DIR:?}" "${ATP_NAS_DATA_DIR:?}"
docker compose --env-file .env --profile phase1 up
```

**Credentials in staging/production (SRS-SEC-001).** The readiness gate
**rejects** any catalogued secret (`ATP_IB_ACCOUNT`, `ATP_SMTP_API_KEY`,
`ATP_PUSH_TOPIC`, `ATP_PUSH_TOKEN`, `DATABENTO_API_KEY`, `SHARADAR_API_KEY`) supplied as a real
plaintext value when `ATP_ENV` is `staging`/`production` — those credentials
must be encrypted at rest in the credential vault, never edited into `.env`.
Keep the placeholders in `.env` and seal the real secrets instead:

```bash
mkdir -p ./secrets && chmod 700 ./secrets
python -m atp_config.vault generate-key > ./secrets/atp.key && chmod 600 ./secrets/atp.key
ATP_IB_ACCOUNT=U... ATP_SMTP_API_KEY=... ATP_PUSH_TOPIC=... ATP_PUSH_TOKEN=... \
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
| `phase1-ntfy` *(profile `notify`, optional)* | `binwiederhier/ntfy` | SRS-NOTIF-001 IF-11 push endpoint |

`phase1-ntfy` is deliberately **absent** from the SRS-ARCH-004 `required_services`
metadata that `tools/deployment_check.py` enforces. That check tests
required ⊆ compose in one direction only, so an optional service is permitted but
unverified — which is the intent here: requiring it would force this deployment
shape on an operator who already runs ntfy elsewhere. Do not read its absence
from the metadata as drift.

The strategy runtime container is the canonical template the Strategy
Orchestrator clones for each live or paper strategy instance. Resource
profiles match SyRS SYS-11: live container ≤ 512 MB RAM and ≤ 0.25
CPU cores; paper container ≤ 300 MB RAM and ≤ 0.10 CPU cores.

**Least-privilege (SRS-SEC-003 / NFR-S5).** The template runs with no
privileged mode (`privileged: false`, all Linux capabilities dropped,
`no-new-privileges:true`), no host network access (no `network_mode: host`,
and confined to the `internal: true` `atp_strategy_net` network so it has no
host/LAN/internet egress), and no access to other strategy filesystems (own
writable root layer, read-only SSD/NAS tiers, no host Docker socket, no
`volumes_from`, no credential-vault mount). `tools/container_isolation_check.py`
enforces this statically in CI. See `SECURITY.md` § "Least-privilege strategy
containers (SRS-SEC-003)".

**Jupyter isolation (SRS-SEC-004 / NFR-S6).** The `phase1-jupyter` research
environment holds **no** brokerage/notification credentials (secrets blanked via
`x-atp-no-secrets`, no credential-vault mount) and has **no direct access to the
execution engine**: it is confined to a dedicated `internal: true`
`atp_research_net` network with no execution-API peer (the execution engine and IB
Gateway are on the default bridge, off this network), so it can open no socket to a
broker and submit no live order. Its SSD/NAS data mounts are read-only. The
dashboard→Jupyter proxy (IF-13 / SRS-RES-001) preserves a one-way boundary — the
live-control-bearing `phase1-dashboard-api` (SRS-API-001) is **never** placed on
`atp_research_net`, or Jupyter would reach the operator kill-switch / Hot-Swap REST.
`tools/jupyter_isolation_check.py` enforces statically in CI that the execution
engine, the IB Gateway, and the dashboard/API are never on Jupyter's network (and
that no peer shares its network namespace). See `SECURITY.md` § "Jupyter
research-environment isolation (SRS-SEC-004)".

**Embedded research environment (SRS-RES-001 / IF-13).** JupyterLab is reachable
**from the dashboard, not directly**: the operator browser opens the dashboard at
`http://127.0.0.1:8080/dashboard`, and the Research panel embeds JupyterLab in a
same-origin iframe under the `/research/` prefix. The chain is
`browser → dashboard-api (runtime reverse proxy, /research/*) →
phase1-research-proxy (one-way L4 hop on the internal atp_research_edge_net) →
phase1-jupyter (internal atp_research_net, base_url=/research/)`:

- `phase1-dashboard-api` gets `ATP_RESEARCH_UPSTREAM=http://phase1-research-proxy:8890`
  (declarative until that image's CMD serves `python -m atp_dashboard`; the
  dashboard-api image CMD is still the compile-only stub owned by the deployment
  feature). Unset, the dashboard renders an honest "not configured" research cell
  and registers no proxy route.
- `phase1-research-proxy` runs `python -m atp_research_proxy` — a stdlib TCP
  forwarder whose upstream is fixed at `phase1-jupyter:8888`; it holds no secrets,
  no volumes, no published ports.
- `phase1-jupyter` publishes **no host port** (IF-13: not a standalone endpoint)
  and runs JupyterLab token-less (`--IdentityProvider.token=`): the auth boundary
  is the network path itself — only the loopback-bound dashboard reaches the
  research proxy, and only the research proxy reaches Jupyter. Injecting a token
  env would contradict the SRS-SEC-004 no-secrets stance the container is checked
  against. IF-13's "proxied through dashboard HTTPS" refers to the operator's
  documented external layer: HTTPS termination, like all external exposure, is the
  operator-managed authenticated reverse proxy in front of the loopback bind
  (NFR-S3 / SRS-SEC-002) — the runtime itself speaks HTTP on loopback.

- `phase1-ntfy` is **not** in the `phase1` profile — the operator may already run
  ntfy elsewhere on the LAN, and publishing an alert endpoint on a LAN interface
  should be deliberate rather than a side effect of the default bring-up. It takes
  no ATP credentials (an explicit `NTFY_*` environment rather than `*atp-env`), so
  it needs neither the vault mount nor a place in the `x-atp-no-secrets` blanking
  list. ATP reaches it by the **host's** RFC 1918 address, never by service name —
  see *Standing up the IF-11 push endpoint* below for why the transport refuses a
  hostname.

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

**Provisioning (required before `up`).** The `atp_ssd` / `atp_nas` volumes are
bind mounts onto the host paths `${ATP_SSD_DATA_DIR}` / `${ATP_NAS_DATA_DIR}`, so
those directories **must exist before `docker compose up`** — otherwise the
volume mount fails with `no such file or directory` and the containers stay in
`created` state instead of starting. Create them as part of bring-up — after
loading `.env` into the shell (`set -a; . ./.env; set +a`), run
`mkdir -p "$ATP_SSD_DATA_DIR" "$ATP_NAS_DATA_DIR"` (see *Bring-up commands*). On
the Phase 1 Proxmox VM these are the local SSD and NAS mount points; a
development host can point them at any writable directory. Because a Docker named
volume caches its bind target on first creation, **changing** `ATP_SSD_DATA_DIR` /
`ATP_NAS_DATA_DIR` after the `atp_ssd` / `atp_nas` volumes already exist requires
`docker compose --profile phase1 down -v` (to drop the stale volumes) before the
new path takes effect.

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
- `ATP_SMTP_API_KEY`, `ATP_PUSH_TOPIC`, `ATP_PUSH_TOKEN` — notification channel
  credentials.
- `DATABENTO_API_KEY`, `SHARADAR_API_KEY` — vendor data provider
  credentials, isolated behind adapter interfaces (SRS-ARCH-003).

The six secret keys (`ATP_IB_ACCOUNT`, `ATP_SMTP_API_KEY`, `ATP_PUSH_TOPIC`, `ATP_PUSH_TOKEN`,
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

## Standing up the IF-11 push endpoint (ntfy)

SRS-NOTIF-001 fans every operator alert to **email (IF-10) and push (IF-11)**,
and `REQUIRED_CHANNELS` is enforced fail-closed — if push is unconfigured,
nothing is sent on either channel. This is how you stand push up.

Push replaced SMS as IF-11 on 2026-08-17. US A2P 10DLC registration is weeks of
lead time and carriers filter unregistered traffic silently, so an SMS channel
could not be shown to deliver; a self-hosted ntfy on the LAN, reached from the
operator's phone over the VPN, meets StRS SN-1.12's intent without a carrier in
the path.

### Where it runs, and why that is forced

Run ntfy **on the Proxmox VM, bound to the VM's RFC 1918 LAN address** — not
reached by compose service name.

`PushConfig::new` refuses a hostname and requires a loopback / RFC 1918 **IP
literal**. That is deliberate: a name cannot be shown to stay private without
resolving it, and a resolution performed at startup does not bind what the name
resolves to at send time. The alternative — validating only at send time — means
the misconfiguration surfaces as the connectivity-loss alert that never arrives,
during the incident it exists to report.

The phone needs a routable LAN address over the VPN regardless, so one address
serves both the phone and the ATP containers.

### 1. Bring up the server

The stack ships an optional ntfy service under the `notify` profile. It is not in
the `phase1` profile because you may already run ntfy elsewhere, and because
publishing on a LAN interface should be a deliberate act:

```bash
# in .env — the loopback default cannot reach your phone
ATP_NTFY_BIND=192.168.1.50        # this VM's RFC 1918 address
ATP_NTFY_PORT=8090

docker compose --env-file .env --profile phase1 --profile notify up -d
```

To run it outside compose instead:

```bash
docker run -d --name atp-ntfy --restart unless-stopped \
  -p 192.168.1.50:8090:80 \
  -v atp_ntfy:/var/lib/ntfy \
  -e NTFY_BASE_URL=http://192.168.1.50:8090 \
  -e NTFY_CACHE_FILE=/var/lib/ntfy/cache.db \
  -e NTFY_AUTH_FILE=/var/lib/ntfy/auth.db \
  -e NTFY_AUTH_DEFAULT_ACCESS=deny-all \
  binwiederhier/ntfy:v2.27.0 serve
```

Pin the version rather than tracking `:latest`. This image sits on the alert path,
and 2.27.0 is what the behaviours documented below were reproduced against.

The two paths use the **same container name** (`atp-ntfy`) so the commands below
work either way, but *not* the same volume: compose creates the project-prefixed
`<project>_atp_ntfy` while the bare-docker line above uses a plain `atp_ntfy`.
Switching paths therefore lands on an empty `auth.db`, and every alert 401s until
you recreate the user, ACL and token. Pick one path and stay on it.

`NTFY_AUTH_DEFAULT_ACCESS=deny-all` is load-bearing. Without it every topic on
the instance is publishable by anyone who can route to it, and on ntfy the topic
alone is enough to publish — so an unauthenticated instance lets anyone who
learns or guesses the topic forge an operator alert.

### 2. Create the topic, the user and the token

You need **two identities**, not one. ATP publishes and must not be able to read;
the phone subscribes and must not be able to publish. Granting one account both
is what most ntfy walkthroughs do, and it means a leaked publishing token can
also read your entire alert history.

```bash
TOPIC="atp-alerts-$(openssl rand -hex 16)"   # long + random: this is a credential
echo "$TOPIC"

# 1. ATP: write-only. This account's token goes in ATP_PUSH_TOKEN.
docker exec -e NTFY_PASSWORD='<atpbot-password>' atp-ntfy \
  ntfy user add --role=user atpbot
docker exec atp-ntfy ntfy access atpbot "$TOPIC" wo
docker exec atp-ntfy ntfy token add atpbot         # prints tk_... for ATP_PUSH_TOKEN

# 2. The phone: read-only. This is the account you sign the ntfy APP in as.
docker exec -e NTFY_PASSWORD='<operator-password>' atp-ntfy \
  ntfy user add --role=user operator
docker exec atp-ntfy ntfy access operator "$TOPIC" ro
```

Confirm the split took:

```bash
docker exec atp-ntfy ntfy access        # atpbot: write-only, operator: read-only
```

Three things that are easy to get wrong:

- **`NTFY_PASSWORD` is required.** Without it `ntfy user add` tries to prompt and
  fails with `inappropriate ioctl for device` — there is no TTY under
  `docker exec`.
- **Do not sign the phone in as `atpbot`.** Under `deny-all` a write-only account
  cannot subscribe, so the app is refused with 403 and shows nothing — while
  ATP's publishes keep returning HTTP 200 with a message id. That failure is
  invisible from the ATP side and looks exactly like a working alert path. It is
  the acceptance-is-not-receipt gap in its most literal form.

  Measured against ntfy 2.27.0 with `deny-all`, so this is not a guess:

  | account | grant | publish | subscribe |
  |---|---|---|---|
  | `atpbot` | `wo` | `200` | **`403`** |
  | `operator` | `ro` | **`403`** | `200` |
- **The topic must use ntfy's alphabet** (ASCII letters, digits, `-`, `_`). It
  becomes the URL path, so a `/`, `?`, `#` or space would retarget the publish;
  startup readiness rejects anything else, and the rejection never echoes the
  value.

### 3. Subscribe the phone

Install the ntfy app, add server `http://192.168.1.50:8090`, and sign in as
**`operator`** — the read-only account, not `atpbot`. Subscribe to the topic.
Connect the VPN first.

Then prove the two halves separately, because they fail differently:

```bash
# publish as ATP would (write-only token) — expect HTTP 200 + a JSON "id"
curl -sS -H "Authorization: Bearer $ATP_PUSH_TOKEN" -d "setup check" \
  "http://192.168.1.50:8090/$TOPIC"
```

The phone should display it. If the publish returns 200 and the phone shows
nothing, the publish half is fine and the subscription half is not — check that
the app is signed in as `operator` and that `operator` holds `ro` on this topic.

### 4. Point ATP at it

```
ATP_PUSH_HOST=192.168.1.50      # IP literal, RFC 1918 — not a hostname
ATP_PUSH_PORT=8090
ATP_PUSH_TOPIC=<the topic>      # secret
ATP_PUSH_TOKEN=tk_...           # secret
```

Confirm the endpoint before ATP touches it:

```bash
curl -v -H "Authorization: Bearer $ATP_PUSH_TOKEN" -d "hello" \
  "http://192.168.1.50:8090/$ATP_PUSH_TOPIC"
```

Expect `HTTP 200` and a JSON body carrying `"id"`. `403` means the ACL did not
take; `401` means the token is wrong.

### The vault interaction, which will bite you at flip time

`ATP_PUSH_TOPIC` and `ATP_PUSH_TOKEN` are catalogued secrets and belong in the
encrypted vault for the stack. But `notif001_operator_alert_cli` **cannot open
the vault** — the vault is a Python component and that binary is the Rust
composition root. Rather than publish with whatever is in the environment, it
refuses to run when `ATP_ENV` is `staging` or `production` and any notification
credential is still the catalogue placeholder.

So for an operator alert run, export the real values into the environment for
that run. Sealing them in the vault covers the long-running services; it does not
cover this binary.

### Two ntfy behaviours the transport defends against

Both were reproduced against a live server, and both are silent — the request
still returns `HTTP 200` with a valid message id:

- **A body of 4,096 bytes or more becomes a file attachment.** ntfy's docs say
  "greater than 4,096"; it is actually inclusive. On conversion the alert text is
  replaced by "You received a file: attachment.txt", so the operator's phone shows
  a filename instead of the outage. The transport caps bodies at 1,024 bytes and
  additionally *fails* a 2xx that comes back carrying an attachment.
- **An empty body becomes the literal word "triggered".** An empty composition is
  replaced with an explicit marker instead.

### What this does not give you

Push reaching ntfy is not push reaching the operator: acceptance is not receipt,
and it does not prove the phone was subscribed, online, or that the notification
was displayed. Email is a separate gap — it still needs the
`phase1-notification-egress` relay, which is not built. Until that exists an
operator alert run shows `email=Failed` and exits non-zero while push delivers.

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
| `notification_channels` | `ATP_SMTP_API_KEY`, `ATP_OPERATOR_EMAIL`, `ATP_PUSH_HOST`, `ATP_PUSH_PORT`, `ATP_PUSH_TOPIC`, `ATP_PUSH_TOKEN` |

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
