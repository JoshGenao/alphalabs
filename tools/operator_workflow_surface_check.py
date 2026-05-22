#!/usr/bin/env python3
"""Operator workflow surface SDK-surface contract check for SRS-API-001.

Verifies that the three already-declared interface packages
(``python/atp_api`` REST routes, ``python/atp_cli`` CLI commands,
``python/atp_ws`` WebSocket channels) jointly cover the eight operator
workflows the SRS-API-001 acceptance criterion enumerates verbatim:

    Live designation, strategy management, kill switch, Hot-Swap,
    Reservoir ranking, backtests, system status, and logs are available
    through documented API paths or CLI commands.

The per-surface checks (``rest_api_check.py`` / ``cli_check.py`` /
``websocket_api_check.py``) each verify their own slice; this check adds
the SRS-API-001 cross-surface AC binding so a CLI group rename, a REST
route deletion, or a ``Capability``/``Group`` bucket renaming cannot
silently drop an operator workflow.

The PASS line is ``SRS-API-001 SDK-SURFACE PASS`` (not bare ``PASS``) to
mirror the SDK-004 / SDK-005 / ERR-9 partial-pass pattern: the SDK
surface is locked, the request/response handlers + HTTP/WebSocket server
+ CLI runner dispatcher + every downstream subsystem (SRS-EXE-001,
SRS-EXE-006, SRS-ORCH-004, SRS-RESV-002..006, SRS-BT-001 / 009,
SRS-DATA-002, SRS-LOG-001, SRS-NOTIF-001) still hold the feature_list
entry at ``passes:false``.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from atp_api import routes as rest_routes  # noqa: E402
from atp_cli import commands as cli_commands  # noqa: E402
from atp_ws import channels as ws_channels  # noqa: E402

_CONTRACT_BLOCK = "operator_workflow_surface_contract"
_RUNTIME_SERVICES = ROOT / "architecture" / "runtime_services.json"
_AC_CANONICAL = (
    "Live designation, strategy management, kill switch, Hot-Swap, "
    "Reservoir ranking, backtests, system status, and logs"
)
_WRITE_METHODS = {"POST", "PUT", "DELETE"}
_CROSS_SURFACE_IMPORT_PAIRS: tuple[tuple[str, str], ...] = (
    ("python/atp_api", "atp_cli"),
    ("python/atp_api", "atp_ws"),
    ("python/atp_cli", "atp_api"),
    ("python/atp_cli", "atp_ws"),
    ("python/atp_ws", "atp_api"),
    ("python/atp_ws", "atp_cli"),
)


class OperatorWorkflowSurfaceCheckError(AssertionError):
    """Raised when the operator workflow surface diverges from the contract."""


def _fail(message: str) -> None:
    raise OperatorWorkflowSurfaceCheckError(message)


def _load_contract(root: Path = ROOT) -> dict[str, Any]:
    raw = json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))
    block = raw.get(_CONTRACT_BLOCK)
    if not isinstance(block, dict):
        _fail(f"runtime_services.json is missing the {_CONTRACT_BLOCK!r} block")
    return block


def _assert_keys(block: dict[str, Any], required: tuple[str, ...]) -> None:
    missing = [k for k in required if k not in block]
    if missing:
        _fail(f"{_CONTRACT_BLOCK!r} is missing required keys: {missing}")


def _assert_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{name} must be an int (got {type(value).__name__})")
    return value


def _assert_str_list(name: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        _fail(f"{name} must be a list[str] (got {value!r})")
    return list(value)


def check_contract_block_shape(block: dict[str, Any]) -> str:
    required = (
        "description",
        "ac_traces",
        "syrs_refs",
        "strs_refs",
        "surface_packages",
        "required_workflow_ids",
        "ac_workflows",
        "ac_phrase_canonical",
        "orphan_buckets_allowed",
        "min_surface_entries_per_workflow",
        "loopback_bind_host",
        "expected_auth_model",
        "forbidden_remote_bind_tokens",
        "confirmation_required_workflows",
        "snapshot_paths",
        "vendor_forbidden_tokens",
        "deferred",
    )
    _assert_keys(block, required)
    minimum = _assert_int(
        "min_surface_entries_per_workflow", block["min_surface_entries_per_workflow"]
    )
    if minimum < 1:
        _fail(f"min_surface_entries_per_workflow must be >= 1 (got {minimum})")
    _assert_str_list("required_workflow_ids", block["required_workflow_ids"])
    if not isinstance(block["ac_workflows"], list) or not block["ac_workflows"]:
        _fail("ac_workflows must be a non-empty list")
    return f"contract block declares all {len(required)} required keys with valid types"


def check_surface_packages(block: dict[str, Any], root: Path) -> str:
    expected_ids = ("rest", "cli", "websocket")
    packages = block["surface_packages"]
    if not isinstance(packages, list) or len(packages) != 3:
        _fail(f"surface_packages must enumerate exactly 3 surfaces (got {len(packages)})")
    actual_ids = tuple(p.get("id") for p in packages)
    if actual_ids != expected_ids:
        _fail(f"surface_packages ids must be {expected_ids!r} in order (got {actual_ids!r})")
    for spec in packages:
        for key in (
            "id",
            "package",
            "module",
            "collection",
            "bucket_enum",
            "bucket_attr",
            "snapshot",
        ):
            if not isinstance(spec.get(key), str) or not spec[key]:
                _fail(f"surface_packages[{spec.get('id', '?')!r}] missing/invalid key {key!r}")
        module_path = root / spec["module"]
        if not module_path.is_file():
            _fail(f"surface_packages[{spec['id']!r}].module does not exist: {spec['module']}")
        snap_path = root / spec["snapshot"]
        if not snap_path.is_file():
            _fail(f"surface_packages[{spec['id']!r}].snapshot does not exist: {spec['snapshot']}")
    return "surface_packages enumerates (rest, cli, websocket) with all module + snapshot files present"


def check_surface_collections_resolve(block: dict[str, Any]) -> str:
    summary_parts: list[str] = []
    for spec in block["surface_packages"]:
        module = importlib.import_module(spec["package"] + "." + Path(spec["module"]).stem)
        collection = getattr(module, spec["collection"], None)
        if not isinstance(collection, tuple) or not collection:
            _fail(
                f"{spec['package']}.{Path(spec['module']).stem}.{spec['collection']} "
                f"is not a non-empty tuple"
            )
        bucket_enum = getattr(module, spec["bucket_enum"], None)
        if not isinstance(bucket_enum, type) or not issubclass(bucket_enum, StrEnum):
            _fail(
                f"{spec['package']}.{Path(spec['module']).stem}.{spec['bucket_enum']} "
                f"is not a StrEnum subclass"
            )
        summary_parts.append(f"{spec['id']}={len(collection)} entries, {len(bucket_enum)} buckets")
    return "surface collections resolved: " + "; ".join(summary_parts)


def _workflow_index(block: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {w["id"]: w for w in block["ac_workflows"]}


def check_required_workflow_ids_match(block: dict[str, Any]) -> str:
    declared = list(block["required_workflow_ids"])
    derived = [w["id"] for w in block["ac_workflows"]]
    if declared != derived:
        _fail(
            f"required_workflow_ids ({declared}) does not match the order of "
            f"ac_workflows ids ({derived})"
        )
    if len(declared) != 8:
        _fail(f"required_workflow_ids must contain exactly 8 entries (got {len(declared)})")
    if len(set(declared)) != len(declared):
        _fail(f"required_workflow_ids contains duplicates: {declared}")
    return f"required_workflow_ids declares the {len(declared)} AC workflows in order"


def check_workflow_buckets_exist(block: dict[str, Any]) -> str:
    rest_buckets = {b.value for b in rest_routes.Capability}
    cli_buckets = {g.value for g in cli_commands.Group}
    ws_buckets = {c.value for c in ws_channels.Channel}
    missing: list[str] = []
    for wf in block["ac_workflows"]:
        for cap in _assert_str_list(f"{wf['id']}.rest_capabilities", wf["rest_capabilities"]):
            if cap not in rest_buckets:
                missing.append(f"{wf['id']}.rest:{cap}")
        for grp in _assert_str_list(f"{wf['id']}.cli_groups", wf["cli_groups"]):
            if grp not in cli_buckets:
                missing.append(f"{wf['id']}.cli:{grp}")
        for chan in _assert_str_list(f"{wf['id']}.websocket_channels", wf["websocket_channels"]):
            if chan not in ws_buckets:
                missing.append(f"{wf['id']}.ws:{chan}")
        rc = wf.get("requires_confirmation")
        if not isinstance(rc, bool):
            _fail(f"{wf['id']}.requires_confirmation must be a bool (got {type(rc).__name__})")
    if missing:
        _fail(f"ac_workflows reference unknown buckets: {missing}")
    return (
        f"all {len(block['ac_workflows'])} workflows map to existing buckets "
        f"({len(rest_buckets)} REST / {len(cli_buckets)} CLI / {len(ws_buckets)} WS)"
    )


def check_workflow_min_surface_coverage(block: dict[str, Any]) -> str:
    minimum = block["min_surface_entries_per_workflow"]
    shortfalls: list[str] = []
    for wf in block["ac_workflows"]:
        rest_caps = set(wf["rest_capabilities"])
        cli_grps = set(wf["cli_groups"])
        rest_count = sum(1 for r in rest_routes.ROUTES if r.capability.value in rest_caps)
        cli_count = sum(1 for c in cli_commands.COMMANDS if c.group.value in cli_grps)
        if rest_count + cli_count < minimum:
            shortfalls.append(
                f"{wf['id']} has {rest_count} REST + {cli_count} CLI entries (< {minimum})"
            )
        if not wf["rest_capabilities"] and not wf["cli_groups"]:
            _fail(
                f"{wf['id']} has no REST capabilities AND no CLI groups; "
                "AC requires at least one of REST or CLI"
            )
    if shortfalls:
        _fail("AC coverage shortfall: " + "; ".join(shortfalls))
    return f"every workflow has >= {minimum} documented REST+CLI entry"


def check_capability_orphans(block: dict[str, Any]) -> str:
    mapped = {cap for wf in block["ac_workflows"] for cap in wf["rest_capabilities"]}
    allowed_orphans = set(block["orphan_buckets_allowed"].get("rest", []))
    orphans = {c.value for c in rest_routes.Capability} - mapped - allowed_orphans
    if orphans:
        _fail(
            f"REST Capability members are not mapped to any AC workflow and not in "
            f"orphan_buckets_allowed.rest: {sorted(orphans)}"
        )
    return f"every REST Capability is mapped or allow-listed (orphans allowed: {sorted(allowed_orphans)})"


def check_group_orphans(block: dict[str, Any]) -> str:
    mapped = {grp for wf in block["ac_workflows"] for grp in wf["cli_groups"]}
    allowed_orphans = set(block["orphan_buckets_allowed"].get("cli", []))
    orphans = {g.value for g in cli_commands.Group} - mapped - allowed_orphans
    if orphans:
        _fail(
            f"CLI Group members are not mapped to any AC workflow and not in "
            f"orphan_buckets_allowed.cli: {sorted(orphans)}"
        )
    return f"every CLI Group is mapped or allow-listed (orphans allowed: {sorted(allowed_orphans)})"


def check_channel_orphans(block: dict[str, Any]) -> str:
    mapped = {chan for wf in block["ac_workflows"] for chan in wf["websocket_channels"]}
    allowed_orphans = set(block["orphan_buckets_allowed"].get("websocket", []))
    orphans = {c.value for c in ws_channels.Channel} - mapped - allowed_orphans
    if orphans:
        _fail(
            f"WebSocket Channel members are not mapped to any AC workflow and not in "
            f"orphan_buckets_allowed.websocket: {sorted(orphans)}"
        )
    return (
        f"every WebSocket Channel is mapped or allow-listed "
        f"(orphans allowed: {sorted(allowed_orphans)})"
    )


def check_confirmation_required_workflows(block: dict[str, Any]) -> str:
    workflow_ids = set(block["required_workflow_ids"])
    confirmation_ids = _assert_str_list(
        "confirmation_required_workflows", block["confirmation_required_workflows"]
    )
    extra = set(confirmation_ids) - workflow_ids
    if extra:
        _fail(f"confirmation_required_workflows names unknown workflows: {sorted(extra)}")
    index = _workflow_index(block)
    failures: list[str] = []
    for wid in confirmation_ids:
        wf = index[wid]
        if not wf.get("requires_confirmation"):
            _fail(
                f"workflow {wid} is in confirmation_required_workflows but "
                f"its ac_workflows.requires_confirmation is False"
            )
        for cap in wf["rest_capabilities"]:
            for route in rest_routes.ROUTES:
                if route.capability.value != cap:
                    continue
                if route.method.value in _WRITE_METHODS and not route.requires_confirmation:
                    failures.append(f"REST {route.method.value} {route.path}")
        for grp in wf["cli_groups"]:
            for cmd in cli_commands.COMMANDS:
                if cmd.group.value != grp:
                    continue
                if not cmd.requires_confirmation and _is_state_mutating_command(cmd):
                    failures.append(f"CLI `{cmd.invocation}`")
    if failures:
        _fail(
            "state-mutating entries on confirmation-required workflows are missing "
            f"requires_confirmation: {failures}"
        )
    return (
        f"every state-mutating entry on the {len(confirmation_ids)} confirmation-required "
        f"workflows carries requires_confirmation=True"
    )


def _is_state_mutating_command(cmd: cli_commands.Command) -> bool:
    name = cmd.name.lower()
    return any(
        token in name for token in ("activate", "promote", "rollback", "trigger", "start", "stop")
    )


def check_loopback_bind_policy(block: dict[str, Any]) -> str:
    expected = block["loopback_bind_host"]
    forbidden = block["forbidden_remote_bind_tokens"]
    for label, host in (
        ("atp_api.routes.BIND_HOST", rest_routes.BIND_HOST),
        ("atp_ws.channels.BIND_HOST", ws_channels.BIND_HOST),
    ):
        if host != expected:
            _fail(f"{label} is {host!r}; SRS-SEC-002 requires {expected!r}")
        if any(token in host for token in forbidden):
            _fail(f"{label}={host!r} contains a forbidden remote-bind token from {forbidden}")
    return f"REST + WebSocket bind to {expected} (SRS-SEC-002 loopback policy)"


def check_auth_model_parity(block: dict[str, Any]) -> str:
    expected = block["expected_auth_model"]
    triples = (
        ("atp_api.routes.AUTH_MODEL", rest_routes.AUTH_MODEL),
        ("atp_cli.commands.AUTH_MODEL", cli_commands.AUTH_MODEL),
        ("atp_ws.channels.AUTH_MODEL", ws_channels.AUTH_MODEL),
    )
    for label, value in triples:
        if value != expected:
            _fail(f"{label}={value!r} does not match expected_auth_model {expected!r}")
    return f"all three surfaces declare auth_model={expected!r}"


def check_snapshots_present(block: dict[str, Any], root: Path) -> str:
    for rel in block["snapshot_paths"]:
        path = root / rel
        if not path.is_file():
            _fail(f"snapshot {rel} is missing on disk")
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _fail(f"snapshot {rel} is not valid JSON: {exc}")
    return f"all {len(block['snapshot_paths'])} contract snapshots are present and parse as JSON"


def check_workflow_routes_carry_srs_refs(block: dict[str, Any]) -> str:
    failures: list[str] = []
    for wf in block["ac_workflows"]:
        for cap in wf["rest_capabilities"]:
            for route in rest_routes.ROUTES:
                if route.capability.value != cap:
                    continue
                if not route.srs_refs:
                    failures.append(f"REST {route.path}")
        for grp in wf["cli_groups"]:
            for cmd in cli_commands.COMMANDS:
                if cmd.group.value != grp:
                    continue
                if not cmd.srs_refs:
                    failures.append(f"CLI `{cmd.invocation}`")
    if failures:
        _fail(f"workflow-mapped entries missing srs_refs: {failures}")
    return "every workflow-mapped REST route and CLI command carries srs_refs"


def check_deferred_list(block: dict[str, Any]) -> str:
    deferred = block["deferred"]
    if not isinstance(deferred, list) or not deferred:
        _fail("deferred must be a non-empty list (SDK-surface partial-pass needs the audit trail)")
    for entry in deferred:
        if not isinstance(entry, dict):
            _fail(f"deferred entries must be dicts (got {type(entry).__name__})")
        for key in ("feature", "what"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                _fail(f"deferred entry missing non-empty {key!r}: {entry}")
    required_features = {
        "SRS-EXE-001",
        "SRS-EXE-006",
        "SRS-RESV-003",
        "SRS-BT-001",
        "SRS-LOG-001",
        "SRS-NOTIF-001",
    }
    declared = {entry["feature"] for entry in deferred}
    missing = required_features - declared
    if missing:
        _fail(
            "deferred list is missing the downstream features that must land for "
            f"passes:true: {sorted(missing)}"
        )
    return f"deferred list has {len(deferred)} entries, all required downstream features named"


def check_no_remote_bind_in_declarations(block: dict[str, Any]) -> str:
    forbidden = tuple(block["forbidden_remote_bind_tokens"])
    surveyed: list[tuple[str, str]] = []
    for route in rest_routes.ROUTES:
        for label in (route.path, route.summary, *route.request_fields, *route.response_fields):
            for token in forbidden:
                if token in label:
                    surveyed.append((f"REST {route.path}", token))
    for cmd in cli_commands.COMMANDS:
        for label in (cmd.summary, *(arg.summary for arg in cmd.arguments)):
            for token in forbidden:
                if token in label:
                    surveyed.append((f"CLI {cmd.invocation}", token))
    for chan in ws_channels.EVENT_CHANNELS:
        for label in (chan.summary, *chan.payload_fields):
            for token in forbidden:
                if token in label:
                    surveyed.append((f"WS {chan.name.value}", token))
    if surveyed:
        _fail(f"surface declarations contain forbidden remote-bind tokens: {surveyed}")
    return f"no surface declaration contains any of {list(forbidden)}"


def check_cross_surface_dependency_direction(block: dict[str, Any], root: Path) -> str:
    del block
    for parent_rel, forbidden_package in _CROSS_SURFACE_IMPORT_PAIRS:
        parent = root / parent_rel
        for py in parent.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                _fail(f"{py} is not parseable: {exc}")
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == forbidden_package or alias.name.startswith(
                            forbidden_package + "."
                        ):
                            _fail(
                                f"{py.relative_to(root)} imports {alias.name!r}; "
                                f"{parent_rel} must not depend on {forbidden_package!r}"
                            )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module == forbidden_package or module.startswith(forbidden_package + "."):
                        _fail(
                            f"{py.relative_to(root)} imports from {module!r}; "
                            f"{parent_rel} must not depend on {forbidden_package!r}"
                        )
    return "atp_api / atp_cli / atp_ws do not import from each other"


def check_vendor_token_isolation(block: dict[str, Any], root: Path) -> str:
    forbidden = tuple(block["vendor_forbidden_tokens"])
    survey: list[str] = []
    for spec in block["surface_packages"]:
        package_root = root / Path(spec["module"]).parent
        for py in package_root.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text.lower():
                    survey.append(f"{py.relative_to(root)}:{token}")
    if survey:
        _fail(f"vendor SDK tokens leaked into the operator surface packages: {survey}")
    return f"none of {list(forbidden)} appears in atp_api / atp_cli / atp_ws sources"


def check_ac_phrase_spells_workflow_labels(block: dict[str, Any]) -> str:
    if block["ac_phrase_canonical"] != _AC_CANONICAL:
        _fail(
            "ac_phrase_canonical does not match the SRS-API-001 AC text verbatim; "
            "drift between SRS and contract will silently mismatch the workflow set"
        )
    labels = [wf["label"] for wf in block["ac_workflows"]]
    expected_labels = [
        "Live designation",
        "Strategy management",
        "Kill switch",
        "Hot-Swap",
        "Reservoir ranking",
        "Backtests",
        "System status",
        "Logs",
    ]
    if labels != expected_labels:
        _fail(
            f"ac_workflows labels {labels} do not spell the AC text in order "
            f"(expected {expected_labels})"
        )
    return "ac_phrase_canonical + ac_workflows labels spell the SRS-API-001 AC verbatim"


def check_workflow_id_to_phrase_uniqueness(block: dict[str, Any]) -> str:
    phrases = [wf["ac_phrase"] for wf in block["ac_workflows"]]
    if len(set(phrases)) != len(phrases):
        _fail(f"ac_workflows ac_phrase values must be unique (got {phrases})")
    return f"all {len(phrases)} ac_phrase values are unique"


def assert_operator_workflow_surface_static(
    root: Path = ROOT,
) -> list[str]:
    block = _load_contract(root=root)
    return [
        check_contract_block_shape(block),
        check_surface_packages(block, root),
        check_surface_collections_resolve(block),
        check_required_workflow_ids_match(block),
        check_ac_phrase_spells_workflow_labels(block),
        check_workflow_id_to_phrase_uniqueness(block),
        check_workflow_buckets_exist(block),
        check_workflow_min_surface_coverage(block),
        check_capability_orphans(block),
        check_group_orphans(block),
        check_channel_orphans(block),
        check_confirmation_required_workflows(block),
        check_loopback_bind_policy(block),
        check_auth_model_parity(block),
        check_snapshots_present(block, root),
        check_workflow_routes_carry_srs_refs(block),
        check_no_remote_bind_in_declarations(block),
        check_cross_surface_dependency_direction(block, root),
        check_vendor_token_isolation(block, root),
        check_deferred_list(block),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root (default: parent of this script)"
    )
    args = parser.parse_args(argv)
    try:
        evidence = assert_operator_workflow_surface_static(root=args.root)
    except OperatorWorkflowSurfaceCheckError as exc:
        print(f"SRS-API-001 SDK-SURFACE FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "SRS-API-001 SDK-SURFACE PASS — operator workflow coverage contract "
        "(REST/CLI/WebSocket handlers + HTTP server + WS server + CLI runner "
        "dispatcher deferred to SRS-EXE-001 / SRS-EXE-006 / SRS-ORCH-004 / "
        "SRS-RESV-002..006 / SRS-BT-001 / SRS-BT-009 / SRS-DATA-002 / "
        "SRS-LOG-001 / SRS-NOTIF-001 / operator-interface-runtime)"
    )
    for line in evidence:
        print(f"  * {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
