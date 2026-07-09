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
  `127.0.0.1`). Every published `docker-compose.yml` port is bound to a
  loopback / RFC 1918 host (`127.0.0.1:8080:8080`, `127.0.0.1:8888:8888`,
  `${ATP_IB_HOST:-127.0.0.1}:…`) — never a bare `PORT:PORT` that would publish
  on `0.0.0.0`.
- **Fail-closed policy, no public opt-in.** `is_allowed_bind_host` /
  `assert_bind_allowed` (`python/atp_runtime/rest_server.py`) permit only
  loopback (`127.0.0.0/8`, `::1`) and the three RFC 1918 IPv4 ranges; `0.0.0.0`,
  `::`, link-local, CGNAT, and any publicly-routable address raise
  `BindPolicyError` **before** the socket opens. The runtime intentionally
  provides **no** flag or environment variable to bind a public interface.
- **External exposure is auth-gated and operator-managed.** Reaching the
  dashboard from beyond the local network requires the operator to place an
  **authenticated access-control component (e.g. a reverse proxy)** in front of
  the loopback / RFC 1918 bind (NFR-S3; OWASP authentication guidance). The ATP
  process is never itself exposed on a public interface; see
  `docs/DEPLOYMENT.md` (portability constraint 5).
- **Enforcement.** `tools/network_binding_check.py` (in CI) proves the compose
  mappings are loopback/RFC 1918-bound, that no source binds all interfaces,
  that the policy fails closed, and that this external-authentication
  requirement is documented; the L7 `tests/domain/test_network_binding.py`
  starts the real runtime on its default host and proves it listens on loopback
  only.
