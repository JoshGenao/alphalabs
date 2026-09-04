# Scope, serialization, and stopping honestly

Read this at Step 4.1 (classifying the work) and again whenever a review will not converge.

## Before you build

1. **If the claimed feature is already built, do not rebuild it.** Read
   `progress.d/session-<id>.md`, then VERIFY its claims against the tree (`git ls-files`,
   grep the named modules). UI-5's second session found every artefact present and clean —
   the scheduler had simply re-offered it. Five sessions across the log produced zero code
   for this reason. The honest output is `block --on <owners>` + `release`, and a note saying
   so. `(UI-5 s2)`
2. **`unmet_deps=[]` does not mean closeable.** The dependency graph models features, not
   capabilities. MD-003 showed no unmet deps while both of its live producers were absent
   from the IB transport surface entirely. Check that the capability your AC needs actually
   exists in the code. `(MD-003)`
3. **If the work is premature feature work, say so and recommend deferring.** Twice in one
   session the operator answered an honest "this is actually SRS-ORCH-002 territory" with
   "stop here" — not "push through anyway". A deferral with a corrected breadcrumb is often
   the right output even when the ask was framed as a fix. `(SESSION 20)`
4. **Prefer the foundational substrate the cluster reuses** over another surface. But note
   the hard-won correction: substrate alone does not reduce the failing count. A cluster
   closes when ONE REAL CONSUMER's execution path invokes it — "a standalone callable
   function is not wiring a consumer". DATA-007 took S68→S74 to learn that, with four
   premature flips reverted. `(DATA-007 chain)`

## Classifying the outcome

5. **`complete` means every step passed SOLO.** If any step needs IB, integration, live, or
   dashboard-e2e, the mode is `serialized`: the code merges, `passes` stays false, and the
   operator finishes verification. This is the honest path — 35 of the 38 session notes on
   file end here, and that is not a failure.
6. **Name the exact step and the exact blocker.** "Serialized" without "which step, whose
   owner" is what makes the next session re-derive everything.
7. **A serialized feature's e2e ships UNRUN.** The test and the implementation were written
   in the same session and never checked against each other, so the first real verification
   run routinely fails on a detail neither side noticed. That is the point of the run. **Fix
   the implementation, not the assertion** — relaxing it is the false-green trap. Diagnose
   first (`git log -- <test>` showing a single commit shared with the feature = never run),
   read the test's stated intent, and check the sibling branch for asymmetry. Regression-scope
   by blast radius, not by feature id: a closed-green feature going red is the worst outcome
   of a close. `(RES-003)`
7b. **"Serialized" is four different situations; say which one.** The label drove one
   bucket for eleven features, and telling them apart cost every fresh-context session
   ~2,000 lines of note reading. **A** = done, evidence never recorded (close it).
   **B** = an unbuilt FEATURE is in the way (`block --on`). **C** = a real-world resource
   no feature owns (`external_blocker`). **D** = a graph cycle (an operator decides which
   direction is a code edge). Rule 6 already demands the exact step and blocker; this is
   the shape that demand takes. `tools/verify_queue.py list` derives it, and
   `docs/verification-queue.md` is the standing triage. `(2026-08-12)`
7c. **A visual acceptance criterion needs a visual artifact.** "The dashboard shows IB
   equity, daily and cumulative P&L" is a claim about what a human would SEE; an exit code
   cannot show it, and closing on one asks the reviewer to take the record's word for
   exactly the thing it cannot evidence. `evidence.py verify` now refuses an `e2e` or
   `live-ib` record with no image on the AC step. Browser tests get it free from
   `tests/e2e/capture.py`; `evidence.py render` writes the `EVIDENCE.md` the reviewer
   reads on GitHub. Not applied to `solo`/`integration` — their captured stdout IS the
   artifact, and demanding a screenshot of `cargo fmt --check` only teaches everyone to
   produce a meaningless one. `(2026-08-12)`

## When the review will not converge

Some loops are structurally non-convergent: each round surfaces the NEXT deferred dependency
as "do not ship". That is not a sign the work is bad.

8. **Fix every IN-SCOPE finding first** — they are real. FAC-001's ten rounds caught three
   genuine fail-open bugs. Do not dispute findings to save rounds.
