//! SRS-DATA-010 boundary (L4) test: drive the compiled `data010_eviction_cli` end-to-end over a
//! fixture SSD tier built >80% full, proving every acceptance clause against real on-disk stores.
//!
//! This is the operator workflow the verification step names ("CLI/API workflows with fixture market
//! data ... and persisted output inspection"): build a fixture SSD primary + cold-read cache, write a
//! protection-inputs file + a real access journal, run `report` / `plan` / `enforce`, and inspect the
//! reloaded stores. The eviction POLICY is proven solo here; the deferred producers (a real
//! live-strategy-symbols feed, a real running-job registry) are the reason the feature stays
//! `passes:false` (serialized) — not any gap in the policy this test exercises.

use std::collections::HashMap;
use std::path::Path;
use std::process::Command;

use atp_data::access_journal::AccessJournal;
use atp_data::cold_read::COLD_READ_CACHE_SUBDIR;
use atp_data::store::{DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey};
use atp_data::{JobId, JobKind, JobRef};

const NOW: i64 = 1_700_000_000;

fn daily(symbol: &str, event_ts: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        },
        [MarketField {
            name: "close".to_string(),
            value_minor: 10_000,
        }],
    )
    .unwrap()
}

/// A unique scratch tier root for a test (the crate has no `tempfile` dependency).
fn tier_root(tag: &str) -> std::path::PathBuf {
    let base = std::env::temp_dir().join(format!(
        "atp-data010-{}-{}-{}",
        tag,
        std::process::id(),
        line!()
    ));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(base.join("ssd")).unwrap();
    std::fs::create_dir_all(base.join("nas")).unwrap();
    base
}

fn save_store(dir: &Path, records: &[MarketDataRecord]) {
    std::fs::create_dir_all(dir).unwrap();
    let mut store = MarketDataStore::new();
    for record in records {
        store.upsert(record.clone()).unwrap();
    }
    store.save_to_path(dir).unwrap();
}

struct CliRun {
    code: i32,
    fields: HashMap<String, String>,
    evictions: Vec<String>,
}

fn run_cli(args: &[&str]) -> CliRun {
    let output = Command::new(env!("CARGO_BIN_EXE_data010_eviction_cli"))
        .args(args)
        .output()
        .expect("run data010_eviction_cli");
    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut fields = HashMap::new();
    let mut evictions = Vec::new();
    for line in stdout.lines() {
        if let Some(rest) = line.strip_prefix("evict:") {
            evictions.push(rest.to_string());
        } else if let Some((key, value)) = line.split_once(':') {
            fields.insert(key.to_string(), value.to_string());
        }
    }
    CliRun {
        code: output.status.code().unwrap_or(-1),
        fields,
        evictions,
    }
}

#[test]
fn enforce_evicts_oldest_non_listed_cache_and_protects_live_recent_and_hot() {
    let root = tier_root("main");
    let ssd = root.join("ssd");
    let nas = root.join("nas");

    // Hot store (4 records, all inside the 90-day retention floor at NOW → retention-pinned; includes
    // the live symbol). Enforce must never touch these.
    save_store(
        &ssd,
        &[
            daily("LIVE", NOW - 1_000),
            daily("LIVE", NOW - 2_000),
            daily("HOTX", NOW - 3_000),
            daily("HOTY", NOW - 4_000),
        ],
    );
    let hot_bytes_before = std::fs::read(ssd.join("market_data.store")).unwrap();

    // Cold cache (6 records): 2 old non-listed (evict targets), 1 live, 1 recently-accessed, 1
    // watchlisted (deprioritised), 1 active-listed (deprioritised).
    let cache_dir = ssd.join(COLD_READ_CACHE_SUBDIR);
    save_store(
        &cache_dir,
        &[
            daily("OLD1", 1_000), // oldest non-listed → evicted first
            daily("OLD2", 2_000), // next-oldest non-listed → evicted second
            daily("LIVE", 500),   // pinned (live) even though it is the oldest
            daily("RECENT", 600), // pinned (recency via the journal)
            daily("WATCH", 700),  // deprioritised (watchlist)
            daily("ACTIVE", 800), // deprioritised (active list)
        ],
    );
    // usage = 4 hot + 6 cold = 10; capacity 10 → target floor(10*80/100)=8 → must evict 2.

    // Real AC-3 producer: a running backtest accessed RECENT within the window.
    let journal = AccessJournal::under_ssd(&ssd);
    assert!(journal.append(
        &JobRef::new(JobKind::Backtest, JobId::new("bt-live").unwrap()),
        "RECENT",
        NOW - 100,
    ));

    // Protection-inputs file: live + the two deprioritised lists.
    let prot = root.join("protect.txt");
    std::fs::write(
        &prot,
        "# fixture protection inputs\nlive LIVE\nwatchlist WATCH\nactive ACTIVE\n",
    )
    .unwrap();

    let ssd_s = ssd.to_str().unwrap();
    let nas_s = nas.to_str().unwrap();
    let prot_s = prot.to_str().unwrap();
    let now_s = NOW.to_string();

    // --- plan (dry) first: cold-before-hot ordering, evicts OLD1 then OLD2 ---
    let plan = run_cli(&[
        "plan",
        "--ssd",
        ssd_s,
        "--nas",
        nas_s,
        "--ssd-capacity",
        "10",
        "--now",
        &now_s,
        "--protection-inputs",
        prot_s,
        "--use-journal",
    ]);
    assert_eq!(plan.code, 0);
    assert_eq!(plan.fields["target"], "8");
    assert_eq!(plan.fields["usage_before"], "10");
    assert_eq!(plan.fields["reached_target"], "true");
    assert_eq!(
        plan.fields["pinned_live"], "3",
        "1 hot + 1 cold LIVE + ... = 3 live-pinned"
    );
    assert_eq!(plan.fields["pinned_recent"], "1");
    assert_eq!(plan.evictions, vec!["cold:OLD1:1000", "cold:OLD2:2000"]);

    // plan must NOT have mutated the cache.
    assert_eq!(
        MarketDataStore::load_from_path(&cache_dir).unwrap().len(),
        6
    );

    // --- enforce: physically evict the two oldest non-listed cache records ---
    let enforce = run_cli(&[
        "enforce",
        "--ssd",
        ssd_s,
        "--nas",
        nas_s,
        "--ssd-capacity",
        "10",
        "--now",
        &now_s,
        "--protection-inputs",
        prot_s,
        "--use-journal",
    ]);
    assert_eq!(enforce.code, 0, "enforce reached the mark → exit 0");
    assert_eq!(enforce.fields["cold_evicted"], "2");
    assert_eq!(enforce.fields["usage_after"], "8");
    assert_eq!(enforce.fields["reached_target"], "true");
    assert_eq!(enforce.fields["hot_pressure_deferred"], "0");

    // Cache: OLD1/OLD2 gone; LIVE, RECENT, WATCH, ACTIVE retained.
    let cache_after = MarketDataStore::load_from_path(&cache_dir).unwrap();
    assert_eq!(cache_after.len(), 4);
    let remaining: Vec<&str> = cache_after
        .records()
        .iter()
        .map(|r| r.key().symbol.as_str())
        .collect();
    assert!(!remaining.contains(&"OLD1"));
    assert!(!remaining.contains(&"OLD2"));
    for kept in ["LIVE", "RECENT", "WATCH", "ACTIVE"] {
        assert!(remaining.contains(&kept), "{kept} must survive eviction");
    }

    // Hot store byte-identical — enforce never opened the SSD primary.
    let hot_bytes_after = std::fs::read(ssd.join("market_data.store")).unwrap();
    assert_eq!(
        hot_bytes_before, hot_bytes_after,
        "hot data must be untouched"
    );
}

