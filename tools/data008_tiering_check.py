#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-008 (SSD-primary / NAS-archival tiered storage).

SRS-DATA-008 (SyRS SYS-24 / SYS-67, AC-5, NFR-SC2; StRS C-5 / SN-1.26 / SN-1.27). The acceptance
criterion: "All ingestion writes to SSD first; new data is synced to NAS; SSD retains at least 90
days of configured hot data; NAS is used for indefinite retention; storage growth estimates are
documented in Section 12.1."

The tier coordinator that wraps the SRS-DATA-016 ``MarketDataStore`` directory lives in
``crates/atp-data`` (module ``tiering``), per the structural contract in
``architecture/runtime_services.json`` (block ``tiered_storage_contract``):

  (a) ``TieredStore::ingest`` writes the batch to the SSD store and durably persists it BEFORE any
      NAS write (the ``ssd.save_to_path`` precedes the ``push_to_nas`` call — the SSD-first
      ordering), then syncs the full SSD snapshot to NAS so NAS converges to a superset.
  (b) the hot-retention window is FLOOR-ENFORCED at 90 days: ``TierConfig::new`` rejects a smaller
      window (``HotRetentionBelowFloor``) and two identical tiers (``TiersNotDistinct``).
  (c) ``archive_cold`` drops a record from SSD only when it is cold AND confirmed byte-identical on
      NAS (``nas.get(record.key()) == Some(record)``); there is no NAS delete, so NAS retention is
      indefinite. An unreachable NAS degrades (``NasSyncStatus::Degraded``) the ingest rather than
      losing the SSD write.
  (d) ``retention_report`` independently cross-checks the tiers (``hot_missing_from_ssd`` /
      ``ssd_missing_from_nas``); the §12.1 growth estimates are documented in ``docs/SRS.md``.
  (e) the tier reads no wall-clock (the hot/cold boundary is a pure function of the caller-supplied
      ``now_ts``), uses no floating-point, names no vendor SDK, and carries no broker dependency.

The PASS line is ``SRS-DATA-008 TIERED-STORAGE PASS`` — it names the deferred owners (routing all
ingestion through the tier, the real provider network adapters via SRS-DATA-001/003/005/006,
cold-read failover via SRS-DATA-009, the eviction POLICY via SRS-DATA-010, and real SSD/NAS capacity
via NFR-SC2 / SRS-ARCH-004). Unlike SRS-DATA-016, SRS-DATA-008 STAYS passes:false: this check proves
the tier SUBSTRATE is structurally present + runnable, but the AC's cross-cutting "all ingestion
writes to SSD first; new data is synced to NAS" clause is not demonstrated end to end while a raw
ingest path (data016_ingest_cli) still writes a single store dir with no NAS sync. Closing
SRS-DATA-008 needs the production ingestion paths routed through TieredStore.

Invoke:
    python3 tools/data008_tiering_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _struct_body

ROOT = Path(__file__).resolve().parents[1]


class TieringCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise TieringCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    block = config.get("tiered_storage_contract")
    if block is None:
        fail("runtime_services.json is missing the tiered_storage_contract block")
    return block


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


def _strip_comments(src: str) -> str:
    """Remove Rust block + line comments so a forbidden token mentioned in documentation (e.g. the
    word ``f64`` in a doc comment) is never mistaken for a use of the token in code."""
    no_block = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", no_block)


def tiering_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    path = root / block["data_crate"]["path"] / "src" / f"{block['tiering_module']}.rs"
    if not path.is_file():
        fail(f"tier coordinator module not found at {path}")
    return path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    return (root / block["data_crate"]["path"] / "src" / "lib.rs").read_text(encoding="utf-8")


def cli_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    path = root / block["data_crate"]["path"] / "src" / "bin" / f"{block['cli_bin']}.rs"
    if not path.is_file():
        fail(f"operator CLI not found at {path}")
    return path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    return (
        root / block["data_crate"]["path"] / block["no_broker_dependency"]["cargo_toml"]
    ).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors (each returns a one-line evidence string).
# --------------------------------------------------------------------------- #


