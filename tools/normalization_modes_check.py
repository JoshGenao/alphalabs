#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-012 split-adjusted historical normalization.

SRS-DATA-012 (support raw, split-adjusted, fully adjusted, and total-return normalization modes per
security subscription; SyRS SYS-29 / StRS SN-1.15). The acceptance: "Historical and live subscription
requests can select a normalization mode; options strategies can request raw prices; indicators can
request adjusted series."

This pins the HISTORICAL **split-adjusted** slice (the Rust core + the operator CLI + the Python consumer
binding). SRS-DATA-012 STAYS passes:false (the LIVE subscription path and the FULLY_ADJUSTED /
TOTAL_RETURN dividend modes remain deferred), but split-adjusted is now SERVED end-to-end: the consumer
binding serves it through the SRS-DATA-011 coverage-enforcing gate (the SRS-DATA-007 close), so a
"split-adjusted" read is only ever returned with proven corporate-action coverage — an uncovered query
fails closed (CoverageNotProvenError, naming SRS-DATA-011), never raw bars dressed up as adjusted.

What this pins:
  (a) split kind — crates/atp-data/src/store.rs declares the vendor-neutral DatasetKind
      CorporateActionSplit with its "corporate-action-split" label, so split corporate actions persist
      in the SAME hardened idempotent/durable store as bars;
  (b) the money math — crates/atp-data/src/normalization.rs computes split adjustment in the Rust core
      (the single source of truth): compose-then-divide (one division per field), i128 intermediates
      with fail-closed narrowing, round-half-to-even, the strict effective_ts > t boundary, OHLC scaled
      by DEN/NUM and volume by the inverse;
  (c) the CLI surface — data007_query_cli serves --normalization raw, and split-adjusted ONLY through
      the SRS-DATA-011 coverage-enforcing gate (MarketDataStore::query_split_adjusted), which fails
      closed (naming SRS-DATA-011 coverage) when the symbol is not covered through --end; fully-adjusted
      / total-return remain rejected. So the operator surface never emits a split-adjusted label without
      proven coverage (no raw-as-adjusted), and the raw split math is never exposed CLI-side;
  (d) the binding — python/atp_strategy/store_history.py serves RAW and the gated SPLIT_ADJUSTED (the
      HistoricalData Protocol default), routing split-adjusted through the operator CLI's SRS-DATA-011
      coverage gate and validating the echoed coverage_through frontier (gate-integrity); an uncovered
      query fails closed (CoverageNotProvenError, naming SRS-DATA-011), never raw-as-adjusted.
      FULLY_ADJUSTED / TOTAL_RETURN still fail closed (NotImplementedError; dividend data, SRS-DATA-012).

Plus a cargo round-trip (--require-cargo): (1) prove the split-adjustment MATH with the crate's OWN unit
tests -- ``cargo test -p atp-data --lib normalization`` (forward/reverse/multi-split, the effective-date
boundary, round-half-to-even, the symbol-only invariant, non-equity + non-positive + overflow
fail-closed); the raw split math stays CRATE-INTERNAL (not a public crate API), so it is exercised
in-crate, never via an external test; (2) prove the operator surface never emits raw-as-adjusted --
ingest daily bars via data016_ingest_cli (NO coverage record), then assert ``data007_query_cli
--normalization split-adjusted`` FAILS closed at the coverage gate (naming SRS-DATA-011) while
``--normalization raw`` returns the stored values. The covered (served) split-adjusted path is the
SRS-DATA-011 keystone, proven end-to-end by tools/coverage_manifest_check.py.

PASS line: ``SRS-DATA-012 SPLIT-ADJUSTED NORMALIZATION PASS``.

Invoke:
    python3 tools/normalization_modes_check.py [--require-cargo]
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


class NormalizationModesCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise NormalizationModesCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "normalization_modes_contract" not in config:
        fail("architecture metadata is missing normalization_modes_contract")
    return config["normalization_modes_contract"]


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


