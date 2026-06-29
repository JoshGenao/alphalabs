# Coding Agent Prompt

You are continuing work on a long-running autonomous development task.
This is a **fresh context window** — you have no memory of previous sessions.
Your job is to implement exactly one feature, verify it end-to-end, commit,
and leave the environment clean for the next session.

You may be **one of several agents running in parallel**, each in its own git
worktree, on its own branch, working on its own assigned feature. The rules
below keep your work fully isolated from the other agents — never edit a shared
file (`feature_list.json`, `progress.txt`), never run a test that binds a shared
port or the live broker, and never merge your own branch. Follow them exactly.

---

## Step 0 — Confirm your assignment and worktree (do this FIRST)

You are assigned **exactly one** feature by the orchestrator, not by your own
selection. Confirm the handoff before doing anything else:

```bash
# 1. Your feature is pre-assigned via this env var.
echo "${ATP_FEATURE_ID:?}"

# 2. Confirm you are inside your dedicated worktree, not the shared checkout.
git rev-parse --show-toplevel   # must end in  alphalabs-wt-<ATP_FEATURE_ID>
git branch --show-current       # must be      agent/<ATP_FEATURE_ID>
```

- If `ATP_FEATURE_ID` is **unset**, STOP. Tell the operator to launch you via
  `tools/spawn_agents.sh` (which creates the worktree, branch, and assignment),
  or to export `ATP_FEATURE_ID` for a deliberate solo run. Do **not** fall back
  to picking a feature yourself — parallel agents would all pick the same one.
- If you are **not** inside `alphalabs-wt-<ATP_FEATURE_ID>` on branch
  `agent/<ATP_FEATURE_ID>`, STOP. Do not work in the shared checkout; you would
  collide with sibling agents.
- Your feature is the `feature_list.json` entry whose `id == $ATP_FEATURE_ID`.
  Do not read, touch, or reason about any other feature.

---

## Step 1 — Orient yourself (do this before touching anything)

Run these commands in order:

```bash
# 1. Confirm location
pwd

# 2. Understand the project structure
ls -la

# 3. Read the navigation index
cat AGENTS.md

# 4. Read the session handoff. progress.txt is the archived/folded log; the
#    living per-session notes are in progress.d/ (one file per session).
cat progress.txt
ls -t progress.d/ | head -20        # most recent sessions first
#    Then read the few most relevant — any prior session on YOUR feature
#    (progress.d/*<ATP_FEATURE_ID>*) or an adjacent SRS area.

# 5. Check recent git history
git log --oneline -20

# 6. Count remaining work (context only — your feature is already assigned)
cat feature_list.json | grep '"passes": false' | wc -l

# 7. Re-read YOUR assigned feature's full intent (srs_ref + steps).
cat feature_list.json | python3 -c "
import json, os, sys
fid = os.environ['ATP_FEATURE_ID']
features = json.load(sys.stdin)
f = next(f for f in features if f['id'] == fid)
print(json.dumps(f, indent=2))
"
```

Do not skip this step. Your first message must summarise what you found:
your assigned feature, the relevant prior session notes, and what you plan to do.

---

## Step 2 — Start the environment

```bash
./init.sh
```

Wait for the `✓ Environment ready` message before proceeding.

**Worktree isolation (automatic).** `init.sh` derives every path from its own
location, so running it *inside your worktree* creates a worktree-local `.venv`,
`target/`, `data/ssd`, `data/nas`, and `.devserver.*` — you do not share build
or data state with sibling agents. The launcher also exported a private port
block for you (`ATP_DEV_PORT`, `ATP_IB_LIVE_PORT`, `ATP_IB_PAPER_PORT`) so your
dev server does not collide with theirs. **Do not override these ports.**

