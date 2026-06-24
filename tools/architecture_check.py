#!/usr/bin/env python3
"""Architecture checks for SRS-ARCH-001 through SRS-ARCH-003."""

from __future__ import annotations

import importlib
import json
import sys
import tomllib
from pathlib import Path

from adapter_check import AdapterContractError, assert_adapter_contract_static
from adapter_isolation_check import AdapterIsolationError, assert_adapter_isolation_static
from backtest_check import BacktestCheckError, assert_backtest_static
from backtest_cost_check import BacktestCostCheckError, assert_backtest_cost_static
from backtest_store_check import BacktestStoreCheckError, assert_backtest_store_static
from ingestion_idempotency_check import (
    IngestionIdempotencyCheckError,
    assert_ingestion_idempotency_static,
)
from unified_query_check import (
    UnifiedQueryCheckError,
    assert_unified_query_static,
)
from concurrent_read_check import (
    ConcurrentReadCheckError,
    assert_concurrent_read_static,
)
from store_history_check import (
    StoreHistoryCheckError,
    assert_store_history_static,
)
from normalization_modes_check import (
    NormalizationModesCheckError,
    assert_normalization_modes_static,
)
from benchmark_check import BenchmarkCheckError, assert_sim_benchmark_static
from determinism_check import DeterminismCheckError, assert_determinism_static
from factor_analysis_check import FactorAnalysisCheckError, assert_factor_analysis_static
from factor_job_check import FactorJobCheckError, assert_factor_job_static
from config_check import ConfigCheckError, assert_configuration_static
from connectivity_check import ConnectivityCheckError, assert_connectivity_static
from data_provider_check import (
    DataProviderContractError,
    assert_data_provider_contract_static,
)
from dependency_boundary_check import DependencyBoundaryError, assert_dependency_direction
from deployment_check import DeploymentCheckError, assert_deployment_static
from error_handling_check import (
    ErrorHandlingCheckError,
    assert_error_handling_static,
)
from freshness_check import FreshnessCheckError, assert_freshness_static
from historical_data_check import (
    HistoricalDataCheckError,
    assert_unified_historical_data_static,
)
from hot_swap_demotion_check import (
    HotSwapDemotionCheckError,
    assert_hot_swap_demotion_static,
)
from ingestion_validation_check import (
    IngestionValidationCheckError,
    assert_ingestion_validation_static,
)
from kill_switch_timeout_check import (
    KillSwitchTimeoutCheckError,
    assert_kill_switch_timeout_static,
)
from live_designation_check import (
    LiveDesignationCheckError,
    assert_live_designation_static,
)
from metrics_check import MetricsCheckError, assert_sim_metrics_static
from orchestrator_deployment_version_check import (
    DeploymentVersionCheckError,
    assert_orchestrator_deployment_version_static,
)
from orchestrator_lifecycle_check import (
    OrchestratorLifecycleCheckError,
    assert_orchestrator_lifecycle_static,
)
from orchestrator_resource_profile_check import (
    ResourceProfileCheckError,
    assert_orchestrator_resource_profile_static,
)
from orchestrator_workload_priority_check import (
    WorkloadPriorityCheckError,
    assert_orchestrator_workload_priority_static,
)
from order_event_dispatch_check import (
    OrderEventDispatchCheckError,
    assert_order_event_dispatch_static,
)
from order_lifecycle_check import (
    OrderLifecycleCheckError,
    assert_order_lifecycle_static,
)
from order_routing_check import (
    OrderRoutingCheckError,
    assert_order_routing_static,
)
from order_type_check import (
    OrderTypeCheckError,
    assert_order_type_static,
)
from pacing_budget_check import (
    PacingBudgetCheckError,
    assert_pacing_budget_static,
)
from sim_cost_check import SimCostCheckError, assert_sim_cost_static
from sim_fill_check import SimFillCheckError, assert_sim_fill_static
from sim_ledger_check import SimLedgerCheckError, assert_sim_ledger_static
from sim_order_check import SimOrderCheckError, assert_sim_order_static
from sim_persistence_check import (
    SimPersistenceCheckError,
    assert_sim_persistence_static,
)
from strategy_api_order_events_check import (
    StrategyApiOrderEventsCheckError,
    assert_strategy_api_order_events_static,
)
from strategy_api_parity_check import (
    StrategyApiParityCheckError,
    assert_strategy_api_parity_static,
)
from strategy_api_scheduler_check import (
    StrategyApiSchedulerCheckError,
    assert_strategy_api_scheduler_static,
)
from strategy_api_subscriptions_check import (
    StrategyApiSubscriptionsCheckError,
    assert_strategy_api_subscriptions_static,
)
from strategy_api_warmup_check import (
    StrategyApiWarmupCheckError,
    assert_strategy_api_warmup_static,
)
from subscription_limit_check import (
    SubscriptionLimitCheckError,
    assert_subscription_limit_static,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
COMPOSE_PATH = ROOT / "docker-compose.yml"


class ArchitectureCheckError(AssertionError):
    pass


def read_toml(path: Path) -> dict:
    with path.open("rb") as file:
        return tomllib.load(file)


def fail(message: str) -> None:
    raise ArchitectureCheckError(message)


def assert_workspace_members(config: dict) -> list[str]:
    manifest = read_toml(ROOT / "Cargo.toml")
    members = set(manifest.get("workspace", {}).get("members", []))
    expected = [service["path"] for service in config["core_runtime_services"]]
    missing = sorted(set(expected) - members)
    if missing:
        fail(f"Cargo workspace is missing core service members: {', '.join(missing)}")
    return expected


def assert_rust_service_crates(config: dict) -> list[str]:
    evidence: list[str] = []
    for service in config["core_runtime_services"]:
        crate_path = ROOT / service["path"]
        manifest_path = crate_path / "Cargo.toml"
        source_path = crate_path / "src" / "lib.rs"
        if not manifest_path.exists():
            fail(f"{service['name']} is missing Rust manifest at {manifest_path}")
        if not source_path.exists():
            fail(f"{service['name']} is missing Rust source at {source_path}")

        manifest = read_toml(manifest_path)
        package_name = manifest.get("package", {}).get("name")
        if package_name != service["crate"]:
            fail(
                f"{service['name']} manifest names {package_name!r}, expected {service['crate']!r}"
            )

        python_files = sorted(crate_path.rglob("*.py"))
        if python_files:
            relative = ", ".join(str(path.relative_to(ROOT)) for path in python_files)
            fail(f"{service['name']} has Python files in a core Rust crate: {relative}")

        evidence.append(f"{service['crate']} -> {service['path']}/Cargo.toml")
    return evidence


def assert_strategy_api(config: dict) -> list[str]:
    strategy_api = config["strategy_api"]
    package_path = ROOT / strategy_api["path"]
    if not package_path.exists():
        fail(f"Python Strategy API package path does not exist: {package_path}")

    sys.path.insert(0, str(ROOT / "python"))
    try:
        module = importlib.import_module(strategy_api["package"])
    finally:
        sys.path.pop(0)

    missing = [name for name in strategy_api["required_exports"] if not hasattr(module, name)]
    if missing:
        fail(f"Python Strategy API is missing exports: {', '.join(missing)}")

    return [f"{strategy_api['package']} exports {', '.join(strategy_api['required_exports'])}"]


def assert_rest_api(config: dict) -> list[str]:
    rest_api = config.get("rest_api")
    if rest_api is None:
        return []

    package_path = ROOT / rest_api["path"]
    if not package_path.exists():
        fail(f"REST API package path does not exist: {package_path}")

    sys.path.insert(0, str(ROOT / "python"))
    try:
        module = importlib.import_module(rest_api["package"])
    finally:
        sys.path.pop(0)

    declared_capabilities = {capability.name for capability in module.Capability}
    missing = sorted(set(rest_api["required_capabilities"]) - declared_capabilities)
    if missing:
        fail(f"REST API package is missing capabilities: {', '.join(missing)}")

    snapshot_path = ROOT / rest_api["openapi_snapshot"]
    if not snapshot_path.exists():
        fail(f"REST API OpenAPI snapshot is missing: {snapshot_path}")
    try:
        json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"REST API OpenAPI snapshot is not valid JSON: {error}")

    if module.BIND_HOST != rest_api["bind_host"]:
        fail(f"REST API BIND_HOST is {module.BIND_HOST!r}, expected {rest_api['bind_host']!r}")
    if module.AUTH_MODEL != rest_api["auth_model"]:
        fail(f"REST API AUTH_MODEL is {module.AUTH_MODEL!r}, expected {rest_api['auth_model']!r}")

    return [
        f"{rest_api['package']} covers {len(rest_api['required_capabilities'])} "
        f"capabilities and binds {module.BIND_HOST} ({module.AUTH_MODEL})"
    ]


