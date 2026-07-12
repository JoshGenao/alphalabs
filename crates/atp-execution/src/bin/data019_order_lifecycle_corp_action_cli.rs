//! SRS-DATA-019 — adjust/cancel live resting orders on corporate actions (scenario CLI).
//!
//! Drives the real [`atp_execution::corporate_action_orders`] planner over fixture
//! resting orders (parsed from `--order` specs and/or a `--orders-file`) and a
//! fixture corporate action (`--split N:M` or `--delisting` on `--symbol`). For
//! each order it prints an `outcome:` JSON line — an `ADJUSTED` order carries its
//! before/after quantity + limit/stop prices; a `CANCELLED` order carries the
//! structured reason PLUS the emitted operator-notification intent
//! (`trigger_kind` / `severity` / `summary`) and the strategy-callback intent
//! (`event_type` CANCELLED / `reason`) — so a scenario test can assert BOTH AC
//! notify clauses from stdout. A final `summary:` line tallies the outcomes.
//!
//! This is a pure planner: it makes the adjust/cancel DECISION over fixtures. The
//! production feed of live resting-order state and the routing of the resulting
//! cancel / cancel-replace to the real IB adapter is the deferred SRS-EXE-001 /
//! SRS-EXE-006 runtime; real operator email/SMS is SRS-NOTIF-001; live callback
//! delivery is SRS-SDK-004. std-only; depends only on the already-declared
//! `atp-execution` / `atp-types` crates.
//!
//! Exit codes: `0` = every order planned; `2` = usage / fixture error (unknown
//! flag, malformed order spec, blank symbol, non-integer ratio, or neither/both
//! of `--split` / `--delisting`).

use atp_execution::corporate_action_orders::{
    plan_resting_order, RestingOrderCorporateAction, RestingOrderOutcome,
};
use atp_types::{
    AssetClass, ClientCorrelationId, OrderKey, OrderSide, OrderSubmission, OrderType, StrategyId,
};
use std::process::ExitCode;

const STRATEGY_ID: &str = "live-1";

const USAGE: &str = "\
data019_order_lifecycle_corp_action_cli — SRS-DATA-019 resting-order corporate-action planner

USAGE:
    data019_order_lifecycle_corp_action_cli plan --symbol <SYM> (--split N:M | --delisting) \\
        [--order SPEC ...] [--orders-file <path> ...]

ARGS:
    --symbol <SYM>        The security the corporate action affects (non-blank).
    --split N:M           An N-for-M split (forward when N>M, reverse when N<M); N and M integers.
    --delisting           The security was delisted (mutually exclusive with --split).
    --order SPEC          A resting order (repeatable). SPEC is comma-separated key=value:
                            id=<s>,side=BUY|SELL,qty=<i>,type=MARKET|LIMIT|STOP|STOP_LIMIT
                            [,sym=<SYM>][,limit=<minor>][,stop=<minor>]
                          `sym` defaults to --symbol. LIMIT needs limit; STOP needs stop;
                          STOP_LIMIT needs both; MARKET takes neither.
    --orders-file <path>  Read order SPECs, one per line (repeatable). Blank lines and lines
                          beginning with '#' are skipped.

OUTPUT:
    outcome:{...}   one JSON object per order (ADJUSTED old/new, or CANCELLED + reason +
                    notification + callback intents, or UNAFFECTED)
    summary:{\"adjusted\":N,\"cancelled\":M,\"unaffected\":K}
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (subcommand, rest) = match args.split_first() {
        None => {
            eprintln!("data019_order_lifecycle_corp_action_cli: missing subcommand\n\n{USAGE}");
            return ExitCode::from(2);
        }
        Some((first, rest)) => (first.as_str(), rest),
    };
    match subcommand {
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            ExitCode::SUCCESS
        }
        "plan" => match cmd_plan(rest) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("data019_order_lifecycle_corp_action_cli: {error}");
                ExitCode::from(2)
            }
        },
        other => {
            eprintln!(
                "data019_order_lifecycle_corp_action_cli: unknown subcommand '{other}'\n\n{USAGE}"
            );
            ExitCode::from(2)
        }
    }
}

