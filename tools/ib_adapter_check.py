#!/usr/bin/env python3
"""Contract evidence for SRS-EXE-006 — the IB Gateway brokerage adapter runtime.

The interface-level adapter surface (BrokerageAdapter / version discovery) is
covered by ``tools/adapter_check.py`` (API-5). This check verifies the *behavior*
SRS-EXE-006 adds in ``crates/atp-adapters/src/interactive_brokers.rs``:

  * the deterministic IB-error -> SyRS SYS-64 classifier maps every documented
    code in the contract's ``mapped_categories`` onto the declared category;
  * the runtime is reachable through the CANONICAL ``BrokerageAdapter`` /
    ``MarketDataAdapter`` / ``HistoricalDataAdapter`` traits (SYS-52) and every
    failure flows through the common ``AdapterError::Brokerage`` taxonomy (never a
    parallel bespoke surface, never a dropped rejection);
  * brokerage configuration FAILS CLOSED on a malformed ``ATP_IB_*`` port rather
    than silently defaulting to an unintended endpoint;
  * the live transport scaffold (``TcpIbGateway``) fails closed via the
    ``IB_CODE_LIVE_WIRE_PROTOCOL_PENDING`` sentinel (with an explicit connect
    timeout) rather than fabricating a result; and
  * the operator-gated IB paper-account integration test exists, is ``#[ignore]``,
    and is guarded by ``ATP_RUN_INTEGRATION`` (SyRS SYS-2e) — the gate that flips
    SRS-EXE-006 to ``passes:true``.

Mirrors the PASS/FAIL output style of ``tools/adapter_check.py``.

Invoke:
    python3 tools/ib_adapter_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class IbAdapterContractError(AssertionError):
    pass


def fail(message: str) -> None:
    raise IbAdapterContractError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def ib_runtime(config: dict) -> dict:
    adapter = config.get("adapter_contract")
    if not adapter or "ib_brokerage_runtime" not in adapter:
        fail("architecture metadata is missing adapter_contract.ib_brokerage_runtime")
    return adapter["ib_brokerage_runtime"]


def read_source(rel_path: str) -> str:
    path = ROOT / rel_path
    if not path.exists():
        fail(f"SRS-EXE-006 source missing: {rel_path}")
    return path.read_text(encoding="utf-8")


def _block(source: str, header_regex: str, label: str) -> str:
    """Return a brace-balanced block whose opening line matches ``header_regex``."""
    match = re.search(header_regex, source)
    if not match:
        fail(f"could not find {label} (pattern {header_regex!r})")
    start = source.index("{", match.start())
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    fail(f"unbalanced braces while reading {label}")
    return ""  # unreachable


def check_transport_trait(runtime: dict, source: str) -> str:
    trait = runtime["transport_trait"]
    block = _block(source, rf"pub trait {re.escape(trait)}\b", f"trait {trait}")
    missing = [m for m in runtime["transport_methods"] if f"fn {m}" not in block]
    if missing:
        fail(f"transport trait {trait} is missing method(s): {', '.join(missing)}")
    return f"transport trait {trait} exposes {len(runtime['transport_methods'])} AC operations"


def check_classifier_maps_every_code(runtime: dict, source: str) -> str:
    classifier = runtime["classifier_fn"]
    block = _block(source, rf"pub fn {re.escape(classifier)}\b", f"fn {classifier}")
    # Resolve the documented `IB_CODE_* = <n>` constants so we can assert the
    # classifier arms reference the exact codes the contract declares.
    code_consts = dict(re.findall(r"pub const (IB_CODE_[A-Z_]+): i32 = (-?\d+);", source))
    const_by_value = {int(v): name for name, v in code_consts.items()}

    results = []
    for category, codes in runtime["mapped_categories"].items():
        for code in codes:
            const_name = const_by_value.get(code)
            if const_name is None:
                fail(
                    f"contract maps code {code} ({category}) but no IB_CODE_* constant has that value"
                )
            if const_name not in block:
                fail(
                    f"classifier {classifier} does not reference {const_name} "
                    f"(code {code} for {category})"
                )
        results.append(category)
    # Every SYS-64 broker-validation category SRS-ERR-001 needs must be covered.
    required = {
        "INVALID_SYMBOL",
        "INSUFFICIENT_BUYING_POWER",
        "RATE_LIMITED",
        "CONNECTIVITY_BLOCKED",
    }
    covered = set(results)
    if not required.issubset(covered):
        fail(
            f"classifier must map the SYS-64 broker categories; missing: {sorted(required - covered)}"
        )
    return f"classifier maps {len(covered)} SYS-64 categories from documented IB codes"


def check_canonical_boundary(runtime: dict, source: str) -> str:
    """The runtime must be reachable through the CANONICAL adapter traits (SYS-52),
    with failures flowing through the common AdapterError taxonomy — not a parallel
    bespoke surface. Verify the adapter implements every canonical trait and maps
    IB failures via the mapper fn onto AdapterError::<variant> (never dropped)."""
    adapter = runtime["adapter_struct"]
    variant = runtime["common_taxonomy_variant"]
    mapper = runtime["mapper_fn"]
    for trait in runtime["canonical_traits"]:
        if f"impl<C: IbGatewayConnection> {trait} for {adapter}<C>" not in source:
            fail(f"{adapter} must implement the canonical {trait} trait (SYS-52 adapter interface)")
    # The mapper is the single point IB errors cross into the common taxonomy.
    mapper_block = _block(source, rf"fn {re.escape(mapper)}\b", f"fn {mapper}")
    if f"AdapterError::{variant}" not in mapper_block:
        fail(f"{mapper} must map IB failures onto AdapterError::{variant} (common taxonomy)")
    for field in ("category:", "code:", "message:"):
        if field not in mapper_block:
            fail(
                f"AdapterError::{variant} via {mapper} must carry {field} (never drop the failure)"
            )
    # The canonical submit_order must return AdapterResult, not a raw IB error.
    impl_block = _block(
        source,
        rf"impl<C: IbGatewayConnection> BrokerageAdapter for {adapter}<C>",
        "BrokerageAdapter impl",
    )
    if "-> AdapterResult<OrderReceipt>" not in impl_block:
        fail(
            f"{adapter}::submit_order must return AdapterResult<OrderReceipt> (canonical boundary)"
        )
    if "IbApiError" in impl_block:
        fail(f"{adapter} canonical trait methods must not leak raw IbApiError past the seam")
    return (
        f"{adapter} implements {len(runtime['canonical_traits'])} canonical adapter traits; "
        f"failures flow through AdapterError::{variant} via {mapper} (never dropped)"
    )


def check_provider_bridge(runtime: dict, source: str) -> str:
    """The documented zero-config provider (the API-5 provider_struct) must bridge
    to the FUNCTIONAL runtime — otherwise a caller following the documented adapter
    contract reaches an inert NotConfigured stub. Verify the bridge constructor on
    the discovery struct produces the functional adapter."""
    discovery = runtime["discovery_struct"]
    adapter = runtime["adapter_struct"]
    bridge = runtime["bridge_method"]
    impl_block = _block(source, rf"impl {re.escape(discovery)}\b", f"impl {discovery}")
    if f"fn {bridge}" not in impl_block:
        fail(f"{discovery} must expose `{bridge}` to construct the functional runtime")
    if f"-> {adapter}<" not in impl_block:
        fail(f"{discovery}::{bridge} must return the functional {adapter} (discovery -> runtime)")
    return f"documented provider {discovery} bridges to the functional {adapter} via {bridge}"


def check_live_transport_fails_closed(runtime: dict, source: str) -> str:
    struct = runtime["live_transport_struct"]
    sentinel = runtime["live_wire_pending_sentinel"]
    if f"pub struct {struct}" not in source:
        fail(f"live transport struct {struct} is missing")
    if f"pub const {sentinel}" not in source:
        fail(f"live-wire sentinel const {sentinel} is missing")
    # The sentinel must be negative so it can never collide with a real IB code.
    match = re.search(rf"pub const {re.escape(sentinel)}: i32 = (-?\d+);", source)
    if not match or int(match.group(1)) >= 0:
        fail(f"{sentinel} must be a negative sentinel (never a real IB code)")
    if f"impl IbGatewayConnection for {struct}" not in source:
        fail(f"{struct} must implement IbGatewayConnection")
    # The IB-touching socket establishment must use an EXPLICIT timeout budget —
    # never the unbounded OS default — so a black-holed Gateway cannot hang the
    # live path. Assert both the timeout const and connect_timeout are wired in.
    timeout_const = runtime["connect_timeout_const"]
    # Scope to the live transport's own inherent impl so a same-named bridge method
    # elsewhere (e.g. the discovery struct's `connect`) cannot satisfy this check.
    impl_block = _block(source, rf"impl {re.escape(struct)}\b", f"impl {struct}")
    connect_block = _block(impl_block, r"pub fn connect\b", f"{struct}::connect")
    if "connect_timeout" not in connect_block:
        fail(f"{struct}.connect must use TcpStream::connect_timeout (explicit deadline)")
    if timeout_const not in connect_block:
        fail(f"{struct}.connect must bound the socket with {timeout_const}")
    if "set_read_timeout" not in connect_block or "set_write_timeout" not in connect_block:
        fail(f"{struct}.connect must set read/write timeouts so a half-open session cannot hang")
    # No DNS step inside connect — name resolution cannot be bounded by the socket
    # deadline, so connect must use a literal SocketAddr (config endpoint), never
    # to_socket_addrs.
    if "to_socket_addrs" in connect_block:
        fail(f"{struct}.connect must not resolve names (DNS can hang outside {timeout_const})")
    return (
        f"live transport {struct} fails closed via {sentinel} (no fabricated success) "
        f"with an explicit {timeout_const} connect/read/write deadline (no DNS step)"
    )


def check_config_fails_closed(runtime: dict, source: str) -> str:
    """Brokerage configuration must FAIL CLOSED on a malformed ATP_IB_* value — a
    typo must never silently fall back to a default endpoint (an unintended IB
    Gateway). Verify the typed config error + the port parser that rejects
    non-numeric / out-of-range / zero ports."""
    error_type = runtime["config_error_type"]
    parser = runtime["config_parser_fn"]
    if f"pub struct {error_type}" not in source:
        fail(f"config error type {error_type} is missing")
    if "pub fn from_env" not in source or "Result<Self, IbConnectionConfigError>" not in source:
        fail("from_env must return Result<Self, IbConnectionConfigError> (fail closed)")
    parser_block = _block(source, rf"fn {re.escape(parser)}\b", f"fn {parser}")
    # The parser must reject port 0 and surface the typed error on a bad value.
    if "!= 0" not in parser_block and "filter" not in parser_block:
        fail(f"{parser} must reject port 0 (a zero port is not a valid endpoint)")
    if error_type not in parser_block:
        fail(f"{parser} must return {error_type} on a malformed port (never coerce to a default)")
    # The host must be a validated literal IP (no DNS), and from_env must validate it
    # at load so a hostname misconfiguration fails closed before any IB-touching call.
    if "pub fn ip(" not in source or "parse::<IpAddr>()" not in source:
        fail("config must validate ATP_IB_HOST as a literal IpAddr (no DNS resolution)")
    from_env_block = _block(source, r"pub fn from_env\b", "fn from_env")
    if ".ip()" not in from_env_block:
        fail("from_env must validate the host (config.ip()) at load — fail closed on a hostname")
    return (
        f"config fails closed on malformed ATP_IB_* ports ({error_type} via {parser}) "
        "and on a non-literal-IP host"
    )


def check_integration_test(runtime: dict) -> str:
    spec = runtime["integration_test"]
    source = read_source(spec["path"])
    test = spec["operator_gated_test"]
    if f"fn {test}" not in source:
        fail(f"operator-gated integration test {test} missing from {spec['path']}")
    # Must be ignored by default (binds the fixed IB paper port) and gated by env.
    if "#[ignore" not in source:
        fail(f"{test} must be #[ignore] (binds fixed IB paper port {spec['paper_port']})")
    if spec["gate_env"] not in source:
        fail(f"{test} must be gated by {spec['gate_env']} (SyRS SYS-2e operator-initiated)")
    # The gated test must FAIL CLOSED when explicitly invoked without the env gate —
    # an early `return` would report a vacuous green for the documented flip gate.
    test_block = _block(source, rf"fn {re.escape(test)}\b", f"fn {test}")
    if "assert" not in test_block or spec["gate_env"] not in test_block:
        fail(f"{test} must assert {spec['gate_env']} (fail closed), not silently return when unset")
    if re.search(r"!=\s*Ok\(\"1\"\)\s*\{[^}]*return", test_block):
        fail(f"{test} must not early-return on a missing {spec['gate_env']} (vacuous pass)")
    missing = [t for t in runtime["boundary_tests"] if f"fn {t}" not in source]
    if missing:
        fail(f"boundary tests missing from {spec['path']}: {', '.join(missing)}")
    return (
        f"integration harness present: 1 operator-gated test ({test}, fails closed without "
        f"{spec['gate_env']}) + {len(runtime['boundary_tests'])} solo boundary tests"
    )


def check_serialized_status(runtime: dict) -> str:
    if runtime.get("status") != "serialized":
        fail("ib_brokerage_runtime.status must be 'serialized' until the operator integration runs")
    if not runtime.get("deferred"):
        fail("ib_brokerage_runtime must enumerate its deferred (operator-gated) work")
    joined = " ".join(runtime["deferred"]).lower()
    if "operator-initiated" not in joined or "paper-account" not in joined:
        fail(
            "deferred[] must name the operator-initiated IB paper-account integration as the flip gate"
        )
    return f"status serialized; {len(runtime['deferred'])} deferred items documented"


def check_cargo_smoke(runtime: dict) -> str:
    # This gate's PASS claims the cargo boundary suite proved the real Rust binary,
    # so it must FAIL CLOSED when cargo is absent — a skip would make the evidence
    # vacuous. SRS-EXE-006 lives in a Rust crate; cargo is a hard requirement here.
    cargo = shutil.which("cargo")
    if cargo is None:
        fail(
            "cargo is not on PATH — the SRS-EXE-006 boundary suite cannot be proven; "
            "this gate requires the Rust toolchain (run from the worktree where init.sh built it)"
        )
    result = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-adapters",
            "--test",
            "srs_exe_006_ib_adapter",
            "--quiet",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    if result.returncode != 0:
        fail(f"cargo test srs_exe_006_ib_adapter failed:\n{combined}")
    if "test result: ok" not in combined:
        fail(f"cargo test output did not include `test result: ok`:\n{combined}")
    return "cargo test srs_exe_006_ib_adapter: ok (boundary suite green)"


CHECKS = (
    ("transport seam", lambda cfg, rt, src: check_transport_trait(rt, src)),
    ("error classification", lambda cfg, rt, src: check_classifier_maps_every_code(rt, src)),
    ("canonical boundary", lambda cfg, rt, src: check_canonical_boundary(rt, src)),
    ("provider bridge", lambda cfg, rt, src: check_provider_bridge(rt, src)),
    ("config fail-closed", lambda cfg, rt, src: check_config_fails_closed(rt, src)),
    ("live transport fail-closed", lambda cfg, rt, src: check_live_transport_fails_closed(rt, src)),
    ("integration harness", lambda cfg, rt, src: check_integration_test(rt)),
    ("serialized status", lambda cfg, rt, src: check_serialized_status(rt)),
    ("cargo smoke", lambda cfg, rt, src: check_cargo_smoke(rt)),
)


def run(as_json: bool = False) -> int:
    config = load_config()
    runtime = ib_runtime(config)
    source = read_source(runtime["module"])
    findings = []
    for label, check in CHECKS:
        try:
            detail = check(config, runtime, source)
        except IbAdapterContractError as err:
            if as_json:
                print(json.dumps({"status": "FAIL", "check": label, "error": str(err)}))
            else:
                print(f"FAIL [{label}]: {err}")
            return 1
        findings.append((label, detail))

    if as_json:
        print(json.dumps({"status": "PASS", "checks": dict(findings)}, indent=2))
    else:
        for label, detail in findings:
            print(f"PASS [{label}]: {detail}")
        print("SRS-EXE-006 IB ADAPTER RUNTIME PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    args = parser.parse_args()
    return run(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
