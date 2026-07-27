//! SRS-DATA-015 boundary (L4) test: read the byte-frozen golden corpus with TODAY's readers and drive
//! the compiled `data015_schema_cli` end to end.
//!
//! This is the operator workflow the verification step names ("CLI/API workflows with fixture market
//! data ... and persisted output inspection"), and it is the direct evidence for both acceptance
//! clauses:
//!
//! - **"data written under older schema versions remains queryable ... without bulk migration"** —
//!   every historical blob in `tests/fixtures/schema_evolution/` is opened by the current reader and
//!   its bytes are asserted UNCHANGED afterwards. A reader that had to upgrade the file to read it
//!   would fail this test, which is exactly the property "no bulk migration" names.
//! - **"each persisted entity records a schema version"** — the CLI reports the declared version of
//!   each file straight from its bytes, and refuses (non-zero exit) a version it cannot read.

use std::fs;
use std::path::PathBuf;
use std::process::Command;

use atp_data::access_journal::AccessJournal;
use atp_data::schema_registry::{descriptor, supports_version, PERSISTED_ENTITIES};
use atp_data::store::{MarketDataStore, StoreError};

/// The committed golden corpus.
fn corpus() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/schema_evolution")
}

fn cli() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target/debug/data015_schema_cli")
}

/// A unique scratch directory (the crate has no `tempfile` dependency).
fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("atp-data015-{tag}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).expect("scratch dir");
    dir
}

// --------------------------------------------------------------------------- //
// AC clause 2 — older versions stay queryable, and the bytes are never touched
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_015_every_historical_market_store_version_still_loads() {
    // The store has evolved v1 -> v4 by adding dataset kinds. Every one of those historical blobs
    // must still restore with the CURRENT reader; that is schema evolution as SYS-66 defines it.
    for (file, expected_version, expected_records) in [
        ("market_store_v1.store", 1, 2),
        ("market_store_v2.store", 2, 2),
        ("market_store_v3.store", 3, 2),
        ("market_store_v4.store", 4, 2),
    ] {
        let path = corpus().join(file);
        let before = fs::read(&path).expect("fixture readable");
        let serialized = String::from_utf8(before.clone()).expect("fixture is utf-8");

        let store = MarketDataStore::restore(&serialized).unwrap_or_else(|err| {
            panic!("{file} (v{expected_version}) must still restore: {err:?}")
        });
        assert_eq!(store.len(), expected_records, "{file}: record count");

        // The declared version is the third line (magic, checksum, version).
        let declared: i64 = serialized
            .lines()
            .nth(2)
            .expect("version line")
            .parse()
            .expect("version is an integer");
        assert_eq!(declared, expected_version, "{file}: declared version");

        // ...and reading it did not rewrite it. No bulk migration, not even a silent one.
        assert_eq!(fs::read(&path).unwrap(), before, "{file} must be unchanged");
    }
}

#[test]
fn srs_data_015_a_legacy_access_journal_is_still_read_in_place() {
    // A journal written before the line-schema tag existed. It must answer the same recency query it
    // always did, and must not be rewritten to do so.
    let dir = scratch("legacy-journal");
    let journal_dir = dir.join("access_journal");
    fs::create_dir_all(&journal_dir).unwrap();
    let src = corpus().join("access_journal_legacy.log");
    let before = fs::read(&src).unwrap();
    let dst = journal_dir.join("access_journal.log");
    fs::write(&dst, &before).unwrap();

    let journal = AccessJournal::under_ssd(&dir);
    let recent = journal
        .recent(86_400, 1_600_000_100, None)
        .expect("a legacy journal must remain readable");
    assert_eq!(recent.get("AAPL"), Some(&1_600_000_000));
    assert_eq!(recent.get("MSFT"), Some(&1_600_000_060));
    assert_eq!(fs::read(&dst).unwrap(), before, "journal must be unchanged");
}

#[test]
fn srs_data_015_a_legacy_journal_accepts_new_appends_and_both_forms_read() {
    // The realistic upgrade: an existing journal that a post-upgrade job appends to. Both line forms
    // then coexist in one file and both must be honoured — an append-only log cannot be partitioned
    // by schema version.
    let dir = scratch("mixed-journal");
    let journal_dir = dir.join("access_journal");
    fs::create_dir_all(&journal_dir).unwrap();
    fs::copy(
        corpus().join("access_journal_legacy.log"),
        journal_dir.join("access_journal.log"),
    )
    .unwrap();

    let journal = AccessJournal::under_ssd(&dir);
    let job = atp_data::JobRef::new(
        atp_data::JobKind::Backtest,
        atp_data::JobId::new("bt-after-upgrade").unwrap(),
    );
    assert!(journal.append(&job, "tsla", 1_600_000_090));

    let recent = journal.recent(86_400, 1_600_000_100, None).unwrap();
    assert_eq!(recent.get("AAPL"), Some(&1_600_000_000), "legacy line");
    assert_eq!(recent.get("TSLA"), Some(&1_600_000_090), "versioned line");
}