#[test]
fn enforce_exits_nonzero_when_the_mark_needs_pinned_or_hot_data() {
    let root = tier_root("blocked");
    let ssd = root.join("ssd");
    let nas = root.join("nas");

    // 4 hot (retention-pinned) + 1 evictable cold. capacity 5 → target 4; usage 5 → need to evict 1,
    // and exactly one evictable cold record exists → reachable. Now pin THAT cold record as live too,
    // so nothing is evictable and the mark cannot be met without touching pinned/hot data.
    save_store(
        &ssd,
        &[
            daily("LIVE", NOW - 1),
            daily("LIVE", NOW - 2),
            daily("LIVE", NOW - 3),
            daily("LIVE", NOW - 4),
        ],
    );
    let cache_dir = ssd.join(COLD_READ_CACHE_SUBDIR);
    save_store(&cache_dir, &[daily("LIVE", 100)]);
    let cache_bytes_before = std::fs::read(cache_dir.join("market_data.store")).unwrap();

    let prot = root.join("protect.txt");
    std::fs::write(&prot, "live LIVE\n").unwrap();

    let enforce = run_cli(&[
        "enforce",
        "--ssd",
        ssd.to_str().unwrap(),
        "--nas",
        nas.to_str().unwrap(),
        "--ssd-capacity",
        "5",
        "--now",
        &NOW.to_string(),
        "--protection-inputs",
        prot.to_str().unwrap(),
    ]);
    assert_ne!(
        enforce.code, 0,
        "cannot meet the mark without evicting pinned data → non-zero"
    );

    // Nothing was evicted — the cache is byte-identical (fail-safe: never evict pinned data).
    let cache_bytes_after = std::fs::read(cache_dir.join("market_data.store")).unwrap();
    assert_eq!(
        cache_bytes_before, cache_bytes_after,
        "no pinned record may be evicted"
    );
}

#[test]
fn enforce_refuses_without_an_explicit_protection_source() {
    let root = tier_root("gate");
    let run = run_cli(&[
        "enforce",
        "--ssd",
        root.join("ssd").to_str().unwrap(),
        "--nas",
        root.join("nas").to_str().unwrap(),
        "--ssd-capacity",
        "10",
    ]);
    assert_ne!(
        run.code, 0,
        "enforce must fail closed without a protection source"
    );
}

#[test]
fn a_corrupt_access_journal_makes_use_journal_fail_closed() {
    let root = tier_root("corrupt");
    let ssd = root.join("ssd");
    save_store(&ssd, &[daily("AAA", NOW - 10)]);

    // Write a corrupt (complete, malformed) journal line.
    let journal = AccessJournal::under_ssd(&ssd);
    std::fs::create_dir_all(journal.dir()).unwrap();
    std::fs::write(journal.log_path(), b"not-a-ts\tbacktest\tjob\tAAA\n").unwrap();

    let run = run_cli(&[
        "report",
        "--ssd",
        ssd.to_str().unwrap(),
        "--nas",
        root.join("nas").to_str().unwrap(),
        "--ssd-capacity",
        "10",
        "--use-journal",
    ]);
    assert_ne!(run.code, 0, "a corrupt journal must fail the read closed");
}
