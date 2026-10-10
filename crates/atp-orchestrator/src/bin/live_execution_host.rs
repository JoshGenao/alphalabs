//! `live_execution_host` — SRS-EXE-001 the one process that routes strategy orders
//! to IB, and only for the designated live strategy.
//!
//! See `atp_orchestrator::live_host` for the per-order sequence. This binary only
//! chooses the transport tier, opens the durable state, and binds the sockets.
//!
//! ```text
//! live_execution_host serve --designation-state <file> --outbox <dir> [--outbox-init]
//!     --socket-dir <dir> --strategy <id> [--strategy <id> ...]
//!     --transport fixture [--fixture-wire-ledger <file>]
//! ```
//!
//! On success it prints one `live-host-ready:true` line (with the tier and the
//! socket paths) and then serves until the process is stopped.
//!
//! # Tiers
//!
//! * `--transport fixture` — the broker port is an in-process gateway that accepts
//!   every order and mints `IB-<n>` ids; connectivity and freshness are fixtures.
//!   Every reply says `tier=FIXTURE`. `--fixture-wire-ledger` appends one line per
//!   order that reached the gateway, which is how a test proves a rejected strategy
//!   never got there (`wire-attempts:0`).
//! * `--transport ib` — REFUSED. Every live order must pass the stale-data gate,
//!   and no production freshness producer exists yet (owner SRS-MD-004). Starting a
//!   live tier with a fixture "always fresh" probe would disable stale-data blocking
//!   on real orders, so the host does not start that tier at all.

use atp_adapters::{
    DataBatch, HistoricalDataRequest, HistoricalQueryResult, IbApiError, IbGatewayConnection,
    MarketDataSubscription, SubscriptionReceipt,
};
use atp_orchestrator::live_host::{server, HostConfig, HostPorts, HostTier, LiveExecutionHost};
use atp_orchestrator::order_routing_wiring::{
    CollectingConnectivitySink, CollectingStaleDataSink, FreshMarketDataFixture,
    HealthyConnectivityFixture, IbBrokerageBridge, RecordingIbGateway,
};
use atp_types::{CompositeOrderSubmission, OrderReceipt, OrderSubmission};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::{Arc, Mutex};

const USAGE: &str = "\
live_execution_host — SRS-EXE-001 route orders to IB only for the designated live strategy

USAGE:
    live_execution_host serve [FLAGS]

FLAGS:
    --designation-state <file>     the shared durable live-designation snapshot (required)
    --outbox <dir>                 the SRS-EXE-009 order outbox directory (required)
    --outbox-init                  first start only: create an empty outbox; refused if
                                   <dir> exists
    --socket-dir <dir>             mode 0700 directory; each strategy gets <dir>/<strategy>/order.sock
                                   (required)
    --strategy <id>                a strategy to serve; repeat for each (at least one)
    --transport <fixture|ib>       broker transport (required). `ib` is refused until a
                                   real stale-data producer exists (SRS-MD-004)
    --fixture-wire-ledger <file>   fixture tier only: append one line per order that
                                   reached the gateway
";

/// Exit code for a refused start (bad flags, unusable state, refused tier).
const EXIT_REFUSED: u8 = 2;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("{message}");
            ExitCode::from(EXIT_REFUSED)
        }
    }
}

#[derive(Default)]
struct Flags {
    designation_state: Option<PathBuf>,
    outbox: Option<PathBuf>,
    outbox_init: bool,
    socket_dir: Option<PathBuf>,
    strategies: Vec<String>,
    transport: Option<String>,
    fixture_wire_ledger: Option<PathBuf>,
}

fn parse_flags(args: &[String]) -> Result<Flags, String> {
    let mut flags = Flags::default();
    let mut iter = args.iter();
    while let Some(flag) = iter.next() {
        let mut value = || {
            iter.next()
                .filter(|v| !v.starts_with("--"))
                .cloned()
                .ok_or_else(|| format!("`{flag}` needs a value\n\n{USAGE}"))
        };
        let once = |given: bool| {
            if given {
                Err(format!("`{flag}` was given twice\n\n{USAGE}"))
            } else {
                Ok(())
            }
        };
        match flag.as_str() {
            "--designation-state" => {
                once(flags.designation_state.is_some())?;
                flags.designation_state = Some(PathBuf::from(value()?));
            }
            "--outbox" => {
                once(flags.outbox.is_some())?;
                flags.outbox = Some(PathBuf::from(value()?));
            }
            "--outbox-init" => {
                if flags.outbox_init {
                    return Err(format!("`{flag}` was given twice\n\n{USAGE}"));
                }
                flags.outbox_init = true;
            }
            "--socket-dir" => {
                once(flags.socket_dir.is_some())?;
                flags.socket_dir = Some(PathBuf::from(value()?));
            }
            "--strategy" => flags.strategies.push(value()?),
            "--transport" => {
                once(flags.transport.is_some())?;
                flags.transport = Some(value()?);
            }
            "--fixture-wire-ledger" => {
                once(flags.fixture_wire_ledger.is_some())?;
                flags.fixture_wire_ledger = Some(PathBuf::from(value()?));
            }
            other => return Err(format!("unknown flag `{other}`\n\n{USAGE}")),
        }
    }
    Ok(flags)
}

fn required<T>(slot: Option<T>, flag: &str) -> Result<T, String> {
    slot.ok_or_else(|| format!("`{flag}` is required\n\n{USAGE}"))
}

