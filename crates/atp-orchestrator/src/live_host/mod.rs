//! SRS-EXE-001 — the live execution host: the one process that routes strategy
//! orders to IB (SyRS SYS-1 / SYS-2a / SYS-2d / AC-15).
//!
//! Before this module, the live-designation gate (`ExecutionEngine::route_order`)
//! was real but nothing ran it: only fixture CLIs called it. The host is the
//! production call site.
//!
//! # What it does, per order
//!
//! 1. A strategy writes one `submit` frame to ITS socket ([`server`]). The socket,
//!    not the frame, says which strategy is speaking.
//! 2. The host takes its in-process order mutex, then the SAME
//!    [`ExclusiveGuard`] over the designation snapshot that the Hot-Swap CLI holds
//!    for a swap, and re-reads the snapshot ([`live_designation_store`]). Nothing is
//!    cached, so a Hot-Swap or a demotion takes effect at the very next order, and
//!    an order can never interleave with a swap that is moving the live slot.
//! 3. It builds an [`ExecutionEngine`] holding that designation and calls
//!    [`ExecutionEngine::route_order_durably`]: authority gate first (a
//!    non-designated strategy is rejected with `NON_LIVE_STRATEGY_SUBMISSION` before
//!    any port is touched), then the SRS-EXE-009 write-ahead outbox, then the
//!    ERR-2/ERR-3 connectivity and freshness gates, then the broker.
//! 4. It writes one reply frame ([`protocol::Reply`]).
//!
//! # What it does not do
//!
//! The ports are injected. This module never picks a fixture: the binary decides,
//! and labels every reply with the [`HostTier`] it chose, so fixture evidence can
//! never be read as a live run. The live IB tier needs a real stale-data producer,
//! which does not exist yet (owner SRS-MD-004), so the binary refuses to start that
//! tier. Restart reconciliation of an in-flight order is SRS-EXE-009: [`open`]
//! refuses an outbox holding an unbound, unresolved intent rather than guessing.
//!
//! [`open`]: LiveExecutionHost::open
//! [`live_designation_store`]: crate::live_designation_store

pub mod protocol;
pub mod server;

use crate::live_designation_store::load_designation;
use crate::trigger_config_store::ExclusiveGuard;
use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, DurableSubmitError, ExecutionEngine,
    LiveBrokerageSubmit, LiveDesignationConfirmation, MarketDataFreshnessProbe, OrderOutbox,
    OutboxSnapshot, StaleDataEventSink,
};
use atp_types::{ClientCorrelationId, OrderSubmission, StrategyId};
use protocol::{Reply, SubmitRequest};
use std::fmt;
use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// Which transport the host's broker port is wired to. Every reply carries it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostTier {
    /// A deterministic in-process gateway. No order reaches IB.
    Fixture,
    /// The real IB Gateway socket transport.
    LiveIb,
}

impl HostTier {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Fixture => "FIXTURE",
            Self::LiveIb => "LIVE_IB",
        }
    }
}

/// The ports one live order passes through, in the order the engine consults them.
#[derive(Debug)]
pub struct HostPorts<B, C, E, F, S> {
    pub broker: B,
    pub connectivity: C,
    pub events: E,
    pub freshness: F,
    pub stale_events: S,
}

/// Where the host keeps its durable state.
#[derive(Debug, Clone)]
pub struct HostConfig {
    /// The shared durable designation snapshot (`ATP_HOT_SWAP_DESIGNATION_STATE`).
    pub designation_path: PathBuf,
    /// The SRS-EXE-009 outbox directory.
    pub outbox_dir: PathBuf,
    /// A genuine first start: create an empty outbox. Refused when the directory
    /// already exists, so a restart can never silently discard prior intents.
    pub outbox_init: bool,
}

/// Why the host refused to start.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostStartError {
    pub reason: String,
}

impl fmt::Display for HostStartError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-EXE-001 live execution host refused to start: {}",
            self.reason
        )
    }
}

impl std::error::Error for HostStartError {}

fn start_error(reason: impl Into<String>) -> HostStartError {
    HostStartError {
        reason: reason.into(),
    }
}

/// The acknowledgement a restored designation carries. The operator confirmed the
/// designation when it was WRITTEN (`exe001_live_designation_cli promote` or a
/// Hot-Swap); reading it back is not a new confirmation, and says so.
const RESTORED_ACKNOWLEDGEMENT: &str = "restored from the durable designation snapshot";

struct Core<B, C, E, F, S> {
    outbox: OrderOutbox,
    ports: HostPorts<B, C, E, F, S>,
}

/// The live execution host. See the module docs.
pub struct LiveExecutionHost<B, C, E, F, S> {
    designation_path: PathBuf,
    outbox_dir: PathBuf,
    tier: HostTier,
    /// The OS lock on `<outbox_dir>/host.lock`, held for the host's lifetime. See
    /// [`lock_outbox`].
    _instance_lock: File,
    core: Mutex<Core<B, C, E, F, S>>,
}