def assert_websocket_api(config: dict) -> list[str]:
    websocket_api = config.get("websocket_api")
    if websocket_api is None:
        return []

    package_path = ROOT / websocket_api["path"]
    if not package_path.exists():
        fail(f"WebSocket API package path does not exist: {package_path}")

    sys.path.insert(0, str(ROOT / "python"))
    try:
        module = importlib.import_module(websocket_api["package"])
    finally:
        sys.path.pop(0)

    declared_channels = {channel.name for channel in module.Channel}
    missing = sorted(set(websocket_api["required_channels"]) - declared_channels)
    if missing:
        fail(f"WebSocket API package is missing channels: {', '.join(missing)}")

    snapshot_path = ROOT / websocket_api["asyncapi_snapshot"]
    if not snapshot_path.exists():
        fail(f"WebSocket API AsyncAPI snapshot is missing: {snapshot_path}")
    try:
        json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"WebSocket API AsyncAPI snapshot is not valid JSON: {error}")

    if module.BIND_HOST != websocket_api["bind_host"]:
        fail(
            f"WebSocket API BIND_HOST is {module.BIND_HOST!r}, "
            f"expected {websocket_api['bind_host']!r}"
        )
    if module.AUTH_MODEL != websocket_api["auth_model"]:
        fail(
            f"WebSocket API AUTH_MODEL is {module.AUTH_MODEL!r}, "
            f"expected {websocket_api['auth_model']!r}"
        )
    if module.WS_PATH != websocket_api["ws_path"]:
        fail(f"WebSocket API WS_PATH is {module.WS_PATH!r}, expected {websocket_api['ws_path']!r}")
    if module.MAX_REFRESH_SECONDS != websocket_api["max_refresh_seconds"]:
        fail(
            "WebSocket API MAX_REFRESH_SECONDS is "
            f"{module.MAX_REFRESH_SECONDS}, "
            f"expected {websocket_api['max_refresh_seconds']}"
        )
    for event in module.EVENT_CHANNELS:
        if event.refresh_seconds < 0 or event.refresh_seconds > module.MAX_REFRESH_SECONDS:
            fail(
                f"WebSocket channel {event.name.value} refresh_seconds="
                f"{event.refresh_seconds} violates "
                f"[0, {module.MAX_REFRESH_SECONDS}]s NFR-P2 ceiling"
            )

    return [
        f"{websocket_api['package']} covers "
        f"{len(websocket_api['required_channels'])} channels and binds "
        f"{module.BIND_HOST} {module.WS_PATH} ({module.AUTH_MODEL})"
    ]


def assert_cli(config: dict) -> list[str]:
    cli = config.get("cli")
    if cli is None:
        return []

    package_path = ROOT / cli["path"]
    if not package_path.exists():
        fail(f"CLI package path does not exist: {package_path}")

    sys.path.insert(0, str(ROOT / "python"))
    try:
        module = importlib.import_module(cli["package"])
    finally:
        sys.path.pop(0)

    declared_groups = {group.value for group in module.Group}
    missing = sorted(set(cli["required_groups"]) - declared_groups)
    if missing:
        fail(f"CLI package is missing groups: {', '.join(missing)}")

    snapshot_path = ROOT / cli["manual_snapshot"]
    if not snapshot_path.exists():
        fail(f"CLI manual snapshot is missing: {snapshot_path}")
    try:
        json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"CLI manual snapshot is not valid JSON: {error}")

    if module.ACCESS_MODEL != cli["access_model"]:
        fail(f"CLI ACCESS_MODEL is {module.ACCESS_MODEL!r}, expected {cli['access_model']!r}")
    if module.AUTH_MODEL != cli["auth_model"]:
        fail(f"CLI AUTH_MODEL is {module.AUTH_MODEL!r}, expected {cli['auth_model']!r}")
    if module.CLI_ENTRY_POINT != cli["entry_point"]:
        fail(f"CLI entry point is {module.CLI_ENTRY_POINT!r}, expected {cli['entry_point']!r}")

    declared_irreversible = {c.invocation for c in module.COMMANDS if c.requires_confirmation}
    missing_confirm = sorted(set(cli["confirmation_required_commands"]) - declared_irreversible)
    if missing_confirm:
        fail("CLI commands missing requires_confirmation flag: " + ", ".join(missing_confirm))

    return [
        f"{cli['package']} covers {len(cli['required_groups'])} groups and "
        f"runs {module.CLI_ENTRY_POINT} via {module.ACCESS_MODEL} "
        f"({module.AUTH_MODEL})"
    ]


def assert_adapter_contract(config: dict) -> list[str]:
    contract = config.get("adapter_contract")
    if contract is None:
        return []

    static_evidence = assert_adapter_contract_static(config, ROOT)
    method_total = sum(len(v) for v in contract["required_methods"].values())
    ib = contract["interactive_brokers"]
    summary = (
        f"{contract['adapter_crate']['crate']} declares {method_total} required "
        f"trait methods across {len(contract['required_methods'])} adapter traits and "
        f"{ib['provider_struct']} documents {ib['protocol_label']} version "
        f"{ib['protocol_version']} (API-5)"
    )
    return static_evidence + [summary]


