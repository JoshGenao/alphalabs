=== SESSION SRS-DATA-015 ===
Date: 2026-07-27
Feature: SRS-DATA-015 — support schema evolution for stored data entities (SyRS SYS-66; SRS-5.5; P2)
Outcome: complete (passes:true) — every acceptance clause is provable solo with fixtures + file
         inspection; no IB, integration, live, or browser e2e is required by the AC.

## Session provenance (read this if the board looks odd)

The launcher first claimed **SRS-DATA-010**, which was already built and integrated (serialized,
2026-07-26). It was re-offered because the PRIMARY CHECKOUT
`~/Documents/Programming/Python/alphalabs` is behind `origin/main` **and dirty** (17 paths of
in-flight mypy/type-annotation work), so `_sync_primary_checkout()` bails
(`tools/agent_pool.py:254-262`) and `serialized_notes()` never sees `session-SRS-DATA-010.md` — the
exact churn loop the de-churn logic exists to prevent. De-churned via
`agent_pool.py block SRS-DATA-010 --on SRS-EXE-001 SRS-FAC-001 SRS-BT-001 SRS-NOTIF-001` (its real
deferred owners, per its own session note), released, re-claimed → the scheduler then handed over
SRS-DATA-015 legitimately.

**Operator action still required:** resolve those 17 dirty ROOT paths (they are the in-progress fix
for the 68 pre-existing mypy errors) so the scheduler stops re-offering serialized features.

## What I built

The AC's first clause — "**each** persisted entity records a schema version" — quantifies over the
whole system, so it is only as strong as its enumeration. The deliverable is therefore a registry
plus a gate that keeps the registry honest, not a set of spot fixes.

- `crates/atp-data/src/schema_registry.rs` — one `SchemaDescriptor` per persisted entity (15), as
  PURE DATA (`&'static str` + `i64`, no cross-crate imports) so the data layer can name entities in
  execution/simulation/orchestration without inverting the one-way dependency direction.
  `EvolutionPosture` = `Ranged` | `MigrateOnRead` | `Pinned`, with `Pinned ⇒ min == current`.
- **Versioned the five formats that recorded no version at all:** the DATA-010 access journal
  (per-line `v<N>` tag), the SAFE-001 kill-switch last-activation record, the LOG-001 JSONL segment
  envelope, the RESV-003 hot-swap trigger log, and the MD-006 readiness alert sink — *the last of
  which the gate found, not a human*. All version per RECORD, not per file: each is append-only or
  atomically replaced, so no writer owns "the start of the file" and a header would race.
- **Uniform read semantics** everywhere: a payload with no version field is read at
  `min_supported_version` (that IS "queryable without bulk migration" — files on disk are read where
  they lie and never rewritten), and an unknown FUTURE version ALWAYS fails closed.
- ORCH-005 rollback state gained an explicit version const compile-time-asserted against its magic
  (persisted bytes unchanged); the SEC-001 vault gained the read gate it never had (it always wrote
  a `version` but never checked one).
- `LogRecord.as_dict()` deliberately NOT widened — the version lives in the persisted envelope the
  store composes, so the LOG-001 SDK schema shared with the API/UI sinks stays byte-identical. The
  `log_persistence_contract.required_exports` pin was updated for the three new module constants.
- `tools/data015_schema_check.py` — parses the registry (no build dependency), checks its
  invariants, binds every entity to its real writer constants + magic, and scans `crates/*/src` +
  `python/` for durable-write surfaces, failing on any that is neither registered nor explicitly
  justified. Wired into BOTH `.github/workflows/ci.yml` and `tools/run_ci_locally.sh`.
- `crates/atp-data/src/bin/data015_schema_cli.rs` — the AC's *inspection* leg: `report` + `inspect`.
  Identifies by magic, reports `unidentified` rather than guessing at a magic-less format, is
  strictly read-only, and exits NON-ZERO on a file whose declared version this build cannot read
  (a pre-upgrade gate). Its `version_supported` field is deliberately narrower than "readable" — see
  the scope note under Critic verdicts.
- `tests/fixtures/schema_evolution/` — byte-frozen golden corpus: real v1/v2/v3/v4 market stores
  emitted by the actual writer, plus a legacy payload per retrofitted entity. Every test asserts the
  bytes are UNCHANGED after reading — the operational meaning of "no bulk migration".
- `architecture/runtime_services.json` → `schema_evolution_contract`.

