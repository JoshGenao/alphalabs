//! SRS-MD-005 scheduled-restart-window operator-CLI integration test.
//!
//! Drives the `md005_connectivity_restart_window_cli` binary the way an
//! operator would — in fresh OS processes via the `CARGO_BIN_EXE_*` path Cargo
//! wires for integration tests — and asserts every clause of SyRS SYS-75
//! through the production authority chain and a REAL TCP endpoint:
//!
//!   (a) 60 s before the expected restart, order submission AND market-data
//!       requests are suspended, with ZERO IB orders created;
//!   (b) the connectivity notification is SUPPRESSED and nothing reaches a
//!       transport;
//!   (c) a reconnection attempt is requested, and a gateway that answers again
//!       inside the window resumes normal operations;
//!   (d) a gateway still unreachable after the window escalates to
//!       `Unreachable` WITHOUT the maintenance marker, and the notification is
//!       DISPATCHED.
//!
//! Plus the non-vacuity boundary in all three directions: each `--inject`
//! forces the opposite class, so the proof cannot be derived and the run must
//! fail closed with NO proof line. Without those, a gate that refused
//! everything — silently disabling live trading and market data — would pass
//! the suspension case and look correct.
//!
//! The endpoints are loopback ports the binary binds and releases itself. It
//! never touches 4001/4002, so this suite is safe beside other agents.

use std::process::{Command, Output};

/// The `[[bin]]` path Cargo wires for integration tests.
const CLI: &str = env!("CARGO_BIN_EXE_md005_connectivity_restart_window_cli");

/// Every proof headline. None may appear under an injected fault or a rejected
/// parse.
const PROOF_LINES: [&str; 3] = [
    "restart-window-suspension-proven:true",
    "restart-window-escalation-proven:true",
    "restart-window-resume-proven:true",
];

/// True if ANY exact success sentinel appears anywhere in the output, even
/// mid-line.
///
/// A safety-evidence CLI must never emit a success token on a failure path: an
/// operator or CI check that greps the RAW output — rather than parsing
/// standalone lines plus exit status — would otherwise false-positive. So the
/// USAGE and error text must not contain the sentinels either, and a success
/// line is BOTH a standalone `:true` line AND the only place the sentinel
/// appears at all.
fn contains_success_sentinel(out: &str) -> bool {
    PROOF_LINES.iter().any(|sentinel| out.contains(sentinel))
}

fn run(args: &[&str]) -> Output {
    Command::new(CLI)
        .args(args)
        // Clear the catalogue keys so a developer's shell cannot change what
        // these tests measure. The env-driven cases below set them explicitly.
        .env_remove("ATP_IB_RESTART_WINDOW_SECONDS")
        .env_remove("ATP_IB_RESTART_SUSPEND_LEAD_SECONDS")
        .output()
        .expect("the md005_connectivity_restart_window_cli binary runs")
}

fn run_with_env(args: &[&str], key: &str, value: &str) -> Output {
    Command::new(CLI)
        .args(args)
        .env_remove("ATP_IB_RESTART_WINDOW_SECONDS")
        .env_remove("ATP_IB_RESTART_SUSPEND_LEAD_SECONDS")
        .env(key, value)
        .output()
        .expect("the md005_connectivity_restart_window_cli binary runs")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is utf-8")
}

fn combined(output: &Output) -> String {
    stdout(output) + &String::from_utf8(output.stderr.clone()).expect("stderr is utf-8")
}

/// Extract a `key:value` field from a space-separated line.
fn field<'a>(line: &'a str, key: &str) -> &'a str {
    let needle = format!("{key}:");
    let token = line
        .split_whitespace()
        .find(|tok| tok.starts_with(&needle))
        .unwrap_or_else(|| panic!("line missing `{key}`:\n{line}"));
    &token[needle.len()..]
}

fn line_starting<'a>(out: &'a str, prefix: &str) -> &'a str {
    out.lines()
        .find(|line| line.starts_with(prefix))
        .unwrap_or_else(|| panic!("output missing a `{prefix}` line:\n{out}"))
}

