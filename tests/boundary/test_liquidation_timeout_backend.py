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
        "event_sink_recorded": True,
    },
}


def _no_cleanup_payload(disposition: str) -> dict[str, object]:
    """A CONSISTENT payload for a disposition whose contract is 'no SYS-44b
    cleanup ran' — mirrors what the real CLI prints for filled / fail-closed
    probe outcomes (empty gateway calls, zero accepted pages, every cleanup
    leg NOT_ATTEMPTED)."""
    payload = dict(_TIMEOUT_PAYLOAD)
    payload["disposition"] = disposition
    payload["manual_resolution_required"] = False
    payload["gateway_calls"] = []
    payload["notification"] = {"events": 0, "email_accepted": 0, "sms_accepted": 0}
    payload["cleanup"] = {
        "operator_alert": {"status": "NOT_ATTEMPTED"},
        "liquidation_cancel": {"status": "NOT_ATTEMPTED"},
        "ib_disconnect": {"status": "NOT_ATTEMPTED"},
        "event_sink_recorded": False,
    }
    if disposition == "FILLED_BEFORE_TIMEOUT":
        payload["category"] = None
        payload["error_type"] = None
        payload.pop("unfilled_order", None)
    else:
        payload["category"] = "KILL_SWITCH_LIQUIDATION_PROBE_UNAVAILABLE"
        payload["error_type"] = (
            "KillSwitchLiquidationProbeInconsistent"
            if disposition == "PROBE_INCONSISTENT"
            else "KillSwitchLiquidationProbeUnavailable"
        )
    return payload


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


def test_launch_oserror_surfaces_as_the_typed_backend_error() -> None:
    # Codex r7: a launch failure (PermissionError / ENOENT under a race) must
    # be the TYPED fail-closed error, never a raw OSError at the operator
    # boundary of a safety workflow.
    def exploding_runner(argv, *, timeout_s):  # noqa: ANN001, ANN202 - test double
        raise PermissionError(13, "Permission denied")

    backend = RustCliLiquidationTimeoutBackend(runner=exploding_runner)
    with pytest.raises(LiquidationTimeoutBackendError, match="could not be launched"):
        backend.resolve()


def test_existing_but_non_executable_binary_fails_closed_typed(tmp_path: Path) -> None:
    # The real runner path: the file exists (passes the is_file() check) but
    # is not executable — subprocess.run raises PermissionError, which must
    # arrive as the typed backend error.
    not_executable = tmp_path / "safe002_cli"
    not_executable.write_text("#!/bin/sh\n")
    not_executable.chmod(0o644)
    backend = RustCliLiquidationTimeoutBackend(binary=not_executable)
    with pytest.raises(LiquidationTimeoutBackendError, match="could not be launched"):
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
    payload = _no_cleanup_payload("PROBE_INCONSISTENT")
    backend = _backend_with(_completed(3, f"outcome:{json.dumps(payload)}\n"))
    outcome = backend.resolve()
    assert not outcome.timed_out
    assert outcome.disposition == "PROBE_INCONSISTENT"


