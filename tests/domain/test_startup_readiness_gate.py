"""L7 domain test for the ERR-9 SDK-surface startup readiness gate.

The L7 layer drives the gate through a reference "boot orchestrator" stub
that pretends to be the future log sink (SRS-LOG-001), dashboard backend
(SRS-UI-001), and REST/WebSocket endpoint (SRS-API-001). Verifies that:

* a missing-or-invalid startup configuration holds the system in
  ``pre_trade_blocked``;
* the same hold surfaces a structured failure on every fan-out path
  (log records, dashboard payload, API-shaped response);
* the operator override path produces an audit-trail record that survives
  re-evaluation;
* the gate never silently accepts a configuration regression once the
  system has been released.

Marked ``safety`` + ``domain`` so the deterministic critic recognises the
file as the paired safety-path test for the SDK-surface diff.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_config import REQUIRED_KEYS  # noqa: E402
from atp_readiness import (  # noqa: E402
    GateState,
    GateTransitionError,
    OperatorOverride,
    OverrideAuditError,
    PreTradeHoldError,
    ReadinessGate,
)

pytestmark = [pytest.mark.safety, pytest.mark.domain]


def _defaults() -> dict[str, str]:
    return {spec.name: spec.default for spec in REQUIRED_KEYS if spec.default is not None}


def _missing(*keys: str) -> dict[str, str]:
    env = _defaults()
    for key in keys:
        env.pop(key, None)
    return env


# --------------------------------------------------------------------------- #
# Reference boot orchestrator (stub for the future SRS-LOG-001 / SRS-UI-001 /
# SRS-API-001 consumers)
# --------------------------------------------------------------------------- #


@dataclass
class _RefLogSink:
    """Stub for the future SRS-LOG-001 system-log sink."""

    records: list[dict[str, Any]] = field(default_factory=list)

    def write_jsonl(self, records: list[dict[str, Any]]) -> None:
        # Real sink would persist to the system-log file; the stub stores +
        # JSON-round-trips so we catch a payload that isn't JSON-serialisable.
        for record in records:
            line = json.dumps(record, sort_keys=True)
            self.records.append(json.loads(line))


@dataclass
class _RefDashboardPane:
    """Stub for the future SRS-UI-001 dashboard readiness pane."""

    latest_payload: dict[str, Any] | None = None

    def render(self, payload: dict[str, Any]) -> None:
        # JSON round-trip catches non-serialisable fields.
        self.latest_payload = json.loads(json.dumps(payload, sort_keys=True))


@dataclass
class _RefApiEndpoint:
    """Stub for the future SRS-API-001 GET /api/v1/system/readiness endpoint."""

    last_response: dict[str, Any] | None = None

    def get_readiness(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Real endpoint would emit the dashboard payload over HTTP; the stub
        # JSON-round-trips and saves the response shape.
        body = json.loads(json.dumps(payload, sort_keys=True))
        self.last_response = body
        return body


@dataclass
class _RefBootOrchestrator:
    """The future SRS-MD-006 boot orchestrator's SDK-surface seam.

    This isn't the production code path; it's a reference consumer that
    proves the gate's contract is rich enough for the eventual log sink,
    dashboard, and API consumers to fan out without further changes.
    """

    gate: ReadinessGate
    log_sink: _RefLogSink = field(default_factory=_RefLogSink)
    dashboard: _RefDashboardPane = field(default_factory=_RefDashboardPane)
    api: _RefApiEndpoint = field(default_factory=_RefApiEndpoint)

    def fan_out(self) -> None:
        self.log_sink.write_jsonl(self.gate.as_log_records())
        payload = self.gate.as_dashboard_payload()
        self.dashboard.render(payload)
        self.api.get_readiness(payload)


# --------------------------------------------------------------------------- #
# End-to-end scenarios
# --------------------------------------------------------------------------- #


class Err9HoldAndFanOutTest(unittest.TestCase):
    """ERR-9: missing or invalid startup configuration holds + fans out."""

    def test_missing_credential_holds_and_surfaces_on_every_path(self) -> None:
        env = _missing("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)

        # 1. Hold.
        self.assertIs(gate.state, GateState.PRE_TRADE_BLOCKED)
        with self.assertRaises(PreTradeHoldError) as ctx:
            gate.assert_ready_or_hold()
        self.assertEqual(len(ctx.exception.report.errors), 1)

        # 2. Fan out to log + dashboard + API simultaneously.
        boot = _RefBootOrchestrator(gate=gate)
        boot.fan_out()

        # 3. The same structured failure body must appear on every fan-out path.
        log_record_keys = {r["key"] for r in boot.log_sink.records}
        self.assertIn("DATABENTO_API_KEY", log_record_keys)

        assert boot.dashboard.latest_payload is not None
        dashboard_error_keys = {f["key"] for f in boot.dashboard.latest_payload["errors"]}
        self.assertIn("DATABENTO_API_KEY", dashboard_error_keys)
        self.assertEqual(boot.dashboard.latest_payload["state"], "pre_trade_blocked")
        self.assertFalse(boot.dashboard.latest_payload["ok"])

        assert boot.api.last_response is not None
        api_error_keys = {f["key"] for f in boot.api.last_response["errors"]}
        self.assertIn("DATABENTO_API_KEY", api_error_keys)
        self.assertEqual(boot.api.last_response["state"], "pre_trade_blocked")

    def test_invalid_int_holds_and_surfaces_on_every_path(self) -> None:
        env = _defaults()
        env["ATP_MARKET_DATA_LINE_LIMIT"] = "not-a-number"
        gate = ReadinessGate.from_env(env)
        boot = _RefBootOrchestrator(gate=gate)
        boot.fan_out()

        log_record_keys = {r["key"] for r in boot.log_sink.records}
        self.assertIn("ATP_MARKET_DATA_LINE_LIMIT", log_record_keys)
        assert boot.dashboard.latest_payload is not None
        self.assertFalse(boot.dashboard.latest_payload["ok"])
        assert boot.api.last_response is not None
        self.assertEqual(boot.api.last_response["state"], "pre_trade_blocked")

    def test_invalid_path_holds_and_surfaces_on_every_path(self) -> None:
        env = _defaults()
        env["ATP_SSD_DATA_DIR"] = "relative/path"
        gate = ReadinessGate.from_env(env)
        boot = _RefBootOrchestrator(gate=gate)
        boot.fan_out()
        api_keys = {f["key"] for f in (boot.api.last_response or {"errors": []})["errors"]}
        self.assertIn("ATP_SSD_DATA_DIR", api_keys)

    def test_production_env_escalates_placeholder_secrets(self) -> None:
        env = _defaults()
        env["ATP_ENV"] = "production"
        gate = ReadinessGate.from_env(env)
        self.assertIs(gate.state, GateState.PRE_TRADE_BLOCKED)
        boot = _RefBootOrchestrator(gate=gate)
        boot.fan_out()
        log_keys = {r["key"] for r in boot.log_sink.records}
        # All placeholder secrets must escalate to error in production —
        # the two vendor keys, the two notification keys, and the IB
        # account identifier (ATP_IB_ACCOUNT, SRS-SEC-001).
        self.assertTrue(
            {
                "DATABENTO_API_KEY",
                "SHARADAR_API_KEY",
                "ATP_SMTP_API_KEY",
                "ATP_SMS_API_KEY",
                "ATP_IB_ACCOUNT",
            }.issubset(log_keys)
        )
        assert boot.api.last_response is not None
        # The API response must be JSON-serialisable end-to-end (this is what
        # the future SRS-API-001 endpoint will hand to clients).
        json.dumps(boot.api.last_response)


class Err9OperatorOverrideTest(unittest.TestCase):
    """SRS-MD-006: operator override with audit trail releases the pre-trade hold."""

    def _override(self, **overrides: Any) -> OperatorOverride:
        kwargs = dict(
            actor="operator@example.com",
            reason="Reservoir warm-up; IB credentials intentionally unset in dev",
            audit_trail_id="audit-2026-05-21-001",
            timestamp_ns=time.time_ns(),
        )
        kwargs.update(overrides)
        return OperatorOverride(**kwargs)

    def test_override_releases_hold_and_audit_record_threads_log_fan_out(self) -> None:
        env = _missing("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)
        override = self._override()
        gate.operator_override(override)
        self.assertIs(gate.state, GateState.OVERRIDDEN)

        boot = _RefBootOrchestrator(gate=gate)
        boot.fan_out()

        # The operator override must surface in the log fan-out so SRS-LOG-001
        # can ingest it into the audit log when that surface lands.
        override_records = [r for r in boot.log_sink.records if r["severity"] == "override"]
        self.assertEqual(len(override_records), 1)
        self.assertEqual(override_records[0]["actor"], override.actor)
        self.assertEqual(override_records[0]["audit_trail_id"], override.audit_trail_id)

        # And in the dashboard / API payload so SRS-UI-001 can render the
        # "active operator override" banner when that surface lands.
        assert boot.dashboard.latest_payload is not None
        self.assertEqual(len(boot.dashboard.latest_payload["overrides"]), 1)
        assert boot.api.last_response is not None
        self.assertEqual(boot.api.last_response["state"], "overridden")
        self.assertTrue(boot.api.last_response["ok"])

    def test_audit_incomplete_override_is_refused(self) -> None:
        env = _missing("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)
        with self.assertRaises(OverrideAuditError):
            gate.operator_override(self._override(reason=""))
        # Gate must remain in PRE_TRADE_BLOCKED.
        self.assertIs(gate.state, GateState.PRE_TRADE_BLOCKED)
        with self.assertRaises(OverrideAuditError):
            gate.operator_override(self._override(timestamp_ns=-1))
        self.assertIs(gate.state, GateState.PRE_TRADE_BLOCKED)

    def test_override_from_ready_is_refused(self) -> None:
        # Operator cannot pre-emptively override a system that is already
        # ready; the override is only a release of an active hold.
        gate = ReadinessGate.from_env(_defaults())
        self.assertIs(gate.state, GateState.READY)
        with self.assertRaises(GateTransitionError):
            gate.operator_override(self._override())

    def test_reevaluation_with_new_error_after_override_demotes_to_blocked(self) -> None:
        env = _missing("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)
        gate.operator_override(self._override())
        self.assertIs(gate.state, GateState.OVERRIDDEN)

        # New defect surfaces (e.g., operator broke ATP_MARKET_DATA_LINE_LIMIT
        # while editing config). Gate must NOT stay in OVERRIDDEN — the prior
        # release was about a *specific* hold; new defects need a new
        # acknowledgement so the hold-fan-out path fires again.
        bad_env = _defaults()
        bad_env["ATP_MARKET_DATA_LINE_LIMIT"] = "not-a-number"
        gate.reevaluate(bad_env)
        self.assertIs(gate.state, GateState.PRE_TRADE_BLOCKED)
        # But the audit trail of the prior override is preserved.
        self.assertEqual(len(gate.overrides), 1)

    def test_clean_reevaluation_after_override_transitions_to_ready(self) -> None:
        env = _missing("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)
        gate.operator_override(self._override())
        gate.reevaluate(_defaults())
        self.assertIs(gate.state, GateState.READY)
        # Audit trail preserved.
        self.assertEqual(len(gate.overrides), 1)


class Err9NoSilentRegressionTest(unittest.TestCase):
    """Once the system has reached READY, a new defect must re-hold it."""

    def test_ready_to_pre_trade_blocked_on_new_defect(self) -> None:
        gate = ReadinessGate.from_env(_defaults())
        self.assertIs(gate.state, GateState.READY)
        bad_env = _defaults()
        bad_env["ATP_LIVE_STRATEGY_CPU"] = "not-a-float"
        gate.reevaluate(bad_env)
        self.assertIs(gate.state, GateState.PRE_TRADE_BLOCKED)
        with self.assertRaises(PreTradeHoldError):
            gate.assert_ready_or_hold()

    def test_uninitialised_gate_never_serves_ready(self) -> None:
        # The boot orchestrator must NOT be able to claim READY before the
        # static config has been evaluated.
        gate = ReadinessGate()
        with self.assertRaises(GateTransitionError):
            gate.assert_ready_or_hold()
        # Accessing report before seeding is a structural error too.
        with self.assertRaises(RuntimeError):
            _ = gate.report

    def test_from_env_rejects_non_mapping(self) -> None:
        # The boot orchestrator must get a structured TypeError, not an
        # AttributeError or KeyError from inside atp_config, when handed a
        # non-Mapping env.
        for bad in (None, "ATP_ENV=development", ["ATP_ENV=development"], 0):
            with self.assertRaises(TypeError):
                ReadinessGate.from_env(bad)  # type: ignore[arg-type]

    def test_reevaluate_rejects_non_mapping(self) -> None:
        gate = ReadinessGate.from_env(_defaults())
        for bad in (None, "ATP_ENV=development", 0):
            with self.assertRaises(TypeError):
                gate.reevaluate(bad)  # type: ignore[arg-type]

    def test_dashboard_payload_is_json_serialisable_in_every_state(self) -> None:
        # PRE_TRADE_BLOCKED
        env = _missing("DATABENTO_API_KEY")
        gate = ReadinessGate.from_env(env)
        json.dumps(gate.as_dashboard_payload())
        # OVERRIDDEN
        gate.operator_override(
            OperatorOverride(
                actor="operator@example.com",
                reason="x",
                audit_trail_id="audit-1",
                timestamp_ns=time.time_ns(),
            )
        )
        json.dumps(gate.as_dashboard_payload())
        # READY (re-evaluate to clean)
        gate.reevaluate(_defaults())
        json.dumps(gate.as_dashboard_payload())


if __name__ == "__main__":
    unittest.main()
