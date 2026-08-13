=== SESSION SRS-RESV-005 ===
Date: 2026-08-11
Feature: SRS-RESV-005 — promote a selected paper strategy to live execution only after
successful demotion (SyRS SYS-49d / AC-14; StRS SN-1.25 / SN-1.30)
Outcome: serialized

## What I did

**The gap.** RESV-004's demotion gate already produced an acceptance
(`HotSwapDemotionResolved`), and nothing consumed it: no promotion path existed anywhere —
`promote(` / `complete_swap(` / `go_live(` were literally a *forbidden token list* in the
demotion contract. `POST /api/v1/hot-swap` was a structured 501.

**The design decision that shaped everything.** `HotSwapDemotionResolved` has public fields
and derives `Clone`, so consuming it directly would make "only after successful demotion"
satisfiable by a struct literal. It could not be tightened — `hot_swap_demotion_check.py`
pins it byte-for-byte and ERR-7's tests depend on its shape — so the fix LAYERS a new
authority on top and leaves the pinned primitive alone (scope-and-serialization r9):

* `DemotionReceipt` — private fields, no `Clone`, no `Default`, `pub(crate)` sole
  constructor that additionally refuses a `promotion_allowed:false` acceptance;
* `promote_after_demotion` is `pub(crate)` and takes the receipt BY VALUE;
* the only public path is `execute_hot_swap`, which runs `resolve_demotion` first.

Violating demote-before-promote is a **compile error**, proven by two `compile_fail`
doctests that build as external consumers of the crate — and mutation-verified (making the
fields `pub` turns them red with "Test compiled successfully, but it's marked
`compile_fail`").

**Shipped:**
* `crates/atp-orchestrator/src/hot_swap_promotion.rs` — the receipt, four read-only ports,
  the gate. Every guard runs before the single designation write; the two post-conditions
  are re-read after it and any drift rolls the designation back. Each port is three-way, so
  unreadable / absent / empty stay three different facts.
* `resv005_hot_swap_promote_cli` — drives the REAL gate over the REAL `LiveDesignation`
  authority (persisted: scratch → fsync → atomic rename; a foreign file refuses the whole
  read rather than reading as "nothing is live") and the REAL SRS-SIM-004 paper snapshot,
  from which the history fingerprint is computed. Serialized under `ExclusiveGuard`.
* `python/atp_orchestration/hot_swap_execution.py` — binds `POST /api/v1/hot-swap`, opt-in,
  so a bare runtime keeps its 501. **The shipped posture REFUSES to promote**: the two
  safety facts SYS-49d turns on (flat account, unchanged artifact) have no real producer, so
  the route answers a structured 501 `SAFETY_INPUTS_UNAVAILABLE` naming SRS-EXE-006 /
  SRS-ORCH-004 unless a composer explicitly declares a drill.
* Both new persisted formats registered in the SRS-DATA-015 schema registry;
  `tools/hot_swap_promotion_check.py` (10 guards) registered in `tools/gates.json`.

**Rebased mid-flight onto SRS-RESV-004**, which landed while this was in review. That was a
gain, not a cost: their `DemotionPendingLock` is exactly what three of my deferral notes had
called deferred, and `resolve_demotion` consults it before its probe — so the promotion path
now INHERITS the cross-attempt block rather than deferring it. I dropped my own
`tools/docs_link_check.py` fix because they had landed an equivalent one.

## Second phase — finishing step 2 (operator: "lets finish this feature")

The browser leg was blocked on the pane's `current_live_strategy_id` cell, which is
**SRS-RESV-005's OWN** deferred producer. Mocking it would have meant faking this feature to
make this feature look green, so it was built instead:

* `CliHotSwapPromotionSource` reads the durable designation snapshot the promotion gate
  itself writes — the pane reports the authority, not a second copy that could drift.
  Three outcomes kept apart: designated / genuinely empty (defers, an under-claim; the
  control goes inert, which is what the gate would do anyway) / unreadable (raises — a
  record that exists but cannot be read is NOT "no strategy is live").
* `CompositeHotSwapStatusSource` gained a third leg and now MERGES `live_state`: RESV-004
  owns the demotion-pending half, RESV-005 the live-strategy half, disjoint key sets, so the
  merge is a union rather than a precedence rule.
* Composed off `ATP_HOT_SWAP_DESIGNATION_STATE`, matching the existing env-gated leg pattern.
  Unset, the cell keeps its deferred placeholder.

Two harness defects surfaced only by trying to file real browser evidence, both of which
produced a record that LOOKED complete (committed separately, `fix(harness)`):
`evidence.py run` discarded artifacts attached during the run it was executing, and
`capture.shot` could only shoot the whole page — which put the pane at y=2933 of a ~3900px
dashboard, illegible, and then filed four completely BLANK PNGs once scoped, because the
`rise` reveal animation lives on the enclosing card and the target reports opacity 1 while
its parent is still transparent.

## What I tested (per step)

* **Step 1: PASS** — `./init.sh` → "✓ Environment ready" (`evidence.py run`).
* **Step 2: PASS (executed)** — `pytest tests/e2e/test_hot_swap_promotion_browser.py` → 2
  passed, recorded via `evidence.py run` with **6 artifacts** (4 pane-scoped screenshots +
  both session recordings), rendered into `EVIDENCE.md`. A real browser drives the real UI-5
  control against the real server: arm → confirm → the pane reports
  `promoted reservoir-b live · swap sw-1`, and the durable designation follows in a separate
  process. **This step had never been run by any prior session.**

  What is injected, and why — all at the SOURCE seam the real producers plug into, never by
  faking the wire: SRS-RESV-002's ranking candidate (without one the control is correctly
  inert, so no walk is possible at all) and SRS-RESV-006's cool-down (the pane refuses to arm
  on ANY unknown safety field — a THIRD blocker this run discovered and which nobody had
  recorded). The flat-account and code-identity facts remain the declared drill
  (`fixture_safety_inputs`). Step 2 permits exactly this: "with the fixtures, mocks, or
  operator controls needed by the requirement".

