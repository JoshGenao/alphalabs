"""L7 domain safety test for SRS-DATA-018 (scheduled NAS backup + validated recovery).

The data-loss invariants, proven at the operator-CLI boundary over REAL on-disk fixtures. A backup
subsystem earns trust by what it *refuses* to claim, so these tests are mostly about the negative
space:

  * an unproven archive never reads as a good one — `run`/`verify`/`restore` exit NON-ZERO on any
    verdict other than ``verified``, and an empty source is ``unverified``, not a vacuous success;
  * integrity is validated against the EXPORTED bytes, so a flipped byte in the archive is caught,
    and a corrupt SOURCE is refused rather than replicated and then declared verified;
  * the backup never mutates the thing it protects;
  * a target that is the NAS root, or nested inside it, is refused — a copy inside the source's
    failure domain is not a backup;
  * a cadence that could never satisfy the SYS-60 7-day RPO is refused at configuration time;
  * with no verified backup on record, `status` reports the RPO as NOT met and exits non-zero,
    rather than treating "no evidence" as compliance.

(The library-level proof over arbitrary fixtures lives in the Rust L4 test
``crates/atp-data/tests/srs_data_018_backup_recovery.rs`` and the module's inline unit tests; this
file adds the Python operator-boundary safety net that a scheduler would actually gate on.)
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "target" / "debug"
BACKUP_CLI = TARGET / "data018_backup_cli"
TIER_CLI = TARGET / "data008_tier_cli"

STORE_FILENAME = "market_data.store"
LEDGER_FILENAME = "backup_ledger.log"
NOW = 1_700_000_000
SECONDS_PER_DAY = 86_400


def _ensure_binaries() -> None:
    """Build the CLIs once if they are not already present (the CI gate builds them first).

    ``data018_backup_cli`` lives in ``atp-simulation``, not ``atp-data``: it is the composition root
    that injects the real ``BacktestResultStore`` decoder into the ``atp-data`` backup engine, which
    a lower layer could not do.
    """
    wanted = [
        ("atp-simulation", "data018_backup_cli", BACKUP_CLI),
        ("atp-data", "data008_tier_cli", TIER_CLI),
    ]
    to_build = [(pkg, name) for pkg, name, path in wanted if not path.exists()]
    if not to_build or shutil.which("cargo") is None:
        return
    for pkg, name in to_build:
        subprocess.run(
            ["cargo", "build", "-p", pkg, "--bin", name],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )


def _run(cli: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(cli), *args], capture_output=True, text=True)


class Data018BackupSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ensure_binaries()

    def setUp(self) -> None:
        if not BACKUP_CLI.exists():
            self.skipTest("data018_backup_cli not built")
        self._tmp = tempfile.mkdtemp(prefix="atp-data018-domain-")
        self.root = Path(self._tmp)
        self.nas = self.root / "nas"
        self.target = self.root / "usb"
        self.nas.mkdir(parents=True, exist_ok=True)
        # "Mount" the external target. The backup deliberately refuses to create a missing target
        # root — see test_an_absent_target_mount_is_refused_rather_than_created_locally.
        self.target.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Fixtures
    # ------------------------------------------------------------------ #

    def _seed_nas_unit(self, unit: str) -> Path:
        """Ingest a real market-data store into ``<nas>/<unit>`` via the DATA-008 tier CLI."""
        if not TIER_CLI.exists():
            self.skipTest("data008_tier_cli not built")
        ssd = self.root / "ssd" / unit
        nas_unit = self.nas / unit
        ssd.mkdir(parents=True, exist_ok=True)
        nas_unit.mkdir(parents=True, exist_ok=True)
        ingest = _run(
            TIER_CLI,
            "ingest",
            "--ssd",
            str(ssd),
            "--nas",
            str(nas_unit),
            "--kind",
            "daily-equity-bar",
            "--event-ts",
            str(NOW - SECONDS_PER_DAY),
        )
        self.assertEqual(ingest.returncode, 0, ingest.stdout + ingest.stderr)
        store = nas_unit / STORE_FILENAME
        self.assertTrue(store.is_file(), f"tier ingest produced no NAS store at {store}")
        return nas_unit

    def _backup(self, *extra: str) -> subprocess.CompletedProcess:
        return _run(
            BACKUP_CLI,
            "run",
            "--nas",
            str(self.nas),
            "--target",
            str(self.target),
            "--now",
            str(NOW),
            *extra,
        )

    # ------------------------------------------------------------------ #
    # The happy path exists, so the refusals below mean something
    # ------------------------------------------------------------------ #

    def test_a_verified_backup_exports_the_unit_and_exits_zero(self) -> None:
        self._seed_nas_unit("equities")
        result = self._backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verified", result.stdout)
        self.assertTrue((self.target / "equities" / STORE_FILENAME).is_file())
        self.assertTrue((self.target / LEDGER_FILENAME).is_file())

    # ------------------------------------------------------------------ #
    # An unproven archive must never read as a good one
    # ------------------------------------------------------------------ #

    def test_an_empty_source_is_unverified_and_exits_non_zero(self) -> None:
        # No units seeded. "Nothing to back up" must not render as "everything is backed up",
        # or a misconfigured NAS path would report a green backup forever.
        result = self._backup()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("unverified", result.stdout)

    def test_a_corrupt_archive_fails_verification_and_exits_non_zero(self) -> None:
        self._seed_nas_unit("equities")
        self.assertEqual(self._backup().returncode, 0)

        archived = self.target / "equities" / STORE_FILENAME
        text = archived.read_text()
        self.assertIn("AAPL", text, "fixture assumption: the tier CLI ingests AAPL")
        archived.write_text(text.replace("AAPL", "AAPX", 1))

        verify = _run(BACKUP_CLI, "verify", "--target", str(self.target))
        self.assertNotEqual(verify.returncode, 0, verify.stdout + verify.stderr)
        self.assertIn("corrupt", verify.stdout)

    def test_a_corrupt_source_is_refused_and_never_reaches_the_target(self) -> None:
        unit = self._seed_nas_unit("equities")
        store = unit / STORE_FILENAME
        store.write_text(store.read_text().replace("AAPL", "AAPX", 1))

        result = self._backup()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("corrupt", result.stdout)
        self.assertFalse(
            (self.target / "equities" / STORE_FILENAME).exists(),
            "a corrupt source must not be replicated over a good archive",
        )

    def test_a_corrupt_unit_never_advances_the_rpo_ledger(self) -> None:
        unit = self._seed_nas_unit("equities")
        store = unit / STORE_FILENAME
        store.write_text(store.read_text().replace("AAPL", "AAPX", 1))
        self._backup()

        ledger = self.target / LEDGER_FILENAME
        recorded = ledger.read_text() if ledger.exists() else ""
        self.assertNotIn(
            "equities",
            recorded,
            "an unproven unit must not be certified as backed up",
        )
        status = _run(
            BACKUP_CLI,
            "status",
            "--nas",
            str(self.nas),
            "--target",
            str(self.target),
            "--now",
            str(NOW),
        )
        self.assertNotEqual(status.returncode, 0, status.stdout)
        self.assertIn("within rpo      : NO", status.stdout)

    def test_a_partially_failed_run_never_reads_as_within_rpo(self) -> None:
        # The subtlest false green: one unit verifies and lands in the ledger while another is
        # corrupt. Judging the RPO on ledger contents alone would then report "backed up 0 days
        # ago, no backup due" while the corrupt unit was never exported at all. The assessment is
        # made against the units actually present on the NAS, so the gap stays visible.
        self._seed_nas_unit("good")
        bad = self._seed_nas_unit("bad")
        store = bad / STORE_FILENAME
        store.write_text(store.read_text().replace("AAPL", "AAPX", 1))

        run = self._backup()
        self.assertNotEqual(run.returncode, 0, run.stdout)

        status = _run(
            BACKUP_CLI,
            "status",
            "--nas",
            str(self.nas),
            "--target",
            str(self.target),
            "--now",
            str(NOW),
        )
        self.assertNotEqual(status.returncode, 0, status.stdout)
        self.assertIn("within rpo      : NO", status.stdout)
        self.assertIn("backup due      : yes", status.stdout)
        self.assertIn("bad", status.stdout, "the unprotected unit must be named")

        # ...and it does not age into compliance a day later either.
        later = _run(
            BACKUP_CLI,
            "status",
            "--nas",
            str(self.nas),
            "--target",
            str(self.target),
            "--now",
            str(NOW + SECONDS_PER_DAY),
        )
        self.assertNotEqual(later.returncode, 0, later.stdout)
        self.assertIn("within rpo      : NO", later.stdout)

    def test_a_unit_added_after_a_green_run_reopens_the_rpo_breach(self) -> None:
        # A complete run, then a new data family appears on the NAS and has never been backed up.
        self._seed_nas_unit("equities")
        self.assertEqual(self._backup().returncode, 0)

        def status_at(now: int) -> subprocess.CompletedProcess:
            return _run(
                BACKUP_CLI,
                "status",
                "--nas",
                str(self.nas),
                "--target",
                str(self.target),
                "--now",
                str(now),
            )

        self.assertEqual(status_at(NOW).returncode, 0)
        self._seed_nas_unit("options")
        after = status_at(NOW + SECONDS_PER_DAY)
        self.assertNotEqual(after.returncode, 0, after.stdout)
        self.assertIn("within rpo      : NO", after.stdout)
        self.assertIn("options", after.stdout)

    def test_status_fails_closed_when_the_archive_is_wiped_but_the_ledger_survives(self) -> None:
        # The ledger records that a backup happened once; the archive is what a recovery would
        # actually read. If the external media is wiped while backup_ledger.log survives, a
        # ledger-only status would certify a recovery point that no longer exists.
        self._seed_nas_unit("equities")
        self.assertEqual(self._backup().returncode, 0)

        def status_at(now: int) -> subprocess.CompletedProcess:
            return _run(
                BACKUP_CLI,
                "status",
                "--nas",
                str(self.nas),
                "--target",
                str(self.target),
                "--now",
                str(now),
            )

        self.assertEqual(status_at(NOW).returncode, 0)

        shutil.rmtree(self.target / "equities")
        self.assertTrue((self.target / LEDGER_FILENAME).is_file(), "ledger must survive")

        gone = status_at(NOW)
        self.assertNotEqual(gone.returncode, 0, gone.stdout)
        self.assertIn("within rpo      : NO", gone.stdout)
        self.assertIn("archive verifies: unverified", gone.stdout)

    def test_status_reports_a_backup_as_due_when_the_archive_no_longer_verifies(self) -> None:
        # Reporting "backup due: no" while the media is wiped would read as "nothing to do" at
        # exactly the moment there is everything to do — and would contradict `run`, which does
        # re-export in that state regardless of the cadence.
        self._seed_nas_unit("equities")
        self.assertEqual(self._backup().returncode, 0)
        shutil.rmtree(self.target / "equities")

        status = _run(
            BACKUP_CLI,
            "status",
            "--nas",
            str(self.nas),
            "--target",
            str(self.target),
            "--now",
            str(NOW),
        )
        self.assertNotEqual(status.returncode, 0, status.stdout)
        self.assertIn("backup due      : yes", status.stdout)
        self.assertIn("archive does not verify", status.stdout)

        # ...and `run` honours it: the cadence has not elapsed, but it exports again anyway.
        rerun = self._backup()
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertTrue((self.target / "equities" / STORE_FILENAME).is_file())

    def test_status_fails_closed_when_the_archive_is_corrupted_but_the_ledger_survives(
        self,
    ) -> None:
        self._seed_nas_unit("equities")
        self.assertEqual(self._backup().returncode, 0)

        archived = self.target / "equities" / STORE_FILENAME
        archived.write_text(archived.read_text().replace("AAPL", "AAPX", 1))

        status = _run(
            BACKUP_CLI,
            "status",
            "--nas",
            str(self.nas),
            "--target",
            str(self.target),
            "--now",
            str(NOW),
        )
        self.assertNotEqual(status.returncode, 0, status.stdout)
        self.assertIn("archive verifies: corrupt", status.stdout)
        self.assertIn("within rpo      : NO", status.stdout)
        # The ledger itself is still fresh — the failure comes from re-reading the media.
        self.assertIn("ledger freshness: within rpo", status.stdout)

    # ------------------------------------------------------------------ #
    # The backup must not damage what it protects
    # ------------------------------------------------------------------ #

    def test_the_source_store_is_byte_identical_after_a_backup(self) -> None:
        unit = self._seed_nas_unit("equities")
        store = unit / STORE_FILENAME
        before = store.read_bytes()
        self._backup()
        self.assertEqual(before, store.read_bytes())

    def test_the_source_store_is_byte_identical_after_a_failed_backup(self) -> None:
        unit = self._seed_nas_unit("equities")
        store = unit / STORE_FILENAME
        store.write_text(store.read_text().replace("AAPL", "AAPX", 1))
        before = store.read_bytes()
        result = self._backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, store.read_bytes())

    # ------------------------------------------------------------------ #
    # Fail-closed configuration
    # ------------------------------------------------------------------ #

    def test_a_target_inside_the_nas_root_is_refused(self) -> None:
        self._seed_nas_unit("equities")
        result = _run(
            BACKUP_CLI,
            "run",
            "--nas",
            str(self.nas),
            "--target",
            str(self.nas / "inner"),
            "--now",
            str(NOW),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("failure domain", result.stderr)

    def test_a_target_aliasing_the_nas_root_is_refused(self) -> None:
        self._seed_nas_unit("equities")
        result = _run(
            BACKUP_CLI,
            "run",
            "--nas",
            str(self.nas),
            "--target",
            str(self.nas) + "/.",
            "--now",
            str(NOW),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("not a backup", result.stderr)

    def test_an_absent_target_mount_is_refused_rather_than_created_locally(self) -> None:
        # If the USB / secondary-NAS mount is not present, creating the path on demand would write
        # the "backup" onto the very machine whose loss it exists to survive — and it would verify,
        # because it would be verifying against itself.
        self._seed_nas_unit("equities")
        missing = self.root / "not-mounted"
        self.assertFalse(missing.exists())

        result = _run(
            BACKUP_CLI,
            "run",
            "--nas",
            str(self.nas),
            "--target",
            str(missing),
            "--now",
            str(NOW),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("degraded", result.stdout)
        self.assertFalse(
            missing.exists(),
            "the backup must not create the external target root on local disk",
        )

    def test_a_cadence_longer_than_the_rpo_is_refused(self) -> None:
        result = _run(
            BACKUP_CLI,
            "run",
            "--nas",
            str(self.nas),
            "--target",
            str(self.target),
            "--cadence-days",
            "14",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("recovery point objective", result.stderr)

    def test_an_unknown_flag_is_refused_rather_than_ignored(self) -> None:
        # A typo in a scheduled job's arguments must not silently change what the backup does.
        result = _run(
            BACKUP_CLI,
            "run",
            "--nas",
            str(self.nas),
            "--target",
            str(self.target),
            "--delete-source",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown flag", result.stderr)

    # ------------------------------------------------------------------ #
    # RPO honesty
    # ------------------------------------------------------------------ #

    def test_status_reports_no_evidence_as_out_of_rpo_not_as_compliant(self) -> None:
        result = _run(
            BACKUP_CLI,
            "status",
            "--nas",
            str(self.nas),
            "--target",
            str(self.target),
            "--now",
            str(NOW),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("never", result.stdout)
        self.assertIn("within rpo      : NO", result.stdout)
        self.assertIn("backup due      : yes", result.stdout)

    def test_rpo_lapses_once_the_archive_is_older_than_seven_days(self) -> None:
        self._seed_nas_unit("equities")
        self.assertEqual(self._backup().returncode, 0)

        def status_at(now: int) -> subprocess.CompletedProcess:
            return _run(
                BACKUP_CLI,
                "status",
                "--nas",
                str(self.nas),
                "--target",
                str(self.target),
                "--now",
                str(now),
            )

        at_ceiling = status_at(NOW + 7 * SECONDS_PER_DAY)
        self.assertEqual(at_ceiling.returncode, 0, at_ceiling.stdout)
        self.assertIn("within rpo      : yes", at_ceiling.stdout)

        past = status_at(NOW + 8 * SECONDS_PER_DAY)
        self.assertNotEqual(past.returncode, 0, past.stdout)
        self.assertIn("within rpo      : NO", past.stdout)

    # ------------------------------------------------------------------ #
    # Validated recovery
    # ------------------------------------------------------------------ #

    def test_restore_reproduces_the_archived_store(self) -> None:
        unit = self._seed_nas_unit("equities")
        self.assertEqual(self._backup().returncode, 0)

        dest = self.root / "recovered"
        restore = _run(BACKUP_CLI, "restore", "--target", str(self.target), "--dest", str(dest))
        self.assertEqual(restore.returncode, 0, restore.stdout + restore.stderr)
        self.assertIn("verified", restore.stdout)
        self.assertEqual(
            (dest / "equities" / STORE_FILENAME).read_bytes(),
            (unit / STORE_FILENAME).read_bytes(),
        )

    def test_verify_leaves_no_scratch_artefacts_inside_the_archive(self) -> None:
        self._seed_nas_unit("equities")
        self.assertEqual(self._backup().returncode, 0)
        before = sorted(p.name for p in self.target.iterdir())

        verify = _run(BACKUP_CLI, "verify", "--target", str(self.target))
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
        self.assertEqual(
            before,
            sorted(p.name for p in self.target.iterdir()),
            "verification must not leave artefacts in the archive it checks",
        )


if __name__ == "__main__":
    unittest.main()
