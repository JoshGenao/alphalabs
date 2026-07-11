//! Paper strategy simulation-state persistence for **SRS-SIM-004** — "persist
//! paper strategy simulation state" (SyRS SYS-89; StRS SN-1.29 / SN-2.05).
//!
//! # What SYS-89 asks for
//!
//! The internal simulation engine must persist each paper strategy's virtual
//! position ledger, pending simulated orders, accumulated performance metrics, and
//! user-state dictionary at a configurable interval (default 60s) and on container
//! shutdown, and restore that state on restart, recoverable within 30s (excluding
//! warm-up). This module owns the **deterministic, dependency-free snapshot and
//! restore** of that state, plus the cadence/restore-deadline configuration.
//!
//! # What is real here vs deferred
//!
//! Of the four sub-states SYS-89 names, three have a runtime type today — the
//! **virtual position ledger** ([`crate::virtual_ledger::VirtualLedgerBook`],
//! SRS-SIM-003), the **accumulated performance metrics**
//! ([`PaperMetricsAccumulator`], SYS-85 / SRS-BT-004), and the **user-state
//! dictionary** (an opaque JSON object per strategy) — and are all captured,
//! persisted to disk, and restored here. The fourth, **pending simulated orders**,
//! has no runtime store (the SRS-SIM-001/002 path routes orders without retaining an
//! accepted-but-unfilled one), so it stays a reserved, fail-closed slot. This slice
//! ships:
//!
//!   * [`PaperStateSnapshot`] — a versioned envelope capturing the full ledger, the
//!     per-strategy metrics accumulators, and the per-strategy user-state
//!     dictionaries, plus the persistence config, with one reserved slot for
//!     pending orders;
//!   * [`PaperStateSnapshot::serialize`] / [`PaperStateSnapshot::deserialize`] — a
//!     hand-rolled, **deterministic** (sorted-key), zero-dependency text codec that
//!     re-validates every restored sub-state's invariants and fails closed on the
//!     first violation;
//!   * [`PaperStateSnapshot::save_to_path`] / [`PaperStateSnapshot::load_from_path`]
//!     — the **atomic on-disk store** (scratch → `fsync` → `rename` → parent-dir
//!     `fsync`), the same durability recipe as the SRS-EXE-005 live-state store;
//!   * [`PersistenceConfig`] — the SYS-89 cadence constants (default 60s interval,
//!     30s restore deadline, persist-on-shutdown), validated fail-closed, plus
//!     [`PersistenceConfig::restore_within_deadline`] and [`recover_from_path`] that
//!     enforce the **30s restore deadline** (excluding warm-up) over a monotonic
//!     clock;
//!   * [`restore`] — the convenience round-trip back to a [`VirtualLedgerBook`].
//!
//! The pieces required to flip SRS-SIM-004 to `passes:true` are deferred (see
//! `architecture/runtime_services.json#sim_persistence_contract.deferred`): the live
//! 60s timer firing inside a running container and a real container **restart**
//! restoring within 30s of container boot need the SRS-EXE-002 orchestrator wiring
//! and the SYS-89 container lifecycle (this slice proves the persist/restore
//! *mechanism* and the deadline enforcement solo, via fault injection, but cannot
//! drive a real container restart in parallel); the `pending_orders` slot needs a
//! paper-order pending store (SRS-SIM-001/002 own none yet); and while the user-state
//! *dictionary* is persisted and restored here, the Python strategy runtime that
//! *writes* it via the strategy API is a separate SRS-SDK owner. So
//! `feature_list.json` keeps SRS-SIM-004 at `passes:false`.
//!
//! # Determinism (the headline invariant)
//!
//! [`HashMap`](std::collections::HashMap) iteration order is unspecified, so the
//! serializer sorts strategies by id and positions by canonical symbol before
//! emitting. The same state therefore always serializes to **byte-identical**
//! output — a 60s checkpoint of an unchanged ledger never churns — and a restore of
//! a captured snapshot reproduces the ledger exactly
//! (`restore(serialize(capture(book))) == book`).
//!
//! # Fail-closed restore
//!
//! [`PaperStateSnapshot::deserialize`] validates the magic header, the schema
//! version, the config, and every position field invariant (the quantity/basis
//! biconditional, sign agreement, non-negative cost components, canonical symbols,
//! no duplicate records) and builds the whole book in a local before returning it,
//! so a corrupt, truncated, or tampered snapshot returns an [`Err`] and yields **no
//! partially-restored book** — never a phantom strategy or symbol.
//!
//! # Money math
//!
//! Integer minor units everywhere (`i64` quantity, `i128` money), no floating
//! point, no `serde` / external dependency — the same money-correctness and
//! zero-dependency discipline as [`crate::virtual_ledger`].

use std::collections::HashMap;
use std::fmt;
use std::fs;
use std::io::{self, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use atp_types::StrategyId;

use crate::backtest::{EquityPoint, Fill};
use crate::paper_metrics::PaperMetricsAccumulator;
use crate::virtual_ledger::{StrategyLedger, VirtualLedgerBook, VirtualPosition};

/// The current snapshot schema version. Bumped when the serialized layout changes;
/// [`PaperStateSnapshot::deserialize`] reads this version and migrates the legacy
/// [`SCHEMA_VERSION_V1`] forward, but rejects any OTHER version loudly
/// ([`PersistenceError::UnknownSchemaVersion`]) rather than silently mis-reading an
/// unknown or future layout.
pub const SCHEMA_VERSION: i64 = 2;

/// The legacy schema version (ledger-only, three reserved empty sub-state slots) that
/// the original SRS-SIM-004 slice wrote. A v1 snapshot is still READ (migrated forward
/// to the current [`SCHEMA_VERSION`] with empty metrics/user-state) so an upgrade never
/// strands persisted paper state — the SYS-89 recovery guarantee must survive a
/// version bump.
pub const SCHEMA_VERSION_V1: i64 = 1;

/// The magic header line that prefixes every serialized snapshot, so a foreign or
/// truncated blob is rejected before any field is parsed.
pub const MAGIC: &str = "ATP-PAPER-STATE";

/// SYS-89 default persistence interval (seconds): persist state every 60 seconds.
pub const DEFAULT_INTERVAL_SECS: u64 = 60;

/// SYS-89 default restore deadline (seconds): restore within 30 seconds of a
/// container restart, excluding warm-up.
pub const DEFAULT_RESTORE_DEADLINE_SECS: u64 = 30;

/// The base name of the paper-state store file inside its directory. A restore
/// reads this file; a save atomically renames the scratch file onto it.
const STORE_FILENAME: &str = "paper_sim_state.snapshot";

/// The base name of the scratch file an atomic save writes (and fsyncs) before
/// renaming it onto [`STORE_FILENAME`]. A per-process, per-call `.<pid>.<seq>`
/// suffix is appended so two writers persisting to the same directory cannot
/// rename over each other's scratch file.
const STORE_TMP_FILENAME: &str = "paper_sim_state.snapshot.tmp";

/// Process-local monotonic counter that disambiguates concurrent scratch files
/// within one process (combined with the pid for cross-process uniqueness). It
/// affects only the scratch file name, never persisted content, so a snapshot
/// stays byte-identical for the same state.
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// The persistence cadence configuration (SYS-89).
///
/// Defaults to the SYS-89 baseline (60s interval, 30s restore deadline, persist on
/// shutdown). The interval and restore deadline must be strictly positive — a
/// zero-second interval would mean "persist constantly" (a busy loop) and a
/// zero-second restore deadline is unmeetable — so [`PersistenceConfig::new`] fails
/// closed with [`PersistenceError::NonPositiveConfig`] on a zero value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PersistenceConfig {
    interval_secs: u64,
    restore_deadline_secs: u64,
    persist_on_shutdown: bool,
}

impl Default for PersistenceConfig {
    /// The SYS-89 baseline: persist every 60s, restore within 30s, persist on
    /// shutdown.
    fn default() -> Self {
        Self {
            interval_secs: DEFAULT_INTERVAL_SECS,
            restore_deadline_secs: DEFAULT_RESTORE_DEADLINE_SECS,
            persist_on_shutdown: true,
        }
    }
}

impl PersistenceConfig {
    /// A validated persistence config. Fails closed on a cadence that cannot meet
    /// SYS-89:
    ///
    /// * a zero-second interval or restore deadline
    ///   ([`PersistenceError::NonPositiveConfig`]) -- a zero interval is a busy
    ///   loop and a zero deadline is unmeetable; and
    /// * a restore deadline above [`DEFAULT_RESTORE_DEADLINE_SECS`]
    ///   ([`PersistenceError::RestoreDeadlineTooLong`]) -- SYS-89 requires state to
    ///   be "recoverable within 30 seconds", a hard ceiling, so the config CANNOT
    ///   encode a slower SLA than the requirement (an operator may only tighten it).
    ///   The persist interval has no such ceiling (SYS-89 makes 60s the *default*,
    ///   not a maximum, so a longer cadence is a permitted operator trade-off).
    ///
    /// Shutdown persistence is NOT a parameter: SYS-89 requires state to be
    /// persisted "and on container shutdown", so it is always on and the caller
    /// cannot disable it ([`persist_on_shutdown`](Self::persist_on_shutdown) is
    /// always `true`).
    pub fn new(interval_secs: u64, restore_deadline_secs: u64) -> Result<Self, PersistenceError> {
        if interval_secs == 0 {
            return Err(PersistenceError::NonPositiveConfig {
                field: "interval_secs",
            });
        }
        if restore_deadline_secs == 0 {
            return Err(PersistenceError::NonPositiveConfig {
                field: "restore_deadline_secs",
            });
        }
        if restore_deadline_secs > DEFAULT_RESTORE_DEADLINE_SECS {
            return Err(PersistenceError::RestoreDeadlineTooLong {
                secs: restore_deadline_secs,
            });
        }
        Ok(Self {
            interval_secs,
            restore_deadline_secs,
            persist_on_shutdown: true,
        })
    }