fn cmd_plan(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let action = parsed.corporate_action()?;
    let orders = parsed.resting_orders(&action.symbol)?;
    if orders.is_empty() {
        return Err("no resting orders given (--order / --orders-file)".to_string());
    }

    let mut adjusted = 0usize;
    let mut cancelled = 0usize;
    let mut unaffected = 0usize;
    for (key, submission) in &orders {
        let outcome = plan_resting_order(key, submission, &action);
        match &outcome {
            RestingOrderOutcome::Adjusted { .. } => adjusted += 1,
            RestingOrderOutcome::Cancelled { .. } => cancelled += 1,
            RestingOrderOutcome::Unaffected { .. } => unaffected += 1,
        }
        println!("outcome:{}", outcome_json(&outcome, submission));
    }
    println!(
        "summary:{{\"adjusted\":{adjusted},\"cancelled\":{cancelled},\"unaffected\":{unaffected}}}"
    );
    Ok(())
}

// --------------------------------------------------------------------------- //
// Argument parsing (fail-closed allowlist)
// --------------------------------------------------------------------------- //

struct ParsedArgs {
    symbol: Option<String>,
    split: Option<(i64, i64)>,
    delisting: bool,
    order_specs: Vec<String>,
    orders_files: Vec<String>,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = Self {
            symbol: None,
            split: None,
            delisting: false,
            order_specs: Vec::new(),
            orders_files: Vec::new(),
        };
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
                "--delisting" => {
                    if parsed.delisting {
                        return Err("--delisting given more than once".to_string());
                    }
                    parsed.delisting = true;
                }
                "--order" => parsed.order_specs.push(take_value(&mut iter, flag)?),
                "--orders-file" => parsed.orders_files.push(take_value(&mut iter, flag)?),
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    /// Build the corporate action, enforcing exactly one of `--split` / `--delisting`
    /// and a non-blank symbol.
    fn corporate_action(&self) -> Result<RestingOrderCorporateAction, String> {
        let symbol = self
            .symbol
            .as_deref()
            .filter(|s| !s.trim().is_empty())
            .ok_or("--symbol is required and must be non-blank")?;
        match (self.split, self.delisting) {
            (Some(_), true) => Err("--split and --delisting are mutually exclusive".to_string()),
            (None, false) => {
                Err("exactly one of --split N:M / --delisting is required".to_string())
            }
            (Some((numerator, denominator)), false) => Ok(RestingOrderCorporateAction::split(
                symbol,
                numerator,
                denominator,
            )),
            (None, true) => Ok(RestingOrderCorporateAction::delisting(symbol)),
        }
    }

    /// Parse every `--order` spec and every line of every `--orders-file` into
    /// (key, submission) pairs, defaulting each order's symbol to `action_symbol`.
    fn resting_orders(
        &self,
        action_symbol: &str,
    ) -> Result<Vec<(OrderKey, OrderSubmission)>, String> {
        let mut orders = Vec::new();
        for spec in &self.order_specs {
            orders.push(parse_order_spec(spec, action_symbol)?);
        }
        for path in &self.orders_files {
            let contents = std::fs::read_to_string(path)
                .map_err(|err| format!("cannot read --orders-file '{path}': {err}"))?;
            for line in contents.lines() {
                let trimmed = line.trim();
                if trimmed.is_empty() || trimmed.starts_with('#') {
                    continue;
                }
                orders.push(parse_order_spec(trimmed, action_symbol)?);
            }
        }
        Ok(orders)
    }
}

fn set_once(slot: &mut Option<String>, value: String, flag: &str) -> Result<(), String> {
    if slot.is_some() {
        return Err(format!("{flag} given more than once"));
    }
    *slot = Some(value);
    Ok(())
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}

