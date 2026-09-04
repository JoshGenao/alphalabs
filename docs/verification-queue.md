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
unbuilt feature.**"* A dependency edge would be a lie, so nothing was recorded, so it sat
in the awaiting bucket forever and dragged `assess_frontier` into `DEADLOCK` with it.
`external_blocker` is where those now go.

## The board's real shape

Two of the three cycle edges were assertions that feature X could not close until feature
Y's `passes` flipped, when what X consumes is Y's **code** — already on main. An edge
meaning *needs the code* and an edge meaning *needs the flip* are not the same edge.
With them cut, `impact_scores()` says something the prose never did:

```
SRS-NOTIF-001  unblocks 56 of 120   deps=[SRS-EXE-006]  ← already passes
   └─→ SRS-PERF-001 (45) ─→ ┬─ SRS-EXE-001 (36) ─→ RESV-001/002 → API-001, LOG-001…
                            └─ SRS-MD-001  (29) ─→ BT-004 → UI-2, SDK-007, DATA-012…
```

**`SRS-NOTIF-001` has no unmet feature dependency at all.** The only thing between it and
green is the operator's LAN ntfy plus the unbuilt egress relay (email only). One deployment step
is worth 56 features. Behind it, `SRS-PERF-001` additionally needs a PTP-disciplined host.

`SRS-MD-003` left the critical path entirely and is now what it always was: a closeable
leaf. `SRS-REL-001`, `SRS-REL-002` and `SRS-SIM-004` unblock **0** features each and
should never have been in the deadlock conversation.

## The queue

### Class A — closeable now

| feature | unblocks | what is missing | command |
|---|---|---|---|
| **SRS-MD-003** | 37 | All four AC legs passed live on **2026-08-03** and were never recorded. Steps 1–2 are already `executed:true` in `.harness/runs/SRS-MD-003/evidence.json`. Steps 3–4 need a live window; the stored `judgment` verdict is **`block`** at 7 rounds and `evidence.verify` demands `approve`, so a fresh review round is real work, not bookkeeping. | live window (`broker-and-live.md` rules 17–24) → `evidence.py record --step 3/4` → re-review → `close_feature.py SRS-MD-003 --verified --attested-by operator` **from the primary checkout** |
| **SRS-MD-005** | 1 | Nothing. All four AC clauses ran solo and are recorded: suspension, suppression, reconnection and the post-window escalation, proven by fault injection against a genuinely dead loopback port (never 4001/4002), plus 8 L5 cases and 15 CLI cases in fresh processes. It sits here rather than closed only because `verification_method` is `integration`, which needs a NAMED human attestation. | `python3 tools/close_feature.py SRS-MD-005 --verified --attested-by operator` **from the primary checkout** |
| **SRS-UI-003** | 1 | Its browser test was **written and never executed** — three siblings held leases and `AGENTS.md:73-76` forbids parallel e2e. `scope-and-serialization.md` rule 7 applies: expect the first real run to fail, and **fix the implementation, not the assertion**. | `playwright install chromium && ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_refresh.py::test_account_and_reservoir_panels_render_honest_deferred` (solo — no sibling leases) |

`SRS-UI-003`'s *flip* additionally needs class-B work (below); running the test is what
turns a written-but-unrun e2e into evidence either way.

### Class B — blocked on an unbuilt feature

The owners are already named in each note's `Resume / next` block. Nobody transcribed them
into `tools/feature_deps.json`, which is why `status` flags them `⚠ no dep edges`.

