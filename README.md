# docs/

This folder is the **single source of truth** for the project's requirements.
All agents read these files at the start of every session. All code must
trace back to a requirement in this folder. Knowledge that lives outside
this folder does not exist to an agent.

---

## Document chain

Requirements flow from stakeholder intent down to developer tickets:

```
docs/StRS.md  →  docs/SyRS.md  →  docs/SRS.md  →  feature_list.json
  (Why)            (What)           (How)          (Agent work queue)
```

| Document | Read it to understand… |
|----------|----------------------|
| `StRS.md` | The stakeholder vision, business goals, and success criteria. Use this when you need to understand *priority* or *intent* behind a requirement. |
| `SyRS.md` | System-level constraints, non-functional requirements, and architectural rules. Use this to understand *scope* and to check if a proposed approach is in bounds. |
| `SRS.md` | Software-level functional requirements, module structure, and acceptance criteria. This is the **primary source for `feature_list.json`**. |

## For agents

- Read all three files before starting any implementation work.
- If a requirement in `SRS.md` conflicts with a constraint in `SyRS.md`,
  the constraint wins — note the conflict in `progress.txt`.
- If a requirement is unclear, check `StRS.md` for the underlying intent.
- Never implement a feature that cannot be traced to a requirement in `SRS.md`.

## For humans

- Keep these documents up to date as requirements change.
- When you add a requirement to `SRS.md`, also add a corresponding entry to
  `feature_list.json` and set `"passes": false`.
- When you remove or change a requirement, update all three files and adjust
  `feature_list.json` accordingly.

## prompts/

The `prompts/` subfolder contains the agent prompt templates:

| File | Used by |
|------|---------|
| `prompts/initializer_prompt.md` | First agent session only — sets up the environment |
| `prompts/coding_prompt.md` | All subsequent sessions — implements features incrementally |
