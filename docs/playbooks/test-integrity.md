# Test integrity — a test that cannot fail is not evidence

Read this whenever you write a test, and before you believe a green one.

## Mutation-verify, always

1. **Every regression test must be proven to discriminate.** Remove the fix, confirm the
   test goes red, restore it. On the NOTIF-001 deadline work each of the two bugs was caught
   by *exactly one* test and by no other in the suite — without the mutation check there was
   no way to know that. `(NOTIF-001; SAFE-002)`
2. **Mutation anchors must be line-unique.** A `.replace(old, new, 1)` against a token that
   appears twice, or that rustfmt re-wrapped, silently no-ops and the test passes for the
   wrong reason. Anchor on a call-with-`(` token, or replace-all. `(SIM-004; SESSION 34)`
3. **Assert the harness reports the moment that matters.** A UI harness that only inspected
   the buffer *after* step 3 hid a duplicate that step 2 introduced and step 3 repaired —
   "wrong for four seconds is still wrong". `(LOG-001 r38)`
4. **A test that describes behaviour the change PRESERVED is not evidence of it.**
   `mutation_verify` names these exactly; the remedy is "tighten or delete", and when the
   pre- and post-change behaviour is identical by design there is no honest tightening.
   Fold the companion assertion into the test that DOES fail — one test asserting both
   directions of a boundary keeps the guard and stays evidence. Deleting it outright drops
   real protection against the over-correction. `(2026-08-12)`
5. **Do not put a `def test_*` in a docstring.** `mutation_verify` scans diffs for added
   test functions, so a usage example inside a module docstring reads to it as a real test
   that never fails — a false finding in the one tool whose worth depends on its findings
   being trustworthy. Same family as CLAUDE.md rule 9: a guard that cries wolf is a guard
   people learn to skip. Write the example without its `def` line. `(2026-08-12)`
6. **An identifier matcher built by dropping a prefix can match everything.**
   `verify_queue.discover_tests` derived a "compact" id form by dropping the first
   segment — `SRS-DATA-013` → `data013`, fine, but `API-3` → `"3"`, and a bare digit is a
   substring of nearly every source file, so those features matched the whole tree. The
   selection is EXECUTED and recorded as a feature's evidence, so a false match files an
   unrelated passing suite as proof. Require a letter and real length, or drop the form.
   `(2026-08-12)`

## False greens

4. **A harness that shells another test must confirm the inner test ASSERTS.** MD-006's gate
   shelled a *diagnostic* binary that prints per-operation results and always exits 0. It
   reported PASSED on `0/6 operations succeeded` against a dead IB connection.
   `(MD-006, 2026-07-21)`
5. **"The binary ran" is not "the thing worked."** A predicate of
   `returncode == 0 and "1 passed" in output` holds when every operation failed. Parse the
   real outcome (`=== 6/6 operations succeeded ===`). `(MD-006)`
6. **Feature-gated test targets can compile to nothing.** A crate-level
   `#![cfg(feature = "…")]` without `--features` yields `0 passed`. And libtest captures
   stdout of a *passing* test, so a tally your predicate greps for never arrives without
   `--nocapture`. `(MD-006)`
7. **A skipped check is a vacuous pass.** `run_ci_locally.sh` guards steps with
   `command -v ruff|mypy|pytest` and prints `skip: not installed`. In a fresh worktree it
   prints "✓ local CI mirror complete" having run none of them — that is how an unformatted
   file reached `main` and left `ruff format --check` red. Install
   `requirements-dev.txt` first, then read the step list. `(SAFE-003, 2026-07-27)`
8. **Don't let a fixture fabricate provenance.** Seeding the fixture store under the
   *requested* symbol makes any symbol "succeed" over invented data. Seed under a fixed
   symbol so an unseeded one fails closed. `(BT-001)`

## Test isolation — the order-dependent flake class

9. **Restore `sys.modules` and `sys.path`.** Mutation rigs that copy the repo to a tmpdir,
   `sys.modules.pop("atp_*")`, and reimport must restore. An autouse `_atp_import_sandbox`
   fixture in `tests/conftest.py` is the safety net; the rig itself is the honest fix.
   `(2026-07-28: 23 failures → 0)`