    /// The configured persistence interval in seconds (SYS-89 default 60).
    pub fn interval_secs(&self) -> u64 {
        self.interval_secs
    }

    /// The configured restore deadline in seconds (SYS-89 default 30).
    pub fn restore_deadline_secs(&self) -> u64 {
        self.restore_deadline_secs
    }

    /// Whether state is persisted on container shutdown. Always `true`: SYS-89
    /// mandates persistence on shutdown, so it cannot be disabled (a snapshot that
    /// encodes it as `false` is rejected on restore -- see
    /// [`PersistenceError::ShutdownPersistenceRequired`]).
    pub fn persist_on_shutdown(&self) -> bool {
        self.persist_on_shutdown
    }

    /// Enforce the SYS-89 restore deadline over `restore_elapsed` -- the wall-clock
    /// time the caller measured for the state-restore phase (load + deserialize +
    /// state rebuild), which **excludes** warm-up per the AC. Fails closed with
    /// [`PersistenceError::RestoreDeadlineExceeded`] if the phase overran the
    /// configured [`restore_deadline_secs`](Self::restore_deadline_secs) (default 30s,
    /// hard-capped at 30s by [`PersistenceConfig::new`]).
    pub fn restore_within_deadline(
        &self,
        restore_elapsed: Duration,
    ) -> Result<(), PersistenceError> {
        if restore_elapsed > Duration::from_secs(self.restore_deadline_secs) {
            return Err(PersistenceError::RestoreDeadlineExceeded {
                elapsed_secs: restore_elapsed.as_secs(),
                deadline_secs: self.restore_deadline_secs,
            });
        }
        Ok(())
    }
}

/// A versioned, restorable snapshot of one simulation engine's paper state.
///
/// Carries the schema version, the [`PersistenceConfig`] in force, and three of the
/// four SYS-89 sub-states: the [`VirtualLedgerBook`] (virtual position ledger), a
/// per-strategy map of accumulated [`PaperMetricsAccumulator`] metrics, and a
/// per-strategy map of user-state dictionaries (each an opaque JSON object). The
/// fourth sub-state SYS-89 names -- **pending simulated orders** -- has no runtime
/// store yet (the SRS-SIM-001/002 paper-order path routes orders without retaining
/// an accepted-but-unfilled one), so it is a reserved, always-empty slot in the
/// serialized form; this reader fails closed
/// ([`PersistenceError::UnsupportedSection`]) if a snapshot ever carries data there,
/// because there is no type to restore it into.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperStateSnapshot {
    schema_version: i64,
    config: PersistenceConfig,
    book: VirtualLedgerBook,
    metrics: HashMap<StrategyId, PaperMetricsAccumulator>,
    user_state: HashMap<StrategyId, String>,
}

impl PaperStateSnapshot {
    /// Capture the ledger and cadence: a snapshot at [`SCHEMA_VERSION`] holding a
    /// clone of `book` and `config`, with **empty** metrics and user-state maps. Pure
    /// and deterministic — no clock, no I/O. Use [`capture_full`](Self::capture_full)
    /// to include the accumulated metrics and user-state sub-states; this convenience
    /// exists for callers (and the SRS-SIM-003 tests) that persist only the ledger.
    pub fn capture(book: &VirtualLedgerBook, config: &PersistenceConfig) -> Self {
        Self::capture_full(book, &HashMap::new(), &HashMap::new(), config)
    }