def test_non_timeout_disposition_claiming_gateway_calls_is_refused() -> None:
    # Codex r1 finding: a version-skewed CLI could report a fail-closed
    # disposition while its own evidence shows the destructive cleanup ran —
    # trusting it would suppress the durable safety record. Refuse.
    payload = _no_cleanup_payload("PROBE_UNAVAILABLE")
    payload["gateway_calls"] = ["cancel:B-0001", "disconnect"]
    backend = _backend_with(_completed(3, f"outcome:{json.dumps(payload)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="contradicts"):
        backend.resolve()


def test_non_timeout_disposition_claiming_cleanup_ran_is_refused() -> None:
    payload = _no_cleanup_payload("PROBE_INCONSISTENT")
    payload["cleanup"] = {
        "operator_alert": {"status": "NOT_ATTEMPTED"},
        "liquidation_cancel": {"status": "SUCCEEDED"},
        "ib_disconnect": {"status": "NOT_ATTEMPTED"},
        "event_sink_recorded": False,
    }
    backend = _backend_with(_completed(3, f"outcome:{json.dumps(payload)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="liquidation_cancel"):
        backend.resolve()


def test_filled_disposition_claiming_accepted_pages_is_refused() -> None:
    payload = _no_cleanup_payload("FILLED_BEFORE_TIMEOUT")
    payload["notification"] = {"events": 1, "email_accepted": 1, "sms_accepted": 1}
    backend = _backend_with(_completed(0, f"outcome:{json.dumps(payload)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="email_accepted"):
        backend.resolve()


def test_probe_refusal_without_order_identity_is_refused() -> None:
    # A fail-closed refusal must stay auditable: no unfilled_order → refuse.
    payload = _no_cleanup_payload("PROBE_UNAVAILABLE")
    payload.pop("unfilled_order")
    backend = _backend_with(_completed(3, f"outcome:{json.dumps(payload)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="not auditable"):
        backend.resolve()


def test_timed_out_disposition_with_unattempted_cleanup_is_refused() -> None:
    # Codex r2 finding (the symmetric direction): a TIMED_OUT_UNFILLED whose
    # own evidence says the SYS-44b sequence did NOT run must be refused —
    # writing the durable record for it would imply the timeout was handled
    # while the page/cancel/disconnect never fired.
    payload = dict(_TIMEOUT_PAYLOAD)
    payload["cleanup"] = {
        "operator_alert": {"status": "SUCCEEDED"},
        "liquidation_cancel": {"status": "NOT_ATTEMPTED"},
        "ib_disconnect": {"status": "SUCCEEDED"},
        "event_sink_recorded": True,
    }
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(payload)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="liquidation_cancel"):
        backend.resolve()


def test_timed_out_disposition_without_manual_resolution_flag_is_refused() -> None:
    payload = dict(_TIMEOUT_PAYLOAD)
    payload["manual_resolution_required"] = False
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(payload)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="manual_resolution_required"):
        backend.resolve()


def test_timed_out_success_claims_without_evidence_are_refused() -> None:
    # Codex r8: SUCCEEDED statuses must be backed by the payload's own
    # evidence — all-SUCCEEDED with zero accepted pages and no broker calls
    # would put a false "handled" record in the durable audit log.
    payload = dict(_TIMEOUT_PAYLOAD)
    payload["notification"] = {"events": 0, "email_accepted": 0, "sms_accepted": 0}
    payload["gateway_calls"] = []
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(payload)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="without"):
        backend.resolve()


def test_timed_out_succeeded_disconnect_without_gateway_call_is_refused() -> None:
    payload = dict(_TIMEOUT_PAYLOAD)
    payload["gateway_calls"] = ["cancel:B-0001"]  # disconnect claim unbacked
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(payload)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="ib_disconnect=SUCCEEDED without"):
        backend.resolve()


def test_timed_out_succeeded_page_without_accepted_channels_is_refused() -> None:
    payload = dict(_TIMEOUT_PAYLOAD)
    payload["notification"] = {"events": 1, "email_accepted": 1, "sms_accepted": 0}
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(payload)}\n"))
    with pytest.raises(LiquidationTimeoutBackendError, match="operator_alert=SUCCEEDED without"):
        backend.resolve()


def test_timed_out_disposition_with_failed_legs_is_a_valid_outcome() -> None:
    # A FAILED attempt is a valid, observable outcome (continue-to-safety) —
    # only an UNATTEMPTED leg on a confirmed timeout is a contract breach.
    payload = dict(_TIMEOUT_PAYLOAD)
    payload["cleanup"] = {
        "operator_alert": {"status": "FAILED", "reason": "SMS gateway down"},
        "liquidation_cancel": {"status": "FAILED", "reason": "IB cancel unreachable"},
        "ib_disconnect": {"status": "SUCCEEDED"},
        "event_sink_recorded": True,
    }
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(payload)}\n"))
    outcome = backend.resolve()
    assert outcome.timed_out
    assert outcome.manual_resolution_required


def test_resolve_writes_the_liquidation_timeout_record_durably(tmp_path: Path) -> None:
    backend = _backend_with(_completed(1, f"outcome:{json.dumps(_TIMEOUT_PAYLOAD)}\n"))
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)

    outcome, record = resolve_liquidation_timeout(backend, store)

    assert outcome.timed_out
    assert record is not None
    # The DURABLE-audit truth is stamped by the persistence step, not the CLI.
    assert outcome.payload["durable_audit_recorded"] is True
    persisted = store.read(source=Source.KILL_SWITCH, event_type="LIQUIDATION_TIMEOUT")
    assert len(persisted) == 1
    entry = persisted[0]
    assert entry.severity is Severity.CRITICAL
    assert entry.correlation_id == "live-momentum/ks-liq-0001"
    for needle in ("AAPL", "SELL", "250", "manual_resolution_required=True"):
        assert needle in entry.message


def test_filled_disposition_writes_no_timeout_record(tmp_path: Path) -> None:
    payload = _no_cleanup_payload("FILLED_BEFORE_TIMEOUT")
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
    # Codex r5: a failed durable write can NEVER masquerade as a recorded
    # audit — the carried outcome is stamped durable_audit_recorded=False,
    # regardless of the CLI's in-memory event_sink_recorded claim.
    assert excinfo.value.outcome.payload["durable_audit_recorded"] is False
    cleanup = excinfo.value.outcome.payload["cleanup"]
    assert cleanup["event_sink_recorded"] is True  # the in-memory sink's scope only
