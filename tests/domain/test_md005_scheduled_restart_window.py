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
SUBSCRIPTIONS_SOURCE = REPO_ROOT / "crates/atp-market-data/src/subscriptions.rs"
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
    source = MARKET_DATA_SOURCE.read_text(encoding="utf-8") + SUBSCRIPTIONS_SOURCE.read_text(
        encoding="utf-8"
    )
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


def test_the_reconnect_ledger_is_bounded_but_the_count_stays_exact() -> None:
    """A sustained outage against a retrying strategy writes the ledger on every
    blocked submission. Unbounded, it grows for the life of the process this
    module documents itself as the production connectivity producer for — while
    truncating the COUNT would under-report the outage, the wrong direction."""
    name = "the_reconnect_ledger_is_bounded_but_the_count_is_exact"
    _assert_one_passed(_producer_test(name), name)


def test_a_local_probe_failure_stays_distinguishable_from_an_outage() -> None:
    """Collapsing the outcome to a bool loses "the gateway said no" versus "we
    could not ask". A ProbeFailed from a local resource limit must still fail
    closed — that part is right — but the operator has to be able to see the
    fault was ours."""
    name = "a_local_probe_failure_stays_distinguishable_from_an_outage"
    _assert_one_passed(_producer_test(name), name)


def test_the_catalogue_keys_actually_move_the_window() -> None:
    """A documented knob that moves no behaviour is a lie with a fuse, and this
    one would have been discovered during a restart: the keys validated while
    the binary read compiled-in constants."""
    name = "the_catalogue_keys_actually_move_the_window"
    _assert_one_passed(_cli_test(name), name)


def test_a_malformed_catalogue_key_is_refused_not_defaulted() -> None:
    """A safety window on a schedule nobody chose is worse than a refusal the
    operator can read."""
    name = "a_malformed_catalogue_key_is_refused_not_defaulted"
    _assert_one_passed(_cli_test(name), name)


def test_consecutive_submissions_do_not_each_pay_the_probe_deadline() -> None:
    """NFR-P1. The execution engine consults this port INLINE on the live
    submission path, so an uncached probe would spend the order's own latency
    budget on the gate against a black-holing endpoint — a paused Gateway VM, a
    DROP rule, or the gateway mid-restart holding the socket unaccepted, which
    are exactly the conditions this feature exists for."""
    name = "consecutive_submissions_do_not_each_pay_the_probe_deadline"
    _assert_one_passed(_producer_test(name), name)


def test_the_phase_is_never_cached_even_though_reachability_is() -> None:
    """Caching reachability must not cache the PHASE: the two instants a cache
    would get wrong are the start of the suspension and the end of the window,
    which are the only two that matter."""
    name = "the_phase_is_never_cached_even_though_reachability_is"
    _assert_one_passed(_producer_test(name), name)


def test_the_probe_deadline_fits_inside_the_nfr_p1_order_budget() -> None:
    """The binding constraint is NFR-P1 (1,000 ms p95 signal-to-ack, including
    all internal system latency), not NFR-R2's 15 s reconnect budget — the
    probe is spent inside the order, not beside it."""
    if shutil.which("cargo") is None:
        pytest.skip(reason=_NO_CARGO)
    name = "the_probe_deadline_fits_inside_the_nfr_p1_order_budget"
    result = subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "atp-adapters",
            "--lib",
            f"gateway_reachability::tests::{name}",
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    _assert_one_passed(result, name)


def test_a_proof_line_never_outruns_the_phase_it_names() -> None:
    """Every other check in the suspension proof is also satisfied INSIDE the
    restart window, so without asserting the phase the tool printed the
    SYS-75(a) pre-restart proof for a lead it never entered — and `--inject`
    could not catch it, because that control overrides the instant itself."""
    for name in (
        "the_suspension_proof_cannot_be_printed_from_inside_the_window",
        "a_one_second_lead_still_lands_inside_the_lead",
        "the_resume_proof_cannot_be_printed_from_outside_the_window",
    ):
        _assert_one_passed(_cli_test(name), name)