    /// Capture the full paper state: the virtual position ledger, the per-strategy
    /// accumulated [`PaperMetricsAccumulator`] metrics, and the per-strategy
    /// user-state dictionaries, plus the cadence config. Pure and deterministic — no
    /// clock, no I/O. The three SYS-89 sub-states with a runtime type are all
    /// captured by clone; the fourth (pending simulated orders) has no runtime store,
    /// so it is not a parameter (see the reserved slot in the serialized form).
    pub fn capture_full(
        book: &VirtualLedgerBook,
        metrics: &HashMap<StrategyId, PaperMetricsAccumulator>,
        user_state: &HashMap<StrategyId, String>,
        config: &PersistenceConfig,
    ) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            config: config.clone(),
            book: book.clone(),
            metrics: metrics.clone(),
            user_state: user_state.clone(),
        }
    }

    /// The snapshot's schema version.
    pub fn schema_version(&self) -> i64 {
        self.schema_version
    }

    /// The persistence config captured in this snapshot.
    pub fn config(&self) -> &PersistenceConfig {
        &self.config
    }

    /// The virtual ledger book captured in this snapshot.
    pub fn book(&self) -> &VirtualLedgerBook {
        &self.book
    }

    /// The per-strategy accumulated performance metrics captured in this snapshot.
    pub fn metrics(&self) -> &HashMap<StrategyId, PaperMetricsAccumulator> {
        &self.metrics
    }

    /// The per-strategy user-state dictionaries (each an opaque JSON object)
    /// captured in this snapshot.
    pub fn user_state(&self) -> &HashMap<StrategyId, String> {
        &self.user_state
    }

    /// Consume the snapshot and return the restored [`VirtualLedgerBook`] (the
    /// ledger-only convenience; use [`into_parts`](Self::into_parts) for the metrics
    /// and user-state too).
    pub fn into_book(self) -> VirtualLedgerBook {
        self.book
    }

    /// Consume the snapshot and return its three restored sub-states: the ledger,
    /// the per-strategy metrics, and the per-strategy user-state dictionaries.
    pub fn into_parts(
        self,
    ) -> (
        VirtualLedgerBook,
        HashMap<StrategyId, PaperMetricsAccumulator>,
        HashMap<StrategyId, String>,
    ) {
        (self.book, self.metrics, self.user_state)
    }

    /// Atomically persist this snapshot to `dir`, creating it if needed.
    ///
    /// Durability recipe (identical to the SRS-EXE-005 live-state and SRS-BT-009
    /// backtest stores): serialize to a uniquely-named scratch file, `fsync` it so
    /// its bytes reach disk, `rename` it onto the live store (an atomic replace — a
    /// concurrent reader never sees a half-written blob), then `fsync` the parent
    /// directory so the rename itself survives a crash. The scratch name carries a
    /// `<pid>.<seq>` suffix so two writers persisting to the same directory cannot
    /// rename over each other's scratch file. On any write error the scratch file is
    /// removed. A single `save_to_path` is atomic; serializing concurrent writers
    /// against one directory is the caller's responsibility (the paper engine is
    /// single-writer per strategy host).
    pub fn save_to_path(&self, dir: &Path) -> Result<(), PersistenceError> {
        // Poison-pill guard: validate every user-state value is a well-formed JSON
        // object BEFORE touching the store. capture_full accepts an arbitrary string,
        // but the fail-closed recovery path rejects a non-object user-state, so
        // publishing one would atomically overwrite the last valid checkpoint with a
        // file restart recovery refuses (SYS-89 recovery must never fail on
        // self-written data). Rejecting it here leaves the previous good store intact.
        for json in self.user_state.values() {
            if !is_json_object(json) {
                return Err(PersistenceError::InconsistentField {
                    context: "user-state value is not a JSON object",
                });
            }
        }
        fs::create_dir_all(dir).map_err(|err| io_error("create paper-state directory", &err))?;
        let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
        let tmp_path = dir.join(format!("{STORE_TMP_FILENAME}.{}.{seq}", std::process::id()));
        let final_path = dir.join(STORE_FILENAME);

        let mut scratch = fs::File::create(&tmp_path)
            .map_err(|err| io_error("create paper-state scratch", &err))?;
        if let Err(err) = scratch
            .write_all(self.serialize().as_bytes())
            .and_then(|()| scratch.sync_all())
        {
            let _ = fs::remove_file(&tmp_path);
            return Err(io_error("write paper-state scratch", &err));
        }
        drop(scratch);

        fs::rename(&tmp_path, &final_path).map_err(|err| {
            let _ = fs::remove_file(&tmp_path);
            io_error("publish paper-state file", &err)
        })?;

        let dir_handle =
            fs::File::open(dir).map_err(|err| io_error("open paper-state directory", &err))?;
        dir_handle
            .sync_all()
            .map_err(|err| io_error("sync paper-state directory", &err))?;
        Ok(())
    }

    /// The path of the on-disk snapshot file inside `dir` (the file
    /// [`save_to_path`](Self::save_to_path) renames onto and
    /// [`load_from_path`](Self::load_from_path) reads). Exposed so operator tooling
    /// (the fault-injection CLI, tests) can locate the store file without hardcoding
    /// its name.
    pub fn store_path(dir: &Path) -> std::path::PathBuf {
        dir.join(STORE_FILENAME)
    }

    /// Load a snapshot previously written by [`save_to_path`](Self::save_to_path)
    /// from `dir`, for restart **recovery** — fail-closed by default.
    ///
    /// Fail-closed taxonomy (recovery assumes durable state SHOULD be present; it
    /// never silently substitutes an empty state, which would drop a strategy's
    /// virtual positions and metrics and mis-state every downstream decision):
    ///   * `dir` **absent or not a directory** → [`PersistenceError::Io`]. An
    ///     unmounted / deleted store path is a configuration failure.
    ///   * `dir` exists but holds **no snapshot file** → [`PersistenceError::Io`]. A
    ///     missing snapshot during recovery could mean a lost / mis-mounted file
    ///     after the strategy ran; restoring empty there would silently discard its
    ///     ledger and metrics. A **genuine first start** (which legitimately has no
    ///     prior state) must NOT call this — it constructs an empty snapshot via
    ///     [`capture`](Self::capture); distinguishing a fresh container from a
    ///     restart is the SRS-EXE-002 orchestrator's container-lifecycle knowledge.
    ///   * A **present** file is decoded through the fail-closed
    ///     [`deserialize`](Self::deserialize), so a corrupt / truncated /
    ///     checksum-mismatching blob returns an [`Err`], never a partial state.
    pub fn load_from_path(dir: &Path) -> Result<Self, PersistenceError> {
        if !dir.is_dir() {
            return Err(PersistenceError::Io {
                context: "paper-state directory is missing or not a directory",
            });
        }
        let final_path = dir.join(STORE_FILENAME);
        match fs::read_to_string(&final_path) {
            Ok(contents) => Self::deserialize(&contents),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Err(PersistenceError::Io {
                context: "paper-state snapshot file is missing from the store directory",
            }),
            Err(err) => Err(io_error("read paper-state file", &err)),
        }
    }

    /// Serialize to the deterministic, dependency-free text form.
    ///
    /// Layout: `MAGIC` line, an integrity `checksum` line over the body, then the
    /// body (schema version, config, the sorted ledger, reserved slots). Strategies
    /// are emitted sorted by id and positions sorted by canonical symbol, so the
    /// output is **byte-identical** for the same state regardless of `HashMap`
    /// iteration order. Variable-length strings are length-prefixed, so symbols
    /// containing spaces (e.g. the OCC option contract `AAPL  240119...`) round-trip
    /// losslessly. The checksum covers the whole body, so any later byte change is
    /// detected on restore (see [`deserialize`](Self::deserialize)).
    pub fn serialize(&self) -> String {
        // Build the body first so the checksum can cover all of it.
        let mut body = String::new();
        push_i128(&mut body, i128::from(self.schema_version));
        // Config.
        push_i128(&mut body, i128::from(self.config.interval_secs));
        push_i128(&mut body, i128::from(self.config.restore_deadline_secs));
        push_i128(
            &mut body,
            i128::from(self.config.persist_on_shutdown as i64),
        );

        // Strategies, sorted by id for determinism (StrategyId is not Ord, so sort
        // by its string form).
        let mut strategies: Vec<(&StrategyId, &StrategyLedger)> =
            self.book.ledgers_iter().collect();
        strategies.sort_by(|a, b| a.0.as_str().cmp(b.0.as_str()));
        push_i128(&mut body, strategies.len() as i128);
        for (strategy, ledger) in strategies {
            push_str(&mut body, strategy.as_str());
            // Positions, sorted by canonical symbol for determinism.
            let mut positions: Vec<(&String, &VirtualPosition)> = ledger.positions_iter().collect();
            positions.sort_by(|a, b| a.0.as_str().cmp(b.0.as_str()));
            push_i128(&mut body, positions.len() as i128);
            for (symbol, position) in positions {
                push_str(&mut body, symbol);
                push_i128(&mut body, i128::from(position.quantity()));
                push_i128(&mut body, position.cost_basis_minor());
                push_i128(&mut body, position.realized_pnl_minor());
                push_i128(&mut body, position.commission_paid_minor());
                push_i128(&mut body, position.slippage_paid_minor());
                push_i128(&mut body, position.spread_impact_paid_minor());
            }
        }

        // Pending simulated orders: still a reserved, forward-compatible slot. The
        // SRS-SIM-001/002 paper-order path retains no accepted-but-unfilled order as
        // runtime state, so there is nothing to persist yet; v2 always writes 0 and
        // deserialize fails closed on a non-zero count rather than dropping data it
        // has no type to restore.
        push_i128(&mut body, 0); // pending simulated orders (reserved)

        // Accumulated performance metrics: one PaperMetricsAccumulator per strategy,
        // sorted by strategy id for determinism (the same reason the ledger is
        // sorted -- HashMap order is unspecified).
        let mut metrics: Vec<(&StrategyId, &PaperMetricsAccumulator)> =
            self.metrics.iter().collect();
        metrics.sort_by(|a, b| a.0.as_str().cmp(b.0.as_str()));
        push_i128(&mut body, metrics.len() as i128);
        for (strategy, accumulator) in metrics {
            push_str(&mut body, strategy.as_str());
            push_metrics(&mut body, accumulator);
        }

        // User-state dictionaries: one opaque JSON-object string per strategy, sorted
        // by strategy id for determinism. Length-prefixed, so arbitrary JSON bytes
        // (nested objects, quoted strings) round-trip losslessly.
        let mut user_state: Vec<(&StrategyId, &String)> = self.user_state.iter().collect();
        user_state.sort_by(|a, b| a.0.as_str().cmp(b.0.as_str()));
        push_i128(&mut body, user_state.len() as i128);
        for (strategy, json) in user_state {
            push_str(&mut body, strategy.as_str());
            push_str(&mut body, json);
        }

        // Assemble: magic + an integrity checksum over the body + the body. The
        // magic is validated by equality; the checksum is verified before the body
        // is parsed, so a structurally-valid byte change (a tampered-but-still
        // sign-consistent quantity/basis, a flipped digit, truncation) is rejected
        // under fault injection rather than restored as fabricated state.
        let mut out = String::with_capacity(body.len() + MAGIC.len() + 32);
        push_line(&mut out, MAGIC);
        push_i128(&mut out, i128::from(checksum(body.as_bytes())));
        out.push_str(&body);
        out
    }

    /// Deserialize a snapshot produced by [`serialize`](Self::serialize), failing
    /// closed on any malformation.
    ///
    /// Validates the magic header, the body integrity checksum (BEFORE building any
    /// state), the schema version, the config, and every position field invariant,
    /// and builds the entire book in a local before returning — so a corrupt,
    /// truncated, or tampered blob returns an [`Err`] and yields no
    /// partially-restored state. The checksum is verified up front, so a
    /// structurally-valid byte change (e.g. a tampered-but-sign-consistent
    /// quantity/basis pair) is rejected with [`PersistenceError::ChecksumMismatch`]
    /// rather than restored as fabricated positions/P&L under fault injection.
    pub fn deserialize(serialized: &str) -> Result<Self, PersistenceError> {
        let mut cursor = Cursor::new(serialized);

        let magic = cursor.read_line("magic header")?;
        if magic != MAGIC {
            return Err(PersistenceError::CorruptSnapshot {
                context: "magic header",
            });
        }
        // Integrity check FIRST: the checksum covers the entire body that follows,
        // so verify it before parsing or building any state.
        let stored_checksum = cursor.read_u64("checksum")?;
        let body = cursor.remaining();
        if checksum(body) != stored_checksum {
            return Err(PersistenceError::ChecksumMismatch);
        }

        let schema_version = cursor.read_i64("schema version")?;
        if schema_version != SCHEMA_VERSION && schema_version != SCHEMA_VERSION_V1 {
            return Err(PersistenceError::UnknownSchemaVersion {
                found: schema_version,
            });
        }

        let interval_secs = cursor.read_u64("interval_secs")?;
        let restore_deadline_secs = cursor.read_u64("restore_deadline_secs")?;
        // SYS-89 mandates persistence on shutdown, so a snapshot that encodes it as
        // disabled is out of contract and rejected (the value is always written as
        // true; this guards a foreign/older writer that set it false).
        let persist_on_shutdown = cursor.read_bool("persist_on_shutdown")?;
        if !persist_on_shutdown {
            return Err(PersistenceError::ShutdownPersistenceRequired);
        }
        let config = PersistenceConfig::new(interval_secs, restore_deadline_secs)?;

        let strategy_count = cursor.read_count("strategy count")?;
        let mut ledgers: HashMap<StrategyId, StrategyLedger> = HashMap::new();
        for _ in 0..strategy_count {
            let strategy_id = cursor.read_str("strategy id")?;
            if strategy_id.trim().is_empty() {
                return Err(PersistenceError::InconsistentField {
                    context: "empty strategy id",
                });
            }
            let symbol_count = cursor.read_count("symbol count")?;
            let mut positions: HashMap<String, VirtualPosition> = HashMap::new();
            for _ in 0..symbol_count {
                let symbol = cursor.read_str("symbol")?;
                let position = read_position(&mut cursor, &symbol)?;
                if positions.insert(symbol, position).is_some() {
                    return Err(PersistenceError::DuplicateRecord {
                        context: "duplicate symbol within a strategy",
                    });
                }
            }
            if ledgers
                .insert(
                    StrategyId::new(strategy_id),
                    StrategyLedger::from_positions(positions),
                )
                .is_some()
            {
                return Err(PersistenceError::DuplicateRecord {
                    context: "duplicate strategy id",
                });
            }
        }

        // Pending simulated orders: still a reserved slot (no runtime store yet), so a
        // non-zero count is unsupported rather than silently ignored. It was one of
        // THREE reserved zero-slots in v1.
        if cursor.read_count("pending simulated orders")? != 0 {
            return Err(PersistenceError::UnsupportedSection {
                context: "pending simulated orders",
            });
        }

        let (metrics, user_state) = if schema_version == SCHEMA_VERSION_V1 {
            // v1 migration (origin/main, ledger-only): the metric and user-state
            // sub-states were reserved, always-empty slots. Restore the ledger/config
            // and initialize the new sub-states EMPTY, so upgrading past a v1 snapshot
            // recovers the ledger instead of stranding it. A non-zero legacy slot is
            // still rejected (it never held data a v1 writer could produce).
            for context in ["accumulated paper metrics", "user-state dictionary"] {
                if cursor.read_count(context)? != 0 {
                    return Err(PersistenceError::UnsupportedSection { context });
                }
            }
            (HashMap::new(), HashMap::new())
        } else {
            // v2: the metrics and user-state sections are real. Each accumulator is
            // re-validated for internal coherence, and each user-state value must be a
            // well-formed JSON object, before it is built.
            let metrics = read_metrics(&mut cursor)?;
            let user_state = read_user_state(&mut cursor)?;
            (metrics, user_state)
        };

        cursor.expect_end()?;

        Ok(Self {
            // A v1 snapshot is migrated forward: the in-memory value is a current-version
            // snapshot (empty new sub-states) that re-serializes as v2.
            schema_version: SCHEMA_VERSION,
            config,
            book: VirtualLedgerBook::from_ledgers(ledgers),
            metrics,
            user_state,
        })
    }
}

