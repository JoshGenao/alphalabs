"""L4 boundary — the ``SRS-RESV-005`` Hot-Swap EXECUTION surface on the runtime.

``mount_hot_swap_execution`` is a composition-time opt-in that binds one route:
``POST /api/v1/hot-swap`` — the route whose success means a swap *happened*. Un-mounted it
keeps the structured 501 the frozen contract gives every unbound operation, so a deployment
that has not composed the handler never accepts a swap it cannot execute.

These run over a fake CLI runner so the whole surface is exercised without a cargo build;
``tests/domain/test_hot_swap_promotion.py`` drives the REAL binary over a REAL SRS-SIM-004
paper snapshot for the safety post-conditions.

The load-bearing invariant here is the status-code split, and it comes from the SHIPPED
consumer rather than from taste: ``assets/app.js`` treats a non-2xx as *"mutated nothing —
retry is allowed"*. So a refusal may only be a non-2xx when it is decided BEFORE the gate
runs. An executed-but-BLOCKED swap must be a 200 carrying ``promotion_state``, or the pane
would offer a retry of a swap whose demotion had already released the live strategy.

SRS trace: ``SRS-RESV-005``, ``SyRS SYS-49d``, ``SRS-API-001`` (contract seam).
"""

from __future__ import annotations

import http.client
import json
import subprocess
from collections.abc import Iterator

import pytest
from atp_api.routes import ROUTES
from atp_orchestration import mount_hot_swap_execution
from atp_orchestration.hot_swap_execution import _DEFAULT_TIMEOUT_S
from atp_runtime import OperatorInterfaceRuntime

pytestmark = pytest.mark.boundary

HOT_SWAP_PATH = "/api/v1/hot-swap"

#: The SYS-49b demotion timeout the subprocess budget must outlast.
DEMOTION_TIMEOUT_S = 60.0