def check_config(config: dict, src: str) -> str:
    spec = contract_block(config)["config"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"tiering must declare `pub struct {spec['struct']}`")
    compact = _compact(src)
    for key in ("floor_const", "default_const", "hot_window_fn", "is_hot_fn"):
        if _compact(spec[key]) not in compact:
            fail(f"{spec['struct']} must declare `{spec[key]}`")
    # The >=90-day floor must be enforced fail-closed, and the two tiers kept distinct.
    if f"{spec['floor_const']}: u32 = {spec['floor_value']}" not in src:
        fail(
            f"the hot-retention floor must be `{spec['floor_const']}: u32 = {spec['floor_value']}`"
        )
    for guard in (spec["floor_guard_token"], spec["distinct_guard_token"]):
        if guard not in src:
            fail(f"TierConfig must reject misconfiguration via `{guard}`")
    return (
        f"{spec['struct']} floor-enforces the >=90-day hot window ({spec['floor_const']} = "
        f"{spec['floor_value']}, rejected via {spec['floor_guard_token']}) and distinct tiers "
        f"({spec['distinct_guard_token']}); hot/cold via {spec['is_hot_fn']}/{spec['hot_window_fn']}"
    )


def check_tiered_store(config: dict, src: str) -> str:
    spec = contract_block(config)["tiered_store"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"tiering must declare `pub struct {spec['struct']}`")
    compact = _compact(src)
    for key in ("ingest_fn", "sync_fn", "archive_fn", "report_fn", "push_nas_fn"):
        if _compact(spec[key]) not in compact:
            fail(f"{spec['struct']} must expose `{spec[key]}`")
    return (
        f"{spec['struct']} exposes the tier API: {spec['ingest_fn']} (SSD-first ingest), "
        f"{spec['sync_fn']} (reconcile), {spec['archive_fn']} (safe archival), "
        f"{spec['report_fn']} (cross-tier verification)"
    )


def check_ssd_first_ordering(config: dict, src: str) -> str:
    spec = contract_block(config)["ssd_first_ordering"]
    compact = _compact(_strip_comments(src))
    first, second = (_compact(t) for t in spec["ordered_tokens"])
    i_first, i_second = compact.find(first), compact.find(second)
    if i_first < 0:
        fail(f"ingest must durably persist SSD via `{spec['ordered_tokens'][0]}`")
    if i_second < 0:
        fail(f"ingest must sync to NAS via `{spec['ordered_tokens'][1]}`")
    if not i_first < i_second:
        fail(
            "SSD-first ordering violated: the SSD save must precede the NAS push in ingest "
            f"({spec['ordered_tokens'][0]} before {spec['ordered_tokens'][1]})"
        )
    return "ingest writes SSD-first: the durable SSD save precedes the NAS push (no NAS-only datum)"


def check_nas_sync_status(config: dict, src: str) -> str:
    spec = contract_block(config)["nas_sync_status"]
    if not re.search(rf"\bpub\s+enum\s+{re.escape(spec['enum'])}\b", src):
        fail(f"tiering must declare `pub enum {spec['enum']}`")
    # Search the enum body with comments stripped, so a variant NAME mentioned in a doc comment
    # (e.g. "never folded into `Degraded`") cannot stand in for an actual variant declaration.
    body = _strip_comments(_enum_body(src, spec["enum"]))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variant(s): {', '.join(missing)}")
    compact = _compact(src)
    for key in ("degraded_token", "failed_token", "classifier_fn", "not_autocreated_token"):
        if _compact(spec[key]) not in compact:
            fail(f"tiering must carry `{spec[key]}` (NAS reachability/alias/failure handling)")
    for token in (spec["nas_unreachable_token"], spec["nas_alias_token"]):
        if token not in src:
            fail(f"tiering must surface the NAS condition `{token}`")
    return (
        f"{spec['enum']} keeps NAS outcomes DISTINCT (Synced / Degraded=unreachable outage / "
        f"Failed=reachable-but-broken incl. alias) via the single {spec['classifier_fn']} classifier; "
        "a recoverable outage is never confused with an integrity failure"
    )


