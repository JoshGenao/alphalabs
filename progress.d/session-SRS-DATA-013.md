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

Adversarial rounds: 15   (whole feature: landing 1 = rounds 1-6, landing 2 = rounds 7-11, landing 3 = rounds 12-15)
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

=== SESSION SRS-DATA-013 (2) - landing 2 of 3: operator notification, 2026-10-19 ===
Outcome: serialized (passes STAYS false). Steps 1-2 re-recorded at the landing-2 head; steps 3-4
need landing 3.

WHAT I BUILT
- atp-notification: TriggerKind::DataQuarantine ("DATA_QUARANTINE", severity Error), never
  suppressed by the MD-005 restart window; store codec tag "Q", schema v4 (reads v3). A store
  refuses a suppressed data-quarantine event, and a pre-v4 store that claims to hold one.
- atp-orchestrator/src/operator_channels.rs: the Email + Push channel construction and the
  placeholder-secret refusal, moved out of notif001_operator_alert_cli so both CLIs share it.
- atp-orchestrator/src/quarantine_notification.rs + data013_quarantine_alert_cli `notify --ssd
  --store`: one alert per run state (the run id, N of M records quarantined, each reason's
  count, and the primary write's outcome), at-least-once. A run is settled by a per-run marker file
  (quarantine_settled/<id>.settled, validated on read) only once its alert got out on a channel
  or it was clean. PENDING runs wait out a 5-minute grace period, then alert provisionally; an
  unreadable run is alerted as unreadable and never settled. Exit 0 ok, 1 when an alert failed,
  a settle failed, or a marker is invalid, 2 for usage/configuration.

REVIEW (landing 2): rounds 7-11. Rounds 7-9 BLOCK, round 10 WARN, round 11 APPROVE with no
findings. Each finding fixed as a class with a mutation-checked test.
  r7  (L2 r1) a run older than the newest 200 fell out of the alert scan
  r8  (L2 r2) a run published late was skipped by the watermark; two saves shared a scratch
      name. Replaced the watermark with one settlement marker per run.
  r9  (L2 r3) a damaged or foreign marker settled its run. Markers are validated on read.
  r10 (L2 r4) the writer's `{:06}` sequence is a minimum width; the parser wanted exactly six,
      so from run 1,000,000 every marker read as invalid. durable-writes.md rule 38.
The 8-round budget ran out after r9; the operator granted one more (recorded in review.jsonl).

VERIFIED: CI mirror green on e22729f (every step ran); deterministic critic APPROVE over
origin/main..HEAD; evidence steps 1-2 pass (34 domain tests).

KNOWN BOUNDS
- Nothing runs `notify` automatically yet. Running it after every ingestion job belongs to the
  ingestion jobs themselves: SRS-DATA-001/002 (both blocked on this feature).
- Not yet run against the real transports (Brevo email, ntfy push). That is landing 3's live run.

Resume / next - landing 3:
  Dashboard pane over `data013_ingestion_validation_cli summary` (ATP_SSD_DATA_DIR), five visual
  states (never configured, store unreadable, no runs yet, clean run, run with quarantines),
  browser evidence with screenshots + a live Email/Push run on the VM. Then evidence steps 3-4,
  a fresh whole-feature judgment review, and the operator's attested close.
  Do NOT rebuild the store, the write-path recording, the conflict screening, or the alerting.

=== SESSION SRS-DATA-013 (3) - landing 3 of 3: the dashboard pane, 2026-10-26 ===
Outcome: serialized (A: done - every step executed through evidence.py run at the reviewed
         head, step 3 with screenshots; the close needs a named attestation because
         verification_method is `e2e`)

WHAT I BUILT
- atp-orchestrator quarantine_notification::alert_status + `data013_quarantine_alert_cli
  status --ssd --store [--limit 1-200]`: read-only, sends and settles nothing. Each of the
  newest runs' alert state (NOT_NEEDED / ALERTED / ALERTED_PROVISIONAL / IN_PROGRESS /
  AWAITING_OUTCOME / DUE / SETTLED_UNVERIFIABLE) through the SAME decide() rule and settlement
  markers a notify pass uses, the per-channel outcome of the stored alert for that state, and
  the run state the verdict was judged against. Reads markers, then events, then runs: the
  reverse of the order notify writes them (r12). describe_unreadable() is now the one wording
  the sender and the reader share.
- python/atp_dashboard/quarantine.py QuarantinePaneProvider, GET /dashboard/api/quarantine,
  always composed by mount_default_dashboard. Two independent legs: counts and reasons
  (data013_ingestion_validation_cli summary over ATP_SSD_DATA_DIR) and delivery (status over
  ATP_NOTIFICATION_STORE_DIR). Strict kv parsing; drift is unreadable, never data. Delivery is
  attached to a run only when the status leg judged the same state the counts show (r13).
  One read serves every request for 5 s (the status read is O(history)); 1.5 s per CLI so
  the worst case fits the browser's 4 s poll (r12).
- The Data Quarantine pane: the newest run as a gate (records in / admitted / held / not
  stored, hazard-striped held segment), six SYS-77 reason tiles with rule-letter badges, the
  operator-alert strip with EMAIL/PUSH chips, and a ledger of the newest 20 runs. Seven pane
  states; not configured, store unreadable and no runs yet never show a count; PENDING shows
  admitted as unknown (r13). Dark and light themes screenshotted in every state.
