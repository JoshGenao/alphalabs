#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-007 (foundational): the Python store-history binding.

SRS-DATA-007 (provide a unified historical data access interface; SyRS SYS-27 / SYS-53). The
acceptance: "Strategy code, backtests, factor jobs, and notebooks query by symbol, date range, and
resolution WITHOUT specifying the original source provider."

``tools/unified_query_check.py`` pins the *Rust* substrate (the source-neutral ``query_unified`` engine
+ ``data007_query_cli`` operator surface). THIS script pins the FIRST in-process consumer **binding**:
the Python ``StoreBackedHistoricalData`` (``python/atp_strategy/store_history.py``), a concrete
``atp_strategy.api.HistoricalData`` implementation that drives the lock-free, source-neutral
``data007_query_cli`` so a real named consumer (strategy code / backtest / factor job / notebook) reads
ingested data by symbol/date-range/resolution with no provider named. The binding serves
``NormalizationMode.RAW`` (verbatim) and the HistoricalData Protocol default
``NormalizationMode.SPLIT_ADJUSTED`` — the latter ONLY through the SRS-DATA-011 coverage-enforcing gate
(``MarketDataStore::query_split_adjusted`` on the operator CLI; see ``tools/coverage_manifest_check.py``
and ``tools/normalization_modes_check.py``). An uncovered split-adjusted query fails closed with
``CoverageNotProvenError`` (naming SRS-DATA-011), never raw-as-adjusted, and the binding validates the
``coverage_through`` frontier the gate echoes (gate-integrity). ``FULLY_ADJUSTED`` / ``TOTAL_RETURN`` stay
deferred (dividend data, SRS-DATA-012). SRS-DATA-007 STAYS passes:false (foundational): the BACKTEST consumer
is now genuinely wired (atp-simulation ``StoreBarSource`` consumes the unified store in ``BacktestEngine::run``)
and strategy + notebook/research code read via this binding (``tests/domain/test_store_history_consumer.py``);
deferred -- the factor-job EXECUTION path (``run_factor_job`` still takes caller-supplied inputs; the
atp-factor-pipeline ``store_inputs`` loader is shipped substrate, and a complete run needs Sharadar
fundamentals, SRS-DATA-005) and the Jupyter notebook HOST runtime (SRS-RES-002).

It is a SEPARATE script from ``unified_query_check.py`` so that script's hard-coded check-count
assertions (``tests/test_unified_query_contract.py``) stay valid -- mirroring how
``historical_data_check.py`` (the API-7 adapter trait) is already split from ``unified_query_check.py``
(the DATA-007 engine).

Static checks (no cargo; used by the L3 contract test):
  (a) the module declares ``StoreBackedHistoricalData`` with the ``get_bars`` Protocol method;
  (b) the public query methods carry NO provider/vendor/source/feed/adapter parameter (source-neutral
      INPUT -- a consumer cannot specify an origin);
  (c) no origin field is read off the result (no ``["provider"]`` / ``["source"]`` style key read);
  (d) normalization honesty -- the binding serves RAW and the gated SPLIT_ADJUSTED (the Protocol default),
      maps SPLIT_ADJUSTED to the 'split-adjusted' CLI label, validates the echoed ``coverage_through``
      frontier (gate-integrity), and fails closed on fully-adjusted / total-return (SRS-DATA-012); an
      uncovered split-adjusted query fails closed naming SRS-DATA-011, never raw-as-adjusted;
  (e) money math -- ``_PRICE_MINOR_SCALE`` is named and applied to the OHLC fields, and ``volume`` is a
      raw count that is NEVER divided by the scale;
  (f) the subprocess is invoked with a LIST argv under a bounded timeout and never ``shell=True``
      (no shell injection, no indefinite hang);
  (g) an empty match (``match_count:0``) is a returned ``[]`` value, never an error;
  (h) the parser fails closed unless the record indexes cover exactly [0, match_count), the
      CLI-echoed symbol/resolution/start/end match the request, every record's event_ts is inside
      the requested inclusive range, and the records are event_ts-ascending (no partial / relabelled
      / future-stale / misordered history).

