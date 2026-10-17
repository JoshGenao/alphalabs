//! The live execution host's socket protocol (SRS-EXE-001).
//!
//! One request line, one reply line, both terminated by `\n`:
//!
//! ```text
//! ATP-LIVE-HOST/1<TAB>submit<TAB>correlation_id=c-1<TAB>symbol=AAPL<TAB>side=BUY<TAB>
//!     quantity=1<TAB>asset_class=EQUITY<TAB>order_type=LIMIT<TAB>limit_price_minor=19000
//! ATP-LIVE-HOST/1<TAB>ack<TAB>correlation_id=c-1<TAB>broker_order_id=IB-1<TAB>durable=true<TAB>tier=FIXTURE
//! ```
//!
//! Tab-separated `key=value` fields, because the workspace carries no JSON library and
//! the repo's Python-to-Rust boundary already speaks `key:value` lines. The request
//! carries NO strategy id: the host knows which strategy is speaking from the socket
//! the connection arrived on (one socket per strategy), so a client cannot submit as
//! another strategy by naming it.
//!
//! The parser fails closed. A missing, repeated, unknown, empty, or control-character
//! field refuses the whole frame, and a price field that the order type does not take
//! is refused rather than ignored, so a typo can never change what reaches IB.
//!
//! Three reply outcomes, kept apart because they mean different things to a strategy:
//!
//! * `ack` - the broker accepted the order; a live order EXISTS. `durable=false` means
//!   the acknowledgement could not be written to the outbox: the order is live, and a
//!   retry would duplicate it.
//! * `reject` - a structured order error (SRS-ERR-001 envelope). No live order exists.
//!   `durable=false` means the REJECTED record could not be written.
//! * `refused` - no live order exists, and there is no structured order error to
//!   report: the frame was malformed, the designation snapshot or its lock was
//!   unavailable, the outbox write-ahead failed, or a rejection could not be
//!   recorded. `error_type` says which.

use atp_types::{AssetClass, OrderSide, OrderType};
use std::fmt;

/// The protocol name and version, the first field of every frame.
pub const PROTOCOL_MAGIC: &str = "ATP-LIVE-HOST/1";

/// The longest frame (excluding the `\n`) either side accepts. An order fits in a
/// few hundred bytes; the bound stops a client from growing the host's buffer.
pub const MAX_FRAME_BYTES: usize = 4096;

/// A parsed `submit` request: everything an [`atp_types::OrderSubmission`] needs
/// except the strategy id, which the socket supplies.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubmitRequest {
    pub correlation_id: String,
    pub symbol: String,
    pub side: OrderSide,
    pub quantity: i64,
    pub asset_class: AssetClass,
    pub order_type: OrderType,
}

/// Why a frame was refused. The message names the field so a strategy author can
/// fix the call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolError {
    pub reason: String,
}

impl ProtocolError {
    fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "malformed {PROTOCOL_MAGIC} frame: {}",
            self.reason
        )
    }
}

impl std::error::Error for ProtocolError {}

/// The host's reply to one request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reply {
    /// The broker accepted the order. A live order exists.
    Ack {
        correlation_id: String,
        broker_order_id: String,
        durable: bool,
        tier: &'static str,
    },
    /// A structured order error. No live order exists.
    Reject {
        correlation_id: String,
        category: String,
        error_type: String,
        message: String,
        durable: bool,
        tier: &'static str,
    },
    /// No live order exists and there is no structured order error to report.
    Refused {
        correlation_id: Option<String>,
        error_type: String,
        message: String,
        tier: &'static str,
    },
}

/// Field keys a `submit` frame may carry. Every other key is refused.
const SUBMIT_KEYS: [&str; 8] = [
    "correlation_id",
    "symbol",
    "side",
    "quantity",
    "asset_class",
    "order_type",
    "limit_price_minor",
    "stop_price_minor",
];

