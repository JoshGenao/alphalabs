//! Durable live-execution-state persistence and restart recovery for
//! **SRS-EXE-005** — "persist live strategy state needed for restart recovery
//! and re-execute the warm-up mechanism on restart" (SyRS SYS-90, NFR-R3; StRS
//! SN-2.05 / SN-1.01 / BG-1).
//!
//! # What this module owns
//!
//! The order lifecycle machine ([`atp_types::OrderLedger`], SRS-EXE-008) is an
//! *in-memory* idempotency authority; its own module docs explicitly defer
//! "durable persistence of the ledger across a *process* restart" to **this**
//! feature. So this module is the durable **snapshot + restore** substrate for
//! the full live execution state the AC enumerates:
//!
//! * pending submissions, awaiting acknowledgements, order statuses, and
//!   correlation IDs — carried by the persisted [`OrderLedger`];
//! * broker IDs — the `(correlation id → broker id)` binding;
//! * fill events; open positions; the account equity snapshot;
//! * the user-accessible JSON-serializable state dictionary.
//!
//! On restart the [`recover_from_path`] flow loads the snapshot, restores the
//! ledger, enforces the NFR-R3 restore deadline (60 s by default, *excluding*
//! warm-up), and re-executes the SRS-SDK-005 warm-up (via a
//! [`WarmUpReexecutionPort`]) so indicator buffers / rolling windows are rebuilt
//! from historical data.
//!
//! ## Survive restart *without duplicate submissions* (the AC spine)
//!
//! The restored [`OrderLedger`] keeps the same `(strategy, correlation id)`
//! idempotency keys it had before the restart, so a strategy that
//! deterministically reproduces a correlation id after a restart has its
//! re-submission rejected as a duplicate (SRS-EXE-008 / SRS-ERR-001). Persisting
//! and restoring the ledger is what makes that hold across a process boundary —
//! the in-memory authority alone could not (see the `order_lifecycle` module
//! docs). The narrower *write-ahead* guarantee for the crash window **between**
//! the durable intent commit and the IB submission — reconciling acknowledged
//! broker IDs against replayed intents — is the SRS-EXE-009 durable outbox and
//! is deliberately **not** claimed here (see *deferred* below).
//!
//! # Deterministic, dependency-free codec
//!
//! Like the paper analogue ([`atp_simulation::paper_state`], SRS-SIM-004), the
//! snapshot serializes to a **byte-identical** form for the same state (orders,
//! broker IDs, fills, and positions are emitted in a canonical order), is
//! prefixed with a magic header and an FNV-1a integrity checksum over the body,
//! and [`LiveStateSnapshot::deserialize`] validates the header, the checksum
//! (before building any state), the schema version, and every field invariant —
//! so a corrupt, truncated, or tampered snapshot returns an [`Err`] and yields
//! **no** partially-restored state under fault injection. The codec is
//! integer-only and uses no external crate (the workspace carries no serde
//! dependency by design); it is intentionally a parallel, per-crate copy of the
//! paper codec rather than a shared one, since hoisting it would be a
//! cross-cutting refactor of an unrelated crate (`atp-simulation`).
//!
//! # What is real here vs deferred
//!
//! Real: the durable snapshot + restore of the ledger and the enumerated state,
//! the fail-closed codec, the restart-recovery orchestration, the NFR-R3
//! deadline enforcement, and the warm-up re-execution seam — all deterministic,
//! dependency-free, and unit/contract/domain tested. Deferred (see
//! `architecture/runtime_services.json#live_state_recovery_contract.deferred`):
//!
//! * **Producers** of broker IDs / fill events (the SRS-EXE-006 IB adapter event
//!   mapping) and of open positions / account equity (a live IB account sync).
//!   This module persists and restores whatever is captured; it does not source
//!   those fields.
//! * **The SRS-EXE-009 durable outbox** (write-ahead intent + broker-ID
//!   reconciliation for the submit-crash window).
//! * **Multi-leg composite option orders (SRS-EXE-004) and per-contract option
//!   positions.** The v1 schema models single-leg orders (through the
//!   [`OrderLedger`]) and positions keyed by symbol. It does NOT yet represent a
//!   live [`atp_types::CompositeOrderSubmission`] (pending combo submission, combo
//!   broker ID / status / per-leg fills) or an option position keyed by
//!   [`OptionContractIdentity::canonical_key`](atp_types::OptionContractIdentity).
//!   This is safe as a bound rather than a silent loss because that state does not
//!   exist to recover today: SRS-EXE-004 is `passes:false` and its live combo wire
//!   is operator-gated/pending; the composite execution path
//!   (`route_composite_order`) is **not** ledger-tracked; and a single-leg *option*
//!   order fails closed at `OrderSubmission::validate` (re-checked on restore), so
//!   the ledger can never hold one. When SRS-EXE-004's composite path goes live and
//!   is wired into the SRS-EXE-008 lifecycle, this schema bumps `SCHEMA_VERSION` to
//!   carry composite records keyed by `canonical_key`. Owners: SRS-EXE-004 (live
//!   composite path) + SRS-EXE-008 (composite lifecycle tracking).
//! * **Wiring the periodic + on-shutdown capture loop into the live execution
//!   engine / orchestrator**, and the actual SRS-SDK-005 warm-up execution
//!   (a strategy-container concern reached through [`WarmUpReexecutionPort`]).
//! * **The end-to-end 60 s container-restart wall-clock proof** (a fault-injection
//!   / integration test), which is why `feature_list.json` keeps SRS-EXE-005 at
//!   `passes:false`.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;
use std::fs;
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use atp_types::{
    AssetClass, ClientCorrelationId, OrderKey, OrderLedger, OrderLifecycle, OrderLifecycleError,
    OrderSide, OrderState, OrderSubmission, OrderType, StrategyId,
};

/// The snapshot schema version. Bumped only when the serialized layout changes
/// in a backward-incompatible way; [`LiveStateSnapshot::deserialize`] rejects any
/// other version rather than silently misreading an older/foreign layout.
const SCHEMA_VERSION: i64 = 1;

/// The magic header line that prefixes every serialized snapshot, so a foreign or
/// truncated blob is rejected before any parsing.
const MAGIC: &str = "ATP-LIVE-EXEC-STATE-V1";

/// NFR-R3 / SYS-90 default restore deadline (seconds): persisted live state is
/// recoverable within 60 seconds of container restart, excluding warm-up.
const DEFAULT_RESTORE_DEADLINE_SECS: u64 = 60;

/// The base name of the live-state store file inside its directory.
const STORE_FILENAME: &str = "live_exec_state.snapshot";

/// The base name of the scratch file an atomic save writes (and fsyncs) before
/// renaming it onto [`STORE_FILENAME`]. A per-process, per-call `.<pid>.<seq>`
/// suffix is appended so two writers persisting to the same directory cannot
/// rename over each other's scratch file.
const STORE_TMP_FILENAME: &str = "live_exec_state.snapshot.tmp";

/// Process-local monotonic counter that disambiguates concurrent scratch files
/// within one process (combined with the pid for cross-process uniqueness). It
/// affects only the scratch file name, never persisted content, so a snapshot
/// stays byte-identical for the same state.
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

// --------------------------------------------------------------------------- //
// Recovery configuration (NFR-R3)
// --------------------------------------------------------------------------- //

/// The restart-recovery configuration (NFR-R3 / SYS-90).
///
/// Defaults to the NFR-R3 baseline: a 60 s restore deadline (excluding warm-up)
/// and warm-up re-execution always on. Warm-up re-execution is **not** a tunable:
/// the AC requires warm-up to be re-executed on restart, so it can never be
/// disabled (mirroring the paper analogue's always-on persist-on-shutdown).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryConfig {
    restore_deadline_secs: u64,
    reexecute_warmup_on_restart: bool,
}

impl Default for RecoveryConfig {
    fn default() -> Self {
        Self {
            restore_deadline_secs: DEFAULT_RESTORE_DEADLINE_SECS,
            reexecute_warmup_on_restart: true,
        }
    }
}

impl RecoveryConfig {
    /// A validated recovery config. Fails closed on a zero restore deadline (a
    /// zero-second deadline could never be met, so it is out of contract).
    pub fn new(restore_deadline_secs: u64) -> Result<Self, PersistenceError> {
        if restore_deadline_secs == 0 {
            return Err(PersistenceError::InvalidConfig {
                context: "restore_deadline_secs must be greater than zero",
            });
        }
        Ok(Self {
            restore_deadline_secs,
            reexecute_warmup_on_restart: true,
        })
    }

    /// The configured restore deadline in seconds (NFR-R3 default 60).
    pub fn restore_deadline_secs(&self) -> u64 {
        self.restore_deadline_secs
    }

    /// The restore deadline as a [`Duration`].
    pub fn restore_deadline(&self) -> Duration {
        Duration::from_secs(self.restore_deadline_secs)
    }

    /// Whether warm-up is re-executed on restart. Always `true`: the AC mandates
    /// warm-up re-execution, so it cannot be disabled.
    pub fn reexecute_warmup_on_restart(&self) -> bool {
        self.reexecute_warmup_on_restart
    }

    /// Enforce the NFR-R3 restore deadline over `restore_elapsed` — the wall-clock
    /// time the caller measured for the state-restore phase (load + deserialize +
    /// ledger rebuild), which **excludes** warm-up per the AC. Fails closed with
    /// [`RecoveryError::RestoreDeadlineExceeded`] if the phase overran.
    pub fn restore_within_deadline(&self, restore_elapsed: Duration) -> Result<(), RecoveryError> {
        if restore_elapsed > self.restore_deadline() {
            return Err(RecoveryError::RestoreDeadlineExceeded {
                elapsed_secs: restore_elapsed.as_secs(),
                deadline_secs: self.restore_deadline_secs,
            });
        }
        Ok(())
    }
}

// --------------------------------------------------------------------------- //
// Account-level state records
// --------------------------------------------------------------------------- //

/// An account equity snapshot (minor currency units). Both components may be
/// negative — `cash_minor` for a margin balance, `market_value_minor` for a net
/// short book — so neither is sign-validated.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct AccountEquitySnapshot {
    cash_minor: i128,
    market_value_minor: i128,
}

impl AccountEquitySnapshot {
    pub fn new(cash_minor: i128, market_value_minor: i128) -> Self {
        Self {
            cash_minor,
            market_value_minor,
        }
    }

    pub fn cash_minor(&self) -> i128 {
        self.cash_minor
    }

