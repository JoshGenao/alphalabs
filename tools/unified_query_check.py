#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-007 (provide a unified historical data access interface).

SRS-DATA-007 (SyRS SYS-27 / SYS-53; StRS SN-1.28 / SN-3.03 / BG-5). The acceptance criterion:
"Strategy code, backtests, factor jobs, and notebooks query by symbol, date range, and resolution
WITHOUT specifying the original source provider."

This is the runnable READ path over the SRS-DATA-016 storage substrate (the canonical
``MarketDataStore`` of vendor-neutral records the data layer persists). It lives in
``crates/atp-data`` (module ``query``), per the structural contract in
``architecture/runtime_services.json`` (block ``unified_query_runtime_contract``). It is DISTINCT from
the ``unified_historical_data`` (API-7) block, which pins the provider-facing adapter *trait* shape in
``atp-adapters``; this block pins the runtime query that actually serves records from the store:

  (a) ``UnifiedHistoricalQuery`` carries ONLY the three acceptance dimensions (symbol, resolution, an
      inclusive ``[start_ts, end_ts]`` event-timestamp range) plus an optional ``DatasetKind``
      disambiguator. There is NO provider / vendor / source / feed field -- a consumer cannot pass one.
  (b) ``UnifiedHistoricalResult`` echoes the queried symbol + resolution and the matched records, with
      NO provider / vendor / source field -- the envelope cannot name a record's origin.
  (c) ``MarketDataStore::query_unified`` filters ``self.records()`` (the store's canonical natural-key
      order) on exactly the vendor-neutral ``NaturalKey`` dimensions -- symbol equality, resolution
      equality, the inclusive ``event_ts`` range, and the optional kind -- returning the survivors in
      deterministic ``event_ts``-ascending order WITHOUT a re-sort, and an empty match as a RETURNED
      value (not a ``Result`` error).
  (d) the ``data007_query_cli`` operator binary loads the atomically-published snapshot read-only (NO
      single-writer ``StoreLock`` -- a read does not need it; concurrent-read-during-write is the
      deferred SRS-DATA-017) and prints a source-neutral ``key:value`` report with no provider line.
  (e) ``query`` carries no broker/adapter dependency and no lowercase vendor SDK token; ``lib.rs``
      re-exports ``pub mod query;``.

The PASS line is ``SRS-DATA-007 UNIFIED-QUERY PASS``. This check is contract EVIDENCE that the query
substrate is correctly built and stays a regression gate. SRS-DATA-007 STAYS passes:false (foundational
substrate). The in-process consumer binding (the Python ``StoreBackedHistoricalData``; see
``tools/store_history_check.py`` + ``store_history_binding_contract``) reads this engine by
symbol/date-range/resolution with no provider named, serving RAW verbatim AND the gated SPLIT_ADJUSTED
(the HistoricalData Protocol default) through the SRS-DATA-011 coverage gate (an uncovered query fails
closed with CoverageNotProvenError naming SRS-DATA-011, never raw-as-adjusted); ``get_bars_range`` is the
explicit ``[start, end]`` range query backtests / factor jobs / notebooks use. The close is NOT complete: the
BACKTEST consumer is now genuinely wired (atp_simulation::store_bar_source::StoreBarSource consumes this
store path in BacktestEngine::run); the FACTOR-JOB consumer now READS the store
(atp_factor_pipeline::store_inputs loaders + assemble_factor_inputs, a point-in-time read primitive;
run_scheduled_factor_job_over_store DERIVES its data as-of from the calendar's session_as_of_ts for the
scheduled session, so a caller cannot pair a session with a future as-of -- only the concrete real-calendar
mapping is deferred, see SRS-FAC-001); and strategy + notebook/research code read via the binding -- but the Jupyter
notebook HOST is SRS-RES-002 (the remaining unwired consumer), so SRS-DATA-007 STAYS passes:false. Other owners
that compose this read path also remain deferred (the real provider network
adapters via SRS-DATA-001/003/005/006, read-while-write via SRS-DATA-017, the FULLY_ADJUSTED /
TOTAL_RETURN + live-subscription normalization of SRS-DATA-012, SSD/NAS tiering via SRS-DATA-008/009/010,
the dashboard/REST surfaces via SRS-UI / SRS-API).

