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
   (see `prompts/coding_prompt.md`, Step 8) and opens a PR.
2. When the operator merges that PR, they run on `main`:

   ```bash
   tools/close_feature.py <feature-id>
   ```

   which flips the feature's `passes` to `true` in `feature_list.json` and
   **folds** this note into `progress.txt` with the next sequential session
   number, then removes the per-session file.

So `progress.txt` remains the canonical, numbered, chronological log; this
directory holds only notes that have not yet been folded in.

Files matching `session-*.md` are working notes; this `README.md` and `.gitkeep`
are permanent.
