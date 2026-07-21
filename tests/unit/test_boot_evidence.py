"""L1 unit tests for the boot-telemetry parsers + fixture assembly (SRS-REL-002, part B).

The parsers are pure (string → value); the assembly builds a ``restart_recovery`` fixture from
host telemetry with the SYS-76 sub-checks honestly absent. No host I/O — every command output is a
fixed string, so the collector's assembly logic is testable off-host.
"""

from __future__ import annotations

import pytest
from atp_reliability.boot_evidence import (
    INFRA_ONLY_REQUIREMENT,
    BootEvidenceError,
    assemble_fixture,
    build_host_collection,
    collect_infra_phases,
    default_command_runner,
    derive_during_market_hours,
    docker_daemon_phase,
    infra_only_report,
    os_boot_phase,
    parse_duration_to_ns,
    parse_proc_stat_btime,
    parse_systemctl_monotonic_us,
    parse_systemd_analyze,
    run_host_collection,
)
from atp_reliability.restart import (
    NS_PER_SECOND,
    GateOutcome,
    RestartPhase,
    SubCheck,
    Verdict,
    compute_restart_recovery,
)
from atp_reliability.restart_cli import RestartRecoveryTarget, _artifact_from_fixture

pytestmark = [pytest.mark.unit]

S = NS_PER_SECOND
_DOCKER_SHOW = (
    "InactiveExitTimestampMonotonic=20000000\n"
    "ActiveEnterTimestampMonotonic=23500000\n"
    "ActiveState=active\n"
)


def test_parse_proc_stat_btime() -> None:
    assert parse_proc_stat_btime("cpu 1 2 3\nbtime 1752393600\nprocesses 42\n") == 1752393600


def test_parse_proc_stat_btime_missing_or_bad_raises() -> None:
    with pytest.raises(BootEvidenceError):
        parse_proc_stat_btime("cpu 1 2 3\nprocesses 42\n")
    with pytest.raises(BootEvidenceError):
        parse_proc_stat_btime("btime notanumber\n")
    with pytest.raises(BootEvidenceError):
        parse_proc_stat_btime("btime 0\n")  # non-positive fails closed


@pytest.mark.parametrize(
    "text,expected_ns",
    [
        ("3.456s", 3_456_000_000),
        ("500ms", 500_000_000),
        ("1min 2.345s", 62_345_000_000),
        ("12us", 12_000),
        ("1h", 3_600 * NS_PER_SECOND),
    ],
)
def test_parse_duration_to_ns(text: str, expected_ns: int) -> None:
    assert parse_duration_to_ns(text) == expected_ns


def test_parse_duration_no_token_raises() -> None:
    with pytest.raises(BootEvidenceError):
        parse_duration_to_ns("garbage")


def test_parse_systemd_analyze_full() -> None:
    d = parse_systemd_analyze(
        "Startup finished in 2.100s (firmware) + 1.200s (loader) + 3.456s (kernel) + "
        "12.789s (userspace) = 19.545s"
    )
    assert d.firmware_ns == 2_100_000_000
    assert d.kernel_ns == 3_456_000_000
    assert d.userspace_ns == 12_789_000_000
    assert d.total_ns == 19_545_000_000


def test_parse_systemd_analyze_vm_no_firmware_and_min_units() -> None:
    d = parse_systemd_analyze(
        "Startup finished in 4.500s (kernel) + 1min 5.250s (userspace) = 1min 9.750s"
    )
    assert d.firmware_ns == 0
    assert d.userspace_ns == 65_250_000_000
    assert d.total_ns == 69_750_000_000


def test_parse_systemd_analyze_missing_line_raises() -> None:
    with pytest.raises(BootEvidenceError):
        parse_systemd_analyze("some other output\n")


def test_parse_systemctl_monotonic() -> None:
    assert parse_systemctl_monotonic_us(_DOCKER_SHOW, "ActiveEnterTimestampMonotonic") == 23_500_000
    with pytest.raises(BootEvidenceError):
        parse_systemctl_monotonic_us(_DOCKER_SHOW, "NoSuchProperty")


