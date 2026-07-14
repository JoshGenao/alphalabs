//! SRS-DATA-021 — apply corporate actions to paper virtual positions and orders
//! (scenario CLI).
//!
//! Two subcommands drive the REAL [`atp_simulation::corporate_actions`] path:
//!
//! * `apply` — fixture positions (`--position`, built through the ledger's own
//!   fill path) and resting orders (`--order`, intake-validated) plus ONE action
//!   flag; prints a `position-outcome:` / `order-outcome:` JSON line per
//!   transform, an `alert:` line per warranted operator page (the
//!   notification intent the composition root maps onto SRS-NOTIF-001), and a
//!   `summary:` tally.
//! * `apply-from-store` — the SAME-DATA-SOURCE scenario (SYS-88): builds an
//!   in-process `MarketDataStore` from corporate-action RECORD flags (the
//!   SRS-DATA-011 record set backtests read through their gated bar reads), runs
//!   the coverage-gated `query_corporate_action_facts` read over
//!   `--facts-symbol` / `--facts-window`, prints each surfaced `fact:` line,
//!   maps facts onto paper actions (`actions_from_facts`), and applies them in
//!   event order to the fixture books. An uncovered store REFUSES the read
//!   (exit 2) — the paper application inherits the SRS-DATA-011 gate.
//!
//! std-only; depends only on the already-declared `atp-simulation` and
//! `atp-data` crates. Exit codes: `0` = applied; `2` = usage / fixture /
//! coverage error.

use std::process::ExitCode;

use atp_data::store::{
    coverage_record, delisting_record, dividend_record, merger_record, symbol_change_record,
    DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey,
};
use atp_data::CorporateActionFact;
use atp_data::UnifiedHistoricalQuery;
use atp_simulation::corporate_actions::{
    actions_from_facts, apply_corporate_action, PaperCorpActionReport, PaperCorporateAction,
    PaperOrderOutcomeKind, PaperPositionOutcomeKind,
};
use atp_simulation::paper_order::{AssetClass, OrderLeg, OrderType, PaperOrderRequest, Side};
use atp_simulation::sim::{PaperFill, PaperSimulationEngine};
use atp_simulation::virtual_ledger::VirtualLedgerBook;
use atp_simulation::virtual_orders::VirtualOrderBook;
use atp_types::StrategyId;

const USAGE: &str = "\
data021_paper_corp_action_cli — SRS-DATA-021 paper corporate-action application

USAGE:
    data021_paper_corp_action_cli apply --symbol <SYM> \\
        (--split N:M | --dividend AMT:PREVCLOSE | --merger SUCC:N:M:CASH \\
         | --symbol-change SUCC | --delisting) \\
        [--position SPEC ...] [--order SPEC ...]
    data021_paper_corp_action_cli apply-from-store \\
        --facts-symbol <SYM> [--facts-symbol <SYM> ...] --facts-window START:END \\
        [--bar SYM:TS:CLOSE ...] [--split-record SYM:TS:N:M ...] \\
        [--dividend-record SYM:TS:AMT ...] [--delisting-record SYM:TS ...] \\
        [--merger-record SYM:SUCC:TS:N:M:CASH ...] \\
        [--symbol-change-record OLD:NEW:TS ...] [--coverage SYM:THROUGH ...] \\
        [--position SPEC ...] [--order SPEC ...]

ARGS:
    --position SPEC   A paper position built through the ledger's own fill path
                      (repeatable): strat=<ID>,sym=<SYM>,qty=<i64>,price=<minor>
                      (cost basis = qty x price, signed; qty < 0 opens a short).
    --order SPEC      A resting virtual order (repeatable):
                      strat=<ID>,sym=<SYM>,side=buy|sell,qty=<i64>,
                      type=market|limit:<PX>|stop:<PX>|stoplimit:<STOP>:<LIMIT>
                      [,asset=equity|option]
    apply-from-store record flags build the fixture store; --coverage is the
    SRS-DATA-011 coverage frontier the gated fact read enforces.

