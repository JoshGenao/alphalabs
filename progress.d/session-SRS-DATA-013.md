=== SESSION SRS-DATA-013 ===
Date: 2026-07-03
Feature: SRS-DATA-013 — validate ingested market & options records before primary write (ERR-5 / SyRS SYS-77)
Outcome: serialized (code merges, passes stays false)

CONTEXT: Keystone (unblocks DATA-001/002/004/005/006/014). The ERR-5 *skeleton* already existed
(gate DataLayer::ingest_record, ports RecordValidator/IngestionValidationEventSink, the 6
QuarantineReason variants, the IngestionValidationEvent/StructuredIngestionError types, the static
contract checker) — but ZERO rule logic: every RecordValidator in the tree was an accept-all stub.
DATA-013 owns the concrete rules. Central gap: the port passed only IngestionRecordSubmission
{source, record_hash} — a SHA hash can't be range-checked or key-deduped, so the validator needs the
real MarketDataRecord.

WHAT I DID:
- crates/atp-data/src/ingestion_validation.rs (NEW): Sys77RecordValidator — real, read-only,
  kind-aware classifier for the six SYS-77 rules, one deterministic reason per record (eval order:
  (d) required fields -> (a)/(b)/(c) OHLC range/band/volume or (f) option SYS-23 fields+non-neg ->
  (e) within-batch duplicate natural key, checked only against previously-admitted valid records so
  no double jeopardy). OHLCV kinds get (a-d); OptionChainSnapshot gets (f)+non-neg; Fundamental /
  CorporateActionSplit are outside SYS-77's OHLCV/option scope (own validation = DATA-005/011/012) so
  only the dup check applies. + QuarantineSummary/QuarantineSummarySink (counts-by-reason + total =
  the SYS-77 "count and nature" alert content). + DataLayer::ingest_market_records_quarantining =
  quarantine-and-continue batch path (invalid dropped/never written; valid subset written SSD-first +
  NAS-synced via the SRS-DATA-008 tier, so the DATA-008 routing guard stays green untouched;
  CorporateActionCoverage refused like the sibling paths). + mixed_validation_fixture (4 valid + one
  malformed per rule).
- Port change: RecordValidator::validate(&MarketDataRecord) (was &IngestionRecordSubmission);
  ingest_record derives the source+hash envelope internally (validation bound to exactly the persisted
  record). IngestionRecordSubmission stays minimal. Static ERR-5 checker unaffected (it requires only
  a validate method + the Valid/Quarantined gate structure). ~4 accept-all stubs (data005/008/016
  CLIs + query/tier/factor-pipeline tests) + the DATA-016 Capturing binding test updated mechanically
  (accept-all bodies unchanged -> DATA-008/016/005 stay green).
- data013_ingestion_validation_cli (NEW): ingest mixed fixture -> counts+reasons; inspect confirms
  quarantined records absent from primary.
- Blast-radius fix: the port text `validator.validate(&record)` -> `validator.validate(record)` broke
  3 literal `.replace()` mutation anchors (test_ingestion_idempotency_contract.py,
  test_ingestion_validation_contract.py, tests/domain/test_ingestion_idempotency.py) — updated to the
  new text (intent preserved; DATA-016 checker itself passes — it scans whole-lib token presence).
  Renamed 2 err_5 Rust tests for accuracy -> updated tests/domain/test_ingestion_validation_quarantine.py.

