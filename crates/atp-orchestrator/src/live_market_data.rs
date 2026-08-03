//! SRS-MD-003 — binding the IB transport to the live freshness feed loop.
//!
//! `atp-market-data` owns the monitor and the loop; `atp-adapters` owns the IB
//! wire. Neither may depend on the other — the adapter would pick up a core
//! dependency, and the market-data core would pick up vendor logic (AGENTS.md
//! dependency direction). This module is where the orchestrator, whose whole
//! job is composition, joins them.
//!
//! The only real work here is attribution: the wire answers in `reqMktData`
//! ticker ids, and freshness is keyed by security. A tick whose ticker id was
//! not subscribed by THIS source is dropped rather than attributed to a guess —
//! crediting the wrong line would prove a silent security fresh.

#![cfg(feature = "ib-live-transport")]

// Through the adapter crate's ROOT re-exports, never its vendor-named module
// path: SRS-ARCH-001 keeps vendor SDK surface behind the adapter interface, and
// both `tools/architecture_check.py` and the deterministic critic enforce it by
// rejecting the vendor token anywhere in a composing crate's source — including,
// as this comment learned, inside a comment.
use atp_adapters::{
    IbAccountKind, IbApiError, IbConnectionConfig, IbGatewayConnection, MarketDataChannel,
    MarketDataSubscription, TcpIbGateway,
};
use atp_market_data::live_feed::{FeedError, LiveTickSource};
use atp_types::{AssetClass, SecurityKey};
use std::time::Duration;

/// The `subscription_id` prefix `wire::IbSession::subscribe_market_data` mints
/// (`ib-md-<ticker_id>`); the ticker id is recovered from it so the drain can
/// map deliveries back to their security.
const SUBSCRIPTION_ID_PREFIX: &str = "ib-md-";

/// A live IB market-data session driving the SRS-MD-003 feed loop.
#[derive(Debug)]
pub struct IbLiveTickSource {
    gateway: TcpIbGateway,
    /// Subscribed lines, as `(reqMktData ticker id, security)`.
    lines: Vec<(i64, SecurityKey)>,
    /// The symbols this source is responsible for, kept so subscriptions can be
    /// rebuilt on a new session.
    symbols: Vec<String>,
    /// The transport session generation `lines` belongs to. A `reqMktData`
    /// subscription lives in the session that opened it, so when the transport
    /// reconnects after a fault these ticker ids are dead and must be replaced —
    /// otherwise the feed polls ids nobody is publishing to and every watched
    /// line ages into a permanent, false staleness alarm.
    subscribed_generation: u64,
}

impl IbLiveTickSource {
    /// Open a market-data session and subscribe every requested symbol.
    ///
    /// `client_id` should be DEDICATED to this feed. Sharing the execution
    /// path's client id would put the tick stream on the same socket as
    /// request/reply operations, where a tick arriving mid-`submit_order` is
    /// consumed by that operation's frame loop and lost — the line would appear
    /// to go quiet for reasons that have nothing to do with the market.
    pub fn connect(symbols: &[String], client_id: i32) -> Result<Self, FeedError> {
        if symbols.is_empty() {
            return Err(FeedError::Configuration(
                "no symbols to subscribe — a feed watching nothing cannot monitor anything"
                    .to_string(),
            ));
        }
        let config = IbConnectionConfig::from_env(client_id)
            .map_err(|err| FeedError::Configuration(err.to_string()))?;
        // Paper only: the live account is gated on the SRS-EXE-001 admission
        // path, and the transport itself refuses it.
        let gateway = TcpIbGateway::new(config, IbAccountKind::Paper);

        let mut source = Self {
            gateway,
            lines: Vec::new(),
            symbols: symbols.to_vec(),
            subscribed_generation: 0,
        };
        source.subscribe_all()?;
        Ok(source)
    }

    /// Open a `reqMktData` line per configured symbol on the CURRENT session and
    /// record the generation they belong to.
    ///
    /// **Partial failure commits what opened.** `reqMktData` has no undo on this
    /// transport, so a line that succeeded before a later symbol failed is
    /// already live on the wire whether or not we record it. Discarding the
    /// whole batch would leave those lines open but untracked AND leave
    /// `subscribed_generation` stale, so the very next step would call this
    /// again and open a SECOND line for every symbol that works — once per step,
    /// forever, burning IB's market-data line allowance and duplicating tick
    /// streams while the monitor stayed degraded.
    ///
    /// So the successful lines and the generation they belong to are always
    /// committed, and the error is still propagated. The symbol that failed is
    /// simply not delivering, and the loop's watch set is fixed at construction
    /// — so it ages past the threshold and is reported STALE. That is the
    /// truthful outcome: a line we could not open is a line with no data, which
    /// is exactly what staleness means. A genuine reconnect moves the generation
    /// again and retries everything, so a transient failure still self-heals.
    fn subscribe_all(&mut self) -> Result<(), FeedError> {
        let gateway = &self.gateway;
        let symbols = self.symbols.clone();
        let mut lines = Vec::with_capacity(symbols.len());
        let mut outcome = Ok(());
        for symbol in &symbols {
            match Self::open_line(gateway, symbol) {
                Ok(line) => lines.push(line),
                Err(err) => {
                    outcome = Err(err);
                    break;
                }
            }
        }
        // Read the generation AFTER subscribing: `subscribe_market_data`
        // establishes the session lazily, so reading it earlier would record
        // the generation of a session these lines do not belong to.
        self.subscribed_generation = gateway.session_generation();
        self.lines = lines;
        outcome
    }