/// Restore a [`VirtualLedgerBook`] from a serialized snapshot — the convenience
/// round-trip for the common case where only the ledger is needed.
/// `restore(snapshot.serialize())` reproduces `snapshot.book()` exactly.
pub fn restore(serialized: &str) -> Result<VirtualLedgerBook, PersistenceError> {
    PaperStateSnapshot::deserialize(serialized).map(PaperStateSnapshot::into_book)
}

/// The outcome of a successful restart recovery ([`recover_from_path`]): the restored
/// snapshot and the measured state-restore duration the SYS-89 deadline was enforced
/// over (excluding warm-up).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryOutcome {
    snapshot: PaperStateSnapshot,
    restore_elapsed: Duration,
}

impl RecoveryOutcome {
    /// The restored snapshot (its ledger, metrics, user-state, and config).
    pub fn snapshot(&self) -> &PaperStateSnapshot {
        &self.snapshot
    }

    /// Consume the outcome and take the restored snapshot.
    pub fn into_snapshot(self) -> PaperStateSnapshot {
        self.snapshot
    }

    /// The measured state-restore duration (excluding warm-up), which was enforced
    /// against the SYS-89 restore deadline.
    pub fn restore_elapsed(&self) -> Duration {
        self.restore_elapsed
    }

    /// The number of strategies whose virtual ledger was restored.
    pub fn strategy_count(&self) -> usize {
        self.snapshot.book().strategy_count()
    }
}

/// Load the paper-state snapshot from `dir` and recover it (SRS-SIM-004), measuring
/// the state-restore phase with a monotonic clock and enforcing the SYS-89 restore
/// deadline carried in the persisted config (excluding warm-up).
///
/// Fail-closed: a missing/corrupt store ([`PaperStateSnapshot::load_from_path`]) or a
/// restore that overran the deadline
/// ([`PersistenceConfig::restore_within_deadline`]) returns an [`Err`] and the
/// strategy does not resume. This is the deterministic recovery mechanism the
/// container lifecycle drives on restart; the live 60s persistence timer and the real
/// container-restart wall-clock are the SRS-EXE-002 orchestrator's to wire (see the
/// module docs), so `feature_list.json` keeps SRS-SIM-004 `passes:false`.
pub fn recover_from_path(dir: &Path) -> Result<RecoveryOutcome, PersistenceError> {
    let start = Instant::now();
    let snapshot = PaperStateSnapshot::load_from_path(dir)?;
    let restore_elapsed = start.elapsed();
    snapshot.config().restore_within_deadline(restore_elapsed)?;
    Ok(RecoveryOutcome {
        snapshot,
        restore_elapsed,
    })
}

/// Read and validate a single persisted [`VirtualPosition`] for `symbol`, failing
/// closed on any field invariant violation BEFORE the position is built.
fn read_position(
    cursor: &mut Cursor<'_>,
    symbol: &str,
) -> Result<VirtualPosition, PersistenceError> {
    // The symbol must already be in canonical form (trim + upper-case), so a
    // restored key is byte-identical to the captured key and aliases cannot creep
    // in. canonical_symbol is idempotent, so a real capture always satisfies this;
    // a hand-tampered non-canonical symbol fails closed.
    if symbol.is_empty() {
        return Err(PersistenceError::InconsistentField {
            context: "empty symbol",
        });
    }
    if symbol != symbol.trim() || symbol != symbol.to_uppercase() {
        return Err(PersistenceError::InconsistentField {
            context: "non-canonical symbol",
        });
    }

    let quantity = cursor.read_i64("quantity")?;
    let cost_basis_minor = cursor.read_i128("cost_basis_minor")?;
    let realized_pnl_minor = cursor.read_i128("realized_pnl_minor")?;
    let commission_paid_minor = cursor.read_i128("commission_paid_minor")?;
    let slippage_paid_minor = cursor.read_i128("slippage_paid_minor")?;
    let spread_impact_paid_minor = cursor.read_i128("spread_impact_paid_minor")?;

    // Cost components are non-negative accumulators (the ledger rejects negative
    // costs at apply_fill); a negative one is corrupt and could fabricate cash on
    // reconciliation.
    for (component, value) in [
        ("commission_paid_minor", commission_paid_minor),
        ("slippage_paid_minor", slippage_paid_minor),
        ("spread_impact_paid_minor", spread_impact_paid_minor),
    ] {
        if value < 0 {
            let _ = component;
            return Err(PersistenceError::InconsistentField {
                context: "negative cost component",
            });
        }
    }

    // The quantity/basis biconditional: a flat position has exactly zero basis and
    // a non-flat position has a basis whose sign matches the quantity (a long's
    // basis is cash laid out > 0, a short's is proceeds received < 0). The ledger
    // conserves this exactly (a full close lands at cost_basis == 0), so a snapshot
    // that violates it is corrupt.
    if (quantity == 0) != (cost_basis_minor == 0) {
        return Err(PersistenceError::InconsistentField {
            context: "quantity/basis flat-state mismatch",
        });
    }
    if quantity != 0 && quantity.signum() as i128 != cost_basis_minor.signum() {
        return Err(PersistenceError::InconsistentField {
            context: "quantity/basis sign mismatch",
        });
    }

    Ok(VirtualPosition::from_components(
        quantity,
        cost_basis_minor,
        realized_pnl_minor,
        commission_paid_minor,
        slippage_paid_minor,
        spread_impact_paid_minor,
    ))
}

/// Read an `Option<u64>` written by [`push_opt_u64`]: `0` -> `None`, `1` -> `Some`.
/// Any other flag is corrupt.
fn read_opt_u64(
    cursor: &mut Cursor<'_>,
    context: &'static str,
) -> Result<Option<u64>, PersistenceError> {
    match cursor.read_count(context)? {
        0 => Ok(None),
        1 => Ok(Some(cursor.read_u64(context)?)),
        _ => Err(PersistenceError::CorruptSnapshot { context }),
    }
}

/// Read the per-strategy accumulated-metrics section, failing closed on a duplicate
/// or incoherent accumulator, building the whole map in a local.
fn read_metrics(
    cursor: &mut Cursor<'_>,
) -> Result<HashMap<StrategyId, PaperMetricsAccumulator>, PersistenceError> {
    let count = cursor.read_count("metrics strategy count")?;
    let mut metrics: HashMap<StrategyId, PaperMetricsAccumulator> = HashMap::new();
    for _ in 0..count {
        let strategy_id = cursor.read_str("metrics strategy id")?;
        if strategy_id.trim().is_empty() {
            return Err(PersistenceError::InconsistentField {
                context: "empty metrics strategy id",
            });
        }
        let accumulator = read_accumulator(cursor)?;
        if metrics
            .insert(StrategyId::new(strategy_id), accumulator)
            .is_some()
        {
            return Err(PersistenceError::DuplicateRecord {
                context: "duplicate metrics strategy id",
            });
        }
    }
    Ok(metrics)
}

/// Read one [`PaperMetricsAccumulator`]. The parsed components are handed to
/// [`PaperMetricsAccumulator::from_components`], which RE-VALIDATES the accumulator's
/// own construction invariants (positive baseline, monotonic trade log, strictly
/// increasing equity curve, coherent last-fill/last-mark cursors, per-fill field
/// invariants) before building it, so an internally-incoherent snapshot fails closed
/// with no partial accumulator.
fn read_accumulator(cursor: &mut Cursor<'_>) -> Result<PaperMetricsAccumulator, PersistenceError> {
    let starting_cash_i128 = cursor.read_i128("metrics starting_cash_minor")?;
    let starting_cash_minor =
        i64::try_from(starting_cash_i128).map_err(|_| PersistenceError::InconsistentField {
            context: "metrics starting_cash_minor out of i64 range",
        })?;
    let cash_minor = cursor.read_i128("metrics cash_minor")?;
    let ledger = read_ledger(cursor)?;

    // A persisted count is UNTRUSTED: a checksum-valid foreign/tampered snapshot can
    // encode a huge count. Do NOT pre-allocate with it (Vec::with_capacity(huge) would
    // capacity-overflow or OOM-abort, defeating the fail-closed guarantee). Grow the Vec
    // as records are actually read, so a count larger than the blob's real contents fails
    // closed on the first short read (CorruptSnapshot) rather than aborting the process.
    let trade_count = cursor.read_count("metrics trade-log count")?;
    let mut trade_log = Vec::new();
    for _ in 0..trade_count {
        trade_log.push(read_fill(cursor)?);
    }

    let equity_count = cursor.read_count("metrics equity-curve count")?;
    let mut equity_curve = Vec::new();
    for _ in 0..equity_count {
        let ts = cursor.read_u64("metrics equity ts")?;
        let equity_minor = cursor.read_i64("metrics equity_minor")?;
        equity_curve.push(EquityPoint { ts, equity_minor });
    }

    let last_mark_ts = read_opt_u64(cursor, "metrics last_mark_ts")?;
    let last_fill_ts = read_opt_u64(cursor, "metrics last_fill_ts")?;

    PaperMetricsAccumulator::from_components(
        starting_cash_minor,
        cash_minor,
        ledger,
        trade_log,
        equity_curve,
        last_mark_ts,
        last_fill_ts,
    )
    .map_err(|_| PersistenceError::InconsistentField {
        context: "incoherent persisted paper metrics accumulator",
    })
}