10. **Restoring a patched staticmethod leaks.** `original = Cls._m` unwraps the descriptor;
    restoring that plain function rebinds it as an ordinary method, and every later
    `self._m(x)` raises `TypeError: takes 1 positional argument but 2 were given` — in
    whatever file pytest runs next. `monkeypatch.setattr` has the same flaw. Capture
    `Cls.__dict__["_m"]`, call via `.__func__(...)`, restore as-is, and assert the shape
    afterwards. Classmethods round-trip safely. `(LOG-001 r34)`
11. **Suspect a leaked class patch before suspecting the code** when a test fails only in
    combination with another file — and read past the `finally` block's exception (a
    `flush of closed file` masks the real error). `(LOG-001 r34)`
12. **Reproduce this class in seconds, not in a 19-minute full suite:** run the suspected
    polluter file followed by the victim file. Collection order puts `tests/<subdir>/`
    before root `tests/test_*.py`. `(2026-07-28)`

## Concurrency tests

13. **Never run two `cargo test --workspace` at once.** ~37 sites across 12 test files build
    scratch paths with a fixed name and `remove_dir_all` them on entry; concurrent runs
    produce phantom failures in crates your diff cannot reach, and they move between runs.
    Check `pgrep -f "cargo test"` first. The safe idiom is PID-qualified:
    `temp_dir().join(format!("atp-data018-{}-{}-{}", tag, process::id(), line!()))`.
    `(2026-07)`
14. **MEASURE overlap, don't infer it.** "N reader threads each did ≥1 read" does not prove
    a read happened *during* a write. Record it: a `write_in_progress` flag the writer holds
    mid-write, readers bracket each read, assert `overlapping_reads >= 1`. `(DATA-017)`
15. **FAIL, never HANG.** Bound every wait with a deadline and set the stop flag from a
    drop guard, so a writer panic still releases the readers and the scope joins. A hung
    gate is worse than a red one. `(DATA-017)`
16. **Enforce lock LIFETIME, not token presence.** A static check that `acquire`/`load`/`save`
    all appear passes a regression that `drop(_lock)` before the save. Enforce order, forbid
    the premature drop, and add a non-vacuity test that injects it. `(DATA-017)`

29. **`mutation_verify` reverts TRACKED MODIFICATIONS only** — its own comment says so, and
    `git checkout` cannot restore a file the base never had. A feature whose implementation
    lives in NEW files is therefore left completely intact, every added test legitimately
    passes, and the tool reports *all* of them as "still pass without the change". That is a
    false accusation shaped exactly like a real one (cf. rule 22). Check
    `git diff --name-status origin/main..HEAD`: if the sources carrying the behaviour are
    `A`, the run proved nothing and the properties must be mutated BY HAND — one property per
    mutation, each killing exactly one named test. `(RESV-005)`
30. **A test can encode the bug.** `a_free_live_slot_still_promotes` asserted that an EMPTY
    live designation still promotes — the permissive behaviour was written down as the
    expectation, so it survived four review rounds and made the gate look covered. When a
    reviewer names a behaviour as wrong, grep your own tests for the assertion that blesses
    it before arguing. `(RESV-005 r5)`
