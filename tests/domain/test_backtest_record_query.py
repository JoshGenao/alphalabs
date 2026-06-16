"""SRS-BT-009 / SyRS SYS-21, SYS-79 -- a completed backtest's result is persisted as one
record that is queryable by strategy, date range, and parameter set, round-trips
deterministically without fabricating money or losing the benchmark identity, and fails
closed on a corrupt blob (yielding no partial store).

L7 domain (safety) test. The acceptance criterion's safety core is that the persisted
backtest history an operator queries -- and the metrics + benchmark comparison they rank
and size capital on -- is *trustworthy*: a record must round-trip EXACTLY (no fabricated
money, no precision-losing or NaN ratio), it must remain queryable by every axis the
acceptance names, two results must never share an identity (a duplicate run id is
rejected), a corrupt or truncated blob (any change that does not also recompute the
non-cryptographic checksum -- i.e. ACCIDENTAL corruption; a deliberate checksum-recomputing
tamperer needs a keyed MAC, out of scope for single-user/local) must fail closed yielding no
partially-restored store, the persistence must be deterministic (a query and a serialized blob
are identical for the
same record set), and the surface must be independent of the IB account. A leak in any of
these is a trading-decision safety bug: a mislabeled, mis-rounded, or partially-restored
backtest record would mis-rank a strategy or misstate historical performance. This test
proves the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration tests
     ``crates/atp-simulation/tests/srs_bt_009_backtest_store.rs`` and
     ``srs_bt_009_persist_query.rs`` and asserts that a real completed backtest is persisted
     and queryable by strategy / date range / parameter set, that the store serialize/restore
     round-trips deterministically (preserving the metrics + benchmark comparison), that a
     corrupt or foreign blob fails closed, that a duplicate run id is rejected, and that the
     durable FILE layer (save_to_path / load_from_path) survives a disk round trip -- a missing
     file loads empty while a corrupt on-disk file fails closed (a persisted run is never lost).

  2. Structural -- it asserts, via ``tools/backtest_store_check.py``, that the
     ``atp-simulation`` crate declares no dependency on the live/broker path
     (``atp-execution`` / ``atp-adapters``) and that ``backtest_store`` leaks no vendor SDK
     token, uses no nondeterminism source, verifies the integrity checksum before building
     any state and guards a non-finite restored ratio, and rejects a duplicate run id.

Each structural guard is checked for non-vacuity: an injected broker dependency, a leaked
vendor token, an injected nondeterminism source, a dropped codec finite/checksum guard, and
a dropped duplicate-run-id guard are each shown to be caught.
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

from backtest_store_check import (  # noqa: E402
    BacktestStoreCheckError,
    cargo_source,
    check_codec,
    check_determinism,
    check_file_persistence,
    check_from_result,
    check_insert,
    check_no_broker_dependency,
    check_record_coherence,
    check_strategy_parameters,
    check_vendor_isolation,
    load_config,
    store_source,
)


def _run_cargo_test(
    test_name: str, test_file: str = "srs_bt_009_backtest_store"
) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-simulation",
            "--test",
            test_file,
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


def test_persists_and_queries_completed_backtest() -> None:
    # The safety core: a real completed backtest's seven artifacts are persisted into one
    # record and remain queryable by strategy, date range, and parameter set.
    _assert_one_passed(
        _run_cargo_test("srs_bt_009_persists_and_queries_completed_backtest"),
        "SRS-BT-009 persist + query",
    )


def test_serialize_restore_round_trips() -> None:
    # The store round-trips deterministically (preserving metrics + benchmark comparison), so
    # a persisted history reproduces exactly.
    _assert_one_passed(
        _run_cargo_test("srs_bt_009_serialize_restore_round_trips"),
        "SRS-BT-009 round-trip determinism",
    )


def test_restore_fails_closed_on_corruption() -> None:
    # Safety: a corrupted (non-recomputed-checksum) or foreign blob is rejected (checksum /
    # magic) yielding no partially-restored store, so corrupt bytes never become fabricated
    # results. (Deliberate checksum-recomputing tampering is out of scope -- single-user/local.)
    _assert_one_passed(
        _run_cargo_test("srs_bt_009_restore_fails_closed_on_corruption"),
        "SRS-BT-009 corrupt-blob fail-closed",
    )


def test_rejects_duplicate_run_id() -> None:
    # Safety: two results can never share an identity, so a query by run can never be
    # ambiguous.
    _assert_one_passed(
        _run_cargo_test("srs_bt_009_rejects_duplicate_run_id"),
        "SRS-BT-009 duplicate-run-id rejection",
    )


def test_persists_to_disk_and_queries_round_trip() -> None:
    # Safety core of the durable file layer: a completed backtest's seven artifacts are written
    # to disk, read back, and remain queryable by strategy / date range / parameter set with the
    # loaded store equal to the original -- the persisted history survives a process restart.
    _assert_one_passed(
        _run_cargo_test(
            "srs_bt_009_persist_to_disk_and_query_round_trip",
            test_file="srs_bt_009_persist_query",
        ),
        "SRS-BT-009 disk persist + query round trip",
    )


def test_load_missing_file_in_present_dir_is_empty() -> None:
    # A provisioned directory that never persisted a result loads an empty store, not an error --
    # so a genuine fresh install is never mistaken for a failure that would block the operator.
    _assert_one_passed(
        _run_cargo_test(
            "srs_bt_009_load_missing_file_in_present_dir_is_empty",
            test_file="srs_bt_009_persist_query",
        ),
        "SRS-BT-009 missing-file empty load",
    )


def test_load_missing_directory_fails_closed() -> None:
    # Safety: a MISSING configured directory (unmounted / deleted / misconfigured storage path)
    # fails closed rather than masquerading as an empty history that silently drops persisted runs.
    _assert_one_passed(
        _run_cargo_test(
            "srs_bt_009_load_missing_directory_fails_closed",
            test_file="srs_bt_009_persist_query",
        ),
        "SRS-BT-009 missing-directory fail-closed",
    )


def test_load_corrupt_file_fails_closed() -> None:
    # Safety: a corrupt on-disk store file fails closed rather than silently dropping persisted
    # runs -- a persisted result is never lost without a loud failure.
    _assert_one_passed(
        _run_cargo_test(
            "srs_bt_009_load_corrupt_file_fails_closed",
            test_file="srs_bt_009_persist_query",
        ),
        "SRS-BT-009 corrupt-file fail-closed",
    )


def test_resave_atomically_replaces_prior_store() -> None:
    # Safety: re-publishing a store atomically replaces the prior file -- never a merge, a
    # partial overwrite, or a leftover scratch file -- so the durable history is always exactly
    # one fully-written store.
    _assert_one_passed(
        _run_cargo_test(
            "srs_bt_009_resave_atomically_replaces_prior_store",
            test_file="srs_bt_009_persist_query",
        ),
        "SRS-BT-009 atomic resave replace",
    )


def test_concurrent_saves_never_corrupt() -> None:
    # Safety: many threads persisting the same store to one directory never produce a half-written
    # or corrupt file (per-call unique scratch + atomic rename); the published store always
    # restores cleanly. A lost-run-under-crash bug here would silently corrupt backtest history.
    _assert_one_passed(
        _run_cargo_test(
            "srs_bt_009_concurrent_saves_never_corrupt",
            test_file="srs_bt_009_persist_query",
        ),
        "SRS-BT-009 concurrent-save corruption safety",
    )


def test_durable_load_delegates_to_failclosed_restore() -> None:
    config = load_config()
    # The real load path delegates a present file to the fail-closed restore() codec, so a
    # corrupt file can never silently become an empty/partial store (a lost-run safety bug).
    check_file_persistence(config, store_source(config))
    # ...and the guard must not be vacuous: replacing the restore() delegation with an empty
    # store (which would silently drop every persisted run) is caught.
    mutated = store_source(config).replace("Self::restore(&contents)", "Ok(Self::new())")
    with pytest.raises(BacktestStoreCheckError):
        check_file_persistence(config, mutated)


def test_durable_save_fsyncs_before_publish() -> None:
    config = load_config()
    # The real save path fsyncs the scratch file BEFORE the atomic rename, so a crash cannot
    # publish unwritten bytes -- a persisted run is durable, not just visible.
    check_file_persistence(config, store_source(config))
    # ...and the guard must not be vacuous: dropping the scratch fsync (which would risk losing a
    # just-persisted store on a power loss) is caught.
    mutated = store_source(config).replace("scratch.sync_all()", "Ok(())")
    with pytest.raises(BacktestStoreCheckError):
        check_file_persistence(config, mutated)


def test_store_crate_has_no_broker_dependency() -> None:
    config = load_config()
    # The real Cargo.toml must declare no live/broker-path dependency, so the persisted
    # record is independent of the IB account at the crate boundary.
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected broker dep is caught.
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(BacktestStoreCheckError):
        check_no_broker_dependency(config, mutated)


def test_store_module_leaks_no_vendor_token() -> None:
    config = load_config()
    check_vendor_isolation(config, store_source(config))
    mutated = store_source(config) + "\n// records mirrored to ib_insync under the hood\n"
    with pytest.raises(BacktestStoreCheckError):
        check_vendor_isolation(config, mutated)


def test_persistence_is_deterministic() -> None:
    config = load_config()
    # The real module uses no parallelism/RNG/clock, so a query and the serialized blob are
    # identical for the same record set.
    check_determinism(config, store_source(config))
    mutated = store_source(config) + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
    with pytest.raises(BacktestStoreCheckError):
        check_determinism(config, mutated)


def test_codec_fails_closed_on_corruption_and_non_finite() -> None:
    config = load_config()
    # The real codec verifies the integrity checksum BEFORE building any state and guards a
    # non-finite restored ratio, so a corrupted (non-recomputed-checksum) blob fails closed
    # yielding no partial store.
    check_codec(config, store_source(config))
    # ...and the guards must not be vacuous: dropping the checksum-first check is caught.
    mutated = store_source(config).replace("if checksum(body) != stored_checksum", "if false")
    with pytest.raises(BacktestStoreCheckError):
        check_codec(config, mutated)
    # ...and dropping the finite guard is caught.
    mutated = store_source(config).replace("is_finite()", "is_nan()")
    with pytest.raises(BacktestStoreCheckError):
        check_codec(config, mutated)


def test_insert_rejects_duplicate_run_id_structurally() -> None:
    config = load_config()
    # The real insert rejects a duplicate run id; dropping the guard is caught. (The token
    # appears in both insert and restore, so drop it everywhere to prove non-vacuity.)
    check_insert(config, store_source(config))
    mutated = store_source(config).replace(
        "StoreError::DuplicateRunId", "StoreError::CorruptRecord"
    )
    with pytest.raises(BacktestStoreCheckError):
        check_insert(config, mutated)


def test_parameter_set_axis_distinguishes_sweep_points() -> None:
    config = load_config()
    # Safety: the parameter-set axis must query the tuned StrategyParameters, not the launch
    # BacktestRequest, or two sweep points that share a request would be indistinguishable and
    # an operator could rank the wrong configuration (SYS-21).
    check_strategy_parameters(config, store_source(config))
    mutated = store_source(config).replace(
        "record.parameters == *params", "record.request == *params"
    )
    with pytest.raises(BacktestStoreCheckError):
        check_strategy_parameters(config, mutated)


def test_record_coherence_is_fail_closed() -> None:
    config = load_config()
    # Safety: a record must not persist a fill from another symbol, a self-contradictory
    # benchmark, or artifacts the metric producer would reject (so the stored data is
    # structurally coherent with the producer's input contract); dropping any guard is caught.
    check_record_coherence(config, store_source(config))
    mutated = store_source(config).replace("fill.symbol != record.request.symbol", "false")
    with pytest.raises(BacktestStoreCheckError):
        check_record_coherence(config, mutated)
    mutated = store_source(config).replace(
        "record.metrics.benchmark_symbol != record.comparison.benchmark_symbol", "false"
    )
    with pytest.raises(BacktestStoreCheckError):
        check_record_coherence(config, mutated)
    # Dropping the non-empty-equity-curve producer invariant is caught.
    mutated = store_source(config).replace("empty equity curve", "ok empty curve")
    with pytest.raises(BacktestStoreCheckError):
        check_record_coherence(config, mutated)
    # Dropping the excess = strategy - benchmark internal-consistency identity is caught.
    mutated = store_source(config).replace("strategy - benchmark - excess", "0.0_f64")
    with pytest.raises(BacktestStoreCheckError):
        check_record_coherence(config, mutated)
    # Dropping a per-metric domain bound (so an impossible win_rate > 1 could persist) is caught.
    mutated = store_source(config).replace("win rate outside [0, 1]", "ok win rate")
    with pytest.raises(BacktestStoreCheckError):
        check_record_coherence(config, mutated)


def test_from_result_binds_provenance() -> None:
    config = load_config()
    # Safety: the safe producer constructor binds artifacts to the BacktestResult and verifies the
    # request's data source + window match it, so a record cannot be persisted under false
    # provenance. Dropping the data-source provenance guard is caught.
    check_from_result(config, store_source(config))
    mutated = store_source(config).replace("result.data_source != request.data_source", "false")
    with pytest.raises(BacktestStoreCheckError):
        check_from_result(config, mutated)


def test_decode_is_allocation_safe_on_untrusted_counts() -> None:
    config = load_config()
    # Safety: a checksum-valid blob with an oversized count must fail closed by exhausting the
    # cursor, never abort on an out-of-memory allocation. Reintroducing a pre-sized vector from
    # the decoded count is caught.
    check_codec(config, store_source(config))
    mutated = store_source(config).replace(
        "let mut trade_log = Vec::new();",
        "let mut trade_log = Vec::with_capacity(trade_count);",
    )
    with pytest.raises(BacktestStoreCheckError):
        check_codec(config, mutated)
    # Reverting to a per-record sorted insert (dropping the single bulk sort) is caught -- it
    # would make restoring a large history O(n^2).
    mutated = store_source(config).replace("records.sort_by", "records.iter")
    with pytest.raises(BacktestStoreCheckError):
        check_codec(config, mutated)
