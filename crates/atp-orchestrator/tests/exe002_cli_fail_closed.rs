//! SRS-EXE-002 — the order-routing operator CLI must FAIL CLOSED at the
//! PROCESS level: malformed / unknown / duplicate / degenerate flags exit
//! NONZERO, so shell automation (and any runtime handler that shells this bin)
//! can never mistake a refused or violated routing run for a success. The
//! happy paths emit the deterministic `key:value` AC-10 proof lines.
//!
//! L4 boundary test: spawns the real `exe002_order_routing_cli` binary.

use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_exe002_order_routing_cli");

fn run(args: &[&str]) -> (bool, String, String) {
    let output = Command::new(BIN)
        .args(args)
        .output()
        .expect("run exe002_order_routing_cli");
    (
        output.status.success(),
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    )
}

#[test]
fn paper_only_route_emits_zero_ib_orders_and_passes() {
    let (ok, out, err) = run(&["route", "--paper-orders", "3"]);
    assert!(ok, "paper-only route failed: {err}");
    assert!(out.contains("srs:SRS-EXE-002"), "{out}");
    assert!(out.contains("scenario.paper_orders:3"), "{out}");
    assert!(out.contains("scenario.designated_live:-"), "{out}");
    assert!(out.contains("ib_orders_created:0"), "{out}");
    assert!(out.contains("simulated_orders_accepted:3"), "{out}");
    assert!(out.contains("resting_orders:3"), "{out}");
    assert!(out.contains("order.0.route:internal_simulation"), "{out}");
    assert!(out.contains("verdict:PASS"), "{out}");
    assert!(!out.contains("live_brokerage"), "{out}");
}

#[test]
fn designated_live_route_emits_exactly_one_ib_order() {
    let (ok, out, err) = run(&["route", "--paper-orders", "2", "--designate-live"]);
    assert!(ok, "mixed route failed: {err}");
    assert!(out.contains("scenario.designated_live:live-alpha"), "{out}");
    assert!(out.contains("ib_orders_created:1"), "{out}");
    assert!(out.contains("ac10.expected_ib_orders:1"), "{out}");
    assert!(out.contains("order.2.route:live_brokerage"), "{out}");
    assert!(out.contains("order.2.receipt:IB-1"), "{out}");
    assert!(out.contains("verdict:PASS"), "{out}");
}

#[test]
fn missing_subcommand_fails_closed() {
    let (ok, _, err) = run(&[]);
    assert!(!ok, "missing subcommand must exit nonzero");
    assert!(err.contains("missing subcommand"), "{err}");
}

#[test]
fn unknown_subcommand_fails_closed() {
    let (ok, _, err) = run(&["launch"]);
    assert!(!ok);
    assert!(err.contains("unknown subcommand"), "{err}");
}

#[test]
fn unknown_flag_fails_closed() {
    let (ok, _, err) = run(&["route", "--paper-orders", "3", "--force"]);
    assert!(!ok, "an unknown flag must exit nonzero");
    assert!(err.contains("unknown flag `--force`"), "{err}");
}

#[test]
fn missing_paper_orders_fails_closed() {
    let (ok, _, err) = run(&["route"]);
    assert!(!ok);
    assert!(err.contains("--paper-orders is required"), "{err}");
}

#[test]
fn valueless_paper_orders_fails_closed() {
    let (ok, _, err) = run(&["route", "--paper-orders"]);
    assert!(!ok);
    assert!(err.contains("--paper-orders requires a value"), "{err}");
}

#[test]
fn degenerate_paper_order_counts_fail_closed() {
    for degenerate in ["0", "-3", "3.5", "thirty", "10001"] {
        let (ok, _, err) = run(&["route", "--paper-orders", degenerate]);
        assert!(!ok, "--paper-orders {degenerate} must exit nonzero");
        assert!(
            !err.is_empty(),
            "--paper-orders {degenerate} must explain the refusal"
        );
    }
}

#[test]
fn duplicate_flags_fail_closed() {
    let (ok, _, err) = run(&["route", "--paper-orders", "2", "--paper-orders", "3"]);
    assert!(!ok);
    assert!(err.contains("duplicate --paper-orders"), "{err}");

    let (ok, _, err) = run(&[
        "route",
        "--paper-orders",
        "2",
        "--designate-live",
        "--designate-live",
    ]);
    assert!(!ok);
    assert!(err.contains("duplicate --designate-live"), "{err}");
}

#[test]
fn help_prints_usage() {
    let (ok, out, _) = run(&["help"]);
    assert!(ok);
    assert!(out.contains("SRS-EXE-002"), "{out}");
    assert!(out.contains("--paper-orders"), "{out}");
}
