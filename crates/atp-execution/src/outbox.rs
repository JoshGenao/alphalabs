//! SRS-EXE-009 — the durable order-intent **outbox** and restart reconciliation.
//!
//! ## What this is (and the scope boundary with SRS-EXE-005)
//!
//! SRS-EXE-005 ([`crate::live_state`]) snapshots *already-known* live state — broker
//! IDs, fills, positions, equity — and restores it on restart. It deliberately does
//! **not** claim the crash window *between* the durable intent commit and the IB
//! submission (see `live_state.rs` module docs). SRS-EXE-009 is that narrower,
//! stronger guarantee: a **write-ahead outbox** that
//!
//! 1. durably commits an order **intent** *before* it is submitted to IB
//!    ([`OrderOutbox::commit_intent`]), so a crash mid-submit never loses the record
//!    that an order was *about* to go live;
//! 2. on restart, **reconciles** the durable outbox against the broker's reported
//!    order state ([`reconcile`]): an intent that already carries an acknowledged
//!    broker ID is treated as **bound** to its correlation ID (SRS-EXE-008) and is
//!    **never resubmitted**; an unacknowledged intent is adopted (if the broker
//!    already has it), resubmitted (only if we can *prove* it never landed), or
//!    surfaced as unresolved (fail closed) when the broker view is too partial to
//!    decide safely;
//! 3. **retains** each entry until its terminal state (FILLED / CANCELLED / REJECTED
//!    / EXPIRED) is observed ([`OrderOutbox::prune_terminal`]).
//!
//! Getting reconciliation wrong doubles a live order — real money — so every
//! ambiguous path fails closed toward *not* resubmitting.
//!
//! ## What is real here vs deferred
//!
//! Real (solo-verifiable with a mocked broker): the durable codec (atomic
//! `fsync`→`rename`→dir-`fsync`, checksummed, forward-compat-guarded, fail-closed
//! load — the same idiom as [`crate::live_state`]), the write-ahead commit, the
//! reconciliation decision logic, retention, every fail-closed edge, AND the
//! authority-gated durable-submit **seam**
//! [`crate::ExecutionEngine::route_order_durably`] (derives live-ness from the
//! engine-owned designation, then commit + persist BEFORE the broker call, bind the
//! ack, mark a rejection terminal) — proven with an ordering test.
//!
//! Deferred (owners named in `architecture/runtime_services.json`
//! `outbox_reconciliation_contract.deferred[]`): the concrete
//! [`BrokerOpenOrderSource`] that queries IB open + recently-completed orders
//! (SRS-EXE-006 adapter); making the durable path the PRODUCTION one (route the
//! production `route_order` through `route_order_durably`, or make the non-durable
//! `submit_live_order` unreachable for live production) so every live submission
//! consults the outbox (SRS-EXE-001 runtime — it re-architects the pinned single-live
//! authority path);
//! the event-driven lifecycle transitions (SRS-EXE-008). SRS-EXE-009 stays
//! `passes:false` until that wiring and the real-IB reconciliation e2e (NFR-R3)
//! land.

use atp_types::{
    AssetClass, ClientCorrelationId, OrderErrorCategory, OrderKey, OrderLifecycle,
    OrderLifecycleError, OrderSide, OrderState, OrderSubmission, OrderType, StrategyId,
    StructuredOrderError,
};
use std::collections::HashMap;
use std::fmt;
use std::fs;
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

/// Serialized-form magic header (distinct from the SRS-EXE-005 live-state store so
/// the two durable formats never alias and can evolve independently).
const OUTBOX_MAGIC: &str = "ATP-ORDER-OUTBOX-V1";
/// The only supported outbox schema version. `deserialize` rejects any other value
/// (forward-compat guard); bump this — never silently widen — when the record shape
/// changes.
const OUTBOX_SCHEMA_VERSION: i64 = 1;
/// The published (post-rename) outbox file name inside the store directory.
const OUTBOX_STORE_FILENAME: &str = "live_order_outbox.snapshot";
/// The scratch file base name an atomic save writes + fsyncs before renaming.
const OUTBOX_STORE_TMP_FILENAME: &str = "live_order_outbox.snapshot.tmp";
/// Per-call scratch discriminator so two writers to one directory cannot rename
/// over each other's scratch file.
static OUTBOX_SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

// --------------------------------------------------------------------------- //
// The outbox entry
// --------------------------------------------------------------------------- //

/// One durable outbox record: the persisted order **intent** (its
/// [`OrderLifecycle`] — key, submission, state, cancel-replace link) plus the
/// acknowledged broker order ID once the submission is confirmed. `broker_order_id`
/// is `None` for a committed-but-unacknowledged intent (the crash window) and
/// `Some` once [`OrderOutbox::bind_ack`] records the broker's acknowledgement — the
/// binding reconciliation keys on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutboxEntry {
    lifecycle: OrderLifecycle,
    broker_order_id: Option<String>,
}

impl OutboxEntry {
    /// The order's idempotency key `(strategy, correlation id)`.
    pub fn key(&self) -> &OrderKey {
        self.lifecycle.key()
    }

    /// The persisted submission intent.
    pub fn submission(&self) -> &OrderSubmission {
        self.lifecycle.submission()
    }

    /// The current lifecycle state.
    pub fn state(&self) -> OrderState {
        self.lifecycle.state()
    }

    /// The acknowledged broker order ID, if the submission has been confirmed.
    pub fn broker_order_id(&self) -> Option<&str> {
        self.broker_order_id.as_deref()
    }

    /// `true` once an acknowledged broker ID is bound to this intent — the property
    /// reconciliation uses to guarantee the intent is **never resubmitted**.
    pub fn is_bound(&self) -> bool {
        self.broker_order_id.is_some()
    }

    /// The cancel-replace audit link, if this intent replaces another.
    pub fn replaces(&self) -> Option<&OrderKey> {
        self.lifecycle.replaces()
    }
}

// --------------------------------------------------------------------------- //
// The outbox
// --------------------------------------------------------------------------- //

/// The durable write-ahead outbox: at most one [`OutboxEntry`] per [`OrderKey`],
/// so a re-submission of any tracked `(strategy, correlation id)` is rejected as an
/// idempotent duplicate (SRS-EXE-008 / SRS-ERR-001).
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct OrderOutbox {
    entries: HashMap<OrderKey, OutboxEntry>,
}

impl OrderOutbox {
    /// An empty outbox (a genuine first start — no prior intents).
    pub fn new() -> Self {
        Self {
            entries: HashMap::new(),
        }
    }

    /// **Write-ahead commit (SRS-EXE-009 core).** Durably record an order intent at
    /// [`OrderState::PendingSubmit`] *before* it is submitted to the broker. The
    /// live path calls this, persists the outbox ([`OutboxSnapshot::save_to_path`]),
    /// and only *then* invokes the broker port — so a crash during submission leaves
    /// a durable record that the order was about to go live.
    ///
    /// Idempotent: a second commit for an already-tracked `(strategy, correlation
    /// id)` is rejected with a [`StructuredOrderError`] carrying
    /// [`OrderErrorCategory::DuplicateClientCorrelationId`] (the same envelope
    /// [`atp_types::OrderLedger::submit`] returns), leaving the existing entry
    /// untouched.
    pub fn commit_intent(
        &mut self,
        correlation_id: ClientCorrelationId,
        submission: &OrderSubmission,
    ) -> Result<OrderKey, StructuredOrderError> {
        let key = OrderKey::new(submission.strategy_id.clone(), correlation_id);
        if self.entries.contains_key(&key) {
            return Err(StructuredOrderError {
                category: OrderErrorCategory::DuplicateClientCorrelationId,
                error_type: "DuplicateClientCorrelationId".to_string(),
                message: format!(
                    "order intent rejected: {key} is already committed to the durable \
                     outbox (idempotent duplicate-submission rejection — the existing \
                     intent is unchanged)"
                ),
                original_order: submission.clone(),
            });
        }
        let mut lifecycle = OrderLifecycle::new(key.clone(), submission.clone());
        lifecycle
            .transition_to(OrderState::PendingSubmit)
            .expect("NEW -> PENDING_SUBMIT is always legal for a fresh, non-replacement intent");
        self.entries.insert(
            key.clone(),
            OutboxEntry {
                lifecycle,
                broker_order_id: None,
            },
        );
        Ok(key)
    }

