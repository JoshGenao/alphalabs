"""SRS-RESV-004 / SyRS SYS-49b / SYS-49c / StRS SN-1.25 — Hot-Swap demotion before promotion.

The acceptance criterion, clause by clause:

    Current live strategy stops new signals, cancels resting IB orders, submits liquidation
    orders, waits for flat confirmation or the configured timeout defaulting to 60 seconds, and
    transitions to paper only after live positions are flat; on timeout, the swap enters
    demotion-pending state, dashboard/email/SMS notifications are sent, unfilled liquidation
    orders are canceled, and promotion is blocked until manual resolution.

L7 domain (safety) test. The diff touches live mode, order cancellation and liquidation
submission, so ``SAFETY_PATH_RE`` requires a paired ``tests/domain/`` test in the same commit —
this is it, and it is not a formality: every case below drives the REAL gate, the REAL sequence,
the REAL durable lockout and the REAL SRS-NOTIF-001 dispatcher through the SHIPPED operator
binary, exactly as ``python/atp_hotswap`` shells it.

**The case that matters most** is
``test_promotion_stays_blocked_across_a_retry_until_an_operator_resolves``. Before this feature
``resolve_demotion`` was a stateless single-attempt decision: a timeout returned ``Err`` and
blocked promotion *for that call*, and a later attempt whose probe reported flat promoted the
candidate over IB positions nobody had resolved. The lockout is what makes the AC's last clause
("promotion is blocked until manual resolution") true.

Transport tier: the IB socket and the SMTP/SMS transports are FIXTURES (the deferred
``atp-adapters`` and SRS-NOTIF-001 legs). Every drill asserts the tier it ran on, so evidence
made from these runs cannot present itself as a live one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_RELATIVE = Path("target") / "debug" / "resv004_hot_swap_demotion_cli"

DEMOTING = "live-momentum"
CANDIDATE = "paper-reversal"


def _cli_binary() -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot build the demotion CLI")
    build = subprocess.run(
        [cargo, "build", "-p", "atp-orchestrator", "--bin", "resv004_hot_swap_demotion_cli"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, (
        f"CLI build failed:\nSTDOUT:\n{build.stdout}\nSTDERR:\n{build.stderr}"
    )
    binary = REPO_ROOT / _CLI_RELATIVE
    assert binary.exists(), f"built binary missing at {binary}"
    return binary


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_cli_binary()), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _proof(stdout: str) -> dict[str, str]:
    """Parse the CLI's ``key:value`` proof lines, refusing a contradictory stream."""

    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        assert key not in values or values[key] == value, (
            f"contradictory {key!r} proof lines: {values.get(key)!r} then {value!r}"
        )
        values[key] = value
    return values