Plus a cargo round-trip smoke (``--require-cargo``): build the data CLIs, ingest a fixture batch via
``data016_ingest_cli``, then read it back through the Python binding and assert the expected OHLCV bar.

The PASS line is ``SRS-DATA-007 STORE-HISTORY BINDING PASS``. Mirrors the PASS/FAIL output style of
``tools/unified_query_check.py``.

Invoke:
    python3 tools/store_history_check.py [--require-cargo]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StoreHistoryCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise StoreHistoryCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "store_history_binding_contract" not in config:
        fail("architecture metadata is missing store_history_binding_contract")
    return config["store_history_binding_contract"]


def module_source(config: dict, root: Path = ROOT) -> str:
    path = root / contract_block(config)["module"]["path"]
    if not path.exists():
        fail(f"source missing: {path.relative_to(root)}")
    return path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so reformatting cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors (each takes the module source so the L3 test can
# inject a regression and prove the guard is non-vacuous).
# --------------------------------------------------------------------------- #


def check_module_and_class(config: dict, src: str) -> str:
    spec = contract_block(config)["module"]
    cls = spec["class"]
    if not re.search(rf"\bclass\s+{re.escape(cls)}\b", src):
        fail(f"store-history module must declare `class {cls}`")
    for method in spec["methods"]:
        if not re.search(rf"\bdef\s+{re.escape(method)}\b", src):
            fail(f"{cls} must implement `{method}` (the HistoricalData binding surface)")
    return (
        f"{cls} implements {', '.join(spec['methods'])} -- a concrete "
        f"{contract_block(config)['protocol']} binding over the durable store"
    )


def check_source_neutral_signature(config: dict, src: str) -> str:
    spec = contract_block(config)
    forbidden = {t.lower() for t in spec["forbidden_provider_tokens"]}
    methods = set(spec["module"]["methods"])
    try:
        tree = ast.parse(src)
    except SyntaxError as error:  # pragma: no cover - defensive
        fail(f"store-history module does not parse: {error}")
    leaked: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in methods:
            seen.add(node.name)
            a = node.args
            names = [arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
            if a.vararg:
                names.append(a.vararg.arg)
            if a.kwarg:
                names.append(a.kwarg.arg)
            leaked += [f"{node.name}({n})" for n in names if n.lower() in forbidden]
    if leaked:
        fail(
            "the query binding must accept NO origin-provider parameter (SRS-DATA-007 queries "
            f"'without specifying the original source provider'): found {', '.join(leaked)}"
        )
    missing = methods - seen
    if missing:
        fail(f"could not locate method definition(s) to inspect: {', '.join(sorted(missing))}")
    return (
        f"the binding's {', '.join(sorted(seen))} carry no "
        f"{'/'.join(spec['forbidden_provider_tokens'])} parameter -- a consumer cannot name a source"
    )


def check_no_origin_field_read(config: dict, src: str) -> str:
    forbidden = contract_block(config)["forbidden_provider_tokens"]
    pattern = re.compile(r"""\[\s*['"](""" + "|".join(re.escape(t) for t in forbidden) + r""")['"]""")
    hit = pattern.search(src)
    if hit:
        fail(
            f"the binding must not read an origin field off the result: found a `[{hit.group(0)[1:]}]` "
            "key read -- the source-neutral result envelope names no provider"
        )
    return (
        "the binding reads no origin-provider field off the result (no "
        f"{'/'.join(forbidden)} key read) -- it parses only event_ts + vendor-neutral value fields"
    )


def check_normalization_honesty(config: dict, src: str) -> str:
    spec = contract_block(config)
    compact = _compact(src)
    if "raiseNotImplementedError" not in compact:
        fail(
            "the binding must raise NotImplementedError for every normalization mode it does not serve "
            "(fully-adjusted / total-return are deferred) -- never return raw bars as adjusted"
        )
    # The binding fails closed for any mode NOT in its served label map (fully-adjusted / total-return,
    # which additionally need dividend data, SRS-DATA-012).
    if "normalizationnotin_NORMALIZATION_LABEL" not in compact:
        fail(
            "the binding must fail closed for any normalization mode it does not serve "
            "(`normalization not in _NORMALIZATION_LABEL`)"
        )
    # The query methods must keep the HistoricalData Protocol default (SPLIT_ADJUSTED) so a caller that
    # omits normalization gets the gated split-adjusted series (or a loud CoverageNotProvenError when the
    # symbol is not covered), never a silent RAW default where the Protocol promises adjusted.
    if "normalization:NormalizationMode=NormalizationMode.SPLIT_ADJUSTED" not in compact:
        fail(
            "the binding's query methods must default normalization to SPLIT_ADJUSTED (matching the "
            "HistoricalData Protocol) so an omitted normalization serves the gated adjusted series -- a "
            "RAW default would silently serve raw bars where the Protocol promises adjusted"
        )
    # The binding MUST serve split-adjusted by mapping it to the coverage-gated CLI label: the
    # data007_query_cli routes --normalization split-adjusted through MarketDataStore::query_split_adjusted,
    # which fails closed (naming SRS-DATA-011) when the symbol is not covered through the query end.
    if 'NormalizationMode.SPLIT_ADJUSTED:"split-adjusted"' not in compact:
        fail(
            "the binding must map SPLIT_ADJUSTED to the 'split-adjusted' CLI label so it serves the "
            "coverage-gated adjusted series (data007_query_cli routes it through the SRS-DATA-011 gate)"
        )
    # Gate-integrity: a split-adjusted response MUST carry the coverage_through frontier (proving it
    # passed the gate); the binding fails closed on a split-adjusted response that omits it.
    if "coverage_through" not in compact:
        fail(
            "the binding must validate the coverage_through frontier on a split-adjusted response "
            "(gate-integrity) -- a split-adjusted label without a proven frontier is un-gated"
        )
    # The binding must name the corporate-action COVERAGE owner (SRS-DATA-011) -- now as the gate that
    # makes a split-adjusted read honest (an uncovered query fails closed naming it), not a deferral.
    if "SRS-DATA-011" not in src:
        fail(
            "the binding must name the corporate-action COVERAGE owner (SRS-DATA-011) -- the gate that "
            "makes a split-adjusted read honest (an uncovered query fails closed naming it)"
        )
    return (
        "normalization honesty: the binding serves NormalizationMode.RAW and the gated SPLIT_ADJUSTED "
        "(the Protocol default), keeps that default so an omitted normalization serves the coverage-gated "
        "adjusted series (CoverageNotProvenError when uncovered, never raw-as-adjusted), validates the "
        "coverage_through frontier (gate-integrity), and fails closed on fully-adjusted / total-return "
        "(SRS-DATA-012); SRS-DATA-011 is the coverage gate. See tools/normalization_modes_check.py"
    )


def check_subprocess_timeout(config: dict, src: str) -> str:
    compact = _compact(src)
    if "timeout=timeout" not in compact:
        fail(
            "the binding's runner must pass a timeout to subprocess.run so a wedged CLI cannot hang a "
            "strategy container indefinitely"
        )
    if "subprocess.TimeoutExpired" not in compact:
        fail(
            "the binding must catch subprocess.TimeoutExpired and map it to StoreQueryError (a read "
            "must surface a typed failure, never block forever)"
        )
    return (
        "no hang: every CLI invocation is bounded by a per-query timeout; a wedged CLI raises "
        "TimeoutExpired which is mapped to StoreQueryError, never an indefinite block"
    )


def check_money_scale(config: dict, src: str) -> str:
    spec = contract_block(config)
    scale = spec["price_scale_constant"]
    compact = _compact(src)
    if not re.search(rf"{re.escape(scale)}\s*=\s*\d+", src):
        fail(f"the binding must define a named price scale constant `{scale}` (no magic number)")
    for field in spec["ohlc_fields"]:
        if f'fields["{field}"]/{scale}' not in compact:
            fail(
                f"the binding must scale the OHLC field '{field}' by {scale} "
                f"(`fields[\"{field}\"] / {scale}`) -- minor units to major units"
            )
    volume = spec["volume_field"]
    # volume is a raw integer count: it must NOT be divided by the price scale, whether referenced
    # via the _VOLUME_FIELD constant or a "volume" literal subscript.
    if re.search(rf'(?:_VOLUME_FIELD|["\']{re.escape(volume)}["\'])\s*\]\s*/\s*{re.escape(scale)}', src):
        fail(
            f"'{volume}' is a raw integer count and must NOT be divided by {scale} "
            "(only OHLC prices are scaled)"
        )
    if "int(fields[_VOLUME_FIELD])" not in compact and f'int(fields["{volume}"])' not in compact:
        fail(f"the binding must read '{volume}' as a raw int(...) count (unscaled)")
    return (
        f"money math: OHLC fields are scaled by the named {scale}; '{volume}' is read as a raw int "
        "count (unscaled). The cents scale is an explicit assumption pending the deferred SDK<->core "
        "money-unit boundary (atp-types order_type.rs)"
    )


def check_list_argv_no_shell(config: dict, src: str) -> str:
    compact = _compact(src)
    if "shell=True" in compact:
        fail("the binding must NOT use shell=True (pass argv as a list so a symbol cannot inject)")
    if "argv=[" not in compact:
        fail("the binding must build the CLI invocation as a LIST argv (`argv = [ ... ]`)")
    if "subprocess.run(argv" not in compact:
        fail("the binding's runner must call subprocess.run(argv, ...) with the list argv")
    return (
        "the binding invokes the CLI with a list argv and shell=False (no shell string) -- a symbol "
        "string can never inject"
    )


def check_empty_match_is_value(config: dict, src: str) -> str:
    compact = _compact(src)
    if "match_count==0" not in compact:
        fail("the binding must recognise `match_count == 0` as a valid empty result")
    if "return[]" not in compact:
        fail("the binding must return an empty list for an empty match (never raise on no rows)")
    return "an empty match (match_count:0) returns [] -- empty is a value, never an error"


def check_count_validated(config: dict, src: str) -> str:
    compact = _compact(src)
    if "set(range(match_count))" not in compact:
        fail(
            "the parser must validate the parsed record indexes cover [0, match_count) "
            "(`set(range(match_count))`) -- a truncated CLI output must not silently return partial "
            "history"
        )
    if "set(records)!=expected" not in compact:
        fail(
            "the parser must compare the parsed record indexes to the expected [0, match_count) set "
            "and fail closed on a mismatch (`set(records) != expected`)"
        )
    if "match_count<0" not in compact:
        fail(
            "the parser must reject a negative match_count (`match_count < 0`) -- an impossible count "
            "must fail closed, never be coerced into an empty result"
        )
    return (
        "count integrity: the parser fails closed on a negative match_count, on match_count:0 with "
        "record lines, and unless the parsed record indexes cover exactly [0, match_count) -- a "
        "truncated/drifted CLI output cannot silently feed partial (or empty) history"
    )


def check_echo_validated(config: dict, src: str) -> str:
    compact = _compact(src)
    if "echoed_symbol!=symbol" not in compact or "echoed_resolution!=resolution" not in compact:
        fail(
            "the parser must validate the CLI-echoed symbol AND resolution match the request before "
            "building any Bar (`echoed_symbol != symbol` / `echoed_resolution != resolution`) -- a "
            "mismatch (CLI/schema drift or a wrong/stale binary) must fail closed, never relabel records"
        )
    return (
        "envelope integrity: the parser requires the CLI-echoed symbol + resolution to match the "
        "request before building bars -- a wrong/stale binary cannot relabel one symbol's records as "
        "another at the trust boundary"
    )


def check_range_and_order(config: dict, src: str) -> str:
    compact = _compact(src)
    if "echoed_start!=start_ts" not in compact or "echoed_end!=end_ts" not in compact:
        fail(
            "the parser must validate the CLI-echoed start/end match the request "
            "(`echoed_start != start_ts` / `echoed_end != end_ts`)"
        )
    if "event_ts<start_ts" not in compact or "event_ts>end_ts" not in compact:
        fail(
            "the parser must reject any record whose event_ts is outside the requested inclusive range "
            "(`event_ts < start_ts or event_ts > end_ts`) -- no future/stale data may leak through"
        )
    if "event_ts<previous_ts" not in compact:
        fail(
            "the parser must reject non-ascending event_ts (`event_ts < previous_ts`) -- get_bars takes "
            "the LAST lookback, which is only correct for ascending input"
        )
    return (
        "range + order integrity: the parser validates the echoed start/end, rejects any record "
        "event_ts outside the requested inclusive range (no future/stale data), and rejects "
        "non-ascending event_ts (no misordered data)"
    )


def check_kind_narrowed(config: dict, src: str) -> str:
    compact = _compact(src)
    if "_EQUITY_BAR_KIND_BY_RESOLUTION" not in compact:
        fail(
            "the binding must map an equity-bar resolution to a vendor-neutral DatasetKind label "
            "(`_EQUITY_BAR_KIND_BY_RESOLUTION`) so it can narrow the query"
        )
    if '"--kind"' not in compact and "'--kind'" not in compact:
        fail(
            "the binding must pass the vendor-neutral --kind disambiguator to narrow an equity query "
            "to the equity-bar dataset -- a fundamental / option-chain record sharing symbol+resolution "
            "must not be able to poison an OHLCV-bar read"
        )
    return (
        "kind narrowing: an equity query passes the vendor-neutral --kind (daily / minute equity bar; "
        "a dataset type, not a provider) so a same-symbol/same-resolution record of another dataset "
        "kind cannot poison the OHLCV-bar result"
    )


def check_round_trip(config: dict, require_cargo: bool = False) -> str:
    """Build the data CLIs, ingest a fixture batch, read it back through the Python binding."""
    block = contract_block(config)
    rt = block["round_trip"]
    crate = block["data_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                "cargo not on PATH but --require-cargo set: cannot verify the Python binding reads the "
                "real store end to end (install the Rust toolchain)"
            )
        return "round-trip ingest->python-read: skipped (cargo not on PATH)"
    for binary in (block["ingest_cli_bin"], block["cli_bin"]):
        built = subprocess.run(
            [cargo, "build", "-q", "-p", crate, "--bin", binary],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if built.returncode != 0:
            fail(f"building {binary} failed:\n{built.stdout}\n{built.stderr}")
    ingest_bin = ROOT / "target" / "debug" / block["ingest_cli_bin"]
    query_bin = ROOT / "target" / "debug" / block["cli_bin"]

    with tempfile.TemporaryDirectory() as tmp:
        ingested = subprocess.run(
            [str(ingest_bin), "ingest", "--dir", tmp, "--kind", rt["kind"], "--init"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if ingested.returncode != 0:
            fail(f"fixture ingest failed:\n{ingested.stdout}\n{ingested.stderr}")

        # Lazy import: the binding pulls the Strategy API (numpy/pandas-ta), only needed for the
        # cargo round-trip, so the static checks above stay import-free.
        python_root = str(ROOT / "python")
        if python_root not in sys.path:
            sys.path.insert(0, python_root)
        try:
            from atp_strategy import NormalizationMode
            from atp_strategy.store_history import (
                CoverageNotProvenError,
                StoreBackedHistoricalData,
            )
        except Exception as error:  # pragma: no cover - environment guard
            fail(f"could not import the Python binding StoreBackedHistoricalData: {error}")

        binding = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        # Fail-closed default over an UNCOVERED store: omitting normalization uses the Protocol default
        # (SPLIT_ADJUSTED), which routes through the coverage gate; this fixture ingests NO coverage
        # record, so it fails closed with CoverageNotProvenError (naming SRS-DATA-011) -- never raw.
        try:
            binding.get_bars(rt["symbol"], lookback=1, frequency=rt["resolution"])
            fail(
                "get_bars with the default (SPLIT_ADJUSTED) normalization over an uncovered store must "
                "fail closed (CoverageNotProvenError), not serve raw"
            )
        except CoverageNotProvenError:
            pass
        bars = binding.get_bars(
            rt["symbol"], lookback=10, frequency=rt["resolution"], normalization=NormalizationMode.RAW
        )
        if not bars:
            fail(
                f"the Python binding returned no bars for {rt['symbol']} {rt['resolution']} after a "
                "real fixture ingest (the consumer binding did not read the store)"
            )
        last = bars[-1]
        if last.symbol != rt["symbol"]:
            fail(f"binding returned the wrong symbol: {last.symbol!r} != {rt['symbol']!r}")
        if abs(last.close - rt["expected_close"]) > 1e-9:
            fail(
                f"binding close {last.close} != expected {rt['expected_close']} "
                f"(money scale {block['price_scale_constant']} mis-applied?)"
            )
        if last.volume != rt["expected_volume"]:
            fail(
                f"binding volume {last.volume} != expected {rt['expected_volume']} "
                "(volume must be a raw, UNSCALED count)"
            )
        # Empty match is a value, not an error -- an unknown symbol returns [].
        empty = binding.get_bars(
            "___NOPE___", lookback=5, frequency=rt["resolution"], normalization=NormalizationMode.RAW
        )
        if empty != []:
            fail("an unknown symbol must return [] (empty match is a value, not an error)")

    return (
        f"round-trip: data016_ingest_cli ingested {rt['kind']} fixtures, then the Python "
        f"StoreBackedHistoricalData read {rt['symbol']} {rt['resolution']} back through "
        f"data007_query_cli -- close={rt['expected_close']} (scaled), volume={rt['expected_volume']} "
        "(unscaled), and an unknown symbol returns [] (no provider named anywhere)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

_STATIC_CHECKS = (
    ("module_and_class", check_module_and_class),
    ("source_neutral_signature", check_source_neutral_signature),
    ("no_origin_field_read", check_no_origin_field_read),
    ("normalization_honesty", check_normalization_honesty),
    ("subprocess_timeout", check_subprocess_timeout),
    ("money_scale", check_money_scale),
    ("list_argv_no_shell", check_list_argv_no_shell),
    ("empty_match_is_value", check_empty_match_is_value),
    ("count_validated", check_count_validated),
    ("echo_validated", check_echo_validated),
    ("range_and_order", check_range_and_order),
    ("kind_narrowed", check_kind_narrowed),
)

_DEFERRED_OWNERS = (
    "the factor-job EXECUTION path (run_factor_job still takes caller-supplied inputs; the atp-factor-pipeline "
    "store_inputs loader is shipped substrate, and a complete run needs SRS-DATA-005 fundamentals) and the "
    "Jupyter notebook HOST (SRS-RES-002) -- the BACKTEST consumer (atp-simulation StoreBarSource) is genuinely "
    "wired and strategy/notebook read via this binding, but those two gaps keep SRS-DATA-007 passes:false",
    "fully-adjusted / total-return normalization modes (they additionally need dividend data, "
    "SRS-DATA-012); split-adjusted is now served through the SRS-DATA-011 coverage gate",
    "the concurrent-read-DURING-write Load test for THIS named Python consumer "
    "(SRS-DATA-017; the binding drives the lock-free read path, the substrate guarantee is proven, "
    "but the Python-consumer-vs-held-writer Load test is the deferred 017 close)",
    "real Databento/IB/Sharadar/option-chain NETWORK adapters that materialize records "
    "(SRS-DATA-001/003/005/006; fixture sources stand in)",
    "an authoritative SDK<->core money-unit scale constant -- the binding assumes the cents (x100) "
    "fixture convention for equity OHLC (deferred with the runtime money boundary, atp-types)",
    "option-chain bar access (the binding serves EQUITY OHLCV bars; OPTION raises) and the "
    "dashboard / REST consumer surfaces (SRS-UI / SRS-API)",
)


def assert_store_history_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    src = module_source(config, root)
    return [check(config, src) for _, check in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_store_history_static(config)
    evidence.append(check_round_trip(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SRS-DATA-007 store-history Python binding contract evidence"
    )
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the end-to-end binding read must run.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except StoreHistoryCheckError as error:
        print(f"SRS-DATA-007 STORE-HISTORY BINDING FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-DATA-007 STORE-HISTORY BINDING PASS")
    for item in evidence:
        print(f"- {item}")
    print("- deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
