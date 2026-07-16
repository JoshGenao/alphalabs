//! SRS-MD-003 heartbeat-freshness operator CLI.
//!
//! Drives the [`HeartbeatFreshnessMonitor`] (time-based staleness, SyRS
//! SYS-39 / NFR-P5) COMPOSED with SRS-MD-007's [`SequenceGapDetector`]
//! (gap-based staleness) over a deterministic fixture observation script —
//! exactly the verification context the feature steps permit ("CLI/API
//! workflows with fixture market data, provider mocks, file reads, and
//! persisted output inspection"). Every timestamp is supplied by the script
//! (or by the caller appending an `evaluate <now_ns>` directive with its own
//! clock reading), so the binary performs NO wall-clock I/O and re-runs
//! byte-identically: the deferred live feed loop
//! (`heartbeat_freshness_contract.deferred[]`) is the only component that
//! ever samples a real clock.
//!
//! Usage: `md003_heartbeat_cli <observations-file>` (or `-` for stdin).
//!
//! Directive grammar (one per line; blank lines and `#` comments ignored):
//!
//! ```text
//! watch-security <symbol> <equity|option>
//! watch-broker
//! tick <symbol> <equity|option> <tick_seq> <observed_at_ns>
//! broker-heartbeat <observed_at_ns>
//! resync <symbol> <equity|option>
//! evaluate <now_ns>
//! ```
//!
//! Output (stdout), all space-separated `key=value` lines:
//!
//! * `event kind=SEQUENCE_GAP ...` — an MD-007 gap detected by a `tick`;
//! * `event kind=HEARTBEAT_STALE|HEARTBEAT_RECOVERED ...` — an MD-003
//!   freshness transition detected by an `evaluate`;
//! * `status feed=market_data|broker ...` — one row per watched feed per
//!   `evaluate`: the continuously-displayed snapshot, carrying `time_stale`
//!   (MD-003), `gap_stale` (MD-007), and the merged `stale` verdict.
//!
//! The parser fails CLOSED: an unknown directive, wrong arity, or
//! unparseable number aborts with a structured message on stderr and a
//! non-zero exit — a monitoring script that cannot be understood must never
//! be half-applied.

use std::env;
use std::fs;
use std::io::Read;
use std::process::ExitCode;

use atp_market_data::{
    HeartbeatEventSink, HeartbeatFreshnessMonitor, HeartbeatPublishError, HeartbeatStatus,
    SequenceGapDetector, SequenceGapEventSink, SequenceGapPublishError,
};
use atp_types::{
    AssetClass, HeartbeatFeed, HeartbeatStalenessEvent, HeartbeatTransition, MarketDataTick,
    SecurityKey, SequenceGapEvent,
};

/// Prints MD-007 gap events as they are detected. Publication here is a
/// stdout write, which cannot meaningfully fail for this operator tool, so
/// `record` always returns `Ok` — the fallibility contract is exercised by
/// the library test suite's failing-sink cases.
struct StdoutGapSink;

impl SequenceGapEventSink for StdoutGapSink {
    fn record(&self, event: SequenceGapEvent) -> Result<(), SequenceGapPublishError> {
        println!(
            "event kind=SEQUENCE_GAP symbol={} asset_class={} expected_sequence={} \
             observed_sequence={} observed_at_ns={}",
            event.symbol,
            asset_class_str(event.asset_class),
            event.expected_sequence,
            event.observed_sequence,
            event.observed_at_ns
        );
        Ok(())
    }
}

/// Prints MD-003 heartbeat transitions as they are detected.
struct StdoutHeartbeatSink;

impl HeartbeatEventSink for StdoutHeartbeatSink {
    fn record(&self, event: HeartbeatStalenessEvent) -> Result<(), HeartbeatPublishError> {
        println!(
            "event kind={} {} staleness_ms={} last_observation_ns={} evaluated_at_ns={} \
             threshold_ms={}",
            match event.transition {
                HeartbeatTransition::BecameStale => "HEARTBEAT_STALE",
                HeartbeatTransition::Recovered => "HEARTBEAT_RECOVERED",
            },
            feed_kv(&event.feed),
            opt_u64(event.staleness_ms),
            opt_i64(event.last_observation_ns),
            event.evaluated_at_ns,
            event.threshold_ms
        );
        Ok(())
    }
}

fn asset_class_str(class: AssetClass) -> &'static str {
    match class {
        AssetClass::Equity => "equity",
        AssetClass::Option => "option",
    }
}

fn feed_kv(feed: &HeartbeatFeed) -> String {
    match feed {
        HeartbeatFeed::MarketData {
            symbol,
            asset_class,
        } => format!(
            "feed=market_data symbol={} asset_class={}",
            symbol,
            asset_class_str(*asset_class)
        ),
        HeartbeatFeed::Broker => "feed=broker".to_string(),
    }
}

fn opt_u64(value: Option<u64>) -> String {
    value.map_or_else(|| "none".to_string(), |v| v.to_string())
}

fn opt_i64(value: Option<i64>) -> String {
    value.map_or_else(|| "none".to_string(), |v| v.to_string())
}

struct DirectiveError {
    line_no: usize,
    line: String,
    reason: String,
}

impl DirectiveError {
    fn new(line_no: usize, line: &str, reason: impl Into<String>) -> Self {
        Self {
            line_no,
            line: line.to_string(),
            reason: reason.into(),
        }
    }
}

fn parse_asset_class(
    token: &str,
    line_no: usize,
    line: &str,
) -> Result<AssetClass, DirectiveError> {
    match token {
        "equity" => Ok(AssetClass::Equity),
        "option" => Ok(AssetClass::Option),
        other => Err(DirectiveError::new(
            line_no,
            line,
            format!("unknown asset class {other:?} (expected equity|option)"),
        )),
    }
}

