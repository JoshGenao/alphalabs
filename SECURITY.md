# Security Policy

## Reporting a vulnerability

**Do not open public GitHub issues for security vulnerabilities.**

Report privately using GitHub's **"Report a vulnerability"** button on the
[Security tab](../../security/advisories/new) of this repository. This opens
a private advisory visible only to the maintainer and people you explicitly
add to the thread.

Please include:

- A description of the issue and its impact
- Steps to reproduce (or a proof-of-concept)
- The commit SHA or branch where you observed it
- Any suggested remediation, if you have one

You can expect an initial response within **7 days**. If you do not hear
back within that window, please re-submit through the same channel — the
maintainer may have missed the notification.

## Supported versions

This project is in active early development. Only the `main` branch
receives security updates; there are no stable release branches yet.

| Version | Supported |
| ------- | --------- |
| `main`  | Yes       |
| Any tag | No        |

## Scope

**In scope:**

- The ATP runtime (Rust crates under `crates/`)
- The Python strategy boundary (`python/atp_*`)
- The CI/CD workflows under `.github/workflows/`
- The Critic Agent and pre-commit hook (`tools/critic_check.py`,
  `tools/install_hooks.sh`)

**Out of scope:**

- Vulnerabilities in third-party brokerage or market-data APIs
  (Interactive Brokers, Databento, Sharadar) — report those to the
  respective vendor.
- Findings that require running with `ATP_CRITIC_BYPASS=1` or
  `git commit --no-verify`. These are explicit human-override paths
  documented in `AGENTS.md`; their grep-ability in shell history is
  intentional, not a vulnerability.
- Denial-of-service against a single-user local deployment that
  requires already-authenticated local access.

## Credential handling (SRS-SEC-001)

Brokerage (IB account) and notification (SMTP, SMS) credentials — plus the
vendor data-provider keys — are treated as secrets end to end (NFR-S1, NFR-S4):

- **Encryption at rest.** The credential vault
  (`python/atp_config/vault.py`) seals secrets into an encrypted file using
  Fernet (AES-128-CBC + HMAC-SHA256 authenticated encryption). The key comes
  from a `0600` key file (`ATP_VAULT_KEY_FILE`) or an `ATP_VAULT_PASSPHRASE`
  (scrypt-derived). When `ATP_VAULT_FILE` is set, `load_vault_into_env`
  decrypts the secrets into memory at startup, so an operator can run the
  stack **without** a plaintext `.env` on the host. Decryption is fail-closed:
  a wrong key or tampered file yields no plaintext. Set the vault up with
  `python -m atp_config.vault generate-key` / `... seal <file>`.
  **Enforced in production:** in `staging`/`production` the readiness gate
  rejects any catalogued secret supplied as a real plaintext value — those
  credentials must be sealed in the vault — so encryption at rest cannot be
  silently bypassed. Development keeps plaintext-env flexibility.
- **No plaintext credential logging.** The redaction layer
  (`python/atp_logging/redaction.py`) is installed on the SRS-LOG-001
  dispatcher and both persistent stores, scrubbing secret values (and
  secret-shaped tokens) from log records before they are written. The
  persistence boundary is **never** zero-redaction: a store built without an
  injected redactor falls back to an always-on pattern-based floor
  (`DEFAULT_REDACTOR`). The sanctioned production boot path is
  `atp_logging_boot.build_boot_log_dispatcher(dir, env)`, which overlays the
  vault and then builds the value-aware `SecretRedactor(secret_values(env))`
  itself — so bare IB/SMTP/SMS credential *values* are masked without the caller
  injecting anything. The operator-facing config view separately renders secret
  keys as `***REDACTED***`.
- **Enforcement.** `tools/credential_security_check.py` (in CI) proves the
  vault produces ciphertext and that redaction is wired on every path; the
  L7 `tests/domain/test_credential_redaction.py` proves IB/SMTP/SMS secrets
  never reach the logs. Committed secrets are independently blocked by
  `tools/critic_check.py` + `.gitleaks.toml`.

