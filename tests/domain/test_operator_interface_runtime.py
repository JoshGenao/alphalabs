"""L7 domain/safety — operator-interface runtime safety invariants.

The runtime is the surface an operator drives the kill switch, live designation,
and Hot-Swap through, so its *interface-level* safety properties must hold
regardless of which domain handlers are wired:

* **Loopback-only bind (SRS-SEC-002).** The runtime refuses to bind any
  non-loopback / non-RFC1918 host and opens no socket when it refuses.
* **Confirmation guard (UI-4 / SRS-SAFE-001).** A state-mutating, confirmation-
  required operation is NEVER dispatched to its handler without a token — proven
  with a spy handler that records every call.
* **Deferred-handler inertness.** An unwired domain operation returns a
  structured 501 and performs no side effect.
* **Dependency direction / no vendor leakage.** The runtime imports only peer
  interface packages — never a core trading engine or a vendor SDK.

These are the paired ``tests/domain/`` safety tests for the kill-switch /
live-mode / safety paths the runtime touches.
"""

from __future__ import annotations

import ast
import io
import json
import threading
from pathlib import Path

import pytest
from atp_runtime import (
    BindPolicyError,
    ErrorCategory,
    HandlerResult,
    InterfaceError,
    OperationKey,
    OperatorInterfaceRuntime,
    Request,
    Surface,
    assert_bind_allowed,
    compute_accept_key,
    is_allowed_bind_host,
)

pytestmark = [pytest.mark.domain, pytest.mark.safety]

PACKAGE = Path(__file__).resolve().parents[2] / "python" / "atp_runtime"

# REST routes that mutate state on the three confirmation-required workflows.
_STATE_MUTATING_CONFIRM_ROUTES = [
    ("POST", "/api/v1/kill-switch"),
    ("POST", "/api/v1/strategies/abc/promote-live"),
    ("POST", "/api/v1/hot-swap"),
]


class _SpyHandler:
    """Records every dispatch so a test can prove the guard ran first."""

    def __init__(self) -> None:
        self.calls: list[Request] = []

    def handle(self, request: Request) -> HandlerResult:
        self.calls.append(request)
        return HandlerResult(200, {"dispatched": True})


# --------------------------------------------------------------------------- #
# SRS-SEC-002 — loopback / RFC 1918 bind only
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "::1", "10.1.2.3", "172.16.5.6", "192.168.1.9", "localhost"]
)
def test_loopback_and_rfc1918_hosts_are_allowed(host):
    assert is_allowed_bind_host(host) is True
    assert_bind_allowed(host)  # does not raise


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "8.8.8.8", "1.2.3.4", "169.254.1.1", "not-an-ip"]
)
def test_public_and_unspecified_hosts_are_refused(host):
    assert is_allowed_bind_host(host) is False
    with pytest.raises(BindPolicyError):
        assert_bind_allowed(host)


def test_start_on_public_host_binds_no_socket():
    runtime = OperatorInterfaceRuntime()
    with pytest.raises(BindPolicyError):
        runtime.start(host="0.0.0.0", port=0)
    # The bind failed closed: no server was created.
    with pytest.raises(RuntimeError):
        runtime.bound_address()


# --------------------------------------------------------------------------- #
# UI-4 / SRS-SAFE-001 — confirmation guard precedes dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("method", "path"), _STATE_MUTATING_CONFIRM_ROUTES)
def test_rest_confirmation_guard_never_dispatches_without_token(method, path):
    runtime = OperatorInterfaceRuntime()
    spy = _SpyHandler()
    # Bind a *real* handler so we prove the guard, not deferral, blocks dispatch.
    identifier = f"{method} {_template_for(path)}"
    runtime.registry.register(OperationKey(Surface.REST, identifier), spy)

    status, body = runtime.dispatch_rest(method, path)
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"
    assert spy.calls == []  # handler must not have run

    status, _ = runtime.dispatch_rest(method, f"{path}?confirm=true", b"{}")
    assert status == 200
    assert len(spy.calls) == 1  # only now, with confirmation, does it run


