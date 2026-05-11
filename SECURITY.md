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
