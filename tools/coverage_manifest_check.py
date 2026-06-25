#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-011 corporate-action COVERAGE (the keystone gate).

SRS-DATA-011 (adjust historical price data for corporate actions; SyRS SYS-28a / StRS SN-1.14).
Acceptance: "Splits, reverse splits, dividends, delistings, mergers, and symbol changes are reflected in
historical records so that backtests spanning corporate-action dates produce correct P&L calculations
under the selected normalization mode."

This pins the COVERAGE-MANIFEST keystone as FOUNDATIONAL substrate (NOT a feature close — SRS-DATA-011
STAYS passes:false: only splits / reverse-splits have adjustment math + coverage here; dividends,
delistings, mergers, and symbol changes are deferred, and real provider corporate-action ingestion is
deferred). It lets the (crate-internal) SRS-DATA-012 split-adjustment math finally be served on a public
surface, but ONLY behind proven coverage.

What this pins:
  (a) the coverage kind — store.rs declares the vendor-neutral DatasetKind::CorporateActionCoverage
      (label "corporate-action-coverage", tag 5, min_schema_version 3) and SCHEMA_VERSION == 3, so the
      per-symbol completeness-through-date frontier persists in the SAME idempotent/durable store;
  (b) the gate condition — coverage.rs::query_split_adjusted serves split-adjusted ONLY when the
      symbol's coverage frontier D = max(complete_through) satisfies D >= query.end_ts, else fails
      closed with CoverageError::NotCovered { have_through, need_through } (D >= end_ts, not ==);
  (c) the kind-narrowed gate — the gate requires an equity-bar query kind (DailyEquityBar /
      MinuteEquityBar), so the split math's UnsupportedKind path is unreachable at runtime and a
      split-adjusted series is equity-only by construction;
  (d) the single public entry point — lib.rs exposes `pub mod coverage` (query_split_adjusted) while the
      split math stays crate-internal (`mod normalization`, not re-exported), so the ONLY public path to
      split-adjusted output is the coverage-enforcing gate (no public path to raw-as-adjusted);
  (e) the CLI routing — data007_query_cli routes --normalization split-adjusted through
      query_split_adjusted, echoes coverage_through, and fails closed (naming SRS-DATA-011) when
      uncovered;
  (f) the coverage CLI — data011_coverage_cli records the frontier (assert-coverage --symbol --through
      under the StoreLock) and shows it (show-coverage).

Plus a cargo round-trip (--require-cargo): the coverage gate's crate unit suite, then an end-to-end
ingest (daily bar + split + coverage) proving (1) COVERED -> split-adjusted returns the ADJUSTED bar
(close 2500 / volume 400000 / coverage_through:200), and (2) UNCOVERED (query end beyond the frontier) ->
the CLI FAILS closed naming SRS-DATA-011.

PASS line: ``SRS-DATA-011 CORPORATE-ACTION COVERAGE PASS``.

Invoke:
    python3 tools/coverage_manifest_check.py [--require-cargo]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CoverageManifestCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise CoverageManifestCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "coverage_manifest_contract" not in config:
        fail("architecture metadata is missing coverage_manifest_contract")
    return config["coverage_manifest_contract"]