def assert_data_provider_contract(config: dict) -> list[str]:
    contract = config.get("data_provider_contract")
    if contract is None:
        return []

    static_evidence = assert_data_provider_contract_static(config, ROOT)
    method_total = sum(len(v) for v in contract["required_methods"].values())
    bindings = contract["provider_bindings"]
    provider_short = "/".join(
        binding["struct"].replace("Adapter", "").replace("Provider", "") for binding in bindings
    )
    summary = (
        f"{contract['adapter_crate']['crate']} declares {method_total} "
        f"data-provider methods across {len(contract['required_methods'])} traits "
        f"and binds {len(bindings)} providers ({provider_short}) to the "
        f"{contract['data_provider_base_trait']} base "
        f"(API-6, SRS-DATA-001..007)"
    )
    return static_evidence + [summary]


def assert_unified_historical_data(config: dict) -> list[str]:
    block = config.get("unified_historical_data")
    if block is None:
        return []

    static_evidence = assert_unified_historical_data_static(config, ROOT)
    summary = (
        f"{block['adapter_crate']['crate']} unified historical query carries "
        f"{len(block['request_fields'])} request fields and a source-neutral "
        f"{block['result_struct']} envelope across "
        f"{len(block['asset_class_variants'])} asset classes / "
        f"{len(block['normalization_variants'])} normalization modes for "
        f"{len(block['consumers'])} consumers (API-7, "
        f"SRS-DATA-007 + SRS-DATA-012)"
    )
    return static_evidence + [summary]


def assert_error_handling(config: dict) -> list[str]:
    block = config.get("error_handling_contract")
    if block is None:
        return []

    static_evidence = assert_error_handling_static(config, ROOT)
    summary = (
        f"{block['execution_crate']['crate']} rejects non-live submissions "
        f"synchronously via {block['entry_point']['type']}::"
        f"{block['entry_point']['method']} with "
        f"{len(block['error_category']['variants'])} SyRS SYS-64 categories "
        f"and {len(block['structured_error']['required_fields'])} structured "
        f"error fields, gating `{block['entry_point']['live_only_call']}` on "
        "StrategyMode::Live (ERR-1, SRS-EXE-001 + SRS-ERR-001)"
    )
    return static_evidence + [summary]


def assert_connectivity(config: dict) -> list[str]:
    block = config.get("connectivity_contract")
    if block is None:
        return []

    static_evidence = assert_connectivity_static(config, ROOT)
    summary = (
        f"{block['execution_crate']['crate']} gates live submissions on "
        f"{block['connectivity_state']['enum']} "
        f"({len(block['connectivity_state']['variants'])} states), "
        f"publishing {block['connectivity_event']['struct']} + invoking "
        f"`{block['guard']['reconnect_call']}` when IB is unreachable "
        "(ERR-2, SRS-SAFE-003 + SRS-MD-005)"
    )
    return static_evidence + [summary]


def assert_freshness(config: dict) -> list[str]:
    block = config.get("freshness_contract")
    if block is None:
        return []

    static_evidence = assert_freshness_static(config, ROOT)
    summary = (
        f"{block['execution_crate']['crate']} gates live submissions on "
        f"{block['freshness_state']['enum']} "
        f"({len(block['freshness_state']['variants'])} states), "
        f"publishing {block['stale_data_event']['struct']} when market "
        "data is stale (ERR-3, SRS-MD-004, NFR-P5 15s threshold)"
    )
    return static_evidence + [summary]


def assert_subscription_limit(config: dict) -> list[str]:
    block = config.get("subscription_limit_contract")
    if block is None:
        return []

    static_evidence = assert_subscription_limit_static(config, ROOT)
    summary = (
        f"{block['market_data_crate']['crate']} gates subscription requests "
        f"on {block['subscription_limit_state']['enum']} "
        f"({len(block['subscription_limit_state']['variants'])} states), "
        f"publishing {block['subscription_limit_event']['struct']} when "
        "the line limit is reached (ERR-4, SRS-MD-002, SyRS SYS-70 / SYS-64)"
    )
    return static_evidence + [summary]


def assert_ingestion_validation(config: dict) -> list[str]:
    block = config.get("ingestion_validation_contract")
    if block is None:
        return []

    static_evidence = assert_ingestion_validation_static(config, ROOT)
    summary = (
        f"{block['data_crate']['crate']} gates ingestion writes on "
        f"{block['record_validation_outcome']['enum']} "
        f"({len(block['record_validation_outcome']['variants'])} states / "
        f"{len(block['quarantine_reason']['variants'])} quarantine reasons), "
        f"publishing {block['ingestion_validation_event']['struct']} when "
        "a record fails structural / range / duplicate / required-field "
        "validation (ERR-5, SRS-DATA-013, SyRS SYS-77)"
    )
    return static_evidence + [summary]


def assert_pacing_budget(config: dict) -> list[str]:
    block = config.get("pacing_budget_contract")
    if block is None:
        return []

    static_evidence = assert_pacing_budget_static(config, ROOT)
    summary = (
        f"{block['data_crate']['crate']} gates ingestion jobs on "
        f"{block['pacing_budget_state']['enum']} "
        f"({len(block['pacing_budget_state']['variants'])} states), "
        f"publishing {block['pacing_budget_event']['struct']} when "
        "projected IB historical-data requests exceed the configured "
        "pacing budget for the job window (ERR-6, SRS-DATA-002, "
        "SRS-DATA-004, SyRS SYS-55)"
    )
    return static_evidence + [summary]


def assert_hot_swap_demotion(config: dict) -> list[str]:
    block = config.get("hot_swap_demotion_contract")
    if block is None:
        return []

    static_evidence = assert_hot_swap_demotion_static(config, ROOT)
    summary = (
        f"{block['orchestrator_crate']['crate']} gates Hot-Swap demotion on "
        f"{block['demotion_outcome']['enum']} "
        f"({len(block['demotion_outcome']['variants'])} outcomes): the timeout "
        f"branch cancels the unfilled order, dispatches the operator alert over "
        f"{len(block['operator_alert_channel']['variants'])} channels "
        f"({', '.join(block['operator_alert_channel']['variants'])}), records "
        f"{block['demotion_event']['struct']}, and blocks promotion (ERR-7, "
        "SRS-RESV-004, SyRS SYS-49b / SYS-49c)"
    )
    return static_evidence + [summary]


def assert_kill_switch_timeout(config: dict) -> list[str]:
    block = config.get("kill_switch_timeout_contract")
    if block is None:
        return []

    static_evidence = assert_kill_switch_timeout_static(config, ROOT)
    summary = (
        f"{block['execution_crate']['crate']} gates kill-switch liquidation on "
        f"{block['liquidation_outcome']['enum']} "
        f"({len(block['liquidation_outcome']['variants'])} outcomes): the timeout "
        f"branch pages the operator over "
        f"{len(block['operator_alert_channel']['variants'])} channels "
        f"({', '.join(block['operator_alert_channel']['variants'])}), cancels the "
        f"unfilled order, disconnects from IB, records "
        f"{block['timeout_event']['struct']}, and refuses (ERR-8, SRS-SAFE-002, "
        "SyRS SYS-44b)"
    )
    return static_evidence + [summary]


