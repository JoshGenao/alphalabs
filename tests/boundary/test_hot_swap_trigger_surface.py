"""L4 boundary — the ``SRS-RESV-003`` Hot-Swap TRIGGER surface on the runtime.

``mount_hot_swap_triggers`` is a composition-time opt-in that binds three routes:
``GET``/``PUT /api/v1/hot-swap/triggers`` and ``POST /api/v1/hot-swap/triggers/manual``.
Un-mounted, all three keep the structured 501 the frozen contract gives every unbound
operation, and — critically — ``POST /api/v1/hot-swap`` (swap EXECUTION, owner
``SRS-RESV-004``/``005``) stays 501 whether or not the trigger layer is mounted.

These run over a fake CLI runner so the whole surface is exercised without a cargo build;
``tests/domain/test_hot_swap_trigger_config.py`` drives the real binary for the safety
post-conditions, and the e2e drives it through a browser.

SRS trace: ``SRS-RESV-003``, ``SyRS SYS-49a``, ``SRS-API-001`` (contract seam).
"""

from __future__ import annotations

import http.client
import json
import subprocess
from collections.abc import Iterator

import pytest
from atp_orchestration import mount_hot_swap_triggers
from atp_runtime import OperatorInterfaceRuntime

pytestmark = pytest.mark.boundary


# --------------------------------------------------------------------------- #
# A fake `resv003_hot_swap_trigger_cli`
# --------------------------------------------------------------------------- #


