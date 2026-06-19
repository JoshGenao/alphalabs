//! SRS-SIM-003 virtual-ledger operator CLI.
//!
//! The operator-facing surface of "maintain an independent virtual position ledger for each paper
//! strategy" (docs/SRS.md SRS-5.7 SRS-SIM-003; SyRS SYS-84; StRS SN-1.29 / SN-1.07). The acceptance
//! criterion is an *isolation* property: "quantity, average cost, unrealized P&L, realized P&L, and
//! commission paid are isolated per paper strategy and independent of IB account positions". The
//! per-strategy ledger ([`VirtualLedgerBook`], keyed by [`StrategyId`]) is already built and the
//! Rust integration test `srs_sim_003_virtual_ledger` already asserts the invariant; this binary
//! makes it *operator-demonstrable* — the same precedent as the SRS-BT-002 / BT-003 / BT-009 / BT-010
//! CLIs (there is no Python↔Rust strategy host, so the operator workflow is demonstrated over the
//! Rust core, driving the real [`PaperSimulationEngine::simulate_fill`] cost path into the real
//! ledger).
//!
//! - `defaults` — print a fresh [`VirtualLedgerBook`] and a fresh [`VirtualPosition`]: the book holds
//!   zero strategies and the position has zero quantity / no average cost / zero realized P&L / zero
//!   commission. This makes the "a virtual position starts flat, and the book holds NO IB account
//!   position" half inspectable (the book's type carries no broker/IB account at all).
//!
//! - `isolate [--lot-a N] [--lot-b N] [--symbol S] [--mark M] [--inject F] [--full]` — open the SAME
//!   symbol under TWO paper strategies (`alpha`, `beta`) with DIFFERENT real fills (alpha goes long
//!   `--lot-a` then partially sells; beta goes short `--lot-b`), print each strategy's five AC
//!   quantities (quantity, average-cost-minor, unrealized-pnl-minor marked against `--mark`,
//!   realized-pnl-minor, commission-paid-minor; all integer minor units), then prove the AC two ways:
//!     * `account-independent:true` — alpha and beta hold the SAME symbol but DIFFERENT positions,
//!       which a single shared IB account position could never produce; the positions are therefore
//!       per-strategy virtual, not account-sourced. (Non-vacuous: the two positions are asserted to
//!       differ.)
//!     * `ledger-isolation:true` — after applying one MORE fill to alpha, beta's position is
//!       byte-for-byte unchanged while alpha's DID change; one strategy's fills never touch another's.
//!       (Non-vacuous: alpha's mutation must be observable AND beta's non-mutation must be observable.)
//!
//! With `isolate --full`, ALPHA'S OWN ledger (the one the isolation workflow just mutated) is closed
//! to flat and reconciled: gross realized P&L minus the FULL transaction cost equals the sum of
//! EVERY alpha fill's cash delta (including the isolation-proof fill), proving no charged cost
//! disappears from the real alpha ledger.
//!
//! Fail closed: an unknown subcommand or flag exits non-zero. `--inject <fault>` corrupts a fill or a
//! mark so the ledger must reject it BEFORE any mutation; the CLI prints `inject=<fault>: ledger
//! failed closed` with the engine error and the unchanged strategy count, and exits non-zero with NO
//! isolation line — a corrupt fill can never produce an isolation "proof". The faults are:
//!   nonpositive-price | zero-quantity | empty-symbol | nonpositive-mark | negative-commission
//! Non-vacuity: `--lot-a` / `--lot-b` must be positive (rejected at parse), so the isolation proof is
//! never asserted over zero fills or a single strategy. `--mark` must be in `[2, i64::MAX-1]`
//! (rejected at parse) so the derived bid/ask book is a strictly-positive, non-overflowing quote —
//! the isolation proof never runs over a snapshot the simulation layer's own rules would reject.

use std::env;
use std::process::ExitCode;

use atp_simulation::fill_model::MarketSnapshot;
use atp_simulation::sim::{PaperFill, PaperSimulationEngine, SimError};
use atp_simulation::virtual_ledger::{LedgerError, VirtualLedgerBook, VirtualPosition};
use atp_types::StrategyId;

const DEFAULT_SYMBOL: &str = "AAPL";
const ALPHA: &str = "alpha";
const BETA: &str = "beta";