def assert_live_designation(config: dict) -> list[str]:
    block = config.get("live_designation_contract")
    if block is None:
        return []

    static_evidence = assert_live_designation_static(config, ROOT)
    summary = (
        f"{block['execution_crate']['crate']} gates live order routing on the "
        f"{block['registry']['struct']} authority via {block['entry_point']['method']}: "
        f"only the single designated strategy ({block['routing_decision']['enum']}::"
        f"{block['guard']['authorized_variant']}) reaches IB; designation requires "
        f"the {block['confirmation_token']['struct']} token (SRS-EXE-001, SyRS "
        "SYS-2a / SYS-2d / AC-15)"
    )
    return static_evidence + [summary]


def assert_order_lifecycle(config: dict) -> list[str]:
    block = config.get("order_lifecycle_contract")
    if block is None:
        return []

    static_evidence = assert_order_lifecycle_static(config, ROOT)
    edge_count = sum(len(t) for t in block["transitions"].values())
    summary = (
        f"{block['types_crate']['crate']} declares the {block['state_enum']['enum']} "
        f"lifecycle ({len(block['state_enum']['variants'])} states, {edge_count} "
        f"documented edges) with {block['correlation_id']['struct']} as the "
        f"idempotency key: duplicate submissions are rejected with the SRS-ERR-001 "
        f"{block['idempotency']['rejection_category']} category and cancel-replace is "
        "cancel-then-new (SRS-EXE-008, SyRS SYS-3 / SYS-7 / SYS-64 / SYS-90)"
    )
    return static_evidence + [summary]


def assert_order_type(config: dict) -> list[str]:
    block = config.get("order_type_contract")
    if block is None:
        return []

    static_evidence = assert_order_type_static(config, ROOT)
    summary = (
        f"{block['types_crate']['crate']} declares the source-neutral "
        f"{block['order_type_enum']} authority for SRS-EXE-003 order types: "
        f"{len(block['order_types'])} types (market/limit/stop/stop-limit), prices encoded in "
        f"the variants (contradictory sets unrepresentable) with an intake-applied positivity "
        f"rule ({block['price_error_enum']}); the single shared definition is consumed by the "
        f"paper path via {block['paper_consumer']['path']} re-export, with live-path consumption "
        f"deferred (SRS-EXE-003 stays passes:false)"
    )
    return static_evidence + [summary]


def assert_order_routing(config: dict) -> list[str]:
    block = config.get("order_routing_contract")
    if block is None:
        return []

    static_evidence = assert_order_routing_static(config, ROOT)
    summary = (
        f"{block['execution_crate']['crate']} declares the source-neutral "
        f"{block['route_enum']['enum']} dispatch authority for SRS-EXE-002: "
        f"{block['destination_decision']['method']} maps the engine-owned "
        f"{block['destination_decision']['authority_enum']} authority so the single "
        f"designated live strategy routes to the broker and EVERY non-live strategy routes "
        f"to the internal simulation engine through the {block['simulation_port']['trait']} "
        f"port — a paper order never creates an IB order (AC-10); live runtime wiring deferred "
        "(SRS-EXE-002 stays passes:false)"
    )
    return static_evidence + [summary]


def assert_order_event_dispatch(config: dict) -> list[str]:
    block = config.get("order_event_dispatch_contract")
    if block is None:
        return []

    static_evidence = assert_order_event_dispatch_static(config, ROOT)
    callback_states = sum(1 for c in block["state_to_event_category"].values() if c is not None)
    summary = (
        f"{block['types_crate']['crate']} declares the source-neutral "
        f"{block['event_category_enum']} authority for SRS-SDK-004 order-event callbacks: "
        f"for_transition derives one of {len(block['all_categories'])} categories from the "
        f"SRS-EXE-008 graph (fail-closed; {callback_states} callback-bearing states), so live "
        f"(SRS-EXE-001) and paper (SRS-SIM-001) dispatchers stay identical by construction "
        f"(SRS-SDK-001 / AC-14); NFR-P4 budgets and the AC-named set share one source of truth "
        f"with strategy_api_order_events_contract"
    )
    return static_evidence + [summary]


def assert_backtest(config: dict) -> list[str]:
    block = config.get("backtest_contract")
    if block is None:
        return []

    static_evidence = assert_backtest_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} runs the deterministic backtest engine "
        f"{block['engine']['struct']}::{block['engine']['run_method']} over the "
        f"{len(block['data_source_enum']['variants'])} launch sources "
        f"({', '.join(block['data_source_enum']['variants'])}) with a configurable "
        f"{block['date_range']['struct']}, integer minor-unit money math, and "
        f"{len(block['error_enum']['variants'])} fail-closed errors (SRS-BT-001, "
        "SyRS SYS-14 / SYS-43a)"
    )
    return static_evidence + [summary]


def assert_backtest_cost(config: dict) -> list[str]:
    block = config.get("backtest_cost_contract")
    if block is None:
        return []

    static_evidence = assert_backtest_cost_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} applies the configurable transaction-cost model "
        f"family ({block['commission_model']['enum']} / {block['slippage_model']['enum']} / "
        f"{block['spread_impact_model']['enum']}) to backtest fills with SyRS-default-matching "
        "constants, a per-run BacktestRequest.cost_config override, and integer minor-unit cost "
        "math that can never fabricate cash (SRS-BT-002, SyRS SYS-15a/b/c/d)"
    )
    return static_evidence + [summary]


def assert_sim_cost(config: dict) -> list[str]:
    block = config.get("sim_cost_contract")
    if block is None:
        return []

    static_evidence = assert_sim_cost_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} internal simulation engine "
        f"({block['engine_struct']['struct']}) applies the SAME cost::CostConfig family the "
        "backtest engine applies — defaulting to the identical SyRS baseline (SYS-15e) and computing "
        "each paper fill via the shared CostConfig::cost_breakdown entry point, so a paper strategy "
        "and a backtest with identical cost configuration compute fills and commissions from the "
        "same model family (SRS-BT-003, SyRS SYS-15e/SYS-83)"
    )
    return static_evidence + [summary]


def assert_sim_order(config: dict) -> list[str]:
    block = config.get("sim_order_contract")
    if block is None:
        return []

    static_evidence = assert_sim_order_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} internal simulation engine accepts paper orders "
        f"({block['order_request_enum']['enum']}: "
        f"{', '.join(block['order_request_enum']['variants'])}) for every "
        f"{block['order_type_enum']['enum']} and {block['asset_class_enum']['enum']}, routing each "
        f"to {block['routing']['enum']}::{block['routing']['allowed_variants'][0]} with NO "
        "brokerage variant — paper orders create no IB API order calls (SRS-SIM-001, SyRS "
        "SYS-82 / SYS-3 / SYS-4)"
    )
    return static_evidence + [summary]


def assert_sim_fill(config: dict) -> list[str]:
    block = config.get("sim_fill_contract")
    if block is None:
        return []

    static_evidence = assert_sim_fill_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} internal simulation engine "
        f"({block['engine_struct']}) simulates fills from live market data "
        f"({block['market_snapshot_struct']['struct']}: "
        f"{', '.join(block['market_snapshot_struct']['fields'])}) using configurable fill models "
        f"({block['limit_fill_enum']['enum']} default), modelling the SYS-83 market/limit/stop/"
        "stop-limit rules and enforcing the SYS-87b volume cap; a filled decision feeds the shared "
        "cost family and stays inside the internal engine — no IB API order call (SRS-SIM-002, SyRS "
        "SYS-83 / SYS-87)"
    )
    return static_evidence + [summary]


