#!/usr/bin/env python3
"""Contract evidence script for feature API-2.

Introspects the declarative ``atp_api`` package and confirms that the
operator REST API contract exposes every capability bucket required by
API-2's description, tracing each to ``SRS-API-001`` and the supporting
clauses listed in ``docs/SRS.md`` §7 and §8.

Mirrors the PASS/FAIL output style of ``tools/architecture_check.py`` and
``tools/strategy_api_check.py``.

Invoke:
    python3 tools/rest_api_check.py            # check (exit 0 on PASS)
    python3 tools/rest_api_check.py --update   # rewrite frozen OpenAPI snapshot
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
SNAPSHOT_PATH = ROOT / "python" / "atp_api" / "openapi.json"


class ContractCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ContractCheckError(message)


def _load() -> object:
    if str(PYTHON_ROOT) not in sys.path:
        sys.path.insert(0, str(PYTHON_ROOT))
    return importlib.import_module("atp_api")


def _routes_for(module, capability_name: str):
    capability = getattr(module.Capability, capability_name)
    matches = [route for route in module.ROUTES if route.capability is capability]
    if not matches:
        fail(f"No route declared for capability {capability_name}")
    return matches


def _expect_path_prefix(routes, prefix: str = "/api/v1/") -> None:
    for route in routes:
        if not route.path.startswith(prefix):
            fail(f"Route {route.method} {route.path} does not start with {prefix}")


def _expect_srs_refs(routes, required: Iterable[str]) -> None:
    refs = set()
    for route in routes:
        if not route.srs_refs:
            fail(f"Route {route.method} {route.path} has empty srs_refs")
        refs.update(route.srs_refs)
    missing = sorted(set(required) - refs)
    if missing:
        fail(
            "Capability is missing required SRS traces: "
            f"{', '.join(missing)}"
        )


def _expect_method(routes, method_name: str) -> None:
    if not any(route.method.value == method_name for route in routes):
        fail(f"Capability has no {method_name} route")


def _summary(label: str, routes) -> str:
    items = ", ".join(f"{r.method.value} {r.path}" for r in routes)
    return f"{label}: {items}"


# --------------------------------------------------------------------------- #
# Capability checks (one per API-2 bucket)
# --------------------------------------------------------------------------- #


def check_api_001_lifecycle(module) -> str:
    routes = _routes_for(module, "STRATEGY_LIFECYCLE")
    _expect_path_prefix(routes)
    _expect_method(routes, "GET")
    _expect_method(routes, "POST")
    _expect_srs_refs(routes, ("SRS-ORCH-004", "SRS-ORCH-005", "SYS-2c"))
    return _summary("STRATEGY_LIFECYCLE (SRS-ORCH-004/005, SYS-2c)", routes)


def check_api_002_live_designation(module) -> str:
    routes = _routes_for(module, "LIVE_DESIGNATION")
    _expect_path_prefix(routes)
    _expect_method(routes, "POST")
    _expect_srs_refs(routes, ("SRS-API-001", "SYS-2c", "SYS-2d"))
    if not all(route.requires_confirmation for route in routes):
        fail("LIVE_DESIGNATION must set requires_confirmation=True (SYS-2d)")
    return _summary("LIVE_DESIGNATION (SYS-2c/2d, requires_confirmation)", routes)


def check_api_003_kill_switch(module) -> str:
    routes = _routes_for(module, "KILL_SWITCH")
    _expect_path_prefix(routes)
    _expect_method(routes, "POST")
    _expect_srs_refs(routes, ("SRS-SAFE-001", "SYS-44a", "SYS-44b", "NFR-P3"))
    if not all(route.requires_confirmation for route in routes):
        fail("KILL_SWITCH must set requires_confirmation=True (SRS-SAFE-001)")
    return _summary("KILL_SWITCH (SRS-SAFE-001, SYS-44a/b, NFR-P3)", routes)


def check_api_004_hot_swap(module) -> str:
    routes = _routes_for(module, "HOT_SWAP")
    _expect_path_prefix(routes)
    _expect_method(routes, "POST")
    _expect_method(routes, "GET")
    _expect_srs_refs(
        routes,
        ("SRS-RESV-003", "SRS-RESV-004", "SRS-RESV-005", "SRS-RESV-006", "SYS-49a"),
    )
    return _summary("HOT_SWAP (SRS-RESV-003..006, SYS-49a..e)", routes)


def check_api_005_backtest_launch(module) -> str:
    routes = _routes_for(module, "BACKTEST_LAUNCH")
    _expect_path_prefix(routes)
    _expect_method(routes, "POST")
    _expect_srs_refs(routes, ("SRS-BT-001", "SYS-14", "SYS-43a"))
    return _summary("BACKTEST_LAUNCH (SRS-BT-001, SYS-14, SYS-43a)", routes)


def check_api_006_backtest_query(module) -> str:
    routes = _routes_for(module, "BACKTEST_QUERY")
    _expect_path_prefix(routes)
    _expect_method(routes, "GET")
    _expect_srs_refs(routes, ("SRS-BT-009", "SYS-21", "SYS-42"))
    response_fields = {field for route in routes for field in route.response_fields}
    for required_field in ("trade_log", "equity_curve", "benchmark_comparison", "metrics"):
        if required_field not in response_fields:
            fail(f"BACKTEST_QUERY response missing field {required_field} (SYS-42)")
    return _summary("BACKTEST_QUERY (SRS-BT-009, SYS-21, SYS-42)", routes)


def check_api_007_reservoir_ranking(module) -> str:
    routes = _routes_for(module, "RESERVOIR_RANKING")
    _expect_path_prefix(routes)
    _expect_method(routes, "GET")
    _expect_srs_refs(routes, ("SRS-RESV-002", "SYS-48"))
    response_fields = {field for route in routes for field in route.response_fields}
    for required_field in ("sharpe", "sortino", "momentum_score"):
        if required_field not in response_fields:
            fail(f"RESERVOIR_RANKING response missing {required_field} (SRS-RESV-002)")
    return _summary("RESERVOIR_RANKING (SRS-RESV-002, SYS-48)", routes)


def check_api_008_watchlist(module) -> str:
    routes = _routes_for(module, "WATCHLIST_CONFIG")
    _expect_path_prefix(routes)
    _expect_method(routes, "GET")
    _expect_method(routes, "PUT")
    _expect_srs_refs(routes, ("SRS-DATA-002", "SYS-22b"))
    return _summary("WATCHLIST_CONFIG (SRS-DATA-002, SYS-22b)", routes)


def check_api_009_system_status(module) -> str:
    routes = _routes_for(module, "SYSTEM_STATUS")
    _expect_path_prefix(routes)
    _expect_method(routes, "GET")
    _expect_srs_refs(routes, ("SYS-76", "SYS-39"))
    response_fields = {field for route in routes for field in route.response_fields}
    if "ready" not in response_fields:
        fail("SYSTEM_STATUS response must include 'ready' (SYS-76)")
    return _summary("SYSTEM_STATUS (SYS-76, SYS-39, SYS-58)", routes)


def check_api_010_logs(module) -> str:
    routes = _routes_for(module, "LOGS")
    _expect_path_prefix(routes)
    _expect_method(routes, "GET")
    _expect_srs_refs(routes, ("SRS-LOG-001", "SYS-38", "SYS-61"))
    request_fields = {field for route in routes for field in route.request_fields}
    if "correlation_id" not in request_fields:
        fail("LOGS query must accept correlation_id (SRS-LOG-001)")
    return _summary("LOGS (SRS-LOG-001, SYS-38, SYS-61)", routes)


def check_api_011_alerts(module) -> str:
    routes = _routes_for(module, "ALERTS")
    _expect_path_prefix(routes)
    _expect_method(routes, "GET")
    _expect_srs_refs(routes, ("SRS-NOTIF-001", "SYS-46"))
    response_fields = {field for route in routes for field in route.response_fields}
    if "delivery_status" not in response_fields:
        fail("ALERTS response must include delivery_status (SRS-NOTIF-001)")
    return _summary("ALERTS (SRS-NOTIF-001, SYS-46, SYS-58)", routes)


# --------------------------------------------------------------------------- #
# Cross-cutting checks
# --------------------------------------------------------------------------- #


def check_api_012_openapi_snapshot(module) -> str:
    """Snapshot of the OpenAPI dict must be byte-equal to the committed file."""

    if not SNAPSHOT_PATH.exists():
        fail(
            "OpenAPI snapshot is missing; "
            "run: python3 tools/rest_api_check.py --update"
        )
    actual = SNAPSHOT_PATH.read_text(encoding="utf-8")
    expected = module.render_snapshot()
    if actual != expected:
        fail(
            "OpenAPI snapshot is stale; "
            "regenerate via: python3 tools/rest_api_check.py --update"
        )
    return f"OpenAPI snapshot in sync ({SNAPSHOT_PATH.relative_to(ROOT)})"


def check_api_013_loopback_policy(module) -> str:
    """Bind host and auth model encode the SRS-SEC-002 single-user policy."""

    if module.BIND_HOST != "127.0.0.1":
        fail(f"BIND_HOST must be 127.0.0.1 (SRS-SEC-002); got {module.BIND_HOST!r}")
    if module.AUTH_MODEL != "local-single-user":
        fail(
            "AUTH_MODEL must be 'local-single-user' (SRS-SEC-002); "
            f"got {module.AUTH_MODEL!r}"
        )
    return "Loopback bind 127.0.0.1 + local-single-user auth (SRS-SEC-002)"


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


def _capability_coverage(module) -> None:
    declared = {route.capability for route in module.ROUTES}
    expected = set(module.Capability)
    missing = sorted(c.value for c in (expected - declared))
    if missing:
        fail(f"ROUTES missing capability buckets: {', '.join(missing)}")


def run_checks() -> list[str]:
    module = _load()
    _capability_coverage(module)

    evidence: list[str] = []
    for check in (
        check_api_001_lifecycle,
        check_api_002_live_designation,
        check_api_003_kill_switch,
        check_api_004_hot_swap,
        check_api_005_backtest_launch,
        check_api_006_backtest_query,
        check_api_007_reservoir_ranking,
        check_api_008_watchlist,
        check_api_009_system_status,
        check_api_010_logs,
        check_api_011_alerts,
        check_api_012_openapi_snapshot,
        check_api_013_loopback_policy,
    ):
        evidence.append(check(module))
    return evidence


def update_snapshot() -> str:
    module = _load()
    SNAPSHOT_PATH.write_text(module.render_snapshot(), encoding="utf-8")
    return f"Wrote {SNAPSHOT_PATH.relative_to(ROOT)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API-2 contract evidence")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Regenerate the frozen OpenAPI snapshot from atp_api.ROUTES.",
    )
    args = parser.parse_args(argv)

    if args.update:
        try:
            message = update_snapshot()
        except Exception as error:  # noqa: BLE001 - surfacing all import/IO errors
            print(f"API-2 UPDATE FAIL: {error}", file=sys.stderr)
            return 1
        print(message)
        return 0

    try:
        evidence = run_checks()
    except ContractCheckError as error:
        print(f"API-2 FAIL: {error}", file=sys.stderr)
        return 1

    print("API-2 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
