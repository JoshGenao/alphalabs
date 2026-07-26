#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-010 (SSD storage eviction policy).

SRS-DATA-010 (SyRS SYS-69; StRS C-5 / BG-6). The acceptance criterion: "At the default 80 percent
high-water mark, eviction prioritizes old inactive data; data for securities with the currently
running live strategy is never evicted; data accessed within the configurable recency window
defaulting to 24 hours by a running backtest or factor pipeline job is not evicted." SYS-68 adds:
cold-read cache entries are evicted BEFORE any hot runtime data.

This is the POLICY brain over the SRS-DATA-008 tier + SRS-DATA-009 cold-read primitives. The
structural contract lives in ``architecture/runtime_services.json`` (block
``storage_eviction_contract``):

  (a) POLICY   -- ``StoragePolicy`` (high-water default 80%, recency default 24h) computes the target
      ``floor(ssd_capacity_records * high_water / 100)`` in INTEGER arithmetic and fails closed on a
      0/>100 high-water or a negative recency.
  (b) PLANNER  -- ``plan_eviction`` is a PURE fn; a PINNED candidate (live / recently-accessed /
      in-retention-floor) NEVER enters the evictable set; the eviction order is Cold-before-Hot
      (SYS-68, via the derived ``Tier`` Ord), non-listed-before-listed, oldest-first (SYS-69).
  (c) ENFORCE  -- ``EvictionEngine::enforce`` physically evicts ONLY the cold-read cache: its write
      path (``evict_cache_keys``) references ``cold_cache_dir`` and NEVER the SSD primary ``ssd_dir``,
      so live/recent/hot data is structurally un-evictable (mirrors DATA-009 ``evict_cold_cache_to``).
  (d) JOURNAL  -- ``access_journal`` is the real AC-3 producer: writes fail open (``record`` delegates
      to ``append`` and discards), reads fail closed on a corrupt line (``AccessJournalError::Corrupt``),
      a torn tail is tolerated (``complete_lines``).
  (e) INSTRUMENT -- the backtest (``RecordingBarSource``) and factor (``assemble_factor_inputs_recorded``)
      read paths are instrumented ADDITIVELY: the existing read fns are unchanged and the wrappers
      delegate to them.
  (f) CLI      -- ``data010_eviction_cli`` (report / plan / enforce) refuses a destructive enforce
      without an explicit protection source and exits NON-ZERO when the mark cannot be met without
      evicting pinned/hot data.
  (g) DETERMINISM -- caller-supplied ``now_ts`` (no wall-clock), integer target (no float), no vendor SDK.

The PASS line is ``SRS-DATA-010 STORAGE-EVICTION PASS`` -- it names the deferred owners (the real
live-symbols feed via SRS-EXE-001/RESV, the running-job registry via the orchestrator WorkloadRegistry,
physical hot-pressure eviction, and real SSD byte capacity via NFR-SC2) that keep the feature
passes:false (serialized).

Invoke:
    python3 tools/data010_eviction_check.py
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


class EvictionCheckError(AssertionError):
    """Raised when a structural contract is violated."""


def fail(message: str) -> None:
    raise EvictionCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    block = config.get("storage_eviction_contract")
    if block is None:
        fail("runtime_services.json is missing the storage_eviction_contract block")
    return block


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