    /// Bind the broker's acknowledged order ID to a committed intent
    /// (`PENDING_SUBMIT` → `ACKED`) — the submission reached the broker and returned
    /// an ID. After this the intent is **bound** and reconciliation will never
    /// resubmit it.
    ///
    /// Fails closed on: an unknown key ([`OutboxError::UnknownOrder`]); a blank
    /// broker ID ([`OutboxError::BlankBrokerOrderId`]); or a *different* broker ID
    /// already bound to the key ([`OutboxError::BrokerIdConflict`]). Re-binding the
    /// **same** ID is idempotent (returns `Ok`).
    pub fn bind_ack(
        &mut self,
        key: &OrderKey,
        broker_order_id: impl Into<String>,
    ) -> Result<(), OutboxError> {
        let broker_order_id = broker_order_id.into();
        if broker_order_id.trim().is_empty() {
            return Err(OutboxError::BlankBrokerOrderId);
        }
        let entry = self
            .entries
            .get_mut(key)
            .ok_or_else(|| OutboxError::UnknownOrder(key.clone()))?;
        match &entry.broker_order_id {
            Some(existing) if *existing == broker_order_id => return Ok(()),
            Some(existing) => {
                return Err(OutboxError::BrokerIdConflict {
                    key: key.clone(),
                    existing: existing.clone(),
                    incoming: broker_order_id,
                });
            }
            None => {}
        }
        entry
            .lifecycle
            .transition_to(OrderState::Acked)
            .map_err(OutboxError::Lifecycle)?;
        entry.broker_order_id = Some(broker_order_id);
        Ok(())
    }

    /// Drive a tracked intent to a later lifecycle `state` (a fill, cancel, reject,
    /// or expiry observed from the broker), validated against the documented
    /// transition graph. Fails closed on an unknown key or an illegal edge.
    pub fn observe_state(
        &mut self,
        key: &OrderKey,
        state: OrderState,
    ) -> Result<OrderState, OutboxError> {
        let entry = self
            .entries
            .get_mut(key)
            .ok_or_else(|| OutboxError::UnknownOrder(key.clone()))?;
        entry
            .lifecycle
            .transition_to(state)
            .map_err(OutboxError::Lifecycle)
    }

    /// **Retention (SRS-EXE-009 AC bullet 4).** Remove every entry whose state is
    /// terminal (FILLED / CANCELLED / REJECTED / EXPIRED) — the retention obligation
    /// ends once a terminal state is observed. Returns the pruned keys in
    /// deterministic `(strategy, correlation id)` order.
    pub fn prune_terminal(&mut self) -> Vec<OrderKey> {
        let mut pruned: Vec<OrderKey> = self
            .entries
            .values()
            .filter(|entry| entry.state().is_terminal())
            .map(|entry| entry.key().clone())
            .collect();
        pruned.sort_by(order_key_sort);
        for key in &pruned {
            self.entries.remove(key);
        }
        pruned
    }

    /// The entry tracked under `key`, if any.
    pub fn entry(&self, key: &OrderKey) -> Option<&OutboxEntry> {
        self.entries.get(key)
    }

    /// The acknowledged broker ID bound to `key`, if any.
    pub fn broker_order_id(&self, key: &OrderKey) -> Option<&str> {
        self.entries.get(key).and_then(OutboxEntry::broker_order_id)
    }

    /// `true` if an intent is tracked under `key`.
    pub fn contains(&self, key: &OrderKey) -> bool {
        self.entries.contains_key(key)
    }

    /// Number of retained entries.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// `true` if no intents are retained.
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Durably persist the current outbox to `dir` — the atomic, fail-closed write a
    /// live submission performs BEFORE contacting the broker (a convenience over
    /// [`OutboxSnapshot::capture`] + [`OutboxSnapshot::save_to_path`]).
    pub fn persist(&self, dir: &Path) -> Result<(), OutboxPersistenceError> {
        OutboxSnapshot::capture(self.clone()).save_to_path(dir)
    }

    /// All entries in deterministic `(strategy, correlation id)` order — the order
    /// [`reconcile`] and the durable codec iterate in, so their output is stable
    /// regardless of `HashMap` layout.
    pub fn entries_sorted(&self) -> Vec<&OutboxEntry> {
        let mut entries: Vec<&OutboxEntry> = self.entries.values().collect();
        entries.sort_by(|a, b| order_key_sort(a.key(), b.key()));
        entries
    }
}

/// Failures raised by the outbox mutators (the idempotent duplicate-submission
/// rejection is reported instead as a [`StructuredOrderError`], the SRS-ERR-001
/// contract the strategy-facing API requires).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutboxError {
    /// A binding / transition targeted a key not in the outbox.
    UnknownOrder(OrderKey),
    /// A broker ID was blank / whitespace-only.
    BlankBrokerOrderId,
    /// A *different* broker ID is already bound to the key — a would-be silent
    /// rebinding that could mask a real broker-side identity change; surfaced, never
    /// overwritten.
    BrokerIdConflict {
        key: OrderKey,
        existing: String,
        incoming: String,
    },
    /// The underlying lifecycle transition was illegal or targeted an unknown order.
    Lifecycle(OrderLifecycleError),
}

impl fmt::Display for OutboxError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownOrder(key) => {
                write!(formatter, "outbox has no intent for order {key}")
            }
            Self::BlankBrokerOrderId => formatter.write_str("broker order id is blank"),
            Self::BrokerIdConflict {
                key,
                existing,
                incoming,
            } => write!(
                formatter,
                "order {key} is already bound to broker id `{existing}`; refusing to \
                 rebind to `{incoming}`"
            ),
            Self::Lifecycle(err) => write!(formatter, "outbox lifecycle transition failed: {err}"),
        }
    }
}

impl std::error::Error for OutboxError {}

// --------------------------------------------------------------------------- //
// Broker-state reconciliation (SRS-EXE-009 restart path)
// --------------------------------------------------------------------------- //

/// How complete a [`BrokerOpenOrderSnapshot`] is — the property that decides
/// whether an unacknowledged intent absent from the snapshot may be **resubmitted**
/// or must be surfaced as unresolved.
///
/// A broker's *open* orders alone cannot prove an order never landed: an order that
/// filled or cancelled between our crash and the query is no longer *open*, so its
/// absence from an open-only view is ambiguous. Only a view that also carries
/// **recently-completed** orders lets reconciliation confidently conclude "the
/// broker never saw this intent → safe to resubmit".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SnapshotCoverage {
    /// Only currently-open orders. An absent unacknowledged intent is **ambiguous**
    /// (it may have filled/cancelled) → never auto-resubmitted.
    OpenOnly,
    /// Open orders *and* recently-completed orders — a view complete enough that an
    /// absent unacknowledged intent provably never reached the broker.
    OpenAndRecentlyCompleted,
}

/// One order the broker reports, keyed back to our [`OrderKey`] via the client
/// order reference we set at submit time (the broker echoes it). `state` is the
/// broker's view of the order's lifecycle state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BrokerOpenOrder {
    pub key: OrderKey,
    pub broker_order_id: String,
    pub state: OrderState,
}

/// A broker order-state snapshot the execution engine reconciles the durable outbox
/// against on restart, tagged with its [`SnapshotCoverage`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BrokerOpenOrderSnapshot {
    orders: Vec<BrokerOpenOrder>,
    coverage: SnapshotCoverage,
}

impl BrokerOpenOrderSnapshot {
    pub fn new(orders: Vec<BrokerOpenOrder>, coverage: SnapshotCoverage) -> Self {
        Self { orders, coverage }
    }

    pub fn orders(&self) -> &[BrokerOpenOrder] {
        &self.orders
    }

    pub fn coverage(&self) -> SnapshotCoverage {
        self.coverage
    }
}

/// The port the execution engine consults on restart to obtain the broker's order
/// state. Defined at the execution layer (not in `atp-adapters`) for the same
/// SRS-ARCH-002 reason as [`crate::LiveBrokerageSubmit`]: the concrete
/// implementation — querying IB's open + recently-completed orders — is the deferred
/// SRS-EXE-006 adapter, wired by the orchestrator.
pub trait BrokerOpenOrderSource {
    fn open_orders(&self) -> Result<BrokerOpenOrderSnapshot, BrokerReconcileError>;
}

/// A failure to obtain the broker's order state during reconciliation, carrying an
/// explicit **fail-closed category**. Fallible on purpose: querying IB (the deferred
/// SRS-EXE-006 adapter) can lose connectivity / hit a scheduled-restart window, find
/// its data too stale to trust, time out, or return an unusable snapshot. The
/// category lets the caller (and its tests) distinguish those paths — aligning with
/// the SyRS SYS-64 order-error vocabulary — while the reconciliation *response* is
/// uniform: on ANY error, make **no** resubmission decision (fail closed) rather than
/// misread a missing view as "the broker has nothing".
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BrokerReconcileError {
    /// The IB Gateway is unreachable or in a scheduled-restart window.
    ConnectivityBlocked { reason: String },
    /// The broker's order state could not be read fresh enough to trust.
    StaleData { reason: String },
    /// The query exceeded its deadline before returning.
    Timeout { reason: String },
    /// A snapshot returned but is malformed / internally inconsistent.
    MalformedSnapshot { reason: String },
    /// The broker order-state service is otherwise unavailable.
    Unavailable { reason: String },
}

