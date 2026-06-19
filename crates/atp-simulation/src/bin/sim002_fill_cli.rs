//! SRS-SIM-002 configurable fill-model operator CLI.
//!
//! The operator-facing surface of "simulate fills using live market data and configurable fill
//! models" (docs/SRS.md SRS-5.7 SRS-SIM-002; SyRS SYS-83 fill simulation, SYS-87b volume constraint;
//! StRS SN-1.29 / SN-1.03). The acceptance criterion is: "Market, limit, stop, and stop-limit
//! simulated fills follow SYS-83 defaults and per-strategy configuration; fill volume constraints
//! are enforced." The fill-model engine ([`PaperSimulationEngine::evaluate_fill`] over a
//! [`MarketSnapshot`] and a per-strategy [`FillModelConfig`]) is already built and the Rust
//! integration test `srs_sim_002_fill_models` already asserts the rules; this binary makes them
//! *operator-demonstrable* — the same precedent as the SRS-BT-002 / BT-003 / BT-009 / BT-010 / SIM-003
//! CLIs (there is no Python↔Rust strategy host, so the operator workflow is demonstrated over the
//! Rust core, driving the real [`PaperSimulationEngine::evaluate_fill`] path).
//!
//! - `defaults` — print the SYS-83 default fill-model config (`immediate-on-cross`, the SYS-83b
//!   default) and resolve all four order types on one clean fixture snapshot, printing each
//!   [`FillDecision`]. Makes "follow SYS-83 defaults" inspectable.
//!
//! - `rules [--inject F]` — drive market / limit / stop / stop-limit (and a market sell) on a fixture
//!   snapshot and assert each SYS-83 reference price holds: a market order fills at the ask (buy) /
//!   bid (sell); a crossed limit fills at the **limit** price (the conservative no-improvement
//!   reference); a triggered stop fills at market; a triggered stop-limit rests as a limit. Emit
//!   `sys83-rules-correct:true` only if EVERY rule fills at its expected price (non-vacuous: each
//!   decision must be a `Filled` at the predicted minor-unit price).
//!
//! - `config [--inject F]` — the two per-strategy [`LimitFillModel`]s on the SAME touch snapshot (the
//!   ask exactly at the limit): `ImmediateOnCross` fills the touch, `RequireThroughCross` does not.
//!   Emit `config-divergent:true` only if the two configs produce DIFFERENT decisions (non-vacuous:
//!   the per-strategy configuration is refused as a proof if both models agree).
//!
//! - `volume [--qty N] [--volume V] [--inject F]` — enforce the SYS-87b volume constraint two ways:
//!   a single order requesting MORE than the bar volume fills only `min(requested, volume)` (a
//!   partial fill), AND threading ONE [`BarVolumeBudget`] through several orders against the same bar
//!   caps the AGGREGATE at the observed volume (the last order sees `zero-volume`). Emit
//!   `volume-capped:true` only if the single order is genuinely capped AND the aggregate of fills
//!   never exceeds the observed volume AND a final order is refused with `zero-volume`.
//!
//! Fail closed: an unknown subcommand or flag exits non-zero. `--inject <fault>` corrupts a snapshot,
//! an order, a quantity, or a budget so the fill model MUST reject it BEFORE producing any fill
//! decision; the CLI prints `inject=<fault>: fill model failed closed` with the engine error and
//! exits non-zero with NO proof line — corrupt market data or a malformed order can never produce a
//! fill "proof". The faults are:
//!   nonpositive-quote | crossed-book | negative-volume | zero-quantity | nonpositive-limit |
//!   nonpositive-stop | budget-mismatch
//! Non-vacuity is guarded at parse: `volume`'s `--qty` must EXCEED `--volume` (so the single-order
//! cap is always a real partial fill) and `--volume` must be `>= 2` (so the aggregate split exercises
//! a genuine cap and a zero-volume tail), and every injected fault is asserted to return its specific
//! [`FillModelError`] variant (never `Ok`), with the budget proven unconsumed on the budget-mismatch
//! path — so a proof is never asserted over an input the simulation layer's own rules would accept by
//! accident or reject silently.

use std::env;
use std::process::ExitCode;