    pub fn market_value_minor(&self) -> i128 {
        self.market_value_minor
    }

    /// Net liquidation value = cash + market value, matching the paper metrics
    /// accumulator's net-liq formula (cash plus the summed position market value).
    pub fn net_liquidation_minor(&self) -> i128 {
        self.cash_minor + self.market_value_minor
    }
}

/// One recorded fill event (SRS-EXE-005 "fill events survive restart"). The
/// canonical richer fill vocabulary is owned by the fill/adapter producers
/// (SRS-EXE-006 / SRS-SIM-002); this is the minimal record the snapshot persists.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FillEventRecord {
    order: OrderKey,
    sequence: u64,
    filled_quantity: i64,
    fill_price_minor: i64,
}

impl FillEventRecord {
    /// A validated fill record. Fails closed on a zero fill quantity (a fill that
    /// moved nothing) or a non-positive fill price (a price is always positive).
    pub fn new(
        order: OrderKey,
        sequence: u64,
        filled_quantity: i64,
        fill_price_minor: i64,
    ) -> Result<Self, PersistenceError> {
        if filled_quantity == 0 {
            return Err(PersistenceError::InconsistentField {
                context: "fill event with zero quantity",
            });
        }
        if fill_price_minor <= 0 {
            return Err(PersistenceError::InconsistentField {
                context: "fill event with non-positive price",
            });
        }
        Ok(Self {
            order,
            sequence,
            filled_quantity,
            fill_price_minor,
        })
    }

    pub fn order(&self) -> &OrderKey {
        &self.order
    }

    pub fn sequence(&self) -> u64 {
        self.sequence
    }

    pub fn filled_quantity(&self) -> i64 {
        self.filled_quantity
    }

    pub fn fill_price_minor(&self) -> i64 {
        self.fill_price_minor
    }
}

// --------------------------------------------------------------------------- //
// The aggregate live execution state
// --------------------------------------------------------------------------- //

/// The full live execution state the SRS-EXE-005 AC requires to survive restart.
///
/// It is built up with the `with_*` methods, each of which fails closed on an
/// inconsistency (a broker ID / fill for an order not in the ledger, a
/// non-canonical or flat position). Those same invariants are re-checked on
/// [`LiveStateSnapshot::deserialize`] so a corrupt snapshot never rehydrates
/// fabricated cross-references.
#[derive(Debug, Default)]
pub struct LiveExecutionState {
    orders: OrderLedger,
    broker_ids: HashMap<OrderKey, String>,
    fills: Vec<FillEventRecord>,
    open_positions: BTreeMap<String, i64>,
    equity: AccountEquitySnapshot,
    user_state_json: String,
    /// The strategies under live management that must be warmed up on restart —
    /// tracked independently of `orders`, so a live strategy with open positions
    /// or user state but no *active* order is still re-warmed (SRS-SDK-005) on
    /// recovery rather than resuming on cold indicators.
    live_strategies: BTreeSet<String>,
}

impl LiveExecutionState {
    /// A live execution state over `orders`, with no broker IDs / fills /
    /// positions yet, a zero equity snapshot, and an empty (`{}`) user-state
    /// dictionary.
    pub fn new(orders: OrderLedger) -> Self {
        Self {
            orders,
            broker_ids: HashMap::new(),
            fills: Vec::new(),
            open_positions: BTreeMap::new(),
            equity: AccountEquitySnapshot::default(),
            user_state_json: "{}".to_string(),
            live_strategies: BTreeSet::new(),
        }
    }

    /// Bind a broker ID to an order's `(strategy, correlation id)` key. Fails
    /// closed if the key is not in the ledger (a broker ID for an unknown order)
    /// or the broker ID is blank.
    pub fn with_broker_id(
        mut self,
        key: OrderKey,
        broker_id: impl Into<String>,
    ) -> Result<Self, PersistenceError> {
        let broker_id = broker_id.into();
        if self.orders.get(&key).is_none() {
            return Err(PersistenceError::InconsistentField {
                context: "broker id for an order not in the ledger",
            });
        }
        if broker_id.trim().is_empty() {
            return Err(PersistenceError::InconsistentField {
                context: "empty broker id",
            });
        }
        if self.broker_ids.insert(key, broker_id).is_some() {
            return Err(PersistenceError::DuplicateRecord {
                context: "two broker ids for one order",
            });
        }
        Ok(self)
    }

    /// Record a fill event. Fails closed if the fill's order key is not in the
    /// ledger, or if a fill with the same identity `(order, sequence)` is already
    /// recorded — a duplicate fill would double-count / replay an execution on
    /// restart, corrupting position and P&L accounting.
    pub fn with_fill(mut self, fill: FillEventRecord) -> Result<Self, PersistenceError> {
        if self.orders.get(fill.order()).is_none() {
            return Err(PersistenceError::InconsistentField {
                context: "fill event for an order not in the ledger",
            });
        }
        if self.fills.iter().any(|existing| {
            existing.order() == fill.order() && existing.sequence() == fill.sequence()
        }) {
            return Err(PersistenceError::DuplicateRecord {
                context: "two fill events with the same (order, sequence) identity",
            });
        }
        self.fills.push(fill);
        Ok(self)
    }

    /// Record an open position: `net_quantity` shares of `symbol` (positive long,
    /// negative short). Fails closed on a blank / non-canonical symbol or a flat
    /// (zero) quantity — an open position is by definition non-flat, and a flat
    /// symbol carrying no exposure is simply absent.
    pub fn with_position(
        mut self,
        symbol: impl Into<String>,
        net_quantity: i64,
    ) -> Result<Self, PersistenceError> {
        let symbol = symbol.into();
        if symbol.trim().is_empty() {
            return Err(PersistenceError::InconsistentField {
                context: "empty position symbol",
            });
        }
        if symbol != symbol.trim() || symbol != symbol.to_uppercase() {
            return Err(PersistenceError::InconsistentField {
                context: "non-canonical position symbol",
            });
        }
        if net_quantity == 0 {
            return Err(PersistenceError::InconsistentField {
                context: "flat (zero-quantity) open position",
            });
        }
        if self.open_positions.insert(symbol, net_quantity).is_some() {
            return Err(PersistenceError::DuplicateRecord {
                context: "two positions for one symbol",
            });
        }
        Ok(self)
    }

    /// Set the account equity snapshot.
    pub fn with_equity(mut self, equity: AccountEquitySnapshot) -> Self {
        self.equity = equity;
        self
    }

    /// Set the user-accessible JSON-serializable state **dictionary**. It is
    /// persisted verbatim (length-prefixed, so any bytes round-trip) but is
    /// validated to be a well-formed JSON object first — the AC calls it a
    /// "JSON-serializable state dictionary", so recovery fails closed on a
    /// non-JSON or non-object value ([`PersistenceError::InconsistentField`])
    /// rather than deferring the failure to strategy restart. The same check runs
    /// on [`LiveStateSnapshot::deserialize`], so a corrupt snapshot cannot
    /// rehydrate an unparseable user dictionary.
    pub fn with_user_state_json(
        mut self,
        user_state_json: impl Into<String>,
    ) -> Result<Self, PersistenceError> {
        let user_state_json = user_state_json.into();
        if !is_json_object(&user_state_json) {
            return Err(PersistenceError::InconsistentField {
                context: "user state dictionary is not a well-formed JSON object",
            });
        }
        self.user_state_json = user_state_json;
        Ok(self)
    }

    /// Register `strategy` as under live management, so restart recovery warms it
    /// up (SRS-SDK-005) even if it has no active order. Fails closed on a blank id
    /// or a duplicate registration.
    pub fn with_live_strategy(mut self, strategy: &StrategyId) -> Result<Self, PersistenceError> {
        if strategy.as_str().trim().is_empty() {
            return Err(PersistenceError::InconsistentField {
                context: "empty live strategy id",
            });
        }
        if !self.live_strategies.insert(strategy.as_str().to_string()) {
            return Err(PersistenceError::DuplicateRecord {
                context: "duplicate live strategy registration",
            });
        }
        Ok(self)
    }

    pub fn orders(&self) -> &OrderLedger {
        &self.orders
    }

    /// Consume the state and take the restored [`OrderLedger`], so the live
    /// execution engine can resume from it after a restart — the restored ledger
    /// still rejects a duplicate `(strategy, correlation id)` submission, which is
    /// the AC's "survive restart without duplicate submissions".
    pub fn into_ledger(self) -> OrderLedger {
        self.orders
    }

    pub fn broker_id(&self, key: &OrderKey) -> Option<&str> {
        self.broker_ids.get(key).map(String::as_str)
    }

    pub fn fills(&self) -> &[FillEventRecord] {
        &self.fills
    }

    pub fn open_position(&self, symbol: &str) -> Option<i64> {
        self.open_positions.get(symbol).copied()
    }

    pub fn open_positions(&self) -> &BTreeMap<String, i64> {
        &self.open_positions
    }

    pub fn equity(&self) -> &AccountEquitySnapshot {
        &self.equity
    }

    pub fn user_state_json(&self) -> &str {
        &self.user_state_json
    }

    /// The registered live strategies (independent of orders) — the set the
    /// restart flow re-executes warm-up for.
    pub fn live_strategies(&self) -> Vec<StrategyId> {
        self.live_strategies
            .iter()
            .cloned()
            .map(StrategyId::new)
            .collect()
    }

    /// The distinct strategies with recovered state, in canonical (sorted) order:
    /// the **union** of the explicitly registered live strategies and every
    /// strategy that owns an order in the restored ledger. This is the set the
    /// restart flow re-executes warm-up for — so a live strategy with positions or
    /// user state but no active order is still warmed up, and a strategy that owns
    /// orders is warmed up even if registration was not persisted.
    pub fn strategies_with_state(&self) -> Vec<StrategyId> {
        let mut ids: BTreeSet<String> = self.live_strategies.clone();
        for order in self.orders.orders_iter() {
            ids.insert(order.strategy_id().as_str().to_string());
        }
        ids.into_iter().map(StrategyId::new).collect()
    }
}

// --------------------------------------------------------------------------- //
// The versioned snapshot (capture / serialize / deserialize / durable store)
// --------------------------------------------------------------------------- //

/// A versioned, restorable snapshot of the live execution state (SRS-EXE-005).
#[derive(Debug)]
pub struct LiveStateSnapshot {
    schema_version: i64,
    config: RecoveryConfig,
    state: LiveExecutionState,
}

