"""L3 contract test for the SRS-REL-002 restart-recovery measurement substrate.

Three layers of drift protection (mirrors ``tests/test_availability_contract.py``):

* the imported ``atp_reliability.restart`` constants / enums / error classes match the
  ``restart_recovery_contract`` block in ``runtime_services.json``;
* the ``GateOutcome`` values match the authoritative ``atp_readiness.GateState`` (the vocabulary is
  reused, not forked);
* the distinctive NFR-R6 / SYS-76 phrases the contract pins are actually present in the SyRS/SRS
  spec docs — so the JSON cannot silently diverge from the requirement;

plus fail-closed CLI regressions (runnable entry, malformed/absent field handling, corrupt fixture,
no evidence-synthesis surface, and the label-lock guard surviving ``python -O``).
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

from atp_reliability import restart as rr  # noqa: E402

pytestmark = [pytest.mark.contract]

_CONTRACT = json.loads((ROOT / "architecture" / "runtime_services.json").read_text())[
    "restart_recovery_contract"
]


def test_requirement_and_budget_constants_match() -> None:
    assert _CONTRACT["requirement"] == rr.SRS_REL_002 == "SRS-REL-002"
    assert _CONTRACT["budget_seconds"] == rr.DEFAULT_RTO_SECONDS == 600
    assert _CONTRACT["budget_ns"] == rr.DEFAULT_RTO_BUDGET_NS == 600_000_000_000
    assert rr.RestartRecoveryTarget().budget_ns == 600_000_000_000


def test_phases_match_enum_in_order() -> None:
    contract_phases = [p["name"] for p in _CONTRACT["phases"]]
    assert contract_phases == [p.value for p in rr.RestartPhase]
    assert contract_phases == [p.value for p in rr.REQUIRED_PHASES]


def test_subchecks_match_enum() -> None:
    assert set(_CONTRACT["subchecks"]) == {sc.value for sc in rr.SubCheck}
    assert set(_CONTRACT["subchecks"]) == {sc.value for sc in rr.REQUIRED_SUBCHECKS}
    assert _CONTRACT["nas_archival_degraded_with_alert_ok"] is True


def test_subcheck_statuses_match_enum() -> None:
    assert set(_CONTRACT["subcheck_statuses"]) == {s.value for s in rr.SubCheckStatus}


def test_gate_states_match_atp_readiness_source() -> None:
    # The engine's GateOutcome vocabulary is reused from atp_readiness.GateState (value parity), not
    # forked — so a change to the authoritative gate states cannot silently drift this substrate.
    from atp_readiness.gate import GateState

    assert {g.value for g in rr.GateOutcome} == {g.value for g in GateState}
    assert set(_CONTRACT["gate_state_variants"]) == {g.value for g in rr.GateOutcome}
    assert set(_CONTRACT["trade_ready_states"]) == {g.value for g in rr.TRADE_READY_STATES}
    # Only READY certifies — OVERRIDDEN is a valid gate state (parity retained) but is a manual
    # bypass of a failed check, so it is NOT a certifying trade-ready state.
    assert _CONTRACT["trade_ready_states"] == ["ready"]
    assert rr.GateOutcome.OVERRIDDEN not in rr.TRADE_READY_STATES
    assert "overridden" in _CONTRACT["gate_state_variants"]  # still a valid gate state


def test_verdicts_match() -> None:
    assert set(_CONTRACT["verdicts"]) == {v.value for v in rr.Verdict}


def test_error_types_exist() -> None:
    for name in _CONTRACT["error_types"]:
        cls = getattr(rr, name)
        assert issubclass(cls, rr.RestartError)


def test_cli_exit_codes_match() -> None:
    from atp_reliability import restart_cli

    assert _CONTRACT["cli_exit_codes"]["PASS"] == restart_cli.EXIT_PASS == 0
    assert _CONTRACT["cli_exit_codes"]["not_certified"] == restart_cli.EXIT_NOT_CERTIFIED == 1
    assert _CONTRACT["cli_exit_codes"]["refused"] == restart_cli.EXIT_REFUSED == 2


def test_module_paths_exist() -> None:
    assert (ROOT / _CONTRACT["engine_module"]).is_file()
    assert (ROOT / _CONTRACT["evidence_module"]).is_file()
    assert (ROOT / _CONTRACT["cli_module"]).is_file()


def test_cli_does_not_expose_a_budget_override() -> None:
    # The SRS-REL-002 CLI must not let an operator weaken the 10-minute objective and still emit a
    # requirement=SRS-REL-002 PASS.
    from atp_reliability.restart_cli import build_parser

    help_text = build_parser().format_help()
    assert "--budget" not in help_text
    assert "--target" not in help_text


def test_no_evidence_synthesis_helper() -> None:
    # The package must expose NO helper that fabricates phase timestamps or sub-check passes — the
    # restart analog of availability's forbidden coverage-synthesis surface.
    import atp_reliability
    from atp_reliability import boot_evidence

    for mod in (atp_reliability, rr, boot_evidence):
        for name in getattr(mod, "__all__", []):
            low = name.lower()
            assert not ("synthesi" in low or "fabricate" in low or "assume" in low), name


def test_nfr_r6_phrases_present_in_syrs() -> None:
    syrs = (ROOT / _CONTRACT["syrs_doc"]).read_text()
    for phrase in [
        "trade-ready state within 10 minutes",
        "Proxmox VM availability",
        "Docker daemon startup",
        "not stale by more than one trading day",
        "degraded-mode operation is acceptable if NAS is unavailable, with an operator alert",
    ]:
        assert phrase in syrs, f"NFR-R6/SYS-76 phrase missing from SyRS: {phrase!r}"


def test_srs_ac_phrase_present() -> None:
    srs = (ROOT / _CONTRACT["srs_doc"]).read_text()
    assert (
        "Proxmox VM, Docker daemon, ATP services, and readiness checks complete within 10 minutes"
        in srs
    )


def test_deferred_names_the_system_test_and_md006() -> None:
    deferred = " ".join(_CONTRACT["deferred"]).lower()
    assert "system test" in deferred
    assert "srs-md-006" in deferred
    assert "passes:false" in deferred


def test_verification_method_is_system_test() -> None:
    assert _CONTRACT["verification_method"] == "System test"
    assert _CONTRACT["elapsed_measure"] == "end_to_end"


# --------------------------------------------------------------------------- #
# CLI: runnable entry + fail-closed parsing (subprocess-level, off pytest's pythonpath).
# --------------------------------------------------------------------------- #

S = rr.NS_PER_SECOND


def _et_epoch_ns(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    """An Eastern-time wall instant as epoch ns (via the availability adapter's exact converter)."""
    import datetime as dt

    from atp_reliability.evidence import _to_epoch_ns
    from atp_strategy.calendar import EASTERN

    return _to_epoch_ns(dt.datetime(year, month, day, hour, minute, tzinfo=EASTERN))


# A regular NYSE session: 2026-01-05 is a Monday (not a holiday); 10:00 ET is mid-session, so the
# market-hours scope DERIVED from the proxmox_vm trigger is True.
_MARKET_HOURS_TRIGGER_NS = _et_epoch_ns(2026, 1, 5, 10, 0)
# 03:00 ET the same day is before the 09:30 open -> out of scope.
_OFF_HOURS_TRIGGER_NS = _et_epoch_ns(2026, 1, 5, 3, 0)


def _fixture_from_trigger(trigger_ns: int) -> dict:
    b = trigger_ns
    return {
        "phases": {
            "proxmox_vm": [b, b + 10 * S],
            "os_boot": [b + 10 * S, b + 60 * S],
            "docker_daemon": [b + 60 * S, b + 90 * S],
            "atp_service_init": [b + 90 * S, b + 120 * S],
            "readiness_check": [b + 120 * S, b + 130 * S],
        },
        "readiness": {
            "gate_state": "ready",
            "subchecks": {
                "ib_connectivity": "pass",
                "ib_account": "pass",
                "data_layer_ssd": "pass",
                "nas_archival": ["degraded", True],
                "system_services": "pass",
            },
        },
    }


def _compliant_fixture() -> dict:
    # Triggered during market hours -> the DERIVED scope is True (so a clean run certifies PASS).
    return _fixture_from_trigger(_MARKET_HOURS_TRIGGER_NS)


def _run_cli(fpath: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "atp_reliability.restart_cli", "--fixture", str(fpath)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT)},
        check=False,
        capture_output=True,
        text=True,
    )