def test_os_boot_excludes_pre_btime_firmware_and_loader() -> None:
    # Regression (adversarial review): btime is the KERNEL start, so OS_BOOT must add only the
    # post-btime kernel+userspace time. Firmware+loader (2.0+1.0=3.0s) happen before btime and must
    # NOT inflate OS_BOOT — duration = kernel(4.5)+userspace(10.0) = 14.5s, not total(17.5s).
    d = parse_systemd_analyze(
        "Startup finished in 2.0s (firmware) + 1.0s (loader) + 4.5s (kernel) + 10.0s (userspace) = 17.5s"
    )
    assert d.total_ns == 17_500_000_000
    osb = os_boot_phase(1752393600, d)
    assert (
        osb.end_ns - osb.start_ns == 14_500_000_000
    )  # excludes the 3.0s pre-btime firmware/loader


def test_parse_systemd_analyze_with_initrd_and_trailing_line() -> None:
    # Regression (adversarial review): a real systemd-analyze line with an (initrd) segment must be
    # captured (initrd is post-btime OS-boot time) and the '= total' captured even with a following
    # 'graphical.target reached...' line — otherwise initrd time is silently dropped.
    text = (
        "Startup finished in 1.2s (firmware) + 0.5s (loader) + 3.0s (kernel) + 4.0s (initrd) + "
        "10.0s (userspace) = 18.7s\n"
        "graphical.target reached after 17.0s in userspace.\n"
    )
    d = parse_systemd_analyze(text)
    assert d.initrd_ns == 4_000_000_000
    assert d.total_ns == 18_700_000_000
    osb = os_boot_phase(1752393600, d)
    # post-btime = kernel(3)+initrd(4)+userspace(10) = 17.0s = total(18.7) - firmware(1.2) - loader(0.5)
    assert osb.end_ns - osb.start_ns == 17_000_000_000


def test_os_boot_and_docker_phases() -> None:
    bt = 1752393600
    durations = parse_systemd_analyze(
        "Startup finished in 4.5s (kernel) + 10.0s (userspace) = 14.5s"
    )
    osb = os_boot_phase(bt, durations)
    assert osb.phase is RestartPhase.OS_BOOT
    assert osb.start_ns == bt * S
    assert osb.end_ns - osb.start_ns == 14_500_000_000
    dock = docker_daemon_phase(bt, _DOCKER_SHOW)
    assert dock.end_ns - dock.start_ns == 3_500_000_000  # 23.5ms-20ms... in us→ns: 3.5s


def test_docker_phase_unset_monotonic_raises() -> None:
    with pytest.raises(BootEvidenceError):
        docker_daemon_phase(
            1752393600,
            "InactiveExitTimestampMonotonic=0\nActiveEnterTimestampMonotonic=0\n",
        )


def test_assemble_fixture_omits_subchecks() -> None:
    # The assembled fixture must carry NO sub-checks (all SYS-76 sub-checks deferred to SRS-MD-006),
    # so feeding it to the engine yields INCONCLUSIVE, never a false PASS.
    bt = 1752393600
    base = bt * S
    phases = collect_infra_phases(
        proc_stat="btime 1752393600\n",
        systemd_analyze="Startup finished in 4.5s (kernel) + 10.0s (userspace) = 14.5s",
        docker_show=_DOCKER_SHOW,
        proxmox_vm=(base - 30 * S, base),
        atp_service_init=(base + 30 * S, base + 90 * S),
        readiness_check=(base + 90 * S, base + 95 * S),
    )
    # No 'during_market_hours' in context — scope is DERIVED from the trigger timestamp by the CLI,
    # not asserted by the collector (a caller boolean would be forgeable / is rejected).
    fixture = assemble_fixture(phases, gate_state=GateOutcome.READY)
    assert "subchecks" not in fixture["readiness"]  # type: ignore[operator]
    assert fixture["readiness"] == {"gate_state": "ready"}
    art = _artifact_from_fixture(fixture, RestartRecoveryTarget())
    assert art.verdict is Verdict.INCONCLUSIVE
    assert set(art.missing_subchecks) == {sc.value for sc in SubCheck}