OUTPUT:
    fact:{...}              (apply-from-store) one line per surfaced fact
    position-outcome:{...}  one line per transformed position
    order-outcome:{...}     one line per transformed order
    alert:{...}             one line per warranted operator page
    summary:{...}           the tally
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (subcommand, rest) = match args.split_first() {
        None => {
            eprintln!("data021_paper_corp_action_cli: missing subcommand\n\n{USAGE}");
            return ExitCode::from(2);
        }
        Some((first, rest)) => (first.as_str(), rest),
    };
    let run = match subcommand {
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        "apply" => cmd_apply(rest),
        "apply-from-store" => cmd_apply_from_store(rest),
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    };
    match run {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("data021_paper_corp_action_cli: {error}");
            ExitCode::from(2)
        }
    }
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

fn cmd_apply(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let action = parsed.corporate_action()?;
    let (mut book, mut orders) = parsed.fixture_books()?;
    let report = apply_corporate_action(&mut book, &mut orders, &action);
    print_report(&report);
    Ok(())
}

fn cmd_apply_from_store(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    if parsed.has_action_flag() {
        return Err(
            "apply-from-store reads its actions from the store's gated fact read; \
             action flags belong to `apply`"
                .to_string(),
        );
    }
    let (start_ts, end_ts) = parsed
        .facts_window
        .ok_or("--facts-window START:END is required")?;
    if parsed.facts_symbols.is_empty() {
        return Err("at least one --facts-symbol is required".to_string());
    }
    let store = parsed.fixture_store()?;
    let (mut book, mut orders) = parsed.fixture_books()?;

    let mut totals = Totals::default();
    for symbol in &parsed.facts_symbols {
        let query = UnifiedHistoricalQuery::new(symbol.clone(), "1d", start_ts, end_ts)
            .with_kind(DatasetKind::DailyEquityBar);
        // The coverage-gated fact read — the SAME corporate-action data source
        // (and the same fail-closed gate) the backtest bar reads consume.
        let facts = store
            .query_corporate_action_facts(&query)
            .map_err(|err| format!("fact read refused for {symbol}: {err}"))?;
        for fact in &facts {
            println!("fact:{}", fact_json(fact));
        }
        for action in actions_from_facts(&facts) {
            let report = apply_corporate_action(&mut book, &mut orders, &action);
            print_outcomes(&report, &mut totals);
        }
    }
    totals.print();
    Ok(())
}