impl LiveStateSnapshot {
    /// Capture `state` at [`SCHEMA_VERSION`] with `config`.
    pub fn capture(state: LiveExecutionState, config: RecoveryConfig) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            config,
            state,
        }
    }

    pub fn schema_version(&self) -> i64 {
        self.schema_version
    }

    pub fn config(&self) -> &RecoveryConfig {
        &self.config
    }

    pub fn state(&self) -> &LiveExecutionState {
        &self.state
    }

    /// Consume the snapshot and return the restored [`LiveExecutionState`].
    pub fn into_state(self) -> LiveExecutionState {
        self.state
    }

    /// Serialize to the durable form.
    ///
    /// Layout: the `MAGIC` line, an integrity `checksum` line over the body, then
    /// the body (schema version, config, the sorted ledger, broker IDs, fills,
    /// positions, equity, and the user-state dictionary). Orders, broker IDs and
    /// fills are emitted in a canonical order, so the output is **byte-identical**
    /// for the same state regardless of `HashMap` iteration order. Strings are
    /// length-prefixed, so a symbol containing spaces round-trips losslessly. The
    /// checksum covers the whole body, so any later byte change is detected on
    /// restore.
    pub fn serialize(&self) -> String {
        let mut body = String::new();
        push_i128(&mut body, i128::from(self.schema_version));
        // Config.
        push_i128(&mut body, i128::from(self.config.restore_deadline_secs));
        push_i128(
            &mut body,
            i128::from(self.config.reexecute_warmup_on_restart as i64),
        );

        // Orders, sorted by their key for determinism.
        let mut orders: Vec<&OrderLifecycle> = self.state.orders.orders_iter().collect();
        orders.sort_by(|a, b| order_key_sort(a.key(), b.key()));
        push_i128(&mut body, orders.len() as i128);
        for order in orders {
            push_order(&mut body, order);
        }

        // Broker IDs, sorted by their order key.
        let mut broker_ids: Vec<(&OrderKey, &String)> = self.state.broker_ids.iter().collect();
        broker_ids.sort_by(|a, b| order_key_sort(a.0, b.0));
        push_i128(&mut body, broker_ids.len() as i128);
        for (key, broker_id) in broker_ids {
            push_str(&mut body, key.strategy_id().as_str());
            push_str(&mut body, key.correlation_id().as_str());
            push_str(&mut body, broker_id);
        }

        // Fills, sorted by (order key, sequence).
        let mut fills: Vec<&FillEventRecord> = self.state.fills.iter().collect();
        fills.sort_by(|a, b| {
            order_key_sort(a.order(), b.order()).then_with(|| a.sequence().cmp(&b.sequence()))
        });
        push_i128(&mut body, fills.len() as i128);
        for fill in fills {
            push_str(&mut body, fill.order().strategy_id().as_str());
            push_str(&mut body, fill.order().correlation_id().as_str());
            push_i128(&mut body, i128::from(fill.sequence()));
            push_i128(&mut body, i128::from(fill.filled_quantity()));
            push_i128(&mut body, i128::from(fill.fill_price_minor()));
        }

        // Open positions (already sorted by symbol via the BTreeMap).
        push_i128(&mut body, self.state.open_positions.len() as i128);
        for (symbol, quantity) in &self.state.open_positions {
            push_str(&mut body, symbol);
            push_i128(&mut body, i128::from(*quantity));
        }

        // Live-managed strategies, the warm-up target set (already sorted via the
        // BTreeSet), independent of orders.
        push_i128(&mut body, self.state.live_strategies.len() as i128);
        for strategy in &self.state.live_strategies {
            push_str(&mut body, strategy);
        }

        // Account equity snapshot.
        push_i128(&mut body, self.state.equity.cash_minor());
        push_i128(&mut body, self.state.equity.market_value_minor());

        // The user-accessible JSON-serializable state dictionary, verbatim.
        push_str(&mut body, &self.state.user_state_json);

        // Assemble: magic + an integrity checksum over the body + the body.
        let mut out = String::with_capacity(body.len() + MAGIC.len() + 32);
        push_line(&mut out, MAGIC);
        push_i128(&mut out, i128::from(checksum(body.as_bytes())));
        out.push_str(&body);
        out
    }

    /// Deserialize a snapshot produced by [`serialize`](Self::serialize), failing
    /// closed on any malformation.
    ///
    /// Validates the magic header, the body integrity checksum (BEFORE building
    /// any state), the schema version, the config, and every field/cross-reference
    /// invariant, building the whole state in locals before returning — so a
    /// corrupt, truncated, or tampered blob returns an [`Err`] and yields no
    /// partially-restored state.
    pub fn deserialize(serialized: &str) -> Result<Self, PersistenceError> {
        let mut cursor = Cursor::new(serialized);

        let magic = cursor.read_line("magic header")?;
        if magic != MAGIC {
            return Err(PersistenceError::CorruptSnapshot {
                context: "magic header",
            });
        }
        // Integrity check FIRST: the checksum covers the entire body that follows.
        let stored_checksum = cursor.read_u64("checksum")?;
        let body = cursor.remaining();
        if checksum(body) != stored_checksum {
            return Err(PersistenceError::ChecksumMismatch);
        }

        let schema_version = cursor.read_i64("schema version")?;
        if schema_version != SCHEMA_VERSION {
            return Err(PersistenceError::UnknownSchemaVersion {
                found: schema_version,
            });
        }

        let restore_deadline_secs = cursor.read_u64("restore_deadline_secs")?;
        // Warm-up re-execution is mandated on restart, so a snapshot that encodes
        // it disabled is out of contract (the value is always written true; this
        // guards a foreign/tampered writer).
        let reexecute_warmup = cursor.read_bool("reexecute_warmup_on_restart")?;
        if !reexecute_warmup {
            return Err(PersistenceError::WarmUpReexecutionRequired);
        }
        let config = RecoveryConfig::new(restore_deadline_secs)?;

        // Orders → rebuild the ledger (fail-closed on cross-order inconsistency).
        let order_count = cursor.read_count("order count")?;
        // Do NOT pre-allocate from the untrusted count — a crafted huge count
        // would OOM before the cursor runs out. Each iteration reads from the
        // cursor and fails closed the moment the data is exhausted.
        let mut lifecycles: Vec<OrderLifecycle> = Vec::new();
        for _ in 0..order_count {
            lifecycles.push(read_order(&mut cursor)?);
        }
        let orders = OrderLedger::restore_from(lifecycles).map_err(PersistenceError::Ledger)?;
        let mut state = LiveExecutionState::new(orders);

        // Broker IDs (each must bind an order in the restored ledger).
        let broker_id_count = cursor.read_count("broker id count")?;
        for _ in 0..broker_id_count {
            let key = read_order_key(&mut cursor)?;
            let broker_id = cursor.read_str("broker id")?;
            state = state.with_broker_id(key, broker_id)?;
        }

        // Fills (each must reference an order in the restored ledger).
        let fill_count = cursor.read_count("fill count")?;
        for _ in 0..fill_count {
            let order = read_order_key(&mut cursor)?;
            let sequence = cursor.read_u64("fill sequence")?;
            let filled_quantity = cursor.read_i64("fill quantity")?;
            let fill_price_minor = cursor.read_i64("fill price")?;
            let fill = FillEventRecord::new(order, sequence, filled_quantity, fill_price_minor)?;
            state = state.with_fill(fill)?;
        }

        // Open positions.
        let position_count = cursor.read_count("position count")?;
        for _ in 0..position_count {
            let symbol = cursor.read_str("position symbol")?;
            let quantity = cursor.read_i64("position quantity")?;
            state = state.with_position(symbol, quantity)?;
        }

        // Live-managed strategies (the warm-up target set).
        let live_strategy_count = cursor.read_count("live strategy count")?;
        for _ in 0..live_strategy_count {
            let strategy = cursor.read_str("live strategy id")?;
            state = state.with_live_strategy(&StrategyId::new(strategy))?;
        }

        // Account equity snapshot.
        let cash_minor = cursor.read_i128("cash_minor")?;
        let market_value_minor = cursor.read_i128("market_value_minor")?;
        state = state.with_equity(AccountEquitySnapshot::new(cash_minor, market_value_minor));

        // The user-accessible state dictionary, verbatim.
        let user_state_json = cursor.read_str("user state dictionary")?;
        state = state.with_user_state_json(user_state_json)?;

        cursor.expect_end()?;

        Ok(Self {
            schema_version,
            config,
            state,
        })
    }

    /// Durably persist this snapshot into `dir`.
    ///
    /// Writes the serialized blob to a per-call-unique scratch file, `fsync`s it
    /// so its bytes reach disk, `rename`s it onto the live store (an atomic
    /// replace — a reader never sees a half-written blob), then `fsync`s the
    /// parent directory so the rename itself survives a crash. The scratch name
    /// carries a `<pid>.<seq>` suffix so two writers persisting to the same
    /// directory cannot rename over each other's scratch file.
    ///
    /// Guarantee scope: a single `save_to_path` is atomic; serializing concurrent
    /// writers against one directory (last-writer-wins otherwise) is the caller's
    /// responsibility (the live engine is single-writer per strategy host).
    pub fn save_to_path(&self, dir: &Path) -> Result<(), PersistenceError> {
        fs::create_dir_all(dir).map_err(|err| io_error("create live-state directory", &err))?;
        let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
        let tmp_path = dir.join(format!("{STORE_TMP_FILENAME}.{}.{seq}", std::process::id()));
        let final_path = dir.join(STORE_FILENAME);

        let mut scratch = fs::File::create(&tmp_path)
            .map_err(|err| io_error("create live-state scratch", &err))?;
        if let Err(err) = io::Write::write_all(&mut scratch, self.serialize().as_bytes())
            .and_then(|()| scratch.sync_all())
        {
            let _ = fs::remove_file(&tmp_path);
            return Err(io_error("write live-state scratch", &err));
        }
        drop(scratch);

        fs::rename(&tmp_path, &final_path).map_err(|err| {
            let _ = fs::remove_file(&tmp_path);
            io_error("publish live-state file", &err)
        })?;

        let dir_handle =
            fs::File::open(dir).map_err(|err| io_error("open live-state directory", &err))?;
        dir_handle
            .sync_all()
            .map_err(|err| io_error("sync live-state directory", &err))?;
        Ok(())
    }

    /// Load a snapshot previously written by [`save_to_path`](Self::save_to_path)
    /// for restart **recovery** — fail-closed by default.
    ///
    /// Fail-closed taxonomy (recovery assumes durable state SHOULD be present; it
    /// never silently substitutes an empty state, which would defeat the
    /// "without duplicate submissions" guarantee):
    /// * `dir` **absent or not a directory** → [`PersistenceError::Io`]. An
    ///   unmounted / deleted store path is a configuration failure.
    /// * `dir` exists but holds **no snapshot file** → [`PersistenceError::Io`].
    ///   A missing snapshot during recovery could mean a lost / mis-mounted /
    ///   removed file after orders were live; restoring empty there would drop the
    ///   ledger and pending broker state and could allow duplicate submissions.
    ///   A **genuine first start** (which legitimately has no prior state) must
    ///   NOT call this — it constructs an empty snapshot explicitly via
    ///   [`capture`](Self::capture) over `LiveExecutionState::new(OrderLedger::new())`.
    ///   Distinguishing first-run from a lost file across process boundaries is the
    ///   orchestrator's container-lifecycle knowledge (a fresh container vs a
    ///   restart) — a durable "initialized" manifest is the SRS-DATA-018
    ///   backup/recovery concern and out of scope here.
    /// * A **present** file is decoded through the fail-closed
    ///   [`deserialize`](Self::deserialize), so a corrupt / truncated /
    ///   checksum-mismatching blob returns an [`Err`], never a partial state.
    pub fn load_from_path(dir: &Path) -> Result<Self, PersistenceError> {
        if !dir.is_dir() {
            return Err(PersistenceError::Io {
                context: "live-state directory is missing or not a directory",
            });
        }
        let final_path = dir.join(STORE_FILENAME);
        match fs::read_to_string(&final_path) {
            Ok(contents) => Self::deserialize(&contents),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Err(PersistenceError::Io {
                context: "no durable live-state snapshot present during recovery \
                          (a restart expects prior state; a genuine first start \
                          initializes an empty snapshot explicitly, it does not recover)",
            }),
            Err(err) => Err(io_error("read live-state file", &err)),
        }
    }
}