def _demote(state: Path, expect: str, *extra: str) -> dict[str, str]:
    result = _run(
        "demote",
        "--demoting",
        DEMOTING,
        "--candidate",
        CANDIDATE,
        "--state",
        str(state),
        "--expect",
        expect,
        *extra,
    )
    assert result.returncode == 0, (
        f"the drill did not produce the expected '{expect}' disposition:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    values = _proof(result.stdout)
    assert values["disposition"] == expect
    # Fixture-tier quarantine: the tier travels into every record made from this run.
    assert values["transports"] == "FIXTURE"
    return values


# --------------------------------------------------------------------------- #
# SYS-49b: the demotion sequence and the flat path
# --------------------------------------------------------------------------- #


def test_a_flat_demotion_runs_the_sequence_and_transitions_to_paper(tmp_path: Path) -> None:
    # AC clauses 1-5: stop new signals, cancel resting orders, submit liquidations, wait for
    # flat, and transition to paper ONLY after the positions are flat.
    values = _demote(
        tmp_path / "pending.json",
        "flat",
        "--position",
        "AAPL:100",
        "--position",
        "MSFT:-50",
        "--resting",
        "AAPL",
        "--resting",
        "MSFT",
        "--flat-after-seconds",
        "12",
    )

    assert values["sequence-ran"] == "true"
    assert values["signal-halt"] == "SUCCEEDED"
    assert values["resting-orders-cancelled"] == "2"
    assert values["resting-order-cancel-failures"] == "0"
    assert values["liquidations-submitted"] == "2"
    assert values["liquidation-failures"] == "0"
    assert values["sequence-fully-clean"] == "true"

    # Transitioned to paper, and promotion is not blocked.
    assert values["paper-transitions"] == "1"
    assert values["promotion-blocked"] == "false"
    assert values["completed-demoting-strategy-id"] == DEMOTING
    assert values["completed-candidate-strategy-id"] == CANDIDATE

    # A clean in-time demotion pages nobody and cancels nothing: those are the timeout path's
    # actions, and firing them here would be an operator page for a swap that worked.
    assert values["operator-pages"] == "0"
    assert values["unfilled-order-cancels"] == "0"
    assert values["event-liquidation-cancel"] == "NOT_ATTEMPTED"
    assert values["event-operator-alert"] == "NOT_ATTEMPTED"
    assert values["event-demotion-pending"] == "NOT_ATTEMPTED"


def test_a_demotion_that_cannot_silence_the_strategy_does_not_complete(tmp_path: Path) -> None:
    # A strategy that was never silenced can open a new position the instant after the probe
    # observes flat, so "flat" describes a moment rather than a state. The positions reach flat
    # here and the demotion is STILL refused — failing closed toward not promoting.
    result = _run(
        "demote",
        "--demoting",
        DEMOTING,
        "--candidate",
        CANDIDATE,
        "--state",
        str(tmp_path / "pending.json"),
        "--expect",
        "flat",
        "--position",
        "AAPL:100",
        "--flat-after-seconds",
        "5",
        "--fail-signal-halt",
    )
    assert result.returncode != 0, (
        f"a demotion whose signal halt failed must not report a completed swap:\n{result.stdout}"
    )
    values = _proof(result.stdout)
    assert values["disposition"] == "refused"
    assert values["signal-halt"] == "FAILED"
    assert values["sequence-safe-to-accept-flat"] == "false"
    assert "cease-new-signals" in values["sequence-degradation"]
    # The container did NOT move to paper.
    assert values["paper-transitions"] == "0"


# --------------------------------------------------------------------------- #
# SYS-49c: the timeout path
# --------------------------------------------------------------------------- #


def test_a_liquidation_timeout_pages_cancels_locks_out_and_blocks_promotion(
    tmp_path: Path,
) -> None:
    # AC clause 6, every conjunct: demotion-pending state, dashboard/email/SMS notifications,
    # the unfilled liquidation order cancelled, and promotion blocked.
    state = tmp_path / "pending.json"
    values = _demote(state, "demotion-pending", "--position", "AAPL:100", "--resting", "AAPL")

    assert values["promotion-blocked"] == "true"
    assert values["error-type"] == "HotSwapDemotionTimeout"

    # (b) the unfilled liquidation order is cancelled.
    assert values["unfilled-order-cancels"] == "1"
    assert values["event-liquidation-cancel"] == "SUCCEEDED"

    # (a) the operator is paged. Per required channel, individually: "the dispatcher was
    # called" is not "the operator was paged".
    assert values["operator-pages"] == "1"
    assert values["operator-page-delivered-email"] == "true"
    assert values["operator-page-delivered-sms"] == "true"
    assert values["event-operator-alert"] == "SUCCEEDED"

    # (c) the swap is held in demotion-pending — DURABLY, on disk.
    assert values["promotion-block-is-durable"] == "true"
    assert values["event-demotion-pending"] == "SUCCEEDED"
    assert state.exists(), "the demotion-pending lockout must have reached disk"

    # The container did NOT move to paper: positions are not flat.
    assert values["paper-transitions"] == "0"

    # And the operator surface reports it.
    status = _run("status", "--state", str(state))
    assert status.returncode == 0, status.stderr
    reported = _proof(status.stdout)
    assert reported["demotion-pending"] == "true"
    assert reported["promotion-blocked"] == "true"
    assert reported["demoting-strategy-id"] == DEMOTING
    assert reported["candidate-strategy-id"] == CANDIDATE


def test_promotion_stays_blocked_across_a_retry_until_an_operator_resolves(
    tmp_path: Path,
) -> None:
    # THE regression this feature exists for — the AC's final clause, "promotion is blocked
    # until manual resolution", exercised across three separate processes.
    state = tmp_path / "pending.json"

    # 1. A demotion times out and engages the lockout.
    _demote(state, "demotion-pending", "--position", "AAPL:100", "--resting", "AAPL")

    # 2. A RETRY whose positions would reach flat in one second. Statelessly this promotes;
    #    with the lockout it is refused before the swap even starts — no cancel, no
    #    liquidation, nothing touched on the account that an operator has not cleared.
    blocked = _demote(
        state,
        "blocked-pending",
        "--position",
        "AAPL:100",
        "--resting",
        "AAPL",
        "--flat-after-seconds",
        "1",
    )
    assert blocked["error-type"] == "HotSwapDemotionPending"
    assert blocked["promotion-blocked"] == "true"
    assert blocked["sequence-ran"] == "false"
    assert blocked["signal-halt"] == "NOT_ATTEMPTED"
    assert blocked["resting-orders-cancelled"] == "0"
    assert blocked["liquidations-submitted"] == "0"
    assert blocked["unfilled-order-cancels"] == "0"
    assert blocked["paper-transitions"] == "0"

    # 3. Manual resolution — and only with an operator acknowledgement.
    refused = _run("resolve", "--state", str(state), "--confirm", "   ")
    assert refused.returncode != 0, "a blank acknowledgement must not clear a lockout"
    assert "acknowledgement" in refused.stderr

    cleared = _run(
        "resolve", "--state", str(state), "--confirm", "operator: AAPL flattened by hand"
    )
    assert cleared.returncode == 0, cleared.stderr
    resolved = _proof(cleared.stdout)
    assert resolved["resolved"] == "true"
    assert resolved["demoting-strategy-id"] == DEMOTING
    # Read back from disk, not asserted from the call: a per-call success is not an end state.
    assert resolved["promotion-blocked"] == "false"

    # 4. Only now does the swap proceed.
    after = _demote(
        state,
        "flat",
        "--position",
        "AAPL:100",
        "--resting",
        "AAPL",
        "--flat-after-seconds",
        "1",
    )
    assert after["promotion-blocked"] == "false"
    assert after["paper-transitions"] == "1"


def test_an_unreadable_lockout_blocks_promotion_and_cannot_be_resolved_away(
    tmp_path: Path,
) -> None:
    # "Unreadable, absent, or unknown is NEVER empty." A corrupt lockout yields no record —
    # the same shape as no lockout — and reading it as "nothing is pending" would be a false
    # all-clear on the one question the file answers.
    state = tmp_path / "pending.json"
    state.write_text('{"magic":"NOT-OURS"}\n', encoding="utf-8")

    status = _run("status", "--state", str(state))
    assert status.returncode != 0, "an unreadable lockout must not report a demotion state"
    assert "UNREADABLE" in status.stderr

    # It blocks a swap exactly like a held one, before anything is attempted.
    blocked = _demote(state, "blocked-pending", "--position", "AAPL:100", "--resting", "AAPL")
    assert blocked["error-type"] == "HotSwapDemotionPending"
    assert blocked["sequence-ran"] == "false"

    # And it cannot be resolved away: removing it would discard the only description of the
    # unresolved positions.
    resolve = _run("resolve", "--state", str(state), "--confirm", "operator: looks fine to me")
    assert resolve.returncode != 0
    assert "still BLOCKS promotion" in resolve.stderr
    assert state.exists(), "the lockout must survive a refused resolution"


def test_a_failed_page_and_a_failed_cancel_are_recorded_and_still_block(tmp_path: Path) -> None:
    # A missed operator page and a failed IB cancel are each safety events in their own right.
    # Neither may suppress the other, neither may be indistinguishable from success, and
    # neither turns the refusal into an acceptance.
    values = _demote(
        tmp_path / "pending.json",
        "demotion-pending",
        "--position",
        "AAPL:100",
        "--fail-unfilled-cancel",
        "--fail-sms",
    )

    assert values["promotion-blocked"] == "true"
    # BOTH were attempted despite each failing.
    assert values["unfilled-order-cancels"] == "1"
    assert values["operator-pages"] == "1"
    # ...and each failure is observable rather than folded into a clean-looking event.
    assert values["event-liquidation-cancel"] == "FAILED"
    assert values["event-operator-alert"] == "FAILED"
    assert values["operator-page-delivered-sms"] == "false"
    # Email still went out: one bad channel must not suppress the others.
    assert values["operator-page-delivered-email"] == "true"
    # The lockout still landed, so the block outlives the call.
    assert values["promotion-block-is-durable"] == "true"


def test_an_unreadable_position_view_is_never_flat(tmp_path: Path) -> None:
    # The single way a demotion could wrongly report flat is to read an unreadable position
    # view as an empty one. It must time out instead — and say WHY, so an operator is not told
    # "the liquidation did not fill" when the truth is "we could not see the positions".
    values = _demote(
        tmp_path / "pending.json",
        "demotion-pending",
        "--position",
        "AAPL:100",
        "--position-fault",
        "connectivity",
    )
    assert values["promotion-blocked"] == "true"
    assert "CONNECTIVITY_BLOCKED" in values["probe-degradation"]


# --------------------------------------------------------------------------- #
# The operator surface refuses what it cannot state exactly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (("status", "--state"), "a flag with no value"),
        (("status", "--sorce", "x"), "an unknown flag"),
        (("status", "--state", "a", "--state", "b"), "a duplicate flag"),
        (("demote", "--demoting", "--candidate"), "a value that is itself a flag"),
    ],
)
def test_the_arg_parser_refuses_rather_than_falling_back(
    args: tuple[str, ...], reason: str
) -> None:
    # A scan-for-known-flags parser silently drops a typo and falls back to a default the
    # operator never chose, then reports success. On a surface that moves live positions that
    # is a fail-open.
    result = _run(*args)
    assert result.returncode != 0, f"{reason} must be refused, not defaulted:\n{result.stdout}"


def test_a_drill_cannot_pass_by_doing_something_other_than_it_claims(tmp_path: Path) -> None:
    # Non-vacuity guard on the harness itself: a scenario configured to reach flat but declared
    # as a timeout (or the reverse) must FAIL, or these drills would prove nothing about which
    # branch ran.
    state = tmp_path / "pending.json"
    wrong = _run(
        "demote",
        "--demoting",
        DEMOTING,
        "--candidate",
        CANDIDATE,
        "--state",
        str(state),
        "--expect",
        "demotion-pending",
        "--position",
        "AAPL:100",
        "--flat-after-seconds",
        "1",
    )
    assert wrong.returncode != 0
    assert "expected disposition 'demotion-pending'" in wrong.stderr
    assert "'flat'" in wrong.stderr