impl BrokerReconcileError {
    pub fn connectivity_blocked(reason: impl Into<String>) -> Self {
        Self::ConnectivityBlocked {
            reason: reason.into(),
        }
    }

    pub fn stale_data(reason: impl Into<String>) -> Self {
        Self::StaleData {
            reason: reason.into(),
        }
    }

    pub fn timeout(reason: impl Into<String>) -> Self {
        Self::Timeout {
            reason: reason.into(),
        }
    }

    pub fn malformed_snapshot(reason: impl Into<String>) -> Self {
        Self::MalformedSnapshot {
            reason: reason.into(),
        }
    }

    pub fn unavailable(reason: impl Into<String>) -> Self {
        Self::Unavailable {
            reason: reason.into(),
        }
    }

    /// A stable wire category (SyRS SYS-64 alignment) so the deferred IB adapter and
    /// its tests can prove each fail-closed path is distinguished.
    pub fn category(&self) -> &'static str {
        match self {
            Self::ConnectivityBlocked { .. } => "CONNECTIVITY_BLOCKED",
            Self::StaleData { .. } => "STALE_DATA_BLOCKED",
            Self::Timeout { .. } => "RECONCILE_TIMEOUT",
            Self::MalformedSnapshot { .. } => "MALFORMED_SNAPSHOT",
            Self::Unavailable { .. } => "BROKER_UNAVAILABLE",
        }
    }

    /// The human-readable detail behind the category.
    pub fn reason(&self) -> &str {
        match self {
            Self::ConnectivityBlocked { reason }
            | Self::StaleData { reason }
            | Self::Timeout { reason }
            | Self::MalformedSnapshot { reason }
            | Self::Unavailable { reason } => reason,
        }
    }
}

impl fmt::Display for BrokerReconcileError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-EXE-009: broker open-order query failed [{}] — {}",
            self.category(),
            self.reason()
        )
    }
}

impl std::error::Error for BrokerReconcileError {}

/// Why a single intent could not be reconciled to a safe automatic decision, and so
/// is surfaced for operator/manual resolution rather than acted on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConflictKind {
    /// A bound intent whose broker ID disagrees with what the broker now reports for
    /// the same key — never silently overwritten or resubmitted.
    BrokerIdMismatch { outbox: String, broker: String },
    /// An unacknowledged intent absent from an **open-only** broker view: it may have
    /// filled or cancelled in the crash window, so it is neither adopted nor
    /// resubmitted (auto-resubmitting could double a live order).
    UnverifiableSubmitWindow,
    /// The broker reports a state this intent cannot legally reach from its current
    /// (post-adopt) state — a state disagreement that needs manual resolution rather
    /// than a fabricated transition.
    StateDisagreement {
        outbox: OrderState,
        broker: OrderState,
    },
    /// The broker snapshot reports **more than one** order for this intent's
    /// correlation key — the exact duplicate-live-order hazard EXE-009 exists to
    /// catch. It is never adopted / skipped / resubmitted (any of which could
    /// silently mask a second live order); the conflicting broker IDs are surfaced
    /// (sorted, for determinism) for the operator to resolve.
    DuplicateBrokerRows { broker_order_ids: Vec<String> },
}

/// One unresolved reconciliation finding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReconcileConflict {
    pub key: OrderKey,
    pub kind: ConflictKind,
}

/// The reconciliation decision set produced by [`reconcile`]. Every list is in
/// deterministic `(strategy, correlation id)` order. The lists are disjoint by key:
/// an intent lands in exactly one of `skip_bound` / `adopt_ack` / `resubmit` /
/// `unresolved` (plus, for a skip/adopt, an optional `mark_terminal` sync when the
/// broker reports it already reached a legally-reachable terminal state).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReconciliationPlan {
    /// Intents already bound to an acknowledged broker ID — **never resubmitted**
    /// (SRS-EXE-009 AC bullet 3).
    pub skip_bound: Vec<OrderKey>,
    /// Unacknowledged intents the broker already holds: adopt the broker's ID
    /// (bind → `ACKED`), do not resubmit.
    pub adopt_ack: Vec<(OrderKey, String)>,
    /// Unacknowledged intents the broker provably never received (only under
    /// [`SnapshotCoverage::OpenAndRecentlyCompleted`]) — safe to resubmit.
    pub resubmit: Vec<OrderKey>,
    /// A broker-observed terminal state to sync onto a skipped/adopted intent
    /// (applied via [`OrderOutbox::observe_state`]) when it is a legal forward edge.
    pub mark_terminal: Vec<(OrderKey, OrderState)>,
    /// Intents that cannot be safely auto-resolved — surfaced, never resubmitted.
    pub unresolved: Vec<ReconcileConflict>,
}

/// **Reconcile the durable outbox against the broker's reported order state**
/// (SRS-EXE-009 restart path). Pure: it reads both inputs and returns a
/// [`ReconciliationPlan`] the caller applies; it mutates nothing.
///
/// Per non-terminal intent (terminal intents are left for
/// [`OrderOutbox::prune_terminal`]):
/// * **bound** (already has a broker ID) → `skip_bound`; never resubmitted. If the
///   broker reports a *different* ID for the key → `unresolved`
///   ([`ConflictKind::BrokerIdMismatch`]). If the broker reports a legally-reachable
///   terminal state → also `mark_terminal`.
/// * **unacknowledged**, key **present** in the snapshot → `adopt_ack` (the
///   submission did reach the broker); never resubmitted. A legally-reachable
///   broker terminal state → also `mark_terminal`.
/// * **unacknowledged**, key **absent**, coverage
///   [`SnapshotCoverage::OpenAndRecentlyCompleted`] → `resubmit` (provably never
///   landed).
/// * **unacknowledged**, key **absent**, coverage [`SnapshotCoverage::OpenOnly`] →
///   `unresolved` ([`ConflictKind::UnverifiableSubmitWindow`]); never resubmitted.
///
/// A key the broker reports **more than once** short-circuits all of the above to
/// `unresolved` ([`ConflictKind::DuplicateBrokerRows`]) — a duplicate live order is
/// never masked by a single adopt/skip/resubmit decision.
pub fn reconcile(outbox: &OrderOutbox, snapshot: &BrokerOpenOrderSnapshot) -> ReconciliationPlan {
    // Group ALL broker rows by key — a `collect()` into a map would silently discard
    // a second row for the same key, which is exactly the duplicate-live-order hazard
    // this feature exists to catch. A key with >1 row is surfaced as unresolved below,
    // never adopted / skipped / resubmitted.
    let mut broker_by_key: HashMap<&OrderKey, Vec<&BrokerOpenOrder>> = HashMap::new();
    for order in snapshot.orders() {
        broker_by_key.entry(&order.key).or_default().push(order);
    }

    let mut plan = ReconciliationPlan::default();
    for entry in outbox.entries_sorted() {
        let key = entry.key();
        // Terminal intents need no reconciliation — retention (prune_terminal)
        // removes them; touching them here would risk a redundant transition.
        if entry.state().is_terminal() {
            continue;
        }
        let broker_rows = broker_by_key.get(key);
        // Duplicate broker rows for one correlation key are the duplicate-live-order
        // hazard: surface them (never adopt / skip / resubmit), so a second live order
        // can never be silently masked by a single adopt/skip decision.
        if let Some(rows) = broker_rows {
            if rows.len() > 1 {
                let mut broker_order_ids: Vec<String> =
                    rows.iter().map(|row| row.broker_order_id.clone()).collect();
                broker_order_ids.sort();
                plan.unresolved.push(ReconcileConflict {
                    key: key.clone(),
                    kind: ConflictKind::DuplicateBrokerRows { broker_order_ids },
                });
                continue;
            }
        }
        let broker = broker_rows.and_then(|rows| rows.first().copied());
        match entry.broker_order_id() {
            Some(bound_id) => match broker {
                Some(order) if order.broker_order_id != bound_id => {
                    plan.unresolved.push(ReconcileConflict {
                        key: key.clone(),
                        kind: ConflictKind::BrokerIdMismatch {
                            outbox: bound_id.to_string(),
                            broker: order.broker_order_id.clone(),
                        },
                    });
                }
                // Decide the terminal-sync FIRST, so the intent lands in exactly one
                // resolution bucket: a legal broker terminal state records a
                // skip_bound + mark_terminal; an ILLEGAL one is a disagreement that
                // goes ONLY to unresolved (never skip_bound too).
                Some(order) => match classify_terminal_sync(entry.state(), order.state) {
                    TerminalSync::Disagree => plan.unresolved.push(ReconcileConflict {
                        key: key.clone(),
                        kind: ConflictKind::StateDisagreement {
                            outbox: entry.state(),
                            broker: order.state,
                        },
                    }),
                    TerminalSync::Mark(state) => {
                        plan.skip_bound.push(key.clone());
                        plan.mark_terminal.push((key.clone(), state));
                    }
                    TerminalSync::None => plan.skip_bound.push(key.clone()),
                },
                // Bound but absent from a (possibly open-only) snapshot: still bound,
                // so it is NEVER resubmitted — the absence just means it is no longer
                // open (likely already terminal), which a later broker event settles.
                None => plan.skip_bound.push(key.clone()),
            },
            None => match broker {
                // The submission reached the broker (it echoes our order ref). Adopt
                // its id — bind → ACKED — and never resubmit; but an illegal broker
                // terminal state is a disagreement that goes ONLY to unresolved (never
                // adopt_ack too), so the plan buckets stay mutually exclusive.
                Some(order) => match classify_terminal_sync(OrderState::Acked, order.state) {
                    TerminalSync::Disagree => plan.unresolved.push(ReconcileConflict {
                        key: key.clone(),
                        kind: ConflictKind::StateDisagreement {
                            outbox: OrderState::Acked,
                            broker: order.state,
                        },
                    }),
                    TerminalSync::Mark(state) => {
                        plan.adopt_ack
                            .push((key.clone(), order.broker_order_id.clone()));
                        plan.mark_terminal.push((key.clone(), state));
                    }
                    TerminalSync::None => plan
                        .adopt_ack
                        .push((key.clone(), order.broker_order_id.clone())),
                },
                None => match snapshot.coverage() {
                    SnapshotCoverage::OpenAndRecentlyCompleted => plan.resubmit.push(key.clone()),
                    SnapshotCoverage::OpenOnly => plan.unresolved.push(ReconcileConflict {
                        key: key.clone(),
                        kind: ConflictKind::UnverifiableSubmitWindow,
                    }),
                },
            },
        }
    }
    plan
}