// --------------------------------------------------------------------------- //
// AC clause 1 — the registry is the enumeration, and it matches reality
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_015_every_registered_writer_exists_and_records_its_marker() {
    // The registry's claim is only worth what its binding to real source is worth: every registered
    // entity must name a file that exists and that actually contains the version marker.
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    for entity in PERSISTED_ENTITIES {
        let writer = root.join(entity.writer_path);
        assert!(
            writer.is_file(),
            "{}: writer {} does not exist",
            entity.entity_id,
            entity.writer_path
        );
        let source = fs::read_to_string(&writer).expect("writer source readable");
        assert!(
            source.contains(entity.marker),
            "{}: writer {} does not contain the version marker {}",
            entity.entity_id,
            entity.writer_path,
            entity.marker
        );
        if let Some(magic) = entity.magic {
            assert!(
                source.contains(magic),
                "{}: writer does not contain its magic {magic}",
                entity.entity_id
            );
        }
    }
}

#[test]
fn srs_data_015_an_unknown_future_store_version_is_refused_not_guessed() {
    // The forward half of the contract: a blob from a NEWER build must be refused cleanly rather than
    // parsed with this build's field expectations.
    let store = descriptor("market-data-store").unwrap();
    assert!(!supports_version(store, store.current_version + 1));

    let serialized = fs::read_to_string(corpus().join("market_store_v4.store")).unwrap();
    // Re-frame the v4 fixture as a "v5" blob, recomputing the checksum exactly as a real future
    // writer would — so the refusal under test is the VERSION gate, not the integrity check.
    // Layout: `<magic>\n<checksum>\n<version>\n<rest-of-body>`; the body is everything after the
    // second newline, and its first line is the version.
    let magic_end = serialized.find('\n').expect("magic line");
    let checksum_end = serialized[magic_end + 1..]
        .find('\n')
        .expect("checksum line")
        + magic_end
        + 1;
    let body = &serialized[checksum_end + 1..];
    let version_end = body.find('\n').expect("version line");
    assert_eq!(&body[..version_end], "4", "the v4 fixture must declare v4");
    let forged_body = format!("5{}", &body[version_end..]);
    let forged = format!(
        "{}\n{}\n{}",
        &serialized[..magic_end],
        fnv1a(forged_body.as_bytes()),
        forged_body
    );
    match MarketDataStore::restore(&forged) {
        Err(StoreError::UnknownSchemaVersion { found }) => assert_eq!(found, 5),
        Err(other) => panic!("a v5 blob must be refused at the VERSION gate, got {other:?}"),
        Ok(_) => panic!("a v5 blob must never restore on a v4 build"),
    }
}