/// Parse one request frame (without its trailing `\n`).
pub fn parse_request(frame: &str) -> Result<SubmitRequest, ProtocolError> {
    if frame.len() > MAX_FRAME_BYTES {
        return Err(ProtocolError::new(format!(
            "frame is {} bytes; the limit is {MAX_FRAME_BYTES}",
            frame.len()
        )));
    }
    let mut parts = frame.split('\t');
    match parts.next() {
        Some(magic) if magic == PROTOCOL_MAGIC => {}
        other => {
            return Err(ProtocolError::new(format!(
                "expected `{PROTOCOL_MAGIC}` as the first field, got {:?}",
                other.unwrap_or("")
            )))
        }
    }
    match parts.next() {
        Some("submit") => {}
        other => {
            return Err(ProtocolError::new(format!(
                "unknown request kind {:?}; the only request is `submit`",
                other.unwrap_or("")
            )))
        }
    }
    let fields = parse_fields(parts, &SUBMIT_KEYS)?;
    let required = |key: &str| -> Result<&str, ProtocolError> {
        fields
            .iter()
            .find(|(k, _)| *k == key)
            .map(|(_, v)| *v)
            .ok_or_else(|| ProtocolError::new(format!("missing required field `{key}`")))
    };
    let optional = |key: &str| fields.iter().find(|(k, _)| *k == key).map(|(_, v)| *v);

    let side = match required("side")? {
        "BUY" => OrderSide::Buy,
        "SELL" => OrderSide::Sell,
        other => {
            return Err(ProtocolError::new(format!(
                "side {other:?} is not one of {:?}",
                OrderSide::ALL_WIRE
            )))
        }
    };
    let asset_class = match required("asset_class")? {
        "EQUITY" => AssetClass::Equity,
        "OPTION" => AssetClass::Option,
        other => {
            return Err(ProtocolError::new(format!(
                "asset_class {other:?} is not EQUITY or OPTION"
            )))
        }
    };
    let quantity = parse_i64("quantity", required("quantity")?)?;
    let limit = optional("limit_price_minor")
        .map(|value| parse_i64("limit_price_minor", value))
        .transpose()?;
    let stop = optional("stop_price_minor")
        .map(|value| parse_i64("stop_price_minor", value))
        .transpose()?;
    let order_type = match (required("order_type")?, limit, stop) {
        ("MARKET", None, None) => OrderType::Market,
        ("LIMIT", Some(limit_price_minor), None) => OrderType::Limit { limit_price_minor },
        ("STOP", None, Some(stop_price_minor)) => OrderType::Stop { stop_price_minor },
        ("STOP_LIMIT", Some(limit_price_minor), Some(stop_price_minor)) => OrderType::StopLimit {
            stop_price_minor,
            limit_price_minor,
        },
        (kind, limit, stop) if OrderType::ALL_WIRE.contains(&kind) => {
            return Err(ProtocolError::new(format!(
                "order_type {kind} does not take the price fields it was given \
                 (limit_price_minor present: {}, stop_price_minor present: {})",
                limit.is_some(),
                stop.is_some()
            )))
        }
        (kind, _, _) => {
            return Err(ProtocolError::new(format!(
                "order_type {kind:?} is not one of {:?}",
                OrderType::ALL_WIRE
            )))
        }
    };
    Ok(SubmitRequest {
        correlation_id: required("correlation_id")?.to_string(),
        symbol: required("symbol")?.to_string(),
        side,
        quantity,
        asset_class,
        order_type,
    })
}

/// Split `key=value` fields, refusing unknown keys, repeats, and empty values.
fn parse_fields<'a>(
    parts: impl Iterator<Item = &'a str>,
    allowed: &[&str],
) -> Result<Vec<(&'a str, &'a str)>, ProtocolError> {
    let mut fields: Vec<(&str, &str)> = Vec::new();
    for part in parts {
        let Some((key, value)) = part.split_once('=') else {
            return Err(ProtocolError::new(format!(
                "field {part:?} is not `key=value`"
            )));
        };
        if !allowed.contains(&key) {
            return Err(ProtocolError::new(format!("unknown field `{key}`")));
        }
        if fields.iter().any(|(seen, _)| *seen == key) {
            return Err(ProtocolError::new(format!("field `{key}` appears twice")));
        }
        check_value(key, value)?;
        fields.push((key, value));
    }
    Ok(fields)
}