use atp_simulation::fill_model::{
    BarVolumeBudget, FillDecision, FillModelConfig, FillModelError, LimitFillModel, MarketSnapshot,
    NoFillReason,
};
use atp_simulation::paper_order::{OrderType, Side};
use atp_simulation::sim::PaperSimulationEngine;

// Deterministic fixture quotes (integer minor units / cents). A clean, strictly-positive,
// non-crossed book the SYS-83 rules resolve against.
const BID_MINOR: i64 = 9_990;
const ASK_MINOR: i64 = 10_000;
const LAST_MINOR: i64 = 10_000;
const FIXTURE_VOLUME: i64 = 10_000;

// A buy limit at 10_050 sits ABOVE the ask (10_000), so it crosses and fills at the limit price (the
// conservative no-improvement reference). Distinct from the ask so the "limit fills at the limit, not
// the market" rule is observable. The stop at 9_995 sits BELOW the last (10_000), so a buy stop
// triggers (last >= stop) and then fills at market.
const RULE_LIMIT_MINOR: i64 = 10_050;
const RULE_STOP_MINOR: i64 = 9_995;
const RULE_QTY: i64 = 100;

// The `config` touch snapshot: the ask sits EXACTLY at the limit, the one snapshot on which the two
// per-strategy limit models disagree (immediate fills the touch, through-cross does not).
const TOUCH_LIMIT_MINOR: i64 = ASK_MINOR;
const CONFIG_QTY: i64 = 100;

// `volume` defaults: a single order requesting more than the bar volume (a guaranteed partial fill),
// and a bar volume large enough that the aggregate split exercises a genuine cap plus a zero-volume
// tail.
const DEFAULT_VOLUME_QTY: i64 = 800;
const DEFAULT_BAR_VOLUME: i64 = 500;
const MIN_BAR_VOLUME: i64 = 2;

const USAGE: &str = "\
sim002_fill_cli — SRS-SIM-002 configurable fill-model operator workflow

USAGE:
    sim002_fill_cli defaults
    sim002_fill_cli rules  [--inject <fault>]
    sim002_fill_cli config [--inject <fault>]
    sim002_fill_cli volume [--qty <n>] [--volume <v>] [--inject <fault>]

COMMANDS:
    defaults  Print the SYS-83 default fill-model config (immediate-on-cross) and resolve all four
              order types on a clean fixture snapshot — 'follow SYS-83 defaults', made inspectable.
    rules     Prove each SYS-83 fill rule: market fills at ask (buy) / bid (sell); a crossed limit
              fills at the limit price; a triggered stop fills at market; a triggered stop-limit rests
              as a limit (sys83-rules-correct:true).
    config    Prove the per-strategy fill model is behavior-changing: the two limit models disagree on
              the SAME touch snapshot (config-divergent:true).
    volume    Prove the SYS-87b volume constraint: a single order is capped at the bar volume and the
              aggregate of fills across orders never exceeds the observed volume (volume-capped:true).

RUN FLAGS:
    --qty <n>     (volume) shares the single order requests; must be > 0 and > --volume so the cap is
                  a genuine partial fill (default 800)
    --volume <v>  (volume) observed bar volume; must be >= 2 (default 500)
    --inject <f>  corrupt an input so the fill model MUST fail closed before any fill decision; one of:
                  nonpositive-quote | crossed-book | negative-volume | zero-quantity |
                  nonpositive-limit | nonpositive-stop | budget-mismatch

An injected fault, a non-positive quantity, or an out-of-range volume is rejected before any proof
line is printed, so the proof can never be vacuous or fabricated.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("sim002_fill_cli: {err}");
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
        "defaults" => cmd_defaults(rest),
        "rules" => cmd_rules(rest),
        "config" => cmd_config(rest),
        "volume" => cmd_volume(rest),
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