fn assert_proved(output: &Output, sentinel: &str) {
    let out = stdout(output);
    assert!(
        output.status.success(),
        "expected exit 0, got {:?}\n{}",
        output.status.code(),
        combined(output)
    );
    assert!(
        out.lines().any(|line| line == sentinel),
        "missing the standalone proof line `{sentinel}`:\n{out}"
    );
    assert!(
        out.contains("transports:FIXTURE"),
        "every path must self-label its transport tier so a drill is never mistaken \
         for live evidence:\n{out}"
    );
}

/// A failure must exit non-zero AND print no sentinel anywhere. Both halves
/// matter: an exit code nobody reads and a proof line nobody should trust are
/// different failures, and the second is the one that ships a false green.
fn assert_failed_closed(output: &Output, label: &str) {
    assert!(
        !output.status.success(),
        "{label}: expected a non-zero exit\n{}",
        combined(output)
    );
    assert!(
        !combined(output).contains("panicked at"),
        "{label}: refused by PANICKING, which prints no reason an operator can read\n{}",
        combined(output)
    );
    assert!(
        !contains_success_sentinel(&combined(output)),
        "{label}: a failure path emitted a success sentinel\n{}",
        combined(output)
    );
}

// --------------------------------------------------------------------------- //
// SyRS SYS-75(a) + (b) — suspension and suppression
// --------------------------------------------------------------------------- //

#[test]
fn suspension_blocks_orders_and_market_data_and_suppresses_the_alert() {
    let output = run(&["prove-suspension"]);
    assert_proved(&output, "restart-window-suspension-proven:true");
    let out = stdout(&output);

    let window = line_starting(&out, "window ");
    assert_eq!(
        field(window, "phase"),
        "Suspending",
        "the default instant must land in the 60 s pre-restart lead"
    );

    let gateway = line_starting(&out, "gateway ");
    assert_eq!(field(gateway, "state"), "ScheduledRestartWindow");
    assert_eq!(
        field(gateway, "reachability"),
        "NOT_PROBED",
        "the evidence path must not probe during the lead either: the gateway serves \
         ONE API client, and a reporting tool that spent the slot would break in the \
         tool the guarantee it reports on"
    );
    assert_eq!(
        field(gateway, "scheduled_restart"),
        "true",
        "the maintenance marker is what the notification dispatcher matches on"
    );

    let order = line_starting(&out, "live-order ");
    assert_eq!(field(order, "outcome"), "BLOCKED");
    assert_eq!(
        field(order, "detail"),
        "CONNECTIVITY_BLOCKED/IbGatewayUnreachable"
    );

    let witness = line_starting(&out, "witness ");
    assert_eq!(
        field(witness, "ib-orders-created"),
        "0",
        "a suspended submission must create ZERO IB orders — that is what proves the \
         refusal happened at the gate and not at the broker"
    );
    assert_eq!(
        field(witness, "reconnects"),
        "1",
        "SyRS SYS-75(c): the gate must request a reconnection attempt"
    );

    let market_data = line_starting(&out, "market-data ");
    assert_eq!(
        field(market_data, "admitted"),
        "false",
        "SyRS SYS-75(a) suspends market-data requests alongside order submission"
    );
    assert_eq!(field(market_data, "admission"), "SCHEDULED_RESTART_WINDOW");

    // The MUTATING admission point — the call that actually opens an upstream
    // IB line. Refusing at the outer manager while the registry still opened
    // one would leave the suspension enforced only on the surface.
    let registry = line_starting(&out, "registry ");
    assert_eq!(field(registry, "lines-opened"), "0");
    assert_eq!(field(registry, "refusal"), "SuspendedForScheduledRestart");

    let alerts = line_starting(&out, "alerts ");
    assert_eq!(field(alerts, "disposition"), "SUPPRESSED");
    assert_eq!(
        field(alerts, "messages-sent"),
        "0",
        "SyRS SYS-75(b): a suppressed notification must send nothing"
    );

    assert!(
        out.contains("route:internal_simulation"),
        "the non-designated paper contrast must still simulate during the window:\n{out}"
    );
}

