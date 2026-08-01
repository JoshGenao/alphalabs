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
from pathlib import Path

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
        #: Set to raise instead of returning — a wedged or unlaunchable binary.
        self.raises: BaseException | None = None

    def queue(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.script.append((returncode, stdout, stderr))

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if self.raises is not None:
            raise self.raises
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
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"top_ranked_promotion_enabled": False},
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
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"top_ranked_promotion_enabled": "true"},
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
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"top_ranked_promotion_enabled": True},
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


# --------------------------------------------------------------------------- #
# The pane cell-swap: source payload shape vs what UI-5 actually reads
# --------------------------------------------------------------------------- #


def test_the_source_payload_resolves_the_panes_per_trigger_chips(fake_cli: _FakeCli) -> None:
    """The shape contract between this source and ``atp_dashboard.hotswap``.

    The pane reads per-trigger detail as ``triggers[kind]["enabled"]``. A flat
    ``<kind>_enabled`` payload parses without error and resolves nothing: the aggregate
    chip goes live while every per-trigger chip keeps rendering its deferred placeholder,
    which reads as "configured, but no trigger is" — worse than an honest deferral because
    it looks resolved. That mismatch is invisible to both modules' own tests, so it is
    pinned here, across the seam.
    """

    from atp_dashboard.hotswap import CliHotSwapTriggerSource, HotSwapStatusProvider

    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250, top_ranked=True))
    source = CliHotSwapTriggerSource("/tmp/triggers.json", binary="/tmp/fake", runner=fake_cli)
    snapshot = HotSwapStatusProvider(source).hot_swap_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["auto_triggers_enabled"]["value"] is True
    chips = {chip["kind"]: chip["enabled"] for chip in snapshot["auto_triggers_live"]}
    assert chips["drawdown_demotion"]["value"] is True
    assert chips["top_ranked_promotion"]["value"] is True
    assert chips["highest_momentum_promotion"]["value"] is False
    # Every RESV-003 chip is now sourced, none left deferred.
    for kind, cell in chips.items():
        assert cell["data_source"] != "deferred:SRS-RESV-003", (kind, cell)


def test_the_other_owners_cells_stay_deferred(fake_cli: _FakeCli) -> None:
    # Resolving the trigger leg must not fabricate the legs RESV-002/004/005/006 own. A
    # source that answered those too would put invented facts on the pane.
    from atp_dashboard.hotswap import CliHotSwapTriggerSource, HotSwapStatusProvider

    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250))
    source = CliHotSwapTriggerSource("/tmp/triggers.json", binary="/tmp/fake", runner=fake_cli)
    snapshot = HotSwapStatusProvider(source).hot_swap_snapshot()

    assert snapshot["current_live_strategy_id"]["data_source"] == "deferred:SRS-RESV-005"
    assert snapshot["demotion_pending"]["data_source"] == "deferred:SRS-RESV-004"
    assert snapshot["cooldown"]["in_effect"]["data_source"] == "deferred:SRS-RESV-006"


def test_an_unreadable_config_does_not_blank_the_pane_wholesale(fake_cli: _FakeCli) -> None:
    # The protocol requires the three legs to fail INDEPENDENTLY. An unreadable trigger
    # configuration must surface as an explicit error on the snapshot, not as a clean
    # all-deferred pane that looks like "nothing configured yet".
    from atp_dashboard.hotswap import CliHotSwapTriggerSource, HotSwapStatusProvider

    fake_cli.queue(returncode=1, stderr="trigger configuration is unreadable (torn line)")
    source = CliHotSwapTriggerSource("/tmp/triggers.json", binary="/tmp/fake", runner=fake_cli)
    snapshot = HotSwapStatusProvider(source).hot_swap_snapshot()

    assert snapshot["ok"] is False, snapshot
    assert any("unreadable" in str(error) for error in snapshot.get("errors", [])), snapshot
    assert snapshot["auto_triggers_enabled"]["value"] is None


# --------------------------------------------------------------------------- #
# Contract ↔ handler agreement
#
# The offline contract documents an unimplemented route's fields as bare strings. Once a
# REAL handler answers, that placeholder stops being a harmless stub and becomes a false
# statement about the wire: a generated client would send `"true"` where the handler demands
# a JSON boolean, and misparse the object it returns.
# --------------------------------------------------------------------------- #


_JSON_TYPE_OF = {bool: "boolean", int: "integer", str: "string", dict: "object"}