// --------------------------------------------------------------------------- //
// Argument parsing (fail-closed allowlist)
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct ParsedArgs {
    symbol: Option<String>,
    split: Option<(i64, i64)>,
    dividend: Option<(i64, i64)>,
    merger: Option<(String, i64, i64, i64)>,
    symbol_change: Option<String>,
    delisting: bool,
    position_specs: Vec<String>,
    order_specs: Vec<String>,
    facts_symbols: Vec<String>,
    facts_window: Option<(i64, i64)>,
    bars: Vec<String>,
    split_records: Vec<String>,
    dividend_records: Vec<String>,
    delisting_records: Vec<String>,
    merger_records: Vec<String>,
    symbol_change_records: Vec<String>,
    coverage_records: Vec<String>,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = Self::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--symbol" => set_once(&mut parsed.symbol, take_value(&mut iter, flag)?, flag)?,
                "--split" => {
                    if parsed.split.is_some() {
                        return Err("--split given more than once".to_string());
                    }
                    parsed.split = Some(parse_ratio(&take_value(&mut iter, flag)?)?);
                }
                "--dividend" => {
                    if parsed.dividend.is_some() {
                        return Err("--dividend given more than once".to_string());
                    }
                    parsed.dividend =
                        Some(parse_pair_i64(&take_value(&mut iter, flag)?, "--dividend")?);
                }
                "--merger" => {
                    if parsed.merger.is_some() {
                        return Err("--merger given more than once".to_string());
                    }
                    parsed.merger = Some(parse_merger(&take_value(&mut iter, flag)?)?);
                }
                "--symbol-change" => set_once(
                    &mut parsed.symbol_change,
                    take_value(&mut iter, flag)?,
                    flag,
                )?,
                "--delisting" => {
                    if parsed.delisting {
                        return Err("--delisting given more than once".to_string());
                    }
                    parsed.delisting = true;
                }
                "--position" => parsed.position_specs.push(take_value(&mut iter, flag)?),
                "--order" => parsed.order_specs.push(take_value(&mut iter, flag)?),
                "--facts-symbol" => parsed.facts_symbols.push(take_value(&mut iter, flag)?),
                "--facts-window" => {
                    if parsed.facts_window.is_some() {
                        return Err("--facts-window given more than once".to_string());
                    }
                    parsed.facts_window = Some(parse_pair_i64(
                        &take_value(&mut iter, flag)?,
                        "--facts-window",
                    )?);
                }
                "--bar" => parsed.bars.push(take_value(&mut iter, flag)?),
                "--split-record" => parsed.split_records.push(take_value(&mut iter, flag)?),
                "--dividend-record" => parsed.dividend_records.push(take_value(&mut iter, flag)?),
                "--delisting-record" => parsed.delisting_records.push(take_value(&mut iter, flag)?),
                "--merger-record" => parsed.merger_records.push(take_value(&mut iter, flag)?),
                "--symbol-change-record" => parsed
                    .symbol_change_records
                    .push(take_value(&mut iter, flag)?),
                "--coverage" => parsed.coverage_records.push(take_value(&mut iter, flag)?),
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn has_action_flag(&self) -> bool {
        self.symbol.is_some()
            || self.split.is_some()
            || self.dividend.is_some()
            || self.merger.is_some()
            || self.symbol_change.is_some()
            || self.delisting
    }

    /// Build the corporate action for `apply`, enforcing exactly one action flag
    /// and a non-blank symbol.
    fn corporate_action(&self) -> Result<PaperCorporateAction, String> {
        let symbol = self
            .symbol
            .as_deref()
            .filter(|s| !s.trim().is_empty())
            .ok_or("--symbol is required and must be non-blank")?;
        let action_count = usize::from(self.split.is_some())
            + usize::from(self.dividend.is_some())
            + usize::from(self.merger.is_some())
            + usize::from(self.symbol_change.is_some())
            + usize::from(self.delisting);
        if action_count != 1 {
            return Err(
                "exactly one of --split / --dividend / --merger / --symbol-change / --delisting \
                 is required"
                    .to_string(),
            );
        }
        if let Some((numerator, denominator)) = self.split {
            return Ok(PaperCorporateAction::split(symbol, numerator, denominator));
        }
        if let Some((amount_minor, prev_close_minor)) = self.dividend {
            return Ok(PaperCorporateAction::dividend(
                symbol,
                amount_minor,
                prev_close_minor,
            ));
        }
        if let Some((successor, numerator, denominator, cash)) = &self.merger {
            return Ok(PaperCorporateAction::merger(
                symbol,
                successor.clone(),
                *numerator,
                *denominator,
                *cash,
            ));
        }
        if let Some(successor) = &self.symbol_change {
            return Ok(PaperCorporateAction::symbol_change(
                symbol,
                successor.clone(),
            ));
        }
        Ok(PaperCorporateAction::delisting(symbol))
    }

    /// Build the fixture position book (through the REAL fill path) and order
    /// book (through the REAL SRS-SIM-001 intake path —
    /// `PaperSimulationEngine::accept_order` via `place_accepted`, so every
    /// resting order the scenario cancels was genuinely accepted by the
    /// engine).
    fn fixture_books(&self) -> Result<(VirtualLedgerBook, VirtualOrderBook), String> {
        let mut book = VirtualLedgerBook::new();
        for spec in &self.position_specs {
            let position = parse_position_spec(spec)?;
            let notional = i128::from(position.quantity) * i128::from(position.price_minor);
            let cash_delta = i64::try_from(-notional)
                .map_err(|_| format!("position spec '{spec}': notional overflows"))?;
            let fill = PaperFill {
                ts: 1,
                symbol: position.symbol.clone(),
                quantity: position.quantity,
                price_minor: position.price_minor,
                commission_minor: 0,
                slippage_minor: 0,
                spread_impact_minor: 0,
                cash_delta_minor: cash_delta,
            };
            book.apply_fill(&StrategyId::new(position.strategy.clone()), &fill)
                .map_err(|err| format!("position spec '{spec}' rejected by the ledger: {err:?}"))?;
        }
        let engine = PaperSimulationEngine::new();
        let mut orders = VirtualOrderBook::new();
        for spec in &self.order_specs {
            let (strategy, leg) = parse_order_spec(spec)?;
            orders
                .place_accepted(
                    &StrategyId::new(strategy),
                    &engine,
                    &PaperOrderRequest::Single(leg),
                )
                .map_err(|err| format!("order spec '{spec}' rejected by intake: {err}"))?;
        }
        Ok((book, orders))
    }

    /// Build the in-process fixture store for `apply-from-store` — every record
    /// goes through the store's own validating `upsert`.
    fn fixture_store(&self) -> Result<MarketDataStore, String> {
        let mut store = MarketDataStore::new();
        let mut upsert = |record: MarketDataRecord, spec: &str| {
            store
                .upsert(record)
                .map(|_| ())
                .map_err(|err| format!("record '{spec}' rejected by the store: {err:?}"))
        };
        for spec in &self.bars {
            let parts = split_parts(spec, 3, "--bar SYM:TS:CLOSE")?;
            let record = MarketDataRecord::new(
                NaturalKey {
                    kind: DatasetKind::DailyEquityBar,
                    symbol: parts[0].to_string(),
                    resolution: "1d".to_string(),
                    event_ts: parse_i64(parts[1], "--bar TS")?,
                    option_contract: None,
                },
                [
                    market_field("close", parse_i64(parts[2], "--bar CLOSE")?),
                    market_field("volume", 1_000),
                ],
            )
            .map_err(|err| format!("bar '{spec}': {err:?}"))?;
            upsert(record, spec)?;
        }
        for spec in &self.split_records {
            let parts = split_parts(spec, 4, "--split-record SYM:TS:N:M")?;
            let record = MarketDataRecord::new(
                NaturalKey {
                    kind: DatasetKind::CorporateActionSplit,
                    symbol: parts[0].to_string(),
                    resolution: "split".to_string(),
                    event_ts: parse_i64(parts[1], "--split-record TS")?,
                    option_contract: None,
                },
                [
                    market_field("denominator", parse_i64(parts[3], "--split-record M")?),
                    market_field("numerator", parse_i64(parts[2], "--split-record N")?),
                ],
            )
            .map_err(|err| format!("split record '{spec}': {err:?}"))?;
            upsert(record, spec)?;
        }
        for spec in &self.dividend_records {
            let parts = split_parts(spec, 3, "--dividend-record SYM:TS:AMT")?;
            upsert(
                dividend_record(
                    parse_i64(parts[1], "--dividend-record TS")?,
                    parts[0],
                    parse_i64(parts[2], "--dividend-record AMT")?,
                ),
                spec,
            )?;
        }
        for spec in &self.delisting_records {
            let parts = split_parts(spec, 2, "--delisting-record SYM:TS")?;
            upsert(
                delisting_record(parse_i64(parts[1], "--delisting-record TS")?, parts[0]),
                spec,
            )?;
        }
        for spec in &self.merger_records {
            let parts = split_parts(spec, 6, "--merger-record SYM:SUCC:TS:N:M:CASH")?;
            upsert(
                merger_record(
                    parse_i64(parts[2], "--merger-record TS")?,
                    parts[0],
                    parts[1],
                    parse_i64(parts[3], "--merger-record N")?,
                    parse_i64(parts[4], "--merger-record M")?,
                    parse_i64(parts[5], "--merger-record CASH")?,
                ),
                spec,
            )?;
        }
        for spec in &self.symbol_change_records {
            let parts = split_parts(spec, 3, "--symbol-change-record OLD:NEW:TS")?;
            upsert(
                symbol_change_record(
                    parse_i64(parts[2], "--symbol-change-record TS")?,
                    parts[0],
                    parts[1],
                ),
                spec,
            )?;
        }
        for spec in &self.coverage_records {
            let parts = split_parts(spec, 2, "--coverage SYM:THROUGH")?;
            upsert(
                coverage_record(parse_i64(parts[1], "--coverage THROUGH")?, parts[0]),
                spec,
            )?;
        }
        Ok(store)
    }
}