#[test]
fn the_suspension_proof_cannot_be_printed_from_inside_the_window() {
    // The reviewer's false green. Every OTHER check in the suspension proof is
    // also satisfied inside the restart window — the state is still
    // ScheduledRestartWindow, the order is still blocked, the alert is still
    // suppressed — so without asserting the phase the tool printed the
    // SYS-75(a) PRE-restart proof for a lead it never entered. `--inject`
    // cannot catch this, because that control overrides the instant itself.
    let inside_window = (1_788_493_500_000_000_000i64 + 60 * 1_000_000_000).to_string();
    let output = run(&[
        "prove-suspension",
        "--restart-ns",
        "1788493500000000000",
        "--now-ns",
        &inside_window,
    ]);
    assert_failed_closed(&output, "prove-suspension at restart+60s");
    assert_eq!(
        field(line_starting(&stdout(&output), "window "), "phase"),
        "Restarting",
        "the run must show WHICH phase it was actually in"
    );
}

#[test]
fn a_one_second_lead_still_lands_inside_the_lead() {
    // The same false green arrived with NO flags at all under the
    // catalogue-legal ATP_IB_RESTART_SUSPEND_LEAD_SECONDS=1, because the
    // default instant was `restart_ns - lead_ns / 2` and integer division
    // collapsed it onto `restart_ns`. The default is now the first instant of
    // the lead, which is inside it for every legal lead.
    let output = run_with_env(
        &["prove-suspension"],
        "ATP_IB_RESTART_SUSPEND_LEAD_SECONDS",
        "1",
    );
    assert_proved(&output, "restart-window-suspension-proven:true");
    assert_eq!(
        field(line_starting(&stdout(&output), "window "), "phase"),
        "Suspending"
    );
}

#[test]
fn the_resume_proof_cannot_be_printed_from_outside_the_window() {
    // The same rule for the third proof: a resume that passed outside the
    // window would be describing an ordinary healthy gateway, not SYS-75(c)/(d).
    let before = (1_788_493_500_000_000_000i64 - 3_600 * 1_000_000_000).to_string();
    let output = run(&[
        "prove-resume",
        "--restart-ns",
        "1788493500000000000",
        "--now-ns",
        &before,
    ]);
    assert_failed_closed(&output, "prove-resume an hour before the restart");
}

#[test]
fn suspension_cannot_be_derived_outside_the_window() {
    // The non-vacuity control. Move the instant outside the window and the
    // suspension has nothing to prove — so it must fail closed rather than
    // print a proof line about a gate that was never engaged.
    let output = run(&["prove-suspension", "--inject", "outside-window"]);
    assert_failed_closed(&output, "prove-suspension --inject outside-window");
}

// --------------------------------------------------------------------------- //
// SyRS SYS-75 escalation
// --------------------------------------------------------------------------- //

#[test]
fn a_gateway_still_dead_after_the_window_escalates_and_pages() {
    let output = run(&["prove-escalation"]);
    assert_proved(&output, "restart-window-escalation-proven:true");
    let out = stdout(&output);

    assert_eq!(field(line_starting(&out, "window "), "phase"), "Elapsed");

    let gateway = line_starting(&out, "gateway ");
    // The non-vacuity partner for NOT_PROBED above: outside the lead the
    // evidence path DOES probe, so "not probed" is a fact about the lead rather
    // than about a tool that never probes.
    assert_eq!(field(gateway, "reachability"), "UNREACHABLE");
    assert_eq!(
        field(gateway, "state"),
        "Unreachable",
        "after the window a dead gateway is an outage, not maintenance"
    );
    assert_eq!(
        field(gateway, "scheduled_restart"),
        "false",
        "the maintenance marker must be absent — that flag is what silences the page"
    );

    let market_data = line_starting(&out, "market-data ");
    assert_eq!(
        field(market_data, "admission"),
        "CONNECTIVITY_LOST",
        "the market-data refusal must read as an outage after the window, or the \
         operator is told to wait out an incident"
    );
    assert_eq!(field(market_data, "refusal"), "IbGatewayUnreachable");

    let alerts = line_starting(&out, "alerts ");
    assert_eq!(field(alerts, "disposition"), "DISPATCHED");
    assert!(
        field(alerts, "messages-sent").parse::<u32>().unwrap() > 0,
        "SyRS SYS-46: an escalated notification must actually reach the transports:\n{out}"
    );

    assert_eq!(
        field(line_starting(&out, "witness "), "ib-orders-created"),
        "0"
    );
}