def _documented_types(path: str, method: str, section: str) -> dict[str, str]:
    import json as _json
    from pathlib import Path as _Path

    snapshot = _json.loads(
        (_Path(__file__).resolve().parents[2] / "python/atp_api/openapi.json").read_text()
    )
    operation = snapshot["paths"][path][method]
    if section == "request":
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
    else:
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    # `type` is a string, or a list for an OpenAPI 3.1 union (e.g. integer-or-null).
    return {name: spec["type"] for name, spec in schema["properties"].items()}


def test_the_documented_get_response_matches_what_the_handler_returns(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250, top_ranked=True))
    _status, body = _request(mounted, "GET", "/api/v1/hot-swap/triggers")
    documented = _documented_types("/api/v1/hot-swap/triggers", "get", "response")
    for name, value in body.items():
        expected = "null" if value is None else _JSON_TYPE_OF[type(value)]
        declared = documented.get(name)
        allowed = declared if isinstance(declared, list) else [declared]
        assert expected in allowed, (name, value, declared)


def test_the_documented_manual_response_matches_what_the_handler_returns(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    fake_cli.queue(stdout=_MANUAL_STDOUT)
    _status, body = _request(
        mounted, "POST", "/api/v1/hot-swap/triggers/manual?confirm=yes", _MANUAL_BODY
    )
    documented = _documented_types("/api/v1/hot-swap/triggers/manual", "post", "response")
    for name, value in body.items():
        assert documented.get(name) == _JSON_TYPE_OF[type(value)], (name, value, documented)


def test_a_contract_valid_put_body_is_accepted_by_the_handler(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # The other direction: a payload built to the DOCUMENTED types must not be refused.
    documented = _documented_types("/api/v1/hot-swap/triggers", "put", "request")
    assert documented["drawdown_demotion_enabled"] == "boolean", documented
    assert "integer" in documented["drawdown_demotion_threshold_bps"], documented

    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250))
    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250))
    status, body = _request(
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"drawdown_demotion_enabled": True, "drawdown_demotion_threshold_bps": 250},
    )
    assert status == 200, body


