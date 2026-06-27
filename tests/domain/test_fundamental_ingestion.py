"""SRS-DATA-005 / SyRS SYS-26, NFR-P8d -- ingested Sharadar fundamentals an operator can trust.

A factor ranking is only as trustworthy as the fundamentals feeding it. The safety core of
SRS-DATA-005's "ingested, validated, cataloged, available to the factor pipeline" is that a
fundamental statement reaches the factor loader (1) WITHOUT lookahead bias -- selected by its filing
(`available_ts`) instant, never its fiscal period end, so a run never consumes a statement that was
not yet knowable; (2) WITHOUT a fabricated ratio -- a non-positive market value (the denominator) or
impossible provenance (`available_ts < period_end_ts`) fails closed rather than yielding a junk
factor input; and (3) WITHOUT a leaked vendor dependency -- the Sharadar token stays in the adapter
layer so the offline factor job cannot couple to a provider SDK (SRS-ARCH-003).

L7 domain (safety) test. It proves the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-factor-pipeline/tests/srs_data_005_fundamental_ingest.rs`` (build -> ingest ->
     persist -> reload -> re-ingest is a no-op -> READ via the REAL ``load_fundamental_input`` with
     point-in-time correctness and negatives-allowed), and to the ``atp-adapters`` Sharadar mapping
     unit tests (a malformed vendor row fails closed).

  2. Structural -- it asserts, via ``tools/fundamental_ingestion_check.py``, that the DTO fails
     closed on impossible provenance / non-positive market value, the builder's ratios record matches
     the loader contract exactly, the adapter mapping carries the vendor columns, and the core stays
     vendor-neutral -- with the vendor-isolation guard shown non-vacuous by an injected leak.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from fundamental_ingestion_check import (  # noqa: E402
    FundamentalIngestionCheckError,
    adapters_source,
    assert_fundamental_ingestion_static,
    check_vendor_isolation,
    data_source,
    load_config,
    types_source,
)


def _run_cargo_test(crate: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [cargo, "test", "-p", crate, *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


# --------------------------------------------------------------------------- #
# Behavioral (end-to-end Rust integration)
# --------------------------------------------------------------------------- #


def test_fundamentals_ingested_and_read_by_factor_loader() -> None:
    # The full SRS-DATA-005 path: build the four statement records, ingest through the idempotent
    # market-record gate, persist/reload, re-ingest (no-op), and read the ratios record back through
    # the REAL load_fundamental_input -- including the point-in-time skip and negatives-allowed paths.
    result = _run_cargo_test(
        "atp-factor-pipeline",
        ["--test", "srs_data_005_fundamental_ingest"],
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-DATA-005 integration failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    # 4 cases: loader-read, point-in-time skip, negatives-allowed, and the restatement fail-closed pin.
    assert "4 passed" in combined, f"unexpected cargo test output:\n{combined}"


def test_sharadar_mapping_fails_closed_on_malformed_rows() -> None:
    # Safety: the provider -> vendor-neutral mapping rejects a row filed before its period ends,
    # with a non-positive market cap, with a negative nonnegative-domain line item, or carrying an
    # unsupported SF1 dimension (so two same-period rows differing only by dimension cannot collapse)
    # -- all surfaced through the common adapter taxonomy (AdapterError::InvalidProviderData).
    result = _run_cargo_test("atp-adapters", ["--lib", "sharadar"])
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"Sharadar mapping tests failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "6 passed" in combined, f"unexpected cargo test output:\n{combined}"


def test_dto_fail_closed_constructor() -> None:
    # The atp-types DTO constructor is the point-of-use guard: its unit tests assert empty symbol,
    # negative period end, impossible provenance, non-positive market value, and negative
    # nonnegative-domain fields all fail closed (while signed fields stay allowed).
    result = _run_cargo_test("atp-types", ["--lib", "fundamental_statements"])
    assert result.returncode == 0, (
        f"FundamentalStatements DTO tests failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


# --------------------------------------------------------------------------- #
# Structural (contract guards)
# --------------------------------------------------------------------------- #


def test_substrate_contract_guards_pass() -> None:
    config = load_config()
    evidence = assert_fundamental_ingestion_static(config)
    # Eight structural guards (DTO, builder, loader-contract, adapter, CLI, ingest-path, numeric,
    # vendor isolation) -- the substrate is fully pinned.
    assert len(evidence) == 8, f"expected 8 structural guards, got {len(evidence)}:\n{evidence}"


def test_vendor_isolation_guard_is_non_vacuous() -> None:
    # The guard must actually catch a vendor leak into the core data layer -- inject a `sharadar`
    # token into the atp-data source and assert it fails closed.
    config = load_config()
    types_src = types_source(config)
    adapters_src = adapters_source(config)
    poisoned_data = data_source(config) + "\n// sharadar leak\n"
    with pytest.raises(FundamentalIngestionCheckError):
        check_vendor_isolation(config, types_src, poisoned_data, adapters_src)
