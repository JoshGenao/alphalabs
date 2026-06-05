# Coding Agent Prompt

You are continuing work on a long-running autonomous development task.
This is a **fresh context window** — you have no memory of previous sessions.
Your job is to implement exactly one feature, verify it end-to-end, commit,
and leave the environment clean for the next session.

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

# 4. Read the session handoff
cat progress.txt

# 5. Check recent git history
git log --oneline -20

# 6. Count remaining work
cat feature_list.json | grep '"passes": false' | wc -l

# 7. See which features are still failing
cat feature_list.json | python3 -c "
import json, sys
features = json.load(sys.stdin)
failing = [f for f in features if not f['passes']]
for f in sorted(failing, key=lambda x: x['priority'])[:10]:
    print(f[\"id\"], f[\"priority\"], f[\"description\"])
"
```

Do not skip this step. Your first message must summarise what you found:
current feature count, last session's work, and what you plan to do.

---

## Step 2 — Start the environment

```bash
./init.sh
```

Wait for the `✓ Environment ready` message before proceeding.

If the environment is broken (server won't start, tests fail), **fix that
first** before implementing anything new. Building on a broken foundation
makes the problem worse. Commit the fix before moving on.

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

## Step 4 — Pick one feature

Select the **single highest-priority failing feature** from `feature_list.json`.

Rules:
- Work on **one feature at a time**. Do not start a second until the first is
  verified and committed.
- P1 before P2 before P3.
- If two features at the same priority level exist, pick the one with the
  lower ID number.
- If a feature is marked `"needs_clarification": true`, skip it and note it
  in `progress.txt`.

---

## Step 5 — Implement

Write the code. As you work:

- Follow the architecture described in `AGENTS.md`.
- If the SRS specifies a module boundary or interface contract, respect it.
- Do not introduce new dependencies without checking `docs/SyRS_v0.7.md` to confirm
  they are in scope.
- Keep changes **atomic and focused** on the selected feature.
- Do not refactor unrelated code in the same commit.
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

---

## Step 6 — Verify end-to-end

Walk through every step in the feature's `"steps"` array exactly as written.
Use the same tools a real user would — browser automation, API calls, or
whatever the step specifies.

**Only mark a feature as passing if every step passes.** Partial passes are
failures.

To update the feature list:

```python
import json

with open('feature_list.json') as f:
    features = json.load(f)

for feature in features:
    if feature['id'] == 'F-XXX':   # replace with actual ID
        feature['passes'] = True
        break

with open('feature_list.json', 'w') as f:
    json.dump(features, f, indent=2)
```

**Do not mark as passing if:**
- You only tested with a unit test or a `curl` command
- The feature works in isolation but not end-to-end
- You are not confident it would pass if a human ran the steps manually

---

## Step 6.5 — Run the Critic Agent

Two passes — both must approve before you commit.

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

Append both verdicts to `progress.txt` under a `Critic verdicts:` block in
this session's entry. Example:

```
Critic verdicts:
  deterministic: APPROVE — 0 findings
  judgment (/codex:adversarial-review): APPROVE — 0 findings
```

Commit only when both verdicts are `approve`. A `warn` requires a written
override sentence next to the verdict explaining why it is acceptable.
Anything `block` halts the session — fix the issue.

---

## Step 7 — Commit

The environment must be in a **clean state** before you commit:
- No broken tests
- No half-implemented features
- No debug code or temporary files
- No uncommitted changes unrelated to this feature

```bash
git add .
git commit -m "feat(F-XXX): [brief description of what was implemented]

- Implemented: [what you built]
- Verified: [how you tested it]
- Remaining: [N] features still failing

Next session: run ./init.sh, then pick next P1 from feature_list.json"
```

---

## Step 8 — Update `progress.txt`

Append a new entry at the top of the file (most recent first):

```
=== SESSION [N] ===
Date: [today's date]
Feature completed: F-XXX — [description]
Status: [N remaining features]

What I did:
- [brief summary of implementation]
- [any decisions made and why]

What I tested:
- [how you verified end-to-end]

Known issues / notes for next agent:
- [anything the next agent should know]
- [any features skipped and why]
```

---

## Constraints — never violate these

- **One feature per session.** If a feature is too large for one session,
  split it into sub-tasks and implement the first sub-task.
- **No premature marking.** It is never acceptable to mark a feature as
  `"passes": true` without full end-to-end verification.
- **No removing tests.** It is unacceptable to delete or modify a feature
  entry in `feature_list.json` except to flip `"passes"` to `true` or add
  a `"notes"` field.
- **Leave it mergeable.** Every commit must represent a state where the main
  branch could be shipped. No "WIP" commits.
- **Read before writing.** If you are unsure about scope or intent, re-read
  `docs/SRS.md` before writing code.
- **Never bypass the critic.** `ATP_CRITIC_BYPASS=1` and `--no-verify` are
  forbidden for autonomous agents. If the critic blocks you, fix the
  underlying issue. Only humans bypass.