/// Print the SYS-83 default config and resolve all four order types on a clean snapshot.
fn cmd_defaults(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if !rest.is_empty() {
        return Err(format!("`defaults` takes no arguments\n\n{USAGE}"));
    }

    let engine = PaperSimulationEngine::new();
    let config = FillModelConfig::syrs_defaults();
    let snapshot = fixture_snapshot(FIXTURE_VOLUME);

    // The SYS-83b default limit-fill model is immediate-on-cross, and the named default equals the
    // type's `Default` (the SyRS baseline), so a strategy that configures nothing gets SYS-83.
    println!(
        "default-limit-fill-model:{}",
        model_str(config.limit_fill)
    );
    println!(
        "default-config-is-syrs-baseline:{}",
        config == FillModelConfig::default()
    );

    for (label, order, side) in [
        ("market", OrderType::Market, Side::Buy),
        (
            "limit",
            OrderType::Limit {
                limit_price_minor: RULE_LIMIT_MINOR,
            },
            Side::Buy,
        ),
        (
            "stop",
            OrderType::Stop {
                stop_price_minor: RULE_STOP_MINOR,
            },
            Side::Buy,
        ),
        (
            "stop-limit",
            OrderType::StopLimit {
                stop_price_minor: RULE_STOP_MINOR,
                limit_price_minor: RULE_LIMIT_MINOR,
            },
            Side::Buy,
        ),
    ] {
        let decision = engine
            .evaluate_fill(&order, side, RULE_QTY, &snapshot, &config)
            .map_err(|err| err.to_string())?;
        println!("default[{label}] {}", render_decision(&decision));
    }
    Ok(())
}

/// Prove each SYS-83 fill rule fills at its expected reference price.
fn cmd_rules(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let engine = PaperSimulationEngine::new();
    let config = FillModelConfig::syrs_defaults();

    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_and_assert_fail_closed(&engine, fault);
    }

    let snapshot = fixture_snapshot(FIXTURE_VOLUME);
    println!("{}", render_snapshot(&snapshot));

    // (label, order type, side, the SYS-83 reference price the fill MUST take).
    let rules: [(&str, OrderType, Side, i64); 5] = [
        // SYS-83a: a market order fills at the ask (buy) / bid (sell).
        ("market-buy", OrderType::Market, Side::Buy, ASK_MINOR),
        ("market-sell", OrderType::Market, Side::Sell, BID_MINOR),
        // SYS-83b: a crossed limit fills at the LIMIT price (conservative no-improvement reference).
        (
            "limit-buy",
            OrderType::Limit {
                limit_price_minor: RULE_LIMIT_MINOR,
            },
            Side::Buy,
            RULE_LIMIT_MINOR,
        ),
        // SYS-83c: a triggered stop fills at market (the ask for a buy).
        (
            "stop-buy",
            OrderType::Stop {
                stop_price_minor: RULE_STOP_MINOR,
            },
            Side::Buy,
            ASK_MINOR,
        ),
        // SYS-83c: a triggered stop-limit rests as a limit (fills at the limit).
        (
            "stop-limit-buy",
            OrderType::StopLimit {
                stop_price_minor: RULE_STOP_MINOR,
                limit_price_minor: RULE_LIMIT_MINOR,
            },
            Side::Buy,
            RULE_LIMIT_MINOR,
        ),
    ];

    let mut all_correct = true;
    for (label, order, side, expected_price) in rules {
        let decision = engine
            .evaluate_fill(&order, side, RULE_QTY, &snapshot, &config)
            .map_err(|err| err.to_string())?;
        let correct = matches!(
            decision,
            FillDecision::Filled { fill_price_minor, .. } if fill_price_minor == expected_price
        );
        all_correct &= correct;
        println!(
            "rule[{label}] expected_fill_price_minor:{expected_price} {} correct:{correct}",
            render_decision(&decision)
        );
    }

    // Non-vacuous: refuse to claim the rules hold unless every one of the five fills landed at its
    // predicted SYS-83 reference price.
    if !all_correct {
        return Err(
            "sys83-rules-correct:false — at least one order type did not fill at its expected \
             SYS-83 reference price (a fill-model regression)"
                .to_string(),
        );
    }
    println!("sys83-rules-correct:true");
    Ok(())
}