def test_the_default_disabled_response_is_valid_against_the_published_schema(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # The DEFAULT state — drawdown demotion off, so the threshold is null — is the commonest
    # response this route serves. Documenting the field as a bare integer made that response
    # schema-invalid for every generated client.
    fake_cli.queue(stdout=_config_stdout(source="default"))
    _status, body = _request(mounted, "GET", "/api/v1/hot-swap/triggers")
    assert body["drawdown_demotion_threshold_bps"] is None, body
    documented = _documented_types("/api/v1/hot-swap/triggers", "get", "response")
    assert "null" in documented["drawdown_demotion_threshold_bps"], documented


# --------------------------------------------------------------------------- #
# Manual trigger: request validation and evidence attribution
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_id", [True, 42, {"x": 1}, ["a"], 3.5], ids=["bool", "int", "object", "list", "float"]
)
def test_a_non_string_strategy_id_is_refused_not_coerced(
    mounted: tuple[str, int], fake_cli: _FakeCli, bad_id: object
) -> None:
    # `str(raw)` is a coercion, not a read: True would become "True" and {"x": 1} would
    # become "{'x': 1}", either of which would go on to name a strategy in a DURABLE audit
    # record. The published schema says these are strings.
    status, body = _request(
        mounted,
        "POST",
        "/api/v1/hot-swap/triggers/manual?confirm=yes",
        {"demoting_strategy_id": "alpha", "candidate_strategy_id": bad_id},
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "NON_STRING_STRATEGY_ID", body
    assert fake_cli.calls == [], "a malformed id must never reach the binary"


def test_a_whitespace_bearing_id_is_refused(mounted: tuple[str, int], fake_cli: _FakeCli) -> None:
    # The binary reports what it fired on a space-delimited proof line, and this handler
    # verifies success by reading that line back — an id with a space could not be read back
    # unambiguously.
    status, body = _request(
        mounted,
        "POST",
        "/api/v1/hot-swap/triggers/manual?confirm=yes",
        {"demoting_strategy_id": "alpha", "candidate_strategy_id": "cand beta"},
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "MALFORMED_STRATEGY_ID", body
    assert fake_cli.calls == []


@pytest.mark.parametrize(
    "proof",
    [
        "fired:MANUAL_PROMOTION demoting:alpha candidate:SOMEONE_ELSE rationale:x",
        "fired:MANUAL_PROMOTION demoting:SOMEONE_ELSE candidate:beta rationale:x",
        "fired:DRAWDOWN_DEMOTION demoting:alpha candidate:beta rationale:x",
        "fired:",
    ],
    ids=["wrong-candidate", "wrong-demoting", "wrong-kind", "no-proof"],
)
def test_a_trigger_that_does_not_match_the_request_is_not_reported_as_success(
    mounted: tuple[str, int], fake_cli: _FakeCli, proof: str
) -> None:
    # A clean exit and a logged ordinal say SOMETHING was recorded; they do not say it was
    # the trigger this request asked for. Reporting the REQUEST's own values back with an
    # ordinal pointing at a different record would misattribute a fired trigger in the
    # durable audit trail — the place it matters most.
    stdout = "\n".join(
        line for line in _MANUAL_STDOUT.splitlines() if not line.startswith("fired:")
    )
    fake_cli.queue(stdout=stdout + "\n" + proof + "\n")
    status, body = _request(
        mounted, "POST", "/api/v1/hot-swap/triggers/manual?confirm=yes", _MANUAL_BODY
    )
    assert status >= 500, body
    assert body.get("error", {}).get("type") == "MANUAL_TRIGGER_MISATTRIBUTED", body
    assert "trigger_id" not in body


def test_a_misspelled_trigger_field_is_refused_not_silently_dropped(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # The same fail-open the durable format already refuses, arriving one layer earlier:
    # reading only expected keys means a typo returns 200 having armed nothing, and the
    # operator walks away believing the trigger is on.
    status, body = _request(
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"drawdown_demotion_enabledd": True, "top_ranked_promotion_enabled": True},
    )
    assert status == 400, body
    assert body.get("error", {}).get("type") == "UNKNOWN_TRIGGER_CONFIG_FIELD", body
    # Nothing was applied — not even the recognised sibling field.
    assert fake_cli.calls == [], fake_cli.calls


def test_the_strict_handler_and_the_published_schema_agree_on_unknown_fields(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # The handler refuses unknown keys; the schema has to say so, or a generated client can
    # build a schema-valid request the live surface rejects.
    import json as _json
    from pathlib import Path as _Path

    snapshot = _json.loads(
        (_Path(__file__).resolve().parents[2] / "python/atp_api/openapi.json").read_text()
    )
    schema = snapshot["paths"]["/api/v1/hot-swap/triggers"]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert schema["additionalProperties"] is False, schema

    # And the two agree in behaviour, not just in wording.
    status, _body = _request(
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"top_ranked_promotion_enabled": True, "not_a_field": 1},
    )
    assert status == 400
    assert fake_cli.calls == []


def test_the_documented_put_response_matches_what_the_handler_returns(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # PUT returns the same re-read body as GET, so its documented response has to carry the
    # same fields. Checking GET and the manual route but not this one is how a field the
    # handler returns went undocumented on a live route.
    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250))
    fake_cli.queue(stdout=_config_stdout(drawdown=True, threshold=250))
    _status, body = _request(
        mounted,
        "PUT",
        "/api/v1/hot-swap/triggers?confirm=yes",
        {"drawdown_demotion_enabled": True, "drawdown_demotion_threshold_bps": 250},
    )
    documented = _documented_types("/api/v1/hot-swap/triggers", "put", "response")
    assert set(body) == set(documented), (sorted(body), sorted(documented))
    for name, value in body.items():
        expected = "null" if value is None else _JSON_TYPE_OF[type(value)]
        declared = documented[name]
        allowed = declared if isinstance(declared, list) else [declared]
        assert expected in allowed, (name, value, declared)


# --------------------------------------------------------------------------- #
# The SHIPPED composition
# --------------------------------------------------------------------------- #


def test_the_production_composition_serves_the_trigger_routes(tmp_path) -> None:
    # Implementing the handlers is not shipping them. `serve()` composes through this helper,
    # so if it does not register them the documented routes answer 501 in production while
    # the tests — which mounted them by hand — pass.
    from atp_dashboard import mount_default_dashboard
    from atp_dashboard.server import _mount_hot_swap_trigger_arm

    env = {
        "ATP_HOT_SWAP_TRIGGER_STATE": str(tmp_path / "triggers.json"),
        "ATP_HOT_SWAP_TRIGGER_LOG": str(tmp_path / "triggers.jsonl"),
    }
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(
        runtime, env, hot_swap_source=_mount_hot_swap_trigger_arm(runtime, env)
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, body = _request((host, port), "GET", "/api/v1/hot-swap/triggers")
        # What this test is FOR is that the route is mounted by the production
        # composition, and that survives the CLI being absent: an unmounted route
        # answers 501, a mounted one whose backing binary is missing answers 504.
        # CI's Python job builds no Rust, so asserting a flat 200 made the shipped
        # -routes property fail for a reason that has nothing to do with shipping
        # the routes. Skipping instead — the sibling idiom — would have stopped
        # checking the one thing this test names.
        assert status in (200, 504), body
        if status == 200:
            assert body["config_source"] == "default", body
        else:
            assert body["error"]["type"] == "TRIGGER_CLI_UNAVAILABLE", body
        # And swap EXECUTION still is not shipped, because nothing can execute one.
        status, body = _request(
            (host, port), "POST", "/api/v1/hot-swap?confirm=yes", {"candidate_strategy_id": "b"}
        )
        assert status == 501, body
    finally:
        publisher.stop()
        runtime.stop()


def test_the_bare_composition_leaves_the_trigger_routes_deferred() -> None:
    # Unset knobs mean no trigger surface at all — not a half-mounted one.
    from atp_dashboard import mount_default_dashboard
    from atp_dashboard.server import _mount_hot_swap_trigger_arm

    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(
        runtime, {}, hot_swap_source=_mount_hot_swap_trigger_arm(runtime, {})
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, body = _request((host, port), "GET", "/api/v1/hot-swap/triggers")
        assert status == 501, body
    finally:
        publisher.stop()
        runtime.stop()


def test_a_trigger_surface_that_cannot_log_refuses_to_start(tmp_path) -> None:
    # "All swap triggers are logged" is an acceptance clause, not a nicety. A surface able to
    # fire a trigger it cannot record must not come up at all — the SRS-MD-003 rule for its
    # audit sink, applied here.
    from atp_dashboard.server import _mount_hot_swap_trigger_arm

    runtime = OperatorInterfaceRuntime()
    with pytest.raises(ValueError, match="ATP_HOT_SWAP_TRIGGER_LOG"):
        _mount_hot_swap_trigger_arm(
            runtime, {"ATP_HOT_SWAP_TRIGGER_STATE": str(tmp_path / "triggers.json")}
        )


def test_the_manual_schema_requires_the_ids_the_handler_requires(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # The handler rejects a body missing either id. A schema that lists them as merely
    # optional lets a generated client build a valid request the shipped surface 400s.
    import json as _json
    from pathlib import Path as _Path

    snapshot = _json.loads(
        (_Path(__file__).resolve().parents[2] / "python/atp_api/openapi.json").read_text()
    )
    schema = snapshot["paths"]["/api/v1/hot-swap/triggers/manual"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert set(schema.get("required", [])) == {
        "demoting_strategy_id",
        "candidate_strategy_id",
    }, schema

    # Behaviour matches the declaration in both directions.
    for missing in ("demoting_strategy_id", "candidate_strategy_id"):
        body_in = {k: v for k, v in _MANUAL_BODY.items() if k != missing}
        status, _body = _request(
            mounted, "POST", "/api/v1/hot-swap/triggers/manual?confirm=yes", body_in
        )
        assert status == 400, missing
    assert fake_cli.calls == []


def test_a_wedged_binary_is_reported_as_unavailable_not_as_a_corrupt_config(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # Restart/repair the dependency versus repair the file are different operator actions.
    fake_cli.raises = subprocess.TimeoutExpired(["resv003_hot_swap_trigger_cli"], 30.0)
    status, body = _request(mounted, "GET", "/api/v1/hot-swap/triggers")
    assert status == 504, body
    assert body.get("error", {}).get("type") == "TRIGGER_CLI_UNAVAILABLE", body


def test_contradictory_proof_lines_are_refused_not_resolved(
    mounted: tuple[str, int], fake_cli: _FakeCli
) -> None:
    # A version-skewed or wrong binary emitting two different values for the same key has
    # not said which is true. Last-one-wins would let the handler accept whichever came
    # second as durable evidence that a trigger fired.
    fake_cli.queue(stdout=_MANUAL_STDOUT + "trigger-record-ordinal:99\n")
    status, body = _request(
        mounted, "POST", "/api/v1/hot-swap/triggers/manual?confirm=yes", _MANUAL_BODY
    )
    assert status >= 500, body
    assert "trigger_id" not in body


def test_the_operator_binary_path_is_configurable(monkeypatch) -> None:
    # target/debug is a DEVELOPMENT layout. Without an override the surface would look
    # mounted and then fail on first use in a deployed image.
    from atp_hotswap import BINARY_ENV_KNOB, default_binary

    assert default_binary({}).name == "resv003_hot_swap_trigger_cli"
    assert default_binary({BINARY_ENV_KNOB: "/opt/atp/bin/triggers"}) == Path(
        "/opt/atp/bin/triggers"
    )
    monkeypatch.setenv(BINARY_ENV_KNOB, "/opt/atp/bin/from-env")
    assert default_binary() == Path("/opt/atp/bin/from-env")
