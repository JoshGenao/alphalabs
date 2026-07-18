//! SRS-EXE-002 / SyRS SYS-2b / SYS-2e / AC-10 operator CLI — the
//! fixture-verification workflow for the order-routing dispatch: drive N
//! non-live (paper) strategy orders (+ optionally the one designated live
//! strategy) through the REAL `ExecutionEngine::dispatch_order` over the real
//! wired components — real
//! `LiveDesignation` authority, real `PaperSimulationEngine` +
//! `VirtualOrderBook` behind the `InternalSimulationSubmit` port, real
//! `InteractiveBrokersBrokerage` behind the `LiveBrokerageSubmit` port — with
//! the deterministic mocked-IB transport ([`RecordingIbGateway`]) recording
//! every order-creating wire operation. This verifies the routing invariant
//! over deterministic fixtures (scenario-authored synthetic strategy ids); it
//! is NOT the deployed strategy-runtime order path — real strategy-container
//! submissions through `dispatch_order` stay deferred to the SRS-SDK strategy
//! host / SRS-ORCH-* runtime (see `order_routing_contract.deferred[]`).
//!
//! Emits deterministic `key:value` proof lines (repo convention) and fails
//! closed on unknown / duplicate / valueless / degenerate flags. The bin
//! SELF-CHECKS the AC before exiting: a run where any paper order created an
//! IB order (or the designated live order failed to) exits nonzero with
//! `verdict:FAIL` — the tool refuses to print passing-looking evidence for a
//! violated invariant.
//!
//! The IB **paper account** is not touched here (or anywhere in this routing
//! runtime): the only paper-account surface is the operator-initiated
//! SRS-EXE-006 adapter integration test (`ATP_RUN_INTEGRATION=1` +
//! `--ignored`, port 4002) — the SYS-2e boundary this CLI's evidence
//! complements.

use std::env;
use std::process::ExitCode;

use atp_orchestrator::order_routing_wiring::{
    run_routing_scenario, RoutingScenario, MAX_SCENARIO_PAPER_ORDERS,
};

const USAGE: &str = "\
exe002_order_routing_cli — SRS-EXE-002 route all non-live strategy orders to the
internal simulation engine (paper orders never create IB orders; SyRS AC-10)

USAGE:
    exe002_order_routing_cli route --paper-orders <N> [--designate-live]
    exe002_order_routing_cli help

SUBCOMMANDS:
    route   Dispatch <N> distinct paper-strategy orders (cycling all four order
            types) through the real ExecutionEngine::dispatch_order wiring;
            with --designate-live, the single operator-confirmed live strategy
            submits one more order that must be the ONLY IB order created.
    help    Print this help.

route FLAGS:
    --paper-orders <N>   how many non-live strategies each submit one order
                         (1..=10000; required)
    --designate-live     also designate `live-alpha` live (explicit operator
                         confirmation, SRS-EXE-001) and submit its order

Exit is nonzero unless every paper order routed to the internal simulation
engine and the mocked-IB gateway recorded exactly the expected order-creating
wire operations (0 without --designate-live, 1 with).";

fn main() -> ExitCode {
    match run(env::args().skip(1).collect()) {
        Ok(output) => {
            print!("{output}");
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("{message}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: Vec<String>) -> Result<String, String> {
    let mut args = args.into_iter();
    let subcommand = args
        .next()
        .ok_or_else(|| format!("missing subcommand\n\n{USAGE}"))?;
    match subcommand.as_str() {
        "help" | "--help" | "-h" => {
            let trailing: Vec<String> = args.collect();
            if !trailing.is_empty() {
                return Err(format!("`help` takes no flags\n\n{USAGE}"));
            }
            Ok(format!("{USAGE}\n"))
        }
        "route" => route(args.collect()),
        other => Err(format!("unknown subcommand `{other}`\n\n{USAGE}")),
    }
}

fn route(args: Vec<String>) -> Result<String, String> {
    let mut paper_orders: Option<u32> = None;
    let mut designate_live = false;

    let mut iter = args.into_iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--paper-orders" => {
                if paper_orders.is_some() {
                    return Err(format!("duplicate --paper-orders\n\n{USAGE}"));
                }
                let raw = iter
                    .next()
                    .ok_or_else(|| format!("--paper-orders requires a value\n\n{USAGE}"))?;
                let value: u32 = raw.parse().map_err(|_| {
                    format!(
                        "--paper-orders `{raw}` is not a whole number in \
                         1..={MAX_SCENARIO_PAPER_ORDERS}\n\n{USAGE}"
                    )
                })?;
                paper_orders = Some(value);
            }
            "--designate-live" => {
                if designate_live {
                    return Err(format!("duplicate --designate-live\n\n{USAGE}"));
                }
                designate_live = true;
            }
            other => return Err(format!("unknown flag `{other}`\n\n{USAGE}")),
        }
    }

    let paper_orders =
        paper_orders.ok_or_else(|| format!("--paper-orders is required\n\n{USAGE}"))?;
    let scenario = RoutingScenario {
        paper_orders,
        designate_live,
    };
    // validate() rejects 0 and >MAX before any dispatch (fail closed).
    scenario
        .validate()
        .map_err(|err| format!("{err}\n\n{USAGE}"))?;

    let evidence = run_routing_scenario(&scenario)?;

    let mut out = String::new();
    out.push_str("srs:SRS-EXE-002\n");
    out.push_str(&format!(
        "scenario.paper_orders:{}\n",
        scenario.paper_orders
    ));
    out.push_str(&format!(
        "scenario.designated_live:{}\n",
        evidence.designated.as_deref().unwrap_or("-")
    ));
    for (index, order) in evidence.orders.iter().enumerate() {
        out.push_str(&format!(
            "order.{index}.strategy:{}\norder.{index}.symbol:{}\norder.{index}.route:{}\n\
             order.{index}.receipt:{}\n",
            order.strategy, order.symbol, order.route, order.receipt
        ));
    }
    out.push_str(&format!(
        "simulated_orders_accepted:{}\n",
        evidence.simulated_orders_accepted
    ));
    out.push_str(&format!("resting_orders:{}\n", evidence.resting_orders));
    out.push_str(&format!(
        "ib_orders_created:{}\n",
        evidence.ib_orders_created
    ));

    // The AC-10 self-check: paper orders must all have routed to the internal
    // simulation engine, and the ONLY permitted IB order is the designated live
    // strategy's. Refuse to exit 0 on a violation.
    let expected_ib = u32::from(scenario.designate_live);
    let paper_ok = evidence.simulated_orders_accepted == scenario.paper_orders;
    let ib_ok = evidence.ib_orders_created == expected_ib;
    out.push_str(&format!(
        "ac10.expected_ib_orders:{expected_ib}\nac10.paper_orders_routed_to_simulation:{}\n",
        paper_ok
    ));
    if paper_ok && ib_ok {
        out.push_str("verdict:PASS\n");
        Ok(out)
    } else {
        Err(format!(
            "{out}verdict:FAIL\nSRS-EXE-002 violation: expected {expected_ib} IB order(s), \
             observed {}; {} of {} paper orders routed to simulation",
            evidence.ib_orders_created, evidence.simulated_orders_accepted, scenario.paper_orders
        ))
    }
}
