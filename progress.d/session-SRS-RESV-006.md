=== SESSION SRS-RESV-006 ===
Date: 2026-08-14
Feature: SRS-RESV-006 — enforce Hot-Swap cool-down behaviour (SyRS SYS-49e; StRS SN-1.25)
Outcome: complete

## What I did

**This was a resume session.** A prior session (2026-08-11) had built the whole feature,
taken a round-2 BLOCK with two findings, fixed one uncommitted, and run out of context. It
left no session note. Two things had changed underneath it, and both mattered more than the
leftover fix:

1. **`origin/main` had moved 61 commits and SRS-RESV-005 had LANDED**, binding
   `POST /api/v1/hot-swap` to a real `execute_hot_swap` over the real `LiveDesignation`
   authority. A hot swap could now actually happen; it could not when the branch was written.
2. **RESV-005 recorded itself blocked on RESV-006** and its browser leg had to INJECT a
   cool-down, because the UI-5 pane refuses to arm on any unknown safety field. Its note
   calls that "a THIRD blocker this run discovered and which nobody had recorded."

So the session rebased first (11 overlapping files, 3 conflicts, `entity_count` re-derived
from `PERSISTED_ENTITIES` as 21 rather than guessed), then answered the open finding against
a tree where the execution path existed.

**The open finding, `cooldown-execution-bypass`, was correct and structural.** SRS-RESV-006
had gated the two SRS-RESV-003 trigger entry points — but those only MINT a proposal, and
nothing in the type graph requires a swap to have come from one. `HotSwapDemotionRequest` is
freely constructible and the CLI builds one straight from argv;
`HotSwapTriggerProposal::to_demotion_request`, the documented bridge, has zero production
call sites. A correctly suppressed evaluation therefore constrained nothing at all.

**Shipped:**

* `execute_hot_swap` takes a REQUIRED `CooldownControl` and refuses on the same
  `proven_clear()` predicate the trigger arms use, ahead of every demotion-side port. Required
  rather than optional is the whole anti-bypass property: a caller cannot execute a swap
  without stating what the window says, and an omitted `--cooldown-state` resolves to
  `Unknown`, which refuses exactly as an active window does.
* **The `deferred-writer:SRS-RESV-005` seam is closed.** A successful swap now records its own
  completion, so SYS-49e's third clause is produced by the producer the requirement names
  rather than by the operator CLI standing in for it. Only the promotion-succeeded arm writes:
  a demotion that ends without a promotion is a failed changeover, not a swap.
* **The window is proved RECORDABLE before anything destructive runs**
  (`HotSwapCooldownPort::probe_recordable`, which applies the SAME rule the writer will —
  r11), so "a swap completed and no window opened" is unreachable by an unwritable or corrupt
  store rather than merely reported. Ordered ahead of the confirmation gate because it is
  unwaivable and that one is not.
* **The window is written TWICE (r13).** `begin_provisional_window` opens it before the
  demotion; the caller's `into_completed` confirms it after the durable publish. No
  interruption between the publish and the confirmation can leave a promoted strategy with
  the automatic triggers armed, and a swap that refuses, fails, or cannot publish abandons
  the provisional window explicitly — so r6's direction (no window without a durable swap)
  survives the fix for r13's. Every surface reports which kind of window it is looking at,
  tri-state, because `unknown` is not `false`.
* **The completion instant is read after the swap**, not reused from the observation instant.
  A demotion may legitimately run for the whole SYS-49b timeout, and reusing one instant
  opened a seven-day window up to 60 seconds early.
* `resv005`'s frozen `OBSERVED_AT_SECONDS = 1_715_000_000` became the real clock with a
  `--now` override — harmless while it only labelled an audit record, not harmless once it
  stamps a cool-down's start.
* Carried through every arm: the promote CLI's flags, the REST route's `confirm_cooldown`
  (kept strictly distinct from the SYS-2d `confirm`) mapping the two refusals to
  CONFIRMATION_REQUIRED and INTERNAL_ERROR respectively, and the dashboard.
* **The class fix.** `check_both_entry_points_are_gated` NAMED two methods and asserted
  nothing about anything else — which is exactly how the bypass survived. It is replaced by a
  collector that DISCOVERS swap paths from source, with reasoned exemptions in
  `guard.ungated_swap_paths`. It immediately found a fourth path nobody had listed
  (`run_fixture_demotion`). The demotion-only paths stay exempt deliberately: requiring a
  cool-down acknowledgement to STOP trading would be a safety regression.

