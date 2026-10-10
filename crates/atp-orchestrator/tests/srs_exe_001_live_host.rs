//! SRS-EXE-001 — the live execution host, over real Unix sockets (L4 boundary).
//!
//! The broker is a counting double that every test can read from outside the host,
//! so "only the live strategy reached IB" is a count of broker calls, not a reading
//! of the host's own replies. Every other component is the production one: the
//! socket server, the protocol parser, the shared designation store and its lock,
//! `ExecutionEngine::route_order_durably`, and the SRS-EXE-009 outbox.

use atp_execution::designation::{LiveDesignation, LiveDesignationConfirmation};
use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, LiveBrokerageSubmit, MarketDataFreshnessProbe,
    OrderOutbox, StaleDataEventSink,
};
use atp_orchestrator::live_designation_store::{save_designation, PublishOutcome};
use atp_orchestrator::live_host::protocol::{encode_request, SubmitRequest, PROTOCOL_MAGIC};
use atp_orchestrator::live_host::{server, HostConfig, HostPorts, HostTier, LiveExecutionHost};
use atp_orchestrator::trigger_config_store::ExclusiveGuard;
use atp_types::{
    AssetClass, ClientCorrelationId, ConnectivityEvent, ConnectivityState, MarketDataFreshness,
    OrderReceipt, OrderSide, OrderSubmission, OrderType, StaleDataEvent, StrategyId,
    StructuredOrderError,
};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

// --------------------------------------------------------------------------- //
// Doubles
// --------------------------------------------------------------------------- //

/// Accepts every order and records who sent it. Shared through an `Arc` so the
/// test reads it after the host has taken ownership of the port.
#[derive(Clone, Default)]
struct CountingBroker {
    calls: Arc<Mutex<Vec<(String, Instant)>>>,
    /// The designation lock file; when set, every call records whether a process
    /// held the swap lock at the moment the order reached the broker.
    lock_file: Arc<Mutex<Option<PathBuf>>>,
    lock_held_at_call: Arc<Mutex<Vec<bool>>>,
}

impl CountingBroker {
    fn strategies(&self) -> Vec<String> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .map(|(id, _)| id.clone())
            .collect()
    }
    fn first_call_at(&self) -> Option<Instant> {
        self.calls.lock().unwrap().first().map(|(_, at)| *at)
    }
}

impl LiveBrokerageSubmit for CountingBroker {
    fn submit_order(
        &self,
        submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        if let Some(lock) = self.lock_file.lock().unwrap().as_ref() {
            self.lock_held_at_call.lock().unwrap().push(lock.exists());
        }
        let mut calls = self.calls.lock().unwrap();
        calls.push((submission.strategy_id.as_str().to_string(), Instant::now()));
        Ok(OrderReceipt {
            broker_order_id: format!("IB-{}", calls.len()),
        })
    }
}

struct Connectivity(ConnectivityState);
impl BrokerageConnectivity for Connectivity {
    fn state(&self) -> ConnectivityState {
        self.0
    }
    fn request_reconnect(&self) {}
}

struct Freshness(MarketDataFreshness);
impl MarketDataFreshnessProbe for Freshness {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        self.0
    }
    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        30
    }
}

struct NoEvents;
impl ConnectivityEventSink for NoEvents {
    fn record(&self, _event: ConnectivityEvent) {}
}
impl StaleDataEventSink for NoEvents {
    fn record(&self, _event: StaleDataEvent) {}
}

type TestHost = LiveExecutionHost<CountingBroker, Connectivity, NoEvents, Freshness, NoEvents>;

// --------------------------------------------------------------------------- //
// Fixture
// --------------------------------------------------------------------------- //

static SEQ: AtomicU64 = AtomicU64::new(0);

/// A short scratch root: Unix socket paths are limited to ~104 bytes.
fn scratch() -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "exe001-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("s")).unwrap();
    std::fs::set_permissions(dir.join("s"), std::fs::Permissions::from_mode(0o700)).unwrap();
    dir
}