WHAT I TESTED (per step):
  Step 1: PASS — ./init.sh -> "✓ Environment ready".
  Step 2/3: PASS — data013_ingestion_validation_cli ingest --ssd S --nas N ->
    records_in:10 valid_written:4 quarantined_total:6 count_{RANGE_VIOLATION,OHLC_OUT_OF_BAND,
    NEGATIVE_VOLUME,NULL_REQUIRED_FIELD,DUPLICATE_RECORD,OPTION_FIELD_MISSING}:1 nas_sync:synced;
    inspect --ssd S -> store_len:4 (only AAPL/MSFT daily + 2 valid AAPL option contracts;
    TSLA/NVDA/AMZN/META + the malformed 240119C00160000 contract ABSENT).
  Tests: cargo test --workspace (all pass); cargo clippy --workspace --all-targets -D warnings clean;
    cargo fmt --check clean; crates/atp-data unit (Sys77 rules) + err_5 (5, real-validator sweep) +
    srs_data_013 (4, quarantine-and-continue e2e) green.
    tools/ingestion_validation_check.py -> ERR-5 PASS; tools/data008_tiering_check.py -> routing guard
    green (my CLI not an SSD-only bypass). pytest -m "not integration and not e2e" green incl.
    tests/domain/test_data013_ingestion_validation.py (behavioral CLI + Rust + scope-honesty).
  Step 4: passes stays FALSE (serialized) — Step 3's dashboard/notification alert DISPLAY needs the
    unbuilt SRS-UI-001/SRS-NOTIF-001 (contract deferred[]); data-layer validate->quarantine->counts
    core is verified solo.

Critic verdicts:
  deterministic (critic_check.py, pre-commit --staged every commit): APPROVE — no findings.
  judgment (adversarial_review.py): the FIRST run (pre-3880988) fell back to claude-fallback because
    Codex's --json envelope was unparseable → a false APPROVE (Codex's real verdict was silently
    dropped). After the operator applied the tools/adversarial_review.py fix (commit 3880988 unwraps
    the envelope), the REAL Codex verdict was BLOCK. Fixed across 4 Codex rounds — all concrete
    in-scope findings resolved + tested:
      R1: option validation accepted any non-empty contract string → OCC structure validation
          (occ_contract_ok, +8 unit cases); duplicate detection was batch-local → store-aware pass;
          quarantined records silently dropped → now RETURNED (quarantined_records: Vec).
      R2: cross-tier gap (cold record archived off SSD, only on NAS) → snapshot spans SSD ∪ NAS;
          +archive-off-SSD regression test.
      R3: fail-OPEN on a present-but-corrupt tier store → now FAILS CLOSED (MarketIngestError::Store);
          contract drift (runtime_services.json still marked the built validator deferred) → contract
          now lists delivered vs genuinely-deferred.
      R4: (D) demo ingest CLIs still used AcceptAllValidator → data016/008/005 rewired to
          Sys77RecordValidator (behaviour-preserving; sibling tests + DATA-008/016 checkers green).
          (C) NAS-UNMOUNTED conflict-absence proof — DEFERRED (operator-authorized via AskUserQuestion):
          fully fixing needs the SRS-DATA-018 durable catalog; failing closed on any NAS-unavailability
          would break SRS-DATA-008's degrade-tolerant write path. Documented + pinned in code +
          runtime_services.json deferred[]. NOT a faked approve — the residual is the authorized deferral.

Resume / next: COMPLETE at the data-layer scope. To flip passes:true (verified-e2e), verify the
dashboard alert pane + email/SMS reason summaries once SRS-UI-001 + SRS-NOTIF-001 land, wiring a
concrete IngestionValidationEventSink that fans the per-record events into those surfaces (the
QuarantineSummarySink here is the reference aggregator). Durable quarantine STORE (rejected payload +
reason + timestamp + source persisted) remains SRS-DATA-014/015. Don't rebuild Sys77RecordValidator /
the quarantining path — wire the display sink.

---

=== SESSION SRS-DATA-013 (2) — landing 1 of 3: the durable quarantine store, 2026-10-19 ===
Session-2 outcome: serialized (passes STAYS false). Operator-selected close-out session; plan
approved by the operator and saved in progress.d/plan-SRS-DATA-013.md.