/// Prove the per-strategy fill model is behavior-changing on the same touch snapshot.
fn cmd_config(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let engine = PaperSimulationEngine::new();

    if let Some(fault) = parse_inject_only(rest)? {
        println!("inject:{}", fault.as_str());
        return inject_and_assert_fail_closed(&engine, fault);
    }

    // The ask sits EXACTLY at the limit — the one snapshot on which the two models disagree.
    let touch = fixture_snapshot(FIXTURE_VOLUME);
    let order = OrderType::Limit {
        limit_price_minor: TOUCH_LIMIT_MINOR,
    };
    println!("{}", render_snapshot(&touch));
    println!("limit_price_minor:{TOUCH_LIMIT_MINOR}");

    let immediate = FillModelConfig {
        limit_fill: LimitFillModel::ImmediateOnCross,
    };
    let through = FillModelConfig {
        limit_fill: LimitFillModel::RequireThroughCross,
    };
    let immediate_decision = engine
        .evaluate_fill(&order, Side::Buy, CONFIG_QTY, &touch, &immediate)
        .map_err(|err| err.to_string())?;
    let through_decision = engine
        .evaluate_fill(&order, Side::Buy, CONFIG_QTY, &touch, &through)
        .map_err(|err| err.to_string())?;

    println!(
        "config[{}] {}",
        model_str(immediate.limit_fill),
        render_decision(&immediate_decision)
    );
    println!(
        "config[{}] {}",
        model_str(through.limit_fill),
        render_decision(&through_decision)
    );

    // Non-vacuous: the per-strategy configuration is a real choice only if the two models produce
    // DIFFERENT decisions on this snapshot. Refuse to claim divergence if they agree.
    if immediate_decision == through_decision {
        return Err(
            "config-divergent:false — the two per-strategy limit models produced the SAME decision; \
             refusing to assert a behavior-changing configuration over indistinguishable outcomes"
                .to_string(),
        );
    }
    println!("config-divergent:true");
    Ok(())
}

/// Prove the SYS-87b volume constraint: a single order is capped, and the aggregate across orders
/// never exceeds the observed bar volume.
fn cmd_volume(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let engine = PaperSimulationEngine::new();
    let config = FillModelConfig::syrs_defaults();
    let parsed = VolumeArgs::parse(rest)?;

    if let Some(fault) = parsed.inject {
        println!("inject:{}", fault.as_str());
        return inject_and_assert_fail_closed(&engine, fault);
    }

    let snapshot = fixture_snapshot(parsed.volume);
    println!("{}", render_snapshot(&snapshot));

    // Single-order cap: a market order requesting MORE than the bar volume fills only the bar volume.
    // `--qty > --volume` is enforced at parse, so this is always a genuine partial fill.
    let single = engine
        .evaluate_fill(&OrderType::Market, Side::Buy, parsed.qty, &snapshot, &config)
        .map_err(|err| err.to_string())?;
    let single_fill = filled_quantity(&single)?;
    let single_capped = single_fill == parsed.volume && single_fill < parsed.qty;
    println!(
        "single requested:{} bar-volume:{} {} capped:{single_capped}",
        parsed.qty,
        parsed.volume,
        render_decision(&single)
    );

    // Aggregate cap (SYS-87b "for the bar period"): thread ONE budget through several orders against
    // the SAME bar. The first order takes a third of the volume, the second over-requests the whole
    // bar (and is capped at the remainder), the third finds no volume left.
    let mut budget = BarVolumeBudget::for_snapshot(&snapshot).map_err(|err| err.to_string())?;
    let first_qty = (parsed.volume / 3).max(1);
    let order_one = engine
        .evaluate_fill_against_budget(
            &OrderType::Market,
            Side::Buy,
            first_qty,
            &snapshot,
            &config,
            &mut budget,
        )
        .map_err(|err| err.to_string())?;
    let order_two = engine
        .evaluate_fill_against_budget(
            &OrderType::Market,
            Side::Buy,
            parsed.volume,
            &snapshot,
            &config,
            &mut budget,
        )
        .map_err(|err| err.to_string())?;
    let order_three = engine
        .evaluate_fill_against_budget(
            &OrderType::Market,
            Side::Buy,
            1,
            &snapshot,
            &config,
            &mut budget,
        )
        .map_err(|err| err.to_string())?;

    let fill_one = filled_quantity(&order_one)?;
    let fill_two = filled_quantity_or_zero(&order_two);
    let aggregate: i128 = i128::from(fill_one) + i128::from(fill_two);
    let second_capped = fill_two < parsed.volume; // the over-requesting order was capped at the remainder
    let third_zero_volume = matches!(
        order_three,
        FillDecision::NoFill {
            reason: NoFillReason::ZeroVolume
        }
    );
    println!(
        "aggregate order-one-fill:{fill_one} order-two-fill:{fill_two} order-three:{} \
         observed-volume:{} aggregate-fill:{aggregate}",
        render_decision(&order_three),
        parsed.volume
    );

    // Non-vacuous: claim the cap only if the single order was genuinely capped, the aggregate never
    // exceeded the observed volume, the over-requesting order WAS capped (not a coincidental fit), and
    // the trailing order found no volume.
    let aggregate_within = aggregate <= i128::from(parsed.volume);
    if !(single_capped && aggregate_within && second_capped && third_zero_volume) {
        return Err(format!(
            "volume-capped:false — SYS-87b not enforced (single-capped:{single_capped} \
             aggregate-within:{aggregate_within} second-capped:{second_capped} \
             third-zero-volume:{third_zero_volume})"
        ));
    }
    println!("volume-capped:true");
    Ok(())
}

