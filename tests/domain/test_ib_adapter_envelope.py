"""L7 domain safety test for SRS-EXE-006 — the IB Gateway brokerage adapter runtime.

Paired with the safety-path change in ``crates/atp-adapters/src/interactive_brokers.rs``
(IB order submission + connectivity classification). Three layers:

1. **Behavioral** — ``tools/ib_adapter_check.py`` passes against the real tree
   (its cargo smoke runs the boundary suite, so the IB-error -> SyRS SYS-64
   mapping is proven on the actual binary).
2. **Structural non-vacuity** — the check's individual assertions are fed mutated
   source/contract and must FAIL, so a regression (a dropped category, a removed
   never-drop variant, a fabricated-success sentinel) cannot pass silently.
3. **Scope honesty** — the architecture metadata marks the runtime ``serialized``
   and names the operator-initiated IB paper-account integration as the flip gate,
   and ``feature_list.json`` keeps SRS-EXE-006 ``passes:false``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "crates" / "atp-adapters" / "src" / "interactive_brokers.rs"


def _load_check():
    spec = importlib.util.spec_from_file_location(
        "ib_adapter_check", ROOT / "tools" / "ib_adapter_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = _load_check()


def _runtime() -> dict:
    config = json.loads((ROOT / "architecture" / "runtime_services.json").read_text())
    return config["adapter_contract"]["ib_brokerage_runtime"]


# --------------------------------------------------------------------------- #
# 1. Behavioral — the real check (incl. cargo boundary smoke) passes
# --------------------------------------------------------------------------- #


def test_ib_adapter_check_passes_on_real_tree():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "ib_adapter_check.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"ib_adapter_check failed:\n{result.stdout}\n{result.stderr}"
    assert "SRS-EXE-006 IB ADAPTER RUNTIME PASS" in result.stdout


# --------------------------------------------------------------------------- #
# 2. Structural non-vacuity — the check catches each regression
# --------------------------------------------------------------------------- #


def test_check_catches_dropped_category_mapping():
    runtime = _runtime()
    source = MODULE.read_text()
    # Remove the INVALID_SYMBOL codes from the classifier body -> must fail.
    broken = source.replace("IB_CODE_NO_SECURITY_DEFINITION | IB_CODE_SECURITY_NOT_AVAILABLE", "")
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_classifier_maps_every_code(runtime, broken)


def test_check_catches_removed_never_drop_variant():
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace("Unmapped {", "Removed {")
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_never_drop(runtime, broken)


def test_check_catches_fabricated_success_sentinel():
    runtime = _runtime()
    source = MODULE.read_text()
    # A non-negative sentinel could collide with a real IB code -> must fail.
    broken = source.replace(
        "pub const IB_CODE_LIVE_WIRE_PROTOCOL_PENDING: i32 = -1;",
        "pub const IB_CODE_LIVE_WIRE_PROTOCOL_PENDING: i32 = 1;",
    )
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_live_transport_fails_closed(runtime, broken)


def test_check_catches_missing_transport_method():
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace("fn cancel_order(&self, broker_order_id: &str)", "fn gone(&self)")
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_transport_trait(runtime, broken)


def test_check_catches_leaked_raw_transport_error():
    # If a public non-order op returns raw IbApiError instead of the IbAdapterError
    # boundary, the check must fail (raw transport error must not leak to callers).
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace(
        "pub fn cancel_order(&self, broker_order_id: &str) -> Result<(), IbAdapterError> {",
        "pub fn cancel_order(&self, broker_order_id: &str) -> Result<(), IbApiError> {",
    )
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_boundary_error_confined(runtime, broken)


def test_check_catches_unbounded_connect():
    # A connect() without an explicit timeout deadline must fail the check.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace("connect_timeout", "connect_no_timeout_xx")
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_live_transport_fails_closed(runtime, broken)


# --------------------------------------------------------------------------- #
# 3. Scope honesty — serialized, operator-gated, stays passes:false
# --------------------------------------------------------------------------- #


def test_runtime_is_serialized_and_operator_gated():
    runtime = _runtime()
    assert runtime["status"] == "serialized"
    deferred = " ".join(runtime["deferred"]).lower()
    assert "operator-initiated" in deferred
    assert "paper-account" in deferred
    # The four SYS-64 broker-validation categories SRS-ERR-001 needs are mapped.
    mapped = set(runtime["mapped_categories"])
    assert {
        "INVALID_SYMBOL",
        "INSUFFICIENT_BUYING_POWER",
        "RATE_LIMITED",
        "CONNECTIVITY_BLOCKED",
    }.issubset(mapped)


def test_feature_stays_passes_false_until_operator_integration():
    features = json.loads((ROOT / "feature_list.json").read_text())
    exe006 = next(f for f in features if f["id"] == "SRS-EXE-006")
    assert exe006["passes"] is False, (
        "SRS-EXE-006 must stay passes:false until the operator runs the IB paper-account "
        "integration test (paper_account_round_trip); it flips via --mode complete then."
    )