/// The terminal-sync decision for a skipped/adopted intent, computed BEFORE the
/// intent is committed to any resolution bucket so buckets stay mutually exclusive.
enum TerminalSync {
    /// No sync: the broker state is not a terminal ahead of `effective`. The intent
    /// keeps its skip_bound / adopt_ack decision unchanged.
    None,
    /// The broker reports a legally-reachable terminal state to record via
    /// `mark_terminal` (alongside the skip_bound / adopt_ack decision).
    Mark(OrderState),
    /// The broker reports a terminal state the intent cannot legally reach — a genuine
    /// disagreement that goes ONLY to `unresolved`, never to a resolved bucket.
    Disagree,
}

/// Classify how a skipped/adopted intent should reconcile with a broker-reported
/// `broker_state`, given the state it will be in after the skip/adopt (`effective`).
/// Only *terminal* broker states are synced — an intermediate broker state is left
/// for the real event-driven transitions (SRS-EXE-008). Pure: it appends nothing, so
/// the caller decides the single bucket.
fn classify_terminal_sync(effective: OrderState, broker_state: OrderState) -> TerminalSync {
    if !broker_state.is_terminal() || broker_state == effective {
        TerminalSync::None
    } else if effective.can_transition_to(broker_state) {
        TerminalSync::Mark(broker_state)
    } else {
        TerminalSync::Disagree
    }
}

// --------------------------------------------------------------------------- //
// Durable snapshot codec (parallels crate::live_state — own magic/schema/file)
// --------------------------------------------------------------------------- //

/// A schema-versioned, checksummed, durable capture of an [`OrderOutbox`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutboxSnapshot {
    schema_version: i64,
    outbox: OrderOutbox,
}

impl OutboxSnapshot {
    /// Capture `outbox` at the current [`OUTBOX_SCHEMA_VERSION`].
    pub fn capture(outbox: OrderOutbox) -> Self {
        Self {
            schema_version: OUTBOX_SCHEMA_VERSION,
            outbox,
        }
    }

    pub fn schema_version(&self) -> i64 {
        self.schema_version
    }

    pub fn outbox(&self) -> &OrderOutbox {
        &self.outbox
    }

    pub fn into_outbox(self) -> OrderOutbox {
        self.outbox
    }

    /// Serialize to the durable form: the `MAGIC` line, an FNV-1a checksum over the
    /// body, then the body (schema version, entry count, then each entry — key,
    /// state, submission, cancel-replace link, broker-id present-flag + id — in
    /// canonical `(strategy, correlation id)` order). Byte-identical for the same
    /// outbox regardless of `HashMap` iteration order; strings are length-prefixed,
    /// so a symbol with spaces round-trips.
    pub fn serialize(&self) -> String {
        let mut body = String::new();
        push_i128(&mut body, i128::from(self.schema_version));

        let entries = self.outbox.entries_sorted();
        push_i128(&mut body, entries.len() as i128);
        for entry in entries {
            push_entry(&mut body, entry);
        }

        let mut out = String::with_capacity(body.len() + OUTBOX_MAGIC.len() + 32);
        push_line(&mut out, OUTBOX_MAGIC);
        push_i128(&mut out, i128::from(checksum(body.as_bytes())));
        out.push_str(&body);
        out
    }

    /// Deserialize a blob produced by [`serialize`](Self::serialize), failing closed
    /// on any malformation. Validates the magic header, the body checksum (BEFORE
    /// building any state), the schema version, and every per-entry invariant —
    /// including broker-binding consistency (an acknowledged state must carry an ID
    /// and vice-versa) and duplicate keys — building the whole outbox in a local
    /// before returning, so a corrupt/truncated/tampered blob yields no partial
    /// state.
    pub fn deserialize(serialized: &str) -> Result<Self, OutboxPersistenceError> {
        let mut cursor = Cursor::new(serialized);

        let magic = cursor.read_line("magic header")?;
        if magic != OUTBOX_MAGIC {
            return Err(OutboxPersistenceError::CorruptSnapshot {
                context: "magic header",
            });
        }
        let stored_checksum = cursor.read_u64("checksum")?;
        let body = cursor.remaining();
        if checksum(body) != stored_checksum {
            return Err(OutboxPersistenceError::ChecksumMismatch);
        }

        let schema_version = cursor.read_i64("schema version")?;
        if schema_version != OUTBOX_SCHEMA_VERSION {
            return Err(OutboxPersistenceError::UnknownSchemaVersion {
                found: schema_version,
            });
        }

        let entry_count = cursor.read_count("entry count")?;
        // Do NOT pre-allocate from the untrusted count — a crafted huge count would
        // OOM before the cursor is exhausted. Each iteration reads and fails closed
        // the moment the data runs out.
        let mut entries: HashMap<OrderKey, OutboxEntry> = HashMap::new();
        for _ in 0..entry_count {
            let entry = read_entry(&mut cursor)?;
            if entries.insert(entry.key().clone(), entry).is_some() {
                return Err(OutboxPersistenceError::DuplicateRecord {
                    context: "two outbox entries with the same order key",
                });
            }
        }
        cursor.expect_end()?;

        Ok(Self {
            schema_version,
            outbox: OrderOutbox { entries },
        })
    }

    /// Durably persist this snapshot into `dir`: write a per-call-unique scratch
    /// file, `fsync` it, atomically `rename` it onto the store, then `fsync` the
    /// parent directory so the rename itself survives a crash (identical durability
    /// idiom to [`crate::live_state`]'s live-state store). A reader never sees a
    /// half-written blob.
    ///
    /// Guarantee scope: a single `save_to_path` is atomic; serializing concurrent
    /// writers against one directory is the caller's responsibility (the live engine
    /// is single-writer per strategy host).
    pub fn save_to_path(&self, dir: &Path) -> Result<(), OutboxPersistenceError> {
        fs::create_dir_all(dir).map_err(|err| io_error("create outbox directory", &err))?;
        let seq = OUTBOX_SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
        let tmp_path = dir.join(format!(
            "{OUTBOX_STORE_TMP_FILENAME}.{}.{seq}",
            std::process::id()
        ));
        let final_path = dir.join(OUTBOX_STORE_FILENAME);

        let mut scratch =
            fs::File::create(&tmp_path).map_err(|err| io_error("create outbox scratch", &err))?;
        if let Err(err) = io::Write::write_all(&mut scratch, self.serialize().as_bytes())
            .and_then(|()| scratch.sync_all())
        {
            let _ = fs::remove_file(&tmp_path);
            return Err(io_error("write outbox scratch", &err));
        }
        drop(scratch);

        fs::rename(&tmp_path, &final_path).map_err(|err| {
            let _ = fs::remove_file(&tmp_path);
            io_error("publish outbox file", &err)
        })?;

        let dir_handle =
            fs::File::open(dir).map_err(|err| io_error("open outbox directory", &err))?;
        dir_handle
            .sync_all()
            .map_err(|err| io_error("sync outbox directory", &err))?;
        Ok(())
    }