* **Step 3: PASS** — `pytest tests/domain/test_hot_swap_promotion.py` → 26 passed
  (`evidence.py run`). Drives the real binary over a real paper snapshot; "preserves prior
  paper performance history" is asserted as the snapshot file being **byte-identical on
  disk** after a successful promotion.
* **Step 4: PASS** — `tools/hot_swap_promotion_check.py` → 10 guards (`evidence.py run`).

Full gate: `pytest -m "not integration and not e2e"` → **4989 passed, 0 failed**;
`cargo test --workspace` exit 0; `cargo clippy --workspace -- -D warnings` exit 0;
`cargo fmt --all --check` exit 0; `ruff check .` + `ruff format --check .` exit 0;
`rest_api_check.py` in sync; `data015_schema_check.py` PASS (20 entities).

**Two environment findings, reported not papered over:**
1. `tools/run_ci_locally.sh` went red once on `tests/test_data010_eviction_contract.py`. It
   passes in isolation, the underlying `cargo test -p atp-data --lib` passes standalone (238
   passed), my diff touches only two descriptors in that crate, and there are **18,557**
   leaked `atp-*` scratch dirs in `$TMPDIR` — the phantom documented in test-integrity r20. I
   could not clean them (the recursive delete was refused by the sandbox). **The operator
   should clear `$TMPDIR/atp-*`.**
2. `mutation_verify.py` reported all 57 added tests as "still pass without the change". It
   reverts TRACKED MODIFICATIONS only, and this feature's sources are `A` (added), so nothing
   was removed. All 8 properties were therefore mutation-verified BY HAND, one property per
   mutation, each killing exactly one named test. Written back to test-integrity as rule 29.

## Critic verdicts

* deterministic (`critic_check.py --staged`): **APPROVE** — no findings. It BLOCKed twice
  during the session and was right both times: a multi-line pytest skip reason, and a
  safety-path change whose paired `tests/domain/` diff covered only the Rust arm.
