# SRS-DATA-015 — schema evolution for stored data entities

Approved plan (operator, 2026-07-27). Persisted here as the durable artifact + resume aid.

## Requirement

SRS-DATA-015 (SyRS SYS-66; SRS-5.5; StRS SN-1.26 / SN-1.27 / C-5; P2, verification "Test,
inspection"):

> **AC:** Each persisted entity records a schema version; data written under older schema versions
> remains queryable after schema updates without bulk migration.

SYS-66: *"The data layer shall support schema evolution such that data ingested under a prior schema
version remains queryable after schema updates, without requiring bulk migration of historical
records. Schema version shall be tracked per data entity."*

## Session provenance

The launcher first claimed **SRS-DATA-010**, which is already built and integrated (serialized,
2026-07-26) and whose remaining work is owned by unbuilt features. It was re-offered because the
primary checkout `~/Documents/Programming/Python/alphalabs` is 12 commits behind `origin/main` and
dirty (17 paths), so `_sync_primary_checkout()` bails (`tools/agent_pool.py:254-262`) and
`serialized_notes()` never sees `session-SRS-DATA-010.md`. De-churned via
`agent_pool.py block SRS-DATA-010 --on SRS-EXE-001 SRS-FAC-001 SRS-BT-001 SRS-NOTIF-001` (its real
deferred owners, per its session note), released, then re-claimed → the scheduler handed over
SRS-DATA-015 legitimately. **Operator action still required:** resolve the 17 dirty ROOT paths so
the scheduler stops re-offering serialized features (14 are awaiting verification).

## What already exists (do NOT rebuild)

All three data tiers — SSD primary, NAS archive, cold-read cache — share **one** codec,
`MarketDataStore::serialize`/`restore` (`crates/atp-data/src/store.rs`), which already implements
real evolution: `MAGIC` + an explicit `schema_version` line, v1→v4, `MIN_SUPPORTED_SCHEMA_VERSION =
1`, per-`DatasetKind` `min_schema_version()` (`store.rs:236`), serialize writes the *minimum* version
its contained kinds require (`store.rs:747`), and a forward-compat guard rejecting a kind newer than
the declared version (`store.rs:807`). `crates/atp-simulation/src/paper_state.rs` demonstrates true
v1→v2 **migration-on-read** — the exemplar.

Already versioned: `backtest_store` v1 (SRS-BT-009), `atp-notification/store` v1 (SRS-NOTIF-001),
`live_state` v1 (SRS-EXE-005), `outbox` v1 (SRS-EXE-009), `determinism` digest+manifest v1
(SRS-BT-010), `atp_config/vault` envelope v1 (SRS-SEC-001).

## The gap — four entities record no version, and nothing enforces the claim

| Entity | Writer |
|---|---|
| Access journal | `crates/atp-data/src/access_journal.rs` (bare TSV) |
| Kill-switch last-activation | `python/atp_safety/state.py:53` (bare `json.dumps`) |
| Hot-swap trigger event log | `crates/atp-orchestrator/src/bin/resv003_hot_swap_trigger_cli.rs:519` |
| JSONL log/audit segments | `python/atp_logging/persistence.py:262` |

Scope (operator-chosen): **repo-wide totality** — every persisted entity records a version, gate-enforced.

## Design decisions

1. **No per-record version stamp in the market-data store.** Version stays blob-level +
   per-`DatasetKind`. A v5 codec bump would rewrite every blob's bytes and break byte-determinism
   assertions across closed-green DATA-007/008/009/011/013/016/017 for **zero** evolution gain —
   `min_schema_version()` already tracks version per data-entity kind, exactly as SYS-66 asks.
2. **Access journal versions per LINE, not per file.** `v<N>\t<access_ts>\t<kind>\t<id>\t<SYMBOL>`.
   A `v`-prefixed first field is unambiguous against a legacy 4-field line (whose first field is a
   bare integer), so a header-write race in an append-only `O_APPEND` log is impossible and a file
   may legitimately mix legacy and versioned lines. Each *record* carries its version — the
   strongest reading of the AC.
3. **The log store versions its persisted envelope, not `LogRecord`.** `LogRecord.as_dict()` is the
   pinned SRS-LOG-001 SDK schema and stays byte-identical; the store writes
   `{**record.as_dict(), "schema_version": 1}`. Verified safe: `_record_from_mapping`
   (`persistence.py:388`) reads keys by name and ignores unknown ones.
4. **Legacy files are read, never rewritten.** Every reader: missing version → v1; unknown *future*
   version → fail closed. That is literally the AC.

**Risk #1 cleared before editing:** `python/atp_dashboard/killswitch.py` `_validated_activation`
reads named keys via `.get()` and never checks an exact key set, so a `schema_version` key is inert
for the UI-4 pane (no allow-listing needed).

## Build

1. `crates/atp-data/src/schema_registry.rs` — `SchemaDescriptor` + `EvolutionPosture
   {Ranged, MigrateOnRead, Pinned}` + a `const` table of every persisted entity. Pure data (no
   cross-crate imports) so the one-way dependency direction holds.
2. `tools/data015_schema_check.py` — the gate (mirrors `tools/data010_eviction_check.py`): parses the
   real constants from each writer's source and cross-checks the registry (drift), then scans
   `crates/*/src` + `python/` for persistence write surfaces and fails on any unregistered writer.
   PASS line `SRS-DATA-015 SCHEMA-EVOLUTION PASS`. Contract block `schema_evolution_contract` in
   `architecture/runtime_services.json`.
3. Version the four bare formats (backward-compatible, per decisions 2–4).
4. `tests/fixtures/schema_evolution/` — byte-frozen legacy blobs; today's build reads them all with
   no migration step and leaves them unchanged on disk.
5. `crates/atp-data/src/bin/data015_schema_cli.rs` — `report` + `inspect --dir <path>`; allow-list
   parser, `key:value` output (the `data010_eviction_cli.rs` pattern). The AC's *inspection* leg.
6. CI: wire the check into **both** `.github/workflows/ci.yml` and `tools/run_ci_locally.sh`.

## Tests

- **L1 unit** (in-module): descriptor invariants (min ≤ current, unique ids/magics); journal
  versioned round-trip, legacy line, unknown-future rejection, torn tail, mixed-version file.
- **L2 property** `tests/property/test_data015_schema_invariants.py` — Hypothesis over version
  fields: well-formed accepted, malformed rejected, unknown-future always fails closed.
- **L3 contract** `tests/test_data015_schema_contract.py` — gate passes on real source + mutation
  spot-checks.
- **L4 boundary** `crates/atp-data/tests/srs_data_015_schema_evolution.rs` — golden corpus through
  the real readers; `data015_schema_cli inspect` e2e.
- **L7 domain** `tests/domain/test_data015_schema_safety.py` — **mandatory**
  (`python/atp_safety/state.py` matches `SAFETY_PATH_RE`): legacy unversioned activation record still
  replays the guard; unknown-future version fails **closed** (never "never activated").

## Completeness target: **complete** (`passes:true`)

Every step is fixtures + file inspection — no IB, no live, no browser e2e. If anything forces
otherwise, integrate `--mode serialized` and say so plainly.
