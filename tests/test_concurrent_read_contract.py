"""Contract tests for SRS-DATA-017 (support concurrent reads during ingestion writes).

SRS-DATA-017 / SyRS SYS-63 / StRS SN-1.26, SN-1.28 -- strategy containers, backtests, factor jobs, and
notebooks read previously ingested data while ingestion jobs write new data WITHOUT corruption or
blocking completed data. Verification mode: Load test. This is a runtime concurrency property of the
SRS-DATA-016 storage substrate (snapshot isolation: lock-free readers + a single-writer lock + atomic
publish + fail-closed reads).

Mirrors ``tests/test_unified_query_contract.py``: shells out to ``tools/concurrent_read_check.py``,
then exercises each per-check function in-process with negative spot-checks that mutate the Rust
source in memory and assert the contract actually catches the regression (an injected reader lock, a
dropped writer lock, a non-atomic publish, a dropped checksum guard, a non-exclusive lock create, a
gutted Load test, an injected broker dependency, a leaked vendor token). A behavioral subprocess test
then runs a real ``data016_ingest_cli`` writer concurrently with ``inspect`` + ``query`` readers and
asserts every concurrent read succeeds with the seed present and no corruption.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from concurrent_read_check import (  # noqa: E402
    ConcurrentReadCheckError,
    assert_concurrent_read_static,
    cargo_source,
    check_atomic_publish,
    check_cargo_test_smoke,
    check_fail_closed_read,
    check_inspect_lock_free,
    check_load_test_present,
    check_no_broker_dependency,
    check_read_cli_lock_free,
    check_single_writer_lock,
    check_vendor_isolation,
    check_writer_serialized,
    load_config,
    load_test_source,
    read_cli_source,
    run_checks,
    store_source,
    write_cli_source,
)


class ConcurrentReadScriptTest(unittest.TestCase):
    def test_srs_data_017_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/concurrent_read_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-DATA-017 CONCURRENT-READ PASS", result.stdout)
        for needle in (
            "data007_query_cli is a lock-free reader",
            "`cmd_inspect` is a lock-free reader",
            "`cmd_ingest` acquires the single-writer StoreLock BEFORE and holds it ACROSS",
            "publishes atomically",
            "reads fail-closed",
            "single-writer guard",
            "Load test srs_data_017_concurrent_reads.rs",
            "Cargo.toml declares no dependency on the broker/execution path",
            "store path is free of all 5 forbidden vendor SDK tokens",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.store_src = store_source(self.config)
        self.read_cli_src = read_cli_source(self.config)
        self.write_cli_src = write_cli_source(self.config)
        self.cargo_src = cargo_source(self.config)
        self.load_test_src = load_test_source(self.config)


class ReadCliLockFreeTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("lock-free reader", check_read_cli_lock_free(self.config, self.read_cli_src))

    def test_injected_reader_lock_is_caught(self) -> None:
        # A read taking the single-writer lock would needlessly serialize against ingestion writers --
        # the exact "blocking completed data" the AC forbids.
        mutated = self.read_cli_src.replace(
            "let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;",
            "let _l = StoreLock::acquire(&dir);\n    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;",
            1,
        )
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_read_cli_lock_free(self.config, mutated)
        self.assertIn("StoreLock::acquire", str(ctx.exception))


class InspectLockFreeTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("lock-free reader", check_inspect_lock_free(self.config, self.write_cli_src))

    def test_injected_inspect_lock_is_caught(self) -> None:
        mutated = self.write_cli_src.replace(
            "    for kind in DatasetKind::all() {",
            "    let _l = StoreLock::acquire(&dir);\n    for kind in DatasetKind::all() {",
            1,
        )
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_inspect_lock_free(self.config, mutated)
        self.assertIn("StoreLock::acquire", str(ctx.exception))


class WriterSerializedTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn(
            "holds it ACROSS the whole load-modify-save",
            check_writer_serialized(self.config, self.write_cli_src),
        )

    def test_dropped_writer_lock_is_caught(self) -> None:
        # Removing the lock from the ingest writer reintroduces last-publish-wins between two jobs.
        mutated = self.write_cli_src.replace(
            "    let _lock = StoreLock::acquire(&dir).map_err(|err| err.to_string())?;\n"
            "    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;\n"
            "    let (inserted, duplicates) = ingest_batch",
            "    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;\n"
            "    let (inserted, duplicates) = ingest_batch",
            1,
        )
        self.assertNotEqual(mutated, self.write_cli_src, "the mutation must change cmd_ingest")
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_writer_serialized(self.config, mutated)
        self.assertIn("StoreLock::acquire", str(ctx.exception))

    def test_premature_lock_drop_is_caught(self) -> None:
        # Acquiring the lock then dropping it BEFORE the load-modify-save passes a naive token-presence
        # check but reopens last-publish-wins data loss; the lifetime check must catch it.
        mutated = self.write_cli_src.replace(
            "    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;\n"
            "    let (inserted, duplicates) = ingest_batch",
            "    drop(_lock);\n"
            "    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;\n"
            "    let (inserted, duplicates) = ingest_batch",
            1,
        )
        self.assertNotEqual(mutated, self.write_cli_src, "the mutation must change cmd_ingest")
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_writer_serialized(self.config, mutated)
        self.assertIn("drop(_lock)", str(ctx.exception))


class AtomicPublishTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("publishes atomically", check_atomic_publish(self.config, self.store_src))

    def test_non_atomic_publish_is_caught(self) -> None:
        # A copy (or any non-atomic in-place write) would let a concurrent reader see a torn store.
        mutated = self.store_src.replace(
            "fs::rename(&tmp_path, &final_path)", "fs::copy(&tmp_path, &final_path)", 1
        )
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_atomic_publish(self.config, mutated)
        self.assertIn("fs::rename(", str(ctx.exception))


class FailClosedReadTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("reads fail-closed", check_fail_closed_read(self.config, self.store_src))

    def test_dropped_checksum_guard_is_caught(self) -> None:
        # Without the checksum-first guard a torn read would yield a partial store, not an error.
        mutated = self.store_src.replace("if checksum(body) != stored_checksum {", "if false {", 1)
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_fail_closed_read(self.config, mutated)
        self.assertIn("checksum", str(ctx.exception).lower())


class SingleWriterLockTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("single-writer guard", check_single_writer_lock(self.config, self.store_src))

    def test_non_exclusive_lock_create_is_caught(self) -> None:
        # Dropping O_EXCL (create_new) would let a second writer silently share the lock.
        mutated = self.store_src.replace(".create_new(true)", ".create(true)", 1)
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_single_writer_lock(self.config, mutated)
        self.assertIn("create_new(true)", str(ctx.exception))


class LoadTestPresentTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("Load test", check_load_test_present(self.config, self.load_test_src))

    def test_gutted_load_test_is_caught(self) -> None:
        # A Load test that no longer spawns concurrent threads is not a load test. Remove EVERY
        # occurrence (the token also appears in a comment) so it is genuinely absent.
        mutated = self.load_test_src.replace("thread::scope", "sequential_only")
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_load_test_present(self.config, mutated)
        self.assertIn("thread::scope", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        self.assertIn("free of all", check_vendor_isolation(self.config, self.store_src))

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.store_src + "\n// records pulled straight from databento under the hood\n"
        with self.assertRaises(ConcurrentReadCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("databento", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("concurrent_read_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("concurrent_read_check.shutil.which", return_value=None):
            with self.assertRaises(ConcurrentReadCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_static_evidence_is_nine_items(self) -> None:
        self.assertEqual(len(assert_concurrent_read_static(load_config(), ROOT)), 9)

    def test_run_checks_emits_ten_items(self) -> None:
        # 9 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 10)


class BehavioralConcurrentReadTest(unittest.TestCase):
    """End-to-end: run a real data016_ingest_cli writer concurrently with inspect + query readers and
    assert every concurrent read succeeds, sees the previously-ingested seed, and is never corrupted."""

    WRITES = 6
    READ_ROUNDS = 12

    @staticmethod
    def _cargo() -> str | None:
        return shutil.which("cargo")

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)

    def test_concurrent_ingest_and_reads_are_safe(self) -> None:
        cargo = self._cargo()
        if cargo is None:
            self.skipTest("cargo not on PATH")
        build = self._run(
            cargo, "build", "-q", "-p", "atp-data",
            "--bin", "data016_ingest_cli", "--bin", "data007_query_cli",
        )
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        ingest_bin = str(ROOT / "target" / "debug" / "data016_ingest_cli")
        query_bin = str(ROOT / "target" / "debug" / "data007_query_cli")
        seed_ts = 1_700_000_000

        with tempfile.TemporaryDirectory() as tmp:
            # Seed the previously-ingested ("completed") data.
            seed = self._run(ingest_bin, "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init")
            self.assertEqual(seed.returncode, 0, seed.stdout + seed.stderr)

            errors: list[str] = []

            def writer() -> None:
                for i in range(1, self.WRITES + 1):
                    res = self._run(
                        ingest_bin, "ingest", "--dir", tmp,
                        "--kind", "daily-equity-bar", "--event-ts", str(seed_ts + i),
                    )
                    if res.returncode != 0:
                        errors.append(f"writer ingest {i} failed: {res.stdout}{res.stderr}")

            def reader() -> None:
                for _ in range(self.READ_ROUNDS):
                    # inspect: a lock-free read; must always parse and never drop below the seed.
                    insp = self._run(ingest_bin, "inspect", "--dir", tmp)
                    if insp.returncode != 0:
                        errors.append(f"inspect read failed: {insp.stdout}{insp.stderr}")
                        continue
                    lens = [ln for ln in insp.stdout.splitlines() if ln.startswith("store_len:")]
                    if not lens or int(lens[0].split(":", 1)[1]) < 2:
                        errors.append(f"inspect saw fewer than the seed records: {insp.stdout!r}")
                    # query: the source-neutral read path the named consumers use.
                    q = self._run(
                        query_bin, "query", "--dir", tmp,
                        "--symbol", "AAPL", "--resolution", "1d", "--start", "0", "--end", "9999999999",
                    )
                    if q.returncode != 0:
                        errors.append(f"query read failed: {q.stdout}{q.stderr}")
                        continue
                    mc = [ln for ln in q.stdout.splitlines() if ln.startswith("match_count:")]
                    if not mc or int(mc[0].split(":", 1)[1]) < 1:
                        errors.append(f"query lost the seed record mid-ingestion: {q.stdout!r}")

            threads = [threading.Thread(target=writer)] + [
                threading.Thread(target=reader) for _ in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], "; ".join(errors))

            # After the writer finishes, the catalog holds the seed + every ingested date.
            final = self._run(ingest_bin, "inspect", "--dir", tmp)
            self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
            store_len = next(
                int(ln.split(":", 1)[1])
                for ln in final.stdout.splitlines()
                if ln.startswith("store_len:")
            )
            self.assertEqual(store_len, (1 + self.WRITES) * 2, final.stdout)


if __name__ == "__main__":
    unittest.main()
