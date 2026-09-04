"""SRS-MD-005 — the scheduled IB Gateway restart window (L7 domain / safety).

SyRS SYS-75, SYS-45, SYS-46, NFR-R2; StRS C-2, SN-2.04, SN-2.05.

IB restarts its Gateway once a day. Without this feature every one of those
restarts looks identical to an outage: live submissions blocked, the operator
paged at 23:45, and — worse in the other direction — nothing that ever
distinguishes the restart that did NOT come back. SRS-MD-005 is the decision
that tells the two apart.

The file path matches ``SAFETY_PATH_RE`` (``connectivity``), so the
deterministic critic requires this pairing — but it would be warranted anyway:
the feature decides when order submission and market-data requests are
suspended, and when a genuine outage stops being suppressed.

## The three halves

1. **Behavioural.** Shells the Rust suites that drive the real
   ``md005_connectivity_restart_window_cli`` binary in fresh OS processes, over
   the real ``ExecutionEngine::dispatch_order -> route_order ->
   submit_live_order`` authority chain, the real subscription manager, the real
   SRS-NOTIF-001 dispatcher, and a real TCP probe against a real (dead or live)
   loopback port.
2. **Structural.** Source guards over the bypasses a behavioural test cannot
   see: that both admission points consult the window, that the escalation arm
   exists, and that the digest-pinned IB transport files were not touched.
3. **Cross-surface.** The SyRS SYS-75 defaults must read the same in the
   ARCH-005 catalogue, ``.env.example``, the Rust constants, the Python
   resolver and the config README. A documented default the code does not
   implement is a lie with a fuse.

## NOT proven here, and not claimable from this path

* **A live IB observation.** Every run above uses FIXTURE transports (the
  recording IB gateway, the SRS-NOTIF-001 fixture email/push) and a loopback
  port. That is sufficient for the acceptance criterion — its sharpest clause is
  about behaviour during an outage, and an outage needs no gateway — but nothing
  here watches the real 23:45 ET restart.
* **Continuous connectivity detection.** The producer answers when asked, at
  order routing and at a subscription request. The loop that watches the gateway
  between those moments is SRS-EXE-001's named scope.
* **Readiness.** A reachable probe means the gateway accepts TCP, not that the
  API answers a handshake. The "readiness checks pass" half of ERR-2 is owned by
  ERR-9 / SRS-MD-006.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]

CLI_SOURCE = REPO_ROOT / "crates/atp-orchestrator/src/bin/md005_connectivity_restart_window_cli.rs"
PRODUCER_SOURCE = REPO_ROOT / "crates/atp-orchestrator/src/restart_window_connectivity.rs"
SCENARIO_SOURCE = REPO_ROOT / "crates/atp-orchestrator/src/restart_window_scenario.rs"
MARKET_DATA_SOURCE = REPO_ROOT / "crates/atp-market-data/src/lib.rs"
TYPES_SOURCE = REPO_ROOT / "crates/atp-types/src/lib.rs"
REACHABILITY_SOURCE = REPO_ROOT / "crates/atp-adapters/src/gateway_reachability.rs"

_NO_CARGO = "cargo not on PATH; the Rust restart-window suites cannot be driven"


def _run_cargo(args: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which("cargo") is None:
        pytest.skip(reason=_NO_CARGO)
    return subprocess.run(
        ["cargo", "test", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_one_passed(result: subprocess.CompletedProcess[str], name: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"{name} failed:\n{combined}"
    # The second half is the anti-vacuity guard: a typo'd test name filters to
    # zero tests and still exits 0, so "the command succeeded" is not evidence.
    assert "1 passed" in combined, f"{name} did not run (filtered out?):\n{combined}"


def _cli_test(name: str) -> subprocess.CompletedProcess[str]:
    return _run_cargo(
        [
            "-p",
            "atp-orchestrator",
            "--test",
            "srs_md_005_restart_window_cli",
            name,
            "--",
            "--exact",
        ]
    )


def _market_data_test(name: str) -> subprocess.CompletedProcess[str]:
    return _run_cargo(
        [
            "-p",
            "atp-market-data",
            "--test",
            "srs_md_005_market_data_suspension",
            name,
            "--",
            "--exact",
        ]
    )


def _producer_test(name: str) -> subprocess.CompletedProcess[str]:
    return _run_cargo(
        [
            "-p",
            "atp-orchestrator",
            "--lib",
            f"restart_window_connectivity::tests::{name}",
            "--",
            "--exact",
        ]
    )


# --------------------------------------------------------------------------- #
# 1. Behavioural — the real chain, in fresh processes
# --------------------------------------------------------------------------- #


def test_suspension_blocks_orders_and_market_data_and_suppresses_the_alert() -> None:
    """SyRS SYS-75(a)+(b): the whole pre-restart posture, in one run."""
    name = "suspension_blocks_orders_and_market_data_and_suppresses_the_alert"
    _assert_one_passed(_cli_test(name), name)


def test_a_gateway_still_dead_after_the_window_escalates_and_pages() -> None:
    """The SYS-75 escalation clause — the criterion this feature is verified by.

    A window that never closed would suppress a real outage indefinitely.
    """
    name = "a_gateway_still_dead_after_the_window_escalates_and_pages"
    _assert_one_passed(_cli_test(name), name)


def test_a_gateway_that_returns_inside_the_window_resumes() -> None:
    """SyRS SYS-75(c)+(d): reconnect once available, then resume."""
    name = "a_gateway_that_returns_inside_the_window_resumes_orders_and_market_data"
    _assert_one_passed(_cli_test(name), name)


def test_suspension_cannot_be_derived_outside_the_window() -> None:
    """Non-vacuity: a gate that refused always would pass the suspension case."""
    name = "suspension_cannot_be_derived_outside_the_window"
    _assert_one_passed(_cli_test(name), name)


def test_escalation_cannot_be_derived_inside_the_window() -> None:
    """The sharpest control: the SAME dead gateway one instant earlier is
    planned maintenance, so an escalation proof that passed here would be
    describing the gateway rather than the window."""
    name = "escalation_cannot_be_derived_inside_the_window"
    _assert_one_passed(_cli_test(name), name)


def test_resume_cannot_be_derived_against_a_dead_gateway() -> None:
    name = "resume_cannot_be_derived_against_a_dead_gateway"
    _assert_one_passed(_cli_test(name), name)


def test_every_rejected_invocation_fails_closed() -> None:
    """A malformed window, an unknown fault and a bare `--help` must all exit
    non-zero with no proof line."""
    name = "every_rejected_invocation_fails_closed"
    _assert_one_passed(_cli_test(name), name)


def test_the_window_boundaries_are_operator_configurable() -> None:
    """SyRS SYS-75 says the window is configurable; a widened lead must move the
    suspension with it, or "configurable" is prose."""
    name = "the_window_boundaries_are_operator_configurable"
    _assert_one_passed(_cli_test(name), name)


def test_market_data_requests_are_suspended_at_the_mutating_admission_point() -> None:
    """`subscribe` is the call that actually opens an upstream IB line."""
    name = "srs_md_005_the_mutating_admission_point_is_gated_too"
    _assert_one_passed(_market_data_test(name), name)


def test_a_refusal_after_the_window_is_labelled_an_outage_not_maintenance() -> None:
    """One boolean would have told the operator to wait out an incident."""
    name = "a_refusal_after_the_window_is_labelled_an_outage_not_maintenance"
    _assert_one_passed(_market_data_test(name), name)


def test_the_lead_suspends_without_spending_a_probe() -> None:
    """The gateway serves ONE API client; a probe during the lead would spend
    the slot the reconnect is waiting for, and could not change the answer."""
    name = "the_lead_suspends_without_spending_a_probe"
    _assert_one_passed(_producer_test(name), name)


def test_the_market_data_gate_inherits_the_probe_skip() -> None:
    """A new code path does not inherit the old one's guarantees.

    The probe-skip started life inside `state()` only, and the market-data gate
    — added by the same feature — silently did not inherit it, so every
    subscription request during the lead paid a blocking TCP connect for an
    answer the phase already fixed.
    """
    name = "restart_window_connectivity::tests::the_market_data_gate_inherits_the_probe_skip"
    _assert_one_passed(_producer_test(name.split("::")[-1]), name)


def test_the_evidence_path_does_not_probe_during_the_lead_either() -> None:
    """The guarantee must hold in the TOOL that reports on it, too.

    The scenario used to observe unconditionally, so the operator CLI and the L5
    suite probed during the lead — spending the gateway's single API-client slot
    on a question the phase already answers, while the gates themselves refused
    to. A reporting path that breaks the invariant it reports on is the same
    class as a guard that flags its own documentation.
    """
    name = "suspension_blocks_orders_and_market_data_and_suppresses_the_alert"
    _assert_one_passed(_cli_test(name), name)
    source = SCENARIO_SOURCE.read_text(encoding="utf-8")
    assert "connectivity.observe_if_needed()" in source, (
        "the scenario must use the phase-aware observation, not the unconditional probe"
    )
    assert "connectivity.observe()" not in source, (
        "an unconditional observe() in the evidence path probes during the lead"
    )


def test_both_gates_read_the_clock_exactly_once() -> None:
    """Sampling the clock again after a probe that can block for two seconds
    lets the two gates put one wall-clock moment in different SYS-75 phases —
    the disagreement this producer exists to make impossible."""
    source = PRODUCER_SOURCE.read_text(encoding="utf-8")
    for fn_name in ("fn state(", "fn market_data_admission("):
        start = source.index(fn_name)
        end = source.index("\n    }", start)
        body = source[start:end]
        assert body.count("(self.clock)()") == 1, (
            f"{fn_name.rstrip('(')} reads the injected clock more than once"
        )


def test_the_state_is_recomputed_rather_than_cached_across_the_boundary() -> None:
    """The two instants a cache would get wrong are the two that matter."""
    name = "the_state_is_recomputed_rather_than_cached_across_the_boundary"
    _assert_one_passed(_producer_test(name), name)


# --------------------------------------------------------------------------- #
# 2. Structural — the bypasses a behavioural test cannot see
# --------------------------------------------------------------------------- #


def test_both_subscription_admission_points_consult_the_window() -> None:
    """Gating only the outer manager leaves the mutating one as a bypass."""
    source = MARKET_DATA_SOURCE.read_text(encoding="utf-8")
    # A hard count of 2 was the weakness the adversarial reviewer named: it pins
    # "these two are gated" and says nothing about a THIRD admission point added
    # later. The load-bearing check is the effect-based discovery in
    # tools/connectivity_check.py, driven against an injected ungated site by
    # test_no_admission_point_can_hide_from_the_static_guard below; this is the
    # cheap floor beneath it, so it asks for AT LEAST the two known guards.
    assert source.count("match window.admission() {") >= 2, (
        "expected the restart-window guard at BOTH subscription admission points"
    )
    for signature in ("pub fn request_subscription<C, S, W>", "pub fn subscribe<S:"):
        assert signature in source, f"missing gated admission point: {signature}"
    assert "window: &W" in source, (
        "the window must arrive as a required port, not an optional argument"
    )


def test_the_window_carries_no_clock_of_its_own() -> None:
    """A type that read the wall clock could not be tested at a boundary, and
    the boundaries are the whole requirement."""
    types_source = TYPES_SOURCE.read_text(encoding="utf-8")
    start = types_source.index("pub struct RestartWindow {")
    end = types_source.index("pub enum MarketDataAdmission", start)
    window_span = types_source[start:end]
    for forbidden in ("SystemTime::now", "Instant::now"):
        assert forbidden not in window_span, (
            f"RestartWindow reads the wall clock via {forbidden}; the instant must be injected"
        )


def test_the_escalation_arm_exists_and_has_no_catch_all() -> None:
    """A catch-all would let a phase added later inherit whichever answer
    happened to be there instead of failing to compile."""
    types_source = TYPES_SOURCE.read_text(encoding="utf-8")
    start = types_source.index("pub const fn connectivity_state(")
    body = types_source[start : types_source.index("\n    }", start)]
    assert "RestartPhase::Elapsed" in body, "the restart window never closes"
    assert "ConnectivityState::Unreachable" in body, "the escalation is unreachable"
    assert not re.search(r"^\s*_\s*=>", body, re.MULTILINE), (
        "connectivity_state uses a catch-all match arm"
    )


def test_the_probe_never_holds_the_single_api_client_slot() -> None:
    """The gateway serves one API client and leaves a prior connection in
    CLOSE_WAIT; a probe that held the socket would block its own reconnect."""
    source = REACHABILITY_SOURCE.read_text(encoding="utf-8")
    assert "drop(stream)" in source, "the reachability probe must close its socket immediately"
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))
    assert "to_socket_addrs" not in code, "a hostname resolves outside the connect deadline"
    assert "connect_timeout" in code, "the probe needs an explicit deadline"


def test_the_digest_pinned_ib_transport_files_are_untouched() -> None:
    """`tools/ib_adapter_check.py` SHA-256s these three against the operator's
    live paper-account evidence. Editing any of them — a comment counts — flips
    closed-green SRS-EXE-006 red, recoverable only by a fresh live run. The
    reachability seam is a NEW file for exactly this reason."""
    if shutil.which("git") is None:
        pytest.skip(reason="git not on PATH; cannot diff the pinned transport files")
    pinned = [
        "crates/atp-adapters/src/interactive_brokers.rs",
        "crates/atp-adapters/src/interactive_brokers/wire.rs",
        "crates/atp-adapters/tests/srs_exe_006_ib_adapter.rs",
    ]
    result = subprocess.run(
        ["git", "diff", "--stat", "origin/main...HEAD", "--", *pinned],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(reason="origin/main is not available in this checkout")
    assert not result.stdout.strip(), (
        "the digest-pinned SRS-EXE-006 transport files were modified:\n" + result.stdout
    )


def _code_only(source: str) -> str:
    """Strip line comments so a guard tests CODE, not the prose about it.

    Without this, a doc comment that says "never touches 4001/4002" trips the
    check forbidding those ports — the guard fires on the sentence explaining
    why the guard exists, and a check that flags its own documentation is one
    people learn to route around.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("//", "*"))
    )


