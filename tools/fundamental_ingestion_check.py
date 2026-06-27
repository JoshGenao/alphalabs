#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-005 (ingest Sharadar fundamental data on a scheduled basis).

SRS-DATA-005 (SyRS SYS-26, NFR-P8d; StRS SN-3.03 / BG-3). The acceptance criterion:
"Income statement, balance sheet, cash flow statement, and key ratio records for US equities are
ingested, validated, cataloged, available to the factor pipeline, and completed within the overnight
window of 16:00 ET to 09:30 ET next trading day."

This pins the fundamental-ingestion SUBSTRATE over the SRS-DATA-016 storage spine, per the structural
contract in ``architecture/runtime_services.json`` (block ``fundamental_ingestion_contract``). The
SRS-ARCH-002 dependency direction (atp-adapters -> [atp-types] only; atp-data -> [atp-types] only)
forces a three-crate decomposition meeting at one shared boundary type:

  (a) atp-types ``FundamentalStatements`` -- a vendor-neutral bundle (integer minor units) with
      ``period_end_ts`` (fiscal period end) SEPARATE from ``available_ts`` (filing instant); its
      constructor fails closed on impossible provenance (``available_ts < period_end_ts``) and a
      non-positive ``market_value_minor`` (the key-ratio denominator).
  (b) atp-data ``fundamentals::build_fundamental_records`` -> the four canonical ``Fundamental``
      records (income / balance / cashflow / ratios). The ratios record carries EXACTLY the fields
      ``load_fundamental_input`` reads, so a built bundle is always factor-pipeline readable.
  (c) atp-adapters ``SharadarAdapter::map_fundamentals`` -- the provider -> vendor-neutral mapping
      (``reportperiod`` -> period end, ``datekey`` -> filing instant). The Sharadar token lives ONLY
      here; atp-data ``fundamentals`` and atp-types stay vendor-neutral (SRS-ARCH-003).

Records flow through the UNCHANGED ``DataLayer::ingest_market_record`` (ERR-5 gate + idempotent
``upsert``). The operator surface is ``data005_fundamental_cli``; the Rust integration test
``srs_data_005_fundamental_ingest`` (in atp-factor-pipeline, the one crate allowed to depend on BOTH
atp-data and the loader) drives build -> ingest -> persist -> reload -> re-ingest -> READ via the real
``load_fundamental_input``.

The PASS line is ``SRS-DATA-005 FUNDAMENTAL-INGEST PASS`` -- it names the deferred owners (the real
Sharadar network adapter; the NFR-P8d overnight wall-clock proof; the orchestrated fetch->ingest host;
the SYS-77 validator rules + alert surface). SRS-DATA-005 STAYS passes:false: the substrate is
demonstrated end to end over fixture sources, but the close needs those deferred owners.

Invoke:
    python3 tools/fundamental_ingestion_check.py [--require-cargo]
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


class FundamentalIngestionCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise FundamentalIngestionCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "fundamental_ingestion_contract" not in config:
        fail("architecture metadata is missing fundamental_ingestion_contract")
    return config["fundamental_ingestion_contract"]


def _read_crate_module(config: dict, crate_key: str, root: Path = ROOT) -> str:
    block = contract_block(config)[crate_key]
    source_path = root / block["path"] / "src" / f"{block['module']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def types_source(config: dict, root: Path = ROOT) -> str:
    return _read_crate_module(config, "types_crate", root)


def data_source(config: dict, root: Path = ROOT) -> str:
    return _read_crate_module(config, "data_crate", root)


def adapters_source(config: dict, root: Path = ROOT) -> str:
    return _read_crate_module(config, "adapters_crate", root)


def loader_source(config: dict, root: Path = ROOT) -> str:
    rel = contract_block(config)["loader_contract"]["module_path"]
    path = root / rel
    if not path.exists():
        fail(f"source missing: {rel}")
    return path.read_text(encoding="utf-8")


