//! SRS-BT-001 operator launch CLI end-to-end test.
//!
//! Drives the compiled `bt001_backtest_cli` binary as a real operator would: launch a
//! backtest over a configurable `YYYY-MM-DD` window, reading bars from the stored
//! catalog via the real `StoreBarSource` (system data). It pins both halves of the
//! acceptance criterion this CLI realizes — "launched with system data" and "start
//! and end dates are selectable" — by asserting that a sub-window genuinely restricts
//! replay (fewer bars processed, the sub-window's first bar in the trade log), and
//! that every fail-closed boundary (deferred uploaded source, inverted window,
//! malformed date, unknown source, missing required flag) exits non-zero so a
//! misconfigured launch never silently runs the wrong window or wrong dataset.
//!
//! Cargo exports the built binary's path as `CARGO_BIN_EXE_bt001_backtest_cli`, so
//! this is a genuine process round trip — not an in-process call.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_bt001_backtest_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the bt001_backtest_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
}

#[test]
fn launches_over_full_window_with_system_data() {
    let output = run(&[
        "run",
        "--symbol",
        "ACME",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-09",
    ]);
    assert!(
        output.status.success(),
        "launch must succeed: {}",
        stderr(&output)
    );
    let out = stdout(&output);
    assert!(
        out.contains("SRS-BT-001 LAUNCH OK"),
        "missing PASS line:\n{out}"
    );
    // System data provenance (the AC's "launched with system data").
    assert!(out.contains("source:          system_data"), "{out}");
    assert!(
        out.contains("window (dates):  2024-01-02 .. 2024-01-09 (inclusive)"),
        "{out}"
    );
    // All six seeded fixture bars are in the full window.
    assert!(out.contains("bars processed:  6"), "{out}");
    assert!(out.contains("symbol:          ACME"), "{out}");
}

#[test]
fn date_range_selection_restricts_replay() {
    // A sub-window of the seeded span must process FEWER bars and reference the
    // sub-window's first bar (2024-01-03 @ 10250) as the fill — proving the engine
    // authoritatively restricts replay to the operator-selected dates.
    let full = run(&["run", "--start", "2024-01-02", "--end", "2024-01-09"]);
    let sub = run(&["run", "--start", "2024-01-03", "--end", "2024-01-05"]);
    assert!(full.status.success() && sub.status.success());

    let full_out = stdout(&full);
    let sub_out = stdout(&sub);
    assert!(full_out.contains("bars processed:  6"), "{full_out}");
    assert!(sub_out.contains("bars processed:  3"), "{sub_out}");
    // The sub-window's first in-window bar is 2024-01-03 (close 10250), not the full
    // window's first bar (2024-01-02 @ 10000).
    assert!(sub_out.contains("qty=10 @ 10250 minor"), "{sub_out}");
    assert!(
        sub_out.contains("window (dates):  2024-01-03 .. 2024-01-05 (inclusive)"),
        "{sub_out}"
    );
    // Different windows produce different final equity (the selection is load-bearing).
    assert_ne!(
        final_equity_line(&full_out),
        final_equity_line(&sub_out),
        "a different date window must produce a different result"
    );
}

#[test]
fn single_day_window_is_valid() {
    let output = run(&["run", "--start", "2024-01-04", "--end", "2024-01-04"]);
    assert!(output.status.success(), "{}", stderr(&output));
    let out = stdout(&output);
    // Exactly one seeded bar falls on 2024-01-04 (close 9900).
    assert!(out.contains("bars processed:  1"), "{out}");
    assert!(out.contains("qty=10 @ 9900 minor"), "{out}");
}

#[test]
fn uploaded_source_is_deferred_and_fails_closed() {
    let output = run(&[
        "run",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-09",
        "--source",
        "uploaded",
    ]);
    assert!(
        !output.status.success(),
        "uploaded Parquet source must fail closed (deferred), not run system data under an \
         UploadedData label"
    );
    let err = stderr(&output);
    assert!(err.contains("DEFERRED"), "{err}");
    assert!(err.contains("Parquet"), "{err}");
}