def assert_sim_ledger(config: dict) -> list[str]:
    block = config.get("virtual_ledger_contract")
    if block is None:
        return []

    static_evidence = assert_sim_ledger_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} internal simulation engine maintains an independent "
        f"virtual position ledger per paper strategy ({block['ledger_book_struct']['struct']} keyed "
        f"by {block['ledger_book_struct']['map_key']}): {block['virtual_position_struct']['struct']} "
        f"tracks {block['virtual_position_struct']['quantity_field']} + "
        f"{', '.join(block['virtual_position_struct']['money_fields'])} per symbol, with "
        f"{block['average_cost_fn']['fn']} / {block['unrealized_fn']['fn']} derived on demand, so "
        "quantity, average cost, realized/unrealized P&L, and commission paid are isolated per "
        "strategy and independent of the IB account (SRS-SIM-003, SyRS SYS-84)"
    )
    return static_evidence + [summary]


def assert_sim_persistence(config: dict) -> list[str]:
    block = config.get("sim_persistence_contract")
    if block is None:
        return []

    static_evidence = assert_sim_persistence_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} internal simulation engine persists paper state "
        f"({block['snapshot_struct']['struct']} envelope over the SRS-SIM-003 "
        f"{block['config_struct']['struct']}: interval {block['config_defaults']['interval_value']}s "
        f"/ restore deadline {block['config_defaults']['deadline_value']}s default) with a "
        "deterministic, dependency-free codec — capture/serialize/restore round-trips the virtual "
        "ledger exactly and fails closed on a corrupt snapshot, with reserved forward-compatible "
        "slots for the not-yet-built pending-order, metric, and user-state sub-states (SRS-SIM-004, "
        "SyRS SYS-89)"
    )
    return static_evidence + [summary]


def assert_sim_metrics(config: dict) -> list[str]:
    block = config.get("sim_metrics_contract")
    if block is None:
        return []

    static_evidence = assert_sim_metrics_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} internal simulation engine computes the shared "
        f"{block['metrics_struct']['struct']} family (Sharpe, Sortino, alpha, beta, max drawdown, "
        "annualized return, annualized volatility, win rate) deterministically from the backtest "
        "equity curve and trade log against an SPY-default benchmark; money enters in integer minor "
        "units and the metrics are dimensionless f64 ratios, an undefined metric is None (never a "
        "fabricated zero), and a non-finite result fails closed (SRS-BT-004, SyRS SYS-16 / SYS-86)"
    )
    return static_evidence + [summary]


def assert_sim_benchmark(config: dict) -> list[str]:
    block = config.get("sim_benchmark_contract")
    if block is None:
        return []

    static_evidence = assert_sim_benchmark_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} internal simulation engine compares strategy "
        f"performance against a selected benchmark defaulting to SPY ({block['selection']['struct']} "
        f"resolves an unselected benchmark to SPY; {block['source_trait']['trait']} is the deferred "
        f"stored-data resolution port; {block['comparison_struct']['struct']} carries the benchmark "
        "identity plus alpha/beta and total/excess return as Option<f64> ratios) -- the resolved "
        "series is re-validated fail-closed at the trust boundary before metrics::compute, money "
        "enters in integer minor units and the comparison emits dimensionless f64 ratios (SRS-BT-005, "
        "SyRS SYS-17 / SYS-36 / SYS-37)"
    )
    return static_evidence + [summary]


def assert_sim_backtest_store(config: dict) -> list[str]:
    block = config.get("sim_backtest_store_contract")
    if block is None:
        return []

    static_evidence = assert_backtest_store_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} internal simulation engine persists completed "
        f"backtest results ({block['record_struct']['struct']} bundles the seven SRS-BT-009 "
        "artifacts -- parameters, metrics, trade log, equity curve, benchmark comparison, code "
        f"version, and timestamp -- into one queryable record; {block['store_struct']['struct']} "
        "answers the by-strategy / by-date-range / by-parameter-set query axes in a deterministic "
        "canonical order and round-trips the whole store through a checksummed, dependency-free "
        "codec that fails closed on a corrupt / tampered / non-finite blob) -- trade-log/equity "
        "money stays integer minor units and the metric/comparison ratios round-trip exactly via "
        "to_bits/from_bits (SRS-BT-009, SyRS SYS-21 / SYS-79)"
    )
    return static_evidence + [summary]


def assert_ingestion_idempotency(config: dict) -> list[str]:
    block = config.get("ingestion_idempotency_contract")
    if block is None:
        return []

    static_evidence = assert_ingestion_idempotency_static(config, ROOT)
    summary = (
        f"{block['data_crate']['crate']} data layer makes ingestion idempotent "
        f"({block['record_struct']['struct']} keyed by a vendor-neutral {block['dataset_kind']['enum']} "
        "natural key; MarketDataStore::upsert inserts a fresh key, no-ops an identical re-ingest "
        "(UnchangedDuplicate -- no duplicate row), and fails closed on a conflicting re-ingest "
        "(ConflictingContent -- existing data intact); ingest_market_record composes the unchanged "
        "ERR-5 validation gate then the idempotent upsert; a deterministic checksummed codec + a "
        "crash-durable atomic file write keep the persisted store byte-identical on re-ingest) -- "
        "the storage substrate the SRS-DATA family composes (SRS-DATA-016, SyRS NFR-R4)"
    )
    return static_evidence + [summary]


def assert_unified_query(config: dict) -> list[str]:
    block = config.get("unified_query_runtime_contract")
    if block is None:
        return []

    static_evidence = assert_unified_query_static(config, ROOT)
    summary = (
        f"{block['data_crate']['crate']} data layer provides the unified historical data access "
        f"interface ({block['query_struct']['struct']} carries only symbol + resolution + an inclusive "
        "event_ts range + an optional vendor-neutral DatasetKind, never a provider; "
        "MarketDataStore::query_unified filters the store's canonical order on those vendor-neutral "
        "NaturalKey dimensions and returns the source-neutral UnifiedHistoricalResult in deterministic "
        "event_ts-ascending order; one path serves every provider kind, and the data007_query_cli "
        "operator surface queries by symbol/date-range/resolution with no provider line) -- the runnable "
        "read path over the SRS-DATA-016 substrate (SRS-DATA-007, SyRS SYS-27 / SYS-53)"
    )
    return static_evidence + [summary]


