# docs/

This folder is the **single source of truth** for the project's requirements.
All code must trace back to a requirement here. Knowledge that lives outside
this folder does not exist to an agent.

*(This content used to sit in the repository root as `README.md`, where it
described "this folder" from one directory up and pointed at `StRS.md` and
`SyRS.md` — neither of which has ever existed. The filenames below are the real
ones.)*

---

## Document chain

Requirements flow from stakeholder intent down to developer tickets:

```
docs/StRS_v0.7.md  →  docs/SyRS_v0.7.md  →  docs/SRS.md  →  feature_list.json
     (Why)                 (What)              (How)        (Agent work queue)
```

| Document | Read it to understand… |
|----------|----------------------|
| `StRS_v0.7.md` | The stakeholder vision, business goals, and success criteria. Use this when you need the *priority* or *intent* behind a requirement. |
| `SyRS_v0.7.md` | System-level constraints, non-functional requirements, and architectural rules. Use this to understand *scope* and to check whether a proposed approach is in bounds. |
| `SRS.md` | Software-level functional requirements, module structure, and acceptance criteria. This is the **primary source for `feature_list.json`**. |

Superseded revisions live in `docs/archive/`. They are history, not truth.

## Also in here

| Path | What it is |
|------|-----------|
| `playbooks/` | What previous sessions learned the expensive way. Routed by `playbooks/INDEX.md` — read the ones whose trigger matches your feature. |
| `DEPLOYMENT.md` | How the system is deployed. |
| `tradestudies/` | Recorded option comparisons behind specific decisions. |
| `plans/` | Long-form plans that outlived a single session. |

## For agents

- Read the requirement your feature traces to (`srs_ref`) before implementing —
  not all three documents end to end.
- If `SRS.md` conflicts with a constraint in `SyRS_v0.7.md`, **the constraint
  wins**; record the conflict in your session note.
- If a requirement is unclear, check `StRS_v0.7.md` for the underlying intent.
- Never implement a feature that cannot be traced to a requirement in `SRS.md`.

## For humans

- Keep these documents up to date as requirements change.
- When you add a requirement to `SRS.md`, add a corresponding entry to
  `feature_list.json` with `"passes": false` and a `verification_method`
  (`solo` / `integration` / `live-ib` / `e2e` — see
  `tools/classify_verification.py`).
- When you remove or change a requirement, update all three documents and adjust
  `feature_list.json` accordingly.
- If a feature is waiting on something **no other feature can supply** — an
  account you have not bought, hardware you do not have, calendar time that has
  not elapsed — give it an `"external_blocker"` naming that thing. It leaves the
  ready frontier and appears under *blocked on an external resource* in
  `agent_pool.py status`, which is the operator's procurement backlog. Do **not**
  use `block --on` for these: a dependency edge asserts a feature owns the
  blocker, and if none does, the edge is a lie that never clears.
  See `docs/verification-queue.md`.

## prompts/

The agent prompt templates live in `prompts/` at the repository root:

| File | Used by |
|------|---------|
| `prompts/coding_prompt.md` | Every coding session — the per-session workflow |
| `prompts/critic_prompt.md` | The judgment-layer critic (Codex or a fresh-context Claude) |
| `prompts/initializer_prompt.md` | First-run scaffolding only; ran once, kept for provenance |