// Deterministic fixture prices (integer minor units / cents). Chosen so the five quantities are all
// meaningfully populated and the two strategies' positions genuinely diverge.
const ALPHA_BUY_PRICE_MINOR: i64 = 10_000; // alpha opens long here
const ALPHA_SELL_PRICE_MINOR: i64 = 10_500; // alpha partially closes here (a gain)
const BETA_SHORT_PRICE_MINOR: i64 = 10_200; // beta opens short here
const ALPHA_ADD_PRICE_MINOR: i64 = 10_300; // the extra fill that proves isolation
const ALPHA_FLATTEN_PRICE_MINOR: i64 = 10_400; // --full closes alpha out here to reconcile it
const DEFAULT_MARK_MINOR: i64 = 10_500; // mark used for unrealized P&L

// The `isolate` snapshot derives a 1-cent book (bid = mark - 1, ask = mark + 1) around the mark, so a
// valid `--mark` must produce a strictly-positive, non-overflowing book: bid = mark - 1 > 0 (mark >= 2)
// and ask = mark + 1 cannot overflow i64 (mark <= i64::MAX - 1). A mark outside this range would build
// a snapshot the simulation layer's own quote rules reject (NonPositiveQuote), so the CLI fails closed
// at parse rather than proving isolation over an impossible quote.
const MIN_MARK_MINOR: i64 = 2;
const MAX_MARK_MINOR: i64 = i64::MAX - 1;

const USAGE: &str = "\
sim003_ledger_cli — SRS-SIM-003 independent virtual-ledger operator workflow

USAGE:
    sim003_ledger_cli defaults
    sim003_ledger_cli isolate [--lot-a <n>] [--lot-b <n>] [--symbol <s>] [--mark <m>]
                              [--inject <fault>] [--full]

COMMANDS:
    defaults  Print a fresh virtual ledger book (0 strategies) and a fresh virtual position (flat:
              zero quantity, no average cost, zero realized P&L, zero commission). The book holds NO
              IB account position at all — the 'a virtual position is independent of the IB account'
              half of the acceptance criterion, made inspectable.
    isolate   Open the SAME symbol under two paper strategies (alpha, beta) with DIFFERENT real fills,
              print each strategy's five AC quantities, and prove they are isolated per strategy
              (account-independent:true, ledger-isolation:true). This is the acceptance criterion
              'quantity, average cost, unrealized P&L, realized P&L, and commission paid are isolated
              per paper strategy and independent of IB account positions', made falsifiable.

RUN FLAGS (isolate):
    --lot-a <n>     shares alpha opens long, then partially sells (default 100; must be > 0)
    --lot-b <n>     shares beta opens short (default 60; must be > 0)
    --symbol <s>    the shared symbol both strategies trade (default AAPL)
    --mark <m>      last-trade price (minor units) used to mark unrealized P&L (default 10500);
                    must be >= 2 and <= i64::MAX-1 so the derived bid/ask book is a valid quote
    --inject <f>    corrupt a fill/mark so the ledger MUST fail closed before any mutation; one of:
                    nonpositive-price | zero-quantity | empty-symbol | nonpositive-mark |
                    negative-commission
    --full          also close ALPHA's own ledger to flat and reconcile it: realized P&L minus the
                    full transaction cost equals the sum of every alpha fill's cash delta

A non-positive lot, an injected fault, or a corrupt mark is rejected before any isolation line is
printed, so the proof can never be vacuous or fabricated.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("sim003_ledger_cli: {err}");
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
        "isolate" => cmd_isolate(rest),
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

/// Print a fresh ledger book and position: the book holds no strategies and no IB account position.
fn cmd_defaults(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    if !rest.is_empty() {
        return Err(format!("`defaults` takes no arguments\n\n{USAGE}"));
    }

    let book = VirtualLedgerBook::new();
    let position = VirtualPosition::new();

    // A fresh book holds zero strategy ledgers and (structurally) no IB account position.
    println!("book-strategy-count:{}", book.strategy_count());
    println!("ib-account-positions:none");

    // A fresh virtual position is flat: every one of the five AC quantities starts at its zero/None.
    println!("default-position-quantity:{}", position.quantity());
    println!(
        "default-position-average-cost-minor:{}",
        fmt_avg_cost(position.average_cost_minor())
    );
    println!(
        "default-position-realized-pnl-minor:{}",
        position.realized_pnl_minor()
    );
    println!(
        "default-position-commission-paid-minor:{}",
        position.commission_paid_minor()
    );
    Ok(())
}

