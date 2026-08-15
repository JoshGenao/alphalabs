# SRS-RESV-006 — enforce Hot-Swap cool-down (SyRS SYS-49e)

## AMENDMENT — SRS-RESV-004 landed mid-session (rebased onto it at f218386)

RESV-004 integrated `serialized` while this plan was being approved, and touched every
shared file the plan named. Deltas to the plan below, all verified against the rebased tree:

1. **`entity_count` is 18 → 19**, not 17 → 18. RESV-004 registered
   `hot-swap-demotion-pending` (owner SRS-RESV-004).
2. **The dashboard seam changed shape.** RESV-004 built `CompositeHotSwapStatusSource`
   (`python/atp_hotswap/__init__.py:472`) with `triggers` + `demotion` legs, where
   `live_state()` delegates to the demotion leg **alone**. So the plan's "implement
   `CliHotSwapTriggerSource.live_state()`" is wrong. Instead: a new
   `CliHotSwapCooldownSource` mirroring `CliHotSwapDemotionSource:348`, a
   `HotSwapCooldownLeg` Protocol, a third `cooldown=` kwarg on the composite, and
   `live_state()` **merges** the demotion + cool-down mappings.
   **Residual to record:** the pane's protocol has one `live_state` leg, so the two
   sub-legs share a failure domain — an unreadable demotion lockout defers the cool-down
   cells too. Fail-closed and honest (the raise names which sub-leg failed), but coarser
   than per-fact independence. Owner of the finer split: UI-5's pane protocol.
3. **`evaluate_automatic_triggers` / `request_manual_promotion` were NOT touched** — the six
   `lib.rs` hunks stand. `resolve_demotion` gained an `L: DemotionPendingLock` generic;
   still do not touch it.
4. **Do not commit `progress.d/plan-<id>.md`** — the integrate guard refuses it
   (`pipeline-and-integrate.md`, added by RESV-004 after hitting it at the last step).
5. **Five new playbook rules apply to this diff**, three of them directly:
   - *safety-paths 41* — a failed durable write must leave a fail-closed STATE, not just a
     truthful error. Applies to `record_completion`: if the window write fails, the swap
     happened and no window started, which is fail-OPEN. A CLI that exits cannot hold an
     in-memory poison, so the honest handling is a nonzero exit whose message says the
     window did NOT start, plus the residual stated in `deferred[]` — per that rule's own
     counter-rule ("state the residual instead of implying it does").
   - *honest-surfaces 38* — validate every value the command will PRINT before it mutates.
     `record-completion` validates ids + timestamp before the durable write.
   - *test-integrity 27* — anchor a mutation on the declaration's SPAN, not a repeated
     token. Bit RESV-004 four times.

## Context

**The requirement.** SRS-5.6 / SYS-49e: *"After successful swap, automatic triggers are
ignored for the configured cool-down period defaulting to 7 calendar days; manual swap
during cool-down requires confirmation warning; the cool-down start time is the timestamp
of the most recent successful swap completion."*

**Why now.** SRS-RESV-003 (trigger decision + config + logging) closed green and explicitly
deferred this. `crates/atp-orchestrator/src/lib.rs:1895-1897` says so in the code: *"The
cool-down confirmation warning for a manual swap during cool-down (SYS-49e) is the deferred
SRS-RESV-006 concern and is intentionally NOT enforced here."* Today **nothing** enforces a
cool-down anywhere in Rust — the only artefacts in the tree are a display constant
(`COOLDOWN_DAYS_DEFAULT = 7`, `python/atp_dashboard/hotswap.py:128`) and a pure JS dial
classifier. Automatic triggers can therefore fire back-to-back with no window at all.

**The gap this closes.** A durable cool-down window, a gate that both of RESV-003's entry
points pass through, and the three UI-5 cool-down cells that have rendered
`deferred:SRS-RESV-006` since UI-5 shipped.

**Prior session:** none for this feature (`progress.d/session-SRS-RESV-006.md` absent).
Verified against the tree — no cool-down module, no swap-completion store, `live_state()`
returns `None`. This is genuinely unbuilt, not churn.

