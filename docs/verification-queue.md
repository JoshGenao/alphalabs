# The verification queue — what "awaiting human verification" actually means

`agent_pool.py status` has one bucket for eleven features and calls it *awaiting human
verification*. That bucket is populated by exactly one signal — the first `Outcome:` line
of `progress.d/session-<id>.md` starting with `serialized` (`agent_pool.py:516-527`).
One signal, four unrelated situations. This file is the triage, so a fresh-context session
does not have to re-derive it from eleven notes totalling ~2,000 lines.

**Keep this current.** When a feature leaves the queue, delete its row. When a session
integrates `serialized`, add one — and say which class it is. `scope-and-serialization.md`
rule 6 already requires naming the exact step and blocker; this is where that lands.

## The four classes

| | situation | what unblocks it | where it belongs |
|---|---|---|---|
| **A** | work is done; evidence was never recorded through the gate | an operator session | close it |
| **B** | blocked on an unbuilt **feature** | that feature landing | `agent_pool.py block --on <ids>` |
| **C** | blocked on a **real-world resource no feature owns** | procurement / calendar time | `external_blocker` |
| **D** | blocked by a **dependency cycle** the graph refuses to record | an operator decision on which edge is real | `unblock` the code-edge |

Class **C** is the one that manufactures fake deadlocks. `SRS-REL-001`'s own note says it
plainly: *"No `block`: deps … are on main; **the block is real-operation evidence, not an
unbuilt feature.**"* A dependency edge would be a lie, so nothing was recorded, so it sits
in the awaiting bucket forever and drags `assess_frontier` into `DEADLOCK` with it.

## The board's real shape

Almost everything funnels through three features. `impact_scores()`, today:

```
SRS-NOTIF-001  unblocks 38 ┐
SRS-SDK-004    unblocks 38 ├─→ SRS-PERF-001 (35) ─→ ┬─ SRS-EXE-001 (28) ─→ RESV-001/002 → API-001, LOG-001…
SRS-MD-003     unblocks 37 ┘                        └─ SRS-MD-001  (22) ─→ BT-004 → UI-2, SDK-007, DATA-012…
```

Everything else in the deadlock message is downstream noise. `SRS-REL-001`, `SRS-REL-002`
and `SRS-SIM-004` unblock **0** features each and should never have been in that
conversation.

## The queue

### Class A — closeable now

| feature | unblocks | what is missing | command |
|---|---|---|---|
| **SRS-MD-003** | 37 | All four AC legs passed live on **2026-08-03** and were never recorded. Steps 1–2 are already `executed:true` in `.harness/runs/SRS-MD-003/evidence.json`. Steps 3–4 need a live window; the stored `judgment` verdict is **`block`** at 7 rounds and `evidence.verify` demands `approve`, so a fresh review round is real work, not bookkeeping. | live window (`broker-and-live.md` rules 17–24) → `evidence.py record --step 3/4` → re-review → `close_feature.py SRS-MD-003 --verified --attested-by operator` **from the primary checkout** |
| **SRS-UI-003** | 1 | Its browser test was **written and never executed** — three siblings held leases and `AGENTS.md:73-76` forbids parallel e2e. `scope-and-serialization.md` rule 7 applies: expect the first real run to fail, and **fix the implementation, not the assertion**. | `playwright install chromium && ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_refresh.py::test_account_and_reservoir_panels_render_honest_deferred` (solo — no sibling leases) |

`SRS-UI-003`'s *flip* additionally needs class-B work (below); running the test is what
turns a written-but-unrun e2e into evidence either way.

### Class B — blocked on an unbuilt feature

The owners are already named in each note's `Resume / next` block. Nobody transcribed them
into `tools/feature_deps.json`, which is why `status` flags them `⚠ no dep edges`.