fn check_value(key: &str, value: &str) -> Result<(), ProtocolError> {
    if value.is_empty() {
        return Err(ProtocolError::new(format!("field `{key}` is empty")));
    }
    if let Some(bad) = value.chars().find(|c| c.is_control()) {
        return Err(ProtocolError::new(format!(
            "field `{key}` contains the control character U+{:04X}",
            bad as u32
        )));
    }
    Ok(())
}

fn parse_i64(key: &str, value: &str) -> Result<i64, ProtocolError> {
    // `i64::from_str` accepts a leading `+`; the wire form is the canonical decimal
    // only, so `+1` and `01` are refused rather than silently normalized.
    let canonical = value
        .parse::<i64>()
        .ok()
        .filter(|parsed| parsed.to_string() == value);
    canonical.ok_or_else(|| {
        ProtocolError::new(format!(
            "field `{key}` value {value:?} is not a canonical integer"
        ))
    })
}

/// Encode a request frame (without the trailing `\n`). Used by the Rust tests and
/// mirrored by the Python client.
pub fn encode_request(request: &SubmitRequest) -> String {
    let mut frame = format!(
        "{PROTOCOL_MAGIC}\tsubmit\tcorrelation_id={}\tsymbol={}\tside={}\tquantity={}\
         \tasset_class={}\torder_type={}",
        request.correlation_id,
        request.symbol,
        request.side.as_str(),
        request.quantity,
        request.asset_class.as_str(),
        request.order_type.as_str(),
    );
    match request.order_type {
        OrderType::Market => {}
        OrderType::Limit { limit_price_minor } => {
            frame.push_str(&format!("\tlimit_price_minor={limit_price_minor}"));
        }
        OrderType::Stop { stop_price_minor } => {
            frame.push_str(&format!("\tstop_price_minor={stop_price_minor}"));
        }
        OrderType::StopLimit {
            stop_price_minor,
            limit_price_minor,
        } => {
            frame.push_str(&format!(
                "\tlimit_price_minor={limit_price_minor}\tstop_price_minor={stop_price_minor}"
            ));
        }
    }
    frame
}

/// Make arbitrary text safe for one field: every control character (which includes
/// the tab and newline that delimit the frame) becomes a space, and an empty value
/// becomes `-` so the field still parses.
pub fn field_text(value: &str) -> String {
    let cleaned: String = value
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect();
    if cleaned.is_empty() {
        "-".to_string()
    } else {
        cleaned
    }
}