struct PositionSpec {
    strategy: String,
    symbol: String,
    quantity: i64,
    price_minor: i64,
}

fn parse_position_spec(spec: &str) -> Result<PositionSpec, String> {
    let mut strategy = None;
    let mut symbol = None;
    let mut quantity = None;
    let mut price = None;
    for pair in spec.split(',') {
        let (key, value) = pair
            .split_once('=')
            .ok_or_else(|| format!("position spec '{spec}': expected key=value, got '{pair}'"))?;
        match key {
            "strat" => set_once(&mut strategy, value.to_string(), "strat")?,
            "sym" => set_once(&mut symbol, value.to_string(), "sym")?,
            "qty" => set_once(&mut quantity, parse_i64(value, "qty")?, "qty")?,
            "price" => set_once(&mut price, parse_i64(value, "price")?, "price")?,
            other => return Err(format!("position spec '{spec}': unknown key '{other}'")),
        }
    }
    Ok(PositionSpec {
        strategy: strategy.ok_or_else(|| format!("position spec '{spec}': missing strat="))?,
        symbol: symbol.ok_or_else(|| format!("position spec '{spec}': missing sym="))?,
        quantity: quantity.ok_or_else(|| format!("position spec '{spec}': missing qty="))?,
        price_minor: price.ok_or_else(|| format!("position spec '{spec}': missing price="))?,
    })
}