/// Parse an `N:M` split ratio into two integers (a non-integer factor is a usage
/// error; a non-positive-but-integer factor flows to the planner, which cancels).
fn parse_ratio(spec: &str) -> Result<(i64, i64), String> {
    let (num, den) = spec
        .split_once(':')
        .ok_or_else(|| format!("--split expects N:M, got '{spec}'"))?;
    let numerator = num
        .trim()
        .parse::<i64>()
        .map_err(|_| format!("--split numerator '{num}' is not an integer"))?;
    let denominator = den
        .trim()
        .parse::<i64>()
        .map_err(|_| format!("--split denominator '{den}' is not an integer"))?;
    Ok((numerator, denominator))
}

/// Parse one comma-separated `key=value` order spec into a validated resting order.
fn parse_order_spec(
    spec: &str,
    action_symbol: &str,
) -> Result<(OrderKey, OrderSubmission), String> {
    let mut id: Option<String> = None;
    let mut symbol: Option<String> = None;
    let mut side: Option<OrderSide> = None;
    let mut quantity: Option<i64> = None;
    let mut type_name: Option<String> = None;
    let mut limit: Option<i64> = None;
    let mut stop: Option<i64> = None;

    for field in spec.split(',') {
        let field = field.trim();
        if field.is_empty() {
            continue;
        }
        let (key, value) = field
            .split_once('=')
            .ok_or_else(|| format!("order field '{field}' is not key=value"))?;
        match key.trim() {
            "id" => set_once(&mut id, value.trim().to_string(), "id")?,
            "sym" => set_once(&mut symbol, value.trim().to_string(), "sym")?,
            "side" => {
                let parsed = match value.trim() {
                    "BUY" => OrderSide::Buy,
                    "SELL" => OrderSide::Sell,
                    other => return Err(format!("order side '{other}' must be BUY or SELL")),
                };
                if side.replace(parsed).is_some() {
                    return Err("order 'side' given more than once".to_string());
                }
            }
            "qty" => {
                let parsed = value
                    .trim()
                    .parse::<i64>()
                    .map_err(|_| format!("order qty '{}' is not an integer", value.trim()))?;
                if quantity.replace(parsed).is_some() {
                    return Err("order 'qty' given more than once".to_string());
                }
            }
            "type" => set_once(&mut type_name, value.trim().to_string(), "type")?,
            "limit" => {
                let parsed = value
                    .trim()
                    .parse::<i64>()
                    .map_err(|_| format!("order limit '{}' is not an integer", value.trim()))?;
                if limit.replace(parsed).is_some() {
                    return Err("order 'limit' given more than once".to_string());
                }
            }
            "stop" => {
                let parsed = value
                    .trim()
                    .parse::<i64>()
                    .map_err(|_| format!("order stop '{}' is not an integer", value.trim()))?;
                if stop.replace(parsed).is_some() {
                    return Err("order 'stop' given more than once".to_string());
                }
            }
            other => return Err(format!("unknown order field '{other}'")),
        }
    }

    let id = id
        .filter(|s| !s.is_empty())
        .ok_or("order 'id' is required")?;
    let side = side.ok_or("order 'side' is required")?;
    let quantity = quantity.ok_or("order 'qty' is required")?;
    let type_name = type_name.ok_or("order 'type' is required")?;
    let symbol = symbol.unwrap_or_else(|| action_symbol.to_string());
    let order_type = build_order_type(&type_name, limit, stop)?;

    let strategy_id = StrategyId::new(STRATEGY_ID);
    let correlation_id =
        ClientCorrelationId::new(&id).map_err(|err| format!("order id '{id}': {err}"))?;
    let key = OrderKey::new(strategy_id.clone(), correlation_id);
    let submission = OrderSubmission::new(
        strategy_id,
        symbol,
        quantity,
        AssetClass::Equity,
        side,
        order_type,
    );
    // A resting order is already-valid live state; a malformed spec is a usage error.
    submission
        .validate()
        .map_err(|err| format!("order '{id}' is not a valid resting order: {err:?}"))?;
    Ok((key, submission))
}