#[test]
fn misspelled_flag_fails_closed_does_not_run_default_source() {
    // A typo'd source flag must NOT be silently dropped and fall back to the default
    // `system` source while reporting success — that would be a wrong-source launch.
    let output = run(&[
        "run",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-09",
        "--sorce", // typo of --source
        "uploaded",
    ]);
    assert!(
        !output.status.success(),
        "a misspelled flag must fail closed, not run the default source: {}",
        stdout(&output)
    );
    assert!(
        stderr(&output).contains("unknown or unexpected argument"),
        "{}",
        stderr(&output)
    );
    // It must NOT have launched anything.
    assert!(
        !stdout(&output).contains("SRS-BT-001 LAUNCH OK"),
        "{}",
        stdout(&output)
    );
}

#[test]
fn duplicate_flag_fails_closed() {
    let output = run(&[
        "run",
        "--start",
        "2024-01-02",
        "--start",
        "2024-01-03",
        "--end",
        "2024-01-09",
    ]);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("duplicate flag"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn flag_missing_value_fails_closed() {
    // `--start` immediately followed by another flag means the value was omitted.
    let output = run(&["run", "--start", "--end", "2024-01-09"]);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("requires a value"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn unknown_source_fails_closed() {
    let output = run(&[
        "run",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-09",
        "--source",
        "bogus",
    ]);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("unknown --source"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn non_positive_cash_fails_closed() {
    for cash in ["0", "-1", "-100000"] {
        let output = run(&[
            "run",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-09",
            "--cash",
            cash,
        ]);
        assert!(
            !output.status.success(),
            "--cash {cash} must fail closed (got success): {}",
            stdout(&output)
        );
        assert!(
            stderr(&output).contains("--cash must be a positive"),
            "{}",
            stderr(&output)
        );
        assert!(
            !stdout(&output).contains("SRS-BT-001 LAUNCH OK"),
            "no launch over a non-positive balance: {}",
            stdout(&output)
        );
    }
}

#[test]
fn unseeded_symbol_fails_closed() {
    // The fixture catalog holds only ACME. Requesting another symbol must fail closed
    // (no stored bars) rather than fabricating a catalog under the requested name and
    // reporting a successful system-data launch.
    let output = run(&[
        "run",
        "--symbol",
        "TSLA",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-09",
    ]);
    assert!(
        !output.status.success(),
        "an unseeded symbol must fail closed: {}",
        stdout(&output)
    );
    assert!(
        stderr(&output).contains("no stored bars for symbol \"TSLA\""),
        "{}",
        stderr(&output)
    );
    assert!(
        !stdout(&output).contains("SRS-BT-001 LAUNCH OK"),
        "{}",
        stdout(&output)
    );
}

#[test]
fn inverted_window_fails_closed() {
    let output = run(&["run", "--start", "2024-01-09", "--end", "2024-01-02"]);
    assert!(!output.status.success());
    assert!(stderr(&output).contains("inverted"), "{}", stderr(&output));
}

#[test]
fn malformed_date_fails_closed() {
    let output = run(&["run", "--start", "2024-1-2", "--end", "2024-01-09"]);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("malformed date"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn impossible_date_fails_closed() {
    // 2023 is not a leap year — Feb 29 does not exist.
    let output = run(&["run", "--start", "2023-02-29", "--end", "2023-03-01"]);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("not a real calendar date"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn missing_required_flag_fails_closed() {
    let output = run(&["run", "--end", "2024-01-09"]);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("--start is required"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn unknown_subcommand_fails_closed() {
    let output = run(&["frobnicate"]);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("unknown subcommand"),
        "{}",
        stderr(&output)
    );
}

/// Extract the `final equity:` line so two runs can be compared.
fn final_equity_line(out: &str) -> &str {
    out.lines()
        .find(|line| line.contains("final equity:"))
        .expect("a final equity line")
}
