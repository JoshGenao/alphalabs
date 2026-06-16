#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-009 (persist completed backtest results).

SRS-BT-009 (SyRS SYS-21, SYS-79; StRS SN-1.02 / SN-1.04). The acceptance criterion:
"Parameters, metrics, trade log, equity curve, benchmark comparison, strategy code
version, and timestamp are queryable by strategy, date range, and parameter set."

The completed-backtest result persistence + query surface lives in
``crates/atp-simulation`` (module ``backtest_store``), per the structural contract in
``architecture/runtime_services.json`` (block ``sim_backtest_store_contract``). It WRAPS
the producer types the prior slices already build -- it recomputes nothing:

  (a) ``BacktestRecord`` bundles the seven artifacts the acceptance names: the
      ``BacktestRequest`` parameters (the parameter set), the SRS-BT-004
      ``PerformanceMetrics`` family, the ``Vec<Fill>`` trade log, the ``Vec<EquityPoint>``
      equity curve, the SRS-BT-005 ``BenchmarkComparison`` (the record's benchmark
      identity), a ``CodeVersion`` (the strategy code version), and a producer-supplied
      ``completed_at_ts`` (the timestamp). ``RunId`` is the record's identity.
  (b) ``BacktestResultStore`` answers the three SRS-BT-009 query axes -- ``query_by_strategy``,
      ``query_by_date_range`` (over the record's own completion timestamp), and
      ``query_by_parameter_set`` (the full ``BacktestRequest`` fingerprint) -- plus a
      combined ``RecordQuery`` that ANDs them. Records are held in one canonical
      ``(completed_at_ts, run_id)`` order, so every query is deterministic and the
      serialized form is byte-identical for the same record set; ``insert`` rejects a
      duplicate run id (``StoreError::DuplicateRunId``).
  (c) ``serialize``/``restore`` is a deterministic, dependency-free text codec (``MAGIC``
      header + FNV-1a ``checksum`` over the body, length-prefixed strings, enum-tagged cost
      models): ``restore`` validates the magic, the checksum BEFORE building any state, the
      schema version, and every record invariant, building the whole store in a local
      before returning -- so a corrupt or truncated blob (any change that does not also
      recompute the checksum) yields no partially-restored store. The FNV-1a checksum is
      non-cryptographic: it catches ACCIDENTAL corruption, not a deliberate
      checksum-recomputing tamperer (that needs a keyed MAC, out of scope for the
      single-user / local-only baseline) -- the per-record invariant + domain checks are the
      independent defense that still rejects an impossible-valued recomputed blob.
  (d) the numeric boundary: trade-log/equity money stays integer minor units
      (``i128::from`` over the ``i64`` ``*_minor`` fields), while the metric/comparison
      ratios are dimensionless ``f64`` round-tripped EXACTLY via ``f64::to_bits`` /
      ``f64::from_bits`` and verified finite on restore (``is_finite``,
      ``StoreError::NonFiniteRatio``) so a NaN/inf can never re-enter a ranking.
  (e) the work is deterministic (no parallelism / RNG / clock); ``backtest_store`` adds no
      broker/adapter dependency and carries no vendor SDK token; ``lib.rs`` re-exports
      ``pub mod backtest_store;``.

The PASS line is ``SRS-BT-009 SDK-SURFACE PASS`` -- it names the deferred owners (the
SSD/NAS durable tier via SRS-DATA-008, the dashboard/report rendering via SRS-UI-004 /
SRS-API, and the orchestrated run producer / run-snapshot identity via SRS-BT-001) so the
partial-pass status (feature_list.json keeps ``passes:false``) is loud.

Mirrors the PASS/FAIL output style of ``tools/benchmark_check.py``.

Invoke:
    python3 tools/backtest_store_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class BacktestStoreCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise BacktestStoreCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "sim_backtest_store_contract" not in config:
        fail("architecture metadata is missing sim_backtest_store_contract")
    return config["sim_backtest_store_contract"]


def store_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["simulation_crate"]["path"] / "src" / f"{block['backtest_store_module']}.rs"
    )
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["simulation_crate"]["path"] / block["no_broker_dependency"]["cargo_toml"]
    )
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_record_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["record_struct"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"backtest_store must declare `pub struct {spec['struct']}`")
    body = _compact(_struct_body(src, spec["struct"]))
    missing = [f for f in spec["fields"] if _compact(f) not in body]
    if missing:
        fail(
            f"{spec['struct']} must persist every SRS-BT-009 artifact: missing field(s) "
            f"{', '.join(missing)}"
        )
    return (
        f"atp-simulation declares {spec['struct']} bundling the seven SRS-BT-009 artifacts "
        "(BacktestRequest parameters, PerformanceMetrics, trade log, equity curve, "
        "BenchmarkComparison, code version, and completion timestamp) into one queryable record"
    )


