//! SRS-DATA-017 (support concurrent reads during ingestion writes) — L5 Load test.
//!
//! Acceptance: "Strategy containers, backtests, factor jobs, and notebooks read previously ingested
//! data while ingestion jobs write new data **without corruption or blocking completed data**."
//! Verification mode: **Load test**.
//!
//! These tests drive the public `atp-data` store concurrency path the operator CLIs use — a
//! single-writer `StoreLock`-held load-modify-save (`data016_ingest_cli ingest`) running concurrently
//! with lock-free `MarketDataStore::load_from_path` reads (`data007_query_cli query` /
//! `data016_ingest_cli inspect`). The store is a snapshot-isolation store: writers serialize behind
//! the exclusive lock and publish atomically (scratch -> fsync -> rename -> dir fsync), readers take
//! no lock and each `load_from_path` is a consistent point-in-time snapshot, and a torn/corrupt read
//! fails closed (`StoreError::ChecksumMismatch`) rather than yielding a partial store.
//!
//! Read/write OVERLAP is a MEASURED condition, not inferred from thread counts: the writer holds its
//! first lock window open until a reader has demonstrably read while the write is in progress, and the
//! test fails if no read ever overlaps an active write. All other assertions are correctness
//! invariants (no timing/throughput asserts), so the Load test is deterministic regardless of how the
//! OS interleaves the threads.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use atp_data::store::{
    fixture_batch, DatasetKind, MarketDataStore, NaturalKey, StoreError, StoreLock, UpsertOutcome,
};