| feature | unblocks | real blockers | note |
|---|---|---|---|
| SRS-DATA-013 | 26 | `SRS-NOTIF-001` | Data layer is complete and solo-verified. Only the dashboard alert pane + email/push reason summaries remain. `SRS-UI-001` (the other named owner) **already passes**. |
| SRS-RESV-004 | 16 | `SRS-LOG-001`, `SRS-EXE-006`†, `SRS-NOTIF-001`, `SRS-RESV-005` | **Also class D** — `SRS-LOG-001` is itself blocked on `SRS-RESV-004`. † `SRS-ARCH-005` and `SRS-EXE-006` already pass. |
| SRS-UI-002 | 3 | `SRS-BT-004` | Five deferred field producers were named; `SRS-ORCH-001`, `SRS-ORCH-004`, `SRS-ARCH-004`, `SRS-SIM-003` **all already pass**. Only the P&L feed (`SRS-BT-004` → `SRS-MD-001`) is left. |
| SRS-SAFE-002 | 1 | `SRS-NOTIF-001`, `SRS-API-001` | `SRS-EXE-006` passes, but its `IbConnectionControl` binding needs an operator-gated paper-account re-run (`ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py`) — a solo session cannot lawfully implement it. |
| SRS-SIM-004 | 0 | `SRS-EXE-002` | Mechanism is done and disk-backed. Needs the 60 s timer + a real container restart wired through the ORCH/EXE-002 lifecycle. **Do not rebuild it.** |

### Class C — blocked on a real-world resource no feature owns

These cannot be represented as dependency edges. They are the operator's procurement and
calendar backlog.

| feature | unblocks | what must be obtained |
|---|---|---|
| **SRS-NOTIF-001** | **38** | **The operator's self-hosted ntfy on the LAN**, with the phone subscribed over VPN. `REQUIRED_CHANNELS` is Email **AND** Push, enforced fail-closed. Push replaced SMS as IF-11 on 2026-08-17 (US A2P 10DLC lead time + silent carrier filtering) and is verified end to end against a real ntfy, so **no SMS provider is needed**. Brevo is chosen for email; the `phase1-notification-egress` relay container is still unbuilt and is now needed for EMAIL ONLY — push posts directly, no relay hop. |
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

## The watch loop

`tools/verify_queue.py` derives all of the above from `feature_list.json`,
`tools/feature_deps.json`, `.harness/runs/` and the session notes — so it is never
staler than the board.

```
tools/verify_queue.py list              # the ranked worklist
tools/verify_queue.py show <FID>        # AC, evidence gaps, discovered tests, Resume/next
tools/verify_queue.py check             # DRIFT ONLY; exit 1 when something needs a decision
tools/verify_queue.py run <FID> --yes   # execute the machine-runnable steps into the record
```

`check` reports only what **changed or is inconsistent** — a dependency cycle, a
feature held off the frontier by a note alone with nothing recording why, an evidence
record invalidated by a re-spec. Steady state prints nothing and exits 0. A watcher
that prints the whole board every run is a watcher whose output stops being read, and
then the one run that mattered scrolls past with the rest.

**Unattended:** `.github/workflows/verification-watch.yml` runs it daily at 06:30 UTC
*and on every push to main that touches `feature_list.json`, `tools/feature_deps.json`
or `.harness/runs/`* — because closing one feature is exactly what unblocks the next,
so "your last blocker just cleared" should arrive in minutes, not tomorrow. It opens or
updates **one** issue, and closes that issue when the board goes clean. It changes
nothing else.

**Interactive:** `/loop 30m tools/verify_queue.py check` in a Claude Code session, or
just run it before claiming.

### What `run` will and will not do

It executes step 1 (`./init.sh`, identical for every feature) and a discovered test
selection for step 2, through `evidence.py run`, so the record holds real exit codes.

It **stops** at steps 3 and 4 — the acceptance criterion and the evidence instruction.
Those are a judgement and a live observation, and it leaves them **unrecorded**. It
will never write a record for a step it did not execute: a hand-written record the
tool invented would satisfy the human-attestation path with nobody's attestation.

`--yes` is required, because the step-2 selection is grep-derived and fallible —
SRS-MD-003's matches include two harness tests that merely name it as an example.
Read the printed plan, or pass `--only <paths>`.

## verification_method — reviewed 2026-08-12

The field decides whether a feature can ever close as `complete` without a human
attestation. It had been set from a machine proposal nobody corrected, derived from
templated prose — three of every four `steps[]` entries are boilerplate — which
produced **solo 9 / non-solo 111** and made `--force-complete` routine.

Re-derived from the two real artifacts, then reviewed row by row:

| | before | after |
|---|---:|---:|
| solo | 9 | **39** |
| integration | 47 | 35 |
| e2e | 49 | 32 |
| live-ib | 15 | **14** |

