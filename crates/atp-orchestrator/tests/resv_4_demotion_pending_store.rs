//! SRS-RESV-004 / SyRS SYS-49c (c) and (d) — the durable demotion-pending lockout.
//!
//! The lockout is what makes "promotion is blocked **until the operator manually
//! resolves** the unfilled positions" true across a retry, a restart, or a second
//! operator surface. These are its fail-closed properties.
//!
//! L7 domain (safety) suite. Post-conditions:
//!   * Round trip: what is engaged is what is read back, including the two
//!     recovery-critical side-effect outcomes.
//!   * Three-state read: absent → `Clear`, readable → `Pending`, unreadable → the
//!     distinct `Unreadable` state — and `Unreadable` blocks promotion exactly like
//!     `Pending`. Unreadable/absent/unknown is never "nothing is pending".
//!   * Every corruption shape fails the read rather than yielding a partial record:
//!     foreign magic, unsupported version, unknown field, blank identity, a `FAILED`
//!     outcome with no reason, a non-`FAILED` outcome carrying one, an empty file.
//!   * An engage never overwrites a held lockout.
//!   * Resolution requires a non-blank operator acknowledgement, refuses when nothing
//!     is pending, and refuses (still blocking) when the lockout cannot be read.
//!   * A control character in a strategy id round-trips instead of poisoning the
//!     record that was written after the safety side effects ran.

use std::fs;
use std::path::PathBuf;
use std::process;

use atp_orchestrator::demotion_pending_store::{
    engage, load, read_state, resolve, serialize, DemotionPendingRecord, DemotionPendingState,
    DemotionPendingStoreError, MAGIC,
};
use atp_types::{SideEffectOutcome, StrategyId};

/// A scratch directory unique to this process AND this call site, removed on drop.
///
/// Both halves matter: a fixed name collides between concurrent `cargo test` runs, and
/// a pid-keyed name that is never removed collides after the OS recycles the pid — a
/// live run of this repo once found 57,131 leaked scratch directories, whose failures
/// land on exactly the tests that assert ABSENCE.
struct Scratch {
    dir: PathBuf,
}