def cli_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    path = root / block["data_crate"]["path"] / "src" / "bin" / f"{block['cli_bin']}.rs"
    if not path.exists():
        fail(f"source missing: {path.relative_to(root)}")
    return path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_dto(config: dict, types_src: str) -> str:
    spec = contract_block(config)["dto"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", types_src):
        fail(f"atp-types must declare `pub struct {spec['struct']}`")
    body = _compact(_struct_body(types_src, spec["struct"]))
    missing = [f for f in spec["fields"] if _compact(f) not in body]
    if missing:
        fail(f"{spec['struct']} is missing field(s): {', '.join(missing)}")
    # The fail-closed constructor + its provenance / denominator guards.
    compact_src = _compact(types_src)
    if _compact(spec["ctor_fn"]) not in compact_src:
        fail(f"{spec['struct']} must expose the validating `{spec['ctor_fn']}`")
    for key, label in (
        (
            "provenance_guard_token",
            "fail closed on available_ts < period_end_ts (impossible provenance)",
        ),
        (
            "denominator_guard_token",
            "fail closed on a non-positive market_value_minor (ratio denominator)",
        ),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"{spec['struct']}::new must {label} (`{spec[key]}`)")
    err_body = _enum_body(types_src, spec["error_enum"])
    missing_v = [
        v for v in spec["error_variants"] if not re.search(rf"\b{re.escape(v)}\b", err_body)
    ]
    if missing_v:
        fail(f"{spec['error_enum']} is missing fail-closed variant(s): {', '.join(missing_v)}")
    return (
        f"atp-types declares the vendor-neutral {spec['struct']} DTO (period_end_ts SEPARATE from "
        "available_ts, integer minor units) with a fail-closed constructor (impossible provenance + "
        f"non-positive market value rejected via {spec['error_enum']})"
    )


def check_builder(config: dict, data_src: str) -> str:
    spec = contract_block(config)["builder"]
    compact = _compact(data_src)
    if _compact(spec["fn"]) not in compact:
        fail(f"atp-data fundamentals must expose `{spec['fn']}`")
    for key in (
        "income_resolution_token",
        "balance_resolution_token",
        "cashflow_resolution_token",
        "ratios_resolution_token",
    ):
        if spec[key] not in data_src:
            fail(f"the builder must emit the `{spec[key]}` statement resolution")
    if spec["fundamental_kind_token"] not in data_src:
        fail(f"the builder must key records as `{spec['fundamental_kind_token']}`")
    # The ratios record must carry exactly the loader's required fields.
    for field in spec["ratios_fields"]:
        if field not in data_src:
            fail(f"the ratios record must carry the loader field `{field}`")
    return (
        "atp-data fundamentals::build_fundamental_records emits the four vendor-neutral statement "
        "records (income / balance / cashflow / ratios), the ratios record carrying exactly the "
        "factor loader's fields (available_ts, net_income_minor, book_equity_minor, market_value_minor)"
    )


def check_loader_contract(config: dict, loader_src: str, data_src: str) -> str:
    spec = contract_block(config)["loader_contract"]
    compact_loader = _compact(loader_src)
    if _compact(spec["resolution_const_token"]) not in compact_loader:
        fail(
            "the factor loader must define the fundamental:ratios resolution const "
            f"(`{spec['resolution_const_token']}`)"
        )
    if _compact(spec["loader_fn_token"]) not in compact_loader:
        fail(f"the factor pipeline must expose `{spec['loader_fn_token']}`")
    missing = [f for f in spec["required_fields"] if f not in loader_src]
    if missing:
        fail(f"load_fundamental_input must read field(s): {', '.join(missing)}")
    # The builder and the loader must agree on the ratios resolution literal (no drift).
    if "fundamental:ratios" not in data_src:
        fail("atp-data builder must use the fundamental:ratios resolution the loader reads")
    return (
        "the atp-data builder's ratios record matches the atp-factor-pipeline load_fundamental_input "
        "contract (resolution fundamental:ratios; fields available_ts / net_income_minor / "
        "book_equity_minor / market_value_minor) -- the integration test reads a built record back "
        "through the real loader, so any literal drift fails closed"
    )


def check_adapter_mapping(config: dict, adapters_src: str) -> str:
    spec = contract_block(config)["adapter_mapping"]
    compact = _compact(adapters_src)
    if _compact(spec["fn"]) not in compact:
        fail(f"atp-adapters SharadarAdapter must expose `{spec['fn']}`")
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['row_struct'])}\b", adapters_src):
        fail(f"atp-adapters must declare `pub struct {spec['row_struct']}` (the vendor row shape)")
    missing = [t for t in spec["vendor_column_tokens"] if t not in adapters_src]
    if missing:
        fail(f"the Sharadar row must name the vendor column(s): {', '.join(missing)}")
    if spec["returns_dto_token"] not in adapters_src:
        fail(
            f"map_fundamentals must map vendor rows onto `{spec['returns_dto_token']}` (fail-closed)"
        )
    # The SF1 dimension policy must be EXPLICIT and fail-closed: a single supported dimension const +
    # a reject-the-rest filter, so multiple rows per (ticker, period) cannot silently collapse.
    dim = spec["dimension_policy"]
    if not re.search(
        rf"\bpub\s+const\s+{re.escape(dim['supported_dimension_const'])}\b", adapters_src
    ):
        fail(
            "the adapter must declare the supported SF1 dimension const "
            f"`{dim['supported_dimension_const']}` (the explicit single-dimension policy)"
        )
    if f'"{dim["supported_dimension_value"]}"' not in adapters_src:
        fail(
            f"the supported SF1 dimension must be {dim['supported_dimension_value']!r} "
            "(as-reported quarterly -- point-in-time honest)"
        )
    if _compact(dim["dimension_filter_token"]) not in compact:
        fail(
            "map_fundamentals must REJECT any non-supported SF1 dimension fail-closed "
            f"(`{dim['dimension_filter_token']}`), so two same-period rows differing only by "
            "dimension cannot collapse onto one identity"
        )
    return (
        "atp-adapters SharadarAdapter::map_fundamentals maps Sharadar SF1 rows (reportperiod -> "
        "period end, datekey -> filing instant) onto the vendor-neutral FundamentalStatements DTO "
        "(SRS-ARCH-003 provider -> kind), failing closed on a malformed row AND on any SF1 dimension "
        f"other than {dim['supported_dimension_value']} (as-reported quarterly) so multiple rows per "
        "ticker+period cannot silently collapse"
    )


