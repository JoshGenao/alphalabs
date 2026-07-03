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
- **Layer 2 — judgment** (`prompts/critic_prompt.md`): an LLM-driven adversarial
  review in a **fresh context**. Autonomous agents run it via the dispatcher —
  `python3 tools/adversarial_review.py origin/main` — which auto-selects the
  reviewer: **Codex** (`tools/codex_review.sh`, shelling the codex companion since
  `/codex:adversarial-review` is `disable-model-invocation:true`) when available,
  and a **fresh-context Claude reviewer** (`git diff | claude -p` with the critic
  prompt, diff-only, read-only plan mode) when Codex is usage-limited or absent. It
  predicts Codex limits from the plugin's job state + a cooldown cache, so a rate
  limit never blocks the pipeline. Check availability with
  `python3 tools/adversarial_review.py --status`. Canonical verdict schema is
  `block|warn|approve` (same as Layer 1); the dispatcher normalizes Codex's
  `approve|needs-attention` into it and tags the result with `reviewer:`.

Coding agents must NEVER bypass with `ATP_CRITIC_BYPASS=1` or
`--no-verify`. Only humans bypass; the env-var bypass is grep-able in
shell history by design.

## Parallel agent runs (self-scheduling)

Several coding agents run at once as **interactive** Claude sessions (so you can
watch each one), each isolated in its own git worktree + branch + private port
block. A lock-guarded scheduler (`tools/agent_pool.py`) hands each session a
different, dependency-ready feature, and each session integrates its own work
back to `main`. No file, branch, or port collisions.

- **Launch (one per terminal):** `tools/claim_and_work.sh`. It calls
  `agent_pool.py claim` (under an `fcntl` lock) to pick the best **ready,
  unclaimed, dependency-satisfied** feature — preferring a subsystem no sibling
  holds — creates `../alphalabs-wt-<id>` on `agent/<id>` off `origin/main` with a
  private port block, then opens an interactive `claude` session in that worktree
  seeded with `prompts/coding_prompt.md`. Open N terminals → N agents on N
  features. (`tools/spawn_agents.sh` still exists for headless/CI pre-assignment.)
- **Dependency graph:** `tools/feature_deps.json` (committed) records what blocks
  what; it's seeded (`agent_pool.py seed`) and **self-learns** — when an agent
  hits an unbuilt prerequisite it runs `agent_pool.py block <id> --on <dep>`,
  lands partial work (`integrate --mode partial`), and **claims the next ready
  feature in the same session**. Ephemeral leases live in the gitignored
  `tools/.agent_runtime.json`; `agent_pool.py status` shows the whole board.
- **Isolation:** `init.sh` is `ROOT_DIR`-relative, so `.venv`, `target/`,
  `data/`, `.devserver.*` are worktree-local. Agents touch shared state
  (`feature_list.json`, `progress.txt`, `main`) **only** through the locked
  `integrate` step; each writes one `progress.d/session-<id>.md` resume note.
- **No parallel integration/live tests.** While siblings run, agents run only
  `pytest -m "not integration and not e2e"` + `cargo test`. Nothing may bind the
  IB ports (4001/4002) or the dashboard/Jupyter stack — that also protects the
  single-live-IB invariant.
- **Integrate + flip (auto):** after the full gate (`run_ci_locally.sh` + tests +
  both critics — deterministic `critic_check.py` **and** the judgment pass via
  `tools/adversarial_review.py`, Codex or fresh-context Claude), the agent runs
  `agent_pool.py integrate <id> --mode complete|serialized`. `complete` rebases
  on `main`, runs `close_feature.py <id> --verified` (flip `passes:true` + fold
  note), and fast-forward-pushes `main` — all under the lock. **Honesty guard:**
  a feature with a step that *needs* IB/integration/live integrates
  `--mode serialized` (code merges, `passes` stays false); the operator finishes
  verification later — manually or via the **`verified-e2e`** label, which still
  triggers `.github/workflows/close-feature.yml`. After a feature is `done`, run
  `tools/cleanup_agents.sh` to remove its worktree + branch.

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
| Agent scheduler | `tools/agent_pool.py` | Locked self-claim / block / integrate / status; the coordination core |
| Interactive launcher | `tools/claim_and_work.sh` | Per-terminal: claim a ready feature + open an interactive agent in its worktree |
| Judgment critic dispatcher | `tools/adversarial_review.py` | Adversarial review with Codex→fresh-context-Claude failover on usage limits |
| Codex reviewer (low-level) | `tools/codex_review.sh` | The Codex leg of the judgment pass (wrapped by the dispatcher) |
| Dependency graph | `tools/feature_deps.json` | Committed DAG of feature prerequisites (seeded + self-learned) |
| Parallel launcher (headless) | `tools/spawn_agents.sh` | Pre-assign worktrees to headless agents (legacy/CI path) |
| Feature closer | `tools/close_feature.py` | Flip `passes` + fold the progress note (run by `integrate` on main) |
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
