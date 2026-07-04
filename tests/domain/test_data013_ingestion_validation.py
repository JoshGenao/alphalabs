"""SRS-DATA-013 / SyRS SYS-77 / ERR-5 — ingested market and options records are validated before the
primary write; records failing a structural / range / duplicate / required-field check are
quarantined and NOT written to primary tables, and the alert carries the count and nature of the
quarantined records.

L7 domain (safety) test. The acceptance criterion's safety core is data integrity at the ingestion
boundary: an invalid record (a crossed OHLC bar, a missing OHLCV field, a duplicate key, a
malformed option snapshot) that reached the primary tables would silently corrupt every downstream
consumer — a backtest fill, a factor computation, a paper/live trading decision. So the invariant is
that the primary store contains EXACTLY the valid records and NONE of the quarantined ones, while the
operator is told how many were rejected and why. This test proves the invariant from three angles:

  1. Behavioral (Rust) — it shells out to the Rust L5 integration tests
     ``crates/atp-data/tests/srs_data_013_ingestion_validation.rs`` (quarantine-and-continue: the
     valid subset is written, the quarantined records are absent, the counts-and-reasons summary is
     exact, and corporate-action COVERAGE is refused fail-closed).

  2. Behavioral (operator CLI) — it drives ``data013_ingestion_validation_cli`` over a real temp
     SSD/NAS tier: ``ingest`` reports one quarantine per SYS-77 rule and 4 valid writes, then
     ``inspect`` confirms the persisted primary store holds only the valid records — the quarantined
     symbols/contract are absent.

  3. Scope honesty — it pins that ``feature_list.json`` keeps SRS-DATA-013 ``passes:false``: the
     data-layer validation core is verified here, but the AC's "dashboard and notification alerts"
     display is deferred to the unbuilt SRS-UI-001 / SRS-NOTIF-001 (per the contract's ``deferred``),
     so this slice cannot silently over-claim the requirement end to end.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]

# The six SyRS SYS-77 rule categories (a..f) as their wire strings.
REASONS = (
    "RANGE_VIOLATION",
    "OHLC_OUT_OF_BAND",
    "NEGATIVE_VOLUME",
    "NULL_REQUIRED_FIELD",
    "DUPLICATE_RECORD",
    "OPTION_FIELD_MISSING",
)


# --------------------------------------------------------------------------- #
# Behavioral: the Rust L5 integration tests.
# --------------------------------------------------------------------------- #


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run the Rust ingestion-validation path")
    return cargo


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _cargo(),
            "test",
            "-p",
            "atp-data",
            "--test",
            "srs_data_013_ingestion_validation",
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


def test_rust_mixed_batch_quarantines_invalid_and_writes_only_valid() -> None:
    _assert_one_passed(
        _run_cargo_test("mixed_batch_quarantines_invalid_and_writes_only_valid"),
        "SRS-DATA-013 quarantine-and-continue (valid written, invalid absent)",
    )


def test_rust_duplicate_within_batch_is_quarantined_not_written() -> None:
    _assert_one_passed(
        _run_cargo_test("duplicate_within_batch_is_quarantined_not_written"),
        "SRS-DATA-013 within-batch duplicate quarantined",
    )


def test_rust_coverage_kind_is_refused_fail_closed() -> None:
    _assert_one_passed(
        _run_cargo_test("coverage_kind_is_refused_fail_closed"),
        "SRS-DATA-013 corporate-action COVERAGE refused fail-closed",
    )


# --------------------------------------------------------------------------- #
# Behavioral: the operator CLI workflow over a real temp SSD/NAS tier.
# --------------------------------------------------------------------------- #


def _build_cli() -> Path:
    build = subprocess.run(
        [_cargo(), "build", "-q", "-p", "atp-data", "--bin", "data013_ingestion_validation_cli"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"building data013 CLI failed:\n{build.stderr}"
    binary = REPO_ROOT / "target" / "debug" / "data013_ingestion_validation_cli"
    assert binary.exists(), f"CLI binary missing at {binary}"
    return binary


def _run_cli(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _fields(stdout: str) -> dict[str, str]:
    """Parse single-valued `key:value` lines (the first colon splits key/value)."""
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        if ":" in line and not line.startswith("record:"):
            key, _, value = line.partition(":")
            fields[key] = value
    return fields


def _record_lines(stdout: str) -> list[str]:
    """The `record:<kind>:<symbol>[:<contract>]` lines from `inspect`."""
    return [line for line in stdout.splitlines() if line.startswith("record:")]


def test_cli_quarantines_and_writes_only_valid_with_counts_and_reasons() -> None:
    binary = _build_cli()
    with tempfile.TemporaryDirectory(prefix="atp_data013_l7_") as tmp:
        ssd = str(Path(tmp) / "ssd")
        nas = str(Path(tmp) / "nas")
        Path(nas).mkdir(parents=True, exist_ok=True)  # provisioned NAS -> Synced, not Degraded

        ingest = _run_cli(binary, "ingest", "--ssd", ssd, "--nas", nas)
        assert ingest.returncode == 0, f"ingest failed:\n{ingest.stdout}\n{ingest.stderr}"
        fields = _fields(ingest.stdout)

        # The count of quarantined records, and its nature (one per SYS-77 rule).
        assert fields["records_in"] == "10"
        assert fields["valid_written"] == "4", "the four well-formed records are written"
        assert fields["quarantined_total"] == "6", "one record quarantined per SYS-77 rule"
        for reason in REASONS:
            assert fields[f"count_{reason}"] == "1", (
                f"exactly one {reason} quarantined; got {fields.get(f'count_{reason}')}"
            )
        # The valid subset is SSD-committed and NAS-synced (not lost).
        assert fields["ssd_inserted"] == "4"
        assert fields["nas_sync"] == "synced"

        # The primary store contains ONLY the valid records — the quarantined ones are absent.
        inspect = _run_cli(binary, "inspect", "--ssd", ssd)
        assert inspect.returncode == 0, f"inspect failed:\n{inspect.stdout}\n{inspect.stderr}"
        ifields = _fields(inspect.stdout)
        assert ifields["store_len"] == "4", "no quarantined record reached primary storage"

        records = "\n".join(_record_lines(inspect.stdout))
        # Valid records present:
        assert "record:daily-equity-bar:AAPL" in records
        assert "record:daily-equity-bar:MSFT" in records
        assert "240119C00150000" in records and "240119P00150000" in records
        # Quarantined records absent (the malformed daily bars + the malformed option contract):
        for absent in ("TSLA", "NVDA", "AMZN", "META", "240119C00160000"):
            assert absent not in records, (
                f"quarantined record {absent} must NOT reach primary storage"
            )


# --------------------------------------------------------------------------- #
# Scope honesty: SRS-DATA-013 stays passes:false (dashboard/notification display deferred).
# --------------------------------------------------------------------------- #


def test_feature_stays_passes_false_pending_dashboard_notification_e2e() -> None:
    features = json.loads((REPO_ROOT / "feature_list.json").read_text(encoding="utf-8"))
    entry = next(f for f in features if f["id"] == "SRS-DATA-013")
    assert entry["passes"] is False, (
        "SRS-DATA-013 must stay passes:false until the dashboard + notification alert display "
        "(SRS-UI-001 / SRS-NOTIF-001) is verified end to end"
    )