The concrete Rust SMTP/SMS channel adapters (which will read these vault-sealed
keys) remain deferred to the SRS-NOTIF-001 adapter work; this feature provides
the at-rest + redaction mechanism they consume.

## Network binding (SRS-SEC-002)

The dashboard/API service — the only ATP process that opens a listening socket
(it serves the REST API and WebSocket surface on one port) — binds **only to
loopback or RFC 1918 addresses** and refuses anything else (NFR-S3, StRS
SN-2.01):

- **Default-safe bind.** `runtime.start` defaults to `127.0.0.1`, and
  `python -m atp_dashboard` reads `ATP_DASHBOARD_BIND_HOST` (default
  `127.0.0.1`). The dashboard/API service publishes a **fixed** loopback
  mapping (`127.0.0.1:8080:8080`) with no operator-overrideable host, and
  Jupyter likewise (`127.0.0.1:8888:8888`). No compose service publishes a bare
  `PORT:PORT` (which would bind `0.0.0.0`), and every published-port default
  host is loopback / RFC 1918. (The IB-gateway ports default to loopback via
  `${ATP_IB_HOST:-127.0.0.1}`; changing `ATP_IB_HOST` is an operator action for
  the broker connection, outside the dashboard/API SRS-SEC-002 boundary.)
- **Fail-closed policy — no process-level public bind.** `is_allowed_bind_host`
  / `assert_bind_allowed` (`python/atp_runtime/rest_server.py`) permit only
  loopback (`127.0.0.0/8`, `::1`) and the three RFC 1918 IPv4 ranges; `0.0.0.0`,
  `::`, link-local, CGNAT, and any publicly-routable address raise
  `BindPolicyError` **before** the socket opens. The runtime deliberately exposes
  **no** flag or environment variable that binds the process itself to a public
  interface.
- **External exposure is auth-gated and operator-managed.** Because the process
  never binds a public interface, the *only* way to reach the dashboard from
  beyond the local network is for the operator to place an **authenticated
  access-control component (e.g. a reverse proxy)** in front of the loopback /
  RFC 1918 bind (NFR-S3; OWASP authentication guidance). That authenticated
  reverse proxy **is** the "explicit operator configuration and documented
  external authentication" SRS-SEC-002 requires for publicly-routable access —
  the ATP process stays bound to loopback / RFC 1918 behind it. See
  `docs/DEPLOYMENT.md` (portability constraint 5).
- **Enforcement.** `tools/network_binding_check.py` (in CI) proves the compose
  mappings are loopback/RFC 1918-bound, that no source binds all interfaces,
  that the policy fails closed, and that this external-authentication
  requirement is documented; the L7 `tests/domain/test_network_binding.py`
  starts the real runtime on its default host and proves it listens on loopback
  only.

## Least-privilege strategy containers (SRS-SEC-003)

Every user strategy runs in its own Docker container, cloned by the Strategy
Orchestrator from the `phase1-strategy-runtime` template in `docker-compose.yml`.
That template runs with **least-privilege permissions** (NFR-S5; CIS Docker
Benchmark) — the three SRS-SEC-003 acceptance clauses:

- **No privileged mode.** The service declares `privileged: false`, drops **all**
  Linux capabilities (`cap_drop: [ALL]`, none added back), and sets
  `security_opt: ["no-new-privileges:true"]`, so a strategy runs with the minimum
  kernel privilege and cannot escalate at exec time.
- **No host network access.** The service sets no `network_mode: host` (nor
  `service:` / `container:` namespace sharing), no `pid: host`, and no `ipc: host`
  / `shareable`. It is confined to the dedicated `atp_strategy_net` network, which
  is declared `internal: true` — Docker attaches **no gateway**, so a strategy has
  no route to the host, the LAN, or the internet. (Absence of `network_mode: host`
  alone is *not* sufficient: a container with no explicit network joins the default
  Compose bridge, which routes outbound through the host — the internal network is
  what removes that egress.) The concrete `StrategyContainerRuntime` (deferred;
  owner SRS-ARCH-004 / SRS-ORCH-002) attaches the specific internal services a
  strategy may reach via the SYS-12 interface onto this network. This holds
  **repo-wide**: no compose service uses host networking.
