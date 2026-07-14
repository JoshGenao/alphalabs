"""L7 domain safety test for SRS-EXE-006 — the IB Gateway brokerage adapter runtime.

Paired with the safety-path change in ``crates/atp-adapters/src/interactive_brokers.rs``
(IB order submission + connectivity classification). Three layers:

1. **Behavioral** — ``tools/ib_adapter_check.py`` passes against the real tree
   (its cargo smoke runs the boundary suite, so the IB-error -> SyRS SYS-64
   mapping is proven on the actual binary).
2. **Structural non-vacuity** — the check's individual assertions are fed mutated
   source/contract and must FAIL, so a regression (a dropped category, a removed
   never-drop variant, a fabricated-success sentinel) cannot pass silently.
3. **Scope honesty** — the architecture metadata marks the runtime ``verified``
   (the operator ran the IB paper-account round trip) while STILL enumerating the
   remaining operator-gated work (the SRS-EXE-004 composite wire, the SRS-EXE-007
   version-upgrade regression), and a ``passes:true`` SRS-EXE-006 always rides on
   that declared evidence — never the other way round.

This domain test is the paired safety pin for the SRS-EXE-006 live wire protocol
(see ``progress.d/session-SRS-EXE-006.md``): the TWS encodings are pinned to
server version 176 and golden-tested against a scripted fake gateway
(``srs_exe_006_ib_wire.rs``); the live transport stays feature-gated OFF by
default so the default surface never advertises a socket it cannot verify solo.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
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


@pytest.mark.skipif(
    shutil.which("cargo") is None,
    reason="ib_adapter_check fails closed without cargo; the cargo boundary proof needs the toolchain",
)
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
    # The PASS must be backed by the real cargo boundary suite + the feature-on
    # build of the gated scaffold, not a skip.
    assert "cargo smoke" in result.stdout
    assert "boundary suite ok" in result.stdout
    assert "ib-live-transport" in result.stdout


def test_cargo_smoke_fails_closed_without_cargo(monkeypatch):
    # If cargo is absent the gate must FAIL CLOSED (never a vacuous skip-as-pass).
    monkeypatch.setattr(CHECK.shutil, "which", lambda _name: None)
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_cargo_smoke(_runtime())


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


def test_check_catches_dropped_canonical_trait():
    # If the adapter stops implementing a canonical trait, callers can't reach the
    # runtime through the documented interface -> the check must fail.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace(
        "impl<C: IbGatewayConnection> BrokerageAdapter for InteractiveBrokersBrokerage<C>",
        "impl<C: IbGatewayConnection> SomethingElse for InteractiveBrokersBrokerage<C>",
    )
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_canonical_boundary(runtime, broken)


def test_check_catches_unimplemented_account_methods():
    # If account_status/positions are dropped from the BrokerageAdapter impl they
    # inherit NotConfigured while advertising a brokerage capability -> must fail.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace(
        "fn account_status(&self) -> AdapterResult<DataBatch> {", "fn x(&self) {"
    )
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_canonical_boundary(runtime, broken)


def test_check_catches_dropped_failure_detail():
    # If the mapper stops carrying the raw code, a failure could be dropped -> fail.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace("code: error.code,", "")
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_canonical_boundary(runtime, broken)


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


def test_check_catches_unbridged_provider():
    # If the documented provider loses its bridge to the functional runtime, a
    # caller following the contract reaches an inert stub -> the check must fail.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace("pub fn with_gateway<C: IbGatewayConnection>", "pub fn gone<C>")
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_provider_bridge(runtime, broken)


def test_check_catches_config_silent_fallback():
    # If the port parser stops rejecting port 0, a malformed config could silently
    # fall back to a default endpoint -> the check must fail.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace(".filter(|&port| port != 0)", "")
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_config_fails_closed(runtime, broken)


def test_check_catches_unbounded_connect():
    # A connect() without an explicit timeout deadline must fail the check.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace("connect_timeout", "connect_no_timeout_xx")
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_live_transport_fails_closed(runtime, broken)


def test_check_catches_dns_in_connect():
    # A DNS resolution step inside connect() (unbounded by the socket deadline) must fail.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace(
        "let stream = TcpStream::connect_timeout(&socket, IB_CONNECT_TIMEOUT)",
        "let _ = socket.to_socket_addrs();\n        let stream = TcpStream::connect_timeout(&socket, IB_CONNECT_TIMEOUT)",
    )
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_live_transport_fails_closed(runtime, broken)


def test_check_catches_unvalidated_host():
    # Dropping the literal-IP host validation must fail the config check.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace("pub fn ip(", "pub fn xx(")
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_config_fails_closed(runtime, broken)


def test_check_catches_ungated_live_transport():
    # If the live socket scaffold is no longer behind the non-default feature, it
    # would ship on the default public surface -> the check must fail.
    runtime = _runtime()
    source = MODULE.read_text()
    broken = source.replace(
        '#[cfg(feature = "ib-live-transport")]\n#[derive(Debug)]', "#[derive(Debug)]"
    )
    with pytest.raises(CHECK.IbAdapterContractError):
        CHECK.check_live_transport_fails_closed(runtime, broken)


# --------------------------------------------------------------------------- #
# 3. Scope honesty — verified, with the remaining gated scope still declared
# --------------------------------------------------------------------------- #


def test_runtime_is_verified_with_remaining_gated_scope():
    runtime = _runtime()
    assert runtime["status"] == "verified"
    assert runtime["pinned_server_version"] == 176
    # Verification must not silently over-claim the still-pending scope: the
    # SRS-EXE-004 composite wire and EXE-007 upgrade regression stay declared.
    deferred = " ".join(runtime["deferred"]).lower()
    assert "operator-initiated" in deferred
    assert "paper-account" in deferred
    assert "composite" in deferred
    # The four SYS-64 broker-validation categories SRS-ERR-001 needs are mapped.
    mapped = set(runtime["mapped_categories"])
    assert {
        "INVALID_SYMBOL",
        "INSUFFICIENT_BUYING_POWER",
        "RATE_LIMITED",
        "CONNECTIVITY_BLOCKED",
    }.issubset(mapped)


def test_passes_true_always_rides_on_declared_verified_evidence():
    # Timing-safe invariant across the flip: BEFORE integrate the board may
    # still read passes:false with the evidence already verified, but a
    # passes:true SRS-EXE-006 without status=='verified' means the flip
    # outlived its evidence — fail closed.
    features = json.loads((ROOT / "feature_list.json").read_text())
    exe006 = next(f for f in features if f["id"] == "SRS-EXE-006")
    if exe006["passes"]:
        assert _runtime()["status"] == "verified", (
            "SRS-EXE-006 is passes:true but the architecture no longer declares the "
            "verified IB paper-account evidence (status regressed)"
        )


# --------------------------------------------------------------------------- #
# 4. Live wire safety pins (SRS-EXE-006 wire completion)
# --------------------------------------------------------------------------- #


def test_live_account_sessions_are_gated_on_srs_exe_001():
    """The LIVE IB account must fail closed in the transport BEFORE any socket
    opens (the execution-engine admission — live-strategy registry, stale-data
    gate, kill-switch — is SRS-EXE-001's still-deferred scope). The gate lives
    in `with_session`, ahead of session establishment, so no operation on a
    live-account transport can reach the broker."""
    source = MODULE.read_text()
    gate_at = source.find("if self.account != IbAccountKind::Paper")
    establish_at = source.find("wire::IbSession::establish")
    assert gate_at != -1, "the live-account admission gate is missing from the transport"
    assert establish_at != -1
    assert gate_at < establish_at, "the gate must run BEFORE session establishment"
    assert "SRS-EXE-001" in source[gate_at : gate_at + 900], (
        "the gate must name its owning deferred feature (SRS-EXE-001)"
    )
    # And the fake-gateway suite pins the runtime behavior of that gate.
    wire_tests = (ROOT / _runtime()["wire_tests"]).read_text()
    assert "live_account_transport_fails_closed_pending_execution_engine" in wire_tests


def test_operator_mode_check_runs_the_paper_round_trip():
    """Under ATP_RUN_INTEGRATION=1 the contract check must EXECUTE the
    acceptance-critical `paper_account_round_trip` (evidence, not metadata
    trust); without the env it must say so rather than silently skip."""
    import inspect

    smoke_source = inspect.getsource(CHECK.check_cargo_smoke)
    spec = _runtime()["integration_test"]
    assert 'os.environ.get(spec["gate_env"])' in smoke_source, (
        "check_cargo_smoke must consult the operator env gate"
    )
    assert '"--ignored"' in smoke_source, "the operator leg must run the #[ignore] flip test itself"
    assert spec["gate_env"] == "ATP_RUN_INTEGRATION"
    # Non-operator runs must LABEL the skipped live leg, never imply it ran.
    assert "SKIPPED" in smoke_source


def test_composite_path_shares_the_live_account_gate():
    """The composite (SRS-EXE-004) path must sit behind the SAME SRS-EXE-001
    live-account admission gate as the session operations — before connect."""
    source = MODULE.read_text()
    # The gated trait impl (not the seam declaration): find the LAST definition,
    # which is TcpIbGateway's.
    composite_at = source.rfind("fn submit_composite_order(")
    assert composite_at != -1
    body = source[composite_at : composite_at + 1600]
    gate_at = body.find("self.account != IbAccountKind::Paper")
    connect_at = body.find("self.connect()")
    assert gate_at != -1, "composite path is missing the live-account gate"
    assert connect_at != -1
    assert gate_at < connect_at, "the composite gate must run BEFORE connect"
    wire_tests = (ROOT / _runtime()["wire_tests"]).read_text()
    assert "live_account_composite_is_gated_before_any_socket" in wire_tests


def test_verified_status_requires_valid_evidence_artifact(monkeypatch):
    """'verified' must ride on a VALID machine-checkable evidence artifact — a
    missing/invalid one must FAIL the gate (never bare metadata)."""
    runtime = _runtime()
    monkeypatch.setattr(CHECK, "_evidence_is_valid", lambda rt: (False, "missing artifact"))
    with pytest.raises(CHECK.IbAdapterContractError, match="evidence"):
        CHECK.check_verified_status(runtime)
    monkeypatch.setattr(CHECK, "_evidence_is_valid", lambda rt: (True, "current wire passed"))
    assert "verified" in CHECK.check_verified_status(runtime)


def test_evidence_artifact_is_code_bound_and_fail_closed(monkeypatch, tmp_path):
    """The evidence artifact only validates when it is present, exit-0,
    `test result: ok`, names the flip test, AND its code_digest matches the
    current wire code — a wire change staleness-invalidates it. A hand-authored
    or absent artifact fails closed."""
    runtime = _runtime()
    # Absent → invalid.
    monkeypatch.setattr(CHECK, "EVIDENCE_PATH", tmp_path / "nope.json")
    ok, detail = CHECK._evidence_is_valid(runtime)
    assert ok is False and "no evidence" in detail

    good = {
        "schema_version": CHECK.EVIDENCE_SCHEMA,
        "test": runtime["integration_test"]["operator_gated_test"],
        "gate_env": runtime["integration_test"]["gate_env"],
        "paper_port": runtime["integration_test"]["paper_port"],
        "pinned_server_version": runtime["pinned_server_version"],
        "returncode": 0,
        "result_line": "test result: ok. 1 passed; 0 failed",
        "code_digest": CHECK._code_digest(runtime),
        "generated_at": "2026-07-15T11:00:00Z",
    }
    artifact = tmp_path / "evidence.json"
    monkeypatch.setattr(CHECK, "EVIDENCE_PATH", artifact)

    artifact.write_text(json.dumps(good))
    ok, _ = CHECK._evidence_is_valid(runtime)
    assert ok is True

    # A stale digest (wire changed since the run) fails closed.
    stale = dict(good, code_digest="0" * 64)
    artifact.write_text(json.dumps(stale))
    ok, detail = CHECK._evidence_is_valid(runtime)
    assert ok is False and "digest" in detail

    # A forged non-passing artifact fails closed.
    forged = dict(good, returncode=1, result_line="test result: FAILED")
    artifact.write_text(json.dumps(forged))
    ok, _ = CHECK._evidence_is_valid(runtime)
    assert ok is False


def test_real_committed_evidence_matches_the_current_wire(monkeypatch):
    """The evidence artifact committed on this branch must validate against the
    CURRENT wire code — i.e. the recorded operator run proves the code that
    ships, not an older revision."""
    runtime = _runtime()
    ok, detail = CHECK._evidence_is_valid(runtime)
    assert ok, f"committed IB paper-account evidence is stale/invalid: {detail}"


# --------------------------------------------------------------------------- #
# 5. No-data market-data is NOT success; evidence trust boundary is documented
# --------------------------------------------------------------------------- #

WIRE = ROOT / "crates" / "atp-adapters" / "src" / "interactive_brokers" / "wire.rs"


def test_competing_session_no_data_is_not_reported_as_subscribe_success():
    """IB 10197 'no market data during competing live session' means the data
    stream is WITHHELD — subscribe must fail closed, never confirm. Guard against
    a regression that re-adds a 10197 -> break (success) shortcut."""
    wire = WIRE.read_text()
    # The old success-shortcut const must be gone.
    assert "IB_CODE_NO_DATA_COMPETING_SESSION" not in wire, (
        "the 10197-as-success const must not exist (no-data is not a confirmation)"
    )
    # subscribe_market_data's error arm must NOT break (confirm) on a ticker
    # error code — it must return Err. Extract the fn body and assert no
    # `code == <n> { break }` success path survives.
    start = wire.index("fn subscribe_market_data")
    end = wire.index("fn historical_data", start)
    body = wire[start:end]
    assert "break" in body  # the positive protocol-ack arms still break
    assert "== 10_197" not in body and "== 10197" not in body, (
        "subscribe must not special-case 10197 as a confirmation"
    )
    # The fake-gateway suite pins the fail-closed behavior.
    wire_tests = (ROOT / _runtime()["wire_tests"]).read_text()
    assert "subscribe_fails_closed_on_competing_live_session_no_data" in wire_tests


def test_evidence_trust_boundary_is_documented():
    """The check must document the two-mode evidence trust boundary: the
    authoritative live-execution path (ATP_RUN_INTEGRATION=1) vs the committed
    record validated in CI, resolved by operator attestation (--force-complete)."""
    check_src = (ROOT / "tools" / "ib_adapter_check.py").read_text()
    assert "AUTHORITATIVE" in check_src
    assert "ATP_RUN_INTEGRATION=1" in check_src
    assert "force-complete" in check_src


def test_pinned_server_version_metadata_matches_the_rust_const():
    """The negotiated server protocol version in the runtime metadata must equal
    the Rust IB_PINNED_SERVER_VERSION — the public contract cannot drift from the
    version the handshake actually pins. And it is DISTINCT from the IB API
    package version (protocol_version), so the two never contradict."""
    runtime = _runtime()
    wire = WIRE.read_text()
    module = (ROOT / runtime["module"]).read_text()
    import re

    m = re.search(r"IB_PINNED_SERVER_VERSION: i32 = (\d+);", module + wire)
    assert m, "IB_PINNED_SERVER_VERSION const not found"
    assert int(m.group(1)) == int(runtime["pinned_server_version"]), (
        "metadata pinned_server_version drifted from the Rust wire pin"
    )
    # The package version (in the separate adapter_contract.interactive_brokers
    # block) and the server protocol version are different values in different
    # fields, so they can never be confused/contradict.
    config = json.loads((ROOT / "architecture" / "runtime_services.json").read_text())
    package_version = config["adapter_contract"]["interactive_brokers"]["protocol_version"]
    assert str(runtime["pinned_server_version"]) != package_version


def test_check_catches_drifted_pinned_server_version(monkeypatch):
    """If the metadata's pinned_server_version disagrees with the Rust const, the
    check must FAIL (drift is never silently accepted)."""
    runtime = dict(_runtime(), pinned_server_version=999)
    monkeypatch.setattr(CHECK, "_evidence_is_valid", lambda rt: (True, "ok"))
    with pytest.raises(CHECK.IbAdapterContractError, match="drift"):
        CHECK.check_verified_status(runtime)


def test_transport_fault_derives_from_the_connectivity_classifier():
    """`is_transport_fault` (which decides whether a failed op drops the cached
    session for reconnect) must DERIVE the connectivity set from
    classify_ib_order_error, not a hand-maintained code list — otherwise a
    connectivity code (1100/2110) can be classified CONNECTIVITY_BLOCKED yet
    fail to drop the stale socket. Guard against a regression to a literal list."""
    wire = WIRE.read_text()
    # Both the session-drop decision AND the notice filter must derive the
    # connectivity set from the classifier (via the shared is_connectivity_fault
    # helper), so a connectivity fault can neither be reused (stale socket) nor
    # masked as a benign farm notice (e.g. 2110 in the 2100-2169 range).
    fault_body = wire[wire.index("fn is_transport_fault") :][:400]
    assert "is_connectivity_fault" in fault_body, (
        "is_transport_fault must delegate to the shared connectivity classifier"
    )
    helper = wire[wire.index("fn is_connectivity_fault") :][:400]
    assert "classify_ib_order_error" in helper and "ConnectivityBlocked" in helper
    notice_body = wire[wire.index("fn is_informational_notice") :][:500]
    assert "is_connectivity_fault" in notice_body, (
        "a connectivity fault must never be skipped as an informational notice"
    )
    # The fake-gateway suite proves the behavior end-to-end (1100 + 2110 -> reconnect).
    wire_tests = (ROOT / _runtime()["wire_tests"]).read_text()
    assert "connectivity_loss_errors_drop_the_session_and_reconnect" in wire_tests