fn designate(state: &Path, strategy: &str) {
    let id = StrategyId::new(strategy);
    let mut designation = LiveDesignation::new();
    designation
        .designate(
            id.clone(),
            LiveDesignationConfirmation::from_operator(id, "test operator confirms").unwrap(),
        )
        .unwrap();
    assert!(matches!(
        save_designation(state, &designation),
        PublishOutcome::Durable
    ));
}

struct Running {
    root: PathBuf,
    broker: CountingBroker,
    _bound: server::BoundHost,
}

impl Running {
    fn socket(&self, strategy: &str) -> PathBuf {
        server::socket_path(&self.root.join("s"), strategy)
    }
    fn state(&self) -> PathBuf {
        self.root.join("designation")
    }
}

fn host_over(
    root: &Path,
    init: bool,
    connectivity: ConnectivityState,
    freshness: MarketDataFreshness,
    broker: CountingBroker,
) -> Result<TestHost, String> {
    LiveExecutionHost::open(
        HostConfig {
            designation_path: root.join("designation"),
            outbox_dir: root.join("outbox"),
            outbox_init: init,
        },
        HostPorts {
            broker,
            connectivity: Connectivity(connectivity),
            events: NoEvents,
            freshness: Freshness(freshness),
            stale_events: NoEvents,
        },
        HostTier::Fixture,
    )
    .map_err(|error| error.to_string())
}

fn start_with(
    root: PathBuf,
    strategies: &[&str],
    connectivity: ConnectivityState,
    freshness: MarketDataFreshness,
) -> Running {
    let broker = CountingBroker::default();
    let host = host_over(&root, true, connectivity, freshness, broker.clone()).unwrap();
    let strategies: Vec<String> = strategies.iter().map(|s| s.to_string()).collect();
    let (bound, _threads) = server::bind(Arc::new(host), &root.join("s"), &strategies).unwrap();
    Running {
        root,
        broker,
        _bound: bound,
    }
}

fn start(strategies: &[&str]) -> Running {
    start_with(
        scratch(),
        strategies,
        ConnectivityState::Connected,
        MarketDataFreshness::Fresh,
    )
}

fn order(correlation_id: &str) -> SubmitRequest {
    SubmitRequest {
        correlation_id: correlation_id.to_string(),
        symbol: "AAPL".to_string(),
        side: OrderSide::Buy,
        quantity: 1,
        asset_class: AssetClass::Equity,
        order_type: OrderType::Limit {
            limit_price_minor: 19_000,
        },
    }
}

/// Send one raw frame on a fresh connection and return the reply's fields.
fn send_raw(socket: &Path, frame: &str) -> Vec<String> {
    let mut stream = UnixStream::connect(socket).unwrap();
    stream.write_all(frame.as_bytes()).unwrap();
    stream.write_all(b"\n").unwrap();
    let mut reply = String::new();
    BufReader::new(stream).read_line(&mut reply).unwrap();
    assert!(
        reply.ends_with('\n'),
        "reply is not newline-terminated: {reply:?}"
    );
    let fields: Vec<String> = reply.trim_end().split('\t').map(str::to_string).collect();
    assert_eq!(fields[0], PROTOCOL_MAGIC);
    fields
}

fn send(socket: &Path, request: &SubmitRequest) -> Vec<String> {
    send_raw(socket, &encode_request(request))
}

fn field<'a>(reply: &'a [String], key: &str) -> &'a str {
    reply
        .iter()
        .find_map(|f| f.strip_prefix(&format!("{key}=")))
        .unwrap_or_else(|| panic!("reply has no `{key}`: {reply:?}"))
}

const PAPER: [&str; 5] = ["paper-1", "paper-2", "paper-3", "paper-4", "paper-5"];

// --------------------------------------------------------------------------- //
// The acceptance criterion: 1 live + 5 paper
// --------------------------------------------------------------------------- //