// --------------------------------------------------------------------------- //
// Restart recovery orchestration (SRS-EXE-005 + SRS-SDK-005 warm-up)
// --------------------------------------------------------------------------- //

/// The seam through which the restart flow re-executes the SRS-SDK-005 warm-up.
///
/// The warm-up mechanism itself (replaying historical bars to rebuild indicator
/// buffers / rolling windows) is a strategy-container concern owned by
/// SRS-SDK-005; this port is how [`recover`] re-triggers it for each strategy
/// with recovered state on restart. A failure fails the recovery closed — a
/// strategy must not resume live trading with un-warmed indicators.
pub trait WarmUpReexecutionPort {
    fn reexecute_warmup(&self, strategy: &StrategyId) -> Result<(), WarmUpError>;
}

/// A warm-up re-execution failure for one strategy (SRS-SDK-005).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WarmUpError {
    pub strategy: String,
    pub reason: String,
}

impl WarmUpError {
    pub fn new(strategy: impl Into<String>, reason: impl Into<String>) -> Self {
        Self {
            strategy: strategy.into(),
            reason: reason.into(),
        }
    }
}

impl fmt::Display for WarmUpError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "warm-up re-execution failed for strategy {:?}: {}",
            self.strategy, self.reason
        )
    }
}

impl std::error::Error for WarmUpError {}

/// The outcome of a successful restart recovery.
#[derive(Debug)]
pub struct RecoveryOutcome {
    snapshot: LiveStateSnapshot,
    restored_order_count: usize,
    warmup_reexecuted: Vec<StrategyId>,
    restore_elapsed: Duration,
}

impl RecoveryOutcome {
    /// The recovered snapshot (its restored [`LiveExecutionState`] and config).
    pub fn snapshot(&self) -> &LiveStateSnapshot {
        &self.snapshot
    }

    /// Consume the outcome and take the recovered snapshot.
    pub fn into_snapshot(self) -> LiveStateSnapshot {
        self.snapshot
    }

    /// The number of orders restored into the ledger.
    pub fn restored_order_count(&self) -> usize {
        self.restored_order_count
    }

    /// The strategies whose warm-up was re-executed, in canonical order.
    pub fn warmup_reexecuted(&self) -> &[StrategyId] {
        &self.warmup_reexecuted
    }

    /// The measured state-restore duration (excluding warm-up).
    pub fn restore_elapsed(&self) -> Duration {
        self.restore_elapsed
    }
}

/// An error that aborts restart recovery (fail-closed — the strategy does not
/// resume live trading on any of these).
#[derive(Debug)]
pub enum RecoveryError {
    /// The snapshot could not be loaded / decoded.
    Persistence(PersistenceError),
    /// The state-restore phase overran the NFR-R3 deadline (excluding warm-up).
    RestoreDeadlineExceeded {
        elapsed_secs: u64,
        deadline_secs: u64,
    },
    /// A strategy's warm-up re-execution failed.
    WarmUp(WarmUpError),
}

impl fmt::Display for RecoveryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Persistence(err) => write!(formatter, "live-state recovery failed: {err}"),
            Self::RestoreDeadlineExceeded {
                elapsed_secs,
                deadline_secs,
            } => write!(
                formatter,
                "live-state restore took {elapsed_secs}s, exceeding the {deadline_secs}s \
                 recovery deadline (NFR-R3, excluding warm-up)"
            ),
            Self::WarmUp(err) => write!(formatter, "{err}"),
        }
    }
}

impl std::error::Error for RecoveryError {}

/// Recover live execution state from an already-loaded `snapshot` after a restart
/// (SRS-EXE-005). `restore_elapsed` is the wall-clock time the caller measured for
/// the state-restore phase (load + deserialize + ledger rebuild), which
/// **excludes** warm-up per the AC.
///
/// Order of operations (all fail closed):
/// 1. enforce the NFR-R3 restore deadline over `restore_elapsed`;
/// 2. re-execute the SRS-SDK-005 warm-up for every strategy with recovered state
///    (the config always mandates it), aborting if any warm-up fails.
///
/// The restored ledger keeps its `(strategy, correlation id)` idempotency keys, so
/// a re-submission after recovery is rejected as a duplicate — the AC's "survive
/// restart without duplicate submissions".
pub fn recover(
    snapshot: LiveStateSnapshot,
    warmup: &dyn WarmUpReexecutionPort,
    restore_elapsed: Duration,
) -> Result<RecoveryOutcome, RecoveryError> {
    snapshot.config().restore_within_deadline(restore_elapsed)?;

    let mut warmup_reexecuted = Vec::new();
    if snapshot.config().reexecute_warmup_on_restart() {
        for strategy in snapshot.state().strategies_with_state() {
            warmup
                .reexecute_warmup(&strategy)
                .map_err(RecoveryError::WarmUp)?;
            warmup_reexecuted.push(strategy);
        }
    }

    let restored_order_count = snapshot.state().orders().len();
    Ok(RecoveryOutcome {
        snapshot,
        restored_order_count,
        warmup_reexecuted,
        restore_elapsed,
    })
}

/// Load the live-state snapshot from `dir` and recover it (SRS-EXE-005), measuring
/// the state-restore phase with a monotonic clock for the NFR-R3 deadline. This is
/// the production entry point; [`recover`] is the deterministic core a test drives
/// with an injected `restore_elapsed`.
pub fn recover_from_path(
    dir: &Path,
    warmup: &dyn WarmUpReexecutionPort,
) -> Result<RecoveryOutcome, RecoveryError> {
    let start = std::time::Instant::now();
    let snapshot = LiveStateSnapshot::load_from_path(dir).map_err(RecoveryError::Persistence)?;
    let restore_elapsed = start.elapsed();
    recover(snapshot, warmup, restore_elapsed)
}

// --------------------------------------------------------------------------- //
// Errors
// --------------------------------------------------------------------------- //

