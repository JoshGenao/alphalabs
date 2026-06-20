//! SRS-SIM-001 paper order-intake operator CLI.
//!
//! The operator-facing surface of "simulate paper strategy orders locally without routing to any
//! brokerage" (docs/SRS.md SRS-5.7 SRS-SIM-001; SyRS SYS-82 local paper order execution, SYS-3 order
//! types, SYS-4 multi-leg composite; StRS SN-1.29 / SN-1.08 / SN-1.24). The acceptance criterion is:
//! "Market, limit, stop, stop-limit, equity, option, and multi-leg orders are processed by the
//! simulation engine and create no IB API order calls." The intake engine
//! ([`PaperSimulationEngine::accept_order`] over a [`PaperOrderRequest`]) is already built and the
//! Rust integration test `srs_sim_001_paper_order_intake` already asserts the routing; this binary
//! makes it *operator-demonstrable* — the same precedent as the SRS-BT-002 / BT-003 / BT-009 / BT-010
//! / SIM-002 / SIM-003 CLIs (there is no Python↔Rust strategy host, so the operator workflow is
//! demonstrated over the Rust core, driving the real [`PaperSimulationEngine::accept_order`] path).
//!
//! - `types [--inject F]` — accept a single equity order of each of the four SYS-3 order types
//!   (market / limit / stop / stop-limit) and assert each routes to the internal simulation engine
//!   with the routed leg carrying the requested order type. Emit `all-order-types-routed:true` only
//!   if EVERY type routes internally as a single (non-composite) order (non-vacuous: each routing
//!   must be `InternalSimulation`, one leg, the leg's order type equal to the requested type).
//!
//! - `assets [--inject F]` — accept a single equity order and a single option order and assert both
//!   route to the internal simulation engine with the routed leg carrying the requested asset class.
//!   Emit `both-asset-classes-routed:true` only if BOTH asset classes route internally (non-vacuous:
//!   the routed leg's asset class must equal the requested one for each).
//!
//! - `multileg [--inject F]` — accept a two-leg option vertical spread (SYS-4) and assert it routes
//!   as ONE composite transaction whose legs fill atomically. Emit `composite-routed:true` only if
//!   the routing is composite AND carries exactly two legs AND both legs are options (non-vacuous: a
//!   non-composite or wrong-arity routing is refused as a proof).
//!
//! - `no-broker [--inject F]` — sweep every accepted order shape (every order type × asset class ×
//!   side single order, plus every option composite) and assert that EVERY routing is the internal
//!   simulation engine and none reaches a brokerage. Emit `no-ib-order-calls:true` only if at least
//!   one order was swept AND every routed order is `InternalSimulation` (the runtime witness of the
//!   compile-time guarantee: `OrderRouting` has no broker variant to construct).
//!
//! Fail closed: an unknown subcommand or flag exits non-zero. `--inject <fault>` corrupts an order
//! leg or a composite shape so intake MUST reject it BEFORE routing; the CLI prints
//! `inject=<fault>: paper order intake failed closed` with the engine error and exits non-zero with
//! NO proof line — a malformed order can never produce a routing "proof". The faults are:
//!   empty-symbol | nonpositive-quantity | nonpositive-limit | nonpositive-stop | empty-multileg |
//!   single-leg-composite | non-option-composite-leg
//! Every injected fault is asserted to return its specific [`OrderError`] variant (never `Ok`), so a
//! proof is never asserted over an input the intake layer's own rules would route or reject silently.

use std::env;
use std::process::ExitCode;

use atp_simulation::paper_order::{
    AssetClass, OrderError, OrderLeg, OrderRouting, OrderType, PaperOrderRequest, Side,
};
use atp_simulation::sim::PaperSimulationEngine;

// Deterministic fixture order parameters (integer minor units / cents). The trigger/limit prices sit
// in a normal relation (stop below limit) so every order is well-formed; intake performs no fill
// arithmetic, so these are only carried through to the routed leg.
const SYMBOL_EQUITY: &str = "AAPL";
const SYMBOL_OPTION: &str = "AAPL  240119C00190000";
const SYMBOL_OPTION_FAR: &str = "AAPL  240119C00200000";
const FIXTURE_QTY: i64 = 100;
const FIXTURE_OPTION_QTY: i64 = 2;
const LIMIT_MINOR: i64 = 9_400;
const STOP_MINOR: i64 = 9_500;