/// Read a [`StrategyLedger`] (a metrics accumulator's own SYS-84 ledger), reusing
/// the fail-closed per-position validation of [`read_position`].
fn read_ledger(cursor: &mut Cursor<'_>) -> Result<StrategyLedger, PersistenceError> {
    let symbol_count = cursor.read_count("metrics ledger symbol count")?;
    let mut positions: HashMap<String, VirtualPosition> = HashMap::new();
    for _ in 0..symbol_count {
        let symbol = cursor.read_str("metrics ledger symbol")?;
        let position = read_position(cursor, &symbol)?;
        if positions.insert(symbol, position).is_some() {
            return Err(PersistenceError::DuplicateRecord {
                context: "duplicate symbol within a metrics ledger",
            });
        }
    }
    Ok(StrategyLedger::from_positions(positions))
}

/// Read one trade-log [`Fill`]. Field invariants (positive price, non-negative
/// costs, non-empty symbol, monotonic timestamps) are re-validated in
/// [`PaperMetricsAccumulator::from_components`] once the whole log is read.
fn read_fill(cursor: &mut Cursor<'_>) -> Result<Fill, PersistenceError> {
    let ts = cursor.read_u64("metrics fill ts")?;
    let symbol = cursor.read_str("metrics fill symbol")?;
    let quantity = cursor.read_i64("metrics fill quantity")?;
    let price_minor = cursor.read_i64("metrics fill price_minor")?;
    let commission_minor = cursor.read_i64("metrics fill commission_minor")?;
    let slippage_minor = cursor.read_i64("metrics fill slippage_minor")?;
    let spread_impact_minor = cursor.read_i64("metrics fill spread_impact_minor")?;
    Ok(Fill {
        ts,
        symbol,
        quantity,
        price_minor,
        commission_minor,
        slippage_minor,
        spread_impact_minor,
    })
}

/// Read the per-strategy user-state section, failing closed on a duplicate strategy
/// or a value that is not a well-formed JSON object, building the whole map in a
/// local.
fn read_user_state(
    cursor: &mut Cursor<'_>,
) -> Result<HashMap<StrategyId, String>, PersistenceError> {
    let count = cursor.read_count("user-state strategy count")?;
    let mut user_state: HashMap<StrategyId, String> = HashMap::new();
    for _ in 0..count {
        let strategy_id = cursor.read_str("user-state strategy id")?;
        if strategy_id.trim().is_empty() {
            return Err(PersistenceError::InconsistentField {
                context: "empty user-state strategy id",
            });
        }
        let json = cursor.read_str("user-state json")?;
        // SYS-89 names a user-state DICTIONARY: the persisted value must be a JSON
        // object, not an array or scalar, so a foreign/tampered value that is not a
        // dictionary fails closed rather than restoring a malformed state.
        if !is_json_object(&json) {
            return Err(PersistenceError::InconsistentField {
                context: "user-state value is not a JSON object",
            });
        }
        if user_state
            .insert(StrategyId::new(strategy_id), json)
            .is_some()
        {
            return Err(PersistenceError::DuplicateRecord {
                context: "duplicate user-state strategy id",
            });
        }
    }
    Ok(user_state)
}

// --------------------------------------------------------------------------- //
// Deterministic, dependency-free text codec
// --------------------------------------------------------------------------- //

/// Append `value` as its own line.
fn push_line(out: &mut String, value: &str) {
    out.push_str(value);
    out.push('\n');
}

/// Append a decimal integer as its own line.
fn push_i128(out: &mut String, value: i128) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append a length-prefixed string: the byte length on one line, then the bytes
/// followed by a newline. Length-prefixing means any byte (spaces, etc.) in the
/// value round-trips without escaping or delimiter collisions.
fn push_str(out: &mut String, value: &str) {
    out.push_str(&value.len().to_string());
    out.push('\n');
    out.push_str(value);
    out.push('\n');
}

/// Append an `Option<u64>`: `0` for `None`, or `1` then the value for `Some`. Read
/// back by [`read_opt_u64`].
fn push_opt_u64(out: &mut String, value: Option<u64>) {
    match value {
        Some(inner) => {
            push_i128(out, 1);
            push_i128(out, i128::from(inner));
        }
        None => push_i128(out, 0),
    }
}

/// Append a [`VirtualPosition`]'s six persisted fields (the same layout the book
/// path emits inline), reused for a metrics accumulator's own ledger.
fn push_position(out: &mut String, position: &VirtualPosition) {
    push_i128(out, i128::from(position.quantity()));
    push_i128(out, position.cost_basis_minor());
    push_i128(out, position.realized_pnl_minor());
    push_i128(out, position.commission_paid_minor());
    push_i128(out, position.slippage_paid_minor());
    push_i128(out, position.spread_impact_paid_minor());
}

/// Append a [`StrategyLedger`]'s positions, sorted by canonical symbol for
/// determinism (a metrics accumulator carries its own ledger).
fn push_ledger(out: &mut String, ledger: &StrategyLedger) {
    let mut positions: Vec<(&String, &VirtualPosition)> = ledger.positions_iter().collect();
    positions.sort_by(|a, b| a.0.as_str().cmp(b.0.as_str()));
    push_i128(out, positions.len() as i128);
    for (symbol, position) in positions {
        push_str(out, symbol);
        push_position(out, position);
    }
}

/// Append a [`Fill`]'s seven fields (an integer-minor-unit trade-log entry).
fn push_fill(out: &mut String, fill: &Fill) {
    push_i128(out, i128::from(fill.ts));
    push_str(out, &fill.symbol);
    push_i128(out, i128::from(fill.quantity));
    push_i128(out, i128::from(fill.price_minor));
    push_i128(out, i128::from(fill.commission_minor));
    push_i128(out, i128::from(fill.slippage_minor));
    push_i128(out, i128::from(fill.spread_impact_minor));
}

/// Append a [`PaperMetricsAccumulator`]: its baseline/running cash, its own SYS-84
/// ledger, the trade log (authoritative execution order), the mark-to-market equity
/// curve (timestamp order), and the two cross-stream cursors. The trade log and
/// equity curve are sequences (`Vec`), so their order is already deterministic and
/// is preserved as-is.
fn push_metrics(out: &mut String, accumulator: &PaperMetricsAccumulator) {
    push_i128(out, i128::from(accumulator.starting_cash_minor()));
    push_i128(out, accumulator.cash_minor());
    push_ledger(out, accumulator.ledger());
    let trade_log = accumulator.trade_log();
    push_i128(out, trade_log.len() as i128);
    for fill in trade_log {
        push_fill(out, fill);
    }
    let equity_curve = accumulator.equity_curve();
    push_i128(out, equity_curve.len() as i128);
    for point in equity_curve {
        push_i128(out, i128::from(point.ts));
        push_i128(out, i128::from(point.equity_minor));
    }
    push_opt_u64(out, accumulator.last_mark_ts());
    push_opt_u64(out, accumulator.last_fill_ts());
}

/// A 64-bit FNV-1a integrity checksum over the serialized body.
///
/// This is a NON-cryptographic checksum: it detects *accidental* corruption
/// (bit flips, truncation, a value changed to another structurally-valid value)
/// under the SYS-89 fault-injection criterion, so a damaged snapshot fails closed
/// instead of restoring fabricated state. It is NOT a security MAC -- defending
/// against a deliberate tamperer who can recompute the checksum needs a keyed MAC
/// and key management, which is out of scope for the single-user, local-only
/// release baseline (no multi-user auth per the system constraints). Deterministic,
/// dependency-free, and integer-only (no floating point, no external crate).
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

/// Structurally validate that `s` is a well-formed JSON **object** (the SYS-89
/// user-state DICTIONARY must be a top-level object, not an array or scalar).
/// Structure-only, dependency-free (no serde), no value capture — it exists so a
/// persisted user-state value that is not a dictionary fails closed on restore.
fn is_json_object(s: &str) -> bool {
    let mut validator = JsonValidator {
        bytes: s.as_bytes(),
        pos: 0,
    };
    validator.skip_ws();
    if validator.peek() != Some(b'{') || !validator.object(0) {
        return false;
    }
    validator.skip_ws();
    validator.pos == validator.bytes.len()
}

/// Maximum JSON nesting depth the user-state validator accepts. A persisted
/// user-state dictionary is strategy-provided (and, on restore, foreign) input, so the
/// recursive-descent validator is bounded: a JSON nested deeper than this is rejected
/// (`is_json_object` returns `false` -> the caller fails closed with
/// [`PersistenceError::InconsistentField`]) rather than being allowed to overflow the
/// call stack. 128 is far beyond any real key-value state dictionary.
const MAX_JSON_DEPTH: usize = 128;