def test_cli_confirmation_guard_never_dispatches_without_flag():
    runtime = OperatorInterfaceRuntime()
    spy = _SpyHandler()
    runtime.registry.register(OperationKey(Surface.CLI, "kill-switch activate"), spy)
    dispatcher = runtime.cli_dispatcher()

    code = dispatcher.dispatch(["kill-switch", "activate"], stdout=io.StringIO())
    assert code == 3  # CONFIRMATION_REQUIRED
    assert spy.calls == []

    code = dispatcher.dispatch(["kill-switch", "activate", "--confirm"], stdout=io.StringIO())
    assert code == 0
    assert len(spy.calls) == 1


def test_cli_handler_failures_become_structured_envelopes_not_tracebacks():
    """A CLI handler that raises must yield a stable exit code + error envelope,
    mirroring the REST surface — never a traceback / process crash."""

    class _Raises:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        def handle(self, request: Request) -> HandlerResult:
            raise self._exc

    # A structured InterfaceError maps to its documented exit code.
    runtime = OperatorInterfaceRuntime()
    runtime.registry.register(
        OperationKey(Surface.CLI, "strategy list"),
        _Raises(InterfaceError(ErrorCategory.NOT_FOUND, "no such strategy")),
    )
    out = io.StringIO()
    code = runtime.cli_dispatcher().dispatch(["strategy", "list", "--json"], stdout=out)
    assert code == 4  # NOT_FOUND
    assert json.loads(out.getvalue())["error"]["category"] == "NOT_FOUND"

    # A handler TimeoutError becomes GATEWAY_TIMEOUT -> ExitCode.TIMEOUT (6),
    # NOT NOT_READY — a dependency timeout must be distinguishable.
    runtime2 = OperatorInterfaceRuntime()
    runtime2.registry.register(
        OperationKey(Surface.CLI, "strategy list"), _Raises(TimeoutError("downstream timed out"))
    )
    out2 = io.StringIO()
    code2 = runtime2.cli_dispatcher().dispatch(["strategy", "list", "--json"], stdout=out2)
    assert code2 == 6  # ExitCode.TIMEOUT
    assert json.loads(out2.getvalue())["error"]["category"] == "GATEWAY_TIMEOUT"

    # A generic exception becomes INTERNAL_ERROR -> a distinct non-zero exit
    # (70), never NOT_READY (5).
    runtime3 = OperatorInterfaceRuntime()
    runtime3.registry.register(
        OperationKey(Surface.CLI, "strategy list"), _Raises(RuntimeError("boom"))
    )
    out3 = io.StringIO()
    code3 = runtime3.cli_dispatcher().dispatch(["strategy", "list", "--json"], stdout=out3)
    assert code3 == 70 and code3 != 5
    assert json.loads(out3.getvalue())["error"]["category"] == "INTERNAL_ERROR"


def test_rest_lifecycle_rollback_requires_confirmation_like_the_cli():
    """The shared lifecycle route is not route-level confirm-gated, but a
    ``rollback`` action must carry the same guard the CLI ``strategy rollback``
    enforces (SRS-ORCH-005 / SYS-80) — a registered handler must never receive
    an unconfirmed rollback."""

    runtime = OperatorInterfaceRuntime()
    spy = _SpyHandler()
    runtime.registry.register(
        OperationKey(Surface.REST, "POST /api/v1/strategies/{strategy_id}/lifecycle"), spy
    )

    # action=rollback WITHOUT a token → 428, handler never reached.
    status, body = runtime.dispatch_rest(
        "POST", "/api/v1/strategies/s1/lifecycle", b'{"action": "rollback"}'
    )
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"
    assert body["error"]["detail"]["action"] == "rollback"
    assert spy.calls == []

    # action=rollback WITH a body token → dispatched.
    status, _ = runtime.dispatch_rest(
        "POST", "/api/v1/strategies/s1/lifecycle", b'{"action": "rollback", "confirm": true}'
    )
    assert status == 200 and len(spy.calls) == 1

    # ... or a query token.
    status, _ = runtime.dispatch_rest(
        "POST", "/api/v1/strategies/s1/lifecycle?confirm=true", b'{"action": "rollback"}'
    )
    assert status == 200 and len(spy.calls) == 2

    # A non-irreversible action (start) is NOT gated.
    status, _ = runtime.dispatch_rest(
        "POST", "/api/v1/strategies/s1/lifecycle", b'{"action": "start"}'
    )
    assert status == 200 and len(spy.calls) == 3


