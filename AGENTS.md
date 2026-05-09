# AGENTS.md - Navigation index

This file is the entry point for every agent session.
Read it first. Follow the links. Do not guess.

## Quick-start checklist (run at the start of every session)

1. `pwd` - confirm working directory
2. `./init.sh` - start dev server, install hooks, verify environment
3. `cat progress.txt` - read handoff from previous session
4. `git log --oneline -20` - understand recent changes
5. `cat feature_list.json | grep '"passes": false' | wc -l` - count remaining work

## Pre-commit gate

Two-layer Critic Agent runs before every commit. **Both must approve.**

- **Layer 1 — deterministic** (`tools/critic_check.py`): mechanical regex/AST
  checks for committed secrets, deleted/skipped tests, vendor-SDK leakage
  into core, dependency-direction violations, and safety-critical changes
  without paired `tests/domain/` diffs. Wired as a git pre-commit hook by
  `tools/install_hooks.sh` (re-installed by `init.sh`).
- **Layer 2 — judgment** (`prompts/critic_prompt.md`): LLM-driven
  adversarial review. Run in a **fresh context** — sub-agent on Claude
  Code, new chat on Codex, paste-into-anywhere for any other LLM. Same
  JSON output schema as Layer 1.

Coding agents must NEVER bypass with `ATP_CRITIC_BYPASS=1` or
`--no-verify`. Only humans bypass; the env-var bypass is grep-able in
shell history by design.

## Document map

| Document | Path | Purpose |
|----------|------|---------|
| Stakeholder requirements | `docs/StRS_v0.7.md` | Why we're building this |
| System requirements | `docs/SyRS_v0.7.md` | What the system must do |
| Software requirements | `docs/SRS.md` | How the software is structured |
| Feature list | `feature_list.json` | Source of truth for all work |
| Session log | `progress.txt` | Handoff notes between sessions |
| Environment setup | `init.sh` | How to start and smoke-test the app |
| Critic Agent | `tools/critic_check.py` + `prompts/critic_prompt.md` | Pre-commit review gate (deterministic + judgment) |
| CI / CD | `.github/workflows/{ci,integration,security}.yml` | Mirror of local checks; secrets isolated to `integration` env |
| Local CI mirror | `tools/run_ci_locally.sh` | Run the same step list as `ci.yml` before pushing |
| Test layout | `tests/{unit,property,boundary,integration,e2e,domain}/` | One bug class per layer (L1–L7) |
| Coding agent prompt | `prompts/coding_prompt.md` | Per-session workflow; includes Steps 5.5 (test layer) and 6.5 (critic) |
| Initializer prompt | `prompts/initializer_prompt.md` | First-run scaffolding (test dirs, critic, CI) |

## Architecture overview

ATP is a single-user algorithmic trading platform for Python-authored strategies.
The core runtime services are Rust services. User strategy code is Python, and
the dashboard backend/API may use another language if it does not become a core
runtime service.

Primary module flow:

`Types/Models -> Data Layer -> Strategy Engine -> Execution Engine / Internal Simulation Engine -> Brokerage Adapter`

Runtime structure:

- Strategy Orchestrator manages each strategy in its own Docker container.
- Exactly one strategy may run in live IB execution mode at a time.
- All non-live strategies run through the internal paper simulation engine.
- Market Data Subscription Manager deduplicates IB subscriptions and fans data out to live and paper strategies.
- Data Layer owns Databento/IB/Sharadar/user-Parquet ingestion, validation, SSD/NAS tiering, corporate actions, normalization, schema evolution, and unified historical queries.
- Execution Engine owns live order routing, order state, live-only IB enforcement, kill switch behavior, stale-data blocking, outbox reconciliation, and watchdog recovery.
- Internal Simulation Engine owns simulated fills, virtual ledgers, paper callbacks, paper metrics, corporate action handling, and persisted paper state.
- Dashboard/API/CLI expose operator workflows for monitoring, backtests, live designation, kill switch, Hot-Swap, logs, Reservoir ranking, and Jupyter access.

## Constraints

- Core ATP runtime services must be implemented in Rust.
- User-authored strategies and the Strategy API must be Python.
- Initial live brokerage integration is Interactive Brokers Gateway only.
- Exactly one strategy may execute against the IB live account at any time.
- IB paper trading is for adapter integration testing only, not Reservoir evaluation.
- Strategy Reservoir paper strategies use the internal simulation engine and shared live market data.
- Each strategy instance must run in its own Docker container managed only by the Strategy Orchestrator.
- Dependency direction is one-way; lower layers must not depend on dashboard or orchestrator components.
- Broker and data providers must be isolated behind adapter interfaces.
- Core strategy, data, and backtest modules must not contain vendor-specific adapter logic.
- Databento is the Phase 1 bulk equity and historical options data provider.
- Sharadar is the Phase 1 fundamental data provider.
- Live equity/options market data and minute watchlist ingestion come from IB.
- Local SSD is the primary runtime storage tier; NAS is the archival tier.
- Dashboard/API bind locally or to RFC 1918 addresses by default.
- Platform safeguards include kill switch, stale-data blocking, connectivity blocking, live-strategy confirmation, credential encryption, and least-privilege strategy containers.
- No multi-user auth, RBAC, HFT infrastructure, native mobile app, crypto/futures implementation, non-IB brokerage implementation, or institutional compliance reporting is in scope for the release baseline.

## Allowed commands

No `security.py` or equivalent bash command allowlist exists yet. Until one is
added, use standard repository inspection, setup, build, test, and local dev
server commands that are necessary for the active requirement, and avoid
destructive commands unless the user explicitly requests them.