#[test]
fn escalation_cannot_be_derived_inside_the_window() {
    // The non-vacuity control, and the sharpest one: the SAME dead gateway one
    // instant earlier is planned maintenance. If this passed, the escalation
    // proof would be describing the gateway rather than the window.
    let output = run(&["prove-escalation", "--inject", "inside-window"]);
    assert_failed_closed(&output, "prove-escalation --inject inside-window");
    assert!(
        stdout(&output).contains("state:ScheduledRestartWindow"),
        "the injected run must show WHY it could not escalate:\n{}",
        stdout(&output)
    );
}

// --------------------------------------------------------------------------- //
// SyRS SYS-75(c)/(d) — resume
// --------------------------------------------------------------------------- //

#[test]
fn a_gateway_that_returns_inside_the_window_resumes_orders_and_market_data() {
    let output = run(&["prove-resume"]);
    assert_proved(&output, "restart-window-resume-proven:true");
    let out = stdout(&output);

    let gateway = line_starting(&out, "gateway ");
    assert_eq!(field(gateway, "reachability"), "REACHABLE");
    assert_eq!(field(gateway, "state"), "Connected");

    assert_eq!(
        field(line_starting(&out, "live-order "), "outcome"),
        "ROUTED_THROUGH"
    );

    let witness = line_starting(&out, "witness ");
    assert_eq!(
        field(witness, "ib-orders-created"),
        "1",
        "the positive control: exactly one IB order, so the `0` in the suspension \
         and escalation proofs is meaningful rather than a broker that never works"
    );
    assert_eq!(field(witness, "reconnects"), "0");

    assert_eq!(
        field(line_starting(&out, "market-data "), "admitted"),
        "true"
    );
    // The positive control for the zero in the suspension proof: the SAME
    // mutating call opens exactly one line here.
    assert_eq!(
        field(line_starting(&out, "registry "), "lines-opened"),
        "1",
        "so `lines-opened:0` while suspended is a fact about the suspension, not \
         about a registry that never opens anything"
    );
    assert_eq!(
        field(line_starting(&out, "alerts "), "disposition"),
        "NO_EVENT",
        "a healthy gateway must never fabricate a connectivity event"
    );
}

#[test]
fn resume_cannot_be_derived_against_a_dead_gateway() {
    let output = run(&["prove-resume", "--inject", "dead-gateway"]);
    assert_failed_closed(&output, "prove-resume --inject dead-gateway");
}

// --------------------------------------------------------------------------- //
// Argument handling — every rejection is fail-closed
// --------------------------------------------------------------------------- //

#[test]
fn every_rejected_invocation_fails_closed() {
    for args in [
        vec![],
        vec!["bogus"],
        vec!["prove-suspension", "--unknown-flag"],
        vec!["prove-suspension", "--inject", "nonsense"],
        // Each subcommand accepts only its OWN opposite class, so a fault that
        // would not actually contradict the proof cannot be smuggled in.
        vec!["prove-suspension", "--inject", "dead-gateway"],
        vec!["prove-escalation", "--inject", "outside-window"],
        vec!["prove-resume", "--inject", "inside-window"],
        vec!["prove-resume", "--now-ns"],
        vec!["prove-suspension", "--now-ns", "not-a-number"],
        // A malformed window must be refused at construction, not applied.
        vec!["prove-suspension", "--lead-seconds", "0"],
        vec!["prove-suspension", "--window-seconds", "-1"],
        // `--help` is honoured only as the sole argument: accepting it here
        // would exit 0 having proved nothing.
        vec!["prove-suspension", "--help"],
        // An instant derived from --restart-ns must REFUSE on overflow, not
        // panic: a panic prints no proof line but also no reason an operator
        // can read, and the arithmetic runs before RestartWindow::new's own
        // checked refusal can fire.
        vec!["prove-escalation", "--restart-ns", "9223372036854775807"],
        vec!["prove-resume", "--restart-ns", "9223372036854775807"],
        vec!["prove-suspension", "--restart-ns", "0"],
    ] {
        let output = run(&args);
        assert_failed_closed(&output, &format!("{args:?}"));
    }
}

