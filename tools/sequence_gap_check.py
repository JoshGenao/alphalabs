#!/usr/bin/env python3
"""Contract evidence script for feature SRS-MD-007.

Verifies that the market-data tick-sequence gap detector declared in
``architecture/runtime_services.json`` (block ``sequence_gap_contract``) is
reachable from the Rust crates ``crates/atp-types`` and
``crates/atp-market-data`` and that the SRS-LOG-001 ``SEQUENCE_GAP`` event type
is pinned in both the architecture metadata and ``python/atp_logging``.

SRS-MD-007 (SyRS SYS-39 / SYS-39a / SYS-70; NFR-P5; StRS SN-2.03 / SN-2.04) —
"The market-data subscription manager shall detect sequence gaps in IB tick
streams and reflect gap state in heartbeat/staleness." The contract guarantees:

  (a) ``SequenceGapEvent`` (atp-types) carries the four SRS-MD-007 acceptance
      fields (symbol, expected_sequence, observed_sequence, observed_at_ns) and
      leaks no broker/vendor/session/tick identifier;
  (b) the ``SequenceGapEventSink`` port (atp-market-data) exposes ``record``;
  (c) ``SequenceGapDetector`` (atp-market-data) declares the observe / resync /
      freshness / stale_since accessors;
  (d) ``observe_tick`` canonicalizes each tick via ``tick.security_key()?``
      (fail closed on empty symbol / option), marks the line Stale and
      publishes a ``SequenceGapEvent`` via ``events.record(`` on a gap, and
      restores Fresh on recovery;
  (e) ``freshness`` fails CLOSED (an unobserved security defaults to
      ``MarketDataFreshness::Stale``);
  (f) the ``GapObservation`` (4 variants) and ``ResyncOutcome`` (2 variants)
      enums, and the reused ``MarketDataFreshness`` states, are declared;
  (g) the ``SEQUENCE_GAP`` log event type is pinned under ``market_data`` in the
      JSON ``log_record_contract`` AND in ``atp_logging.EVENT_TYPES_BY_SOURCE``;
  (h) the market-data crate leaks no vendor SDK token.

Mirrors the PASS/FAIL output style of ``tools/subscription_fanout_check.py``.

Invoke:
    python3 tools/sequence_gap_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _struct_body, _trait_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
CONTRACT = "sequence_gap_contract"
LOG_CONTRACT = "log_record_contract"


class SequenceGapCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise SequenceGapCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if CONTRACT not in config:
        fail(f"architecture metadata is missing {CONTRACT}")
    return config[CONTRACT]


def types_source(config: dict, root: Path = ROOT) -> str:
    path = root / contract_block(config)["types_crate"]["path"] / "src" / "lib.rs"
    if not path.exists():
        fail(f"types crate source not found at {path}")
    return path.read_text(encoding="utf-8")


def market_data_source(config: dict, root: Path = ROOT) -> str:
    path = root / contract_block(config)["market_data_crate"]["path"] / "src" / "lib.rs"
    if not path.exists():
        fail(f"market-data crate source not found at {path}")
    return path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_sequence_gap_event(config: dict, types_src: str) -> str:
    spec = contract_block(config)["sequence_gap_event"]
    body = _struct_body(types_src, spec["struct"])
    missing = [
        f for f in spec["required_fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    leaks = [f for f in spec["forbidden_fields"] if re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)]
    if leaks:
        fail(
            f"{spec['struct']} leaks broker/vendor/tick field(s): {', '.join(leaks)} "
            "(a sequence gap is a data-side condition on the consolidated line)"
        )
    return (
        f"atp-types declares {spec['struct']} with the {len(spec['required_fields'])} "
        f"SRS-MD-007 acceptance fields ({', '.join(spec['required_fields'])}) and "
        f"rejects {len(spec['forbidden_fields'])} forbidden broker/vendor/tick fields"
    )


def check_gap_event_sink_port(config: dict, md_src: str) -> str:
    spec = contract_block(config)["gap_event_sink_port"]
    body = _trait_body(md_src, spec["trait"])
    for method in spec["methods"]:
        if not re.search(rf"\bfn\s+{re.escape(method)}\b", body):
            fail(f"trait {spec['trait']} is missing method `{method}`")
    # SRS-MD-007 makes logging first-class: the sink must be FALLIBLE so a
    # durable-write / transport failure can be surfaced (not silently swallowed).
    result = spec.get("record_result")
    if result and _compact(result) not in _compact(body):
        fail(
            f"trait {spec['trait']}::record must be fallible (`{result}`) so a "
            "failed SRS-LOG-001 / dashboard publication is surfaced, not swallowed"
        )
    return (
        f"{spec['trait']} port with {len(spec['methods'])} fallible method "
        f"({', '.join(spec['methods'])} -> {result})"
    )


def check_detector_struct(config: dict, md_src: str) -> str:
    spec = contract_block(config)["detector"]
    # The struct must exist.
    _struct_body(md_src, spec["struct"])
    methods = (
        spec["observe_method"],
        spec["resync_method"],
        spec["freshness_method"],
        spec["stale_since_method"],
    )
    for method in methods:
        if not re.search(rf"\bpub\s+fn\s+{re.escape(method)}\b", md_src):
            fail(f"{spec['struct']} is missing public method `{method}`")
    return (
        f"{spec['struct']} with the {len(methods)} sequence/staleness methods "
        f"({', '.join(methods)})"
    )


def check_observe_tick_semantics(config: dict, md_src: str) -> str:
    spec = contract_block(config)["detector"]
    body = _compact(_fn_block(md_src, spec["observe_method"]))
    if _compact(spec["canonicalize_call"]) not in body:
        fail(
            f"{spec['observe_method']} must canonicalize each tick via "
            f"`{spec['canonicalize_call']}` (fail closed on empty symbol / option)"
        )
    if _compact(spec["gap_stale_assignment"]) not in body:
        fail(f"{spec['observe_method']} must mark the line Stale on a gap")
    if _compact(spec["gap_publish_call"]) not in body:
        fail(
            f"{spec['observe_method']} must publish a SequenceGapEvent on a gap "
            f"via `{spec['gap_publish_call']}`"
        )
    if _compact(spec["recovery_fresh_assignment"]) not in body:
        fail(f"{spec['observe_method']} must restore Fresh on an in-sequence recovery")
    # stale_since_ns must be set only on the Fresh->Stale transition, so a
    # repeated gap on an already-stale line does not reset the staleness age.
    onset_token = spec.get("stale_onset_preserved_token")
    if onset_token and _compact(onset_token) not in body:
        fail(
            f"{spec['observe_method']} must guard the stale-onset timestamp with "
            f"`{onset_token}` so a repeated gap does not reset stale_since_ns"
        )
    return (
        f"{spec['observe_method']} canonicalizes + fails closed, marks Stale + "
        "publishes on a gap, recovers Fresh on a monotonic tick, and preserves the "
        "original stale-onset time across repeated gaps"
    )


def check_freshness_fail_closed(config: dict, md_src: str) -> str:
    spec = contract_block(config)["detector"]
    body = _compact(_fn_block(md_src, spec["freshness_method"]))
    if _compact(spec["fail_closed_freshness_default"]) not in body:
        fail(
            f"{spec['freshness_method']} must fail CLOSED — an unobserved security "
            f"must default to `{spec['fail_closed_freshness_default']}` so an order "
            "on a silent line is blocked"
        )
    return f"{spec['freshness_method']} fails closed (unobserved security -> Stale)"


def check_gap_observation_enum(config: dict, md_src: str) -> str:
    spec = contract_block(config)["gap_observation_enum"]
    body = _enum_body(md_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variants: {', '.join(missing)}")
    return f"{spec['enum']} with {len(spec['variants'])} outcomes ({', '.join(spec['variants'])})"


def check_resync_outcome_enum(config: dict, md_src: str) -> str:
    spec = contract_block(config)["resync_outcome_enum"]
    body = _enum_body(md_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variants: {', '.join(missing)}")
    return f"{spec['enum']} with {len(spec['variants'])} outcomes ({', '.join(spec['variants'])})"


def check_freshness_state(config: dict, types_src: str) -> str:
    spec = contract_block(config)["freshness_state"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variants: {', '.join(missing)}")
    return (
        f"reuses {spec['enum']} ({', '.join(spec['variants'])}) — the same vocabulary "
        "the SRS-MD-004 gate rejects MARKET_DATA_STALE on"
    )


def check_log_event_pinned(config: dict, root: Path = ROOT) -> str:
    spec = contract_block(config)["log_event"]
    source = spec["source"]
    event_type = spec["event_type"]
    # (g1) architecture metadata log_record_contract.
    log_block = config.get(LOG_CONTRACT)
    if not log_block:
        fail(f"architecture metadata is missing {LOG_CONTRACT}")
    json_types = log_block["event_types_by_source"].get(source, [])
    if event_type not in json_types:
        fail(
            f"{LOG_CONTRACT}.event_types_by_source[{source!r}] must include "
            f"{event_type!r}; got {json_types}"
        )
    # (g2) python/atp_logging EVENT_TYPES_BY_SOURCE (import the module).
    tools_root = str(root / "tools")
    python_root = str(root / "python")
    for entry in (python_root,):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    from atp_logging import EVENT_TYPES_BY_SOURCE, Source  # noqa: PLC0415

    py_types = EVENT_TYPES_BY_SOURCE[Source(source)]
    if event_type not in py_types:
        fail(
            f"atp_logging.EVENT_TYPES_BY_SOURCE[Source.{source.upper()}] must include "
            f"{event_type!r}; got {py_types}"
        )
    return (
        f"{event_type} log event type pinned under {source!r} in both "
        f"{LOG_CONTRACT} and atp_logging.EVENT_TYPES_BY_SOURCE"
    )


def check_vendor_isolation(config: dict, md_src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [tok for tok in tokens if tok in md_src]
    if leaked:
        fail(
            f"market-data crate leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the detector must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return f"market-data crate free of all {len(tokens)} forbidden vendor SDK tokens"


def check_feature_stays_false(config: dict, root: Path = ROOT) -> str:
    features = json.loads((root / "feature_list.json").read_text(encoding="utf-8"))
    entry = next((f for f in features if f["id"] == "SRS-MD-007"), None)
    if entry is None:
        fail("feature_list.json is missing SRS-MD-007")
    if entry["passes"] is not False:
        fail(
            "SRS-MD-007 must remain passes:false until the deferred runtime + "
            "operator surfaces land (see sequence_gap_contract.deferred[])"
        )
    return "feature_list.json keeps SRS-MD-007 passes:false (serialized; runtime deferred)"


_STATIC_CHECKS = (
    (check_sequence_gap_event, "types"),
    (check_freshness_state, "types"),
    (check_gap_event_sink_port, "market_data"),
    (check_detector_struct, "market_data"),
    (check_observe_tick_semantics, "market_data"),
    (check_freshness_fail_closed, "market_data"),
    (check_gap_observation_enum, "market_data"),
    (check_resync_outcome_enum, "market_data"),
    (check_vendor_isolation, "market_data"),
)


def assert_sequence_gap_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    md_src = market_data_source(config, root)
    evidence: list[str] = []
    for check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else md_src
        evidence.append(check(config, source))
    evidence.append(check_log_event_pinned(config, root))
    evidence.append(check_feature_stays_false(config, root))
    return evidence


def _cargo_smoke(config: dict, root: Path = ROOT) -> str:
    test_name = contract_block(config)["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo smoke skipped (cargo not on PATH): would run {test_name}"
    result = subprocess.run(
        [cargo, "test", "-p", "atp-market-data", "--test", test_name],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(
            f"cargo test -p atp-market-data --test {test_name} failed:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return f"cargo integration test {test_name} passes"


def run_checks(root: Path = ROOT) -> list[str]:
    config = load_config(root)
    evidence = assert_sequence_gap_static(config, root)
    evidence.append(_cargo_smoke(config, root))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-MD-007 sequence-gap contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except SequenceGapCheckError as error:
        print(f"SRS-MD-007 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-MD-007 SEQUENCE-GAP PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
