# CLAUDE.md — always-on rules for every session in this repo

Read `AGENTS.md` first: navigation, architecture, hard constraints, the parallel-agent
protocol. This file is only the part that must be true in **every** session, including the
ones running in an `alphalabs-wt-<id>` worktree.

## The playbooks are the project's memory

`docs/playbooks/` holds what past sessions learned the expensive way — each rule is there
because an adversarial reviewer, a live run, or a red `main` found it. Sessions are
fresh-context, so this directory is the only thing that carries forward.

**Read `docs/playbooks/INDEX.md` during orientation and load the 1–3 playbooks whose
trigger matches your feature.** Do not read them all; do not skip the always-on ones.

**Write back.** If review or a live run finds a defect class that is not in a playbook,
add it — same rule format, with provenance — in your chore commit. That is what makes the
next session smarter than this one.

## Rules that cost the most when broken

1. **On a BLOCK, fix the *class*, not the instance.** Before re-requesting review: grep
   every peer call site, surface, and contract block for the same defect; fix them all in
   this round; add a static check or test that fails for the whole class. SRS-LOG-001 spent
   ~20 of its 38 rounds re-finding six recurring classes one call site at a time.
2. **A new code path does not inherit the old one's guarantees.** Adding a second reader,
   a second entry point, or a second transport re-opens every invariant you already fixed
   elsewhere. (LOG-001 r25.)
3. **Unreadable, absent, or unknown is NEVER empty.** `[]`, `0`, `false`, and `ok:true` all
   read as "nothing happened". Missing, corrupt, and unconfigured each need their own
   fail-closed state, distinct from a genuinely empty one.
4. **Never fake a green.** Not an APPROVE, not a `--mode complete`, not a passing harness.
   `serialized` is the honest outcome when a step needs IB/integration/live/e2e — say so
   and record exactly which step.
5. **Verify the artifact, not the intention.** A scripted `.replace()` that matched nothing
   prints nothing and ships nothing; `cargo build | grep '^error'` hides what
   `cargo clippy -- -D warnings` rejects. Prefer the Edit tool (it errors on no-match) and
   re-run the real gate command after the LAST edit.
6. **A test that cannot fail is not evidence.** Mutation-verify every regression test:
   remove the fix, watch the test go red, restore it. A harness that shells another test
   must confirm the inner test *asserts*.
7. **A reviewer TIMEOUT is not a verdict.** A `block` with zero findings is an availability
   failure — retry it. Never shrink the diff with `--base` to make the reviewer finish.
8. **Only pass explicit `.py` paths to `ruff format`.** A JSON file is valid Python syntax;
   ruff will silently rewrite `architecture/runtime_services.json` into invalid JSON.
9. **Never run two `cargo test --workspace` at once.** ~37 fixed-name scratch dirs collide
   and produce phantom failures in crates your diff never touched. Check
   `pgrep -f "cargo test"` first.
10. **Never `git stash` in a worktree.** The stash is repo-global; you will pop a sibling's
    WIP onto your branch. Use `git show origin/main:<path>` or `git diff origin/main...HEAD`.

## Before you commit

- `git status -sb` — the session-start git snapshot goes stale; a concurrent session can
  move the primary checkout onto an `agent/` branch.
- `git diff origin/main...HEAD --stat` — expect **0 deletions** other than a folded
  `progress.d/session-*.md`. Anything else is a stale-base revert of a sibling's work.
- Scope every `git add` to explicit paths. Never `git add -A` here.