fn parse_order_spec(spec: &str) -> Result<(String, OrderLeg), String> {
    let mut strategy = None;
    let mut symbol = None;
    let mut side = None;
    let mut quantity = None;
    let mut order_type = None;
    let mut asset = None;
    for pair in spec.split(',') {
        let (key, value) = pair
            .split_once('=')
            .ok_or_else(|| format!("order spec '{spec}': expected key=value, got '{pair}'"))?;
        match key {
            "strat" => set_once(&mut strategy, value.to_string(), "strat")?,
            "sym" => set_once(&mut symbol, value.to_string(), "sym")?,
            "side" => {
                let parsed = match value {
                    "buy" => Side::Buy,
                    "sell" => Side::Sell,
                    other => return Err(format!("order spec '{spec}': unknown side '{other}'")),
                };
                set_once(&mut side, parsed, "side")?;
            }
            "qty" => set_once(&mut quantity, parse_i64(value, "qty")?, "qty")?,
            "type" => set_once(&mut order_type, parse_order_type(value, spec)?, "type")?,
            "asset" => {
                let parsed = match value {
                    "equity" => AssetClass::Equity,
                    "option" => AssetClass::Option,
                    other => return Err(format!("order spec '{spec}': unknown asset '{other}'")),
                };
                set_once(&mut asset, parsed, "asset")?;
            }
            other => return Err(format!("order spec '{spec}': unknown key '{other}'")),
        }
    }
    let leg = OrderLeg {
        symbol: symbol.ok_or_else(|| format!("order spec '{spec}': missing sym="))?,
        asset_class: asset.unwrap_or(AssetClass::Equity),
        side: side.ok_or_else(|| format!("order spec '{spec}': missing side="))?,
        quantity: quantity.ok_or_else(|| format!("order spec '{spec}': missing qty="))?,
        order_type: order_type.ok_or_else(|| format!("order spec '{spec}': missing type="))?,
    };
    Ok((
        strategy.ok_or_else(|| format!("order spec '{spec}': missing strat="))?,
        leg,
    ))
}

