#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-009 (transparent cold-read failover to NAS + bounded SSD
cold-read cache).

SRS-DATA-009 (SyRS SYS-68; StRS SN-1.28 / BG-5). The acceptance criterion: "Requests outside SSD
retention are served from NAS and cached on SSD without requiring consumer code changes; cold-read
cache entries do not exceed the configurable SSD share defaulting to 20 percent and are evicted
before hot runtime data."

This is the READ counterpart to SRS-DATA-008: the `cold_read` module in ``crates/atp-data`` adds a
``TieredReader`` over the existing ``TieredStore`` (which owns the SSD-first write + retention +
``archive_cold`` that drops cold records off SSD, keeping them only on NAS). Structural contract in
``architecture/runtime_services.json`` (block ``cold_read_failover_contract``):

  (a) TRANSPARENT FALLBACK -- ``TieredReader::query`` runs the SRS-DATA-007 ``UnifiedHistoricalQuery``
      over SSD primary -> cold-read cache -> (for cold ranges only, ``start_ts < hot_window_start``)
      NAS, merging deduped by natural key in the SAME ``event_ts``-ascending order as
      ``MarketDataStore::query_unified`` (parity with a query over ``SSD union NAS``).
  (b) BOUNDED CACHE -- NAS-served records are written into a SEPARATE ``MarketDataStore`` under
      ``<ssd>/cold_read_cache``, capped at ``floor(ssd_capacity_records * cache_share_percent / 100)``
      in INTEGER arithmetic; ``ColdReadConfig`` defaults the share to 20% and fails closed on a
      share > 100% or a zero capacity; ``keep_most_recent`` enforces ``entries <= cap`` on every write.
  (c) EVICTED BEFORE HOT -- ``evict_cold_cache_to`` reclaims SSD by draining ONLY the cold-read cache
      (it operates on ``cold_cache_dir`` and never opens the SSD primary store), so hot runtime data is
      structurally un-evictable here; the eviction POLICY (80% high-water, recency, never-evict live)
      is SRS-DATA-010.
  (d) DETERMINISM -- the hot/cold boundary is a pure function of the caller-supplied ``now_ts`` (no
      wall-clock); the cap is integer arithmetic (no float); no vendor SDK, no broker dependency.
  (e) OPERATOR CLI -- ``data009_cold_read_cli`` (query / cache-report / evict-cache) exits NON-ZERO on
      a cap breach and NEVER persists directly (``.save_to_path(`` is absent from the bin), so the
      cold-read cache write is not an SSD-only ingestion bypass of the SRS-DATA-008 routing guard.

The PASS line is ``SRS-DATA-009 COLD-READ-FAILOVER PASS`` -- it names the deferred owners (the eviction
POLICY + access-recency LRU via SRS-DATA-010, the real provider adapters via SRS-DATA-001/003/005/006,
real SSD/NAS capacity via NFR-SC2, and the in-process Python/backtest bindings).

Invoke:
    python3 tools/data009_cold_read_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body

ROOT = Path(__file__).resolve().parents[1]


class ColdReadCheckError(AssertionError):
    """Raised when a structural contract is violated (mirrors the data008 check's error type)."""


def fail(message: str) -> None:
    raise ColdReadCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    block = config.get("cold_read_failover_contract")
    if block is None:
        fail("runtime_services.json is missing the cold_read_failover_contract block")
    return block


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


def _strip_comments(src: str) -> str:
    """Remove Rust block + line comments so a forbidden token named in documentation (e.g. the word
    ``f64`` in a doc comment) is never mistaken for a use of the token in code."""
    no_block = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", no_block)


def _strip_test_module(src: str) -> str:
    """Drop the trailing ``#[cfg(test)] mod tests { ... }`` so forbidden-token / structural scans
    cover only production code (test scaffolding legitimately uses AtomicU64, process::id, etc.)."""
    marker = src.find("#[cfg(test)]")
    return src if marker == -1 else src[:marker]


def _crate_dir(config: dict, root: Path) -> Path:
    return root / contract_block(config)["data_crate"]["path"]


def cold_read_source(config: dict, root: Path = ROOT) -> str:
    """Production source of the cold_read module (test module stripped)."""
    block = contract_block(config)
    path = _crate_dir(config, root) / "src" / f"{block['cold_read_module']}.rs"
    if not path.is_file():
        fail(f"cold-read module not found at {path}")
    return _strip_test_module(path.read_text(encoding="utf-8"))


def lib_source(config: dict, root: Path = ROOT) -> str:
    return (_crate_dir(config, root) / "src" / "lib.rs").read_text(encoding="utf-8")


def cli_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    path = _crate_dir(config, root) / "src" / "bin" / f"{block['cli_bin']}.rs"
    if not path.is_file():
        fail(f"operator CLI not found at {path}")
    return path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    return (_crate_dir(config, root) / block["no_broker_dependency"]["cargo_toml"]).read_text(
        encoding="utf-8"
    )


def _fn_body(src: str, fn_name: str) -> str:
    """Extract a function body by brace balance from the first `{` after the signature. Fails closed
    if the function is absent or unbalanced (a truncated file must not silently pass a guard)."""
    match = re.search(rf"fn\s+{re.escape(fn_name)}\b", src)
    if match is None:
        fail(f"expected `fn {fn_name}` is absent from the cold-read module")
    start = src.index("{", match.end())
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    fail(f"`fn {fn_name}` has an unbalanced body (could not find its closing brace)")
    return ""  # unreachable (fail raises)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors (each returns a one-line evidence string).
# --------------------------------------------------------------------------- #


def check_module_reexport(config: dict, lib_src: str) -> str:
    token = contract_block(config)["module_reexport"]["lib_reexport_token"]
    if _compact(token) not in _compact(lib_src):
        fail(f"lib.rs must expose the cold-read module (`{token}`)")
    return f"atp-data lib.rs exposes the cold-read module (`{token}`)"


def check_config_type(config: dict, src: str) -> str:
    spec = contract_block(config)["config_type"]
    # The validated config type + its integer cap function.
    if f"struct {spec['struct']}" not in src:
        fail(f"cold_read must define the `{spec['struct']}` validated config type")
    if _compact(spec["cap_fn"]) not in _compact(src):
        fail(f"`{spec['struct']}` must expose the integer cap fn `{spec['cap_fn']}`")
    # Default share is 20% and the max is 100% -- the AC's "defaulting to 20 percent" ceiling.
    for const_key, label in (
        ("default_share_const", "20% default"),
        ("max_share_const", "100% max"),
    ):
        if _compact(spec[const_key]) not in _compact(src):
            fail(f"cold_read must declare the {label} share constant `{spec[const_key]}`")
    # Fail-closed config variants. Word-boundary match so a renamed variant that merely CONTAINS the
    # expected name as a prefix (e.g. `ZeroSsdCapacityMaybe`) does not satisfy the guard.
    err_body = _compact(_enum_body(src, "ColdReadError"))
    for variant in (spec["zero_capacity_variant"], spec["share_above_max_variant"]):
        if re.search(rf"\b{re.escape(variant)}\b", err_body) is None:
            fail(
                f"ColdReadError must carry the fail-closed `{variant}` variant so a bad cold-read "
                "config is rejected at construction"
            )
    return (
        f"`{spec['struct']}` bounds the cache at integer `{spec['cap_fn'].split()[-1]}` = "
        "floor(capacity*share/100), defaults the share to 20% (max 100%), and rejects a zero "
        "capacity / >100% share fail-closed (ZeroSsdCapacity / CacheShareAboveMax)"
    )


def check_reader_type(config: dict, src: str) -> str:
    spec = contract_block(config)["reader_type"]
    if f"struct {spec['struct']}" not in src:
        fail(f"cold_read must define the `{spec['struct']}` transparent read surface")
    for key in ("query_fn", "evict_fn", "report_fn", "cache_dir_fn"):
        if _compact(spec[key]) not in _compact(src):
            fail(f"`{spec['struct']}` must expose `{spec[key]}`")
    if _compact(spec["cache_subdir_const"]) not in _compact(src):
        fail(f"cold_read must pin the cache subdirectory constant `{spec['cache_subdir_const']}`")
    return (
        f"`{spec['struct']}` exposes the transparent read surface (query / evict_cold_cache_to / "
        "cold_cache_report / cold_cache_dir) with the cache pinned under <ssd>/cold_read_cache"
    )


def check_transparency(config: dict, src: str) -> str:
    spec = contract_block(config)["transparency"]
    body = _strip_comments(_fn_body(src, "query"))
    # NAS is consulted only for cold ranges (the hot-window gate), and the merge is event_ts-ordered
    # like query_unified -> parity with a single-store query over SSD union NAS.
    for token, why in (
        (spec["cold_gate_token"], "consult NAS only for cold ranges (hot_window_start gate)"),
        (spec["merge_ordering_token"], "merge in event_ts-ascending order (query_unified parity)"),
        (spec["union_query_token"], "run the SRS-DATA-007 unified query over each tier"),
    ):
        if token not in body:
            fail(f"TieredReader::query must {why} (missing `{token}`)")
    return (
        "TieredReader::query is transparent: it runs query_unified over SSD/cache/NAS, consults NAS "
        "only for cold ranges (hot_window_start), and merges in event_ts order (parity with SSD∪NAS)"
    )


def check_cap_enforcement(config: dict, src: str) -> str:
    spec = contract_block(config)["cap_enforcement"]
    for key in ("write_back_fn", "cap_helper_fn"):
        if re.search(rf"\b{re.escape(spec[key].split()[-1])}\b", src) is None:
            fail(f"cold_read must define `{spec[key]}` (the cap-enforcing write path)")
    # The cache write-back must run through the cap helper (so entries can never exceed the cap).
    write_back = _strip_comments(_fn_body(src, "write_back_cache"))
    if "keep_most_recent" not in write_back:
        fail("write_back_cache must enforce the cap via keep_most_recent so entries <= cap")
    # The cap helper truncates to the cap.
    helper = _strip_comments(_fn_body(src, "keep_most_recent"))
    if "truncate" not in helper and "[..keep]" not in helper and ".take(" not in helper:
        fail("keep_most_recent must actually bound the survivor set to the cap (truncate/take)")
    return (
        "cache write-back bounds entries to the cap via keep_most_recent (keeps the most-recent by "
        "event_ts, drops the rest) so the cold-read cache never exceeds the configurable SSD share"
    )


def check_evict_before_hot(config: dict, src: str) -> str:
    spec = contract_block(config)["evict_before_hot"]
    body = _strip_comments(_fn_body(src, spec["evict_fn"]))
    # It drains the cold-read cache directory...
    if spec["cache_dir_token"] not in body:
        fail(
            f"{spec['evict_fn']} must operate on the cold-read cache (`{spec['cache_dir_token']}`)"
        )
    # ...and NEVER opens the SSD primary store -- so hot data is structurally un-evictable here. The
    # SSD-primary directory accessor must not appear in the eviction body.
    if spec["forbidden_ssd_primary_token"] in body:
        fail(
            f"{spec['evict_fn']} must NOT reference the SSD primary tier (`"
            f"{spec['forbidden_ssd_primary_token']}`): cold-read cache eviction must never touch hot "
            "runtime data (SRS-DATA-009 'evicted before hot runtime data')"
        )
    return (
        "evict_cold_cache_to drains ONLY the cold_cache_dir and never opens the SSD primary store, so "
        "cold-read cache entries are evicted before (and without ever touching) hot runtime data"
    )


def check_numeric_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    code = _strip_comments(src)
    for token in spec["integer_tokens"]:
        if _compact(token) not in _compact(code):
            fail(f"cold_read must compute the cap in integer arithmetic (missing `{token}`)")
    for token in spec["forbidden_tokens"]:
        if token in code:
            fail(
                f"cold_read must contain NO floating point (`{token}`) -- the cap is integer "
                "arithmetic, matching the tier's money/precision discipline"
            )
    return "cold_read cap is integer arithmetic (saturating_mul + /100); no f32/f64 in code"


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    code = _strip_comments(src)
    if _compact(spec["now_param_token"]) not in _compact(code):
        fail(
            "the hot/cold boundary must be a pure function of the caller-supplied now_ts "
            f"(missing `{spec['now_param_token']}`) -- no wall-clock read"
        )
    for token in spec["forbidden_tokens"]:
        if token in code:
            fail(f"cold_read must read no wall-clock / RNG / thread primitive (`{token}`)")
    return "cold_read reads no wall-clock (now_ts is caller-supplied), no RNG, no threads"


def check_no_broker_dependency(config: dict, cargo_src: str) -> str:
    tokens = contract_block(config)["no_broker_dependency"]["forbidden_dep_tokens"]
    for token in tokens:
        if token in cargo_src:
            fail(f"atp-data must not depend on a broker/execution crate (`{token}`)")
    return "atp-data Cargo.toml carries no broker/execution dependency"


def check_no_vendor_tokens(config: dict, src: str) -> str:
    code = _strip_comments(src)
    for token in contract_block(config)["vendor_forbidden_tokens"]:
        if token in code:
            fail(f"cold_read must name no vendor SDK (`{token}`) -- it is provider-agnostic")
    return "cold_read names no vendor SDK (provider-agnostic read path)"


def data007_cli_source(config: dict, root: Path = ROOT) -> str:
    """The EXISTING SRS-DATA-007 operator read CLI that the cold-read is wired into."""
    block = contract_block(config)["consumer_wiring"]
    path = _crate_dir(config, root) / "src" / "bin" / f"{block['consumer_cli']}.rs"
    if not path.is_file():
        fail(f"the SRS-DATA-007 consumer CLI not found at {path}")
    return path.read_text(encoding="utf-8")


def check_consumer_wiring(config: dict, data007_cli_src: str) -> str:
    """The AC's 'without requiring consumer code changes' is met by wiring the transparent cold-read
    into the EXISTING SRS-DATA-007 operator read surface (data007_query_cli), so an operator reading a
    record archived off SSD gets it back from NAS transparently -- not only via the new data009 CLI."""
    spec = contract_block(config)["consumer_wiring"]
    for key, why in (
        ("tiered_reader_token", "route the read through the transparent TieredReader"),
        ("cold_read_flag_token", "accept the cold-read tier flag (--nas)"),
        (
            "env_auto_token",
            "engage tiering from the ATP_NAS_DATA_DIR config key (so the SAME query invocation "
            "auto-tiers with no new flags, not just an explicit --nas)",
        ),
    ):
        if spec[key] not in data007_cli_src:
            fail(
                f"the existing SRS-DATA-007 consumer `{spec['consumer_cli']}` must {why} "
                f"(missing `{spec[key]}`) so cold NAS fallback is transparent to an EXISTING read "
                "surface with an UNCHANGED invocation (SRS-DATA-009 'without consumer code changes')"
            )
    return (
        f"the existing SRS-DATA-007 read surface `{spec['consumer_cli']}` routes through TieredReader, "
        "auto-engaging from the ATP_NAS_DATA_DIR config key (the same query invocation, no new flags), "
        "so archived-off records are served transparently through an EXISTING consumer path"
    )


def check_divergence_guard(config: dict, src: str) -> str:
    """Cross-tier read integrity: a stale/corrupt cold-read cache entry that still decodes but
    disagrees with the authoritative NAS record must FAIL CLOSED, not silently shadow it."""
    spec = contract_block(config)["divergence_guard"]
    err_body = _compact(_enum_body(src, "ColdReadError"))
    if re.search(rf"\b{re.escape(spec['error_variant'])}\b", err_body) is None:
        fail(
            f"ColdReadError must carry the `{spec['error_variant']}` variant so a cross-tier value "
            "divergence (a stale/corrupt cache shadowing NAS) fails closed"
        )
    if re.search(rf"\b{re.escape(spec['merge_fn'].split()[-1])}\b", src) is None:
        fail(
            f"cold_read must merge tiers through `{spec['merge_fn']}` (the divergence-checking merge)"
        )
    merge_body = _strip_comments(_fn_body(src, "merge_record"))
    # The merge must COMPARE full record content on a duplicate key (not just dedup by key) and raise
    # the divergence error -- otherwise a divergent cache entry silently shadows NAS.
    if _compact(spec["value_compare_token"]) not in _compact(merge_body):
        fail(
            "merge_record must compare full record content on a cross-tier duplicate "
            f"(`{spec['value_compare_token']}`), not dedup by key alone"
        )
    if spec["error_variant"] not in merge_body:
        fail(f"merge_record must fail closed with `{spec['error_variant']}` on a value divergence")
    return (
        "TieredReader merges tiers via merge_record, which compares full record content on a "
        "cross-tier duplicate key and fails closed (CrossTierDivergence) rather than letting a "
        "stale/corrupt cold-read cache silently shadow the authoritative NAS record"
    )


def check_retention_guard(config: dict, src: str) -> str:
    """Cold-only NAS fallback: a mixed hot/cold query must serve ONLY cold records from NAS; a hot
    record on NAS but missing from SSD is an SRS-DATA-008 retention breach and must fail closed."""
    spec = contract_block(config)["retention_guard"]
    err_body = _compact(_enum_body(src, "ColdReadError"))
    if re.search(rf"\b{re.escape(spec['error_variant'])}\b", err_body) is None:
        fail(
            f"ColdReadError must carry the `{spec['error_variant']}` variant so a hot record present "
            "on NAS but missing from SSD fails closed instead of masking the SRS-DATA-008 breach"
        )
    query_body = _strip_comments(_fn_body(src, "query"))
    # The NAS fallback must gate on the cold filter (event_ts < hot_window_start) so a hot record is
    # never served from NAS...
    if _compact(spec["cold_filter_token"]) not in _compact(query_body):
        fail(
            "the NAS fallback must restrict served records to the COLD window "
            f"(`{spec['cold_filter_token']}`), not the full query range, so a hot record is never "
            "served from NAS"
        )
    # ...and it must raise the breach for the hot-missing-from-SSD case.
    if spec["error_variant"] not in query_body:
        fail(
            f"TieredReader::query must fail closed with `{spec['error_variant']}` when a hot record is "
            "on NAS but missing from SSD"
        )
    return (
        "the NAS fallback serves only records older than the hot window; a hot record on NAS but "
        "missing from SSD fails closed (HotRetentionBreach), never masking the SRS-DATA-008 breach or "
        "caching hot data as cold"
    )


def check_cli(config: dict, cli_src: str) -> str:
    spec = contract_block(config)["cli"]
    if _compact(spec["cap_breach_exit_token"]) not in _compact(cli_src):
        fail(
            "the operator CLI must exit NON-ZERO on a cold-read cache cap breach (missing "
            f"`{spec['cap_breach_exit_token']}`) so automation catches an unbounded cache"
        )
    # The CLI must NOT persist directly: all durable cache writes are owned by the cold_read library,
    # so the cold-read cache write is not an SSD-only ingest bypass of the SRS-DATA-008 routing guard.
    if spec["no_direct_persist_token"] in _strip_comments(cli_src):
        fail(
            f"the cold-read CLI must NOT persist directly (`{spec['no_direct_persist_token']}`): "
            "durable cache persistence is owned by the cold_read library (so the SRS-DATA-008 all-bins "
            "routing sweep cannot mistake the cold-read cache write for an SSD-only ingestion path)"
        )
    return (
        "operator CLI data009_cold_read_cli exits non-zero on a cap breach and never persists "
        "directly (cache persistence is library-owned, not an SSD-only ingest bypass)"
    )


# Each entry: (label, collector, source_key). source_key selects which source the collector reads.
_STATIC_CHECKS = [
    ("module_reexport", check_module_reexport, "lib"),
    ("config_type", check_config_type, "cold_read"),
    ("reader_type", check_reader_type, "cold_read"),
    ("transparency", check_transparency, "cold_read"),
    ("cap_enforcement", check_cap_enforcement, "cold_read"),
    ("evict_before_hot", check_evict_before_hot, "cold_read"),
    ("numeric_boundary", check_numeric_boundary, "cold_read"),
    ("determinism", check_determinism, "cold_read"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("no_vendor_tokens", check_no_vendor_tokens, "cold_read"),
    ("cli", check_cli, "cli"),
    ("consumer_wiring", check_consumer_wiring, "data007_cli"),
    ("divergence_guard", check_divergence_guard, "cold_read"),
    ("retention_guard", check_retention_guard, "cold_read"),
]

_DEFERRED_OWNERS = [
    "the eviction POLICY (80% high-water trigger / inactivity recency / never-evict live-strategy "
    "data) + access-recency LRU intra-cache order (SRS-DATA-010; this slice ships the "
    "hot-segregated cold-read-cache-first PRIMITIVE evict_cold_cache_to + the bounded write-back)",
    "split-adjusted x cold-read (SRS-DATA-011/012; tiered mode serves RAW and fails closed on "
    "split-adjusted rather than emitting raw-as-adjusted over archived-off bars)",
    "CLOSE BLOCKER (keeps SRS-DATA-009 passes:false): routing the OTHER named unified-read consumers "
    "through TieredReader -- the backtest borrow-streaming StoreBarSource, the Python "
    "StoreBackedHistoricalData binding, and the factor store_inputs -- so 'without consumer code "
    "changes' holds for EVERY consumer, not just the operator CLI. Each is a passes:true feature with "
    "its own contract (a cross-crate follow-up). This slice ships the MECHANISM + one wired consumer "
    "(the env-transparent operator read surface data007_query_cli) as the foundation they reuse.",
    "the real provider network adapters that FEED the tier (SRS-DATA-001/003/005/006; "
    "provider-agnostic, fixture sources stand in)",
    "real SSD/NAS capacity + network mount (NFR-SC2 / SRS-ARCH-004; the cap is modeled in the "
    "store's record unit, the deterministic fixture proxy for byte capacity)",
]


def assert_cold_read_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (reused by the L3 contract test)."""
    sources = {
        "cold_read": cold_read_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
        "cli": cli_source(config, root),
        "data007_cli": data007_cli_source(config, root),
    }
    return [check(config, sources[key]) for _, check, key in _STATIC_CHECKS]


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    """Behavioral smoke: run the cold_read unit tests and build the operator CLI. Gated on cargo
    being available (fail-closed if required but absent), mirroring the data008 check."""
    block = contract_block(config)
    crate = block["data_crate"]["crate"]
    cli_bin = block["cli_bin"]
    if shutil.which("cargo") is None:
        if require_cargo:
            fail("cargo is required for the behavioral smoke but is not on PATH")
        return "cargo not available — behavioral smoke skipped (structural checks still ran)"
    test = subprocess.run(
        ["cargo", "test", "-p", crate, "--lib", "cold_read"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if test.returncode != 0:
        fail(f"`cargo test -p {crate} --lib cold_read` failed:\n{test.stdout}\n{test.stderr}")
    build = subprocess.run(
        ["cargo", "build", "-p", crate, "--bin", cli_bin],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        fail(f"`cargo build -p {crate} --bin {cli_bin}` failed:\n{build.stdout}\n{build.stderr}")
    return f"cargo test -p {crate} --lib cold_read + build --bin {cli_bin}: PASS"


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_cold_read_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    evidence.append("deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-DATA-009 cold-read failover contract check")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="fail (not skip) the behavioral smoke if cargo is unavailable",
    )
    args = parser.parse_args(argv)
    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except ColdReadCheckError as err:
        print(f"SRS-DATA-009 COLD-READ-FAILOVER FAIL\n  - {err}", file=sys.stderr)
        return 1
    for line in evidence:
        print(f"- {line}")
    print("SRS-DATA-009 COLD-READ-FAILOVER PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
