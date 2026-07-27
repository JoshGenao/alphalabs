=== SESSION SRS-DATA-018 ===
Date: 2026-07-27
Feature: SRS-DATA-018 — scheduled backup and validated recovery support for NAS-stored data
         (SRS-5.5 SRS-DATA-018; SyRS SYS-59 + SYS-60; StRS C-5, BG-6)
Outcome: serialized (passes stays FALSE — the AC's "export to an external storage target" on a real
         weekly schedule is Demonstration/inspection over operator hardware)

## Context

Third feature of this session, after the UI-5 de-churn and the UI-6 block. Genuinely unbuilt: a
repo-wide `grep -ri backup` before starting hit only docs, SyRS, feature_list.json and an unrelated
`live_state.rs` mention. No backup code existed.

AC (feature_list Step 3): **weekly default backups can export NAS data to an external target;
backup completion validates integrity; documented RPO is no more than 7 days.**

## What I built

- **`crates/atp-data/src/backup.rs`** (new, ~1.9k lines incl. inline tests) — the backup engine.
  `BackupConfig` (fail-closed), `run_backup`, `verify_archive` (read-only), `restore`,
  `BackupLedger`, `rpo_report`, `due`, `discover_unit_names`, tri-state `BackupVerdict`,
  `TargetStatus`, `VerificationDepth`, `ForeignCodecValidator`.
- **`crates/atp-simulation/src/bin/data018_backup_cli.rs`** (new) — the operator surface:
  `status` / `run` / `verify` / `restore`. **Deliberately hosted in `atp-simulation`, not
  `atp-data`** — see "The layering decision" below. Precedent: `data021_paper_corp_action_cli`.
- **`crates/atp-data/src/lib.rs`** — module + re-exports.
- **`crates/atp-data/src/store.rs`** — one-word change: `checksum` made `pub(crate)` so the backup
  verifier re-checks an exported blob with the very function that wrote it instead of keeping a
  second copy of the FNV constants that could drift.

Reused rather than reinvented: `store.rs`'s `MAGIC` + FNV envelope, `MarketDataStore::restore`,
the durable scratch→fsync→rename→parent-fsync idiom, and `tiering.rs`'s (DATA-008) design idioms —
fail-closed validated config, the `same_directory` alias check, tri-state verdicts that never
report a false pass, and the `NasSyncStatus` Degraded-vs-Failed split.

### The design, in one line each

- A cadence longer than the RPO is refused at construction — the schedule and the objective are not
  independent knobs, and a `const _: () = assert!(DEFAULT_CADENCE_DAYS <= RPO_MAX_DAYS)` proves the
  shipped defaults agree at COMPILE time.
- A target that is the NAS root, aliases it, or nests inside it is refused: a copy inside the
  source's failure domain is not a backup. Symlinked *parents* are caught too, via a best-effort
  resolver, because the target legitimately does not exist before the first run.
- An absent external mount is never created — writing a "backup" onto local disk would defeat the
  whole requirement — and is reported `Degraded` (recoverable), not `Failed`.
- Integrity is validated ON COMPLETION by re-reading the EXPORTED bytes through the owning codec,
  and the export must be byte-identical to the source.
- Validation happens BEFORE publish: the replacement is staged and fully verified, then renamed, so
  a failed run can never destroy the last good archive.
- Verdicts are tri-state; an unreachable target or a zero-unit run is `Unverified`, never
  `Verified`. RPO is judged against the units **currently on the NAS**, so a unit that was never
  backed up is a visible breach rather than being invisible to the ledger.

## The layering decision (SYS-59's two hard edges)