fn parse_order_type(value: &str, spec: &str) -> Result<OrderType, String> {
    let mut parts = value.split(':');
    let kind = parts.next().unwrap_or_default();
    let rest: Vec<&str> = parts.collect();
    match (kind, rest.as_slice()) {
        ("market", []) => Ok(OrderType::Market),
        ("limit", [price]) => Ok(OrderType::Limit {
            limit_price_minor: parse_i64(price, "limit price")?,
        }),
        ("stop", [price]) => Ok(OrderType::Stop {
            stop_price_minor: parse_i64(price, "stop price")?,
        }),
        ("stoplimit", [stop, limit]) => Ok(OrderType::StopLimit {
            stop_price_minor: parse_i64(stop, "stop price")?,
            limit_price_minor: parse_i64(limit, "limit price")?,
        }),
        _ => Err(format!("order spec '{spec}': malformed type '{value}'")),
    }
}

// --------------------------------------------------------------------------- //
// Small parsing helpers
// --------------------------------------------------------------------------- //

fn take_value<'a>(iter: &mut std::slice::Iter<'a, String>, flag: &str) -> Result<String, String> {
    iter.next()
        .filter(|value| !value.starts_with("--"))
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} requires a value"))
}

fn set_once<T>(slot: &mut Option<T>, value: T, flag: &str) -> Result<(), String> {
    if slot.is_some() {
        return Err(format!("{flag} given more than once"));
    }
    *slot = Some(value);
    Ok(())
}

fn parse_i64(value: &str, what: &str) -> Result<i64, String> {
    value
        .trim()
        .parse::<i64>()
        .map_err(|_| format!("{what}: '{value}' is not an integer"))
}

fn parse_ratio(value: &str) -> Result<(i64, i64), String> {
    parse_pair_i64(value, "--split")
}

fn parse_pair_i64(value: &str, flag: &str) -> Result<(i64, i64), String> {
    let (left, right) = value
        .split_once(':')
        .ok_or_else(|| format!("{flag}: expected A:B, got '{value}'"))?;
    Ok((parse_i64(left, flag)?, parse_i64(right, flag)?))
}

fn parse_merger(value: &str) -> Result<(String, i64, i64, i64), String> {
    let parts = split_parts(value, 4, "--merger SUCC:N:M:CASH")?;
    Ok((
        parts[0].to_string(),
        parse_i64(parts[1], "--merger N")?,
        parse_i64(parts[2], "--merger M")?,
        parse_i64(parts[3], "--merger CASH")?,
    ))
}

fn split_parts<'a>(value: &'a str, count: usize, shape: &str) -> Result<Vec<&'a str>, String> {
    let parts: Vec<&str> = value.split(':').collect();
    if parts.len() != count {
        return Err(format!("expected {shape}, got '{value}'"));
    }
    Ok(parts)
}

fn market_field(name: &str, value_minor: i64) -> MarketField {
    MarketField {
        name: name.to_string(),
        value_minor,
    }
}

// --------------------------------------------------------------------------- //
// Output (hand-rolled std-only JSON; C0 control characters escaped)
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct Totals {
    adjusted: usize,
    remapped: usize,
    delisted_hold: usize,
    review: usize,
    orders_adjusted: usize,
    orders_cancelled: usize,
    alerts: usize,
}

impl Totals {
    fn print(&self) {
        println!(
            "summary:{{\"adjusted\":{},\"remapped\":{},\"delisted_hold\":{},\"review\":{},\
             \"orders_adjusted\":{},\"orders_cancelled\":{},\"alerts\":{}}}",
            self.adjusted,
            self.remapped,
            self.delisted_hold,
            self.review,
            self.orders_adjusted,
            self.orders_cancelled,
            self.alerts
        );
    }
}