def check_retention_report(config: dict, src: str) -> str:
    spec = contract_block(config)["retention_report"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"tiering must declare `pub struct {spec['struct']}`")
    body = _compact(_struct_body(src, spec["struct"]))
    missing = [f for f in spec["fields"] if _compact(f) not in body]
    if missing:
        fail(f"{spec['struct']} must carry the cross-tier fields: {', '.join(missing)}")
    compact = _compact(src)
    for key in ("hot_retention_satisfied_fn", "nas_superset_fn"):
        if _compact(spec[key]) not in compact:
            fail(f"{spec['struct']} must expose `{spec[key]}`")
    # The verdicts are TRI-STATE: an unreachable NAS yields Unverified, never a false-positive
    # Satisfied (the cross-tier check could not run). Pin the enum + the Unverified arm.
    if not re.search(rf"\bpub\s+enum\s+{re.escape(spec['verdict_enum'])}\b", src):
        fail(f"tiering must declare the tri-state `pub enum {spec['verdict_enum']}`")
    if _compact(spec["unverified_token"]) not in compact:
        fail(
            "the retention verdict must return `RetentionVerdict::Unverified` when NAS is unreachable "
            "(never a false-positive Satisfied)"
        )
    return (
        f"{spec['struct']} independently cross-checks the tiers (hot_missing_from_ssd / "
        f"ssd_missing_from_nas) and exposes TRI-STATE {spec['verdict_enum']} verdicts "
        "(Satisfied/Violated/Unverified) — an unreachable NAS yields Unverified, never a "
        "false-positive Satisfied"
    )


def check_archive_safety(config: dict, src: str) -> str:
    spec = contract_block(config)["archive_safety"]
    compact = _compact(src)
    if _compact(spec["confirmed_on_nas_token"]) not in compact:
        fail(
            "archive_cold must drop a record only when confirmed byte-identical on NAS "
            f"(`{spec['confirmed_on_nas_token']}`)"
        )
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['archive_outcome_struct'])}\b", src):
        fail(f"tiering must declare `pub struct {spec['archive_outcome_struct']}`")
    if _compact(spec["retained_field"]) not in _compact(
        _struct_body(src, spec["archive_outcome_struct"])
    ):
        fail(f"{spec['archive_outcome_struct']} must carry `{spec['retained_field']}`")
    # Alias guard: same_directory must reject an SSD/NAS alias at construction AND in the centralized
    # nas_access classifier (NasAccess::Aliased), so a post-config symlink alias can never make
    # archival delete the only copy nor make any NAS path report success (NAS indefinite retention).
    compact = _compact(src)
    for key in (
        "alias_guard_helper",
        "alias_guard_config_token",
        "alias_guard_classify_token",
        "alias_failclosed_variant",
    ):
        if _compact(spec[key]) not in compact:
            fail(
                "the tier must guard against an SSD/NAS directory alias on every NAS path "
                f"(missing `{spec[key]}`)"
            )
    return (
        "archive_cold is data-loss-safe: a cold record is dropped from SSD only when confirmed on "
        "NAS, else retained (retained_unconfirmed); NAS is never deleted, and an SSD/NAS alias "
        "(`.`/symlink) is rejected at config AND by the nas_access classifier (NasAccess::Aliased) so "
        "no NAS path can delete the only copy (indefinite retention)"
    )