def test_the_cli_labels_its_transport_tier_on_every_path() -> None:
    """A drill must never be mistaken for live evidence."""
    source = CLI_SOURCE.read_text(encoding="utf-8")
    assert "transports:FIXTURE" in source
    code = _code_only(source)
    assert "4001" not in code and "4002" not in code, (
        "the CLI must never target the shared IB ports"
    )
    # Non-vacuity: the stripper must not have removed everything.
    assert "TcpListener::bind" in code, "the comment stripper ate the code"


def test_the_scenario_drives_the_shared_order_entry_not_the_inner_gate() -> None:
    """Proving through `submit_live_order` would take the mode as a caller
    argument and could reach the broker with no designated live strategy at
    all. The proof has to go through `dispatch_order`."""
    source = SCENARIO_SOURCE.read_text(encoding="utf-8")
    assert ".dispatch_order(" in source
    assert ".submit_live_order(" not in source, "the scenario must not call the inner gate directly"
    assert ".designate(" in source, "the live designation must be exercised, not bypassed"


def test_no_admission_point_can_hide_from_the_static_guard() -> None:
    """The reviewer's bypass, pinned at the domain layer too.

    The first version of the static guard discovered admission points by "takes
    a RestartWindowGate", which is circular — a new path that skips the port is
    exactly what must be caught, and skipping it made the path invisible. This
    drives the real check against an injected ungated site, so the domain layer
    notices if the discovery ever goes back to being circular.
    """
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    try:
        from connectivity_check import (
            ConnectivityCheckError,
            check_market_data_admission_sites,
            load_config,
            market_data_source,
        )
    finally:
        sys.path.pop(0)

    config = load_config()
    source = market_data_source(config)
    bypass = """
impl ConsolidatedSubscriptionRegistry {
    pub fn subscribe_bulk(&mut self, request: &SubscriptionRequest) {
        self.subscribers
            .insert(request.symbol.clone(), vec![request.strategy_id.clone()]);
    }
}
"""
    with pytest.raises(ConnectivityCheckError, match="subscribe_bulk"):
        check_market_data_admission_sites(config, source + bypass)

    # Non-vacuity: the unmutated source must still pass, or the guard is simply
    # refusing everything.
    check_market_data_admission_sites(config, source)