/// Open the same symbol under two strategies and prove their ledgers are isolated.
fn cmd_isolate(rest: &[String]) -> Result<(), String> {
    if wants_help(rest) {
        print!("{USAGE}");
        return Ok(());
    }
    let parsed = ParsedArgs::parse(rest)?;
    let engine = PaperSimulationEngine::new();
    let alpha = StrategyId::new(ALPHA);
    let beta = StrategyId::new(BETA);

    println!("symbol:{}", parsed.symbol);
    println!("mark-minor:{}", parsed.mark_minor);

    // On any injected fault, the ledger must fail closed before producing an isolation line.
    if let Some(fault) = parsed.inject {
        println!("inject:{}", fault.as_str());
        return inject_and_assert_fail_closed(&engine, &parsed, &alpha, fault);
    }

    // Build the two strategies' ledgers from REAL priced fills (the shared cost path). Accumulate
    // alpha's cash deltas across EVERY fill so `--full` can reconcile alpha's ACTUAL ledger (the one
    // the isolation workflow mutated), not a fresh stand-in.
    let mut book = VirtualLedgerBook::new();
    let mut alpha_cash_delta_minor: i128 = 0;

    // alpha: open long `lot_a`, then partially sell half (rounding down, at least one share) so its
    // realized P&L and commission are non-zero and it still holds an open position.
    let alpha_sell = (parsed.lot_a / 2).max(1);
    alpha_cash_delta_minor += i128::from(
        apply_real_fill(
            &engine,
            &mut book,
            &alpha,
            1,
            &parsed.symbol,
            parsed.lot_a,
            ALPHA_BUY_PRICE_MINOR,
        )?
        .cash_delta_minor,
    );
    alpha_cash_delta_minor += i128::from(
        apply_real_fill(
            &engine,
            &mut book,
            &alpha,
            2,
            &parsed.symbol,
            -alpha_sell,
            ALPHA_SELL_PRICE_MINOR,
        )?
        .cash_delta_minor,
    );

    // beta: open short `lot_b` at a different price — same symbol, a genuinely different position.
    apply_real_fill(
        &engine,
        &mut book,
        &beta,
        1,
        &parsed.symbol,
        -parsed.lot_b,
        BETA_SHORT_PRICE_MINOR,
    )?;

    let snapshot = snapshot(parsed.mark_minor);
    print_strategy(&book, &alpha, &parsed.symbol, &snapshot)?;
    print_strategy(&book, &beta, &parsed.symbol, &snapshot)?;
    println!("strategy-count:{}", book.strategy_count());

    // account-independent: alpha and beta hold the SAME symbol but DIFFERENT positions, which a
    // single shared IB account position could never produce. Refuse to claim it if they happen to be
    // equal (that would be a vacuous proof).
    let alpha_pos = book
        .position(&alpha, &parsed.symbol)
        .cloned()
        .ok_or_else(|| "alpha holds no position after its fills".to_string())?;
    let beta_pos = book
        .position(&beta, &parsed.symbol)
        .cloned()
        .ok_or_else(|| "beta holds no position after its fills".to_string())?;
    if alpha_pos == beta_pos {
        return Err(
            "alpha and beta resolved to IDENTICAL positions — refusing to assert \
             account-independent over indistinguishable ledgers (a vacuous proof); choose lots that \
             produce different positions"
                .to_string(),
        );
    }
    println!("account-independent:true");

    // ledger-isolation: apply one MORE fill to alpha and show beta is byte-for-byte unchanged while
    // alpha's position DID change. Both halves must hold or it is not a real isolation proof.
    let beta_before = beta_pos.clone();
    alpha_cash_delta_minor += i128::from(
        apply_real_fill(
            &engine,
            &mut book,
            &alpha,
            3,
            &parsed.symbol,
            alpha_sell,
            ALPHA_ADD_PRICE_MINOR,
        )?
        .cash_delta_minor,
    );
    let beta_after = book
        .position(&beta, &parsed.symbol)
        .cloned()
        .ok_or_else(|| "beta position vanished after mutating alpha".to_string())?;
    let alpha_after = book
        .position(&alpha, &parsed.symbol)
        .cloned()
        .ok_or_else(|| "alpha position vanished after its own fill".to_string())?;
    let beta_unchanged = beta_after == beta_before;
    let alpha_changed = alpha_after != alpha_pos;
    if !alpha_changed {
        return Err(
            "mutating alpha did not change alpha's position — refusing to assert ledger-isolation \
             from a no-op mutation (a vacuous proof)"
                .to_string(),
        );
    }
    if !beta_unchanged {
        return Err(
            "ledger-isolation:false — mutating alpha changed beta's position (SRS-SIM-003 \
             regression: per-strategy ledgers are NOT isolated)"
                .to_string(),
        );
    }
    println!("ledger-isolation:true");

    if parsed.full {
        reconcile_alpha(
            &engine,
            &mut book,
            &alpha,
            &parsed.symbol,
            alpha_after.quantity(),
            alpha_cash_delta_minor,
        )?;
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

/// Produce a real priced fill from the engine and apply it to `strategy`'s ledger.
fn apply_real_fill(
    engine: &PaperSimulationEngine,
    book: &mut VirtualLedgerBook,
    strategy: &StrategyId,
    ts: u64,
    symbol: &str,
    quantity: i64,
    price_minor: i64,
) -> Result<PaperFill, String> {
    let fill = engine
        .simulate_fill(ts, symbol, quantity, price_minor, None)
        .map_err(|err| err.to_string())?;
    book.apply_fill(strategy, &fill)
        .map_err(|err| err.to_string())?;
    Ok(fill)
}

/// Print one strategy's five SRS-SIM-003 quantities for `symbol`, marked against `snapshot`.
fn print_strategy(
    book: &VirtualLedgerBook,
    strategy: &StrategyId,
    symbol: &str,
    snapshot: &MarketSnapshot,
) -> Result<(), String> {
    let position = book.position(strategy, symbol).ok_or_else(|| {
        format!(
            "strategy '{}' holds no position in {symbol}",
            strategy.as_str()
        )
    })?;
    let unrealized = position
        .unrealized_pnl_minor(snapshot)
        .map_err(|err| err.to_string())?;
    println!(
        "strategy[{}] quantity:{} average-cost-minor:{} unrealized-pnl-minor:{} \
         realized-pnl-minor:{} commission-paid-minor:{}",
        strategy.as_str(),
        position.quantity(),
        fmt_avg_cost(position.average_cost_minor()),
        unrealized,
        position.realized_pnl_minor(),
        position.commission_paid_minor(),
    );
    Ok(())
}

/// Round ALPHA's actual ledger to flat (closing the position the isolation workflow built) and prove
/// it reconciles with simulated cash: gross realized P&L minus the FULL transaction cost equals the
/// sum of EVERY alpha fill's cash delta -- including the isolation-proof fill. Because this closes
/// alpha's real ledger (not a fresh stand-in), a defect anywhere in alpha's actual mutation path is
/// caught here. `open_quantity` is alpha's quantity after the isolation fill; `prior_cash_delta_minor`
/// is the sum of every alpha cash delta applied so far.
fn reconcile_alpha(
    engine: &PaperSimulationEngine,
    book: &mut VirtualLedgerBook,
    alpha: &StrategyId,
    symbol: &str,
    open_quantity: i64,
    prior_cash_delta_minor: i128,
) -> Result<(), String> {
    // Close alpha's entire open position; fold this final fill's cash delta into the running sum so
    // the reconciliation covers the WHOLE alpha lifecycle.
    let flatten = apply_real_fill(
        engine,
        book,
        alpha,
        4,
        symbol,
        -open_quantity,
        ALPHA_FLATTEN_PRICE_MINOR,
    )?;
    let total_cash_delta_minor = prior_cash_delta_minor + i128::from(flatten.cash_delta_minor);

    let position = book
        .position(alpha, symbol)
        .ok_or_else(|| "alpha position missing after flattening".to_string())?;
    if position.quantity() != 0 {
        return Err(format!(
            "alpha did not flatten (quantity {}); cannot reconcile a non-flat position",
            position.quantity()
        ));
    }
    let cost = position
        .transaction_cost_paid_minor()
        .map_err(|err| err.to_string())?;
    let net = position.realized_pnl_minor() - cost;
    println!("recon-strategy:{}", alpha.as_str());
    println!("recon-quantity:{}", position.quantity());
    println!("recon-realized-pnl-minor:{}", position.realized_pnl_minor());
    println!("recon-transaction-cost-minor:{cost}");
    println!("recon-net-minor:{net}");
    println!("recon-simulated-cash-minor:{total_cash_delta_minor}");
    println!("recon-reconciles:{}", net == total_cash_delta_minor);
    if net != total_cash_delta_minor {
        return Err(
            "recon-reconciles:false — alpha's net P&L does not equal its simulated cash (a charged \
             cost went missing from the real alpha ledger)"
                .to_string(),
        );
    }
    Ok(())
}

/// Drive the specific guard the fault targets and assert the ledger fails closed WITHOUT mutating
/// the book. Returns Err (non-zero exit) so the isolation line is never printed under a fault.
fn inject_and_assert_fail_closed(
    engine: &PaperSimulationEngine,
    parsed: &ParsedArgs,
    strategy: &StrategyId,
    fault: Fault,
) -> Result<(), String> {
    let mut book = VirtualLedgerBook::new();
    let err: String = match fault {
        // Rejected at simulate_fill, before any PaperFill exists.
        Fault::NonPositivePrice => {
            match engine.simulate_fill(1, &parsed.symbol, parsed.lot_a, 0, None) {
                Err(SimError::NonPositivePrice { .. }) => {
                    "simulate_fill rejected a non-positive price".to_string()
                }
                other => return Err(unexpected("nonpositive-price", other)),
            }
        }
        // simulate_fill accepts a zero-quantity fill; the ledger's apply_fill rejects it.
        Fault::ZeroQuantity => {
            let fill = engine
                .simulate_fill(1, &parsed.symbol, 0, ALPHA_BUY_PRICE_MINOR, None)
                .map_err(|e| format!("simulate_fill failed before the ledger guard: {e}"))?;
            match book.apply_fill(strategy, &fill) {
                Err(LedgerError::ZeroQuantityFill) => {
                    "ledger rejected a zero-quantity fill".to_string()
                }
                other => return Err(unexpected_ledger("zero-quantity", other)),
            }
        }
        // Rejected at simulate_fill (empty symbol).
        Fault::EmptySymbol => {
            match engine.simulate_fill(1, "   ", parsed.lot_a, ALPHA_BUY_PRICE_MINOR, None) {
                Err(SimError::EmptySymbol) => "simulate_fill rejected an empty symbol".to_string(),
                other => return Err(unexpected("empty-symbol", other)),
            }
        }
        // The mark guard is exercised WITHOUT mutating the ledger: a fresh flat position rejects a
        // non-positive mark before computing any value (unrealized_pnl_minor checks the mark BEFORE
        // the flat short-circuit), so no fill is ever applied and the book stays empty -- the fault
        // is genuinely rejected before any mutation, like every other inject path.
        Fault::NonPositiveMark => match VirtualPosition::new().unrealized_pnl_minor(&snapshot(0)) {
            Err(LedgerError::NonPositiveMark { .. }) => {
                "ledger rejected a non-positive mark".to_string()
            }
            other => {
                return Err(format!(
                    "inject=nonpositive-mark: expected a NonPositiveMark error, got {other:?}"
                ))
            }
        },
        // A misconfigured (negative) commission is rejected at simulate_fill (cost validation).
        Fault::NegativeCommission => {
            let bad_engine = PaperSimulationEngine::with_cost_config(negative_commission_config());
            match bad_engine.simulate_fill(
                1,
                &parsed.symbol,
                parsed.lot_a,
                ALPHA_BUY_PRICE_MINOR,
                None,
            ) {
                Err(SimError::Cost(_)) => {
                    "simulate_fill rejected a negative commission config".to_string()
                }
                other => return Err(unexpected("negative-commission", other)),
            }
        }
    };

    // EVERY inject path rejects the fault BEFORE any mutation, so the book must still be empty --
    // the no-mutation safety claim is uniform across all faults (no fault path leaves a position
    // behind, not even a "valid setup" fill).
    if book.strategy_count() != 0 {
        return Err(format!(
            "inject={}: book mutated unexpectedly (strategy-count={}, expected 0)",
            fault.as_str(),
            book.strategy_count()
        ));
    }
    Err(format!(
        "inject={}: ledger failed closed ({err}); book unchanged (strategy-count={})",
        fault.as_str(),
        book.strategy_count()
    ))
}

fn unexpected(fault: &str, got: Result<PaperFill, SimError>) -> String {
    format!("inject={fault}: expected the engine to fail closed, got {got:?}")
}

fn unexpected_ledger(fault: &str, got: Result<(), LedgerError>) -> String {
    format!("inject={fault}: expected the ledger to fail closed, got {got:?}")
}

/// A cost config with a negative per-share commission rate — the misconfiguration the cost family
/// rejects (mirrors the bt003 negative-commission fault).
fn negative_commission_config() -> atp_simulation::cost::CostConfig {
    use atp_simulation::cost::{CommissionModel, CostConfig};
    CostConfig {
        commission: CommissionModel::PerShare {
            rate_centiminor_per_share: -1,
            min_per_order_minor: 0,
        },
        ..CostConfig::default()
    }
}

fn snapshot(last_minor: i64) -> MarketSnapshot {
    MarketSnapshot {
        bid_minor: last_minor - 1,
        ask_minor: last_minor + 1,
        last_minor,
        bar_volume: 10_000,
    }
}

fn fmt_avg_cost(avg: Option<i128>) -> String {
    match avg {
        Some(value) => value.to_string(),
        None => "none".to_string(),
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

/// A fault to inject so the ledger must fail closed before any mutation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fault {
    NonPositivePrice,
    ZeroQuantity,
    EmptySymbol,
    NonPositiveMark,
    NegativeCommission,
}

impl Fault {
    fn parse(spec: &str) -> Result<Self, String> {
        match spec {
            "nonpositive-price" => Ok(Self::NonPositivePrice),
            "zero-quantity" => Ok(Self::ZeroQuantity),
            "empty-symbol" => Ok(Self::EmptySymbol),
            "nonpositive-mark" => Ok(Self::NonPositiveMark),
            "negative-commission" => Ok(Self::NegativeCommission),
            other => Err(format!(
                "unknown fault '{other}' (expected nonpositive-price|zero-quantity|empty-symbol|\
                 nonpositive-mark|negative-commission)"
            )),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::NonPositivePrice => "nonpositive-price",
            Self::ZeroQuantity => "zero-quantity",
            Self::EmptySymbol => "empty-symbol",
            Self::NonPositiveMark => "nonpositive-mark",
            Self::NegativeCommission => "negative-commission",
        }
    }
}

struct ParsedArgs {
    lot_a: i64,
    lot_b: i64,
    symbol: String,
    mark_minor: i64,
    inject: Option<Fault>,
    full: bool,
}

impl Default for ParsedArgs {
    fn default() -> Self {
        Self {
            lot_a: 100,
            lot_b: 60,
            symbol: DEFAULT_SYMBOL.to_string(),
            mark_minor: DEFAULT_MARK_MINOR,
            inject: None,
            full: false,
        }
    }
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--lot-a" => parsed.lot_a = take_i64(&mut iter, flag)?,
                "--lot-b" => parsed.lot_b = take_i64(&mut iter, flag)?,
                "--symbol" => parsed.symbol = take_value(&mut iter, flag)?,
                "--mark" => parsed.mark_minor = take_i64(&mut iter, flag)?,
                "--inject" => parsed.inject = Some(Fault::parse(&take_value(&mut iter, flag)?)?),
                "--full" => parsed.full = true,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        // A non-positive lot would trade nothing (or be a corrupt fill the engine rejects), so the
        // isolation proof could never be exercised over two real, distinct positions. Reject it at
        // parse time so the proof can never be empty. The dedicated zero-quantity fail-closed path is
        // exercised via `--inject zero-quantity`.
        if parsed.lot_a <= 0 {
            return Err(format!(
                "--lot-a must be a positive share count (got {}); a non-positive lot would leave \
                 nothing to isolate",
                parsed.lot_a
            ));
        }
        if parsed.lot_b <= 0 {
            return Err(format!(
                "--lot-b must be a positive share count (got {}); a non-positive lot would leave \
                 nothing to isolate",
                parsed.lot_b
            ));
        }
        // The mark must produce a VALID market snapshot: the `isolate` book is bid = mark - 1,
        // ask = mark + 1, so the mark must yield a strictly-positive, non-crossed, non-overflowing
        // quote (the simulation layer's own snapshot rules). Reject an out-of-range mark at parse so
        // the isolation proof never runs over a snapshot the engine itself would reject as corrupt
        // market data.
        if parsed.mark_minor < MIN_MARK_MINOR || parsed.mark_minor > MAX_MARK_MINOR {
            return Err(format!(
                "--mark must be in [{MIN_MARK_MINOR}, {MAX_MARK_MINOR}] minor units (got {}); the \
                 isolate snapshot derives bid = mark - 1 and ask = mark + 1, so a smaller mark would \
                 build a non-positive (corrupt) quote and a larger one would overflow",
                parsed.mark_minor
            ));
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
