=== SESSION SRS-RESV-003 ===
Date: 2026-07-03
Feature: SRS-RESV-003 — support manual and configurable automatic Hot-Swap triggers
Outcome: serialized on 2026-07-03 (code on main, passes:false; operator finishes
         dashboard/REST e2e). SUPERSEDED 2026-07-31: the REST + dashboard arms were
         built, adversarially reviewed over 14 Codex rounds, and verified e2e in a real
         browser against the shipped composition. passes:true. See "SESSION 2 —
         2026-07-31" at the end.

CONTEXT: SRS-RESV-003 is the keystone of the Hot-Swap cluster (RESV-004/005/006 all
depend on it; it is a direct dep of SRS-API-001). AC (SyRS SYS-49a; StRS SN-1.25/1.30):
"Manual promotion, drawdown-triggered demotion, top-ranked promotion, and highest-momentum
promotion are configurable; automatic triggers default to disabled; all swap triggers are
logged." It is the trigger DECISION + CONFIGURATION + LOGGING layer UPSTREAM of the
already-built SRS-RESV-004 demotion gate (StrategyOrchestrator::resolve_demotion). It does
NOT execute the swap. Everything it consumes is unbuilt (ranking RESV-002, reservoir
RESV-001, durable SYS-61 store LOG-001, dashboard UI-5, REST API-001), so it is built
against INJECTED ports — the same seam pattern RESV-004 uses (HotSwapLiquidationProbe).

WHAT I DID:
- atp-types/src/lib.rs (new block after StructuredHotSwapDemotionError, ~L2673):
  HotSwapTriggerKind (ManualPromotion/DrawdownDemotion/TopRankedPromotion/
  HighestMomentumPromotion; const as_str 1:1 wire strings; is_automatic); DrawdownThresholdBps
  (validated 1..=10000 bps newtype, no float on money); DrawdownDemotionTrigger +
  RankingPromotionTrigger enums each `#[derive(Default)]` with `#[default] Disabled`;
  HotSwapTriggerConfig `#[derive(Default)]` = all automatic disabled (+ all_disabled(),
  any_automatic_enabled); source-neutral input DTOs LiveStrategyState {strategy_id, drawdown_bps},
  RankedStrategy, ReservoirRankingSnapshot with fail-closed top_by_rank/top_by_momentum
  (reject empty + non-finite); TriggerRationale; HotSwapTriggerProposal (+to_demotion_request
  -> HotSwapDemotionRequest reusing HOT_SWAP_DEMOTION_TIMEOUT_SECONDS; +to_event);
  HotSwapTriggerEvent. Inline unit tests (Default all-disabled, wire strings, validation,
  accessors).
- atp-orchestrator/src/lib.rs: ports LiveStrategyProbe (current_live -> Option<LiveStrategyState>),
  ReservoirRankingSource (snapshot), HotSwapTriggerLog (record -> Result, best-effort) +
  TriggerEvaluation {fired, selected}. StrategyOrchestrator::evaluate_automatic_triggers<L,R,S>
  (Disabled => never fire/log; Enabled => resolve demoting=current_live + candidate from ranking,
  fire only if both resolve AND candidate != live AND condition holds; priority drawdown ->
  top-ranked -> momentum; selected = fired[0]) and request_manual_promotion (always fires+logs);
  private fire_trigger helper builds proposal AND log.record in one place (the log-on-every-fire
  guarantee). Sibling methods after resolve_demotion (NOT nested — the demotion guard scans that
  body for promotion tokens).
- atp-orchestrator/src/bin/resv003_hot_swap_trigger_cli.rs (+[[bin]] in Cargo.toml): subcommands
  config / evaluate / manual; key:value proof lines; fail-closed args (unknown/dup/valueless);
  --inject disabled non-vacuity; --log durable fsynced JSONL.
- architecture/runtime_services.json: hot_swap_trigger_contract block (requirement SRS-RESV-003;
  types/enums/structs/ports/entry_points/guard + source-neutral forbidden_fields; deferred[]
  names RESV-001/002 ranking, RESV-004/005/006 execution, LOG-001 durable store, API-001/UI-5).
- tools/hot_swap_trigger_check.py (static, cargo-free) registered in tools/architecture_check.py
  (import + assert_hot_swap_trigger wrapper + evidence.extend) so it runs in BOTH ci.yml and
  run_ci_locally.sh via the aggregated `architecture` step. Asserts: trigger-kind variants +
  1:1 wire strings; #[default] Disabled on each automatic enum; config derives Default;
  source-neutral structs; ports; fire_trigger logs + both entry points route through it;
  drawdown-first priority; to_demotion_request bridge.
- Tests: crates/atp-orchestrator/tests/resv_3_hot_swap_triggers.rs (12 L7 cases, spy/failing/
  forbidden ports) + tests/domain/test_hot_swap_trigger_config.py (5 cargo-shell cases,
  domain+safety marks).

