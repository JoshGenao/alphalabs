//! SRS-BT-008 operator walk-forward CLI end-to-end test.
//!
//! Drives the compiled `bt008_walk_forward_cli` binary as a real operator would: a fold
//! schedule + parameter space + objective produces, per fold, the in-sample-optimized
//! parameter set and both windows' metrics — the acceptance criterion exercised over a
//! genuine process boundary. The no-lookahead invariant and every fail-closed path (a
//! lookahead fold, a half-selected objective, mutually-exclusive schedule flags, a zero
//! rolling argument, malformed flags) must exit non-zero with NO fold output, and two
//! identical invocations must be byte-identical (the cross-process face of the SRS-BT-010
//! determinism discipline).
//!
//! Cargo exports the built binary's path as `CARGO_BIN_EXE_bt008_walk_forward_cli`.

use std::process::{Command, Output};

const CLI: &str = env!("CARGO_BIN_EXE_bt008_walk_forward_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the bt008_walk_forward_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
}

/// The default demo run states its space, objective, and three forward-marching folds,
/// each with in-sample and out-of-sample lines — and every fold obeys no-lookahead.
#[test]
fn srs_bt_008_cli_default_run_reports_forward_folds() {
    let output = run(&["run"]);
    assert!(
        output.status.success(),
        "default run must succeed: {}",
        stderr(&output)
    );
    let out = stdout(&output);
    assert!(out.contains("axis lot = [5, 10, 20]"));
    assert!(out.contains("axis sell_ts = [3, 5]"));
    assert!(
        out.contains("objective: maximize sharpe_ratio"),
        "the default objective is stated, never implicit: {out}"
    );
    assert!(out.contains("folds: 3"));
    // Three forward-tiling folds; each shows the selected params + both windows.
    assert!(out.contains("fold 0: in_sample=[1,4] out_of_sample=[5,6]"));
    assert!(out.contains("fold 1: in_sample=[3,6] out_of_sample=[7,8]"));
    assert!(out.contains("fold 2: in_sample=[5,8] out_of_sample=[9,10]"));
    assert_eq!(
        out.matches("selected params=[").count(),
        3,
        "each fold names its optimized parameter set"
    );
    assert_eq!(out.matches("in_sample: objective=").count(), 3);
    assert_eq!(out.matches("out_of_sample: objective=").count(), 3);
    // The default human report PRESERVES the full eight-metric SYS-16 family + benchmark
    // identity per window (SRS-BT-008 "outputs preserve ... metrics per window"), for both the
    // in-sample and out-of-sample halves of every fold — not a subset. Three folds × two
    // windows = six renderings of each metric field.
    for field in [
        "benchmark=",
        "sharpe=",
        "sortino=",
        "alpha=",
        "beta=",
        "max_drawdown=",
        "ann_return=",
        "ann_volatility=",
        "win_rate=",
    ] {
        assert_eq!(
            out.matches(field).count(),
            6,
            "metric field `{field}` must appear for both windows of all three folds"
        );
    }
}

/// The operator's objective selection drives the optimization end to end: maximizing
/// Sharpe and minimizing max drawdown select a different parameter set for fold 0.
#[test]
fn srs_bt_008_cli_objective_selection_changes_optimized_params() {
    let sharpe = run(&["run"]);
    assert!(sharpe.status.success(), "{}", stderr(&sharpe));
    let sharpe_out = stdout(&sharpe);

    let drawdown = run(&["run", "--objective", "max_drawdown", "--direction", "min"]);
    assert!(drawdown.status.success(), "{}", stderr(&drawdown));
    let drawdown_out = stdout(&drawdown);
    assert!(drawdown_out.contains("objective: minimize max_drawdown"));

    // fold 0's selected params differ between the two objectives (verified live:
    // maximize-Sharpe → lot=20,sell_ts=5; minimize-drawdown → lot=5,sell_ts=5).
    let fold0_params = |out: &str| -> String {
        out.lines()
            .skip_while(|line| !line.starts_with("fold 0:"))
            .find(|line| line.trim_start().starts_with("selected params=["))
            .expect("fold 0 selected params line")
            .trim_start()
            .to_string()
    };
    assert_ne!(
        fold0_params(&sharpe_out),
        fold0_params(&drawdown_out),
        "the selected objective genuinely changes fold 0's optimized parameter set"
    );
}

/// The kv machine format: count first, then contiguous `fold.<i>.*` blocks with both
/// windows' bounds, echoed params, and both objectives — the single grammar a machine
/// consumer can fail closed on.
#[test]
fn srs_bt_008_cli_kv_format_grammar() {
    let output = run(&["run", "--format", "kv"]);
    assert!(output.status.success(), "{}", stderr(&output));
    let out = stdout(&output);

    assert!(out.starts_with("objective.metric:sharpe_ratio\n"));
    assert!(out.contains("objective.direction:max\n"));
    assert!(out.contains("fold_count:3\n"));

    for index in 0..3 {
        assert!(out.contains(&format!("fold.{index}.in_sample.start:")));
        assert!(out.contains(&format!("fold.{index}.in_sample.end:")));
        assert!(out.contains(&format!("fold.{index}.out_of_sample.start:")));
        assert!(out.contains(&format!("fold.{index}.out_of_sample.end:")));
        assert!(out.contains(&format!("fold.{index}.param_count:2\n")));
        assert!(out.contains(&format!("fold.{index}.param.0.key:lot\n")));
        assert!(out.contains(&format!("fold.{index}.param.1.key:sell_ts\n")));
        assert!(out.contains(&format!("fold.{index}.in_sample.objective:")));
        assert!(out.contains(&format!("fold.{index}.in_sample.metric.sharpe:")));
        assert!(out.contains(&format!("fold.{index}.out_of_sample.objective:")));
        assert!(out.contains(&format!("fold.{index}.out_of_sample.metric.sharpe:")));
    }
    // Indices end at fold_count; no human preamble leaks into the machine grammar.
    assert!(
        !out.lines().any(|line| line.starts_with("fold.3.")),
        "indices end at fold_count"
    );
    assert!(!out.contains("parameter space:"));
    assert!(!out.contains("folds: 3"));
}