/// Encode a reply frame (without the trailing `\n`).
pub fn encode_reply(reply: &Reply) -> String {
    match reply {
        Reply::Ack {
            correlation_id,
            broker_order_id,
            durable,
            tier,
        } => format!(
            "{PROTOCOL_MAGIC}\tack\tcorrelation_id={}\tbroker_order_id={}\tdurable={durable}\
             \ttier={tier}",
            field_text(correlation_id),
            field_text(broker_order_id),
        ),
        Reply::Reject {
            correlation_id,
            category,
            error_type,
            message,
            durable,
            tier,
        } => format!(
            "{PROTOCOL_MAGIC}\treject\tcorrelation_id={}\tcategory={}\terror_type={}\
             \tmessage={}\tdurable={durable}\ttier={tier}",
            field_text(correlation_id),
            field_text(category),
            field_text(error_type),
            field_text(message),
        ),
        Reply::Refused {
            correlation_id,
            error_type,
            message,
            tier,
        } => {
            let mut frame = format!("{PROTOCOL_MAGIC}\trefused");
            if let Some(correlation_id) = correlation_id {
                frame.push_str(&format!("\tcorrelation_id={}", field_text(correlation_id)));
            }
            frame.push_str(&format!(
                "\terror_type={}\tmessage={}\ttier={tier}",
                field_text(error_type),
                field_text(message),
            ));
            frame
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(fields: &str) -> String {
        format!("{PROTOCOL_MAGIC}\tsubmit\t{fields}")
    }

    const MARKET: &str = "correlation_id=c-1\tsymbol=AAPL\tside=BUY\tquantity=1\t\
                          asset_class=EQUITY\torder_type=MARKET";

    #[test]
    fn a_market_order_round_trips() {
        let request = parse_request(&frame(MARKET)).expect("a well-formed frame parses");
        assert_eq!(request.correlation_id, "c-1");
        assert_eq!(request.order_type, OrderType::Market);
        assert_eq!(encode_request(&request), frame(MARKET));
    }

    #[test]
    fn every_order_type_round_trips_through_encode_and_parse() {
        for order_type in [
            OrderType::Market,
            OrderType::Limit {
                limit_price_minor: 19_000,
            },
            OrderType::Stop {
                stop_price_minor: 18_000,
            },
            OrderType::StopLimit {
                stop_price_minor: 18_000,
                limit_price_minor: 17_900,
            },
        ] {
            let request = SubmitRequest {
                correlation_id: "c-9".into(),
                symbol: "MSFT".into(),
                side: OrderSide::Sell,
                quantity: 3,
                asset_class: AssetClass::Equity,
                order_type,
            };
            assert_eq!(parse_request(&encode_request(&request)), Ok(request));
        }
    }

    #[test]
    fn a_frame_without_the_magic_is_refused() {
        let err = parse_request(&format!("ATP-LIVE-HOST/2\tsubmit\t{MARKET}")).unwrap_err();
        assert!(err.reason.contains("first field"), "{err}");
    }

    #[test]
    fn unknown_repeated_missing_and_empty_fields_are_refused() {
        for (fields, expect) in [
            (
                format!("{MARKET}\tstrategy_id=live-a"),
                "unknown field `strategy_id`",
            ),
            (format!("{MARKET}\tsymbol=MSFT"), "appears twice"),
            (
                MARKET.replace("\tside=BUY", ""),
                "missing required field `side`",
            ),
            (
                MARKET.replace("symbol=AAPL", "symbol="),
                "`symbol` is empty",
            ),
            (
                MARKET.replace("symbol=AAPL", "symbolAAPL"),
                "is not `key=value`",
            ),
        ] {
            let err = parse_request(&frame(&fields)).unwrap_err();
            assert!(err.reason.contains(expect), "{fields:?}: {err}");
        }
    }

    #[test]
    fn a_control_character_in_a_value_is_refused() {
        let err = parse_request(&frame(&MARKET.replace("AAPL", "AA\u{1}PL"))).unwrap_err();
        assert!(err.reason.contains("control character"), "{err}");
    }

    #[test]
    fn a_price_field_the_order_type_does_not_take_is_refused() {
        let err = parse_request(&frame(&format!("{MARKET}\tlimit_price_minor=100"))).unwrap_err();
        assert!(err.reason.contains("does not take"), "{err}");
        let limit_without_price = MARKET.replace("MARKET", "LIMIT");
        assert!(parse_request(&frame(&limit_without_price)).is_err());
    }

    #[test]
    fn non_canonical_integers_are_refused() {
        for quantity in ["+1", "01", "1.0", "one", "99999999999999999999"] {
            let fields = MARKET.replace("quantity=1", &format!("quantity={quantity}"));
            assert!(parse_request(&frame(&fields)).is_err(), "{quantity}");
        }
    }

    #[test]
    fn an_oversize_frame_is_refused_before_it_is_split() {
        let huge = frame(&format!("{MARKET}\tsymbol={}", "A".repeat(MAX_FRAME_BYTES)));
        assert!(parse_request(&huge).unwrap_err().reason.contains("limit"));
    }

    #[test]
    fn reply_text_cannot_break_the_frame() {
        let reply = encode_reply(&Reply::Reject {
            correlation_id: "c-1".into(),
            category: "BROKER_REJECTED".into(),
            error_type: "X".into(),
            message: "line one\nline\ttwo".into(),
            durable: true,
            tier: "FIXTURE",
        });
        assert!(!reply.contains('\n'));
        assert_eq!(reply.split('\t').count(), 8, "{reply}");
        assert!(reply.contains("message=line one line two"));
    }
}
