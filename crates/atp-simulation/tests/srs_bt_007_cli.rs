//! SRS-BT-007 operator sweep CLI end-to-end test.
//!
//! Drives the compiled `bt007_sweep_cli` binary as a real operator would: a parameter
//! space defined with `--axis` flags produces ranked backtest results by the selected
//! objective function — the acceptance criterion exercised over a genuine process
//! boundary. Fail-closed paths (unknown flags/tokens, a half-selected objective,
//! malformed / duplicate axes) must exit non-zero with NO ranking output, and two
//! identical invocations must be byte-identical (the cross-process face of the
//! SRS-BT-010 determinism discipline).
//!
//! Cargo exports the built binary's path as `CARGO_BIN_EXE_bt007_sweep_cli`.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_bt007_sweep_cli");

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .output()
        .expect("the bt007_sweep_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
}

/// The default demo run states its space, objective, and every rank 1..6.
#[test]
fn srs_bt_007_cli_default_run_ranks_the_demo_space() {
    let output = run(&["run"]);
    assert!(
        output.status.success(),
        "default run must succeed: {}",
        stderr(&output)
    );
    let out = stdout(&output);
    assert!(out.contains("axis lot = [5, 10, 20]"));
    assert!(out.contains("axis sell_ts = [3, 5]"));
    assert!(out.contains("points: 6"));
    assert!(
        out.contains("objective: maximize sharpe_ratio"),
        "the default objective is stated, never implicit: {out}"
    );
    assert!(out.contains("benchmark: SPY (default=true)"));
    assert!(out.contains("ranked (6 point(s), best first):"));
    for rank in 1..=6 {
        assert!(
            out.contains(&format!("rank={rank} ")),
            "rank {rank} present"
        );
    }
    assert!(out.contains("unranked (0 point(s)):"));
}

/// An explicit space + objective: minimizing max_drawdown puts a different point at
/// rank 1 than maximizing it over the same space — the operator's selection drives
/// the ranking, end to end through the process boundary.
#[test]
fn srs_bt_007_cli_objective_selection_flips_rank_one() {
    let space = ["--axis", "lot=5,10,20", "--axis", "sell_ts=3,5"];

    let mut min_args = vec!["run"];
    min_args.extend_from_slice(&space);
    min_args.extend_from_slice(&["--objective", "max_drawdown", "--direction", "min"]);
    let min_run = run(&min_args);
    assert!(min_run.status.success(), "{}", stderr(&min_run));
    let min_out = stdout(&min_run);
    assert!(min_out.contains("objective: minimize max_drawdown"));

    let mut max_args = vec!["run"];
    max_args.extend_from_slice(&space);
    max_args.extend_from_slice(&["--objective", "max_drawdown", "--direction", "max"]);
    let max_run = run(&max_args);
    assert!(max_run.status.success(), "{}", stderr(&max_run));
    let max_out = stdout(&max_run);

    let rank_one = |out: &str| -> String {
        out.lines()
            .find(|line| line.trim_start().starts_with("rank=1 "))
            .expect("a rank=1 line")
            .trim_start()
            .to_string()
    };
    let min_first = rank_one(&min_out);
    let max_first = rank_one(&max_out);
    assert_ne!(
        min_first, max_first,
        "min and max over the same metric must rank different points first"
    );
    // The smallest exposure (lot=5, held to the end) has the smallest drawdown; the
    // largest exposure has the largest — pin both so the direction is genuinely wired.
    assert!(
        min_first.contains("params=[lot=5, sell_ts=5]"),
        "min drawdown rank 1: {min_first}"
    );
    assert!(
        max_first.contains("params=[lot=20, sell_ts=3]"),
        "max drawdown rank 1: {max_first}"
    );
}

/// The kv machine format: counts first, contiguous indexed blocks, params echoed —
/// the single grammar a machine consumer can fail closed on.
#[test]
fn srs_bt_007_cli_kv_format_grammar() {
    let output = run(&["run", "--format", "kv"]);
    assert!(output.status.success(), "{}", stderr(&output));
    let out = stdout(&output);

    assert!(out.starts_with("objective.metric:sharpe_ratio\n"));
    assert!(out.contains("objective.direction:max\n"));
    assert!(out.contains("point_count:6\n"));
    assert!(out.contains("ranked_count:6\n"));
    assert!(out.contains("unranked_count:0\n"));

    // Contiguous 0..6 ranked blocks with 1-based rank fields and echoed params.
    for index in 0..6 {
        assert!(out.contains(&format!("ranked.{index}.rank:{}\n", index + 1)));
        assert!(out.contains(&format!("ranked.{index}.objective_value:")));
        assert!(out.contains(&format!("ranked.{index}.param_count:2\n")));
        assert!(out.contains(&format!("ranked.{index}.param.0.key:lot\n")));
        assert!(out.contains(&format!("ranked.{index}.param.1.key:sell_ts\n")));
        assert!(out.contains(&format!("ranked.{index}.metric.sharpe:")));
        assert!(out.contains(&format!("ranked.{index}.final_equity_minor:")));
        assert!(out.contains(&format!("ranked.{index}.trade_count:")));
    }
    // Line-anchored: "unranked.<i>." also CONTAINS "ranked.<i>." as a substring, so
    // block-absence checks must match line starts, never substrings.
    assert!(
        !out.lines().any(|line| line.starts_with("ranked.6.")),
        "indices end at ranked_count"
    );
    assert!(
        !out.lines().any(|line| line.starts_with("unranked.0.")),
        "no unranked blocks when empty"
    );

    // No human preamble leaks into the machine grammar.
    assert!(!out.contains("parameter space:"));
    assert!(!out.contains("best first"));
}