/// A unique scratch directory under the OS temp dir (a fixed per-test label, not a clock/RNG read).
fn temp_store_dir(label: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("atp_data017_store_{label}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("provision the temp store dir");
    dir
}

const SEED_TS: i64 = 1_700_000_000;
/// Each fixture daily-equity batch is two records (AAPL + MSFT at one event_ts).
const RECORDS_PER_BATCH: usize = 2;

/// Ingest one fixture daily-equity batch at `event_ts` through the public store write path, holding
/// the single-writer lock across the WHOLE load-modify-save — exactly what `data016_ingest_cli
/// ingest` does. Returns the number of freshly inserted records.
fn ingest_batch_locked(dir: &std::path::Path, event_ts: i64) -> usize {
    let _lock = StoreLock::acquire(dir).expect("a writer acquires the single-writer lock");
    let mut store = MarketDataStore::load_from_path(dir).expect("load the existing catalog");
    let mut inserted = 0;
    for record in fixture_batch(DatasetKind::DailyEquityBar, event_ts) {
        if matches!(
            store.upsert(record).expect("upsert a fixture record"),
            UpsertOutcome::Inserted
        ) {
            inserted += 1;
        }
    }
    store
        .save_to_path(dir)
        .expect("atomically publish the modified catalog");
    inserted
}

/// The natural keys of the seeded (previously-ingested / "completed") records, so a reader can assert
/// they are present in every snapshot.
fn seed_keys() -> Vec<NaturalKey> {
    fixture_batch(DatasetKind::DailyEquityBar, SEED_TS)
        .iter()
        .map(|record| record.key().clone())
        .collect()
}

#[test]
fn srs_data_017_concurrent_readers_never_see_a_torn_or_lost_store() {
    let dir = temp_store_dir("load_test");
    // Seed the "previously ingested" / completed data.
    let seeded = ingest_batch_locked(&dir, SEED_TS);
    assert_eq!(
        seeded, RECORDS_PER_BATCH,
        "the seed batch inserts its records"
    );
    let seed_keys = seed_keys();

    const WRITES: usize = 40;
    const READERS: usize = 4;
    let done = AtomicBool::new(false);
    let total_reads = AtomicUsize::new(0);
    // True while a writer holds the lock mid load-modify-save (an ingestion write IN PROGRESS).
    let write_in_progress = AtomicBool::new(false);
    // Reads that provably bracketed an active write — the read-during-write invariant, MEASURED.
    let overlapping_reads = AtomicUsize::new(0);

    // Finally-style guard: set `done` on ANY writer exit — a normal return OR a panic on one of the
    // writer's own assertions (exactly the storage regressions this test is meant to catch). Without
    // this, a writer panic would leave the readers (which exit on `done`) spinning forever and
    // `thread::scope` would wait on them, so the gate would HANG instead of surfacing the failing
    // invariant. With it, a writer panic releases the readers, the scope joins, and the panic
    // propagates as a deterministic FAILURE.
    struct DoneOnDrop<'a>(&'a AtomicBool);
    impl Drop for DoneOnDrop<'_> {
        fn drop(&mut self) {
            self.0.store(true, Ordering::Release);
        }
    }

    std::thread::scope(|scope| {
        // The ingestion writer: many lock-held load-modify-save cycles, each adding a NEW date's
        // records (distinct event_ts => distinct natural keys), so the store grows under the readers.
        scope.spawn(|| {
            let _signal_done = DoneOnDrop(&done);
            for i in 1..=WRITES {
                let _lock =
                    StoreLock::acquire(&dir).expect("a writer acquires the single-writer lock");
                write_in_progress.store(true, Ordering::Release);
                // On the FIRST iteration, hold the in-progress window open (lock HELD) until a reader
                // has demonstrably read during it — a deterministic overlap proof, not a timing race.
                // Readers are lock-free, so on a healthy store one records overlap within microseconds.
                // The wait is BOUNDED by a generous deadline so the gate FAILS rather than HANGS if the
                // property is broken: if a reader blocks (a regressed read lock) or panics (the
                // corruption path this test catches), overlap is never recorded — the writer then stops
                // waiting, releases the lock, finishes, and the scope join propagates any reader panic
                // while the final `overlapping_reads >= 1` assertion fails. The deadline is only ever
                // approached on a broken property; a healthy run clears it immediately.
                if i == 1 {
                    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
                    while overlapping_reads.load(Ordering::Acquire) == 0
                        && std::time::Instant::now() < deadline
                    {
                        std::hint::spin_loop();
                    }
                }
                let mut store =
                    MarketDataStore::load_from_path(&dir).expect("load the existing catalog");
                let mut inserted = 0;
                for record in fixture_batch(DatasetKind::DailyEquityBar, SEED_TS + i as i64) {
                    if matches!(
                        store.upsert(record).expect("upsert"),
                        UpsertOutcome::Inserted
                    ) {
                        inserted += 1;
                    }
                }
                assert_eq!(
                    inserted, RECORDS_PER_BATCH,
                    "each new date inserts fresh records"
                );
                store
                    .save_to_path(&dir)
                    .expect("atomically publish the modified catalog");
                write_in_progress.store(false, Ordering::Release);
            }
            // `_signal_done` drops here on normal completion (and on any panic above), setting `done`.
        });

        // Lock-free readers: each loops doing read-only snapshot loads until the writer signals done,
        // doing at least one read regardless of interleaving. A generous per-reader deadline is a
        // defense-in-depth abort path so the load test can never spin indefinitely even if `done` were
        // somehow never set; on every healthy or bounded-failure run the writer sets `done` first.
        for _ in 0..READERS {
            scope.spawn(|| {
                let mut local_reads = 0usize;
                let mut last_len = 0usize;
                let reader_deadline =
                    std::time::Instant::now() + std::time::Duration::from_secs(60);
                loop {
                    // A READ takes NO lock (it cannot be refused with StoreError::Locked) and must
                    // never observe a half-written store. Bracket the read with the write-in-progress
                    // flag so a read that overlaps an active write is a MEASURED fact.
                    let writing_before = write_in_progress.load(Ordering::Acquire);
                    let store = match MarketDataStore::load_from_path(&dir) {
                        Ok(store) => store,
                        Err(StoreError::ChecksumMismatch) => panic!(
                            "torn read: a concurrent reader observed a half-written store \
                             (StoreError::ChecksumMismatch) — atomic publish is broken"
                        ),
                        Err(other) => panic!(
                            "a lock-free read failed during concurrent ingestion (it should never \
                             block or error): {other}"
                        ),
                    };
                    let writing_after = write_in_progress.load(Ordering::Acquire);
                    if writing_before || writing_after {
                        // This read read previously-ingested data WHILE an ingestion job held the lock.
                        overlapping_reads.fetch_add(1, Ordering::Release);
                    }
                    // Completed data is never lost or blocked: every seeded record is always present.
                    for key in &seed_keys {
                        assert!(
                            store.get(key).is_some(),
                            "previously-ingested record {key:?} vanished mid-ingestion"
                        );
                    }
                    // Each snapshot is consistent: the catalog only grows (writer inserts only), so a
                    // reader never observes a regression in size, and never fewer than the seed.
                    let len = store.len();
                    assert!(
                        len >= seed_keys.len(),
                        "a snapshot dropped below the seed set"
                    );
                    assert!(
                        len >= last_len,
                        "a reader observed the catalog shrink between snapshots"
                    );
                    last_len = len;
                    local_reads += 1;
                    if done.load(Ordering::Acquire) || std::time::Instant::now() >= reader_deadline
                    {
                        break;
                    }
                }
                total_reads.fetch_add(local_reads, Ordering::Relaxed);
            });
        }
    });

    // The readers actually ran concurrently with the writer.
    assert!(
        total_reads.load(Ordering::Relaxed) >= READERS,
        "every reader performed at least one concurrent read"
    );
    // The core read-during-write invariant is a MEASURED condition, not inferred from thread counts:
    // at least one read provably overlapped an active ingestion write (the writer held its first lock
    // window open — up to a bounded deadline — until this was observed, so the assertion cannot pass by
    // the writer racing ahead, and FAILS deterministically here if overlap never occurred).
    assert!(
        overlapping_reads.load(Ordering::Relaxed) >= 1,
        "no read overlapped an active ingestion write — the load test did not exercise read/write overlap"
    );

    // After the writers finish, the catalog holds the seed + every ingested date — no write was lost
    // to a reader, and no reader corrupted a write.
    let final_store = MarketDataStore::load_from_path(&dir).expect("final reload is clean");
    assert_eq!(
        final_store.len(),
        (1 + WRITES) * RECORDS_PER_BATCH,
        "the serialized catalog holds the seed plus every ingested date"
    );
    for key in &seed_keys {
        assert!(
            final_store.get(key).is_some(),
            "the seed survived all the writes"
        );
    }

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn srs_data_017_a_read_never_blocks_on_a_held_writer_lock() {
    // "...without ... blocking completed data": a reader does not need the single-writer lock, so a
    // write IN PROGRESS (the lock held) must not block a read of already-completed data.
    let dir = temp_store_dir("read_during_held_lock");
    let seeded = ingest_batch_locked(&dir, SEED_TS);
    assert_eq!(seeded, RECORDS_PER_BATCH);
    let seed_keys = seed_keys();

    // Simulate an ingestion job mid-write: hold the exclusive writer lock for the whole read below.
    let held = StoreLock::acquire(&dir).expect("a writer holds the lock while ingesting");

    // The reader takes NO lock — it completes immediately against the last atomically-published
    // snapshot, even though a writer currently holds the lock. (If the reader needed the lock, this
    // load would have to wait for `held` to drop.)
    let store = MarketDataStore::load_from_path(&dir)
        .expect("a lock-free read completes while a writer holds the lock");
    for key in &seed_keys {
        assert!(
            store.get(key).is_some(),
            "completed data is readable during an active write"
        );
    }
    assert_eq!(store.len(), RECORDS_PER_BATCH);

    // A SECOND writer, by contrast, IS refused while the lock is held (writers serialize) — proving
    // the lock is real and it is only the lock-free read that proceeds.
    assert!(
        matches!(StoreLock::acquire(&dir), Err(StoreError::Locked)),
        "a second concurrent writer must be refused while the lock is held"
    );

    drop(held);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn srs_data_017_serialized_writers_never_lose_each_others_records() {
    // Behavioral proof that the single-writer lock held ACROSS the whole load-modify-save prevents
    // last-publish-wins: two concurrent ingestion jobs each retry the lock until they acquire it, load
    // the LATEST catalog (already including the other job's committed records), add their own disjoint
    // records, and save while still holding the lock. Both jobs' records must survive — a premature
    // lock release would let one job load a stale catalog and erase the other's records on save. This
    // is un-gameable by token presence: it exercises the real lock lifetime end to end.
    let dir = temp_store_dir("two_writers");
    const PER_WRITER: i64 = 20;
    let dir_ref = dir.as_path();

    std::thread::scope(|scope| {
        for w in 0..2u32 {
            scope.spawn(move || {
                for i in 0..PER_WRITER {
                    // Disjoint event_ts ranges per writer => the two jobs insert disjoint natural keys.
                    let event_ts = SEED_TS + 1 + (w as i64) * 1_000 + i;
                    loop {
                        match StoreLock::acquire(dir_ref) {
                            Ok(_lock) => {
                                let mut store = MarketDataStore::load_from_path(dir_ref)
                                    .expect("load latest catalog");
                                for record in fixture_batch(DatasetKind::DailyEquityBar, event_ts) {
                                    store.upsert(record).expect("upsert");
                                }
                                store
                                    .save_to_path(dir_ref)
                                    .expect("save under the held lock");
                                break; // `_lock` drops here — AFTER the save, never before.
                            }
                            // A held lock refuses the other writer; retry (writers serialize, no loss).
                            Err(StoreError::Locked) => std::hint::spin_loop(),
                            Err(other) => panic!("unexpected store-lock error: {other}"),
                        }
                    }
                }
            });
        }
    });

    let store = MarketDataStore::load_from_path(&dir).expect("final reload is clean");
    assert_eq!(
        store.len(),
        2 * PER_WRITER as usize * RECORDS_PER_BATCH,
        "a concurrent ingestion job's records were lost to last-publish-wins — the lock was not held \
         across the whole load-modify-save"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
