"""L3 contract test for the SRS-REL-001 availability measurement substrate.

Two layers of drift protection (mirrors ``tools/perf_measurement_check.py``):

* the imported ``atp_reliability`` constants / enums / error classes match the
  ``availability_measurement_contract`` block in ``runtime_services.json``;
* the distinctive NFR-R1 phrases the contract pins (99.9%, rolling 30-day period,
  market-holiday exclusion, the SYS-75 restart exclusion, the 1.17-minute
  approximation) are actually present in ``docs/SyRS_v0.7.md`` — so the JSON cannot
  silently diverge from the spec.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[0].parent
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_reliability import availability as av  # noqa: E402

pytestmark = [pytest.mark.contract]

_CONTRACT = json.loads((ROOT / "architecture" / "runtime_services.json").read_text())[
    "availability_measurement_contract"
]


def test_target_and_window_constants_match() -> None:
    assert _CONTRACT["target_per_mille"] == av.DEFAULT_TARGET_PER_MILLE == 999
    assert _CONTRACT["rolling_window_days"] == av.DEFAULT_ROLLING_WINDOW_DAYS == 30


def test_rolling_window_is_enforced() -> None:
    # The contract must pin that PASS requires EXACTLY the rolling period (a rolling
    # metric, not a >=-duration average), and the default target enforces it.
    assert _CONTRACT["rolling_window_enforced"] is True
    assert _CONTRACT["rolling_window_exact"] is True
    assert av.AvailabilityTarget().rolling_window_days == 30
    assert av.SECONDS_PER_DAY == 86_400


def test_market_hours_boundary_matches() -> None:
    assert _CONTRACT["market_hours_boundary"] == av.MARKET_HOURS_BOUNDARY


def test_counting_causes_match() -> None:
    counting = {av.OutageCause(c) for c in _CONTRACT["outage_causes"]["counting"]}
    assert counting == set(av.COUNTING_CAUSES) == {av.OutageCause.HOST_UNPLANNED}


def test_excluded_causes_match() -> None:
    excluded = {av.OutageCause(c) for c in _CONTRACT["outage_causes"]["excluded"]}
    assert excluded == set(av.EXCLUDED_CAUSES)


def test_cli_does_not_expose_a_target_override() -> None:
    # The SRS-REL-001 CLI must not let an operator weaken the objective below 99.9%
    # and still emit requirement=SRS-REL-001 PASS.
    from atp_reliability.cli import build_parser

    assert "--target-per-mille" not in build_parser().format_help()


def test_no_public_coverage_synthesis_helper() -> None:
    # Regression (adversarial review): the package must expose NO helper that turns
    # sessions into coverage — that would let a caller fabricate positive coverage and
    # mint a certifying PASS without the deferred host-liveness feed.
    import atp_reliability
    from atp_reliability import evidence

    assert not hasattr(atp_reliability, "covered_from_sessions")
    assert not hasattr(evidence, "covered_from_sessions")
    assert "covered_from_sessions" not in atp_reliability.__all__
    assert "covered_from_sessions" not in evidence.__all__
    # no public evidence symbol synthesizes coverage from sessions.
    assert not any(
        "covered" in name.lower() and "session" in name.lower() for name in evidence.__all__
    )


def test_all_outage_causes_accounted_for() -> None:
    groups = _CONTRACT["outage_causes"]
    listed = set(groups["counting"]) | set(groups["excluded"]) | set(groups["non_counting"])
    assert listed == {c.value for c in av.OutageCause}


def test_verdicts_match() -> None:
    assert set(_CONTRACT["verdicts"]) == {v.value for v in av.Verdict}


def test_error_types_exist() -> None:
    for name in _CONTRACT["error_types"]:
        cls = getattr(av, name)
        assert issubclass(cls, av.AvailabilityError)


def test_taxonomy_guard_fails_closed_and_is_not_assert_stripped() -> None:
    # The SYS-61 taxonomy guard must raise (not a bare assert, which python -O strips)
    # when a mapped event type disappears — otherwise the adapter would silently drop
    # downtime records. It must also accept the real taxonomy.
    from atp_logging.records import EVENT_TYPES_BY_SOURCE, Source
    from atp_reliability.evidence import _verify_taxonomy

    _verify_taxonomy(EVENT_TYPES_BY_SOURCE[Source.IB_GATEWAY])  # real taxonomy: no raise
    with pytest.raises(RuntimeError, match="IB_GATEWAY taxonomy drift"):
        _verify_taxonomy(("CONNECT",))  # DISCONNECT missing


def test_module_paths_exist() -> None:
    assert (ROOT / _CONTRACT["engine_module"]).is_file()
    assert (ROOT / _CONTRACT["evidence_module"]).is_file()
    assert (ROOT / _CONTRACT["cli_module"]).is_file()


def test_session_length_constants_match_calendar_semantics() -> None:
    # regular 6.5h session and 3.5h early-close, as the boundary phrase implies.
    assert _CONTRACT["regular_session_seconds"] == 6 * 3600 + 1800 == 23_400
    assert _CONTRACT["early_close_session_seconds"] == 3 * 3600 + 1800 == 12_600


def test_nfr_r1_phrases_present_in_syrs() -> None:
    syrs = (ROOT / _CONTRACT["syrs_doc"]).read_text()
    for phrase in [
        "99.9%",
        "rolling 30-day period",
        "excluding market holidays",
        "scheduled IB Gateway daily restart",
        "1.17 minutes",
    ]:
        assert phrase in syrs, f"NFR-R1 phrase missing from SyRS: {phrase!r}"


def test_reconciliation_note_pins_the_binding_gate() -> None:
    note = _CONTRACT["one_seventeen_minute_reconciliation"]
    assert "non-binding" in note
    assert "23.4 s" in note


def test_deferred_names_the_host_liveness_feed_and_30_day_proof() -> None:
    deferred = " ".join(_CONTRACT["deferred"]).lower()
    assert "host-liveness" in deferred or "host liveness" in deferred
    assert "30" in deferred and "passes:false" in deferred


def _full_coverage_fixture() -> dict:
    # A certifying date-based fixture: a >=30-day range, full coverage of every
    # calendar-derived session, one small host-unplanned outage.
    import datetime as dt

    from atp_reliability.evidence import market_sessions
    from atp_strategy.calendar import UsEquityTradingCalendar

    cal = UsEquityTradingCalendar.for_exchange("NYSE")
    _, _, sessions = market_sessions(cal, dt.date(2026, 1, 5), dt.date(2026, 2, 3))
    covered = [[s.start_ns, s.end_ns] for s in sessions]
    sec = av.NS_PER_SECOND
    downtime = [
        [sessions[0].start_ns + 100 * sec, sessions[0].start_ns + 120 * sec, "host_unplanned"]
    ]
    return {
        "exchange": "NYSE",
        "start_date": "2026-01-05",  # exactly 30 calendar days
        "end_date": "2026-02-03",
        "covered": covered,
        "downtime": downtime,
    }


def _run_cli(fpath: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "atp_reliability", "--fixture", str(fpath)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT)},
        check=False,
        capture_output=True,
        text=True,
    )


def test_documented_cli_entry_is_actually_runnable(tmp_path: Path) -> None:
    # The contract's documented invocation must run as-written (independent of
    # pytest's pythonpath) and produce a certifying artifact over a 30-day fixture.
    assert _CONTRACT["cli_entry"] == "PYTHONPATH=python python -m atp_reliability"
    fpath = tmp_path / "evidence.json"
    fpath.write_text(json.dumps(_full_coverage_fixture()))
    result = _run_cli(fpath)
    assert result.returncode == 0, result.stderr
    assert "verdict:PASS" in result.stdout


def test_fixture_with_a_missing_session_day_cannot_certify(tmp_path: Path) -> None:
    # Regression (adversarial review): drop coverage for ONE trading day of a 30-day
    # fixture -> that calendar-derived session is unmeasured -> INCONCLUSIVE, not PASS.
    fixture = _full_coverage_fixture()
    fixture["covered"] = fixture["covered"][1:]  # drop the first trading day's coverage
    fpath = tmp_path / "evidence.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode != 0
    assert "verdict:INCONCLUSIVE" in result.stdout
    assert "verdict:PASS" not in result.stdout


def test_fixture_rejects_raw_sessions(tmp_path: Path) -> None:
    # Raw caller-supplied sessions are refused (they could understate the denominator).
    fpath = tmp_path / "bad.json"
    fpath.write_text(json.dumps({"sessions": [[0, 100]], "covered": []}))
    result = _run_cli(fpath)
    assert result.returncode == 2  # refused
    assert "start_date" in result.stderr


@pytest.mark.parametrize("field", ["downtime", "covered", "excluded_windows"])
@pytest.mark.parametrize("bad", [None, False, 0, "", {}])
def test_fixture_malformed_optional_field_is_refused_not_certified(
    tmp_path: Path, field: str, bad: object
) -> None:
    # Regression (adversarial review): a falsy-but-malformed optional field (null/false/
    # 0/""/{}) must be REFUSED (exit 2), not silently coerced to "no evidence" and
    # certified as clean. Otherwise a broken generator could mint a false PASS with the
    # downtime/coverage field dropped.
    from atp_reliability.cli import run

    fixture = _full_coverage_fixture()
    fixture[field] = bad
    fpath = tmp_path / "bad.json"
    fpath.write_text(json.dumps(fixture))
    code = run(["--fixture", str(fpath)])
    assert code == 2, (field, bad)


def test_calendar_corrupt_log_store_is_refused_not_a_crash(tmp_path: Path) -> None:
    # Regression (adversarial review): a corrupt log store is degraded evidence -> the
    # CLI must refuse (exit 2) with a clear message, not emit an uncaught traceback.
    store = tmp_path / "system.jsonl"
    store.write_text("this is not valid json\n")  # a complete, corrupt line
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "atp_reliability",
            "--calendar",
            "--start",
            "2026-01-02",
            "--end",
            "2026-02-05",
            "--log-store",
            str(store),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "log store" in result.stderr and "Traceback" not in result.stderr