def assert_concurrent_read(config: dict) -> list[str]:
    block = config.get("concurrent_read_runtime_contract")
    if block is None:
        return []

    static_evidence = assert_concurrent_read_static(config, ROOT)
    summary = (
        f"{block['data_crate']['crate']} storage SUBSTRATE supports concurrent reads during ingestion "
        "writes (snapshot isolation over the SRS-DATA-016 store: the data007_query_cli query + "
        "data016_ingest_cli inspect OPERATOR readers take no lock and load the atomically-published "
        "snapshot; data016_ingest_cli ingest holds the single-writer StoreLock across the whole "
        "load-modify-save; save_to_path publishes via scratch->fsync->fs::rename->dir-fsync so a reader "
        "never sees a torn store; load_from_path fails closed via the checksum-first restore -- "
        "demonstrated by the srs_data_017_concurrent_reads Load test driving lock-free reader "
        "threads/processes against a lock-held writer, which exercise the SAME load_from_path path a "
        "named in-process consumer would use). SRS-DATA-017 STAYS passes:false: the FIRST in-process "
        "Python consumer binding now EXISTS (StoreBackedHistoricalData; store_history_binding_contract) "
        "and reads via this exact lock-free path, but the concurrent-read-DURING-write Load test for "
        "THAT named Python consumer -- a Python-consumer-vs-held-writer load test, not the Rust-CLI "
        "analog -- is not yet in place, so flipping would over-claim the AC's named-consumer concurrency "
        "(the dashboard / REST consumer surfaces remain SRS-UI / SRS-API); that Load test is the "
        "remaining load-bearing close (SRS-DATA-017, SyRS SYS-63)"
    )
    return static_evidence + [summary]


def assert_store_history(config: dict) -> list[str]:
    block = config.get("store_history_binding_contract")
    if block is None:
        return []

    static_evidence = assert_store_history_static(config, ROOT)
    summary = (
        f"{block['module']['class']} ({block['module']['path']}) is the in-process consumer binding "
        f"over the unified historical query engine -- a concrete {block['protocol']} implementation "
        "that drives the lock-free, source-neutral data007_query_cli so a real named consumer "
        "(strategy code / backtests / factor jobs / notebooks) reads ingested data by "
        "symbol/date-range/resolution with NO provider named (no provider/vendor/source/feed parameter, "
        "no origin field read) via the explicit RAW path, keeping the Protocol's SPLIT_ADJUSTED default "
        "and FAILING CLOSED on it (and on every adjusted mode), scaling OHLC by the named "
        "_PRICE_MINOR_SCALE while leaving volume a raw count, and invoking the CLI with a list argv "
        "(shell=False) under a bounded timeout so a wedged read fails closed rather than hanging. "
        "SRS-DATA-007 STAYS passes:false (foundational): the binding serves RAW only because "
        "split-adjusted is not a trustworthy strategy-facing default until corporate-action COVERAGE "
        "exists (SRS-DATA-011; absent it a split-adjusted read would be raw-as-adjusted), and the named "
        "backtest / factor / notebook consumers are not yet wired to this store path (deferred to "
        "SRS-DATA-007). The split-adjustment math (Rust core LIBRARY; no public surface exposes it) is pinned by "
        "normalization_modes_check (SRS-DATA-012); the concurrent-read-DURING-write Load test is the "
        "deferred SRS-DATA-017 close (SRS-DATA-007, SyRS SYS-27 / SYS-53)"
    )
    return static_evidence + [summary]


def assert_normalization_modes(config: dict) -> list[str]:
    block = config.get("normalization_modes_contract")
    if block is None:
        return []

    static_evidence = assert_normalization_modes_static(config, ROOT)
    summary = (
        "SRS-DATA-012 split-adjusted historical normalization: split corporate actions persist as a "
        f"vendor-neutral DatasetKind ({block['split_kind_label']}) in the same idempotent/durable "
        f"MarketDataStore as bars, and the Rust core ({block['normalization_module']}) re-quotes bars "
        "onto a split-comparable basis -- compose-then-divide (one division per field), i128 "
        "intermediates with fail-closed try_from narrowing, round-half-to-even, and the strict "
        "effective_ts > event_ts boundary (OHLC scaled by DEN/NUM, volume by the inverse). "
        "The split-adjustment math is exposed on NO public surface: data007_query_cli serves "
        "--normalization raw ONLY (split-adjusted FAILS closed naming SRS-DATA-011 coverage), and the "
        "StoreBackedHistoricalData CONSUMER binding serves RAW only and FAILS CLOSED on split-adjusted. "
        "It is FOUNDATIONAL substrate (the math, proven at the Rust library level), NOT a usable mode, "
        "until corporate-action COVERAGE exists (SRS-DATA-011; absent it a split-adjusted label would be "
        "raw-as-adjusted). SRS-DATA-012 STAYS passes:false: this is the HISTORICAL split-adjustment math "
        "-- the LIVE subscription path and the FULLY_ADJUSTED / TOTAL_RETURN (dividend) modes are also "
        "deferred (SyRS SYS-29 / StRS SN-1.15)"
    )
    return static_evidence + [summary]


def assert_factor_analysis(config: dict) -> list[str]:
    block = config.get("factor_analysis_contract")
    if block is None:
        return []

    static_evidence = assert_factor_analysis_static(config, ROOT)
    summary = (
        f"{block['factor_pipeline_crate']['crate']} factor pipeline computes factor analysis & "
        f"tear-sheet outputs ({block['compute_fn']['fn']} turns a {block['panel']['struct']} of "
        "per-period (security, factor, forward-return) observations into one "
        f"{block['tear_sheet']['struct']} bundling the three SRS-BT-006 deliverables -- the "
        f"{block['information_coefficient']['struct']} per-period Spearman rank IC with mean/std/"
        f"risk-adjusted, the {block['factor_returns']['struct']} quantile mean returns plus the "
        f"top-minus-bottom long-short spread, and the {block['turnover']['struct']} quantile "
        "membership churn) -- factor scores and returns are dimensionless f64 (not a money leak), "
        "the work is deterministic (fixed folds, total-order ties), an undefined statistic is None "
        "(never a fabricated zero), and FactorPanel::validate fails closed at the trust boundary "
        "(SRS-BT-006, SyRS SYS-18)"
    )
    return static_evidence + [summary]


def assert_factor_job(config: dict) -> list[str]:
    block = config.get("factor_job_contract")
    if block is None:
        return []

    static_evidence = assert_factor_job_static(config, ROOT)
    summary = (
        f"{block['factor_pipeline_crate']['crate']} scheduled factor job produces the SRS-BT-006 "
        f"panel ({block['run_fn']['fn']} resolves its schedule through the "
        f"{block['trading_calendar_port']['trait']} port (SyRS SYS-51), enforces the "
        f"{block['full_universe_floor']['const']} = {block['full_universe_floor']['value']} "
        "full-universe floor, computes a user-defined FactorModel over both market and "
        "fundamental inputs -- a security missing either is an auditable SkippedSecurity, never "
        "fabricated -- ranks by the total order, and gates on the calendar-resolved deadline "
        f"INSTANT read from the injected {block['clock']['trait']}, failing closed with "
        f"{block['outcome_enum']['enum']}::DeadlineExceeded on a late start or late finalization "
        "(and with NoUsableCoverage when too few securities are scored); "
        f"{block['assemble_fn']['fn']} builds a REGULAR FactorPanel -- a constant "
        "calendar-resolved rebalance interval + a non-overlapping forward horizon -- for the "
        "tear-sheet's interval/horizon-dependent means; the work is deterministic and free of any "
        "broker/vendor dependency (SRS-FAC-001, SyRS SYS-32/33/51, NFR-P7)"
    )
    return static_evidence + [summary]