def _strip_comments(src: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", no_block)


def _strip_test_module(src: str) -> str:
    """Drop the trailing ``#[cfg(test)]`` module so scans cover only production code."""
    marker = src.find("#[cfg(test)]")
    return src if marker == -1 else src[:marker]


def _crate_dir(config: dict, root: Path) -> Path:
    return root / contract_block(config)["data_crate"]["path"]


def _module_source(config: dict, key: str, root: Path) -> str:
    module = contract_block(config)[key]
    path = _crate_dir(config, root) / "src" / f"{module}.rs"
    if not path.is_file():
        fail(f"module not found at {path}")
    return _strip_test_module(path.read_text(encoding="utf-8"))


def eviction_source(config: dict, root: Path = ROOT) -> str:
    return _module_source(config, "eviction_module", root)


def journal_source(config: dict, root: Path = ROOT) -> str:
    return _module_source(config, "access_journal_module", root)


def lib_source(config: dict, root: Path = ROOT) -> str:
    return (_crate_dir(config, root) / "src" / "lib.rs").read_text(encoding="utf-8")


def cli_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    path = _crate_dir(config, root) / "src" / "bin" / f"{block['cli_bin']}.rs"
    if not path.is_file():
        fail(f"operator CLI not found at {path}")
    return path.read_text(encoding="utf-8")


def bar_source(config: dict, root: Path = ROOT) -> str:
    path = root / contract_block(config)["instrumentation"]["bar_source_file"]
    if not path.is_file():
        fail(f"backtest read path not found at {path}")
    return _strip_test_module(path.read_text(encoding="utf-8"))


def factor_source(config: dict, root: Path = ROOT) -> str:
    path = root / contract_block(config)["instrumentation"]["factor_file"]
    if not path.is_file():
        fail(f"factor read path not found at {path}")
    return _strip_test_module(path.read_text(encoding="utf-8"))


def _fn_body(src: str, fn_name: str) -> str:
    """Extract a function body by brace balance. Fails closed if absent/unbalanced."""
    match = re.search(rf"fn\s+{re.escape(fn_name)}\b", src)
    if match is None:
        fail(f"expected `fn {fn_name}` is absent")
    start = src.index("{", match.end())
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    fail(f"`fn {fn_name}` has an unbalanced body")
    return ""  # unreachable


def _require(src: str, token: str, why: str) -> None:
    if _compact(token) not in _compact(src):
        fail(f"{why} (missing `{token}`)")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors.
# --------------------------------------------------------------------------- #


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    _require(lib_src, spec["eviction_token"], "lib.rs must expose the eviction module")
    _require(lib_src, spec["access_journal_token"], "lib.rs must expose the access_journal module")
    return "atp-data lib.rs exposes both the eviction and access_journal modules"


def check_policy_type(config: dict, src: str) -> str:
    spec = contract_block(config)["policy_type"]
    _require(src, f"struct {spec['struct']}", "eviction must define the StoragePolicy config type")
    _require(src, spec["target_fn"], "StoragePolicy must expose the integer target fn")
    for key, label in (
        ("default_high_water_const", "80% default high-water"),
        ("default_recency_const", "24h default recency window"),
        ("max_high_water_const", "100% max high-water"),
    ):
        _require(src, spec[key], f"eviction must declare the {label} constant")
    err_body = _compact(_enum_body(src, "EvictionError"))
    for variant in (spec["invalid_high_water_variant"], spec["invalid_recency_variant"]):
        if re.search(rf"\b{re.escape(variant)}\b", err_body) is None:
            fail(f"EvictionError must carry the fail-closed `{variant}` variant")
    return (
        "StoragePolicy computes target = floor(capacity*high_water/100) (integer), defaults 80% / 24h, "
        "and rejects a 0/>100 high-water or a negative recency fail-closed "
        "(InvalidHighWater / InvalidRecencyWindow)"
    )


def check_planner(config: dict, src: str) -> str:
    spec = contract_block(config)["planner"]
    _require(src, spec["fn"], "eviction must define the pure plan_eviction fn")
    _require(src, spec["tier_enum_token"], "eviction must define the Tier enum (Cold before Hot)")
    _require(src, f"struct {spec['engine_type']}", "eviction must define the EvictionEngine")
    # Cold-before-hot ordering falls out of the derived Tier Ord.
    body = _strip_comments(_fn_body(src, "plan_eviction"))
    _require(
        body, spec["cold_before_hot_token"], "plan_eviction must order Cold-before-Hot (tier cmp)"
    )
    # A pinned candidate NEVER enters the evictable set: evictable is pushed ONLY in the `None` arm of
    # the pin classification. If a mutation makes the push unconditional, this token disappears.
    _require(
        body,
        spec["pinned_excluded_token"],
        "plan_eviction must push to evictable ONLY when unpinned",
    )
    _require(src, spec["pin_reason_fn"], "eviction must classify pins via pin_reason")
    return (
        "plan_eviction is pure: pinned candidates (live/recent/retention via pin_reason) never enter "
        "the evictable set, and evictable is ordered Cold-before-Hot (SYS-68), non-listed-first, "
        "oldest-first (SYS-69)"
    )


def check_enforce_never_touches_hot(config: dict, src: str) -> str:
    spec = contract_block(config)["enforce_never_touches_hot"]
    body = _strip_comments(_fn_body(src, "evict_cache_keys"))
    _require(body, spec["cache_dir_token"], "the physical eviction must target the cold_cache_dir")
    _require(body, spec["save_token"], "the physical eviction must persist the trimmed cache")
    if spec["forbidden_ssd_primary_token"] in _compact(body):
        fail(
            "evict_cache_keys must NOT reference the SSD primary tier "
            f"(`{spec['forbidden_ssd_primary_token']}`): enforce must never physically evict hot data "
            "(SRS-DATA-010 'never evict live-strategy data' + SYS-68 'evicted before hot')"
        )
    return (
        "EvictionEngine::enforce physically rewrites ONLY the cold-read cache (evict_cache_keys touches "
        "cold_cache_dir + save_to_path, never ssd_dir), so live/recent/hot data is structurally "
        "un-evictable"
    )


def check_access_journal(config: dict, src: str) -> str:
    spec = contract_block(config)["access_journal"]
    _require(src, spec["recorder_trait"], "access_journal must define the AccessRecorder trait")
    _require(src, spec["noop_recorder"], "access_journal must define NoopRecorder")
    _require(src, spec["journal_struct"], "access_journal must define the durable AccessJournal")
    _require(src, spec["recent_fn"], "AccessJournal must expose the recency read")
    _require(src, spec["append_fn"], "AccessJournal must expose the append writer")
    _require(
        src, spec["torn_tail_fn"], "access_journal must split complete lines (torn-tail tolerance)"
    )
    _require(
        src,
        spec["fail_open_token"],
        "AccessJournal::record must fail open (delegate to append, discard)",
    )
    _require(
        src,
        spec["health_fn"],
        "access_journal must expose the fail-closed usability preflight (ensure_usable) so a caller "
        "that trusts the journal can refuse an unwritable one (writes fail open, so an unusable "
        "journal would silently under-protect)",
    )
    err_body = _compact(_enum_body(src, "AccessJournalError"))
    if re.search(rf"\b{re.escape(spec['corrupt_variant'])}\b", err_body) is None:
        fail("AccessJournalError must carry the fail-closed `Corrupt` variant")
    # The recency read raises Corrupt on a malformed complete line.
    recent_body = _strip_comments(_fn_body(src, "parse_line"))
    if spec["corrupt_variant"] not in recent_body:
        fail(
            "parse_line must fail closed with AccessJournalError::Corrupt on a malformed complete line"
        )
    return (
        "access_journal writes fail open (record -> append, discarded) and reads fail closed "
        "(a corrupt complete line -> AccessJournalError::Corrupt; a torn tail is tolerated via "
        "complete_lines)"
    )


def check_instrumentation(config: dict, bar_src: str, factor_src: str) -> str:
    spec = contract_block(config)["instrumentation"]
    # Existing read fns are UNCHANGED (still present) and the wrappers delegate to them.
    _require(
        bar_src, spec["bar_source_existing_daily"], "StoreBarSource::daily must remain (unchanged)"
    )
    _require(
        bar_src,
        spec["bar_source_existing_minute"],
        "StoreBarSource::minute must remain (unchanged)",
    )
    _require(bar_src, spec["bar_source_wrapper"], "the backtest path must add RecordingBarSource")
    _require(
        bar_src,
        spec["bar_source_delegate_token"],
        "RecordingBarSource must delegate to the inner source",
    )
    _require(
        factor_src, spec["factor_existing_fn"], "assemble_factor_inputs must remain (unchanged)"
    )
    _require(
        factor_src,
        spec["factor_recorded_fn"],
        "the factor path must add assemble_factor_inputs_recorded",
    )
    _require(
        factor_src,
        spec["factor_delegate_token"],
        "the recorded assembler must delegate to assemble_factor_inputs",
    )
    # The CANONICAL scheduled factor path must remain AND have a recording-capable entry point, so a
    # running factor job leaves recency evidence (not only the bare assembler helper).
    _require(
        factor_src,
        spec["factor_scheduled_existing_fn"],
        "run_scheduled_factor_job_over_store must remain (unchanged)",
    )
    _require(
        factor_src,
        spec["factor_scheduled_recorded_fn"],
        "the canonical scheduled factor path must have a recording-capable entry point "
        "(run_scheduled_factor_job_over_store_recorded) so a running factor job records recency",
    )
    return (
        "the backtest (RecordingBarSource), the factor assembler (assemble_factor_inputs_recorded), and "
        "the canonical scheduled factor path (run_scheduled_factor_job_over_store_recorded) are "
        "instrumented ADDITIVELY: the existing read fns remain and the recording paths delegate to them"
    )


def check_cli(config: dict, cli_src: str) -> str:
    spec = contract_block(config)["cli"]
    for key in ("report_subcommand", "plan_subcommand", "enforce_subcommand"):
        _require(cli_src, spec[key], f"the CLI must wire the {key.split('_')[0]} subcommand")
    _require(
        cli_src,
        spec["fail_closed_gate_token"],
        "enforce must refuse without an explicit protection source (fail-closed gate)",
    )
    _require(
        cli_src,
        spec["nonzero_on_breach_token"],
        "enforce must exit NON-ZERO when the high-water mark cannot be met (reached_target false)",
    )
    _require(
        cli_src,
        spec["journal_health_token"],
        "--use-journal must fail closed on an unusable journal (call ensure_usable before trusting it)",
    )
    return (
        "data010_eviction_cli wires report/plan/enforce, refuses a destructive enforce without an "
        "explicit protection source, fails closed on an unusable journal under --use-journal, and exits "
        "non-zero when the mark cannot be met without pinned/hot data"
    )


def check_numeric_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    code = _strip_comments(src)
    for token in spec["integer_tokens"]:
        _require(code, token, "the eviction target must be integer arithmetic")
    for token in spec["forbidden_tokens"]:
        if token in code:
            fail(
                f"eviction must contain NO floating point (`{token}`) — the target is integer arithmetic"
            )
    return "eviction target is integer arithmetic (saturating_mul + /100); no f32/f64 in code"


def check_determinism(config: dict, evict_src: str, journal_src: str) -> str:
    spec = contract_block(config)["determinism"]
    for label, src in (("eviction", evict_src), ("access_journal", journal_src)):
        code = _strip_comments(src)
        for token in spec["forbidden_tokens"]:
            if token in code:
                fail(f"{label} must read no wall-clock / RNG / thread primitive (`{token}`)")
    _require(
        evict_src,
        spec["now_param_token"],
        "the eviction policy must be a fn of the caller-supplied now_ts",
    )
    return "eviction + access_journal read no wall-clock (now_ts is caller-supplied), no RNG, no threads"


# Each entry: (label, collector, source_keys).
_STATIC_CHECKS = [
    ("module_reexport", check_module_reexport, ("lib",)),
    ("policy_type", check_policy_type, ("eviction",)),
    ("planner", check_planner, ("eviction",)),
    ("enforce_never_touches_hot", check_enforce_never_touches_hot, ("eviction",)),
    ("access_journal", check_access_journal, ("journal",)),
    ("instrumentation", check_instrumentation, ("bar", "factor")),
    ("cli", check_cli, ("cli",)),
    ("numeric_boundary", check_numeric_boundary, ("eviction",)),
    ("determinism", check_determinism, ("eviction", "journal")),
]


def assert_eviction_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (reused by the L3 contract test)."""
    sources = {
        "eviction": eviction_source(config, root),
        "journal": journal_source(config, root),
        "lib": lib_source(config, root),
        "cli": cli_source(config, root),
        "bar": bar_source(config, root),
        "factor": factor_source(config, root),
    }
    return [check(config, *[sources[k] for k in keys]) for _, check, keys in _STATIC_CHECKS]


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    """Behavioral smoke: run the eviction + access_journal unit tests and build the operator CLI."""
    block = contract_block(config)
    crate = block["data_crate"]["crate"]
    cli_bin = block["cli_bin"]
    if shutil.which("cargo") is None:
        if require_cargo:
            fail("cargo is required for the behavioral smoke but is not on PATH")
        return "cargo not available — behavioral smoke skipped (structural checks still ran)"
    # cargo test takes a single positional filter, so run the two module suites separately.
    for module_filter in ("eviction::", "access_journal::"):
        test = subprocess.run(
            ["cargo", "test", "-p", crate, "--lib", module_filter],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if test.returncode != 0:
            fail(
                f"`cargo test -p {crate} --lib {module_filter}` failed:\n{test.stdout}\n{test.stderr}"
            )
    build = subprocess.run(
        ["cargo", "build", "-p", crate, "--bin", cli_bin],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        fail(f"`cargo build -p {crate} --bin {cli_bin}` failed:\n{build.stdout}\n{build.stderr}")
    return f"cargo test -p {crate} --lib eviction/access_journal + build --bin {cli_bin}: PASS"


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_eviction_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    evidence.append("deferred to: " + "; ".join(contract_block(config)["deferred_owners"]))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SRS-DATA-010 storage eviction policy contract check"
    )
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="fail (not skip) the behavioral smoke if cargo is unavailable",
    )
    args = parser.parse_args(argv)
    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except EvictionCheckError as err:
        print(f"SRS-DATA-010 STORAGE-EVICTION FAIL\n  - {err}", file=sys.stderr)
        return 1
    for line in evidence:
        print(f"- {line}")
    print("SRS-DATA-010 STORAGE-EVICTION PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