**Workload priority — deliberately not implemented here.** SYS-59 also says the backup job runs
"at the lowest priority in the workload hierarchy (SYS-57)". `atp_types::WorkloadPriority` is a
CLOSED 7-variant hierarchy matching SYS-57's enumerated list, with no backup rung, and its `rank()`
match is deliberately exhaustive so an addition breaks the build across three orchestrator anchors
(`orch_3_workload_priority_contract.rs`, `tools/orchestrator_workload_priority_check.py`,
`tests/test_orchestrator_workload_priority_contract.py`). **SYS-67 used the identical phrase** for
DATA-008's NAS sync job and `tiering.rs` implements no priority binding at all. This feature's AC
never mentions priority. So scheduling/arbitration stays `atp-orchestrator`'s (SYS-57/58); the
module exposes `cadence_days()` + `due()` so a scheduler can drive it. Owner for the binding:
**SRS-ORCH / SYS-57-58**.

**Backtest-results codec — closed by moving the composition root, not by weakening the claim.**
SYS-59 lists backtest results as a NAS family to back up, but proving a `backtest_results.store` is
*restorable* needs `atp_simulation::backtest_store::BacktestResultStore::restore`, and `atp-data`
must not depend on `atp-simulation` (that edge inverts the architecture; `atp-simulation` already
depends on `atp-data`). Resolution:
  1. `atp_data::backup` FAILS CLOSED — a backtest unit with no injected validator is `Unverified`,
     never `Verified`, because envelope integrity + byte-identity cannot prove restorability.
  2. `ForeignCodecValidator` is the seam that closes it.
  3. `data018_backup_cli` was **moved to `atp-simulation`**, the composition root that can inject
     the real `BacktestResultStore::restore`. The ENGINE stays in `atp-data`; only the wiring moved.
  4. `crates/atp-simulation/tests/srs_data_018_backup_composition.rs` pins that composition, so a
     refactor that drops the validator fails a test instead of silently reverting the operator
     surface to envelope-only evidence.

## What I tested (per feature step)

- **Step 1** (`./init.sh` reports Environment ready): **PASS** — run with my assigned ports;
  "✓ Environment ready". Note the fresh worktree had no `.venv` and `init.sh` skips
  `requirements-dev.txt`, so `pip install -r requirements-dev.txt` was needed before pytest.
- **Step 2** (exercise via CLI/API workflows with fixtures + persisted-output inspection):
  **PASS (solo)** — full operator walkthrough against real on-disk stores seeded by
  `data008_tier_cli`: `status` before any backup (exit 1, "never … within rpo: NO") → `run`
  (verified, record-level, 2 records) → `status` (within rpo: yes, due: no) → `verify` (verified) →
  flip one byte in the archive → `verify` (corrupt, exit 1) → wipe the archive → `status`
  (archive verifies: unverified, within rpo: NO, due: yes) → `restore` (verified, byte-identical).
- **Step 3** (AC: weekly default export to an external target; integrity validated; RPO ≤ 7 days):
  **PARTIAL → serialized.** Weekly-default cadence, integrity validation and the 7-day RPO are all
  proven solo (below). What is NOT proven solo: the target here is a temp directory standing in for
  real external media. "Export to an external storage target (USB drive, secondary NAS, or cloud
  archival bucket)" on a real weekly schedule is exactly the Demonstration/inspection the SRS names,
  and needs the operator's hardware.
- **Step 4** (record objective evidence; leave passes false): **PASS** — evidence recorded here;
  `passes` stays FALSE by design.

Automated (this session):
- `crates/atp-data/src/backup.rs` inline unit tests — 34 PASS (config refusals, verdict aggregation
  incl. empty-run-is-Unverified, ledger torn-tail + malformed-line, RPO/cadence arithmetic incl. the
  7-days-plus-one-second boundary, envelope verification, kind vocabulary).
- `crates/atp-data/tests/srs_data_018_backup_recovery.rs` — 35 PASS (L4 over real on-disk fixtures:
  each AC clause, plus every adversarial regression listed below).
- `crates/atp-simulation/tests/srs_data_018_backup_composition.rs` — 2 PASS (the real codec wired in
  verifies at record level; an envelope-valid blob the real codec rejects is Corrupt and never
  reaches the archive).
- `tests/domain/test_data018_backup_safety.py` — 21 PASS (L7 operator-CLI data-loss invariants).
- `tests/test_data018_backup_store_contract.py` — 5 PASS (L3 cross-crate literal drift guard: the
  backtest filename/magic duplicated in `backup.rs` are pinned against their owning definitions,
  and the guard asserts `atp-data` still does not depend on `atp-simulation`).
