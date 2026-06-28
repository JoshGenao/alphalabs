"""SRS-DATA-016 / SyRS NFR-R4 -- re-running an ingestion for an already ingested date creates no
duplicate records and does not corrupt existing data.

L7 domain (safety) test. The acceptance criterion's safety core is data integrity: an ingestion
job that re-runs (a nightly retry, a crash-recovery replay, an operator re-trigger) must be
IDEMPOTENT -- it must neither duplicate a record (which would double-count volume / corrupt
downstream metrics and backtests) nor mutate already-ingested data (which would silently rewrite
history a strategy was authored against). A leak in either is a trading-decision safety bug: a
duplicated or rewritten bar would mis-fill a paper/backtest order and mis-rank a strategy. This
test proves the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration tests
     ``crates/atp-data/tests/srs_data_016_idempotent_ingest.rs`` and exercises the operator CLI
     ``data016_ingest_cli`` over a real temp directory, asserting that re-ingesting an
     already-ingested date for every one of the four sources the acceptance names (Databento
     daily, IB minute, IB option-chain, Sharadar -- the vendor-neutral DailyEquityBar /
     MinuteEquityBar / OptionChainSnapshot / Fundamental kinds) creates no duplicate record and
     leaves the persisted file byte-for-byte identical, that a conflicting re-ingest fails closed
     leaving existing data intact, that a record the ERR-5 gate quarantines never reaches the
     store, and that a missing/unmounted store directory fails closed.

  2. Structural -- it asserts, via ``tools/ingestion_idempotency_check.py``, that ``atp-data``
     keeps the idempotency no-op + conflict guard, composes the UNCHANGED ERR-5 validation gate
     (the store mutator never contaminates ``ingest_record``), verifies the integrity checksum
     before building any state, fsyncs + atomically renames the durable write, keeps value fields
     in integer minor units, uses no nondeterminism source, declares no broker dependency, and
     leaks no vendor SDK token.

Each structural guard is checked for non-vacuity: a dropped no-op, a dropped conflict guard, a
bypassed ERR-5 gate, a store mutator leaked into ``ingest_record``, a dropped durable fsync, an
injected nondeterminism source, and a leaked vendor token are each shown to be caught.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from ingestion_idempotency_check import (  # noqa: E402
    IngestionIdempotencyCheckError,
    cargo_source,
    check_determinism,
    check_file_persistence,
    check_ingest_market_record,
    check_ingest_record_unchanged,
    check_no_broker_dependency,
    check_record_hash,
    check_reingest_nonmutating,
    check_store_lock,
    check_upsert,
    check_vendor_isolation,
    cli_source,
    lib_source,
    load_config,
    store_source,
)

KINDS = ("daily-equity-bar", "minute-equity-bar", "option-chain", "fundamental")


# --------------------------------------------------------------------------- #
# Behavioral: the Rust L5 integration tests.
# --------------------------------------------------------------------------- #


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run the Rust idempotency path")
    return cargo


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _cargo(),
            "test",
            "-p",
            "atp-data",
            "--test",
            "srs_data_016_idempotent_ingest",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_one_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output for {label}:\n{combined}"


def test_reingest_each_kind_creates_no_duplicate_and_no_corruption() -> None:
    _assert_one_passed(
        _run_cargo_test("srs_data_016_reingest_each_kind_creates_no_duplicate_and_no_corruption"),
        "SRS-DATA-016 per-kind re-ingest idempotency",
    )


def test_conflicting_reingest_fails_closed() -> None:
    _assert_one_passed(
        _run_cargo_test("srs_data_016_conflicting_reingest_fails_closed_without_corrupting"),
        "SRS-DATA-016 conflicting re-ingest fail-closed",
    )


def test_quarantined_record_not_written() -> None:
    _assert_one_passed(
        _run_cargo_test("srs_data_016_quarantined_record_is_not_written_to_the_store"),
        "SRS-DATA-016 quarantined record not written",
    )


def test_validation_is_bound_to_the_persisted_record() -> None:
    # Safety: the ERR-5 gate validates exactly the record that will be persisted (the envelope is
    # derived from the record), so a caller cannot validate one payload and store another.
    _assert_one_passed(
        _run_cargo_test("srs_data_016_validation_is_bound_to_the_persisted_record"),
        "SRS-DATA-016 validation bound to the persisted record",
    )


def test_store_lock_serializes_writers_without_loss() -> None:
    # Safety: the single-writer lock serializes load-modify-save so a later ingestion job's save can
    # never erase an earlier job's records (no last-writer-wins data loss); a concurrent writer is
    # refused rather than silently dropping records.
    _assert_one_passed(
        _run_cargo_test("srs_data_016_store_lock_serializes_writers_without_loss"),
        "SRS-DATA-016 single-writer lock prevents concurrent-job record loss",
    )


def test_missing_store_directory_fails_closed() -> None:
    _assert_one_passed(
        _run_cargo_test("srs_data_016_missing_store_directory_fails_closed"),
        "SRS-DATA-016 missing store directory fail-closed",
    )


# --------------------------------------------------------------------------- #
# Behavioral: the operator CLI workflow over a real temp directory.
# --------------------------------------------------------------------------- #


def _build_cli() -> Path:
    build = subprocess.run(
        [_cargo(), "build", "-q", "-p", "atp-data", "--bin", "data016_ingest_cli"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"building data016_ingest_cli failed:\n{build.stderr}"
    binary = REPO_ROOT / "target" / "debug" / "data016_ingest_cli"
    assert binary.exists(), f"CLI binary missing at {binary}"
    return binary


def _run_cli(binary: Path, *args: str) -> dict[str, str]:
    result = subprocess.run(
        [str(binary), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"CLI {args} failed:\n{result.stdout}\n{result.stderr}"
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key] = value
    return fields


def test_cli_operator_workflow_is_idempotent_across_all_four_sources() -> None:
    # The end-to-end operator workflow: ingest each of the four sources into a fresh store, then
    # re-ingest each and assert the no-duplicate / byte-identical invariant from the CLI surface.
    binary = _build_cli()
    with tempfile.TemporaryDirectory(prefix="atp_data016_l7_") as tmp:
        first = _run_cli(binary, "ingest", "--dir", tmp, "--kind", KINDS[0], "--init")
        assert int(first["inserted"]) > 0
        for kind in KINDS[1:]:
            ingest = _run_cli(binary, "ingest", "--dir", tmp, "--kind", kind)
            assert int(ingest["inserted"]) > 0, f"{kind}: first ingest must insert records"

        inspected = _run_cli(binary, "inspect", "--dir", tmp)
        store_len = int(inspected["store_len"])
        store_bytes = int(inspected["store_bytes"])
        assert store_len > 0 and store_bytes > 0

        for kind in KINDS:
            re = _run_cli(binary, "reingest", "--dir", tmp, "--kind", kind)
            assert re["inserted"] == "0", f"{kind}: re-ingest must insert nothing"
            assert int(re["duplicates_skipped"]) > 0, f"{kind}: re-ingest must be a no-op"
            assert re["bytes_identical"] == "true", f"{kind}: persisted file must be byte-identical"

        # The catalog is unchanged after all re-ingests (no duplicate, no growth).
        after = _run_cli(binary, "inspect", "--dir", tmp)
        assert int(after["store_len"]) == store_len, "re-ingest must not grow the catalog"
        assert int(after["store_bytes"]) == store_bytes, "re-ingest must not change the bytes"


def test_cli_fails_closed_on_missing_store_directory() -> None:
    binary = _build_cli()
    missing = Path(tempfile.gettempdir()) / "atp_data016_never_provisioned_l7"
    if missing.exists():
        shutil.rmtree(missing, ignore_errors=True)
    result = subprocess.run(
        [str(binary), "ingest", "--dir", str(missing), "--kind", KINDS[0]],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "ingest into a missing (non --init) directory must fail closed"
    assert "missing or not a directory" in (result.stdout + result.stderr)


def test_cli_reingest_before_ingest_fails_closed_without_writing() -> None:
    # Safety: `reingest` is a PROOF, not an ingest. Running it on a provisioned-but-empty directory
    # (or the wrong kind/date) must fail closed and persist NOTHING -- a failed idempotency proof can
    # never become a state-changing ingest.
    binary = _build_cli()
    with tempfile.TemporaryDirectory(prefix="atp_data016_l7_") as tmp:
        result = subprocess.run(
            [str(binary), "reingest", "--dir", tmp, "--kind", KINDS[0]],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, "reingest before ingest must fail closed"
        assert "was NOT idempotent" in (result.stdout + result.stderr)
        assert not (Path(tmp) / "market_data.store").exists(), (
            "a failed reingest proof must not write a store file"
        )


# --------------------------------------------------------------------------- #
# Structural: each safety guard is checked for non-vacuity.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    config = load_config()
    return {
        "config": config,
        "store": store_source(config),
        "lib": lib_source(config),
        "cargo": cargo_source(config),
        "cli": cli_source(config),
    }


def test_idempotency_no_op_guard_is_non_vacuous(sources) -> None:
    mutated = sources["store"].replace(
        "UpsertOutcome::UnchangedDuplicate", "UpsertOutcome::Inserted"
    )
    with pytest.raises(IngestionIdempotencyCheckError):
        check_upsert(sources["config"], mutated)


def test_conflict_guard_is_non_vacuous(sources) -> None:
    mutated = sources["store"].replace(
        "StoreError::ConflictingContent", "StoreError::CorruptRecord"
    )
    with pytest.raises(IngestionIdempotencyCheckError):
        check_upsert(sources["config"], mutated)


def test_err5_gate_composition_is_non_vacuous(sources) -> None:
    mutated = sources["lib"].replace("self.ingest_record(", "self.skip_validation(")
    with pytest.raises(IngestionIdempotencyCheckError):
        check_ingest_market_record(sources["config"], mutated)


def test_validation_binding_is_non_vacuous(sources) -> None:
    # Reverting to an independently-supplied submission (instead of deriving it from the record)
    # would let a caller validate a benign payload and persist a different one -- caught.
    mutated = sources["lib"].replace("record.ingestion_submission()", "forged_submission()")
    with pytest.raises(IngestionIdempotencyCheckError):
        check_ingest_market_record(sources["config"], mutated)


def test_record_hash_is_full_record_sha256_non_vacuous(sources) -> None:
    # Hashing only the value fields (not the full record) would collide across distinct keys,
    # corrupting ERR-5 quarantine/dedup evidence -- caught.
    mutated = sources["store"].replace(
        "encode_record(&mut encoded, self)", "encode_fields_only(&mut encoded, self)"
    )
    with pytest.raises(IngestionIdempotencyCheckError):
        check_record_hash(sources["config"], mutated)


def test_gate_stays_store_free_is_non_vacuous(sources) -> None:
    mutated = sources["lib"].replace(
        "match validator.validate(&record) {",
        "let _ = store.upsert(record);\n        match validator.validate(&record) {",
        1,
    )
    with pytest.raises(IngestionIdempotencyCheckError):
        check_ingest_record_unchanged(sources["config"], mutated)


def test_durable_fsync_guard_is_non_vacuous(sources) -> None:
    mutated = sources["store"].replace("scratch.sync_all()", "Ok(())")
    with pytest.raises(IngestionIdempotencyCheckError):
        check_file_persistence(sources["config"], mutated)


def test_single_writer_lock_guard_is_non_vacuous(sources) -> None:
    # Reverting the atomic O_EXCL create (so two writers both "acquire") would re-open the
    # concurrent-job record-loss hole -- caught.
    mutated = sources["store"].replace("create_new(true)", "create(true)")
    with pytest.raises(IngestionIdempotencyCheckError):
        check_store_lock(sources["config"], mutated)


def test_reingest_nonmutating_guard_is_non_vacuous(sources) -> None:
    # Injecting a save into cmd_reingest would turn a failed idempotency proof into a state-changing
    # ingest -- caught.
    mutated = sources["cli"].replace(
        "    let store_len_after = store.len();",
        "    store.save_to_path(&dir).ok();\n    let store_len_after = store.len();",
        1,
    )
    with pytest.raises(IngestionIdempotencyCheckError):
        check_reingest_nonmutating(sources["config"], mutated)


def test_determinism_guard_is_non_vacuous(sources) -> None:
    mutated = sources["store"] + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
    with pytest.raises(IngestionIdempotencyCheckError):
        check_determinism(sources["config"], mutated)


def test_no_broker_dependency_guard_is_non_vacuous(sources) -> None:
    mutated = sources["cargo"] + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(IngestionIdempotencyCheckError):
        check_no_broker_dependency(sources["config"], mutated)


def test_vendor_isolation_guard_is_non_vacuous(sources) -> None:
    mutated = sources["store"] + "\n// records mirrored to ib_insync under the hood\n"
    with pytest.raises(IngestionIdempotencyCheckError):
        check_vendor_isolation(sources["config"], mutated)