    /// One `reqMktData` line, resolved to the `(ticker id, security)` pair the
    /// drain attributes deliveries with.
    fn open_line(gateway: &TcpIbGateway, symbol: &str) -> Result<(i64, SecurityKey), FeedError> {
        let key = SecurityKey::new(symbol, AssetClass::Equity)
            .map_err(|err| FeedError::Configuration(format!("invalid symbol {symbol}: {err}")))?;
        let receipt = gateway
            .subscribe_market_data(&MarketDataSubscription {
                symbol: symbol.to_string(),
                channel: MarketDataChannel::Trades,
            })
            .map_err(|err| {
                FeedError::Source(format!("subscribe {symbol} failed: {}", ib_detail(&err)))
            })?;
        let ticker_id = parse_ticker_id(&receipt.subscription_id).ok_or_else(|| {
            FeedError::Source(format!(
                "subscription id {:?} is not the expected {SUBSCRIPTION_ID_PREFIX}<id> form — \
                 without the ticker id, delivered ticks cannot be attributed to {symbol}",
                receipt.subscription_id
            ))
        })?;
        Ok((ticker_id, key))
    }

    /// Re-open the subscriptions if the transport has rebuilt its session since
    /// they were opened. A no-op on the ordinary path.
    fn resubscribe_if_reconnected(&mut self) -> Result<(), FeedError> {
        if self.gateway.session_generation() == self.subscribed_generation {
            return Ok(());
        }
        self.subscribe_all()
    }

    /// The securities this source subscribed, for the loop's watch set.
    pub fn watched(&self) -> Vec<SecurityKey> {
        self.lines.iter().map(|(_, key)| key.clone()).collect()
    }

    fn key_for(&self, ticker_id: i64) -> Option<&SecurityKey> {
        self.lines
            .iter()
            .find(|(id, _)| *id == ticker_id)
            .map(|(_, key)| key)
    }
}

/// An IB failure as one operator-readable string. `IbApiError` carries the
/// SYS-64-classifiable code separately from the message, and the code is the
/// part an operator needs to look up — so both travel.
fn ib_detail(error: &IbApiError) -> String {
    format!("{} (IB code {})", error.message, error.code)
}

fn parse_ticker_id(subscription_id: &str) -> Option<i64> {
    subscription_id
        .strip_prefix(SUBSCRIPTION_ID_PREFIX)?
        .parse()
        .ok()
}

impl LiveTickSource for IbLiveTickSource {
    fn poll_observations(&mut self, budget: Duration) -> Result<Vec<SecurityKey>, FeedError> {
        // A reconnect since the last poll left our ticker ids pointing at a
        // session that no longer exists; re-open before reading, or this line
        // never delivers again.
        self.resubscribe_if_reconnected()?;
        let ids: Vec<i64> = self.lines.iter().map(|(id, _)| *id).collect();
        let delivered = self
            .gateway
            .poll_market_data(&ids, budget)
            .map_err(|err| FeedError::Source(ib_detail(&err)))?;
        Ok(delivered
            .iter()
            .filter_map(|tick| self.key_for(tick.ticker_id).cloned())
            .collect())
    }