- **No access to other strategy filesystems.** Container-per-strategy gives each
  instance its own writable root layer. The service mounts no host Docker socket,
  no host-path bind, and no `volumes_from`; the shared SSD/NAS data tiers are
  mounted **read-only** (`atp_ssd:/ssd:ro`, `atp_nas:/nas:ro`) so a strategy cannot
  write into a tier a sibling would read, and the SRS-SEC-001 credential vault is
  not mounted at all (see SRS-SEC-004).
- **Enforcement.** `tools/container_isolation_check.py` (in CI, and transitively
  via `tools/architecture_check.py`) statically inspects the compose template and
  fails closed on any privileged / host-network / cross-filesystem violation; its
  `--fixture` self-tests prove it rejects each. The L7
  `tests/domain/test_strategy_container_least_privilege.py` asserts the same
  invariant, and a gated L5 `tests/integration/test_strategy_container_inspect.py`
  runs `docker inspect` on a real strategy container when `ATP_RUN_INTEGRATION=1`.
  Because the concrete Docker-backed `StrategyContainerRuntime` is deferred (owner:
  SRS-ARCH-004 / SRS-ORCH-002), the compose template is the authoritative
  declarative source and this static inspection is the primary evidence — the same
  convention SRS-ARCH-004 and SRS-SEC-004 are verified under.