def test_infra_only_report_is_non_srs_labelled() -> None:
    # The infra-only report must NOT claim SRS-REL-002 — it shows the real span under a plain label.
    bt = 1752393600
    base = bt * S
    phases = collect_infra_phases(
        proc_stat="btime 1752393600\n",
        systemd_analyze="Startup finished in 4.5s (kernel) + 10.0s (userspace) = 14.5s",
        docker_show=_DOCKER_SHOW,
        proxmox_vm=(base - 30 * S, base),
        atp_service_init=(base + 30 * S, base + 90 * S),
        readiness_check=(base + 90 * S, base + 95 * S),
    )
    report = infra_only_report(phases)
    assert report.requirement == INFRA_ONLY_REQUIREMENT
    assert report.requirement != "SRS-REL-002"
    assert report.observed_span_ns == 125 * S  # base-30 .. base+95

    hc = build_host_collection(phases, gate_state=GateOutcome.READY)
    assert hc.infra_span_ns == 125 * S


def test_collect_infra_phases_ordering_is_valid_for_engine() -> None:
    # The phases the collector emits must satisfy the engine's ordering/overlap validation.
    bt = 1752393600
    base = bt * S
    phases = collect_infra_phases(
        proc_stat="btime 1752393600\n",
        systemd_analyze="Startup finished in 4.5s (kernel) + 10.0s (userspace) = 14.5s",
        docker_show=_DOCKER_SHOW,
        proxmox_vm=(base - 30 * S, base),
        atp_service_init=(base + 30 * S, base + 90 * S),
        readiness_check=(base + 90 * S, base + 95 * S),
    )
    # Does not raise (ordering valid); verdict is INCONCLUSIVE (sub-checks absent).
    art = compute_restart_recovery(phases=phases, readiness=None)
    assert art.verdict is Verdict.INCONCLUSIVE


def test_docker_starting_during_os_boot_is_accepted() -> None:
    # On real systemd boots, docker.service starts DURING userspace OS boot, so DOCKER_DAEMON nests
    # inside OS_BOOT. The collector's evidence must be ACCEPTED by the engine (not refused as an
    # overlap), which is the whole point of the reference-deployment measurement.
    bt = 1752393600
    base = bt * S
    # os_boot = [base, base+14.5s]; docker InactiveExit=8s / ActiveEnter=9s => [base+8s, base+9s],
    # fully nested inside os_boot.
    docker_nested = (
        "InactiveExitTimestampMonotonic=8000000\nActiveEnterTimestampMonotonic=9000000\n"
    )
    phases = collect_infra_phases(
        proc_stat="btime 1752393600\n",
        systemd_analyze="Startup finished in 4.5s (kernel) + 10.0s (userspace) = 14.5s",
        docker_show=docker_nested,
        proxmox_vm=(base - 20 * S, base),
        atp_service_init=(base + 30 * S, base + 90 * S),
        readiness_check=(base + 90 * S, base + 95 * S),
    )
    docker = next(p for p in phases if p.phase is RestartPhase.DOCKER_DAEMON)
    os_boot = next(p for p in phases if p.phase is RestartPhase.OS_BOOT)
    assert os_boot.start_ns <= docker.start_ns and docker.end_ns <= os_boot.end_ns  # nested
    # The engine accepts the overlapping evidence (no raise); INCONCLUSIVE only for absent sub-checks.
    art = compute_restart_recovery(phases=phases, readiness=None)
    assert art.verdict is Verdict.INCONCLUSIVE


# --------------------------------------------------------------------------- #
# Host command runner — must fail closed (bounded timeout + typed errors), never hang.
# --------------------------------------------------------------------------- #