* judgment (`adversarial_review.py`, reviewer=**codex**): **BLOCK** — 12 rounds, every one
  a real finding, none disputed. The final round's finding is the evidence-vs-HEAD ordering
  above, which this chore commit resolves; the substantive code loop converged at r11. No
  APPROVE was faked and none is claimed. Recorded
  verbatim in `.harness/runs/SRS-RESV-005/evidence.json`; no APPROVE was faked.

## Adversarial rounds

Adversarial rounds: 16
(plus 1 hung attempt between r10 and r11, retried — a timeout is an availability failure,
not a verdict, and the ledger correctly records no zero-finding round.)

Every round found a REAL defect; none was disputed. Severity trended down.

* **r1** [critical] the served route could report PROMOTED on FIXTURE safety facts (the CLI
  defaulted `--positions` to flat) → opt-in at both layers. [high] the read-execute-write
  sequence was unserialized → `ExclusiveGuard` held for the critical section.
* **r2** [high] an unwritable journal left the candidate live with a clean `PROMOTED` →
  `promotion-recorded` is three-way and the handler answers `PROMOTED_UNRECORDED`. [medium]
  the uncomposed 501 named the capability owner (RESV-003) not the route's → route-level
  `served_by` now wins.
* **r3** [critical] declaring the fixture TIER still left the fixture FACTS defaulting to
  success → all three individually required, guarded against `unwrap_or(`. [medium] the
  published schema said `additionalProperties: true` over a strict handler.
* **r4** [high] SCOPE — the branch mixed a harness fix with the feature. Dissolved rather
  than overridden: SRS-RESV-004 landed an equivalent fix, so I dropped mine.
* **r5** [critical] the SHARED gate promoted into an EMPTY live slot — the REST wrapper
  refused it, the CLI and Rust arms did not. [high] a post-rename persistence failure
  surfaced as a retry-safe non-2xx after the slot had moved → `PublishOutcome` split.
* **r6** [high] the audit record was appended before the durable publish → buffered, and
  committed only once the state is settled.
* **r7** [critical] the live-slot guard ran AFTER `resolve_demotion`, which engages the
  lockout, cancels orders and pages the operator on its timeout branch → moved ahead of every
  demotion-side port, proven with ports that PANIC if touched. [high] evidence stale.
* **r8** [critical] `demotion_state` was derived as "FLAT_CONFIRMED or else DEMOTION_PENDING",
  so a truncated proof stream produced a 200 claiming a promotion with no demotion proof →
  closed vocabulary + incoherent-combination refusal.
* **r9** [critical] `flat_confirmed()` was a DENYLIST, so r7's two new variants inherited
  "the demotion succeeded" → inverted to a fail-closed allowlist, both directions pinned.
  **This one was introduced by r7's own fix** — the pattern adversarial-precheck warns about.
* **r10** [high] the published schema did not mark `candidate_strategy_id` required while the
  handler rejects it → declared and pinned against the frozen artefact.
* **r11** [critical] the corrected `false` from r9 then printed `DEMOTION_PENDING` for
  refusals that never started, so REST answered 200 and the pane held its control inert
  awaiting a lockout that was never engaged. Root cause one level below all three rounds:
  `flat_confirmed() -> bool` was a boolean over a THREE-valued fact → replaced with
  `DemotionProof { FlatConfirmed, TimedOut, NotStarted }`, and `NOT_STARTED` maps to a
  non-2xx because nothing mutated. **The r7 → r9 → r11 chain is the single most useful thing
  this review found**: each round fixed the previous round's fix, and only the third exposed
  the type that was wrong from the start.

* **r12** [high] the committed evidence recorded an older HEAD than the commit under review.
  Structural, not a code defect: evidence is recorded BEFORE the commit that contains it, so
  it always names the parent. Resolved by pinning every step to the final CODE commit
  (`642b38d`) and keeping this chore commit code-free — the notes and playbooks that follow
  change no behaviour, so the record still covers everything shipped. Worth knowing for the
  next session: do not write "re-recorded at this HEAD" in a commit message; the record names
  the parent, and the honest claim is "recorded at the final code commit".

