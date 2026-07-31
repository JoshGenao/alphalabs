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


def test_a_mounted_producer_reports_the_default_posture_as_known_off(tmp_path: Path) -> None:
    # Clause 2 ("automatic triggers default to disabled") has to be OBSERVABLE, not merely
    # unclaimed. Before this producer existed the chips read deferred — "SRS-RESV-003 has
    # not produced this yet" — which was honest then and is wrong now: with the source
    # mounted and readable, the runtime posture is known and it is disabled. A deferred cell
    # here would understate what the system can actually say, and leave the AC's default
    # clause unevidenced on the surface an operator reads.
    from atp_dashboard.hotswap import CliHotSwapTriggerSource, HotSwapStatusProvider

    state = tmp_path / "never-configured.json"
    assert not state.exists()
    snapshot = HotSwapStatusProvider(
        CliHotSwapTriggerSource(state, binary=_trigger_cli())
    ).hot_swap_snapshot()

    assert snapshot["ok"] is True, snapshot
    assert snapshot["auto_triggers_enabled"]["value"] is False, snapshot
    for chip in snapshot["auto_triggers_live"]:
        assert chip["enabled"]["value"] is False, chip
        assert chip["enabled"]["data_source"] == "hot_swap_state", chip
    # A KNOWN false, not an unknown — the two must stay distinguishable, because the
    # unreadable case still has to render as unknown (asserted above).
    assert state.exists() is False, "reading a configuration must not create one"


# --------------------------------------------------------------------------- #
# Concurrency: two operator actions at once must not lose a change or misattribute
# an audit record. Both surfaces (CLI and the threaded REST server) funnel through
# the same binary, so the guard lives there and these drive it directly.
# --------------------------------------------------------------------------- #


def test_concurrent_trigger_changes_never_lose_a_confirmed_change(tmp_path: Path) -> None:
    # Each --set-* is a read-modify-write over the WHOLE configuration. Unserialised, two
    # changes to DIFFERENT triggers both read the old state and both write a full
    # replacement, so the second silently discards the first while both callers are told
    # they succeeded — an operator sees a success for a change that is not on disk.
    import concurrent.futures

    state = tmp_path / "triggers.json"
    binary = _trigger_cli()
    changes = [
        ["--set-drawdown-threshold", "250"],
        ["--set-top-ranked", "on"],
        ["--set-highest-momentum", "on"],
    ]

    def apply(args: list[str]) -> int:
        return subprocess.run(
            [str(binary), "config", "--state", str(state), *args],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        ).returncode

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(changes)) as pool:
        codes = list(pool.map(apply, changes))
    assert all(code == 0 for code in codes), codes

    # Every confirmed change survived; none was overwritten by a concurrent sibling.
    final = _kv(_cli("config", "--state", str(state)).stdout)
    assert final["drawdown-demotion-enabled"] == "true", final
    assert final["drawdown-demotion-threshold-bps"] == "250", final
    assert final["top-ranked-promotion-enabled"] == "true", final
    assert final["highest-momentum-promotion-enabled"] == "true", final


