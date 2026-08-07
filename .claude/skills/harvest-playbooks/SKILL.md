---
name: harvest-playbooks
description: Harvest the lessons out of new agent session notes into docs/playbooks/, then open a PR. Use when a batch of sessions has landed, on a weekly cadence, or when the operator asks to update the playbooks / run the self-improvement loop.
---

# Harvest the playbooks

`docs/playbooks/` is the only thing that carries knowledge between fresh-context agent
sessions. This is the job that keeps it current: read what the last batch of sessions
learned the expensive way, fold the transferable part into the playbooks, and open a PR.

**Land the result as a branch + PR — never push playbook edits straight to `main`.**
Every agent reads these rules, so a wrong rule is worse than a missing one, and the
operator is the review gate.

## 1. Collect

```bash
python3 tools/playbook_harvest.py
```

It reports the notes added or changed since the last harvest marker, their
`Adversarial rounds:` counts, the round bullets (the actual defect classes), any
zero-code churn sessions, and whether the playbook set is still internally coherent.

**If it reports nothing new, stop and say so.** That is the common case and it is not a
failure. Do not manufacture rules to justify the run.

If the report mentions `progress.txt also changed`, a note was folded at close — read
`git diff <marker>..HEAD -- progress.txt` too; the folded text is new material.

## 2. Read the material

For each new note, read the whole thing — the summary lines are not enough. What you are
looking for, in priority order:

1. **A defect class that cost more than one review round**, or that was found at a second
   call site after being fixed at the first. This is the highest-value material in the
   repo; it is what the round counts are made of.
2. **A false green** — a harness, gate, or check that passed over a real failure.
3. **A mechanical trap** that cost real time: a tool that silently no-ops, a guard that
   fires on the wrong thing, a git operation that did something other than advertised.
4. **A live-run finding** that no offline test could have produced.
5. **A rule already in a playbook that turned out to be wrong.** Deleting it is as
   valuable as adding one.

Explicitly NOT material: anything derivable from the repo (what a feature does, what is
built, what blocks a flip — that lives in the session note and `feature_list.json`),
and anything true of exactly one feature and never again.

## 3. Decide, per candidate

For each candidate, grep the playbooks first:

```bash
grep -rin "<the concept>" docs/playbooks/
```

- **Already covered, and the rule held** → nothing to do. Note it; a rule that keeps
  proving itself is evidence the loop works.
- **Already covered but the session hit it anyway** → the rule is in the wrong playbook,
  is buried, or is worded too weakly. Fix the placement or the wording; do not add a
  second copy. Two rules for one defect is how the set rots.
- **Not covered** → add it, in the existing format: **the rule** — why it matters —
  `(<feature> rN)`. Put it in the playbook whose trigger matches, and only create a new
  playbook if three or more rules have no home.
- **Contradicts an existing rule** → the newer evidence usually wins, but say so
  explicitly in the PR body. Never leave both.

Keep each playbook under the line budget the script enforces. When one is full, that
means prune: the weakest rules are the ones with no provenance, the ones no session has
hit twice, and the ones now enforced by a check in CI.

## 4. Check the loop is actually working

The script prints this batch's round counts against the pre-playbook baseline. Put the
comparison in the PR body — it is the only falsifiable claim this whole system makes.

- If round counts are NOT coming down, say so plainly. Then look at what the reviewer
  found: if a finding was already a written rule, the delivery failed (the agent did not
  read it), not the rule. That is a `prompts/coding_prompt.md` problem, not a playbook
  problem — and worth raising as such.
- If a note has no `Adversarial rounds:` line, the agent skipped the template. Flag the
  feature id in the PR body; do not guess a number.

## 5. Land it

```bash
git checkout -b chore/playbook-harvest-<YYYY-MM-DD>
# ... edits ...
python3 tools/playbook_harvest.py --set-marker HEAD    # after committing the edits
python3 tools/playbook_harvest.py                      # integrity must be clean
python3 tools/critic_check.py --staged --format text   # must APPROVE
git commit -m "chore(playbooks): harvest <N> sessions — <the classes added>"
gh pr create --title "..." --body "..."
```

Commit the edits first, then set the marker and amend it in, so the marker always names
the commit that contains the work it accounts for.

**The PR body must contain**, in this order:

1. Which notes were harvested (feature ids), and the round-count comparison.
2. Each rule ADDED — the rule, and the specific finding that produced it, with a link or
   quote from the note. A rule with no cited evidence should not be in the PR.
3. Each rule CHANGED or DELETED, and why.
4. Anything you deliberately did NOT add, and why — "this was specific to one feature",
   "already covered by <playbook> rule N". This is what stops the set bloating.
5. Any zero-code churn sessions the script flagged. Those are not playbook material;
   they are a scheduler problem for the operator, so surface them and move on.

## Guardrails

- **Never edit a playbook rule you cannot trace to evidence in a note.** Provenance tags
  are what let a future session judge whether a rule still applies.
- **Never delete a rule just because it is old.** Delete it because it is wrong, because
  a CI check now enforces it, or because it duplicates another rule.
- **Do not touch `feature_list.json`, `progress.txt`, or `progress.d/`.** They are
  integrator-owned; a branch that modifies them is refused.
- **Do not push to `main`.** Branch and PR, always.
- If the integrity check fails, fix that first — a stale INDEX means agents are not
  reading a playbook that exists.
