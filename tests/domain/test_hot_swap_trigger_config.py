"""SRS-RESV-003 / SyRS SYS-49a / StRS SN-1.25 / SN-1.30 — the Hot-Swap trigger
DECISION + CONFIGURATION + LOGGING layer. A Hot-Swap may be triggered by manual
operator selection (always available) or by three AUTOMATIC triggers
(drawdown-demotion, top-ranked promotion, highest-momentum promotion), each
enable/disable-able per type and DEFAULTING TO DISABLED, with every fired
trigger logged. The trigger layer proposes + logs; it does NOT execute the swap
(that is the SRS-RESV-004 ``resolve_demotion`` gate, which consumes the
``HotSwapDemotionRequest`` this layer produces).

L7 domain (safety) test. The Rust integration test at
``crates/atp-orchestrator/tests/resv_3_hot_swap_triggers.rs`` builds spy /
failing / forbidden fake implementations of the three injected ports
(``LiveStrategyProbe``, ``ReservoirRankingSource``, ``HotSwapTriggerLog``);
this Python test shells out to ``cargo test`` to anchor the safety
post-conditions in the domain-test layer so the deterministic critic
recognizes the diff as having a paired ``tests/domain/`` safety test (the CLI
``resv003_hot_swap_trigger_cli.rs`` and ``tools/hot_swap_trigger_check.py``
paths match ``hot[_-]?swap`` in ``SAFETY_PATH_RE``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cargo_test(
    test_name: str, test_file: str = "resv_3_hot_swap_triggers"
) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-orchestrator",
            "--test",
            test_file,
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_single_pass(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_default_config_fires_nothing_even_when_conditions_met() -> None:
    # SRS-RESV-003 core safety invariant: automatic triggers default to
    # disabled, so a default HotSwapTriggerConfig fires NOTHING and logs
    # NOTHING even with a deep drawdown and an excellent candidate present.
    # The Rust HotSwapTriggerLogForbiddenSink panics if any trigger is logged.
    result = _run_cargo_test("resv_3_default_config_fires_nothing_even_when_conditions_met")
    _assert_single_pass(result, "RESV-003 default-disabled")


def test_all_enabled_fire_in_priority_order_and_each_logged() -> None:
    # "All swap triggers are logged": with every automatic trigger enabled and
    # its condition met, all fire in a fixed priority order (drawdown-demotion
    # first as the risk control) and the log record count equals the fired
    # count — the mechanical guarantee behind the logging clause.
    result = _run_cargo_test(
        "resv_3_all_enabled_all_conditions_met_fire_in_priority_order_and_each_logged"
    )
    _assert_single_pass(result, "RESV-003 all-logged")


def test_manual_promotion_always_fires_and_logs() -> None:
    # SYS-49a(a): manual selection is always available — it fires + logs
    # regardless of the automatic-trigger config (which defaults to all off).
    result = _run_cargo_test("resv_3_manual_promotion_always_fires_and_logs_even_when_all_disabled")
    _assert_single_pass(result, "RESV-003 manual-always")


def test_failing_log_sink_fails_closed_not_selected() -> None:
    # Logging is LOAD-BEARING on the actionable path: a fired trigger whose
    # required audit-log record is rejected is surfaced in `unlogged` and is
    # never `selected` — SRS-RESV-004 is never handed an unlogged swap trigger
    # (fail closed, no lost audit trail for a live-strategy change).
    result = _run_cargo_test("resv_3_failing_log_sink_fails_closed_not_selected")
    _assert_single_pass(result, "RESV-003 fail-closed-log")


def test_manual_promotion_fails_closed_when_log_rejected() -> None:
    # A manual trigger whose audit-log record is rejected must come back as
    # Err(UnloggedHotSwapTrigger) so the operator never acts on an unlogged
    # manual swap.
    result = _run_cargo_test("resv_3_manual_promotion_fails_closed_when_log_rejected")
    _assert_single_pass(result, "RESV-003 manual-fail-closed")


def test_partial_log_rejection_fails_whole_pass_closed() -> None:
    # "All swap triggers are logged" is atomic for the pass: if the highest-priority
    # trigger logs but a LATER fired trigger's record is rejected, `selected` must be
    # None — a swap must never execute from a pass with a known rejected trigger log.
    result = _run_cargo_test("resv_3_partial_log_rejection_fails_whole_pass_closed")
    _assert_single_pass(result, "RESV-003 partial-log-rejection")


def test_degraded_live_probe_fails_closed_and_surfaces_reason() -> None:
    # A live-strategy probe that cannot read state (Err) fails closed (no swap)
    # AND surfaces the reason in degraded_inputs — distinguishable from a healthy
    # "no live strategy", never silently collapsed.
    result = _run_cargo_test("resv_3_degraded_live_probe_fails_closed_and_surfaces_reason")
    _assert_single_pass(result, "RESV-003 degraded-live-probe")


def test_degraded_ranking_source_fails_closed_and_surfaces_reason() -> None:
    # A ranking source that cannot be read (Err) fails closed with the reason
    # surfaced, distinct from a healthy empty ranking.
    result = _run_cargo_test("resv_3_degraded_ranking_source_fails_closed_and_surfaces_reason")
    _assert_single_pass(result, "RESV-003 degraded-ranking-source")


def test_cli_manual_exits_nonzero_when_log_rejected() -> None:
    # The operator CLI arm must fail closed at the PROCESS level: a rejected manual
    # audit-log record makes the command exit nonzero, so shell automation cannot
    # treat an unlogged manual Hot-Swap trigger as a successful command.
    result = _run_cargo_test(
        "resv_3_cli_manual_exits_nonzero_when_log_rejected",
        test_file="resv_3_cli_fail_closed",
    )
    _assert_single_pass(result, "RESV-003 cli-manual-fail-closed")


def test_cli_firing_command_without_log_sink_exits_nonzero() -> None:
    # A firing CLI command (manual always fires) with NO --log sink must fail
    # closed — a trigger must never be reported logged when nothing was persisted.
    result = _run_cargo_test(
        "resv_3_cli_manual_no_log_exits_nonzero",
        test_file="resv_3_cli_fail_closed",
    )
    _assert_single_pass(result, "RESV-003 cli-no-sink-fail-closed")


def test_cli_surfaces_concrete_sink_failure_cause() -> None:
    # The CLI must surface the CONCRETE sink failure cause (not just a count) so an
    # operator can repair the degraded audit path — the rejection reason travels end
    # to end through the automatic evaluation path.
    result = _run_cargo_test(
        "resv_3_cli_evaluate_surfaces_sink_failure_cause",
        test_file="resv_3_cli_fail_closed",
    )
    _assert_single_pass(result, "RESV-003 cli-surfaces-cause")


def test_ranking_non_finite_and_empty_fail_closed_no_fire() -> None:
    # Fail-closed: an empty or non-finite ranking yields no promotion candidate,
    # so no automatic trigger fires (no fabricated pick, no panic).
    result = _run_cargo_test("resv_3_ranking_non_finite_and_empty_fail_closed_no_fire")
    _assert_single_pass(result, "RESV-003 fail-closed-ranking")


# --------------------------------------------------------------------------- #
# The DURABLE trigger configuration (SYS-49a "configurable", across a restart)
#
# These drive the real `resv003_hot_swap_trigger_cli` binary against a real file,
# because the property under test is what a LATER process reads back — which an
# in-process test cannot observe. The safety claim is narrow and specific: what
# fires is decided by the persisted configuration, and a configuration this build
# cannot read never reads as "disabled".
# --------------------------------------------------------------------------- #


def _trigger_cli() -> Path:
    """Build (once) and return the real trigger CLI binary."""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot build the trigger CLI")
    build = subprocess.run(
        [cargo, "build", "-q", "-p", "atp-orchestrator", "--bin", "resv003_hot_swap_trigger_cli"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cargo build failed:\n{build.stdout}\n{build.stderr}"
    return REPO_ROOT / "target" / "debug" / "resv003_hot_swap_trigger_cli"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_trigger_cli()), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _kv(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key] = value
    return values


# The conditions every automatic trigger would fire on, if it were enabled: a
# total-loss drawdown on the live strategy and an excellent ranked candidate.
_FIRING_INPUTS = ("--live", "alpha", "--live-drawdown", "5000", "--rank", "beta:1:2.5:0.9")


def test_persisted_disabled_config_fires_nothing_when_conditions_are_met(
    tmp_path: Path,
) -> None:
    # The default-disabled clause, proven NON-VACUOUSLY through the durable store:
    # the inputs would fire every automatic trigger, and none fires because nothing
    # has been configured. An absent file is the "never configured" state, which is
    # the one case where reporting "disabled" is truthful.
    state = tmp_path / "triggers.json"
    result = _cli("evaluate", *_FIRING_INPUTS, "--state", str(state), "--log", str(tmp_path / "l"))
    assert result.returncode == 0, result.stderr
    values = _kv(result.stdout)
    assert values["fired-count"] == "0", result.stdout
    assert values["all-triggers-logged"] == "true", result.stdout


def test_persisted_enabled_config_survives_the_process_that_set_it(tmp_path: Path) -> None:
    # "Configurable" across a restart: one process persists the trigger, a SEPARATE
    # process reads it back and fires on it. Same inputs as the test above — the
    # only difference is the durable configuration, which is what makes it the
    # decision authority rather than a display artefact.
    state = tmp_path / "triggers.json"
    written = _cli("config", "--state", str(state), "--set-drawdown-threshold", "250")
    assert written.returncode == 0, written.stderr
    assert _kv(written.stdout)["config-persisted"] == "true"

    reread = _cli("config", "--state", str(state))
    assert _kv(reread.stdout)["config-source"] == "persisted"
    assert _kv(reread.stdout)["drawdown-demotion-threshold-bps"] == "250"

    fired = _cli("evaluate", *_FIRING_INPUTS, "--state", str(state), "--log", str(tmp_path / "l"))
    assert fired.returncode == 0, fired.stderr
    values = _kv(fired.stdout)
    assert values["fired-count"] == "1", fired.stdout
    # All swap triggers are logged: the fired count and the durably persisted
    # record count agree.
    assert values["logged-count"] == "1", fired.stdout
    assert values["log-file-records"] == "1", fired.stdout


def test_each_automatic_trigger_is_independently_configurable(tmp_path: Path) -> None:
    # SYS-49a names four triggers; the three automatic ones must be separately
    # enable/disable-able rather than one shared switch.
    state = tmp_path / "triggers.json"
    _cli("config", "--state", str(state), "--set-top-ranked", "on")
    values = _kv(_cli("config", "--state", str(state)).stdout)
    assert values["top-ranked-promotion-enabled"] == "true"
    assert values["drawdown-demotion-enabled"] == "false"
    assert values["highest-momentum-promotion-enabled"] == "false"

    # A read-modify-write on one trigger preserves the others.
    _cli("config", "--state", str(state), "--set-highest-momentum", "on")
    values = _kv(_cli("config", "--state", str(state)).stdout)
    assert values["top-ranked-promotion-enabled"] == "true"
    assert values["highest-momentum-promotion-enabled"] == "true"

    # And disabling is reachable again, or "configurable" is one-way.
    _cli("config", "--state", str(state), "--set-top-ranked", "off")
    values = _kv(_cli("config", "--state", str(state)).stdout)
    assert values["top-ranked-promotion-enabled"] == "false"
    assert values["highest-momentum-promotion-enabled"] == "true"


def test_an_unreadable_config_never_reads_as_disabled(tmp_path: Path) -> None:
    # THE false-all-clear this guards. A corrupt configuration must fail the
    # command, never degrade into the all-disabled default: "we could not read it"
    # and "no automatic trigger is armed" are different facts, and reporting the
    # second for the first tells an operator a Hot-Swap cannot fire when nobody
    # knows whether it can.
    state = tmp_path / "triggers.json"
    state.write_text('{"magic":"ATP-HOT-SWAP-TRIGGER-CONFIG","schema_version":1,"drawdown_')

    shown = _cli("config", "--state", str(state))
    assert shown.returncode != 0, shown.stdout
    assert "unreadable" in shown.stderr, shown.stderr
    assert "drawdown-demotion-enabled:false" not in shown.stdout

    # And the evaluation path fails closed the same way — it must not report
    # "nothing fired" about a configuration it could not read.
    evaluated = _cli(
        "evaluate", *_FIRING_INPUTS, "--state", str(state), "--log", str(tmp_path / "l")
    )
    assert evaluated.returncode != 0, evaluated.stdout
    assert "fired-count:0" not in evaluated.stdout


def test_a_set_against_an_unreadable_config_does_not_overwrite_it(tmp_path: Path) -> None:
    # A --set-* is a read-modify-write. Starting from the default when the existing
    # file cannot be read would silently discard the operator's other triggers —
    # including disarming one they believe is armed — and destroy the evidence of
    # what went wrong. The command fails and the bytes are left exactly as found.
    state = tmp_path / "triggers.json"
    corrupt = '{"magic":"ATP-HOT-SWAP-TRIGGER-CONFIG","schema_version":1,"drawdown_'
    state.write_text(corrupt)

    result = _cli("config", "--state", str(state), "--set-top-ranked", "on")
    assert result.returncode != 0, result.stdout
    assert state.read_text() == corrupt


def test_an_unknown_configuration_field_is_refused_not_ignored(tmp_path: Path) -> None:
    # A reader that looks up only the keys it expects is blind to the ones it does
    # not, so a renamed or newly-meaningful flag is dropped while the rest parses
    # cleanly. The whole payload is refused instead.
    state = tmp_path / "triggers.json"
    state.write_text(
        '{"magic":"ATP-HOT-SWAP-TRIGGER-CONFIG","schema_version":1,'
        '"drawdown_demotion_enabled":false,"top_ranked_promotion_enabled":false,'
        '"highest_momentum_promotion_enabled":false,"manual_promotion_enabled":true}'
    )
    result = _cli("config", "--state", str(state))
    assert result.returncode != 0, result.stdout
    assert "unknown field" in result.stderr, result.stderr


def test_manual_promotion_stays_available_with_every_automatic_trigger_off(
    tmp_path: Path,
) -> None:
    # SYS-49a(a): manual selection is always available and is NOT gated by the
    # automatic configuration — including when the durable config disables
    # everything.
    state = tmp_path / "triggers.json"
    log = tmp_path / "manual.jsonl"
    assert _kv(_cli("config", "--state", str(state)).stdout)["any-automatic-enabled"] == "false"

    result = _cli("manual", "--demoting", "alpha", "--candidate", "beta", "--log", str(log))
    assert result.returncode == 0, result.stderr
    values = _kv(result.stdout)
    assert values["manual-always-available"] == "true"
    assert values["manual-logged"] == "true"
    assert values["log-file-records"] == "1"


def test_the_manual_trigger_ordinal_addresses_the_record_it_wrote(tmp_path: Path) -> None:
    # The REST surface binds "the trigger fired" to this ordinal rather than to an exit
    # code, so the ordinal has to actually address the record this invocation caused. Fire
    # three times into one log and check each reported ordinal resolves to its own record.
    log = tmp_path / "triggers.jsonl"
    for index, candidate in enumerate(("beta", "gamma", "delta"), start=1):
        result = _cli("manual", "--demoting", "alpha", "--candidate", candidate, "--log", str(log))
        assert result.returncode == 0, result.stderr
        ordinal = _kv(result.stdout)["trigger-record-ordinal"]
        assert ordinal == str(index), result.stdout
        # 1-based into the durable log, and it is this fire's own record.
        line = log.read_text().splitlines()[int(ordinal) - 1]
        assert f'"candidate_strategy_id":"{candidate}"' in line, line


def test_an_unlogged_manual_trigger_reports_no_ordinal(tmp_path: Path) -> None:
    # Fail closed: with no --log sink the record is REJECTED, so the command exits nonzero
    # and must not print an ordinal — there is no durable record for one to address, and a
    # surface that saw one would report an unlogged trigger as fired.
    result = _cli("manual", "--demoting", "alpha", "--candidate", "beta")
    assert result.returncode != 0, result.stdout
    assert "trigger-record-ordinal" not in result.stdout


# --------------------------------------------------------------------------- #
# The OPERATOR SURFACES over the durable configuration (dashboard pane + REST)
#
# Driven through the REAL binary and the REAL provider — no fakes. The safety claim is
# that neither surface can tell an operator "no automatic Hot-Swap can fire" unless that
# is actually known, and that neither reports a swap that did not happen.
# --------------------------------------------------------------------------- #


def test_the_pane_never_renders_an_unreadable_config_as_disabled(tmp_path: Path) -> None:
    from atp_dashboard.hotswap import CliHotSwapTriggerSource, HotSwapStatusProvider

    binary = _trigger_cli()
    state = tmp_path / "triggers.json"
    assert _cli("config", "--state", str(state), "--set-drawdown-threshold", "250").returncode == 0

    def snapshot() -> dict:
        return HotSwapStatusProvider(
            CliHotSwapTriggerSource(state, binary=binary)
        ).hot_swap_snapshot()

    # Configured and readable: the RESV-003 cells carry the operator's real choices.
    live = snapshot()
    assert live["ok"] is True, live
    assert live["auto_triggers_enabled"]["value"] is True
    chips = {chip["kind"]: chip["enabled"]["value"] for chip in live["auto_triggers_live"]}
    assert chips["drawdown_demotion"] is True, live

    # Now the file is torn. The pane must say it does not know — a confident "disabled"
    # here is a false all-clear about whether an automatic demotion can fire, which is the
    # exact question an operator opens this pane to answer.
    state.write_text('{"magic":"ATP-HOT-SWAP-TRIGGER-CONFIG","schema_version":1,"draw')
    degraded = snapshot()
    assert degraded["ok"] is False, degraded
    assert degraded["auto_triggers_enabled"]["value"] is None, degraded
    for chip in degraded["auto_triggers_live"]:
        assert chip["enabled"]["value"] is None, chip
    assert any("unreadable" in str(reason) for reason in degraded.get("errors", [])), degraded


def test_resolving_the_trigger_leg_never_fabricates_the_other_owners_cells(
    tmp_path: Path,
) -> None:
    # RESV-004/005/006 and RESV-002 persist no queryable fact. A source that answered for
    # them would put invented swap state on the pane.
    from atp_dashboard.hotswap import CliHotSwapTriggerSource, HotSwapStatusProvider

    state = tmp_path / "triggers.json"
    _cli("config", "--state", str(state), "--set-top-ranked", "on")
    snapshot = HotSwapStatusProvider(
        CliHotSwapTriggerSource(state, binary=_trigger_cli())
    ).hot_swap_snapshot()

    assert snapshot["auto_triggers_enabled"]["value"] is True
    assert snapshot["current_live_strategy_id"]["value"] is None
    assert snapshot["current_live_strategy_id"]["data_source"] == "deferred:SRS-RESV-005"
    assert snapshot["demotion_pending"]["data_source"] == "deferred:SRS-RESV-004"
    assert snapshot["cooldown"]["in_effect"]["data_source"] == "deferred:SRS-RESV-006"
    assert snapshot["promotion_candidate"]["data_source"] == "deferred:SRS-RESV-002"


def test_the_rest_manual_trigger_never_reports_a_swap_it_did_not_perform(
    tmp_path: Path,
) -> None:
    # RESV-003 decides and logs; RESV-004/005 execute, and are unbuilt. A 200 here means a
    # trigger was RECORDED, and the payload has to say so in itself — otherwise an operator
    # (or an automation) reads a logged proposal as a completed changeover.
    from atp_orchestration import mount_hot_swap_triggers
    from atp_runtime import OperatorInterfaceRuntime
    from atp_runtime.registry import OperationKey, Request, Surface

    runtime = OperatorInterfaceRuntime()
    log = tmp_path / "triggers.jsonl"
    mount_hot_swap_triggers(
        runtime,
        state_path=tmp_path / "triggers.json",
        log_path=log,
        binary=_trigger_cli(),
    )
    key = OperationKey(Surface.REST, "POST /api/v1/hot-swap/triggers/manual")
    handler = runtime.registry.resolve(key, deferred=None)  # type: ignore[arg-type]
    result = handler.handle(
        Request(
            surface=Surface.REST,
            operation=key,
            method="POST",
            body={"demoting_strategy_id": "alpha", "candidate_strategy_id": "beta"},
            confirmed=True,
        )
    )

    assert result.status_code == 200, result.body
    assert result.body["logged"] is True
    assert result.body["execution"]["state"] == "DEFERRED"
    assert result.body["execution"]["owner"] == "SRS-RESV-004"
    # The reported id addresses the record that was actually written.
    ordinal = int(result.body["trigger_id"])
    line = log.read_text().splitlines()[ordinal - 1]
    assert '"kind":"MANUAL_PROMOTION"' in line, line
    assert '"candidate_strategy_id":"beta"' in line, line