def test_concurrent_manual_fires_each_get_their_own_record(tmp_path: Path) -> None:
    # The ordinal is the log's record count taken AFTER the append. Unserialised, a
    # concurrent fire lands between the two and the ordinal then addresses somebody else's
    # record — and that ordinal is the identity the REST surface hands back for the
    # trigger, so the audit trail would attribute a fire to the wrong request.
    import concurrent.futures

    log = tmp_path / "triggers.jsonl"
    binary = _trigger_cli()
    candidates = [f"cand-{index}" for index in range(6)]

    def fire(candidate: str) -> tuple[str, str]:
        result = subprocess.run(
            [
                str(binary),
                "manual",
                "--demoting",
                "alpha",
                "--candidate",
                candidate,
                "--log",
                str(log),
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return candidate, _kv(result.stdout)["trigger-record-ordinal"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        reported = list(pool.map(fire, candidates))

    ordinals = [ordinal for _candidate, ordinal in reported]
    assert sorted(int(o) for o in ordinals) == list(range(1, len(candidates) + 1)), ordinals

    # And each reported ordinal addresses the record for ITS OWN candidate.
    lines = log.read_text().splitlines()
    assert len(lines) == len(candidates), lines
    for candidate, ordinal in reported:
        assert f'"candidate_strategy_id":"{candidate}"' in lines[int(ordinal) - 1], (
            candidate,
            ordinal,
        )


def test_a_held_lock_refuses_the_write_rather_than_interleaving(tmp_path: Path) -> None:
    # Proves the guard is genuinely ON the write path (not merely present in the source):
    # with the lock held, a --set must refuse. Refusing is recoverable; two writers
    # interleaving over a read-modify-write is not.
    state = tmp_path / "triggers.json"
    assert _cli("config", "--state", str(state), "--set-top-ranked", "on").returncode == 0

    held = tmp_path / "triggers.json.lock"
    held.write_text("")  # stand in for another operation holding it
    try:
        refused = _cli("config", "--state", str(state), "--set-highest-momentum", "on")
        assert refused.returncode != 0, refused.stdout
        assert "another Hot-Swap trigger operation holds" in refused.stderr, refused.stderr
    finally:
        held.unlink()

    # The refused change was not applied, and the earlier one is intact.
    after = _kv(_cli("config", "--state", str(state)).stdout)
    assert after["highest-momentum-promotion-enabled"] == "false", after
    assert after["top-ranked-promotion-enabled"] == "true", after

    # A crashed holder is never silently stolen from — but once the file is gone the
    # next operation proceeds normally.
    assert _cli("config", "--state", str(state), "--set-highest-momentum", "on").returncode == 0
    assert (
        _kv(_cli("config", "--state", str(state)).stdout)["highest-momentum-promotion-enabled"]
        == "true"
    )


def test_a_held_log_lock_refuses_a_manual_fire(tmp_path: Path) -> None:
    # Same proof on the audit path: with the log lock held, the fire is refused rather
    # than appending and then reporting an ordinal that could address another record.
    log = tmp_path / "triggers.jsonl"
    held = tmp_path / "triggers.jsonl.lock"
    held.write_text("")
    try:
        refused = _cli("manual", "--demoting", "a", "--candidate", "b", "--log", str(log))
        assert refused.returncode != 0, refused.stdout
        assert "another Hot-Swap trigger operation holds" in refused.stderr, refused.stderr
        # Nothing was written: a refused fire leaves no audit record behind.
        assert not log.exists() or log.read_text() == ""
    finally:
        held.unlink()


def test_both_operator_surfaces_share_one_unavailable_exception(tmp_path: Path) -> None:
    # Not a naming quibble. The pane degrades on `HotSwapStatusUnavailable`; if the client
    # raises a same-named class from a different module, the provider's except-clause misses
    # it entirely and an unreadable configuration 500s the whole snapshot route instead of
    # rendering its honest error state. That defect shipped once already, which is why the
    # class now lives in the neutral atp_hotswap package and both surfaces re-export it.
    import atp_hotswap
    from atp_dashboard.hotswap import HotSwapStatusProvider, HotSwapStatusUnavailable
    from atp_orchestration import HotSwapStatusUnavailable as RestUnavailable

    assert HotSwapStatusUnavailable is atp_hotswap.HotSwapStatusUnavailable
    assert RestUnavailable is atp_hotswap.HotSwapStatusUnavailable

    # And the behaviour that identity buys: a torn configuration degrades, never raises out.
    state = tmp_path / "triggers.json"
    _cli("config", "--state", str(state), "--set-top-ranked", "on")
    state.write_text('{"magic":"ATP-HOT-SWAP-TRIGGER-CONFIG","schema_version":1,"top_')
    snapshot = HotSwapStatusProvider(
        atp_hotswap.CliHotSwapTriggerSource(state, binary=_trigger_cli())
    ).hot_swap_snapshot()
    assert snapshot["ok"] is False, snapshot
    assert snapshot["auto_triggers_enabled"]["value"] is None, snapshot


def test_neither_operator_surface_imports_the_other(tmp_path: Path) -> None:
    # The dependency direction the runtime documents: surfaces compose onto atp_runtime from
    # above and never onto each other. The shared client is what made this tempting to
    # break, so the rule is asserted rather than trusted to review.
    import ast

    root = REPO_ROOT / "python"
    offenders: list[str] = []
    for package, forbidden in (
        ("atp_orchestration", "atp_dashboard"),
        ("atp_dashboard", "atp_orchestration"),
        ("atp_hotswap", "atp_dashboard"),
        ("atp_hotswap", "atp_orchestration"),
    ):
        for source in (root / package).rglob("*.py"):
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any(name == forbidden or name.startswith(f"{forbidden}.") for name in names):
                    offenders.append(f"{source.relative_to(root)} imports {forbidden}")
    assert not offenders, offenders


def test_a_self_swap_is_refused_by_the_domain_layer_not_just_the_rest_wrapper(
    tmp_path: Path,
) -> None:
    # A swap names two strategies. Demoting and promoting the SAME one is not a Hot-Swap,
    # and logging it would hand SRS-RESV-004 a proposal asking it to take the live strategy
    # down in order to put it back. The REST handler rejected this, but the CLI and the Rust
    # API — two of SYS-49a's three operator arms — reached the domain path directly, so the
    # guard belongs in the domain layer where every arm passes through it.
    log = tmp_path / "triggers.jsonl"
    result = _cli("manual", "--demoting", "alpha", "--candidate", "alpha", "--log", str(log))

    assert result.returncode != 0, result.stdout
    assert "must name two different strategies" in result.stderr, result.stderr
    # Refused outright: no proposal was created, so nothing claims to have fired...
    assert "fired:" not in result.stdout, result.stdout
    assert "manual-refused:SAME_STRATEGY" in result.stdout, result.stdout
    # ...and no audit record exists for a swap that was never proposed.
    assert not log.exists() or log.read_text() == "", log.read_text()


def test_an_unreadable_log_refuses_the_fire_instead_of_joining_it(tmp_path: Path) -> None:
    # The contradiction this ordering prevents: counting the log AFTER appending meant a
    # malformed prior line failed the command once the new record was already fsynced. The
    # caller was told the trigger did not fire while the durable log said it did — and the
    # REST wrapper turns that nonzero exit into exactly that claim.
    log = tmp_path / "triggers.jsonl"
    log.write_text('{"kind":"MANUAL_PROMOTION","candidate_strategy_id":\n')  # torn prior line
    before = log.read_text()

    result = _cli("manual", "--demoting", "alpha", "--candidate", "beta", "--log", str(log))

    assert result.returncode != 0, result.stdout
    # Refused BEFORE writing: the log is byte-identical, so the failure report and the
    # durable record agree that nothing fired.
    assert log.read_text() == before, log.read_text()
    assert "MANUAL_PROMOTION" not in result.stdout or "beta" not in log.read_text()


def test_the_rest_success_is_correlated_to_the_record_the_binary_wrote(tmp_path: Path) -> None:
    # Three-way agreement against the REAL binary: the kind, the demoting id and the
    # candidate id the CLI reports firing must all match what was requested before any
    # success is reported — and the response then carries the PROOF's values, not the
    # request's, so a 200 can never describe a trigger other than the one on disk.
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
            body={"demoting_strategy_id": "momentum-v3", "candidate_strategy_id": "meanrev-v7"},
            confirmed=True,
        )
    )
    assert result.status_code == 200, result.body
    assert result.body["trigger_kind"] == "MANUAL_PROMOTION", result.body
    assert result.body["demoting_strategy_id"] == "momentum-v3", result.body
    assert result.body["candidate_strategy_id"] == "meanrev-v7", result.body

    # The record at the reported ordinal names exactly those strategies.
    record = log.read_text().splitlines()[int(result.body["trigger_id"]) - 1]
    assert '"demoting_strategy_id":"momentum-v3"' in record, record
    assert '"candidate_strategy_id":"meanrev-v7"' in record, record


def test_a_non_string_strategy_id_never_reaches_the_audit_log(tmp_path: Path) -> None:
    # str() over an arbitrary JSON value is a coercion, not a read: `True` would become the
    # strategy "True" in a durable audit record. Refused before the binary is invoked, so no
    # record can exist for it.
    from atp_orchestration import mount_hot_swap_triggers
    from atp_runtime import OperatorInterfaceRuntime
    from atp_runtime.errors import InterfaceError
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

    for bad in (True, 42, {"x": 1}, ["a"]):
        with pytest.raises(InterfaceError):
            handler.handle(
                Request(
                    surface=Surface.REST,
                    operation=key,
                    method="POST",
                    body={"demoting_strategy_id": "alpha", "candidate_strategy_id": bad},
                    confirmed=True,
                )
            )
    assert not log.exists(), log.read_text()


def test_a_misspelled_rest_trigger_field_never_persists_a_partial_change(
    tmp_path: Path,
) -> None:
    # End to end against the REAL binary: a typo'd flag must not quietly apply its correctly
    # spelled sibling and report success. The operator would leave believing both triggers
    # are armed, and the durable file would disagree.
    from atp_orchestration import mount_hot_swap_triggers
    from atp_runtime import OperatorInterfaceRuntime
    from atp_runtime.errors import InterfaceError
    from atp_runtime.registry import OperationKey, Request, Surface

    state = tmp_path / "triggers.json"
    runtime = OperatorInterfaceRuntime()
    mount_hot_swap_triggers(
        runtime,
        state_path=state,
        log_path=tmp_path / "triggers.jsonl",
        binary=_trigger_cli(),
    )
    key = OperationKey(Surface.REST, "PUT /api/v1/hot-swap/triggers")
    handler = runtime.registry.resolve(key, deferred=None)  # type: ignore[arg-type]

    with pytest.raises(InterfaceError):
        handler.handle(
            Request(
                surface=Surface.REST,
                operation=key,
                method="PUT",
                body={
                    "drawdown_demotion_enabledd": True,
                    "top_ranked_promotion_enabled": True,
                },
                confirmed=True,
            )
        )

    # Nothing persisted at all: not the typo, and not its correctly spelled sibling.
    assert not state.exists(), state.read_text()
    after = _kv(_cli("config", "--state", str(state)).stdout)
    assert after["any-automatic-enabled"] == "false", after


@pytest.mark.parametrize("bad_id", ["", "   ", "\t\n"], ids=["empty", "spaces", "blank"])
def test_the_cli_never_writes_a_record_its_own_reader_would_refuse(
    tmp_path: Path, bad_id: str
) -> None:
    # Self-poisoning: validate_trigger_log_line refuses a record whose strategy ids are
    # empty or blank, so writing one would leave a line THIS build cannot read back. Every
    # later count or fire against that log then fails on a record we wrote ourselves, while
    # the fire that produced it reported success.
    #
    # Scoped to emptiness on purpose: an id with an INTERIOR control character round-trips
    # fine (the writer escapes it), and resv_3_control_characters_in_an_id_do_not_poison_
    # the_log pins that, so refusing it here would delete a working guarantee.
    log = tmp_path / "triggers.jsonl"
    result = _cli("manual", "--demoting", "alpha", "--candidate", bad_id, "--log", str(log))

    assert result.returncode != 0, result.stdout
    assert not log.exists(), log.read_text()

    # Non-vacuous: a healthy id still fires and the log stays readable afterwards.
    ok = _cli("manual", "--demoting", "alpha", "--candidate", "beta", "--log", str(log))
    assert ok.returncode == 0, ok.stderr
    assert _kv(ok.stdout)["trigger-record-ordinal"] == "1", ok.stdout


def test_evaluation_inputs_obey_the_same_id_rule(tmp_path: Path) -> None:
    # Every writer into the trigger log obeys the reader's invariant, not just the manual
    # one: --live and --rank ids reach the same records through the automatic path.
    log = tmp_path / "triggers.jsonl"
    blank_live = _cli("evaluate", "--live", "   ", "--log", str(log))
    assert blank_live.returncode != 0, blank_live.stdout

    blank_rank = _cli("evaluate", "--live", "alpha", "--rank", ":1:2.0:0.5", "--log", str(log))
    assert blank_rank.returncode != 0, blank_rank.stdout
    assert not log.exists(), log.read_text()


def test_a_relative_state_path_is_still_durably_published(tmp_path: Path) -> None:
    # A relative `--state triggers.json` has an EMPTY parent, not an absent one. Filtering it
    # away skipped the directory fsync that makes the publishing rename crash-durable — so a
    # just-confirmed configuration could revert, leaving an automatic trigger armed that the
    # operator believes they disabled. The observable half here is that a relative path still
    # round-trips correctly from the directory it was written in.
    binary = _trigger_cli()
    written = subprocess.run(
        [str(binary), "config", "--state", "triggers.json", "--set-drawdown-threshold", "250"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert written.returncode == 0, written.stderr
    assert (tmp_path / "triggers.json").is_file()
    # No scratch or lock residue left beside it.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["triggers.json"], list(tmp_path.iterdir())

    reread = subprocess.run(
        [str(binary), "config", "--state", "triggers.json"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    values = _kv(reread.stdout)
    assert values["config-source"] == "persisted", reread.stdout
    assert values["drawdown-demotion-threshold-bps"] == "250", reread.stdout


def test_a_relative_log_path_still_fires_and_reads_back(tmp_path: Path) -> None:
    # Same shape on the audit path: a newly created log's DIRECTORY ENTRY also has to be made
    # durable, or the first fire reports logged:true with an ordinal a crash would erase.
    binary = _trigger_cli()
    fired = subprocess.run(
        [str(binary), "manual", "--demoting", "alpha", "--candidate", "beta", "--log", "t.jsonl"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert fired.returncode == 0, fired.stderr
    assert _kv(fired.stdout)["trigger-record-ordinal"] == "1", fired.stdout
    assert '"candidate_strategy_id":"beta"' in (tmp_path / "t.jsonl").read_text()