/// An explicit `--fold` schedule is honored: the two named folds are reported, and the
/// no-lookahead invariant holds (out-of-sample strictly after in-sample).
#[test]
fn srs_bt_008_cli_explicit_folds_are_honored() {
    let output = run(&[
        "run", "--fold", "1:4:5:6", "--fold", "3:6:7:8", "--format", "kv",
    ]);
    assert!(output.status.success(), "{}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("fold_count:2\n"));
    assert!(out.contains("fold.0.in_sample.start:1\n"));
    assert!(out.contains("fold.0.out_of_sample.start:5\n"));
    assert!(out.contains("fold.1.in_sample.start:3\n"));
    assert!(out.contains("fold.1.out_of_sample.start:7\n"));
}

/// Two identical invocations are byte-identical: walk-forward is deterministic across
/// fresh processes.
#[test]
fn srs_bt_008_cli_repeat_runs_byte_identical() {
    let args = [
        "run",
        "--rolling",
        "1:4:2:2:3",
        "--objective",
        "sortino_ratio",
        "--direction",
        "max",
        "--format",
        "kv",
    ];
    let first = run(&args);
    let second = run(&args);
    assert!(first.status.success());
    assert!(second.status.success());
    assert_eq!(
        first.stdout, second.stdout,
        "fresh-process repeat runs must be byte-identical"
    );
    assert!(!first.stdout.is_empty());
}

/// Every malformed invocation exits non-zero with NO fold output — a misdefined
/// walk-forward never silently hands back a partial or default report.
#[test]
fn srs_bt_008_cli_malformed_invocations_fail_closed() {
    let cases: &[(&[&str], &str)] = &[
        (&["run", "--bogus"], "unknown flag"),
        // The no-lookahead invariant, surfaced to the operator.
        (&["run", "--fold", "1:5:5:6"], "lookahead window"),
        (&["run", "--fold", "1:4:5"], "--fold expects"),
        (
            &["run", "--fold", "1:4:5:6", "--rolling", "1:4:2:2:3"],
            "mutually exclusive",
        ),
        (
            &["run", "--rolling", "1:0:2:2:3"],
            "window length must be positive",
        ),
        (&["run", "--rolling", "1:4:2:0:3"], "step must be positive"),
        (
            &["run", "--rolling", "1:4:2:2:0"],
            "fold count must be positive",
        ),
        // An oversized fold count fails closed (typed error, no allocation panic).
        (&["run", "--rolling", "1:4:2:2:20000"], "exceeding the cap"),
        (&["run", "--rolling", "1:4:2:1:2"], "overlapping folds"),
        (&["run", "--rolling", "1:4:5"], "--rolling expects"),
        (
            &["run", "--objective", "profit", "--direction", "max"],
            "unknown objective metric",
        ),
        (
            &["run", "--objective", "sharpe_ratio"],
            "--objective requires --direction",
        ),
        (&["run", "--axis", "lot=5,5"], "lists value '5' twice"),
        (&["run", "--format", "xml"], "--format expects"),
        (&["walk"], "unknown subcommand"),
        (&[], "missing subcommand"),
    ];
    for (args, expected) in cases {
        let output = run(args);
        assert!(
            !output.status.success(),
            "must fail closed: {args:?} — stdout: {}",
            stdout(&output)
        );
        assert!(
            stderr(&output).contains(expected),
            "{args:?} must name the fault '{expected}', got: {}",
            stderr(&output)
        );
        assert!(
            !stdout(&output).contains("fold "),
            "{args:?} must not emit any human fold output"
        );
        assert!(
            !stdout(&output).contains("fold."),
            "{args:?} must not emit any kv fold output"
        );
    }
}

/// A point the fixture strategy cannot interpret aborts the analysis naming the offending
/// window — never a silent default run misattributed to the labeled parameters.
#[test]
fn srs_bt_008_cli_uninterpretable_point_aborts_named() {
    let output = run(&["run", "--axis", "lot=5,abc", "--axis", "sell_ts=3"]);
    assert!(!output.status.success());
    let err = stderr(&output);
    assert!(
        err.contains("in_sample=[1,4]"),
        "the offending fold's window is named: {err}"
    );
    assert!(
        err.contains("[lot=abc, sell_ts=3]"),
        "the offending point is named: {err}"
    );
}

/// `help` prints the usage and exits zero.
#[test]
fn srs_bt_008_cli_help_prints_usage() {
    let output = run(&["help"]);
    assert!(output.status.success());
    let out = stdout(&output);
    assert!(out.contains("bt008_walk_forward_cli — SRS-BT-008"));
    assert!(out.contains("--fold <is_start:is_end:oos_start:oos_end>"));
    assert!(out.contains("--rolling <start:is_len:oos_len:step:count>"));
    assert!(out.contains("--objective <metric>"));
}
