"""SRS-SAFE-002 — fail-closed boundary tests for the liquidation-timeout
backend (``atp_safety.timeout``): the subprocess bridge to
``safe002_liquidation_timeout_cli`` must never mistake a drill that could not
run (or could not be trusted) for one that ran, and the durable-audit
composition must never swallow a failed SRS-LOG-001 write.

All tests drive the injectable runner — no cargo build required.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from atp_logging import LogClass, Severity, Source
from atp_logging.persistence import JsonlLogStore
from atp_safety import (
    LiquidationTimeoutAuditError,
    LiquidationTimeoutBackendError,
    RustCliLiquidationTimeoutBackend,
    resolve_liquidation_timeout,
)

pytestmark = [pytest.mark.boundary]


_TIMEOUT_PAYLOAD: dict[str, object] = {
    "disposition": "TIMED_OUT_UNFILLED",
    "notification": {"events": 1, "email_accepted": 1, "sms_accepted": 1},
    "gateway_calls": ["cancel:B-0001", "disconnect"],
    "probe_polls": 61,
    "simulated_elapsed_ms": 30000,
    "category": "KILL_SWITCH_LIQUIDATION_TIMEOUT",
    "error_type": "KillSwitchLiquidationTimeout",
    "message": "…",
    "unfilled_order": {
        "order_id": "live-momentum/ks-liq-0001",
        "symbol": "AAPL",
        "side": "SELL",
        "quantity": 250,
    },
    "manual_resolution_required": True,
    "cleanup": {
        "operator_alert": {"status": "SUCCEEDED"},
        "liquidation_cancel": {"status": "SUCCEEDED"},
        "ib_disconnect": {"status": "SUCCEEDED"},
        "audit_recorded": True,
    },
}


def _completed(returncode: int, stdout: str, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["cli"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _runner_returning(completed: subprocess.CompletedProcess[str]):
    def runner(argv, *, timeout_s):  # noqa: ANN001, ANN202 - test double
        return completed

    return runner


def _backend_with(completed: subprocess.CompletedProcess[str]) -> RustCliLiquidationTimeoutBackend:
    return RustCliLiquidationTimeoutBackend(runner=_runner_returning(completed))


def test_missing_binary_fails_closed(tmp_path: Path) -> None:
    backend = RustCliLiquidationTimeoutBackend(binary=tmp_path / "absent-cli")
    with pytest.raises(LiquidationTimeoutBackendError, match="not found"):
        backend.resolve()


def test_nonpositive_timeout_is_rejected() -> None:
    with pytest.raises(LiquidationTimeoutBackendError, match="must be positive"):
        RustCliLiquidationTimeoutBackend(timeout_s=0)


def test_usage_error_exit_2_fails_closed() -> None:
    backend = _backend_with(_completed(2, "", stderr="unknown flag '--bogus'"))
    with pytest.raises(LiquidationTimeoutBackendError, match="could not run"):
        backend.resolve()


def test_unknown_exit_code_fails_closed() -> None:
    backend = _backend_with(_completed(7, "outcome:{}"))
    with pytest.raises(LiquidationTimeoutBackendError, match="exit 7"):
        backend.resolve()


def test_missing_outcome_line_fails_closed() -> None:
    backend = _backend_with(_completed(1, "no outcome here\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="no outcome line"):
        backend.resolve()


def test_malformed_json_fails_closed() -> None:
    backend = _backend_with(_completed(1, "outcome:{not json}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="not valid JSON"):
        backend.resolve()


def test_non_object_outcome_fails_closed() -> None:
    backend = _backend_with(_completed(1, "outcome:[1,2,3]\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="JSON object"):
        backend.resolve()


def test_missing_required_keys_fail_closed() -> None:
    truncated = {"disposition": "TIMED_OUT_UNFILLED"}
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(truncated)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="missing required keys"):
        backend.resolve()


def test_disposition_exit_code_mismatch_fails_closed() -> None:
    # Exit 0 claims "filled" but the payload says TIMED_OUT_UNFILLED —
    # version skew the backend must refuse rather than trust either side.
    backend = _backend_with(_completed(0, f"outcome:{json.dumps(_TIMEOUT_PAYLOAD)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="mismatched outcome"):
        backend.resolve()


def test_subprocess_timeout_surfaces_as_timeout_error() -> None:
    def hanging_runner(argv, *, timeout_s):  # noqa: ANN001, ANN202 - test double
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_s)

    backend = RustCliLiquidationTimeoutBackend(runner=hanging_runner)
    with pytest.raises(TimeoutError, match="exceeded"):
        backend.resolve()


def test_exit_1_parses_as_a_normal_timed_out_outcome() -> None:
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(_TIMEOUT_PAYLOAD)}\n"))
    outcome = backend.resolve()
    assert outcome.timed_out
    assert outcome.exit_code == 1
    assert outcome.manual_resolution_required
    assert outcome.payload["gateway_calls"] == ["cancel:B-0001", "disconnect"]


def test_exit_3_parses_as_a_fail_closed_probe_outcome() -> None:
    payload = dict(_TIMEOUT_PAYLOAD)
    payload["disposition"] = "PROBE_INCONSISTENT"
    payload["category"] = "KILL_SWITCH_LIQUIDATION_PROBE_UNAVAILABLE"
    payload["error_type"] = "KillSwitchLiquidationProbeInconsistent"
    payload["manual_resolution_required"] = False
    payload["gateway_calls"] = []
    backend = _backend_with(_completed(3, f"outcome:{json.dumps(payload)}\n"))
    outcome = backend.resolve()
    assert not outcome.timed_out
    assert outcome.disposition == "PROBE_INCONSISTENT"


def test_resolve_writes_the_liquidation_timeout_record_durably(tmp_path: Path) -> None:
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(_TIMEOUT_PAYLOAD)}\n"))
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)

    outcome, record = resolve_liquidation_timeout(backend, store)

    assert outcome.timed_out
    assert record is not None
    persisted = store.read(source=Source.KILL_SWITCH, event_type="LIQUIDATION_TIMEOUT")
    assert len(persisted) == 1
    entry = persisted[0]
    assert entry.severity is Severity.CRITICAL
    assert entry.correlation_id == "live-momentum/ks-liq-0001"
    for needle in ("AAPL", "SELL", "250", "manual_resolution_required=True"):
        assert needle in entry.message


def test_filled_disposition_writes_no_timeout_record(tmp_path: Path) -> None:
    payload = dict(_TIMEOUT_PAYLOAD)
    payload["disposition"] = "FILLED_BEFORE_TIMEOUT"
    payload["category"] = None
    payload["error_type"] = None
    payload["manual_resolution_required"] = False
    payload["gateway_calls"] = []
    backend = _backend_with(_completed(0, f"outcome:{json.dumps(payload)}\n"))
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)

    outcome, record = resolve_liquidation_timeout(backend, store)

    assert not outcome.timed_out
    assert record is None
    assert store.read(source=Source.KILL_SWITCH) == []


def test_failed_audit_write_surfaces_and_carries_the_outcome(tmp_path: Path) -> None:
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(_TIMEOUT_PAYLOAD)}\n"))

    class RefusingStore:
        def write(self, record):  # noqa: ANN001, ANN202 - test double
            raise OSError("disk full")

    with pytest.raises(LiquidationTimeoutAuditError, match="audit write failed") as excinfo:
        resolve_liquidation_timeout(backend, RefusingStore())  # type: ignore[arg-type]
    # The outcome is carried on the error — what happened is never lost.
    assert excinfo.value.outcome.timed_out
