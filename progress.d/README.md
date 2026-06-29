# progress.d/ — per-session handoff notes

Each parallel agent session writes **one file** here instead of prepending to the
shared `progress.txt`. The filename is keyed to the assigned feature so two
agents running at once can never collide:

```
progress.d/session-<feature-id>.md      e.g. session-SRS-EXE-001.md
```

This exists because `progress.txt` is prepended every session — under parallel
agents that is a guaranteed top-of-file merge conflict. One file per session
sidesteps it entirely.

## Lifecycle

1. An agent writes `progress.d/session-<feature-id>.md` as its **chore** commit
   (see `prompts/coding_prompt.md`, Step 8). It is a **resume handoff** — what's
   done, tested, and what (if anything) is left or blocking.
2. The agent integrates via `tools/agent_pool.py integrate <id> --mode …` (which
   holds the pool lock, rebases on `main`, and pushes):
   - `--mode complete` → runs `tools/close_feature.py <id> --verified`, which
     flips the feature's `passes` to `true` in `feature_list.json` and **folds**
     this note into `progress.txt` (next sequential session number), then removes
     the per-session file.
   - `--mode partial` / `--mode serialized` → the code lands on `main` but
     `passes` stays `false`; **this note stays** as the resume pointer for the
     next session (or for the operator's serialized `verified-e2e` verification).

So `progress.txt` remains the canonical, numbered, chronological log; this
directory holds only resume notes for features not yet `complete`.

Files matching `session-*.md` are working notes; this `README.md` and `.gitkeep`
are permanent.