- Full gate: `cargo test --workspace` exit 0; `cargo clippy --workspace --all-targets -D warnings`
  exit 0; `cargo fmt --check` exit 0; `ruff check .` clean;
  `pytest -m "not integration and not e2e"` → **4193 passed, 4 skipped** (the 4 skips are
  pre-existing), up from 4168 on main.

## Critic verdicts

  deterministic (critic_check.py --staged): APPROVE — no findings, every round.

  judgment (adversarial_review.py origin/main, reviewer=codex): **12 rounds.** This is a data-loss
  surface, so the review converged slowly and every finding was real. Recorded in full because the
  pattern is reusable — the whole class is "evidence that is weaker than the claim it supports":
    r1  restore swallowed record-codec errors via `.ok()` and still returned Verified → Corrupt.
    r2  an unreadable NAS subtree was skipped, shrinking the unit set so a partial backup reported
        Verified and advanced the ledger → fail closed on any unreadable subtree.
    r3  the failure-domain guard only canonicalized existing paths, but the target does not exist
        before the first run → best-effort resolver over the deepest existing ancestor.
    r4  a mixed good+corrupt run recorded the good unit, and a ledger-only RPO read then reported
        green → judge the RPO against the units CURRENTLY on the NAS; an unbacked unit is a breach.
        (Codex's own proposal — gate the ledger on whole-run success — does NOT close this: a new
        unit appearing after a green run would still be invisible. Fixed the root cause instead.)
    r5  `--now` defaulted to a frozen constant, so a cron entry that omitted it would see zero
        elapsed time forever and stop backing up → real system clock at the CLI boundary only.
    r6  the not-due path trusted the ledger without re-reading the media → `verify_archive`.
    r7  backtest units were reported Verified on envelope-only evidence → `VerificationDepth`.
    r8  the export published BEFORE the full source/target check, so a codec-invalid source could
        destroy the last good archive → stage, verify, then publish. (My own recorded
        "validate BEFORE persist" rule, which I had violated.)
    r9  unit identity was the directory alone, collapsing a `market_data.store` and a
        `backtest_results.store` in one directory → identity now includes the filename.
    r10 restore's destination could nest inside the archive → mirrored failure-domain guard.
    r11 restore overwrote the destination before validating → same stage-verify-publish ordering.
    r12 status trusted the ledger without re-verifying the archive → both must hold.
    r13 the reachability classifier called `create_dir_all`, able to recreate a vanished mount
        locally mid-run → classify only, never create.
    r14 backtest exports were not compared to the source bytes → byte equality for EVERY kind.
    r15 the RPO floored elapsed time to whole days, so 7d+1s stayed green for nearly a day →
        compare raw seconds.
    r16/r17 the CLI passed `None` for the validator, so the fail-closed rule made a NAS containing
        backtest results permanently unverifiable through the shipped surface → moved the CLI to
        `atp-simulation` and injected the real codec (see "The layering decision").
    r18 a per-unit target SUBDIRECTORY could be a symlink into the NAS (`usb/equities ->
        nas/equities`); the root-level guard passes and the export would publish into the source
        tree, potentially overwriting newer NAS data under concurrent ingestion → resolve each
        unit's own parent and require it under the target root and outside the NAS root.
    r19/r21 SYS-59's scheduler + lowest-workload-priority clause is not implemented. The reviewer's
        own alternative is "keep this feature serialized/partial with `passes` left false" — which
        is exactly how it integrates. Accepted as the resolution, owner named above; the module
        exposes `cadence_days()`/`due()` so the orchestrator can drive it.
    r20 export/verify/ledger-advance were three separate steps, so two concurrent runs could
        interleave — an older run publishing staler bytes over a newer archive while the newer
        ledger timestamp survived, giving a confident within-RPO status for data no longer there →
        `run_backup_locked` does all three under the crate's existing single-writer `StoreLock` on
        the target; a concurrent holder fails closed with `StoreError::Locked`.
    r22 `verify_archive` checked only the EXPECTED units, so a stale/corrupt blob left in the
        archive (one no longer on the NAS) was invisible to `status` — yet `restore` discovers and
        restores every archived unit, so status was blessing an archive whose recovery would fail →
        verify the UNION of expected and present units.
    r23 `is_file()` and `read_to_string` both FOLLOW symlinks, so an archive entry that was really
        a link into the NAS (`usb/equities/market_data.store -> /nas/...`) read and checksummed
        perfectly while holding no bytes of its own — a backup that dies with the thing it was meant
        to survive → `symlink_metadata` guard on both the verify and restore read paths.

  Every finding above has a named regression test.

  **Convergence and its honest limit.** After 15 rounds the reviewer was still surfacing ~1 real
  finding per round — all in one family (evidence weaker than the claim it supports), all fixed.
  The operator was shown the rate and chose to LAND SERIALIZED rather than keep iterating to a
  clean approve. So: r23 was fixed and fully regression-tested, the deterministic critic re-ran
  APPROVE and the whole gate is green, but **r23's fix was not itself put through another
  adversarial round**. Recorded plainly rather than claimed as a converged APPROVE. Anyone
  resuming should assume the same family may still have members; the distilled checklist is the
  place to start looking.

  Two review-process notes worth carrying forward:
    * An unmodified re-run of one round returned approve-with-no-findings before I had changed
      anything. I did not take the convenient verdict — the prose was tightened and re-reviewed.
      Treat a verdict that flips in your favour without a diff change as unresolved.
    * Twice the reviewer's literal recommendation would have been wrong (staging
      `tools/feature_deps.json`, which integrate rejects; and gating the ledger on whole-run
      success, which does not close the hole). The OBSERVATIONS were right both times. Fix the
      root cause the observation points at, not the letter of the recommendation.