def check_identity_newtypes(config: dict, src: str) -> str:
    spec = contract_block(config)["identity_newtypes"]
    for key, label in (
        ("run_id", "the record identity"),
        ("code_version", "the strategy code version"),
    ):
        if not re.search(rf"\bpub\s+struct\s+{re.escape(spec[key])}\b", src):
            fail(f"backtest_store must declare `pub struct {spec[key]}` ({label})")
    # Both newtypes must validate fail-closed (reject an empty label).
    if _compact("return Err(StoreError::InconsistentField") not in _compact(src):
        fail("RunId/CodeVersion must fail closed on an empty label (StoreError::InconsistentField)")
    return (
        f"atp-simulation declares the {spec['run_id']} and {spec['code_version']} identity "
        "newtypes (each fails closed on an empty label), so a persisted result is always "
        "keyed by an identifiable run and code version"
    )


def check_store_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["store_struct"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"backtest_store must declare `pub struct {spec['struct']}`")
    body = _compact(_struct_body(src, spec["struct"]))
    if _compact(spec["records_field"]) not in body:
        fail(f"{spec['struct']} must hold the records (`{spec['records_field']}`)")
    return (
        f"atp-simulation declares {spec['struct']} -- the queryable, persistable collection of "
        "completed-backtest results"
    )


def check_query_fns(config: dict, src: str) -> str:
    spec = contract_block(config)["query_fns"]
    compact_src = _compact(src)
    for key, label in (
        ("by_strategy", "query by strategy"),
        ("by_run_window", "query by the SYS-21 run-window date axis (request.range overlap)"),
        ("by_completion_window", "query by the completion-timestamp axis"),
        ("by_parameter_set", "query by parameter set"),
        ("combined", "a combined AND query"),
    ):
        token = spec[key] + "(" if key == "combined" else spec[key]
        if _compact(token) not in compact_src:
            fail(f"BacktestResultStore must expose {label} (`{spec[key]}`)")
    if _compact(spec["run_window_overlap_token"]) not in compact_src:
        fail(
            "the run-window date axis must use overlap semantics on request.range "
            f"(`{spec['run_window_overlap_token']}`), so a historical run is found by the period "
            "it tested (SYS-21)"
        )
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['query_struct'])}\b", src):
        fail(f"backtest_store must declare the combined `pub struct {spec['query_struct']}`")
    return (
        "atp-simulation BacktestResultStore answers the SRS-BT-009 query axes -- by strategy, by "
        "the SYS-21 date range (query_by_run_window: the backtest's tested period request.range, "
        "overlap semantics, so a 2020 run is found regardless of when it executed), by the "
        "distinct query_by_completion_window, and by parameter set -- plus a combined RecordQuery "
        "that ANDs them"
    )


