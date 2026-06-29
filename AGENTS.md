# AGENTS.md - Navigation index

This file is the entry point for every agent session.
Read it first. Follow the links. Do not guess.

## Quick-start checklist (run at the start of every session)

0. `echo "${ATP_FEATURE_ID:?}"` - confirm your assigned feature (parallel runs)
1. `pwd` - confirm working directory (your own `alphalabs-wt-<id>` worktree)
2. `./init.sh` - start dev server, install hooks, verify environment
3. `cat progress.txt` + `ls -t progress.d/` - read handoff (archived log + the
   living per-session notes)
4. `git log --oneline -20` - understand recent changes
5. `cat feature_list.json | grep '"passes": false' | wc -l` - count remaining work

See `prompts/coding_prompt.md` for the full per-session workflow.

## Pre-commit gate

Two-layer Critic Agent runs before every commit. **Both must approve.**

- **Layer 1 — deterministic** (`tools/critic_check.py`): mechanical regex/AST
  checks for committed secrets, deleted/skipped tests, vendor-SDK leakage
  into core, dependency-direction violations, and safety-critical changes
  without paired `tests/domain/` diffs. Wired as a git pre-commit hook by
  `tools/install_hooks.sh` (re-installed by `init.sh`).
- **Layer 2 — judgment** (`prompts/critic_prompt.md` via
  `/codex:adversarial-review`): LLM-driven adversarial review run in a
  **fresh Codex context**. The default invocation is
  `/codex:adversarial-review --wait $(cat prompts/critic_prompt.md)` —
  the prompt file holds the repo-specific judgment criteria and Codex
  reads the diff itself. Fallback (Codex unavailable): paste
  `prompts/critic_prompt.md` + `git diff --cached` into any other
  fresh-context LLM. Same JSON output schema as Layer 1.

Coding agents must NEVER bypass with `ATP_CRITIC_BYPASS=1` or
`--no-verify`. Only humans bypass; the env-var bypass is grep-able in
shell history by design.

## Parallel agent runs

Multiple coding agents can run at once, each isolated in its own git worktree so
there are no file, branch, or port collisions.

- **Launch:** `tools/spawn_agents.sh [-n N] [FEATURE_ID ...]` picks unclaimed
  failing features, creates a `../alphalabs-wt-<id>` worktree on branch
  `agent/<id>` off `origin/main`, and assigns each agent a private port block.
  Each agent receives its feature via `ATP_FEATURE_ID` and does **not** select
  its own work (deterministic selection would make them all pick the same one).
- **Isolation:** `init.sh` is `ROOT_DIR`-relative, so `.venv`, `target/`,
  `data/`, and `.devserver.*` are worktree-local automatically. Agents never
  edit the shared `feature_list.json` or `progress.txt`; each writes one
  `progress.d/session-<id>.md` note.
- **No parallel integration/live tests.** While siblings run, agents run only
  `pytest -m "not integration and not e2e"` + `cargo test`. Nothing may bind the
  IB ports (4001/4002) or the dashboard/Jupyter stack — that also protects the
  single-live-IB invariant.
- **Merge + verify:** each agent opens a PR (`agent/<id>` → `main`) and never
  merges itself. **Merging integrates the code but does NOT mark the feature
  passing.** A reviewer marks it passing by adding the **`verified-e2e`** label
  — only after confirming every step in the feature's `steps[]` passes
  end-to-end (not partial, not a unit test alone, not "works in isolation", and
  they'd be confident a human running the steps would pass). The label triggers
  `.github/workflows/close-feature.yml`, which runs
  `tools/close_feature.py <id> --verified` on `main` to flip `passes:true` and
  fold the note into `progress.txt`. Then run `tools/cleanup_agents.sh` to remove
  the worktree + branch. (To close by hand: `tools/close_feature.py <id>
  --verified` on `main`, then commit.)

## Document map

| Document | Path | Purpose |
|----------|------|---------|
| Stakeholder requirements | `docs/StRS_v0.7.md` | Why we're building this |
| System requirements | `docs/SyRS_v0.7.md` | What the system must do |
| Software requirements | `docs/SRS.md` | How the software is structured |
| Feature list | `feature_list.json` | Source of truth for all work |
| Session log | `progress.txt` | Canonical handoff log (folded in at merge) |
| Per-session notes | `progress.d/` | One note per parallel session; see its README |
| Environment setup | `init.sh` | How to start and smoke-test the app |
| Critic Agent | `tools/critic_check.py` + `prompts/critic_prompt.md` | Pre-commit review gate (deterministic + judgment) |
| CI / CD | `.github/workflows/{ci,integration,security}.yml` | Mirror of local checks; secrets isolated to `integration` env |
| Verified close | `.github/workflows/close-feature.yml` | `verified-e2e` label on a merged `agent/<id>` PR flips `passes` + folds the note |
| Local CI mirror | `tools/run_ci_locally.sh` | Run the same step list as `ci.yml` before pushing |
| Test layout | `tests/{unit,property,boundary,integration,e2e,domain}/` | One bug class per layer (L1–L7) |
| Coding agent prompt | `prompts/coding_prompt.md` | Per-session workflow; includes Steps 5.5 (test layer) and 6.5 (critic) |
| Parallel launcher | `tools/spawn_agents.sh` | Create worktrees + assign features to parallel agents |
| Feature closer | `tools/close_feature.py` | At merge: flip `passes` + fold the progress note (run on main) |
| Worktree teardown | `tools/cleanup_agents.sh` | Remove closed agents' worktrees + branches |
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
