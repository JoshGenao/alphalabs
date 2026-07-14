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
  * the live transport (``TcpIbGateway``) keeps its explicit connect deadline and
    the ``IB_CODE_LIVE_WIRE_PROTOCOL_PENDING`` sentinel (now only the SRS-EXE-004
    composite wire returns it — never a fabricated result), and its TWS wire
    encoders match the ibapi golden vectors (the fake-gateway suite is RUN here);
  * the operator-gated IB paper-account integration test exists, is ``#[ignore]``,
    and is guarded by ``ATP_RUN_INTEGRATION`` (SyRS SYS-2e) — the evidence behind
    the declared ``verified`` status.

Mirrors the PASS/FAIL output style of ``tools/adapter_check.py``.

Invoke:
    python3 tools/ib_adapter_check.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"

#: Machine-checkable evidence that the operator-run IB paper-account round trip
#: passed. Written by ``check_cargo_smoke`` under ``ATP_RUN_INTEGRATION=1`` from
#: the ACTUAL passing run (never hand-authored), and bound to the current wire
#: code by a digest so any change to the encodings staleness-invalidates it.
#:
#: Trust boundary (two modes):
#:   * AUTHORITATIVE — ``ATP_RUN_INTEGRATION=1``: this tool EXECUTES the ignored
#:     paper_account_round_trip against the live IB paper account and (re)writes
#:     this artifact from the observed result. This path cannot be self-attested
#:     — it actually connects to IB Gateway and runs the six operations.
#:   * RECORD — no env (CI, where no gateway exists): this tool validates the
#:     committed artifact and that its digest still matches the shipped wire
#:     code. A committed record is, like any file, editable by someone with
#:     commit access; its trust therefore derives from the operator having run
#:     the authoritative path and from the ``integrate --force-complete``
#:     operator attestation at flip time — the same human-authorization boundary
#:     every operator-gated (SyRS SYS-2e) feature in this repo lands on.
EVIDENCE_PATH = ROOT / "architecture" / "ib_paper_account_evidence.json"
#: Schema version of the evidence artifact.
EVIDENCE_SCHEMA = 1


