#!/usr/bin/env python3
"""Contract evidence script for SRS-MD-001 (consolidated subscription fan-out).

SRS-MD-001 is the *consolidation + fan-out* half of SyRS SYS-70 (the
line-limit half is SRS-MD-002, enforced by
``tools/subscription_limit_check.py``). The acceptance criterion:
"Multiple strategies subscribing to the same security consume one IB
subscription; each subscriber receives fan-out data ...".

This script verifies the SDK-SURFACE contract declared in
``architecture/runtime_services.json`` (block
``subscription_fanout_contract``) is reachable from the Rust crates
``crates/atp-types`` and ``crates/atp-market-data``:

  (a) ``MarketDataTick`` (the source-neutral fan-out payload) and
      ``SubscriptionChangeEvent`` carry their required fields and no
      broker / vendor / session / tick-id leakage; ``SubscriptionChange``
      enumerates the six transitions.
  (b) ``ConsolidatedSubscriptionRegistry`` exposes the dedup + fan-out
      surface (subscribe / unsubscribe / fan_out / distinct_subscriptions
      / subscriber_count / is_subscribed) and the ``SubscriptionChangeSink``
      publication port + ``SubscriptionRegistryError`` fail-closed enum.
  (c) the registry *is* the concrete ``SubscriptionLineCounter`` the
      SRS-MD-002 gate consumes (closing the subscription_limit_contract
      deferral that named SRS-MD-001 as the owner of the live set).
  (d) the dedup invariant is visible in ``subscribe``: only the
      first-subscriber path inserts a new upstream subscription
      (``self.subscribers.insert(`` appears exactly once); an additional
      subscriber appends to the existing list (``existing.push(``).
  (e) the registry fails closed on empty symbol / strategy id
      (``fan_out`` and ``subscribe`` validate their inputs).
  (f) the core crate carries no vendor-SDK token.

The PASS line is ``SRS-MD-001 SDK-SURFACE PASS`` — it names the deferred
downstream owners so the partial-pass status (feature_list.json keeps
``passes:false``) is loud. The <=100 ms fan-out latency NFR, the real IB
feed, and the async fan-out transport are deferred runtime halves (see
the contract block's ``deferred[]``).

Mirrors the PASS/FAIL output style of ``tools/subscription_limit_check.py``.

Invoke:
    python3 tools/subscription_fanout_check.py
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


class SubscriptionFanoutCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise SubscriptionFanoutCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "subscription_fanout_contract" not in config:
        fail("architecture metadata is missing subscription_fanout_contract")
    return config["subscription_fanout_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["types_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def market_data_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["market_data_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"market-data crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _impl_block(source: str, header_regex: str, label: str) -> str:
    """Return the body of ``impl ... { }`` matched by ``header_regex``."""
    match = re.search(header_regex, source)
    if not match:
        fail(f"market-data source is missing `{label}`")
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
        fail(f"could not parse impl body for `{label}`")
    return source[start : index - 1]


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token
    (e.g. ``self.subscribers\\n    .insert(`` -> ``self.subscribers.insert(``)."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def _check_struct(spec: dict, src: str, kind: str) -> None:
    body = _struct_body(src, spec["struct"])
    missing = [
        f for f in spec["required_fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    leaks = [f for f in spec["forbidden_fields"] if re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)]
    if leaks:
        fail(
            f"{spec['struct']} leaks broker/vendor/tick field(s): {', '.join(leaks)} "
            f"({kind} must not carry broker/session/tick identifiers)"
        )


def check_market_data_tick(config: dict, types_src: str) -> str:
    spec = contract_block(config)["market_data_tick"]
    _check_struct(spec, types_src, "SRS-MD-001 fan-out payloads")
    return (
        f"atp-types declares {spec['struct']} with the {len(spec['required_fields'])} "
        f"fan-out fields ({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/vendor/exchange fields"
    )


def check_subscription_change_enum(config: dict, types_src: str) -> str:
    spec = contract_block(config)["subscription_change_enum"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    # Sanity: the line-affecting + no-op partitions must be a subset of the
    # declared variants (cross-field consistency in the contract block).
    for key in ("line_affecting_variants", "noop_variants"):
        stray = [v for v in spec[key] if v not in spec["variants"]]
        if stray:
            fail(f"contract {key} names variant(s) not in {spec['enum']}: {', '.join(stray)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} transitions "
        f"({', '.join(spec['variants'])}); Opened/Closed are the only line-affecting "
        "ones (SRS-MD-001 consolidation property)"
    )


def check_subscription_change_event(config: dict, types_src: str) -> str:
    spec = contract_block(config)["subscription_change_event"]
    _check_struct(spec, types_src, "SRS-MD-001 subscription_change events")
    return (
        f"atp-types declares {spec['struct']} with the {len(spec['required_fields'])} "
        f"required fields ({', '.join(spec['required_fields'])}) carrying the "
        "post-transition subscriber_count + lines_in_use, and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/vendor/tick fields"
    )


def check_change_sink_port(config: dict, md_src: str) -> str:
    spec = contract_block(config)["change_sink_port"]
    body = _trait_body(md_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-market-data declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method ({', '.join(spec['methods'])}) — the "
        "SRS-LOG-001 subscription_change publication channel (Source.MARKET_DATA)"
    )


def check_registry_error_enum(config: dict, md_src: str) -> str:
    spec = contract_block(config)["registry_error_enum"]
    body = _enum_body(md_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-market-data declares {spec['enum']} with {len(spec['variants'])} "
        f"fail-closed variants ({', '.join(spec['variants'])})"
    )


def check_registry_struct(config: dict, md_src: str) -> str:
    spec = contract_block(config)["registry"]
    impl = _impl_block(
        md_src,
        rf"impl\s+{re.escape(spec['struct'])}\b[^{{]*\{{",
        f"impl {spec['struct']}",
    )
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", impl)]
    if missing:
        fail(f"{spec['struct']} inherent impl is missing methods: {', '.join(missing)}")
    return (
        f"atp-market-data declares {spec['struct']} with the {len(spec['methods'])} "
        f"dedup + fan-out methods ({', '.join(spec['methods'])}) — the consolidated "
        "live subscription set SRS-MD-002 deferred to SRS-MD-001"
    )


def check_line_counter_impl(config: dict, md_src: str) -> str:
    spec = contract_block(config)["registry"]
    trait = spec["line_counter_impl_trait"]
    struct = spec["struct"]
    impl = _impl_block(
        md_src,
        rf"impl\s+{re.escape(trait)}\s+for\s+{re.escape(struct)}\b[^{{]*\{{",
        f"impl {trait} for {struct}",
    )
    missing = [
        m
        for m in spec["line_counter_impl_methods"]
        if not re.search(rf"\bfn\s+{re.escape(m)}\b", impl)
    ]
    if missing:
        fail(
            f"impl {trait} for {struct} is missing methods: {', '.join(missing)} "
            "(the registry must be the concrete line counter the SRS-MD-002 gate consumes)"
        )
    # The dedup-aware probe must be able to recognise an already-subscribed
    # security (it consumes no new line) — the `contains_key` short-circuit.
    if "contains_key" not in _compact(impl):
        fail(
            f"impl {trait} for {struct}::try_acquire must short-circuit on an "
            "already-subscribed security (contains_key) so a duplicate consumes no new line"
        )
    return (
        f"atp-market-data: {struct} IS the concrete {trait} "
        f"({', '.join(spec['line_counter_impl_methods'])}); try_acquire is dedup-aware "
        "(an existing symbol consumes no new line) — closes subscription_limit_contract "
        "deferral 'owner: SRS-MD-001'"
    )


def check_dedup_invariant(config: dict, md_src: str) -> str:
    spec = contract_block(config)["dedup_invariant"]
    body = _fn_block(md_src, contract_block(config)["registry"]["subscribe_method"])
    compact = _compact(body)
    scrutinee = _compact(spec["match_scrutinee"])
    if scrutinee not in compact:
        fail(f"subscribe does not match on `{spec['match_scrutinee']}`")
    for variant in (spec["opened_variant"], spec["subscriber_added_variant"]):
        if _compact(variant) not in compact:
            fail(f"subscribe is missing the `{variant}` production")
    insert_token = _compact(spec["new_line_insert"])
    inserts = compact.count(insert_token)
    if inserts != 1:
        fail(
            f"subscribe calls `{spec['new_line_insert']}` {inserts} time(s) — the "
            "SRS-MD-001 dedup invariant requires EXACTLY ONE upstream-subscription "
            "insert (only the first subscriber opens a line; additional subscribers "
            "must dedup onto the existing line, not insert again)"
        )
    if _compact(spec["dedup_push"]) not in compact:
        fail(
            f"subscribe does not append an additional subscriber via "
            f"`{spec['dedup_push']}` — the dedup path must reuse the existing line"
        )
    return (
        "atp-market-data::subscribe matches on self.subscribers.get_mut, inserts a new "
        "upstream subscription EXACTLY ONCE (first subscriber → Opened) and appends "
        "additional subscribers via existing.push (→ SubscriberAdded, no new line) — "
        "the SRS-MD-001 consolidation invariant"
    )


def check_input_validation(config: dict, md_src: str) -> str:
    spec = contract_block(config)["validation"]
    key_method = _compact(spec["security_key_method"] + "(")
    empty_err = _compact(spec["empty_symbol_error"])
    fan_out = _compact(_fn_block(md_src, spec["fan_out_method"]))
    if key_method not in fan_out or empty_err not in fan_out:
        fail(
            f"{spec['fan_out_method']} must canonicalize its routing key via "
            f".{spec['security_key_method']}() and fail closed with "
            f"`{spec['empty_symbol_error']}` on an empty symbol"
        )
    subscribe = _compact(_fn_block(md_src, spec["subscribe_method"]))
    if key_method not in subscribe or empty_err not in subscribe:
        fail(
            f"{spec['subscribe_method']} must canonicalize via "
            f".{spec['security_key_method']}() and fail closed with "
            f"`{spec['empty_symbol_error']}` on an empty symbol"
        )
    if _compact(spec["subscribe_validates_strategy_token"]) not in subscribe:
        fail(
            f"{spec['subscribe_method']} must reject an empty strategy id via "
            f"`{spec['subscribe_validates_strategy_token']}`"
        )
    return (
        "atp-market-data: fan_out + subscribe canonicalize via SecurityKey "
        "(.security_key()) and fail closed on empty symbol / strategy id"
    )


def check_asset_class_enum(config: dict, types_src: str) -> str:
    spec = contract_block(config)["asset_class_enum"]
    body = _enum_body(types_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} tradable classes "
        f"({', '.join(spec['variants'])}) — the security-identity dimension (SRS-SDK-003 scope)"
    )


def check_security_key(config: dict, types_src: str) -> str:
    spec = contract_block(config)["security_key"]
    # The struct must exist; its fields are private (no `pub`) so a key can
    # only be built through the normalizing constructor.
    body = _struct_body(types_src, spec["struct"])
    if re.search(r"\bpub\s+\w+\s*:", body):
        fail(
            f"{spec['struct']} must keep its fields private so every key is "
            "built through the normalizing constructor"
        )
    if not re.search(rf"\bpub\s+fn\s+{re.escape(spec['constructor'])}\b", types_src):
        fail(f"{spec['struct']} is missing its public constructor `{spec['constructor']}`")
    for accessor in spec["accessors"]:
        if not re.search(rf"\bpub\s+fn\s+{re.escape(accessor)}\b", types_src):
            fail(f"{spec['struct']} is missing accessor `{accessor}`")
    compact = _compact(types_src)
    if "trim()" not in compact or "to_uppercase()" not in compact:
        fail(
            f"{spec['struct']}::{spec['constructor']} must normalize the symbol "
            "(trim + to_uppercase) so case/whitespace variants are one security"
        )
    for carrier in spec["carriers"]:
        impl = _impl_block(types_src, rf"impl\s+{re.escape(carrier)}\b[^{{]*\{{", f"impl {carrier}")
        if not re.search(rf"\bfn\s+{re.escape(spec['carrier_method'])}\b", impl):
            fail(f"{carrier} is missing the canonical `{spec['carrier_method']}()` accessor")
    return (
        f"atp-types declares {spec['struct']} (private symbol+asset_class, normalizing "
        f"`{spec['constructor']}`) carried by {', '.join(spec['carriers'])} via "
        f".{spec['carrier_method']}() — the canonical dedup / fan-out key"
    )


def check_registry_key_type(config: dict, md_src: str) -> str:
    spec = contract_block(config)["registry"]
    decl = _compact(spec["key_field_decl"])
    if decl not in _compact(md_src):
        fail(
            f"{spec['struct']} must key the consolidated set on {spec['key_type']} "
            f"(expected field declaration `{spec['key_field_decl']}`)"
        )
    return (
        f"atp-market-data: {spec['struct']} keys the consolidated set on "
        f"{spec['key_type']} (`{spec['key_field_decl']}`) — no raw-symbol conflation"
    )


def check_atomic_admission(config: dict, md_src: str) -> str:
    spec = contract_block(config)["atomic_admission"]
    body = _compact(_fn_block(md_src, spec["subscribe_method"]))
    for token_key in ("limit_check_token", "limit_error_variant", "new_line_insert"):
        if _compact(spec[token_key]) not in body:
            fail(
                f"{spec['subscribe_method']} must enforce the line limit ATOMICALLY in "
                f"the admission path — missing `{spec[token_key]}` (a new line past the "
                "cap must be refused in the same borrow that inserts)"
            )
    return (
        "atp-market-data::subscribe enforces the IB line ceiling atomically (checks "
        ">= self.line_limit and returns LineLimitReached before the insert) — closes "
        "the probe-then-mutate race"
    )


def check_vendor_isolation(config: dict, md_src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in md_src]
    if leaked:
        fail(
            f"atp-market-data leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core subscription manager must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-market-data is free of all {len(tokens)} forbidden vendor SDK tokens "
        "(SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["market_data_crate"]["crate"]
    integration = block["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib failed:\n{lib.stdout}\n{lib.stderr}")
    integ = subprocess.run(
        [cargo, "test", "-p", crate, "--test", integration, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(f"cargo test -p {crate} --test {integration} failed:\n{integ.stdout}\n{integ.stderr}")
    return (
        f"cargo test -p {crate} --lib + {integration}: PASS "
        "(dedup, fan-out isolation, lifecycle, change events, line-counter seam verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

_STATIC_CHECKS = (
    ("market_data_tick", check_market_data_tick, "types"),
    ("asset_class_enum", check_asset_class_enum, "types"),
    ("security_key", check_security_key, "types"),
    ("subscription_change_enum", check_subscription_change_enum, "types"),
    ("subscription_change_event", check_subscription_change_event, "types"),
    ("change_sink_port", check_change_sink_port, "market_data"),
    ("registry_error_enum", check_registry_error_enum, "market_data"),
    ("registry_struct", check_registry_struct, "market_data"),
    ("registry_key_type", check_registry_key_type, "market_data"),
    ("line_counter_impl", check_line_counter_impl, "market_data"),
    ("dedup_invariant", check_dedup_invariant, "market_data"),
    ("atomic_admission", check_atomic_admission, "market_data"),
    ("input_validation", check_input_validation, "market_data"),
    ("vendor_isolation", check_vendor_isolation, "market_data"),
)

_DEFERRED_OWNERS = (
    "SRS-MD-001-runtime",
    "SRS-PERF-001",
    "SRS-EXE-006",
)


def assert_subscription_fanout_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    types_src = types_source(config, root)
    md_src = market_data_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        evidence.append(check(config, types_src if scope == "types" else md_src))
    return evidence


def run_checks() -> list[str]:
    config = load_config()
    evidence = assert_subscription_fanout_static(config)
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-MD-001 SDK-surface contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except SubscriptionFanoutCheckError as error:
        print(f"SRS-MD-001 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-MD-001 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + " (real IB feed + async fan-out transport + <=100 ms latency NFR); "
        "feature_list.json keeps SRS-MD-001 passes:false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