/// The four SYS-3 order types, with a stable label, in a fixed order.
fn all_order_types() -> [(&'static str, OrderType); 4] {
    [
        ("market", OrderType::Market),
        (
            "limit",
            OrderType::Limit {
                limit_price_minor: LIMIT_MINOR,
            },
        ),
        (
            "stop",
            OrderType::Stop {
                stop_price_minor: STOP_MINOR,
            },
        ),
        (
            "stop-limit",
            OrderType::StopLimit {
                stop_price_minor: STOP_MINOR,
                limit_price_minor: LIMIT_MINOR,
            },
        ),
    ]
}

const USAGE: &str = "\
sim001_paper_order_cli — SRS-SIM-001 paper order-intake operator workflow

USAGE:
    sim001_paper_order_cli types     [--inject <fault>]
    sim001_paper_order_cli assets    [--inject <fault>]
    sim001_paper_order_cli multileg  [--inject <fault>]
    sim001_paper_order_cli no-broker [--inject <fault>]

COMMANDS:
    types      Accept a single equity order of each SYS-3 order type (market / limit / stop /
               stop-limit) and prove each routes to the internal simulation engine
               (all-order-types-routed:true).
    assets     Accept a single equity order and a single option order and prove both route to the
               internal simulation engine (both-asset-classes-routed:true).
    multileg   Accept a two-leg option spread (SYS-4) and prove it routes as ONE composite
               transaction (composite-routed:true).
    no-broker  Sweep every accepted order shape and prove every routing is the internal simulation
               engine — no order reaches a brokerage (no-ib-order-calls:true).

RUN FLAGS:
    --inject <f>  corrupt an order so intake MUST fail closed before routing; one of:
                  empty-symbol | nonpositive-quantity | nonpositive-limit | nonpositive-stop |
                  empty-multileg | single-leg-composite | non-option-composite-leg

An injected fault is rejected before any proof line is printed, so the proof can never be vacuous or
fabricated.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("sim001_paper_order_cli: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let (command, rest) = match args.split_first() {
        Some(parts) => parts,
        None => return Err(format!("missing subcommand\n\n{USAGE}")),
    };
    match command.as_str() {
        "types" => cmd_types(rest),
        "assets" => cmd_assets(rest),
        "multileg" => cmd_multileg(rest),
        "no-broker" => cmd_no_broker(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

/// True if any token requests help, so a subcommand can show usage instead of erroring.
fn wants_help(args: &[String]) -> bool {
    args.iter()
        .any(|arg| matches!(arg.as_str(), "help" | "--help" | "-h"))
}

/// Accept each of the four SYS-3 order types as a single equity order and prove each routes
/// internally carrying the requested order type.
fn cmd_types(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let engine = PaperSimulationEngine::new();

    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_and_assert_fail_closed(&engine, fault);
    }

    let mut all_routed = true;
    for (label, order_type) in all_order_types() {
        let leg = equity_leg(order_type);
        let routing = engine
            .accept_order(&PaperOrderRequest::Single(leg))
            .map_err(|err| err.to_string())?;
        // Non-vacuous: a single equity order must route internally, as one non-composite leg whose
        // order type is the requested one.
        let routed = is_internal(&routing)
            && !routing.is_composite()
            && routing.legs().len() == 1
            && routing.legs()[0].order_type == order_type;
        all_routed &= routed;
        println!("type[{label}] {} routed:{routed}", render_routing(&routing));
    }

    if !all_routed {
        return Err(
            "all-order-types-routed:false — at least one SYS-3 order type did not route to the \
             internal simulation engine as expected (an intake regression)"
                .to_string(),
        );
    }
    println!("all-order-types-routed:true");
    Ok(())
}

/// Accept an equity and an option single order and prove both route internally carrying the
/// requested asset class.
fn cmd_assets(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let engine = PaperSimulationEngine::new();

    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_and_assert_fail_closed(&engine, fault);
    }

    let cases = [
        ("equity", equity_leg(OrderType::Market)),
        ("option", option_leg(Side::Buy, OrderType::Market)),
    ];
    let mut both_routed = true;
    for (label, leg) in cases {
        let requested_class = leg.asset_class;
        let routing = engine
            .accept_order(&PaperOrderRequest::Single(leg))
            .map_err(|err| err.to_string())?;
        // Non-vacuous: each asset class must route internally with the routed leg carrying that same
        // asset class.
        let routed = is_internal(&routing)
            && routing.legs().len() == 1
            && routing.legs()[0].asset_class == requested_class;
        both_routed &= routed;
        println!(
            "asset[{label}] {} routed:{routed}",
            render_routing(&routing)
        );
    }

    if !both_routed {
        return Err(
            "both-asset-classes-routed:false — an equity or option order did not route to the \
             internal simulation engine as expected (an intake regression)"
                .to_string(),
        );
    }
    println!("both-asset-classes-routed:true");
    Ok(())
}

/// Accept a two-leg option spread and prove it routes as one atomic composite transaction (SYS-4).
fn cmd_multileg(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let engine = PaperSimulationEngine::new();

    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_and_assert_fail_closed(&engine, fault);
    }

    // A vertical option spread: buy one call, sell another (SYS-4) — two legs that fill atomically.
    let request = PaperOrderRequest::MultiLeg {
        legs: vec![
            option_leg(Side::Buy, OrderType::Market),
            OrderLeg {
                symbol: SYMBOL_OPTION_FAR.to_string(),
                asset_class: AssetClass::Option,
                side: Side::Sell,
                quantity: FIXTURE_OPTION_QTY,
                order_type: OrderType::Limit {
                    limit_price_minor: LIMIT_MINOR,
                },
            },
        ],
    };
    let routing = engine
        .accept_order(&request)
        .map_err(|err| err.to_string())?;
    println!("multileg {}", render_routing(&routing));

    // Non-vacuous: a multi-leg order must route as ONE composite (composite:true) carrying exactly the
    // two option legs that fill atomically — not as a single order or a split.
    let composite = is_internal(&routing)
        && routing.is_composite()
        && routing.legs().len() == 2
        && routing
            .legs()
            .iter()
            .all(|leg| leg.asset_class == AssetClass::Option);
    if !composite {
        return Err(
            "composite-routed:false — the multi-leg option order did not route as one atomic \
             two-leg composite transaction (SYS-4)"
                .to_string(),
        );
    }
    println!("composite-routed:true");
    Ok(())
}