/// An undefined objective surfaces as an unranked kv block, never a fabricated rank.
#[test]
fn srs_bt_007_cli_kv_unranked_block_for_undefined_objective() {
    // A one-point space whose strategy sells at ts=1 (the same bar it would open on):
    // position is 0 at ts=1, so -position = 0 and lot never opens — zero trades, so
    // win_rate is mathematically undefined.
    let output = run(&[
        "run",
        "--axis",
        "lot=5",
        "--axis",
        "sell_ts=1",
        "--objective",
        "win_rate",
        "--direction",
        "max",
        "--format",
        "kv",
    ]);
    assert!(output.status.success(), "{}", stderr(&output));
    let out = stdout(&output);
    assert!(out.contains("point_count:1\n"));
    assert!(out.contains("ranked_count:0\n"));
    assert!(out.contains("unranked_count:1\n"));
    assert!(out.contains("unranked.0.reason:objective_undefined\n"));
    assert!(out.contains("unranked.0.metric.win_rate:n/a\n"));
    // Line-anchored: "unranked.0." CONTAINS "ranked.0." as a substring.
    assert!(
        !out.lines().any(|line| line.starts_with("ranked.0.")),
        "nothing is ranked"
    );
}

/// Two identical invocations are byte-identical: the ranking is deterministic across
/// fresh processes.
#[test]
fn srs_bt_007_cli_repeat_runs_byte_identical() {
    let args = [
        "run",
        "--axis",
        "lot=5,10,20",
        "--axis",
        "sell_ts=3,5",
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

/// Every malformed invocation exits non-zero with NO ranking output — a misdefined
/// sweep never silently hands back a partial or default ranking.
#[test]
fn srs_bt_007_cli_malformed_invocations_fail_closed() {
    let cases: &[(&[&str], &str)] = &[
        (&["run", "--bogus"], "unknown flag"),
        (
            &["run", "--objective", "profit", "--direction", "max"],
            "unknown objective metric",
        ),
        (
            &["run", "--objective", "sharpe_ratio", "--direction", "up"],
            "unknown objective direction",
        ),
        (
            &["run", "--objective", "sharpe_ratio"],
            "--objective requires --direction",
        ),
        (
            &["run", "--direction", "max"],
            "--direction requires --objective",
        ),
        (&["run", "--axis", "lot"], "--axis expects"),
        (&["run", "--axis", "=5"], "axis name is empty"),
        (&["run", "--axis", "lot="], "empty value token"),
        (&["run", "--axis", "lot=5,5"], "lists value '5' twice"),
        (
            &["run", "--axis", "lot=5", "--axis", "lot=10"],
            "duplicate axis 'lot'",
        ),
        (&["run", "--format", "xml"], "--format expects"),
        (&["sweep"], "unknown subcommand"),
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
            !stdout(&output).contains("rank="),
            "{args:?} must not emit any ranking output"
        );
        assert!(
            !stdout(&output).contains("ranked."),
            "{args:?} must not emit any kv ranking output"
        );
    }
}

/// A point the fixture strategy cannot interpret aborts the sweep naming the point —
/// never a silent default run misattributed to the labeled parameters.
#[test]
fn srs_bt_007_cli_uninterpretable_point_aborts_named() {
    let unparseable = run(&["run", "--axis", "lot=5,abc", "--axis", "sell_ts=3"]);
    assert!(!unparseable.status.success());
    let err = stderr(&unparseable);
    assert!(
        err.contains("[lot=abc, sell_ts=3]"),
        "the offending point is named: {err}"
    );
    assert!(err.contains("expected an integer share count"), "{err}");

    let unknown_axis = run(&[
        "run",
        "--axis",
        "lot=5",
        "--axis",
        "sell_ts=3",
        "--axis",
        "phase=1",
    ]);
    assert!(!unknown_axis.status.success());
    assert!(
        stderr(&unknown_axis).contains("does not declare parameter 'phase'"),
        "{}",
        stderr(&unknown_axis)
    );

    let non_positive = run(&["run", "--axis", "lot=0", "--axis", "sell_ts=3"]);
    assert!(!non_positive.status.success());
    assert!(
        stderr(&non_positive).contains("lot must be positive"),
        "{}",
        stderr(&non_positive)
    );
}

/// `help` prints the usage and exits zero.
#[test]
fn srs_bt_007_cli_help_prints_usage() {
    let output = run(&["help"]);
    assert!(output.status.success());
    let out = stdout(&output);
    assert!(out.contains("bt007_sweep_cli — SRS-BT-007"));
    assert!(out.contains("--axis <name=v1,v2,...>"));
    assert!(out.contains("--objective <metric>"));
    assert!(out.contains("--direction <max|min>"));
}
