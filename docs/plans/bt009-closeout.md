# Closeout plan — SRS-BT-009 (persist completed backtest results) → `passes:true`

**Operator-directed target.** This is a multi-session plan to take **SRS-BT-009** all the way to
`passes:true` — the project's first genuine end-to-end feature close. It is written for cold-start
autonomous agents: read this in full before touching code. Split into **Phase 1** (one session) and
**Phase 2** (one session). Each phase follows the repo's prep → feat → chore commit cadence and the
two-layer critic gate.

> **Why BT-009 first:** Unlike the execution/sim/data threads (gated on the Python strategy host, IB
> adapter, orchestrator, durable tiering), BT-009's remaining gap is **self-contained Rust file I/O +
> a runnable verification path**. The store logic is already built; the result *producer* runs in pure
> Rust over fixture ports. So it is the cheapest path to a real green checkmark, and the durable-file
> primitive it builds also unlocks the "stable across restart" clauses of SRS-SIM-004 and SRS-EXE-008.

---

## 1. The acceptance criterion (what must be true to flip `passes:true`)

`feature_list.json` SRS-BT-009 steps:
- **Step 1:** `./init.sh` → `Environment ready`.
- **Step 2:** Exercise via **CLI/API workflows with fixture market data, provider mocks, file reads,
  and persisted-output inspection**.
- **Step 3 (AC):** *Parameters, metrics, trade log, equity curve, benchmark comparison, strategy code
  version, and timestamp are **queryable by strategy, date range, and parameter set**.*
- **Step 4:** Record objective evidence; leave `passes:false` until proven end to end.

SRS table (`docs/SRS.md`): AC = "persist completed backtest results"; method = the 7 artifacts
queryable by the 3 axes. Traces **SyRS SYS-21, SYS-79; StRS SN-1.02 / SN-1.04**.

**BT-009 is NOT safety-critical** (no `tests/domain/` pairing required). Verification method is
test/inspection — no performance gate, no live infra.

---

## 2. What already exists (DO NOT rebuild — wrap it)

`crates/atp-simulation/src/backtest_store.rs` (~2168 lines) already provides, tested:
- **`BacktestRecord`** — bundles all 7 AC artifacts: `BacktestRequest` params (the parameter set),
  SRS-BT-004 `PerformanceMetrics`, `Vec<Fill>` trade log, `Vec<EquityPoint>` equity curve, SRS-BT-005
  `BenchmarkComparison`, `CodeVersion`, producer-supplied `completed_at_ts`. `RunId` is the key.
  `BacktestRecord::from_result(...)` binds a `BacktestResult` to its persisted artifacts.
- **`BacktestResultStore { records: Vec<BacktestRecord> }`** with `insert` (rejects duplicate `RunId`),
  `query_by_strategy`, `query_by_run_window`, `query_by_completion_window`, `query_by_parameter_set`,
  and combined `query(&RecordQuery)`. Records held in canonical `(completed_at_ts, run_id)` order →
  deterministic queries.
- **`serialize(&self) -> String`** and **`restore(serialized: &str) -> Result<Self, StoreError>`** — a
  fail-closed, checksummed, dependency-free text codec where `restore(serialize(store)) == store`.

`crates/atp-simulation/src/backtest.rs`:
- **`Backtester::run(request, &mut impl BacktestStrategy, &impl BarSource) -> Result<BacktestResult>`** —
  runs over the **Rust `BarSource` + `BacktestStrategy` ports**. A fixture `VecSource` (`impl BarSource`)
  already exists in its tests. **A complete backtest result can be produced in pure Rust with a fixture
  strategy + fixture bars — no Python strategy host.**

Wiring already present: `tools/backtest_store_check.py` (run by `init.sh` with `--require-cargo` and via
`tools/architecture_check.py::assert_sim_backtest_store`); `tests/test_backtest_store_contract.py`;
`architecture/runtime_services.json` block `sim_backtest_store_contract`. `python/atp_config` has a
`STORAGE_PATHS` config category + a `PATH` key type (absolute-path validation).

---

## 3. The gap (exactly what is missing)

1. **Durable file persistence** — `serialize()` produces the blob but nothing writes it to disk; the
   module doc defers "writing that blob to the SSD-primary/NAS-archival tier" to SRS-DATA-008. **The
   store has no `save`/`load` to a filesystem path.**
2. **A runnable end-to-end operator workflow** (Step 2) — there is no CLI/API path that persists a
   completed result to a file and queries it back with file inspection. `atp_cli` has **no** backtest
   command; the REST endpoint in `openapi.json` is "contract only".

**Correctly deferred (DO NOT pull in — keep BT-009 atomic):**
- The **physical SSD-primary/NAS-archival tiering, eviction, and NAS fallback** = SRS-DATA-008.
  BT-009 owns *a* durable file write to a **configured results directory**; DATA-008 later makes that
  directory a tiered/evicting/failover store. (DATA-008 has no storage abstraction yet, so there is
  nothing to wire to — define the minimal file layer here.)