fn parse_u64(token: &str, what: &str, line_no: usize, line: &str) -> Result<u64, DirectiveError> {
    token
        .parse::<u64>()
        .map_err(|_| DirectiveError::new(line_no, line, format!("unparseable {what} {token:?}")))
}

fn parse_i64(token: &str, what: &str, line_no: usize, line: &str) -> Result<i64, DirectiveError> {
    token
        .parse::<i64>()
        .map_err(|_| DirectiveError::new(line_no, line, format!("unparseable {what} {token:?}")))
}

fn security_key(
    symbol: &str,
    class: AssetClass,
    line_no: usize,
    line: &str,
) -> Result<SecurityKey, DirectiveError> {
    SecurityKey::new(symbol, class)
        .map_err(|err| DirectiveError::new(line_no, line, format!("invalid security: {err}")))
}

fn print_status(status: &HeartbeatStatus, gaps: &SequenceGapDetector, evaluated_at_ns: i64) {
    let (gap_stale, feed_fields) = match &status.feed {
        HeartbeatFeed::MarketData {
            symbol,
            asset_class,
        } => {
            let gap_stale = SecurityKey::new(symbol, *asset_class)
                .map(|key| gaps.is_stale(&key))
                // An uncanonicalizable line cannot be proven gap-free: fail
                // closed (the time side already reports Stale for it too).
                .unwrap_or(true);
            (gap_stale, feed_kv(&status.feed))
        }
        HeartbeatFeed::Broker => (false, feed_kv(&status.feed)),
    };
    let time_stale = status.freshness.is_stale();
    println!(
        "status {} last_observation_ns={} staleness_ms={} never_observed={} time_stale={} \
         gap_stale={} stale={} threshold_ms={} evaluated_at_ns={}",
        feed_fields,
        opt_i64(status.last_observation_ns),
        opt_u64(status.staleness_ms),
        status.last_observation_ns.is_none(),
        time_stale,
        gap_stale,
        time_stale || gap_stale,
        atp_types::HEARTBEAT_STALENESS_THRESHOLD_MS,
        evaluated_at_ns
    );
}

fn run(script: &str) -> Result<(), DirectiveError> {
    let mut monitor = HeartbeatFreshnessMonitor::new();
    let mut gaps = SequenceGapDetector::new();
    let gap_sink = StdoutGapSink;
    let heartbeat_sink = StdoutHeartbeatSink;

    for (index, raw_line) in script.lines().enumerate() {
        let line_no = index + 1;
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let tokens: Vec<&str> = line.split_whitespace().collect();
        match tokens.as_slice() {
            ["watch-security", symbol, class] => {
                let class = parse_asset_class(class, line_no, line)?;
                let key = security_key(symbol, class, line_no, line)?;
                monitor.watch_security(key);
            }
            ["watch-broker"] => monitor.watch_broker(),
            ["tick", symbol, class, seq, at_ns] => {
                let class = parse_asset_class(class, line_no, line)?;
                let tick = MarketDataTick {
                    symbol: (*symbol).to_string(),
                    asset_class: class,
                    tick_seq: parse_u64(seq, "tick_seq", line_no, line)?,
                };
                let observed_at_ns = parse_i64(at_ns, "observed_at_ns", line_no, line)?;
                monitor.observe_tick(&tick, observed_at_ns).map_err(|err| {
                    DirectiveError::new(line_no, line, format!("tick rejected: {err}"))
                })?;
                gaps.observe_tick(&tick, observed_at_ns, &gap_sink)
                    .map_err(|err| {
                        DirectiveError::new(line_no, line, format!("tick rejected: {err}"))
                    })?;
            }
            ["broker-heartbeat", at_ns] => {
                monitor.observe_broker_heartbeat(parse_i64(
                    at_ns,
                    "observed_at_ns",
                    line_no,
                    line,
                )?);
            }
            ["resync", symbol, class] => {
                let class = parse_asset_class(class, line_no, line)?;
                let key = security_key(symbol, class, line_no, line)?;
                gaps.acknowledge_resync(&key);
            }
            ["evaluate", now_ns] => {
                let now_ns = parse_i64(now_ns, "now_ns", line_no, line)?;
                for status in monitor.evaluate(now_ns, &heartbeat_sink) {
                    print_status(&status, &gaps, now_ns);
                }
            }
            _ => {
                return Err(DirectiveError::new(
                    line_no,
                    line,
                    "unknown directive or wrong arity (expected watch-security|watch-broker|\
                     tick|broker-heartbeat|resync|evaluate)",
                ));
            }
        }
    }
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    let [_, source] = args.as_slice() else {
        eprintln!("md003_heartbeat_cli: usage: md003_heartbeat_cli <observations-file|->");
        return ExitCode::from(2);
    };

    let script = if source == "-" {
        let mut buffer = String::new();
        if let Err(err) = std::io::stdin().read_to_string(&mut buffer) {
            eprintln!("md003_heartbeat_cli: failed to read stdin: {err}");
            return ExitCode::from(2);
        }
        buffer
    } else {
        match fs::read_to_string(source) {
            Ok(contents) => contents,
            Err(err) => {
                eprintln!("md003_heartbeat_cli: failed to read {source:?}: {err}");
                return ExitCode::from(2);
            }
        }
    };

    match run(&script) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!(
                "md003_heartbeat_cli: line {}: {}: {:?}",
                err.line_no, err.reason, err.line
            );
            ExitCode::from(2)
        }
    }
}