# --------------------------------------------------------------------------- #
# Deferred-handler inertness
# --------------------------------------------------------------------------- #


def test_deferred_domain_op_is_inert_and_names_owner():
    runtime = OperatorInterfaceRuntime()
    # Confirmed kill switch on a BARE runtime (no atp_safety composition, so
    # no backend was explicitly supplied) → structured 501, no effect. This is
    # the SRS-SAFE-001 no-fabrication posture: uncovered capability → no
    # public surface.
    status, body = runtime.dispatch_rest("POST", "/api/v1/kill-switch?confirm=true", b"{}")
    assert status == 501
    assert body["error"]["type"] == "HANDLER_DEFERRED"
    assert body["error"]["detail"]["owner"] == "SRS-SAFE-001"
    # Status report still says not-ready: nothing was actually executed.
    _, status_body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    assert status_body["ready"] is False


def test_cli_readiness_check_exits_not_ready_when_runtime_not_ready():
    """`readiness check` must exit NOT_READY (non-zero) while the runtime reports
    ready=false, so automation cannot read a not-ready trading runtime as ready —
    even though the status handler returns a healthy HTTP 200. The downgrade is
    scoped to commands that document NOT_READY."""

    runtime = OperatorInterfaceRuntime()
    cli = runtime.cli_dispatcher()

    out = io.StringIO()
    code = cli.dispatch(["readiness", "check", "--json"], stdout=out)
    assert code == 5  # ExitCode.NOT_READY
    assert json.loads(out.getvalue())["ready"] is False

    # A non-readiness runtime-owned command (no `ready` key, does not document
    # NOT_READY) still exits OK — the downgrade does not leak to other commands.
    assert cli.dispatch(["admin", "version"], stdout=io.StringIO()) == 0


def test_handler_failures_surface_as_distinct_structured_errors_not_silent_closes():
    """A handler/dependency failure must become a structured response the
    operator can see — a timeout as 504 GATEWAY_TIMEOUT, any other exception as
    500 INTERNAL_ERROR — never a swallowed exception or a bare connection close."""

    class _Raises:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        def handle(self, request: Request) -> HandlerResult:
            raise self._exc

    timeout_runtime = OperatorInterfaceRuntime()
    timeout_runtime.registry.register(
        OperationKey(Surface.REST, "GET /api/v1/strategies"), _Raises(TimeoutError("ib timeout"))
    )
    status, body = timeout_runtime.dispatch_rest("GET", "/api/v1/strategies")
    assert status == 504
    assert body["error"]["category"] == "GATEWAY_TIMEOUT"

    internal_runtime = OperatorInterfaceRuntime()
    internal_runtime.registry.register(
        OperationKey(Surface.REST, "GET /api/v1/strategies"), _Raises(RuntimeError("boom"))
    )
    status, body = internal_runtime.dispatch_rest("GET", "/api/v1/strategies")
    assert status == 500
    assert body["error"]["category"] == "INTERNAL_ERROR"
    assert body["error"]["detail"]["exception"] == "RuntimeError"


def test_status_never_overstates_readiness_while_domain_deferred():
    runtime = OperatorInterfaceRuntime()
    _, body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    assert body["ready"] is False
    not_served = [w for w in body["workflows"] if not w["fully_served"]]
    # Every not-fully-served workflow names who still has to land.
    assert not_served, "expected domain workflows to be deferred"
    for workflow in not_served:
        assert workflow["deferred_owners"], workflow["id"]


