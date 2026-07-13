//! SRS-DATA-020 — adjust live positions on corporate actions (scenario CLI).
//!
//! Drives the real [`atp_execution::corporate_action_positions`] planner over fixture
//! positions (parsed from `--position` specs and/or a `--positions-file`) and a
//! fixture corporate action (`--split`, `--dividend`, `--merger`, `--symbol-change`,
//! or `--delisting` on `--symbol`). For each position it prints an `outcome:` JSON
//! line — an `ADJUSTED` position carries its before/after quantity + cost basis +
//! average cost; a `REMAPPED` position carries its successor + resulting position; a
//! `DELISTED` position carries the emitted operator-notification intent PLUS the
//! strategy-callback intent; a `MANUAL_REVIEW` position carries the structured reason
//! + the notification intent. A final `summary:` line tallies the outcomes.
//!
//! This is a pure planner: it makes the adjust / remap / delist / review DECISION over
//! fixtures. The production feed of live positions **carrying cost basis** is the
//! deferred SRS-EXE-006 / API-5 brokerage adapter positions sync; real operator
//! email/SMS is SRS-NOTIF-001; live callback delivery is SRS-SDK-004. std-only;
//! depends only on the already-declared `atp-execution` crate.
//!
//! Exit codes: `0` = every position planned; `2` = usage / fixture error (unknown
//! flag, malformed position spec, blank symbol, non-integer field, or not exactly one
//! of the action flags).

use atp_execution::corporate_action_positions::{
    plan_positions, LivePosition, PositionChangeEvent, PositionCorpActionOutcome,
    PositionCorporateAction,
};
use std::process::ExitCode;

const USAGE: &str = "\
data020_position_corp_action_cli — SRS-DATA-020 live-position corporate-action planner

USAGE:
    data020_position_corp_action_cli plan --symbol <SYM> \\
        (--split N:M | --dividend AMT:PREVCLOSE | --merger SUCC:N:M:CASH \\
         | --symbol-change SUCC | --delisting) \\
        [--position SPEC ...] [--positions-file <path> ...]

ARGS:
    --symbol <SYM>            The security the corporate action affects (non-blank).
    --split N:M               An N-for-M split (forward when N>M, reverse when N<M).
    --dividend AMT:PREVCLOSE   A cash dividend of AMT minor units per share against reference
                              close PREVCLOSE minor units.
    --merger SUCC:N:M:CASH     A merger into SUCC at N successor shares per M acquired shares,
                              with CASH minor units cash per acquired share (0 = stock-for-stock).
    --symbol-change SUCC      A relabel to successor SUCC.
    --delisting               The security was delisted.
    --position SPEC           A live position (repeatable). SPEC is comma-separated key=value:
                                sym=<SYM>,qty=<i>,basis=<i128>[,status=ACTIVE|DELISTED]
                              `sym` defaults to --symbol; `basis` is the signed total cost basis
                              in minor units. Exactly one action flag is required.
    --positions-file <path>   Read position SPECs, one per line (repeatable). Blank lines and
                              lines beginning with '#' are skipped.

OUTPUT:
    outcome:{...}   one JSON object per position (ADJUSTED / REMAPPED / DELISTED /
                    MANUAL_REVIEW / UNAFFECTED, with notification + callback intents)
    summary:{\"adjusted\":A,\"remapped\":R,\"delisted\":D,\"review\":V,\"unaffected\":U}
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (subcommand, rest) = match args.split_first() {
        None => {
            eprintln!("data020_position_corp_action_cli: missing subcommand\n\n{USAGE}");
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
                eprintln!("data020_position_corp_action_cli: {error}");
                ExitCode::from(2)
            }
        },
        other => {
            eprintln!("data020_position_corp_action_cli: unknown subcommand '{other}'\n\n{USAGE}");
            ExitCode::from(2)
        }
    }
}

fn cmd_plan(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let action = parsed.corporate_action()?;
    let positions = parsed.positions(&action.symbol)?;
    if positions.is_empty() {
        return Err("no positions given (--position / --positions-file)".to_string());
    }

    let outcomes = plan_positions(&positions, &action);
    let mut adjusted = 0usize;
    let mut remapped = 0usize;
    let mut delisted = 0usize;
    let mut review = 0usize;
    let mut unaffected = 0usize;
    for outcome in &outcomes {
        match outcome {
            PositionCorpActionOutcome::Adjusted { .. } => adjusted += 1,
            PositionCorpActionOutcome::Remapped { .. } => remapped += 1,
            PositionCorpActionOutcome::Delisted { .. } => delisted += 1,
            PositionCorpActionOutcome::RequiresManualReview { .. } => review += 1,
            PositionCorpActionOutcome::Unaffected { .. } => unaffected += 1,
        }
        println!("outcome:{}", outcome_json(outcome));
    }
    println!(
        "summary:{{\"adjusted\":{adjusted},\"remapped\":{remapped},\"delisted\":{delisted},\
         \"review\":{review},\"unaffected\":{unaffected}}}"
    );
    Ok(())
}

// --------------------------------------------------------------------------- //
// Argument parsing (fail-closed allowlist)
// --------------------------------------------------------------------------- //