/// A recursive-descent JSON grammar validator (structure only, no value capture),
/// ported from the SRS-EXE-005 live-state codec so paper user-state validation is
/// identical to live user-state validation.
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

    fn value(&mut self, depth: usize) -> bool {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => self.string(),
            Some(b't') => self.literal(b"true"),
            Some(b'f') => self.literal(b"false"),
            Some(b'n') => self.literal(b"null"),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => false,
        }
    }

    fn object(&mut self, depth: usize) -> bool {
        // Bound the recursion so an adversarially deep (but syntactically valid) dict
        // fails closed instead of overflowing the stack.
        if depth >= MAX_JSON_DEPTH {
            return false;
        }
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
            if !self.eat(b':') || !self.value(depth + 1) {
                return false;
            }
            self.skip_ws();
            if self.eat(b',') {
                continue;
            }
            return self.eat(b'}');
        }
    }

    fn array(&mut self, depth: usize) -> bool {
        if depth >= MAX_JSON_DEPTH {
            return false;
        }
        if !self.eat(b'[') {
            return false;
        }
        self.skip_ws();
        if self.eat(b']') {
            return true; // empty array
        }
        loop {
            if !self.value(depth + 1) {
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

/// Map an I/O error to the fail-closed [`PersistenceError::Io`]. The underlying
/// `io::Error` is not carried in the variant (it is not `PartialEq`, which the enum
/// needs, and it is not persisted); `context` names the failing operation.
fn io_error(context: &'static str, _err: &io::Error) -> PersistenceError {
    PersistenceError::Io { context }
}

/// A forward-only cursor over a serialized snapshot's bytes. Reads are exact and
/// fail closed: a missing newline, a malformed integer, a truncated length-prefixed
/// string, or trailing garbage all surface as [`PersistenceError::CorruptSnapshot`].
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

    /// The not-yet-consumed bytes (used to checksum the body after the header).
    fn remaining(&self) -> &'a [u8] {
        &self.bytes[self.pos..]
    }

    /// Read up to (and consuming) the next `\n`, returning the line without it.
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
        match self.read_line(context)? {
            "0" => Ok(false),
            "1" => Ok(true),
            _ => Err(PersistenceError::CorruptSnapshot { context }),
        }
    }

    /// Read a non-negative count line (a `usize`).
    fn read_count(&mut self, context: &'static str) -> Result<usize, PersistenceError> {
        self.read_line(context)?
            .parse::<usize>()
            .map_err(|_| PersistenceError::CorruptSnapshot { context })
    }

    /// Read a length-prefixed string: a byte-length line, then exactly that many
    /// bytes, then a terminating `\n`.
    fn read_str(&mut self, context: &'static str) -> Result<String, PersistenceError> {
        let len = self.read_count(context)?;
        let end = self
            .pos
            .checked_add(len)
            .ok_or(PersistenceError::CorruptSnapshot { context })?;
        if end >= self.bytes.len() || self.bytes[end] != b'\n' {
            return Err(PersistenceError::CorruptSnapshot { context });
        }
        let value = std::str::from_utf8(&self.bytes[self.pos..end])
            .map_err(|_| PersistenceError::CorruptSnapshot { context })?
            .to_string();
        self.pos = end + 1; // consume the trailing '\n'
        Ok(value)
    }

    /// Confirm the cursor is exhausted; trailing bytes mean the blob is corrupt or
    /// carries unexpected extra data.
    fn expect_end(&self) -> Result<(), PersistenceError> {
        if self.pos == self.bytes.len() {
            Ok(())
        } else {
            Err(PersistenceError::CorruptSnapshot {
                context: "trailing data",
            })
        }
    }
}