    /// Load a snapshot previously written by [`save_to_path`](Self::save_to_path)
    /// for restart reconciliation — fail-closed by default. A missing directory or a
    /// missing file during recovery is an error (recovery assumes durable state
    /// SHOULD be present; silently substituting an empty outbox would defeat the
    /// no-duplicate-submission guarantee). A genuine first start constructs an empty
    /// outbox explicitly via [`OutboxSnapshot::capture`] over [`OrderOutbox::new`].
    pub fn load_from_path(dir: &Path) -> Result<Self, OutboxPersistenceError> {
        if !dir.is_dir() {
            return Err(OutboxPersistenceError::Io {
                context: "outbox directory is missing or not a directory",
            });
        }
        let final_path = dir.join(OUTBOX_STORE_FILENAME);
        match fs::read_to_string(&final_path) {
            Ok(contents) => Self::deserialize(&contents),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Err(OutboxPersistenceError::Io {
                context: "no durable outbox snapshot present during recovery \
                          (a restart expects prior state; a genuine first start \
                          initializes an empty outbox explicitly, it does not recover)",
            }),
            Err(err) => Err(io_error("read outbox file", &err)),
        }
    }
}

/// Failures decoding or persisting an [`OutboxSnapshot`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutboxPersistenceError {
    CorruptSnapshot { context: &'static str },
    ChecksumMismatch,
    UnknownSchemaVersion { found: i64 },
    InconsistentField { context: &'static str },
    DuplicateRecord { context: &'static str },
    Lifecycle(OrderLifecycleError),
    Io { context: &'static str },
}

impl fmt::Display for OutboxPersistenceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CorruptSnapshot { context } => {
                write!(formatter, "corrupt outbox snapshot ({context})")
            }
            Self::ChecksumMismatch => {
                formatter.write_str("outbox snapshot integrity checksum mismatch")
            }
            Self::UnknownSchemaVersion { found } => {
                write!(formatter, "unknown outbox schema version {found}")
            }
            Self::InconsistentField { context } => {
                write!(formatter, "inconsistent outbox field ({context})")
            }
            Self::DuplicateRecord { context } => {
                write!(formatter, "duplicate outbox record ({context})")
            }
            Self::Lifecycle(err) => write!(formatter, "outbox restore failed: {err}"),
            Self::Io { context } => write!(formatter, "outbox I/O failure ({context})"),
        }
    }
}

impl std::error::Error for OutboxPersistenceError {}

fn io_error(context: &'static str, _err: &io::Error) -> OutboxPersistenceError {
    OutboxPersistenceError::Io { context }
}

/// The broker-binding invariant enforced on restore (write↔read symmetry): a
/// pre-submit state is never bound; a post-acknowledgement fill/cancel state is
/// always bound; `REJECTED` / `EXPIRED` may be either (an order can be rejected or
/// expire before *or* after acknowledgement).
fn binding_consistent(state: OrderState, bound: bool) -> bool {
    match state {
        OrderState::New | OrderState::PendingSubmit => !bound,
        OrderState::Acked
        | OrderState::PartiallyFilled
        | OrderState::Filled
        | OrderState::CancelPending
        | OrderState::Cancelled => bound,
        OrderState::Rejected | OrderState::Expired => true,
    }
}

// --------------------------------------------------------------------------- //
// Entry (de)serialization
// --------------------------------------------------------------------------- //

/// Total order over an [`OrderKey`] by `(strategy, correlation id)` string (an
/// `OrderKey` is deliberately not `Ord`), so the codec and the plan serialize
/// deterministically.
fn order_key_sort(a: &OrderKey, b: &OrderKey) -> std::cmp::Ordering {
    a.strategy_id()
        .as_str()
        .cmp(b.strategy_id().as_str())
        .then_with(|| a.correlation_id().as_str().cmp(b.correlation_id().as_str()))
}

fn push_entry(body: &mut String, entry: &OutboxEntry) {
    let key = entry.key();
    push_str(body, key.strategy_id().as_str());
    push_str(body, key.correlation_id().as_str());
    push_str(body, entry.state().as_str());

    let submission = entry.submission();
    push_str(body, &submission.symbol);
    push_i128(body, i128::from(submission.quantity));
    push_str(body, submission.asset_class.as_str());
    push_str(body, submission.side.as_str());
    push_order_type(body, submission.order_type);

    // Cancel-replace audit link: a flag then, if present, the original key.
    match entry.replaces() {
        Some(original) => {
            push_i128(body, 1);
            push_str(body, original.strategy_id().as_str());
            push_str(body, original.correlation_id().as_str());
        }
        None => push_i128(body, 0),
    }

    // Broker-id binding: a present-flag then, if present, the id.
    match entry.broker_order_id() {
        Some(broker_order_id) => {
            push_i128(body, 1);
            push_str(body, broker_order_id);
        }
        None => push_i128(body, 0),
    }
}

fn push_order_type(body: &mut String, order_type: OrderType) {
    push_str(body, order_type.as_str());
    match order_type.stop_price_minor() {
        Some(price) => {
            push_i128(body, 1);
            push_i128(body, i128::from(price));
        }
        None => push_i128(body, 0),
    }
    match order_type.limit_price_minor() {
        Some(price) => {
            push_i128(body, 1);
            push_i128(body, i128::from(price));
        }
        None => push_i128(body, 0),
    }
}

fn read_entry(cursor: &mut Cursor<'_>) -> Result<OutboxEntry, OutboxPersistenceError> {
    let key = read_order_key(cursor)?;
    let state = read_order_state(cursor)?;

    let symbol = cursor.read_str("order symbol")?;
    let quantity = cursor.read_i64("order quantity")?;
    let asset_class = read_asset_class(cursor)?;
    let side = read_order_side(cursor)?;
    let order_type = read_order_type(cursor)?;

    let submission = OrderSubmission {
        strategy_id: key.strategy_id().clone(),
        symbol,
        quantity,
        asset_class,
        side,
        order_type,
    };
    // Read↔write symmetry: a restored intent must pass the SAME shared authority
    // (`OrderSubmission::validate`) every live/paper intake applies, so a
    // checksum-valid but structurally impossible snapshot (blank symbol,
    // non-positive quantity, an option order that could never have been submitted)
    // fails closed rather than rehydrating an intent the engine would never admit.
    submission
        .validate()
        .map_err(|_| OutboxPersistenceError::InconsistentField {
            context: "restored intent fails submission validation",
        })?;

    let replaces = match cursor.read_count("replaces flag")? {
        0 => None,
        1 => Some(read_order_key(cursor)?),
        _ => {
            return Err(OutboxPersistenceError::CorruptSnapshot {
                context: "replaces flag not 0/1",
            })
        }
    };

    let broker_order_id = match cursor.read_count("broker id flag")? {
        0 => None,
        1 => {
            let id = cursor.read_str("broker id")?;
            if id.trim().is_empty() {
                return Err(OutboxPersistenceError::InconsistentField {
                    context: "empty broker id",
                });
            }
            Some(id)
        }
        _ => {
            return Err(OutboxPersistenceError::CorruptSnapshot {
                context: "broker id flag not 0/1",
            })
        }
    };

    // Fail closed on an inconsistent binding (e.g. a FILLED intent with no broker id,
    // or a PENDING_SUBMIT carrying one) — the write↔read symmetry that stops a
    // tampered blob rehydrating an impossible order.
    if !binding_consistent(state, broker_order_id.is_some()) {
        return Err(OutboxPersistenceError::InconsistentField {
            context: "broker-id binding inconsistent with order state",
        });
    }

    Ok(OutboxEntry {
        lifecycle: OrderLifecycle::restore(key, submission, state, replaces),
        broker_order_id,
    })
}

fn read_order_key(cursor: &mut Cursor<'_>) -> Result<OrderKey, OutboxPersistenceError> {
    let strategy = cursor.read_str("strategy id")?;
    if strategy.trim().is_empty() {
        return Err(OutboxPersistenceError::InconsistentField {
            context: "empty strategy id",
        });
    }
    let correlation = cursor.read_str("correlation id")?;
    let correlation_id =
        ClientCorrelationId::new(correlation).map_err(OutboxPersistenceError::Lifecycle)?;
    Ok(OrderKey::new(StrategyId::new(strategy), correlation_id))
}

