"""SRS-DATA-013 / SyRS SYS-77 — rejected records are quarantined in a separate, durable store.

L7 domain (safety) test, driven through the operator CLIs exactly as an operator would run them.
SYS-77 says records failing validation "shall be quarantined in a separate store" and the operator
alerted "with the count and nature of quarantined records". The safety core is that a rejected
record is never lost silently and never reaches primary storage:

  1. Every ingest records a run in ``<ssd>/quarantine`` that ``summary`` reads back with the same
     counts and reasons the ingest reported — through the quarantining CLI and through an ordinary
     ingest CLI (``data016_ingest_cli``), which now writes through the recorded tiered surface.
  2. A batch whose run cannot be recorded writes NOTHING to primary storage.
  3. Unknown is never empty: a missing SSD primary is an error, and a corrupt run is reported in its
     place as unreadable — never skipped, never counted as clean.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
REASONS = (
    "RANGE_VIOLATION",
    "OHLC_OUT_OF_BAND",
    "NEGATIVE_VOLUME",
    "NULL_REQUIRED_FIELD",
    "DUPLICATE_RECORD",
    "OPTION_FIELD_MISSING",
)


def _bin(name: str) -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot build the ingestion CLIs")
    build = subprocess.run(
        [cargo, "build", "-q", "-p", "atp-data", "--bin", name],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cargo build {name} failed:\n{build.stderr}"
    return REPO_ROOT / "target" / "debug" / name


def _run(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), *args], cwd=REPO_ROOT, check=False, capture_output=True, text=True
    )


def _fields(stdout: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key] = value
    return fields


def test_quarantining_ingest_is_read_back_with_the_same_counts_and_reasons() -> None:
    cli = _bin("data013_ingestion_validation_cli")
    with tempfile.TemporaryDirectory(prefix="atp_data013_qs_") as tmp:
        ssd, nas = Path(tmp) / "ssd", Path(tmp) / "nas"
        nas.mkdir()
        ingest = _run(cli, "ingest", "--ssd", str(ssd), "--nas", str(nas))
        assert ingest.returncode == 0, ingest.stderr
        reported = _fields(ingest.stdout)
        assert reported["quarantine_run_finalized"] == "true"

        summary = _run(cli, "summary", "--ssd", str(ssd))
        assert summary.returncode == 0, summary.stderr
        run = _fields(summary.stdout)
        assert run["total_runs"] == "1"
        assert run["run_0_id"] == reported["quarantine_run_id"]
        assert run["run_0_status"] == "readable"
        assert run["run_0_primary"] == "WRITTEN"
        assert run["run_0_written"] == reported["valid_written"] == "4"
        assert run["run_0_records_in"] == reported["records_in"] == "10"
        assert run["run_0_quarantined_total"] == reported["quarantined_total"] == "6"
        for reason in REASONS:
            assert run[f"run_0_count_{reason}"] == reported[f"count_{reason}"] == "1", reason


def test_an_ordinary_ingest_cli_records_its_run_too() -> None:
    # data016_ingest_cli is not a quarantine tool; it still may not reach primary unrecorded.
    data016 = _bin("data016_ingest_cli")
    data013 = _bin("data013_ingestion_validation_cli")
    with tempfile.TemporaryDirectory(prefix="atp_data013_qs_") as tmp:
        ssd = Path(tmp) / "ssd"
        ingest = _run(data016, "ingest", "--dir", str(ssd), "--kind", "daily-equity-bar", "--init")
        assert ingest.returncode == 0, ingest.stderr
        assert _fields(ingest.stdout)["quarantine_run_finalized"] == "true"
        run = _fields(_run(data013, "summary", "--ssd", str(ssd)).stdout)
        assert run["total_runs"] == "1"
        assert run["run_0_id"] == _fields(ingest.stdout)["quarantine_run_id"]
        assert run["run_0_primary"] == "WRITTEN"
        assert run["run_0_quarantined_total"] == "0"


def test_an_unrecordable_run_writes_nothing_to_primary() -> None:
    cli = _bin("data013_ingestion_validation_cli")
    with tempfile.TemporaryDirectory(prefix="atp_data013_qs_") as tmp:
        ssd, nas = Path(tmp) / "ssd", Path(tmp) / "nas"
        ssd.mkdir()
        nas.mkdir()
        (ssd / "quarantine").write_text("a file where the store directory belongs")
        ingest = _run(cli, "ingest", "--ssd", str(ssd), "--nas", str(nas))
        assert ingest.returncode != 0, "an unrecordable quarantine must refuse the batch"
        assert "quarantine store" in ingest.stderr
        inspect = _fields(_run(cli, "inspect", "--ssd", str(ssd)).stdout)
        assert inspect["store_len"] == "0", "nothing reached primary storage"


def test_a_missing_primary_is_an_error_not_an_empty_history() -> None:
    cli = _bin("data013_ingestion_validation_cli")
    with tempfile.TemporaryDirectory(prefix="atp_data013_qs_") as tmp:
        summary = _run(cli, "summary", "--ssd", str(Path(tmp) / "not-mounted"))
        assert summary.returncode != 0
        assert "total_runs" not in summary.stdout
        assert "SSD primary directory is missing" in summary.stderr


def test_a_corrupt_run_is_reported_in_place_never_skipped() -> None:
    cli = _bin("data013_ingestion_validation_cli")
    with tempfile.TemporaryDirectory(prefix="atp_data013_qs_") as tmp:
        ssd, nas = Path(tmp) / "ssd", Path(tmp) / "nas"
        nas.mkdir()
        assert _run(cli, "ingest", "--ssd", str(ssd), "--nas", str(nas)).returncode == 0
        (run_file,) = (ssd / "quarantine").glob("*.qrun")
        run_file.write_text(run_file.read_text().replace("NEGATIVE_VOLUME", "NEGATIVE_VOLUMF", 1))
        summary = _run(cli, "summary", "--ssd", str(ssd))
        assert summary.returncode == 0, summary.stderr
        run = _fields(summary.stdout)
        assert run["total_runs"] == "1"
        assert run["run_0_status"] == "unreadable"
        assert run["run_0_error"] == "quarantine run checksum mismatch"
        assert "run_0_quarantined_total" not in run, "an unreadable run carries no counts"
