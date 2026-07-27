//! SRS-DATA-015 on the SRS-RESV-003 trigger log: the audit reader must not count a record it
//! cannot actually parse.
//!
//! `count_log_records` is the evidence the CLI's fail-closed check reads ("all swap triggers are
//! logged"). Counting a line whose declared schema version this build cannot interpret would
//! manufacture that evidence — the command would report the trigger as durably logged on the
//! strength of bytes it does not understand.
//!
//! Adversarial-review finding (round 1): the first implementation matched the version key only as a
//! leading prefix and accepted any leading digits, so a reordered key, a float, or a future version
//! could all be waved through as "legacy". These tests pin the corrected three-state rule:
//!
//! * key genuinely ABSENT      → legacy, counted (an existing log stays readable — SRS-DATA-015's
//!                               "no bulk migration");
//! * key present and VALID     → counted iff in the supported range;
//! * key present and MALFORMED → hard error, never silently downgraded to legacy.

use std::fs;
use std::path::PathBuf;
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_resv003_hot_swap_trigger_cli");

fn log_dir(tag: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("resv003-schema-{tag}"))
}

/// A scratch log seeded with `seed`, then appended to by a real `manual` invocation.
fn run_manual_over_seeded_log(tag: &str, seed: &str) -> (bool, String) {
    let dir = log_dir(tag);
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).expect("scratch dir");
    let log = dir.join("triggers.jsonl");
    fs::write(&log, seed.as_bytes()).expect("seed log");

    let output = Command::new(BIN)
        .args([
            "manual",
            "--demoting",
            "live-a",
            "--candidate",
            "cand-b",
            "--log",
        ])
        .arg(&log)
        .output()
        .expect("run resv003_hot_swap_trigger_cli");
    (
        output.status.success(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    )
}

#[test]
fn resv_3_a_legacy_version_less_log_still_accepts_appends() {
    // The upgrade path: a log written before SRS-DATA-015 has no schema_version key. It must stay
    // usable — refusing it would strand every existing audit trail.
    let (ok, stderr) = run_manual_over_seeded_log(
        "legacy",
        "{\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"a\",\
         \"candidate_strategy_id\":\"b\",\"rationale\":\"x\",\"observed_at_seconds\":1}\n",
    );
    assert!(ok, "a legacy log must remain appendable: {stderr}");
}

#[test]
fn resv_3_a_future_version_line_is_refused_wherever_the_key_sits() {
    // A record from a NEWER build. Found regardless of key order — a gate that only looks at the
    // first key is disabled by a writer that reorders them.
    for (tag, seed) in [
        ("future-first", "{\"schema_version\":99,\"kind\":\"X\"}\n"),
        (
            "future-middle",
            "{\"kind\":\"X\",\"schema_version\":99,\"rationale\":\"y\"}\n",
        ),
        ("future-last", "{\"kind\":\"X\",\"schema_version\":99}\n"),
    ] {
        let (ok, stderr) = run_manual_over_seeded_log(tag, seed);
        assert!(
            !ok,
            "{tag}: a v99 record must not be counted as valid evidence"
        );
        assert!(
            stderr.contains("cannot read"),
            "{tag}: expected a read refusal, got: {stderr}"
        );
    }
}

#[test]
fn resv_3_a_malformed_version_is_an_error_not_a_legacy_line() {
    // Each of these has a version field this build cannot interpret. Treating any of them as
    // "legacy" would count an unparseable record as proof the trigger was logged.
    for (tag, seed) in [
        ("float", "{\"schema_version\":1.5,\"kind\":\"X\"}\n"),
        ("string", "{\"schema_version\":\"1\",\"kind\":\"X\"}\n"),
        ("bool", "{\"schema_version\":true,\"kind\":\"X\"}\n"),
        ("null", "{\"schema_version\":null,\"kind\":\"X\"}\n"),
        ("negative", "{\"schema_version\":-1,\"kind\":\"X\"}\n"),
        ("not-json", "this is not a json object at all\n"),
    ] {
        let (ok, stderr) = run_manual_over_seeded_log(tag, seed);
        assert!(!ok, "{tag}: must be refused, not counted");
        assert!(
            stderr.contains("cannot read"),
            "{tag}: expected a read refusal, got: {stderr}"
        );
    }
}

#[test]
fn resv_3_a_version_shaped_string_value_cannot_spoof_a_version() {
    // `rationale` is operator-supplied text. A substring search would read version 99 out of it and
    // refuse a perfectly good legacy record — a false alarm in the opposite direction.
    let (ok, stderr) = run_manual_over_seeded_log(
        "spoof",
        "{\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\
         \"rationale\":\"note: \\\"schema_version\\\":99 appears here\",\"observed_at_seconds\":1}\n",
    );
    assert!(
        ok,
        "a version-shaped string VALUE must not be read as the record's version: {stderr}"
    );
}