**Live-sibling constraint.** `SRS-RESV-004` (demotion) and `SRS-RESV-005` (promotion) are
leased and running *right now* off the same base commit `04ed0ff`. RESV-005 is the real
producer of "successful swap completion". The design below deliberately keeps ~95% of the
diff in two new files no sibling will open, and touches `crates/atp-types/src/lib.rs` and
`python/atp_dashboard/hotswap.py` **zero** times.

**Operator decisions taken during planning:**
1. Omitted `--cooldown-state` → **fail closed** (Unknown → suppressed → nonzero exit).
2. **Full surface scope** — core + CLI + dashboard + REST.
3. **Extend `SAFETY_PATH_RE`** with `hot_swap` + `cooldown` in a prep commit.

**Expected completeness: `serialized`.** Step 2 requires browser automation against the
dashboard plus REST checks, which bind shared resources I must not touch while siblings
run. Steps 1/3/4 are provable solo. After integrating I will
`block SRS-RESV-006 --on SRS-RESV-005` — the production writer of a swap completion.

---

## The design in one paragraph

A pure classifier (`cooldown.rs`) turns *(last completion, configured period, now)* into a
four-state `CooldownState`. A durable store (`cooldown_store.rs`) holds the period + the
last completion and is the only place a store failure becomes a state. One predicate —
`CooldownState::proven_clear()`, true only for `NeverSwapped | Expired` — drives **both**
arms: automatic **suppresses**, manual **requires acknowledgement**. Manual is never
blocked; the identical call with `Acknowledged` always fires.

---

## Files

### New — carry ~95% of the diff, zero sibling conflict

| Path | What |
|---|---|
| `crates/atp-orchestrator/src/cooldown.rs` | Pure types + `classify` + `proven_clear`. No I/O, no clock. |
| `crates/atp-orchestrator/src/cooldown_store.rs` | Durable window. Mirrors `trigger_config_store.rs` exactly. |
| `crates/atp-orchestrator/src/bin/resv006_hot_swap_cooldown_cli.rs` | `status` / `configure` / `record-completion`. |
| `tools/hot_swap_cooldown_check.py` | Static gate, mirroring `tools/hot_swap_trigger_check.py`. |

### Modified — small, surgical hunks

| Path | Change |
|---|---|
| `crates/atp-orchestrator/src/lib.rs` | **6 hunks, ~45 lines.** `pub mod` + `use`; `TriggerEvaluation.cooldown` field + `empty()` signature; the two gates; `ManualPromotionError::CooldownConfirmationRequired`. **Do not touch `resolve_demotion` (~L1650-1713) — that is RESV-004's.** |
| `crates/atp-orchestrator/src/bin/resv003_hot_swap_trigger_cli.rs` | Real clock; `--now`, `--cooldown-state`, `--confirm-cooldown`; proof lines; USAGE. |
| `crates/atp-data/src/schema_registry.rs` | One `SchemaDescriptor` row inserted **after `hot-swap-trigger-config`** (mid-file, not appended — siblings append). |
| `architecture/runtime_services.json` | New `hot_swap_cooldown_contract` block right after `hot_swap_trigger_contract`; `schema_evolution_contract.entity_count` **17 → 18** (verified: `PERSISTED_ENTITIES` holds exactly 17). |
| `tools/gates.json`, `tools/architecture_check.py` | Register the new check (`gates_registry_check.py` fails CI otherwise). |
| `python/atp_hotswap/__init__.py` | `ATP_HOT_SWAP_COOLDOWN_BINARY` knob; implement `live_state()`. |
| `python/atp_dashboard/server.py` | Wire the cool-down state path. |
| `python/atp_orchestration/hot_swap_triggers.py` | `confirm_cooldown` key; the CONFIRMATION_REQUIRED branch; required mount arg. |
| `python/atp_dashboard/assets/hotswap.js` | `in_effect` becomes the authority for `state`. |
| `tools/critic_check.py` | **prep commit** — `SAFETY_PATH_RE` += `hot[_-]?swap|cool[_-]?down`. |