def test_one_registered_handler_does_not_flip_a_multi_op_workflow_ready():
    """Regression: registering a single operation of a multi-operation workflow
    must not mark it served or flip `/api/v1/system/status` ready — every
    operation in every required workflow must be wired first."""

    runtime = OperatorInterfaceRuntime()
    # Strategy management spans GET /strategies, POST lifecycle, the whole
    # `strategy` CLI group, and the STRATEGY_STATE WebSocket channel. Wire one.
    runtime.registry.register(OperationKey(Surface.REST, "GET /api/v1/strategies"), _SpyHandler())
    _, body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    assert body["ready"] is False
    strategy = next(w for w in body["workflows"] if w["id"] == "STRATEGY_MANAGEMENT")
    assert strategy["implemented_operations"] >= 1
    assert strategy["implemented_operations"] < strategy["total_operations"]
    assert strategy["fully_served"] is False


def test_status_never_names_an_untracked_deferred_owner():
    """Honesty: every owner the status report names as deferred must be a real
    tracked feature in the contract deferred[] (or the runtime). The runtime
    must never point an operator at an untracked or self-referential owner."""

    from atp_runtime.contract import deferred_feature_ids, load_contract_block

    root = Path(__file__).resolve().parents[2]
    tracked = deferred_feature_ids(load_contract_block(root)) | {"runtime"}

    runtime = OperatorInterfaceRuntime()
    _, body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    for workflow in body["workflows"]:
        for owner in workflow["deferred_owners"]:
            assert owner in tracked, f"{workflow['id']} names untracked owner {owner!r}"


def test_logs_readiness_depends_only_on_its_owner_not_the_runtime_or_alerts():
    """Status honesty: the LOGS workflow's readiness must depend only on
    SRS-LOG-001 (the blocker) — it must never self-defer to the runtime feature,
    and must never pull in the shared `admin` group's unrelated commands
    (`admin alerts` -> SRS-NOTIF-001; `admin config`/`version` -> runtime), which
    would keep the system reported not-ready for the wrong reason."""

    runtime = OperatorInterfaceRuntime()
    _, body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    logs = next(w for w in body["workflows"] if w["id"] == "LOGS")
    assert logs["deferred_owners"] == ["SRS-LOG-001"]
    assert "operator-interface-runtime" not in logs["deferred_owners"]
    assert "SRS-NOTIF-001" not in logs["deferred_owners"]
    # The shared admin group contributes only `admin logs`: REST logs + CLI
    # admin-logs + LOGS WS channel = 3 operations.
    assert logs["total_operations"] == 3


def test_websocket_obligation_keeps_a_workflow_not_fully_served():
    """A workflow with a WebSocket channel is not fully served until its
    publisher is registered — registering only REST/CLI handlers must not flip
    readiness while the API-3 publisher is still absent."""

    runtime = OperatorInterfaceRuntime()
    _, body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    # SYSTEM_STATUS maps to the HEARTBEAT channel (publisher owned by SRS-UI-001).
    sysstat = next(w for w in body["workflows"] if w["id"] == "SYSTEM_STATUS")
    assert sysstat["fully_served"] is False
    assert "SRS-UI-001" in sysstat["deferred_owners"]
    before = sysstat["implemented_operations"]

    # Registering the publisher counts the channel as served.
    assert runtime.is_publisher_registered("HEARTBEAT") is False
    runtime.register_publisher("HEARTBEAT")
    assert runtime.is_publisher_registered("HEARTBEAT") is True
    _, body2 = runtime.dispatch_rest("GET", "/api/v1/system/status")
    sysstat2 = next(w for w in body2["workflows"] if w["id"] == "SYSTEM_STATUS")
    assert sysstat2["implemented_operations"] == before + 1
    assert "SRS-UI-001" not in sysstat2["deferred_owners"]