- The **operator dashboard "backtest result history" view** (SYS-21 rendering) = SRS-UI-004 / SRS-API-001.
- The **real orchestrated run producer** that stamps a real `RunId` / `CodeVersion` from a live
  Python-authored strategy = **SRS-BT-001-runtime** (the Rust↔Python boundary). BT-009 persists
  caller-supplied provenance; it does not need the Python host.

---

## 4. The decisive scope argument (pre-empt the adversarial reviewer)

**Anticipated "do not ship": "these aren't REAL backtest results — no Python strategy ran."**
Rebuttal, grounded in the AC: SRS-BT-009's requirement and verification method are about **persisting
and querying** completed results, not **producing** them. The producer (a live Python-authored strategy
under the Rust↔Python host) is **SRS-BT-001-runtime**, explicitly deferred on *both* the BT-001 and
`sim_backtest_store_contract` ledgers. A `BacktestResult` produced by `Backtester::run` over a **Rust
fixture `BacktestStrategy` + `VecSource`** is a genuine completed result (real metrics/trade-log/equity
computed by the real engine) — it exercises the persistence+query surface exactly as the AC demands.
Use fixture **market data** + a fixture strategy (Step 2 literally says "fixture market data, provider
mocks"). If a reviewer still insists on the Python host, that is a **scope dispute** — escalate to the
operator rather than absorbing SRS-BT-001-runtime into this commit.

Because BT-009 is genuinely closeable (a real persist→file→query round-trip, not a deferred-runtime
slice), the adversarial loop should **converge** (contrast the EXE-002 non-convergence). Fix any real
in-scope finding; do not let the reviewer expand scope into DATA-008 / BT-001-runtime / the UI.

---

## 5. Phase 1 (Session A) — durable file persistence layer (stays `passes:false`)

A real feat slice that adds the missing file I/O and proves the disk round-trip. Does **not** flip the
flag (the operator workflow + full Step-2 walk is Phase 2).

**Implement (Rust, `crates/atp-simulation/src/backtest_store.rs`):**
- Add filesystem persistence that **wraps the existing codec** — do not invent a new format:
  - `BacktestResultStore::save_to_path(&self, dir: &Path) -> Result<(), StoreError>` — write
    `self.serialize()` to a file under `dir` (e.g. `dir/backtest_results.store`). Create the dir if
    absent; write atomically (write temp + rename) so a crash mid-write can't corrupt the store.
  - `BacktestResultStore::load_from_path(dir: &Path) -> Result<Self, StoreError>` — read the file and
    `Self::restore(&contents)`. A missing file restores an **empty** store (fresh install), but a
    **present-but-corrupt/checksum-mismatch** file FAILS CLOSED (never silently drop persisted runs).
  - Map `std::io::Error` into a new `StoreError::Io { .. }` variant (keep the existing fail-closed
    style). No new external crate (no `serde`); `std::fs` only.
- Keep the SSD/NAS *tiering* out — this is a plain file write to a caller-supplied directory.

**Config (`python/atp_config` + `architecture/runtime_services.json`):**
- Add a `STORAGE_PATHS` key for the backtest-results directory (absolute `PATH`), e.g.
  `storage_paths.backtest_results_dir`, with a sane default and the existing absolute-path validation.
  Mirror an existing `STORAGE_PATHS` key for shape/tests.

**Tests:**
- `crates/atp-simulation/tests/srs_bt_009_persist_query.rs` (L5-style integration, in-crate):
  build a fixture `VecSource` + a fixture `BacktestStrategy`, `Backtester::run` → `BacktestResult` →
  `BacktestRecord::from_result` → `insert` (≥2 records, distinct strategies/params/timestamps) →
  `save_to_path(tmp)` → assert the **file exists on disk** → `load_from_path(tmp)` → assert the loaded
  store **equals** the original and every query axis (`by_strategy`, run/completion window,
  `by_parameter_set`, combined `RecordQuery`) returns the **same records** with all 7 artifacts intact.
  Use `std::env::temp_dir()` + a unique subdir; clean up.
- Unit tests in `backtest_store.rs`: missing-file→empty; corrupt-file→`StoreError`; atomic-write leaves
  no partial file on simulated failure (if feasible).
- Extend `tools/backtest_store_check.py` to pin `save_to_path`/`load_from_path` + the fail-closed
  corrupt-file behavior + the config key. Extend `tests/test_backtest_store_contract.py` with the
  matching positive + `.replace()` negative spot-checks. The check is already wired into `init.sh`
  (`--require-cargo`) and `architecture_check.py`; just extend it (CI mirror stays in sync — see the
  CI-mirror lesson).
- Update the `sim_backtest_store_contract` block: move "durable persisted-record storage" from
  `deferred[]` to in-scope **for the file layer**, leaving the SSD/NAS *tiering* (DATA-008) deferred.

**Commit cadence:** prep (`chore(critic)`: extend `SAFETY_PATH_RE` only if a new safety path is added —
BT-009 is not safety-critical, so likely **no** prep commit needed; verify `backtest_store.rs` /
`/srs_bt_009` aren't already matched) → feat `feat(SRS-BT-009): durable file persistence for the
backtest result store (SYS-21/79)` → chore (record Session A).
**SRS-BT-009 stays `passes:false` after Phase 1.** `feature_list.json` unchanged.

---

## 6. Phase 2 (Session B) — operator workflow + end-to-end close → `passes:true`

**Decision up front — the "CLI/API workflow" (Step 2):** there is **no Python↔Rust runtime bridge**
(Python exercises Rust via text-parsing contract checks). Pick the lightest path that genuinely
demonstrates the operator persist→inspect→query flow, in priority order:
1. **A small Rust binary / example** (`crates/atp-simulation/examples/bt009_store_cli.rs` or a
   `[[bin]]`) — subcommands `persist` (run a fixture backtest, persist to the configured dir) and
   `query --strategy/--from/--to/--param` (load + query + print). This is a real "CLI workflow" in the
   Rust core and writes inspectable files. **Preferred** (no fragile re-implementation of the codec).
2. If an operator-facing **Python** CLI is required: add an `atp_cli` `backtest results` command that
   **reads the persisted file** and surfaces the query. Only do this if the serialized blob format is
   stable enough to parse read-only in Python; do **not** re-implement the write codec in Python.
3. Fallback: argue the Phase-1 Rust integration test + on-disk file inspection **is** the Step-2
   evidence ("CLI/API workflows ... file reads, and persisted output inspection"). Acceptable if 1/2
   are disproportionate, but weaker for "operator workflow".

**End-to-end verification (walk the 4 steps literally, as a human operator would):**
1. `./init.sh` → `Environment ready` (new BT-009 gate green).
2. Run the chosen workflow: persist ≥2 completed fixture-backtest results to the configured directory;
   **inspect the persisted file(s) on disk** (cat/hexdump — confirm the checksummed blob + all 7
   artifacts present); query back by **strategy**, by **date range** (both run and completion windows),
   and by **parameter set**; confirm each query returns the right records with parameters, metrics,
   trade log, equity curve, benchmark comparison, code version, and timestamp intact.
3. Confirm all of Step 3's artifacts/axes from real output (not just unit asserts).
4. Capture the command transcript + file inspection as the objective evidence.

**Flip the flag — only if every step passes with confidence:**
```python
import json
fs = json.load(open('feature_list.json'))
for f in fs:
    if f['id'] == 'SRS-BT-009':
        f['passes'] = True
json.dump(fs, open('feature_list.json','w'), indent=2)
```
(This is the ONLY permitted `feature_list.json` edit: flip `passes` to `true`.)

**Critic (both must approve to commit, per the gate):**
- Pass 1 deterministic `tools/critic_check.py --staged`.
- Pass 2 `/codex:adversarial-review` (USER-triggered, fresh context). Expect **convergence** here
  because the close is real. Hold the scope line from §4: any push toward DATA-008 tiering /
  BT-001-runtime / the UI dashboard is **out of scope** and already deferred with named owners — do not
  absorb it. Fix genuine in-scope findings; record verdicts honestly.

**Commit cadence:** prep (if needed) → feat `feat(SRS-BT-009): operator persist/query workflow; close
SRS-BT-009 end to end` (includes the `feature_list.json` flip) → chore (record Session B; note
SRS-BT-009 now `passes:true`, count drops to 91 failing).

---

## 7. Verification command crib
- `cargo test -p atp-simulation --test srs_bt_009_persist_query` — the disk round-trip.
- `cargo test -p atp-simulation --lib backtest_store` — store unit tests.
- `python3 tools/backtest_store_check.py --require-cargo` — contract evidence.
- `python3 tools/architecture_check.py` (under `.venv` — system python lacks numpy).
- `.venv/bin/python -m pytest tests/test_backtest_store_contract.py -q`.
- `./init.sh` → `Environment ready`.
- Keep new Rust `rustfmt`/`clippy`-clean (format the new file only — do NOT whole-crate `cargo fmt`,
  the workspace rustfmt skew is pre-existing). Keep new Python `ruff`-clean.

## 8. Risks & guardrails
- **#1 risk — verifier demands a live Python strategy.** Mitigation: the §4 scope argument; fixture
  strategy + fixture market data is what Step 2 specifies. Escalate a hard disagreement to the operator.
- **Atomicity** — keep DATA-008 tiering, the UI view, and BT-001-runtime OUT. If a finding can only be
  closed by one of those, it is by definition deferred; name the owner, don't expand the commit.
- **Determinism** — the store is already byte-deterministic; preserve canonical record ordering so the
  on-disk blob is reproducible (this also serves SRS-BT-010 later).
- **No new dependencies** — `std::fs` only; the codec stays zero-dependency.
- **Pre-existing repo health** (not BT-009's to fix): workspace `cargo fmt`/clippy red, the
  `architecture_check.py` ruff I001, the BBands property flake, the orphaned trading-calendar stash.

## 9. Definition of done
`SRS-BT-009.passes == true` in `feature_list.json`, backed by: the durable file round-trip test, the
operator persist/inspect/query transcript, `init.sh` green with the extended gate, deterministic critic
APPROVE, an honestly-recorded judgment verdict, and a `progress.txt` Session B entry. Failing count
drops 92 → 91 — the first feature taken to green from the execution/sim/backtest thread.