class _FakeCli:
    """Records argv and replays scripted ``key:value`` stdout, like the real binary."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.script: list[tuple[int, str, str]] = []

    def queue(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.script.append((returncode, stdout, stderr))

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if not self.script:
            raise AssertionError(f"unscripted CLI call: {argv}")
        returncode, stdout, stderr = self.script.pop(0)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _config_stdout(
    *,
    source: str = "persisted",
    drawdown: bool = False,
    threshold: int | None = None,
    top_ranked: bool = False,
    momentum: bool = False,
) -> str:
    lines = [
        "config-path:/tmp/triggers.json",
        f"config-source:{source}",
        "manual-promotion-available:true",
        f"drawdown-demotion-enabled:{str(drawdown).lower()}",
    ]
    if threshold is not None:
        lines.append(f"drawdown-demotion-threshold-bps:{threshold}")
    lines += [
        f"top-ranked-promotion-enabled:{str(top_ranked).lower()}",
        f"highest-momentum-promotion-enabled:{str(momentum).lower()}",
        f"any-automatic-enabled:{str(drawdown or top_ranked or momentum).lower()}",
        "default-disabled:true",
        "config-persisted:false",
    ]
    return "\n".join(lines) + "\n"


_MANUAL_STDOUT = (
    "manual-always-available:true\n"
    "fired:MANUAL_PROMOTION demoting:alpha candidate:beta rationale:operator-selected\n"
    "manual-logged:true\n"
    "log-persisted:/tmp/triggers.jsonl\n"
    "log-file-records:7\n"
    "trigger-record-ordinal:7\n"
)


@pytest.fixture
def fake_cli() -> _FakeCli:
    return _FakeCli()


@pytest.fixture
def mounted(fake_cli: _FakeCli, tmp_path) -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    mount_hot_swap_triggers(
        runtime,
        state_path=tmp_path / "triggers.json",
        log_path=tmp_path / "triggers.jsonl",
        binary=tmp_path / "fake-bin",
        runner=fake_cli,
    )
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        runtime.stop()


@pytest.fixture
def bare() -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        runtime.stop()


def _request(
    where: tuple[str, int], method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    host, port = where
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read() or b"{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        return response.status, parsed
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Mounting boundary
# --------------------------------------------------------------------------- #


def test_unmounted_trigger_routes_stay_deferred(bare: tuple[str, int]) -> None:
    status, body = _request(bare, "GET", "/api/v1/hot-swap/triggers")
    assert status == 501, body
    assert body.get("error", {}).get("type") == "HANDLER_DEFERRED", body


def test_mounting_triggers_never_binds_swap_execution(mounted: tuple[str, int]) -> None:
    # THE scope boundary. RESV-003 decides and logs; it cannot demote or promote. If
    # mounting the trigger layer also answered on the execution route, a caller would read
    # a 200 there as "the swap happened" — the single most dangerous confusion available on
    # this surface.
    status, body = _request(
        mounted, "POST", "/api/v1/hot-swap?confirm=yes", {"candidate_strategy_id": "beta"}
    )
    assert status == 501, body
    assert body.get("error", {}).get("type") == "HANDLER_DEFERRED", body


# --------------------------------------------------------------------------- #
# GET — reading the configuration
# --------------------------------------------------------------------------- #


def test_get_reports_the_never_configured_state_distinctly(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    fake_cli.queue(stdout=_config_stdout(source="default"))
    status, body = _request(mounted, "GET", "/api/v1/hot-swap/triggers")
    assert status == 200, body
    # "off by default" and "deliberately turned off" are different operator facts.
    assert body["config_source"] == "default"
    assert body["any_automatic_enabled"] is False
    assert body["default_disabled"] is True
    assert body["manual_promotion_available"] is True


def test_get_reports_a_persisted_configuration(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250, top_ranked=True))
    status, body = _request(mounted, "GET", "/api/v1/hot-swap/triggers")
    assert status == 200, body
    assert body["config_source"] == "persisted"
    assert body["drawdown_demotion_enabled"] is True
    assert body["drawdown_demotion_threshold_bps"] == 250
    assert body["top_ranked_promotion_enabled"] is True
    assert body["highest_momentum_promotion_enabled"] is False
    assert body["any_automatic_enabled"] is True


def test_an_unreadable_configuration_is_an_error_not_a_disabled_report(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # The false all-clear this route must never produce: 200 + "everything disabled" for a
    # configuration nobody could read.
    fake_cli.queue(returncode=1, stderr="trigger configuration ... is unreadable (torn line)")
    status, body = _request(mounted, "GET", "/api/v1/hot-swap/triggers")
    assert status >= 500, body
    assert body.get("error", {}).get("type") == "TRIGGER_CONFIG_UNREADABLE", body
    assert "drawdown_demotion_enabled" not in body


def test_a_truncated_proof_line_is_refused_rather_than_read_as_false(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # A missing boolean line must not default to False — that is the same false all-clear
    # arriving through a parsing gap instead of a read failure.
    truncated = "config-source:persisted\nmanual-promotion-available:true\n"
    fake_cli.queue(stdout=truncated)
    status, body = _request(mounted, "GET", "/api/v1/hot-swap/triggers")
    assert status >= 500, body
    assert body.get("error", {}).get("type") == "TRIGGER_CONFIG_OUTPUT_UNPARSEABLE", body


def test_an_enabled_drawdown_with_no_threshold_is_refused(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=None))
    status, body = _request(mounted, "GET", "/api/v1/hot-swap/triggers")
    assert status >= 500, body


# --------------------------------------------------------------------------- #
# PUT — configuring the triggers
# --------------------------------------------------------------------------- #


def test_put_sets_a_trigger_and_reports_the_re_read_state(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250))  # the --set call
    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250))  # the re-read
    status, body = _request(
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"drawdown_demotion_enabled": True, "drawdown_demotion_threshold_bps": 250},
    )
    assert status == 200, body
    assert body["drawdown_demotion_enabled"] is True
    assert body["drawdown_demotion_threshold_bps"] == 250
    assert ["--set-drawdown-threshold", "250"] == fake_cli.calls[0][-2:]
    # The response is a genuine re-read, not the request echoed back.
    assert len(fake_cli.calls) == 2
    assert fake_cli.calls[1][1:] == ["config", "--state", fake_cli.calls[0][3]]


def test_put_disables_a_trigger(mounted: tuple[str, int], fake_cli: _FakeCli) -> None:
    fake_cli.queue(stdout=_config_stdout())
    fake_cli.queue(stdout=_config_stdout())
    status, body = _request(
        mounted, "PUT", "/api/v1/hot-swap/triggers?confirm=yes", {"top_ranked_promotion_enabled": False}
    )
    assert status == 200, body
    assert ["--set-top-ranked", "off"] == fake_cli.calls[0][-2:]


def test_put_refuses_an_enabled_drawdown_with_no_threshold(mounted: tuple[str, int]) -> None:
    status, body = _request(
        mounted, "PUT", "/api/v1/hot-swap/triggers?confirm=yes", {"drawdown_demotion_enabled": True}
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "MISSING_DRAWDOWN_THRESHOLD", body


def test_put_refuses_a_contradictory_drawdown_request(mounted: tuple[str, int]) -> None:
    status, body = _request(
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"drawdown_demotion_enabled": False, "drawdown_demotion_threshold_bps": 250},
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "CONTRADICTORY_DRAWDOWN_CONFIG", body


def test_put_refuses_a_coerced_boolean(mounted: tuple[str, int]) -> None:
    # "true" is a string. Arming an automatic Hot-Swap off a coercion is exactly the
    # ambiguity the durable format refuses, and the wire must refuse it too.
    status, body = _request(
        mounted, "PUT", "/api/v1/hot-swap/triggers?confirm=yes", {"top_ranked_promotion_enabled": "true"}
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "NON_BOOLEAN_TRIGGER_FLAG", body


@pytest.mark.parametrize("bps", [0, 10_001, -250])
def test_put_refuses_an_out_of_range_threshold(mounted: tuple[str, int], bps: int) -> None:
    status, body = _request(
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"drawdown_demotion_enabled": True, "drawdown_demotion_threshold_bps": bps},
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "THRESHOLD_OUT_OF_RANGE", body


def test_put_requires_confirmation(mounted: tuple[str, int], fake_cli: _FakeCli) -> None:
    # Arming an automatic trigger is consequential precisely because nothing further is
    # asked of the operator afterwards: once enabled, a drawdown breach demotes the live
    # strategy on its own. It takes the same explicit confirmation as the irreversible
    # actions on this workflow — and the binary is never reached without it.
    status, body = _request(
        mounted, "PUT", "/api/v1/hot-swap/triggers", {"top_ranked_promotion_enabled": True}
    )
    assert status == 428, body
    assert fake_cli.calls == []


def test_put_refuses_an_empty_request(mounted: tuple[str, int]) -> None:
    status, body = _request(mounted, "PUT", "/api/v1/hot-swap/triggers?confirm=yes", {})
    assert status == 400, body
    assert body.get("error", {}).get("type") == "EMPTY_TRIGGER_CONFIG_REQUEST", body


def test_a_refused_write_is_not_reported_as_applied(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    fake_cli.queue(returncode=1, stderr="trigger configuration ... is unreadable")
    status, body = _request(
        mounted, "PUT", "/api/v1/hot-swap/triggers?confirm=yes", {"top_ranked_promotion_enabled": True}
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "TRIGGER_CONFIG_REFUSED", body


# --------------------------------------------------------------------------- #
# POST manual — firing the always-available trigger
# --------------------------------------------------------------------------- #


_MANUAL_BODY = {"demoting_strategy_id": "alpha", "candidate_strategy_id": "beta"}


def test_manual_trigger_fires_and_says_it_executed_nothing(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    fake_cli.queue(stdout=_MANUAL_STDOUT)
    status, body = _request(
        mounted, "POST", "/api/v1/hot-swap/triggers/manual?confirm=yes", _MANUAL_BODY
    )
    assert status == 200, body
    assert body["trigger_kind"] == "MANUAL_PROMOTION"
    assert body["logged"] is True
    # Bound to the durable record's position, not to the 200.
    assert body["trigger_id"] == "7"
    # And the payload states, in itself, that no swap happened.
    assert body["execution"]["state"] == "DEFERRED"
    assert body["execution"]["owner"] == "SRS-RESV-004"


def test_manual_trigger_requires_confirmation(mounted: tuple[str, int]) -> None:
    status, body = _request(mounted, "POST", "/api/v1/hot-swap/triggers/manual", _MANUAL_BODY)
    assert status == 428, body


def test_an_unlogged_manual_trigger_is_not_reported_as_fired(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # Logging is load-bearing: the binary exits nonzero when the audit record was rejected,
    # and that must surface as a failure, never as a fired trigger.
    fake_cli.queue(returncode=1, stderr="manual Hot-Swap trigger was not logged")
    status, body = _request(
        mounted, "POST", "/api/v1/hot-swap/triggers/manual?confirm=yes", _MANUAL_BODY
    )
    assert status >= 500, body
    assert body.get("error", {}).get("type") == "MANUAL_TRIGGER_UNLOGGED", body
    assert "trigger_id" not in body


def test_a_clean_exit_that_reports_unlogged_is_refused(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # A contradiction between the exit code and the proof line: refuse rather than pick the
    # friendlier half.
    fake_cli.queue(stdout=_MANUAL_STDOUT.replace("manual-logged:true", "manual-logged:false"))
    status, body = _request(
        mounted, "POST", "/api/v1/hot-swap/triggers/manual?confirm=yes", _MANUAL_BODY
    )
    assert status >= 500, body
    assert body.get("error", {}).get("type") == "MANUAL_TRIGGER_UNLOGGED", body


def test_a_fire_without_a_durable_ordinal_is_refused(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # Success must be evidenced by a record a later reader can find. Without the ordinal
    # there is no such artefact, so there is nothing to report.
    fake_cli.queue(stdout=_MANUAL_STDOUT.replace("trigger-record-ordinal:7\n", ""))
    status, body = _request(
        mounted, "POST", "/api/v1/hot-swap/triggers/manual?confirm=yes", _MANUAL_BODY
    )
    assert status >= 500, body
    assert body.get("error", {}).get("type") == "MANUAL_TRIGGER_UNEVIDENCED", body


def test_manual_trigger_refuses_a_self_swap(mounted: tuple[str, int]) -> None:
    status, body = _request(
        mounted,
        "POST",
        "/api/v1/hot-swap/triggers/manual?confirm=yes",
        {"demoting_strategy_id": "alpha", "candidate_strategy_id": "alpha"},
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "SAME_STRATEGY_SWAP", body


@pytest.mark.parametrize("missing", ["demoting_strategy_id", "candidate_strategy_id"])
def test_manual_trigger_requires_both_strategies(mounted: tuple[str, int], missing: str) -> None:
    body_in = {key: value for key, value in _MANUAL_BODY.items() if key != missing}
    status, body = _request(
        mounted, "POST", "/api/v1/hot-swap/triggers/manual?confirm=yes", body_in
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "MISSING_STRATEGY_ID", body
