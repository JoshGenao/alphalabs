=== SESSION SRS-RESV-004 ===
Date: 2026-08-11
Feature: SRS-RESV-004 — execute Hot-Swap demotion before promotion (SyRS SYS-49b / SYS-49c, StRS SN-1.25)
Outcome: serialized — code merges, passes stays FALSE. The judgment critic's round-7 [critical]
finding is unfixed by design: the demotion-pending lockout is not durable on the
persistence-FAILURE path, which is the AC's own "blocked until manual resolution" clause.

WHAT WAS THERE, AND WHAT WAS MISSING (verified in code, not read off a note — no prior note existed):
ERR-7's commit 82ef06f shipped a "SDK-surface contract slice": `resolve_demotion` modelling only
the binary timeout DECISION over four injected ports, every concrete producer deferred. Three of
the seven entries in `hot_swap_demotion_contract.deferred[]` were this feature's, and the third
was a live safety hole:

  `resolve_demotion` is a STATELESS single-attempt decision. On timeout it cancels, alerts,
  records, and blocks promotion FOR THAT CALL — so a later attempt whose probe reported flat
  promoted the candidate over IB positions nobody resolved. The AC's last clause, "promotion is
  blocked until manual resolution", was not enforced at all.

WHAT SHIPPED:
- `demotion_pending_store.rs` — the durable lockout. Versioned + magic-marked JSON,
  scratch→fsync→rename→parent-fsync. THREE-state read (absent / held / unreadable); `read_state`
  is the ONE place the fail-closed collapse happens. An engage never overwrites a held record;
  `resolve` needs a non-blank operator acknowledgement and refuses to clear what it cannot read.
  Registered as SRS-DATA-015 entity `hot-swap-demotion-pending` (Pinned).
- `hot_swap_demotion.rs` — the SYS-49b sequence (authorize → cease signals → cancel resting →
  liquidate), the concrete flat-confirmation probe, and the paper transition.
  `complete_demotion_to_paper` takes the acceptance token only the gate's flat arm can construct,
  so "transitions to paper only after flat" holds by TYPE, not by convention.
- `resolve_demotion` — consults the lockout before the probe, engages BEFORE the destructive side
  effects and amends after, and gained a block-WITHOUT-cancel branch for a self-contradicting probe.
- `hot_swap_demotion_drill.rs` + `resv004_hot_swap_demotion_cli` (demote / status / resolve).
- `python/atp_hotswap` producer + `ATP_HOT_SWAP_DEMOTION_STATE`: the UI-5 pane's
  `demotion_pending` / `demotion_detail` cells now render the durable lockout instead of a
  `deferred:SRS-RESV-004` placeholder. No new dashboard panel, no new REST route.

Ports withhold capabilities on purpose: `DemotionPendingLock` has no clearing method (only an
operator resolves), `DemotionBrokerageControl` no `disconnect` (a changeover is not a kill switch).
`tools/hot_swap_demotion_check.py` grew from 11 to 20 static collectors — including the phase
ordering and the engage/amend ordering by BYTE POSITION, and the single `transition_to_paper` call
site — each mutation-proven to fail when its invariant is broken.

WHAT I TESTED (per step, re-run at the final tree):
  Step 1: PASS — ./init.sh → "✓ Environment ready"
  Step 2: PASS — ATP_RUN_E2E=1 pytest tests/e2e/test_hot_swap_demotion.py → 1 passed (real
          headless browser, real CLI, real lockout file, through the SHIPPED
          `mount_default_dashboard`; ephemeral port, so no sibling collision)
  Step 3: PASS — pytest tests/domain + tests/boundary → 34 passed
  Step 4: PASS — tools/hot_swap_demotion_check.py → ERR-7 PASS (20 evidence items)
Plus: cargo test --workspace 163 binaries / 0 failures; clippy + fmt clean;
pytest -m "not integration and not e2e" → 4889 passed / 0 failed; mypy 11 → 4 errors (the 4 that
remain are pre-existing in a file this branch does not touch).