WHAT I TESTED (per step):
- Step 1 (env): PASS — ./init.sh -> "✓ Environment ready".
- Step 2 (dashboard browser + REST/WS e2e): DEFERRED — UI-5 + API-001 unbuilt, not
  parallel-runnable. This is why the outcome is serialized. CLI arm of the SYS-49a surface
  IS demonstrated (below).
- Step 3 (AC): PASS solo via CLI + tests —
  * configurable: `evaluate --drawdown-threshold/--top-ranked/--highest-momentum` fires the
    enabled triggers (fired:DRAWDOWN_DEMOTION/TOP_RANKED_PROMOTION/HIGHEST_MOMENTUM_PROMOTION).
  * default disabled: `config` -> all *-enabled:false, default-disabled:true; and `evaluate
    ... --inject disabled` -> fired-count:0 even with met conditions (non-vacuity).
  * all logged: `evaluate` -> logged-count == fired-count, all-triggers-logged:true; --log
    persists one JSONL line per fired trigger (fsynced); L7 test asserts log count == fired count.
  * manual always available: `manual --demoting --candidate` -> fired:MANUAL_PROMOTION even with
    all automatic disabled.
- Step 4 (evidence): cargo test --workspace 1366 pass; pytest -m "not integration and not e2e"
  2837 pass (4 pre-existing skips incl. test_single_live_invariant = deferred Hot-Swap runtime);
  cargo fmt/clippy --all-targets -D warnings clean; python3 tools/hot_swap_trigger_check.py PASS;
  python3 tools/architecture_check.py PASS. run_ci_locally.sh green through ruff; dies only at
  PRE-EXISTING mypy in python/atp_strategy/examples + atp_runtime/atp_api/... (16 files, NONE
  mine; my 2 py files mypy-clean); integrate skips mypy.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings
  judgment (adversarial_review.py origin/main, reviewer=claude-fallback): APPROVE — no findings
    (Codex ran but its output was unparseable; the dispatcher's fresh-Claude failover reviewed
     the diff and approved — the designed failover path)

RESUME / NEXT:
- passes stays false (serialized). To flip: run the dashboard browser + REST/WS e2e once
  SRS-UI-001/UI-5 (Hot-Swap controls + status) and SRS-API-001 (/api/v1/hot-swap + reservoir/
  ranking) are built, or apply the `verified-e2e` label on a merged agent/SRS-RESV-003 PR.
- Wiring points for later features (do NOT rebuild): RESV-002 supplies a concrete
  ReservoirRankingSource (Sharpe/Sortino/momentum over the eval window); RESV-001 supplies the
  live-strategy drawdown via LiveStrategyProbe; RESV-004 already consumes the emitted
  HotSwapDemotionRequest (resolve_demotion) — feed it `evaluation.selected.to_demotion_request()`;
  RESV-005 = flat-start promotion; RESV-006 = cool-down (SYS-49e) — the manual-during-cooldown
  confirmation is intentionally NOT enforced here; LOG-001 = durable SYS-61 store consuming
  HotSwapTriggerEvent (Source.HOT_SWAP in python/atp_logging already exists as the wiring point);
  API-001/UI-5 = the REST/dashboard arms (CLI arm ships here as resv003_hot_swap_trigger_cli).
- Gotchas: SAFETY_PATH_RE matches hot_swap in the CLI + check paths (forced the paired
  tests/domain test, which is present); atp-types unit tests are inline (resv003_trigger_config_tests);
  hot_swap_trigger_check.py is static-only and reached via architecture_check (no standalone
  ci.yml/run_ci loop entry needed).

=== FIX 2026-07-04 (adversarial-review-driven, fail-closed hardening) ===
Trigger: the tools/adversarial_review.py fix (commit 3880988) unwraps Codex's --json envelope,
which previously made EVERY Codex reply "unparseable" → a fail-open claude-fallback APPROVE. Re-ran
the now-fixed reviewer on the RESV-003 diff; Codex returned REAL verdicts and, across 6 rounds,
caught 5 genuine fail-opens/gaps the broken reviewer had masked. All fixed + regression-tested in
commit `fix(SRS-RESV-003): fail closed on rejected/absent trigger log + degraded inputs`:
  1. [high] fire_trigger swallowed log.record() Err and still returned a `selected` proposal →
     an UNLOGGED Hot-Swap trigger could reach the RESV-004 gate and swap a LIVE strategy. Now the
     log outcome is LOAD-BEARING: fire_trigger returns (proposal, Result); TriggerEvaluation gains
     `unlogged`; `selected` requires the whole pass logged.
  2. [high] partial-log-rejection: `selected` keyed only off the FIRST trigger's flag → a later
     rejected record still left selected=Some. "All swap triggers are logged" is now ATOMIC for the
     pass (selected requires unlogged.is_empty()).
  3. [high] CLI cmd_manual printed manual-logged:false but exited 0 → shell automation saw success.
     Manual returns Result<_, UnloggedHotSwapTrigger{proposal, rejection_reason}>; CLI exits nonzero
     (evaluate too, when any trigger unlogged / any input degraded).
  4. [high] CLI no-`--log` sink returned Ok without persisting → firing command claimed logged with
     no record. CollectingTriggerLog now REJECTS when no sink; --log required to log (and act on) a
     fired trigger.
  5. [high] automatic path dropped the rejection REASON (only a count). unlogged is now
     Vec<UnloggedHotSwapTrigger> carrying the reason end-to-end; CLI surfaces the concrete cause.
  + doc-drift: reconciled all "best-effort" wording → "load-bearing"; the guard now REJECTS stale
    "best-effort" wording in the contract.