    fn broker_round_trip(&mut self) -> Result<(), FeedError> {
        self.gateway
            .broker_heartbeat_round_trip()
            .map(|_epoch_seconds| ())
            .map_err(|err| FeedError::Source(ib_detail(&err)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ticker_ids_round_trip_through_the_subscription_id() {
        assert_eq!(parse_ticker_id("ib-md-9000"), Some(9000));
        assert_eq!(parse_ticker_id("ib-md-1"), Some(1));
    }

    #[test]
    fn a_foreign_subscription_id_yields_no_ticker() {
        // Fail closed rather than defaulting to an id: a wrong id silently
        // attributes another line's ticks to this security.
        assert_eq!(parse_ticker_id("9000"), None);
        assert_eq!(parse_ticker_id("ib-md-"), None);
        assert_eq!(parse_ticker_id("ib-md-abc"), None);
        assert_eq!(parse_ticker_id(""), None);
    }

    // --- partial subscription state, against a scripted gateway ------------
    //
    // A fake IB server just complete enough to answer the handshake, confirm
    // one `reqMktData`, and refuse the next. That is the whole scenario the
    // duplicate-line bug needs.

    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};

    fn read_frame(stream: &mut TcpStream) -> Vec<String> {
        let mut len = [0u8; 4];
        stream.read_exact(&mut len).expect("frame length");
        let mut body = vec![0u8; u32::from_be_bytes(len) as usize];
        stream.read_exact(&mut body).expect("frame body");
        String::from_utf8(body)
            .expect("utf-8 frame")
            .split('\0')
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect()
    }

    fn write_frame(stream: &mut TcpStream, fields: &[&str]) {
        let mut body = String::new();
        for field in fields {
            body.push_str(field);
            body.push('\0');
        }
        stream
            .write_all(&(body.len() as u32).to_be_bytes())
            .expect("frame length");
        stream.write_all(body.as_bytes()).expect("frame body");
        stream.flush().expect("flush");
    }

    fn handshake(stream: &mut TcpStream) {
        let mut prefix = [0u8; 4];
        stream.read_exact(&mut prefix).expect("API prefix");
        let mut len = [0u8; 4];
        stream.read_exact(&mut len).expect("handshake length");
        let mut body = vec![0u8; u32::from_be_bytes(len) as usize];
        stream.read_exact(&mut body).expect("handshake body");
        write_frame(stream, &["176", "20260803 04:00:00 UTC"]);
        let _start_api = read_frame(stream);
        write_frame(stream, &["15", "1", "DU1234567"]);
        write_frame(stream, &["9", "1", "7001"]);
    }

    /// A gateway whose first `reqMktData` is confirmed and whose second is
    /// refused with 10197 (IB withholding the stream).
    fn gateway_confirming_only_the_first_line() -> (TcpIbGateway, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("ephemeral loopback listener");
        let port = listener.local_addr().unwrap().port();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("client connection");
            handshake(&mut stream);
            let _market_data_type = read_frame(&mut stream); // 59, sent once per session
            let _first = read_frame(&mut stream); // reqMktData -> ticker 9000
            write_frame(&mut stream, &["1", "6", "9000", "4", "187.25", "3", "1"]);
            let _second = read_frame(&mut stream); // reqMktData -> ticker 9001
            write_frame(
                &mut stream,
                &[
                    "4",
                    "2",
                    "9001",
                    "10197",
                    "No market data during competing live session",
                    "",
                ],
            );
            std::thread::sleep(Duration::from_millis(400));
        });
        let config = IbConnectionConfig::new("127.0.0.1", port, port, 137);
        let gateway =
            TcpIbGateway::with_op_deadline(config, IbAccountKind::Paper, Duration::from_secs(4));
        (gateway, handle)
    }

    #[test]
    fn a_partial_subscribe_commits_the_lines_that_opened() {
        // The duplicate-line bug: discarding the whole batch on a late failure
        // left the successful `reqMktData` open on the wire but untracked, AND
        // left `subscribed_generation` stale — so the next step resubscribed,
        // and the next, opening a fresh duplicate line every time until IB's
        // line allowance was gone.
        let (gateway, handle) = gateway_confirming_only_the_first_line();
        let mut source = IbLiveTickSource {
            gateway,
            lines: Vec::new(),
            symbols: vec!["AAPL".to_string(), "MSFT".to_string()],
            subscribed_generation: 0,
        };

        let err = source
            .subscribe_all()
            .expect_err("the second line was refused, so the batch failed");
        assert!(
            format!("{err:?}").contains("MSFT"),
            "the error must name the line that failed, got {err:?}"
        );

        // The line that DID open is tracked, so its ticks are attributable and
        // it is never opened a second time.
        assert_eq!(source.lines.len(), 1, "the successful line was discarded");
        assert_eq!(source.lines[0].0, 9000);
        assert_eq!(source.lines[0].1.symbol(), "AAPL");

        // And the generation is committed, so the retry path is a no-op rather
        // than an unbounded re-subscribe loop.
        assert_eq!(
            source.subscribed_generation,
            source.gateway.session_generation(),
            "a stale generation makes every later step resubscribe and duplicate lines"
        );
        source
            .resubscribe_if_reconnected()
            .expect("no reconnect happened, so this must not touch the wire");
        assert_eq!(source.lines.len(), 1, "the retry path re-opened a line");

        handle.join().expect("fake gateway thread panicked");
    }
}