Critic verdicts:
  deterministic: APPROVE — no findings (it blocked once, correctly: a safety-path change whose
    tests had gone to tests/ and crates/…/tests/ but not tests/domain/. Fixed by adding the L7
    case, not by moving the file.)
  judgment (adversarial_review.py, reviewer=codex): BLOCK at round 7 — recorded verbatim, not
    worked around. Rounds 1-6 found nine defects and every one is fixed; round 7 names a residual
    the code itself documents, and its own recommendation is "integrate serialized and leave
    passes:false until a named operator accepts the residual risk". Taken. Seven rounds, seven
    real verdicts, no timeouts and no dropped summaries (`.harness/runs/.../review.jsonl`).

Adversarial rounds: 7
  r1 [high] — the demotion liquidated ACCOUNT-level positions keyed on a caller-supplied
     `demoting_strategy_id` never checked against the live registry. Class: an identity that
     SELECTS state, not one that labels a record. Fixed with `authorize_demotion` as phase 0
     (three refusals: nobody live / wrong one / more than one), the sequence returning Result,
     the drill propagating it ahead of every port, and a static ordering guard.
  r1 [high] — "still fixture-tier, do not close as complete". Correct, and now acted on: see
     "Why serialized and not complete".
  r2 [high] — the committed evidence predated the r1 safety fix. Class: evidence must bind to
     the tree being shipped. Fixed by re-running all four steps; done again after r3 and r4.
  r2 [high] — harness changes on a feature branch. Not acted on, and the divergence is stated:
     both were prerequisites for this feature's OWN mandatory gates (a red CI mirror and a
     mutation gate returning a false accusation), both are isolated in their own commits with
     their own critic approvals, and both make the harness STRICTER. There is no separate
     review lane in this pipeline — `integrate` takes the branch.
  r3 [high] — a FAILED lockout write left the retry free to promote: the gate reported
     `promotion_block_is_durable = false` and the store was still empty, so the next attempt read
     `Clear`. Class: describing a fail-open is not closing it. Fixed with a poison on the port
     impl — and deliberately NOT for `AlreadyPending`, which the non-vacuity test caught before
     it shipped a lock that never reopens.
  r3 [medium] — one alert body claimed a cancel that the probe-inconsistency branch never
     performs. Class: a page is a recovery instruction; derive it from the recorded outcome.
     `OperatorAlertEvent` now carries `liquidation_cancel`.
  r4 [critical] — `resolve` deleted the lockout and THEN emitted proof lines, so an unprintable
     acknowledgement unblocked promotion while reporting failure. Class: order of operations
     around durable writes, on the OUTPUT side. Fixed by validating everything first.
  r4 [high] — crash window between the destructive side effects and the lockout write. Fixed
     with a two-phase write: engage (outcomes NotAttempted) → cancel → alert → amend.
  r5 [critical] — I had fixed that window on the TIMEOUT arm and left the probe-inconsistency
     branch paging first. The instance, not the class. Fixed on the branch AND by making the
     static guard enumerate every blocked branch instead of naming one, plus a behavioural twin
     asserting engage < {cancel, alert} < amend on both.
  r6 [high] — the pending-refusal factory hard-coded `promotion_block_is_durable = true`, which
     stopped being true when r3's poison made the blocking state possibly in-memory. Fixed with
     a distinct `Poisoned` state and an `is_durable()` the gate passes through.
  r7 [critical] — UNFIXED, and the reason this is serialized. The poison lives in an
     `Arc<Mutex<..>>`, so a fresh process constructs the lock unpoisoned, reads the unwritten
     file as `Clear`, and a later flat probe is accepted without manual resolution. Closing it
     needs a second durable location or a startup gate that refuses the swap path when the store
     is unwritable — SRS-ARCH-005 / SRS-LOG-001. Recorded in `deferred[]`.

