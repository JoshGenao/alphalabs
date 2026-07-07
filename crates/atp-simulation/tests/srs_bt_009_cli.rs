//! SRS-BT-009 operator persist/query CLI end-to-end test (Phase 2).
//!
//! Drives the compiled `bt009_store_cli` binary as a real operator would: `persist` runs the
//! fixture backtests and durably writes the store file, then `query` loads it back and answers every
//! SRS-BT-009 axis (by strategy, by backtest run window, by completion window, by parameter set, and
//! the combined query) with all seven artifacts intact. The fail-closed paths (a missing results
//! directory, an unknown subcommand, a half-specified date window, a missing directory config) must
//! exit non-zero so a misconfigured workflow never silently hands back an empty or partial history.
//!
//! Cargo exports the built binary's path as `CARGO_BIN_EXE_bt009_store_cli`, so this is a genuine
//! process round trip over the persisted file — not an in-process call.

use std::fs;
use std::path::PathBuf;
use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_bt009_store_cli");

/// A unique scratch directory under the OS temp dir. The suffix is a fixed per-test label (not a
/// clock/RNG read), so each test owns a distinct directory and parallel runs do not collide.
fn temp_dir(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("atp_bt009_cli_{label}"));
    let _ = fs::remove_dir_all(&dir);
    dir
}

