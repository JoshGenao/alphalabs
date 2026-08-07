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