def test_the_producer_serves_both_gates_from_one_window() -> None:
    """Two separately-configured gates over one requirement drift, and the
    drift is invisible until a deployment updates only one."""
    source = PRODUCER_SOURCE.read_text(encoding="utf-8")
    assert "impl<P, C> BrokerageConnectivity for ScheduledRestartConnectivity<P, C>" in source
    assert (
        "impl<P, C> atp_market_data::RestartWindowGate for ScheduledRestartConnectivity<P, C>"
        in source
    )


# --------------------------------------------------------------------------- #
# 3. Cross-surface — the defaults must agree everywhere
# --------------------------------------------------------------------------- #


def _catalogue_key(name: str) -> dict:
    catalogue = json.loads(
        (REPO_ROOT / "architecture/runtime_services.json").read_text(encoding="utf-8")
    )
    for key in catalogue["configuration"]["keys"]:
        if key["name"] == name:
            return key
    raise AssertionError(f"{name} is not in the SRS-ARCH-005 configuration catalogue")


def _env_example_value(name: str) -> str:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}=(.*)$", text, re.MULTILINE)
    assert match is not None, f"{name} is not documented in .env.example"
    return match.group(1).strip()


@pytest.mark.parametrize(
    ("env_key", "rust_const", "python_const"),
    [
        (
            "ATP_IB_RESTART_WINDOW_SECONDS",
            "DEFAULT_RESTART_WINDOW_SECONDS",
            "DEFAULT_RESTART_WINDOW_SECONDS",
        ),
        (
            "ATP_IB_RESTART_SUSPEND_LEAD_SECONDS",
            "DEFAULT_RESTART_SUSPEND_LEAD_SECONDS",
            "DEFAULT_RESTART_SUSPEND_LEAD_SECONDS",
        ),
    ],
)
def test_every_surface_agrees_on_the_restart_window_defaults(
    env_key: str, rust_const: str, python_const: str
) -> None:
    """The SyRS SYS-75 durations appear in five places. A default documented in
    one and implemented differently in another is the bug class that already
    shipped once here, on the push endpoint."""
    catalogue_default = int(_catalogue_key(env_key)["default"])
    env_default = int(_env_example_value(env_key))

    types_source = TYPES_SOURCE.read_text(encoding="utf-8")
    rust_match = re.search(rf"{re.escape(rust_const)}: i64 = (\d+);", types_source)
    assert rust_match is not None, f"{rust_const} is missing from atp-types"
    rust_default = int(rust_match.group(1))

    sys.path.insert(0, str(REPO_ROOT / "python"))
    try:
        from atp_orchestration import restart_schedule
    finally:
        sys.path.pop(0)
    python_default = getattr(restart_schedule, python_const)

    init_source = (REPO_ROOT / "init.sh").read_text(encoding="utf-8")
    init_match = re.search(rf"{re.escape(env_key)}:-(\d+)", init_source)
    assert init_match is not None, f"{env_key} has no init.sh dev default"
    init_default = int(init_match.group(1))

    observed = {
        "catalogue": catalogue_default,
        ".env.example": env_default,
        "atp-types": rust_default,
        "atp_orchestration": python_default,
        "init.sh": init_default,
    }
    assert len(set(observed.values())) == 1, f"the surfaces disagree on {env_key}: {observed}"