/// FNV-1a, matching the store's own checksum so the forged blob above is realistic.
fn fnv1a(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

// --------------------------------------------------------------------------- //
// The operator inspection surface
// --------------------------------------------------------------------------- //

fn run_cli(args: &[&str]) -> (bool, String, String) {
    let output = Command::new(cli())
        .args(args)
        .output()
        .expect("data015_schema_cli must be built (cargo build -p atp-data)");
    (
        output.status.success(),
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    )
}

#[test]
fn srs_data_015_cli_report_lists_every_entity_with_its_version_range() {
    let (ok, stdout, stderr) = run_cli(&["report"]);
    assert!(ok, "report must succeed: {stderr}");
    assert!(stdout.contains(&format!("entities:{}", PERSISTED_ENTITIES.len())));
    for entity in PERSISTED_ENTITIES {
        assert!(
            stdout.contains(&format!("entity:{}", entity.entity_id)),
            "report omits {}",
            entity.entity_id
        );
    }
    // The report must state a version range for every entity — that IS the AC clause-1 evidence.
    let ranges = stdout.matches("reads_versions:").count();
    assert_eq!(ranges, PERSISTED_ENTITIES.len());
}

#[test]
fn srs_data_015_cli_inspect_reports_each_stores_declared_version() {
    let (ok, stdout, stderr) = run_cli(&["inspect", "--dir", corpus().to_str().unwrap()]);
    assert!(ok, "inspecting the golden corpus must succeed: {stderr}");
    // Each market-store fixture is identified by magic and its declared version read from the bytes.
    for version in 1..=4 {
        assert!(
            stdout.contains(&format!("declared_version:{version}")),
            "inspect did not report declared_version:{version}\n{stdout}"
        );
    }
    assert!(stdout.contains("entity:market-data-store"));
    assert!(stdout.contains("version_supported:yes"));
    assert!(stdout.contains("unsupported_version:0"));
}

#[test]
fn srs_data_015_cli_inspect_names_a_magic_less_file_only_when_told() {
    // Honesty: a format with no magic cannot be identified from bytes alone, so --dir reports it as
    // unidentified rather than attaching a version range that may not govern it.
    let legacy = corpus().join("access_journal_legacy.log");
    let (ok, stdout, _) = run_cli(&["inspect", "--file", legacy.to_str().unwrap()]);
    assert!(ok);
    assert!(stdout.contains("entity:unidentified"), "{stdout}");

    // Named explicitly, it is interpreted — and a version-less legacy payload is readable because
    // this entity accepts one.
    let (ok, stdout, _) = run_cli(&[
        "inspect",
        "--file",
        legacy.to_str().unwrap(),
        "--entity",
        "access-journal",
    ]);
    assert!(ok);
    assert!(stdout.contains("entity:access-journal"), "{stdout}");
    assert!(stdout.contains("declared_version:none(legacy)"), "{stdout}");
    assert!(stdout.contains("version_supported:yes"), "{stdout}");
}

#[test]
fn srs_data_015_cli_exits_non_zero_on_a_file_this_build_cannot_read() {
    // The pre-upgrade gate: a file from a newer build must make the command FAIL, so a script that
    // runs it before starting a service refuses to proceed.
    let dir = scratch("future-journal");
    let future = dir.join("future.log");
    fs::write(&future, b"v99\t1600000000\tbacktest\tbt-1\tAAPL\n").unwrap();
    let (ok, stdout, stderr) = run_cli(&[
        "inspect",
        "--file",
        future.to_str().unwrap(),
        "--entity",
        "access-journal",
    ]);
    assert!(!ok, "an unreadable file must exit non-zero");
    assert!(stdout.contains("declared_version:99"), "{stdout}");
    assert!(stdout.contains("version_supported:no"), "{stdout}");
    assert!(stdout.contains("unsupported_version:1"), "{stdout}");
    assert!(stderr.contains("cannot read"), "{stderr}");
}

// --------------------------------------------------------------------------- //
// Adversarial-review round 1: a PRESENT-but-unparseable version must never
// degrade to "legacy, readable". For an entity that accepts version-less
// payloads, `absent` means readable — so collapsing "malformed" into "absent"
// would walk a future or corrupt record straight past the pre-upgrade gate.
// --------------------------------------------------------------------------- //

/// Write `line` to a scratch file and inspect it AS `entity`. Returns (success, stdout).
fn inspect_line_as(tag: &str, entity: &str, line: &str) -> (bool, String) {
    let dir = scratch(tag);
    let path = dir.join("record.jsonl");
    fs::write(&path, line.as_bytes()).unwrap();
    let (ok, stdout, _) = run_cli(&[
        "inspect",
        "--file",
        path.to_str().unwrap(),
        "--entity",
        entity,
    ]);
    (ok, stdout)
}

#[test]
fn srs_data_015_a_future_version_is_caught_wherever_the_key_sits() {
    // A writer is free to order its JSON keys. A version found only when it happens to be FIRST is
    // a gate that a key reordering silently disables.
    for (tag, line) in [
        (
            "json-future-first",
            "{\"schema_version\":99,\"kind\":\"COOL_DOWN_ELAPSED\"}\n",
        ),
        (
            "json-future-middle",
            "{\"kind\":\"COOL_DOWN_ELAPSED\",\"schema_version\":99,\"rationale\":\"x\"}\n",
        ),
        (
            "json-future-last",
            "{\"kind\":\"COOL_DOWN_ELAPSED\",\"schema_version\":99}\n",
        ),
    ] {
        let (ok, stdout) = inspect_line_as(tag, "hot-swap-trigger-log", line);
        assert!(!ok, "{tag}: a v99 record must not pass the gate\n{stdout}");
        assert!(stdout.contains("declared_version:99"), "{tag}: {stdout}");
        assert!(stdout.contains("version_supported:no"), "{tag}: {stdout}");
    }
}

#[test]
fn srs_data_015_a_well_formed_but_out_of_range_version_is_reported_as_that_version() {
    // A negative version is a well-formed JSON integer, so it is reported as the version it is —
    // and refused because it falls outside the supported range. That distinction matters for the
    // operator: "declares v-1, unsupported" is actionable; "malformed" would misdescribe the file.
    let (ok, stdout) = inspect_line_as(
        "json-negative",
        "hot-swap-trigger-log",
        "{\"schema_version\":-1,\"kind\":\"X\"}\n",
    );
    assert!(!ok, "{stdout}");
    assert!(stdout.contains("declared_version:-1"), "{stdout}");
    assert!(stdout.contains("version_supported:no"), "{stdout}");
}

#[test]
fn srs_data_015_a_malformed_version_is_invalid_not_legacy() {
    // Each of these is a version field this build cannot interpret. None may be reported as a
    // version-less legacy payload, which for this entity would mean "readable".
    for (tag, line) in [
        ("json-float", "{\"schema_version\":1.5,\"kind\":\"X\"}\n"),
        ("json-string", "{\"schema_version\":\"1\",\"kind\":\"X\"}\n"),
        ("json-bool", "{\"schema_version\":true,\"kind\":\"X\"}\n"),
        ("json-null", "{\"schema_version\":null,\"kind\":\"X\"}\n"),
        ("json-plus", "{\"schema_version\":+1,\"kind\":\"X\"}\n"),
        ("json-truncated", "{\"schema_version\":\n"),
    ] {
        let (ok, stdout) = inspect_line_as(tag, "hot-swap-trigger-log", line);
        assert!(!ok, "{tag}: must not pass the gate\n{stdout}");
        assert!(
            stdout.contains("declared_version:invalid"),
            "{tag} must be INVALID, not legacy: {stdout}"
        );
        assert!(stdout.contains("version_supported:no"), "{tag}: {stdout}");
    }
}

#[test]
fn srs_data_015_a_version_shaped_string_value_cannot_spoof_a_version() {
    // The `rationale` field is operator-supplied text. If the scan were a substring search, this
    // legacy record would report version 99 and be refused — a false alarm on readable data.
    let line = "{\"kind\":\"COOL_DOWN_ELAPSED\",\"rationale\":\"contains \\\"schema_version\\\":99 verbatim\"}\n";
    let (ok, stdout) = inspect_line_as("json-spoof", "hot-swap-trigger-log", line);
    assert!(ok, "a genuinely legacy record must stay readable\n{stdout}");
    assert!(stdout.contains("declared_version:none(legacy)"), "{stdout}");
    assert!(stdout.contains("version_supported:yes"), "{stdout}");
}

#[test]
fn srs_data_015_a_nested_version_is_not_read_as_the_top_level_one() {
    // A `schema_version` inside a nested object describes that sub-object, not the record.
    let line = "{\"kind\":\"X\",\"payload\":{\"schema_version\":99}}\n";
    let (ok, stdout) = inspect_line_as("json-nested", "hot-swap-trigger-log", line);
    assert!(ok, "{stdout}");
    assert!(stdout.contains("declared_version:none(legacy)"), "{stdout}");
}

#[test]
fn srs_data_015_a_malformed_journal_version_tag_is_invalid_not_legacy() {
    // The same rule for the access journal's `v<N>` tag form.
    for (tag, line) in [
        ("tag-float", "v1.5\t1600000000\tbacktest\tbt-1\tAAPL\n"),
        ("tag-alpha", "vX\t1600000000\tbacktest\tbt-1\tAAPL\n"),
        ("tag-empty", "v\t1600000000\tbacktest\tbt-1\tAAPL\n"),
        ("tag-no-field", "v1\n"),
    ] {
        let (ok, stdout) = inspect_line_as(tag, "access-journal", line);
        assert!(!ok, "{tag}: must not pass the gate\n{stdout}");
        assert!(
            stdout.contains("declared_version:invalid"),
            "{tag} must be INVALID, not legacy: {stdout}"
        );
    }
}

#[test]
fn srs_data_015_corrupt_bytes_are_never_readable_for_any_magic_less_entity() {
    // Adversarial-review round 2: a shared "neither a v-tag nor a JSON object → Absent" fallback
    // reported arbitrary garbage as a readable LEGACY payload for every entity that accepts
    // version-less data. The real readers reject these bytes outright, so a gate that passes them
    // is worse than no gate — it is false assurance before an upgrade.
    for entity in [
        "hot-swap-trigger-log",
        "system-log-segment",
        "readiness-alert-sink",
        "kill-switch-last-activation",
        "access-journal",
    ] {
        for (kind, line) in [
            ("garbage", "this is not a record at all\n"),
            ("html", "<html><body>oops</body></html>\n"),
            ("truncated-json", "{\"kind\":\"X\"\n"),
            ("array-not-object", "[1,2,3]\n"),
        ] {
            let (ok, stdout) = inspect_line_as(&format!("junk-{entity}-{kind}"), entity, line);
            assert!(
                !ok,
                "{entity}/{kind}: corrupt bytes must fail the gate\n{stdout}"
            );
            assert!(
                stdout.contains("version_supported:no"),
                "{entity}/{kind}: must be version_supported:no\n{stdout}"
            );
        }
    }
}

#[test]
fn srs_data_015_a_record_less_file_declares_nothing_and_hides_nothing() {
    // An empty or whitespace-only log is not corruption: it holds no records, so there is nothing
    // this build cannot read. The real readers agree (access_journal::complete_lines and
    // JsonlLogStore both skip blank lines), and reporting it unreadable would block an upgrade over
    // a log that simply has not been written to yet.
    for (tag, content) in [("blank", ""), ("whitespace", "   \n"), ("newlines", "\n\n")] {
        let (ok, stdout) =
            inspect_line_as(&format!("empty-{tag}"), "hot-swap-trigger-log", content);
        assert!(ok, "{tag}: an empty log must not fail the gate\n{stdout}");
        assert!(
            stdout.contains("declared_version:none(legacy)"),
            "{tag}: {stdout}"
        );
    }
}

#[test]
fn srs_data_015_a_journal_line_of_the_wrong_shape_is_not_legacy() {
    // "Legacy" for the access journal means a line the real reader accepts, not merely one without
    // a version tag. Otherwise `version_supported:yes` would over-claim.
    for (tag, line) in [
        ("too-few-fields", "1600000000\tbacktest\tbt-1\n"),
        (
            "too-many-fields",
            "1600000000\tbacktest\tbt-1\tAAPL\textra\n",
        ),
        ("non-integer-ts", "not-a-ts\tbacktest\tbt-1\tAAPL\n"),
        ("unknown-job-kind", "1600000000\tlive-trade\tbt-1\tAAPL\n"),
        ("empty-symbol", "1600000000\tbacktest\tbt-1\t\n"),
    ] {
        let (ok, stdout) = inspect_line_as(tag, "access-journal", line);
        assert!(!ok, "{tag}: must fail the gate\n{stdout}");
        assert!(stdout.contains("version_supported:no"), "{tag}: {stdout}");
    }
}

#[test]
fn srs_data_015_the_vault_envelope_version_is_read_under_its_own_key() {
    // The config vault predates SRS-DATA-015 and has always recorded `version`, not
    // `schema_version`. Reading it with the wrong key would report a properly-versioned envelope as
    // version-less — and since the vault does NOT accept version-less payloads, as unreadable.
    let (ok, stdout) = inspect_line_as(
        "vault-current",
        "config-vault-envelope",
        "{\"kdf\":\"raw\",\"token\":\"x\",\"version\":1}\n",
    );
    assert!(
        ok,
        "a current vault envelope must read as supported\n{stdout}"
    );
    assert!(stdout.contains("declared_version:1"), "{stdout}");
    assert!(stdout.contains("version_supported:yes"), "{stdout}");

    // ...and a future envelope is refused.
    let (ok, stdout) = inspect_line_as(
        "vault-future",
        "config-vault-envelope",
        "{\"kdf\":\"raw\",\"token\":\"x\",\"version\":99}\n",
    );
    assert!(!ok, "{stdout}");
    assert!(stdout.contains("declared_version:99"), "{stdout}");
    assert!(stdout.contains("version_supported:no"), "{stdout}");
}

#[test]
fn srs_data_015_malformation_after_a_valid_version_still_fails_the_gate() {
    // Adversarial-review round 3: the scanner returned as soon as the key matched, so a torn line
    // whose remaining fields never arrived reported a supported version and passed as readable.
    // The whole object must validate before any version is believed.
    for (tag, line) in [
        ("truncated-after-version", "{\"schema_version\":1,\n"),
        ("unterminated-key", "{\"schema_version\":1,\"kind\"\n"),
        ("empty-value", "{\"schema_version\":1,\"kind\":}\n"),
        ("no-closing-brace", "{\"schema_version\":1\n"),
        ("trailing-junk", "{\"schema_version\":1}trailing\n"),
        (
            "two-objects",
            "{\"schema_version\":1} {\"schema_version\":1}\n",
        ),
        (
            "duplicate-key",
            "{\"schema_version\":1,\"schema_version\":1}\n",
        ),
    ] {
        let (ok, stdout) = inspect_line_as(tag, "hot-swap-trigger-log", line);
        assert!(!ok, "{tag}: malformed bytes must fail the gate\n{stdout}");
        assert!(
            stdout.contains("declared_version:invalid"),
            "{tag}: must be INVALID even though a version parsed early: {stdout}"
        );
        assert!(stdout.contains("version_supported:no"), "{tag}: {stdout}");
    }
}

#[test]
fn srs_data_015_a_genuine_legacy_journal_line_is_still_readable() {
    // The other direction: the strictness must not start refusing real legacy data.
    let (ok, stdout) = inspect_line_as(
        "tag-legacy",
        "access-journal",
        "1600000000\tbacktest\tbt-1\tAAPL\n",
    );
    assert!(ok, "{stdout}");
    assert!(stdout.contains("declared_version:none(legacy)"), "{stdout}");
    assert!(stdout.contains("version_supported:yes"), "{stdout}");
}

#[test]
fn srs_data_015_cli_rejects_an_unknown_entity_and_a_stray_flag() {
    // Fail-closed argument handling, matching the DATA-010 CLI: an unknown entity or flag is an
    // error, never a silently-ignored argument that makes the report mean something else.
    let (ok, _, stderr) = run_cli(&["inspect", "--file", "/tmp/x", "--entity", "no-such-entity"]);
    assert!(!ok);
    assert!(stderr.contains("unknown entity id"), "{stderr}");

    let (ok, _, stderr) = run_cli(&["inspect", "--dir", "/tmp", "--bogus", "1"]);
    assert!(!ok);
    assert!(stderr.contains("unknown flag"), "{stderr}");

    let (ok, _, stderr) = run_cli(&["report", "--dir", "/tmp"]);
    assert!(!ok);
    assert!(stderr.contains("takes no arguments"), "{stderr}");
}

#[test]
fn srs_data_015_cli_requires_exactly_one_of_dir_or_file() {
    let (ok, _, stderr) = run_cli(&["inspect"]);
    assert!(!ok);
    assert!(
        stderr.contains("exactly one of --dir or --file"),
        "{stderr}"
    );

    let (ok, _, stderr) = run_cli(&[
        "inspect",
        "--dir",
        "/tmp",
        "--file",
        corpus().join("market_store_v1.store").to_str().unwrap(),
    ]);
    assert!(!ok, "both --dir and --file must be refused");
    assert!(
        stderr.contains("exactly one of --dir or --file"),
        "{stderr}"
    );
}

#[test]
fn srs_data_015_inspecting_the_corpus_leaves_every_byte_untouched() {
    // The inspection surface is READ-ONLY. Proving old data is readable must not involve rewriting
    // it — if it did, "queryable without bulk migration" would be false by construction.
    let before: Vec<(PathBuf, Vec<u8>)> = fs::read_dir(corpus())
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.is_file())
        .map(|p| {
            let bytes = fs::read(&p).unwrap();
            (p, bytes)
        })
        .collect();
    assert!(before.len() >= 8, "the corpus must not have shrunk");

    let (ok, _, stderr) = run_cli(&["inspect", "--dir", corpus().to_str().unwrap()]);
    assert!(ok, "{stderr}");

    for (path, bytes) in before {
        assert_eq!(
            fs::read(&path).unwrap(),
            bytes,
            "{} was modified by inspection",
            path.display()
        );
    }
}