#[test]
fn with_one_live_and_five_paper_strategies_only_the_live_one_reaches_the_broker() {
    let mut all = vec!["live-a"];
    all.extend(PAPER);
    let host = start(&all);
    designate(&host.state(), "live-a");

    for round in 0..4 {
        let reply = send(&host.socket("live-a"), &order(&format!("live-{round}")));
        assert_eq!(reply[1], "ack", "{reply:?}");
        assert_eq!(field(&reply, "durable"), "true");
        assert_eq!(field(&reply, "tier"), "FIXTURE");
        for paper in PAPER {
            let reply = send(&host.socket(paper), &order(&format!("{paper}-{round}")));
            assert_eq!(reply[1], "reject", "{paper}: {reply:?}");
            assert_eq!(field(&reply, "category"), "NON_LIVE_STRATEGY_SUBMISSION");
            assert_eq!(field(&reply, "error_type"), "NotDesignatedLiveStrategy");
            assert_eq!(field(&reply, "correlation_id"), format!("{paper}-{round}"));
        }
    }
    // The broker's own record: four calls, all from the live strategy. Every paper
    // rejection happened at wire-attempts:0.
    assert_eq!(host.broker.strategies(), vec!["live-a"; 4]);
}

#[test]
fn with_nobody_designated_every_strategy_is_rejected() {
    let host = start(&["live-a", "paper-1"]);
    for strategy in ["live-a", "paper-1"] {
        let reply = send(&host.socket(strategy), &order("c-1"));
        assert_eq!(field(&reply, "category"), "NON_LIVE_STRATEGY_SUBMISSION");
    }
    assert!(host.broker.strategies().is_empty());
}

#[test]
fn a_frame_cannot_claim_another_strategys_identity() {
    let host = start(&["live-a", "paper-1"]);
    designate(&host.state(), "live-a");
    let forged = format!("{}\tstrategy_id=live-a", encode_request(&order("c-1")));
    let reply = send_raw(&host.socket("paper-1"), &forged);
    assert_eq!(reply[1], "refused", "{reply:?}");
    assert!(field(&reply, "message").contains("unknown field `strategy_id`"));
    assert!(host.broker.strategies().is_empty());
}

// --------------------------------------------------------------------------- //
// The designation is re-read on every order, under the swap lock
// --------------------------------------------------------------------------- //

#[test]
fn a_moved_designation_takes_effect_at_the_next_order() {
    let host = start(&["live-a", "paper-1"]);
    designate(&host.state(), "live-a");
    assert_eq!(send(&host.socket("live-a"), &order("a-1"))[1], "ack");

    // What a completed Hot-Swap leaves on disk.
    designate(&host.state(), "paper-1");
    assert_eq!(send(&host.socket("live-a"), &order("a-2"))[1], "reject");
    assert_eq!(send(&host.socket("paper-1"), &order("p-1"))[1], "ack");
    assert_eq!(host.broker.strategies(), vec!["live-a", "paper-1"]);
}

#[test]
fn an_order_waits_for_a_swap_holding_the_designation_lock() {
    let host = start(&["live-a"]);
    designate(&host.state(), "live-a");

    let guard = ExclusiveGuard::acquire_creating(&host.state()).unwrap();
    let released_at = Arc::new(Mutex::new(None::<Instant>));
    let socket = host.socket("live-a");
    let sender = std::thread::spawn(move || send(&socket, &order("c-1")));
    std::thread::sleep(Duration::from_millis(300));
    assert!(
        host.broker.strategies().is_empty(),
        "the order reached the broker while a swap held the designation lock"
    );
    *released_at.lock().unwrap() = Some(Instant::now());
    drop(guard);

    let reply = sender.join().unwrap();
    assert_eq!(reply[1], "ack", "{reply:?}");
    let released = released_at.lock().unwrap().unwrap();
    assert!(host.broker.first_call_at().unwrap() >= released);
}

