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
    inside that span. Bit this session three times, in all three shapes. `(SRS-RESV-004)`
28. **A harness that ran nothing must not return a verdict.** `mutation_verify` passed pytest
    every changed `tests/` path — including Rust `.rs` files, which `is_test_path` also
    matches — so pytest exited 4 with "ERROR: not found", collected NOTHING, and all 34 added
    tests fell through `run_tests`' final `else` to "skipped", reported as *"still pass
    without the change"*. The output is indistinguishable from a real finding; the only tell
    was that the accusation was unanimous. Rule 22's false-accusation mode with a second
    cause. If a verdict indicts EVERY test you added, suspect the harness before the tests —
    and make the tool raise instead of classifying. `(SRS-RESV-004, found live)`