#[test]
fn srs_data_015_inspect_survives_an_unreadable_or_binary_file() {
    // A deployment directory holds whatever it holds. Inspection must classify what it can and report
    // the rest as unidentified — never abort, and never claim a file it could not parse.
    let dir = scratch("junk");
    fs::write(dir.join("empty.bin"), b"").unwrap();
    fs::write(dir.join("binary.bin"), [0x00u8, 0xff, 0xfe, 0x01]).unwrap();
    fs::create_dir_all(dir.join("nested")).unwrap();
    fs::copy(
        corpus().join("market_store_v1.store"),
        dir.join("nested/market_data.store"),
    )
    .unwrap();

    let (ok, stdout, stderr) = run_cli(&["inspect", "--dir", dir.to_str().unwrap()]);
    assert!(ok, "{stderr}");
    assert!(stdout.contains("inspected:3"), "{stdout}");
    assert!(stdout.contains("unidentified:2"), "{stdout}");
    // ...and the nested real store is still found and read.
    assert!(stdout.contains("entity:market-data-store"), "{stdout}");
    assert!(stdout.contains("declared_version:1"), "{stdout}");
}

#[test]
fn srs_data_015_inspect_rejects_a_missing_directory() {
    let (ok, _, stderr) = run_cli(&["inspect", "--dir", "/nonexistent/atp-data015"]);
    assert!(!ok);
    assert!(stderr.contains("not a directory"), "{stderr}");
}