## Known pre-existing red on main — NOT this feature's

`ruff format --check` wants to reformat `tests/domain/test_safe003_connectivity_block_cli.py`,
which arrived on main in `fa8b837` (feat(SRS-SAFE-003)). Deliberately not fixed here: formatting a
sibling's file inside a feature branch is the known CI-red-behind-format-gates anti-pattern, and
the SAFE-003 agent held a live lease while this ran. Owner should run
`ruff format tests/domain/test_safe003_connectivity_block_cli.py`.

## Resume / next (the flip path)

`passes:false` is CORRECT. To flip SRS-DATA-018 the operator needs to demonstrate the AC against
**real external media**, which no parallel agent can do:
1. Mount a genuine external target (USB drive, secondary NAS export, or a cloud-archive mount) —
   note the tool REFUSES to create a missing target root, so mount it first.
2. `cargo build -p atp-simulation --bin data018_backup_cli`
3. `data018_backup_cli status --nas $ATP_NAS_DATA_DIR --target <mount>` → expect exit 1
   ("never … within rpo: NO") on a NAS that has never been backed up.
4. `data018_backup_cli run --nas $ATP_NAS_DATA_DIR --target <mount>` → expect exit 0 and one
   `verified` line per NAS unit.
5. Re-run `status` → `within rpo: yes`, `backup due: no`. Leave it a week (or pass `--now`) and
   confirm the cadence re-arms and that the RPO lapses past 7 days.
6. `data018_backup_cli verify --target <mount> --nas $ATP_NAS_DATA_DIR`, then unplug the media and
   re-run `status` → must report `archive verifies: unreadable` and exit non-zero.
7. `data018_backup_cli restore --target <mount> --dest <scratch>` → byte-identical recovery.
Then flip via the `verified-e2e` label. Do NOT re-derive the engine — it is complete; what is
missing is only the hardware demonstration.

Deferred with named owners (neither blocks the AC):
- SYS-59's lowest-workload-priority binding → **atp-orchestrator (SYS-57/58)**.
- A `tools/data018_backup_check.py`-style static check was NOT added; the cross-crate drift guard
  is a pytest contract test instead, so it already runs in the normal gate with no CI wiring.