def _read(config: dict, key: str, root: Path = ROOT) -> str:
    rel = contract_block(config)[key]
    path = root / rel
    if not path.exists():
        fail(f"source missing: {rel}")
    return path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so reformatting cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors (each takes the relevant source so the L3 test
# can inject a regression and prove the guard is non-vacuous).
# --------------------------------------------------------------------------- #


def check_coverage_kind(config: dict, store_src: str) -> str:
    block = contract_block(config)
    label = block["coverage_kind_label"]
    if "CorporateActionCoverage" not in store_src:
        fail(
            "store.rs must declare a vendor-neutral DatasetKind::CorporateActionCoverage so the "
            "per-symbol coverage frontier persists in the same idempotent/durable store as bars"
        )
    if f'"{label}"' not in store_src:
        fail(f"store.rs must map CorporateActionCoverage to the '{label}' label")
    compact = _compact(store_src)
    # The coverage kind is a v3-introduced kind, and SCHEMA_VERSION must be bumped to 3 so a v1/v2
    # reader rejects a coverage-bearing store cleanly at the version gate (not mid-restore on tag 5).
    if "CorporateActionCoverage=>5" not in compact and "CorporateActionCoverage=>5," not in compact:
        fail("store.rs must give DatasetKind::CorporateActionCoverage the codec tag 5")
    if "CorporateActionCoverage=>3" not in compact:
        fail(
            "store.rs must set CorporateActionCoverage::min_schema_version() = 3 so a v1/v2 store "
            "cannot smuggle the coverage kind"
        )
    if f"SCHEMA_VERSION:i64={block['schema_version']}" not in compact:
        fail(f"store.rs SCHEMA_VERSION must be {block['schema_version']} (the coverage kind's version)")
    # The single coverage-record constructor binds event_ts to the through-date (the frontier identity).
    if "fncoverage_record" not in compact:
        fail("store.rs must provide the coverage_record(through, symbol) constructor (single shape)")
    # A coverage record asserts the trust decision the split-adjusted gate reads (the frontier), and
    # MarketDataRecord::new is public — so store validation MUST enforce its self-consistency (a single
    # complete_through field equal to the key event_ts) at upsert AND restore, or a forged frontier could
    # grant coverage its key does not carry. This cannot be left to the coverage_record constructor.
    if "value_minor==record.key.event_ts" not in compact:
        fail(
            "store.rs validate_record must enforce a CorporateActionCoverage record's complete_through "
            "field equals its key event_ts (so a forged frontier via the public MarketDataRecord::new "
            "fails closed before the split-adjusted gate can trust it)"
        )
    # The provider fixture generator must NOT mint coverage (coverage is an operator trust assertion,
    # not provider market data), so a generic ingestion flow iterating dataset kinds cannot create a
    # trusted frontier through the fixture path.
    if "CorporateActionCoverage=>Vec::new()" not in compact:
        fail(
            "store.rs fixture_batch must emit NO records for CorporateActionCoverage "
            "(`CorporateActionCoverage => Vec::new()`) -- coverage is an operator trust assertion, not "
            "provider fixture data"
        )
    return (
        f"coverage kind: store.rs declares DatasetKind::CorporateActionCoverage ('{label}', tag 5, "
        f"min_schema_version 3) and SCHEMA_VERSION {block['schema_version']}; validate_record fails "
        "closed on a coverage record whose complete_through field disagrees with its key event_ts (a "
        "forged frontier), at upsert and restore — the per-symbol frontier reuses the hardened "
        "idempotent/durable store and is trustworthy"
    )


def check_gate_condition(config: dict, coverage_src: str) -> str:
    compact = _compact(coverage_src)
    for token in ("query_split_adjusted", "coverage_frontier", "SplitAdjustedResult", "CoverageError"):
        if token not in coverage_src:
            fail(f"coverage.rs must define `{token}` (the coverage-gate surface)")
    # The gate condition: frontier D >= query.end_ts, else NotCovered carrying have/need.
    if "d>=query.end_ts" not in compact:
        fail(
            "coverage.rs::query_split_adjusted must serve split-adjusted only when the frontier "
            "D >= query.end_ts (the precise, honest coverage condition)"
        )
    if "NotCovered" not in coverage_src or "have_through" not in coverage_src or "need_through" not in coverage_src:
        fail(
            "coverage.rs must fail closed with CoverageError::NotCovered { have_through, need_through } "
            "when the symbol is not covered through the query end"
        )
    # The frontier is the MAX completeness-through over the symbol's coverage records (monotonic).
    if ".max()" not in compact:
        fail("coverage_frontier must be the MAX completeness-through over the symbol's coverage records")
    # The split set is collected up to the coverage frontier D (the as-of date), not the query window:
    # a split in (end, D] still adjusts in-window bars, but a split with effective_ts > D is EXCLUDED so
    # the result is never adjusted past the advertised coverage_through (the as-of-D contract).
    if "CorporateActionSplit" not in coverage_src:
        fail("query_split_adjusted must collect the symbol's CorporateActionSplit records")
    if "event_ts<=coverage_through" not in compact:
        fail(
            "query_split_adjusted must bound the applied splits to effective_ts <= the coverage frontier "
            "(`event_ts <= coverage_through`) -- a split beyond D would adjust the series PAST the "
            "advertised coverage_through (breaking the as-of-D contract)"
        )
    return (
        "gate condition: coverage.rs::query_split_adjusted serves split-adjusted ONLY when the symbol's "
        "frontier D = max(complete_through) satisfies D >= query.end_ts, else fails closed with "
        "NotCovered{have_through,need_through}; the applied split set is bounded to effective_ts <= D "
        "(the as-of-D contract -- a split in (end, D] adjusts in-window bars, a split > D is excluded)"
    )


def check_kind_narrowed_gate(config: dict, coverage_src: str) -> str:
    compact = _compact(coverage_src)
    if "UnsupportedQueryKind" not in coverage_src:
        fail(
            "query_split_adjusted must reject a non-equity / unspecified query kind "
            "(CoverageError::UnsupportedQueryKind) so the split math's UnsupportedKind path is "
            "unreachable at runtime"
        )
    # The gate must require an explicit equity-bar kind before any coverage/math.
    if "DailyEquityBar" not in coverage_src or "MinuteEquityBar" not in coverage_src:
        fail(
            "the gate must require an explicit DailyEquityBar / MinuteEquityBar query kind "
            "(a split-adjusted series is equity-only by construction)"
        )
    if "\"unspecified\"" not in compact and "'unspecified'" not in compact:
        fail("a kind-agnostic (None) split-adjusted query must be rejected as 'unspecified'")
    return (
        "kind-narrowed gate: query_split_adjusted requires an explicit DailyEquityBar / MinuteEquityBar "
        "kind (else UnsupportedQueryKind), so split_adjust_record's UnsupportedKind path is unreachable "
        "and a split-adjusted series is equity-only by construction"
    )


def check_single_public_entry(config: dict, lib_src: str) -> str:
    compact = _compact(lib_src)
    # The coverage gate is the SINGLE public path to split-adjusted output.
    if "pubmodcoverage" not in compact:
        fail("lib.rs must expose the coverage gate (`pub mod coverage;`)")
    # The split math stays crate-internal: `mod normalization` (private), not re-exported. This is what
    # keeps the coverage gate the ONLY public path to split-adjusted output (no raw-as-adjusted).
    if "pubmodnormalization" in compact:
        fail(
            "the split-adjustment math must stay crate-internal (`mod normalization`, NOT `pub mod`) — "
            "the coverage gate must be the only public path to split-adjusted output"
        )
    if "modnormalization;" not in compact:
        fail("lib.rs must declare the crate-internal `mod normalization;`")
    if "pubusecrate::normalization" in compact:
        fail(
            "lib.rs must NOT re-export anything from the crate-internal normalization module "
            "(split_adjust_records / SplitEvent must not be a public crate API)"
        )
    return (
        "single public entry: lib.rs exposes `pub mod coverage` (query_split_adjusted) while the split "
        "math stays crate-internal (`mod normalization`, not re-exported) — so the coverage-enforcing "
        "gate is the ONLY public path to split-adjusted output, with no public path to raw-as-adjusted"
    )


def check_cli_routes_gated(config: dict, cli_src: str) -> str:
    compact = _compact(cli_src)
    if "query_split_adjusted" not in compact:
        fail(
            "data007_query_cli must route --normalization split-adjusted through the coverage gate "
            "MarketDataStore::query_split_adjusted (never CLI-side split math)"
        )
    if '"split-adjusted"=>Ok' not in compact:
        fail("data007_query_cli must ACCEPT --normalization split-adjusted (route it through the gate)")
    if "coverage_through" not in cli_src:
        fail("data007_query_cli must echo a coverage_through:<D> line for a served split-adjusted result")
    # fully-adjusted / total-return remain rejected (dividends deferred); SRS-DATA-011 named.
    if '"fully-adjusted"' not in compact or '"total-return"' not in compact:
        fail("data007_query_cli must still reject --normalization fully-adjusted / total-return")
    if "SRS-DATA-011" not in cli_src:
        fail("data007_query_cli must name SRS-DATA-011 (the coverage owner the split-adjusted gate needs)")
    return (
        "CLI routing: data007_query_cli routes --normalization split-adjusted through the coverage gate "
        "(query_split_adjusted), echoes coverage_through, and fails closed (naming SRS-DATA-011) when "
        "uncovered; fully-adjusted / total-return remain deferred"
    )


def check_coverage_cli(config: dict, coverage_cli_src: str) -> str:
    compact = _compact(coverage_cli_src)
    for token in ('"assert-coverage"', '"show-coverage"', '"--through"', '"--symbol"'):
        if token not in compact:
            fail(f"data011_coverage_cli must declare {token}")
    if "coverage_record" not in compact:
        fail("data011_coverage_cli must build the coverage record via store::coverage_record")
    # assert-coverage must hold the single-writer lock across the load-modify-save (no last-publish-wins).
    if "StoreLock::acquire" not in compact:
        fail("data011_coverage_cli assert-coverage must hold the StoreLock across the load-modify-save")
    if "save_to_path" not in compact:
        fail("data011_coverage_cli assert-coverage must durably persist the coverage record")
    return (
        "coverage CLI: data011_coverage_cli records the frontier (assert-coverage --symbol --through, "
        "under the StoreLock, load-modify-save) and shows it (show-coverage)"
    )


def check_ingest_excludes_coverage(config: dict, ingest_cli_src: str) -> str:
    compact = _compact(ingest_cli_src)
    # The corporate-action COVERAGE frontier is a TRUST assertion the split-adjusted gate reads, so it
    # must have a SINGLE write surface (data011_coverage_cli assert-coverage). The generic market-data
    # ingest CLI (data016) must REFUSE --kind corporate-action-coverage, or its fixture path would be a
    # second, untracked route to grant coverage and enable split-adjusted output.
    if "CorporateActionCoverage" not in ingest_cli_src:
        fail(
            "data016_ingest_cli must explicitly reference DatasetKind::CorporateActionCoverage to "
            "reject it (the coverage frontier is asserted only via data011_coverage_cli)"
        )
    if "kind==DatasetKind::CorporateActionCoverage" not in compact and "data011_coverage_cli" not in ingest_cli_src:
        fail(
            "data016_ingest_cli must REFUSE --kind corporate-action-coverage and point to "
            "data011_coverage_cli (the single coverage-assertion surface)"
        )
    return (
        "single write surface: data016_ingest_cli REFUSES --kind corporate-action-coverage (the "
        "coverage frontier is a trust assertion, asserted only via data011_coverage_cli assert-coverage) "
        "-- there is no second, fixture-shaped route to grant split-adjusted coverage"
    )


def check_data_layer_rejects_coverage(config: dict, lib_src: str) -> str:
    compact = _compact(lib_src)
    # The generic market-data ingestion API (DataLayer::ingest_market_record) is the DECISIVE trust
    # boundary: it must REFUSE a CorporateActionCoverage record so no generic ingest path (even one
    # handed a coverage record directly) can create the trust assertion the split-adjusted gate reads.
    if "record.key().kind==DatasetKind::CorporateActionCoverage" not in compact:
        fail(
            "DataLayer::ingest_market_record must reject a CorporateActionCoverage record (the generic "
            "market-data ingestion path must not create the coverage trust assertion)"
        )
    if "UnsupportedKind" not in lib_src:
        fail("ingest_market_record must fail closed with MarketIngestError::UnsupportedKind for coverage")
    return (
        "data-layer boundary: DataLayer::ingest_market_record REFUSES CorporateActionCoverage "
        "(MarketIngestError::UnsupportedKind) -- the decisive trust boundary so no generic ingestion "
        "path can mint a trusted coverage frontier; coverage is asserted only via data011_coverage_cli"
    )


def check_round_trip(config: dict, require_cargo: bool = False) -> str:
    """Prove the coverage gate end-to-end: the crate unit suite, then COVERED -> adjusted and
    UNCOVERED -> fail-closed over a real ingested store."""
    block = contract_block(config)
    rt = block["round_trip"]
    crate = block["data_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                "cargo not on PATH but --require-cargo set: cannot verify the coverage gate "
                "(install the Rust toolchain)"
            )
        return "round-trip: skipped (cargo not on PATH)"

    # 1) The coverage gate's crate unit suite (gate boundaries, frontier max, idempotent advance, the
    #    kind guard).
    unit = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "coverage", "--quiet"],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if unit.returncode != 0:
        fail(f"cargo test -p {crate} --lib coverage failed:\n{unit.stdout}\n{unit.stderr}")

    # 2) Build the three operator CLIs.
    for binary in (block["ingest_cli_bin"], block["coverage_cli_bin"], block["cli_bin"]):
        built = subprocess.run(
            [cargo, "build", "-q", "-p", crate, "--bin", binary],
            cwd=ROOT, check=False, capture_output=True, text=True,
        )
        if built.returncode != 0:
            fail(f"building {binary} failed:\n{built.stdout}\n{built.stderr}")
    ingest_bin = ROOT / "target" / "debug" / block["ingest_cli_bin"]
    coverage_bin = ROOT / "target" / "debug" / block["coverage_cli_bin"]
    query_bin = ROOT / "target" / "debug" / block["cli_bin"]

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)

    with tempfile.TemporaryDirectory() as tmp:
        # Ingest the daily bar + the split, then assert coverage through the split's effective date.
        if run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", rt["kind"],
               "--event-ts", str(rt["bar_event_ts"]), "--init").returncode != 0:
            fail("ingest daily bar failed")
        if run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", rt["split_kind"],
               "--event-ts", str(rt["split_event_ts"])).returncode != 0:
            fail("ingest split failed")
        asserted = run(str(coverage_bin), "assert-coverage", "--dir", tmp,
                       "--symbol", rt["symbol"], "--through", str(rt["covered_through"]))
        if asserted.returncode != 0:
            fail(f"assert-coverage failed:\n{asserted.stdout}\n{asserted.stderr}")
        if f"frontier:{rt['covered_through']}" not in asserted.stdout:
            fail(f"assert-coverage must report frontier:{rt['covered_through']}, got:\n{asserted.stdout}")

        def query(end: int) -> subprocess.CompletedProcess[str]:
            return run(
                str(query_bin), "query", "--dir", tmp, "--symbol", rt["symbol"],
                "--resolution", rt["resolution"], "--start", "0", "--end", str(end),
                "--kind", rt["kind"], "--normalization", "split-adjusted",
            )

        # COVERED -> the ADJUSTED bar (close 2500 / volume 400000) + coverage_through echo.
        covered = query(rt["covered_query_end"])
        if covered.returncode != 0:
            fail(f"covered split-adjusted query must succeed, got:\n{covered.stdout}\n{covered.stderr}")
        fields = {}
        for line in covered.stdout.splitlines():
            if line.startswith("record.0.field."):
                name, value = line[len("record.0.field."):].split(":", 1)
                fields[name] = int(value)
        if fields.get("close") != rt["adjusted_close_minor"]:
            fail(f"covered split-adjusted close {fields.get('close')} != {rt['adjusted_close_minor']}")
        if fields.get("volume") != rt["adjusted_volume"]:
            fail(f"covered split-adjusted volume {fields.get('volume')} != {rt['adjusted_volume']}")
        if f"coverage_through:{rt['covered_through']}" not in covered.stdout:
            fail(f"covered result must echo coverage_through:{rt['covered_through']}")
        if "normalization:split-adjusted" not in covered.stdout:
            fail("covered result must echo normalization:split-adjusted")

        # UNCOVERED (query end beyond the frontier) -> fail closed naming SRS-DATA-011.
        uncovered = query(rt["uncovered_query_end"])
        if uncovered.returncode == 0:
            fail(
                "split-adjusted query past the coverage frontier must FAIL closed; CLI returned 0 "
                f"with:\n{uncovered.stdout}"
            )
        if "SRS-DATA-011" not in uncovered.stderr:
            fail(f"the uncovered gate failure must name SRS-DATA-011, got:\n{uncovered.stderr}")

    return (
        "round-trip: the coverage gate's crate unit suite passes; an end-to-end ingest (daily bar + "
        f"split + coverage through {rt['covered_through']}) proves COVERED -> split-adjusted returns the "
        f"ADJUSTED bar (close {rt['adjusted_close_minor']} / volume {rt['adjusted_volume']} / "
        f"coverage_through:{rt['covered_through']}), and UNCOVERED (query end {rt['uncovered_query_end']} "
        "> frontier) FAILS closed naming SRS-DATA-011"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# Each static check is paired with the contract-block source key it reads, so the L3 test can load
# exactly that source, mutate it, and prove the guard is non-vacuous.
_STATIC_CHECKS = (
    ("coverage_kind", "store_source", check_coverage_kind),
    ("gate_condition", "coverage_module", check_gate_condition),
    ("kind_narrowed_gate", "coverage_module", check_kind_narrowed_gate),
    ("single_public_entry", "lib_source", check_single_public_entry),
    ("cli_routes_gated", "cli_source", check_cli_routes_gated),
    ("coverage_cli", "coverage_cli_source", check_coverage_cli),
    ("ingest_excludes_coverage", "ingest_cli_source", check_ingest_excludes_coverage),
    ("data_layer_rejects_coverage", "lib_source", check_data_layer_rejects_coverage),
)

_DEFERRED_OWNERS = (
    "dividend / delisting / merger / symbol-change adjustment math and their coverage — only splits / "
    "reverse-splits are handled this session (SRS-DATA-011 remainder)",
    "fully-adjusted / total-return modes and the live-subscription normalization path (SRS-DATA-012 "
    "remainder)",
    "real provider corporate-action ingestion from Databento / IB — the operator-set / fixture frontier "
    "stands in (SRS-DATA-001/003/006)",
    "the StoreBackedHistoricalData SPLIT_ADJUSTED binding flip and wiring the named backtest / factor / "
    "notebook consumers to the gated split-adjusted path (SRS-DATA-007)",
)


def assert_coverage_manifest_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        key: _read(config, key, root)
        for key in {src_key for _, src_key, _ in _STATIC_CHECKS}
    }
    return [check(config, sources[src_key]) for _, src_key, check in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_coverage_manifest_static(config)
    evidence.append(check_round_trip(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SRS-DATA-011 corporate-action coverage contract evidence"
    )
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the end-to-end coverage gate must run.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except CoverageManifestCheckError as error:
        print(f"SRS-DATA-011 CORPORATE-ACTION COVERAGE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-DATA-011 CORPORATE-ACTION COVERAGE PASS")
    for item in evidence:
        print(f"- {item}")
    print("- deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