/// Fail-closed errors from the live-state persistence codec and store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PersistenceError {
    /// A structurally malformed snapshot (bad magic, a missing newline, a
    /// malformed integer, a truncated string, or trailing garbage).
    CorruptSnapshot { context: &'static str },
    /// The body integrity checksum did not match (accidental corruption or a
    /// structurally-valid byte change under fault injection).
    ChecksumMismatch,
    /// The snapshot's schema version is not the one this reader understands.
    UnknownSchemaVersion { found: i64 },
    /// A field violated an invariant (empty/non-canonical symbol, a fill/broker ID
    /// for an unknown order, a non-positive fill price, a flat position, ...).
    InconsistentField { context: &'static str },
    /// A record appeared more than once where it must be unique (a symbol, a
    /// broker ID for one order).
    DuplicateRecord { context: &'static str },
    /// A snapshot section this version cannot restore was non-empty.
    UnsupportedSection { context: &'static str },
    /// A snapshot encoded warm-up re-execution as disabled, which the AC forbids.
    WarmUpReexecutionRequired,
    /// A recovery config value was out of contract.
    InvalidConfig { context: &'static str },
    /// Restoring the order ledger failed a cross-order invariant.
    Ledger(OrderLifecycleError),
    /// An I/O failure reading or writing the durable store.
    Io { context: &'static str },
}

impl fmt::Display for PersistenceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CorruptSnapshot { context } => {
                write!(formatter, "corrupt live-state snapshot ({context})")
            }
            Self::ChecksumMismatch => {
                formatter.write_str("live-state snapshot integrity checksum mismatch")
            }
            Self::UnknownSchemaVersion { found } => {
                write!(formatter, "unknown live-state schema version {found}")
            }
            Self::InconsistentField { context } => {
                write!(formatter, "inconsistent live-state field ({context})")
            }
            Self::DuplicateRecord { context } => {
                write!(formatter, "duplicate live-state record ({context})")
            }
            Self::UnsupportedSection { context } => {
                write!(formatter, "unsupported live-state section ({context})")
            }
            Self::WarmUpReexecutionRequired => formatter.write_str(
                "live-state snapshot disabled warm-up re-execution, which the AC mandates",
            ),
            Self::InvalidConfig { context } => {
                write!(formatter, "invalid recovery config ({context})")
            }
            Self::Ledger(err) => write!(formatter, "live-state ledger restore failed: {err}"),
            Self::Io { context } => write!(formatter, "live-state I/O failure ({context})"),
        }
    }
}

impl std::error::Error for PersistenceError {}

fn io_error(context: &'static str, _err: &io::Error) -> PersistenceError {
    PersistenceError::Io { context }
}

// --------------------------------------------------------------------------- //
// Order (de)serialization helpers
// --------------------------------------------------------------------------- //

/// Total order over an [`OrderKey`] by `(strategy, correlation id)` string, so a
/// snapshot serializes deterministically (an `OrderKey` is not itself `Ord`).
fn order_key_sort(a: &OrderKey, b: &OrderKey) -> std::cmp::Ordering {
    a.strategy_id()
        .as_str()
        .cmp(b.strategy_id().as_str())
        .then_with(|| a.correlation_id().as_str().cmp(b.correlation_id().as_str()))
}

/// Serialize one order lifecycle: its key (strategy + correlation id), its
/// submission intent, its state, and any cancel-replace `replaces` link.
fn push_order(body: &mut String, order: &OrderLifecycle) {
    // Key (also supplies the submission's strategy on restore, so we never
    // serialize a mismatched strategy).
    push_str(body, order.strategy_id().as_str());
    push_str(body, order.correlation_id().as_str());
    push_str(body, order.state().as_str());

    // Submission intent (strategy derives from the key above).
    let submission = order.submission();
    push_str(body, &submission.symbol);
    push_i128(body, i128::from(submission.quantity));
    push_str(body, submission.asset_class.as_str());
    push_str(body, submission.side.as_str());
    push_order_type(body, submission.order_type);

    // Cancel-replace audit link: a flag then, if present, the original key.
    match order.replaces() {
        Some(original) => {
            push_i128(body, 1);
            push_str(body, original.strategy_id().as_str());
            push_str(body, original.correlation_id().as_str());
        }
        None => push_i128(body, 0),
    }
}

/// Serialize an order type as its wire string plus a present-flag + value for each
/// of its two possible prices, so any variant round-trips.
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

/// Read one order key `(strategy, correlation id)`, failing closed on a blank id.
fn read_order_key(cursor: &mut Cursor<'_>) -> Result<OrderKey, PersistenceError> {
    let strategy = cursor.read_str("strategy id")?;
    if strategy.trim().is_empty() {
        return Err(PersistenceError::InconsistentField {
            context: "empty strategy id",
        });
    }
    let correlation = cursor.read_str("correlation id")?;
    let correlation_id = ClientCorrelationId::new(correlation).map_err(PersistenceError::Ledger)?;
    Ok(OrderKey::new(StrategyId::new(strategy), correlation_id))
}

/// Read and validate one persisted order lifecycle.
fn read_order(cursor: &mut Cursor<'_>) -> Result<OrderLifecycle, PersistenceError> {
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
    // Read↔write symmetry: a restored order intent must pass the SAME shared
    // authority (`OrderSubmission::validate` — non-blank symbol, strictly-positive
    // quantity, positive prices, and options fail-closed pending contract identity)
    // that every live/paper intake applies, so a checksum-valid but structurally
    // impossible snapshot (blank symbol, zero/negative quantity, an option order
    // that could never have been submitted live) fails closed rather than
    // rehydrating an intent the engine would never have admitted.
    submission
        .validate()
        .map_err(|_| PersistenceError::InconsistentField {
            context: "restored order fails submission validation",
        })?;

    let replaces = match cursor.read_count("replaces flag")? {
        0 => None,
        1 => Some(read_order_key(cursor)?),
        _ => {
            return Err(PersistenceError::CorruptSnapshot {
                context: "replaces flag not 0/1",
            })
        }
    };

    Ok(OrderLifecycle::restore(key, submission, state, replaces))
}

fn read_order_state(cursor: &mut Cursor<'_>) -> Result<OrderState, PersistenceError> {
    let wire = cursor.read_str("order state")?;
    match wire.as_str() {
        "NEW" => Ok(OrderState::New),
        "PENDING_SUBMIT" => Ok(OrderState::PendingSubmit),
        "ACKED" => Ok(OrderState::Acked),
        "PARTIALLY_FILLED" => Ok(OrderState::PartiallyFilled),
        "FILLED" => Ok(OrderState::Filled),
        "CANCEL_PENDING" => Ok(OrderState::CancelPending),
        "CANCELLED" => Ok(OrderState::Cancelled),
        "REJECTED" => Ok(OrderState::Rejected),
        "EXPIRED" => Ok(OrderState::Expired),
        _ => Err(PersistenceError::InconsistentField {
            context: "unknown order state",
        }),
    }
}

fn read_asset_class(cursor: &mut Cursor<'_>) -> Result<AssetClass, PersistenceError> {
    match cursor.read_str("asset class")?.as_str() {
        "EQUITY" => Ok(AssetClass::Equity),
        "OPTION" => Ok(AssetClass::Option),
        _ => Err(PersistenceError::InconsistentField {
            context: "unknown asset class",
        }),
    }
}

fn read_order_side(cursor: &mut Cursor<'_>) -> Result<OrderSide, PersistenceError> {
    match cursor.read_str("order side")?.as_str() {
        "BUY" => Ok(OrderSide::Buy),
        "SELL" => Ok(OrderSide::Sell),
        _ => Err(PersistenceError::InconsistentField {
            context: "unknown order side",
        }),
    }
}

/// Read an order type, validating that the price present-flags match the variant
/// and that any present price is strictly positive (re-running the shared
/// [`OrderType::validate_prices`] authority so a tampered price fails closed).
fn read_order_type(cursor: &mut Cursor<'_>) -> Result<OrderType, PersistenceError> {
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
            return Err(PersistenceError::InconsistentField {
                context: "order type / price presence mismatch",
            })
        }
    };
    order_type
        .validate_prices()
        .map_err(|_| PersistenceError::InconsistentField {
            context: "non-positive order-type price",
        })?;
    Ok(order_type)
}

/// Read a present-flag then, if present, an `i64` price.
fn read_optional_price(
    cursor: &mut Cursor<'_>,
    context: &'static str,
) -> Result<Option<i64>, PersistenceError> {
    match cursor.read_count(context)? {
        0 => Ok(None),
        1 => Ok(Some(cursor.read_i64(context)?)),
        _ => Err(PersistenceError::CorruptSnapshot {
            context: "price present-flag not 0/1",
        }),
    }
}

// --------------------------------------------------------------------------- //
// Deterministic, dependency-free text codec (parallel to `paper_state`)
// --------------------------------------------------------------------------- //

fn push_line(out: &mut String, value: &str) {
    out.push_str(value);
    out.push('\n');
}

fn push_i128(out: &mut String, value: i128) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append a length-prefixed string: the byte length on one line, then the bytes
/// followed by a newline, so any byte in the value round-trips without escaping.
fn push_str(out: &mut String, value: &str) {
    out.push_str(&value.len().to_string());
    out.push('\n');
    out.push_str(value);
    out.push('\n');
}

/// Validate that `s` is a well-formed JSON **object** (the AC's "JSON-serializable
/// state dictionary"). A compact, dependency-free structural validator — the
/// workspace carries no JSON crate by design — over the full JSON grammar
/// (objects, arrays, strings with escapes, numbers, `true`/`false`/`null`,
/// whitespace). It is a *validator*, not a deserializer: it confirms the value
/// parses as a single top-level object with no trailing garbage, so a non-JSON or
/// non-object user dictionary fails closed at capture/restore instead of breaking
/// the strategy later.
fn is_json_object(s: &str) -> bool {
    let mut v = JsonValidator {
        bytes: s.as_bytes(),
        pos: 0,
    };
    v.skip_ws();
    // The dictionary must be a JSON object at the top level (not an array/scalar).
    if v.peek() != Some(b'{') || !v.object() {
        return false;
    }
    v.skip_ws();
    v.pos == v.bytes.len()
}

