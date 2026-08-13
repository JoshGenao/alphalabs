# Committing, rebasing, and integrating in a parallel-worktree repo

~48 worktrees can exist at once and they share one git object store, one stash stack, and
one `main`. Most of this playbook is about that sharing.

## Before the first commit

1. **`git status -sb` — do not trust the session-start snapshot.** It is taken at launch; a
   concurrent session can check the primary directory out onto an `agent/<ID>` branch
   mid-session. On 2026-07-31 the snapshot said `main` while the directory was on
   `agent/SRS-NOTIF-001`, four commits ahead, two of them another session's. The tell is
   `agent_pool.py status` still reporting the old `done:N` — the scheduler reads
   `origin/main`, so a flip that never reached main does not move the board.
2. **Untracked files you did not create mean a concurrent session is mid-build.** Never
   `git add -A`; scope every add to explicit paths.
3. **Confirm you are where you think you are.** A fresh Bash shell does NOT inherit the
   launcher's exports and defaults to the primary checkout — `ATP_FEATURE_ID` can be empty
   and cwd can be the main repo (observed: `./init.sh` built main's env and wrote
   `progress.d/plan-.md` into main). Prefix commands with
   `cd <worktree> && export ATP_FEATURE_ID=… ATP_DEV_PORT=… ATP_AGENT_OWNER=<host:pid>`, and
   verify with `git rev-parse --show-toplevel`.

## Never do these

4. **Never `git stash`.** The stash is repo-global: on a clean tree `stash` is a no-op, so
   the later `pop` applies a *sibling's* WIP onto your branch, producing add/add conflicts in
   files you never touched. Recovery is `git reset --hard HEAD` (the foreign stash stays in
   the list). Compare with `git show origin/main:<path>`, `git diff origin/main...HEAD`, or a
   throwaway worktree.
5. **Never `git reset --soft origin/main` after main has moved.** It makes the NEW main your
   parent WITHOUT merging it, so your diff silently REVERTS the sibling edits to any shared
   manifest you also touched — and the integrate rebase is then a no-op, so nothing
   re-merges. Reset to the fork point instead. Verify with
   `git diff origin/main...HEAD --stat`: demand 0 deletions and no sibling files.
6. **Never edit an already-tracked `progress.d/plan-<id>.md`.** It is writable exactly once,
   while untracked. Committing it on a branch trips `shared_state_violations` (exit 6);
   leaving it dirty makes `git rebase` refuse before it starts — and `cmd_integrate` reports
   that as "rebase onto origin/main conflicted", which reads like a sibling conflict and is
   not one. Check `git ls-files --error-unmatch` first; put the new plan in the session note.
7. **Never run a whole-crate `cargo fmt`.** The local toolchain is newer than whatever last
   formatted the repo, so it rewraps pre-existing lines — and several L3 contract tests
   mutate Rust source with a literal `source.replace("MemBelowFloor { mem_mb: u32, … },", …)`.
   A rewrap makes the mutation a no-op and the test fails with "CheckError not raised". Add
   Rust with the Edit tool, match surrounding style, and verify only YOUR line ranges with
   `rustfmt --emit stdout` diffed against the working file.
8. **Never pass a file list that can contain `.json` to `ruff format`** — see
   [contract-drift.md](contract-drift.md) rule 19.
9. **Never bypass the critic** (`ATP_CRITIC_BYPASS=1`, `--no-verify`). Only humans bypass.

## The critic gate

10. **A safety-path diff needs a paired `tests/domain/` test in the SAME STAGED SET.**
    `SAFETY_PATH_RE` contains literal feature ids, so `progress.d/session-SRS-SDK-004.md`
    *is* a safety path and a notes-only chore commit is blocked. Fold the note into the same
    commit as the domain test: `git reset --soft HEAD~1`, re-add both, `git commit -C
    ORIG_HEAD`. Amending does not help — `--staged` only sees newly staged files.
11. **`git commit --amend` diffs the INCREMENTAL staged change.** If the domain test is
    already in the prior commit, the amend has none and the critic blocks — silently, if you
    redirected output. After any amend, `git show HEAD:<file>` to confirm it landed.
12. **After a `reset --soft`, the index is the last-commit tree, not the working tree.**
    `git add -A` (scoped) or your working-tree fixes silently do not get committed.
13. **A change to `tools/critic_check.py` is a prep commit the reviewer will not
    auto-approve** (it reads it as self-modification pending human review). Consider dropping
    it if an existing safety path already covers your diff.
14. **Resolve rebase conflicts Read-before-Edit, and never `git add` after an Edit failure.**
    A failed Edit followed by a chained `git add && git rebase --continue` committed conflict
    markers. Separate the steps: resolve → `grep -c '<<<<<<<'` is 0 → build → then continue.