Mirrors the PASS/FAIL output style of ``tools/ingestion_idempotency_check.py``.

Invoke:
    python3 tools/unified_query_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _struct_body

ROOT = Path(__file__).resolve().parents[1]


class UnifiedQueryCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise UnifiedQueryCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "unified_query_runtime_contract" not in config:
        fail("architecture metadata is missing unified_query_runtime_contract")
    return config["unified_query_runtime_contract"]


def query_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["data_crate"]["path"] / "src" / f"{block['query_module']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["data_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["data_crate"]["path"] / "Cargo.toml"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cli_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["data_crate"]["path"] / "src" / "bin" / f"{block['cli_bin']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_query_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["query_struct"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"query module must declare `pub struct {spec['struct']}`")
    body = _compact(_struct_body(src, spec["struct"]))
    missing = [f for f in spec["fields"] if _compact(f) not in body]
    if missing:
        fail(
            f"{spec['struct']} must carry the three acceptance query dimensions: missing "
            f"{', '.join(missing)}"
        )
    return (
        f"atp-data declares {spec['struct']} carrying the acceptance query dimensions "
        "(symbol, resolution, an inclusive [start_ts, end_ts] range) + an optional vendor-neutral "
        "DatasetKind disambiguator"
    )


def check_query_struct_no_provider(config: dict, src: str) -> str:
    spec = contract_block(config)["query_struct"]
    body = _struct_body(src, spec["struct"]).lower()
    leaked = [t for t in spec["forbidden_provider_tokens"] if t.lower() in body]
    if leaked:
        fail(
            f"{spec['struct']} must NOT carry an origin-provider field/token: found "
            f"{', '.join(leaked)} -- the whole point of SRS-DATA-007 is to query 'without specifying "
            "the original source provider'"
        )
    return (
        f"atp-data {spec['struct']} names NO origin provider (none of "
        f"{', '.join(spec['forbidden_provider_tokens'])}) -- a consumer cannot specify a source"
    )


def check_result_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["result_struct"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"query module must declare `pub struct {spec['struct']}`")
    body = _compact(_struct_body(src, spec["struct"]))
    missing = [f for f in spec["fields"] if _compact(f"{f}:") not in body and _compact(f) not in body]
    if missing:
        fail(f"{spec['struct']} must expose {', '.join(missing)}")
    return (
        f"atp-data declares the source-neutral {spec['struct']} (echoes symbol + resolution + the "
        "matched records, borrowed in canonical event_ts-ascending order)"
    )


def check_result_source_neutral(config: dict, src: str) -> str:
    spec = contract_block(config)["result_struct"]
    body = _struct_body(src, spec["struct"]).lower()
    leaked = [t for t in spec["forbidden_provider_tokens"] if t.lower() in body]
    if leaked:
        fail(
            f"{spec['struct']} must be source-neutral: found origin token(s) {', '.join(leaked)} -- "
            "the result envelope must not name where a record came from"
        )
    return (
        f"atp-data {spec['struct']} is source-neutral (none of "
        f"{', '.join(spec['forbidden_provider_tokens'])}) -- a consumer cannot branch on origin"
    )


def check_query_method(config: dict, src: str) -> str:
    spec = contract_block(config)["query_method"]
    compact = _compact(src)
    if _compact(spec["fn"]) not in compact:
        fail(f"MarketDataStore must expose the unified query `{spec['fn']}`")
    if _compact(spec["returns_token"]) not in compact:
        fail(
            f"query_unified must return the source-neutral envelope (`{spec['returns_token']}`)"
        )
    # Empty match is a RETURNED value, never a Result error.
    if _compact(spec["no_result_token"]) in compact:
        fail(
            f"query_unified must return a value, not a Result (`{spec['no_result_token']}` found) -- "
            "an empty match is a valid empty result, never an error"
        )
    return (
        "atp-data MarketDataStore::query_unified returns the source-neutral "
        "UnifiedHistoricalResult (a value -- an empty match is a valid empty result, never an error)"
    )