// --------------------------------------------------------------------------- //
// Fail-closed injection
// --------------------------------------------------------------------------- //

/// Drive the specific guard the fault targets and assert the fill model fails closed BEFORE any fill
/// decision (returns the expected [`FillModelError`], never an `Ok` fill). Returns Err (non-zero
/// exit) so no proof line is ever printed under a fault.
fn inject_and_assert_fail_closed(
    engine: &PaperSimulationEngine,
    fault: Fault,
) -> Result<(), String> {
    let config = FillModelConfig::syrs_defaults();
    let err: String = match fault {
        // A non-positive quote (corrupt market data) is rejected by the snapshot guard.
        Fault::NonpositiveQuote => {
            let snapshot = MarketSnapshot {
                bid_minor: 0,
                ask_minor: ASK_MINOR,
                last_minor: LAST_MINOR,
                bar_volume: FIXTURE_VOLUME,
            };
            match engine.evaluate_fill(&OrderType::Market, Side::Buy, RULE_QTY, &snapshot, &config) {
                Err(FillModelError::NonPositiveQuote { .. }) => {
                    "fill model rejected a non-positive quote".to_string()
                }
                other => return Err(unexpected("nonpositive-quote", other)),
            }
        }
        // A crossed/locked book (bid > ask) is corrupt quote data with no well-defined fill price.
        Fault::CrossedBook => {
            let snapshot = MarketSnapshot {
                bid_minor: ASK_MINOR + 10,
                ask_minor: ASK_MINOR,
                last_minor: LAST_MINOR,
                bar_volume: FIXTURE_VOLUME,
            };
            match engine.evaluate_fill(&OrderType::Market, Side::Buy, RULE_QTY, &snapshot, &config) {
                Err(FillModelError::CrossedBook { .. }) => {
                    "fill model rejected a crossed book".to_string()
                }
                other => return Err(unexpected("crossed-book", other)),
            }
        }
        // A negative bar volume (corrupt volume data) must never widen a fill.
        Fault::NegativeVolume => {
            let snapshot = MarketSnapshot {
                bid_minor: BID_MINOR,
                ask_minor: ASK_MINOR,
                last_minor: LAST_MINOR,
                bar_volume: -1,
            };
            match engine.evaluate_fill(&OrderType::Market, Side::Buy, RULE_QTY, &snapshot, &config) {
                Err(FillModelError::NegativeVolume { .. }) => {
                    "fill model rejected a negative bar volume".to_string()
                }
                other => return Err(unexpected("negative-volume", other)),
            }
        }
        // A non-positive requested quantity would make the volume cap meaningless.
        Fault::ZeroQuantity => {
            let snapshot = fixture_snapshot(FIXTURE_VOLUME);
            match engine.evaluate_fill(&OrderType::Market, Side::Buy, 0, &snapshot, &config) {
                Err(FillModelError::NonPositiveQuantity { .. }) => {
                    "fill model rejected a non-positive requested quantity".to_string()
                }
                other => return Err(unexpected("zero-quantity", other)),
            }
        }
        // A non-positive limit price must never reach the fill path (a negative limit could otherwise
        // cross and return a fill AT that negative price).
        Fault::NonpositiveLimit => {
            let snapshot = fixture_snapshot(FIXTURE_VOLUME);
            let order = OrderType::Limit {
                limit_price_minor: 0,
            };
            match engine.evaluate_fill(&order, Side::Buy, RULE_QTY, &snapshot, &config) {
                Err(FillModelError::NonPositiveLimitPrice { .. }) => {
                    "fill model rejected a non-positive limit price".to_string()
                }
                other => return Err(unexpected("nonpositive-limit", other)),
            }
        }
        // A non-positive stop price must never reach the fill path either.
        Fault::NonpositiveStop => {
            let snapshot = fixture_snapshot(FIXTURE_VOLUME);
            let order = OrderType::Stop {
                stop_price_minor: 0,
            };
            match engine.evaluate_fill(&order, Side::Buy, RULE_QTY, &snapshot, &config) {
                Err(FillModelError::NonPositiveStopPrice { .. }) => {
                    "fill model rejected a non-positive stop price".to_string()
                }
                other => return Err(unexpected("nonpositive-stop", other)),
            }
        }
        // A budget built for a DIFFERENT bar must never let fills exceed THIS bar's observed volume.
        // The rejected evaluation must also consume NOTHING from the budget (no fill happened).
        Fault::BudgetMismatch => {
            let snapshot = fixture_snapshot(DEFAULT_BAR_VOLUME);
            let mut budget = BarVolumeBudget::new(snapshot.bar_volume + 1)
                .map_err(|err| format!("could not build the mismatched budget: {err}"))?;
            let remaining_before = budget.remaining();
            let decision = engine.evaluate_fill_against_budget(
                &OrderType::Market,
                Side::Buy,
                RULE_QTY,
                &snapshot,
                &config,
                &mut budget,
            );
            match decision {
                Err(FillModelError::BudgetSnapshotMismatch { .. }) => {
                    if budget.remaining() != remaining_before {
                        return Err(format!(
                            "inject=budget-mismatch: budget consumed despite a rejected evaluation \
                             (remaining {} != {remaining_before})",
                            budget.remaining()
                        ));
                    }
                    "fill model rejected a budget bound to a different bar".to_string()
                }
                other => return Err(unexpected("budget-mismatch", other)),
            }
        }
    };

    Err(format!(
        "inject={}: fill model failed closed ({err}); no fill produced",
        fault.as_str()
    ))
}