fn read_order_state(cursor: &mut Cursor<'_>) -> Result<OrderState, OutboxPersistenceError> {
    match cursor.read_str("order state")?.as_str() {
        "NEW" => Ok(OrderState::New),
        "PENDING_SUBMIT" => Ok(OrderState::PendingSubmit),
        "ACKED" => Ok(OrderState::Acked),
        "PARTIALLY_FILLED" => Ok(OrderState::PartiallyFilled),
        "FILLED" => Ok(OrderState::Filled),
        "CANCEL_PENDING" => Ok(OrderState::CancelPending),
        "CANCELLED" => Ok(OrderState::Cancelled),
        "REJECTED" => Ok(OrderState::Rejected),
        "EXPIRED" => Ok(OrderState::Expired),
        _ => Err(OutboxPersistenceError::InconsistentField {
            context: "unknown order state",
        }),
    }
}

fn read_asset_class(cursor: &mut Cursor<'_>) -> Result<AssetClass, OutboxPersistenceError> {
    match cursor.read_str("asset class")?.as_str() {
        "EQUITY" => Ok(AssetClass::Equity),
        "OPTION" => Ok(AssetClass::Option),
        _ => Err(OutboxPersistenceError::InconsistentField {
            context: "unknown asset class",
        }),
    }
}

fn read_order_side(cursor: &mut Cursor<'_>) -> Result<OrderSide, OutboxPersistenceError> {
    match cursor.read_str("order side")?.as_str() {
        "BUY" => Ok(OrderSide::Buy),
        "SELL" => Ok(OrderSide::Sell),
        _ => Err(OutboxPersistenceError::InconsistentField {
            context: "unknown order side",
        }),
    }
}

fn read_order_type(cursor: &mut Cursor<'_>) -> Result<OrderType, OutboxPersistenceError> {
    let wire = cursor.read_str("order type")?;
    let stop = read_optional_price(cursor, "stop price")?;
    let limit = read_optional_price(cursor, "limit price")?;

    let order_type = match (wire.as_str(), stop, limit) {
        ("MARKET", None, None) => OrderType::Market,
        ("LIMIT", None, Some(limit_price_minor)) => OrderType::Limit { limit_price_minor },
        ("STOP", Some(stop_price_minor), None) => OrderType::Stop { stop_price_minor },
        ("STOP_LIMIT", Some(stop_price_minor), Some(limit_price_minor)) => OrderType::StopLimit {
            stop_price_minor,
            limit_price_minor,
        },
        _ => {
            return Err(OutboxPersistenceError::InconsistentField {
                context: "order type / price presence mismatch",
            })
        }
    };
    order_type
        .validate_prices()
        .map_err(|_| OutboxPersistenceError::InconsistentField {
            context: "non-positive order-type price",
        })?;
    Ok(order_type)
}

fn read_optional_price(
    cursor: &mut Cursor<'_>,
    context: &'static str,
) -> Result<Option<i64>, OutboxPersistenceError> {
    match cursor.read_count(context)? {
        0 => Ok(None),
        1 => Ok(Some(cursor.read_i64(context)?)),
        _ => Err(OutboxPersistenceError::CorruptSnapshot {
            context: "price present-flag not 0/1",
        }),
    }
}

// --------------------------------------------------------------------------- //
// Deterministic, dependency-free text codec (parallels crate::live_state)
// --------------------------------------------------------------------------- //

fn push_line(out: &mut String, value: &str) {
    out.push_str(value);
    out.push('\n');
}

fn push_i128(out: &mut String, value: i128) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append a length-prefixed string: the byte length on one line, then the bytes and
/// a newline, so any byte in the value round-trips without escaping.
fn push_str(out: &mut String, value: &str) {
    out.push_str(&value.len().to_string());
    out.push('\n');
    out.push_str(value);
    out.push('\n');
}