def check_numeric_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    compact = _compact(src)
    for token in spec["integer_tokens"]:
        if _compact(token) not in compact:
            fail(f"tiering must use the integer boundary token `{token}`")
    code = _strip_comments(src)
    present = [t for t in spec["forbidden_tokens"] if t in code]
    if present:
        fail(f"tiering must use no floating-point in code, found: {', '.join(present)}")
    return "timestamps are i64 epoch seconds; no floating-point in the tier code"


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    code = _strip_comments(src)
    present = [t for t in spec["forbidden_tokens"] if t in code]
    if present:
        fail(
            "the tier core must read no wall-clock / spawn no threads / use no RNG, found: "
            f"{', '.join(present)}"
        )
    return (
        "the tier reads no wall-clock (now_ts is caller-supplied), spawns no threads, uses no RNG"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    token = contract_block(config)["module_reexport"]["lib_reexport_token"]
    if _compact(token) not in _compact(lib_src):
        fail(f"lib.rs must re-export the tier module (`{token}`)")
    return f"atp-data lib.rs exposes the tier module (`{token}`)"


def check_no_broker_dependency(config: dict, cargo_src: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    present = [t for t in spec["forbidden_dep_tokens"] if t in cargo_src]
    if present:
        fail(
            f"atp-data must not depend on the broker/execution crates, found: {', '.join(present)}"
        )
    return "atp-data carries no broker/execution dependency (one-way dependency direction)"


def check_no_vendor_tokens(config: dict, src: str) -> str:
    # Scan code only: a comment explaining the vendor-neutral provider->kind mapping may NAME the
    # vendors (Databento/Sharadar); what is forbidden is a vendor SDK token used in CODE.
    lowered = _strip_comments(src).lower()
    present = [t for t in contract_block(config)["vendor_forbidden_tokens"] if t in lowered]
    if present:
        fail(f"the vendor-neutral tier must name no vendor SDK, found: {', '.join(present)}")
    return (
        "the tier names no vendor SDK (provider-agnostic; the adapter layer maps provider -> kind)"
    )


def check_storage_growth_doc(config: dict, root: Path = ROOT) -> str:
    spec = contract_block(config)["storage_growth_doc"]
    doc = (root / spec["file"]).read_text(encoding="utf-8")
    for key in ("section_token", "ssd_token", "nas_token"):
        if spec[key] not in doc:
            fail(f"{spec['file']} must document the storage growth estimates (`{spec[key]}`)")
    return f"{spec['file']} §12.1 documents the SSD hot-tier + NAS archival-tier growth estimates"


def check_env_config_keys(config: dict, root: Path = ROOT) -> str:
    keys = contract_block(config)["env_config_keys"]
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    missing = [k for k in keys if k not in env_example]
    if missing:
        fail(f".env.example must declare the tier config keys: {', '.join(missing)}")
    return f".env.example declares the tier directory config keys ({', '.join(keys)})"


# Each entry: (label, collector, source_key). source_key selects which source the collector reads.
_STATIC_CHECKS = [
    ("config", check_config, "tiering"),
    ("tiered_store", check_tiered_store, "tiering"),
    ("ssd_first_ordering", check_ssd_first_ordering, "tiering"),
    ("nas_sync_status", check_nas_sync_status, "tiering"),
    ("retention_report", check_retention_report, "tiering"),
    ("archive_safety", check_archive_safety, "tiering"),
    ("numeric_boundary", check_numeric_boundary, "tiering"),
    ("determinism", check_determinism, "tiering"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("no_vendor_tokens", check_no_vendor_tokens, "tiering"),
]

_DEFERRED_OWNERS = [
    "route ALL production ingestion through TieredStore — the AC's 'all ingestion ... synced to NAS' "
    "clause (data016_ingest_cli retrofit + SRS-DATA-001/003/005/006); SRS-DATA-008 stays passes:false "
    "until then",
    "real provider network adapters (SRS-DATA-001/003/005/006)",
    "cold-read NAS failover (SRS-DATA-009)",
    "the eviction POLICY (SRS-DATA-010)",
    "real SSD/NAS capacity + network mount (NFR-SC2 / SRS-ARCH-004)",
]


def assert_data_tiering_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (reused by the L3 contract test)."""
    sources = {
        "tiering": tiering_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    evidence = [check(config, sources[key]) for _, check, key in _STATIC_CHECKS]
    # CLI presence + its fail-closed exit semantics + the doc / config-key inspections.
    block = contract_block(config)
    cli_src = cli_source(config, root)  # fail-closed if the operator CLI is absent
    if _compact(block["cli_failed_exit_token"]) not in _compact(cli_src):
        fail(
            "the operator CLI must exit NON-ZERO on a NAS archival integrity failure "
            f"(missing `{block['cli_failed_exit_token']}`) so automation cannot mistake a broken "
            "archive for a clean ingest"
        )
    evidence.append(
        f"operator CLI `{block['cli_bin']}` present (ingest/report/archive-cold/sync) and exits "
        "non-zero on a NasSyncStatus::Failed archival integrity failure"
    )
    evidence.append(check_storage_growth_doc(config, root))
    evidence.append(check_env_config_keys(config, root))
    return evidence


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["data_crate"]["crate"]
    integration = block["rust_integration_test"]
    cli_bin = block["cli_bin"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "tiered-storage path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib failed:\n{lib.stdout}\n{lib.stderr}")
    integ = subprocess.run(
        [cargo, "test", "-p", crate, "--test", integration, "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if integ.returncode != 0:
        fail(f"cargo test -p {crate} --test {integration} failed:\n{integ.stdout}\n{integ.stderr}")
    build_cli = subprocess.run(
        [cargo, "build", "-p", crate, "--bin", cli_bin, "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if build_cli.returncode != 0:
        fail(
            f"cargo build -p {crate} --bin {cli_bin} failed:\n{build_cli.stdout}\n{build_cli.stderr}"
        )
    return f"cargo test -p {crate} --lib + {integration} + build --bin {cli_bin}: PASS"


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_data_tiering_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-DATA-008 tiered-storage contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable tiering path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except TieringCheckError as error:
        print(f"SRS-DATA-008 TIERED-STORAGE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-DATA-008 TIERED-STORAGE PASS")
    for item in evidence:
        print(f"- {item}")
    print("- deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