#[test]
fn srs_data_015_the_corpus_covers_every_retrofitted_entity() {
    // Guard against the corpus quietly losing a case: every entity that accepts a version-less
    // payload must have a legacy fixture proving it, or the "no bulk migration" claim is untested
    // for that entity.
    let files: Vec<String> = fs::read_dir(corpus())
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    for entity in PERSISTED_ENTITIES.iter().filter(|e| e.legacy_unversioned) {
        let stem = entity.entity_id.replace('-', "_");
        assert!(
            files
                .iter()
                .any(|f| f.contains("legacy") && f.contains(&stem_hint(&stem))),
            "no legacy fixture for {} (files: {files:?})",
            entity.entity_id
        );
    }
}

/// The fixture-name fragment for an entity id (the corpus names files after the artefact, not the
/// registry key, so `kill-switch-last-activation` maps to `kill_switch_last_activation`).
fn stem_hint(stem: &str) -> String {
    match stem {
        "system_log_segment" => "system_log_segment".to_string(),
        other => other.to_string(),
    }
}

#[test]
fn srs_data_015_market_store_fixtures_are_the_declared_versions() {
    // Belt-and-braces on the corpus itself: a fixture whose name says v2 but whose bytes say v3 would
    // make every assertion above meaningless.
    for (file, version) in [
        ("market_store_v1.store", "1"),
        ("market_store_v2.store", "2"),
        ("market_store_v3.store", "3"),
        ("market_store_v4.store", "4"),
    ] {
        let text = fs::read_to_string(corpus().join(file)).unwrap();
        let declared = text.lines().nth(2).unwrap();
        assert_eq!(declared, version, "{file} declares the wrong version");
    }
}