**Future hardening (not yet applied).** A read-only container root filesystem
(`read_only: true` + a `tmpfs` scratch mount) is a stronger CIS control deferred to
the concrete container runtime, where it can be validated against a live strategy
image without risking an unverifiable startup break in the template. (The dedicated
`internal: true` strategy network is already applied — see the "no host network
access" bullet above.)

## Jupyter research-environment isolation (SRS-SEC-004)

The embedded Jupyter research environment (`phase1-jupyter` in `docker-compose.yml`)
is **isolated from live trading credentials and the execution APIs** (SyRS NFR-S6;
StRS SN-1.18) — the two SRS-SEC-004 acceptance clauses:

- **Cannot read brokerage credentials.** Jupyter's `environment` merges the
  `x-atp-no-secrets` anchor **first** in the `<<` sequence, and YAML merge is
  earlier-wins — so every catalogued secret (`ATP_IB_ACCOUNT`, the SMTP/SMS API keys,
  the DataBento/Sharadar keys) and all vault-unlock material (`ATP_VAULT_FILE` /
  `ATP_VAULT_KEY_FILE` / `ATP_VAULT_PASSPHRASE`) is blanked to `""`. Even a populated
  plaintext `.env` cannot leak a credential into the kernel, and no catalogued secret
  is re-set inline. The SRS-SEC-001 credential vault (`/run/atp-secrets`) is **not
  mounted at all** — the shared `*atp-volumes` anchor is deliberately not applied — so
  Jupyter cannot open the vault.
- **Cannot submit live orders / no direct access to the execution engine.** Jupyter is
  confined to the dedicated `atp_research_net` network, declared `internal: true` —
  Docker attaches **no gateway**, so a container on it has no route to the host, the
  LAN, or the internet, and no execution-API peer is placed on it (the execution
  engine and the IB Gateway sit on the default bridge, off this network). So Jupyter
  can open no socket to a brokerage / execution API and submit no live order. (Absence
  of `network_mode: host` alone is *not* sufficient: a container with no explicit
  network joins the default Compose bridge, which both routes outbound through the
  host and reaches every other default-bridge container — the internal, single-member
  network removes both.) Host / shared-namespace networking is refused for the same
  reason, and a peer that would share Jupyter's network namespace via
  `network_mode: service:phase1-jupyter` / `container:…` is refused too. The
  dashboard→Jupyter proxy (IF-13 / SRS-RES-001) preserves this one-way boundary:
  the live-control-bearing `phase1-dashboard-api` (SRS-API-001 kill switch / live
  designation / Hot-Swap) is **never** placed on `atp_research_net`, or Jupyter
  would gain a path to the live-control REST — the checker forbids the execution
  engine, the IB Gateway, **and** the dashboard/API from sharing Jupyter's network.
- **The one-way dashboard→Jupyter proxy chain (SRS-RES-001).** The browser reaches
  Jupyter **only** through the dashboard origin: `browser → 127.0.0.1:8080
  dashboard-api → (internal atp_research_edge_net) phase1-research-proxy →
  (internal atp_research_net) phase1-jupyter`. Two properties make the chain
  one-way: (1) each hop's upstream is **fixed at start-up, never request-derived**
  — the dashboard runtime's reverse proxy (`python/atp_runtime/proxy.py`) forwards
  only to its registered upstream (validated loopback/RFC-1918, refusing chunked
  request bodies and any prefix that could shadow `/api/v1` or `/dashboard`), and
  the L4 hop (`python/atp_research_proxy`) pipes only toward Jupyter, so a
  connection initiated FROM the Jupyter container can only loop back to Jupyter
  itself; (2) `phase1-jupyter` publishes **no host port** (IF-13: not a standalone
  endpoint) and the research proxy carries **no secrets, no volumes, no published
  ports, internal-only networks, and no network shared with an execution peer** —
  all enforced statically by the checker's research-proxy assertions. The Jupyter
  container runs token-less because the network path IS the auth boundary
  (loopback-bound dashboard → internal edge net → internal research net);
  injecting a token env would contradict the no-secrets stance this container is
  checked against.
- **Residual risk (in-model, stated explicitly).** With the embed served
  same-origin, JavaScript emitted by a notebook and *rendered in the operator's
  own browser* shares the dashboard origin, so it could call the operator REST
  from that browser exactly as the operator can. That is within the single-operator
  network-locality trust model (NFR-S3): the boundary SRS-SEC-004 enforces is the
  Jupyter **server/kernel container** — which holds no credentials and has no
  network route to the execution engine or live-control REST — not the operator's
  own browser session. Mutating operator routes remain confirmation-guarded
  (UI-4 / SRS-SAFE-001) regardless of caller.
- **Read-only market-data / backtest-result access.** Jupyter mounts **only** the
  sanctioned SSD/NAS data tiers, and only **read-only** (`atp_ssd:/ssd:ro`,
  `atp_nas:/nas:ro`) — it reads market data and backtest results through the data
  layer (filesystem, no network) and can write into no shared tier.
- **Enforcement.** `tools/jupyter_isolation_check.py` (in CI, and transitively via
  `tools/architecture_check.py`) statically inspects the compose template and fails
  closed on any credential / vault / execution-network / read-write violation; its
  `--fixture` self-tests prove it rejects each Compose-equivalent bypass (merge
  order, aliased/interpolated security values, service-level merge / `extends`,
  duplicate keys, long/flow volume syntax, `external:` networks). The L7
  `tests/domain/test_jupyter_credential_isolation.py` asserts the same invariant, and
  a gated L5 `tests/integration/test_jupyter_isolation_inspect.py` runs `docker
  inspect` on a real `phase1-jupyter` container when `ATP_RUN_INTEGRATION=1`. The
  credential-blanking + no-vault half is also asserted by
  `tools/deployment_check.py::assert_credential_vault_wiring`. The checker also
  asserts the SRS-RES-001 proxy-chain shape: `phase1-jupyter` publishes no port,
  and `phase1-research-proxy` (the one-way hop) is secret-blanked, volume-less,
  port-less, on internal-only networks, and shares no network with an execution
  peer — with `--fixture` self-tests for each new bypass (a re-published Jupyter
  port, the proxy on the default bridge / sharing an execution network / gaining
  the vault / a reversed env merge, and dashboard-api "simplified" onto
  `atp_research_net`). The compose template remains the authoritative declarative
  source and this static inspection the primary SRS-SEC-004 "Security test"
  evidence — the same convention SRS-ARCH-004 and SRS-SEC-003 are verified under;
  the live-stack demonstration of the embed itself is SRS-RES-001 acceptance
  evidence (`tests/e2e/test_research_embed.py` plus the operator's compose run).
