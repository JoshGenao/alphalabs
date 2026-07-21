"""Host boot-telemetry adapters that feed the restart-recovery engine (SRS-REL-002).

This is the only layer in the restart substrate allowed to touch a host; the engine
(:mod:`atp_reliability.restart`) stays dependency-free and clock-free.

**What :func:`run_host_collection` actually measures** (the real host-telemetry it reads):

* ``OS_BOOT`` — ``/proc/stat`` ``btime`` (the wall-clock epoch the kernel booted) + ``systemd-
  analyze`` (the reported startup duration).
* ``DOCKER_DAEMON`` — ``systemctl show docker`` ``InactiveExitTimestampMonotonic`` /
  ``ActiveEnterTimestampMonotonic`` (microseconds since the monotonic clock's zero ≈ boot),
  rebased onto ``btime``.

**External evidence — passed in, NOT collected here** (the collector does *not* drive Docker
Compose or the readiness gate; supplying these is the caller's responsibility, so the provenance is
not overstated):

* ``PROXMOX_VM`` — the hypervisor-side ``qm start`` → guest-reachable instants, measured on the
  Proxmox host (a run from inside the guest with no hypervisor evidence leaves it absent).
* ``ATP_SERVICE_INIT`` / ``READINESS_CHECK`` — supplied by whoever drives the Docker-Compose bring-up
  and the SYS-76 readiness check. Those runtimes are **deferred** (the phase1 compose services are
  ``cargo test`` stubs, not a running platform; SYS-76 runtime probes → SRS-MD-006), so today these
  intervals are typically **absent** → the engine reports them missing (``INCONCLUSIVE``).

**Honesty boundary (SYS-76).** The five SYS-76 runtime readiness sub-checks (IB connectivity/auth,
IB account, SSD ingestion-freshness, NAS reachability, service health) are deferred to SRS-MD-006
and are **not** implemented today, so the collector emits them as **absent** — the engine then
returns ``INCONCLUSIVE`` for the full SRS-REL-002 objective rather than a false ``PASS``. The
collector can still report the **real infra-timeline span** (a non-certifying, non-SRS-labelled
number) over whatever phases are present — the measured OS-boot + Docker span, plus any external
phases (``PROXMOX_VM`` etc.) the caller supplied.

The parsers here are pure (string → value); the only I/O is in :func:`run_host_collection`, which
takes an injectable command runner so the assembly logic is unit-testable without a host. No host
identity (IP/hostname) is embedded — the collector runs locally on the VM and the hypervisor-side
``PROXMOX_VM`` instants are passed in.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .restart import (
    NS_PER_SECOND,
    GateOutcome,
    ObservedPhase,
    RestartPhase,
    RestartRecoveryArtifact,
    RestartRecoveryTarget,
    compute_restart_recovery,
)

_NS_PER_US = 1_000

#: A non-SRS requirement label for the infra-only report, so the real (partial) infra elapsed can
#: be shown WITHOUT minting a mislabelled ``requirement: SRS-REL-002`` artifact (the label lock in
#: ``RestartRecoveryTarget`` forbids a weakened SRS-labelled target).
INFRA_ONLY_REQUIREMENT = "infra-timeline (non-certifying)"

#: A generous non-binding budget for the infra-only report (it is not a certification gate).
DEFAULT_INFRA_BUDGET_NS = 24 * 3_600 * NS_PER_SECOND  # 24h — informational only


class BootEvidenceError(Exception):
    """A host-telemetry string could not be parsed — fail closed rather than fabricate a phase."""


def derive_during_market_hours(trigger_ns: int, *, exchange: str = "NYSE") -> bool:
    """Derive whether a restart-trigger instant falls within a US-equity **regular session**.

    NFR-R6 scopes its objective to restarts *during market hours*. That scope is a **derived fact**
    from the real restart-trigger epoch-ns — NOT a caller-supplied claim (a raw boolean would be
    forgeable, letting an out-of-hours restart mint an SRS-REL-002 PASS). It is computed here in the
    evidence adapter (the pure engine stays calendar-free) using the real DST/holiday-aware
    ``UsEquityTradingCalendar`` (SYS-50) — the same authority the availability substrate uses. A
    trigger on a non-session day, or outside ``[session_open, session_close)`` ET, is out of scope.

    Raises :class:`BootEvidenceError` if ``trigger_ns`` is not a non-negative int or the calendar
    cannot place the instant (e.g. horizon exceeded) — fail closed, never guess the scope.
    """

    import datetime as _dt

    from atp_strategy.calendar import EASTERN, UsEquityTradingCalendar

    from .evidence import _to_epoch_ns

    if isinstance(trigger_ns, bool) or not isinstance(trigger_ns, int) or trigger_ns < 0:
        raise BootEvidenceError(f"trigger_ns must be a non-negative int; got {trigger_ns!r}")
    try:
        calendar = UsEquityTradingCalendar.for_exchange(exchange)
        seconds, _ = divmod(trigger_ns, 1_000_000_000)
        utc_dt = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=seconds)
        session_date = utc_dt.astimezone(EASTERN).date()
        if not calendar.is_session(session_date):
            return False
        open_ns = _to_epoch_ns(calendar.session_open(session_date))
        close_ns = _to_epoch_ns(calendar.session_close(session_date))
    except BootEvidenceError:
        raise
    except Exception as exc:  # unknown exchange / calendar horizon / conversion
        raise BootEvidenceError(
            f"cannot determine market-hours scope for trigger {trigger_ns} (exchange {exchange!r}): {exc}"
        ) from exc
    return open_ns <= trigger_ns < close_ns


@dataclass(frozen=True, slots=True)
class BootDurations:
    """Parsed ``systemd-analyze`` startup durations (ns; a field is 0 when the segment is absent).

    ``firmware`` and ``loader`` are pre-kernel (pre-``btime``); ``kernel``, ``initrd``, and
    ``userspace`` are post-``btime`` (part of OS boot). ``total`` is ``systemd-analyze``'s reported
    ``= <total>`` (the sum of all segments).
    """

    firmware_ns: int = 0
    loader_ns: int = 0
    kernel_ns: int = 0
    initrd_ns: int = 0
    userspace_ns: int = 0
    total_ns: int = 0


# --------------------------------------------------------------------------- #
# Pure parsers.
# --------------------------------------------------------------------------- #


def parse_proc_stat_btime(text: str) -> int:
    """Return the kernel boot wall-clock epoch **seconds** from ``/proc/stat`` ``btime`` line.

    Raises :class:`BootEvidenceError` if the ``btime`` line is missing or malformed — a boot
    timeline with no anchor epoch must fail closed, not default to 0.
    """

    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "btime":
            try:
                value = int(parts[1])
            except ValueError as exc:
                raise BootEvidenceError(
                    f"/proc/stat btime is not an integer: {parts[1]!r}"
                ) from exc
            if value <= 0:
                raise BootEvidenceError(f"/proc/stat btime must be positive; got {value}")
            return value
    raise BootEvidenceError("/proc/stat has no 'btime' line")


_DURATION_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(h|min|m?s|us|ms)")
_UNIT_NS: dict[str, int] = {
    "h": 3_600 * NS_PER_SECOND,
    "min": 60 * NS_PER_SECOND,
    "s": NS_PER_SECOND,
    "ms": 1_000_000,
    "us": 1_000,
}


def parse_duration_to_ns(text: str) -> int:
    """Parse a systemd-style duration (``"1min 2.345s"``, ``"500ms"``, ``"3.4s"``) to integer ns.

    Sums every ``<number><unit>`` token (``h`` / ``min`` / ``s`` / ``ms`` / ``us``). Raises
    :class:`BootEvidenceError` if no recognisable token is present (so a garbled duration cannot be
    silently read as 0 and understate a phase).
    """

    total = 0
    matched = False
    for value, unit in _DURATION_TOKEN_RE.findall(text):
        matched = True
        # normalise: a bare "m" (rare) is not used by systemd; "min" and "ms" are the tokens.
        total += round(float(value) * _UNIT_NS[unit])
    if not matched:
        raise BootEvidenceError(f"no recognisable duration token in {text!r}")
    return total


#: A systemd duration run (``"1min 5.752s"``, ``"18.7s"``, ``"500ms"``) — one or more number+unit
#: tokens. Reused for both the per-segment and the ``= total`` captures in ``parse_systemd_analyze``.
_DUR = r"[0-9.]+\s*(?:h|min|m?s|us|ms)(?:\s+[0-9.]+\s*(?:h|min|m?s|us|ms))*"


def parse_systemd_analyze(text: str) -> BootDurations:
    """Parse ``systemd-analyze`` (``time``) output into :class:`BootDurations`.

    Handles the ``Startup finished in <firmware> (firmware) + <loader> (loader) + <kernel> (kernel)
    + <initrd> (initrd) + <userspace> (userspace) = <total>`` line. The ``(initrd)`` segment is
    recognised (it is post-``btime`` OS-boot time, common on real Linux with an initramfs) and the
    ``= <total>`` is captured even when trailing text follows on the line — so a host that reports
    an initrd segment cannot silently drop it and understate the OS-boot duration. Raises
    :class:`BootEvidenceError` if the ``Startup finished`` line is absent.
    """

    line = next((ln for ln in text.splitlines() if "Startup finished in" in ln), None)
    if line is None:
        raise BootEvidenceError("systemd-analyze output has no 'Startup finished in' line")
    segments = {"firmware": 0, "loader": 0, "kernel": 0, "initrd": 0, "userspace": 0}
    for seg_text, seg_name in re.findall(
        rf"({_DUR})\s*\((firmware|loader|kernel|initrd|userspace)\)", line
    ):
        segments[seg_name] = parse_duration_to_ns(seg_text)
    # Capture the '= <total>' anywhere on the line (not only at end-of-line, so trailing target
    # text does not force the fallback). Fall back to the segment sum only if there is no '=' total.
    total_match = re.search(rf"=\s*({_DUR})", line)
    total_ns = parse_duration_to_ns(total_match.group(1)) if total_match else sum(segments.values())
    return BootDurations(
        firmware_ns=segments["firmware"],
        loader_ns=segments["loader"],
        kernel_ns=segments["kernel"],
        initrd_ns=segments["initrd"],
        userspace_ns=segments["userspace"],
        total_ns=total_ns,
    )


def parse_systemctl_monotonic_us(text: str, key: str) -> int:
    """Return the microseconds value of a ``systemctl show`` ``<key>=<us>`` monotonic property.

    Raises :class:`BootEvidenceError` if the key is absent or non-integer. A value of ``0`` means
    the event has not occurred (systemd's sentinel) and is returned as ``0`` for the caller to
    handle — it is never silently turned into a fabricated timestamp.
    """

    for line in text.splitlines():
        if line.startswith(key + "="):
            raw = line[len(key) + 1 :].strip()
            try:
                return int(raw)
            except ValueError as exc:
                raise BootEvidenceError(f"{key} is not an integer: {raw!r}") from exc
    raise BootEvidenceError(f"systemctl output has no {key!r} property")


# --------------------------------------------------------------------------- #
# Phase builders (pure — btime + parsed telemetry -> ObservedPhase).
# --------------------------------------------------------------------------- #


def os_boot_phase(btime_seconds: int, durations: BootDurations) -> ObservedPhase:
    """Build the ``OS_BOOT`` phase ``[btime, btime + (kernel + userspace)]`` (kernel boot → up).

    ``btime`` (from ``/proc/stat``) is the epoch the **kernel** started, so only the post-``btime``
    startup (kernel + initrd + userspace) is added. The ``systemd-analyze`` **firmware/loader**
    segments happen *before* the kernel starts (VM/BIOS bring-up, attributed to ``PROXMOX_VM``) and
    are deliberately EXCLUDED — adding them would double-count pre-``btime`` time and inflate the
    OS-boot interval. The post-``btime`` duration is computed as ``total - firmware - loader``
    (== kernel + initrd + userspace, since ``systemd-analyze``'s ``= total`` is the sum of all its
    segments), which is robust whether or not the individual post-btime segments were broken out and
    **includes the initrd/initramfs time**.
    """

    start_ns = btime_seconds * NS_PER_SECOND
    post_btime_ns = durations.total_ns - durations.firmware_ns - durations.loader_ns
    end_ns = start_ns + post_btime_ns
    if end_ns <= start_ns:
        raise BootEvidenceError(
            f"OS_BOOT has non-positive post-btime duration ({post_btime_ns} ns from total="
            f"{durations.total_ns}, firmware={durations.firmware_ns}, loader={durations.loader_ns}); "
            "systemd-analyze evidence is degenerate"
        )
    return ObservedPhase(phase=RestartPhase.OS_BOOT, start_ns=start_ns, end_ns=end_ns)


def docker_daemon_phase(btime_seconds: int, docker_show_text: str) -> ObservedPhase:
    """Build the ``DOCKER_DAEMON`` phase from ``systemctl show docker`` monotonic timestamps.

    ``InactiveExitTimestampMonotonic`` (daemon began starting) → start; ``ActiveEnterTimestamp
    Monotonic`` (daemon active) → end, both rebased onto ``btime``. Raises
    :class:`BootEvidenceError` if either is the ``0`` sentinel or the interval is degenerate — a
    fabricated docker window would understate the restart timeline.
    """

    btime_ns = btime_seconds * NS_PER_SECOND
    start_us = parse_systemctl_monotonic_us(docker_show_text, "InactiveExitTimestampMonotonic")
    end_us = parse_systemctl_monotonic_us(docker_show_text, "ActiveEnterTimestampMonotonic")
    if start_us <= 0 or end_us <= 0:
        raise BootEvidenceError(
            "docker.service monotonic timestamps are unset (0) — cannot bound the DOCKER_DAEMON phase"
        )
    start_ns = btime_ns + start_us * _NS_PER_US
    end_ns = btime_ns + end_us * _NS_PER_US
    if end_ns <= start_ns:
        raise BootEvidenceError(
            f"DOCKER_DAEMON has non-positive duration (start_us={start_us}, end_us={end_us})"
        )
    return ObservedPhase(phase=RestartPhase.DOCKER_DAEMON, start_ns=start_ns, end_ns=end_ns)


# --------------------------------------------------------------------------- #
# Fixture assembly + infra-only report.
# --------------------------------------------------------------------------- #


def assemble_fixture(
    phases: Sequence[ObservedPhase],
    *,
    gate_state: GateOutcome | None = None,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble a ``restart_recovery`` fixture dict from observed phases + the config-gate state.

    The SYS-76 runtime sub-checks are **deliberately omitted** (all absent) — they are deferred to
    SRS-MD-006, so the engine will return ``INCONCLUSIVE`` for the full objective rather than a
    false PASS. ``gate_state`` is a **caller-supplied** ``atp_readiness`` config-gate result (the
    static-config half of the gate; this function does not evaluate the gate itself); pass ``None``
    when no readiness evidence was supplied.
    """

    phases_obj: dict[str, list[int]] = {p.phase.value: [p.start_ns, p.end_ns] for p in phases}
    fixture: dict[str, object] = {"phases": phases_obj}
    if gate_state is not None:
        # No 'subchecks' key: every SYS-76 sub-check is absent (honest — deferred to SRS-MD-006).
        fixture["readiness"] = {"gate_state": gate_state.value}
    if context:
        fixture["restart_context"] = context
    return fixture


def infra_only_report(phases: Sequence[ObservedPhase]) -> RestartRecoveryArtifact:
    """Return a NON-SRS-labelled artifact over the measured phases so the real infra elapsed shows.

    Uses :data:`INFRA_ONLY_REQUIREMENT` (not ``SRS-REL-002``) so this never claims certification;
    the value of interest is ``observed_span_ns`` (the real span over the supplied phases — e.g.
    VM-start → OS-boot/Docker-up), which the artifact reports regardless of verdict.
    """

    target = RestartRecoveryTarget(
        requirement=INFRA_ONLY_REQUIREMENT, budget_ns=DEFAULT_INFRA_BUDGET_NS
    )
    return compute_restart_recovery(phases=list(phases), readiness=None, target=target)


# --------------------------------------------------------------------------- #
# Host collection (the only I/O; command runner is injectable for tests).
# --------------------------------------------------------------------------- #

#: A runner takes a command argv and returns its stdout (or raises BootEvidenceError).
CommandRunner = Callable[[Sequence[str]], str]

#: Bounded wait for every host telemetry command (``cat /proc/stat``, ``systemd-analyze``,
#: ``systemctl show docker``). A reliability evidence collector must FAIL CLOSED, never HANG — a
#: stuck command must surface as a refused measurement, not stall collection forever.
DEFAULT_COMMAND_TIMEOUT_S = 30.0

#: The host commands the collector runs (all read-only telemetry).
_PROC_STAT_CMD = ("cat", "/proc/stat")
_SYSTEMD_ANALYZE_CMD = ("systemd-analyze", "time")
_DOCKER_SHOW_CMD = ("systemctl", "show", "docker", "--no-pager")


def _run_host_command(cmd: Sequence[str], timeout_s: float) -> str:
    """Run a host telemetry command with a bounded timeout; translate every failure to BootEvidenceError.

    A timeout, a non-zero exit, or a missing binary all become :class:`BootEvidenceError` (with the
    command for context) so the collector fails closed rather than hanging or leaking a raw
    ``subprocess`` traceback.
    """

    printable = " ".join(cmd)
    try:
        result = subprocess.run(
            list(cmd), check=True, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as exc:
        raise BootEvidenceError(f"host command timed out after {timeout_s}s: {printable}") from exc
    except subprocess.CalledProcessError as exc:
        raise BootEvidenceError(
            f"host command failed (exit {exc.returncode}): {printable}: {(exc.stderr or '').strip()}"
        ) from exc
    except OSError as exc:
        raise BootEvidenceError(f"host command could not be run: {printable}: {exc}") from exc
    return result.stdout


def default_command_runner(timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S) -> CommandRunner:
    """Return a :data:`CommandRunner` that runs each command with a bounded ``timeout_s``."""

    def _runner(cmd: Sequence[str]) -> str:
        return _run_host_command(cmd, timeout_s)

    return _runner


@dataclass(frozen=True, slots=True)
class HostCollection:
    """The result of a host boot-telemetry sweep."""

    phases: tuple[ObservedPhase, ...]
    gate_state: GateOutcome | None
    fixture: dict[str, object]
    infra_span_ns: int


def collect_infra_phases(
    *,
    proc_stat: str,
    systemd_analyze: str,
    docker_show: str,
    proxmox_vm: tuple[int, int] | None = None,
    atp_service_init: tuple[int, int] | None = None,
    readiness_check: tuple[int, int] | None = None,
) -> list[ObservedPhase]:
    """Build the observable phases from host-telemetry strings + caller-supplied external instants.

    ``proc_stat`` / ``systemd_analyze`` / ``docker_show`` are the raw command outputs (parsed purely)
    that yield the **measured** ``OS_BOOT`` + ``DOCKER_DAEMON`` phases. ``proxmox_vm`` /
    ``atp_service_init`` / ``readiness_check`` are ``(start_ns, end_ns)`` pairs supplied by the caller
    as **external evidence** (the hypervisor-side timing, and — when their deferred runtimes are
    driven — the compose/gate timing); this function does not itself drive Compose or the gate.
    Absent pairs leave those phases out (→ the engine reports them missing).
    """

    btime_s = parse_proc_stat_btime(proc_stat)
    durations = parse_systemd_analyze(systemd_analyze)
    phases: list[ObservedPhase] = [
        os_boot_phase(btime_s, durations),
        docker_daemon_phase(btime_s, docker_show),
    ]
    for phase, pair in (
        (RestartPhase.PROXMOX_VM, proxmox_vm),
        (RestartPhase.ATP_SERVICE_INIT, atp_service_init),
        (RestartPhase.READINESS_CHECK, readiness_check),
    ):
        if pair is not None:
            phases.append(ObservedPhase(phase=phase, start_ns=pair[0], end_ns=pair[1]))
    return phases


def build_host_collection(
    phases: Sequence[ObservedPhase],
    *,
    gate_state: GateOutcome | None = None,
    context: dict[str, object] | None = None,
) -> HostCollection:
    """Bundle observed phases into a fixture + infra-span for reporting."""

    fixture = assemble_fixture(phases, gate_state=gate_state, context=context)
    infra = infra_only_report(phases)
    return HostCollection(
        phases=tuple(phases),
        gate_state=gate_state,
        fixture=fixture,
        infra_span_ns=infra.observed_span_ns,
    )


def run_host_collection(
    *,
    runner: CommandRunner | None = None,
    timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    proxmox_vm: tuple[int, int] | None = None,
    atp_service_init: tuple[int, int] | None = None,
    readiness_check: tuple[int, int] | None = None,
    gate_state: GateOutcome | None = None,
    context: dict[str, object] | None = None,
) -> HostCollection:
    """Run the host telemetry commands (bounded, fail-closed) and build a :class:`HostCollection`.

    This is the collector's only host-I/O entry point. It **measures only** the ``OS_BOOT`` and
    ``DOCKER_DAEMON`` phases, by reading ``/proc/stat``, ``systemd-analyze time``, and ``systemctl
    show docker`` through ``runner`` (default: a bounded runner that translates any timeout /
    non-zero exit / missing binary into :class:`BootEvidenceError`). It does **not** drive Docker
    Compose or the readiness gate: ``proxmox_vm`` (hypervisor-side), ``atp_service_init``, and
    ``readiness_check`` are **external evidence supplied by the caller** (absent when their deferred
    runtimes have not been driven), so this function never overstates what it collected. ``runner``
    is injectable so the assembly is testable off-host.
    """

    run = runner or default_command_runner(timeout_s)
    proc_stat = run(_PROC_STAT_CMD)
    systemd_analyze = run(_SYSTEMD_ANALYZE_CMD)
    docker_show = run(_DOCKER_SHOW_CMD)
    phases = collect_infra_phases(
        proc_stat=proc_stat,
        systemd_analyze=systemd_analyze,
        docker_show=docker_show,
        proxmox_vm=proxmox_vm,
        atp_service_init=atp_service_init,
        readiness_check=readiness_check,
    )
    return build_host_collection(phases, gate_state=gate_state, context=context)


__all__ = [
    "DEFAULT_COMMAND_TIMEOUT_S",
    "DEFAULT_INFRA_BUDGET_NS",
    "BootDurations",
    "BootEvidenceError",
    "CommandRunner",
    "HostCollection",
    "INFRA_ONLY_REQUIREMENT",
    "assemble_fixture",
    "build_host_collection",
    "collect_infra_phases",
    "default_command_runner",
    "derive_during_market_hours",
    "docker_daemon_phase",
    "infra_only_report",
    "os_boot_phase",
    "parse_duration_to_ns",
    "parse_proc_stat_btime",
    "parse_systemctl_monotonic_us",
    "parse_systemd_analyze",
    "run_host_collection",
]