#[test]
fn srs_data_015_corpus_path_is_stable() {
    assert!(
        corpus().is_dir(),
        "golden corpus missing at {}",
        corpus().display()
    );
    assert!(
        corpus().join("README.md").is_file(),
        "corpus README missing"
    );
}

#[test]
fn srs_data_015_a_future_market_store_version_is_reported_and_refused() {
    // Adversarial-review round 6: the version was previously located by scanning forward for "the
    // first plausible-looking integer". A real future store declaring v99 had its version line
    // skipped as implausible and the RECORD COUNT line read as the version instead — so the file
    // was reported readable at whatever its record count happened to be, and the pre-upgrade gate
    // exited zero on a store this build cannot restore.
    let serialized = fs::read_to_string(corpus().join("market_store_v4.store")).unwrap();
    let magic_end = serialized.find('\n').expect("magic line");
    let checksum_end = serialized[magic_end + 1..]
        .find('\n')
        .expect("checksum line")
        + magic_end
        + 1;
    let body = &serialized[checksum_end + 1..];
    let version_end = body.find('\n').expect("version line");
    // The record count follows the version, and is deliberately a small, "plausible" number — it is
    // exactly what the old scan latched onto.
    let record_count: i64 = body[version_end + 1..]
        .lines()
        .next()
        .and_then(|line| line.trim().parse().ok())
        .expect("record count line");
    assert!(
        (1..=5).contains(&record_count),
        "the fixture's record count must be small enough to look like a version"
    );

    let forged_body = format!("99{}", &body[version_end..]);
    let forged = format!(
        "{}\n{}\n{}",
        &serialized[..magic_end],
        fnv1a(forged_body.as_bytes()),
        forged_body
    );
    let dir = scratch("future-store");
    let path = dir.join("market_data.store");
    fs::write(&path, forged.as_bytes()).unwrap();

    // The real reader refuses it...
    assert!(
        MarketDataStore::restore(&forged).is_err(),
        "a v99 store must not restore on this build"
    );
    // ...and so must the gate, reporting the version the file ACTUALLY declares.
    let (ok, stdout, stderr) = run_cli(&["inspect", "--file", path.to_str().unwrap()]);
    assert!(!ok, "a v99 store must make inspect exit non-zero\n{stdout}");
    assert!(
        stdout.contains("declared_version:99"),
        "must report the declared version, not a body field: {stdout}"
    );
    assert!(stdout.contains("version_supported:no"), "{stdout}");
    assert!(stderr.contains("cannot read"), "{stderr}");
}