/// The outbox has exactly one writer: take an OS file lock on
/// `<outbox_dir>/host.lock` and hold it until the host is dropped.
///
/// Two hosts over one outbox would each keep their own in-memory copy and overwrite
/// each other's snapshot, losing the duplicate-submission record the outbox exists
/// to keep. An OS lock rather than an `O_EXCL` file, because the kernel releases it
/// when the process dies: a crashed host must not block its own restart.
fn lock_outbox(outbox_dir: &Path) -> Result<File, HostStartError> {
    let path = outbox_dir.join("host.lock");
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&path)
        .map_err(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                start_error(format!(
                    "cannot recover the outbox at {} (the directory does not exist); pass \
                     --outbox-init only for a genuine first start",
                    outbox_dir.display()
                ))
            } else {
                start_error(format!("cannot open {}: {error}", path.display()))
            }
        })?;
    file.try_lock().map_err(|_| {
        start_error(format!(
            "another live execution host already owns the outbox at {} (it holds {})",
            outbox_dir.display(),
            path.display()
        ))
    })?;
    Ok(file)
}

impl<B, C, E, F, S> LiveExecutionHost<B, C, E, F, S>
where
    B: LiveBrokerageSubmit,
    C: BrokerageConnectivity,
    E: ConnectivityEventSink,
    F: MarketDataFreshnessProbe,
    S: StaleDataEventSink,
{
    /// Open the host over its durable state.
    ///
    /// Refuses when the designation snapshot's directory is missing or the snapshot
    /// is unreadable (an unreadable snapshot is not "nobody is live"), when
    /// `outbox_init` is set over an existing outbox directory, when a restart finds
    /// no outbox, or when the outbox holds an intent that was written ahead but
    /// never acknowledged or resolved (reconciling it is SRS-EXE-009).
    pub fn open(
        config: HostConfig,
        ports: HostPorts<B, C, E, F, S>,
        tier: HostTier,
    ) -> Result<Self, HostStartError> {
        let parent = config
            .designation_path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        if !parent.is_dir() {
            return Err(start_error(format!(
                "the designation snapshot's directory {} does not exist",
                parent.display()
            )));
        }
        {
            let _guard = ExclusiveGuard::acquire_creating(&config.designation_path)
                .map_err(|error| start_error(format!("designation lock: {error}")))?;
            load_designation(&config.designation_path).map_err(start_error)?;
        }
        if config.outbox_init {
            // `create_dir`, not `create_dir_all`: it IS the existence check, atomically,
            // so two first starts racing on one directory cannot both succeed.
            std::fs::create_dir(&config.outbox_dir).map_err(|error| {
                if error.kind() == std::io::ErrorKind::AlreadyExists {
                    start_error(format!(
                        "--outbox-init was given but {} already exists; a restart must \
                         recover the outbox, not replace it",
                        config.outbox_dir.display()
                    ))
                } else {
                    start_error(format!(
                        "cannot create outbox directory {}: {error}",
                        config.outbox_dir.display()
                    ))
                }
            })?;
        }
        let instance_lock = lock_outbox(&config.outbox_dir)?;
        let outbox = if config.outbox_init {
            let outbox = OrderOutbox::new();
            outbox
                .persist(&config.outbox_dir)
                .map_err(|error| start_error(format!("cannot write the empty outbox: {error}")))?;
            outbox
        } else {
            let outbox = OutboxSnapshot::load_from_path(&config.outbox_dir)
                .map_err(|error| {
                    start_error(format!(
                        "cannot recover the outbox at {} ({error}); pass --outbox-init only for a \
                     genuine first start",
                        config.outbox_dir.display()
                    ))
                })?
                .into_outbox();
            let unresolved: Vec<String> = outbox
                .entries_sorted()
                .into_iter()
                .filter(|entry| !entry.is_bound() && !entry.state().is_terminal())
                .map(|entry| {
                    format!(
                        "{}/{}",
                        entry.key().strategy_id().as_str(),
                        entry.key().correlation_id().as_str()
                    )
                })
                .collect();
            if !unresolved.is_empty() {
                return Err(start_error(format!(
                    "the outbox holds {} intent(s) written ahead but never acknowledged or \
                     resolved ({}); one may be a live IB order. Reconcile against the broker \
                     first (SRS-EXE-009) - serving now could submit a duplicate",
                    unresolved.len(),
                    unresolved.join(", ")
                )));
            }
            outbox
        };
        Ok(Self {
            designation_path: config.designation_path,
            outbox_dir: config.outbox_dir,
            tier,
            _instance_lock: instance_lock,
            core: Mutex::new(Core { outbox, ports }),
        })
    }

    /// The tier every reply is labelled with.
    pub fn tier(&self) -> HostTier {
        self.tier
    }

    /// Handle one raw request frame from `strategy`'s socket.
    pub fn handle_frame(&self, strategy: &StrategyId, frame: &str) -> Reply {
        match protocol::parse_request(frame) {
            Ok(request) => self.submit(strategy, request),
            Err(error) => self.refused(None, "ProtocolError", error.to_string()),
        }
    }

    /// Route one order from `strategy`. See the module docs for the sequence.
    pub fn submit(&self, strategy: &StrategyId, request: SubmitRequest) -> Reply {
        let correlation_text = request.correlation_id.clone();
        let correlation = match ClientCorrelationId::new(request.correlation_id) {
            Ok(id) => id,
            Err(error) => {
                return self.refused(Some(correlation_text), "ProtocolError", error.to_string())
            }
        };
        let submission = OrderSubmission::new(
            strategy.clone(),
            request.symbol,
            request.quantity,
            request.asset_class,
            request.side,
            request.order_type,
        );

        // A poisoned mutex means a previous order panicked part-way through, so the
        // in-memory outbox may not match what is on disk. Refuse every later order.
        let Ok(mut core) = self.core.lock() else {
            return self.refused(
                Some(correlation_text),
                "HostPoisoned",
                "a previous order panicked inside the host; restart it so the outbox is \
                 recovered from disk"
                    .to_string(),
            );
        };
        // Held until this function returns: the designation cannot move between the
        // read below and the broker call.
        // `acquire_if_parent_exists`, not `acquire_creating`: a state directory that
        // vanished must refuse the order, not be silently recreated and then read as
        // "nobody is live".
        let _designation_guard =
            match ExclusiveGuard::acquire_if_parent_exists(&self.designation_path) {
                Ok(Some(guard)) => guard,
                Ok(None) => {
                    return self.refused(
                        Some(correlation_text),
                        "DesignationUnreadable",
                        format!(
                            "the directory holding the designation snapshot {} no longer exists",
                            self.designation_path.display()
                        ),
                    )
                }
                Err(error) => {
                    return self.refused(
                        Some(correlation_text),
                        "DesignationLockUnavailable",
                        error.to_string(),
                    )
                }
            };
        let mut engine = ExecutionEngine::default();
        match load_designation(&self.designation_path) {
            Ok(designation) => {
                if let Some(live) = designation.designated() {
                    let restored = LiveDesignationConfirmation::from_operator(
                        live.clone(),
                        RESTORED_ACKNOWLEDGEMENT,
                    )
                    .and_then(|confirmation| engine.designate(live.clone(), confirmation));
                    if let Err(error) = restored {
                        return self.refused(
                            Some(correlation_text),
                            "DesignationUnreadable",
                            error.to_string(),
                        );
                    }
                }
            }
            Err(reason) => {
                return self.refused(Some(correlation_text), "DesignationUnreadable", reason)
            }
        }

        let Core { outbox, ports } = &mut *core;
        let result = engine.route_order_durably(
            outbox,
            &self.outbox_dir,
            correlation,
            submission,
            &ports.broker,
            &ports.connectivity,
            &ports.events,
            &ports.freshness,
            &ports.stale_events,
        );
        self.reply_for(correlation_text, result)
    }

    fn reply_for(
        &self,
        correlation_id: String,
        result: Result<atp_types::OrderReceipt, DurableSubmitError>,
    ) -> Reply {
        let tier = self.tier.as_str();
        match result {
            Ok(receipt) => Reply::Ack {
                correlation_id,
                broker_order_id: receipt.broker_order_id,
                durable: true,
                tier,
            },
            Err(DurableSubmitError::AckNotDurable { receipt, source }) => {
                eprintln!(
                    "live-host: order {correlation_id} is LIVE as {} but its acknowledgement \
                     is not durable ({source}); reconcile before restart",
                    receipt.broker_order_id
                );
                Reply::Ack {
                    correlation_id,
                    broker_order_id: receipt.broker_order_id,
                    durable: false,
                    tier,
                }
            }
            Err(DurableSubmitError::Rejected(error)) => Reply::Reject {
                correlation_id,
                category: error.category.as_str().to_string(),
                error_type: error.error_type,
                message: error.message,
                durable: true,
                tier,
            },
            Err(DurableSubmitError::WriteAheadPersistence(error)) => self.refused(
                Some(correlation_id),
                "OutboxWriteAheadFailed",
                format!("the order was not sent: {error}"),
            ),
            Err(DurableSubmitError::RejectionCleanupFailed(source)) => self.refused(
                Some(correlation_id),
                "RejectionNotDurable",
                format!(
                    "the order was rejected (no live order) but the REJECTED record could \
                     not be written: {source}"
                ),
            ),
        }
    }

    fn refused(&self, correlation_id: Option<String>, error_type: &str, message: String) -> Reply {
        Reply::Refused {
            correlation_id,
            error_type: error_type.to_string(),
            message,
            tier: self.tier.as_str(),
        }
    }
}