def check_insert(config: dict, src: str) -> str:
    spec = contract_block(config)["insert"]
    compact_src = _compact(src)
    if _compact(spec["insert_fn"]) not in compact_src:
        fail(f"BacktestResultStore must expose `{spec['insert_fn']}`")
    if _compact(spec["duplicate_guard_token"]) not in compact_src:
        fail(
            f"insert must reject a duplicate run id (`{spec['duplicate_guard_token']}`) so two "
            "results can never share an identity"
        )
    if _compact(spec["canonical_order_token"]) not in compact_src:
        fail(
            f"the store must keep records in a canonical order (`{spec['canonical_order_token']}`) "
            "so every query is deterministic"
        )
    return (
        "atp-simulation BacktestResultStore::insert rejects a duplicate run id "
        "(DuplicateRunId) and inserts in canonical (completed_at_ts, run_id) order, so the "
        "store stays sorted and every query is deterministic"
    )


def check_strategy_parameters(config: dict, src: str) -> str:
    spec = contract_block(config)["strategy_parameters"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"backtest_store must declare the parameter-set type `pub struct {spec['struct']}`")
    compact_src = _compact(src)
    if _compact(spec["from_pairs_fn"]) not in compact_src:
        fail(
            f"{spec['struct']} must build canonically via `{spec['from_pairs_fn']}` (sorted, "
            "unique keys)"
        )
    if _compact(spec["query_field_token"]) not in compact_src:
        fail(
            "query_by_parameter_set must filter on the strategy parameter set "
            f"(`{spec['query_field_token']}`), NOT the launch BacktestRequest -- otherwise two "
            "points of a parameter sweep that share a request are indistinguishable (SYS-21)"
        )
    for key, label in (
        ("empty_key_guard", "reject an empty parameter key"),
        ("duplicate_key_guard", "reject a duplicate parameter key"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"{spec['struct']} must {label} (`{spec[key]}`)")
    return (
        "atp-simulation declares StrategyParameters (the tuned parameter set) built canonically "
        "via from_pairs (sorted, unique non-empty keys); query_by_parameter_set filters on "
        "record.parameters, so two parameter-sweep points that share a BacktestRequest are still "
        "told apart -- the SYS-21 backtest-history axis"
    )


def check_record_coherence(config: dict, src: str) -> str:
    spec = contract_block(config)["record_coherence"]
    compact_src = _compact(src)
    for key, label in (
        ("fill_symbol_guard", "reject a trade-log fill from another symbol"),
        ("fill_window_guard", "bind every trade-log fill to the equity-curve window"),
        ("fill_negative_cost_guard", "reject a negative fill cost component"),
        ("benchmark_identity_guard", "reject a metrics/comparison benchmark-symbol mismatch"),
        ("coefficient_agreement_guard", "reject a metrics/comparison alpha disagreement"),
        ("excess_return_identity_guard", "reject an excess_return that contradicts its own totals"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"validate_record must {label} (`{spec[key]}`)")
    # The metric PRODUCER invariants (metrics::compute) must be enforced on the stored artifacts,
    # so the stored metrics are reproducible from the stored curve/trade-log. Each is pinned by
    # its fail-closed error context.
    missing_contexts = [
        ctx for ctx in spec["producer_invariant_contexts"] if _compact(ctx) not in compact_src
    ]
    if missing_contexts:
        fail(
            "validate_record must enforce the metric producer's invariants on the stored "
            f"artifacts (so the metrics are reproducible): missing guard(s) {missing_contexts}"
        )
    # Per-metric domain bounds (a per-value sanity class, like finiteness): a win_rate / drawdown
    # outside [0, 1] or a negative volatility is impossible regardless of inputs.
    missing_domains = [
        ctx for ctx in spec["metric_domain_contexts"] if _compact(ctx) not in compact_src
    ]
    if missing_domains:
        fail(
            "validate_record must reject out-of-domain metric values (impossible regardless of "
            f"inputs): missing guard(s) {missing_domains}"
        )
    return (
        "atp-simulation validate_record is fail-closed on trade-log coherence (every fill matches "
        "the run symbol + the equity-curve window with a non-negative cost), benchmark-identity "
        "coherence (metrics and comparison, from one BenchmarkReport, identify the same benchmark "
        "and share alpha/beta), the SRS-BT-004 metric-producer INPUT invariants (a non-empty, "
        "strictly-increasing, positive equity curve over positive starting cash and a "
        "non-decreasing trade log), and per-metric DOMAIN bounds (win rate / max drawdown in "
        "[0, 1], non-negative volatility), so the stored artifacts are STRUCTURALLY coherent with "
        "the producer's input contract (full value-level metric reproducibility is the deferred "
        "run-snapshot-identity boundary -- it needs the unpersisted MetricsConfig + benchmark "
        "level series)"
    )


def check_from_result(config: dict, src: str) -> str:
    spec = contract_block(config)["from_result"]
    compact_src = _compact(src)
    if _compact(spec["fn"]) not in compact_src:
        fail(f"backtest_store must expose the safe producer constructor `{spec['fn']}`")
    for key, label in (
        ("data_source_guard", "verify the result's data source matches the request"),
        ("range_guard", "verify the result's window matches the request"),
        ("binds_artifacts_token", "take the trade log + equity curve FROM the BacktestResult"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"from_result must {label} (`{spec[key]}`) so a record cannot be persisted under "
                "false provenance (e.g. a SystemData request with an UploadedData result)"
            )
    return (
        "atp-simulation declares the SAFE producer constructor BacktestRecord::from_result -- it "
        "binds the persisted trade log + equity curve to the authoritative BacktestResult and "
        "verifies the result's data_source + range match the request, so a record can never be "
        "persisted under false provenance (new() remains for the restore reconstruction path)"
    )


def check_codec(config: dict, src: str) -> str:
    spec = contract_block(config)["codec"]
    compact_src = _compact(src)
    for key, label in (
        ("magic_token", "a MAGIC header constant"),
        ("schema_version_token", "a SCHEMA_VERSION constant"),
        ("serialize_fn", "a serialize() entry point"),
        ("restore_fn", "a restore() entry point"),
        ("checksum_fn", "an integrity checksum"),
        ("checksum_first_token", "the checksum verified BEFORE building any state"),
        ("to_bits_token", "exact f64 ratio encoding via to_bits"),
        ("from_bits_token", "exact f64 ratio decoding via from_bits"),
        ("finite_guard_token", "a finite guard on a restored ratio"),
        ("non_finite_error_token", "a fail-closed non-finite-ratio error"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"backtest_store codec must provide {label} (`{spec[key]}`)")
    # Allocation safety: the decode path must NOT pre-allocate from an untrusted decoded count,
    # or a checksum-valid oversized count would abort on an out-of-memory allocation instead of
    # failing closed.
    if spec["forbidden_alloc_token"] in src:
        fail(
            f"backtest_store decode must not pre-allocate from an untrusted count "
            f"(`{spec['forbidden_alloc_token']}`) -- grow the vectors incrementally so an "
            "oversized count fails closed by exhausting the cursor, not an OOM abort"
        )
    # Bulk restore must be O(n log n): decode into a Vec and sort ONCE, not a per-record sorted
    # insert (which re-scans + shifts the Vec on every record -> O(n^2) startup/recovery time).
    if _compact(spec["bulk_restore_sort_token"]) not in compact_src:
        fail(
            f"restore must canonicalize with a single sort (`{spec['bulk_restore_sort_token']}`) "
            "rather than a per-record sorted insert, so restoring a large history is O(n log n)"
        )
    return (
        "atp-simulation serialize/restore is a deterministic, dependency-free text codec "
        "(MAGIC + integrity checksum verified before any state is built, length-prefixed "
        "strings, exact f64 ratios via to_bits/from_bits) that round-trips the store, fails "
        "closed on a corrupt / truncated / non-recomputed-checksum / non-finite blob (the FNV-1a "
        "checksum catches ACCIDENTAL corruption -- a deliberate checksum-recomputing tamperer "
        "needs a keyed MAC, out of scope for single-user/local), and never pre-allocates from an "
        "untrusted count (a checksum-valid oversized count fails closed, not an OOM abort)"
    )


def check_error_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["error_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed variants "
        f"({', '.join(spec['variants'])})"
    )


def check_file_persistence(config: dict, src: str) -> str:
    spec = contract_block(config)["file_persistence"]
    compact_src = _compact(src)
    for key, label in (
        ("save_fn", "a save_to_path() entry point"),
        ("load_fn", "a load_from_path() entry point"),
        ("store_filename_const", "a STORE_FILENAME constant"),
        ("tmp_filename_const", "a STORE_TMP_FILENAME scratch-file constant"),
        (
            "unique_scratch_token",
            "a per-call unique scratch name (so concurrent writers cannot clobber the scratch)",
        ),
        (
            "file_sync_token",
            "an fsync of the scratch file BEFORE the rename (crash durability of the bytes)",
        ),
        ("atomic_rename_token", "an atomic temp+rename publish (write scratch, then rename)"),
        (
            "dir_sync_token",
            "an fsync of the parent directory AFTER the rename (crash durability of the publish)",
        ),
        (
            "restore_on_load_token",
            "load delegating a present file to the fail-closed restore() codec",
        ),
        (
            "missing_dir_failclosed_token",
            "a missing configured directory failing closed (not masquerading as empty history)",
        ),
        ("missing_file_empty_token", "a missing file restoring an empty store (not an error)"),
        ("io_error_variant", "a fail-closed StoreError::Io for a real I/O failure"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"backtest_store durable persistence must provide {label} (`{spec[key]}`)")
    # The persisted directory must be a catalogued, validated storage_paths PATH key so the
    # operator workflow persists to an absolute, readiness-checked location -- not a hard-coded
    # path. (The SSD/NAS TIERING of that directory remains the deferred SRS-DATA-008 owner.)
    config_key = spec["config_key"]
    keys = config.get("configuration", {}).get("keys", [])
    match = next((entry for entry in keys if entry.get("name") == config_key), None)
    if match is None:
        fail(
            f"durable backtest store needs the {config_key} configuration key catalogued "
            "(architecture/runtime_services.json configuration.keys)"
        )
    if match.get("category") != "storage_paths" or match.get("type") != "path":
        fail(
            f"{config_key} must be a storage_paths path key (got "
            f"category={match.get('category')!r}, type={match.get('type')!r})"
        )
    return (
        "atp-simulation save_to_path / load_from_path durably persist the store to the "
        f"{config_key} directory via a crash-durable write (unique scratch file, fsync, atomic "
        "rename, parent-directory fsync) that wraps the codec, fail closed through restore() on a "
        "corrupt file and through StoreError::Io on a missing/unmounted directory, and restore an "
        "empty store only when the provisioned directory holds no file (a persisted run is never "
        "silently dropped or left half-written; only multi-writer merge coordination + SSD/NAS "
        "tiering are deferred to SRS-DATA-008)"
    )


def check_numeric_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    compact_src = _compact(src)
    missing_money = [t for t in spec["integer_minor_tokens"] if _compact(t) not in compact_src]
    if missing_money:
        fail(
            f"trade-log/equity money must stay integer minor units: missing "
            f"{', '.join(missing_money)}"
        )
    missing_ratio = [t for t in spec["ratio_exact_tokens"] if _compact(t) not in compact_src]
    if missing_ratio:
        fail(
            f"metric/comparison ratios must round-trip EXACTLY via to_bits/from_bits: missing "
            f"{', '.join(missing_ratio)}"
        )
    return (
        "atp-simulation keeps trade-log/equity money in integer minor units (i128::from over the "
        "i64 *_minor fields) and round-trips the dimensionless f64 ratios exactly via "
        "to_bits/from_bits -- the f64 is the metric domain, not a money leak"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    leaked = [t for t in spec["forbidden_tokens"] if t in src]
    if leaked:
        fail(
            f"backtest_store must be deterministic (SRS-BT-010): found nondeterminism source(s) "
            f"{', '.join(leaked)} -- persistence/query must use fixed folds with no parallelism, "
            "RNG, or wall-clock read"
        )
    return (
        "atp-simulation backtest_store is deterministic: no parallelism / RNG / clock token, so a "
        "query answers identically and the serialized form is byte-identical for the same record "
        "set (SRS-BT-010)"
    )


def check_metrics_reuse(config: dict, src: str) -> str:
    spec = contract_block(config)["metrics_reuse"]
    compact_src = _compact(src)
    for key, label in (
        ("import_token", "the SRS-BT-004 PerformanceMetrics family"),
        ("comparison_import_token", "the SRS-BT-005 BenchmarkComparison"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"backtest_store must persist {label} (`{spec[key]}`) rather than re-declare a "
                "divergent metric/comparison shape"
            )
    return (
        "atp-simulation backtest_store persists the SRS-BT-004 PerformanceMetrics family and the "
        "SRS-BT-005 BenchmarkComparison directly (no re-declared divergent shape)"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so the "
            "backtest-record surface is part of the simulation engine"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the live/broker path: found "
            f"{', '.join(leaked)} -- the persisted backtest record must be independent of the "
            "IB account"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the live/broker path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- the backtest-record surface is "
        "broker-independent"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-simulation backtest_store module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation backtest_store module is free of all {len(tokens)} forbidden vendor SDK "
        "tokens (SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["simulation_crate"]["crate"]
    integration = block["rust_integration_test"]
    module = block["backtest_store_module"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "backtest-record path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", module, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib {module} failed:\n{lib.stdout}\n{lib.stderr}")
    integ = subprocess.run(
        [cargo, "test", "-p", crate, "--test", integration, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(f"cargo test -p {crate} --test {integration} failed:\n{integ.stdout}\n{integ.stderr}")
    return (
        f"cargo test -p {crate} --lib {module} + {integration}: PASS "
        "(bundles + persists the seven artifacts, queries by strategy / date range / parameter "
        "set, round-trips the store deterministically, and fails closed on a corrupt / "
        "duplicate-run-id / non-finite blob)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "store" reads backtest_store.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("record_struct", check_record_struct, "store"),
    ("identity_newtypes", check_identity_newtypes, "store"),
    ("from_result", check_from_result, "store"),
    ("store_struct", check_store_struct, "store"),
    ("query_fns", check_query_fns, "store"),
    ("strategy_parameters", check_strategy_parameters, "store"),
    ("insert", check_insert, "store"),
    ("record_coherence", check_record_coherence, "store"),
    ("codec", check_codec, "store"),
    ("error_enum", check_error_enum, "store"),
    ("file_persistence", check_file_persistence, "store"),
    ("numeric_boundary", check_numeric_boundary, "store"),
    ("determinism", check_determinism, "store"),
    ("metrics_reuse", check_metrics_reuse, "store"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "store"),
)

_DEFERRED_OWNERS = (
    "SSD/NAS tiering, eviction, and failover of the persisted store directory (SRS-DATA-008) -- "
    "the durable file write itself is shipped (save_to_path / load_from_path)",
    "dashboard / backtest report history rendering (SRS-UI-004 / SRS-API; SYS-21)",
    "operator persist/query workflow + the end-to-end step walk that flips passes:true (Phase 2)",
    "orchestrated run producer / real provenance (SRS-BT-001 / orchestrator)",
    "full value-level metric verification + atomic run-snapshot identity (needs the unpersisted "
    "MetricsConfig + benchmark level series; metric correctness owned by SRS-BT-004, the run "
    "snapshot by the deferred accumulator)",
)


def assert_backtest_store_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "store": store_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_backtest_store_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-009 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable persistence path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except BacktestStoreCheckError as error:
        print(f"SRS-BT-009 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-009 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-BT-009 passes:false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