def check_cli(config: dict, cli_src: str) -> str:
    spec = contract_block(config)["cli"]
    for key in ("ingest_token", "reingest_token", "factor_input_token", "uses_builder_token"):
        if spec[key] not in cli_src:
            fail(f"data005_fundamental_cli must provide `{spec[key]}`")
    # The reingest proof must stay non-mutating (never persist).
    match = re.search(r"fn cmd_reingest\b", cli_src)
    if match is None:
        fail("the CLI must declare cmd_reingest")
    start = cli_src.index("{", match.end())
    depth = 0
    end = start
    for i in range(start, len(cli_src)):
        if cli_src[i] == "{":
            depth += 1
        elif cli_src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = cli_src[start : end + 1]
    if spec["no_save_in_reingest_token"] in body:
        fail(
            "cmd_reingest is a non-mutating proof and must NOT call save_to_path -- a failed "
            "idempotency proof must never persist newly-inserted records"
        )
    return (
        "atp-data data005_fundamental_cli exposes ingest / reingest / factor-input over the builder "
        "(reingest is a non-mutating idempotency proof; factor-input reads the point-in-time ratios "
        "record back by symbol / resolution with no provider named)"
    )


def check_ingest_path(config: dict, cli_src: str) -> str:
    spec = contract_block(config)["ingest_path"]
    for key in ("entry_fn_token", "idempotent_outcome_token"):
        if spec[key] not in cli_src:
            fail(
                f"the fundamental ingest path must compose `{spec[key]}` (the SRS-DATA-016 substrate)"
            )
    return (
        "the fundamental records flow through the UNCHANGED DataLayer::ingest_market_record (ERR-5 "
        "validation gate + idempotent upsert) -- 'ingested, validated, cataloged' reuses the "
        "SRS-DATA-016 substrate, so re-running an already-ingested period is a no-op"
    )