/// Build an [`OrderType`] from the type name and the prices present, enforcing the
/// per-type price matrix (LIMIT needs limit; STOP needs stop; STOP_LIMIT both;
/// MARKET neither).
fn build_order_type(
    type_name: &str,
    limit: Option<i64>,
    stop: Option<i64>,
) -> Result<OrderType, String> {
    match type_name {
        "MARKET" => match (limit, stop) {
            (None, None) => Ok(OrderType::Market),
            _ => Err("MARKET order takes neither limit nor stop".to_string()),
        },
        "LIMIT" => match (limit, stop) {
            (Some(limit_price_minor), None) => Ok(OrderType::Limit { limit_price_minor }),
            (None, _) => Err("LIMIT order requires limit".to_string()),
            (Some(_), Some(_)) => Err("LIMIT order takes no stop".to_string()),
        },
        "STOP" => match (limit, stop) {
            (None, Some(stop_price_minor)) => Ok(OrderType::Stop { stop_price_minor }),
            (_, None) => Err("STOP order requires stop".to_string()),
            (Some(_), Some(_)) => Err("STOP order takes no limit".to_string()),
        },
        "STOP_LIMIT" => match (limit, stop) {
            (Some(limit_price_minor), Some(stop_price_minor)) => Ok(OrderType::StopLimit {
                stop_price_minor,
                limit_price_minor,
            }),
            _ => Err("STOP_LIMIT order requires both limit and stop".to_string()),
        },
        other => Err(format!(
            "order type '{other}' must be MARKET|LIMIT|STOP|STOP_LIMIT"
        )),
    }
}

// --------------------------------------------------------------------------- //
// JSON emission (hand-rolled; std-only)
// --------------------------------------------------------------------------- //

fn outcome_json(outcome: &RestingOrderOutcome, submission: &OrderSubmission) -> String {
    match outcome {
        RestingOrderOutcome::Adjusted {
            key,
            old,
            new_submission,
        } => format!(
            "{{\"order_id\":\"{}\",\"symbol\":\"{}\",\"result\":\"ADJUSTED\",\"old\":{},\"new\":{}}}",
            json_escape(&key.to_string()),
            json_escape(&submission.symbol),
            submission_json(old),
            submission_json(new_submission),
        ),
        RestingOrderOutcome::Cancelled {
            key,
            symbol,
            reason,
        } => {
            let alert = outcome
                .alert()
                .expect("a Cancelled outcome always yields an alert");
            let summary = json_escape(&alert.operator_summary());
            let callback_reason = json_escape(&alert.callback_reason());
            format!(
                "{{\"order_id\":\"{order_id}\",\"symbol\":\"{symbol}\",\"result\":\"CANCELLED\",\
                 \"reason\":\"{reason}\",\
                 \"notification\":{{\"trigger_kind\":\"CRITICAL_FAILURE\",\"severity\":\"CRITICAL\",\
                 \"summary\":\"{summary}\"}},\
                 \"callback\":{{\"event_type\":\"CANCELLED\",\"order_id\":\"{order_id}\",\
                 \"fill_price\":0.0,\"fill_quantity\":0,\"reason\":\"{callback_reason}\"}}}}",
                order_id = json_escape(&key.to_string()),
                symbol = json_escape(symbol),
                reason = reason.as_str(),
            )
        }
        RestingOrderOutcome::Unaffected { key } => format!(
            "{{\"order_id\":\"{}\",\"symbol\":\"{}\",\"result\":\"UNAFFECTED\"}}",
            json_escape(&key.to_string()),
            json_escape(&submission.symbol),
        ),
    }
}

fn submission_json(submission: &OrderSubmission) -> String {
    format!(
        "{{\"quantity\":{},\"limit_minor\":{},\"stop_minor\":{}}}",
        submission.quantity,
        opt_minor(submission.order_type.limit_price_minor()),
        opt_minor(submission.order_type.stop_price_minor()),
    )
}

fn opt_minor(value: Option<i64>) -> String {
    match value {
        Some(price) => price.to_string(),
        None => "null".to_string(),
    }
}

fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            other => out.push(other),
        }
    }
    out
}