- `notify` and `status` refuse a repeated flag (was last-wins).
- tests/fixtures/operator_transports.py: loopback SMTP relay + ntfy stand-ins, so tests drive
  the REAL notify CLI end to end.

ALSO FIXED ON THE WAY (own commits)
- c2634b1 The topbar brand title slid under the Research key at laptop widths, and below
  ~1100px the Readiness chip overran it. tests/e2e/test_dashboard_topbar_layout.py (6 widths;
  red at 5 of them against the old CSS). The footer named SRS-NOTIF-001 (closed) as the owner
  of the critical-alerts feed; UI-1 owns it.
- 6b4f028 tests/unit/test_evidence.py ran a bare `pytest` from PATH: red in any shell without
  the venv activated (the one failure in a 5686-test run).

WHAT I TESTED (per step, all through `evidence.py run` at the final head)
  Step 1: PASS - ./init.sh -> "✓ Environment ready".
  Step 2: PASS - the DATA-013 domain tests (store, ingestion validation, alert incl. the new
          status cases), the pane's unit tests and its boundary tests over real binaries:
          89 passed.
  Step 3: PASS - ATP_RUN_E2E=1 ATP_CAPTURE_EVIDENCE=1 tests/e2e/test_dashboard_quarantine_browser.py:
          4 passed. Real ingest of the SYS-77 mixed fixture (10 records, one failing each of the
          six rules); `inspect` proves only the 4 valid records reached primary; the pane, through
          the SHIPPED composition, shows 6 held / 4 admitted / one per reason and NOT YET
          DELIVERED; the real notify CLI sends through the loopback relay + ntfy, both bodies carry
          "6 of 10 records quarantined" and all six REASON=1; the pane then shows "operator
          alerted" with EMAIL · QUEUED and PUSH · DELIVERED. Two screenshots attached. Plus: an
          unreadable store shows no count, an all-failed attempt reads "attempted" and stays DUE,
          a PENDING run shows admitted as unknown.
  Step 4: PASS - cargo test -p atp-data -p atp-orchestrator --test srs_data_013_ingestion_validation
          --test srs_data_013_quarantine_store --test srs_data_013_quarantine_alert: 8 + 14 + 15.
  Gates: full `pytest -m "not integration and not e2e"` 5686 passed (the 1 failure was the
  PATH-dependent evidence test, fixed); full tests/e2e 78 passed, 3 skipped (jupyterlab not
  installed locally x2; RESV-005's known skip); cargo clippy --workspace -D warnings clean;
  tools/run_ci_locally.sh "every step ran", 85/85 + 23/23 contract checks; mutation_verify
  33/33 red (by collection failure - see test-integrity rule 38), plus hand mutations of the
  repeated-flag refusal, the provisional-alert mapping and the PENDING admitted cell, each red.

WHAT IS NOT PROVEN HERE
- The real transports. The alert path ran against loopback stand-ins that speak the relay's
  SMTP and ntfy's publish protocol; SRS-NOTIF-001 proved the real Brevo relay and the operator's
  ntfy on 2026-09-01 for its own trigger kinds. A live DATA_QUARANTINE page on the VM was
  offered to the operator, not run. The VM checkout (~/atp) is on a 2026-09-01 commit.
- No deterministic test for the r12 read-order fix: the race needs an interleaving the library
  has no hook for. The argument is in alert_status's comment.
- Nothing runs notify after an ingestion job yet (SRS-DATA-001/002, in deferred[]); the pane's
  DUE text tells the operator to run it.
- Known cosmetic nit: the ledger's columns shift slightly when the alert chip's text changes
  width (auto table layout).

CLASS LEFT OPEN, RECORDED NOT CHANGED: the inventory pane (inventory.py, 10 s) and the Hot-Swap
status sources (atp_hotswap, 30 s) have CLI budgets longer than the browser's 4 s poll abort -
the r12 defect. Owners UI-2 / UI-5; honest-surfaces rule 23b.

REVIEW (landing 3): rounds 12-15.
  r12 WARN (claude-fallback; Codex output unparseable) - read longer than the poll; markers read
      after runs; "sent" on an all-failed attempt; DUE text promised an automatic pass; status
      total_runs unchecked. All fixed (2b5ec04).
  r13 WARN (codex) - delivery joined by id alone across a run that changed between reads;
      PENDING rendered "0 admitted". Both fixed (70ea28b).
  r14 APPROVE (codex) at 70ea28b, no findings.
  r15 APPROVE (codex) at 488f111 - the same code plus the queue doc and two playbook rules,
      run so the approval names the tree that ships. No findings. Evidence steps 1-4 and both
      critic stamps were then re-recorded at 488f111; `evidence.py verify` (strict) passes.

PLAYBOOK WRITE-BACKS: honest-surfaces 23b (server read vs browser poll abort);
test-integrity 37 (first captured run attaches nothing), 38 (red by import failure).

Resume / next: the operator's attested close, from the PRIMARY checkout after this branch
integrates:  python3 tools/close_feature.py SRS-DATA-013 --verified --attested-by operator