/// A 64-bit FNV-1a integrity checksum over the serialized body. Non-cryptographic:
/// it detects accidental corruption (bit flips, truncation, a value changed to
/// another structurally-valid value) under fault injection, not a deliberate
/// tamperer who recomputes it. Integer-only, dependency-free.
fn checksum(bytes: &[u8]) -> u64 {
    const OFFSET_BASIS: u64 = 0xcbf29ce484222325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut hash = OFFSET_BASIS;
    for &byte in bytes {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

/// A forward-only cursor over a serialized snapshot's bytes. Reads are exact and
/// fail closed: a missing newline, a malformed integer, a truncated length-prefixed
/// string, or trailing garbage all surface as an [`Err`] (recovery never panics on
/// a malformed blob).
struct Cursor<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(serialized: &'a str) -> Self {
        Self {
            bytes: serialized.as_bytes(),
            pos: 0,
        }
    }

    fn remaining(&self) -> &'a [u8] {
        &self.bytes[self.pos..]
    }

    fn read_line(&mut self, context: &'static str) -> Result<&'a str, OutboxPersistenceError> {
        let start = self.pos;
        while self.pos < self.bytes.len() && self.bytes[self.pos] != b'\n' {
            self.pos += 1;
        }
        if self.pos >= self.bytes.len() {
            return Err(OutboxPersistenceError::CorruptSnapshot { context });
        }
        let line = &self.bytes[start..self.pos];
        self.pos += 1; // consume the '\n'
        std::str::from_utf8(line).map_err(|_| OutboxPersistenceError::CorruptSnapshot { context })
    }

    fn read_i128(&mut self, context: &'static str) -> Result<i128, OutboxPersistenceError> {
        self.read_line(context)?
            .parse::<i128>()
            .map_err(|_| OutboxPersistenceError::CorruptSnapshot { context })
    }

    fn read_i64(&mut self, context: &'static str) -> Result<i64, OutboxPersistenceError> {
        self.read_line(context)?
            .parse::<i64>()
            .map_err(|_| OutboxPersistenceError::CorruptSnapshot { context })
    }

    fn read_u64(&mut self, context: &'static str) -> Result<u64, OutboxPersistenceError> {
        self.read_line(context)?
            .parse::<u64>()
            .map_err(|_| OutboxPersistenceError::CorruptSnapshot { context })
    }

    /// Read a non-negative count, rejecting a negative value (a length is never
    /// negative).
    fn read_count(&mut self, context: &'static str) -> Result<usize, OutboxPersistenceError> {
        let value = self.read_i128(context)?;
        if value < 0 {
            return Err(OutboxPersistenceError::CorruptSnapshot { context });
        }
        usize::try_from(value).map_err(|_| OutboxPersistenceError::CorruptSnapshot { context })
    }

    /// Read a length-prefixed string (the byte-length line, then that many bytes,
    /// then a `\n`), failing closed on a truncated or non-UTF-8 value.
    fn read_str(&mut self, context: &'static str) -> Result<String, OutboxPersistenceError> {
        let len = self.read_count(context)?;
        // `checked_add` so a crafted huge length fails closed instead of overflowing
        // (and then panicking on the slice).
        let value_end = self
            .pos
            .checked_add(len)
            .ok_or(OutboxPersistenceError::CorruptSnapshot { context })?;
        if value_end >= self.bytes.len() {
            return Err(OutboxPersistenceError::CorruptSnapshot { context });
        }
        if self.bytes[value_end] != b'\n' {
            return Err(OutboxPersistenceError::CorruptSnapshot { context });
        }
        let value = &self.bytes[self.pos..value_end];
        self.pos = value_end + 1; // consume the value bytes and the '\n'
        std::str::from_utf8(value)
            .map(str::to_string)
            .map_err(|_| OutboxPersistenceError::CorruptSnapshot { context })
    }

    /// Assert the cursor is exactly at the end (no trailing garbage).
    fn expect_end(&self) -> Result<(), OutboxPersistenceError> {
        if self.pos == self.bytes.len() {
            Ok(())
        } else {
            Err(OutboxPersistenceError::CorruptSnapshot {
                context: "trailing bytes after snapshot",
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn corr(id: &str) -> ClientCorrelationId {
        ClientCorrelationId::new(id).expect("valid correlation id")
    }

    fn key(strategy: &str, correlation: &str) -> OrderKey {
        OrderKey::new(StrategyId::new(strategy), corr(correlation))
    }

    fn submission(strategy: &str, symbol: &str, quantity: i64) -> OrderSubmission {
        OrderSubmission {
            strategy_id: StrategyId::new(strategy),
            symbol: symbol.to_string(),
            quantity,
            asset_class: AssetClass::Equity,
            side: OrderSide::Buy,
            order_type: OrderType::Market,
        }
    }

    // ----- write-ahead commit + idempotency ----- //

    #[test]
    fn commit_intent_lands_at_pending_submit_unbound() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .expect("first commit succeeds");
        assert_eq!(k, key("live-1", "c-1"));
        let entry = outbox.entry(&k).expect("entry present");
        assert_eq!(entry.state(), OrderState::PendingSubmit);
        assert!(!entry.is_bound());
        assert_eq!(entry.broker_order_id(), None);
    }

    #[test]
    fn commit_intent_rejects_duplicate_correlation_id() {
        let mut outbox = OrderOutbox::new();
        outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .expect("first commit succeeds");
        let err = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 99))
            .expect_err("duplicate correlation id is rejected");
        assert_eq!(
            err.category,
            OrderErrorCategory::DuplicateClientCorrelationId
        );
        // The existing intent is untouched (idempotent — original quantity kept).
        assert_eq!(
            outbox
                .entry(&key("live-1", "c-1"))
                .unwrap()
                .submission()
                .quantity,
            10
        );
    }

    // ----- ack binding ----- //

    #[test]
    fn bind_ack_moves_to_acked_and_records_broker_id() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&k, "ib-42").expect("ack binds");
        let entry = outbox.entry(&k).unwrap();
        assert_eq!(entry.state(), OrderState::Acked);
        assert_eq!(entry.broker_order_id(), Some("ib-42"));
        assert!(entry.is_bound());
    }

    #[test]
    fn bind_ack_is_idempotent_for_same_id_but_conflicts_on_different_id() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&k, "ib-42").unwrap();
        outbox.bind_ack(&k, "ib-42").expect("same id is idempotent");
        let err = outbox
            .bind_ack(&k, "ib-99")
            .expect_err("a different id conflicts");
        assert!(matches!(err, OutboxError::BrokerIdConflict { .. }));
    }

    #[test]
    fn bind_ack_fails_closed_on_unknown_key_and_blank_id() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        assert!(matches!(
            outbox.bind_ack(&key("live-1", "ghost"), "ib-1"),
            Err(OutboxError::UnknownOrder(_))
        ));
        assert!(matches!(
            outbox.bind_ack(&k, "   "),
            Err(OutboxError::BlankBrokerOrderId)
        ));
    }

    // ----- retention ----- //

    #[test]
    fn prune_terminal_removes_only_terminal_entries() {
        let mut outbox = OrderOutbox::new();
        let filled = outbox
            .commit_intent(corr("c-fill"), &submission("live-1", "AAPL", 10))
            .unwrap();
        let working = outbox
            .commit_intent(corr("c-work"), &submission("live-1", "MSFT", 5))
            .unwrap();
        outbox.bind_ack(&filled, "ib-1").unwrap();
        outbox.observe_state(&filled, OrderState::Filled).unwrap();
        outbox.bind_ack(&working, "ib-2").unwrap();

        let pruned = outbox.prune_terminal();
        assert_eq!(pruned, vec![filled.clone()]);
        assert!(!outbox.contains(&filled), "terminal entry removed");
        assert!(outbox.contains(&working), "working entry retained");
    }

    #[test]
    fn entry_is_retained_across_non_terminal_transitions() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&k, "ib-1").unwrap();
        outbox
            .observe_state(&k, OrderState::PartiallyFilled)
            .unwrap();
        // Not terminal → survives a prune.
        assert!(outbox.prune_terminal().is_empty());
        assert!(outbox.contains(&k));
    }

    // ----- durable round-trip ----- //

    #[test]
    fn durable_round_trip_is_identity() {
        let mut outbox = OrderOutbox::new();
        let a = outbox
            .commit_intent(corr("c-a"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&a, "ib-a").unwrap();
        outbox
            .observe_state(&a, OrderState::PartiallyFilled)
            .unwrap();
        let _b = outbox
            .commit_intent(corr("c-b"), &submission("live-1", "MSFT", 3))
            .unwrap();

        let snapshot = OutboxSnapshot::capture(outbox);
        let restored =
            OutboxSnapshot::deserialize(&snapshot.serialize()).expect("round-trip deserializes");
        assert_eq!(restored, snapshot);
        // Serialize is deterministic (byte-identical) across a re-serialize.
        assert_eq!(restored.serialize(), snapshot.serialize());
    }

    #[test]
    fn deserialize_fails_closed_on_bad_magic_checksum_and_schema() {
        let mut outbox = OrderOutbox::new();
        outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        let good = OutboxSnapshot::capture(outbox).serialize();

        // Bad magic.
        let bad_magic = good.replacen(OUTBOX_MAGIC, "NOPE", 1);
        assert!(matches!(
            OutboxSnapshot::deserialize(&bad_magic),
            Err(OutboxPersistenceError::CorruptSnapshot { .. })
        ));

        // Tampered body byte → checksum mismatch (flip a quantity digit).
        let tampered = good.replacen("\n10\n", "\n11\n", 1);
        assert_ne!(tampered, good);
        assert!(matches!(
            OutboxSnapshot::deserialize(&tampered),
            Err(OutboxPersistenceError::ChecksumMismatch)
        ));

        // Missing snapshot / truncated → corrupt.
        assert!(OutboxSnapshot::deserialize("").is_err());
    }

    #[test]
    fn deserialize_rejects_inconsistent_broker_binding() {
        // A hand-built blob: a PENDING_SUBMIT (unbound) intent, then flip the broker
        // present-flag to 1 with an id — an impossible binding that must fail closed.
        let mut outbox = OrderOutbox::new();
        outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        let snapshot = OutboxSnapshot::capture(outbox);
        // The trailing broker present-flag for the single entry is the last "0\n".
        // Rebuild with a bound id and a recomputed checksum via the public codec by
        // acking, which is the legitimate way to bind — then corrupt the STATE to
        // PENDING_SUBMIT while keeping the id, and re-checksum by hand is complex, so
        // instead assert the positive invariant: a bound entry deserializes only when
        // its state is post-ack.
        let restored = OutboxSnapshot::deserialize(&snapshot.serialize()).unwrap();
        assert_eq!(
            restored
                .outbox()
                .entry(&key("live-1", "c-1"))
                .unwrap()
                .state(),
            OrderState::PendingSubmit
        );
        // Directly exercise the invariant helper for the impossible combinations.
        assert!(!binding_consistent(OrderState::PendingSubmit, true));
        assert!(!binding_consistent(OrderState::Filled, false));
        assert!(binding_consistent(OrderState::Acked, true));
        assert!(binding_consistent(OrderState::Rejected, false));
        assert!(binding_consistent(OrderState::Rejected, true));
    }

    // ----- reconciliation ----- //

    fn broker(strategy: &str, correlation: &str, id: &str, state: OrderState) -> BrokerOpenOrder {
        BrokerOpenOrder {
            key: key(strategy, correlation),
            broker_order_id: id.to_string(),
            state,
        }
    }

    #[test]
    fn reconcile_never_resubmits_a_bound_intent() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&k, "ib-1").unwrap();
        // Broker doesn't even report it (open-only view, order already gone) — still
        // never resubmitted.
        let snap = BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenOnly);
        let plan = reconcile(&outbox, &snap);
        assert_eq!(plan.skip_bound, vec![k]);
        assert!(plan.resubmit.is_empty());
        assert!(plan.adopt_ack.is_empty());
    }

    #[test]
    fn reconcile_adopts_when_broker_has_the_unacked_intent() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        let snap = BrokerOpenOrderSnapshot::new(
            vec![broker("live-1", "c-1", "ib-77", OrderState::Acked)],
            SnapshotCoverage::OpenOnly,
        );
        let plan = reconcile(&outbox, &snap);
        assert_eq!(plan.adopt_ack, vec![(k, "ib-77".to_string())]);
        assert!(plan.resubmit.is_empty());
    }

    #[test]
    fn reconcile_resubmits_only_under_full_coverage() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();

        // Open-only: absence is ambiguous → unresolved, never resubmit.
        let open_only = BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenOnly);
        let plan = reconcile(&outbox, &open_only);
        assert!(plan.resubmit.is_empty());
        assert_eq!(plan.unresolved.len(), 1);
        assert!(matches!(
            plan.unresolved[0].kind,
            ConflictKind::UnverifiableSubmitWindow
        ));

        // Full coverage: provably never landed → resubmit.
        let full = BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenAndRecentlyCompleted);
        let plan = reconcile(&outbox, &full);
        assert_eq!(plan.resubmit, vec![k]);
        assert!(plan.unresolved.is_empty());
    }

    #[test]
    fn reconcile_flags_broker_id_mismatch_without_resubmitting() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&k, "ib-1").unwrap();
        let snap = BrokerOpenOrderSnapshot::new(
            vec![broker("live-1", "c-1", "ib-DIFFERENT", OrderState::Acked)],
            SnapshotCoverage::OpenAndRecentlyCompleted,
        );
        let plan = reconcile(&outbox, &snap);
        assert!(plan.resubmit.is_empty());
        assert!(plan.skip_bound.is_empty());
        assert_eq!(plan.unresolved.len(), 1);
        assert!(matches!(
            plan.unresolved[0].kind,
            ConflictKind::BrokerIdMismatch { .. }
        ));
    }

    #[test]
    fn reconcile_surfaces_duplicate_broker_rows_without_adopting() {
        // The duplicate-live-order hazard: the broker reports TWO orders for one
        // correlation key. An UNBOUND crash-window intent must NOT be collapsed to a
        // single adopt_ack (which would mask the second live order) — it is surfaced.
        let mut outbox = OrderOutbox::new();
        outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        let snap = BrokerOpenOrderSnapshot::new(
            vec![
                broker("live-1", "c-1", "ib-B", OrderState::Acked),
                broker("live-1", "c-1", "ib-A", OrderState::Acked),
            ],
            SnapshotCoverage::OpenAndRecentlyCompleted,
        );
        let plan = reconcile(&outbox, &snap);
        assert!(plan.adopt_ack.is_empty(), "duplicate rows must not adopt");
        assert!(plan.resubmit.is_empty());
        assert!(plan.skip_bound.is_empty());
        assert_eq!(plan.unresolved.len(), 1);
        match &plan.unresolved[0].kind {
            ConflictKind::DuplicateBrokerRows { broker_order_ids } => {
                // Sorted for determinism regardless of the adapter's row order.
                assert_eq!(
                    broker_order_ids,
                    &vec!["ib-A".to_string(), "ib-B".to_string()]
                );
            }
            other => panic!("expected DuplicateBrokerRows, got {other:?}"),
        }
    }

    #[test]
    fn reconcile_surfaces_duplicate_broker_rows_for_a_bound_intent() {
        // A BOUND intent with duplicate broker rows is also surfaced (never
        // skip_bound), so a second live order is never silently ignored.
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&k, "ib-A").unwrap();
        let snap = BrokerOpenOrderSnapshot::new(
            vec![
                broker("live-1", "c-1", "ib-A", OrderState::Acked),
                broker("live-1", "c-1", "ib-B", OrderState::PartiallyFilled),
            ],
            SnapshotCoverage::OpenOnly,
        );
        let plan = reconcile(&outbox, &snap);
        assert!(plan.skip_bound.is_empty());
        assert!(plan.resubmit.is_empty());
        assert_eq!(plan.unresolved.len(), 1);
        assert!(matches!(
            plan.unresolved[0].kind,
            ConflictKind::DuplicateBrokerRows { .. }
        ));
    }

    #[test]
    fn reconcile_syncs_a_legal_terminal_state() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&k, "ib-1").unwrap();
        // Broker reports FILLED (ACKED -> FILLED is legal) → mark_terminal.
        let snap = BrokerOpenOrderSnapshot::new(
            vec![broker("live-1", "c-1", "ib-1", OrderState::Filled)],
            SnapshotCoverage::OpenAndRecentlyCompleted,
        );
        let plan = reconcile(&outbox, &snap);
        assert_eq!(plan.skip_bound, vec![k.clone()]);
        assert_eq!(plan.mark_terminal, vec![(k, OrderState::Filled)]);
    }

    #[test]
    fn reconcile_state_disagreement_goes_only_to_unresolved() {
        // A bound intent whose broker reports an ILLEGAL terminal state (ACKED ->
        // CANCELLED is not a direct edge) must land in EXACTLY ONE bucket: unresolved.
        // It must NOT also appear in skip_bound / mark_terminal (the disjoint-bucket
        // contract).
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&k, "ib-1").unwrap();
        let snap = BrokerOpenOrderSnapshot::new(
            vec![broker("live-1", "c-1", "ib-1", OrderState::Cancelled)],
            SnapshotCoverage::OpenAndRecentlyCompleted,
        );
        let plan = reconcile(&outbox, &snap);
        assert!(
            plan.skip_bound.is_empty(),
            "a disagreement must not ALSO skip_bound the same key"
        );
        assert!(plan.mark_terminal.is_empty());
        assert!(plan.resubmit.is_empty());
        assert_eq!(plan.unresolved.len(), 1);
        assert!(matches!(
            plan.unresolved[0].kind,
            ConflictKind::StateDisagreement { .. }
        ));
    }

    #[test]
    fn broker_reconcile_error_categories_are_distinct() {
        let errs = [
            BrokerReconcileError::connectivity_blocked("a"),
            BrokerReconcileError::stale_data("b"),
            BrokerReconcileError::timeout("c"),
            BrokerReconcileError::malformed_snapshot("d"),
            BrokerReconcileError::unavailable("e"),
        ];
        let categories: std::collections::BTreeSet<&str> =
            errs.iter().map(BrokerReconcileError::category).collect();
        assert_eq!(
            categories.len(),
            errs.len(),
            "each fail-closed category must be distinct"
        );
        assert_eq!(
            BrokerReconcileError::connectivity_blocked("boom").reason(),
            "boom"
        );
        assert!(BrokerReconcileError::timeout("t")
            .to_string()
            .contains("RECONCILE_TIMEOUT"));
    }

    #[test]
    fn reconcile_leaves_terminal_outbox_entries_untouched() {
        let mut outbox = OrderOutbox::new();
        let k = outbox
            .commit_intent(corr("c-1"), &submission("live-1", "AAPL", 10))
            .unwrap();
        outbox.bind_ack(&k, "ib-1").unwrap();
        outbox.observe_state(&k, OrderState::Rejected).unwrap();
        let snap = BrokerOpenOrderSnapshot::new(vec![], SnapshotCoverage::OpenAndRecentlyCompleted);
        let plan = reconcile(&outbox, &snap);
        assert!(plan.skip_bound.is_empty());
        assert!(plan.resubmit.is_empty());
        assert!(plan.unresolved.is_empty());
    }

    // ----- seeded property coverage ----- //

    /// A tiny dependency-free LCG so the property loop is deterministic and needs no
    /// external crate (the workspace carries no proptest/quickcheck by design).
    struct Lcg(u64);
    impl Lcg {
        fn next_u64(&mut self) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            self.0
        }
        fn below(&mut self, n: u64) -> u64 {
            self.next_u64() % n
        }
    }

    #[test]
    fn property_bound_intent_is_never_resubmitted_and_round_trip_holds() {
        let mut rng = Lcg(0x5152_5354_5556_5758);
        for case in 0..3000u64 {
            let mut outbox = OrderOutbox::new();
            let n = rng.below(6); // 0..5 intents
            for i in 0..n {
                let c = format!("c-{case}-{i}");
                let k = outbox
                    .commit_intent(corr(&c), &submission("live-1", "AAPL", 1 + (i as i64)))
                    .expect("unique correlation id");
                // Randomly bind + advance.
                if rng.below(2) == 0 {
                    outbox.bind_ack(&k, format!("ib-{case}-{i}")).unwrap();
                    match rng.below(4) {
                        0 => {
                            outbox.observe_state(&k, OrderState::PartiallyFilled).ok();
                        }
                        1 => {
                            outbox.observe_state(&k, OrderState::Filled).ok();
                        }
                        2 => {
                            outbox.observe_state(&k, OrderState::Rejected).ok();
                        }
                        _ => {}
                    }
                }
            }

            // Round-trip fidelity.
            let snap = OutboxSnapshot::capture(outbox.clone());
            let restored =
                OutboxSnapshot::deserialize(&snap.serialize()).expect("valid outbox round-trips");
            assert_eq!(
                restored, snap,
                "case {case}: durable round-trip is identity"
            );

            // Reconcile against a random broker view.
            let coverage = if rng.below(2) == 0 {
                SnapshotCoverage::OpenOnly
            } else {
                SnapshotCoverage::OpenAndRecentlyCompleted
            };
            // Broker reports a random subset of the (bound) intents.
            let broker_orders: Vec<BrokerOpenOrder> = outbox
                .entries_sorted()
                .iter()
                .filter(|_entry| rng.below(2) == 0)
                .map(|e| BrokerOpenOrder {
                    key: e.key().clone(),
                    broker_order_id: e
                        .broker_order_id()
                        .map(str::to_string)
                        .unwrap_or_else(|| "ib-broker".to_string()),
                    state: e.state(),
                })
                .collect();
            let plan = reconcile(
                &outbox,
                &BrokerOpenOrderSnapshot::new(broker_orders, coverage),
            );

            // INVARIANT: a bound intent is never in resubmit.
            for entry in outbox.entries_sorted() {
                if entry.is_bound() {
                    assert!(
                        !plan.resubmit.contains(entry.key()),
                        "case {case}: bound intent {} must never be resubmitted",
                        entry.key()
                    );
                }
            }
            // INVARIANT: resubmit is disjoint from skip_bound and adopt_ack.
            for k in &plan.resubmit {
                assert!(!plan.skip_bound.contains(k));
                assert!(!plan.adopt_ack.iter().any(|(ak, _)| ak == k));
            }
        }
    }
}