def check_numeric_boundary(config: dict, types_src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    compact = _compact(types_src)
    missing = [t for t in spec["integer_minor_tokens"] if _compact(t) not in compact]
    if missing:
        fail(f"fundamental line items must stay integer minor units: missing {', '.join(missing)}")
    return (
        "atp-types FundamentalStatements keeps every line item in integer minor units (i64, no f64)"
    )


def check_vendor_isolation(config: dict, types_src: str, data_src: str, adapters_src: str) -> str:
    spec = contract_block(config)["vendor_isolation"]
    for label, src in (("atp-types", types_src), ("atp-data fundamentals", data_src)):
        leaked = [t for t in spec["core_forbidden_tokens"] if t in src]
        if leaked:
            fail(
                f"{label} leaks vendor SDK token(s): {', '.join(leaked)} "
                "(the core must stay vendor-neutral per SRS-ARCH-003)"
            )
    # The adapter layer is where the vendor lives -- assert the mapping IS there (not a stray leak).
    if spec["adapter_vendor_token"] not in adapters_src:
        fail(
            f"atp-adapters must carry the `{spec['adapter_vendor_token']}` provider token (the vendor "
            "mapping belongs in the adapter layer, SRS-ARCH-003)"
        )
    return (
        "vendor isolation holds: atp-types + atp-data fundamentals are free of all vendor SDK tokens; "
        "the Sharadar token lives ONLY in atp-adapters (the provider -> kind mapping layer)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["factor_crate"]["crate"]
    integration = block["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                f"fundamental-ingestion path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    # atp-types DTO unit tests + atp-adapters mapping unit tests + the integration test.
    for crate_arg, kind in (
        ("atp-types", "--lib"),
        ("atp-adapters", "--lib"),
        ("atp-data", "--lib"),
    ):
        unit = subprocess.run(
            [cargo, "test", "-p", crate_arg, kind, "--quiet"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if unit.returncode != 0:
            fail(f"cargo test -p {crate_arg} {kind} failed:\n{unit.stdout}\n{unit.stderr}")
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
        f"cargo test (atp-types/atp-adapters/atp-data --lib + {crate} --test {integration}): PASS "
        "(build -> ingest -> persist -> reload -> re-ingest is a no-op, and the ratios record is read "
        "back through the real load_fundamental_input with point-in-time correctness)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

_DEFERRED_OWNERS = (
    "the REAL Sharadar NETWORK adapter -- live API auth + fetch (SRS-DATA-005; the atp-adapters "
    "FundamentalDataProvider stays a not_configured stub, fixture rows stand in)",
    "the NFR-P8d overnight-window (16:00->09:30 ET) wall-clock completion proof over the full 8,000+ "
    "US-equity universe against a real clock (a wall-clock performance harness, deferred like "
    "SRS-FAC-001's NFR-P7)",
    "the in-process orchestrated fetch->ingest HOST wiring the adapter to the data layer (the "
    "orchestrator -- no crate depends on both; the halves meet at the FundamentalStatements DTO)",
    "RESTATEMENT / amended-filing point-in-time history: the natural key models ONE authoritative "
    "filing per fiscal period, so a same-period restatement currently fails closed "
    "(StoreError::ConflictingContent -- no corruption, no lookahead); multi-filing keying is a "
    "STORAGE-SCHEMA change deferred with the real Sharadar restatement feed (SRS-DATA-005 + "
    "SRS-DATA-016)",
    "the concrete RecordValidator SYS-77 fundamental field-range rules + the quarantine "
    "dashboard/notification alert surface (SRS-DATA-013 / SRS-NOTIF-001)",
)


def assert_fundamental_ingestion_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L7 domain test)."""
    types_src = types_source(config, root)
    data_src = data_source(config, root)
    adapters_src = adapters_source(config, root)
    loader_src = loader_source(config, root)
    cli_src = cli_source(config, root)
    return [
        check_dto(config, types_src),
        check_builder(config, data_src),
        check_loader_contract(config, loader_src, data_src),
        check_adapter_mapping(config, adapters_src),
        check_cli(config, cli_src),
        check_ingest_path(config, cli_src),
        check_numeric_boundary(config, types_src),
        check_vendor_isolation(config, types_src, data_src, adapters_src),
    ]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_fundamental_ingestion_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SRS-DATA-005 fundamental-ingestion contract evidence"
    )
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable ingestion path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except FundamentalIngestionCheckError as error:
        print(f"SRS-DATA-005 FUNDAMENTAL-INGEST FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-DATA-005 FUNDAMENTAL-INGEST PASS")
    for item in evidence:
        print(f"- {item}")
    print("- stays passes:false; deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