/// Fail-closed errors from paper-state persistence. Carries no broker/vendor
/// identifiers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PersistenceError {
    /// The serialized blob was malformed: a bad magic header, a missing newline, a
    /// non-integer where an integer was expected, a truncated length-prefixed
    /// string, or trailing data. `context` names where parsing failed.
    CorruptSnapshot { context: &'static str },
    /// The snapshot's schema version is not one this reader understands. Rejected
    /// loudly rather than mis-read.
    UnknownSchemaVersion { found: i64 },
    /// A field violated a ledger invariant (the quantity/basis biconditional, sign
    /// agreement, a non-negative cost component, a canonical/non-empty symbol, or a
    /// non-empty strategy id). `context` names the violation.
    InconsistentField { context: &'static str },
    /// The snapshot repeated a strategy id, or a symbol within one strategy.
    DuplicateRecord { context: &'static str },
    /// The snapshot carried data in the reserved pending-simulated-orders slot,
    /// which this version has no runtime type to restore.
    UnsupportedSection { context: &'static str },
    /// The persistence config carried a zero-second interval or restore deadline.
    NonPositiveConfig { field: &'static str },
    /// The configured restore deadline exceeds the SYS-89 ceiling of
    /// [`DEFAULT_RESTORE_DEADLINE_SECS`] seconds, so it would encode a slower SLA
    /// than the requirement allows.
    RestoreDeadlineTooLong { secs: u64 },
    /// The snapshot's integrity checksum did not match the body, so the bytes were
    /// corrupted or tampered after serialization. Rejected before any state is
    /// built (the SYS-89 fault-injection fail-closed guarantee).
    ChecksumMismatch,
    /// The snapshot encoded shutdown persistence as disabled. SYS-89 mandates
    /// persistence on container shutdown, so it cannot be turned off; such a
    /// snapshot is out of contract and rejected.
    ShutdownPersistenceRequired,
    /// A filesystem operation on the on-disk store failed (create/write/fsync/rename
    /// on save, or a missing directory / missing snapshot file on load). `context`
    /// names the failing operation. Recovery fails closed rather than substituting
    /// an empty state.
    Io { context: &'static str },
    /// The state-restore phase overran the SYS-89 restore deadline (excluding
    /// warm-up). The strategy must not silently resume from a too-slow restore.
    RestoreDeadlineExceeded {
        elapsed_secs: u64,
        deadline_secs: u64,
    },
}

impl fmt::Display for PersistenceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CorruptSnapshot { context } => {
                write!(f, "corrupt paper-state snapshot at {context}")
            }
            Self::UnknownSchemaVersion { found } => write!(
                f,
                "unknown paper-state schema version {found} (this build understands {SCHEMA_VERSION})"
            ),
            Self::InconsistentField { context } => {
                write!(f, "inconsistent persisted paper-state field: {context}")
            }
            Self::DuplicateRecord { context } => {
                write!(f, "duplicate record in paper-state snapshot: {context}")
            }
            Self::UnsupportedSection { context } => write!(
                f,
                "paper-state snapshot carries unsupported data in reserved slot: {context}"
            ),
            Self::NonPositiveConfig { field } => {
                write!(f, "persistence config field {field} must be a positive number of seconds")
            }
            Self::RestoreDeadlineTooLong { secs } => write!(
                f,
                "restore deadline {secs}s exceeds the SYS-89 ceiling of {DEFAULT_RESTORE_DEADLINE_SECS}s"
            ),
            Self::ChecksumMismatch => write!(
                f,
                "paper-state snapshot integrity checksum mismatch (corrupted or tampered after serialization)"
            ),
            Self::ShutdownPersistenceRequired => write!(
                f,
                "paper-state snapshot disables shutdown persistence, which SYS-89 mandates"
            ),
            Self::Io { context } => write!(f, "paper-state store I/O error: {context}"),
            Self::RestoreDeadlineExceeded {
                elapsed_secs,
                deadline_secs,
            } => write!(
                f,
                "paper-state restore took {elapsed_secs}s, exceeding the {deadline_secs}s SYS-89 \
                 recovery deadline (excluding warm-up)"
            ),
        }
    }
}

impl std::error::Error for PersistenceError {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sim::PaperSimulationEngine;

    fn engine() -> PaperSimulationEngine {
        PaperSimulationEngine::new()
    }

    /// Build a book with a couple of strategies and positions, including a flat
    /// (fully-closed) position that still carries realized P&L and commission.
    fn sample_book() -> VirtualLedgerBook {
        let engine = engine();
        let mut book = VirtualLedgerBook::new();
        let a = StrategyId::new("reservoir-a");
        let b = StrategyId::new("reservoir-b");

        // Strategy A: an open long in AAPL and a fully-closed round trip in MSFT.
        book.apply_fill(
            &a,
            &engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap(),
        )
        .unwrap();
        book.apply_fill(
            &a,
            &engine.simulate_fill(2, "MSFT", 50, 20_000, None).unwrap(),
        )
        .unwrap();
        book.apply_fill(
            &a,
            &engine.simulate_fill(3, "MSFT", -50, 21_000, None).unwrap(),
        )
        .unwrap();

        // Strategy B: an open short in AAPL (independent of A's AAPL long).
        book.apply_fill(
            &b,
            &engine.simulate_fill(1, "AAPL", -30, 10_500, None).unwrap(),
        )
        .unwrap();
        book
    }

    /// Wrap a serialized *body* (everything after the checksum line) in a valid
    /// frame: the magic header, a CORRECT integrity checksum over the body, then the
    /// body. Lets a hand-crafted corrupt-FIELD body reach the field validation
    /// (because the checksum matches), as opposed to a corrupt-BYTES test which
    /// mutates a real frame and is meant to trip the checksum.
    fn framed(body: &str) -> String {
        format!("{MAGIC}\n{}\n{body}", checksum(body.as_bytes()))
    }

    #[test]
    fn config_default_is_the_sys89_baseline() {
        let config = PersistenceConfig::default();
        assert_eq!(config.interval_secs(), 60);
        assert_eq!(config.restore_deadline_secs(), 30);
        assert!(config.persist_on_shutdown());
    }

    #[test]
    fn config_new_validates_positive_seconds() {
        let ok = PersistenceConfig::new(60, 30).expect("valid");
        assert!(ok.persist_on_shutdown()); // mandatory, always on
        assert_eq!(
            PersistenceConfig::new(0, 30),
            Err(PersistenceError::NonPositiveConfig {
                field: "interval_secs"
            })
        );
        assert_eq!(
            PersistenceConfig::new(60, 0),
            Err(PersistenceError::NonPositiveConfig {
                field: "restore_deadline_secs"
            })
        );
    }

    #[test]
    fn config_new_rejects_a_restore_deadline_over_the_sys89_ceiling() {
        // SYS-89 requires recovery within 30s; the config cannot encode a slower
        // SLA (30 is the boundary and is allowed; 31+ is rejected).
        assert!(PersistenceConfig::new(60, DEFAULT_RESTORE_DEADLINE_SECS).is_ok());
        assert_eq!(
            PersistenceConfig::new(60, 31),
            Err(PersistenceError::RestoreDeadlineTooLong { secs: 31 })
        );
        assert_eq!(
            PersistenceConfig::new(60, 600),
            Err(PersistenceError::RestoreDeadlineTooLong { secs: 600 })
        );
        // A tighter deadline (operator may shorten it) is fine.
        assert!(PersistenceConfig::new(60, 5).is_ok());
        // The persist interval has no upper ceiling (SYS-89 makes 60s a default).
        assert!(PersistenceConfig::new(600, 30).is_ok());
    }

    #[test]
    fn capture_stamps_the_schema_version_and_clones_state() {
        let book = sample_book();
        let config = PersistenceConfig::default();
        let snapshot = PaperStateSnapshot::capture(&book, &config);
        assert_eq!(snapshot.schema_version(), SCHEMA_VERSION);
        assert_eq!(snapshot.config(), &config);
        assert_eq!(snapshot.book(), &book);
    }

    #[test]
    fn round_trip_reproduces_the_book_exactly() {
        let book = sample_book();
        let config = PersistenceConfig::default();
        let snapshot = PaperStateSnapshot::capture(&book, &config);
        let restored = PaperStateSnapshot::deserialize(&snapshot.serialize()).unwrap();
        assert_eq!(restored, snapshot);
        assert_eq!(restored.book(), &book);
        assert_eq!(restore(&snapshot.serialize()).unwrap(), book);
    }

    #[test]
    fn round_trip_preserves_a_flat_position_with_realized_history() {
        // Strategy A's MSFT position is flat (quantity 0, basis 0) but carries the
        // realized P&L and commission from the closed round trip; persistence must
        // not drop it.
        let book = sample_book();
        let restored =
            restore(&PaperStateSnapshot::capture(&book, &PersistenceConfig::default()).serialize())
                .unwrap();
        let a = StrategyId::new("reservoir-a");
        let msft = restored
            .position(&a, "MSFT")
            .expect("flat MSFT position survives");
        assert_eq!(msft.quantity(), 0);
        assert_eq!(msft.cost_basis_minor(), 0);
        assert_eq!(msft.realized_pnl_minor(), 50_000); // (21_000 - 20_000) * 50
        assert!(msft.commission_paid_minor() > 0);
    }

    #[test]
    fn serialize_is_deterministic_and_byte_identical() {
        let book = sample_book();
        let config = PersistenceConfig::default();
        // Re-capturing and re-serializing the same state yields byte-identical
        // output (sorted keys), so an unchanged 60s checkpoint never churns.
        let first = PaperStateSnapshot::capture(&book, &config).serialize();
        let second = PaperStateSnapshot::capture(&book, &config).serialize();
        assert_eq!(first, second);
    }

    #[test]
    fn serialize_order_is_independent_of_insertion_order() {
        // Two books with the same positions inserted in different orders serialize
        // identically — determinism comes from sorting, not HashMap order.
        let engine = engine();
        let s = StrategyId::new("s");
        let mut first = VirtualLedgerBook::new();
        first
            .apply_fill(&s, &engine.simulate_fill(1, "AAA", 1, 100, None).unwrap())
            .unwrap();
        first
            .apply_fill(&s, &engine.simulate_fill(2, "ZZZ", 1, 100, None).unwrap())
            .unwrap();
        let mut second = VirtualLedgerBook::new();
        second
            .apply_fill(&s, &engine.simulate_fill(2, "ZZZ", 1, 100, None).unwrap())
            .unwrap();
        second
            .apply_fill(&s, &engine.simulate_fill(1, "AAA", 1, 100, None).unwrap())
            .unwrap();
        let cfg = PersistenceConfig::default();
        assert_eq!(
            PaperStateSnapshot::capture(&first, &cfg).serialize(),
            PaperStateSnapshot::capture(&second, &cfg).serialize()
        );
    }

    #[test]
    fn round_trip_preserves_a_custom_config() {
        let book = sample_book();
        let config = PersistenceConfig::new(15, 10).unwrap();
        let restored = PaperStateSnapshot::deserialize(
            &PaperStateSnapshot::capture(&book, &config).serialize(),
        )
        .unwrap();
        assert_eq!(restored.config(), &config);
        // Shutdown persistence is always on (SYS-89 mandate), even via new().
        assert!(restored.config().persist_on_shutdown());
    }

    #[test]
    fn deserialize_rejects_disabled_shutdown_persistence() {
        // SYS-89 mandates shutdown persistence; a body that disables it (persist
        // flag = 0) is out of contract even with a valid checksum.
        let serialized = framed(&format!("{SCHEMA_VERSION}\n60\n30\n0\n0\n0\n0\n0\n"));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::ShutdownPersistenceRequired)
        );
    }

    #[test]
    fn empty_book_round_trips() {
        let book = VirtualLedgerBook::new();
        let restored =
            restore(&PaperStateSnapshot::capture(&book, &PersistenceConfig::default()).serialize())
                .unwrap();
        assert_eq!(restored.strategy_count(), 0);
        assert_eq!(restored, book);
    }

    #[test]
    fn symbol_with_spaces_round_trips() {
        // An OCC option contract string contains spaces; length-prefixing must keep
        // it intact through serialize/restore.
        let engine = engine();
        let mut book = VirtualLedgerBook::new();
        let s = StrategyId::new("opt");
        book.apply_fill(
            &s,
            &engine
                .simulate_fill(1, "AAPL  240119C00190000", 1, 250, None)
                .unwrap(),
        )
        .unwrap();
        let restored =
            restore(&PaperStateSnapshot::capture(&book, &PersistenceConfig::default()).serialize())
                .unwrap();
        assert_eq!(restored, book);
        assert!(restored.position(&s, "AAPL  240119C00190000").is_some());
    }

    #[test]
    fn deserialize_rejects_a_bad_magic_header() {
        let serialized = PaperStateSnapshot::capture(&sample_book(), &PersistenceConfig::default())
            .serialize()
            .replacen(MAGIC, "NOT-ATP", 1);
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::CorruptSnapshot {
                context: "magic header"
            })
        );
    }

    #[test]
    fn deserialize_rejects_an_unknown_schema_version() {
        // A body whose schema version is 999 (with a valid checksum so the version
        // gate, not the checksum, is what fails).
        let serialized = framed("999\n60\n30\n1\n0\n0\n0\n0\n");
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::UnknownSchemaVersion { found: 999 })
        );
    }

    #[test]
    fn deserialize_rejects_trailing_garbage() {
        // Appending bytes changes the checksummed body, so it is caught by the
        // integrity check (and, defence-in-depth, would also fail expect_end).
        let serialized = PaperStateSnapshot::capture(&sample_book(), &PersistenceConfig::default())
            .serialize()
            + "extra\n";
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::ChecksumMismatch)
        );
    }

    #[test]
    fn deserialize_rejects_truncation() {
        let full =
            PaperStateSnapshot::capture(&sample_book(), &PersistenceConfig::default()).serialize();
        let truncated = &full[..full.len() / 2];
        // Truncation is caught (checksum mismatch or a parse error); either way no
        // partial book is returned.
        assert!(matches!(
            PaperStateSnapshot::deserialize(truncated),
            Err(PersistenceError::ChecksumMismatch) | Err(PersistenceError::CorruptSnapshot { .. })
        ));
    }

    #[test]
    fn deserialize_rejects_a_negative_cost_component() {
        // A body for a single open long whose commission is negative (with a valid
        // checksum, so the field invariant -- not the checksum -- is what fails).
        let serialized = framed(&format!(
            "{SCHEMA_VERSION}\n60\n30\n1\n1\n1\ns\n1\n4\nAAPL\n100\n1000000\n0\n-35\n500\n1000\n0\n0\n0\n"
        ));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::InconsistentField {
                context: "negative cost component"
            })
        );
    }

    #[test]
    fn deserialize_rejects_a_structurally_valid_tampered_value() {
        // The Codex finding: a value changed to ANOTHER structurally-valid value
        // (here AAPL's cost basis 1_000_000 -> 1_000_001, still positive and
        // sign-consistent with the long quantity) would pass every field invariant.
        // The integrity checksum is what makes it fail closed under fault injection
        // instead of restoring fabricated P&L.
        let serialized =
            PaperStateSnapshot::capture(&sample_book(), &PersistenceConfig::default()).serialize();
        let tampered = serialized.replacen("\n1000000\n", "\n1000001\n", 1);
        assert_ne!(tampered, serialized);
        assert_eq!(
            PaperStateSnapshot::deserialize(&tampered),
            Err(PersistenceError::ChecksumMismatch)
        );
    }

    #[test]
    fn deserialize_rejects_a_flat_position_with_nonzero_basis() {
        // Hand-craft a v1 snapshot for one strategy holding one symbol that is flat
        // (quantity 0) but has a non-zero basis — a corruption the ledger can never
        // produce (a full close lands at basis 0).
        let serialized = framed(&format!(
            "{SCHEMA_VERSION}\n60\n30\n1\n1\n1\ns\n1\n4\nAAPL\n0\n500\n0\n0\n0\n0\n0\n0\n0\n"
        ));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::InconsistentField {
                context: "quantity/basis flat-state mismatch"
            })
        );
    }

    #[test]
    fn deserialize_rejects_a_basis_sign_mismatch() {
        // A long (quantity > 0) with a negative basis is impossible in the ledger.
        let serialized = framed(&format!(
            "{SCHEMA_VERSION}\n60\n30\n1\n1\n1\ns\n1\n4\nAAPL\n10\n-1000\n0\n0\n0\n0\n0\n0\n0\n"
        ));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::InconsistentField {
                context: "quantity/basis sign mismatch"
            })
        );
    }

    #[test]
    fn deserialize_rejects_a_non_canonical_symbol() {
        // Lower-case symbol is not canonical; a real capture only ever writes the
        // canonical (upper-case, trimmed) form.
        let serialized = framed(&format!(
            "{SCHEMA_VERSION}\n60\n30\n1\n1\n1\ns\n1\n4\naapl\n10\n1000\n0\n0\n0\n0\n0\n0\n0\n"
        ));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::InconsistentField {
                context: "non-canonical symbol"
            })
        );
    }

    #[test]
    fn deserialize_rejects_a_duplicate_symbol() {
        // One strategy "s" (id length 1) holding two positions both keyed "AAPL".
        let serialized = framed(&format!(
            "{SCHEMA_VERSION}\n60\n30\n1\n1\n1\ns\n2\n4\nAAPL\n10\n1000\n0\n0\n0\n0\n4\nAAPL\n10\n1000\n0\n0\n0\n0\n0\n0\n0\n"
        ));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::DuplicateRecord {
                context: "duplicate symbol within a strategy"
            })
        );
    }

    #[test]
    fn deserialize_rejects_a_duplicate_strategy() {
        let serialized = framed(&format!(
            "{SCHEMA_VERSION}\n60\n30\n1\n2\n1\ns\n0\n1\ns\n0\n0\n0\n0\n"
        ));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::DuplicateRecord {
                context: "duplicate strategy id"
            })
        );
    }

    #[test]
    fn deserialize_rejects_an_empty_strategy_id() {
        let serialized = framed(&format!(
            "{SCHEMA_VERSION}\n60\n30\n1\n1\n0\n\n0\n0\n0\n0\n"
        ));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::InconsistentField {
                context: "empty strategy id"
            })
        );
    }

    #[test]
    fn deserialize_rejects_a_nonzero_reserved_slot() {
        // A pending-order count of 1 has no v1 representation: fail closed rather
        // than silently dropping the data.
        let serialized = framed(&format!("{SCHEMA_VERSION}\n60\n30\n1\n0\n1\n0\n0\n"));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::UnsupportedSection {
                context: "pending simulated orders"
            })
        );
    }

    #[test]
    fn deserialize_rejects_a_zero_interval_config() {
        let serialized = framed(&format!("{SCHEMA_VERSION}\n0\n30\n1\n0\n0\n0\n0\n"));
        assert_eq!(
            PaperStateSnapshot::deserialize(&serialized),
            Err(PersistenceError::NonPositiveConfig {
                field: "interval_secs"
            })
        );
    }

    #[test]
    fn deserialize_rejects_metrics_cash_that_does_not_reconcile() {
        // The adversarial-review end-to-end guard: a CHECKSUM-VALID snapshot whose
        // metrics cash disagrees with its own trade log must fail closed. capture_full
        // always produces a coherent accumulator, so this hand-frames a snapshot with a
        // tampered cash line AND a recomputed checksum (defeating the byte-integrity
        // guard), to prove the metrics re-validation -- not merely the checksum --
        // catches an internally-inconsistent running cash from a buggy/foreign writer.
        let engine = engine();
        let strategy = StrategyId::new("s");
        let mut accumulator = PaperMetricsAccumulator::new(1_000_000).unwrap();
        accumulator
            .apply_fill(&engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap())
            .unwrap();
        let cash = accumulator.cash_minor();
        let mut metrics = HashMap::new();
        metrics.insert(strategy, accumulator);
        let snapshot = PaperStateSnapshot::capture_full(
            &VirtualLedgerBook::new(),
            &metrics,
            &HashMap::new(),
            &PersistenceConfig::default(),
        );
        // The genuine snapshot restores fine.
        assert!(PaperStateSnapshot::deserialize(&snapshot.serialize()).is_ok());

        // Strip the magic + checksum lines to get the body, tamper the (unique) cash
        // line, then re-frame with a CORRECT checksum over the tampered body.
        let serialized = snapshot.serialize();
        let after_magic = &serialized[serialized.find('\n').unwrap() + 1..];
        let body = &after_magic[after_magic.find('\n').unwrap() + 1..];
        let needle = format!("\n{cash}\n");
        assert_eq!(
            body.matches(&needle).count(),
            1,
            "the cash value must appear exactly once so the tamper targets it"
        );
        let tampered_body = body.replacen(&needle, &format!("\n{}\n", cash + 1), 1);
        let mut reframed = String::new();
        push_line(&mut reframed, MAGIC);
        push_i128(
            &mut reframed,
            i128::from(checksum(tampered_body.as_bytes())),
        );
        reframed.push_str(&tampered_body);

        // The checksum now matches, but the metrics cash no longer reconciles with the
        // trade log, so deserialize fails closed with no partially-restored state.
        assert_eq!(
            PaperStateSnapshot::deserialize(&reframed),
            Err(PersistenceError::InconsistentField {
                context: "incoherent persisted paper metrics accumulator"
            })
        );
    }

    #[test]
    fn deserialize_migrates_a_v1_ledger_only_snapshot() {
        // The adversarial-review schema-evolution fix: a v1 snapshot (origin/main,
        // ledger-only with three reserved empty slots) must still restore after the v2
        // upgrade instead of stranding state. Build the EXACT v1 wire form the prior
        // slice produced and confirm the ledger + config come back with empty metrics /
        // user-state, migrated forward to the current version.
        let book = sample_book();
        let config = PersistenceConfig::default();

        let mut body = String::new();
        push_i128(&mut body, i128::from(SCHEMA_VERSION_V1));
        push_i128(&mut body, i128::from(config.interval_secs()));
        push_i128(&mut body, i128::from(config.restore_deadline_secs()));
        push_i128(&mut body, i128::from(config.persist_on_shutdown() as i64));
        // The ledger section is byte-identical between v1 and v2 (unchanged layout).
        let mut strategies: Vec<(&StrategyId, &StrategyLedger)> = book.ledgers_iter().collect();
        strategies.sort_by(|a, b| a.0.as_str().cmp(b.0.as_str()));
        push_i128(&mut body, strategies.len() as i128);
        for (strategy, ledger) in strategies {
            push_str(&mut body, strategy.as_str());
            push_ledger(&mut body, ledger);
        }
        // v1's three reserved zero-slots (pending, metrics, user-state).
        push_i128(&mut body, 0);
        push_i128(&mut body, 0);
        push_i128(&mut body, 0);

        let mut v1 = String::new();
        push_line(&mut v1, MAGIC);
        push_i128(&mut v1, i128::from(checksum(body.as_bytes())));
        v1.push_str(&body);

        let restored = PaperStateSnapshot::deserialize(&v1).expect("v1 snapshot migrates forward");
        assert_eq!(restored.book(), &book); // the ledger survives the upgrade exactly
        assert!(restored.metrics().is_empty()); // new sub-states initialize empty
        assert!(restored.user_state().is_empty());
        assert_eq!(restored.schema_version(), SCHEMA_VERSION); // migrated to current
                                                               // ...and the migrated snapshot re-serializes as a valid v2 snapshot.
        assert!(PaperStateSnapshot::deserialize(&restored.serialize()).is_ok());
    }

    #[test]
    fn deserialize_rejects_a_huge_untrusted_record_count_without_aborting() {
        // The adversarial-review DoS fix: a checksum-valid snapshot can encode an
        // enormous record count. The reader must NOT pre-allocate with it (that would
        // capacity-overflow / OOM-abort, defeating fail-closed restore); it reads records
        // as it goes, so a count larger than the blob's real contents fails closed on the
        // first short read. Hand-frame a v2 body whose metrics trade-log count is usize::MAX
        // but which carries NO fills.
        let config = PersistenceConfig::default();
        let mut body = String::new();
        push_i128(&mut body, i128::from(SCHEMA_VERSION));
        push_i128(&mut body, i128::from(config.interval_secs()));
        push_i128(&mut body, i128::from(config.restore_deadline_secs()));
        push_i128(&mut body, i128::from(config.persist_on_shutdown() as i64));
        push_i128(&mut body, 0); // 0 ledger strategies
        push_i128(&mut body, 0); // pending reserved slot
        push_i128(&mut body, 1); // 1 metrics strategy
        push_str(&mut body, "s"); // strategy id
        push_i128(&mut body, 1_000_000); // starting_cash_minor
        push_i128(&mut body, 0); // cash_minor
        push_i128(&mut body, 0); // metrics ledger: 0 positions
        push_i128(&mut body, usize::MAX as i128); // HUGE, untrusted trade-log count
                                                  // ...and no fills follow.

        let mut blob = String::new();
        push_line(&mut blob, MAGIC);
        push_i128(&mut blob, i128::from(checksum(body.as_bytes())));
        blob.push_str(&body);

        // Returns a typed error on the missing fill, never OOM/panic-abort.
        assert!(matches!(
            PaperStateSnapshot::deserialize(&blob),
            Err(PersistenceError::CorruptSnapshot { .. })
        ));
    }

    #[test]
    fn deep_user_state_json_fails_closed_without_overflowing() {
        // The adversarial-review DoS fix: a syntactically-valid but adversarially DEEP
        // user-state dict must fail closed (is_json_object -> false -> InconsistentField)
        // rather than stack-overflow the recursive-descent validator, on BOTH the write
        // (save_to_path) and the restore (deserialize) path.
        let depth = 50_000;
        let mut deep = String::new();
        for _ in 0..depth {
            deep.push_str("{\"a\":");
        }
        deep.push('1');
        for _ in 0..depth {
            deep.push('}');
        }
        // The bounded validator rejects it without overflowing; a shallow dict is fine.
        assert!(!is_json_object(&deep));
        assert!(is_json_object("{\"a\":1}"));

        let mut user_state: HashMap<StrategyId, String> = HashMap::new();
        user_state.insert(StrategyId::new("s"), deep);
        let snapshot = PaperStateSnapshot::capture_full(
            &VirtualLedgerBook::new(),
            &HashMap::new(),
            &user_state,
            &PersistenceConfig::default(),
        );
        let not_a_json_object = PersistenceError::InconsistentField {
            context: "user-state value is not a JSON object",
        };
        // Restore path fails closed.
        assert_eq!(
            PaperStateSnapshot::deserialize(&snapshot.serialize()),
            Err(not_a_json_object.clone())
        );
        // Write path fails closed too (before creating the store).
        let dir = std::env::temp_dir().join(format!("atp-sim004-deepjson-{}", std::process::id()));
        assert_eq!(snapshot.save_to_path(&dir), Err(not_a_json_object));
    }
}