#[test]
fn resv_3_a_nested_version_is_not_the_records_version() {
    let (ok, stderr) = run_manual_over_seeded_log(
        "nested",
        "{\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\
         \"rationale\":\"r\",\"observed_at_seconds\":1,\"payload\":{\"schema_version\":99}}\n",
    );
    assert!(
        ok,
        "a nested schema_version describes the sub-object: {stderr}"
    );
}

#[test]
fn resv_3_the_current_writer_stamps_a_readable_version() {
    // Round-trip: what this build writes, this build reads back on the next append.
    let (ok, stderr) = run_manual_over_seeded_log("roundtrip", "");
    assert!(ok, "{stderr}");

    let contents =
        fs::read_to_string(log_dir("roundtrip").join("triggers.jsonl")).expect("log written");
    assert!(
        contents.starts_with("{\"schema_version\":1,"),
        "every written line records its schema version: {contents}"
    );
}

#[test]
fn resv_3_malformation_after_a_valid_version_is_still_refused() {
    // Adversarial-review round 3: returning the version the moment the key matched let a torn
    // record be counted as valid audit evidence. The whole object must validate first.
    for (tag, seed) in [
        ("truncated", "{\"schema_version\":1,\n"),
        ("no-closing-brace", "{\"schema_version\":1\n"),
        ("trailing-junk", "{\"schema_version\":1}trailing\n"),
        (
            "duplicate-key",
            "{\"schema_version\":1,\"schema_version\":1}\n",
        ),
        ("empty-value", "{\"schema_version\":1,\"kind\":}\n"),
    ] {
        let (ok, stderr) = run_manual_over_seeded_log(tag, seed);
        assert!(
            !ok,
            "{tag}: a malformed record must not be counted as logged evidence"
        );
        assert!(
            stderr.contains("cannot read"),
            "{tag}: expected a read refusal, got: {stderr}"
        );
    }
}

#[test]
fn resv_3_a_supported_version_alone_is_not_a_trigger_record() {
    // Adversarial-review round 4: a supported schema version says the reader understands the
    // record's LAYOUT, not that the record has any content. `{"schema_version":1}` declares a
    // layout this build knows and carries no trigger at all — counting it would let an empty
    // object stand in for durable proof that a Hot-Swap trigger was logged.
    let (ok, stderr) = run_manual_over_seeded_log("empty-v1", "{\"schema_version\":1}\n");
    assert!(!ok, "an empty v1 object must not count as a logged trigger");
    assert!(stderr.contains("cannot read"), "{stderr}");
}

#[test]
fn resv_3_a_record_missing_any_required_field_is_refused() {
    // Each seed drops exactly one field from an otherwise-valid v1 record.
    let full = [
        ("schema_version", "1"),
        ("kind", "\"MANUAL_PROMOTION\""),
        ("demoting_strategy_id", "\"live-a\""),
        ("candidate_strategy_id", "\"cand-b\""),
        ("rationale", "\"manual\""),
        ("observed_at_seconds", "1715000000"),
    ];
    for dropped in [
        "kind",
        "demoting_strategy_id",
        "candidate_strategy_id",
        "rationale",
        "observed_at_seconds",
    ] {
        let body: Vec<String> = full
            .iter()
            .filter(|(key, _)| *key != dropped)
            .map(|(key, value)| format!("\"{key}\":{value}"))
            .collect();
        let seed = format!("{{{}}}\n", body.join(","));
        let (ok, stderr) = run_manual_over_seeded_log(&format!("missing-{dropped}"), &seed);
        assert!(!ok, "a record missing '{dropped}' must be refused");
        assert!(stderr.contains("cannot read"), "{dropped}: {stderr}");
    }
}

#[test]
fn resv_3_a_record_with_a_malformed_required_field_is_refused() {
    for (tag, seed) in [
        (
            "unknown-kind",
            "{\"schema_version\":1,\"kind\":\"NOT_A_TRIGGER\",\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\"rationale\":\"r\",\"observed_at_seconds\":1}\n",
        ),
        (
            "kind-not-a-string",
            "{\"schema_version\":1,\"kind\":7,\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\"rationale\":\"r\",\"observed_at_seconds\":1}\n",
        ),
        (
            "empty-strategy-id",
            "{\"schema_version\":1,\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"\",\"candidate_strategy_id\":\"b\",\"rationale\":\"r\",\"observed_at_seconds\":1}\n",
        ),
        (
            "non-integer-timestamp",
            "{\"schema_version\":1,\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\"rationale\":\"r\",\"observed_at_seconds\":\"soon\"}\n",
        ),
        (
            "negative-timestamp",
            "{\"schema_version\":1,\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\"rationale\":\"r\",\"observed_at_seconds\":-1}\n",
        ),
    ] {
        let (ok, stderr) = run_manual_over_seeded_log(tag, seed);
        assert!(!ok, "{tag}: must be refused");
        assert!(stderr.contains("cannot read"), "{tag}: {stderr}");
    }
}