The reviewer also HUNG once between r10 and r11 (>25 min, no output). Per
`adversarial-precheck.md` that is an availability failure, not a verdict — it was killed and
retried, and the ledger correctly shows 11 rows with findings and no zero-finding round.

## Rounds 15-16, and the honest close

* **r15** [medium] my composition comment claimed leg independence the implementation does
  not provide — both halves feed ONE `live_state`, so a leg that RAISES defers the readable
  one too. Comment corrected to the real semantics; the peer claim in `atp_hotswap` is
  ACCURATE and stays, because `trigger_config` and `live_state` are separate methods.
  [high] SCOPE — harness commits on a feature branch (see below).
* **r16** [high] and this one was MY fix from r15's phase: carrying artifacts forward let a
  LATER run stamp the step at a new commit while older screenshots rode along, so the visual
  gate could certify a dashboard nobody looked at on this code. Artifacts now carry the head
  they were captured at; the carry-forward and `verify` both require a match. Four regression
  tests, both directions, each mutation-verified to die to exactly one guard.

**Closed on operator authorization with the verdict recorded verbatim: BLOCK.** The only
finding left standing is r15's scope objection — the branch carries three harness commits
(evidence pipeline + browser capture) alongside the feature. They are not incidental: without
them step 2 cannot file its artifacts at all, and the last one closes a false-green I had
introduced. They are separate commits and independently revertable. The operator chose to
record the override rather than split, exactly as for the r4 scoping block. No APPROVE was
faked and none is claimed.

**Unrelated failure, reported not fixed:** `tests/property/test_indicators_property.py::
test_bbands_property_matches_batch_talib` began failing during this session — a Bollinger
Bands parity tolerance ~8% too tight (`1.079e-05 <= 1.000e-05`), found by Hypothesis and
cached in `.hypothesis/examples`, so it now reproduces deterministically. **Owner:
SRS-SDK-006**, whose most recent commit on that file is already "bound the BollingerBands
parity check by TA-Lib's relative precision". My diff touches nothing in indicators/talib.
Retuning another feature's numeric tolerance is how a real precision bug gets masked, and
clearing the example DB would be re-running until green — so neither was done. **It leaves
the CI mirror red on that one step.**

## Playbook updates

* `safety-paths.md` r43–r46 — guard placement after a destructive gate; allowlists not
  denylists for safety predicates; declaring the fixture tier ≠ stating the fixture facts; an
  audit record must not precede the durable publish.
* `honest-surfaces.md` 9b–9c — never derive a headline state as "X or else Y"; a non-2xx is a
  promise that nothing mutated, so check what the consumer already believes.
* `test-integrity.md` r29–r31 — `mutation_verify` reverts tracked modifications only (a
  new-file feature is reported as 100% "cannot fail"); a test can encode the bug;
  `compile_fail` doctests are cheap, real, mutation-verifiable evidence.
* `pipeline-and-integrate.md` — `ruff format` can rewrite a compliant skip decorator into a
  critic BLOCK; pass explicit `.py` paths, never `.`.

## Resume / next

`passes` stays **false**. What is left is Step 2's browser leg, and it is blocked on other
features, not on this one:

1. **SRS-RESV-002** — the Reservoir ranking that names a promotion candidate. Until it lands,
   the UI-5 promote control is inert (`hotCandidate === null`), so there is no armed button
   to drive and `tests/e2e/test_hot_swap_promotion.py::test_the_dashboard_promote_control_drives_the_swap`
   stays skipped.
2. **SRS-EXE-006 / SRS-ORCH-004** — the real IB position feed and the durable
   deployed-version registry. Until they land, the served route refuses with
   `SAFETY_INPUTS_UNAVAILABLE`; wire them at `mount_hot_swap_execution` and the route
   promotes with no change to the module.

To continue: run `tests/e2e/test_hot_swap_promotion.py` (REST leg is ready today, browser leg
after RESV-002), then close with `--attested-by`. The gate, its ordering and all three AC
clauses are already proven offline and at the CLI.

Blocked-on recorded via `agent_pool.py block SRS-RESV-005 --on SRS-RESV-002 SRS-RESV-006 SRS-EXE-006`.