## Running the gate

15. **`tools/run_ci_locally.sh` needs the venv and the dev requirements.** `init.sh` installs
    `requirements.txt` only, and the mirror SKIPS ruff/mypy/pytest when they are absent while
    still printing "✓ local CI mirror complete". Install `requirements-dev.txt` first and
    read the step list. See [test-integrity.md](test-integrity.md) rule 7.
16. **Main is pre-existing mypy-red and partly ruff-dirty.** Keep YOUR files clean; do not
    reflow unrelated files. When you must fix a pre-existing format red, fix only the
    offending file, in its own commit.
17. **Run the gate's real command after your LAST edit** — `cargo clippy --workspace -- -D
    warnings`, not `cargo build | grep '^error'`. Check `$?`, don't grep. `(NOTIF-001)`
18. **Check `pgrep -f "cargo test"` before a workspace suite.** See
    [test-integrity.md](test-integrity.md) rule 13.

## Gates that fail for reasons no diff caused

- **Do not commit `progress.d/plan-<id>.md`, even though Step 4.1 tells you to write it.** The
  integrate guard treats everything in `progress.d/` except `session-<id>.md` as shared
  coordination state and refuses the branch — at the very last step, after the whole gate has
  passed. Write the plan there if you want it as a working aid, but keep it out of the commit
  (or put its content in the session note, which is the artefact that survives anyway).
  `(SRS-RESV-004, hit at integrate)`


- **A gate that is red in every worktree is red for nobody.** `docs_link_check` failed in every
  `alphalabs-wt-<id>` on two correct references: `.git/hooks/pre-commit` (in a linked worktree
  `.git` is a FILE holding a `gitdir:` pointer, so the literal path never resolves) and
  `tools/.agent_runtime.json` (the scheduler writes it in the PRIMARY checkout). Both made
  `run_ci_locally.sh` red for work that did not cause it, which is CLAUDE.md rule 9's
  "a guard that always fires is a guard everyone learns to ignore" arriving from a new
  direction. Resolve `.git/…` through `git rev-parse --git-common-dir`, and give gitignored
  RUNTIME artifacts an explicit allowlist — an allowlist, not a blanket exemption, so a genuinely
  dead path is still reported. Before assuming a red gate is yours, check whether the file it
  names is even in your diff. `(SRS-RESV-004)`

## Integrating

19. **`cmd_integrate` targets `alphalabs-wt-<feature-id>`, not the directory you are in.** On
    2026-08-03 a session running in `alphalabs-wt-md003-stream` integrated the stale
    `alphalabs-wt-SRS-MD-003`, which sat exactly at `origin/main`: the rebase was a no-op, the
    push was a no-op fast-forward, and it printed `✓ integrated` while five commits of real
    work stayed on the session's branch. Always verify:
    `git fetch origin && git merge-base --is-ancestor <your-first-feat-sha> origin/main`.
20. **Recovery for that case:** `git -C ../alphalabs-wt-<ID> merge --ff-only <your-branch>`
    then re-run integrate. Do not `git push origin main` by hand — the locked integrator is
    the only writer of main. Re-claim first if siblings are active (the false success
    released your lease).
21. **A fresh shell has no `ATP_AGENT_OWNER` and possibly no ssh identity.** Without the
    owner, integrate refuses with "leased by another active session" naming your own
    launcher's PID; without `ssh-add ~/.ssh/id_ed25519`, the push fails after the local
    rebase. Integrate is **idempotent** — fix the auth and re-run.
22. **Nothing but `progress.d/session-<your-id>.md` may come from a branch.**
    `INTEGRATE_ALLOWLIST` (feature_list.json, progress.txt, progress.d, feature_deps.json) is
    integrator-only.
23. **`block --on` is IMMEDIATE; a serialized NOTE is not.** `cmd_block` writes
    `ROOT/tools/feature_deps.json` in place under the lock, so the edge takes effect at the
    next claim regardless of git staleness. A serialized note only reaches the scheduler once
    it can read it from the integrated tree. **When a serialized feature has real unbuilt
    producers, always record them with `block` even if you also write the note** — derive the
    owner ids from the code (the deferred-cell `*_OWNER` constants), not from prose, and
    exclude producers no acceptance criterion depends on. Over-blocking is as dishonest as
    under-blocking.
24. **A feature with no dependency edge and a flip-blocking gap returns to the ready frontier
    every cycle.** That is the churn loop; the edge is the fix. `agent_pool.py status` now
    flags awaiting-verification features that have no edges.
25. **`feature_list.json`'s `notes` field is updated by NO tooling path.** `close_feature.py`
    has no notes handling and `integrate --mode serialized` only syncs the deps file, so a
    serialized feature's notes drift until a human edits them on main — and the reviewer
    reliably raises "the feature record contradicts the shipped scope". You cannot fix it on
    the branch. Leave the file byte-identical, put the full replacement text verbatim in your
    session note under a labelled section, and answer the finding with "this is a tooling
    gap, not a decision the branch may make." Budget one round for it.
26. **A green CI run does not mean every step passed.** `ci.yml`'s mypy step carries
    `continue-on-error: true`, so the job, the run, and the step all report `success` while
    printing four real errors. `gh run list` showing green is not evidence a gate held —
    read the step's own output, or grep the log for `error:`. Any step you are relying on,
    verify is actually blocking. `(harness-p0)`
27. **A "mirror" can diverge in three dimensions, not one.** `run_ci_locally.sh` vs `ci.yml`
    differed in the *step list* (25 vs 32 checks), in *argv* (22 checks took
    `--require-cargo` in `init.sh` and bare in CI — the flag turns a missing-toolchain SKIP
    into a FAILURE), and in *blocking semantics* (mypy blocking locally, advisory in CI).
    Fixing only the list leaves a mirror that still lies. `tools/gates.json` now carries
    scope + argv; blocking-ness is called out inline in the runner. `(harness-p0)`
28. **`pgrep -f <anything the prompts mention>` always matches while an agent runs.** The
    session's argv IS `prompts/coding_prompt.md` (and the reviewer's is
    `critic_prompt.md`), so `pgrep -f "cargo test"` and `pgrep -f run_ci_locally` both
    match with nothing of the sort running. Match the executable, not a command line:
    **`pgrep -x cargo`**, or `ps -eo pid,command | grep "[r]un_ci_locally"`. A guard that
    fires unconditionally is one everybody learns to ignore, which is worse than no
    guard. `(harness-p0, found live — twice, the second time by the fix for the first)`
29. **A harness change cannot pass the judgment critic, and that is structural.**
    `critic_prompt.md` refusal clause 2 requires a BLOCK when the reviewer cannot
    identify the in-flight feature in `feature_list.json` — and harness/pipeline work
    has no entry there by design (22 categories, none for tooling). So every harness
    branch ends on `verdict: block, findings: [meta:missing-feature-record]` even when
    no code finding survives. Read the FINDING LIST, not the verdict: if the only
    block is that one, the review is clean and the operator merges, which is how
    `80fe849`, `ad43185`, `226d0a1` and the P0–P2 branches all landed. Say so
    explicitly in the commit rather than letting a `block` look like an approval —
    and never invent a `feature_list.json` entry to satisfy it. `(harness-reexec r2)`
30. **Telemetry only sees the path it was wired into, and only the shape its author
    imagined.** SRS-MD-003 ran its 7 rounds through `tools/codex_review.sh` directly;
    `append_round` lives in `adversarial_review.py`, so none reached
    `.harness/runs/<id>/review.jsonl` and the note claimed an APPROVE the ledger had
    no trace of. Those it did record stored `rules=['?']`: Codex emits
    `{"title","severity":"high"}`, the fallback `{"rule","severity":"block"}`, and
    only the latter was read. Two entry points to one gate with one instrumented is
    the same defect as three divergent check lists. Use `adversarial_review.py` — it
    dispatches, fails over, and records. `(SRS-MD-003, found live — both halves)`
31. **Killing a gate run leaves its children alive.** Stopping the wrapper shell does not
    stop `cargo test --workspace`; it re-parents and keeps the `target/` lock, so the next
    run trips the rule-9 guard with a 6-minute-old orphan. After aborting a gate, check
    `pgrep -x cargo` and `ps -eo pid,command | grep "[r]un_ci_locally"` and clear both
    before re-running. `(harness-p0, found live)`
32. **A cycle-forming `block --on` is REFUSED, with the cycle printed — exit 13.**
    *(FIXED; this rule used to describe the defect.)* `cmd_block` previously dropped
    cycle-forming edges to stderr and returned 0, so a session checking the exit code
    believed it recorded a block that did not exist — and `if cur:` meant a feature
    with no prior edges whose every edge was dropped had nothing written while still
    printing `✓`. Four features (MD-003, MD-001, EXE-001, PERF-001) sat unrecordable
    behind it for six weeks. It now writes nothing, exits 13, prints the full path
    (`SRS-MD-003 -> SRS-MD-001 -> SRS-PERF-001 -> SRS-MD-003`) and names the one
    `unblock` that breaks it. All-or-nothing: a partial write behind a non-zero exit
    is the ambiguous state that caused the confusion. `(MD-003 s4; fixed 2026-08-12)`
34. **An edge meaning "needs the code" and an edge meaning "needs the flip" are not
    the same edge.** Both cycles above were the first kind recorded as the second.
    SRS-PERF-001 is a measurement substrate — "there is no standalone CLI" — that
    consumes the MD-003 and SDK-004 runtimes, whose code is on main; it never needed
    their `passes`. Cutting those two edges dissolved both cycles and revealed that
    SRS-NOTIF-001 unblocks **56 of 120** features with no unmet feature dependency at
    all. Before recording an edge, ask which one you mean. `(2026-08-12)`
35. **A blocker no FEATURE owns needs `external_blocker`, never `block --on`.** "30
    real market-hours days", "an SMS provider account", "a PTP-disciplined host" cannot
    be a dependency edge, because a dependency edge asserts some feature owns the
    blocker — and if none does, the edge never clears. SRS-REL-001's note said this and
    correctly recorded nothing, which left it in `ready`, then in the serialized-note
    bucket, dragging `assess_frontier` into DEADLOCK over work no agent could do.
    `external_blocker` takes it out of the frontier and into `status`'s procurement
    section. Four features carry one. `(2026-08-12)`
36. **An observation that is also a verdict becomes a ratchet with no release.** One
    `Outcome: serialized` note fed BOTH `serialized_notes()` (feature leaves the claim
    pool) and `classify_verification.derive()` with `needs_review=False` (pinned
    non-solo forever), so a single wedged-gateway session permanently reclassified
    SRS-MD-003 — whose own step 2 says "fixture market data, provider mocks" — as
    `live-ib`, and no session could claim it to undo either half. An observation may
    PROPOSE; only a human may pin. `(2026-08-12)`
37. **`verify_queue.py check` before you claim, and read what it says is class C.**
    `list` ranks the queue, `show <FID>` is the whole verification brief in one screen
    (AC, evidence gaps, discovered tests, the note's own Resume/next), `check` reports
    only drift and exits 1 when something needs a decision.
    `.github/workflows/verification-watch.yml` runs it daily and on every main push
    that touches the board. `(2026-08-12)`
33. **Record step evidence AFTER the final code commit, in its own commit.** The record
    stamps the HEAD it ran against, so `git commit --amend` orphans it — the reviewer
    checks `git merge-base --is-ancestor` and blocks on evidence that "did not exercise
    the changes in this diff". Putting `evidence.json` *inside* the feat commit cannot
    express the right thing either (the hash does not exist yet, so it stamps the
    parent). Sequence: commit the code, run `evidence.py run`, commit the record.
    `(MD-003 s4 r6)`
34. **A reviewer outage is an `attempt`, not a round.** A rate-limited Codex, unreadable
    envelope or timed-out fallback records `kind:"attempt"`, excluded from every count, so
    it cannot satisfy `Adversarial rounds:` (which counts passes that reached a VERDICT).
    It still blocks: fail-closed is the exit code, not the record. `(harness-p1, r7)`

- **`ruff format` can rewrite a compliant pytest skip decorator into a BLOCK.** `critic_check`'s
  skip rule is line-based (`reason=` must be on the decorator's line), and ruff re-wraps a long
  inline `reason=` onto its own line — so a passing commit becomes a blocked one the next time
  anyone formats the file. Put the text in a short module constant:
  a decorator whose `reason=` is a short module constant survives both. `(SRS-RESV-005)`
- **Pass EXPLICIT `.py` paths to `ruff format`, never `.`** — the CI mirror runs
  `ruff format --check .`, so it is tempting to fix it with `ruff format .`, which silently
  rewrites `architecture/runtime_services.json` into invalid JSON (CLAUDE.md r8). Format the
  files you touched, by name, then re-run the mirror. `(SRS-RESV-005)`

- **Never edit `tools/critic_check.py` to make your own diff pass.** The judgment critic
  blocks critic-gate self-modification outright (`meta:critic-self-modification`, confidence
  1.0), and it is right to: an agent that can loosen its own gate and then approve itself has
  no gate. When the deterministic critic false-positives on your diff, the fix is upstream of
  the gate — a caption-derived screenshot filename matched `SAFETY_PATH_RE`, and the answer
  was generic filenames with the caption on the record, not a new carve-out.
  `(SRS-RESV-005 r14)`
- **Evidence names the commit it was recorded on, which is the PARENT of the commit that
  carries it.** Two review rounds went on this. Record after the last CODE commit, then land
  the record in a code-free chore commit — the certification still covers everything shipped,
  and no commit message should claim "re-recorded at this HEAD". `(SRS-RESV-005 r12/r13)`
- **Artifacts attached DURING `evidence.py run` used to be dropped by that same run**
  (`_store_step` replaced the step entry wholesale). Fixed, but the shape is worth knowing: a
  record saying `artifacts: []` while the files are on disk is a write-ordering bug, not a
  capture failure. `(SRS-RESV-005)`