class _FakeCli:
    """Records argv and replays scripted ``key:value`` stdout, like the real binary."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.script: list[tuple[int, str, str]] = []
        self.raises: BaseException | None = None
        self.timeouts: list[float] = []

    def queue(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.script.append((returncode, stdout, stderr))

    def queue_status(self, designated: str) -> None:
        self.queue(stdout=f"designated:{designated}\n")

    def queue_swap(
        self,
        *,
        promotion: str = "PROMOTED",
        demotion: str = "FLAT_CONFIRMED",
        ordinal: str = "1",
        refusal: str | None = None,
        recorded: str = "true",
    ) -> None:
        lines = [
            "transports:FIXTURE",
            f"demotion-outcome:{demotion}",
            f"promotion:{promotion}",
        ]
        if refusal is not None:
            lines.append(f"refusal:{refusal}")
        lines.append(f"swap-record-ordinal:{ordinal}")
        lines.append(f"promotion-recorded:{recorded}")
        self.queue(returncode=0 if promotion == "PROMOTED" else 1, stdout="\n".join(lines) + "\n")

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        if not self.script:
            raise AssertionError(f"unscripted CLI call: {argv}")
        returncode, stdout, stderr = self.script.pop(0)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    @property
    def swap_calls(self) -> list[list[str]]:
        return [call for call in self.calls if "swap" in call]


@pytest.fixture
def fake_cli() -> _FakeCli:
    return _FakeCli()


@pytest.fixture
def mounted(fake_cli: _FakeCli, tmp_path) -> Iterator[tuple[str, int]]:
    runtime = OperatorInterfaceRuntime()
    mount_hot_swap_execution(
        runtime,
        state_path=tmp_path / "live.state",
        paper_state_dir=tmp_path / "paper",
        log_path=tmp_path / "swaps.jsonl",
        # An explicit DRILL composition. The shipped posture is the `unwired`
        # fixture below, which declares nothing and therefore refuses.
        fixture_safety_inputs={"positions": "flat", "deployed_version": "sha256:" + "a" * 64},
        binary=tmp_path / "fake-bin",
        runner=fake_cli,
    )
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield host, port
    finally:
        runtime.stop()


@pytest.fixture
def unwired(fake_cli: _FakeCli, tmp_path) -> Iterator[tuple[str, int]]:
    """Mounted, but with NO safety-input declaration — the shipped posture."""
    runtime = OperatorInterfaceRuntime()
    mount_hot_swap_execution(
        runtime,
        state_path=tmp_path / "live.state",
        paper_state_dir=tmp_path / "paper",
        log_path=tmp_path / "swaps.jsonl",
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


def _post(where: tuple[str, int], path: str, body: dict | None = None) -> tuple[int, dict]:
    host, port = where
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request("POST", path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read() or b"{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        return response.status, parsed
    finally:
        conn.close()


def _confirmed(path: str = HOT_SWAP_PATH) -> str:
    return f"{path}?confirm=true"


# --------------------------------------------------------------------------- #
# Mounting boundary
# --------------------------------------------------------------------------- #


def test_a_bare_runtime_still_answers_the_structured_501(bare):
    status, body = _post(bare, _confirmed(), {"candidate_strategy_id": "paper-b", "confirm": True})

    assert status == 501
    # A deployment that has not composed the handler must not look like one that has.
    assert body["error"]["category"] == "NOT_IMPLEMENTED"


def test_the_mounted_runtime_serves_the_route(mounted, fake_cli):
    fake_cli.queue_status("live-a")
    fake_cli.queue_swap()

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 200
    assert body["promotion_state"] == "PROMOTED"
    assert body["demotion_state"] == "DEMOTED"
    assert body["swap_id"] == "sw-1"


# --------------------------------------------------------------------------- #
# Declared contract == emitted surface, BOTH directions
# --------------------------------------------------------------------------- #


def _declared_response_fields() -> set[str]:
    declared = {
        route.response_fields
        for route in ROUTES
        if route.path == HOT_SWAP_PATH and route.method.value.upper() == "POST"
    }
    assert len(declared) == 1, "exactly one POST /api/v1/hot-swap route must be declared"
    return set(next(iter(declared)))


@pytest.mark.parametrize(
    ("promotion", "ordinal"),
    [
        pytest.param("PROMOTED", "1", id="promoted-and-journalled"),
        pytest.param("BLOCKED", "2", id="blocked-and-journalled"),
        # The degraded case is the one a subset check would miss: no journal record
        # means no id, and the field must still be PRESENT (as null).
        pytest.param("PROMOTED", "-", id="promoted-without-a-journal-record"),
    ],
)
def test_the_emitted_body_matches_the_declared_response_fields(
    mounted, fake_cli, promotion, ordinal
):
    fake_cli.queue_status("live-a")
    fake_cli.queue_swap(promotion=promotion, ordinal=ordinal)

    _, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    # Superset => undeclared drift; subset => an unkept promise. Both are defects,
    # so the comparison runs in both directions on a REAL served response — and on
    # every outcome the handler can produce, not just the happy one.
    assert set(body) == _declared_response_fields()


def test_the_declared_request_fields_are_the_accepted_set(mounted, fake_cli):
    status, body = _post(
        mounted, _confirmed(), {"candidate_strategy_id": "paper-b", "sneaky": 1}
    )

    # Accepting and ignoring an undeclared field would report a swap the caller did
    # not ask for as though it were the one they did.
    assert status == 400
    assert body["error"]["type"] == "UNKNOWN_REQUEST_FIELD"
    assert fake_cli.calls == []


# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #


def test_an_unconfirmed_swap_never_reaches_the_binary(mounted, fake_cli):
    status, body = _post(mounted, HOT_SWAP_PATH, {"candidate_strategy_id": "paper-b"})

    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"
    # The spy is the proof: the guard precedes dispatch, so nothing was read or run.
    assert fake_cli.calls == [], "an unconfirmed swap must not invoke the binary at all"


def test_the_handler_refuses_unconfirmed_even_when_the_transport_does_not(fake_cli, tmp_path):
    """The ACTION-level guard, isolated from the route-level one.

    The route carries ``requires_confirmation=True``, so the transport answers 428
    before dispatch — which means the test above passes whether or not the handler
    checks for itself. Mutation verification caught exactly that: deleting the
    handler's own guard left that test green.

    So this one calls the handler DIRECTLY with ``confirmed=False``, the position a
    future dispatch path (a CLI arm, a batch runner, a re-routed surface) would be
    in. Defence in depth is only defence if something proves the inner layer.
    """
    from atp_api.routes import Method
    from atp_orchestration.hot_swap_execution import REST_HOT_SWAP_EXECUTE, SwapExecutionHandler
    from atp_runtime.errors import InterfaceError
    from atp_runtime.registry import Request, Surface

    handler = SwapExecutionHandler(
        state_path=tmp_path / "live.state",
        paper_state_dir=tmp_path / "paper",
        log_path=tmp_path / "swaps.jsonl",
        binary=tmp_path / "fake-bin",
        runner=fake_cli,
    )
    request = Request(
        surface=Surface.REST,
        operation=REST_HOT_SWAP_EXECUTE,
        method=Method.POST.value.upper(),
        path=HOT_SWAP_PATH,
        body={"candidate_strategy_id": "paper-b"},
        confirmed=False,
    )

    with pytest.raises(InterfaceError) as excinfo:
        handler.handle(request)

    assert excinfo.value.category.value == "CONFIRMATION_REQUIRED"
    assert fake_cli.calls == [], "the handler's own guard must precede every read"


# --------------------------------------------------------------------------- #
# Request validation (all decided BEFORE the gate runs => non-2xx is honest)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "candidate",
    [pytest.param(None, id="absent"), pytest.param("   ", id="blank"), pytest.param(7, id="coerced-int"), pytest.param(True, id="coerced-bool")],
)
def test_a_missing_or_coerced_candidate_is_refused(mounted, fake_cli, candidate):
    body = {} if candidate is None else {"candidate_strategy_id": candidate}

    status, response = _post(mounted, _confirmed(), body)

    assert status == 400
    assert response["error"]["type"] == "MISSING_CANDIDATE_STRATEGY_ID"
    assert fake_cli.calls == []


def test_no_live_strategy_names_the_designation_route_instead(mounted, fake_cli):
    fake_cli.queue_status("none")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 400
    assert body["error"]["type"] == "NO_LIVE_STRATEGY_TO_DEMOTE"
    # Named owner + route: promoting with nothing to demote is a DESIGNATION.
    assert body["error"]["detail"]["owner"] == "SRS-EXE-001"
    assert "promote-live" in body["error"]["detail"]["route"]
    assert fake_cli.swap_calls == [], "no swap may be attempted with nothing to demote"


def test_promoting_the_already_live_strategy_is_refused(mounted, fake_cli):
    fake_cli.queue_status("live-a")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "live-a"})

    assert status == 400
    assert body["error"]["type"] == "SAME_STRATEGY_SWAP"
    assert fake_cli.swap_calls == []


# --------------------------------------------------------------------------- #
# Unknown is never "nothing is live"
# --------------------------------------------------------------------------- #


def test_an_unreadable_designation_record_is_not_no_strategy_is_live(mounted, fake_cli):
    fake_cli.queue(returncode=1, stderr="state file is not a RESV005 snapshot")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 500
    assert body["error"]["type"] == "LIVE_DESIGNATION_UNREADABLE"
    # The critical part: it did NOT fall through to "nothing is live", which would
    # have let the swap proceed over a live strategy.
    assert fake_cli.swap_calls == []


def test_a_failed_status_read_is_refused_even_when_it_printed_a_designation(mounted, fake_cli):
    """Isolates the EXIT-CODE guard from the missing-line guard.

    Mutation verification caught this: deleting the ``returncode != 0`` check left
    the previous test green, because its fake also produced no ``designated`` line
    and the second guard picked it up. A binary that fails *and still prints* a
    plausible line is the case that distinguishes them — and trusting that line
    would mean promoting against a designation the tool itself could not stand
    behind.
    """
    fake_cli.queue(returncode=1, stdout="designated:live-a\n", stderr="snapshot unreadable")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 500
    assert body["error"]["type"] == "LIVE_DESIGNATION_UNREADABLE"
    assert fake_cli.swap_calls == []


def test_a_status_read_without_a_designated_line_fails_closed(mounted, fake_cli):
    fake_cli.queue(returncode=0, stdout="something-else:true\n")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 500
    assert body["error"]["type"] == "LIVE_DESIGNATION_UNREADABLE"
    assert fake_cli.swap_calls == []


def test_contradictory_proof_lines_are_refused_not_resolved(mounted, fake_cli):
    fake_cli.queue_status("live-a")
    fake_cli.queue(
        returncode=0,
        stdout="promotion:PROMOTED\npromotion:BLOCKED\nswap-record-ordinal:1\n",
    )

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 500
    assert body["error"]["type"] == "SWAP_OUTPUT_UNREADABLE"


def test_a_binary_that_reports_no_outcome_is_never_a_2xx(mounted, fake_cli):
    fake_cli.queue_status("live-a")
    fake_cli.queue(returncode=1, stdout="transports:FIXTURE\n", stderr="exploded")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 500
    assert body["error"]["type"] == "SWAP_OUTCOME_UNREADABLE"


# --------------------------------------------------------------------------- #
# The status-code split the shipped SPA depends on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("demotion", "refusal"),
    [
        pytest.param("DEMOTION_PENDING", "DEMOTION_REFUSED", id="demotion-timed-out"),
        pytest.param("FLAT_CONFIRMED", "LIVE_POSITIONS_OPEN", id="positions-open"),
        pytest.param("FLAT_CONFIRMED", "LIVE_POSITIONS_UNPROVABLE", id="positions-unprovable"),
    ],
)
def test_an_executed_but_blocked_swap_is_a_200_not_an_error(mounted, fake_cli, demotion, refusal):
    fake_cli.queue_status("live-a")
    fake_cli.queue_swap(promotion="BLOCKED", demotion=demotion, refusal=refusal, ordinal="4")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    # The gate RAN. A non-2xx here would tell the pane nothing mutated and a retry
    # is safe — but a demotion may already have released the live strategy.
    assert status == 200
    assert body["promotion_state"] == "BLOCKED"
    assert body["demotion_state"] == (
        "DEMOTED" if demotion == "FLAT_CONFIRMED" else "DEMOTION_PENDING"
    )
    assert body["swap_id"] == "sw-4"


def test_a_swap_without_a_journal_record_carries_no_fabricated_id(mounted, fake_cli):
    fake_cli.queue_status("live-a")
    fake_cli.queue_swap(ordinal="-", recorded="false")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 200
    # No durable position to address => an explicitly NULL id, not a missing key and
    # not an invented one. The pane already refuses to call a response whose swap_id
    # is not a string "promoted", so the null is load-bearing.
    assert body["swap_id"] is None
    # And the promotion is NOT reported as a clean success: the candidate may be live
    # with nothing durable addressing the swap, which needs reconciliation.
    assert body["promotion_state"] == "PROMOTED_UNRECORDED"


# --------------------------------------------------------------------------- #
# Subprocess budget and launch failures
# --------------------------------------------------------------------------- #


def test_the_subprocess_budget_outlasts_the_demotion_it_waits_on(mounted, fake_cli):
    fake_cli.queue_status("live-a")
    fake_cli.queue_swap()

    _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    # A budget shorter than the SYS-49b demotion timeout would manufacture an
    # ambiguous result on every slow-but-successful swap.
    assert _DEFAULT_TIMEOUT_S > DEMOTION_TIMEOUT_S
    assert all(budget > DEMOTION_TIMEOUT_S for budget in fake_cli.timeouts)


def test_a_wedged_binary_is_reported_as_ambiguous_not_as_a_failed_swap(mounted, fake_cli):
    fake_cli.raises = subprocess.TimeoutExpired(cmd="swap", timeout=90.0)

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 504
    assert body["error"]["type"] == "SWAP_CLI_TIMEOUT"
    # The operator is told the swap may still be in flight, and who owns the
    # durable state that settles it.
    assert body["error"]["detail"]["owner"] == "SRS-RESV-004"


def test_an_unlaunchable_binary_is_a_structured_error(mounted, fake_cli):
    fake_cli.raises = OSError("no such file")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 504
    assert body["error"]["type"] == "SWAP_CLI_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# The trigger layer and the execution layer stay distinct
# --------------------------------------------------------------------------- #


def test_mounting_execution_does_not_serve_the_trigger_routes(mounted, fake_cli):
    status, body = _post(mounted, "/api/v1/hot-swap/triggers/manual?confirm=true",
                         {"demoting_strategy_id": "a", "candidate_strategy_id": "b"})

    # Executing swaps says nothing about the SRS-RESV-003 trigger layer.
    assert status == 501
    assert body["error"]["category"] == "NOT_IMPLEMENTED"


def test_mounting_execution_does_not_serve_the_status_route(mounted, fake_cli):
    host, port = mounted
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/api/v1/hot-swap/status")
        response = conn.getresponse()
        body = json.loads(response.read() or b"{}")
        status = response.status
    finally:
        conn.close()

    # Two of its four declared fields are owned by the deferred SRS-RESV-004
    # lockout and the unbuilt SRS-RESV-006 cool-down; half a payload would be
    # worse than no payload.
    assert status == 501
    assert body["error"]["category"] == "NOT_IMPLEMENTED"


# --------------------------------------------------------------------------- #
# The safety inputs the promotion turns on are not fabricable
# --------------------------------------------------------------------------- #


def test_without_declared_safety_inputs_the_route_refuses_to_promote(unwired, fake_cli):
    """The shipped posture: mounted, real gate behind it, and it will not promote.

    SYS-49d turns on two facts — the account is flat, and the artifact is the same.
    Their producers are deferred, so a served route that promoted anyway would
    report PROMOTED without proving either. That is a false green on a live trading
    path, and worse than an unbound route.
    """
    status, body = _post(unwired, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 501
    assert body["error"]["type"] == "SAFETY_INPUTS_UNAVAILABLE"
    assert body["error"]["detail"]["owner"] == "SRS-EXE-006, SRS-ORCH-004"
    assert body["error"]["detail"]["missing"] == [
        "deployed version (code identity)",
        "open IB positions (flat-start)",
    ]
    # Refused before anything was read or run — nothing mutated.
    assert fake_cli.calls == []


def test_a_partial_safety_declaration_still_refuses_and_names_only_what_is_missing(
    fake_cli, tmp_path
):
    """Half the safety picture is not the safety picture (UI-5 r5-r12's rule)."""
    runtime = OperatorInterfaceRuntime()
    mount_hot_swap_execution(
        runtime,
        state_path=tmp_path / "live.state",
        paper_state_dir=tmp_path / "paper",
        log_path=tmp_path / "swaps.jsonl",
        fixture_safety_inputs={"positions": "flat"},  # no deployed_version
        binary=tmp_path / "fake-bin",
        runner=fake_cli,
    )
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        status, body = _post((host, port), _confirmed(), {"candidate_strategy_id": "paper-b"})
    finally:
        runtime.stop()

    assert status == 501
    assert body["error"]["detail"]["owner"] == "SRS-ORCH-004"
    assert body["error"]["detail"]["missing"] == ["deployed version (code identity)"]
    assert fake_cli.calls == []


def test_a_drill_composition_carries_its_tier_into_the_binary(mounted, fake_cli):
    """The fixture tier travels WITH the values, so the binary can refuse a caller
    that supplies them without saying what they are."""
    fake_cli.queue_status("live-a")
    fake_cli.queue_swap()

    _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    swap_argv = fake_cli.swap_calls[0]
    assert "--allow-fixture-safety-inputs" in swap_argv
    assert "--positions" in swap_argv
    assert "--deployed-version" in swap_argv


def test_a_promotion_whose_audit_record_did_not_land_is_not_a_clean_success(mounted, fake_cli):
    """Round-2 adversarial review [high].

    The audit sink is best-effort by design — the designation is already written
    when it runs, so a sink failure cannot roll it back. That left an unwritable
    journal able to produce a LIVE candidate with a clean `PROMOTED` and no durable
    id: an irreversible live-trading state change nobody can address.
    """
    fake_cli.queue_status("live-a")
    fake_cli.queue_swap(ordinal="-", recorded="false")

    status, body = _post(mounted, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 200, "the swap RAN; a non-2xx would claim nothing mutated"
    assert body["promotion_state"] == "PROMOTED_UNRECORDED"
    assert body["swap_id"] is None
    # The shipped pane requires promotion === "PROMOTED" to call it promoted, so
    # this value holds its control inert and waits for durable confirmation.
    assert body["promotion_state"] != "PROMOTED"


def test_a_bare_uncomposed_execution_route_names_ITS_owner_not_the_capability_owner(bare):
    """Round-2 adversarial review [medium].

    HOT_SWAP spans two features: SRS-RESV-003 owns the trigger routes, SRS-RESV-005
    owns execution. Deriving the 501's owner from the capability sent an operator to
    the trigger feature for a gap in the execution route.
    """
    status, body = _post(bare, _confirmed(), {"candidate_strategy_id": "paper-b"})

    assert status == 501
    assert body["error"]["detail"]["owner"] == "SRS-RESV-005"