fn unexpected(fault: &str, got: Result<FillDecision, FillModelError>) -> String {
    format!("inject={fault}: expected the fill model to fail closed, got {got:?}")
}

// --------------------------------------------------------------------------- //
// Rendering helpers
// --------------------------------------------------------------------------- //

/// A clean, strictly-positive, non-crossed fixture snapshot with the given bar volume.
fn fixture_snapshot(bar_volume: i64) -> MarketSnapshot {
    MarketSnapshot {
        bid_minor: BID_MINOR,
        ask_minor: ASK_MINOR,
        last_minor: LAST_MINOR,
        bar_volume,
    }
}

fn render_snapshot(snapshot: &MarketSnapshot) -> String {
    format!(
        "snapshot bid-minor:{} ask-minor:{} last-minor:{} bar-volume:{}",
        snapshot.bid_minor, snapshot.ask_minor, snapshot.last_minor, snapshot.bar_volume
    )
}

fn render_decision(decision: &FillDecision) -> String {
    match decision {
        FillDecision::Filled {
            fill_price_minor,
            fill_quantity,
        } => format!("decision:filled fill_price_minor:{fill_price_minor} fill-quantity:{fill_quantity}"),
        FillDecision::NoFill { reason } => format!("decision:no-fill reason:{}", reason_str(reason)),
    }
}

fn reason_str(reason: &NoFillReason) -> &'static str {
    match reason {
        NoFillReason::LimitNotCrossed => "limit-not-crossed",
        NoFillReason::StopNotTriggered => "stop-not-triggered",
        NoFillReason::ZeroVolume => "zero-volume",
    }
}

fn model_str(model: LimitFillModel) -> &'static str {
    match model {
        LimitFillModel::ImmediateOnCross => "immediate-on-cross",
        LimitFillModel::RequireThroughCross => "require-through-cross",
    }
}