#[test]
fn srs_data_015_a_store_with_a_non_integer_version_line_is_invalid() {
    // The version line must be a version. A corrupt header is not an occasion to go looking
    // elsewhere in the file for something that parses.
    let serialized = fs::read_to_string(corpus().join("market_store_v1.store")).unwrap();
    let mut lines: Vec<String> = serialized.lines().map(str::to_string).collect();
    lines[2] = "not-a-version".to_string();
    let dir = scratch("bad-version-line");
    let path = dir.join("market_data.store");
    fs::write(&path, lines.join("\n") + "\n").unwrap();

    let (ok, stdout, _) = run_cli(&["inspect", "--file", path.to_str().unwrap()]);
    assert!(!ok, "{stdout}");
    assert!(stdout.contains("declared_version:invalid"), "{stdout}");
    assert!(stdout.contains("version_supported:no"), "{stdout}");
}

#[test]
fn srs_data_015_a_later_record_with_a_future_version_fails_the_whole_file() {
    // Adversarial-review round 8: only the FIRST line was inspected, so an append-only log whose
    // first record is legacy or current but whose later record came from a newer build passed the
    // gate. The real readers fail closed over EVERY complete record, so the gate must too — an
    // operator who ran `inspect` and proceeded would hit that record at runtime.
    let dir = scratch("later-future-record");

    // Access journal: legacy first line, future-version line third.
    let journal = dir.join("access_journal.log");
    fs::write(
        &journal,
        "1600000000\tbacktest\tbt-1\tAAPL\n\
         v1\t1600000060\tbacktest\tbt-2\tMSFT\n\
         v99\t1600000120\tbacktest\tbt-3\tTSLA\n",
    )
    .unwrap();
    let (ok, stdout, stderr) = run_cli(&[
        "inspect",
        "--file",
        journal.to_str().unwrap(),
        "--entity",
        "access-journal",
    ]);
    assert!(!ok, "a later v99 record must fail the whole file\n{stdout}");
    assert!(stdout.contains("declared_version:99"), "{stdout}");
    assert!(stdout.contains("version_supported:no"), "{stdout}");
    assert!(stderr.contains("cannot read"), "{stderr}");

    // JSONL log: valid first record, corrupt second.
    let jsonl = dir.join("triggers.jsonl");
    fs::write(
        &jsonl,
        "{\"schema_version\":1,\"kind\":\"MANUAL_PROMOTION\"}\n\
         {\"schema_version\":1,\"kind\":}\n",
    )
    .unwrap();
    let (ok, stdout, _) = run_cli(&[
        "inspect",
        "--file",
        jsonl.to_str().unwrap(),
        "--entity",
        "hot-swap-trigger-log",
    ]);
    assert!(
        !ok,
        "a later corrupt record must fail the whole file\n{stdout}"
    );
    assert!(stdout.contains("declared_version:invalid"), "{stdout}");
    assert!(stdout.contains("version_supported:no"), "{stdout}");
}