If the environment is broken (server won't start, tests fail), **fix that
first** before implementing anything new. Building on a broken foundation
makes the problem worse. Commit the fix before moving on (to your branch — see
Step 7; a broken-foundation fix is in-scope for your feature's branch).

---

## Step 3 — Read the requirements (if you haven't already this session)

The docs folder contains the full requirement chain. Read them if any
feature you're about to implement references them:

```bash
cat docs/StRS_v0.7.md   # stakeholder intent — understand WHY
cat docs/SyRS_v0.7.md   # system constraints — understand WHAT is in/out of scope
cat docs/SRS.md    # software spec — understand HOW the feature should work
```

Cross-reference the feature's `"srs_ref"` field against `docs/SRS.md` to
understand the full intent before writing a single line of code.

---

## Step 4 — Confirm your assigned feature

Your feature is **already chosen for you**: it is the `feature_list.json` entry
whose `id == $ATP_FEATURE_ID` (the orchestrator guarantees no two parallel
agents share a feature). Do **not** run any "highest-priority failing" selection
— that would make parallel agents converge on the same feature.

Rules:
- Work on **only** your assigned feature. Do not touch any other feature entry.
- Re-read its `srs_ref` and `steps` (Step 1 command #7) before writing code.
- If your assigned feature is marked `"needs_clarification": true`, STOP: do not
  implement it. Record the blocker in your `progress.d/` note (Step 8) and the
  PR body (Step 7.5), then end the session without a feature change.

---

## Step 5 — Implement

Write the code. As you work:

- Follow the architecture described in `AGENTS.md`.
- If the SRS specifies a module boundary or interface contract, respect it.
- Do not introduce new dependencies without checking `docs/SyRS_v0.7.md` to confirm
  they are in scope.
- Keep changes **atomic and focused** on the selected feature.
- Do not refactor unrelated code in the same commit.
- Touch only files within your feature's scope. **Never** edit the shared
  coordination files `feature_list.json` (see Step 6) or `progress.txt` (see
  Step 8) — sibling agents would clobber them. Your session writes exactly one
  new file under `progress.d/`.
- Utilize ~/.codex/skills/ddia_reference skills for architecture insight

---

## Step 5.5 — Write tests for the right layer

Every feature lands with at least one test. Pick the layer from the bug-class
table — do not put a test in the wrong layer just because it's faster to
write.

| Layer | Directory | Use when... |
|---|---|---|
| L1 unit | `tests/unit/` | Pure-function logic, no I/O |
| L2 property | `tests/property/` | Invariants over generated inputs (use Hypothesis) |
| L3 contract | `tests/` (existing) | API/interface drift between Python and Rust |
| L4 boundary | `tests/boundary/` | Wiring with stub adapters |
| L5 integration | `tests/integration/` | Real containers / I/O — gated by `ATP_RUN_INTEGRATION=1` |
| L6 e2e | `tests/e2e/` | Playwright / WebSocket round-trip |
| L7 domain | `tests/domain/` | Trading-system-specific safety/invariant |

**Hard rule:** if the feature is `safety_critical: true` in `feature_list.json`
(or touches `kill_switch`, `connectivity`, `stale_data`, `live_mode`, or
`safety` paths), the same commit MUST include a `tests/domain/` test. The
deterministic critic will block the commit otherwise.

**Parallel-safety — which tests you may run.** While sibling agents are active,
run only tests that bind no shared resource:

```bash
pytest -m "not integration and not e2e"
cargo test --workspace
```

Do **not** set `ATP_RUN_INTEGRATION=1`, and do **not** run anything that touches
the IB gateway ports (4001/4002), `docker-compose`, or the dashboard (8080) /
Jupyter (8888) stack. Those bind fixed, shared resources and would collide with
other agents — and live/paper IB use violates the hard invariant *"exactly one
strategy against the IB live account at a time."* If verifying your feature
genuinely **requires** an integration/e2e/live test, STOP: do not run it in
parallel. Note it in your `progress.d/` file and the PR body, and flag it for
serialized verification by the operator after merge.

---

## Step 6 — Verify end-to-end

Walk through every step in the feature's `"steps"` array exactly as written.
Use the same tools a real user would — browser automation, API calls, or
whatever the step specifies.

**A feature only counts as passing if every step passes.** Partial passes are
failures.

**Do NOT edit `feature_list.json`.** Your branch leaves it byte-for-byte
unchanged. Instead, **produce a per-step verification record**: map every entry
of the feature's `steps[]` to PASS or FAIL with the exact command you ran and
the observed output. Put this table in your `progress.d/` note (Step 8) and the
PR body (Step 7.5). If any step is not verifiable end-to-end, it is a FAIL.

**A merge does NOT mark the feature passing.** The `passes: false → true` flip
is gated on human verification: the `close-feature` workflow runs
`tools/close_feature.py $ATP_FEATURE_ID --verified` (flip + fold into
`progress.txt`) **only when a reviewer adds the `verified-e2e` label** to your
PR — which they do only after confirming every step end-to-end. Merging without
the label integrates your code but keeps `passes:false`. Do not add the label
yourself; request it in the PR only when the record above is honestly all-PASS.

**Every step must pass. Partial passes are failures.** Do not present a pass
(and do not request the `verified-e2e` label) if:
- You only tested with a unit test or a `curl` command
- The feature works in isolation but not end-to-end
- You are not confident it would pass if a human ran the steps manually

---

## Step 6.5 — Run the Critic Agent

Two passes — both must approve before you commit. Run **both passes from inside
your worktree** — the deterministic critic and `/codex:adversarial-review` both
diff the current working tree's staged changes (cwd determines the diff), so
each agent reviews only its own work. This is safe to run while sibling agents
run their own reviews concurrently.

### Pass 1 — deterministic (always)

```bash
git add <your changes>
python3 tools/critic_check.py --staged --format json > .critic_report.json
python3 tools/critic_check.py --staged --format text   # human-readable copy
```

If the verdict is `block`, fix the violation and re-run. Do **not** set
`ATP_CRITIC_BYPASS=1` — that flag is for humans only and shows up in shell
history. Do **not** use `--no-verify`.

### Pass 2 — judgment (fresh context via `/codex:adversarial-review`)

Run the judgment layer in a **fresh Codex context** — **do not** review in
the same window where you implemented the change (the celesteanders
best-practices doc warns: *"agents consistently rate their own work too
generously"*).

Do not skip this step. If you can't invoke this. Pause and let me know. Give me options on what to do. 

Invoke from inside Claude Code:

```
/codex:adversarial-review --wait $(cat prompts/critic_prompt.md)
```

- `prompts/critic_prompt.md` is the authoritative judgment-layer
  instructions; pass its contents to Codex as the focus text so the
  repo-specific criteria (architectural intent, IB race conditions,
  kill-switch ordering, money math, single-live-strategy invariant, etc.)
  apply.
- `--wait` runs in the foreground so the JSON verdict comes back in this
  session. Use `--background` for large diffs and follow up with
  `/codex:status` / `/codex:result`.
- Codex sees the staged diff via its own `git` access — you do not need to
  paste it.

**Fallback if Codex is unavailable** (`/codex:setup` reports not ready,
auth failing, etc.): open `prompts/critic_prompt.md` in any fresh LLM
context (new Claude Code sub-agent, new ChatGPT tab, etc.) and paste the
prompt + `git diff --cached`. Same JSON schema; same approve/warn/block
rules.

The judgment pass returns the same JSON schema as Pass 1. Save both
verdicts.

### Recording the verdicts

Record both verdicts under a `Critic verdicts:` block in your per-session note
`progress.d/session-$ATP_FEATURE_ID.md` (Step 8), and echo them into the PR body
(Step 7.5). Example:

```
Critic verdicts:
  deterministic: APPROVE — 0 findings
  judgment (/codex:adversarial-review): APPROVE — 0 findings
```

Commit only when both verdicts are `approve`. A `warn` requires a written
override sentence next to the verdict explaining why it is acceptable.
Anything `block` halts the session — fix the issue.

---

## Step 7 — Commit (to your branch)

You are already on your `agent/$ATP_FEATURE_ID` branch (the worktree was created
on it). Commit there — never to `main`, never to another agent's branch.

The environment must be in a **clean state** before you commit:
- No broken tests
- No half-implemented features
- No debug code or temporary files
- No uncommitted changes unrelated to this feature

Keep the **3-commit cadence** (prep → feat → chore):
- **prep** (optional, `chore(...)`): only if your feature needs a new shared
  rule — e.g. extending `SAFETY_PATH_RE` in `tools/critic_check.py`. Skip if not
  needed. (Note: shared-infra edits are the one place parallel branches can
  still conflict at merge; keep them minimal.)
- **feat**: the implementation + its tests. This commit **must NOT modify
  `feature_list.json`** — the `passes` flip happens at merge (Step 6).
- **chore**: writes your `progress.d/session-$ATP_FEATURE_ID.md` note (Step 8).

```bash
git add <feature + test files>
git commit -m "feat($ATP_FEATURE_ID): [brief description of what was implemented]

- Implemented: [what you built]
- Verified: [how you tested it — exact commands]
- Closes $ATP_FEATURE_ID (flip feature_list.json passes on merge via
  tools/close_feature.py $ATP_FEATURE_ID)"
```

---

## Step 7.5 — Open a PR (do NOT merge)

Your branch goes back to `main` through a human-reviewed PR — never merge it
yourself, and never remove your own worktree.

```bash
git push -u origin "agent/$ATP_FEATURE_ID"
gh pr create --base main \
  --title "feat($ATP_FEATURE_ID): [brief description] — close $ATP_FEATURE_ID" \
  --body "$(cat <<'EOF'
## Summary
- [what you built]

## Step verification (every step must PASS end-to-end)
- Step 1: PASS — [exact command] → [observed result]
- Step 2: PASS — [exact command] → [observed result]
- ...
(Any step not verifiable end-to-end = FAIL. If anything is FAIL, do not request
the verified-e2e label.)

## Critic verdicts
- deterministic: APPROVE — [findings]
- judgment (/codex:adversarial-review): APPROVE — [findings]

## How this gets marked passing
Merging integrates the code but does NOT flip feature_list.json. A reviewer
adds the `verified-e2e` label ONLY after confirming every step above passes
end-to-end. That triggers close-feature.yml to flip passes:true and fold this
note into progress.txt on main. Merging without the label keeps passes:false.
EOF
)"
```

Report the PR URL as your final output. Leave the worktree in place — the
operator removes it after merge (or via `tools/cleanup_agents.sh`).

---

## Step 8 — Write your per-session note

Write **one new file**, `progress.d/session-$ATP_FEATURE_ID.md` (a unique name,
so it can never collide with a sibling agent's note). Do **not** edit the shared
`progress.txt` — it is the archived log, folded in at integration by
`tools/close_feature.py`. Commit this file as your **chore** commit (Step 7).

```
=== SESSION <feature-id> ===
Date: [today's date]
Feature: $ATP_FEATURE_ID — [description]
Branch / PR: agent/$ATP_FEATURE_ID — [PR URL]

What I did:
- [brief summary of implementation]
- [any decisions made and why]

What I tested:
- [how you verified end-to-end — exact commands]

Critic verdicts:
  deterministic: [APPROVE/WARN/BLOCK] — [findings]
  judgment (/codex:adversarial-review): [APPROVE/WARN/BLOCK] — [findings]

Known issues / notes for next agent:
- [anything the next agent should know]
- [any deferred / serialized-verification items]
```

---

## Constraints — never violate these

- **One worktree, one branch, one feature.** You implement only your assigned
  `$ATP_FEATURE_ID`, on branch `agent/$ATP_FEATURE_ID`, inside
  `alphalabs-wt-$ATP_FEATURE_ID`. If it is too large for one session, split it
  and implement the first sub-task.
- **Never touch the shared coordination files.** In a parallel run you must not
  edit `feature_list.json` or `progress.txt` at all. The `passes` flip and the
  progress fold-in happen at merge via `tools/close_feature.py`. Your only
  status artifact is the new file `progress.d/session-$ATP_FEATURE_ID.md`.
- **No premature/self marking.** Never assert `"passes": true` (in your note or
  PR) without full end-to-end verification.
- **No removing tests.** Deleting or weakening existing tests is forbidden.
- **No parallel integration/live tests.** No `ATP_RUN_INTEGRATION=1`, no IB
  ports, no docker-compose/dashboard/Jupyter stack while siblings run. Flag any
  feature that needs them for serialized verification.
- **Never merge your own branch.** Push and open a PR; the operator merges.
- **Leave it mergeable.** Every commit must represent a shippable state. No
  "WIP" commits.
- **Read before writing.** If unsure about scope or intent, re-read
  `docs/SRS.md` before writing code.
- **Never bypass the critic.** `ATP_CRITIC_BYPASS=1` and `--no-verify` are
  forbidden for autonomous agents. If the critic blocks you, fix the
  underlying issue. Only humans bypass.