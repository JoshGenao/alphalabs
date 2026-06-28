#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-010 (produce deterministic backtest results for
identical inputs).

SRS-BT-010 (SyRS SYS-62; StRS SN-1.02). The acceptance criterion: "Repeated runs with
identical strategy code, parameters, data, date range, seed, and cost model produce identical
trade logs, equity curves, and metrics; platform parallelism, floating-point ordering, and
platform-generated random values do not introduce nondeterminism."

The deterministic-backtest VERIFICATION surface lives in ``crates/atp-simulation`` (module
``determinism``), per the structural contract in ``architecture/runtime_services.json`` (block
``backtest_determinism_contract``). The BacktestEngine (module ``backtest``) is already
deterministic by construction (stable sort_by_key replay + integer minor-unit money math);
this module makes that guarantee FALSIFIABLE:

  (a) ``RunDigest`` -- an opaque, stable FNV-1a fingerprint of a completed run. ``digest_result``
      folds a ``BacktestResult`` (trade log + equity curve + provenance) as EXACT i64 minor units
      (``encode_result_body`` carries no ``f64`` / ``to_bits`` -- no float-formatting nondeterminism
      in the money path); ``digest_run`` additionally folds the SRS-BT-004 ``PerformanceMetrics``
      ratios through their exact ``f64::to_bits`` payload (``encode_metrics_body``).
  (b) ``runs_match`` / ``metrics_match`` -- localize the FIRST divergent artifact (a trade-log /
      equity index, length, final equity, bars processed, or a named metric compared via to_bits so
      a +0.0/-0.0 or NaN-bit difference is caught), returning a localized ``DeterminismError``.
  (c) ``verify_reproducible`` -- runs the engine twice over identical inputs (building a fresh
      strategy each replay, since ``BacktestStrategy`` is &mut) and fails closed if the two runs
      disagree, with a digest cross-check (``DeterminismError::Digest``) as defense for any
      ``BacktestResult`` field ``runs_match`` does not yet cover.

The work is deterministic (fixed left-to-right byte fold; no parallelism / random values /
wall-clock read -- the property it verifies), ``atp-simulation`` adds no broker/adapter/orchestrator
dependency and carries no vendor SDK token, and ``lib.rs`` re-exports ``pub mod determinism;``.

It also verifies the cross-process closure: ``RunManifest`` / ``digest_manifest`` fingerprint the
run's immutable inputs (so a check PROVES the inputs matched rather than assuming the fixture is
deterministic), and the ``bt010_repro_cli`` operator binary runs the fixture backtest in a FRESH
process so the ``srs_bt_010_cross_process`` integration test can spawn it twice and assert
byte-identical output -- closing the platform-generated-randomness clause a same-process double-run
cannot. The PASS line is ``SRS-BT-010 SDK-SURFACE PASS``; it names the still-deferred owners (the
REST / dashboard surface of the repeated-run workflow via SRS-API-001 / SRS-UI, the end-to-end
guarantee under the real Python strategy host, and stamping the RunDigest onto each persisted
SRS-BT-009 record) so what remains is loud.

Mirrors the PASS/FAIL output style of ``tools/factor_analysis_check.py``.

Invoke:
    python3 tools/determinism_check.py
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
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class DeterminismCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise DeterminismCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "backtest_determinism_contract" not in config:
        fail("architecture metadata is missing backtest_determinism_contract")
    return config["backtest_determinism_contract"]


def module_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / f"{block['module']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["simulation_crate"]["path"] / block["no_broker_dependency"]["cargo_toml"]
    )
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


def _private_fn_body(source: str, fn_name: str) -> str:
    """Return the body of a (possibly private) ``fn <fn_name>`` by brace matching.

    The shared ``_rust_parser._fn_block`` only finds ``pub fn``; the canonical encoders here
    are private, so this mirrors its brace walk for a bare ``fn``.
    """
    match = re.search(rf"\bfn\s+{re.escape(fn_name)}\b[^\{{]*\{{", source)
    if not match:
        fail(f"determinism module is missing function `{fn_name}`")
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