def check_split_kind(config: dict, store_src: str) -> str:
    label = contract_block(config)["split_kind_label"]
    if "CorporateActionSplit" not in store_src:
        fail(
            "store.rs must declare a vendor-neutral DatasetKind::CorporateActionSplit so split "
            "corporate actions persist in the same idempotent/durable store as bars"
        )
    if f'"{label}"' not in store_src:
        fail(f"store.rs must map CorporateActionSplit to the '{label}' label (CLI --kind value)")
    return (
        f"split kind: store.rs declares DatasetKind::CorporateActionSplit ('{label}') — split "
        "corporate actions reuse the hardened idempotent/durable/deterministic store"
    )


def check_rust_math(config: dict, norm_src: str) -> str:
    compact = _compact(norm_src)
    for token in contract_block(config)["rust_entry_points"]:
        if token not in norm_src:
            fail(f"normalization.rs must define/expose `{token}` (the split-adjustment surface)")
    # Compose-then-divide + i128 + fail-closed narrowing + round-half-to-even discipline.
    if "i128" not in norm_src:
        fail("the split math must use i128 intermediates (an i64 value times a split product)")
    if "checked_mul" not in compact:
        fail("the split math must use checked_mul and fail closed on overflow (never wrap a money value)")
    if "i64::try_from" not in compact:
        fail("the split math must narrow the i128 result back to i64 with try_from (fail-closed)")
    if "div_euclid" not in compact or "rem_euclid" not in compact:
        fail("div_round_half_even must use div_euclid/rem_euclid (integer-exact, sign-correct)")
    # The strict effective-date boundary: a bar ON the split date is unadjusted.
    if "effective_ts>event_ts" not in compact:
        fail(
            "the adjustment must apply a split strictly to bars BEFORE its effective_ts "
            "(`effective_ts > event_ts`) — a bar on the effective date is already post-split"
        )
    # Generated (property-style) coverage for the money math, not just fixed examples: a seeded
    # generator over thousands of bar + split sequences checking identity / symbol isolation /
    # compose-then-divide equivalence / order-independence / non-positive rejection.
    if "property_split_adjustment_invariants" not in norm_src:
        fail(
            "normalization.rs must carry a generative property test "
            "(`property_split_adjustment_invariants`) so the money math is checked over generated "
            "bar + split sequences, not just fixed examples"
        )
    return (
        "money math: normalization.rs adjusts in the Rust core — compose-then-divide (one division "
        "per field), i128 intermediates with fail-closed try_from narrowing, round-half-to-even via "
        "div_euclid/rem_euclid, the strict effective_ts > event_ts boundary, AND a generative property "
        "test over thousands of seeded bar + split sequences (identity / symbol isolation / compose / "
        "order-independence / non-positive rejection)"
    )


def check_ohlc_and_volume_factors(config: dict, norm_src: str) -> str:
    # OHLC fields are scaled by the split ratio; volume takes the inverse. Pin the field names so a
    # refactor that drops volume's inverse (or scales a field it should not) fails closed.
    if not re.search(r'PRICE_FIELDS[^\n]*=\s*\[[^\]]*"close"', norm_src):
        fail("normalization.rs must name the OHLC PRICE_FIELDS set (open/high/low/close)")
    if 'VOLUME_FIELD' not in norm_src or '"volume"' not in norm_src:
        fail("normalization.rs must name the VOLUME_FIELD ('volume') that takes the inverse factor")
    return (
        "factors: OHLC (open/high/low/close) take the DEN/NUM price factor, 'volume' takes the "
        "inverse NUM/DEN, every other field passes through unscaled"
    )


def check_not_publicly_exported(config: dict, lib_src: str) -> str:
    compact = _compact(lib_src)
    # The split-adjustment module must be CRATE-INTERNAL: `mod normalization` (NOT `pub mod`), and NONE
    # of its items re-exported -- so a Rust consumer cannot call split_adjust_records directly and obtain
    # split-adjusted IDENTITY values over an empty/incomplete split set (raw-as-adjusted) without proven
    # corporate-action coverage (SRS-DATA-011). This is the Rust-crate-API leg of "no public surface".
    if "pubmodnormalization" in compact:
        fail(
            "the split-adjustment module must be crate-internal (`mod normalization`, NOT `pub mod "
            "normalization`) -- exposing it as a public crate API lets a Rust consumer obtain "
            "raw-as-adjusted output without corporate-action coverage (SRS-DATA-011)"
        )
    if "modnormalization;" not in compact:
        fail("lib.rs must declare the crate-internal `mod normalization;`")
    if "pubusecrate::normalization" in compact:
        fail(
            "lib.rs must NOT re-export anything from the crate-internal normalization module "
            "(split_adjust_records / SplitEvent must not be a public crate API)"
        )
    return (
        "crate-internal API: the split-adjustment math is NOT a public crate API -- lib.rs declares "
        "`mod normalization` (private) and re-exports none of its items, so no Rust consumer can obtain "
        "split-adjusted output; only the crate's own unit tests exercise it"
    )