def test_runner_timeout_fails_closed() -> None:
    runner = default_command_runner(timeout_s=0.1)
    with pytest.raises(BootEvidenceError, match="timed out"):
        runner(["sleep", "5"])


def test_runner_nonzero_exit_fails_closed() -> None:
    runner = default_command_runner(timeout_s=5)
    with pytest.raises(BootEvidenceError, match="failed"):
        runner(["sh", "-c", "echo boom >&2; exit 3"])


def test_runner_missing_binary_fails_closed() -> None:
    runner = default_command_runner(timeout_s=5)
    with pytest.raises(BootEvidenceError, match="could not be run"):
        runner(["this_binary_does_not_exist_xyz_123"])


def test_run_host_collection_with_injected_runner() -> None:
    # An injected runner makes the host-collection assembly testable off-host.
    outputs = {
        ("cat", "/proc/stat"): "btime 1752393600\n",
        (
            "systemd-analyze",
            "time",
        ): "Startup finished in 4.5s (kernel) + 10.0s (userspace) = 14.5s",
        ("systemctl", "show", "docker", "--no-pager"): _DOCKER_SHOW,
    }

    def fake(cmd: object) -> str:
        return outputs[tuple(cmd)]  # type: ignore[arg-type]

    bt = 1752393600
    base = bt * S
    hc = run_host_collection(
        runner=fake,
        proxmox_vm=(base - 20 * S, base),
        atp_service_init=(base + 30 * S, base + 90 * S),
        readiness_check=(base + 90 * S, base + 95 * S),
        gate_state=GateOutcome.READY,
    )
    assert hc.infra_span_ns == 115 * S  # (base+95) - (base-20)
    assert hc.fixture["readiness"] == {"gate_state": "ready"}
    art = compute_restart_recovery(phases=list(hc.phases), readiness=None)
    assert art.verdict is Verdict.INCONCLUSIVE  # sub-checks absent


def test_run_host_collection_propagates_runner_failure() -> None:
    def boom(cmd: object) -> str:
        raise BootEvidenceError("host command failed (exit 1): systemctl")

    with pytest.raises(BootEvidenceError):
        run_host_collection(runner=boom)


# --------------------------------------------------------------------------- #
# Market-hours scope derivation — from the real trigger timestamp + trading calendar (not forgeable).
# --------------------------------------------------------------------------- #


def _et_ns(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    import datetime as dt

    from atp_reliability.evidence import _to_epoch_ns
    from atp_strategy.calendar import EASTERN

    return _to_epoch_ns(dt.datetime(year, month, day, hour, minute, tzinfo=EASTERN))


def test_derive_market_hours_in_session_is_true() -> None:
    # 2026-01-05 is a Monday trading session; 10:00 ET is mid-session.
    assert derive_during_market_hours(_et_ns(2026, 1, 5, 10, 0)) is True


@pytest.mark.parametrize(
    "hour,minute",
    [(3, 0), (9, 29), (16, 0), (17, 0)],  # pre-open, one min before open, at close, after close
)
def test_derive_market_hours_outside_session_is_false(hour: int, minute: int) -> None:
    assert derive_during_market_hours(_et_ns(2026, 1, 5, hour, minute)) is False


def test_derive_market_hours_at_open_boundary_is_true() -> None:
    # The session is [open, close): 09:30 ET exactly is in-session.
    assert derive_during_market_hours(_et_ns(2026, 1, 5, 9, 30)) is True


def test_derive_market_hours_weekend_is_false() -> None:
    # 2026-01-03 is a Saturday — not a session day.
    assert derive_during_market_hours(_et_ns(2026, 1, 3, 10, 0)) is False


def test_derive_market_hours_rejects_non_int_trigger() -> None:
    with pytest.raises(BootEvidenceError):
        derive_during_market_hours(True)  # type: ignore[arg-type]
    with pytest.raises(BootEvidenceError):
        derive_during_market_hours(-1)
