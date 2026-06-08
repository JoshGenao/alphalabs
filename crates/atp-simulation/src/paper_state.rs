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
//! Of the four sub-states SYS-89 names, only the **virtual position ledger**
//! ([`crate::virtual_ledger::VirtualLedgerBook`], SRS-SIM-003) has a runtime
//! representation today. So this slice ships:
//!
//!   * [`PaperStateSnapshot`] — a versioned envelope that captures the full ledger
//!     (every strategy, every symbol, all six [`VirtualPosition`] fields) plus the
//!     persistence config, with reserved, forward-compatible slots for the three
//!     not-yet-built sub-states (pending orders, metrics, user-state);
//!   * [`PaperStateSnapshot::serialize`] / [`PaperStateSnapshot::deserialize`] — a
//!     hand-rolled, **deterministic** (sorted-key), zero-dependency text codec;
//!   * [`PersistenceConfig`] — the SYS-89 cadence constants (default 60s interval,
//!     30s restore deadline, persist-on-shutdown), validated fail-closed;
//!   * [`restore`] — the convenience round-trip back to a [`VirtualLedgerBook`].
//!
//! The pieces required to flip SRS-SIM-004 to `passes:true` are deferred (see
//! `architecture/runtime_services.json#sim_persistence_contract.deferred`): a live
//! 60s timer firing inside a running container and a real restart restoring within
//! 30s need the SRS-EXE-002 orchestrator wiring and the SYS-89 container lifecycle;
//! the `pending_orders` slot needs a paper-order pending store (SRS-SIM-001/002 own
//! no such store yet); the `metrics` slot needs the SYS-85 / SRS-BT-004 paper
//! metric family; the `user_state` slot needs the Python strategy runtime. So
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

use atp_types::StrategyId;

use crate::virtual_ledger::{StrategyLedger, VirtualLedgerBook, VirtualPosition};

/// The snapshot schema version. Bumped only when the serialized layout changes in
/// a backward-incompatible way; [`PaperStateSnapshot::deserialize`] rejects any
/// other version loudly ([`PersistenceError::UnknownSchemaVersion`]) rather than
/// silently mis-reading an old or future layout.
pub const SCHEMA_VERSION: i64 = 1;

/// The magic header line that prefixes every serialized snapshot, so a foreign or
/// truncated blob is rejected before any field is parsed.
pub const MAGIC: &str = "ATP-PAPER-STATE";

/// SYS-89 default persistence interval (seconds): persist state every 60 seconds.
pub const DEFAULT_INTERVAL_SECS: u64 = 60;

/// SYS-89 default restore deadline (seconds): restore within 30 seconds of a
/// container restart, excluding warm-up.
pub const DEFAULT_RESTORE_DEADLINE_SECS: u64 = 30;

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
}

/// A versioned, restorable snapshot of one simulation engine's paper state.
///
/// Carries the schema version, the [`PersistenceConfig`] in force, and a clone of
/// the [`VirtualLedgerBook`]. The pending-order, metric, and user-state sub-states
/// SYS-89 also names have no runtime representation yet, so they are reserved,
/// always-empty slots in the serialized form (see the module docs); this reader
/// fails closed ([`PersistenceError::UnsupportedSection`]) if a snapshot ever
/// carries data in them, because a v1 reader has no type to restore it into.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaperStateSnapshot {
    schema_version: i64,
    config: PersistenceConfig,
    book: VirtualLedgerBook,
}

impl PaperStateSnapshot {
    /// Capture the current paper state: a snapshot at [`SCHEMA_VERSION`] holding a
    /// clone of `book` and `config`. Pure and deterministic — no clock, no I/O.
    pub fn capture(book: &VirtualLedgerBook, config: &PersistenceConfig) -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            config: config.clone(),
            book: book.clone(),
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

    /// Consume the snapshot and return the restored [`VirtualLedgerBook`].
    pub fn into_book(self) -> VirtualLedgerBook {
        self.book
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

        // Reserved, forward-compatible slots for the SYS-89 sub-states that have no
        // runtime representation yet. v1 always writes 0; deserialize fails closed
        // on a non-zero count rather than silently dropping data it cannot restore.
        push_i128(&mut body, 0); // pending simulated orders
        push_i128(&mut body, 0); // accumulated paper metrics
        push_i128(&mut body, 0); // user-state dictionary entries

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
        if schema_version != SCHEMA_VERSION {
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

        // Reserved slots: v1 has no type to restore these into, so any non-zero
        // count is unsupported rather than silently ignored.
        for context in [
            "pending simulated orders",
            "accumulated paper metrics",
            "user-state dictionary",
        ] {
            if cursor.read_count(context)? != 0 {
                return Err(PersistenceError::UnsupportedSection { context });
            }
        }

        cursor.expect_end()?;

        Ok(Self {
            schema_version,
            config,
            book: VirtualLedgerBook::from_ledgers(ledgers),
        })
    }
}

/// Restore a [`VirtualLedgerBook`] from a serialized snapshot — the convenience
/// round-trip for the common case where only the ledger is needed.
/// `restore(snapshot.serialize())` reproduces `snapshot.book()` exactly.
pub fn restore(serialized: &str) -> Result<VirtualLedgerBook, PersistenceError> {
    PaperStateSnapshot::deserialize(serialized).map(PaperStateSnapshot::into_book)
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
    /// The snapshot carried data in a reserved slot (pending orders, metrics, or
    /// user-state) that this version has no type to restore.
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
}