**`crates/atp-types/src/lib.rs` and `python/atp_dashboard/hotswap.py`: zero edits.** Every
new type lives in `atp-orchestrator`, where `TriggerEvaluation` / `ManualPromotionError` /
the three RESV-003 ports already live. The dashboard flip seam works as designed.

---

## Key signatures

```rust
// cooldown.rs
pub const COOLDOWN_DAYS_DEFAULT: u32 = 7;
pub const SECONDS_PER_CALENDAR_DAY: u64 = 86_400;   // UTC: no DST, no leap seconds
pub const COOLDOWN_DAYS_MAX: u32 = 365;

pub struct CooldownPeriodDays(u32);                 // ::new refuses 0 and > 365
pub struct SwapCompletion { completed_at_seconds, demoted_strategy_id, promoted_strategy_id }

pub enum CooldownState {
    NeverSwapped,
    Active  { started_at_seconds, expires_at_seconds, remaining_seconds },
    Expired { started_at_seconds, expires_at_seconds },
    Unknown { reason: String },
}
impl CooldownState {
    pub fn classify(Option<&SwapCompletion>, CooldownPeriodDays, now_seconds) -> Self;
    pub fn proven_clear(&self) -> bool;             // NeverSwapped | Expired — THE predicate
    pub fn confirmation_warning(&self) -> Option<String>;   // Some ⟺ !proven_clear()
}

/// An enum, not a bool: a bare `true` at the call site is one character from the opposite.
/// Derives NO Default — a caller must state which one it means.
pub enum ManualCooldownAcknowledgement { NotAcknowledged, Acknowledged }
```

`classify`'s two load-bearing lines:
- `now.saturating_sub(started)` — **not** `now - started`. A backwards NTP step would wrap
  to ~5.8e11 years elapsed and silently retire a live window.
- `started.checked_add(period)` — **not** `saturating_add`. A saturated `u64::MAX` reads as
  "active forever" with no explanation; `Unknown` names the cause.

Window is half-open `[start, start + period)` — at exactly `start + 7d` the state is
`Expired`. Pinned by a boundary test pair.

```rust
// lib.rs — the two gates
pub fn evaluate_automatic_triggers<L, R, S>(
    &self, config: &HotSwapTriggerConfig, cooldown: &CooldownState,
    live: &L, ranking: &R, log: &S, observed_at_seconds: u64) -> TriggerEvaluation

pub fn request_manual_promotion<S: HotSwapTriggerLog>(
    &self, demoting_strategy_id: StrategyId, candidate_strategy_id: StrategyId,
    cooldown: &CooldownState, acknowledgement: ManualCooldownAcknowledgement,
    log: &S, observed_at_seconds: u64) -> Result<HotSwapTriggerProposal, ManualPromotionError>
```