/// Sweep every accepted order shape and prove every routing is the internal simulation engine — the
/// runtime witness of the compile-time 'no IB API order calls' guarantee.
fn cmd_no_broker(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let engine = PaperSimulationEngine::new();

    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_and_assert_fail_closed(&engine, fault);
    }

    let mut swept: u64 = 0;
    let mut internal: u64 = 0;

    // Every single order: each asset class × side × order type.
    for asset_class in [AssetClass::Equity, AssetClass::Option] {
        for side in [Side::Buy, Side::Sell] {
            for (_, order_type) in all_order_types() {
                let leg = OrderLeg {
                    symbol: symbol_for(asset_class).to_string(),
                    asset_class,
                    side,
                    quantity: qty_for(asset_class),
                    order_type,
                };
                let routing = engine
                    .accept_order(&PaperOrderRequest::Single(leg))
                    .map_err(|err| err.to_string())?;
                swept += 1;
                internal += routed_internally(&routing);
            }
        }
    }

    // Every option composite: each side × order type (two identical option legs).
    for side in [Side::Buy, Side::Sell] {
        for (_, order_type) in all_order_types() {
            let request = PaperOrderRequest::MultiLeg {
                legs: vec![option_leg(side, order_type), option_leg(side, order_type)],
            };
            let routing = engine
                .accept_order(&request)
                .map_err(|err| err.to_string())?;
            swept += 1;
            internal += routed_internally(&routing);
        }
    }

    println!(
        "no-broker swept:{swept} internal-simulation:{internal} broker:{}",
        swept - internal
    );

    // Non-vacuous: a real sweep (at least one order) where EVERY routing was the internal simulation
    // engine — none reached a brokerage. `OrderRouting` has no broker variant to construct, so the
    // runtime sweep witnesses the compile-time guarantee.
    if swept == 0 || internal != swept {
        return Err(format!(
            "no-ib-order-calls:false — swept {swept} orders but only {internal} routed to the \
             internal simulation engine ({} reached a brokerage)",
            swept - internal
        ));
    }
    println!("no-ib-order-calls:true");
    Ok(())
}