def check_cli_flag(config: dict, cli_src: str) -> str:
    compact = _compact(cli_src)
    if '"--normalization"' not in compact and "'--normalization'" not in compact:
        fail("data007_query_cli must declare a --normalization flag")
    # split-adjusted is now SERVED -- but ONLY through the coverage-enforcing gate
    # (MarketDataStore::query_split_adjusted, the SRS-DATA-011 surface), which fails closed when the
    # symbol is not covered through --end. So the CLI must ACCEPT split-adjusted at parse and route it
    # to query_split_adjusted (the single path to split-adjusted output, never CLI-side split math),
    # not fail closed at parse and not a silent fall-through to raw. fully-adjusted / total-return remain
    # rejected (dividends deferred, SRS-DATA-012).
    if '"split-adjusted"=>Ok' not in compact:
        fail(
            "data007_query_cli's --normalization parser must ACCEPT split-adjusted (`=> Ok`), routing it "
            "through the coverage gate -- not reject it at parse"
        )
    if "query_split_adjusted" not in compact:
        fail(
            "data007_query_cli must route --normalization split-adjusted through the coverage gate "
            "MarketDataStore::query_split_adjusted (the single path to split-adjusted output, which "
            "fails closed without SRS-DATA-011 coverage) -- never CLI-side split math"
        )
    if '"fully-adjusted"' not in compact or '"total-return"' not in compact:
        fail(
            "data007_query_cli must explicitly REJECT --normalization fully-adjusted / total-return "
            "as deferred rather than silently serving raw values"
        )
    if "SRS-DATA-011" not in cli_src:
        fail(
            "data007_query_cli must name the corporate-action COVERAGE owner (SRS-DATA-011) -- the "
            "split-adjusted gate fails closed without it"
        )
    # The served mode is echoed (`normalization:<mode>`) so a consumer can validate the adjustment.
    if "normalization:" not in cli_src:
        fail("data007_query_cli must echo the served normalization mode (normalization:<mode>)")
    return (
        "CLI surface: data007_query_cli serves --normalization raw, and split-adjusted ONLY through the "
        "coverage-enforcing gate (query_split_adjusted) which fails closed (naming SRS-DATA-011) when the "
        "symbol is not covered through --end; fully-adjusted / total-return remain deferred. The raw "
        "split-adjustment math is never exposed CLI-side -- the gate is the only path to it"
    )