#[test]
fn srs_data_015_a_torn_final_record_does_not_condemn_the_file() {
    // The other direction: a crash mid-append leaves a fragment with no terminating newline. The
    // real readers drop it as a torn tail rather than treating it as corruption, and so must the
    // gate — otherwise every crashed writer would block the next upgrade.
    let dir = scratch("torn-tail");
    let journal = dir.join("access_journal.log");
    fs::write(
        &journal,
        "v1\t1600000000\tbacktest\tbt-1\tAAPL\n\
         v99\t1600000060\tbacktest\tbt-2",
    )
    .unwrap();
    let (ok, stdout, _) = run_cli(&[
        "inspect",
        "--file",
        journal.to_str().unwrap(),
        "--entity",
        "access-journal",
    ]);
    assert!(ok, "a torn tail must not fail the gate\n{stdout}");
    assert!(stdout.contains("declared_version:1"), "{stdout}");
    assert!(stdout.contains("version_supported:yes"), "{stdout}");
}

#[test]
fn srs_data_015_version_supported_is_a_claim_about_the_version_only() {
    // Adversarial-review round 12 asked this gate to validate every record BODY. It deliberately
    // does not: doing so would mean re-implementing four other features' record schemas inside the
    // data layer, three of them in Python and one in a crate atp-data must not depend on. Stale
    // copies of someone else's invariants inside a GATE are worse than none.
    //
    // What the tool must therefore do is be precise about its claim. This test pins that precision:
    // a record with a supported VERSION but an empty body reports version_supported:yes and exits
    // zero — and the field is named for exactly that, not "readable".
    let (ok, stdout) = inspect_line_as(
        "version-only-claim",
        "hot-swap-trigger-log",
        "{\"schema_version\":1}\n",
    );
    assert!(ok, "{stdout}");
    assert!(stdout.contains("declared_version:1"), "{stdout}");
    assert!(
        stdout.contains("version_supported:yes"),
        "the claim is about the declared version: {stdout}"
    );
    assert!(
        !stdout.contains("readable:"),
        "the output must not use the broader word 'readable': {stdout}"
    );

    // ...and the owning reader IS the body gate. The same bytes are refused there.
    let owner_cli = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../target/debug/resv003_hot_swap_trigger_cli");
    if owner_cli.exists() {
        let dir = scratch("version-only-owner");
        let log = dir.join("triggers.jsonl");
        fs::write(&log, b"{\"schema_version\":1}\n").unwrap();
        let output = Command::new(&owner_cli)
            .args(["manual", "--demoting", "a", "--candidate", "b", "--log"])
            .arg(&log)
            .output()
            .expect("run resv003_hot_swap_trigger_cli");
        assert!(
            !output.status.success(),
            "the OWNING reader must refuse a body-less record, so the boundary is covered somewhere"
        );
    }
}