#[test]
fn the_designation_lock_is_held_while_the_order_is_at_the_broker() {
    let host = start(&["live-a"]);
    designate(&host.state(), "live-a");
    let mut lock = host.state().into_os_string();
    lock.push(".lock");
    *host.broker.lock_file.lock().unwrap() = Some(PathBuf::from(lock));

    assert_eq!(send(&host.socket("live-a"), &order("c-1"))[1], "ack");
    // Released early, a Hot-Swap could move the live slot between the designation
    // read and the broker call, and the demoted strategy's order would still land.
    assert_eq!(*host.broker.lock_held_at_call.lock().unwrap(), vec![true]);
}

#[test]
fn an_unreadable_designation_refuses_the_order_instead_of_reading_as_nobody() {
    let host = start(&["live-a"]);
    designate(&host.state(), "live-a");
    std::fs::write(host.state(), "not a designation snapshot\n").unwrap();
    let reply = send(&host.socket("live-a"), &order("c-1"));
    assert_eq!(reply[1], "refused", "{reply:?}");
    assert_eq!(field(&reply, "error_type"), "DesignationUnreadable");
    assert!(host.broker.strategies().is_empty());
}

// --------------------------------------------------------------------------- //
// The inner ERR-2 / ERR-3 gates still run for the live strategy
// --------------------------------------------------------------------------- //

#[test]
fn an_unreachable_gateway_blocks_the_live_strategy_before_the_broker() {
    let host = start_with(
        scratch(),
        &["live-a"],
        ConnectivityState::Unreachable,
        MarketDataFreshness::Fresh,
    );
    designate(&host.state(), "live-a");
    let reply = send(&host.socket("live-a"), &order("c-1"));
    assert_eq!(field(&reply, "category"), "CONNECTIVITY_BLOCKED");
    assert!(host.broker.strategies().is_empty());
}

#[test]
fn stale_market_data_blocks_the_live_strategy_before_the_broker() {
    let host = start_with(
        scratch(),
        &["live-a"],
        ConnectivityState::Connected,
        MarketDataFreshness::Stale,
    );
    designate(&host.state(), "live-a");
    let reply = send(&host.socket("live-a"), &order("c-1"));
    assert_eq!(field(&reply, "category"), "MARKET_DATA_STALE");
    assert!(host.broker.strategies().is_empty());
}

// --------------------------------------------------------------------------- //
// Durable state across a restart
// --------------------------------------------------------------------------- //

#[test]
fn a_restart_recovers_the_outbox_so_a_replayed_order_is_not_resubmitted() {
    let root = scratch();
    designate(&root.join("designation"), "live-a");
    {
        let broker = CountingBroker::default();
        let host = host_over(
            &root,
            true,
            ConnectivityState::Connected,
            MarketDataFreshness::Fresh,
            broker.clone(),
        )
        .unwrap();
        let reply = host.submit(&StrategyId::new("live-a"), order("c-1"));
        assert!(matches!(
            reply,
            atp_orchestrator::live_host::protocol::Reply::Ack { .. }
        ));
    }
    let broker = CountingBroker::default();
    let host = host_over(
        &root,
        false,
        ConnectivityState::Connected,
        MarketDataFreshness::Fresh,
        broker.clone(),
    )
    .unwrap();
    let reply = host.submit(&StrategyId::new("live-a"), order("c-1"));
    match reply {
        atp_orchestrator::live_host::protocol::Reply::Reject { category, .. } => {
            assert_eq!(category, "DUPLICATE_CLIENT_CORRELATION_ID");
        }
        other => panic!("a replayed correlation id after restart was not rejected: {other:?}"),
    }
    assert!(broker.strategies().is_empty());
}