**Step 2's browser leg was RUN, for the first time on this feature.** Every prior attempt had
a live sibling and the parallel-agent protocol forbids binding the dashboard stack then; this
session had none. It found a defect no offline test could: the pane RAISED SYS-49e's
confirmation warning on arm and then posted no acknowledgement at all, so once the route
enforced the window an operator who was shown the warning and confirmed it got a 428 refusing
them for not confirming.

**Operator decisions taken during the session:**
1. Full enforcement (gate + writer), not a narrower deferral.
2. Target `complete`, running the browser leg rather than recording it serialized.
3. Initially KEEP the `SAFETY_PATH_RE` prep commit — then **reversed** once rounds 1 and 3
   showed what it actually costs (see below). The rule it added is recorded under
   "Recommended follow-up" rather than lost.

## What I tested (per step)

* **Step 1: PASS** — `./init.sh` → "✓ Environment ready".
* **Step 2: PASS (executed)** — `ATP_RUN_E2E=1 pytest tests/e2e/test_hot_swap_cooldown_browser.py`
  → 2 passed, with pane-scoped screenshots attached. A real browser drives the real UI-5
  control against the real server and the real gate: an absent window reads READY and arms; a
  real recorded completion turns the dial ACTIVE; arming inside it raises the SYS-49e warning;
  confirming PERMITS the swap (200 PROMOTED) rather than blocking it; the swap restarts the
  window at its own completion timestamp, verified afterwards in a separate process. Plus the
  non-vacuity partner: a corrupt window renders UNKNOWN and holds the control inert.
* **Step 3: PASS** — `pytest tests/domain/test_hot_swap_cooldown.py -q` → 21 passed. Drives
  three real binaries and one real file, including a genuinely chmod-ed unwritable directory
  rather than a stubbed error.
* **Step 4: PASS** — `cargo test -p atp-orchestrator` → 0 failures.

Every step above was re-run and re-recorded through `evidence.py run` at the FINAL code
commit (`764c1c4`), not at the head they first passed against — adversarial review r16
blocked on a stale record still carrying step 2 as `fail` from the prior session. Step 2's
screenshots are attached to the record, and two are attached to step 3 because that is the
step whose acceptance criterion is about what a reviewer can SEE: the armed control carrying
the SYS-49e warning over an ACTIVE window, and the same pane after the swap with the dial's
start moved to the new completion instant.

Full gate: `pytest -m "not integration and not e2e"` → 5193 passed, 1 failed (see 3 below);
`pytest tests/e2e/` → 75 passed, 3 skipped;
`cargo clippy --workspace --all-targets -- -D warnings` → 0; `cargo fmt --all --check` → 0;
`ruff check` / `ruff format --check` → clean; `tools/run_ci_locally.sh` → "✓ local CI mirror
complete — every step ran"; `mutation_verify origin/main..HEAD` → all added tests went red
without the change.

**Two pre-existing reds, reported not papered over:**
1. `cargo clippy` failed on two lines byte-identical to `origin/main`
   (`doc_overindented_list_items`, `manual_contains`) under the local clippy 0.1.95. Fixed in
   their own `style:` commit rather than folded into the feature diff.
2. `mypy python/` reports 4 errors in `hot_swap_triggers.py`. **Verified identical on
   `origin/main`** by running mypy against a throwaway worktree at that commit — same four
   lines, none of them mine. The mirror treats it as advisory, matching `ci.yml`.
3. `tests/property/test_indicators_property.py::test_bbands_property_matches_batch_talib`
   fails: `BB.upper period=2 wrapper=10.0 batch=10.000015407080042`, i.e.
   `1.540708004199587e-05 <= 1.0000015407080042e-05`. **Owner SRS-SDK-006.** The code under
   test is BYTE-IDENTICAL to `origin/main` (`git diff origin/main...HEAD` over
   indicators/talib is empty), and SRS-RESV-005's note records hitting the same defect and
   integrating with it red. Hypothesis has now cached the falsifying example in the local
   (unshipped) `.hypothesis/examples`, so it reproduces every run in this worktree.
   **Not fixed here, deliberately:** retuning another feature's numeric tolerance is how a
   real precision regression gets masked — and the owner's most recent commit on that file
   was already "bound the BollingerBands parity check by TA-Lib's relative precision" — while
   clearing the example DB is re-running until green (`test-integrity` rule 21). So
   `tools/run_ci_locally.sh` is RED on the pytest step in this worktree, for a reason this
   diff did not cause. Operator authorised integrating with it documented.