impl Scratch {
    fn new(tag: &str, line: u32) -> Self {
        let dir =
            std::env::temp_dir().join(format!("atp-resv004-store-{tag}-{}-{line}", process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("create scratch dir");
        Self { dir }
    }

    fn path(&self) -> PathBuf {
        self.dir.join("demotion-pending.json")
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn record() -> DemotionPendingRecord {
    DemotionPendingRecord {
        demoting_strategy_id: StrategyId::new("live-momentum"),
        candidate_strategy_id: StrategyId::new("paper-reversal"),
        elapsed_seconds: 72,
        timeout_seconds: 60,
        observed_at_seconds: 1_715_000_000,
        liquidation_cancel: SideEffectOutcome::Succeeded,
        operator_alert: SideEffectOutcome::Failed {
            reason: "SMS gateway timed out".to_string(),
        },
    }
}

#[test]
fn resv_4_an_engaged_lockout_round_trips_with_its_recovery_critical_facts() {
    let scratch = Scratch::new("roundtrip", line!());
    let path = scratch.path();

    engage(&path, &record()).expect("engage the lockout");

    let read_back = load(&path).expect("read").expect("a lockout is held");
    assert_eq!(read_back, record());
    // The two facts an operator resolves against survive: whether a live liquidation
    // order may still be resting, and whether anyone was actually paged.
    assert_eq!(read_back.liquidation_cancel, SideEffectOutcome::Succeeded);
    assert!(read_back.operator_alert.is_failed());

    match read_state(&path) {
        DemotionPendingState::Pending(held) => assert_eq!(*held, record()),
        other => panic!("expected Pending, got {other:?}"),
    }
    assert!(read_state(&path).blocks_promotion());
}

#[test]
fn resv_4_an_absent_lockout_is_clear_and_does_not_block() {
    let scratch = Scratch::new("absent", line!());
    let path = scratch.path();

    assert_eq!(load(&path).expect("read an absent store"), None);
    assert_eq!(read_state(&path), DemotionPendingState::Clear);
    assert!(!read_state(&path).blocks_promotion());
}

#[test]
fn resv_4_an_unreadable_lockout_blocks_exactly_like_a_held_one() {
    // The defect this guards: an unreadable file yields no record, which is the same
    // shape as no file at all. Collapsing the two would render a corrupt lockout as a
    // confident "nothing is pending" — a false all-clear on the one question the file
    // exists to answer.
    let scratch = Scratch::new("unreadable", line!());
    let path = scratch.path();
    fs::write(&path, "{\"magic\":\"SOMEONE-ELSES-FILE\"}\n").expect("write a foreign payload");

    assert!(load(&path).is_err());
    let state = read_state(&path);
    assert!(matches!(state, DemotionPendingState::Unreadable { .. }));
    assert!(
        state.blocks_promotion(),
        "an unreadable lockout must block promotion"
    );
    assert!(state.reason().contains("cannot be read"));
    // And it is a DISTINCT state, not folded into either of the other two.
    assert_ne!(state, DemotionPendingState::Clear);
}

#[test]
fn resv_4_every_corruption_shape_fails_the_read_rather_than_yielding_a_partial_record() {
    let scratch = Scratch::new("corrupt", line!());
    let path = scratch.path();
    let good = serialize(&record());

    let cases: Vec<(&str, String)> = vec![
        ("empty file", String::new()),
        ("whitespace only", "   \n".to_string()),
        ("not JSON", "this is not a json object".to_string()),
        ("foreign magic", good.replace(MAGIC, "ATP-SOMETHING-ELSE")),
        (
            "unsupported version",
            good.replace("\"schema_version\":1", "\"schema_version\":99"),
        ),
        (
            "unknown field",
            good.replace("{", "{\"unexpected_flag\":true,"),
        ),
        (
            "blank identity",
            good.replace("\"live-momentum\"", "\"   \""),
        ),
        (
            "misspelled field",
            good.replace("demoting_strategy_id", "demotng_strategy_id"),
        ),
        (
            "negative elapsed",
            good.replace("\"elapsed_seconds\":72", "\"elapsed_seconds\":-1"),
        ),
        (
            "unknown outcome",
            good.replace("\"liquidation_cancel\":\"SUCCEEDED\"", "\"liquidation_cancel\":\"MAYBE\""),
        ),
        (
            // A recorded failure with no reason cannot tell an operator what to recover.
            "FAILED with no reason",
            good.replace(
                ",\"operator_alert_reason\":\"SMS gateway timed out\"",
                "",
            ),
        ),
        (
            // The payload would then state the outcome twice, and disagree.
            "SUCCEEDED carrying a reason",
            good.replace(
                "\"liquidation_cancel\":\"SUCCEEDED\"",
                "\"liquidation_cancel\":\"SUCCEEDED\",\"liquidation_cancel_reason\":\"but also it failed\"",
            ),
        ),
        // The escape-decoding class. The reader decodes identity and reason text so the
        // writer satisfies its own reader; each shape below is one this writer never
        // emits, so decoding it would mean inventing an identity.
        (
            "unknown escape",
            good.replace("live-momentum", "live-\\qmomentum"),
        ),
        (
            "truncated \\u escape",
            good.replace("live-momentum", "live-\\u01"),
        ),
        (
            "non-hex \\u escape",
            good.replace("live-momentum", "live-\\uZZZZ"),
        ),
        (
            "lone surrogate half",
            good.replace("live-momentum", "live-\\ud800"),
        ),
        (
            // An escaped spelling of a closed-vocabulary label is refused as unknown
            // rather than decoded into a match.
            "escaped outcome label",
            good.replace(
                "\"liquidation_cancel\":\"SUCCEEDED\"",
                "\"liquidation_cancel\":\"SUCCEEDE\\u0044\"",
            ),
        ),
    ];

    for (name, payload) in cases {
        fs::write(&path, &payload).unwrap_or_else(|_| panic!("write {name}"));
        assert!(
            load(&path).is_err(),
            "{name}: a corrupt lockout must fail the read, not parse partially"
        );
        assert!(
            read_state(&path).blocks_promotion(),
            "{name}: a corrupt lockout must still block promotion"
        );
    }
}

#[test]
fn resv_4_an_engage_never_overwrites_the_record_an_operator_still_must_resolve() {
    let scratch = Scratch::new("no-overwrite", line!());
    let path = scratch.path();
    engage(&path, &record()).expect("engage the first lockout");

    let second = DemotionPendingRecord {
        demoting_strategy_id: StrategyId::new("live-other"),
        candidate_strategy_id: StrategyId::new("paper-other"),
        elapsed_seconds: 90,
        timeout_seconds: 60,
        observed_at_seconds: 1_715_000_600,
        liquidation_cancel: SideEffectOutcome::Succeeded,
        operator_alert: SideEffectOutcome::Succeeded,
    };
    let error = engage(&path, &second).expect_err("a second engage must refuse");
    assert!(matches!(
        error,
        DemotionPendingStoreError::AlreadyPending { .. }
    ));
    // The FIRST record — describing the positions that are still unresolved — survives.
    assert_eq!(load(&path).expect("read").expect("still held"), record());
}

#[test]
fn resv_4_an_engage_will_not_overwrite_a_lockout_it_merely_cannot_read() {
    // Fail closed in the same direction: an unreadable lockout is still a lockout, so
    // an engage must not quietly replace it with a fresh, readable one and lose the
    // evidence of the older unresolved demotion.
    let scratch = Scratch::new("no-overwrite-corrupt", line!());
    let path = scratch.path();
    fs::write(&path, "{\"magic\":\"ATP-SOMETHING-ELSE\"}\n").expect("write a foreign payload");

    assert!(engage(&path, &record()).is_err());
    assert!(read_state(&path).blocks_promotion());
}

#[test]
fn resv_4_resolution_requires_a_non_blank_operator_acknowledgement() {
    // This is the control standing between an automated retry and a live position
    // nobody looked at, so it cannot be satisfied by a flag an automation sets.
    let scratch = Scratch::new("ack", line!());
    let path = scratch.path();
    engage(&path, &record()).expect("engage");

    for blank in ["", "   ", "\t\n"] {
        let error = resolve(&path, blank).expect_err("a blank acknowledgement must refuse");
        assert!(matches!(
            error,
            DemotionPendingStoreError::MissingAcknowledgement
        ));
    }
    // ...and the lockout is still held after every refusal.
    assert!(read_state(&path).blocks_promotion());

    let cleared = resolve(&path, "operator jg: AAPL flattened by hand at 15:42").expect("resolve");
    assert_eq!(cleared, record());
    assert_eq!(read_state(&path), DemotionPendingState::Clear);
    assert!(!read_state(&path).blocks_promotion());
}

#[test]
fn resv_4_resolving_nothing_is_an_error_not_a_silent_success() {
    // Reporting "resolved" when nothing was pending would let a FAILED engage read as
    // a completed resolution.
    let scratch = Scratch::new("not-pending", line!());
    let path = scratch.path();

    let error = resolve(&path, "operator jg").expect_err("nothing to resolve");
    assert!(matches!(
        error,
        DemotionPendingStoreError::NotPending { .. }
    ));
}

#[test]
fn resv_4_an_unreadable_lockout_cannot_be_resolved_away_and_keeps_blocking() {
    // Removing a lockout this build cannot read would discard the only description of
    // the unresolved positions. The error names the file for deliberate removal — the
    // same posture the exclusive guard takes toward a stale lock.
    let scratch = Scratch::new("unreadable-resolve", line!());
    let path = scratch.path();
    fs::write(&path, "{\"magic\":\"ATP-SOMETHING-ELSE\"}\n").expect("write a foreign payload");

    let error = resolve(&path, "operator jg").expect_err("an unreadable lockout must not clear");
    assert!(matches!(
        error,
        DemotionPendingStoreError::UnreadableResolution { .. }
    ));
    assert!(error.to_string().contains("still BLOCKS promotion"));
    // The file is still there, and still blocking.
    assert!(path.exists());
    assert!(read_state(&path).blocks_promotion());
}

#[test]
fn resv_4_a_control_character_in_a_strategy_id_round_trips_instead_of_poisoning_the_record() {
    // `StrategyId::new` validates nothing, and this record is written AFTER the safety
    // side effects ran — so a hand-rolled escaper that missed a C0 byte would corrupt
    // the payload and suppress the evidence of the cancel and the page.
    let scratch = Scratch::new("c0", line!());
    let path = scratch.path();
    let hostile = DemotionPendingRecord {
        demoting_strategy_id: StrategyId::new("live-\u{0001}\"mo\\mentum\n"),
        candidate_strategy_id: StrategyId::new("paper-\u{001f}reversal"),
        elapsed_seconds: 72,
        timeout_seconds: 60,
        observed_at_seconds: 1_715_000_000,
        liquidation_cancel: SideEffectOutcome::Failed {
            reason: "IB said \"no\"\u{0007} and dropped\r\n".to_string(),
        },
        operator_alert: SideEffectOutcome::Succeeded,
    };

    engage(&path, &hostile).expect("engage a hostile record");
    let read_back = load(&path)
        .expect("the payload must still parse")
        .expect("held");
    // The identity survives verbatim rather than being silently mangled or truncated.
    assert_eq!(read_back.demoting_strategy_id, hostile.demoting_strategy_id);
    assert_eq!(
        read_back.candidate_strategy_id,
        hostile.candidate_strategy_id
    );
    assert_eq!(read_back.liquidation_cancel, hostile.liquidation_cancel);

    // And the raw bytes carry no unescaped control character.
    let raw = fs::read_to_string(&path).expect("read raw");
    assert!(
        !raw.chars().any(|c| (c as u32) < 0x20 && c != '\n'),
        "every C0 control must be escaped: {raw:?}"
    );
    assert!(raw.contains("\\u0001"));
    assert!(raw.contains("\\u001f"));
    assert!(raw.contains("\\u0007"));
}

#[test]
fn resv_4_a_lockout_survives_being_read_by_a_fresh_reader() {
    // The whole point is durability across processes. Engaging and then reading
    // through a completely fresh path handle is the in-process stand-in for the
    // restart case the requirement is about.
    let scratch = Scratch::new("durable", line!());
    let path = scratch.path();
    engage(&path, &record()).expect("engage");

    let reopened = PathBuf::from(path.to_str().expect("utf-8 path"));
    assert!(read_state(&reopened).blocks_promotion());
    assert_eq!(
        load(&reopened)
            .expect("read")
            .expect("held")
            .elapsed_seconds,
        72
    );
}