fn print_report(report: &PaperCorpActionReport) {
    let mut totals = Totals::default();
    print_outcomes(report, &mut totals);
    totals.print();
}

fn print_outcomes(report: &PaperCorpActionReport, totals: &mut Totals) {
    for outcome in &report.position_outcomes {
        match &outcome.kind {
            PaperPositionOutcomeKind::Adjusted { .. } => totals.adjusted += 1,
            PaperPositionOutcomeKind::Remapped { .. } => totals.remapped += 1,
            PaperPositionOutcomeKind::DelistedHold { .. } => totals.delisted_hold += 1,
            PaperPositionOutcomeKind::RequiresManualReview { .. } => totals.review += 1,
        }
        println!("position-outcome:{}", position_outcome_json(outcome));
    }
    for outcome in &report.order_outcomes {
        match &outcome.kind {
            PaperOrderOutcomeKind::Adjusted { .. } => totals.orders_adjusted += 1,
            PaperOrderOutcomeKind::Cancelled { .. } => totals.orders_cancelled += 1,
        }
        println!("order-outcome:{}", order_outcome_json(outcome));
    }
    for alert in report.alerts() {
        totals.alerts += 1;
        println!(
            "alert:{{\"strategy\":\"{}\",\"symbol\":\"{}\",\"kind\":\"{}\",\"summary\":\"{}\"}}",
            json_escape(alert.strategy.as_str()),
            json_escape(&alert.symbol),
            alert.reason.kind_str(),
            json_escape(&alert.operator_summary()),
        );
    }
}

fn position_outcome_json(
    outcome: &atp_simulation::corporate_actions::PaperPositionOutcome,
) -> String {
    let head = format!(
        "\"strategy\":\"{}\",\"symbol\":\"{}\"",
        json_escape(outcome.strategy.as_str()),
        json_escape(&outcome.symbol)
    );
    match &outcome.kind {
        PaperPositionOutcomeKind::Adjusted {
            quantity_before,
            quantity_after,
            cost_basis_before_minor,
            cost_basis_after_minor,
        } => format!(
            "{{{head},\"kind\":\"ADJUSTED\",\"quantity_before\":{quantity_before},\
             \"quantity_after\":{quantity_after},\
             \"cost_basis_before_minor\":{cost_basis_before_minor},\
             \"cost_basis_after_minor\":{cost_basis_after_minor}}}"
        ),
        PaperPositionOutcomeKind::Remapped {
            successor,
            quantity_after,
            cost_basis_after_minor,
        } => format!(
            "{{{head},\"kind\":\"REMAPPED\",\"successor\":\"{}\",\
             \"quantity_after\":{quantity_after},\
             \"cost_basis_after_minor\":{cost_basis_after_minor}}}",
            json_escape(successor)
        ),
        PaperPositionOutcomeKind::DelistedHold {
            quantity,
            cost_basis_minor,
        } => format!(
            "{{{head},\"kind\":\"DELISTED_HOLD\",\"quantity\":{quantity},\
             \"cost_basis_minor\":{cost_basis_minor}}}"
        ),
        PaperPositionOutcomeKind::RequiresManualReview { reason } => format!(
            "{{{head},\"kind\":\"MANUAL_REVIEW\",\"reason\":\"{}\"}}",
            reason.as_str()
        ),
    }
}