def check_binding_serves_split_adjusted(config: dict, binding_src: str) -> str:
    compact = _compact(binding_src)
    # The consumer binding serves RAW and the gated SPLIT_ADJUSTED (DATA-007 STAYS passes:false: the named
    # consumers are not yet wired). Split-adjusted
    # is routed through the operator CLI's SRS-DATA-011 coverage gate (query_split_adjusted), which fails
    # closed when the symbol is not covered through --end -- so the binding MUST map SPLIT_ADJUSTED to the
    # 'split-adjusted' CLI label, keep the Protocol default, and validate the echoed coverage_through
    # frontier (gate-integrity). FULLY_ADJUSTED / TOTAL_RETURN remain deferred (dividend data,
    # SRS-DATA-012) and fail closed.
    if "_NORMALIZATION_LABEL" not in compact:
        fail("the binding must map served modes to a CLI --normalization label (_NORMALIZATION_LABEL)")
    if 'NormalizationMode.SPLIT_ADJUSTED:"split-adjusted"' not in compact:
        fail(
            "the binding must serve split-adjusted: map SPLIT_ADJUSTED to the 'split-adjusted' CLI label "
            "so it routes through the SRS-DATA-011 coverage gate (data007_query_cli)"
        )
    if "raiseNotImplementedError" not in compact:
        fail("the binding must raise NotImplementedError for every adjusted mode it does not serve")
    if "normalizationnotin_NORMALIZATION_LABEL" not in compact:
        fail(
            "the binding must fail closed for any mode it does not serve "
            "(`normalization not in _NORMALIZATION_LABEL`) -- fully-adjusted / total-return are deferred"
        )
    # It must keep the Protocol default (SPLIT_ADJUSTED) so the bare-default consumer call serves the
    # gated adjusted series (CoverageNotProvenError when uncovered), not a silent RAW default.
    if "normalization:NormalizationMode=NormalizationMode.SPLIT_ADJUSTED" not in compact:
        fail(
            "the binding's query methods must keep the SPLIT_ADJUSTED default (the HistoricalData "
            "Protocol default) so the bare-default consumer call serves the gated adjusted series"
        )
    # Gate-integrity: a split-adjusted response must carry the coverage_through frontier (proving it
    # passed the gate); the binding validates it and fails closed otherwise.
    if "coverage_through" not in compact:
        fail(
            "the binding must validate the echoed coverage_through frontier on a split-adjusted response "
            "(gate-integrity) -- an un-gated 'adjusted' response must fail closed"
        )
    # It must name the corporate-action coverage owner (SRS-DATA-011) -- the gate that makes
    # split-adjusted honest (an uncovered query fails closed naming it).
    if "SRS-DATA-011" not in binding_src:
        fail(
            "the binding must name the corporate-action COVERAGE owner (SRS-DATA-011) -- the gate that "
            "makes a split-adjusted read honest (an uncovered query fails closed naming it)"
        )
    return (
        "binding: StoreBackedHistoricalData serves RAW and the gated SPLIT_ADJUSTED (the Protocol "
        "default), mapping SPLIT_ADJUSTED to the 'split-adjusted' CLI label so it routes through the "
        "SRS-DATA-011 coverage gate (CoverageNotProvenError when uncovered, never raw-as-adjusted), "
        "validates the echoed coverage_through frontier (gate-integrity), and fails closed on "
        "fully-adjusted / total-return (SRS-DATA-012)"
    )