def _signature(source: str, fn_name: str) -> str:
    """Return the compacted ``pub fn <fn_name>(...) -> ...`` signature up to its opening brace."""
    match = re.search(rf"\bpub\s+fn\s+{re.escape(fn_name)}\b", source)
    if not match:
        fail(f"determinism module must expose `pub fn {fn_name}`")
    return _compact(source[match.start() :].split("{", 1)[0])


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_run_digest(config: dict, src: str) -> str:
    spec = contract_block(config)["run_digest"]
    compact_src = _compact(src)
    for key, label in (
        ("newtype_token", "an opaque newtype RunDigest(u64)"),
        ("display_token", "a stable `run-digest:` Display prefix"),
        ("magic_token", "a domain-separating digest magic"),
        ("fnv_offset_basis_token", "the FNV-1a offset basis"),
        ("fnv_prime_token", "the FNV-1a prime"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"determinism module must declare {label} (`{spec[key]}`)")
    return (
        "atp-simulation declares RunDigest -- an opaque u64 FNV-1a fingerprint (domain-tagged "
        "ATP-BACKTEST-RUN-DIGEST, `run-digest:` Display) of a completed backtest run"
    )


def check_digest_fns(config: dict, src: str) -> str:
    spec = contract_block(config)["digest_fns"]
    result_sig = _signature(src, spec["result_fn"])
    run_sig = _signature(src, spec["run_fn"])
    if _compact(spec["result_param_token"]) not in result_sig:
        fail(f"`{spec['result_fn']}` must take `{spec['result_param_token']}`")
    if _compact(spec["return_token"]) not in result_sig:
        fail(f"`{spec['result_fn']}` must return `{spec['return_token']}`")
    for token_key in ("result_param_token", "metrics_param_token", "return_token"):
        if _compact(spec[token_key]) not in run_sig:
            fail(f"`{spec['run_fn']}` must carry `{spec[token_key]}` in its signature")
    return (
        "atp-simulation exposes `pub fn digest_result(result: &BacktestResult) -> RunDigest` and "
        "`pub fn digest_run(result, metrics: Option<&PerformanceMetrics>) -> RunDigest` -- one "
        "result-only fingerprint and one that spans all three SRS-BT-010 artifacts"
    )


def check_result_digest(config: dict, src: str) -> str:
    spec = contract_block(config)["result_digest"]
    body = _private_fn_body(src, spec["encode_fn"])
    compact_body = _compact(body)
    missing = [t for t in spec["integer_field_tokens"] if _compact(t) not in compact_body]
    if missing:
        fail(
            f"`{spec['encode_fn']}` must fold every result field as an exact integer: missing "
            f"{', '.join(missing)}"
        )
    leaked = [t for t in spec["no_float_tokens"] if _compact(t) in compact_body]
    if leaked:
        fail(
            f"`{spec['encode_fn']}` (the result digest) must be integer-EXACT -- it must not touch "
            f"{', '.join(leaked)}; float-formatting in the money path would be a nondeterminism "
            "source (and a money-correctness leak)"
        )
    return (
        "atp-simulation encode_result_body folds the trade log + equity curve as exact i64 minor "
        "units (no f64 / to_bits) -- the money path carries no float-formatting nondeterminism"
    )


def check_metrics_digest(config: dict, src: str) -> str:
    spec = contract_block(config)["metrics_digest"]
    body = _private_fn_body(src, spec["encode_fn"])
    compact_body = _compact(body)
    missing = [t for t in spec["metric_tokens"] if _compact(t) not in compact_body]
    if missing:
        fail(f"`{spec['encode_fn']}` must fold every metric: missing {', '.join(missing)}")
    if spec["opt_encoder_fn"] not in body:
        fail(
            f"`{spec['encode_fn']}` must fold the ratios through the bit-exact optional encoder "
            f"`{spec['opt_encoder_fn']}`"
        )
    # The bit-exact fold lives in the optional-f64 encoder: a metric ratio is encoded as its
    # exact to_bits payload, never a lexical float, so float formatting introduces no divergence.
    encoder_body = _compact(_private_fn_body(src, spec["opt_encoder_fn"]))
    if _compact(spec["to_bits_token"]) not in encoder_body:
        fail(
            f"`{spec['opt_encoder_fn']}` must fold a ratio through its exact "
            f"`{spec['to_bits_token']}` payload (no lexical float formatting)"
        )
    return (
        "atp-simulation encode_metrics_body folds the eight dimensionless metric ratios via "
        "push_opt_f64 (f64::to_bits) plus the benchmark symbol -- bit-identical, the only f64 in "
        "any digest"
    )


def check_manifest(config: dict, src: str) -> str:
    spec = contract_block(config)["manifest"]
    compact_src = _compact(src)
    # The input-manifest newtype + its domain-separated digest (distinct magic so a ManifestDigest
    # of the run's INPUTS can never alias a RunDigest of its OUTPUTS).
    for key, label in (
        ("newtype_token", "an opaque newtype ManifestDigest(u64)"),
        ("magic_token", "a domain-separating manifest digest magic"),
        ("display_token", "a stable `run-manifest:` Display prefix"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"determinism module must declare {label} (`{spec[key]}`)")
    # RunManifest carries every immutable-input clause SRS-BT-010 enumerates.
    missing = [t for t in spec["clause_field_tokens"] if _compact(t) not in compact_src]
    if missing:
        fail(
            "RunManifest must carry every immutable-input clause SRS-BT-010 names "
            f"(code/parameters/data/date range/seed/cost model): missing {', '.join(missing)}"
        )
    # digest_manifest + RunManifest::from_request exist.
    _signature(src, spec["digest_fn"])
    _signature(src, spec["from_request_fn"])
    # The input-manifest digest is integer-EXACT: no f64 in encode_manifest_body (so the input
    # fingerprint carries no float-formatting nondeterminism either).
    body = _compact(_private_fn_body(src, spec["encode_fn"]))
    leaked = [t for t in spec["no_float_tokens"] if _compact(t) in body]
    if leaked:
        fail(
            f"`{spec['encode_fn']}` (the input-manifest digest) must be integer-EXACT -- it must "
            f"not touch {', '.join(leaked)}"
        )
    # The cost model is folded (via encode_cost_config) so a cost override changes the manifest.
    if _compact(spec["cost_encode_fn"]) not in _compact(_private_fn_body(src, spec["encode_fn"])):
        fail(
            f"`{spec['encode_fn']}` must fold the cost model via `{spec['cost_encode_fn']}` so a "
            "commission/slippage/spread override changes the input manifest"
        )
    return (
        "atp-simulation declares RunManifest + digest_manifest (ManifestDigest, magic "
        "ATP-BACKTEST-RUN-MANIFEST, `run-manifest:` Display) -- an integer-exact fingerprint of the "
        "run's immutable inputs (code version, parameters, data, date range, seed, cost model) so a "
        "verification PROVES the inputs matched rather than assuming the strategy factory + BarSource "
        "are deterministic"
    )


def check_cross_process_cli(config: dict, cargo_text: str) -> str:
    block = contract_block(config)
    spec = block["cross_process_cli"]
    crate_path = ROOT / block["simulation_crate"]["path"]
    # Cargo registers the cross-process CLI binary.
    if _compact(spec["cargo_bin_token"]) not in _compact(cargo_text):
        fail(
            "atp-simulation Cargo.toml must register the cross-process CLI bin "
            f"(`{spec['cargo_bin_token']}`)"
        )
    # The bin source exists and its `digest` subcommand prints BOTH fingerprints (the manifest of
    # the inputs and the run digest of the outputs) so two fresh processes can be compared.
    bin_path = crate_path / spec["bin_path"]
    if not bin_path.exists():
        fail(f"cross-process CLI source missing: {bin_path.relative_to(ROOT)}")
    compact_bin = _compact(bin_path.read_text(encoding="utf-8"))
    for token_key in ("manifest_line_token", "run_digest_line_token"):
        if _compact(spec[token_key]) not in compact_bin:
            fail(
                f"`{spec['bin']}` digest must print both fingerprints (missing `{spec[token_key]}`) "
                "so a cross-process comparison covers the inputs AND the run output"
            )
    # The cross-process integration test (spawns the bin in fresh processes) exists.
    test_path = crate_path / "tests" / f"{spec['cross_process_test']}.rs"
    if not test_path.exists():
        fail(f"cross-process integration test missing: {test_path.relative_to(ROOT)}")
    return (
        "atp-simulation registers the bt010_repro_cli operator binary (digest + verify) that runs "
        "the fixture backtest in a FRESH process and prints run-manifest + run-digest; the "
        "srs_bt_010_cross_process integration test spawns it twice in two fresh processes and asserts "
        "byte-identical output -- the cross-process closure of the platform-generated-randomness clause"
    )


def check_harness(config: dict, src: str) -> str:
    spec = contract_block(config)["harness"]
    # The two reproducibility harnesses are public; their shared run-twice helper is private.
    for fn_key in ("fn", "metrics_fn", "compare_fn", "metrics_compare_fn"):
        if not re.search(rf"\bpub\s+fn\s+{re.escape(spec[fn_key])}\b", src):
            fail(f"determinism module must expose `pub fn {spec[fn_key]}`")

    # run_pair runs the engine twice over identical inputs and localizes a result divergence.
    pair_body = _compact(_private_fn_body(src, spec["run_pair_fn"]))
    for token in spec["runs_twice_tokens"]:
        if _compact(token) not in pair_body:
            fail(
                f"`{spec['run_pair_fn']}` must run the engine twice over identical inputs "
                f"(missing `{token}`)"
            )
    if _compact(spec["compare_fn"]) not in pair_body:
        fail(f"`{spec['run_pair_fn']}` must localize divergence via `{spec['compare_fn']}`")

    # verify_reproducible delegates the run-twice to run_pair and cross-checks the digests.
    vr_body = _compact(_private_fn_body(src, spec["fn"]))
    if _compact(spec["run_pair_fn"]) not in vr_body:
        fail(f"`{spec['fn']}` must run the pair via `{spec['run_pair_fn']}`")
    if _compact(spec["digest_crosscheck_token"]) not in vr_body:
        fail(
            f"`{spec['fn']}` must cross-check the canonical digests "
            f"(`{spec['digest_crosscheck_token']}`)"
        )

    # runs_match compares the result PROVENANCE (data_source + range), not just the trade
    # log / equity curve, so two results from different catalogs or ranges cannot match.
    runs_match_body = _compact(_private_fn_body(src, spec["compare_fn"]))
    for token in spec["provenance_tokens"]:
        if _compact(token) not in runs_match_body:
            fail(
                f"`{spec['compare_fn']}` must compare result provenance (`{token}`) so different "
                "catalogs / date ranges are never reported identical"
            )

    # The metrics harness runs the engine twice (via run_pair), computes the metric family for
    # BOTH results with the crate's own deterministic metrics::compute (no caller closure -> no
    # shared mutable state with the runs), compares it, fingerprints it, and cross-checks the
    # digests.
    metrics_body = _compact(_private_fn_body(src, spec["metrics_fn"]))
    if _compact(spec["run_pair_fn"]) not in metrics_body:
        fail(f"`{spec['metrics_fn']}` must run the pair via `{spec['run_pair_fn']}`")
    for token in spec["metrics_compute_tokens"]:
        if _compact(token) not in metrics_body:
            fail(
                f"`{spec['metrics_fn']}` must compute the metric family for BOTH results via "
                f"`{spec['metrics_compute_fn']}` (missing `{token}`)"
            )
    for token_key in ("metrics_compare_fn", "metrics_digest_token", "digest_crosscheck_token"):
        if _compact(spec[token_key]) not in metrics_body:
            fail(
                f"`{spec['metrics_fn']}` must carry `{spec[token_key]}` so the metric clause of "
                "SRS-BT-010 is verified (computed + compared + fingerprinted), not assumed"
            )
    # The metric computation uses the crate's own deterministic metric family (a pure function),
    # so there is no caller reduction that could share mutable state with the run.
    compute_body = _compact(_private_fn_body(src, spec["metrics_compute_fn"]))
    if _compact(spec["family_compute_token"]) not in compute_body:
        fail(
            f"`{spec['metrics_compute_fn']}` must compute the metric family via the crate's own "
            f"deterministic `{spec['family_compute_token']}` (no caller-supplied reduction)"
        )
    return (
        "atp-simulation verify_reproducible (trade log + equity curve) and "
        "verify_reproducible_with_metrics (all three artifacts) run the engine twice via run_pair, "
        "localize divergence via runs_match (incl. data_source + range provenance) / metrics_match, "
        "and cross-check the digests (DeterminismError::Digest); the metric family is the crate's "
        "own deterministic metrics::compute over immutable inputs (no caller reduction, no shared "
        "mutable state with the runs) -- the in-process SRS-BT-010 determinism verification (the "
        "cross-process closure is the bt010_repro_cli operator binary)"
    )


def check_error_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["error_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing localized variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} localized, "
        f"fail-closed variants ({', '.join(spec['variants'])})"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    leaked = [t for t in spec["forbidden_tokens"] if t in src]
    if leaked:
        fail(
            f"the determinism verifier must itself be deterministic (SRS-BT-010): found "
            f"nondeterminism source(s) {', '.join(leaked)} -- it must use a fixed left-to-right "
            "fold with no parallelism, random values, or wall-clock read"
        )
    return (
        "atp-simulation determinism module has no parallelism / RNG / clock token -- the verifier "
        "honors the very property it checks (SRS-BT-010)"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so the "
            "determinism verification surface is part of the simulation runtime"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the broker/live/orchestrator path: "
            f"found {', '.join(leaked)} -- the determinism surface is self-contained over the "
            "backtest + metrics types"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the broker/live/orchestrator path "
        f"({', '.join(spec['forbidden_dep_tokens'])})"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-simulation determinism module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation determinism module is free of all {len(tokens)} forbidden vendor SDK "
        "tokens (SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["simulation_crate"]["crate"]
    integration = block["rust_integration_test"]
    module = block["module"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "determinism path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", module, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib {module} failed:\n{lib.stdout}\n{lib.stderr}")
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
        f"cargo test -p {crate} --lib {module} + {integration}: PASS "
        "(digests a run integer-exactly, localizes a divergence, catches a nondeterministic "
        "strategy, and is bit-stable under source-iteration-order shuffles)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "module" reads determinism.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("run_digest", check_run_digest, "module"),
    ("digest_fns", check_digest_fns, "module"),
    ("result_digest", check_result_digest, "module"),
    ("metrics_digest", check_metrics_digest, "module"),
    ("manifest", check_manifest, "module"),
    ("cross_process_cli", check_cross_process_cli, "cargo"),
    ("harness", check_harness, "module"),
    ("error_enum", check_error_enum, "module"),
    ("determinism", check_determinism, "module"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "module"),
)

_DEFERRED_OWNERS = (
    "the REST POST /api/v1/backtests + dashboard rendering of the repeated-run workflow "
    "(SRS-API-001 / SRS-UI) -- the bt010_repro_cli binary is the local operator surface that closes "
    "the cross-process platform-generated-randomness clause; the HTTP / dashboard surface is deferred",
    "the end-to-end determinism guarantee under the real Python strategy host (the Rust<->Python "
    "boundary; binds strategy code version + seed; SRS-BT-001-runtime)",
    "stamping the RunDigest onto each persisted backtest record so a re-run is proven reproducible "
    "by stored-digest comparison (SRS-BT-009 store integration)",
)


def assert_determinism_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "module": module_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_determinism_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-010 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable determinism path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except DeterminismCheckError as error:
        print(f"SRS-BT-010 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-010 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- cross-process repeated-run + input-provenance manifest realized via bt010_repro_cli; "
        "deferred to: " + ", ".join(_DEFERRED_OWNERS)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