def test_publish_holds_the_session_lock_through_emit_no_unsubscribe_race():
    """Regression: a publish must not leak a stale EVENT after a concurrent
    UNSUBSCRIBE takes effect. The subscription check and the outbound frame are
    emitted under one lock hold, so an unsubscribe cannot interleave mid-deliver.
    Bounded waits throughout — the test fails (never hangs) on a regression."""

    from atp_runtime.ws_protocol import WsSession

    arm = threading.Event()
    in_emit = threading.Event()
    release = threading.Event()
    sent: list[bytes] = []

    def send(frame: bytes) -> None:
        sent.append(frame)
        if arm.is_set():  # block only the EVENT emit, not the SUBSCRIBE ack
            in_emit.set()
            release.wait(timeout=2)

    session = WsSession(send)
    session.handle_text(json.dumps({"type": "SUBSCRIBE", "channels": ["LOGS"]}))
    arm.set()

    deliverer = threading.Thread(target=lambda: session.deliver("LOGS", {"x": 1}))
    deliverer.start()
    assert in_emit.wait(2), "deliver never reached emit"

    unsub_done = threading.Event()

    def unsubscribe() -> None:
        session.handle_text(json.dumps({"type": "UNSUBSCRIBE", "channels": ["LOGS"]}))
        unsub_done.set()

    unsubscriber = threading.Thread(target=unsubscribe)
    unsubscriber.start()
    # While deliver holds the lock mid-emit, unsubscribe must be blocked.
    assert not unsub_done.wait(0.3), "unsubscribe completed mid-deliver — lock not held (race)"

    release.set()
    deliverer.join(2)
    unsubscriber.join(2)
    assert unsub_done.is_set()

    # After the unsubscribe took effect, a fresh deliver emits nothing.
    arm.clear()
    sent.clear()
    assert session.deliver("LOGS", {"y": 2}) is False
    assert sent == []


# --------------------------------------------------------------------------- #
# Dependency direction / no vendor leakage
# --------------------------------------------------------------------------- #

_ALLOWED_FIRST_PARTY = {"atp_api", "atp_cli", "atp_ws", "atp_config", "atp_runtime"}
_FORBIDDEN_TOKENS = (
    "ibapi",
    "ib_insync",
    "interactive_brokers",
    "databento",
    "sharadar",
)


def _imported_top_level_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_runtime_imports_only_peer_interface_packages():
    for source in PACKAGE.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for module in _imported_top_level_modules(tree):
            if module.startswith("atp_"):
                assert module in _ALLOWED_FIRST_PARTY, (
                    f"{source.name} imports core/engine package {module!r}; the operator "
                    f"runtime is the top layer and must only import interface peers"
                )


def test_runtime_contains_no_vendor_sdk_tokens():
    for source in PACKAGE.glob("*.py"):
        text = source.read_text(encoding="utf-8").lower()
        for token in _FORBIDDEN_TOKENS:
            assert token not in text, f"{source.name} references vendor token {token!r}"


def _template_for(path: str) -> str:
    """Map a concrete path back to its declared route template (params → {name})."""

    return path.replace("/abc/", "/{strategy_id}/")


# --- Paired safety tests for the bandit security-annotation fixes ---
# These anchor the invariants behind the `# nosec B104` (host denylist) and the
# ws_frames `usedforsecurity=False` (RFC 6455 handshake) annotations, so those
# comments can never mask a real regression.
_LOOPBACK_OR_RFC1918 = ("127.0.0.1", "::1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "localhost")
_PUBLIC_OR_ALL_INTERFACES = ("0.0.0.0", "::", "8.8.8.8", "1.2.3.4")


def test_bind_policy_refuses_all_interfaces_and_public_hosts():
    # SRS-SEC-002: "0.0.0.0"/"::" are the OPPOSITE of a bind target — the runtime
    # must refuse them and every public IP, and allow only loopback / RFC1918.
    for host in _LOOPBACK_OR_RFC1918:
        assert is_allowed_bind_host(host), f"{host} must be an allowed loopback/RFC1918 bind"
    for host in _PUBLIC_OR_ALL_INTERFACES:
        assert not is_allowed_bind_host(host), f"{host} must be refused (SRS-SEC-002)"
        with pytest.raises(BindPolicyError):
            assert_bind_allowed(host)


def test_ws_accept_key_matches_rfc6455_known_answer():
    # The SHA-1 handshake transform must produce the canonical RFC 6455 §1.3
    # example; usedforsecurity=False documents the non-security use without
    # altering the output.
    assert compute_accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
