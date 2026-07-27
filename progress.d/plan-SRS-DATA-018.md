# PLAN SRS-DATA-018 — scheduled NAS backup + validated recovery (2026-07-27)

Operator-approved 2026-07-27. Third feature of this session (after the UI-5 de-churn and the
UI-6 block).

## Context

`SRS-DATA-018` (P2, `passes:false`, nothing built) requires *"scheduled backup and validated
recovery support for NAS-stored data."* AC: **weekly default backups can export NAS data to an
external target; backup completion validates integrity; documented RPO is no more than 7 days.**
Owning clauses: **SYS-59** (scheduled export of all NAS data to an external target, default
weekly, lowest workload priority) and **SYS-60** (7-day RPO; validate backup integrity on
completion).

No backup code existed anywhere in the repo. This is genuinely unbuilt substrate.

## Reused, not reinvented

- **`store.rs`** — `STORE_FILENAME`, `MAGIC`, the FNV-1a `checksum()` (made `pub(crate)`),
  `MarketDataStore::restore`, `StoreError`, the durable scratch→rename idiom, `StoreLock`.
  Integrity validation re-reads the *exported* bytes through this same fail-closed codec.
- **`tiering.rs`** (DATA-008) — the design precedent: fail-closed validated config, the
  `same_directory` alias idiom, tri-state verdicts that never report a false pass, and the
  `NasSyncStatus` Degraded-vs-Failed split.

## Scope decision (stated, not silently narrowed)

SYS-59's *"backup job shall run at the lowest priority in the workload hierarchy (SYS-57)"* does
**not** mutate `atp_types::WorkloadPriority`. It is a closed 7-variant hierarchy matching SYS-57's
enumerated list with no backup rung, its `rank()` match is deliberately exhaustive (additions break
the build across three orchestrator anchors), **SYS-67 used the identical phrase** for DATA-008's
NAS sync job and `tiering.rs` implements no priority binding at all, and this feature's AC never
mentions priority. Scheduling/arbitration stays `atp-orchestrator`'s (SYS-57/58); the module
exposes `cadence_days()` + `due()` so a scheduler can drive it.

## Built

1. `crates/atp-data/src/backup.rs` — `BackupConfig` (fail-closed: cadence ≤ RPO ceiling, target not
   aliased to / nested in the NAS root), `run_backup`, `run_backup_locked` (export+verify+ledger
   under one `StoreLock`), `verify_archive` (read-only), `restore`, `BackupLedger`, `rpo_report`,
   `due`, `discover_unit_names`, tri-state `BackupVerdict`, `TargetStatus`, `VerificationDepth`,
   `ForeignCodecValidator` seam.
2. `crates/atp-simulation/src/bin/data018_backup_cli.rs` — `status` / `run` / `verify` / `restore`,
   allow-list flags, exit codes as contract.
   **Hosted in `atp-simulation`, not `atp-data`** — a deviation from the approved plan, made during
   adversarial review and recorded here. SYS-59 requires backing up backtest results, and proving
   such a blob restorable needs `BacktestResultStore::restore`, which the lower `atp-data` layer
   must not depend on. The engine stays in `atp-data`; only the composition root moved up, so the
   CLI can inject the real decoder instead of shipping envelope-only evidence. Precedent:
   `data021_paper_corp_action_cli` already lives there.
3. `crates/atp-data/src/lib.rs` — module + re-exports.

## Tests

- Inline `#[cfg(test)]` unit tests in `backup.rs` (config, verdict aggregation, ledger parsing,
  RPO/cadence arithmetic, envelope verification).
- `crates/atp-data/tests/srs_data_018_backup_recovery.rs` — L4 over real on-disk fixtures.
- `tests/domain/test_data018_backup_safety.py` — L7 operator-CLI data-loss invariants.
- `tests/test_data018_backup_store_contract.py` — L3 cross-crate literal drift guard.

No new `tools/` check script was added, so no CI wiring was needed — the contract test runs under
the normal pytest gate.

## Completeness: serialized

The integrity/RPO surface is solo-verifiable with fixture stores and a temp directory standing in
for external media. Step 3's *"export to an external storage target (USB drive, secondary NAS, or
cloud archival bucket)"* on a real weekly schedule is Demonstration/inspection over operator
hardware. Code integrates; `passes` stays **false**.

## Verification

```bash
source .venv/bin/activate        # run_ci_locally.sh dies on system python3 (no numpy)
cargo test -p atp-data && cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings && cargo fmt --check
.venv/bin/pytest tests/domain/test_data018_backup_safety.py \
                 tests/test_data018_backup_store_contract.py -q
.venv/bin/pytest -m "not integration and not e2e" -q
```
Operator walkthrough: `status` (never backed up → non-zero) → `run` → `status` (within RPO) →
`verify` → corrupt a byte → `verify` fails → `restore` → equality.

**Known pre-existing red, not this feature's:** `ruff format --check` wants to reformat
`tests/domain/test_safe003_connectivity_block_cli.py` (arrived in `fa8b837`, owner SRS-SAFE-003).
