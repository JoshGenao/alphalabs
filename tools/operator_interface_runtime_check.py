#!/usr/bin/env python3
"""Contract evidence for the operator-interface runtime (SRS-API-001).

Introspects :mod:`atp_runtime` — the HTTP + WebSocket + CLI runtime that binds
the declarative operator contract to handlers — and confirms that it serves the
*full* documented surface while enforcing the interface-level invariants that
hold independent of any domain feature:

* every declared REST route and CLI command is reachable (matched + dispatched,
  never a routing/usage miss);
* runtime-owned operations (system status, version, config) return real data;
* the ``SRS-SEC-002`` loopback bind, the ``UI-4``/``SRS-SAFE-001`` confirmation
  guard, secret redaction, and the deferral-to-named-owner are all in force;
* the runtime imports no core engine and no vendor SDK (dependency direction).

Mirrors the PASS/FAIL output style of ``tools/rest_api_check.py`` /
``tools/operator_workflow_surface_check.py``.

Invoke:
    python3 tools/operator_interface_runtime_check.py     # check (exit 0 on PASS)
"""

from __future__ import annotations

import argparse
import ast
import importlib
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
PACKAGE_DIR = PYTHON_ROOT / "atp_runtime"

_ALLOWED_FIRST_PARTY = {"atp_api", "atp_cli", "atp_ws", "atp_config", "atp_runtime"}
_VENDOR_TOKENS = ("ibapi", "ib_insync", "interactive_brokers", "databento", "sharadar")
_ALLOWED_HOSTS = ("127.0.0.1", "::1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "localhost")
# Denylist of hosts the runtime must REFUSE to bind — the opposite of a bind
# target, so bandit's "binds to all interfaces" (B104) is a false positive here.
_REFUSED_HOSTS = ("0.0.0.0", "::", "8.8.8.8", "1.2.3.4")  # nosec B104
_CONFIRM_ROUTES = (
    ("POST", "/api/v1/kill-switch"),
    ("POST", "/api/v1/strategies/sample/promote-live"),
    ("POST", "/api/v1/hot-swap"),
)


class ContractCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ContractCheckError(message)


def _load():
    if str(PYTHON_ROOT) not in sys.path:
        sys.path.insert(0, str(PYTHON_ROOT))
    runtime_mod = importlib.import_module("atp_runtime")
    atp_api = importlib.import_module("atp_api")
    atp_cli = importlib.import_module("atp_cli")
    atp_ws = importlib.import_module("atp_ws")
    return runtime_mod, atp_api, atp_cli, atp_ws


def _concrete_path(template: str) -> str:
    return re.sub(r"\{[^}]+\}", "sample", template)


def _argv_for(command) -> list[str]:
    argv = [command.group.value]
    if command.name:
        argv.append(command.name)
    for argument in command.arguments:
        if not argument.required:
            continue
        if argument.is_flag:
            argv.append(argument.name)
        elif argument.name.startswith("--"):
            argv += [argument.name, "sample"]
        else:
            argv.append("sample")
    return argv


def check_owner_validation(rt, atp_api) -> str:
    from atp_runtime.contract import load_contract_block

    block = load_contract_block(ROOT)
    rt.validate_owners(
        block,
        atp_api.ROUTES,
        importlib.import_module("atp_cli").COMMANDS,
        importlib.import_module("atp_ws").EVENT_CHANNELS,
    )
    return "REST/CLI/WebSocket owner maps validated; every owner is runtime or in deferred[]"


def check_every_rest_route_served(rt, atp_api) -> str:
    runtime = rt.OperatorInterfaceRuntime(ROOT)
    served = 0
    for route in atp_api.ROUTES:
        path = _concrete_path(route.path)
        if route.requires_confirmation:
            path = f"{path}?confirm=true"
        status, _ = runtime.dispatch_rest(route.method.value, path, b"{}")
        if status not in (200, 501):
            fail(f"REST route {route.method.value} {route.path} not served (status {status})")
        served += 1
    return f"all {served} declared REST routes are matched + dispatched (200 or deferred 501)"


def check_every_cli_command_dispatched(rt, atp_cli) -> str:
    runtime = rt.OperatorInterfaceRuntime(ROOT)
    dispatched = 0
    for command in atp_cli.COMMANDS:
        try:
            code = runtime.cli_dispatcher().dispatch(_argv_for(command), stdout=io.StringIO())
        except SystemExit as exc:
            fail(f"CLI command {command.invocation!r} raised SystemExit({exc.code})")
        # 0 runtime-owned, 5 readiness-not-ready, 64 deferred NOT_IMPLEMENTED —
        # all are real dispatch outcomes; 2 (usage error) would be a failure.
        if code not in (0, 5, 64):
            fail(f"CLI command {command.invocation!r} returned non-dispatch exit {code}")
        dispatched += 1
    return f"all {dispatched} declared CLI commands dispatch (real 0 / not-ready 5 / deferred 64)"


def check_runtime_owned_real(rt) -> str:
    runtime = rt.OperatorInterfaceRuntime(ROOT)
    status, body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    if status != 200 or "workflows" not in body:
        fail("GET /api/v1/system/status did not return a real status body")
    if body["ready"] is not False:
        fail("status.ready must be False while domain workflows are deferred")
    out = io.StringIO()
    if runtime.cli_dispatcher().dispatch(["admin", "version", "--json"], stdout=out) != 0:
        fail("`admin version` did not return exit 0")
    return "runtime-owned ops (system status, version) return real data; ready=False (honest)"


def check_readiness_exit_code(rt) -> str:
    runtime = rt.OperatorInterfaceRuntime(ROOT)
    code = runtime.cli_dispatcher().dispatch(["readiness", "check", "--json"], stdout=io.StringIO())
    if code != 5:
        fail(f"`readiness check` must exit NOT_READY (5) while ready=false; got {code}")
    return "`readiness check` exits NOT_READY (5) while ready=false (no false-ready success)"


def check_confirmation_guard(rt) -> str:
    runtime = rt.OperatorInterfaceRuntime(ROOT)
    for method, path in _CONFIRM_ROUTES:
        status, body = runtime.dispatch_rest(method, path, b"{}")
        if status != 428 or body["error"]["category"] != "CONFIRMATION_REQUIRED":
            fail(f"{method} {path} did not require confirmation (status {status})")
    return "all 3 confirmation-required workflows reject state mutation without a token (428)"


def check_lifecycle_action_confirmation(rt) -> str:
    runtime = rt.OperatorInterfaceRuntime(ROOT)
    status, body = runtime.dispatch_rest(
        "POST", "/api/v1/strategies/sample/lifecycle", b'{"action": "rollback"}'
    )
    if status != 428 or body["error"]["category"] != "CONFIRMATION_REQUIRED":
        fail("lifecycle action=rollback dispatched without confirmation (SRS-ORCH-005)")
    return "lifecycle rollback action requires confirmation, like the CLI (SRS-ORCH-005 / UI-4)"


def check_secret_redaction(rt) -> str:
    runtime = rt.OperatorInterfaceRuntime(ROOT)
    out = io.StringIO()
    runtime.cli_dispatcher().dispatch(["admin", "config", "--json"], stdout=out)
    import json

    keys = json.loads(out.getvalue())["keys"]
    for entry in keys:
        if entry["secret"] and entry["value"] != "***REDACTED***":
            fail(f"config key {entry['name']!r} leaked a secret value")
    return f"config view redacts every secret value ({len(keys)} keys, SRS-SEC-001)"


def check_bind_policy(rt) -> str:
    for host in _ALLOWED_HOSTS:
        if not rt.is_allowed_bind_host(host):
            fail(f"bind host {host!r} should be allowed (loopback/RFC1918)")
    for host in _REFUSED_HOSTS:
        if rt.is_allowed_bind_host(host):
            fail(f"bind host {host!r} should be refused (SRS-SEC-002)")
        try:
            rt.assert_bind_allowed(host)
        except rt.BindPolicyError:
            continue
        fail(f"assert_bind_allowed({host!r}) did not raise BindPolicyError")
    return "loopback/RFC1918 bind allowed; public/unspecified refused (SRS-SEC-002)"


def check_deferred_owners_named(rt, atp_api) -> str:
    runtime = rt.OperatorInterfaceRuntime(ROOT)
    _, body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    if body["ready"] is not False:
        fail("status.ready must be False while any required workflow is not fully served")
    not_served = [w for w in body["workflows"] if not w["fully_served"]]
    if not not_served:
        fail("expected domain workflows to be deferred")
    owners: set[str] = set()
    for workflow in not_served:
        if not workflow["deferred_owners"]:
            fail(f"not-fully-served workflow {workflow['id']} names no deferred owner")
        owners.update(workflow["deferred_owners"])
    return f"every not-fully-served workflow names its deferred owners: {', '.join(sorted(owners))}"


def check_websocket_channels(rt, atp_ws) -> str:
    declared = frozenset(c.name.value for c in atp_ws.EVENT_CHANNELS)
    if rt.VALID_CHANNELS != declared:
        fail("runtime VALID_CHANNELS diverge from declared EVENT_CHANNELS")
    return f"WebSocket channel set matches the {len(declared)} declared event channels"


def check_real_cli_entrypoint(rt) -> str:
    runtime = rt.OperatorInterfaceRuntime(ROOT)
    discovery = runtime.dispatch_rest("GET", "/")[1]
    program = discovery["surfaces"]["cli"]["program"]
    if program != "python -m atp_runtime":
        fail(f"discovery must advertise the real CLI entrypoint; got {program!r}")
    if not (PACKAGE_DIR / "__main__.py").exists():
        fail("the `python -m atp_runtime` entrypoint (__main__.py) is missing")
    return "documented CLI entrypoint is `python -m atp_runtime` (real dispatcher, not the stub)"


def check_dependency_direction() -> str:
    for source in sorted(PACKAGE_DIR.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name.startswith("atp_") and name not in _ALLOWED_FIRST_PARTY:
                    fail(
                        f"{source.name} imports non-interface package {name!r} (dependency direction)"
                    )
        lowered = source.read_text(encoding="utf-8").lower()
        for token in _VENDOR_TOKENS:
            if token in lowered:
                fail(f"{source.name} references vendor token {token!r}")
    return "atp_runtime imports only interface peers; no vendor SDK tokens"


def run_checks() -> list[str]:
    rt, atp_api, atp_cli, atp_ws = _load()
    return [
        check_owner_validation(rt, atp_api),
        check_every_rest_route_served(rt, atp_api),
        check_every_cli_command_dispatched(rt, atp_cli),
        check_runtime_owned_real(rt),
        check_readiness_exit_code(rt),
        check_confirmation_guard(rt),
        check_lifecycle_action_confirmation(rt),
        check_secret_redaction(rt),
        check_bind_policy(rt),
        check_deferred_owners_named(rt, atp_api),
        check_websocket_channels(rt, atp_ws),
        check_real_cli_entrypoint(rt),
        check_dependency_direction(),
    ]


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Operator-interface runtime contract evidence").parse_args(
        argv
    )
    try:
        evidence = run_checks()
    except ContractCheckError as error:
        print(f"OPERATOR INTERFACE RUNTIME FAIL: {error}", file=sys.stderr)
        return 1

    print("OPERATOR INTERFACE RUNTIME PASS — SRS-API-001 operator-interface-runtime substrate")
    for item in evidence:
        print(f"- {item}")
    print(
        "- SRS-API-001 stays passes:false: domain handlers "
        "(SRS-EXE-001 / SRS-ORCH-004/005 / SRS-RESV-002/003 / SRS-BT-001 / "
        "SRS-DATA-002 / SRS-LOG-001 / SRS-NOTIF-001) register on the registry as they land"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