| feature | unblocks | real blockers | note |
|---|---|---|---|
| SRS-DATA-013 | 26 | `SRS-NOTIF-001` | Data layer is complete and solo-verified. Only the dashboard alert pane + email/SMS reason summaries remain. `SRS-UI-001` (the other named owner) **already passes**. |
| SRS-RESV-004 | 16 | `SRS-LOG-001`, `SRS-EXE-006`†, `SRS-NOTIF-001`, `SRS-RESV-005` | **Also class D** — `SRS-LOG-001` is itself blocked on `SRS-RESV-004`. † `SRS-ARCH-005` and `SRS-EXE-006` already pass. |
| SRS-UI-002 | 3 | `SRS-BT-004` | Five deferred field producers were named; `SRS-ORCH-001`, `SRS-ORCH-004`, `SRS-ARCH-004`, `SRS-SIM-003` **all already pass**. Only the P&L feed (`SRS-BT-004` → `SRS-MD-001`) is left. |
| SRS-SAFE-002 | 1 | `SRS-NOTIF-001`, `SRS-API-001` | `SRS-EXE-006` passes, but its `IbConnectionControl` binding needs an operator-gated paper-account re-run (`ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py`) — a solo session cannot lawfully implement it. |
| SRS-SIM-004 | 0 | `SRS-EXE-002` | Mechanism is done and disk-backed. Needs the 60 s timer + a real container restart wired through the ORCH/EXE-002 lifecycle. **Do not rebuild it.** |

### Class C — blocked on a real-world resource no feature owns

These cannot be represented as dependency edges. They are the operator's procurement and
calendar backlog.

| feature | unblocks | what must be obtained |
|---|---|---|
| **SRS-NOTIF-001** | **38** | **An SMS provider account.** `REQUIRED_CHANNELS` is Email **AND** Sms, enforced fail-closed — two email providers cannot close it. Brevo is chosen for email; **no SMS provider has been chosen**. Also needs the `phase1-notification-egress` relay container (not built), which must authenticate its clients. |
| **SRS-PERF-001** | **35** | **A PTP-disciplined host clock.** The AC demands percentiles "measured against a PTP-disciplined host clock" and `LatencyVerificationArtifact::from_samples` fails closed without one. Nothing in the repo provisions or documents PTP. |
| SRS-REL-001 | 0 | **30 rolling market-hours days** of real platform operation, plus a host-liveness feed and an operator outage ledger. The `--assume-full-coverage` and `--target-per-mille` shortcuts were **deliberately deleted** (r2/r6) — this cannot be short-circuited. |
| SRS-REL-002 | 0 | **A real full-stack restart during RTH on the reference Proxmox deployment**, plus the SYS-76 runtime probes (`SRS-MD-006`). A real partial measurement exists (infra span 8.775 s of a 600 s budget) and honestly reports **INCONCLUSIVE**, because the ATP service phases are `cargo test` stubs. |

`SRS-PERF-001` is the hardest gate on the board: `SRS-EXE-001` and `SRS-SDK-004` both need
its artifact, and everything downstream of them waits on PTP.

### Class D — dependency cycles

`agent_pool.py block` drops a cycle-forming edge, writes nothing, prints `✓`, and exits 0
(`pipeline-and-integrate.md` rule 32). So these blockers are **unrecordable** through the
normal path and each needs an explicit operator decision about which edge is real.

| cycle | the question | recommendation |
|---|---|---|
| `SRS-MD-003 → SRS-MD-001 → SRS-PERF-001 → SRS-MD-003` and `SRS-SDK-004 → SRS-EXE-001 → SRS-PERF-001 → SRS-SDK-004` | Does `SRS-PERF-001` need those features' **code** or their **flip**? | **Code.** PERF-001 is a measurement substrate — its own note says *"There is no standalone CLI"* — and all three runtimes' code is on main. Cut `SRS-PERF-001 → {SRS-MD-003, SRS-SDK-004}`; both cycles dissolve at once. |
| `SRS-RESV-004 → SRS-LOG-001 → SRS-RESV-004` | Same question for the log sink. | Same shape: RESV-004 needs LOG-001's **sink code**, LOG-001 needs RESV-004's **record types**. Cut the edge whose consumer only needs code. |

## Why `integrate --force-complete` does not get you out of this

The deadlock message says *"verify + `integrate --force-complete`"*. It does not work, and
following it loops forever.

`--force-complete` overrides **the honesty guard only** — `agent_pool.py:1322-1331` states
this outright. The evidence gate at `:1332-1376` runs regardless and rewrites `mode` back
to `serialized` whenever the record is incomplete. Ten of the eleven queued features have
**no evidence record at all**, and the one that does carries `judgment.verdict: "block"`.

The two real paths to `passes: true` (`AGENTS.md:160-187`):

1. **The tool ran it** — every step through `evidence.py run`, real exit codes, both
   critic layers `approve`; then `integrate --mode complete`.
2. **A named person says so** — `close_feature.py <id> --verified --attested-by operator`,
   or the `verified-e2e` PR label. This admits hand-recorded (`executed:false`) steps —
   but it relaxes *which* steps count, **never whether there is a record**
   (`close_feature.py:225-230`).

Describing the work is not one of them.