def assert_backtest_determinism(config: dict) -> list[str]:
    block = config.get("backtest_determinism_contract")
    if block is None:
        return []

    static_evidence = assert_determinism_static(config, ROOT)
    summary = (
        f"{block['simulation_crate']['crate']} determinism module makes the SRS-BT-010 "
        f"guarantee falsifiable ({block['digest_fns']['result_fn']} / "
        f"{block['digest_fns']['run_fn']} fold a BacktestResult -- trade log + equity curve as "
        "exact i64 minor units, metric ratios via f64::to_bits -- into a stable "
        f"{block['run_digest']['struct']}; {block['harness']['fn']} and "
        f"{block['harness']['metrics_fn']} run the engine twice over identical inputs and fail "
        f"closed with a localized {block['error_enum']['enum']} if the trade log, equity curve, "
        "provenance, or metrics disagree -- the metric family computed by the crate's own "
        "deterministic metrics::compute over immutable inputs, no caller reduction), and the "
        "verifier is itself deterministic (no parallelism / RNG / clock); the end-to-end "
        "guarantee under the real Python strategy host + the operator repeated-run workflow are "
        "deferred (SRS-BT-010, SyRS SYS-62)"
    )
    return static_evidence + [summary]


def assert_orchestrator_lifecycle(config: dict) -> list[str]:
    block = config.get("orchestrator_lifecycle_contract")
    if block is None:
        return []

    static_evidence = assert_orchestrator_lifecycle_static(config, ROOT)
    summary = (
        f"{block['orchestrator_crate']['crate']} gates strategy container "
        f"lifecycle on {block['lifecycle_action']['enum']} "
        f"({len(block['lifecycle_action']['variants'])} actions) + "
        f"{block['container_health_state']['enum']} "
        f"({len(block['container_health_state']['variants'])} states), "
        f"publishing {block['container_health_event']['struct']} when "
        "a launch breaches NFR-P9 or a container becomes unresponsive "
        "(SRS-ORCH-001, SyRS SYS-10 / SYS-13 / AC-12 / NFR-P9)"
    )
    return static_evidence + [summary]


def assert_orchestrator_resource_profile(config: dict) -> list[str]:
    block = config.get("resource_profile_contract")
    if block is None:
        return []

    static_evidence = assert_orchestrator_resource_profile_static(config, ROOT)
    summary = (
        f"{block['orchestrator_crate']['crate']} enforces "
        f"{block['resource_profile']['struct']} at the launch boundary — "
        f"defaults match SyRS SYS-11 (live: "
        f"{block['spec_constants']['live_mem_mb']['value']} MB / "
        f"{block['spec_constants']['live_cpu_hundredths']['value']} hundredths CPU; "
        f"paper: {block['spec_constants']['paper_mem_mb']['value']} MB / "
        f"{block['spec_constants']['paper_cpu_hundredths']['value']} hundredths CPU); "
        "configuration overrides validated against SRS-ARCH-005 catalogue bounds; "
        "misconfigured launches refused with category "
        f"{block['rejection_category']} ({block['rejection_wire_string']}) "
        "before the runtime port is invoked (SRS-ORCH-002, SyRS SYS-11 / SYS-57)"
    )
    return static_evidence + [summary]


def assert_orchestrator_workload_priority(config: dict) -> list[str]:
    block = config.get("workload_priority_contract")
    if block is None:
        return []

    static_evidence = assert_orchestrator_workload_priority_static(config, ROOT)
    summary = (
        f"{block['orchestrator_crate']['crate']} enforces the SyRS SYS-57 "
        "workload-priority hierarchy at admission — "
        f"new workloads refused when admitting would drop available host "
        f"memory below the {block['spec_constants']['safety_margin_default_mb']['value']} MB "
        "default safety margin; lowest-priority active batch workload "
        "evicted to make room for a higher-priority arriving workload; "
        "live strategy never selected for eviction; refusals carry "
        f"category {block['rejection_category']} "
        f"({block['rejection_wire_string']}) (SRS-ORCH-003, SyRS SYS-57 / SYS-58)"
    )
    return static_evidence + [summary]


def assert_orchestrator_deployment_version(config: dict) -> list[str]:
    block = config.get("deployment_version_contract")
    if block is None:
        return []

    static_evidence = assert_orchestrator_deployment_version_static(config, ROOT)
    summary = (
        f"{block['orchestrator_crate']['crate']} records the deployed code "
        "version (source hash + deployment timestamp) for each strategy at "
        "deployment time and exposes it through the "
        f"{block['deployed_version_registry_port']['trait']} port; "
        f"misformed source hashes refused with category "
        f"{block['rejection_category']} "
        f"({block['rejection_wire_string']}); DeadlineExceeded launches "
        "skip the version record (SRS-ORCH-004, SyRS SYS-79 / SYS-41 / "
        "SYS-21 / IF-9)"
    )
    return static_evidence + [summary]


def assert_strategy_api_parity(config: dict) -> list[str]:
    block = config.get("strategy_api_parity_contract")
    if block is None:
        return []

    static_evidence = assert_strategy_api_parity_static(config, ROOT)
    summary = (
        f"python/{block['sdk_package']}/ enforces SRS-SDK-001 parity: "
        "no execution-mode discriminator leakage, no vendor-SDK imports, "
        f"all {len(block['required_context_methods'])} required "
        "StrategyContext methods + "
        f"{len(block['required_context_attrs'])} required attributes "
        "present, no mode parameters on any method, no mode fields on "
        "StrategyConfig (SRS-SDK-001, SyRS AC-14 / SYS-82..SYS-87)"
    )
    return static_evidence + [summary]


def assert_strategy_api_scheduler(config: dict) -> list[str]:
    block = config.get("strategy_api_scheduler_contract")
    if block is None:
        return []

    static_evidence = assert_strategy_api_scheduler_static(config, ROOT)
    summary = (
        f"python/atp_strategy/ enforces SRS-SDK-002 scheduling: "
        f"Scheduler Protocol with {len(block['required_scheduler_methods'])} "
        f"required methods, TradingCalendar Protocol with "
        f"{len(block['required_calendar_methods'])} required methods, "
        f"concrete {block['calendar_class']} resolving "
        f"{len(block['required_exchange_handles'])} exchange handles "
        "(NYSE / NASDAQ / CBOE), tz-aware US-Eastern session times "
        "(SRS-SDK-002, SyRS SYS-6 / SYS-50 / SYS-51)"
    )
    return static_evidence + [summary]


def assert_strategy_api_subscriptions(config: dict) -> list[str]:
    block = config.get("strategy_api_subscriptions_contract")
    if block is None:
        return []

    static_evidence = assert_strategy_api_subscriptions_static(config, ROOT)
    summary = (
        f"python/atp_strategy/ enforces SRS-SDK-003 single tradable asset "
        f"class invariant: AssetClass{{{', '.join(block['required_asset_class_members'])}}} "
        f"enum, StrategyConfig.tradable_asset_class required (no default), "
        f"OrderRequest.asset_class default = "
        f"AssetClass.{block['required_request_default_asset_class']}, "
        f"StrategyContext.subscribe(asset_class=...) for both-class "
        f"analysis subscriptions, shipped {', '.join(block['required_helper_functions'])} "
        f"guard raises AssetClassViolation on mismatched orders "
        "(SRS-SDK-003, SyRS SYS-5 / SYS-64)"
    )
    return static_evidence + [summary]