/// Run the CLI with `args` and an explicit results `dir`, returning the captured output. The
/// `ATP_BACKTEST_RESULTS_DIR` env var is cleared so the `--dir` argument is unambiguously in play.
fn run(dir: &PathBuf, args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        .arg("--dir")
        .arg(dir)
        .env_remove("ATP_BACKTEST_RESULTS_DIR")
        .output()
        .expect("the bt009_store_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

#[test]
fn srs_bt_009_cli_persist_then_query_every_axis() {
    let dir = temp_dir("round_trip");

    // persist: run the fixture backtests and write the durable store file (--init: fresh dir).
    let persisted = run(&dir, &["persist", "--init"]);
    assert!(
        persisted.status.success(),
        "persist must succeed: {}",
        String::from_utf8_lossy(&persisted.stderr)
    );
    let persisted_out = stdout(&persisted);
    assert!(persisted_out.contains("store now holds 2 result(s) (2 new this run)"));
    assert!(persisted_out.contains("run=run-momentum"));
    assert!(persisted_out.contains("run=run-meanrev"));

    // The durable blob landed on disk and is the operator-inspectable checksummed codec output.
    let store_file = dir.join("backtest_results.store");
    assert!(
        store_file.exists(),
        "the persisted store file must exist on disk"
    );
    let on_disk = fs::read_to_string(&store_file).unwrap();
    assert!(
        on_disk.starts_with("ATP-BACKTEST-RECORD"),
        "the on-disk blob must be the checksummed codec output"
    );

    // (1) By strategy: only momentum, and every one of the seven artifacts is rendered.
    let by_strategy = run(&dir, &["query", "--strategy", "momentum"]);
    assert!(by_strategy.status.success());
    let out = stdout(&by_strategy);
    assert!(out.contains("1 match the query"));
    assert!(out.contains("record run-momentum"));
    assert!(!out.contains("record run-meanrev"));
    // The seven SRS-BT-009 artifacts (+ launch request) are all present.
    for marker in [
        "request:",
        "parameters:",
        "metrics:",
        "comparison:",
        "trade_log:",
        "equity_curve:",
        "code_version:",
        "completed_at:",
    ] {
        assert!(out.contains(marker), "query output must render {marker}");
    }
    assert!(out.contains("lookback=20")); // parameter set
    assert!(out.contains("benchmark=SPY")); // benchmark comparison identity
    assert!(out.contains("code_version: sha:deadbeef")); // strategy code version
    assert!(out.contains("completed_at: 1700000000")); // timestamp
    assert!(out.contains("trade_log:    2 fill(s)")); // trade log
    assert!(out.contains("equity_curve: 5 point(s)")); // equity curve

    // (2) By backtest run window (the SYS-21 "date range" axis): both runs tested [0, 100].
    let by_run_window = run(&dir, &["query", "--from", "50", "--to", "200"]);
    assert!(by_run_window.status.success());
    assert!(stdout(&by_run_window).contains("2 match the query"));
    let outside = run(&dir, &["query", "--from", "200", "--to", "300"]);
    assert!(stdout(&outside).contains("0 match the query"));

    // (3) By completion window (the distinct "when was it run" axis): only meanrev.
    let by_completion = run(
        &dir,
        &[
            "query",
            "--completed-from",
            "1700000400",
            "--completed-to",
            "1700000600",
        ],
    );
    assert!(by_completion.status.success());
    let out = stdout(&by_completion);
    assert!(out.contains("1 match the query"));
    assert!(out.contains("record run-meanrev"));

    // (4) By parameter set: window=5 tells meanrev apart; a different value matches nothing.
    let by_param = run(&dir, &["query", "--param", "window=5"]);
    assert!(stdout(&by_param).contains("1 match the query"));
    assert!(stdout(&by_param).contains("record run-meanrev"));
    let by_param_miss = run(&dir, &["query", "--param", "window=9"]);
    assert!(stdout(&by_param_miss).contains("0 match the query"));

    // (5) Combined query ANDs every axis down to the single momentum run.
    let combined = run(
        &dir,
        &[
            "query",
            "--strategy",
            "momentum",
            "--from",
            "0",
            "--to",
            "100",
            "--param",
            "lookback=20",
            "--param",
            "threshold=0.5",
        ],
    );
    assert!(combined.status.success());
    let out = stdout(&combined);
    assert!(out.contains("1 match the query"));
    assert!(out.contains("record run-momentum"));

    // (6) --full renders the COMPLETE trade log and equity curve, so interior fills and equity
    // marks (the ones the first/last summary omits) are recoverable from the query output.
    let full = run(&dir, &["query", "--strategy", "momentum", "--full"]);
    assert!(full.status.success());
    let out = stdout(&full);
    assert!(out.contains("fill[0] ts=1 symbol=AAPL qty=10 price_minor=100"));
    assert!(out.contains("fill[1] ts=5 symbol=AAPL qty=-10 price_minor=125"));
    // Interior equity points (ts 2, 3, 4) are absent from the summary but present with --full.
    assert!(out.contains("equity[1] ts=2 equity_minor=1000188"));
    assert!(out.contains("equity[2] ts=3 equity_minor=999888"));
    assert!(out.contains("equity[3] ts=4 equity_minor=1000288"));

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_cli_query_kv_format_is_indexed_proof_lines() {
    // `--format kv --full` emits the flat, indexed record.<i>.<field> proof lines the SRS-UI-004
    // dashboard history view parses: a leading record_count, contiguous indices, every artifact on
    // its own key:value line (value after the FIRST colon so `sha:deadbeef` survives), and the
    // interior fills + equity points under --full for the drill-down chart.
    let dir = temp_dir("kv_format");
    assert!(run(&dir, &["persist", "--init"]).status.success());

    let out = stdout(&run(&dir, &["query", "--format", "kv", "--full"]));
    // The record_count anchors the contiguous 0..N indexing the consumer fails closed against.
    assert!(
        out.contains("record_count:2"),
        "kv must lead with record_count"
    );
    // Scalar artifacts, one per line, both records present and index-contiguous.
    assert!(out.contains("record.0.run_id:run-momentum"));
    assert!(out.contains("record.1.run_id:run-meanrev"));
    // A colon-bearing value survives (parser splits on the FIRST colon only).
    assert!(out.contains("record.0.code_version:sha:deadbeef"));
    assert!(out.contains("record.0.completed_at:1700000000"));
    assert!(out.contains("record.0.run_window_start:0"));
    assert!(out.contains("record.0.run_window_end:100"));
    // All eight metrics are emitted (real, full-precision — never a fabricated 0).
    for metric in [
        "metric.sharpe:",
        "metric.sortino:",
        "metric.alpha:",
        "metric.beta:",
        "metric.max_drawdown:",
        "metric.annualized_return:",
        "metric.annualized_volatility:",
        "metric.win_rate:",
    ] {
        assert!(
            out.contains(&format!("record.0.{metric}")),
            "kv missing {metric}"
        );
    }
    // Benchmark comparison identity + indexed parameters.
    assert!(out.contains("record.0.comparison.benchmark_symbol:SPY"));
    assert!(out.contains("record.0.comparison.is_default:true"));
    assert!(out.contains("record.0.param_count:2"));
    assert!(out.contains("record.0.param.0.key:lookback"));
    assert!(out.contains("record.0.param.0.value:20"));
    // --full renders every interior fill + equity point for the drill-down.
    assert!(out.contains("record.0.trade_count:2"));
    assert!(out.contains("record.0.equity_count:5"));
    assert!(out.contains("record.0.trade.0.ts:1"));
    assert!(out.contains("record.0.trade.1.quantity:-10"));
    assert!(out.contains("record.0.equity.2.ts:3"));
    assert!(out.contains("record.0.equity.2.equity_minor:999888"));
    // No human preamble leaks into the machine format.
    assert!(
        !out.contains("match the query"),
        "kv must not emit the human preamble"
    );
    assert!(
        !out.contains("filters:"),
        "kv must not emit the human preamble"
    );

    // Without --full the per-entry detail is omitted but the summary counts remain.
    let summary = stdout(&run(&dir, &["query", "--format", "kv"]));
    assert!(summary.contains("record.0.trade_count:2"));
    assert!(!summary.contains("record.0.trade.0.ts:"));

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_cli_query_bad_format_fails_closed() {
    // An unknown --format value is rejected rather than silently defaulting to a rendering the
    // consumer cannot parse.
    let dir = temp_dir("bad_format");
    assert!(run(&dir, &["persist", "--init"]).status.success());
    let output = run(&dir, &["query", "--format", "xml"]);
    assert!(
        !output.status.success(),
        "an unknown --format must exit non-zero"
    );
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_cli_query_kv_rejects_control_char_in_value() {
    // The store does not forbid a control character in a parameter value at persist time, so a
    // newline-bearing value could forge a `record.<i>.<field>:` proof line in the flat kv machine
    // format. The kv emitter fails CLOSED on such a value (unforgeable by construction) — while the
    // human rendering, which is not a line-forgeable machine grammar, is unaffected.
    let dir = temp_dir("kv_control_char");
    let persisted = run(
        &dir,
        &[
            "persist",
            "--init",
            "--run-id",
            "run-evil",
            "--strategy",
            "evil",
            "--completed-at",
            "100",
            "--param",
            "k=v\nrecord.0.run_id:HACKED",
        ],
    );
    assert!(
        persisted.status.success(),
        "the store accepts the value verbatim (persist is not the guard): {}",
        String::from_utf8_lossy(&persisted.stderr)
    );
    let kv = run(&dir, &["query", "--format", "kv", "--full"]);
    assert!(
        !kv.status.success(),
        "kv must fail closed on a control-char (forgeable) value"
    );
    assert!(
        String::from_utf8_lossy(&kv.stderr).contains("control character"),
        "the failure names the control-character guard"
    );
    // The human rendering still round-trips the stored record.
    assert!(run(&dir, &["query"]).status.success());
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_cli_persist_preserves_existing_history() {
    // Regression for the persist-clobbers-history hazard: a persist must LOAD existing records and
    // accumulate, never replace the store with only its own fixtures.
    let dir = temp_dir("preserve_history");

    // Seed the default demo pair into a fresh directory.
    let first = run(&dir, &["persist", "--init"]);
    assert!(first.status.success());
    assert!(stdout(&first).contains("store now holds 2 result(s)"));

    // Persist a distinct operator-labeled run into the same directory.
    let second = run(
        &dir,
        &[
            "persist",
            "--run-id",
            "run-extra",
            "--strategy",
            "extra",
            "--completed-at",
            "1700001000",
            "--param",
            "k=v",
        ],
    );
    assert!(second.status.success());
    assert!(stdout(&second).contains("store now holds 3 result(s) (1 new this run)"));

    // All three records survive — the pre-existing pair was not clobbered by the second persist.
    let all = run(&dir, &["query"]);
    assert!(all.status.success());
    let out = stdout(&all);
    assert!(out.contains("3 match the query"));
    assert!(out.contains("record run-momentum"));
    assert!(out.contains("record run-meanrev"));
    assert!(out.contains("record run-extra"));

    // Re-seeding the demo pair is idempotent: the run ids already present are left untouched.
    let reseed = run(&dir, &["persist"]);
    assert!(reseed.status.success());
    assert!(stdout(&reseed).contains("store now holds 3 result(s) (0 new this run)"));

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_cli_persist_duplicate_run_id_fails_closed() {
    // An operator-labeled persist whose run id already exists must fail closed rather than silently
    // replace the existing run's results.
    let dir = temp_dir("duplicate_run_id");
    assert!(run(&dir, &["persist", "--init"]).status.success()); // seeds run-momentum

    let dup = run(
        &dir,
        &[
            "persist",
            "--run-id",
            "run-momentum",
            "--strategy",
            "momentum",
            "--completed-at",
            "5",
        ],
    );
    assert!(
        !dup.status.success(),
        "a duplicate run id must exit non-zero"
    );

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_cli_missing_directory_fails_closed() {
    // The configured directory was never provisioned (unmounted / deleted / misconfigured): a query
    // must fail closed rather than report an empty history that silently drops persisted runs.
    let dir = temp_dir("missing_dir");
    let output = run(&dir, &["query"]);
    assert!(
        !output.status.success(),
        "a missing results directory must exit non-zero"
    );
}

#[test]
fn srs_bt_009_cli_load_present_but_empty_directory_is_empty() {
    // A provisioned directory that has never been persisted to is a legitimate fresh install: the
    // query succeeds and reports zero records (distinct from the missing-directory failure above).
    let dir = temp_dir("empty_dir");
    fs::create_dir_all(&dir).unwrap();
    let output = run(&dir, &["query"]);
    assert!(output.status.success());
    assert!(stdout(&output).contains("loaded 0 record(s)"));
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_cli_persist_missing_directory_without_init_fails_closed() {
    // Without --init, persisting into a non-existent directory fails closed (it is an unmounted /
    // deleted / mistyped path), rather than silently forking a fresh history at the wrong location.
    let dir = temp_dir("persist_missing_dir");
    let output = run(&dir, &["persist"]);
    assert!(
        !output.status.success(),
        "persist into a missing directory without --init must exit non-zero"
    );
    assert!(
        !dir.join("backtest_results.store").exists(),
        "a fail-closed persist must not create a store file"
    );
}

#[test]
fn srs_bt_009_cli_persist_init_creates_fresh_directory() {
    // --init is the explicit fresh-install path: it creates a brand-new directory and persists.
    let dir = temp_dir("persist_init");
    let output = run(&dir, &["persist", "--init"]);
    assert!(output.status.success());
    assert!(dir.join("backtest_results.store").exists());
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_cli_unknown_subcommand_fails_closed() {
    let dir = temp_dir("unknown_cmd");
    let output = run(&dir, &["frobnicate"]);
    assert!(
        !output.status.success(),
        "an unknown subcommand must exit non-zero"
    );
}

#[test]
fn srs_bt_009_cli_half_specified_date_window_fails_closed() {
    // A half-open date axis is ambiguous, so the CLI rejects it rather than guessing a bound.
    let dir = temp_dir("half_window");
    fs::create_dir_all(&dir).unwrap();
    let output = run(&dir, &["query", "--from", "50"]);
    assert!(
        !output.status.success(),
        "a half-specified date window must exit non-zero"
    );
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_cli_no_directory_configured_fails_closed() {
    // Neither --dir nor ATP_BACKTEST_RESULTS_DIR: the CLI fails closed instead of defaulting to an
    // implicit path (e.g. the working directory).
    let output = Command::new(CLI)
        .arg("query")
        .env_remove("ATP_BACKTEST_RESULTS_DIR")
        .output()
        .expect("the bt009_store_cli binary runs");
    assert!(
        !output.status.success(),
        "no configured results directory must exit non-zero"
    );
}

#[test]
fn srs_bt_009_cli_env_var_supplies_directory() {
    // The ATP_BACKTEST_RESULTS_DIR config key (read here as an env var) is the directory source when
    // --dir is omitted: persist with it, then query with it, and get the persisted run back.
    let dir = temp_dir("env_var");
    let persist = Command::new(CLI)
        .args(["persist", "--init"])
        .env("ATP_BACKTEST_RESULTS_DIR", &dir)
        .output()
        .expect("the bt009_store_cli binary runs");
    assert!(persist.status.success());

    let query = Command::new(CLI)
        .args(["query", "--strategy", "meanrev"])
        .env("ATP_BACKTEST_RESULTS_DIR", &dir)
        .output()
        .expect("the bt009_store_cli binary runs");
    assert!(query.status.success());
    assert!(stdout(&query).contains("record run-meanrev"));

    let _ = fs::remove_dir_all(&dir);
}