WHAT SESSION 1 GOT WRONG (fixed here): the deferred[] entry naming SRS-DATA-014/015 as the
quarantine store's owner. DATA-014 is SYS-78 anomaly detection (R2) and DATA-015 is schema
evolution. SyRS SYS-77 says rejects are "quarantined in a separate store": that store is this
feature's.

WHAT I BUILT
- crates/atp-data/src/quarantine_store.rs: one atomically written, checksummed run file per
  ingestion batch under <ssd>/quarantine. A run holds every quarantined record IN FULL (reason,
  ERR-5 source + hash, payload), the batch totals, and the primary-write outcome:
  PENDING (written before the primary write; still PENDING = outcome unknown) -> WRITTEN /
  REFUSED / FAILED. Reads are bounded (MAX_RUNS_READ) and tri-state.
- Both primary-write paths (ingest_market_records_tiered, ingest_market_records_quarantining)
  record every batch, clean or not, and every exit (QuarantineStore::refuse; a static check,
  check_every_exit_is_recorded, fails on any other early return).
- TieredStore::ingest is crate-private (compile_fail doctest). data016_ingest_cli and
  data005_fundamental_cli wrote the store themselves; they now ingest through
  ingest_market_records_tiered like data008_tier_cli. DATA-017's writer rule moved to the one
  shared locked writer; the DATA-008 sweep refuses any direct save_to_path outside the coverage
  CLI.
- Conflicting re-ingests (same key, different content — the batch-scoped validator cannot see
  them) are decided by TieredStore::ingest under the SSD writer lock (OnConflict), against SSD,
  a readable NAS archive, and the batch, and recorded on the run BEFORE the primary save.
- data013_ingestion_validation_cli summary: the newest runs with every SYS-77 reason's count;
  each ingest CLI prints its run id and exits non-zero if the run could not be finalized.
- The run format is registered in PERSISTED_ENTITIES (quarantine-run, Pinned v1); the DATA-015
  scanner no longer truncates at the first #[cfg(test)].

REVIEW: 6 Codex rounds (Codex works again: ~/.codex/config.toml pinned gpt-5.5, which the
account no longer offers; now gpt-6.1-sol). Rounds 1-5 BLOCK, each a real defect, each fixed as
a class with a mutation-checked test; round 6 APPROVE with no findings.
  r1 rejects dropped on early exits; directory not durably linked; FAILED recorded after rename
  r2 ancestor chain not re-synced on retry / relative path
  r3 conflicting re-ingest lost (failed in upsert)
  r4 writer-found conflicts returned after the save
  r5 archived (NAS-only) key not screened; run format unregistered + scanner blind
Five lessons written back: durable-writes.md 34-37, test-integrity.md 45.

KNOWN BOUNDS (in ingestion_validation_contract.deferred[])
- An archived key cannot be screened while the NAS is unmounted or its store unreadable
  (SRS-DATA-008 keeps the SSD write going; the push reports a Failed sync). Owner: SRS-DATA-018.
- The store keeps every run; read_latest lists the directory. Pruning: SRS-DATA-010.
- SSD only: not archived to NAS, not a backup unit.

PROCESS NOTE: twice I edited the tree while a background CI mirror was running against it.
Both runs were stopped and discarded; the green mirror recorded for this landing ran on the
committed, unedited round-5 tree (5619 passed, every step ran).

Resume / next — landings 2 and 3 (the plan file has the detail):
  2. Operator notification: TriggerKind::DataQuarantine (severity Error; not suppressible by the
     MD-005 restart window), store codec tag, data013_quarantine_alert_cli notify (reads unnotified
     runs, one Email + Push per run with counts and reasons, durable cursor). Owner of running it
     after every ingestion job: SRS-DATA-001/002.
  3. Dashboard pane over `summary` (ATP_SSD_DATA_DIR), five visual states, browser evidence +
     a live Email/Push run on the VM. Then steps 3-4 of the evidence record and the operator's
     attested close.
  Do NOT rebuild the store, the write-path recording, or the conflict screening.