9. **Recognise the stop signals:**
   - The reviewer is walking a graph whose authority is a deferred component (EXE-008's state
     machine, whose edges come from the unbuilt IB adapter). Add only the UNAMBIGUOUS edge;
     scope the debatable ones to the owning adapter rather than guessing semantics.
   - Two findings are in direct tension and only a deferred feature closes both (DATA-007 r1
     forbade a RAW default; the fix triggered r10, that the binding no longer serves the
     documented call). When each fix re-opens the other, stop.
   - The reviewer's target is load-bearing for a DIFFERENT shipped feature's pinned contract
     (`submit_live_order` is pinned `pub` by `error_handling_check.py` and by ERR-1/2/3's
     tests). Layer the new authority on top; keep the pinned primitive byte-stable.
   - The block names ONLY the deferred scope and its own recommendation is "keep it
     serialized". Take it.
10. **Then do the honest close:** scope every claim precisely (say "observational" or
    "computation over the inputs given, not their trustworthiness", not "enforced"), name the
    deferred owner in `deferred[]` AND the check's `_DEFERRED_OWNERS` AND the module docs, add
    an L7 pin asserting the deferral is declared, get operator authorization, and record the
    verdict verbatim — "judgment loop did not reach APPROVE on deferred scope, committed on
    human authorization". **Never fake an APPROVE.** `(FAC-001; EXE-001; EXE-008; DATA-007)`
11. **A convergent loop looks different.** When the closeable scope is a FINITE set you can
    enumerate — `grep -rn "StructuredOrderError {" crates/ | grep -vE "/tests/|/bin/"` gave
    ERR-001 exactly five construction sites — the reviewer walks them one per round and then
    stops. Do the grep up front and fix all of them in round one. `(ERR-001)`
12. **A clean APPROVE on a serialized scope needs no authorization ritual.** Integrate
    serialized, then `block --on` the deferred owners. `(DATA-020)`

## Reframing an AC so it can flip

Occasionally a feature is held `passes:false` by a test that can only ever SKIP in the real
deployment. The operator may authorize reframing the AC to its provable structural form —
but the acceptance record IS the contract, so this is a full requirement reconciliation and
the reviewer will block until every source agrees.

13. Make the test non-skipping AND keep the old empirical check as a runs-when-present step
    (nothing is "removed"), asserting a strictly STRONGER invariant.
14. Reconcile `feature_list.json` — its AC step AND its "leave passes false until end-to-end"
    step. A branch cannot edit it; the operator does, on main, with a surgical raw-string
    replace (never a JSON reserialize, which reformats the whole file).
15. Reconcile `docs/SRS.md`: both the AC column and the requirement STATEMENT. A statement
    that permits what the new AC refuses is itself a contradiction.
16. Flip via `integrate --mode complete --force-complete` — the honesty guard
    false-positives on stray keywords in the description.
17. **The reframe does NOT work when the AC quantifies over producers that do not exist**
    ("route ALL non-live strategy orders" while the strategy host is a stub). Fixture-CLI
    evidence proves the wired components, not the deployed path. Restore the explicit
    serialized entry naming the producer owner, `block --on` it, integrate serialized, and
    record that the operator may force-complete. Do not argue the verdict. `(SEC-002; EXE-002)`

## Closing a widely-referenced feature

18. **Flipping a widely-consumed dependency turns every "deferred SRS-X" into a public
    contradiction.** DATA-007 was referenced ~50 times and took 7 rounds of whack-a-mole.
    Grep repo-wide FIRST — `deferred SRS-<id>`, `owner: SRS-<id>`, `STAYS passes:false` near
    the id, `<id> … not built` — across JSON, check tools, and Rust source/bins/tests. Verify
    no check or mutation test asserts the token's PRESENCE before sweeping it.
19. **Write the guard test that scans the whole surface**, including a proximity regex
    anchored on the id first, or the loop does not converge and the drift returns.
20. **Frame the flag correctly on the branch:** "is COMPLETE and closes to passes:true AT
    INTEGRATION (this branch does not edit feature_list.json)" — a bare "(passes:true)" reads
    as a prose/source-of-truth split. `(DATA-007 close)`

## A guard is not the property it guards (SRS-MD-005, 13 rounds)