def test_the_admission_guard_is_closed_by_rusts_own_privacy() -> None:
    """The guard's fourth and final shape.

    Three earlier versions each described the dangerous code — by the port it
    took, by two effect forms, by public `&mut self` on the inherent impl — and
    a reviewer walked past each in turn. The closure that holds is not a
    description: `subscribers` is a PRIVATE field, so the complete set of code
    that can reach the consolidated set is "the functions in this file naming
    it", bounded by the compiler rather than by imagination.
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
    # The owning module carries no `#[cfg(test)]` block, so appending is already
    # production code; where a marker exists the bypass is spliced before it, so
    # the check (which correctly ignores the unshipped test module) still sees
    # the injection.
    head, marker, tail = source.partition("\n#[cfg(test)]\nmod tests {")

    shapes = {
        "a trait impl with &mut self": """
trait Sneaky { fn admit(&mut self, k: SecurityKey, s: StrategyId); }
impl Sneaky for ConsolidatedSubscriptionRegistry {
    fn admit(&mut self, k: SecurityKey, s: StrategyId) {
        self.subscribers.insert(k, vec![s]);
    }
}
""",
        "a public free function writing the map": """
pub fn force_register(r: &mut ConsolidatedSubscriptionRegistry, k: SecurityKey, s: StrategyId) {
    r.subscribers.insert(k, vec![s]);
}
""",
        "entry().or_default().push()": """
impl ConsolidatedSubscriptionRegistry {
    pub fn force_subscribe(&mut self, k: SecurityKey, s: StrategyId) {
        self.subscribers.entry(k).or_default().push(s);
    }
}
""",
        "a PRIVATE helper writing the map": """
impl ConsolidatedSubscriptionRegistry {
    fn quietly_admit(&mut self, k: SecurityKey, s: StrategyId) {
        self.subscribers.insert(k, vec![s]);
    }
}
""",
    }
    for injected in shapes.values():
        with pytest.raises(ConnectivityCheckError):
            check_market_data_admission_sites(config, head + injected + marker + tail)

    # Non-vacuity: the real source must still pass.
    check_market_data_admission_sites(config, source)


def test_an_exemption_cannot_be_inherited_by_a_new_function() -> None:
    """Exempting by bare NAME is a hole.

    A trait impl reusing an exempt name inherits the exemption and is never
    asked to consult the window — the same trait-impl shape that had already
    defeated an earlier version of this guard. An exemption now has to resolve
    to exactly one function, keep its declared receiver, and stay unable to add
    a subscriber.
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

    reused_name = """
trait Sneaky { fn is_subscribed(&mut self, k: SecurityKey, s: StrategyId); }
impl Sneaky for ConsolidatedSubscriptionRegistry {
    fn is_subscribed(&mut self, k: SecurityKey, s: StrategyId) {
        self.subscribers.insert(k, vec![s]);
    }
}
"""
    with pytest.raises(ConnectivityCheckError, match="is_subscribed"):
        check_market_data_admission_sites(config, source + reused_name)

    # A DESCENDANT module would inherit visibility and go unscanned, leaving the
    # closure one level short.
    with pytest.raises(ConnectivityCheckError, match="submodule"):
        check_market_data_admission_sites(config, source + "\nmod inner;\n")

    # An exempt reader that becomes a mutator makes its own exemption false.
    became_mutator = source.replace(
        "pub fn is_subscribed(&self,", "pub fn is_subscribed(&mut self,", 1
    )
    assert became_mutator != source, "the receiver anchor moved"
    with pytest.raises(ConnectivityCheckError, match="&mut self"):
        check_market_data_admission_sites(config, became_mutator)

    # Non-vacuity: the real source still passes.
    check_market_data_admission_sites(config, source)


def test_a_derived_instant_refuses_overflow_rather_than_panicking() -> None:
    """A panic exits non-zero, so a check reading only the exit code counts it
    as the guard working — while the operator gets a backtrace instead of a
    reason. The CLI's default instant is derived from --restart-ns before the
    window's own checked arithmetic can refuse it."""
    name = "every_rejected_invocation_fails_closed"
    _assert_one_passed(_cli_test(name), name)