fn run(args: &[String]) -> Result<(), String> {
    match args.first().map(String::as_str) {
        Some("serve") => {}
        Some("help" | "--help" | "-h") => {
            print!("{USAGE}");
            return Ok(());
        }
        Some(other) => return Err(format!("unknown subcommand `{other}`\n\n{USAGE}")),
        None => return Err(format!("missing subcommand\n\n{USAGE}")),
    }
    let flags = parse_flags(&args[1..])?;
    let transport = required(flags.transport, "--transport")?;
    let config = HostConfig {
        designation_path: required(flags.designation_state, "--designation-state")?,
        outbox_dir: required(flags.outbox, "--outbox")?,
        outbox_init: flags.outbox_init,
    };
    let socket_dir = required(flags.socket_dir, "--socket-dir")?;

    match transport.as_str() {
        "fixture" => {
            let ledger = flags
                .fixture_wire_ledger
                .map(|path| {
                    OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(&path)
                        .map_err(|error| format!("cannot open {}: {error}", path.display()))
                })
                .transpose()?;
            let ports = HostPorts {
                broker: IbBrokerageBridge::new(LedgerIbGateway {
                    inner: RecordingIbGateway::new(),
                    ledger: Mutex::new(ledger),
                }),
                connectivity: HealthyConnectivityFixture,
                events: CollectingConnectivitySink::default(),
                freshness: FreshMarketDataFixture,
                stale_events: CollectingStaleDataSink::default(),
            };
            let host = LiveExecutionHost::open(config, ports, HostTier::Fixture)
                .map_err(|error| error.to_string())?;
            serve(Arc::new(host), socket_dir, &flags.strategies)
        }
        "ib" => {
            if flags.fixture_wire_ledger.is_some() {
                return Err("`--fixture-wire-ledger` is fixture-tier only".to_string());
            }
            Err(
                "SRS-EXE-001 live execution host refused to start the live IB tier: every \
                 live order must pass the stale-data gate, and no production market-data \
                 freshness producer exists yet (owner SRS-MD-004). A fixture freshness probe \
                 on real orders would disable stale-data blocking, so this tier stays closed \
                 until SRS-MD-004 lands."
                    .to_string(),
            )
        }
        other => Err(format!(
            "`--transport {other}` is not `fixture` or `ib`\n\n{USAGE}"
        )),
    }
}

fn serve<B, C, E, F, S>(
    host: Arc<LiveExecutionHost<B, C, E, F, S>>,
    socket_dir: PathBuf,
    strategies: &[String],
) -> Result<(), String>
where
    B: atp_execution::LiveBrokerageSubmit + Send + 'static,
    C: atp_execution::BrokerageConnectivity + Send + 'static,
    E: atp_execution::ConnectivityEventSink + Send + 'static,
    F: atp_execution::MarketDataFreshnessProbe + Send + 'static,
    S: atp_execution::StaleDataEventSink + Send + 'static,
{
    let tier = host.tier();
    let (bound, handles) =
        server::bind(host, &socket_dir, strategies).map_err(|error| error.to_string())?;
    let sockets: Vec<String> = bound
        .sockets()
        .iter()
        .map(|path| path.display().to_string())
        .collect();
    println!(
        "live-host-ready:true\ttier={}\tsockets={}",
        tier.as_str(),
        sockets.join(",")
    );
    let _ = std::io::stdout().flush();
    for handle in handles {
        let _ = handle.join();
    }
    Err("every accept loop exited; the host is no longer serving".to_string())
}

/// The fixture-tier gateway: [`RecordingIbGateway`] plus an append-only ledger line
/// per order that reached it. The ledger is the test's outside view of the wire.
struct LedgerIbGateway {
    inner: RecordingIbGateway,
    ledger: Mutex<Option<File>>,
}

impl IbGatewayConnection for LedgerIbGateway {
    fn submit_order(&self, order: &OrderSubmission) -> Result<OrderReceipt, IbApiError> {
        let receipt = self.inner.submit_order(order)?;
        let mut ledger = self
            .ledger
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Some(file) = ledger.as_mut() {
            // A ledger that cannot be written is a broken test fixture, not an order
            // failure: the gateway has already accepted. Say so loudly.
            if let Err(error) = writeln!(
                file,
                "submit\t{}\t{}\t{}",
                order.strategy_id.as_str(),
                order.symbol,
                receipt.broker_order_id
            )
            .and_then(|()| file.flush())
            {
                eprintln!("live-host: fixture wire ledger write failed: {error}");
            }
        }
        Ok(receipt)
    }

    fn submit_composite_order(
        &self,
        order: &CompositeOrderSubmission,
    ) -> Result<OrderReceipt, IbApiError> {
        self.inner.submit_composite_order(order)
    }

    fn cancel_order(&self, broker_order_id: &str) -> Result<(), IbApiError> {
        self.inner.cancel_order(broker_order_id)
    }

    fn subscribe_market_data(
        &self,
        request: &MarketDataSubscription,
    ) -> Result<SubscriptionReceipt, IbApiError> {
        self.inner.subscribe_market_data(request)
    }

    fn historical_data(
        &self,
        request: &HistoricalDataRequest,
    ) -> Result<HistoricalQueryResult, IbApiError> {
        self.inner.historical_data(request)
    }

    fn account_status(&self) -> Result<DataBatch, IbApiError> {
        self.inner.account_status()
    }

    fn positions(&self) -> Result<DataBatch, IbApiError> {
        self.inner.positions()
    }
}