31. **A `compile_fail` doctest is real evidence, and cheap.** Encapsulation claims ("this
    token cannot be forged outside the crate") are provable by two doctests that build as an
    external consumer — and they are mutation-verifiable: make the field `pub` and they go red
    with "Test compiled successfully, but it's marked `compile_fail`". Prefer them to a grep
    that merely asserts the word `pub(crate)` appears. `(RESV-005)`

## Browser evidence

32. **A wait that the INITIAL DOM already satisfies proves nothing.** The pane's static
    markup ships `data-state="deferred"`, so a negative test that waited for exactly that
    passed before any fetch resolved — and stayed green when the binary was mutated to accept
    a foreign snapshot. Anchor on a POSITIVE signal only a completed fetch can produce (a cell
    fed by a leg that still works), then assert the negative. `(RESV-005 browser leg)`
33. **An element screenshot of an animated card comes out BLANK** unless you wait on the
    ANCESTOR opacity chain. The dashboard reveals cards with `rise`
    (`delay = --i * 90ms + 120ms`); the target itself reports `opacity: 1` while its parent is
    still fully transparent, so every check passes and four empty PNGs get filed as proof.
    `(RESV-005 browser leg)`
34. **A full-page screenshot of a long dashboard is not evidence.** The pane sat at y=2933 of
    a ~3900px page — a few illegible pixels once viewed inline. Scope the shot to the element
    (`cap.shot(page, caption, element="#hs")`). `(RESV-005 browser leg)`
35. **Enumerate EVERY cell the control gates on before promising a browser walk.** UI-5's
    promote button requires candidate AND live-strategy AND `demotion_pending === false` AND
    a KNOWN cool-down — so the walk was blocked on SRS-RESV-006 as well as SRS-RESV-002, a
    dependency nobody had recorded. Read the actionability predicate, not the obvious cell.
    `(RESV-005 browser leg)`

32. **A browser test that clicks twice on a self-disarming control re-arms instead of
    confirming — silently, because both clicks succeed.** The UI-5 promote control disarms
    itself after 5s, and a `cap.shot()` between arm and confirm legitimately outlives that.
    The confirm click then landed on a resting button, no request was ever made, and the
    failure surfaced as a 30s `expect_response` timeout that says nothing about the cause.
    Wait on the real armed STATE rather than assuming a click produced it, and put nothing
    slow between the arm and the confirm. `(RESV-006, first real browser run)`
33. **Match a response by URL SUBSTRING plus method, never `endswith` on the path.** The
    pane posts to `/api/v1/hot-swap?confirm=true` — the confirmation token is a query param —
    so an `endswith("/api/v1/hot-swap")` matcher never fires and the test times out looking
    like a broken feature rather than a broken assertion. `(RESV-006, same run)`

34. **Rule 27's trap fires from a SIBLING feature too, and a static check has the same
    weakness as its mutation test.** SRS-RESV-005's `check_receipt_encapsulation` searched
    the whole module for `pub(crate) fn mint(` — unique when written. SRS-RESV-006 added
    `PendingCooldownWindow::mint`, and from that commit on `DemotionReceipt::mint` could be
    made `pub fn` while the check still found the OTHER match and reported the encapsulation
    intact. Its own mutation test is what caught it, by failing for the one reason that looks
    most like the guard working. Two lessons: scope a check to the type's `impl` SPAN rather
    than to a module-wide token, and when a peer feature's test breaks after your diff,
    suspect that you made one of ITS anchors ambiguous before you suspect the test.
    `(RESV-006, on SRS-RESV-005's guard)`

35. **A GATED suite you did not run is not a suite that passed — and a required new
    parameter breaks every caller, including the ones behind the gate.** SRS-RESV-006 made
    `cooldown_state_path` required on `mount_hot_swap_execution`, updated the callers it knew
    about, and ran `tests/e2e/` only as three named files for four review rounds.
    `test_hot_swap_promotion.py` calls that mount too and had been erroring the whole time —
    invisible because `pytest -m "not e2e"` skips the directory entirely, and because
    "ATP_RUN_E2E=1 pytest a.py b.py c.py" reads like e2e coverage in a commit message while
    covering three files. After changing a shared signature, `grep -rn "<name>(" tests/` and
    run the WHOLE gated directory at least once before believing the diff is clean.
    `(RESV-006, found four rounds late)`

36. **A guard written through a code generator inherits the generator's escaping — and a
    guard with no test of its own cannot tell you it is inert.** SRS-RESV-006 added a check
    rejecting a reintroduced forgeable field, wrote it via a Python heredoc, and `\b` inside
    a non-raw triple-quoted string became a literal backspace (`\x08`). The pattern could
    never match; CI would have stayed green while the critical bypass it guarded came back.
    Two habits close this: after emitting code programmatically, `repr()` the line you
    generated rather than eyeballing the file, and give EVERY guard for a critical bypass its
    own mutation test — reintroduce the defect, watch the guard fail. The same session had
    already been caught by a `compile_fail` doctest that failed for the wrong reason; both are
    the same trap, one in a regex and one in a type. `(RESV-006 r12)`

## Layer selection

`tests/unit` L1 · `tests/property` L2 · `tests/` L3 contract · `tests/boundary` L4 ·
`tests/integration` L5 (`ATP_RUN_INTEGRATION=1`) · `tests/e2e` L6 · `tests/domain` L7.

17. **Safety-path diffs need a paired `tests/domain/` test in the SAME staged set** — the
    deterministic critic blocks otherwise, including on a docs-only or notes-only commit
    whose *filename* matches `SAFETY_PATH_RE`. See
    [pipeline-and-integrate.md](pipeline-and-integrate.md).
18. **JS is testable here.** Node is available: drive the real `app.js` render path under a
    small DOM stub from a `tests/boundary/` test rather than asserting on source text. Skip
    (never silently pass) where node is absent. `(LOG-001 r13)`
19. **A wall-clock performance test must be `#[ignore]`-gated** in Rust; the deterministic
    critic only flags pytest/unittest skips. `(DATA-007)`
20. **A Rust test scratch dir keyed on `process::id()` and never removed WILL collide.**
    `atp-data`'s `access_journal::tests::tempdir()` builds
    `env::temp_dir()/atp-access-journal-<pid>-<seq>` and never deletes it. macOS recycles
    PIDs within ~99k, and a live run found **57,131** leaked `atp-*` scratch dirs (1.1 GB)
    from 12+ different helpers — so a fresh process inherits a *populated* directory. The
    failures land on the tests that assert ABSENCE or a specific corruption
    (`absent_journal_is_benign_empty`, `torn_tail_is_tolerated_not_corruption`,
    `corrupt_complete_line_fails_closed`, `recent.is_empty()`), in a crate your diff never
    touched, and they **pass in isolation** — the signature of a phantom, not a regression.
    Before believing such a failure: `find "$TMPDIR" -maxdepth 1 -name 'atp-*' -type d | wc -l`,
    clean, re-run the exact failing command. A new scratch helper must use a unique dir AND
    remove it (or the collision is only a matter of time). `(harness-p0, found live)`
21. **A test that fails spuriously is no more evidence than one that cannot fail.** Rule 6
    says mutation-verify; its mirror is that an intermittent red must be diagnosed, not
    re-run until green. Re-running until green is how a real regression gets attributed to
    "flakiness". `(harness-p0)`
22. **Run `mutation_verify.py` with the VENV interpreter.** It shells pytest via
    `sys.executable`. Invoked as `python3 tools/mutation_verify.py`, that is the system
    interpreter — which has no pytest — so pytest never runs, every added test falls
    through to the `"skipped"` branch, and the tool reports them all as **"still pass
    without the change"**. A false accusation that looks exactly like a real one: the same
    output appears whether your tests are worthless or your interpreter is wrong. Activate
    the venv first, and sanity-check that the named tests really do go red by reverting one
    source file by hand. Absent tooling is not a passing test. `(MD-003 s4)`
23. **Mutation-verify cannot see a test that lives in the file it reverts.** Rust unit
    tests sit in the same `.rs` as their subject, so `git checkout <base> -- <file>` removes
    the test along with the code and the run is vacuously green. Mutate the FUNCTION BODY
    instead, one property per mutation, and require each test to die to exactly one — that
    also proves the tests are orthogonal rather than three checks of one thing. MD-003's
    backoff had three: always-return-zero killed the growth test, cap-removed killed the
    threshold test, zero-guard-removed killed the healthy-path test. `(MD-003 s4)`
24. **An added blank line before an existing `def test_` makes that test look added.**
    `ADDED_TEST_RE` is `^\+\s*def\s+(test_\w+)`, and `\s` matches newlines — so a diff
    hunk ending in a bare `+` followed by the context line ` def test_old(...)` attributes
    a PRE-EXISTING test to your range, which then shows up as "cannot fail". Insert new
    tests where the following line is not a `def` (their own banner section, or the end of
    the file) rather than arguing with the report. `(MD-003 s4)`
25. **Monkeypatching the function that holds the bug tests nothing.** The telemetry branch
    shipped four consecutive fixes for "a failed reviewer is not recorded"; each added unit
    tests that monkeypatched `run_codex`, the very function whose real behaviour (setting
    `ATP_REVIEW_DISPATCHED=1`, so the shell deliberately stays silent) caused the drop. All
    four passed while the defect stayed live, and only a fifth round driving the REAL
    dispatcher against a stub reviewer caught it. When a defect lives in the seam between
    two components, the regression test must cross that seam. Stub the paid/slow leaf
    (a network reviewer), never the unit under test. `(harness-p1, found live, r5)`
26. **A test whose premise depends on ambient state passes for the wrong reason.** The
    first version of that seam test read the developer's real `tools/.codex_cooldown.json`;
    on a day when Codex was cooling down, `review()` short-circuits before the code under
    test ever runs — and the test goes green having exercised nothing. Force every
    availability predicate the path consults, and stub any writer that would mutate real
    state from a test run. `(harness-p1)`

## Mutating the right thing (SRS-RESV-004)

27. **Anchor a mutation on the declaration's SPAN, not on a token the file repeats.** A
    `.replace("    pub liquidation_cancel: SideEffectOutcome,", "", 1)` mutated whichever
    struct declared it FIRST; `rindex` mutated the LAST — and atp-types has four. Either way
    the check under test received an intact subject and stayed silent, which reads exactly
    like "the guard works". The same trap has three shapes: a field several structs share, a
    trait method anchored as `fn last_one(...);\n}` (stale the moment the trait grows), and a
    call the function now makes twice (`alerts.dispatch` once the probe-inconsistency branch
    landed). Find the `pub struct X {` / `pub trait X {` and its closing brace, then mutate
    inside that span — and for a match ARM, index forward from the `match` itself. Bit this
    session FOUR times: each time the guard under test received an intact subject and reported
    success, which is the failure mode that looks most like working. A fifth shape to expect:
    once a function calls the same port twice (`lock.engage` then `lock.amend`), neither
    `replace(..., 1)` nor `rindex` lands where you mean. `(SRS-RESV-004)`
28. **A harness that ran nothing must not return a verdict.** `mutation_verify` passed pytest
    every changed `tests/` path — including Rust `.rs` files, which `is_test_path` also
    matches — so pytest exited 4 with "ERROR: not found", collected NOTHING, and all 34 added
    tests fell through `run_tests`' final `else` to "skipped", reported as *"still pass
    without the change"*. The output is indistinguishable from a real finding; the only tell
    was that the accusation was unanimous. Rule 22's false-accusation mode with a second
    cause. If a verdict indicts EVERY test you added, suspect the harness before the tests —
    and make the tool raise instead of classifying. `(SRS-RESV-004, found live)`
7. **Evidence certifies the commit it was produced on — steps, artifacts, AND critic
   verdicts.** Each was bound in a separate pass, and the gap between passes was
   exploitable every time: a screenshot reused across runs, then a run and its
   screenshot both captured on an older commit (internally consistent, still stale),
   then an `approve` recorded against a tree the implementation had since left. The
   question is never "does this equal HEAD" — evidence is recorded BEFORE the commit
   that carries it, so a valid record names the parent of its own chore commit. It is
   "has any non-evidence path moved since". `code_changed_since` answers it and
   returns `None`, never `[]`, when it cannot — an unverifiable head is not a fresh
   one. `(2026-08-13/14, three rounds)`
8. **A tool that reports on ANOTHER system must never render its own blind spots as
   findings.** `tools/ci_watch.sh` needed four corrections in one afternoon, all the
   same bug pointing outward: an in-flight run shown with a blank status (GitHub
   returns `conclusion: ""`, not null, so jq's `//` does not fall through); running
   workflows reported as failures; cancelled ones — the `cancel-in-progress`
   concurrency group doing its job — reported as failures; and an abbreviated sha
   handed to `gh run list --commit`, which matches only the full 40 chars, printing
   "no workflow runs found … either the push has not landed or no workflow matches
   it" about a commit with three live runs. That is rule 3 one layer out: absence of
   evidence rendered as evidence of absence, about a system you are only observing.
   Distinguish *cannot see it*, *not finished*, *finished badly*, and *superseded* —
   an alarm that fires on benign states is one people skim past on the day it is
   right. `(2026-08-14)`