def test_the_restart_time_default_agrees_across_surfaces() -> None:
    sys.path.insert(0, str(REPO_ROOT / "python"))
    try:
        from atp_orchestration import restart_schedule
    finally:
        sys.path.pop(0)
    observed = {
        "catalogue": _catalogue_key("ATP_IB_RESTART_ET")["default"],
        ".env.example": _env_example_value("ATP_IB_RESTART_ET"),
        "atp_orchestration": restart_schedule.DEFAULT_RESTART_ET,
    }
    assert len(set(observed.values())) == 1, f"the surfaces disagree: {observed}"
    assert observed["catalogue"] == "23:45", "SyRS SYS-75 puts the restart at 23:45 ET"


def test_the_eastern_restart_instant_moves_with_daylight_saving() -> None:
    """23:45 ET is 03:45 UTC in EDT and 04:45 UTC in EST.

    A hand-rolled or fixed-offset resolution would suspend trading an hour off
    for half the year — and a real restart arriving unsuppressed is exactly the
    page this feature exists to prevent.
    """
    import datetime as dt

    sys.path.insert(0, str(REPO_ROOT / "python"))
    try:
        from atp_orchestration.restart_schedule import resolve_restart_instant_ns
    finally:
        sys.path.pop(0)

    summer = resolve_restart_instant_ns(dt.date(2026, 9, 3))
    winter = resolve_restart_instant_ns(dt.date(2026, 12, 3))
    summer_utc = dt.datetime.fromtimestamp(summer // 1_000_000_000, dt.timezone.utc)
    winter_utc = dt.datetime.fromtimestamp(winter // 1_000_000_000, dt.timezone.utc)
    assert (summer_utc.hour, summer_utc.minute) == (3, 45), summer_utc
    assert (winter_utc.hour, winter_utc.minute) == (4, 45), winter_utc


def test_a_malformed_restart_schedule_is_refused_rather_than_defaulted() -> None:
    """A window that looks configured and fires at the wrong hour is worse than
    a startup failure the operator can read."""
    sys.path.insert(0, str(REPO_ROOT / "python"))
    try:
        from atp_orchestration.restart_schedule import RestartSchedule, RestartScheduleError
    finally:
        sys.path.pop(0)

    for bad in (
        {"restart_et": "25:00"},
        {"restart_et": "23:45:00"},
        {"restart_et": ""},
        {"window_seconds": 0},
        {"window_seconds": 999_999},
        {"suspend_lead_seconds": -1},
    ):
        with pytest.raises(RestartScheduleError):
            RestartSchedule(**bad)

    # The non-vacuity partner: the documented defaults must be accepted, or the
    # refusals above would pass on a constructor that rejects everything.
    accepted = RestartSchedule()
    assert accepted.window_seconds == 300
    assert accepted.suspend_lead_seconds == 60