def check_query_filter_dimensions(config: dict, src: str) -> str:
    spec = contract_block(config)["query_method"]
    compact = _compact(src)
    # The query filters the store's canonical order, then sorts the survivors explicitly by event_ts
    # (the store's canonical order sorts by kind BEFORE event_ts, so a kind-agnostic cross-kind match
    # is not event_ts-ascending in store order -- the explicit sort honors the date-range contract).
    if _compact(spec["canonical_source_token"]) not in compact:
        fail(
            f"query_unified must filter the store's canonical order (`{spec['canonical_source_token']}`)"
        )
    for key, label in (
        ("event_ts_sort_token", "sort the matched records"),
        ("event_ts_order_token", "order PRIMARILY by event_ts ascending (the date-range contract)"),
    ):
        if _compact(spec[key]) not in compact:
            fail(
                f"query_unified must {label} (`{spec[key]}`) -- the store sorts by kind before "
                "event_ts, so a kind-agnostic cross-kind match needs the explicit event_ts sort to be "
                "deterministically event_ts-ascending"
            )
    for key, label in (
        ("symbol_filter_token", "match the symbol exactly"),
        ("resolution_filter_token", "match the resolution exactly"),
        ("range_lower_token", "honor the inclusive lower range bound"),
        ("range_upper_token", "honor the inclusive upper range bound"),
        ("kind_filter_token", "honor the optional vendor-neutral kind disambiguator"),
    ):
        if _compact(spec[key]) not in compact:
            fail(f"the query predicate must {label} (`{spec[key]}`)")
    return (
        "atp-data query predicate covers exactly the three acceptance dimensions -- exact symbol, "
        "exact resolution, the inclusive [start, end] event_ts range -- plus the optional kind, "
        "reading only vendor-neutral NaturalKey fields, then sorts the survivors explicitly into "
        "deterministic event_ts-ascending order (the store sorts by kind before event_ts)"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(f"atp-data lib.rs must re-export `{spec['lib_reexport_token']}`")
    return f"atp-data lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_cli_registered(config: dict, cli_src: str) -> str:
    spec = contract_block(config)["cli"]
    if re.search(rf"\bfn\s+{re.escape(spec['cmd_fn'].split()[-1])}\b", cli_src) is None:
        fail(f"the operator CLI must declare `{spec['cmd_fn']}` (the unified query command)")
    compact = _compact(cli_src)
    if spec["dir_env_token"] not in cli_src:
        fail(
            f"the CLI must resolve the store dir from the {spec['dir_env_token']} config key "
            "(fail-closed, no silently-empty catalog)"
        )
    missing = [f for f in spec["flag_tokens"] if _compact(f) not in compact]
    if missing:
        fail(f"the CLI must parse the query flags: missing {', '.join(missing)}")
    return (
        f"atp-data {contract_block(config)['cli_bin']} exposes `{spec['cmd_fn']}` parsing "
        f"{', '.join(spec['flag_tokens'])} over the {spec['dir_env_token']} store directory"
    )


def check_cli_source_neutral(config: dict, cli_src: str) -> str:
    spec = contract_block(config)["cli"]
    leaked = [t for t in spec["forbidden_output_tokens"] if t in cli_src]
    if leaked:
        fail(
            f"the CLI output must be source-neutral: found origin line token(s) {', '.join(leaked)} -- "
            "the operator report must not print the provider a record came from"
        )
    missing = [t for t in spec["header_tokens"] if t not in cli_src]
    if missing:
        fail(f"the CLI must print the source-neutral header tokens: missing {', '.join(missing)}")
    return (
        "atp-data data007_query_cli prints a source-neutral report (symbol / resolution / "
        "match_count + each record's event_ts + integer-minor fields) with no provider/source/vendor "
        "line"
    )


def check_cli_no_writer_lock(config: dict, cli_src: str) -> str:
    spec = contract_block(config)["cli"]
    if _compact(spec["no_writer_lock_token"]) in _compact(cli_src):
        fail(
            f"the query CLI is a READ and must NOT acquire the single-writer lock "
            f"(`{spec['no_writer_lock_token']}` found) -- it loads the atomically-published snapshot; "
            "read-while-write coordination is the deferred SRS-DATA-017"
        )
    return (
        "atp-data data007_query_cli is a read-only snapshot load -- it takes no single-writer "
        "StoreLock (read-while-write coordination is the deferred SRS-DATA-017)"
    )


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-data Cargo.toml must NOT depend on the broker/execution path: found "
            f"{', '.join(leaked)} -- the unified query is broker-independent"
        )
    return (
        f"atp-data Cargo.toml declares no dependency on the broker/execution path "
        f"({', '.join(spec['forbidden_dep_tokens'])})"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-data query path leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the unified query is vendor-neutral per SRS-ARCH-003)"
        )
    return (
        f"atp-data query path is free of all {len(tokens)} forbidden vendor SDK tokens "
        "(the dataset kinds are vendor-neutral; SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["data_crate"]["crate"]
    integration = block["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "unified-query path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib failed:\n{lib.stdout}\n{lib.stderr}")
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
        f"cargo test -p {crate} --lib + {integration}: PASS "
        "(ingest -> persist -> reload -> query filters by symbol + resolution + inclusive range, one "
        "path serves every provider kind, the optional kind narrows, the result is deterministic "
        "across a persisted reload, and an empty match is a value not an error)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "query" reads query.rs, "lib" reads lib.rs, "cargo" reads
# Cargo.toml, "cli" reads the operator CLI.
_STATIC_CHECKS = (
    ("query_struct", check_query_struct, "query"),
    ("query_struct_no_provider", check_query_struct_no_provider, "query"),
    ("result_struct", check_result_struct, "query"),
    ("result_source_neutral", check_result_source_neutral, "query"),
    ("query_method", check_query_method, "query"),
    ("query_filter_dimensions", check_query_filter_dimensions, "query"),
    ("module_reexport", check_module_reexport, "lib"),
    ("cli_registered", check_cli_registered, "cli"),
    ("cli_source_neutral", check_cli_source_neutral, "cli"),
    ("cli_no_writer_lock", check_cli_no_writer_lock, "cli"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "query"),
)

_DEFERRED_OWNERS = (
    "real Databento/IB/Sharadar/option-chain NETWORK adapters that materialize records "
    "(SRS-DATA-001/003/005/006); fixture sources stand in, as the verification step permits",
    "concurrent READS during an active ingestion WRITE -- read-while-write coordination "
    "(SRS-DATA-017; the atomic whole-file publish is the groundwork)",
    "FULLY_ADJUSTED / TOTAL_RETURN + live-subscription normalization (dividend data, SRS-DATA-012); "
    "split-adjusted is now served through the SRS-DATA-011 coverage gate by both the operator CLI and "
    "the StoreBackedHistoricalData consumer binding",
    "the Jupyter notebook HOST (SRS-RES-002) -- the BACKTEST consumer (atp-simulation StoreBarSource) is "
    "genuinely wired and the FACTOR-JOB consumer now READS the store (atp-factor-pipeline store_inputs loaders "
    "+ assemble_factor_inputs, a point-in-time read primitive; the as-of -> scheduled-session binding is the "
    "deferred calendar boundary, see SRS-FAC-001), and strategy + notebook read via the binding, so the "
    "notebook HOST is the remaining gap that keeps SRS-DATA-007 passes:false",
    "SSD-primary / NAS-archival tiering, eviction, cold-read failover of the queried directory "
    "(SRS-DATA-008/009/010)",
    "the dashboard / REST consumer surfaces (SRS-UI / SRS-API)",
)


def assert_unified_query_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "query": query_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
        "cli": cli_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_unified_query_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-DATA-007 unified historical query contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable query path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except UnifiedQueryCheckError as error:
        print(f"SRS-DATA-007 UNIFIED-QUERY FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-DATA-007 UNIFIED-QUERY PASS")
    for item in evidence:
        print(f"- {item}")
    print("- deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
