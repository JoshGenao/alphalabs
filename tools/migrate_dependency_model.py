#!/usr/bin/env python3
"""Migrate the board to typed dependencies. Idempotent; dry-run by default.

What it does:

1. Adds a classification to ``tools/dependency_types.json`` for every dependency
   edge from an open MVP feature that the 2026-11-07 dependency audit classified
   (see ``CLASSIFICATIONS``; sources are file:line in this repo). An edge that
   already has a classification is NEVER overwritten, so re-running is a no-op
   and an operator's later correction survives.
2. Removes NOTHING. Edges the audit found stale, reversed, or pointed at the
   wrong feature keep their place in ``tools/feature_deps.json`` and are typed
   conservatively (``verification``: they stop gating implementation but still
   gate the requirement close) with a ``review`` flag for the operator.
3. Does not touch ``feature_list.json``, evidence records, contracts, or any
   requirement text. Old ``passes: true`` stays requirement-verified; a pre-gate
   close is reported as legacy, never as fresh proof; ``serialized`` code is
   reported as implementation still to be recorded, never as complete.

Usage:
    python3 tools/migrate_dependency_model.py            # dry run: what would change
    python3 tools/migrate_dependency_model.py --apply    # write the classifications
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dependency_model as dm  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

AUDIT = "2026-11-07 dependency audit"
IMPL, IFACE, INTEG, VERIF = dm.EDGE_TYPES

# (feature, prerequisite, type, reason, source, confidence, extra)
# `extra` may hold `review` (an operator follow-up; nothing is removed here) and,
# for interface edges, `contract` {path, symbol | pattern}.
CLASSIFICATIONS: list[tuple] = [
    # --- market data / execution core ------------------------------------------------
    (
        "SRS-MD-004",
        "SRS-MD-003",
        VERIF,
        "uses HeartbeatFreshnessMonitor::combined_line_freshness, which is on main; waited only for MD-003's flag",
        "crates/atp-market-data/src/lib.rs:745",
        "high",
        {},
    ),
    (
        "SRS-EXE-001",
        "SRS-MD-004",
        IMPL,
        "the live host's ib tier refuses to start until a production MarketDataFreshnessProbe exists (MD-004's deliverable)",
        "crates/atp-orchestrator/src/bin/live_execution_host.rs:189",
        "high",
        {},
    ),
    (
        "SRS-EXE-001",
        "SRS-PERF-001",
        VERIF,
        "PTP latency measurement; SRS §3.1 measures EXE-001's p95 on the host monotonic clock",
        "docs/SRS.md:124",
        "high",
        {"review": "proposed-removal: R2 target (SyRS §1.1 rule 5)"},
    ),
    (
        "SRS-MD-001",
        "SRS-PERF-001",
        VERIF,
        "PTP latency measurement; global rule 2 measures latency on the host clock at p95",
        "docs/SRS.md:100",
        "high",
        {"review": "proposed-removal: R2 target (SyRS §1.1 rule 5)"},
    ),
    (
        "SRS-SDK-004",
        "SRS-EXE-001",
        INTEG,
        "live fill callbacks need a broker-fill path through the live host; the IB exec stream is unowned",
        "progress.d/session-SRS-SDK-004.md:85",
        "medium",
        {},
    ),
    (
        "SRS-EXE-002",
        "SRS-SDK-004",
        INTEG,
        "waits on a deployed paper-strategy order path into dispatch_order; SDK-004's paper seam is already built",
        "progress.d/session-SRS-EXE-002.md:8",
        "medium",
        {"review": "wrong-target: the real need is an unowned deployed paper-strategy runtime"},
    ),
    (
        "SRS-EXE-005",
        "SRS-EXE-001",
        INTEG,
        "state-capture loop must be wired into the live host and exercised with a real restart",
        "progress.d/session-SRS-EXE-005.md:105",
        "medium",
        {},
    ),
    (
        "SRS-EXE-008",
        "SRS-EXE-001",
        INTEG,
        "OrderLedger submit/cancel_replace must be wired into the live path",
        "crates/atp-execution/src/outbox.rs:155",
        "medium",
        {},
    ),
    (
        "SRS-EXE-008",
        "SRS-EXE-009",
        VERIF,
        "durable ledger persistence; the outbox already consumes OrderLedger::submit",
        "crates/atp-execution/src/outbox.rs:155",
        "medium",
        {},
    ),
    (
        "SRS-EXE-009",
        "SRS-EXE-001",
        INTEG,
        "outbox is wired into the host (route_order_durably); needs the ib tier and a real open-orders source",
        "progress.d/session-SRS-EXE-009.md:137",
        "medium",
        {},
    ),
    (
        "SRS-EXE-003",
        "SRS-EXE-004",
        IFACE,
        "needs only the OptionContractIdentity type",
        "progress.d/session-SRS-EXE-004.md:127",
        "high",
        {
            "contract": {
                "path": "crates/atp-types/src/composite_order.rs",
                "symbol": "OptionContractIdentity",
            }
        },
    ),
    (
        "SRS-EXE-003",
        "SRS-EXE-008",
        INTEG,
        "'state-tracked' needs the lifecycle ledger wired into submissions",
        "crates/atp-types/src/order_lifecycle.rs",
        "medium",
        {},
    ),
    (
        "SRS-EXE-004",
        "SRS-UI-002",
        INTEG,
        "a composite dashboard position; UI-002's criterion covers only position count",
        "progress.d/session-SRS-EXE-004.md:122",
        "medium",
        {"review": "wrong-target: no positions view exists and no feature owns one"},
    ),
    (
        "SRS-SAFE-001",
        "SRS-EXE-001",
        INTEG,
        "live resting-order and position producers plus a real transport",
        "progress.d/session-SRS-SAFE-001.md:157",
        "medium",
        {},
    ),
    (
        "SRS-SAFE-001",
        "SRS-EXE-002",
        INTEG,
        "real paper strategies registered on the halt gates",
        "crates/atp-simulation/src/halt.rs",
        "medium",
        {},
    ),
    (
        "SRS-SAFE-003",
        "ERR-9",
        IFACE,
        "the readiness gate on the live order path; wiring it is SAFE-003's own work",
        "progress.d/session-SRS-SAFE-003.md:155",
        "medium",
        {
            "contract": {"path": "python/atp_readiness/gate.py"},
            "review": "mostly spurious: ERR-9's criterion is a startup-config hold",
        },
    ),
    (
        "SRS-SAFE-003",
        "SRS-EXE-001",
        INTEG,
        "binding a real connectivity producer into the ib tier",
        "progress.d/session-SRS-EXE-001.md:219",
        "medium",
        {"review": "ownership overlap: EXE-001 claims the same connectivity-producer work"},
    ),
    (
        "SRS-SAFE-003",
        "SRS-MD-005",
        VERIF,
        "ScheduledRestartConnectivity is on main; waits only for MD-005's flag",
        "crates/atp-orchestrator/src/restart_window_connectivity.rs:362",
        "medium",
        {},
    ),
    (
        "SRS-SIM-004",
        "SRS-EXE-002",
        INTEG,
        "a paper container lifecycle to run the 60 s timer and the restart",
        "progress.d/session-SRS-SIM-004.md:4",
        "medium",
        {},
    ),
    (
        "SRS-DATA-019",
        "SRS-EXE-001",
        INTEG,
        "cancel resting orders and reject new ones on the live path",
        "progress.d/session-SRS-DATA-019.md:82",
        "medium",
        {},
    ),
    (
        "SRS-DATA-019",
        "SRS-SDK-004",
        IFACE,
        "the strategy callback dispatch seam",
        "python/atp_strategy/dispatch.py",
        "medium",
        {
            "contract": {"path": "python/atp_strategy/dispatch.py"},
            "review": "wrong-target: a corporate-action callback is not in SDK-004's criterion",
        },
    ),
    (
        "SRS-DATA-020",
        "SRS-EXE-001",
        VERIF,
        "not needed by the MVP criterion (re-read IB positions; never adjust)",
        "docs/SRS.md:131",
        "medium",
        {"review": "proposed-removal: predates the 2026-10-08 MVP split"},
    ),
    (
        "SRS-DATA-020",
        "SRS-SDK-004",
        VERIF,
        "the MVP criterion has no strategy callback",
        "docs/SRS.md:131",
        "medium",
        {"review": "proposed-removal: predates the 2026-10-08 MVP split"},
    ),
    (
        "SRS-DATA-021",
        "SRS-EXE-002",
        INTEG,
        "the VirtualOrderBook held by WiredPaperSimulation",
        "progress.d/session-SRS-DATA-021.md:52",
        "medium",
        {},
    ),
    (
        "SRS-RESV-001",
        "SRS-EXE-001",
        INTEG,
        "a 1 live + 5 paper system test on the reference host",
        "python/atp_dashboard/loadgen.py",
        "medium",
        {},
    ),
    (
        "SRS-RESV-001",
        "SRS-PERF-001",
        VERIF,
        "PTP latency measurement; global rule 2 uses the host clock",
        "docs/SRS.md:100",
        "high",
        {"review": "proposed-removal: R2 target (SyRS §1.1 rule 5)"},
    ),
    # --- logging / API / reservoir / error handling / operator UI --------------------
    (
        "SRS-LOG-001",
        "SRS-EXE-001",
        INTEG,
        "ORDER_ROUTING coverage: nothing writes ROUTING_OUTCOME records yet",
        "python/atp_dashboard/logs.py:131",
        "medium",
        {},
    ),
    (
        "SRS-LOG-001",
        "SRS-MD-001",
        INTEG,
        "SUBSCRIPTION_CHANGE coverage: change events exist, the LogRecord bridge is unowned",
        "python/atp_dashboard/logs.py:184",
        "medium",
        {},
    ),
    (
        "SRS-LOG-001",
        "SRS-RESV-005",
        INTEG,
        "HOT_SWAP promotion coverage: the gate emits no LogRecord",
        "python/atp_dashboard/logs.py:170",
        "medium",
        {},
    ),
    (
        "SRS-RESV-004",
        "SRS-LOG-001",
        VERIF,
        "durable log sink, which is on main",
        "python/atp_logging/persistence.py",
        "medium",
        {},
    ),
    (
        "SRS-RESV-004",
        "SRS-RESV-005",
        VERIF,
        "POST /api/v1/hot-swap is RESV-005's route; RESV-004's demotion criterion never needs it",
        "python/atp_orchestration/hot_swap_execution.py:659",
        "medium",
        {
            "review": "reversed: RESV-005 needs RESV-004's real demotion runtime, not the other way round"
        },
    ),
    (
        "SRS-RESV-005",
        "SRS-EXE-001",
        INTEG,
        "'starts live' needs the live host to accept --transport ib",
        "progress.d/session-SRS-RESV-005.md:266",
        "medium",
        {},
    ),
    (
        "SRS-API-001",
        "SRS-EXE-001",
        VERIF,
        "LIVE_DESIGNATION handler is registered",
        "python/atp_orchestration/live_designation.py:323",
        "medium",
        {},
    ),
    (
        "SRS-API-001",
        "SRS-LOG-001",
        VERIF,
        "LOGS handler is registered",
        "python/atp_logs_service/wiring.py:88",
        "medium",
        {},
    ),
    (
        "SRS-API-001",
        "SRS-SAFE-001",
        VERIF,
        "KILL_SWITCH handler is registered",
        "python/atp_safety/wiring.py:60",
        "medium",
        {},
    ),
    (
        "SRS-API-001",
        "SRS-RESV-002",
        VERIF,
        "Reservoir ranking; the MVP criterion excludes it",
        "docs/SRS.md:136",
        "high",
        {"review": "proposed-removal: R2 target (SyRS §1.1 rule 5)"},
    ),
    (
        "ERR-7",
        "SRS-LOG-001",
        VERIF,
        "ERR-7's criterion has no logging clause",
        "feature_list.json ERR-7",
        "medium",
        {"review": "proposed-removal: no matching criterion clause"},
    ),
    (
        "ERR-7",
        "SRS-RESV-004",
        VERIF,
        "the same demotion-timeout behaviour (SRS.md traces ERR-7 to RESV-004)",
        "docs/SRS.md:361",
        "high",
        {},
    ),
    (
        "ERR-8",
        "SRS-LOG-001",
        VERIF,
        "the KILL_SWITCH LIQUIDATION_TIMEOUT record is already produced",
        "python/atp_safety/audit.py:160",
        "medium",
        {},
    ),
    (
        "ERR-8",
        "SRS-SAFE-001",
        VERIF,
        "kill-switch activation runtime, on main",
        "crates/atp-orchestrator/src/kill_switch_timeout.rs",
        "medium",
        {},
    ),
    (
        "ERR-9",
        "SRS-API-001",
        VERIF,
        "GET /api/v1/system/status is runtime-owned and on main",
        "python/atp_runtime/runtime.py:120",
        "medium",
        {},
    ),
    (
        "ERR-9",
        "SRS-LOG-001",
        VERIF,
        "the sink exists; ERR-9's own readiness log producer is its own work",
        "python/atp_logging/records.py:105",
        "medium",
        {},
    ),
    (
        "SRS-SEC-001",
        "SRS-LOG-001",
        VERIF,
        "redaction at the persistence boundary, on main",
        "python/atp_logging/redaction.py",
        "medium",
        {},
    ),
    (
        "SRS-SAFE-002",
        "SRS-API-001",
        VERIF,
        "a durable lockout workflow that is not in SAFE-002's criterion",
        "progress.d/session-SRS-SAFE-002.md:124",
        "medium",
        {"review": "proposed-removal: no matching criterion clause"},
    ),
    (
        "SRS-MD-002",
        "SRS-API-001",
        VERIF,
        "operator-alert display; ALERTS belongs to NOTIF-001, which passes",
        "python/atp_runtime/contract.py",
        "medium",
        {"review": "proposed-removal: added without rationale in a531262"},
    ),
    (
        "SRS-MD-007",
        "SRS-LOG-001",
        VERIF,
        "SEQUENCE_GAP sink, on main",
        "progress.d/session-SRS-MD-007.md:79",
        "medium",
        {},
    ),
    (
        "SRS-MD-007",
        "SRS-MD-004",
        INTEG,
        "gap-stale must block orders through the production freshness probe",
        "crates/atp-execution/src/order_routing.rs",
        "medium",
        {},
    ),
    (
        "UI-3",
        "SRS-API-001",
        VERIF,
        "the backtest launch handler; contract.py names SRS-BT-001 as its owner",
        "python/atp_runtime/contract.py:45",
        "high",
        {
            "review": "wrong-target: re-pointing at SRS-BT-001 would form a cycle with BT-001 -> UI-3"
        },
    ),
    (
        "UI-4",
        "SRS-SAFE-001",
        VERIF,
        "real wire_kill_switch composed into the dashboard env; on main",
        "progress.d/session-UI-4.md:128",
        "medium",
        {},
    ),
    (
        "UI-4",
        "SRS-SAFE-002",
        IFACE,
        "needs an activation id in the LIQUIDATION_TIMEOUT record schema, which does not have one yet",
        "progress.d/session-UI-4.md:133",
        "high",
        {
            "contract": {
                "path": "python/atp_safety/audit.py",
                "pattern": r"LIQUIDATION_TIMEOUT_FIELDS[^=]*=\s*\([^)]*\"activation_id\"",
            }
        },
    ),
    (
        "UI-5",
        "SRS-API-001",
        VERIF,
        "hot-swap handlers are now bound by RESV-003 and RESV-005",
        "python/atp_orchestration/hot_swap_execution.py:659",
        "medium",
        {"review": "proposed-removal: predates RESV-005's handler"},
    ),
    (
        "UI-5",
        "SRS-RESV-004",
        VERIF,
        "demotion_pending cell; on main",
        "python/atp_hotswap",
        "medium",
        {},
    ),
    (
        "UI-5",
        "SRS-RESV-005",
        INTEG,
        "promote control and current_live cell; the manual selector is unbuilt",
        "progress.d/session-SRS-RESV-005.md:282",
        "medium",
        {},
    ),
    (
        "UI-5",
        "SRS-RESV-002",
        VERIF,
        "Reservoir ranking; the MVP Hot-Swap is a manual trigger",
        "docs/SRS.md:144",
        "high",
        {"review": "proposed-removal: R2 target (SyRS §1.1 rule 5)"},
    ),
    # --- backtesting / data / dashboards ---------------------------------------------
    (
        "SRS-BT-001",
        "SRS-API-001",
        IFACE,
        "only the handler registry, to register POST /api/v1/backtests",
        "python/atp_runtime/contract.py:45",
        "high",
        {
            "contract": {"path": "python/atp_runtime/registry.py"},
            "review": "reversed: API-001 needs BT-001's launch handler",
        },
    ),
    (
        "SRS-BT-001",
        "UI-3",
        INTEG,
        "the dashboard date pickers",
        "progress.d/session-UI-3.md:27",
        "medium",
        {"review": "reversed: UI-3's launch route returns 501 naming owner SRS-BT-001"},
    ),
    (
        "SRS-BT-004",
        "SRS-MD-001",
        INTEG,
        "a live mark feed into PaperMetricsAccumulator::mark",
        "progress.d/session-SRS-BT-004.md:80",
        "low",
        {"review": "wrong-target: BT-004's note names the paper runtime, never MD-001"},
    ),
    (
        "SRS-BT-005",
        "SRS-API-001",
        VERIF,
        "the backtest benchmark is already shown via UI-3's drill-down",
        "python/atp_dashboard/backtests.py:312",
        "medium",
        {"review": "proposed-removal: predates UI-3"},
    ),
    (
        "SRS-BT-007",
        "SRS-BT-001",
        INTEG,
        "a production SweepStrategyFactory backed by a Python strategy host; every impl today is a fixture",
        "progress.d/session-SRS-BT-007.md:80",
        "medium",
        {"review": "wrong-target: the real need is an unowned Python backtest host"},
    ),
    (
        "SRS-BT-008",
        "SRS-BT-007",
        VERIF,
        "SweepRunner is on main",
        "crates/atp-simulation/src/sweep.rs",
        "medium",
        {},
    ),
    (
        "SRS-DATA-003",
        "SRS-DATA-001",
        IFACE,
        "the BulkEquityDataProvider port",
        "crates/atp-adapters/src/lib.rs:401",
        "high",
        {
            "contract": {
                "path": "crates/atp-adapters/src/lib.rs",
                "symbol": "BulkEquityDataProvider",
            }
        },
    ),
    (
        "SRS-DATA-012",
        "SRS-DATA-006",
        IFACE,
        "strategy-facing option-chain data; the store kind exists",
        "crates/atp-data/src/store.rs:152",
        "medium",
        {"contract": {"path": "crates/atp-data/src/store.rs", "symbol": "OptionChainSnapshot"}},
    ),
    (
        "SRS-DATA-012",
        "SRS-MD-001",
        IFACE,
        "a normalization-mode field on live subscription requests",
        "progress.d/session-SRS-DATA-012.md:111",
        "medium",
        {"contract": {"path": "crates/atp-market-data/src/subscriptions.rs"}},
    ),
    (
        "SRS-SDK-007",
        "SRS-MD-001",
        INTEG,
        "a live minute feed into ctx.consolidate; the criterion names no live feed",
        "progress.d/session-SRS-SDK-007.md:5",
        "medium",
        {"review": "over-broad: the historical path already meets the criterion text"},
    ),
    (
        "SRS-UI-002",
        "SRS-BT-004",
        INTEG,
        "the per-strategy P&L cell needs a runtime metrics producer",
        "python/atp_dashboard/inventory.py:99",
        "medium",
        {},
    ),
    (
        "SRS-UI-003",
        "SRS-RESV-002",
        VERIF,
        "SYS-48 rankings; the MVP criterion excludes them",
        "docs/SRS.md:133",
        "high",
        {"review": "proposed-removal: R2 target (SyRS §1.1 rule 5)"},
    ),
    ("UI-1", "SRS-UI-002", VERIF, "roll-up of the inventory panel", "docs/SRS.md:329", "high", {}),
    (
        "UI-1",
        "SRS-UI-003",
        VERIF,
        "roll-up of the account and reservoir panels",
        "docs/SRS.md:329",
        "high",
        {},
    ),
    (
        "UI-2",
        "SRS-BT-004",
        INTEG,
        "key-metric cells need a runtime metrics producer",
        "progress.d/session-UI-2.md:84",
        "medium",
        {},
    ),
    (
        "UI-2",
        "SRS-EXE-001",
        VERIF,
        "the promote-live handler, on main since 2026-10-10",
        "python/atp_orchestration/live_designation.py:323",
        "medium",
        {},
    ),
]


def plan(deps: dict, types: dict) -> tuple[dict, list[str], list[str]]:
    """(new_edges, skipped_existing, gone). Pure.

    ``gone`` lists audited edges no longer in the graph - an operator retracted
    them since the audit. They are skipped, never re-added: the table never invents
    an edge, and a later `unblock` must not make the migration refuse to run.
    """
    edges = {(a, b) for a, prereqs in deps.items() for b in prereqs}
    new: dict = {}
    skipped, problems = [], []
    for a, b, kind, reason, source, conf, extra in CLASSIFICATIONS:
        key = dm.edge_key(a, b)
        if (a, b) not in edges:
            problems.append(key)
            continue
        if key in (types.get("edges") or {}):
            skipped.append(key)
            continue
        meta = {
            "type": kind,
            "reason": reason,
            "source": source,
            "confidence": conf,
            "classified_by": AUDIT,
            **extra,
        }
        new[key] = meta
    return new, skipped, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write tools/dependency_types.json")
    ap.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    deps_file = args.root / "tools" / "feature_deps.json"
    types_file = args.root / dm.TYPES_REL
    features = json.loads((args.root / "feature_list.json").read_text(encoding="utf-8"))
    deps = json.loads(deps_file.read_text(encoding="utf-8"))
    types = dm.load_types(types_file)
    by_id = {f["id"]: f for f in features}

    new, skipped, gone = plan(deps, types)
    for key in gone:
        print(f"• {key}: no longer in tools/feature_deps.json - skipped (not re-added)")
    counts: dict = {}
    for meta in new.values():
        counts[meta["type"]] = counts.get(meta["type"], 0) + 1
    flagged = sorted(k for k, m in new.items() if m.get("review"))
    print(
        f"classifications to add: {len(new)}  ({', '.join(f'{k}:{n}' for k, n in sorted(counts.items()))})"
    )
    print(f"already classified (left untouched): {len(skipped)}")
    print(f"flagged for operator review (NOT removed): {len(flagged)}")

    open_edges = [
        (a, b)
        for a, prereqs in deps.items()
        for b in prereqs
        if a in by_id
        and b in by_id
        and by_id[a].get("passes") is not True
        and by_id[b].get("passes") is not True
    ]
    merged = {"edges": {**(types.get("edges") or {}), **new}}
    left = [dm.edge_key(a, b) for a, b in open_edges if dm.edge_key(a, b) not in merged["edges"]]
    print(f"open edges still unclassified (gate implementation by default): {len(left)}")
    for k in left:
        print(f"    {k}")

    if not args.apply:
        print("dry run - nothing written. Re-run with --apply to write.")
        return 0
    if not new:
        print("✓ nothing to do (already migrated)")
        return 0
    out = dict(types)
    out.setdefault(
        "$comment",
        [
            "Edge types for tools/feature_deps.json. Key: 'A -> B' means A depends on B.",
            "implementation: A's code needs B's implementation complete (gates A's implementation).",
            "interface: A needs only B's contract, which must exist at contract.path (gates until it does).",
            "integration: proving A's integration obligations needs B running (gates that run only).",
            "verification: A's requirement evidence relies on B's (gates nothing before the close).",
            "Unclassified edges gate implementation. Every requirement close still needs every",
            "prerequisite of every type verified (tools/close_feature.py). `review` marks an",
            "operator follow-up; nothing here removes an edge. See tools/dependency_model.py.",
        ],
    )
    out["edges"] = merged["edges"]
    types_file.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"✓ wrote {len(new)} classification(s) to {dm.TYPES_REL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
