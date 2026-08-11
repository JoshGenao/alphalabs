"""L4 boundary — ``SRS-RESV-004``: the Python demotion-state client's fail-closed paths.

``CliHotSwapDemotionSource`` is what turns the UI-5 pane's ``deferred:SRS-RESV-004`` cells into
real values. Every way the binary can fail to answer must reach the pane as *unknown*, never as
"no demotion is pending" — that reading is a false all-clear about whether a live changeover is
half-finished, which is the exact failure the demotion-pending lockout exists to prevent.

The subprocess is stubbed here (the drill itself is covered by
``tests/domain/test_hot_swap_demotion_sequence.py`` against the real binary); what is under test
is the client's response to each shape of answer.
"""

from __future__ import annotations

import subprocess

import pytest
from atp_dashboard.hotswap import (
    CliHotSwapDemotionSource,
    CompositeHotSwapStatusSource,
    HotSwapStatusProvider,
    HotSwapStatusUnavailable,
    HotSwapTriggerCliUnavailable,
    HotSwapTriggerOutputUnreadable,
)

BINARY = "/nonexistent/resv004_hot_swap_demotion_cli"

CLEAR_STDOUT = (
    "state-source:clear\n"
    "demotion-pending:false\n"
    "promotion-blocked:false\n"
    "demotion-detail:no demotion is pending\n"
)

PENDING_STDOUT = (
    "state-source:pending\n"
    "demotion-pending:true\n"
    "promotion-blocked:true\n"
    "demoting-strategy-id:live-momentum\n"
    "candidate-strategy-id:paper-reversal\n"
    "elapsed-seconds:60\n"
    "timeout-seconds:60\n"
    "liquidation-cancel:SUCCEEDED\n"
    "operator-alert:SUCCEEDED\n"
    "demotion-detail:a demotion of live-momentum timed out and is unresolved\n"
)


def _runner(stdout: str = "", stderr: str = "", returncode: int = 0):
    def run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run


def _source(**kwargs) -> CliHotSwapDemotionSource:
    return CliHotSwapDemotionSource("/tmp/pending.json", binary=BINARY, **kwargs)


def test_a_clear_lockout_is_a_real_false_not_a_deferred_cell() -> None:
    # A readable producer always has an answer. Returning None would render the cells deferred
    # — "SRS-RESV-004 has not produced this yet" — which stopped being true when it was mounted.
    state = _source(runner=_runner(CLEAR_STDOUT)).live_state()
    assert state == {
        "demotion_pending": False,
        "demotion_detail": "no demotion is pending",
    }
    # No changeover rung is claimed: a clear lockout says nothing about whether the
    # demotion-pending phase was ever reached.
    assert "sequence" not in state


def test_a_held_lockout_resolves_the_pending_cells_and_the_timeout_rung() -> None:
    state = _source(runner=_runner(PENDING_STDOUT)).live_state()
    assert state["demotion_pending"] is True
    assert "timed out" in str(state["demotion_detail"])
    assert state["sequence"] == {
        "demotion_pending": {
            "status": "BLOCKED",
            "detail": "a demotion of live-momentum timed out and is unresolved",
        }
    }


def test_a_nonzero_exit_is_unavailable_never_a_clear_lockout() -> None:
    source = _source(runner=_runner("", "lockout is UNREADABLE (bad magic)", returncode=1))
    with pytest.raises(HotSwapStatusUnavailable) as unavailable:
        source.live_state()
    assert "UNREADABLE" in str(unavailable.value)