**A stop signal, added because this one cost eight rounds:** when the reviewer's
findings move from the FEATURE to the CHECK TOOL that guards it, and each fix
invites the next probe of the same surface, you are no longer hardening the
system — you are hardening a scanner. SRS-MD-005 spent rounds 1-4 on real
feature defects, round 5 on the sharpest of the session, and rounds 5-13 almost
entirely on five successive holes in one static check.

Two questions tell you which side of the line you are on:

1. **Is the property enforced anywhere but the check?** If a compiler, a type or
   a module boundary already enforces it, the check is a SECOND layer and a hole
   in it is not a hole in the system. SRS-MD-005's "every admission point
   consults the window" is enforced by Rust privacy — the registry's field is
   private to its own module, proven by E0616 and two mutation-verified
   `compile_fail` doctests. The scanner is belt-and-braces.
2. **Would the next finding change shipped behaviour?** If the answer is "it
   would change a regex", the loop has stopped paying for itself.

When both point the same way, stop and say so — with the round count, the
verdict verbatim, and what a fresh round would probably find. Do not fake an
APPROVE, and do not quietly delete the check either: an imperfect second layer
over a compiler-enforced property is still worth having. `(SRS-MD-005 r5-r13)`

**The counterweight, from the same session:** round 12 was the first
`warn`-only verdict and still found a genuine operator-facing defect — a refusal
that told the operator to retry something that could never succeed. Rounds that
look like diminishing returns are not always diminishing returns. The signal is
WHERE the findings land, not how many there are. `(SRS-MD-005 r12)`

## Ask the operator when

- The scope posture is a genuine cross-feature commitment (routing other features' CLIs
  through your new coordinator).
- The defer-vs-override decision on a real gap — get it BEFORE the loop, not after.
- The auto-picked feature contradicts the standing "build foundational, highest-impact"
  directive.
- You are about to stop a non-convergent loop and commit on authorization.

## Closing a widely-referenced feature: measure it, then ratchet (NOTIF-001)

21. **The drift is repo-wide and pre-existing, not something your close creates.** A
    proximity scan on 2026-09-01 found **274 contradiction-shaped lines across 38 of the
    58 closed features** — every one of them a "deferred SRS-X" naming a feature that is
    already done. Rule 18 says grep first; this is what the grep actually returns, and it
    is why DATA-007's seven rounds of whack-a-mole were a symptom rather than bad luck.
    Budget for it, or scope the guard so it does not demand a 274-line sweep nobody has
    agreed to. `(NOTIF-001)`
22. **Make the guard a RATCHET, not a repo-wide gate.** `tests/unit/test_closed_feature_references.py`
    enforces only ids in `SWEPT_FEATURES`; closing a feature and adding it there is the
    moment the sweep gets done, while the measured backlog stays visible in the docstring.
    A gate that fails on 274 pre-existing lines gets an exemption list so long it means
    nothing — which is the same as no gate. `(NOTIF-001)`
23. **Not every match is a contradiction, and the matcher's SHAPE is where the work is.**
    Four iterations, each fixing the previous one's blind spot: per-line windows missed a
    wrapped comment whose "landed" sat on the next line; a character window spanning line
    breaks pushed 7 findings to 56 because `runtime_services.json` packs unrelated contract
    blocks within 140 characters; searching the whole line re-found 13 for the same reason.
    The shape that works is **asymmetric** — the CLAIM must be near the id AND on its line,
    the EXONERATION may wrap. State a contradiction on one line and you are flagged; answer
    it anywhere nearby and you are not. `(NOTIF-001)`
24. **"Deferred to SRS-X" is often still TRUE after X closes** — it usually means *that
    consumer's own wiring* is deferred, not that X is missing. Rewriting to name the missing
    PART ("SRS-NOTIF-001's dispatcher landed; a runtime that subscribes has not") is both
    more accurate than deleting the word and more useful than leaving it. `(NOTIF-001)`
25. **Re-point the contract VALUES, not just the prose.** The dashboard's alert feed carried
    `data_source: "deferred:SRS-NOTIF-001"` — a shipped payload value asserted in three
    tests. After the close it named a DONE feature as the thing being awaited, visible to
    the operator on the dashboard itself. Find the feature that genuinely owes the surface
    (here UI-1, whose own pane it is) and move the value there. `(NOTIF-001)`