def _code_digest(runtime: dict, root: Path = ROOT) -> str:
    """SHA-256 over the code whose live correctness the paper-account round trip
    proves — the wire codec, the transport module, and the integration test.
    A change to any of them invalidates recorded evidence until it is re-run."""
    module = runtime["module"]
    files = [
        module,
        module.replace(".rs", "/wire.rs"),
        runtime["integration_test"]["path"],
    ]
    hasher = hashlib.sha256()
    for rel in files:
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update((root / rel).read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


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
    # account_status + positions (API-5) must be OVERRIDDEN, not left to inherit the
    # default NotConfigured — else the adapter advertises a brokerage capability it
    # fails at runtime.
    for method in ("account_status", "positions"):
        if f"fn {method}" not in impl_block:
            fail(f"{adapter} must implement {method} (API-5), not inherit NotConfigured")
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
    # The half-built live socket scaffold must be behind the non-default feature so
    # the default public surface never advertises it (it cannot be verified solo).
    feature = runtime["live_transport_feature"]
    if not re.search(
        rf'#\[cfg\(feature = "{re.escape(feature)}"\)\]\s*#\[derive\(Debug\)\]\s*'
        rf"pub struct {re.escape(struct)}\b",
        source,
    ):
        fail(f"{struct} must be gated behind the non-default `{feature}` feature")
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


def _load_evidence() -> dict | None:
    """Load the paper-account evidence artifact, or None if absent/unreadable."""
    if not EVIDENCE_PATH.exists():
        return None
    try:
        return json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _write_evidence(runtime: dict, result_line: str, timestamp: str) -> None:
    """Record the passing operator run as a structured, code-bound artifact.

    Called ONLY from the operator path (``ATP_RUN_INTEGRATION=1``) after the
    live round trip returned ``test result: ok`` — never hand-authored. The
    ``code_digest`` binds the evidence to the exact wire code that produced it.
    """
    evidence = {
        "schema_version": EVIDENCE_SCHEMA,
        "test": runtime["integration_test"]["operator_gated_test"],
        "gate_env": runtime["integration_test"]["gate_env"],
        "paper_port": runtime["integration_test"]["paper_port"],
        "pinned_server_version": runtime["pinned_server_version"],
        "returncode": 0,
        "result_line": result_line,
        "code_digest": _code_digest(runtime),
        "generated_at": timestamp,
    }
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def _evidence_is_valid(runtime: dict) -> tuple[bool, str]:
    """Whether the recorded evidence artifact proves the CURRENT wire code passed
    the operator round trip: present, well-formed, exit 0, ``test result: ok``,
    the right test, and a ``code_digest`` matching the current wire code (so a
    later wire change staleness-invalidates it)."""
    evidence = _load_evidence()
    if evidence is None:
        return False, f"no evidence artifact at {EVIDENCE_PATH.name}"
    if evidence.get("schema_version") != EVIDENCE_SCHEMA:
        return False, "evidence schema_version mismatch"
    if evidence.get("returncode") != 0:
        return False, "evidence returncode is not 0"
    if "test result: ok" not in str(evidence.get("result_line", "")):
        return False, "evidence result_line does not show `test result: ok`"
    if evidence.get("test") != runtime["integration_test"]["operator_gated_test"]:
        return False, "evidence names a different test than the operator flip gate"
    if evidence.get("code_digest") != _code_digest(runtime):
        return False, (
            "evidence code_digest does not match the current wire code — the wire "
            "changed since the operator run; re-run under ATP_RUN_INTEGRATION=1"
        )
    return True, "current wire code passed the operator paper-account round trip"


def check_verified_status(runtime: dict) -> str:
    """The runtime's declared status must be honest in BOTH directions: 'verified'
    only rides on the operator-run IB paper-account round trip having passed, and
    the remaining operator-gated work (the SRS-EXE-004 composite wire, EXE-007
    version-upgrade regression) must stay enumerated in deferred[] — a verified
    adapter must not silently over-claim the still-pending combo/BAG wire."""
    if runtime.get("status") != "verified":
        fail(
            "ib_brokerage_runtime.status must be 'verified' (the operator IB paper-account "
            "round trip passed); anything else means the wire evidence regressed"
        )
    pinned = runtime.get("pinned_server_version")
    if not pinned:
        fail("ib_brokerage_runtime must declare pinned_server_version (the TWS wire pin)")
    # The metadata's negotiated server protocol version must MATCH the Rust
    # source of truth (`IB_PINNED_SERVER_VERSION`), so the public contract can
    # never drift from the version the handshake actually pins.
    wire = read_source(runtime["module"].replace(".rs", "/wire.rs"))
    src = read_source(runtime["module"]) + wire
    match = re.search(r"IB_PINNED_SERVER_VERSION: i32 = (\d+);", src)
    if not match:
        fail("could not find IB_PINNED_SERVER_VERSION in the adapter source")
    if int(match.group(1)) != int(pinned):
        fail(
            f"pinned_server_version {pinned} in the metadata disagrees with the Rust "
            f"IB_PINNED_SERVER_VERSION={match.group(1)} — the wire pin and the public "
            f"contract must not drift"
        )
    # 'verified' must ride on a MACHINE-CHECKABLE evidence artifact bound to the
    # current wire code — never hand-editable prose. The artifact is written by
    # the operator run itself (ATP_RUN_INTEGRATION=1, check_cargo_smoke) and its
    # code_digest must still match, so a later wire change forces a re-run.
    ok, detail = _evidence_is_valid(runtime)
    if not ok:
        fail(
            f"status is 'verified' but the paper-account evidence is not valid: {detail}. "
            f"Run `ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py` against the IB "
            f"paper account (port 4002) to (re)generate {EVIDENCE_PATH.relative_to(ROOT)}"
        )
    if not runtime.get("deferred"):
        fail("ib_brokerage_runtime must enumerate its remaining deferred (operator-gated) work")
    joined = " ".join(runtime["deferred"]).lower()
    if "operator-initiated" not in joined or "paper-account" not in joined:
        fail(
            "deferred[] must keep naming the remaining operator-initiated IB paper-account "
            "work (SRS-EXE-004 composite wire, SRS-EXE-007 upgrade regression)"
        )
    if "composite" not in joined:
        fail("deferred[] must name the still-pending SRS-EXE-004 composite (combo/BAG) wire")
    return (
        f"status verified (pinned server version {runtime['pinned_server_version']}); "
        f"{detail}; {len(runtime['deferred'])} remaining deferred items documented"
    )


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
    # Also COMPILE the feature-gated integration test target (the operator flip gate
    # paper_account_round_trip lives behind the feature). `cargo build` would not
    # compile the test target, so use `cargo test --no-run` — this catches the gated
    # scaffold + test bit-rotting without running it (it binds the fixed IB port).
    feature = runtime["live_transport_feature"]
    test_name = runtime["integration_test"]["path"].split("/")[-1].removesuffix(".rs")
    build = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-adapters",
            "--test",
            test_name,
            "--features",
            feature,
            "--no-run",
            "--quiet",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        fail(f"cargo test --features {feature} --no-run failed:\n{build.stdout}{build.stderr}")
    # RUN the fake-gateway wire suite (ephemeral loopback ports — parallel-safe):
    # the golden vectors pin the encoder to the ibapi layout at the pinned server
    # version, so wire drift fails this gate before any paper-account run.
    wire_name = runtime["wire_tests"].split("/")[-1].removesuffix(".rs")
    wire = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-adapters",
            "--test",
            wire_name,
            "--features",
            feature,
            "--quiet",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if wire.returncode != 0 or "test result: ok" not in wire.stdout + wire.stderr:
        fail(
            f"cargo test {wire_name} (fake-gateway wire suite) failed:\n{wire.stdout}{wire.stderr}"
        )
    # OPERATOR MODE: when the operator env gate is set, this check RUNS the
    # acceptance-critical paper-account round trip itself — so under
    # ATP_RUN_INTEGRATION=1 a PASS from this tool IS the live IB evidence, not
    # a metadata attestation. Without the env (parallel agents, CI) the live
    # leg is skipped by design (fixed shared port 4002; SyRS SYS-2e).
    live_note = "live paper leg SKIPPED (set ATP_RUN_INTEGRATION=1 to run it)"
    spec = runtime["integration_test"]
    if os.environ.get(spec["gate_env"]) == "1":
        test_name = spec["path"].split("/")[-1].removesuffix(".rs")
        live = subprocess.run(
            [
                cargo,
                "test",
                "-p",
                "atp-adapters",
                "--test",
                test_name,
                "--features",
                feature,
                "--",
                "--ignored",
                spec["operator_gated_test"],
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        combined_live = live.stdout + live.stderr
        if live.returncode != 0 or "test result: ok" not in combined_live:
            fail(
                f"OPERATOR paper-account round trip ({spec['operator_gated_test']}) failed:\n"
                f"{combined_live}"
            )
        # Record the passing run as the machine-checkable, code-bound evidence
        # artifact check_verified_status requires (never hand-authored).
        result_line = next(
            (ln.strip() for ln in combined_live.splitlines() if "test result: ok" in ln),
            "test result: ok",
        )
        import datetime as _dt

        _write_evidence(
            runtime,
            result_line,
            _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        live_note = (
            f"OPERATOR paper-account round trip GREEN ({spec['operator_gated_test']}); "
            f"evidence written to {EVIDENCE_PATH.relative_to(ROOT)}"
        )
    return (
        f"cargo test boundary suite ok + feature-gated flip target compiles + "
        f"wire golden suite ok (--features {feature}); {live_note}"
    )


CHECKS = (
    ("transport seam", lambda cfg, rt, src: check_transport_trait(rt, src)),
    ("error classification", lambda cfg, rt, src: check_classifier_maps_every_code(rt, src)),
    ("canonical boundary", lambda cfg, rt, src: check_canonical_boundary(rt, src)),
    ("provider bridge", lambda cfg, rt, src: check_provider_bridge(rt, src)),
    ("config fail-closed", lambda cfg, rt, src: check_config_fails_closed(rt, src)),
    ("live transport fail-closed", lambda cfg, rt, src: check_live_transport_fails_closed(rt, src)),
    ("integration harness", lambda cfg, rt, src: check_integration_test(rt)),
    # cargo smoke runs BEFORE verified status: under ATP_RUN_INTEGRATION=1 it
    # executes the live round trip and (re)writes the evidence artifact that
    # verified status then validates. In CI (no env) it proves the fake-gateway
    # wire suite and verified status validates the committed artifact's digest.
    ("cargo smoke", lambda cfg, rt, src: check_cargo_smoke(rt)),
    ("verified status", lambda cfg, rt, src: check_verified_status(rt)),
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
