"""L7 domain safety test for SRS-DATA-010 (SSD storage eviction policy).

The trading-safety invariants — proven at the operator-CLI boundary over REAL on-disk fixtures, not
just structurally:

  * enforce NEVER physically evicts hot runtime data, and when the high-water mark cannot be met
    without evicting pinned/hot data it leaves the mark breached and exits non-zero (fail-safe) rather
    than ever removing protected data;
  * enforce fails closed without an explicit protection source (so a deployment cannot silently treat
    "no live feed wired" as "evict everything");
  * a corrupt access journal makes the recency read fail closed (eviction refuses on an unreadable
    recency signal);
  * a malformed protection-inputs file and a degenerate high-water config are rejected fail-closed.

(The full cross-tier ordering + live/recent protection proof over arbitrary fixtures lives in the Rust
L4 integration test ``crates/atp-data/tests/srs_data_010_eviction.rs``; this file adds the Python
operator-boundary safety net.)
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "target" / "debug"
EVICT_CLI = TARGET / "data010_eviction_cli"
TIER_CLI = TARGET / "data008_tier_cli"


def _ensure_binaries() -> None:
    """Build the CLIs once if they are not already present (the CI gate builds them first)."""
    missing = [
        ("data010_eviction_cli", EVICT_CLI),
        ("data008_tier_cli", TIER_CLI),
    ]
    to_build = [name for name, path in missing if not path.exists()]
    if not to_build or shutil.which("cargo") is None:
        return
    for name in to_build:
        subprocess.run(
            ["cargo", "build", "-p", "atp-data", "--bin", name],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )


def _run(cli: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(cli), *args], capture_output=True, text=True)


def _fields(stdout: str) -> dict:
    out = {}
    for line in stdout.splitlines():
        if ":" in line and not line.startswith("evict:"):
            key, value = line.split(":", 1)
            out[key] = value
    return out


class Data010EvictionSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ensure_binaries()
        if not EVICT_CLI.exists():
            raise unittest.SkipTest("data010_eviction_cli not built (cargo unavailable)")

    def _tiers(self) -> tuple[Path, Path]:
        base = Path(tempfile.mkdtemp(prefix="atp-data010-domain-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        ssd, nas = base / "ssd", base / "nas"
        ssd.mkdir()
        nas.mkdir()
        return ssd, nas

    def test_enforce_never_evicts_hot_data_and_reports_the_breach(self) -> None:
        if not TIER_CLI.exists():
            self.skipTest("data008_tier_cli not built")
        ssd, nas = self._tiers()

        # Ingest a real hot store (2 daily records at ts 1_699_990_000).
        ingest = _run(
            TIER_CLI,
            "ingest",
            "--ssd",
            str(ssd),
            "--nas",
            str(nas),
            "--kind",
            "daily-equity-bar",
            "--event-ts",
            "1699990000",
        )
        self.assertEqual(ingest.returncode, 0, ingest.stdout + ingest.stderr)
        store_file = ssd / "market_data.store"
        hot_before = store_file.read_bytes()

        # capacity 1 → target 0; usage 2 (both hot, inside the 90-day floor at --now → retention-pinned).
        # The mark cannot be met without evicting hot data → fail-safe: exit non-zero, evict nothing.
        enforce = _run(
            EVICT_CLI,
            "enforce",
            "--ssd",
            str(ssd),
            "--nas",
            str(nas),
            "--ssd-capacity",
            "1",
            "--now",
            "1699990500",
            "--assume-unprotected",
        )
        self.assertNotEqual(
            enforce.returncode, 0, "enforce must fail-safe rather than evict hot data"
        )
        fields = _fields(enforce.stdout)
        self.assertEqual(fields.get("cold_evicted"), "0")
        self.assertEqual(fields.get("reached_target"), "false")
        # Hot store byte-identical — enforce never opened the SSD primary.
        self.assertEqual(store_file.read_bytes(), hot_before, "hot data must be untouched")

    def test_enforce_fails_closed_without_a_protection_source(self) -> None:
        ssd, nas = self._tiers()
        run = _run(
            EVICT_CLI,
            "enforce",
            "--ssd",
            str(ssd),
            "--nas",
            str(nas),
            "--ssd-capacity",
            "10",
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("protection source", run.stderr)

    def test_enforce_with_assume_unprotected_over_empty_store_is_safe(self) -> None:
        ssd, nas = self._tiers()
        run = _run(
            EVICT_CLI,
            "enforce",
            "--ssd",
            str(ssd),
            "--nas",
            str(nas),
            "--ssd-capacity",
            "10",
            "--assume-unprotected",
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        fields = _fields(run.stdout)
        self.assertEqual(fields.get("cold_evicted"), "0")
        self.assertEqual(fields.get("reached_target"), "true")

    def test_corrupt_access_journal_fails_closed(self) -> None:
        ssd, nas = self._tiers()
        journal_dir = ssd / "access_journal"
        journal_dir.mkdir()
        # A complete (newline-terminated) but malformed line → Corrupt on read.
        (journal_dir / "access_journal.log").write_text("not-a-ts\tbacktest\tjob\tAAPL\n")
        run = _run(
            EVICT_CLI,
            "report",
            "--ssd",
            str(ssd),
            "--nas",
            str(nas),
            "--ssd-capacity",
            "10",
            "--use-journal",
        )
        self.assertNotEqual(run.returncode, 0, "a corrupt journal must fail the read closed")
        self.assertIn("fail", run.stderr.lower())

    def test_use_journal_over_an_unusable_journal_fails_closed(self) -> None:
        # A running job's recording fails OPEN (never breaks the read), so an unusable journal would
        # silently hold no recency evidence. Opting into --use-journal must fail closed if the journal
        # is not usable, rather than trust an empty read and evict recently-accessed data. Make the
        # journal path a FILE (not a directory) — a portable "unwritable" stand-in.
        ssd, nas = self._tiers()
        (ssd / "access_journal").write_text("i am a file, not a directory")
        run = _run(
            EVICT_CLI,
            "report",
            "--ssd",
            str(ssd),
            "--nas",
            str(nas),
            "--ssd-capacity",
            "10",
            "--use-journal",
        )
        self.assertNotEqual(run.returncode, 0, "an unusable journal must fail closed")
        self.assertIn("not usable", run.stderr)

    def test_malformed_protection_file_fails_closed(self) -> None:
        ssd, nas = self._tiers()
        prot = ssd.parent / "protect.txt"
        prot.write_text("live AAPL\nbogus XYZ\n")
        run = _run(
            EVICT_CLI,
            "report",
            "--ssd",
            str(ssd),
            "--nas",
            str(nas),
            "--ssd-capacity",
            "10",
            "--protection-inputs",
            str(prot),
        )
        self.assertNotEqual(run.returncode, 0)

    def test_degenerate_high_water_is_rejected(self) -> None:
        ssd, nas = self._tiers()
        for bad in ("0", "101"):
            run = _run(
                EVICT_CLI,
                "report",
                "--ssd",
                str(ssd),
                "--nas",
                str(nas),
                "--ssd-capacity",
                "10",
                "--high-water",
                bad,
            )
            self.assertNotEqual(
                run.returncode, 0, f"--high-water {bad} must be rejected fail-closed"
            )


if __name__ == "__main__":
    unittest.main()