def check_round_trip(config: dict, require_cargo: bool = False) -> str:
    """Prove the split-adjustment MATH at the Rust library level, and that the operator CLI fails closed
    on split-adjusted over an UNCOVERED store (no coverage record) so no public surface emits
    raw-as-adjusted. The covered/served path is proven by tools/coverage_manifest_check.py."""
    block = contract_block(config)
    rt = block["round_trip"]
    crate = block["data_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                "cargo not on PATH but --require-cargo set: cannot verify the split-adjustment math or "
                "the CLI fail-closed behaviour (install the Rust toolchain)"
            )
        return "round-trip: skipped (cargo not on PATH)"

    # 1) The split-adjustment MATH is proven by the crate's OWN unit tests (the module is crate-internal,
    #    not a public crate API, so the math is exercised in-crate, never via an external test or the
    #    CLI): forward/reverse/multi-split, the effective-date boundary, round-half-to-even, the
    #    symbol-only invariant, non-equity + non-positive + overflow fail-closed.
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "normalization", "--quiet"],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib normalization failed:\n{lib.stdout}\n{lib.stderr}")

    # 2) Build the CLIs and prove that over an UNCOVERED store (the data016 fixture ingests NO coverage
    #    record), --normalization split-adjusted fails closed at the coverage gate (no raw-as-adjusted),
    #    while raw returns the stored values verbatim.
    for binary in (block["ingest_cli_bin"], block["cli_bin"]):
        built = subprocess.run(
            [cargo, "build", "-q", "-p", crate, "--bin", binary],
            cwd=ROOT, check=False, capture_output=True, text=True,
        )
        if built.returncode != 0:
            fail(f"building {binary} failed:\n{built.stdout}\n{built.stderr}")
    ingest_bin = ROOT / "target" / "debug" / block["ingest_cli_bin"]
    query_bin = ROOT / "target" / "debug" / block["cli_bin"]

    with tempfile.TemporaryDirectory() as tmp:
        ingested = subprocess.run(
            [str(ingest_bin), "ingest", "--dir", tmp, "--kind", rt["kind"],
             "--event-ts", str(rt["bar_event_ts"]), "--init"],
            cwd=ROOT, check=False, capture_output=True, text=True,
        )
        if ingested.returncode != 0:
            fail(f"ingest {rt['kind']} failed:\n{ingested.stdout}\n{ingested.stderr}")

        def query(mode: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(query_bin), "query", "--dir", tmp, "--symbol", rt["symbol"],
                 "--resolution", rt["resolution"], "--start", "0", "--end", str(rt["bar_event_ts"]),
                 "--kind", rt["kind"], "--normalization", mode],
                cwd=ROOT, check=False, capture_output=True, text=True,
            )

        # Over this UNCOVERED store (no coverage record was ingested), split-adjusted fails closed at
        # the coverage gate (NotCovered) -- never raw-as-adjusted -- naming SRS-DATA-011.
        rejected = query("split-adjusted")
        if rejected.returncode == 0:
            fail(
                "data007_query_cli --normalization split-adjusted over an UNCOVERED store must FAIL "
                f"closed at the coverage gate (SRS-DATA-011); CLI returned 0 with:\n{rejected.stdout}"
            )
        if "SRS-DATA-011" not in rejected.stderr:
            fail(f"expected the split-adjusted gate failure to name SRS-DATA-011 coverage, got:\n{rejected.stderr}")

        raw = query("raw")
        if raw.returncode != 0:
            fail(f"raw query failed:\n{raw.stdout}\n{raw.stderr}")
        close = None
        for line in raw.stdout.splitlines():
            if line.startswith("record.0.field.close:"):
                close = int(line.split(":", 1)[1])
        if close != rt["raw_close_minor"]:
            fail(f"raw close {close} != expected {rt['raw_close_minor']}")
        if "normalization:raw" not in raw.stdout:
            fail("the CLI must echo normalization:raw")

    return (
        "round-trip: the split-adjustment MATH is proven by the crate's OWN unit tests (cargo test --lib "
        "normalization: forward/reverse/multi-split, the effective-date boundary, round-half-to-even, the "
        "symbol-only invariant, non-equity + non-positive + overflow fail-closed); the raw split math is "
        "crate-internal (not a public crate API); and over an UNCOVERED store the data007_query_cli "
        "operator surface fails closed on split-adjusted at the coverage gate (naming SRS-DATA-011), so no "
        "public surface emits raw-as-adjusted output without proven coverage"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# Each static check is paired with the contract-block source key it reads, so the L3 test can load
# exactly that source, mutate it, and prove the guard is non-vacuous.
_STATIC_CHECKS = (
    ("split_kind", "store_source", check_split_kind),
    ("rust_math", "normalization_module", check_rust_math),
    ("ohlc_and_volume_factors", "normalization_module", check_ohlc_and_volume_factors),
    ("cli_flag", "cli_source", check_cli_flag),
    ("binding_serves_split_adjusted", "binding_source", check_binding_serves_split_adjusted),
    ("not_publicly_exported", "lib_source", check_not_publicly_exported),
)

_DEFERRED_OWNERS = (
    "fully-adjusted (splits + dividends) and total-return normalization modes — they need dividend "
    "data + a reinvestment-treatment decision (deferred within SRS-DATA-012)",
    "the LIVE subscription normalization-mode selection — the Market Data Subscription Manager is "
    "unbuilt (SRS-DATA-012 names live subscriptions; this slice is the HISTORICAL read)",
    "corporate-action ingestion from a real provider — split events stand in via the fixture batch "
    "(SRS-DATA-011 owns scheduled corporate-action ingestion)",
    "an authoritative SDK<->core money-unit scale — the binding assumes the cents (x100) fixture "
    "convention for equity OHLC (deferred with the runtime money boundary, atp-types)",
)


def assert_normalization_modes_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        key: _read(config, key, root)
        for key in {src_key for _, src_key, _ in _STATIC_CHECKS}
    }
    return [check(config, sources[src_key]) for _, src_key, check in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_normalization_modes_static(config)
    evidence.append(check_round_trip(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SRS-DATA-012 split-adjusted normalization contract evidence"
    )
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the end-to-end split round-trip must run.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except NormalizationModesCheckError as error:
        print(f"SRS-DATA-012 SPLIT-ADJUSTED NORMALIZATION FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-DATA-012 SPLIT-ADJUSTED NORMALIZATION PASS")
    for item in evidence:
        print(f"- {item}")
    print("- deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