A resolved **value**, not a 4th injected port: the store read and the `Err → Unknown`
mapping then happen in exactly one function (`cooldown_store::resolve`) at the composition
root, and the ~10 gate tests inject fixtures with zero I/O. The fabrication risk that opens
is closed by a static check, not by types (see gate #9 below).

`TriggerEvaluation` gains `pub cooldown: CooldownState`, and `empty()` **takes** it — if it
defaulted to `NeverSwapped`, a degraded-probe pass would claim "no cool-down in effect"
about a fact it never read. `ManualPromotionError` gains
`CooldownConfirmationRequired { state, warning }`, which makes the CLI's existing match
non-exhaustive → compile error → forced handling.

---

## Fail-closed table

Automatic suppresses ⟺ `!proven_clear()`. Manual warns ⟺ `!proven_clear()`. **Same
predicate**, so one mutation reddens both test families. The asymmetry is the
*consequence*: automatic → SUPPRESSED, manual → WARNED. Neither ever permanently blocks.

| Condition | State | Automatic | Manual |
|---|---|---|---|
| No swap ever / store absent | `NeverSwapped` | proceeds | fires, no confirmation |
| `--cooldown-state` omitted | `Unknown` | suppressed + degraded, **exit ≠ 0** | confirmation required |
| Empty / corrupt / foreign magic / bad version | `Unknown` | suppressed + degraded | confirmation required |
| Unreadable (EACCES) / lock timeout | `Unknown` | suppressed + degraded | confirmation required |
| `now` inside window | `Active` | suppressed, **nothing fired, nothing logged** | required; `Acknowledged` → fires + logs |
| `now ≥ start + period` | `Expired` | proceeds | fires, no confirmation |
| Clock steps backwards / completion in the future | `Active` | suppressed | confirmation required |
| `start + period` overflows u64 | `Unknown` | suppressed + degraded | confirmation required |
| Clock before Unix epoch | CLI refuses before classifying | pass refused | request refused |
| Older completion offered to `record_completion` | window unchanged | — | — (`KeptNewer`, exit ≠ 0) |

**Absent = permissive is deliberate and inverts the `trigger_config_store` precedent.**
Before RESV-005 exists no swap has ever completed, so no window can logically be in effect;
treating "no history" as "in cool-down" would leave RESV-003's automatic triggers
permanently dead on a fresh install. An **empty-but-present** file is a torn write →
`Unknown`. This reasoning goes in the contract block, not just a comment.

`Unknown` also joins `degraded_inputs`, so the CLI's existing process-level fail-closed
check catches it with no new CLI branch. `Active` is **not** degraded — a working cool-down
is healthy, exit 0 is correct.

---

## Durable store

`MAGIC = "ATP-HOT-SWAP-COOLDOWN"`, `COOLDOWN_SCHEMA_VERSION: i64 = 1`, posture `Pinned`,
`legacy_unversioned: false`. One entity holds **both** period and last completion —
classification needs them atomically, and one lock beats two files.

Write recipe verbatim from `trigger_config_store::save`: scratch
`<name>.<pid>.<seq>.hot-swap-cooldown.tmp` → `write_all` + `flush` + `sync_all` → `rename`
→ parent-dir `sync_all`. **Reuse `trigger_config_store::ExclusiveGuard` unchanged** — a
duplicate O_EXCL lock implementation is a BLOCK under CLAUDE.md rule 1. Convert
`TriggerConfigStoreError → CooldownStoreError` by structural match on its public variants,
never by string-munging its `Display` (or the operator sees "cannot lock trigger
configuration" for a cool-down file).

The three `last_*` payload fields are **all-present-or-all-absent**; any partial
combination is `Malformed`. `cooldown_days` is re-validated through
`CooldownPeriodDays::new` on read — a hand-edited file is exactly where a 0 would enter.

`record_completion` returns `Recorded { previous }` or `KeptNewer { stored, offered }` — a
window only moves forward, or a backwards clock could shorten a live safety window.

**All `fs` writes stay inside `cooldown_store.rs`.** `tools/data015_schema_check.py`
greps `crates/**/src/**` (including `src/bin/`) for `File::create` / `OpenOptions` /
`fs::write`; a CLI that writes bytes itself becomes an unregistered persisted format and
fails CI.

---

## The write path — real now, honestly deferred

`record_completion` is durable and tested from day one. Its **production** caller is
RESV-005's promotion path, which does not exist yet. So:

- `resv006_hot_swap_cooldown_cli record-completion` is the shipped write surface, and says
  so in its own output: `deferred-writer:SRS-RESV-005`.
- `deferred[]` records the precise seam, including the trap: **RESV-004's demotion can
  finish without a promotion** (the SYS-49b demotion-pending timeout). That is not a swap
  and must not start a window. Only the promotion side may write the record.
- **Do not** add a `SwapCompletionSink` port to `lib.rs` — that designs RESV-005's seam for
  them, in the file most likely to conflict. A module-level `pub fn` is callable from
  wherever they put their gate.
- **Do not** call `record_completion` from `resolve_demotion`.

The whole loop — `record-completion` → `evaluate` suppresses → 7 days later `evaluate`
fires — runs through two real binaries and one real file, so the AC is demonstrable today
with zero fabrication.

---

## CLI + surfaces

**`resv003_hot_swap_trigger_cli.rs`**: delete `const OBSERVED_AT_SECONDS = 1_715_000_000`.
Read the real clock by default with `--now <epoch-seconds>` to override (durable-writes
rule 23: *"a frozen `--now` default makes a cron entry see zero elapsed time forever"*). A
pre-epoch clock is `Err`, **not** `unwrap_or(0)` — a 0 would make every window read as long
expired. `SystemTime::now()` is already used in this crate
(`kill_switch_activation.rs:357`, `connectivity_notification.rs:95`) and
`tools/determinism_check.py` is scoped to `atp-simulation`, so this is allowed. Rewrite the
module doc line that currently claims wall-clock is never read.

New proof lines: `observed-at-seconds`, `cooldown-state:{NEVER_SWAPPED|ACTIVE|EXPIRED|UNKNOWN}`,
`cooldown-suppressed`, `cooldown-started-at-seconds` / `-expires-at-seconds` /
`-remaining-seconds`; manual adds `cooldown-confirmation-required`, `cooldown-confirmed`,
`cooldown-override`, and on refusal `manual-refused:COOLDOWN_CONFIRMATION_REQUIRED` +
`cooldown-warning:<text>`. `manual-always-available:true` **stays**.

**REST** (`hot_swap_triggers.py`): add `confirm_cooldown` to the accepted-keys allow-list.
**Keep it distinct from `request.confirmed`** — that is the transport's SYS-49a
confirmation, and conflating them would make every ordinary confirmed manual trigger a
silent cool-down override. Before the generic `returncode != 0 → MANUAL_TRIGGER_UNLOGGED`
branch (`hot_swap_triggers.py:302`), detect the cool-down refusal and raise
`CONFIRMATION_REQUIRED` / `HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED` — otherwise a
confirmation refusal is reported as "the trigger was not logged", which is a lie.
`POST /api/v1/hot-swap` and `GET /api/v1/hot-swap/status` **stay 501** (their other three
fields are RESV-004/005 territory; binding them would fabricate). No new REST route — the
dashboard route already carries the cells. Record as a deliberate refusal, not a deferral.

**Dashboard**: implement `CliHotSwapTriggerSource.live_state()` to return **only**
`{"cooldown": {...}}`. `NEVER_SWAPPED` → `in_effect: False`, no timestamps.
`ACTIVE`/`EXPIRED` → bool + two ISO-8601 UTC strings. **`UNKNOWN` → raise
`HotSwapStatusUnavailable`** — it must never emit `in_effect: False` (rule 3). The
`current_live_strategy_id` / `demotion_pending` / `sequence` keys stay **absent**, so those
cells keep rendering deferred to RESV-005/004 (verified: `hotswap.py:437` `_rung` reads
`live.get("sequence")` and defers on absence). `hotswap.js` takes `in_effect` as the
authority for `state`, keeping its clock arithmetic only for `fraction`/`label` — otherwise
client clock skew becomes a second source of truth.

---

## Tests

Rust unit tests inline with their subject are invisible to `tools/mutation_verify.py` (it
reverts the whole file), so **only** serializer round-trips stay inline; every behavioural
test lives in `crates/atp-orchestrator/tests/`.

- **`resv_6_cooldown_classification.rs`** (pure) — never-swapped ≠ active; the
  `[start, start+7d)` boundary **pair**; backwards clock cannot expire; future timestamp
  fails closed to active; u64 overflow → `Unknown` not `Expired`; `Unknown` is not
  `proven_clear`; 0-day and >365-day periods refused; default is exactly 604 800s;
  `confirmation_warning` is `Some` ⟺ `!proven_clear`.
- **`resv_6_cooldown_gate.rs`** — active cool-down suppresses all three armed triggers **and
  the sink holds zero records** (a log-count assertion is what makes "ignored" mean
  ignored); **expired lets them fire again** (the non-vacuity control — without it the first
  test passes on a wholly broken evaluator); unknown suppresses + surfaces a degraded input;
  suppression is distinguishable from no-candidates; manual unacknowledged is refused and
  logs nothing; **manual acknowledged fires and logs** (the always-available invariant);
  manual outside cool-down is unchanged; self-swap refused *before* the cool-down warning;
  the warning names the expiry.
- **`resv_6_cooldown_store.rs`** — absent → `Ok(None)`; empty file is corrupt not absent;
  foreign magic; unknown field; future version; partial `last_*` triple; round-trip; an
  older completion cannot shorten a live window; a persisted 0-day period refused on read;
  **an unreadable store resolves to `Unknown`, never `NeverSwapped`**; `set_period`
  preserves the completion.
- **`resv_6_cli_fail_closed.rs`** — omitted `--cooldown-state` suppresses + exits nonzero;
  evaluate during cool-down appends no log record; manual during cool-down exits nonzero and
  names the warning; `--confirm-cooldown` appends exactly one record; `record-completion`
  starts a window the next `evaluate` observes (both binaries, one file); **`--now` defaults
  to the real clock** (assert `observed-at-seconds` is within seconds of the harness clock).
- **L2 property** `tests/property/test_hot_swap_cooldown_cells.py` — Hypothesis over
  arbitrary CLI stdout: the pane never renders `in_effect: true` without both timestamps,
  and never renders `UNKNOWN` as `in_effect: false`.
- **L4 boundary** `tests/boundary/test_hot_swap_cooldown_surface.py` — cool-down refusal is
  `CONFIRMATION_REQUIRED`, not `INTERNAL_ERROR`; `confirm_cooldown` forwarded as a flag
  (argv assertion); **transport confirmation does not satisfy the cool-down confirmation**;
  unknown state makes the live leg unavailable, not false; the cool-down leg failing does
  not blank the trigger chips; mount requires a cool-down path; cells resolve with
  `data_source: hot_swap_state`; status + execution routes stay 501.
- **L7 domain** `tests/domain/test_hot_swap_cooldown.py` (drives the real binaries) — a
  recorded completion suppresses automatic triggers for 7 days; **the window start is the
  completion timestamp, not the write time** (the AC's third clause, verbatim); manual
  during cool-down requires confirmation and says why; the cool-down survives a process
  restart; shortening the period reopens the window; a corrupt file never reads as no
  cool-down.

**No L5/L6 runs** while siblings are active. `tests/e2e/test_hot_swap_triggers.py` needs a
follow-up cool-down scenario — written but shipped **unrun**, and named explicitly as the
serialized step.

**Mutation-verify** at minimum: the backwards-clock test, the overflow test, `proven_clear`
excluding `Unknown`, the suppress/expired **pair**, the manual unacknowledged/acknowledged
**pair**, the real-clock default, the unreadable-store test, and the two REST
error-mapping tests.

---

## Contract + static gate

New `hot_swap_cooldown_contract` block in `architecture/runtime_services.json`, inserted
contiguously right after `hot_swap_trigger_contract` so a JSON conflict resolves by keeping
both hunks. It pins the wire strings, `zero_is_disabled: false`, the store's magic/marker/
durability/read-states, and the guard tokens.

`tools/hot_swap_cooldown_check.py` (reuses `tools/_rust_parser`) asserts:
1. `CooldownState` declares four variants, each 1:1 with its wire string.
2. `CooldownPeriodDays::new` guards 0 and `COOLDOWN_DAYS_MAX`; the two constants are declared.
3. `ManualCooldownAcknowledgement` has both variants and **does not derive `Default`**.
4. `classify` contains `saturating_sub` **and** `checked_add`, and no bare `now_seconds -`.
5. `proven_clear` matches exactly `NeverSwapped | Expired` and never names `Unknown`.
6. **Both** entry-point bodies reference `proven_clear` — the anti-bypass check.
7. `TriggerEvaluation.cooldown` and `ManualPromotionError::CooldownConfirmationRequired` exist.
8. `cooldown_store.rs` declares its magic + version marker, the four durability tokens, and
   `record_completion` names `ExclusiveGuard` + the keep-newer branch.
9. **The clock/fabrication guard**: both CLI subcommand bodies contain
   `cooldown_store::resolve(` and **no** `CooldownState::` literal; the file contains
   `SystemTime::now` and **not** `1_715_000_000`.
10. The registry row exists with the same `entity_id`, magic and marker.

Register in `tools/architecture_check.py` and `tools/gates.json`
(`gates_registry_check.py` fails CI otherwise); bump the `architecture_check` "54 of the
other checks" string to 55.

---

## Order of work

1. **prep commit** — `SAFETY_PATH_RE` += `hot[_-]?swap|cool[_-]?down` in
   `tools/critic_check.py`, with provenance. Keep it to that one hunk: it lands on the
   siblings' rebase too and will demand a paired `tests/domain/` diff from their in-flight
   commits.
2. `cooldown.rs` → its classification tests (pin the classifier while it is cheap to change).
3. `cooldown_store.rs` → its store tests.
4. Registry row + `entity_count: 18`; run `tools/data015_schema_check.py`.
5. `lib.rs` — the six hunks. Update RESV-003's ~20 existing call sites in
   `resv_3_hot_swap_triggers.rs` with `&CooldownState::NeverSwapped` /
   `ManualCooldownAcknowledgement::NotAcknowledged`, asserting its behaviour is unchanged.
6. `resv_6_cooldown_gate.rs`.
7. RESV-003 CLI (real clock + flags); fix `resv_3_cli_fail_closed.rs`.
8. `resv006_hot_swap_cooldown_cli.rs` + `Cargo.toml` `[[bin]]`; `resv_6_cli_fail_closed.rs`.
9. Contract block + check + `architecture_check` + `gates.json`; run
   `gates_registry_check.py && architecture_check.py`.
10. Python: `atp_hotswap` `live_state()`, `server.py` wiring, REST arm, `hotswap.js`.
11. The three Python test files.
12. Playbook write-back (Step 8.1).

---

## Verification

Every step recorded **through the recorder**, since the integrator re-runs a solo feature's
commands (`tools/evidence.py run`, never `record`, no shell metacharacters):

```bash
python3 tools/evidence.py run "$ATP_FEATURE_ID" --step 1 -- ./init.sh
python3 tools/evidence.py run "$ATP_FEATURE_ID" --step 3 -- pytest tests/domain/test_hot_swap_cooldown.py -q
python3 tools/evidence.py run "$ATP_FEATURE_ID" --step 4 -- cargo test -p atp-orchestrator
python3 tools/evidence.py verify "$ATP_FEATURE_ID"
```

Step 2 (browser automation + REST) is the **serialized** step — it binds the dashboard
stack, which I must not touch while siblings run. It will be recorded as such, not faked.

Gate, in order — check `pgrep -x cargo` is empty before the workspace suite (**not**
`pgrep -f "cargo test"`, which matches this prompt's own text):

```bash
source .venv/bin/activate && pip install -r requirements-dev.txt   # init.sh skips these
python3 tools/mutation_verify.py origin/main..HEAD                 # with the VENV python
tools/run_ci_locally.sh                                            # must print "every step ran"
cargo test --workspace
pytest -m "not integration and not e2e"
python3 tools/critic_check.py --staged --format text
python3 tools/adversarial_review.py origin/main                    # both passes must APPROVE
```

Then: commit prep → feat → chore, `integrate --mode serialized`, and
`agent_pool.py block SRS-RESV-006 --on SRS-RESV-005`. The board shows
`frontier: DEADLOCK` with 0 ready features, so the session ends there rather than claiming
another.

**Expect a rebase conflict** on `architecture/runtime_services.json` (`entity_count` is a
single integer both siblings may bump) and possibly `schema_registry.rs`. `integrate`
aborts rather than pushing a conflicted `main`; I resolve, re-run the gate, and retry.
