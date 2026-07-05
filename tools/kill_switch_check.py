#!/usr/bin/env python3
"""SRS-SAFE-001 kill-switch ACTIVATION runtime contract evidence.

Structural companion to ``tools/sim_halt_check.py`` (which pins the per-engine
HALTED gate): this tool pins the activation slice built on top of it —
the ``atp-execution`` activation gate, the ``atp-simulation`` fleet fan-out,
and the ``python/atp_safety`` operator surfaces — against
``architecture/runtime_services.json#kill_switch_activation_contract``.

The load-bearing safety pins:

* the gate's signature RETURNS the report (never ``Result``) and its body has
  no early ``return`` — the structural form of continue-to-safety: every
  phase outcome is recorded and the report comes back on every path;
* phase order: halt fan-out BEFORE any cancel BEFORE any liquidation BEFORE
  disconnect (the 1 s HALTED-observability budget cannot sit behind up to
  5 s of lawful brokerage I/O; disconnect-after-liquidation is the AC's one
  explicit ordering);
* the NFR-P3 / observability budget constants are 5 000 / 1 000 ms;
* the fleet halt visits EVERY registered engine and hands out no engine
  reference an unhalted copy could survive through;
* ``wire_kill_switch`` takes its backend EXPLICITLY (no default, no fixture
  fallback constructed in the wiring) — uncovered capability → no public
  surface;
* the subprocess backend and the handlers fail CLOSED (backend-unavailable /
  audit-write-failed / state-corrupt are structured errors, never
  success-shaped; the replay guard is consulted BEFORE the backend fires);
* kill-switch ownership is re-pointed to SRS-SAFE-001 on both surfaces;
* scope honesty: SRS-SAFE-001 stays ``passes:false`` (serialized) and the
  contract's ``deferred[]`` names the live-path owners.

Invoke:
    python3 tools/kill_switch_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _rust_parser import _fn_block

ROOT = Path(__file__).resolve().parents[1]


class KillSwitchCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise KillSwitchCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "kill_switch_activation_contract" not in config:
        fail("architecture metadata is missing kill_switch_activation_contract")
    return config["kill_switch_activation_contract"]


def _read(root: Path, relative: str) -> str:
    path = root / relative
    if not path.exists():
        fail(f"source missing: {relative}")
    return path.read_text(encoding="utf-8")


def gate_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    return _read(
        root,
        f"{block['execution_crate']['path']}/src/{block['gate_module']}.rs",
    )


def types_source(config: dict, root: Path = ROOT) -> str:
    return _read(root, "crates/atp-types/src/lib.rs")


def fleet_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)["fleet"]
    return _read(root, f"crates/{block['crate']}/src/{block['module']}.rs")


def wiring_source(config: dict, root: Path = ROOT) -> str:
    return _read(root, "python/atp_safety/wiring.py")


def backend_source(config: dict, root: Path = ROOT) -> str:
    return _read(root, "python/atp_safety/backend.py")


def handlers_source(config: dict, root: Path = ROOT) -> str:
    return _read(root, "python/atp_safety/handlers.py")


def owners_source(config: dict, root: Path = ROOT) -> str:
    return _read(root, "python/atp_runtime/contract.py")


def features_source(config: dict, root: Path = ROOT) -> str:
    return _read(root, "feature_list.json")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_entry_point_returns_report_not_result(config: dict, gate_src: str) -> str:
    block = contract_block(config)["entry_point"]
    method = block["method"]
    compact = _compact(gate_src)
    if f"pubfn{method}<" not in compact:
        fail(f"atp-execution kill_switch must define `pub fn {method}<...>`")
    if f")->{block['returns']}where" not in compact:
        fail(
            f"{method} must return `{block['returns']}` directly — a Result return "
            "would let a failed phase abort the sequence instead of recording the "
            "outcome and continuing to safety"
        )
    if f"->Result<{block['returns']}" in compact:
        fail(f"{method} must not wrap the report in a Result")
    fn_body = _fn_block(gate_src, method)
    if re.search(r"\breturn\b", fn_body):
        fail(
            f"{method} must have NO early `return` — every phase is attempted and "
            "the report is the final expression on the single path"
        )
    if "?" in fn_body.replace("'?'", "").replace('"?"', ""):
        fail(f"{method} must not propagate errors with `?` — outcomes are recorded, not thrown")
    return (
        f"{block['on']}::{method} returns {block['returns']} directly (no Result, no early "
        "return, no `?`) — continue-to-safety is structural: every phase outcome is recorded "
        "and the report comes back on every path"
    )


def check_phase_order(config: dict, gate_src: str) -> str:
    block = contract_block(config)
    tokens = block["phase_order_tokens"]
    fn_body = _compact(_fn_block(gate_src, block["entry_point"]["method"]))
    positions = []
    for token in tokens:
        index = fn_body.find(_compact(token))
        if index < 0:
            fail(f"activation gate is missing phase call `{token}`")
        positions.append((token, index))
    for (earlier, earlier_index), (later, later_index) in zip(
        positions, positions[1:], strict=False
    ):
        if earlier_index >= later_index:
            fail(
                f"activation phase order violated: `{earlier}` must run before `{later}` "
                "(halt first protects the 1s HALTED-observability budget; disconnect is "
                "always last per the AC)"
            )
    return (
        "activation phase order pinned: " + " -> ".join(tokens) + " (halt first; disconnect last)"
    )


def check_budget_constants(config: dict, types_src: str) -> str:
    budgets = contract_block(config)["budgets"]
    compact = _compact(types_src)
    if _compact(f"pub const {budgets['activation_budget_const']}: u64 = 5_000") not in compact:
        fail(f"atp-types must pin {budgets['activation_budget_const']} = 5_000 (NFR-P3)")
    if _compact(f"pub const {budgets['halt_observability_const']}: u64 = 1_000") not in compact:
        fail(
            f"atp-types must pin {budgets['halt_observability_const']} = 1_000 "
            "(SRS-LOG-001 observability)"
        )
    if _compact("liquidations_submitted_ms<=KILL_SWITCH_ACTIVATION_BUDGET_MS") not in compact:
        fail(
            "within_nfr_p3 must judge the liquidations_submitted_ms mark against "
            "KILL_SWITCH_ACTIVATION_BUDGET_MS"
        )
    return (
        f"budgets pinned: {budgets['activation_budget_const']}=5000ms (NFR-P3, judged at "
        f"{budgets['nfr_p3_measurement_field']}), {budgets['halt_observability_const']}=1000ms"
    )


def check_fleet_halts_every_engine(config: dict, fleet_src: str) -> str:
    fleet = contract_block(config)["fleet"]
    fn_body = _fn_block(fleet_src, fleet["halt_all_fn"])
    compact_body = _compact(fn_body)
    if _compact("for (engine_id, engine) in &mut self.engines") not in compact_body:
        fail(f"{fleet['halt_all_fn']} must iterate EVERY registered engine")
    for counter in ("transitioned+=1", "already_halted+=1"):
        if counter not in compact_body:
            fail(
                f"{fleet['halt_all_fn']} must count `{counter}` so "
                f"`{fleet['count_invariant']}` holds"
            )
    compact_src = _compact(fleet_src)
    for leak in (
        "pubengines:",
        "->&HaltablePaperEngine",
        "->&mutHaltablePaperEngine",
        "->Option<&HaltablePaperEngine>",
        "->Option<&mutHaltablePaperEngine>",
    ):
        if leak in compact_src:
            fail(
                f"the fleet leaks an engine reference (`{leak}`) — a caller-held engine "
                "could escape halt_all"
            )
    return (
        f"{fleet['struct']}::{fleet['halt_all_fn']} visits every registered engine "
        f"({fleet['count_invariant']}) and leaks no engine reference"
    )


def check_wiring_requires_explicit_backend(config: dict, wiring_src: str) -> str:
    compact = _compact(wiring_src)
    if (
        _compact(
            "def wire_kill_switch(runtime: OperatorInterfaceRuntime,*,backend: KillSwitchBackend,"
        )
        not in compact
    ):
        fail("wire_kill_switch must take `backend` as a required keyword-only argument")
    if _compact("backend: KillSwitchBackend=") in compact or _compact("backend=") in _compact(
        wiring_src.split("def wire_kill_switch", 1)[1].split(") ->", 1)[0]
    ):
        fail("wire_kill_switch must not default the backend")
    if "RustCliKillSwitchBackend(" in wiring_src:
        fail(
            "wire_kill_switch must not construct a fallback backend — the composer "
            "supplies it explicitly (uncovered capability -> no public surface)"
        )
    return (
        "wire_kill_switch takes backend as a required keyword-only argument with no default "
        "and constructs no fixture fallback — a bare runtime keeps serving the deferred 501"
    )


def check_backend_fails_closed(config: dict, backend_src: str) -> str:
    for token, why in (
        ("raise KillSwitchBackendError", "structured backend failures"),
        ("kill-switch CLI not found", "missing-binary fail-closed"),
        ("raise TimeoutError", "hung-activation -> TimeoutError (504/exit TIMEOUT)"),
        ("completed.returncode not in (0, 1)", "usage/fixture exit codes refused"),
        ("_REQUIRED_REPORT_KEYS", "report completeness validation"),
        ("refusing a mismatched report", "activation-id echo validation"),
    ):
        if token not in backend_src:
            fail(f"RustCliKillSwitchBackend is missing {why} (`{token}`)")
    return (
        "RustCliKillSwitchBackend fails closed: missing binary, timeout (TimeoutError -> 504), "
        "non-runnable exit codes, incomplete report, and mismatched activation_id are all "
        "structured errors — never success-shaped"
    )


def check_handlers_never_fabricate(config: dict, handlers_src: str) -> str:
    for token, why in (
        ("KILL_SWITCH_BACKEND_UNAVAILABLE", "backend failure surfaced as 500"),
        ("KILL_SWITCH_AUDIT_WRITE_FAILED", "failed durable audit write surfaced"),
        ("KILL_SWITCH_STATE_CORRUPT", "corrupt replay-guard state fails closed"),
        ('"activated": False, "last_activation": None', "honest-empty status"),
    ):
        if token not in handlers_src:
            fail(f"kill-switch handlers are missing {why} (`{token}`)")
    guard_index = handlers_src.find("_load_guard(self._state_dir)")
    backend_index = handlers_src.find("self._backend.activate(")
    if guard_index < 0 or backend_index < 0 or guard_index >= backend_index:
        fail(
            "the activate handler must consult the durable replay guard BEFORE firing "
            "the backend — a repeat activation must replay, never re-liquidate"
        )
    persist_index = handlers_src.find("persist_last_activation(self._state_dir, record)")
    audit_index = handlers_src.find("self._store.write(build_activation_record")
    if persist_index < 0 or audit_index < 0 or persist_index >= audit_index:
        fail(
            "the activate handler must arm the replay guard BEFORE the audit writes so "
            "any later failure replays instead of re-firing"
        )
    return (
        "handlers never fabricate: replay guard consulted before the backend and armed before "
        "the audit writes; backend/audit/state failures are structured errors; status is "
        "honest-empty before any activation"
    )


def check_owner_repoint(config: dict, owners_src: str) -> str:
    for token in ('"KILL_SWITCH": "SRS-SAFE-001"', '"kill-switch": "SRS-SAFE-001"'):
        if token not in owners_src:
            fail(f"operator-runtime owner map must carry {token}")
    return (
        "kill-switch ownership re-pointed to SRS-SAFE-001 on both the REST capability and "
        "CLI group owner maps"
    )


def check_serialized_honesty(config: dict, features_src: str) -> str:
    features = json.loads(features_src)
    entry = next((f for f in features if f["id"] == "SRS-SAFE-001"), None)
    if entry is None:
        fail("SRS-SAFE-001 missing from feature_list.json")
    if entry["passes"] is not False:
        fail(
            "SRS-SAFE-001 must stay passes:false (serialized) — the live path "
            "(SRS-EXE-006 transport, live state producers, hosted strategies) is deferred"
        )
    deferred = contract_block(config).get("deferred", [])
    text = " ".join(f"{e.get('feature', '')} {e.get('what', '')}" for e in deferred)
    for owner in ("SRS-EXE-006", "SRS-EXE-002", "SRS-EXE-005", "SRS-NOTIF-001", "UI-4"):
        if owner not in text:
            fail(f"kill_switch_activation_contract.deferred[] must name {owner}")
    return (
        "scope honesty: SRS-SAFE-001 stays passes:false; deferred[] names the live-path "
        "owners (SRS-EXE-006 transport, SRS-EXE-001/005 producers, SRS-EXE-002 hosting, "
        "SRS-NOTIF-001, UI-4)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

_STATIC_CHECKS = (
    ("entry_point", check_entry_point_returns_report_not_result, "gate"),
    ("phase_order", check_phase_order, "gate"),
    ("budget_constants", check_budget_constants, "types"),
    ("fleet_halts_every_engine", check_fleet_halts_every_engine, "fleet"),
    ("wiring_requires_backend", check_wiring_requires_explicit_backend, "wiring"),
    ("backend_fails_closed", check_backend_fails_closed, "backend"),
    ("handlers_never_fabricate", check_handlers_never_fabricate, "handlers"),
    ("owner_repoint", check_owner_repoint, "owners"),
    ("serialized_honesty", check_serialized_honesty, "features"),
)

_SOURCES = {
    "gate": gate_source,
    "types": types_source,
    "fleet": fleet_source,
    "wiring": wiring_source,
    "backend": backend_source,
    "handlers": handlers_source,
    "owners": owners_source,
    "features": features_source,
}


def assert_kill_switch_static(config: dict, root: Path = ROOT) -> list[str]:
    sources = {key: loader(config, root) for key, loader in _SOURCES.items()}
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SRS-SAFE-001 kill-switch activation runtime contract evidence"
    )
    parser.parse_args(argv)
    try:
        evidence = assert_kill_switch_static(load_config())
    except KillSwitchCheckError as error:
        print(f"SRS-SAFE-001 ACTIVATION-GATE FAIL: {error}", file=sys.stderr)
        return 1
    print("SRS-SAFE-001 ACTIVATION-GATE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- serialized: the scenario/perf evidence runs the mocked-IB fixture transport the "
        "feature's own Step 2 prescribes; SRS-SAFE-001 stays passes:false until the live "
        "path (SRS-EXE-006 / SRS-EXE-001 / SRS-EXE-005 / SRS-EXE-002) lands"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
