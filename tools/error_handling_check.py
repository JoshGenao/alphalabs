#!/usr/bin/env python3
"""Contract evidence script for feature ERR-1.

Verifies that the structured-error vocabulary and the synchronous live-path
rejection declared in ``architecture/runtime_services.json``
(block ``error_handling_contract``) are reachable from the Rust crates
``crates/atp-types`` and ``crates/atp-execution``.

ERR-1 traces SRS-EXE-001 + SRS-ERR-001 (SyRS SYS-1 / SYS-64 / AC-15). The
contract guarantees: (a) ``StrategyMode``, ``OrderErrorCategory``, and
``StructuredOrderError`` are declared in ``atp-types`` with the exact
shape SyRS SYS-64 names; (b) ``ExecutionEngine::submit_live_order`` returns
``Result<OrderReceipt, StructuredOrderError>``; (c) the brokerage port is
called ONLY inside the ``StrategyMode::Live`` arm — so a Paper submission
can never produce an IB order side effect.

Mirrors the PASS/FAIL output style of ``tools/historical_data_check.py``.

Invoke:
    python3 tools/error_handling_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _match_arm, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class ErrorHandlingCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ErrorHandlingCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def err_block(config: dict) -> dict:
    if "error_handling_contract" not in config:
        fail("architecture metadata is missing error_handling_contract")
    return config["error_handling_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = err_block(config)
    crate_path = root / block["types_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def execution_source(config: dict, root: Path = ROOT) -> str:
    block = err_block(config)
    crate_path = root / block["execution_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"execution crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def execution_cargo_source(config: dict, root: Path = ROOT) -> str:
    block = err_block(config)
    cargo_path = root / block["execution_crate"]["path"] / "Cargo.toml"
    if not cargo_path.exists():
        fail(f"execution crate Cargo.toml missing: {cargo_path.relative_to(root)}")
    return cargo_path.read_text(encoding="utf-8")


def cli_source(config: dict, root: Path = ROOT) -> str:
    block = err_block(config)
    if "cli" not in block:
        fail("error_handling_contract is missing the cli sub-block")
    source_path = root / block["execution_crate"]["path"] / block["cli"]["bin_path"]
    if not source_path.exists():
        fail(f"error-envelope CLI source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_strategy_mode_enum(config: dict, types_src: str) -> str:
    block = err_block(config)
    spec = block["strategy_mode"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"variants ({', '.join(spec['variants'])}) — single-live-strategy "
        "designation (SRS-EXE-001 / SyRS AC-15)"
    )


def check_error_category_enum(config: dict, types_src: str) -> str:
    block = err_block(config)
    spec = block["error_category"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    # Wire strings must appear (the as_str() match arms encode them).
    missing_wire = [
        wire for wire in spec["wire_strings"] if f'"{wire}"' not in types_src
    ]
    if missing_wire:
        fail(
            f"{spec['enum']}::as_str() is missing SyRS SYS-64 wire string(s): "
            f"{', '.join(missing_wire)}"
        )
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        "SyRS SYS-64 categories including NonLiveStrategySubmission, each "
        "mapped to its upper-snake wire string"
    )


def check_structured_error_struct(config: dict, types_src: str) -> str:
    block = err_block(config)
    spec = block["structured_error"]
    body = _struct_body(types_src, spec["struct"])
    missing = [
        field
        for field in spec["required_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    leaks = [
        field
        for field in spec["forbidden_fields"]
        if re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if leaks:
        fail(
            f"{spec['struct']} leaks broker/vendor field(s): {', '.join(leaks)} "
            "(SRS-ERR-001 requires exactly category + error_type + message + "
            "original_order)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} SRS-ERR-001 fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/vendor fields"
    )


def check_submit_live_order_signature(config: dict, exec_src: str) -> str:
    block = err_block(config)
    entry = block["entry_point"]
    fn_match = re.search(
        rf"\bpub\s+fn\s+{re.escape(entry['method'])}\s*<[^>]*>\s*\([^)]*\)\s*->\s*"
        rf"{re.escape(entry['result'])}",
        exec_src,
        re.DOTALL,
    )
    if not fn_match:
        fail(
            f"{entry['type']}::{entry['method']} does not return "
            f"{entry['result']} (ERR-1 / SRS-ERR-001)"
        )
    return (
        f"atp-execution declares {entry['type']}::{entry['method']} -> "
        f"{entry['result']} (SRS-EXE-001 / SRS-ERR-001 entry point)"
    )


def check_synchronous_rejection_has_no_broker_side_effect(
    config: dict, exec_src: str
) -> str:
    block = err_block(config)
    entry = block["entry_point"]
    body = _fn_block(exec_src, entry["method"])

    # The brokerage port may only be called inside the StrategyMode::Live arm.
    live_arm = _match_arm(body, "StrategyMode::Live")
    paper_arm = _match_arm(body, "StrategyMode::Paper")
    call_token = entry["live_only_call"] + "("

    if call_token not in live_arm:
        fail(
            f"{entry['method']} does not call `{call_token}` inside the "
            "StrategyMode::Live arm — live submissions would never reach the broker"
        )
    if call_token in paper_arm:
        fail(
            f"{entry['method']} calls `{call_token}` inside the "
            "StrategyMode::Paper arm — ERR-1 requires zero IB side effect for "
            "non-live submissions"
        )
    # Defensive: no call site outside the match arms either.
    arm_free = body.replace(live_arm, "").replace(paper_arm, "")
    if call_token in arm_free:
        fail(
            f"{entry['method']} calls `{call_token}` outside the mode match — "
            "ERR-1 requires the call site to be gated on StrategyMode::Live"
        )
    # Paper arm must produce a NonLiveStrategySubmission error.
    rejection = block["rejection_categories"][0]
    if f"OrderErrorCategory::{rejection}" not in paper_arm:
        fail(
            f"StrategyMode::Paper arm of {entry['method']} does not raise "
            f"OrderErrorCategory::{rejection}"
        )
    return (
        f"atp-execution::{entry['method']} calls `{call_token}` ONLY inside "
        f"the StrategyMode::Live arm; Paper submissions raise "
        f"OrderErrorCategory::{rejection} with zero broker side effect (ERR-1)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = err_block(config)
    crate = block["execution_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
    result = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"cargo test -p {crate} failed:\n{result.stdout}\n{result.stderr}")
    combined = result.stdout + result.stderr
    if "test result: ok" not in combined and "0 failed" not in combined:
        fail(f"cargo test output did not include `test result: ok`:\n{combined}")
    # Also smoke the L7 domain integration test.
    integ = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            crate,
            "--test",
            "err_1_no_ib_side_effect",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test err_1_no_ib_side_effect failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    # Also smoke the L5 operator-CLI integration test (the err001_error_envelope_cli surface).
    cli_l5 = block["cli"]["l5_test"]
    cli = subprocess.run(
        [cargo, "test", "-p", crate, "--test", cli_l5, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if cli.returncode != 0:
        fail(f"cargo test -p {crate} --test {cli_l5} failed:\n{cli.stdout}\n{cli.stderr}")
    return (
        f"cargo test -p {crate} --lib + err_1_no_ib_side_effect + {cli_l5}: PASS "
        "(synchronous rejection + zero broker side effect + operator-CLI envelope proofs verified)"
    )


def check_error_cli(config: dict, cli_src: str, root: Path = ROOT) -> str:
    """The operator binary that makes the SRS-ERR-001 acceptance criterion demonstrable.

    Verifies the bin is Cargo-registered, exposes its subcommands, drives the REAL execution engine
    (so the envelope proofs run over the real ``submit_live_order``, not a hand-rolled echo that could
    agree with itself), prints each ``:true`` proof headline, carries the fail-closed ``--inject``
    non-vacuity path, and is backed by the L5 integration test.
    """
    block = err_block(config)
    if "cli" not in block:
        fail("error_handling_contract is missing the cli sub-block")
    spec = block["cli"]

    # Cargo-registered (without the [[bin]], the operator surface does not build).
    cargo = execution_cargo_source(config, root)
    if f'name = "{spec["bin_name"]}"' not in cargo:
        fail(
            f"Cargo.toml must register the operator binary `{spec['bin_name']}` — without it the "
            "SRS-ERR-001 error-envelope operator surface does not build"
        )

    missing_cmds = [c for c in spec["subcommands"] if f'"{c}"' not in cli_src]
    if missing_cmds:
        fail(f"{spec['bin_name']} is missing subcommand(s): {', '.join(missing_cmds)}")

    # Drives the REAL execution engine — the envelope proof must run over the real
    # ExecutionEngine::submit_live_order, not a hand-rolled stand-in that could agree with itself.
    missing_engine = [t for t in spec["engine_tokens"] if t not in cli_src]
    if missing_engine:
        fail(
            f"{spec['bin_name']} must drive the real execution engine (missing "
            f"{', '.join(missing_engine)}) so the envelope proofs are genuine (SRS-ERR-001)"
        )

    # Each `:true` proof headline the acceptance criterion turns on (category vocabulary, envelope
    # completeness, no-broker side effect).
    missing_proofs = [t for t in spec["proof_tokens"] if t not in cli_src]
    if missing_proofs:
        fail(
            f"{spec['bin_name']} must print every proof headline (missing "
            f"{', '.join(missing_proofs)}) — the category / envelope / no-broker acceptance halves"
        )

    # An injected fault fails closed before any proof (no fabricated proof on a success path).
    if spec["fail_closed_token"] not in cli_src:
        fail(
            f"{spec['bin_name']} must fail closed on an injected fault "
            f"(`{spec['fail_closed_token']}`) so a success path never produces a reject proof"
        )

    # Backed by the L5 integration test.
    l5_path = root / block["execution_crate"]["path"] / "tests" / f"{spec['l5_test']}.rs"
    if not l5_path.exists():
        fail(f"missing L5 integration test {l5_path.relative_to(root)}")

    return (
        f"operator binary {spec['bin_name']} is Cargo-registered, exposes "
        f"{', '.join(spec['subcommands'])}, drives the REAL execution engine "
        f"({', '.join(spec['engine_tokens'])}), prints {', '.join(spec['proof_tokens'])}, fails "
        f"closed on an injected fault, and is driven in fresh processes by the L5 {spec['l5_test']}"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("strategy_mode", check_strategy_mode_enum, "types"),
    ("error_category", check_error_category_enum, "types"),
    ("structured_error", check_structured_error_struct, "types"),
    ("entry_point", check_submit_live_order_signature, "execution"),
    ("synchronous_rejection", check_synchronous_rejection_has_no_broker_side_effect, "execution"),
    ("error_cli", check_error_cli, "cli"),
)


def run_checks() -> list[str]:
    config = load_config()
    sources = {
        "types": types_source(config),
        "execution": execution_source(config),
        "cli": cli_source(config),
    }
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        evidence.append(check(config, sources[scope]))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_error_handling_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    sources = {
        "types": types_source(config, root),
        "execution": execution_source(config, root),
        "cli": cli_source(config, root),
    }
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        evidence.append(check(config, sources[scope]))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERR-1 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except ErrorHandlingCheckError as error:
        print(f"ERR-1 FAIL: {error}", file=sys.stderr)
        return 1

    print("ERR-1 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