#[test]
fn help_is_available_alone_and_carries_no_success_sentinel() {
    let output = run(&["--help"]);
    assert!(output.status.success());
    let out = combined(&output);
    assert!(out.contains("prove-suspension"));
    assert!(
        !contains_success_sentinel(&out),
        "USAGE text must not contain a proof sentinel — a grep over raw output \
         would then see a success that never happened:\n{out}"
    );
}

#[test]
fn identical_inputs_produce_identical_output_across_processes() {
    // The instant is a flag and nothing reads the wall clock, so two runs must
    // agree byte for byte. A tool whose evidence changed between runs could not
    // be re-run by the integrator to confirm a recorded step.
    let first = run(&["prove-suspension", "--now-ns", "1788493470000000000"]);
    let second = run(&["prove-suspension", "--now-ns", "1788493470000000000"]);
    assert_proved(&first, "restart-window-suspension-proven:true");
    assert_eq!(
        stdout(&first),
        stdout(&second),
        "the CLI must be deterministic for a fixed instant"
    );
}

#[test]
fn the_catalogue_keys_actually_move_the_window() {
    // SRS-ARCH-005 catalogues ATP_IB_RESTART_WINDOW_SECONDS and describes it as
    // controlling the suspension window. Before this, it validated and changed
    // nothing — the binary read compiled-in constants — so a documented knob
    // moved no behaviour, which would have been discovered during a restart.
    // Held at a FIXED instant so only the configuration varies.
    let restart_ns: i64 = 1_788_493_500_000_000_000;
    let just_past_default = (restart_ns + 301 * 1_000_000_000).to_string();
    let args = [
        "prove-escalation",
        "--restart-ns",
        "1788493500000000000",
        "--now-ns",
        &just_past_default,
    ];

    let default_window = run(&args);
    assert_proved(&default_window, "restart-window-escalation-proven:true");

    let widened = run_with_env(&args, "ATP_IB_RESTART_WINDOW_SECONDS", "900");
    assert_failed_closed(&widened, "ATP_IB_RESTART_WINDOW_SECONDS=900");
    assert_eq!(
        field(line_starting(&stdout(&widened), "gateway "), "state"),
        "ScheduledRestartWindow",
        "a 15-minute window must still be suppressing at this instant"
    );
    assert_eq!(
        field(line_starting(&stdout(&widened), "window "), "window_ns"),
        "900000000000"
    );
}

#[test]
fn a_malformed_catalogue_key_is_refused_not_defaulted() {
    // A safety window on a schedule nobody chose is worse than a refusal the
    // operator can read, and an EMPTY value is usually a variable that expanded
    // to nothing rather than a deliberate unset.
    for value in ["0", "-5", "", "   ", "not-a-number"] {
        let output = run_with_env(
            &["prove-escalation"],
            "ATP_IB_RESTART_WINDOW_SECONDS",
            value,
        );
        assert_failed_closed(&output, &format!("ATP_IB_RESTART_WINDOW_SECONDS={value:?}"));
    }
    // Non-vacuity: a WELL-FORMED value is accepted, so the refusals above are
    // about the values rather than about a parser that rejects everything.
    let ok = run_with_env(
        &["prove-escalation"],
        "ATP_IB_RESTART_WINDOW_SECONDS",
        "300",
    );
    assert_proved(&ok, "restart-window-escalation-proven:true");
}

#[test]
fn the_window_boundaries_are_operator_configurable() {
    // SyRS SYS-75 says the restart time and the window are configurable, so a
    // non-default lead has to move the suspension with it. Prove it by asking
    // for suspension at an instant that is OUTSIDE the default 60 s lead and
    // inside a widened one.
    let restart_ns: i64 = 1_788_493_500_000_000_000;
    let two_minutes_before = restart_ns - 120_000_000_000;

    let default_lead = run(&[
        "prove-suspension",
        "--now-ns",
        &two_minutes_before.to_string(),
    ]);
    assert_failed_closed(&default_lead, "120 s before, default 60 s lead");

    let widened = run(&[
        "prove-suspension",
        "--now-ns",
        &two_minutes_before.to_string(),
        "--lead-seconds",
        "300",
    ]);
    assert_proved(&widened, "restart-window-suspension-proven:true");
}
