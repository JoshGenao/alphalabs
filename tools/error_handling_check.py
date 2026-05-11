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

from historical_data_check import _enum_body, _struct_body

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


def _fn_block(source: str, fn_name: str) -> str:
    """Return the body of ``pub fn <fn_name>`` up to its closing brace."""
    match = re.search(rf"\bpub\s+fn\s+{re.escape(fn_name)}\b[^\{{]*\{{", source)
    if not match:
        fail(f"execution crate is missing function `{fn_name}`")
    start = match.end()
    depth = 1
    index = start
    while index < len(source) and depth:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    if depth:
        fail(f"could not parse function body for `{fn_name}`")
    return source[start : index - 1]


def _match_arm(body: str, pattern: str) -> str:
    """Return the body of the match arm whose pattern matches ``pattern``.

    Looks for ``<pattern> =>`` and returns the expression up to the next
    top-level ``,`` (skipping nested braces).
    """
    arm_match = re.search(rf"{re.escape(pattern)}\s*=>\s*", body)
    if not arm_match:
        fail(f"submit_live_order is missing match arm for `{pattern}`")
    start = arm_match.end()
    depth = 0
    index = start
    in_string = False
    string_char = ""
    while index < len(body):
        char = body[index]
        if in_string:
            if char == "\\" and index + 1 < len(body):
                index += 2
                continue
            if char == string_char:
                in_string = False
        elif char in ('"', "'"):
            in_string = True
            string_char = char
        elif char == "{" or char == "(":
            depth += 1
        elif char == "}" or char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            break
        index += 1
    return body[start:index]


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
    return (
        f"cargo test -p {crate} --lib + err_1_no_ib_side_effect: PASS "
        "(synchronous rejection + zero broker side effect verified)"
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
)


def run_checks() -> list[str]:
    config = load_config()
    types_src = types_source(config)
    exec_src = execution_source(config)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else exec_src
        evidence.append(check(config, source))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_error_handling_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    exec_src = execution_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else exec_src
        evidence.append(check(config, source))
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