Deliberately NOT done: a per-record version stamp inside the market-data store. It would rewrite
every blob's bytes and break byte-determinism assertions across closed-green DATA-007/008/009/011/
013/016/017 for zero evolution gain — `DatasetKind::min_schema_version()` already tracks version per
data-entity kind, which is what SYS-66 asks for.

## What I tested (per feature step)

- **Step 1: PASS** — `./init.sh` → `✓ Environment ready`.
- **Step 2: PASS** — `data015_schema_cli report` lists all 15 entities with their version ranges;
  `data015_schema_cli inspect --dir tests/fixtures/schema_evolution` identifies each store by magic,
  reports the version each file declares, and exits 0; `inspect` on a v99 store or record exits
  NON-ZERO with `version_supported:no`.
- **Step 3 (AC): PASS with fixtures** —
  * clause 1 (*each persisted entity records a schema version*): `tools/data015_schema_check.py`
    enumerates every durable-write surface in the repo and fails on an unregistered/unversioned one
    → `SRS-DATA-015 SCHEMA-EVOLUTION PASS: 15 persisted entities registered across 14 writers`.
  * clause 2 (*older versions remain queryable without bulk migration*): the golden corpus loads
    through the real readers with the on-disk bytes asserted unchanged.
  * L1 in-module: registry invariants + access-journal codec (versioned round-trip, legacy line,
    mixed-version file, unknown-future rejection, malformed tag, torn tail).
  * L2 property (`tests/property/test_data015_schema_invariants.py`, 10 tests): Hypothesis over the
    whole integer version domain for the retrofitted Python readers — supported accepted,
    everything else (including every future version) fails closed; `bool`/str/float/null rejected.
  * L3 contract (`tests/test_data015_schema_contract.py`, 22 tests): the gate passes on real
    source, plus mutation guards that each break the registry and demand a FAIL.
  * L4 boundary (`crates/atp-data/tests/srs_data_015_schema_evolution.rs`, 34 tests): golden corpus
    + CLI e2e + the full adversarial-review regression set.
  * L1 in-module (`crates/atp-types/src/json_scan.rs`, 16 tests): the shared fail-closed JSON
    version scanner — mismatched delimiters, invalid escapes, non-scalar garbage, duplicate keys,
    malformed nesting, and a nesting cap.
  * L4 boundary (`crates/atp-orchestrator/tests/resv_3_trigger_log_schema.rs`, 13 tests).
  * L7 domain (`tests/domain/test_data015_schema_safety.py`, 56 tests): mandatory —
    `python/atp_safety/state.py` matches `SAFETY_PATH_RE`. A kill-switch activation record must
    NEVER read as absent, whatever its version, or a repeat activation re-runs the liquidate
    sequence against already-flat positions.
- **Step 4: PASS** — evidence recorded here and in `architecture/runtime_services.json`.

Full gate: `cargo test --workspace` (1934 passed), `pytest -m "not integration and not e2e"`
(4378 passed, 5 skipped), `cargo fmt --check`, `cargo clippy --workspace -D warnings`, all 32
architecture/contract checks including the new `data015_schema_check`.

**Pre-existing CI red, NOT introduced here:** `mypy python/` reports 68 errors in 16 files. Zero of
them are in any file this branch touches — verified identical (68/16) on a clean tree with no
DATA-015 changes. Those 16 files are exactly the set the operator has uncommitted fixes for in the
primary checkout. Separately, `ruff format --check` was red on `main` for one drifted SRS-SAFE-003
test; fixed in its own `style:` commit (mirrors 6ca5bb9), so the format gate is now green.

## Critic verdicts

- deterministic (`critic_check.py --staged`): **APPROVE** — no findings.
- judgment (`adversarial_review.py origin/main`, reviewer=**codex**): **APPROVE on round 13**, after
  12 blocking rounds. Every finding was real and in my own new code; none were waved through.