/// A recursive-descent JSON grammar validator (structure only, no value capture).
struct JsonValidator<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl JsonValidator<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn bump(&mut self) -> Option<u8> {
        let c = self.peek();
        if c.is_some() {
            self.pos += 1;
        }
        c
    }

    fn eat(&mut self, c: u8) -> bool {
        if self.peek() == Some(c) {
            self.pos += 1;
            true
        } else {
            false
        }
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn value(&mut self) -> bool {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => self.string(),
            Some(b't') => self.literal(b"true"),
            Some(b'f') => self.literal(b"false"),
            Some(b'n') => self.literal(b"null"),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => false,
        }
    }

    fn object(&mut self) -> bool {
        if !self.eat(b'{') {
            return false;
        }
        self.skip_ws();
        if self.eat(b'}') {
            return true; // empty object
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') || !self.string() {
                return false; // keys must be strings
            }
            self.skip_ws();
            if !self.eat(b':') || !self.value() {
                return false;
            }
            self.skip_ws();
            if self.eat(b',') {
                continue;
            }
            return self.eat(b'}');
        }
    }

    fn array(&mut self) -> bool {
        if !self.eat(b'[') {
            return false;
        }
        self.skip_ws();
        if self.eat(b']') {
            return true; // empty array
        }
        loop {
            if !self.value() {
                return false;
            }
            self.skip_ws();
            if self.eat(b',') {
                continue;
            }
            return self.eat(b']');
        }
    }

    fn string(&mut self) -> bool {
        if !self.eat(b'"') {
            return false;
        }
        loop {
            match self.bump() {
                None => return false,
                Some(b'"') => return true,
                Some(b'\\') => match self.bump() {
                    Some(b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't') => {}
                    Some(b'u') => {
                        for _ in 0..4 {
                            match self.bump() {
                                Some(c) if c.is_ascii_hexdigit() => {}
                                _ => return false,
                            }
                        }
                    }
                    _ => return false,
                },
                // Unescaped control characters are not allowed in a JSON string.
                Some(c) if c < 0x20 => return false,
                Some(_) => {} // any other byte (UTF-8 continuation bytes included)
            }
        }
    }

    fn number(&mut self) -> bool {
        self.eat(b'-');
        match self.peek() {
            Some(b'0') => self.pos += 1,
            Some(c) if (b'1'..=b'9').contains(&c) => {
                while matches!(self.peek(), Some(d) if d.is_ascii_digit()) {
                    self.pos += 1;
                }
            }
            _ => return false,
        }
        if self.peek() == Some(b'.') {
            self.pos += 1;
            if !matches!(self.peek(), Some(d) if d.is_ascii_digit()) {
                return false;
            }
            while matches!(self.peek(), Some(d) if d.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(d) if d.is_ascii_digit()) {
                return false;
            }
            while matches!(self.peek(), Some(d) if d.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        true
    }

    fn literal(&mut self, lit: &[u8]) -> bool {
        if self.bytes[self.pos..].starts_with(lit) {
            self.pos += lit.len();
            true
        } else {
            false
        }
    }
}

/// A 64-bit FNV-1a integrity checksum over the serialized body. Non-cryptographic:
/// it detects *accidental* corruption (bit flips, truncation, a value changed to
/// another structurally-valid value) under fault injection, not a deliberate
/// tamperer who recomputes it (that needs a keyed MAC + key management, out of
/// scope for the single-user, local-only baseline). Integer-only, dependency-free.
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
/// fail closed: a missing newline, a malformed integer, a truncated
/// length-prefixed string, or trailing garbage all surface as an [`Err`].
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

    fn read_line(&mut self, context: &'static str) -> Result<&'a str, PersistenceError> {
        let start = self.pos;
        while self.pos < self.bytes.len() && self.bytes[self.pos] != b'\n' {
            self.pos += 1;
        }
        if self.pos >= self.bytes.len() {
            return Err(PersistenceError::CorruptSnapshot { context });
        }
        let line = &self.bytes[start..self.pos];
        self.pos += 1; // consume the '\n'
        std::str::from_utf8(line).map_err(|_| PersistenceError::CorruptSnapshot { context })
    }

    fn read_i128(&mut self, context: &'static str) -> Result<i128, PersistenceError> {
        self.read_line(context)?
            .parse::<i128>()
            .map_err(|_| PersistenceError::CorruptSnapshot { context })
    }

    fn read_i64(&mut self, context: &'static str) -> Result<i64, PersistenceError> {
        self.read_line(context)?
            .parse::<i64>()
            .map_err(|_| PersistenceError::CorruptSnapshot { context })
    }

    fn read_u64(&mut self, context: &'static str) -> Result<u64, PersistenceError> {
        self.read_line(context)?
            .parse::<u64>()
            .map_err(|_| PersistenceError::CorruptSnapshot { context })
    }

    fn read_bool(&mut self, context: &'static str) -> Result<bool, PersistenceError> {
        match self.read_i64(context)? {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(PersistenceError::CorruptSnapshot { context }),
        }
    }

    /// Read a non-negative count, rejecting a negative value (a length can never
    /// be negative).
    fn read_count(&mut self, context: &'static str) -> Result<usize, PersistenceError> {
        let value = self.read_i128(context)?;
        if value < 0 {
            return Err(PersistenceError::CorruptSnapshot { context });
        }
        usize::try_from(value).map_err(|_| PersistenceError::CorruptSnapshot { context })
    }

    /// Read a length-prefixed string (the byte length line, then that many bytes,
    /// then a `\n`), failing closed on a truncated or non-UTF-8 value.
    fn read_str(&mut self, context: &'static str) -> Result<String, PersistenceError> {
        let len = self.read_count(context)?;
        // `checked_add` so a crafted huge length fails closed with an Err instead
        // of overflowing the addition (and then panicking on the slice) — recovery
        // must never panic on a malformed snapshot.
        let value_end = self
            .pos
            .checked_add(len)
            .ok_or(PersistenceError::CorruptSnapshot { context })?;
        // Need `len` value bytes AND the trailing '\n' at `value_end`.
        if value_end >= self.bytes.len() {
            return Err(PersistenceError::CorruptSnapshot { context });
        }
        if self.bytes[value_end] != b'\n' {
            return Err(PersistenceError::CorruptSnapshot { context });
        }
        let value = &self.bytes[self.pos..value_end];
        self.pos = value_end + 1; // consume the value bytes and the '\n'
        std::str::from_utf8(value)
            .map(str::to_string)
            .map_err(|_| PersistenceError::CorruptSnapshot { context })
    }

    /// Assert the cursor is exactly at the end (no trailing garbage).
    fn expect_end(&self) -> Result<(), PersistenceError> {
        if self.pos == self.bytes.len() {
            Ok(())
        } else {
            Err(PersistenceError::CorruptSnapshot {
                context: "trailing bytes after snapshot",
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::OrderErrorCategory;
    use std::cell::RefCell;

    // --------------------------------------------------------------------- //
    // Builders
    // --------------------------------------------------------------------- //

    fn corr(id: &str) -> ClientCorrelationId {
        ClientCorrelationId::new(id).expect("non-empty id")
    }

    fn key(strat: &str, id: &str) -> OrderKey {
        OrderKey::new(StrategyId::new(strat), corr(id))
    }

    fn submission(strat: &str, symbol: &str, qty: i64, order_type: OrderType) -> OrderSubmission {
        OrderSubmission {
            strategy_id: StrategyId::new(strat),
            symbol: symbol.to_string(),
            quantity: qty,
            asset_class: AssetClass::Equity,
            side: OrderSide::Buy,
            order_type,
        }
    }

    /// A ledger with two orders under `strat-1` (one ACKED market, one NEW limit).
    fn sample_ledger() -> OrderLedger {
        let mut ledger = OrderLedger::new();
        ledger
            .submit(
                corr("c-1"),
                &submission("strat-1", "AAPL", 10, OrderType::Market),
            )
            .unwrap();
        ledger
            .transition(&key("strat-1", "c-1"), OrderState::PendingSubmit)
            .unwrap();
        ledger
            .transition(&key("strat-1", "c-1"), OrderState::Acked)
            .unwrap();
        ledger
            .submit(
                corr("c-2"),
                &submission(
                    "strat-1",
                    "MSFT",
                    5,
                    OrderType::Limit {
                        limit_price_minor: 30_000,
                    },
                ),
            )
            .unwrap();
        ledger
    }

    fn sample_state() -> LiveExecutionState {
        LiveExecutionState::new(sample_ledger())
            .with_broker_id(key("strat-1", "c-1"), "IB-8001")
            .unwrap()
            .with_fill(FillEventRecord::new(key("strat-1", "c-1"), 1, 10, 19_050).unwrap())
            .unwrap()
            .with_position("AAPL", 10)
            .unwrap()
            .with_position("MSFT", -3)
            .unwrap()
            .with_equity(AccountEquitySnapshot::new(1_000_000, 250_000))
            .with_user_state_json(r#"{"phase":"scaling","n":3}"#)
            .unwrap()
    }

    struct RecordingWarmUp {
        calls: RefCell<Vec<String>>,
    }
    impl RecordingWarmUp {
        fn new() -> Self {
            Self {
                calls: RefCell::new(Vec::new()),
            }
        }
    }
    impl WarmUpReexecutionPort for RecordingWarmUp {
        fn reexecute_warmup(&self, strategy: &StrategyId) -> Result<(), WarmUpError> {
            self.calls.borrow_mut().push(strategy.as_str().to_string());
            Ok(())
        }
    }

    struct FailingWarmUp {
        fail_for: String,
    }
    impl WarmUpReexecutionPort for FailingWarmUp {
        fn reexecute_warmup(&self, strategy: &StrategyId) -> Result<(), WarmUpError> {
            if strategy.as_str() == self.fail_for {
                Err(WarmUpError::new(
                    strategy.as_str(),
                    "warm-up data unavailable",
                ))
            } else {
                Ok(())
            }
        }
    }

    static TEST_DIR_SEQ: AtomicU64 = AtomicU64::new(0);
    fn temp_dir() -> std::path::PathBuf {
        let seq = TEST_DIR_SEQ.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("atp-exe005-{}-{}", std::process::id(), seq));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Frame a raw body with the magic header and a correct integrity checksum, so
    /// a test can craft a *valid-checksum* but structurally-specific blob (to
    /// exercise fail-closed field validation past the checksum gate).
    fn frame(body: &str) -> String {
        let mut out = String::new();
        push_line(&mut out, MAGIC);
        push_i128(&mut out, i128::from(checksum(body.as_bytes())));
        out.push_str(body);
        out
    }

    /// A minimal empty-state body with a chosen warm-up-reexecution flag.
    fn minimal_body(reexecute_warmup: i128) -> String {
        let mut body = String::new();
        push_i128(&mut body, i128::from(SCHEMA_VERSION));
        push_i128(&mut body, 60); // restore_deadline_secs
        push_i128(&mut body, reexecute_warmup);
        push_i128(&mut body, 0); // order count
        push_i128(&mut body, 0); // broker id count
        push_i128(&mut body, 0); // fill count
        push_i128(&mut body, 0); // position count
        push_i128(&mut body, 0); // live strategy count
        push_i128(&mut body, 0); // cash
        push_i128(&mut body, 0); // market value
        push_str(&mut body, "{}");
        body
    }

    // --------------------------------------------------------------------- //
    // Round-trip / determinism
    // --------------------------------------------------------------------- //

    #[test]
    fn round_trip_reproduces_full_state() {
        let snapshot = LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default());
        let serialized = snapshot.serialize();
        let restored = LiveStateSnapshot::deserialize(&serialized).unwrap();

        assert_eq!(restored.schema_version(), SCHEMA_VERSION);
        assert_eq!(restored.config(), &RecoveryConfig::default());

        let state = restored.state();
        assert_eq!(state.orders().len(), 2);
        assert_eq!(
            state.orders().state(&key("strat-1", "c-1")).unwrap(),
            OrderState::Acked
        );
        assert_eq!(
            state.orders().state(&key("strat-1", "c-2")).unwrap(),
            OrderState::New
        );
        assert_eq!(state.broker_id(&key("strat-1", "c-1")), Some("IB-8001"));
        assert_eq!(state.fills().len(), 1);
        assert_eq!(state.fills()[0].fill_price_minor(), 19_050);
        assert_eq!(state.open_position("AAPL"), Some(10));
        assert_eq!(state.open_position("MSFT"), Some(-3));
        assert_eq!(state.equity().net_liquidation_minor(), 1_250_000);
        assert_eq!(state.user_state_json(), r#"{"phase":"scaling","n":3}"#);

        // Re-serializing the restored snapshot is byte-identical (faithful).
        assert_eq!(restored.serialize(), serialized);
    }

    #[test]
    fn round_trip_preserves_a_cancel_replace_link_and_order_types() {
        let mut ledger = OrderLedger::new();
        ledger
            .submit(
                corr("orig"),
                &submission(
                    "strat-1",
                    "SPY",
                    100,
                    OrderType::Stop {
                        stop_price_minor: 45_000,
                    },
                ),
            )
            .unwrap();
        ledger
            .transition(&key("strat-1", "orig"), OrderState::PendingSubmit)
            .unwrap();
        ledger
            .transition(&key("strat-1", "orig"), OrderState::Acked)
            .unwrap();
        ledger
            .cancel_replace(
                &key("strat-1", "orig"),
                &submission(
                    "strat-1",
                    "SPY",
                    60,
                    OrderType::StopLimit {
                        stop_price_minor: 45_000,
                        limit_price_minor: 45_100,
                    },
                ),
                corr("repl"),
            )
            .unwrap();

        let snapshot =
            LiveStateSnapshot::capture(LiveExecutionState::new(ledger), RecoveryConfig::default());
        let restored = LiveStateSnapshot::deserialize(&snapshot.serialize()).unwrap();
        let orders = restored.state().orders();

        assert_eq!(
            orders.state(&key("strat-1", "orig")).unwrap(),
            OrderState::CancelPending
        );
        let replacement = orders.get(&key("strat-1", "repl")).unwrap();
        assert_eq!(replacement.replaces(), Some(&key("strat-1", "orig")));
        assert_eq!(replacement.submission().quantity, 60);
        assert_eq!(
            replacement.submission().order_type,
            OrderType::StopLimit {
                stop_price_minor: 45_000,
                limit_price_minor: 45_100
            }
        );
    }

    #[test]
    fn serialization_is_byte_identical_regardless_of_build_order() {
        let a = LiveExecutionState::new(sample_ledger())
            .with_position("AAPL", 10)
            .unwrap()
            .with_position("MSFT", -3)
            .unwrap();
        let b = LiveExecutionState::new(sample_ledger())
            .with_position("MSFT", -3)
            .unwrap()
            .with_position("AAPL", 10)
            .unwrap();
        let sa = LiveStateSnapshot::capture(a, RecoveryConfig::default()).serialize();
        let sb = LiveStateSnapshot::capture(b, RecoveryConfig::default()).serialize();
        assert_eq!(sa, sb);
    }

    // --------------------------------------------------------------------- //
    // The AC spine: survive restart WITHOUT duplicate submissions
    // --------------------------------------------------------------------- //

    #[test]
    fn no_duplicate_submission_after_restart() {
        // Persist a ledger with c-1 ACKED, then "restart": serialize -> deserialize.
        let snapshot = LiveStateSnapshot::capture(
            LiveExecutionState::new(sample_ledger()),
            RecoveryConfig::default(),
        );
        let serialized = snapshot.serialize();
        let restored = LiveStateSnapshot::deserialize(&serialized).unwrap();
        let mut ledger = restored.into_state().orders;

        // A re-submission of c-1 after the restart is rejected as a duplicate; the
        // existing order is untouched (still ACKED), and no second order is made.
        let err = ledger
            .submit(
                corr("c-1"),
                &submission("strat-1", "AAPL", 10, OrderType::Market),
            )
            .unwrap_err();
        assert_eq!(
            err.category,
            OrderErrorCategory::DuplicateClientCorrelationId
        );
        assert_eq!(
            ledger.state(&key("strat-1", "c-1")).unwrap(),
            OrderState::Acked
        );
        assert_eq!(ledger.len(), 2);
    }

    // --------------------------------------------------------------------- //
    // Fail-closed codec (fault injection)
    // --------------------------------------------------------------------- //

    #[test]
    fn corrupt_magic_fails_closed() {
        let mut serialized =
            LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default()).serialize();
        serialized.replace_range(0..3, "XXX");
        assert!(matches!(
            LiveStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::CorruptSnapshot { .. })
        ));
    }

    #[test]
    fn checksum_mismatch_fails_closed() {
        // Flip a byte in the body (a structurally-valid change) -> ChecksumMismatch.
        let serialized =
            LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default()).serialize();
        let mut bytes = serialized.into_bytes();
        let last = bytes.len() - 2; // inside the body, before the trailing '\n'
        bytes[last] = if bytes[last] == b'0' { b'1' } else { b'0' };
        let tampered = String::from_utf8(bytes).unwrap();
        assert_eq!(
            LiveStateSnapshot::deserialize(&tampered).unwrap_err(),
            PersistenceError::ChecksumMismatch
        );
    }

    #[test]
    fn truncated_fails_closed() {
        let serialized =
            LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default()).serialize();
        let truncated = &serialized[..serialized.len() / 2];
        assert!(LiveStateSnapshot::deserialize(truncated).is_err());
    }

    #[test]
    fn trailing_garbage_fails_closed() {
        let mut serialized =
            LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default()).serialize();
        serialized.push_str("99\n");
        // The checksum covers only the original body, so extra bytes flip it.
        assert!(LiveStateSnapshot::deserialize(&serialized).is_err());
    }

    #[test]
    fn unknown_schema_version_fails_closed() {
        let mut body = minimal_body(1);
        // overwrite the schema-version first line with a bogus version.
        body = body.replacen(&format!("{SCHEMA_VERSION}\n"), "99\n", 1);
        assert!(matches!(
            LiveStateSnapshot::deserialize(&frame(&body)),
            Err(PersistenceError::UnknownSchemaVersion { found: 99 })
        ));
    }

    #[test]
    fn warmup_reexecution_disabled_in_blob_fails_closed() {
        // A valid-checksum blob that encodes warm-up as disabled is out of contract.
        assert_eq!(
            LiveStateSnapshot::deserialize(&frame(&minimal_body(0))).unwrap_err(),
            PersistenceError::WarmUpReexecutionRequired
        );
        // Sanity: the same body with the flag enabled restores an empty state.
        assert!(LiveStateSnapshot::deserialize(&frame(&minimal_body(1))).is_ok());
    }

    #[test]
    fn zero_restore_deadline_in_blob_fails_closed() {
        let body = minimal_body(1).replacen("60\n", "0\n", 1);
        assert!(matches!(
            LiveStateSnapshot::deserialize(&frame(&body)),
            Err(PersistenceError::InvalidConfig { .. })
        ));
    }

    #[test]
    fn tampered_non_positive_order_price_fails_closed() {
        // Craft a single LIMIT order carrying a zero limit price with a correct
        // checksum -> the order-type price re-validation rejects it.
        let mut body = String::new();
        push_i128(&mut body, i128::from(SCHEMA_VERSION));
        push_i128(&mut body, 60);
        push_i128(&mut body, 1);
        push_i128(&mut body, 1); // one order
        push_str(&mut body, "strat-1");
        push_str(&mut body, "c-1");
        push_str(&mut body, "NEW");
        push_str(&mut body, "AAPL");
        push_i128(&mut body, 10);
        push_str(&mut body, "EQUITY");
        push_str(&mut body, "BUY");
        push_str(&mut body, "LIMIT");
        push_i128(&mut body, 0); // stop absent
        push_i128(&mut body, 1); // limit present
        push_i128(&mut body, 0); // ...with price 0 (invalid)
        push_i128(&mut body, 0); // replaces flag
        push_i128(&mut body, 0); // broker ids
        push_i128(&mut body, 0); // fills
        push_i128(&mut body, 0); // positions
        push_i128(&mut body, 0);
        push_i128(&mut body, 0); // equity
        push_str(&mut body, "{}");
        assert!(matches!(
            LiveStateSnapshot::deserialize(&frame(&body)),
            Err(PersistenceError::InconsistentField { .. })
        ));
    }

    // --------------------------------------------------------------------- //
    // Builder & field validation
    // --------------------------------------------------------------------- //

    #[test]
    fn broker_id_or_fill_for_unknown_order_fails_closed() {
        let state = LiveExecutionState::new(sample_ledger());
        assert!(matches!(
            LiveExecutionState::new(sample_ledger())
                .with_broker_id(key("strat-1", "ghost"), "IB-1"),
            Err(PersistenceError::InconsistentField { .. })
        ));
        assert!(matches!(
            state.with_fill(FillEventRecord::new(key("strat-1", "ghost"), 1, 10, 100).unwrap()),
            Err(PersistenceError::InconsistentField { .. })
        ));
    }

    #[test]
    fn fill_and_position_field_validation() {
        assert!(FillEventRecord::new(key("strat-1", "c-1"), 1, 0, 100).is_err()); // zero qty
        assert!(FillEventRecord::new(key("strat-1", "c-1"), 1, 10, 0).is_err()); // non-positive price
        assert!(matches!(
            LiveExecutionState::new(sample_ledger()).with_position("", 10),
            Err(PersistenceError::InconsistentField { .. })
        ));
        assert!(matches!(
            LiveExecutionState::new(sample_ledger()).with_position("aapl", 10),
            Err(PersistenceError::InconsistentField { .. })
        ));
        assert!(matches!(
            LiveExecutionState::new(sample_ledger()).with_position("AAPL", 0),
            Err(PersistenceError::InconsistentField { .. })
        ));
    }

    // --------------------------------------------------------------------- //
    // Restart recovery orchestration (deadline + warm-up)
    // --------------------------------------------------------------------- //

    #[test]
    fn recovery_reexecutes_warmup_for_each_strategy_with_state() {
        let mut ledger = sample_ledger();
        ledger
            .submit(
                corr("z-1"),
                &submission("strat-2", "TSLA", 1, OrderType::Market),
            )
            .unwrap();
        let snapshot =
            LiveStateSnapshot::capture(LiveExecutionState::new(ledger), RecoveryConfig::default());

        let warmup = RecordingWarmUp::new();
        let outcome = recover(snapshot, &warmup, Duration::from_secs(2)).unwrap();

        assert_eq!(outcome.restored_order_count(), 3);
        let warmed: Vec<&str> = outcome
            .warmup_reexecuted()
            .iter()
            .map(StrategyId::as_str)
            .collect();
        assert_eq!(warmed, vec!["strat-1", "strat-2"]); // sorted, deduped
        assert_eq!(*warmup.calls.borrow(), vec!["strat-1", "strat-2"]);
    }

    #[test]
    fn recovery_fails_closed_when_restore_deadline_exceeded() {
        let snapshot = LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default());
        let warmup = RecordingWarmUp::new();
        let err = recover(snapshot, &warmup, Duration::from_secs(61)).unwrap_err();
        assert!(matches!(
            err,
            RecoveryError::RestoreDeadlineExceeded {
                deadline_secs: 60,
                ..
            }
        ));
        // Warm-up must NOT run if the deadline was already blown.
        assert!(warmup.calls.borrow().is_empty());
    }

    #[test]
    fn recovery_fails_closed_when_a_warmup_fails() {
        let snapshot = LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default());
        let warmup = FailingWarmUp {
            fail_for: "strat-1".to_string(),
        };
        let err = recover(snapshot, &warmup, Duration::from_secs(1)).unwrap_err();
        assert!(matches!(err, RecoveryError::WarmUp(_)));
    }

    // --------------------------------------------------------------------- //
    // Durable store (real file I/O)
    // --------------------------------------------------------------------- //

    #[test]
    fn save_then_load_round_trips_through_the_filesystem() {
        let dir = temp_dir();
        LiveStateSnapshot::capture(sample_state(), RecoveryConfig::default())
            .save_to_path(&dir)
            .unwrap();
        let loaded = LiveStateSnapshot::load_from_path(&dir).unwrap();
        assert_eq!(loaded.state().orders().len(), 2);
        assert_eq!(loaded.state().open_position("AAPL"), Some(10));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_from_missing_directory_fails_closed() {
        let dir = std::env::temp_dir().join(format!("atp-exe005-absent-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        assert!(matches!(
            LiveStateSnapshot::load_from_path(&dir),
            Err(PersistenceError::Io { .. })
        ));
    }

    #[test]
    fn load_from_directory_with_no_snapshot_file_fails_closed() {
        // Recovery must NOT silently restore empty when the snapshot file is
        // missing (a lost/mis-mounted file after orders were live) — that would
        // drop the ledger and could allow duplicate submissions.
        let dir = temp_dir();
        assert!(matches!(
            LiveStateSnapshot::load_from_path(&dir),
            Err(PersistenceError::Io { .. })
        ));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn warmup_reexecutes_for_a_registered_strategy_with_no_orders() {
        // A live strategy with recovered positions / user state but NO active order
        // must still be warmed up on restart (else it resumes on cold indicators).
        let state = LiveExecutionState::new(OrderLedger::new())
            .with_live_strategy(&StrategyId::new("cold-strat"))
            .unwrap()
            .with_position("AAPL", 5)
            .unwrap();
        let snapshot = LiveStateSnapshot::capture(state, RecoveryConfig::default());
        // survives the round trip...
        let restored = LiveStateSnapshot::deserialize(&snapshot.serialize()).unwrap();
        assert_eq!(restored.state().orders().len(), 0);
        // ...and warm-up runs for the registered strategy despite zero orders.
        let warmup = RecordingWarmUp::new();
        let outcome = recover(restored, &warmup, Duration::from_secs(1)).unwrap();
        assert_eq!(*warmup.calls.borrow(), vec!["cold-strat"]);
        assert_eq!(outcome.warmup_reexecuted().len(), 1);
    }

    #[test]
    fn load_of_a_corrupt_file_fails_closed_not_empty() {
        let dir = temp_dir();
        fs::write(dir.join(STORE_FILENAME), b"not a snapshot at all\n").unwrap();
        assert!(LiveStateSnapshot::load_from_path(&dir).is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn end_to_end_no_duplicate_submission_across_a_disk_restart() {
        // The fault-injection shape: persist -> (process dies) -> load -> the
        // restored ledger still rejects a duplicate submission.
        let dir = temp_dir();
        LiveStateSnapshot::capture(
            LiveExecutionState::new(sample_ledger()),
            RecoveryConfig::default(),
        )
        .save_to_path(&dir)
        .unwrap();

        let warmup = RecordingWarmUp::new();
        let outcome = recover_from_path(&dir, &warmup).unwrap();
        let mut ledger = outcome.into_snapshot().into_state().orders;

        let err = ledger
            .submit(
                corr("c-1"),
                &submission("strat-1", "AAPL", 10, OrderType::Market),
            )
            .unwrap_err();
        assert_eq!(
            err.category,
            OrderErrorCategory::DuplicateClientCorrelationId
        );
        assert!(!warmup.calls.borrow().is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    fn one_market_order_body(symbol: &str, quantity: i128) -> String {
        let mut body = String::new();
        push_i128(&mut body, i128::from(SCHEMA_VERSION));
        push_i128(&mut body, 60);
        push_i128(&mut body, 1);
        push_i128(&mut body, 1); // one order
        push_str(&mut body, "strat-1");
        push_str(&mut body, "c-1");
        push_str(&mut body, "NEW");
        push_str(&mut body, symbol);
        push_i128(&mut body, quantity);
        push_str(&mut body, "EQUITY");
        push_str(&mut body, "BUY");
        push_str(&mut body, "MARKET");
        push_i128(&mut body, 0); // stop absent
        push_i128(&mut body, 0); // limit absent
        push_i128(&mut body, 0); // replaces flag
        push_i128(&mut body, 0); // broker ids
        push_i128(&mut body, 0); // fills
        push_i128(&mut body, 0); // positions
        push_i128(&mut body, 0); // live strategies
        push_i128(&mut body, 0);
        push_i128(&mut body, 0); // equity
        push_str(&mut body, "{}");
        body
    }

    #[test]
    fn restored_order_with_blank_symbol_fails_closed() {
        // A checksum-valid snapshot whose order carries a blank symbol must fail
        // closed via OrderSubmission::validate, never rehydrate an unroutable order.
        assert!(matches!(
            LiveStateSnapshot::deserialize(&frame(&one_market_order_body("   ", 10))),
            Err(PersistenceError::InconsistentField { .. })
        ));
    }

    #[test]
    fn restored_order_with_non_positive_quantity_fails_closed() {
        for qty in [0i128, -5] {
            assert!(matches!(
                LiveStateSnapshot::deserialize(&frame(&one_market_order_body("AAPL", qty))),
                Err(PersistenceError::InconsistentField { .. })
            ));
        }
        // sanity: a well-formed order restores.
        assert!(LiveStateSnapshot::deserialize(&frame(&one_market_order_body("AAPL", 10))).is_ok());
    }

    #[test]
    fn crafted_huge_string_length_fails_closed_without_panic() {
        // A valid-checksum body whose first length-prefixed string claims a huge
        // length must return Err, never overflow the cursor arithmetic and panic.
        let mut body = String::new();
        push_i128(&mut body, i128::from(SCHEMA_VERSION));
        push_i128(&mut body, 60);
        push_i128(&mut body, 1);
        push_i128(&mut body, 1); // one order
        body.push_str(&format!("{}\n", u64::MAX)); // strategy-id length: absurd
        body.push_str("strat-1\n");
        assert!(matches!(
            LiveStateSnapshot::deserialize(&frame(&body)),
            Err(PersistenceError::CorruptSnapshot { .. })
        ));
    }

    fn body_one_order_with(fills: &[(u64, i64, i64)], user_state: &str) -> String {
        let mut body = String::new();
        push_i128(&mut body, i128::from(SCHEMA_VERSION));
        push_i128(&mut body, 60);
        push_i128(&mut body, 1);
        push_i128(&mut body, 1); // one order
        push_str(&mut body, "strat-1");
        push_str(&mut body, "c-1");
        push_str(&mut body, "NEW");
        push_str(&mut body, "AAPL");
        push_i128(&mut body, 10);
        push_str(&mut body, "EQUITY");
        push_str(&mut body, "BUY");
        push_str(&mut body, "MARKET");
        push_i128(&mut body, 0); // stop absent
        push_i128(&mut body, 0); // limit absent
        push_i128(&mut body, 0); // replaces flag
        push_i128(&mut body, 0); // broker ids
        push_i128(&mut body, fills.len() as i128);
        for (seq, qty, price) in fills {
            push_str(&mut body, "strat-1");
            push_str(&mut body, "c-1");
            push_i128(&mut body, i128::from(*seq));
            push_i128(&mut body, i128::from(*qty));
            push_i128(&mut body, i128::from(*price));
        }
        push_i128(&mut body, 0); // positions
        push_i128(&mut body, 0); // live strategies
        push_i128(&mut body, 0);
        push_i128(&mut body, 0); // equity
        push_str(&mut body, user_state);
        body
    }

    #[test]
    fn duplicate_fill_identity_fails_closed_in_builder() {
        let state = LiveExecutionState::new(sample_ledger())
            .with_fill(FillEventRecord::new(key("strat-1", "c-1"), 1, 10, 100).unwrap())
            .unwrap();
        // Same (order, sequence) again is a duplicate execution.
        assert!(matches!(
            state.with_fill(FillEventRecord::new(key("strat-1", "c-1"), 1, 10, 100).unwrap()),
            Err(PersistenceError::DuplicateRecord { .. })
        ));
    }

    #[test]
    fn crafted_snapshot_with_duplicate_fill_fails_closed() {
        // A valid-checksum snapshot that repeats a fill identity must not recover
        // (it would double-count the execution).
        assert!(matches!(
            LiveStateSnapshot::deserialize(&frame(&body_one_order_with(
                &[(1, 10, 100), (1, 10, 100)],
                "{}"
            ))),
            Err(PersistenceError::DuplicateRecord { .. })
        ));
        // ...but two distinct sequences for one order recover fine.
        let ok = LiveStateSnapshot::deserialize(&frame(&body_one_order_with(
            &[(1, 10, 100), (2, 5, 101)],
            "{}",
        )))
        .unwrap();
        assert_eq!(ok.state().fills().len(), 2);
    }

    #[test]
    fn user_state_must_be_a_json_object() {
        let base = || LiveExecutionState::new(sample_ledger());
        assert!(base().with_user_state_json("not json").is_err());
        assert!(base().with_user_state_json("[1,2,3]").is_err()); // array, not object
        assert!(base().with_user_state_json("{").is_err()); // truncated
        assert!(base().with_user_state_json("42").is_err()); // scalar
        assert!(base().with_user_state_json("{}").is_ok());
        assert!(base()
            .with_user_state_json(r#"{"a":[1,2,{"b":true}],"c":null,"d":-1.5e3,"e":"x\ny"}"#)
            .is_ok());
    }

    #[test]
    fn crafted_snapshot_with_non_json_user_state_fails_closed() {
        assert!(matches!(
            LiveStateSnapshot::deserialize(&frame(&body_one_order_with(&[], "garbage not json"))),
            Err(PersistenceError::InconsistentField { .. })
        ));
    }

    #[test]
    fn is_json_object_validator_accepts_and_rejects() {
        for ok in [
            "{}",
            "  { }  ",
            r#"{"k":"v"}"#,
            r#"{"n":[1,-2,3.5e2],"o":{"p":null}}"#,
        ] {
            assert!(is_json_object(ok), "should accept: {ok}");
        }
        for bad in [
            "",
            "null",
            "[]",
            "{",
            "}",
            r#"{"k":}"#,
            r#"{"k":1,}"#,
            "{} trailing",
            r#"{'k':1}"#,
        ] {
            assert!(!is_json_object(bad), "should reject: {bad}");
        }
    }
}
