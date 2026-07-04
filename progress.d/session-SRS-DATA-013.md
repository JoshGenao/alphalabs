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
  deterministic (critic_check.py, origin/main..HEAD after rebase; also pre-commit --staged): APPROVE — no findings
  judgment (adversarial_review.py, reviewer=claude-fallback — codex output unparseable): APPROVE — no findings.
    Corroborated by an independent skeptical sub-agent review (read files + ran tests/checks): APPROVE,
    no real bugs — 2 non-blocking observations (dedup key includes DatasetKind beyond literal
    symbol+date+resolution = physically-unreachable false-negative, consistent with store identity;
    `written` counts records handed to the tier not net-new inserts, doc states this) require no change.

Resume / next: COMPLETE at the data-layer scope. To flip passes:true (verified-e2e), verify the
dashboard alert pane + email/SMS reason summaries once SRS-UI-001 + SRS-NOTIF-001 land, wiring a
concrete IngestionValidationEventSink that fans the per-record events into those surfaces (the
QuarantineSummarySink here is the reference aggregator). Durable quarantine STORE (rejected payload +
reason + timestamp + source persisted) remains SRS-DATA-014/015. Don't rebuild Sys77RecordValidator /
the quarantining path — wire the display sink.