def test_a_wedged_binary_is_unavailable_not_a_clear_lockout() -> None:
    def hang(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(HotSwapTriggerCliUnavailable):
        _source(runner=hang).live_state()


def test_a_missing_binary_is_unavailable_not_a_clear_lockout() -> None:
    def missing(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    with pytest.raises(HotSwapTriggerCliUnavailable) as unavailable:
        _source(runner=missing).live_state()
    assert "could not be launched" in str(unavailable.value)


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", "an empty answer"),
        ("state-source:something-else\ndemotion-pending:false\n", "an unknown state source"),
        ("state-source:clear\n", "no demotion-pending line"),
        ("state-source:clear\ndemotion-pending:maybe\n", "a non-boolean demotion-pending"),
        # A missing key must not read as False: absent-means-nothing-pending is the exact
        # false all-clear this surface avoids.
        ("state-source:clear\npromotion-blocked:false\n", "a missing demotion-pending key"),
        # The two facts must agree; picking one would publish a state nobody asserted.
        (
            "state-source:clear\ndemotion-pending:true\ndemotion-detail:x\n",
            "a self-contradicting stream",
        ),
        (
            "state-source:pending\ndemotion-pending:false\ndemotion-detail:x\n",
            "a self-contradicting stream, the other way",
        ),
        # A demotion state with no description is not a usable operator answer.
        ("state-source:clear\ndemotion-pending:false\n", "no demotion-detail line"),
    ],
)
def test_an_unreadable_answer_is_refused_rather_than_interpreted(stdout: str, reason: str) -> None:
    with pytest.raises(HotSwapStatusUnavailable):
        _source(runner=_runner(stdout)).live_state()


def test_contradictory_proof_lines_are_refused() -> None:
    # Last-one-wins would let a version-skewed binary have its second answer accepted as
    # durable evidence.
    stdout = CLEAR_STDOUT + "demotion-pending:true\n"
    with pytest.raises(HotSwapTriggerOutputUnreadable):
        _source(runner=_runner(stdout)).live_state()


def test_the_demotion_leg_answers_only_its_own_question() -> None:
    # The current live strategy is SRS-RESV-005's and the cool-down is SRS-RESV-006's. This
    # source invents neither.
    source = _source(runner=_runner(PENDING_STDOUT))
    assert source.trigger_config() is None
    assert source.promotion_candidate() is None


# --------------------------------------------------------------------------- #
# The composed pane
# --------------------------------------------------------------------------- #


class _RaisingTriggers:
    def trigger_config(self):
        raise HotSwapStatusUnavailable("trigger configuration unreadable")

    def promotion_candidate(self):
        return None


def test_an_unreadable_trigger_config_does_not_blank_a_readable_demotion_state() -> None:
    # The legs must fail INDEPENDENTLY. A composite that caught the exception itself would turn
    # one degraded producer into a silently all-deferred pane.
    provider = HotSwapStatusProvider(
        CompositeHotSwapStatusSource(
            triggers=_RaisingTriggers(),
            demotion=_source(runner=_runner(PENDING_STDOUT)),
        )
    )
    snapshot = provider.hot_swap_snapshot()

    assert snapshot["ok"] is False
    assert any("trigger configuration" in error for error in snapshot["errors"])
    # ...and the demotion cells are still RESOLVED.
    assert snapshot["demotion_pending"]["value"] is True
    assert snapshot["demotion_detail"]["value"]
    # ...while the trigger cells stay deferred rather than reading as "disabled".
    assert snapshot["auto_triggers_enabled"]["value"] is None


def test_an_unreadable_demotion_state_renders_unknown_not_none_pending() -> None:
    provider = HotSwapStatusProvider(
        CompositeHotSwapStatusSource(
            demotion=_source(runner=_runner("", "lockout UNREADABLE", returncode=1)),
        )
    )
    snapshot = provider.hot_swap_snapshot()

    assert snapshot["ok"] is False
    # Tri-state: None, NOT False. A False here would read as "no demotion is pending".
    assert snapshot["demotion_pending"]["value"] is None
    assert snapshot["demotion_pending"]["data_source"] == "deferred:SRS-RESV-004"


def test_an_unconfigured_pane_keeps_its_deferred_placeholder() -> None:
    # No demotion leg composed at all: the cells must stay deferred rather than claim False.
    # An unconfigured dashboard does not know whether a demotion is pending.
    snapshot = HotSwapStatusProvider(CompositeHotSwapStatusSource()).hot_swap_snapshot()
    assert snapshot["ok"] is True
    assert snapshot["demotion_pending"]["value"] is None
    assert snapshot["demotion_pending"]["data_source"] == "deferred:SRS-RESV-004"