Playbook updates:
  test-integrity.md 27–28 — span-scoped mutation anchors (this bit me FOUR times in one session,
    in four shapes); a harness that ran nothing must not return a verdict.
  safety-paths.md 40–42 — persist the block before the destructive side effects; a failed
    durable write needs a fail-closed STATE; prove a caller-supplied identity that selects
    account-level state.
  honest-surfaces.md 38–39 — validate before mutating; per-branch alert text.
  pipeline-and-integrate.md — a gate that is red in every worktree is red for nobody.

Two harness fixes were prerequisites for this feature's OWN mandatory gates, and are isolated in
their own commits:
  - `docs_link_check` was red in every worktree (`.git` is a file in a linked worktree; the lease
    file lives in the primary checkout), so `run_ci_locally.sh` could not go green.
  - `mutation_verify` handed pytest the Rust test paths, ran NOTHING, and reported all 34 added
    tests as unable to fail. Both changes make the harness STRICTER, never laxer.

Why serialized and not complete:
  The session was planned to target `complete` on the fixture-tier reading (the RESV-003
  precedent), under honesty conditions I wrote into the plan: "if any of these fails I integrate
  --mode serialized". Two of them failed, and they point the same way.

  1. r7 [critical], above: the AC clause "promotion is blocked until manual resolution" does not
     hold across a restart on the persistence-failure path. That is the feature's core
     guarantee, not a peripheral one.
  2. r1 [high] made the same point about transports: the gate, probe, sequence, lockout,
     `OperatorNotifier` and paper transition are REAL and execute, but the IB socket and the
     SMTP/SMS transports are fixtures (self-labelled `transports: FIXTURE`). RESV-003 closed
     complete on fixtures because its AC is about CONFIGURATION; this one says "cancels resting
     IB orders, submits liquidation orders", which a fixture adapter demonstrates without
     proving against IB.

  So `passes` stays false and the operator finishes verification. Everything else built here is
  real, tested and merged — this is the honest classification, not a failure.

Harness contradiction worth fixing (not fixed here — it is the prompt's, not this feature's):
  `prompts/coding_prompt.md` Step 4.1 says the first action after plan approval is to persist
  the plan to `progress.d/plan-$ATP_FEATURE_ID.md`. `agent_pool.py integrate` then REFUSES the
  branch: "branch commits modify shared coordination files ['progress.d/plan-SRS-RESV-004.md']
  — only the integrator may write them." Every session that follows Step 4.1 literally will hit
  this at the last step, after all the work is done. Either the guard should allow
  `plan-<id>.md` alongside `session-<id>.md`, or Step 4.1 should name a path outside
  `progress.d/`. I removed the file; the plan's content is in this note.

Resume / next:
  - Integrated `--mode serialized`; `passes` stays false. The two things that would flip it:
    (a) close the r7 restart gap — a durable fallback location, or a startup/readiness gate that
        refuses the swap path when the demotion-pending store is unwritable (SRS-ARCH-005 +
        SRS-LOG-001);
    (b) the live-IB leg for "cancels resting IB orders / submits liquidation orders"
        (SRS-EXE-006 atp-adapters) and real SMTP/SMS (SRS-NOTIF-001).
  - Then re-run `adversarial_review.py origin/main` — round 8 would be reviewing (a) and (b).
  - Deferred owners, all recorded in `hot_swap_demotion_contract.deferred[]`: the concrete
    SignalHalt / DemotionBrokerageControl / LivePositionSource / PaperTransition runtimes
    (SRS-ORCH-* / SRS-EXE-006 / SRS-SIM-*), the IB `cancel_order` leg, the real SMTP/SMS
    transports (SRS-NOTIF-001), the SRS-LOG-001 sink, and `POST /api/v1/hot-swap` — which stays
    501 deliberately: its declared response carries `promotion_state`, and wiring it from the
    demotion half alone would report a promotion that did not happen. That route is
    SRS-RESV-005's to complete.
  - The r7 residual is now declared in `hot_swap_demotion_contract.deferred[]` with its owners,
    so the next session does not have to rediscover it.