def assert_strategy_api_warmup(config: dict) -> list[str]:
    block = config.get("strategy_api_warmup_contract")
    if block is None:
        return []

    static_evidence = assert_strategy_api_warmup_static(config, ROOT)
    summary = (
        f"python/atp_strategy/ enforces SRS-SDK-005 warm-up mechanism: "
        f"WarmupState{{{', '.join(block['required_state_machine_members'])}}} "
        f"lifecycle, WarmupController shipped with run() + "
        f"{len(block['required_controller_properties'])} introspection "
        f"properties, shipped {', '.join(block['required_helper_functions'])} "
        f"guard gates the executable boundary, behavioural check exercises "
        f"the AC at the canonical "
        f"{block['required_warmup_bars_canonical']}-bar warm-up — the "
        "architecture metadata block is the cross-language source of "
        "truth (Rust core dispatchers re-implement the gate locally per "
        "AGENTS.md dependency direction)"
    )
    return static_evidence + [summary]


def assert_strategy_api_order_events(config: dict) -> list[str]:
    block = config.get("strategy_api_order_events_contract")
    if block is None:
        return []

    static_evidence = assert_strategy_api_order_events_static(config, ROOT)
    summary = (
        f"python/atp_strategy/ enforces the SDK-surface half of "
        f"SRS-SDK-004 order event callback contract: OrderEventType "
        f"covers {block['required_event_type_members']}, OrderEvent "
        f"dataclass carries fill_price / fill_quantity / commission + "
        f"order identifiers, shipped "
        f"{', '.join(block['required_helper_functions'])} guard raises "
        f"OrderEventContractError on FILL/PARTIAL_FILL/CANCELLED/"
        f"REJECTED missing fill_price/fill_quantity/commission, "
        f"LIVE_CALLBACK_LATENCY_P95_MS = "
        f"{block['required_live_callback_latency_p95_ms']} ms / "
        f"PAPER_CALLBACK_LATENCY_P95_MS = "
        f"{block['required_paper_callback_latency_p95_ms']} ms — the "
        "architecture metadata block is the cross-language source of "
        "truth (Rust core dispatchers read directly per AGENTS.md "
        "dependency direction; Python SDK constants are the Python-"
        "side view kept in parity) — NFR-P4 latency proof gated on "
        "SRS-EXE-001 + SRS-SIM-001; SRS-SDK-004 stays passes:false "
        "until those ship"
    )
    return static_evidence + [summary]


def assert_container_language_boundary(config: dict) -> list[str]:
    if not COMPOSE_PATH.exists():
        fail("docker-compose.yml is missing")
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    missing_crates = [
        service["crate"]
        for service in config["core_runtime_services"]
        if service["crate"] not in compose_text
        and service["crate"] not in {"atp-types", "atp-adapters"}
    ]
    if missing_crates:
        fail(f"Container config does not reference core Rust crates: {', '.join(missing_crates)}")

    if "docker/core-runtime.Dockerfile" not in compose_text:
        fail("Container config does not reference the Rust runtime Dockerfile")
    if "docker/strategy-python.Dockerfile" not in compose_text:
        fail("Container config does not reference the Python strategy Dockerfile")

    return ["docker-compose.yml maps core services to docker/core-runtime.Dockerfile"]


def run_checks() -> list[str]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    evidence: list[str] = []
    evidence.extend(assert_workspace_members(config))
    evidence.extend(assert_rust_service_crates(config))
    try:
        evidence.extend(assert_dependency_direction(config, ROOT))
    except DependencyBoundaryError as error:
        fail(str(error))
    try:
        evidence.extend(assert_adapter_isolation_static(config, ROOT))
    except AdapterIsolationError as error:
        fail(str(error))
    evidence.extend(assert_strategy_api(config))
    evidence.extend(assert_rest_api(config))
    evidence.extend(assert_websocket_api(config))
    evidence.extend(assert_cli(config))
    try:
        evidence.extend(assert_adapter_contract(config))
    except AdapterContractError as error:
        fail(str(error))
    try:
        evidence.extend(assert_data_provider_contract(config))
    except DataProviderContractError as error:
        fail(str(error))
    try:
        evidence.extend(assert_unified_historical_data(config))
    except HistoricalDataCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_error_handling(config))
    except ErrorHandlingCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_connectivity(config))
    except ConnectivityCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_freshness(config))
    except FreshnessCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_subscription_limit(config))
    except SubscriptionLimitCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_ingestion_validation(config))
    except IngestionValidationCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_pacing_budget(config))
    except PacingBudgetCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_hot_swap_demotion(config))
    except HotSwapDemotionCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_kill_switch_timeout(config))
    except KillSwitchTimeoutCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_live_designation(config))
    except LiveDesignationCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_order_lifecycle(config))
    except OrderLifecycleCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_order_event_dispatch(config))
    except OrderEventDispatchCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_order_type(config))
    except OrderTypeCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_order_routing(config))
    except OrderRoutingCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_backtest(config))
    except BacktestCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_backtest_cost(config))
    except BacktestCostCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_sim_cost(config))
    except SimCostCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_sim_order(config))
    except SimOrderCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_sim_fill(config))
    except SimFillCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_sim_ledger(config))
    except SimLedgerCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_sim_persistence(config))
    except SimPersistenceCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_sim_metrics(config))
    except MetricsCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_sim_benchmark(config))
    except BenchmarkCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_sim_backtest_store(config))
    except BacktestStoreCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_ingestion_idempotency(config))
    except IngestionIdempotencyCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_unified_query(config))
    except UnifiedQueryCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_concurrent_read(config))
    except ConcurrentReadCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_store_history(config))
    except StoreHistoryCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_normalization_modes(config))
    except NormalizationModesCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_factor_analysis(config))
    except FactorAnalysisCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_factor_job(config))
    except FactorJobCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_backtest_determinism(config))
    except DeterminismCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_orchestrator_lifecycle(config))
    except OrchestratorLifecycleCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_orchestrator_resource_profile(config))
    except ResourceProfileCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_orchestrator_workload_priority(config))
    except WorkloadPriorityCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_orchestrator_deployment_version(config))
    except DeploymentVersionCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_strategy_api_parity(config))
    except StrategyApiParityCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_strategy_api_scheduler(config))
    except StrategyApiSchedulerCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_strategy_api_subscriptions(config))
    except StrategyApiSubscriptionsCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_strategy_api_order_events(config))
    except StrategyApiOrderEventsCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_strategy_api_warmup(config))
    except StrategyApiWarmupCheckError as error:
        fail(str(error))
    evidence.extend(assert_container_language_boundary(config))
    try:
        evidence.extend(assert_deployment_static(config, ROOT))
    except DeploymentCheckError as error:
        fail(str(error))
    try:
        evidence.extend(assert_configuration_static(config, ROOT))
    except ConfigCheckError as error:
        fail(str(error))
    return evidence


def main() -> int:
    try:
        evidence = run_checks()
    except ArchitectureCheckError as error:
        print(f"SRS-ARCH-001 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-ARCH-001 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