def test_a_window_whose_suspension_predates_the_epoch_is_refused() -> None:
    """A non-negative restart instant is not enough: with a normal lead, a small
    one puts the SUSPENSION before the epoch. Refused rather than clamped —
    clamping silently shortens the suspension, the one direction it must never
    move."""
    name = "restart_window_refuses_a_configuration_that_cannot_describe_maintenance"
    if shutil.which("cargo") is None:
        pytest.skip(reason=_NO_CARGO)
    result = subprocess.run(
        ["cargo", "test", "-p", "atp-types", "--lib", f"tests::{name}", "--", "--exact"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    _assert_one_passed(result, name)


def test_a_reachable_outcome_is_reused_only_briefly() -> None:
    """Both directions of the cache on the live-order path.

    Caching a positive for long means that after the gateway dies, `state()`
    still answers `Connected` and the ERR-2 gate hands a live order to an
    unreachable gateway. Caching nothing means `state()` — called once per
    submission — opens a fresh connection per order against a resource this
    feature elsewhere calls scarce. The answer is a SHORT positive window, and
    the test pins the bound in both directions: reuse inside it, re-probe just
    past it.
    """
    name = "a_reachable_outcome_is_reused_only_briefly_and_the_bound_is_what_protects_the_gate"
    _assert_one_passed(_producer_test(name), name)


def test_a_backwards_clock_re_probes_rather_than_reusing() -> None:
    """A negative age means the clock is untrustworthy, not that the entry is
    fresh. `saturating_sub` on i64 saturates at the type's bounds — it does not
    clamp to zero — so the earlier reading let `with_probe_ttl(0)` reuse an
    entry it had promised never to."""
    name = "a_zero_ttl_probes_every_time_and_a_backwards_clock_re_probes"
    _assert_one_passed(_producer_test(name), name)


def test_the_admission_scan_reads_deref_assignments_too() -> None:
    """The comment stripper used to delete real code.

    It dropped every line starting with `*`, taking
    `*self.subscribers.entry(k).or_default() = vec![s];` with it — so an ungated
    admission point was invisible. A stripper that removes code is worse than
    the false positive it was added for.
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
    pub fn force(&mut self, k: SecurityKey, s: StrategyId) {
        *self.subscribers.entry(k).or_default() = vec![s];
    }
}
"""
    with pytest.raises(ConnectivityCheckError, match="force"):
        check_market_data_admission_sites(config, source + bypass)

    # Non-vacuity: the real source still passes.
    check_market_data_admission_sites(config, source)


def test_the_privacy_closure_survives_any_widened_visibility() -> None:
    """The closure IS Rust's privacy, so any widening breaks it.

    `pub(crate)` re-opens the field to every sibling module — including
    `live_feed`, the exact module the registry was moved into its own file to
    escape — and the earlier check matched only a bare `pub`.
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
    for visibility in ("pub", "pub(crate)", "pub(super)"):
        mutated = source.replace(
            "    subscribers: BTreeMap", f"    {visibility} subscribers: BTreeMap", 1
        )
        assert mutated != source, "the field anchor moved"
        with pytest.raises(ConnectivityCheckError, match="not private"):
            check_market_data_admission_sites(config, mutated)

    # Non-vacuity: the real source still passes.
    check_market_data_admission_sites(config, source)


def test_a_permanently_invalid_request_is_not_relabelled_as_maintenance() -> None:
    """Precedence, and a real operator-facing defect the review found.

    The suspension tells the operator to retry once the window closes. For an
    option subscription, an empty symbol or an empty strategy id that is false —
    those are refused in every phase — so answering "wait five minutes" sends
    them to retry something that can never succeed.

    Scoped precisely, because the fix is not feature-wide: the REGISTRY
    (`ConsolidatedSubscriptionRegistry::subscribe`) now validates before
    consulting the window, so only a request that would have been admitted is
    suspended there. The manager's entry point cannot, because its structural
    check lives downstream in the pinned ERR-4 line-counter probe; that residual
    is owned by ERR-4 / SRS-MD-002, recorded in
    `connectivity_contract.restart_window.deferred[]`, and pinned by
    `the_managers_entry_point_still_relabels_an_invalid_request_and_that_is_recorded`
    so it cannot drift unnoticed.
    """
    name = "a_permanently_invalid_request_is_refused_on_its_own_terms_during_the_window"
    _assert_one_passed(_market_data_test(name), name)


def test_the_retained_reachability_observation_expires() -> None:
    """`last_outcome` promises an observation no older than the reuse window.
    Dropping the instant meant a 90-second-old outage was still handed back as
    current during the lead, where nothing probes — a stale fact wearing a fresh
    label."""
    name = "the_retained_outcome_expires_with_the_reuse_window"
    _assert_one_passed(_producer_test(name), name)


def test_only_the_declared_producer_may_implement_the_admission_gate() -> None:
    """Taking a port does not make the answer unforgeable.

    An `impl RestartWindowGate` returning `Admitted` is exactly as forgeable as
    passing `true` — three test files write one — and a PRODUCTION type doing
    the same would bypass SYS-75(a) with every other guard, this suite and all
    three CLI proofs still green. The only defence was a comment saying the test
    double is not exported, and this feature's own history says a comment is not
    a guard. The production implementors are enumerated from the crate sources
    and matched against the contract instead.
    """
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    try:
        from connectivity_check import (
            ConnectivityCheckError,
            check_restart_window_gate_implementors,
            load_config,
        )
    finally:
        sys.path.pop(0)

    config = load_config()
    # Non-vacuity first: the real tree passes, so the failure below is the
    # injection and not a check that refuses everything.
    check_restart_window_gate_implementors(config, "")

    target = REPO_ROOT / "crates/atp-market-data/src/lib.rs"
    original = target.read_text(encoding="utf-8")
    injected = (
        original
        + """
pub struct AlwaysOpenWindow;
impl RestartWindowGate for AlwaysOpenWindow {
    fn admission(&self) -> MarketDataAdmission {
        MarketDataAdmission::Admitted
    }
}
"""
    )
    try:
        target.write_text(injected, encoding="utf-8")
        with pytest.raises(ConnectivityCheckError, match="AlwaysOpenWindow"):
            check_restart_window_gate_implementors(config, "")
    finally:
        # Restore and stamp the mtime forward: a mtime-preserving restore leaves
        # cargo serving the mutant on the next run.
        target.write_text(original, encoding="utf-8")
        target.touch()

    check_restart_window_gate_implementors(config, "")


def test_the_resume_proof_labels_its_readiness_scope() -> None:
    """A reachable probe means the endpoint accepted TCP.

    A real IB Gateway accepts TCP before its API will answer a handshake, so
    `Connected` here is the socket-level half of "the gateway is back". The run
    self-labels it, for the same reason every path prints `transports:FIXTURE` —
    evidence that names a whole acceptance clause while covering half of it is
    the overclaim this feature has spent rounds removing.
    """
    source = CLI_SOURCE.read_text(encoding="utf-8")
    assert "readiness:SOCKET_LEVEL_ONLY" in source
    name = "a_gateway_that_returns_inside_the_window_resumes_orders_and_market_data"
    _assert_one_passed(_cli_test(name), name)


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


def test_the_reporting_surface_honours_the_same_bound_as_the_gate() -> None:
    """A bound that only half the module respects is not a bound.

    Once positives began to be cached, `last_outcome()` - the surface that
    describes reachability to an operator - still filtered on the 1 s NEGATIVE
    TTL, so a `Reachable` could be reported as current for ten times the 100 ms
    bound the module installs as its safety property. Both the gate and the
    report now route through one `ttl_for`, and this pins that they agree.

    NOT proven here: that the bound is the right length. It is argued against
    NFR-P1's 1,000 ms order budget in the module rustdoc and asserted at compile
    time; this test only proves the two surfaces cannot drift apart.
    """
    name = "the_reporting_surface_honours_the_same_bound_as_the_gate"
    _assert_one_passed(_producer_test(name), name)


def test_no_public_doc_still_describes_the_replaced_cache_policy() -> None:
    """Prose that ARGUES for a design is worse than absent prose when it is stale.

    The reachability cache changed policy once (r14: cache both outcomes, with
    a short bound on the positive). Its documentation did not, and the drift
    blocked three consecutive reviews:

      r15  the constant's rustdoc still called the OLD policy "the whole design"
      r15  `state()` still promised "a fresh probe on every read"
      r16  `last_outcome()` still told callers `None` meant the gateway had
           answered again

    Each was a public doc comment a reader would reasonably trust. This pins the
    claims that are now false, so the next policy change cannot leave them
    behind a fourth time.

    NOT proven here: that the current prose is a GOOD description. Only that the
    specific superseded claims are gone and the replacement names both bounds.
    """
    source = PRODUCER_SOURCE.read_text(encoding="utf-8")
    docs = "\n".join(line for line in source.splitlines() if line.strip().startswith("///"))
    # Drop QUOTED spans first. A rustdoc that says: this comment once said
    # "a fresh probe on every read", and that stopped being true - is recording
    # the drift, not repeating it. This guard flagged exactly that on its first
    # run, which is the third time in this feature a source-scanning check has
    # accused its own documentation (docs/playbooks/adversarial-precheck.md).
    normalised = " ".join(re.sub(r'"[^"]*"', " ", docs).split())

    superseded = {
        "only-a-negative-is-cached": "Only a NEGATIVE outcome is cached",
        "a-positive-is-never-retained": "never retained",
        "state-probes-on-every-read": "a fresh probe on\n/// every read",
        "none-means-healthy": "`None` once the gateway answers again",
    }
    still_present = {
        label: text
        for label, text in superseded.items()
        if " ".join(text.replace("///", " ").split()) in normalised
    }
    assert not still_present, (
        "public rustdoc still describes the cache policy replaced in round 14: "
        f"{sorted(still_present)}"
    )

    # Non-vacuity: the replacement must actually name BOTH bounds, or this test
    # would pass just as well against a module with no documentation at all.
    assert "REACHABLE_CACHE_TTL_NS" in normalised
    assert "REACHABILITY_CACHE_TTL_NS" in normalised
    assert "Both outcomes are cached" in normalised or "Both outcomes are retained" in normalised


def test_with_probe_ttl_can_shorten_the_positive_cap_but_never_raise_it() -> None:
    """A public builder must not be able to widen a safety bound.

    `with_probe_ttl` promised "reuse for `ttl_ns`" for a round after `ttl_for`
    began capping a REACHABLE observation at 100 ms, so a caller passing 2 s was
    silently getting 100 ms for a positive. It then said "2 s gets 2 s for an
    unreachable gateway" for a round after round 22 capped that branch too.

    This pins the behaviour in all three directions: asking for more gets each
    outcome's OWN cap (100 ms reachable, 1 s unreachable - not the 2 s asked
    for), and asking for zero disables reuse entirely.

    NOT proven here: that 100 ms is the right ceiling. That is argued against
    NFR-P1's 1,000 ms order budget in the module rustdoc and asserted at compile
    time.
    """
    name = "with_probe_ttl_can_shorten_the_positive_cap_but_never_raise_it"
    _assert_one_passed(_producer_test(name), name)


def test_a_shortened_ttl_shortens_both_directions() -> None:
    """The compile asserts relate the DEFAULTS, not the runtime bounds.

    An earlier comment claimed the cache asymmetry was compiler-enforced. It is
    not: the negative bound is `self.ttl_ns`, which the public `with_probe_ttl`
    may set to any non-negative value, so a caller passing 100 ms or less gets
    equal bounds in both directions and no asymmetry at all.

    That is safe, because `ttl_for` takes a `min` on BOTH branches and so a
    configured value can only ever SHORTEN a window - shorter is the cautious
    side of every one of these trades.

    It was not always true. Until round 22 the negative branch returned the
    configured value verbatim, so `with_probe_ttl(2s)` lengthened the
    unreachable window past its own 1 s default while this docstring, two module
    comments and a unit test all asserted the opposite. Nothing unsafe shipped -
    a stale `Unreachable` errs toward blocking - but an invariant asserted in
    five places and held in none is exactly the kind of claim these tests exist
    to catch, so the CODE was changed to match.

    NOT proven here: that any particular configured TTL is a good choice. Only
    that lowering it lowers BOTH bounds.
    """
    name = "a_shortened_ttl_shortens_both_directions"
    _assert_one_passed(_producer_test(name), name)


def test_the_admission_guard_survives_a_rename_and_a_nested_bound() -> None:
    """Two ways past the guard that closes SyRS SYS-75(a), both verified live.

    The gate-implementor enumeration and the exempt-function scan are the only
    things standing between the tree and a production type that always returns
    `Admitted`. Round 19 walked past both:

      * `use atp_market_data::RestartWindowGate as Gate;` then `impl Gate for X`
        matched nothing, while the check still printed "this enumeration is what
        makes it unforgeable".
      * `fn is_subscribed<T: Into<Vec<u8>>>` exceeded the one nesting level the
        generic-list pattern allowed, so the exemption was inherited by a second
        declaration that writes the consolidated registry.

    Both are exercised against the real checker in
    `tests/test_connectivity_contract.py`; this L7 test asserts the SHIPPED
    tools still reject them, so a refactor of either scan cannot quietly reopen
    the bypass.

    NOT proven here: that no OTHER spelling evades them. That is what the round
    log is for.
    """
    import subprocess

    checker = REPO_ROOT / "tests" / "test_connectivity_contract.py"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(checker),
            "-q",
            "-k",
            "renamed_trait_import or nested_generic_bound",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined[-2000:]
    assert "2 passed" in combined, combined[-2000:]


def test_the_implementor_enumeration_refuses_what_it_cannot_parse() -> None:
    """The backstop that ends "your regex did not anticipate this shape".

    Five separate review rounds found a shape the gate-implementor scan could
    not read: an arrow in the generics, a reference or tuple target, a
    parenthesised bound, two levels of nesting, an aliased trait name. Each fix
    was correct and each round found the next one, because a scan that fails to
    MATCH reports nothing and nothing reads as clean.

    The scan now counts what a loose pattern can see and refuses when the strict
    pass accounts for fewer. Any future unreadable shape turns the guard red
    instead of silently shrinking the set it calls closed - which is the only
    property that makes the "closed set" claim honest, and the one this L7 test
    protects because SyRS SYS-75(a) rests on it.

    NOT proven here: that the loose pattern sees every impl either. It is a
    lower bound, and a lower bound that fails closed is worth more than a
    precise one that fails open.
    """
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(REPO_ROOT / "tests" / "test_connectivity_contract.py"),
            "-q",
            "-k",
            "cannot_name or scan_that_finds_nothing",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined[-2000:]
    assert "2 passed" in combined, combined[-2000:]


def test_the_admission_guard_fails_closed_on_syntax_it_cannot_parse() -> None:
    """The round that showed a backstop can share the blind spot it backs up.

    Round 20 added a completeness count so an unreadable impl would turn the
    gate-implementor scan red instead of shrinking the set it calls closed.
    Round 21 showed it was bounded by `[^;]` - the SAME boundary the strict
    pattern used - so `impl<T: Into<[u8; 4]>> RestartWindowGate for AlwaysOpen`
    defeated both and scanned as clean. The same `;` made the exempt-declaration
    counter drop a declaration entirely rather than refuse it, so a second
    `fn is_subscribed<T: Into<[u8; 4]>>` inherited the exemption and reached the
    consolidated registry without consulting the window.

    Both are SyRS SYS-75(a) bypasses that leave every other guard, this suite
    and all three CLI proofs green, which is why they are pinned at L7.

    NOT proven here: that no further syntax defeats them. The backstop is what
    converts "unreadable" into a red gate, and that is the property under test.
    """
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(REPO_ROOT / "tests" / "test_connectivity_contract.py"),
            "-q",
            "-k",
            "semicolon_inside_a_generic_bound or backstop_does_not_share or two_hop_rename",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined[-2000:]
    assert "3 passed" in combined, combined[-2000:]