#[test]
fn open_refuses_unsafe_outbox_states() {
    let healthy = |root: &Path, init: bool| {
        host_over(
            root,
            init,
            ConnectivityState::Connected,
            MarketDataFreshness::Fresh,
            CountingBroker::default(),
        )
        .map(|_| ())
    };

    // A restart with no outbox is not a first start.
    let root = scratch();
    let err = healthy(&root, false).unwrap_err();
    assert!(
        err.contains("--outbox-init only for a genuine first start"),
        "{err}"
    );

    // --outbox-init over an existing outbox would discard prior intents.
    healthy(&root, true).unwrap();
    let err = healthy(&root, true).unwrap_err();
    assert!(err.contains("already exists"), "{err}");

    // An intent written ahead but never acknowledged may be a live IB order.
    let root = scratch();
    let mut outbox = OrderOutbox::new();
    let submission = OrderSubmission::new(
        StrategyId::new("live-a"),
        "AAPL",
        1,
        AssetClass::Equity,
        OrderSide::Buy,
        OrderType::Market,
    );
    outbox
        .commit_intent(ClientCorrelationId::new("c-1").unwrap(), &submission)
        .unwrap();
    std::fs::create_dir_all(root.join("outbox")).unwrap();
    outbox.persist(&root.join("outbox")).unwrap();
    let err = healthy(&root, false).unwrap_err();
    assert!(
        err.contains("SRS-EXE-009") && err.contains("live-a/c-1"),
        "{err}"
    );

    // An unreadable designation is not "nobody is live".
    let root = scratch();
    std::fs::write(root.join("designation"), "garbage\n").unwrap();
    let err = healthy(&root, true).unwrap_err();
    assert!(err.contains("RESV005-LIVE-DESIGNATION-STATE"), "{err}");
}

#[test]
fn a_second_host_over_the_same_outbox_is_refused_while_the_first_lives() {
    let root = scratch();
    let open = |init: bool| {
        host_over(
            &root,
            init,
            ConnectivityState::Connected,
            MarketDataFreshness::Fresh,
            CountingBroker::default(),
        )
    };
    let first = open(true).unwrap();
    // Two hosts over one outbox would each keep their own copy and overwrite each
    // other's snapshot, losing the duplicate-submission record.
    let err = open(false)
        .err()
        .expect("a second host opened the same outbox");
    assert!(err.contains("already owns the outbox"), "{err}");
    // The lock is released with the host, so a restart is never wedged by it.
    drop(first);
    open(false).unwrap();
}

#[test]
fn a_vanished_state_directory_refuses_the_order_instead_of_being_recreated() {
    let root = scratch();
    let state_dir = root.join("state");
    std::fs::create_dir(&state_dir).unwrap();
    designate(&state_dir.join("designation"), "live-a");
    let broker = CountingBroker::default();
    let host = LiveExecutionHost::open(
        HostConfig {
            designation_path: state_dir.join("designation"),
            outbox_dir: root.join("outbox"),
            outbox_init: true,
        },
        HostPorts {
            broker: broker.clone(),
            connectivity: Connectivity(ConnectivityState::Connected),
            events: NoEvents,
            freshness: Freshness(MarketDataFreshness::Fresh),
            stale_events: NoEvents,
        },
        HostTier::Fixture,
    )
    .unwrap();
    std::fs::remove_dir_all(&state_dir).unwrap();

    match host.submit(&StrategyId::new("live-a"), order("c-1")) {
        atp_orchestrator::live_host::protocol::Reply::Refused { error_type, .. } => {
            assert_eq!(error_type, "DesignationUnreadable");
        }
        other => panic!("a vanished state directory did not refuse: {other:?}"),
    }
    assert!(
        !state_dir.exists(),
        "the order recreated the state directory"
    );
    assert!(broker.strategies().is_empty());
}

#[test]
fn an_oversize_designation_snapshot_is_refused() {
    let root = scratch();
    let state = root.join("designation");
    designate(&state, "live-a");
    let mut body = std::fs::read_to_string(&state).unwrap();
    body.push_str(&"\n".repeat(5000));
    std::fs::write(&state, body).unwrap();
    let err = atp_orchestrator::live_designation_store::load_designation(&state).unwrap_err();
    assert!(err.contains("larger than"), "{err}");
}