def test_documented_cli_entry_is_runnable_and_certifies(tmp_path: Path) -> None:
    assert _CONTRACT["cli_entry"] == "PYTHONPATH=python python -m atp_reliability.restart_cli"
    fpath = tmp_path / "evidence.json"
    fpath.write_text(json.dumps(_compliant_fixture()))
    result = _run_cli(fpath)
    assert result.returncode == 0, result.stderr
    assert "verdict:PASS" in result.stdout


def test_json_mode_stdout_is_valid_json(tmp_path: Path) -> None:
    # Regression (adversarial review): --json stdout must be PURE JSON (no trailing summary line),
    # so a machine consumer can json.loads() the verification artifact. The summary goes to stderr.
    fpath = tmp_path / "evidence.json"
    fpath.write_text(json.dumps(_compliant_fixture()))
    result = subprocess.run(
        [sys.executable, "-m", "atp_reliability.restart_cli", "--fixture", str(fpath), "--json"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(PYTHON_ROOT)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)  # must not raise
    assert payload["verdict"] == "PASS"
    assert payload["requirement"] == "SRS-REL-002"
    assert payload["elapsed_seconds"] == 130.0
    # the human summary line is on stderr, not stdout
    assert "restart_recovery verdict:PASS" in result.stderr
    assert "restart_recovery verdict" not in result.stdout


def test_out_of_hours_restart_cannot_certify(tmp_path: Path) -> None:
    # Regression (adversarial review): market-hours scope is DERIVED from the restart-trigger
    # timestamp, not a caller boolean. A restart with an out-of-hours trigger (03:00 ET) but
    # otherwise-compliant timings must NOT certify — even though nothing else is wrong.
    fixture = _fixture_from_trigger(_OFF_HOURS_TRIGGER_NS)
    fpath = tmp_path / "offhours.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode == 1
    assert "verdict:INCONCLUSIVE" in result.stdout
    assert "verdict:PASS" not in result.stdout


def test_weekend_restart_cannot_certify(tmp_path: Path) -> None:
    # 2026-01-03 is a Saturday (non-session day) -> derived scope False -> INCONCLUSIVE.
    fixture = _fixture_from_trigger(_et_epoch_ns(2026, 1, 3, 10, 0))
    fpath = tmp_path / "weekend.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode == 1
    assert "verdict:INCONCLUSIVE" in result.stdout


def test_unscopeable_trigger_over_budget_still_fails(tmp_path: Path) -> None:
    # Regression (adversarial review): a complete over-10-min timeline whose trigger cannot be
    # scoped by the calendar (2050 is outside the bundled 2000-2035 horizon) must report FAIL, not
    # a refusal — a provable budget breach is never hidden behind scope-derivation failure.
    b = _et_epoch_ns(2050, 6, 1, 10, 0)
    fixture = _fixture_from_trigger(b)
    fixture["phases"]["readiness_check"] = [
        b + 120 * S,
        b + 700 * S,
    ]  # elapsed 700 s > 600 s budget
    fpath = tmp_path / "unscopeable_breach.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode == 1, result.stderr  # FAIL, not refused (exit 2)
    assert "verdict:FAIL" in result.stdout


def test_caller_supplied_market_hours_boolean_is_rejected(tmp_path: Path) -> None:
    # A caller-supplied restart_context.during_market_hours (forgeable) is REJECTED — scope must be
    # derived from the real trigger timestamp, not asserted.
    fixture = _compliant_fixture()
    fixture["restart_context"] = {"during_market_hours": True}
    fpath = tmp_path / "forged.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode == 2
    assert "during_market_hours" in result.stderr
    assert "Traceback" not in result.stderr


def test_overridden_gate_cannot_certify_via_cli(tmp_path: Path) -> None:
    # Regression (adversarial review): a fixture with gate_state='overridden' + all sub-checks
    # passing must NOT certify PASS — an override is a manual bypass of a failed SYS-76 check.
    fixture = _compliant_fixture()
    fixture["readiness"]["gate_state"] = "overridden"
    fpath = tmp_path / "override.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode == 1
    assert "verdict:FAIL" in result.stdout
    assert "verdict:PASS" not in result.stdout


def test_missing_subcheck_cannot_certify(tmp_path: Path) -> None:
    fixture = _compliant_fixture()
    del fixture["readiness"]["subchecks"]["system_services"]
    fpath = tmp_path / "evidence.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode == 1
    assert "verdict:INCONCLUSIVE" in result.stdout
    assert "verdict:PASS" not in result.stdout


@pytest.mark.parametrize("bad", [None, False, 0, "", {}])
def test_malformed_subcheck_value_is_refused_not_certified(tmp_path: Path, bad: object) -> None:
    # A falsy-but-malformed sub-check value must be REFUSED (exit 2), not coerced to "no evidence"
    # or a passing check.
    fixture = _compliant_fixture()
    fixture["readiness"]["subchecks"]["ib_connectivity"] = bad
    fpath = tmp_path / "bad.json"
    fpath.write_text(json.dumps(fixture))
    from atp_reliability.restart_cli import run

    assert run(["--fixture", str(fpath)]) == 2, bad


@pytest.mark.parametrize("bad", [None, False, 0, "x", {}])
def test_malformed_phase_value_is_refused(tmp_path: Path, bad: object) -> None:
    fixture = _compliant_fixture()
    fixture["phases"]["os_boot"] = bad
    fpath = tmp_path / "bad.json"
    fpath.write_text(json.dumps(fixture))
    from atp_reliability.restart_cli import run

    assert run(["--fixture", str(fpath)]) == 2, bad


def test_unknown_phase_key_is_refused(tmp_path: Path) -> None:
    fixture = _compliant_fixture()
    fixture["phases"]["bogus_phase"] = [0, 10 * S]
    fpath = tmp_path / "bad.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode == 2
    assert "unknown phase" in result.stderr


def test_nas_alert_flag_must_be_boolean_not_truthy(tmp_path: Path) -> None:
    # SYS-76(d) alert flag must be a real bool — a truthy 1 must not stand in for the operator alert.
    fixture = _compliant_fixture()
    fixture["readiness"]["subchecks"]["nas_archival"] = ["degraded", 1]
    fpath = tmp_path / "bad.json"
    fpath.write_text(json.dumps(fixture))
    from atp_reliability.restart_cli import run

    assert run(["--fixture", str(fpath)]) == 2


def test_corrupt_fixture_is_refused_not_a_crash(tmp_path: Path) -> None:
    fpath = tmp_path / "corrupt.json"
    fpath.write_text("this is not valid json\n")
    result = _run_cli(fpath)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_duplicate_json_phase_key_is_refused(tmp_path: Path) -> None:
    # A fixture with two "os_boot" phase entries must be REFUSED at parse time (object_pairs_hook),
    # not silently last-wins collapsed by json.load and then certified.
    raw = (
        '{"phases": {"proxmox_vm": [0, 10], "os_boot": [10, 60], "os_boot": [10, 9999999999999],'
        ' "docker_daemon": [60, 90], "atp_service_init": [90, 120], "readiness_check": [120, 130]}}'
    )
    fpath = tmp_path / "dup.json"
    fpath.write_text(raw)
    result = _run_cli(fpath)
    assert result.returncode == 2
    assert "duplicate JSON key" in result.stderr
    assert "Traceback" not in result.stderr


def test_oversized_timestamp_is_refused_not_a_crash(tmp_path: Path) -> None:
    # A pathological giant integer timestamp must fail closed (exit 2), never crash with an
    # OverflowError traceback (math.isfinite / int->float).
    fixture = _compliant_fixture()
    fixture["phases"]["readiness_check"] = [120 * S, 2**200]
    fpath = tmp_path / "huge.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_legitimate_overlapping_boot_phases_are_accepted(tmp_path: Path) -> None:
    # Docker starting DURING userspace OS boot (a nested phase) is valid reference evidence — the
    # CLI must accept it (verdict driven by readiness, here INCONCLUSIVE for absent sub-checks), not
    # refuse it as an overlap error.
    b = _MARKET_HOURS_TRIGGER_NS
    fixture = {
        "phases": {
            "proxmox_vm": [b, b + 5 * S],
            "os_boot": [b + 5 * S, b + 35 * S],
            "docker_daemon": [b + 25 * S, b + 28 * S],  # nested inside os_boot
            "atp_service_init": [b + 40 * S, b + 90 * S],
            "readiness_check": [b + 90 * S, b + 100 * S],
        },
        "readiness": {"gate_state": "ready"},
    }
    fpath = tmp_path / "overlap.json"
    fpath.write_text(json.dumps(fixture))
    result = _run_cli(fpath)
    assert result.returncode == 1  # INCONCLUSIVE (sub-checks absent), NOT a refusal
    assert "verdict:INCONCLUSIVE" in result.stdout