fn order_outcome_json(outcome: &atp_simulation::corporate_actions::PaperOrderOutcome) -> String {
    let head = format!(
        "\"id\":{},\"strategy\":\"{}\",\"symbol\":\"{}\"",
        outcome.id.value(),
        json_escape(outcome.strategy.as_str()),
        json_escape(&outcome.symbol)
    );
    match &outcome.kind {
        PaperOrderOutcomeKind::Adjusted { leg_after } => format!(
            "{{{head},\"kind\":\"ADJUSTED\",\"symbol_after\":\"{}\",\"quantity_after\":{},\
             \"order_type_after\":\"{}\"}}",
            json_escape(&leg_after.symbol),
            leg_after.quantity,
            order_type_str(&leg_after.order_type),
        ),
        PaperOrderOutcomeKind::Cancelled { reason } => format!(
            "{{{head},\"kind\":\"CANCELLED\",\"reason\":\"{}\"}}",
            reason.as_str()
        ),
    }
}

fn fact_json(fact: &CorporateActionFact) -> String {
    match fact {
        CorporateActionFact::Split {
            symbol,
            effective_ts,
            numerator,
            denominator,
        } => format!(
            "{{\"kind\":\"SPLIT\",\"symbol\":\"{}\",\"effective_ts\":{effective_ts},\
             \"numerator\":{numerator},\"denominator\":{denominator}}}",
            json_escape(symbol)
        ),
        CorporateActionFact::Dividend {
            symbol,
            ex_ts,
            amount_minor,
            prev_close_minor,
        } => format!(
            "{{\"kind\":\"DIVIDEND\",\"symbol\":\"{}\",\"ex_ts\":{ex_ts},\
             \"amount_minor\":{amount_minor},\"prev_close_minor\":{prev_close_minor}}}",
            json_escape(symbol)
        ),
        CorporateActionFact::Delisting {
            symbol,
            effective_ts,
        } => format!(
            "{{\"kind\":\"DELISTING\",\"symbol\":\"{}\",\"effective_ts\":{effective_ts}}}",
            json_escape(symbol)
        ),
        CorporateActionFact::Merger {
            symbol,
            successor,
            numerator,
            denominator,
            cash_per_share_minor,
            effective_ts,
        } => format!(
            "{{\"kind\":\"MERGER\",\"symbol\":\"{}\",\"successor\":\"{}\",\
             \"numerator\":{numerator},\"denominator\":{denominator},\
             \"cash_per_share_minor\":{cash_per_share_minor},\"effective_ts\":{effective_ts}}}",
            json_escape(symbol),
            json_escape(successor)
        ),
        CorporateActionFact::SymbolChange {
            predecessor,
            successor,
            effective_ts,
        } => format!(
            "{{\"kind\":\"SYMBOL_CHANGE\",\"predecessor\":\"{}\",\"successor\":\"{}\",\
             \"effective_ts\":{effective_ts}}}",
            json_escape(predecessor),
            json_escape(successor)
        ),
    }
}

fn order_type_str(order_type: &OrderType) -> String {
    match order_type {
        OrderType::Market => "market".to_string(),
        OrderType::Limit { limit_price_minor } => format!("limit:{limit_price_minor}"),
        OrderType::Stop { stop_price_minor } => format!("stop:{stop_price_minor}"),
        OrderType::StopLimit {
            stop_price_minor,
            limit_price_minor,
        } => format!("stoplimit:{stop_price_minor}:{limit_price_minor}"),
    }
}

/// Escape a string for a JSON string literal — quotes, backslashes, and EVERY C0
/// control character (`\uXXXX`), so a hostile symbol can never emit invalid JSON.
fn json_escape(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for c in value.chars() {
        match c {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                escaped.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => escaped.push(c),
        }
    }
    escaped
}

#[cfg(test)]
mod tests {
    use super::json_escape;

    #[test]
    fn json_escape_handles_quotes_backslashes_and_c0_controls() {
        assert_eq!(json_escape("plain"), "plain");
        assert_eq!(json_escape("a\"b"), "a\\\"b");
        assert_eq!(json_escape("a\\b"), "a\\\\b");
        assert_eq!(json_escape("a\nb\tc\rd"), "a\\nb\\tc\\rd");
        assert_eq!(json_escape("a\u{1}b"), "a\\u0001b");
    }
}