/// The fill quantity of a decision that MUST be a fill (errors if it is a no-fill — used where the
/// fixture guarantees a fill, so a no-fill is a regression, not a vacuous pass).
fn filled_quantity(decision: &FillDecision) -> Result<i64, String> {
    match decision {
        FillDecision::Filled { fill_quantity, .. } => Ok(*fill_quantity),
        FillDecision::NoFill { reason } => Err(format!(
            "expected a fill, got no-fill (reason:{})",
            reason_str(reason)
        )),
    }
}

/// The fill quantity of a decision, or 0 if it did not fill (used for the aggregate sum, where a
/// no-fill legitimately contributes zero).
fn filled_quantity_or_zero(decision: &FillDecision) -> i64 {
    match decision {
        FillDecision::Filled { fill_quantity, .. } => *fill_quantity,
        FillDecision::NoFill { .. } => 0,
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

/// A fault to inject so the fill model must fail closed before any fill decision.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fault {
    NonpositiveQuote,
    CrossedBook,
    NegativeVolume,
    ZeroQuantity,
    NonpositiveLimit,
    NonpositiveStop,
    BudgetMismatch,
}

impl Fault {
    fn parse(spec: &str) -> Result<Self, String> {
        match spec {
            "nonpositive-quote" => Ok(Self::NonpositiveQuote),
            "crossed-book" => Ok(Self::CrossedBook),
            "negative-volume" => Ok(Self::NegativeVolume),
            "zero-quantity" => Ok(Self::ZeroQuantity),
            "nonpositive-limit" => Ok(Self::NonpositiveLimit),
            "nonpositive-stop" => Ok(Self::NonpositiveStop),
            "budget-mismatch" => Ok(Self::BudgetMismatch),
            other => Err(format!(
                "unknown fault '{other}' (expected nonpositive-quote|crossed-book|negative-volume|\
                 zero-quantity|nonpositive-limit|nonpositive-stop|budget-mismatch)"
            )),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::NonpositiveQuote => "nonpositive-quote",
            Self::CrossedBook => "crossed-book",
            Self::NegativeVolume => "negative-volume",
            Self::ZeroQuantity => "zero-quantity",
            Self::NonpositiveLimit => "nonpositive-limit",
            Self::NonpositiveStop => "nonpositive-stop",
            Self::BudgetMismatch => "budget-mismatch",
        }
    }
}

/// Parse a subcommand that accepts only an optional `--inject <fault>` (rules / config).
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

struct VolumeArgs {
    qty: i64,
    volume: i64,
    inject: Option<Fault>,
}

impl Default for VolumeArgs {
    fn default() -> Self {
        Self {
            qty: DEFAULT_VOLUME_QTY,
            volume: DEFAULT_BAR_VOLUME,
            inject: None,
        }
    }
}

impl VolumeArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = VolumeArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--qty" => parsed.qty = take_i64(&mut iter, flag)?,
                "--volume" => parsed.volume = take_i64(&mut iter, flag)?,
                "--inject" => parsed.inject = Some(Fault::parse(&take_value(&mut iter, flag)?)?),
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        // An injected fault is self-contained, so it does not need a valid qty/volume. Otherwise the
        // non-fault proof must run over a non-vacuous configuration.
        if parsed.inject.is_none() {
            // The bar volume must support a genuine aggregate split (a first fill, a capped second,
            // and a zero-volume tail), so it must be >= 2.
            if parsed.volume < MIN_BAR_VOLUME {
                return Err(format!(
                    "--volume must be >= {MIN_BAR_VOLUME} (got {}); a smaller bar cannot exercise a \
                     genuine aggregate cap with a zero-volume tail",
                    parsed.volume
                ));
            }
            // The single order must request MORE than the bar volume so its cap is a genuine partial
            // fill (not a coincidental full fill the proof would treat as "capped" vacuously).
            if parsed.qty <= parsed.volume {
                return Err(format!(
                    "--qty must exceed --volume to demonstrate the single-order cap (got --qty {} \
                     <= --volume {}); a request within the bar volume fills in full",
                    parsed.qty, parsed.volume
                ));
            }
        }
        Ok(parsed)
    }
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}

fn take_i64<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<i64, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<i64>()
        .map_err(|_| format!("{flag} expects an integer, got '{raw}'"))
}