The blocks fell into one class, worth recording because it recurred: **a persisted record this build
could not actually read being silently reclassified as one it could.** Concretely:

  1-2. `version_from_record_line` / `line_schema_version` scraped leading digits after a fixed
     prefix, so a reordered key, a float, or a quoted number degraded to "no version" → *legacy* →
     readable. Fixed with a three-state result (`Absent` / `Valid(n)` / `Invalid`) where only a
     genuinely missing key takes the legacy path.
  3. The scanner returned the moment the key matched, so `{"schema_version":1,` — a torn line whose
     remaining fields never arrived — parsed as a well-formed v1 record. Now the whole object is
     validated before any version is believed.
  4. A supported version was treated as sufficient evidence that a trigger was logged;
     `{"schema_version":1}` counted. `count_log_records` now requires the full v1 record shape, with
     the `kind` allow-list derived from `HotSwapTriggerKind` itself so it cannot drift.
  5. The scanner used a depth COUNTER, so `[}` balanced. Replaced with an explicit delimiter stack,
     plus strict escape and scalar validation.
  6. `declared_version` located the version by scanning for "the first plausible-looking integer",
     so a real v99 store had its version line skipped and its RECORD COUNT read as the version.
     Layout is now a declared fact per format (`HeaderLayout`), read from the exact line.
  7. Nested containers were only delimiter-balanced, so `{"payload":{bad}}` passed. Now recursively
     validated, with a nesting cap so a pathological line fails closed instead of the stack.
  8. Only the FIRST record of a per-record log was inspected, so a v99 record deeper in the file
     passed. Now every complete record is scanned; a torn tail is still tolerated.
  9. + 11. Python's `json.loads` is last-value-wins, so a record declaring both `schema_version:99`
     and `schema_version:1` read as v1. All four Python persisted readers now parse with an
     `object_pairs_hook` that refuses duplicates, matching the Rust gate.
  10. My own round-5 hardening (refusing raw control characters in strings, as JSON requires) made
     the trigger log's hand-rolled `json_escape` — which covered five characters — able to write a
     record its own reader rejects. Now escapes every C0 character.

**Round 12 was answered differently, and deliberately.** It asked the DATA-015 CLI to validate every
record BODY for the magic-less JSON entities. Doing so would mean re-implementing four other
features' record schemas inside the data layer — three of them in Python, one in `atp-orchestrator`,
which `atp-data` must not depend on — so the copies could not even be compiled against their
originals and would rot as their owners evolved. A stale copy of someone else's invariants inside a
*gate* is worse than no copy. The legitimate core of that finding was that the output word
`readable` **overstated** what the tool establishes, so the field is now `version_supported`, the
boundary is documented in the CLI, in `schema_evolution_contract.inspection_scope`, and pinned by
`srs_data_015_version_supported_is_a_claim_about_the_version_only` — which also asserts the OWNING
reader refuses the same bytes, so the boundary is covered somewhere rather than merely disclaimed.
Body validity is enforced in each owning reader (SRS-RESV-003 / SRS-LOG-001 / SRS-MD-006 /
SRS-SAFE-001), each of which fails closed; the access journal, which `atp-data` owns, IS shape-validated
by the CLI.

## The gate caught a live one before it even landed

While this branch was in review, a sibling agent integrated **SRS-DATA-018** (scheduled NAS backup).
On the pre-integrate rebase, `data015_schema_check.py` immediately failed on
`crates/atp-data/src/backup.rs` — a durable-write surface it had never seen, registered nowhere.
That is the totality clause working on real, fresh code from another agent rather than on a fixture.

The correct answer turned out to be "not a new entity": DATA-018 deliberately reuses the existing
store magics, and its `envelope` framing is `<magic>\n<checksum>\n<body>` — the store layout itself.
An export is a byte-for-byte copy of an already-registered entity, re-verified through that entity's
own codec. So it is allow-listed as a justified non-entity writer — and because that is a claim
about bytes, `srs_data_015_a_backup_export_is_still_the_source_entity` proves it: an exported blob
must identify as `market-data-store` and report the source's version. If a later change gives the
backup its own header, that test fails and the allow-list entry is exposed as wrong.

## Resume / next

Nothing outstanding for this feature. For whoever extends it:

- **Adding a persisted format?** Add a `SchemaDescriptor` row or `data015_schema_check.py` fails.
  That is deliberate — it is how the MD-006 readiness alert sink was caught.
- **Evolving a format?** Bump its `current_version`, keep `min_supported_version` where it is, add a
  migrate-on-read branch, and add a byte-frozen fixture to `tests/fixtures/schema_evolution/`.
  Never regenerate an existing fixture: that converts a regression lock into a tautology (the corpus
  README says so, and `test_no_legacy_fixture_carries_a_version_key` enforces it).
- The two `top_level_json_field` scanners are duplicated in `data015_schema_cli.rs` and
  `resv003_hot_swap_trigger_cli.rs` because `atp-orchestrator` does not depend on `atp-data`.
  If a third magic-less JSON format appears, promote the scanner to `atp-types` rather than widening
  the composition layer's dependency surface.