// --------------------------------------------------------------------------- //
// Fail-closed injection
// --------------------------------------------------------------------------- //

/// Build the order shape the fault targets and assert intake fails closed BEFORE routing (returns the
/// expected [`OrderError`], never an `Ok` routing). Returns Err (non-zero exit) so no proof line is
/// ever printed under a fault.
fn inject_and_assert_fail_closed(
    engine: &PaperSimulationEngine,
    fault: Fault,
) -> Result<(), String> {
    let err: String = match fault {
        // An empty / whitespace symbol is rejected before routing.
        Fault::EmptySymbol => {
            let mut leg = equity_leg(OrderType::Market);
            leg.symbol = "   ".to_string();
            match engine.accept_order(&PaperOrderRequest::Single(leg)) {
                Err(OrderError::EmptySymbol) => "intake rejected an empty symbol".to_string(),
                other => return Err(unexpected("empty-symbol", other)),
            }
        }
        // A non-positive quantity is rejected before routing.
        Fault::NonpositiveQuantity => {
            let mut leg = equity_leg(OrderType::Market);
            leg.quantity = 0;
            match engine.accept_order(&PaperOrderRequest::Single(leg)) {
                Err(OrderError::NonPositiveQuantity { .. }) => {
                    "intake rejected a non-positive quantity".to_string()
                }
                other => return Err(unexpected("nonpositive-quantity", other)),
            }
        }
        // A non-positive limit price must never reach the fill path.
        Fault::NonpositiveLimit => {
            let leg = equity_leg(OrderType::Limit {
                limit_price_minor: 0,
            });
            match engine.accept_order(&PaperOrderRequest::Single(leg)) {
                Err(OrderError::NonPositiveLimitPrice { .. }) => {
                    "intake rejected a non-positive limit price".to_string()
                }
                other => return Err(unexpected("nonpositive-limit", other)),
            }
        }
        // A non-positive stop price must never reach the fill path either.
        Fault::NonpositiveStop => {
            let leg = equity_leg(OrderType::Stop {
                stop_price_minor: -1,
            });
            match engine.accept_order(&PaperOrderRequest::Single(leg)) {
                Err(OrderError::NonPositiveStopPrice { .. }) => {
                    "intake rejected a non-positive stop price".to_string()
                }
                other => return Err(unexpected("nonpositive-stop", other)),
            }
        }
        // An empty multi-leg request carries no legs to route.
        Fault::EmptyMultileg => {
            match engine.accept_order(&PaperOrderRequest::MultiLeg { legs: vec![] }) {
                Err(OrderError::EmptyMultiLeg) => {
                    "intake rejected an empty multi-leg request".to_string()
                }
                other => return Err(unexpected("empty-multileg", other)),
            }
        }
        // A one-leg composite is not a SYS-4 multi-leg order.
        Fault::SingleLegComposite => match engine.accept_order(&PaperOrderRequest::MultiLeg {
            legs: vec![option_leg(Side::Buy, OrderType::Market)],
        }) {
            Err(OrderError::SingleLegComposite) => {
                "intake rejected a single-leg composite".to_string()
            }
            other => return Err(unexpected("single-leg-composite", other)),
        },
        // A composite is options-only; an equity leg in a composite is rejected before routing.
        Fault::NonOptionCompositeLeg => match engine.accept_order(&PaperOrderRequest::MultiLeg {
            legs: vec![
                option_leg(Side::Buy, OrderType::Market),
                equity_leg(OrderType::Market),
            ],
        }) {
            Err(OrderError::NonOptionCompositeLeg) => {
                "intake rejected a non-option composite leg".to_string()
            }
            other => return Err(unexpected("non-option-composite-leg", other)),
        },
    };

    Err(format!(
        "inject={}: paper order intake failed closed ({err}); no order routed",
        fault.as_str()
    ))
}