#[test]
fn a_second_host_over_the_same_socket_directory_is_refused() {
    let host = start(&["live-a"]);
    let other_root = scratch();
    let second = host_over(
        &other_root,
        true,
        ConnectivityState::Connected,
        MarketDataFreshness::Fresh,
        CountingBroker::default(),
    )
    .unwrap();
    let err = server::bind(
        Arc::new(second),
        &host.root.join("s"),
        &["live-a".to_string()],
    )
    .err()
    .expect("a second host bound the same sockets");
    assert!(err.reason.contains("another live execution host"), "{err}");
    // The first host still serves.
    designate(&host.state(), "live-a");
    assert_eq!(send(&host.socket("live-a"), &order("c-1"))[1], "ack");
}

#[test]
fn each_strategy_gets_a_private_directory_holding_only_its_own_socket() {
    let host = start(&["live-a", "paper-1"]);
    for strategy in ["live-a", "paper-1"] {
        let dir = server::strategy_dir(&host.root.join("s"), strategy);
        let mode = std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o700, "{strategy} directory");
        let socket = std::fs::symlink_metadata(host.socket(strategy)).unwrap();
        assert_eq!(
            socket.permissions().mode() & 0o777,
            0o600,
            "{strategy} socket"
        );
        // A container given this directory can name nothing else.
        let entries: Vec<_> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        assert_eq!(entries, vec![server::SOCKET_FILE_NAME.to_string()]);
    }
}

#[test]
fn a_socket_directory_open_to_other_users_is_refused() {
    let root = scratch();
    std::fs::set_permissions(root.join("s"), std::fs::Permissions::from_mode(0o755)).unwrap();
    let host = host_over(
        &root,
        true,
        ConnectivityState::Connected,
        MarketDataFreshness::Fresh,
        CountingBroker::default(),
    )
    .unwrap();
    let err = server::bind(Arc::new(host), &root.join("s"), &["live-a".to_string()])
        .err()
        .expect("an open socket directory was served");
    assert!(err.reason.contains("mode 0755"), "{err}");
    assert!(!server::socket_path(&root.join("s"), "live-a").exists());
}

#[test]
fn a_symlinked_strategy_directory_is_refused() {
    let root = scratch();
    let elsewhere = root.join("elsewhere");
    std::fs::create_dir(&elsewhere).unwrap();
    std::os::unix::fs::symlink(&elsewhere, root.join("s").join("live-a")).unwrap();
    let host = host_over(
        &root,
        true,
        ConnectivityState::Connected,
        MarketDataFreshness::Fresh,
        CountingBroker::default(),
    )
    .unwrap();
    let err = server::bind(Arc::new(host), &root.join("s"), &["live-a".to_string()])
        .err()
        .expect("a symlinked strategy directory was served");
    assert!(err.reason.contains("is not a directory"), "{err}");
    assert_eq!(std::fs::read_dir(&elsewhere).unwrap().count(), 0);
}

#[test]
fn an_oversize_frame_is_refused_and_the_connection_closed() {
    let host = start(&["live-a"]);
    designate(&host.state(), "live-a");
    let mut stream = UnixStream::connect(host.socket("live-a")).unwrap();
    // 10,000 bytes is more than the socket buffer. The host stops reading at the
    // frame limit, replies, and closes, so the tail of this write can fail with a
    // broken pipe. That is the behaviour under test, not an error; the reply below
    // is what matters.
    let _ = stream.write_all(&vec![b'A'; 10_000]);
    let mut reader = BufReader::new(stream);
    let mut reply = String::new();
    reader.read_line(&mut reply).unwrap();
    assert!(
        reply.contains("\trefused\t") && reply.contains("ProtocolError"),
        "{reply}"
    );
    let mut rest = String::new();
    assert_eq!(
        reader.read_line(&mut rest).unwrap(),
        0,
        "connection stayed open"
    );
    assert!(host.broker.strategies().is_empty());
}