Also: LiveStrategyProbe/ReservoirRankingSource now return Result — a degraded (Err) input fails
closed AND surfaces its reason in TriggerEvaluation.degraded_inputs (distinct from healthy
Ok(None)/empty); typed error taxonomy stays deferred to concrete RESV-001/002 probes.
Tests grew to 17 L7 + 6 L4 CLI-exit boundary + 12 domain cases. Verified: cargo test --workspace
(1412 pass); clippy/fmt -D warnings clean; architecture_check + hot_swap_trigger_check PASS.
Critics: deterministic APPROVE (no findings); judgment APPROVE (reviewer=claude-fallback, Codex
rate-limited at re-review time — the designed Codex→Claude failover; earlier rounds 1-5 were real
Codex BLOCKs that drove these fixes). Still SERIALIZED (passes:false) — Step 2 dashboard/REST e2e
unchanged.


# === SESSION SRS-RESV-003 (2) — the REST + dashboard arms, 2026-07-31 ===
Outcome: CLOSED green. Operator accepted CLI+REST as SYS-49a's manual arm.

WHAT WAS MISSING (verified in code, not read off the note):
1. No durable trigger config — the CLI rebuilt HotSwapTriggerConfig from flags every
   invocation, so UI-5's HotSwapStatusSource.trigger_config() seam had no producer.
2. No Python surface — HotSwapStatusSource had zero implementors.
3. No browser/REST evidence — only the CLI arm had ever been demonstrated.

WHAT SHIPPED:
- crates/atp-orchestrator/src/trigger_config_store.rs: versioned, magic-marked JSON written
  scratch->fsync->rename->parent-fsync. THREE-state read (absent / readable / unreadable);
  an unreadable config is never folded into "disabled". Unknown keys fail the read.
  Registered as SRS-DATA-015 entity `hot-swap-trigger-config` (Pinned).
- ExclusiveGuard (O_EXCL lockfile) held across read-modify-write and across append+count, in
  the BINARY so all three arms serialise across threads and processes.
- python/atp_hotswap: the surface-NEUTRAL client both operator surfaces import downward.
  HotSwapStatusUnavailable is declared there and re-exported by atp_dashboard.hotswap so the
  class the pane catches and the client raises are ONE object.
- python/atp_orchestration/hot_swap_triggers.py: GET/PUT /api/v1/hot-swap/triggers and
  POST /api/v1/hot-swap/triggers/manual. POST /api/v1/hot-swap (EXECUTION) is deliberately
  NOT bound and keeps its 501.
- atp_dashboard: ATP_HOT_SWAP_TRIGGER_STATE resolves the UI-5 auto-trigger chips;
  ATP_HOT_SWAP_TRIGGER_LOG is required alongside it (fail at boot, not a no-audit mode).
  serve() composes the REST arm, so it actually ships.

EVIDENCE: tests/e2e/test_hot_swap_triggers.py (3 cases) through the SHIPPED composition —
default-disabled proven non-vacuously in the browser, each automatic trigger configured ON
and back OFF with the pane reflecting it, manual fired+logged with its ordinal addressing the
record written, and a corrupted config degrading to unknown rather than "disabled".
Plus 53 boundary + 44 domain tests. cargo 2034 / python 4508 / 0 failures.

WHAT IS NOT DONE, and why it is a refusal rather than a gap:
- No dashboard button FIRES a manual trigger. UI-5's promote-live control targets
  POST /api/v1/hot-swap (EXECUTION, owner RESV-004/005, still 501); repointing it at the
  trigger route would tell an operator they promoted a strategy when a proposal was written
  to a log. Owners: UI-5 + SRS-RESV-004/005. SYS-49a asks for "dashboard, CLI, OR REST".
- request_manual_promotion takes the demoting id from its caller; the LiveStrategyProbe
  producer is the deferred RESV-001/002 runtime. A logged manual proposal is a REQUEST to
  swap — the RESV-004 gate MUST revalidate the live strategy at execution time.

Resume / next: RESV-004/005/006 consume HotSwapDemotionRequest from these proposals; when
they can execute a swap, UI-5 gains an honest dashboard manual control.