fn unexpected(fault: &str, got: Result<OrderRouting, OrderError>) -> String {
    format!("inject={fault}: expected intake to fail closed, got {got:?}")
}

// --------------------------------------------------------------------------- //
// Order builders and rendering helpers
// --------------------------------------------------------------------------- //

/// A well-formed single equity leg carrying the given order type.
fn equity_leg(order_type: OrderType) -> OrderLeg {
    OrderLeg {
        symbol: SYMBOL_EQUITY.to_string(),
        asset_class: AssetClass::Equity,
        side: Side::Buy,
        quantity: FIXTURE_QTY,
        order_type,
    }
}

/// A well-formed single option leg with the given side and order type.
fn option_leg(side: Side, order_type: OrderType) -> OrderLeg {
    OrderLeg {
        symbol: SYMBOL_OPTION.to_string(),
        asset_class: AssetClass::Option,
        side,
        quantity: FIXTURE_OPTION_QTY,
        order_type,
    }
}

fn symbol_for(asset_class: AssetClass) -> &'static str {
    match asset_class {
        AssetClass::Equity => SYMBOL_EQUITY,
        AssetClass::Option => SYMBOL_OPTION,
    }
}

fn qty_for(asset_class: AssetClass) -> i64 {
    match asset_class {
        AssetClass::Equity => FIXTURE_QTY,
        AssetClass::Option => FIXTURE_OPTION_QTY,
    }
}

/// Whether a routing is the internal simulation engine. `OrderRouting` has exactly one variant, so
/// this is always true for an accepted order — the point is that there is structurally no other arm
/// to return, which is the 'no IB API order calls' guarantee.
fn is_internal(routing: &OrderRouting) -> bool {
    matches!(routing, OrderRouting::InternalSimulation { .. })
}

/// 1 if the routing is the internal simulation engine, 0 otherwise (for the sweep counter).
fn routed_internally(routing: &OrderRouting) -> u64 {
    u64::from(is_internal(routing))
}

fn render_routing(routing: &OrderRouting) -> String {
    let target = match routing {
        OrderRouting::InternalSimulation { .. } => "internal-simulation",
    };
    format!(
        "routing:{target} legs:{} composite:{}",
        routing.legs().len(),
        routing.is_composite()
    )
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

/// A fault to inject so paper order intake must fail closed before routing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fault {
    EmptySymbol,
    NonpositiveQuantity,
    NonpositiveLimit,
    NonpositiveStop,
    EmptyMultileg,
    SingleLegComposite,
    NonOptionCompositeLeg,
}

impl Fault {
    fn parse(spec: &str) -> Result<Self, String> {
        match spec {
            "empty-symbol" => Ok(Self::EmptySymbol),
            "nonpositive-quantity" => Ok(Self::NonpositiveQuantity),
            "nonpositive-limit" => Ok(Self::NonpositiveLimit),
            "nonpositive-stop" => Ok(Self::NonpositiveStop),
            "empty-multileg" => Ok(Self::EmptyMultileg),
            "single-leg-composite" => Ok(Self::SingleLegComposite),
            "non-option-composite-leg" => Ok(Self::NonOptionCompositeLeg),
            other => Err(format!(
                "unknown fault '{other}' (expected empty-symbol|nonpositive-quantity|\
                 nonpositive-limit|nonpositive-stop|empty-multileg|single-leg-composite|\
                 non-option-composite-leg)"
            )),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::EmptySymbol => "empty-symbol",
            Self::NonpositiveQuantity => "nonpositive-quantity",
            Self::NonpositiveLimit => "nonpositive-limit",
            Self::NonpositiveStop => "nonpositive-stop",
            Self::EmptyMultileg => "empty-multileg",
            Self::SingleLegComposite => "single-leg-composite",
            Self::NonOptionCompositeLeg => "non-option-composite-leg",
        }
    }
}

/// Parse a subcommand that accepts only an optional `--inject <fault>`.
fn parse_inject_only(rest: &[String]) -> Result<Option<Fault>, String> {
    let mut inject = None;
    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--inject" => inject = Some(Fault::parse(&take_value(&mut iter, flag)?)?),
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        }
    }
    Ok(inject)
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}