## Critic verdicts

  deterministic (`critic_check.py --staged`): APPROVE — no findings, on every commit.
  judgment (`adversarial_review.py`, reviewer=**codex**): PENDING-FINAL-VERDICT

## Adversarial rounds: PENDING-FINAL-VERDICT

* **r1 `block`** — `meta:critic-self-modification`. Structural refusal of any diff touching
  `tools/critic_check.py`. Class: reviewer refusal, not a code finding. (Prior session.)
* **r2 `block`** — `cooldown-execution-bypass` + `persisted-json-escaping`. Classes:
  *guard placed at the decision, not the action*, and *writer must satisfy its own reader*
  (adversarial-precheck rule 8, the RESV-003 r3 class). (Prior session; finding 2 fixed by it,
  uncommitted.)
* **r3 `block`** — the same meta refusal, one finding, nothing about the code. This is what
  exposed the real cost of keeping the prep commit, and the operator reversed the decision.
* **r4 `block`** — *successful swap can complete without starting the cool-down* [critical] and
  *OpenAPI advertises the wrong type for `confirm_cooldown`* [high]. Classes: *"loud" is not
  "enforced" for an irreversible action*, and *declared contract vs handler drift, on a TYPE*
  (adversarial-precheck rule 6).
* **r5 `block`** — *cool-down window is stamped before swap completion* [high]. Class:
  *one clock instant reused for two different moments*.
* **r6 `block`** — *cool-down can be committed before the swap is durably completed* [high].
  Class: *a durable side effect ordered against the WRONG durable write* — the gate recorded
  the window while the designation was still only in memory, so a publish failing before its
  rename left a seven-day window for a swap the authority never accepted. Fixed with a
  `PendingCooldownWindow` token the caller redeems only after the publish; `HotSwapPromoted`
  lost its `Clone` derive as a consequence, which is the same tightening `DemotionReceipt`
  already had.
* **r7 `block`** — *cool-down repair command can reopen the window at the wrong instant*
  [high]. Class: *a recovery instruction is executable text and inherits the code's bugs* —
  r5's defect survived in the remediation message, which told the operator to reopen the
  window with the instant the attempt STARTED. The failure outcome now carries the completion
  instant and the surfaces print that; when the clock could not be read at all, no timestamp
  is offered rather than the nearest one to hand.
* **r8 `block`** — *public promotion success can be observed without opening the cool-down
  window* [high]. Class: *a lint is not a type*. `#[must_use]` plus a public token field let a
  caller read the success facts and drop the window. `HotSwapPromoted` is now OPAQUE and
  `into_completed` — which redeems the window — is the only way to read what a swap did, proven
  by two `compile_fail` doctests that build as external consumers.
* **r9 `block`** — *shipped dashboard never mounts the Hot-Swap execution route* [high].
  Class: **implemented is not shipped** (`adversarial-precheck` rule 7) — a NEW thread, not a
  further turn of rounds 4-8. The browser walk mounted the route on its own fixture runtime, so
  a pass said nothing about `python -m atp_dashboard`, which did not mount it at all. Composed
  it in `serve()` on operator decision, deliberately WITHOUT fixture safety inputs: the route
  still refuses, but now with `SAFETY_INPUTS_UNAVAILABLE` naming SRS-EXE-006 / SRS-ORCH-004
  instead of a generic `HANDLER_DEFERRED` that names nobody.