struct ParsedArgs {
    symbol: Option<String>,
    split: Option<(i64, i64)>,
    dividend: Option<(i64, i64)>,
    merger: Option<(String, i64, i64, i64)>,
    symbol_change: Option<String>,
    delisting: bool,
    position_specs: Vec<String>,
    positions_files: Vec<String>,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = Self {
            symbol: None,
            split: None,
            dividend: None,
            merger: None,
            symbol_change: None,
            delisting: false,
            position_specs: Vec::new(),
            positions_files: Vec::new(),
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
                "--dividend" => {
                    if parsed.dividend.is_some() {
                        return Err("--dividend given more than once".to_string());
                    }
                    parsed.dividend = Some(parse_dividend(&take_value(&mut iter, flag)?)?);
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
                "--positions-file" => parsed.positions_files.push(take_value(&mut iter, flag)?),
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    /// Build the corporate action, enforcing exactly one action flag and a non-blank
    /// symbol.
    fn corporate_action(&self) -> Result<PositionCorporateAction, String> {
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
            return Ok(PositionCorporateAction::split(
                symbol,
                numerator,
                denominator,
            ));
        }
        if let Some((amount_minor, prev_close_minor)) = self.dividend {
            return Ok(PositionCorporateAction::dividend(
                symbol,
                amount_minor,
                prev_close_minor,
            ));
        }
        if let Some((successor, numerator, denominator, cash)) = &self.merger {
            return Ok(PositionCorporateAction::merger(
                symbol,
                successor.clone(),
                *numerator,
                *denominator,
                *cash,
            ));
        }
        if let Some(successor) = &self.symbol_change {
            return Ok(PositionCorporateAction::symbol_change(
                symbol,
                successor.clone(),
            ));
        }
        Ok(PositionCorporateAction::delisting(symbol))
    }

    /// Parse every `--position` spec and every line of every `--positions-file` into a
    /// validated [`LivePosition`], defaulting each position's symbol to `action_symbol`.
    fn positions(&self, action_symbol: &str) -> Result<Vec<LivePosition>, String> {
        let mut positions = Vec::new();
        for spec in &self.position_specs {
            positions.push(parse_position_spec(spec, action_symbol)?);
        }
        for path in &self.positions_files {
            let contents = std::fs::read_to_string(path)
                .map_err(|err| format!("cannot read --positions-file '{path}': {err}"))?;
            for line in contents.lines() {
                let trimmed = line.trim();
                if trimmed.is_empty() || trimmed.starts_with('#') {
                    continue;
                }
                positions.push(parse_position_spec(trimmed, action_symbol)?);
            }
        }
        Ok(positions)
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

/// Parse an `N:M` split ratio into two integers.
fn parse_ratio(spec: &str) -> Result<(i64, i64), String> {
    let (num, den) = spec
        .split_once(':')
        .ok_or_else(|| format!("--split expects N:M, got '{spec}'"))?;
    let numerator = parse_i64(num, "--split numerator")?;
    let denominator = parse_i64(den, "--split denominator")?;
    Ok((numerator, denominator))
}

/// Parse an `AMT:PREVCLOSE` dividend spec into two integers.
fn parse_dividend(spec: &str) -> Result<(i64, i64), String> {
    let (amount, prev_close) = spec
        .split_once(':')
        .ok_or_else(|| format!("--dividend expects AMT:PREVCLOSE, got '{spec}'"))?;
    let amount_minor = parse_i64(amount, "--dividend amount")?;
    let prev_close_minor = parse_i64(prev_close, "--dividend prev-close")?;
    Ok((amount_minor, prev_close_minor))
}

/// Parse a `SUCC:N:M:CASH` merger spec into (successor, numerator, denominator, cash).
fn parse_merger(spec: &str) -> Result<(String, i64, i64, i64), String> {
    let parts: Vec<&str> = spec.split(':').collect();
    if parts.len() != 4 {
        return Err(format!("--merger expects SUCC:N:M:CASH, got '{spec}'"));
    }
    let successor = parts[0].trim().to_string();
    if successor.is_empty() {
        return Err("--merger successor is blank".to_string());
    }
    let numerator = parse_i64(parts[1], "--merger numerator")?;
    let denominator = parse_i64(parts[2], "--merger denominator")?;
    let cash = parse_i64(parts[3], "--merger cash")?;
    Ok((successor, numerator, denominator, cash))
}

fn parse_i64(value: &str, label: &str) -> Result<i64, String> {
    value
        .trim()
        .parse::<i64>()
        .map_err(|_| format!("{label} '{}' is not an integer", value.trim()))
}

fn parse_i128(value: &str, label: &str) -> Result<i128, String> {
    value
        .trim()
        .parse::<i128>()
        .map_err(|_| format!("{label} '{}' is not an integer", value.trim()))
}

/// Parse one comma-separated `key=value` position spec into a validated [`LivePosition`].
fn parse_position_spec(spec: &str, action_symbol: &str) -> Result<LivePosition, String> {
    let mut symbol: Option<String> = None;
    let mut quantity: Option<i64> = None;
    let mut basis: Option<i128> = None;
    let mut status: Option<String> = None;

    for field in spec.split(',') {
        let field = field.trim();
        if field.is_empty() {
            continue;
        }
        let (key, value) = field
            .split_once('=')
            .ok_or_else(|| format!("position field '{field}' is not key=value"))?;
        match key.trim() {
            "sym" => set_once(&mut symbol, value.trim().to_string(), "sym")?,
            "qty" => {
                let parsed = parse_i64(value, "position qty")?;
                if quantity.replace(parsed).is_some() {
                    return Err("position 'qty' given more than once".to_string());
                }
            }
            "basis" => {
                let parsed = parse_i128(value, "position basis")?;
                if basis.replace(parsed).is_some() {
                    return Err("position 'basis' given more than once".to_string());
                }
            }
            "status" => set_once(&mut status, value.trim().to_string(), "status")?,
            other => return Err(format!("unknown position field '{other}'")),
        }
    }

    let quantity = quantity.ok_or("position 'qty' is required")?;
    let basis = basis.ok_or("position 'basis' is required")?;
    let symbol = symbol.unwrap_or_else(|| action_symbol.to_string());
    let build = match status.as_deref() {
        None | Some("ACTIVE") => LivePosition::new(symbol, quantity, basis),
        Some("DELISTED") => LivePosition::delisted(symbol, quantity, basis),
        Some(other) => {
            return Err(format!(
                "position status '{other}' must be ACTIVE or DELISTED"
            ))
        }
    };
    build.map_err(|err| format!("invalid position: {}", err.reason))
}

// --------------------------------------------------------------------------- //
// JSON emission (hand-rolled; std-only)
// --------------------------------------------------------------------------- //

fn outcome_json(outcome: &PositionCorpActionOutcome) -> String {
    match outcome {
        PositionCorpActionOutcome::Adjusted {
            symbol,
            before,
            after,
        } => format!(
            "{{\"symbol\":\"{}\",\"result\":\"ADJUSTED\",\"before\":{},\"after\":{},\"callback\":{}}}",
            json_escape(symbol),
            position_json(before),
            position_json(after),
            callback_json(outcome),
        ),
        PositionCorpActionOutcome::Remapped { from_symbol, after } => format!(
            "{{\"symbol\":\"{}\",\"result\":\"REMAPPED\",\"successor\":\"{}\",\"after\":{},\
             \"callback\":{}}}",
            json_escape(from_symbol),
            json_escape(after.symbol()),
            position_json(after),
            callback_json(outcome),
        ),
        PositionCorpActionOutcome::Delisted { position } => {
            let alert = outcome.alert().expect("a Delisted outcome yields an alert");
            format!(
                "{{\"symbol\":\"{}\",\"result\":\"DELISTED\",\"position\":{},\
                 \"notification\":{},\"callback\":{}}}",
                json_escape(position.symbol()),
                position_json(position),
                notification_json(&alert.operator_summary()),
                callback_json(outcome),
            )
        }
        PositionCorpActionOutcome::RequiresManualReview { symbol, reason } => {
            let alert = outcome
                .alert()
                .expect("a RequiresManualReview outcome yields an alert");
            format!(
                "{{\"symbol\":\"{}\",\"result\":\"MANUAL_REVIEW\",\"reason\":\"{}\",\
                 \"notification\":{}}}",
                json_escape(symbol),
                reason.as_str(),
                notification_json(&alert.operator_summary()),
            )
        }
        PositionCorpActionOutcome::Unaffected { symbol } => format!(
            "{{\"symbol\":\"{}\",\"result\":\"UNAFFECTED\"}}",
            json_escape(symbol),
        ),
    }
}

fn position_json(position: &LivePosition) -> String {
    format!(
        "{{\"symbol\":\"{}\",\"quantity\":{},\"cost_basis_minor\":{},\"avg_cost_minor\":{}}}",
        json_escape(position.symbol()),
        position.quantity(),
        position.cost_basis_minor(),
        opt_minor(position.average_cost_minor()),
    )
}

fn notification_json(summary: &str) -> String {
    format!(
        "{{\"trigger_kind\":\"CRITICAL_FAILURE\",\"severity\":\"CRITICAL\",\"summary\":\"{}\"}}",
        json_escape(summary),
    )
}

fn callback_json(outcome: &PositionCorpActionOutcome) -> String {
    match outcome.strategy_callback() {
        Some(event) => change_event_json(&event),
        None => "null".to_string(),
    }
}

fn change_event_json(event: &PositionChangeEvent) -> String {
    format!(
        "{{\"kind\":\"{}\",\"symbol\":\"{}\",\"previous_symbol\":\"{}\",\"new_quantity\":{},\
         \"new_cost_basis_minor\":{},\"reason\":\"{}\"}}",
        event.kind.as_str(),
        json_escape(&event.symbol),
        json_escape(&event.previous_symbol),
        event.new_quantity,
        event.new_cost_basis_minor,
        json_escape(&event.summary()),
    )
}

fn opt_minor(value: Option<i128>) -> String {
    match value {
        Some(minor) => minor.to_string(),
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