#[test]
fn resv_3_a_complete_legacy_record_is_still_accepted() {
    // The strictness must not start refusing real pre-SRS-DATA-015 records: same body, no version.
    let (ok, stderr) = run_manual_over_seeded_log(
        "complete-legacy",
        "{\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"live-a\",\"candidate_strategy_id\":\"cand-b\",\"rationale\":\"manual\",\"observed_at_seconds\":1715000000}\n",
    );
    assert!(
        ok,
        "a complete legacy record must remain valid evidence: {stderr}"
    );
}

#[test]
fn resv_3_a_corrupt_nested_field_disqualifies_an_otherwise_valid_record() {
    // Adversarial-review round 7: every REQUIRED top-level field is present and well-formed here,
    // so a scanner that merely balanced delimiters counted the record as durable evidence. A record
    // carrying corrupt bytes is not a record this build can claim to have read.
    for (tag, seed) in [
        (
            "nested-garbage",
            "{\"schema_version\":1,\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\"rationale\":\"r\",\"observed_at_seconds\":1,\"payload\":{bad}}\n",
        ),
        (
            "nested-missing-value",
            "{\"schema_version\":1,\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\"rationale\":\"r\",\"observed_at_seconds\":1,\"payload\":{\"k\":}}\n",
        ),
        (
            "nested-trailing-comma",
            "{\"schema_version\":1,\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\"rationale\":\"r\",\"observed_at_seconds\":1,\"list\":[1,]}\n",
        ),
    ] {
        let (ok, stderr) = run_manual_over_seeded_log(tag, seed);
        assert!(!ok, "{tag}: a corrupt sub-object must disqualify the record");
        assert!(stderr.contains("cannot read"), "{tag}: {stderr}");
    }

    // ...while a well-formed extra sub-object does NOT disqualify it.
    let (ok, stderr) = run_manual_over_seeded_log(
        "nested-valid",
        "{\"schema_version\":1,\"kind\":\"MANUAL_PROMOTION\",\"demoting_strategy_id\":\"a\",\"candidate_strategy_id\":\"b\",\"rationale\":\"r\",\"observed_at_seconds\":1,\"payload\":{\"k\":[1,2]}}\n",
    );
    assert!(ok, "well-formed nesting must remain readable: {stderr}");
}

#[test]
fn resv_3_control_characters_in_an_id_do_not_poison_the_log() {
    // Adversarial-review round 10: the writer hand-escapes JSON and previously covered only the
    // five named escapes, while the reader (correctly) refuses raw control characters inside a
    // string. An operator id carrying a backspace or form feed therefore produced a durable record
    // that this build's own reader rejected — a poisoned audit log, and a direct violation of
    // SRS-DATA-015's promise that data this build writes stays queryable by it.
    //
    // Each id below is appended by a real `manual` run; the SECOND run over the same log re-reads
    // and counts what the first wrote, so a round-trip failure surfaces as a non-zero exit.
    // (A NUL byte is excluded: the OS refuses it in argv, so it cannot reach the writer through
    // this surface at all.)
    for (tag, id) in [
        ("backspace", "live-\u{08}a"),
        ("form-feed", "live-\u{0c}a"),
        ("unit-separator", "live-\u{1f}a"),
        ("bell", "live-\u{07}a"),
        ("newline", "live-\na"),
        ("quote", "live-\"a"),
        ("backslash", "live-\\a"),
    ] {
        let dir = log_dir(&format!("control-{tag}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("scratch dir");
        let log = dir.join("triggers.jsonl");

        for pass in 0..2 {
            let output = Command::new(BIN)
                .args(["manual", "--demoting", id, "--candidate", "cand-b", "--log"])
                .arg(&log)
                .output()
                .expect("run resv003_hot_swap_trigger_cli");
            assert!(
                output.status.success(),
                "{tag} pass {pass}: a control character must not poison the log: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }

        // Both records are present and readable — the count is the evidence the CLI checks.
        let contents = fs::read_to_string(&log).expect("log readable");
        assert_eq!(
            contents.lines().filter(|l| !l.trim().is_empty()).count(),
            2,
            "{tag}: both appends must have landed"
        );
        // ...and no raw control byte reached the file.
        assert!(
            !contents.bytes().any(|b| b < 0x20 && b != b'\n'),
            "{tag}: a raw control byte was written into the log"
        );
    }
}