* **r10 `block`** — *execution gate accepts forgeable cool-down state* [critical]. Class:
  *a caller-supplied proof is a forgeable proof* — and it invalidated a design decision this
  feature had DOCUMENTED and defended ("a resolved VALUE, not a port... the fabrication risk
  is closed by a static check, not by types"). `CooldownState` is a public enum, so any
  external caller could hand the gate `NeverSwapped` and execute through an active window; a
  check over this repo's call sites is not a property of the API. `CooldownControl` lost its
  `state` field entirely, and the gate now reads the window through
  `HotSwapCooldownPort::resolve_window` at execution time — which also closes a staleness
  gap nobody had asked about, since the window is read when the swap is gated rather than
  whenever the caller happened to look.
* **r11 `block`** — *cool-down writer can reject IDs only after the live swap is already
  published* [critical]. Class: *two of my own fixes left a gap between them*. r2 established
  that an id carrying `"` or `\` cannot be recorded; r4 added a pre-flight proving the store
  was writable. Neither was wrong, but "the file is writable" is not "THIS completion can be
  written" — so a swap so named passed the probe, ran, published the designation, and only
  then failed to open its window. The pre-flight now takes the ids and applies the same rule
  the writer will (`probe_writable` -> `probe_recordable`).
* **r12 `block`** — *CooldownControl state-field guard is inert* [high]. Class: *a guard
  written through a code generator inherits the generator's escaping, and a guard with no test
  of its own cannot tell you it is inert*. The regex I added in r10 to stop the forgeable field
  returning was written via a Python heredoc, so `\b` became a literal backspace and the
  pattern could never match. CI would have stayed green while the critical bypass came back.
  Fixed, and both halves of the r10 guard now have their own mutation tests.
* **r13 `block`** — *a completed swap can still fail open without a cool-down* [critical].
  Class: *fixing one direction of an ordering constraint opens the other*. r6 moved the window
  write to AFTER the durable publish, which stopped a failed publish from suppressing the
  triggers for seven days. But the publish and the window are two separate file writes, and a
  crash, a kill or a full disk between them left the candidate LIVE with no window at all —
  the automatic triggers armed against the strategy just promoted, failing open and silently.
  Recording before was wrong, recording after was wrong; the answer was **both**, modelled on
  SRS-RESV-004's engage-then-amend lockout. The window is opened PROVISIONALLY before the
  demotion and confirmed after the publish, so every interruption now lands on a window that
  exists. A swap that refuses or fails abandons it, which is r6's direction, kept.

  Two things the fix itself opened, found while writing it and closed in the same round:
  the CLI's publish-failed arm now `abandon`s explicitly (dropping the token stopped meaning
  "no window" the moment phase one existed), and every surface reports whether a window is
  provisional (`cooldown-completion-provisional`, tri-state) — a window an operator cannot
  tell apart from a completed one is a window they cannot resolve.

* **r14 `block`** — *the architecture contract still defers the two-phase protocol this diff
  implements* [high]. Class: *closing a residual turns the entry that recorded it into a
  lie*. `hot_swap_cooldown_contract.deferred` still said the fail-open "needs a two-phase
  window ... belongs to whoever next revises this entity" while r13 had shipped one, and
  named the mechanism by a type and a function both renamed three rounds earlier. A future
  reviewer reads that registry as the statement of what is deliberately open. Fixed in all
  four places carrying the claim (registry, `cooldown_store` module doc, a test comment, this
  note), and the mechanical half is now a guard: every `Type::method` the block names must
  exist in the source, with foreign types enumerated rather than inferred — a renamed local
  type is undeclared too, which is exactly the case.

* **r15 `block`** — *a failed in-window swap can clear the prior cool-down* [critical].
  Class: *the fix for a fail-open carried its own*. r13's first draft wrote the provisional
  record into the SAME `last_completion` slot with a boolean flag. SYS-49a(a) guarantees an
  acknowledged manual swap stays available during a window — so the ordinary sequence
  "swap A completes, operator acknowledges swap B inside A's window" had B's provisional
  record (newer instant) overwrite A's completion, and B failing then wrote
  `last_completion: None`, deleting a cool-down still in force. The automatic triggers
  resumed days early: the exact fail-open the feature exists to prevent, reintroduced by
  the fix for a different one.

  The root cause is that two facts with different LIFETIMES shared one slot — a provisional
  marker is discarded when its swap fails, a completion never is. They are now separate
  triples (`provisional_*` beside `last_*`): abandoning touches only the provisional slot,
  confirming retires only its own marker, and `resolve` classifies against whichever runs
  LATER so the pair can never resolve to less suppression than either alone. Four store
  regressions, three mutation-verified against the exact defect.

* **r16 `block`** — *the required e2e evidence is recorded as not run* [high]. Class: *the
  record is part of the deliverable*. The committed `evidence.json` still carried step 2 —
  the browser leg — as `fail` from the prior session that never got to run it. Every step
  re-run and re-recorded at the final code commit, with screenshots attached to step 3 as
  well as step 2, since step 3 is the one whose AC is about what a reviewer can SEE.
* **r17 `block`** — *the cool-down acknowledgement is not limited to manual swaps* [high].
  Class: *a waiver with no proof of who is waiving* — the r2 class, one layer in. SYS-49e
  ignores AUTOMATIC triggers for the whole window; SYS-49a(a) keeps MANUAL promotion
  available behind a confirmation. Two rules, two callers, and the gate could not tell them
  apart: it took a bare `ManualCooldownAcknowledgement`, and `HotSwapDemotionRequest` carries
  no trigger kind, so an automatic proposal converted into a request and handed
  `Acknowledged` executed straight through an active window.

  Fixed structurally rather than with another check: the waiver is now a field of
  `SwapOrigin::Manual` and exists nowhere else, so "automatic swap, cool-down waived" has no
  representation — `SwapOrigin::Automatic` is a unit variant with nothing to pass, and
  `waives_cooldown()` is the single place the two rules meet. Honest about its limit: this is
  not unforgeability, since no type inside one process can stop a caller misdescribing itself
  (`LiveDesignationConfirmation::from_operator` is `pub` and takes any string). What it does
  is remove the accidental path and give the guard something to check — four static
  assertions with their own mutation tests, and a four-case regression quartet including the
  non-vacuity control that a clear window still lets an automatic swap through, without which
  a gate that refused every automatic swap would pass and silently disable SRS-RESV-003.

* **r18 `block`** — *abandon can delete a newer same-pair provisional window it did not
  create* [critical]. Class: *"is this mine?" answered by identity when it needed provenance*.
  `abandon_provisional` matched on the strategy pair alone. For a RETRY of the same swap
  under a backwards clock, `begin_provisional`'s monotonicity rule keeps the newer record and
  the retry writes NOTHING — but on failure it abandoned anyway and deleted the first
  attempt's marker, removing the only window suppressing the automatic triggers after an
  interrupted swap. The backward-clock case the feature exists to make safe.

  `PendingCooldownWindow` now carries the attempt instant, and the store clears on FULL
  equality: an attempt clears exactly the record it wrote, or nothing — no need to thread
  phase one's outcome through the swap. Phase two still matches on identity, and must,
  because it rewrites the timestamp.

  Worth recording how the second half was found: the first mutation (`abandon` matching the
  pair again) went red, but the second (the token forgetting its instant) stayed GREEN — the
  execution-layer stub records whatever it is handed and never emulates the store's matching,
  so the token→store contract had no test at all. Fixed by asserting the abandoned record
  EQUALS the provisional one rather than counting calls, which is the assertion that makes
  the two layers agree.

* **r19 `block`** — *a confirmed retry can replace a newer provisional window with an older
  completion* [high]. Class: *the same rule, one slot over* — the r18 hole on the success arm.
  Monotonicity compared the offered completion against `last_completion` ONLY, so a
  confirmation whose clock had stepped backwards cleared a newer provisional marker on its
  way past and wrote the older completion, shortening the suppression an interrupted attempt
  had established.

  The rule the requirement actually states is about the window IN FORCE, and the window in
  force is whichever slot runs later — which `resolve` already knew and the writers did not.
  Both writers and the reader now call one `governing()`, so "the window in force" cannot
  mean one thing when it is read and another when it is written, and the static check
  requires every site that decides it to go through that function.

  Three store regressions, one domain test at the operator's repair surface, and two
  mutations: guarding only `last_completion` reddens two cases; making the READER disagree
  with the writers reddens four.

* **r20 `block`** — *the cool-down pre-flight can roll back a concurrent period change*
  [high]. Class: *a read-modify-write split across two locks is not locked*.
  `probe_writable` — the r4/r11 proof that a swap's window can be written before anything
  destructive runs — read the period OUTSIDE the lock and handed it to `set_period`, which
  reacquires the lock and wrote the stale value back. An operator running
  `configure --set-days 30` in that gap had their change silently reverted: the pane keeps
  saying 30 days and the cool-down expires after 7.

  Now one locked read-modify-write that writes back exactly the record it read — a
  pre-flight that proves the store is WRITABLE must not be able to CHANGE it.

  The guard needed tightening twice, which is the part worth remembering. `probe_writable`
  is private, and the writer check used a `pub fn`-only parser, so it had never looked at
  the one writer that was wrong. And once it could see it, the check still passed: it gave
  delegation credit to any writer whose helper held the guard — which is precisely how this
  defect is spelled. A writer that calls `load(` must now hold the guard ITSELF.

* **r21 `block` x2 + `warn` x2** — the round the reviewer went wide.
  1. *`begin_provisional` clobbers a DIFFERENT swap's provisional window* [block]. The
     arrangement r13/r15/r18/r19 had not covered: swap A→B publishes durably and is killed
     before phase two, leaving P1 as the only suppression; an acknowledged manual swap B→C
     (legal under SYS-49a(a)) overwrites P1 with its own P2 — which looks harmless because
     P2 is newer — and then FAILS, abandoning P2 and leaving nothing at all, with B live and
     its automatic triggers armed. `begin_provisional` now REFUSES to displace another
     swap's unconfirmed marker and says how to reconcile it; the same rule was already
     asserted on the other writer.
  2. *the execution arm opts in on the pane's DISPLAY knob* [block]. `_mount_hot_swap_
     execution_arm` keyed off `ATP_HOT_SWAP_DESIGNATION_STATE` and then hard-failed startup
     unless four more variables were set — contradicting the composition contract three
     functions above it ("any one composes without the others") and, worse, a boot
     REGRESSION: a deployment that had always set only the display knob would no longer come
     up. It now opts in on `ATP_HOT_SWAP_PROMOTION_LOG`, which exists for this route and
     nothing else.
  3. *contradictory proof lines* [warn]. `record-completion` could print
     `cooldown-window-started:true` and `:false` on one stdout. Emitted once now, after the
     re-read, so it is a claim about the VERIFIED window — and this repo's own strict parser
     accepts it, which the duplicate broke.
  4. *doc drift* [warn]. The `cooldown_window` field's comment claimed NOT_STARTED on a
     BLOCKED swap; the code and the registry both say UNKNOWN, and the boundary test
     asserted presence without value. Comment corrected, assertion tightened.

  **Five findings in one family** (r13, r15, r18, r19, r21#1), each an arrangement of the
  two slots the previous fix had not covered. Rather than a sixth patch, the contract now
  carries a closure ARGUMENT over the enumerated writer set: every writer either preserves
  or extends the suppression in force, and `guarded_writers` is checked, so a sixth writer
  cannot appear without being declared. That is what turns "the cases we thought of" into a
  property of the code.

* **r22 `block`** — *no operator path clears a stranded provisional cool-down* [high].
  Class: *an error message that names a capability the surface does not have*. My own r21
  refusal told the operator to "clear it if it did not complete", and nothing could: the
  only public write path was `record-completion`, which would have turned a swap that never
  completed into one that did. The honest options left were hand-editing the durable file or
  lying to the tool — and an unreconcilable marker suppresses the automatic triggers AND
  blocks every subsequent swap.

  `clear-provisional` is the recovery surface that refusal promised, and it is deliberately
  narrow: it names the swap (so an operator states which interruption they reconciled), it
  requires `--confirm` (retiring suppression is a safety act, not a cleanup), it can never
  touch a CONFIRMED completion, and it reports what it FOUND rather than exiting silently —
  "nothing matched" and "cleared" must not look the same to someone who believes they have
  just reconciled something.

  The static check's subcommand LIST became a discovery in the same commit. A list is the
  shape that failed at r2, and this round proved it again: the check named three
  subcommands, r22 added a fourth, and it would have said nothing. Exemptions are now
  declared in the contract with reasons, and a stale one is an error.

Every finding was accepted and fixed; none was disputed.

## Playbook updates

* `adversarial-precheck.md` — rule 0 gains the sharper form: a guard shaped like a CHECKLIST
  cannot catch the arm nobody added to it; discover the arms from source and prove the
  discovery itself cannot pass vacuously.
* `safety-paths.md` 47–49 — "reported loudly" is not "enforced" for an irreversible action,
  so move the check to where a guarantee is still available; order an unwaivable refusal ahead
  of a waivable one; the instant an operation STARTS is not the instant it COMPLETES.
* `honest-surfaces.md` 40 — a control that DISPLAYS a confirmation warning must TRANSMIT the
  acknowledgement, bound at arm time and only for the known-active case.
* `durable-writes.md` 26–28 — a guarantee spanning two writes needs TWO writes (provisional
  then confirmed); once phase one exists, "drop the token" stops meaning "nothing happened",
  so every abandon must be explicit and must NOT be a `Drop` impl; and a provisional marker
  is a new durable state that every reader has to be able to name, tri-state.
* `test-integrity.md` 37–38 — never `git checkout -- <path>` to undo a by-hand mutation (it
  restores from HEAD and eats the session's unstaged work in that file; `cp` to scratch
  first); and a reordering mutation must be asserted to have actually reordered.
* `pipeline-and-integrate.md` 41 — a structural reviewer refusal reviews NONE of your diff;
  budget it as the whole review, not one finding.
* `test-integrity.md` 32–33 — the self-disarming-control double-click trap, and matching a
  response by URL substring + method rather than `endswith` on a path that carries a query
  string.
* `safety-paths.md` 50 — a caller-supplied proof is a forgeable proof, and a static check
  over your own call sites is not a property of the API. Give the gate the port.
* `honest-surfaces.md` 41 — a recovery instruction is EXECUTABLE TEXT and inherits every bug
  the code has. After fixing a value, grep the strings that TELL someone what to do with it.
* `test-integrity.md` 36 — a guard written through a code generator inherits the
  generator's escaping, and a guard with no test of its own cannot tell you it is inert.
  `repr()` what you emit; give every critical-bypass guard a mutation test.
* `test-integrity.md` 35 — a GATED suite you did not run is not a suite that passed. Making
  `cooldown_state_path` required broke `tests/e2e/test_hot_swap_promotion.py`, and it stayed
  broken for four rounds because every run named three e2e files rather than the directory.
  MY OWN defect, found by finally running the whole gated suite.
* `test-integrity.md` 34 — rule 27's ambiguous-anchor trap fires from a SIBLING feature too.
  Adding `PendingCooldownWindow::mint` made `pub(crate) fn mint(` non-unique, which silently
  disarmed SRS-RESV-005's `check_receipt_encapsulation` (it searched the whole module and
  found the other match). Its own mutation test caught it. Both were fixed: the check is now
  scoped to `impl DemotionReceipt`'s span, and so is the test's mutation.

## Recommended follow-up (not done here, deliberately)

**Extend `SAFETY_PATH_RE` with `cool[_-]?down`.** `tools/critic_check.py` matches
`hot[_-]?swap`, which covers every file in this diff, but NOT `crates/atp-orchestrator/src/
cooldown.rs` or `cooldown_store.rs` — the two files holding all of the suppression logic and
all of the durability. A future session could rewrite the whole rule in those two files with
no paired `tests/domain/` test demanded. The change and its 17-case unit test were written and
mutation-verified this session, then dropped so the feature code could be reviewed at all (see
r1/r3 above). It needs its own human-reviewed commit; the diff is one regex alternative plus
`tests/unit/test_critic_safety_paths.py`.

## Resume / next

Nothing is left for this feature — it integrates `complete` and flips `passes:true`.

Two notes for whoever comes next:

1. **`block SRS-RESV-006 --on SRS-RESV-005` must NOT be attempted.** `tools/feature_deps.json`
   on main already carries `SRS-RESV-005 → SRS-RESV-006`, so the reverse is a cycle;
   `cmd_block` drops cycle-forming edges on stderr and still exits 0
   (`pipeline-and-integrate.md` r32). The prior plan ended with exactly that step.
2. **SRS-RESV-005's third blocker is cleared.** Its browser test no longer injects a cool-down
   — it composes the real `CliHotSwapCooldownSource` — so RESV-005 is now blocked only on
   SRS-RESV-002 (a ranking candidate) and SRS-EXE-006 / SRS-ORCH-004 (the flat-account and
   code-identity producers).
3. **That residual was closed in round 13, in this session.** It had been recorded in
   `hot_swap_cooldown_contract.deferred` as future work — a store writable at the pre-flight
   and unwritable seconds later, needing a two-phase window modelled on SRS-RESV-004's
   engage-then-amend. Round 13 found the same gap was reachable by a plain process kill and
   made it blocking, so the two-phase window is now SHIPPED and the deferred entry describes
   what actually remains: an unconfirmed window expires up to the SYS-49b liquidation timeout
   early, and a provisional window whose abandon also failed over-suppresses until an operator
   clears it. Both are the safe direction, both are visible, neither leaves the automatic
   triggers armed against a strategy that was just promoted.

   Round 14 blocked on the fact that the deferred entry still SAID it was future work while
   the code implemented it — contract drift in the architecture registry, and a fair finding:
   a future reviewer reading the registry would have concluded the race was intentionally
   open. Fixed in all four places that carried the claim, with a static check added so the
   next stale identifier in that block fails CI instead of a review round.