The derivation now reads the **acceptance criterion** (`steps[2]` — the only
feature-specific prose) and the **pytest markers on the feature's own tests**. They
answer different questions and disagree legitimately: SRS-MD-003's tests are all
fixtures while its AC names a real gateway. When they disagree the AC wins, because
that is what `passes: true` asserts, and the row is flagged.

**The judgement that recurs: a resource NAMED is not a resource NEEDED.** A third of
the ACs that mention IB mention it to require the system *not* touch it —
*"paper strategy orders **never** create IB orders"*, *"Jupyter **cannot** submit
live orders"*, *"independent of IB account positions"* — or to name it as a data
source shared with live trading, or to exclude it from a measurement window. Those
are proven by showing nothing reached the gateway, which needs no gateway. Reading
them as `live-ib` is the exact inverse of the old ` ib ` keyword scan, and it spends
the scarcest resource on the board.

23 rows where no text rule gets it right without breaking a different row live in
**`tools/verification_method_overrides.json`**, each with the sentence of AC that
decided it. `propose` regenerates `.harness/verification-method-review.txt` from
scratch every run — method column and `# why` comment alike — so a hand edit there
survives only until the next `--rederive`, and its reason survives not at all. The
review file is a worksheet; the override file is the record, and it is validated:
a bad method or a missing reason is refused.

```
tools/classify_verification.py propose --rederive --from-tests   # re-derive
tools/classify_verification.py apply                             # write it back
tools/classify_verification.py status
```

**13 not-yet-passing features are now solo-closable** — they can reach
`passes: true` through `integrate --mode complete` on evidence alone, with no
operator window: ERR-7, SRS-API-001, SRS-BT-007, SRS-BT-008, SRS-EXE-002,
SRS-MD-002, SRS-MD-004, SRS-SAFE-003, SRS-SDK-007, SRS-SDK-008, UI-3, UI-4, UI-5.

## Closure artifacts — what you review on GitHub

A captured exit code proves a command ran. It cannot show you that the dashboard
displayed the stale row, and *"the dashboard shows IB equity, daily and cumulative
P&L, margin usage"* is an acceptance criterion about exactly that.

Every record renders to **`.harness/runs/<FID>/EVIDENCE.md`** — the acceptance
criterion, each step's command and captured output, and the screenshots **inline**.
GitHub displays it directly from the PR's file tree.

```
tools/evidence.py artifact <FID> --step 3 --file shot.png --caption "what it shows"
tools/evidence.py render   <FID>          # regenerate; run/record do it automatically
```

Browser tests get it for free — `tests/e2e/capture.py`:

```python
with evidence_browser(sync_api, "SRS-UI-003", step=3) as cap:
    page = cap.page(url)
    assert page.locator("#account-equity").is_visible()
    cap.shot(page, "account panel with live equity")
```

Screenshots *and* video land in `.harness/runs/<FID>/artifacts/` and attach to the
step on context close. Capture is off unless `ATP_CAPTURE_EVIDENCE=1`; CI sets it on
the L6 e2e job and also uploads the results as a downloadable run artifact.

**The gate.** For `e2e` and `live-ib` features, `evidence.verify` refuses a record
with no image on the acceptance-criterion step, so those features cannot close
without one. `solo` and `integration` features are not gated this way — their
captured stdout *is* the artifact, and demanding a screenshot of
`cargo fmt --check` only teaches everyone to produce a meaningless one.

**Two constraints worth knowing.** GitHub renders PNG/JPG inline from a repo path
but will **not** play `.webm`/`.mp4` from one — inline playback works only for files
uploaded into a comment. So video is *linked*, and `EVIDENCE.md` says why rather
than leaving you clicking a dead player. And this repo has **no git-lfs**, so every
artifact byte is permanent history for every clone and every worktree: 2 MB an
image, 8 MB a video, 20 MB a feature, refused above that rather than truncated.

Artifacts retire with the record on close. A reopened feature must not start with
the previous session's screenshot sitting where its own evidence belongs — a stale
screenshot is *more* convincing than a stale exit code, not less.

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
