#!/usr/bin/env python3
"""Contract evidence script for SRS-FAC-001 (compute scheduled factors across the full US
equity universe).

SRS-FAC-001 (SyRS SYS-32, SYS-33, SYS-51, NFR-P7; StRS SN-2.06, BG-3). The acceptance
criterion: "A factor job processes 8,000+ securities using market and Sharadar data, resolves
its schedule through the same trading calendar used by strategy scheduling, and completes
before the user-configured scheduled deadline."

The deterministic, dependency-free scheduled-factor-job surface lives in
``crates/atp-factor-pipeline`` (module ``factor_job``), per the structural contract in
``architecture/runtime_services.json`` (block ``factor_job_contract``). It is the upstream
PRODUCER of the SRS-BT-006 factor panel the ``factor_analysis`` tear-sheet consumes -- the
panel-regularity owner that module explicitly defers to SRS-FAC-001. The surface bundles the
four acceptance facets, each made falsifiable over immutable inputs:

  (a) Full universe (SYS-32/33) -- ``run_factor_job`` enforces the ``FULL_UNIVERSE_MIN`` (8,000)
      floor (a smaller universe fails closed with ``UniverseBelowMinimum``) and ranks the scored
      cross-section by the total order ``(factor_value desc, SecurityKey asc)``.
  (b) Market + fundamental data (SYS-32) -- each ``SecurityFactorInputs`` carries BOTH a
      ``MarketFactorInput`` and a ``FundamentalFactorInput``; a security missing either, or for
      which the ``FactorModel`` abstains, is an auditable ``SkippedSecurity`` with a
      ``FactorSkipReason``, never a fabricated score. The store-backed READ path
      ``store_inputs`` (``load_daily_market_input`` / ``load_fundamental_input`` /
      ``assemble_factor_inputs``) sources BOTH halves from the unified historical store by symbol /
      date range / resolution with no provider named (the SRS-DATA-007 factor-job READ consumer,
      point-in-time SAFE loaders); ``run_scheduled_factor_job_over_store`` composes that read with the
      schedule gate, DERIVING the data as-of from the calendar (``TradingCalendar::session_as_of_ts``)
      for the scheduled session -- so a caller cannot pair a session with a future as-of. Only the
      concrete real-calendar ``SessionOrdinal`` -> epoch mapping is deferred.
  (c) Schedule resolves through the trading calendar (SYS-51) -- the run resolves against a
      ``TradingCalendar`` port (the same calendar contract strategy scheduling resolves against);
      a non-session target day fails closed with ``NotASession``.
  (d) Deadline INSTANT, absolute + session-aware (NFR-P7) -- the run reads an injected ``Clock``
      (returning a session-aware ``Instant``) at the start (an EARLY start fails closed with
      ``StartedBeforeScheduledStart``; a late start -- even on a later session -- fails closed) and
      again after scoring, ranking, and output construction (a late finalization fails closed),
      comparing against the resolved deadline instant (the deadline minute is EXCLUSIVE -- a run
      still executing during it is late) -- ``FactorJobOutcome::DeadlineExceeded``, fail-closed. The
      real wall-clock clock is the deferred runtime owner. Every security must be an EQUITY
      (``NonEquitySecurity``), a run that SCORES fewer than ``min_scored_ratio`` of the universe
      fails closed (``NoUsableCoverage``), and BOTH the scores and the skipped list are sorted by
      key so the outcome is order-independent.

``assemble_regular_panel`` is the producer bridge to SRS-BT-006: it builds a REGULAR
``FactorPanel`` (a constant calendar-resolved rebalance interval + a non-overlapping forward
horizon) and re-validates it at the ``FactorPanel`` trust boundary. The work is deterministic for
a pure model (the ``FactorModel`` is invoked in canonical ``SecurityKey`` order, ranking by the
total order, the deadline read from the injected clock -- no clock of its own -- and no
parallelism / RNG -- the SRS-BT-010 criterion), factor scores are dimensionless ``f64`` (the
factor domain,
not a money leak), ``atp-factor-pipeline`` adds no broker/adapter/simulation dependency and
carries no vendor SDK token, and ``lib.rs`` re-exports ``pub mod factor_job;``.

The PASS line is ``SRS-FAC-001 SDK-SURFACE PASS``. The store-backed READ path is now present
(``run_scheduled_factor_job_over_store`` over the unified store), and the cargo smoke runs the
full-universe 8,000+ job over store-resident market + fundamental data within the calendar-resolved
deadline read from a DETERMINISTIC clock -- which proves the deadline-gating LOGIC, not completion
before a real wall-clock deadline. SRS-FAC-001's acceptance is a PERFORMANCE TEST (NFR-P7): the live
wall-clock harness over real securities is a deferred close blocker. The store-backed run DERIVES its
data as-of from the calendar's ``session_as_of_ts(schedule.session)`` (NOT a caller timestamp), so a
caller cannot pair a session with a future as-of -- only the CONCRETE real-calendar
``SessionOrdinal`` -> epoch mapping (test calendars stand in) is deferred -- so feature_list.json keeps
SRS-FAC-001 ``passes:false`` (the store-backed read is a foundational primitive). The other DEFERRED
owners (the REAL Databento/Sharadar network adapters, the SYS-57 workload-priority admission, and the
SRS-UI / SRS-API operator surface) are other features.

Mirrors the PASS/FAIL output style of ``tools/factor_analysis_check.py``.

Invoke:
    python3 tools/factor_job_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _struct_body, _trait_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class FactorJobCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise FactorJobCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "factor_job_contract" not in config:
        fail("architecture metadata is missing factor_job_contract")
    return config["factor_job_contract"]


def module_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["factor_pipeline_crate"]["path"] / "src" / f"{block['module']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["factor_pipeline_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["factor_pipeline_crate"]["path"] / block["no_broker_dependency"]["cargo_toml"]
    )
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


def _check_struct_fields(src: str, spec: dict, label: str) -> None:
    body = _compact(_struct_body(src, spec["struct"]))
    missing = [f for f in spec["fields"] if _compact(f"{f}:") not in body]
    if missing:
        fail(f"{spec['struct']} ({label}) is missing fields: {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_full_universe_floor(config: dict, src: str) -> str:
    spec = contract_block(config)["full_universe_floor"]
    if _compact(spec["token"]) not in _compact(src):
        fail(
            f"factor_job must declare the full-universe floor `{spec['token']}` ({spec['value']}) "
            "so a scheduled run attests SYS-32/33 full-universe coverage"
        )
    return (
        f"atp-factor-pipeline declares the SYS-32/33 full-universe floor "
        f"{spec['const']} = {spec['value']} (a smaller universe fails closed)"
    )


def check_trading_calendar_port(config: dict, src: str) -> str:
    spec = contract_block(config)["trading_calendar_port"]
    body = _trait_body(src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(
            f"TradingCalendar (the SyRS SYS-51 schedule-resolution port) is missing methods: "
            f"{', '.join(missing)}"
        )
    return (
        "atp-factor-pipeline declares the TradingCalendar port (is_session / session_open / "
        "session_close / is_early_close / next_session / session_as_of_ts) the factor job resolves its "
        "schedule against -- the SyRS SYS-51 reuse boundary, mirroring the calendar contract strategy "
        "scheduling resolves against; session_as_of_ts is the SessionOrdinal <-> epoch binding the "
        "store-backed run DERIVES its data as-of from (not a caller timestamp)"
    )


def check_clock(config: dict, src: str) -> str:
    block = contract_block(config)
    instant = block["instant"]
    _check_struct_fields(src, instant, "session-aware instant")
    spec = block["clock"]
    body = _trait_body(src, spec["trait"])
    if not re.search(rf"\bfn\s+{re.escape(spec['method'])}\b", body):
        fail(
            f"Clock must declare `fn {spec['method']}` returning a session-aware Instant -- the "
            "injected ABSOLUTE time authority the run checks its start/completion against the "
            "resolved deadline instant (NFR-P7)"
        )
    return (
        "atp-factor-pipeline declares the session-aware Instant (session + minute) and the injected "
        "Clock port (now) the run reads at start and completion against the resolved deadline "
        "INSTANT -- so an early/late start (even on a later session) or a late finalization is "
        "caught against the absolute deadline; the real wall-clock clock is the deferred runtime "
        "owner"
    )


def check_factor_model(config: dict, src: str) -> str:
    spec = contract_block(config)["factor_model"]
    body = _trait_body(src, spec["trait"])
    if not re.search(rf"\bfn\s+{re.escape(spec['method'])}\b", body):
        fail(f"FactorModel must declare `fn {spec['method']}` (the user-defined factor)")
    # The factor must take BOTH a market and a fundamental input (SYS-32 "both ... data").
    signature = _compact(body)
    if "MarketFactorInput" not in signature or "FundamentalFactorInput" not in signature:
        fail(
            "FactorModel::compute must take BOTH a MarketFactorInput and a FundamentalFactorInput "
            "(SyRS SYS-32 'using both market data and fundamental data')"
        )
    return (
        "atp-factor-pipeline declares the user-defined FactorModel trait whose compute takes BOTH "
        "a market and a fundamental input and may abstain (None), never fabricating a score"
    )


def check_inputs(config: dict, src: str) -> str:
    block = contract_block(config)
    _check_struct_fields(src, block["market_input"], "market input")
    _check_struct_fields(src, block["fundamental_input"], "fundamental input")
    _check_struct_fields(src, block["security_inputs"], "security inputs")
    body = _compact(_struct_body(src, block["security_inputs"]["struct"]))
    missing = [
        t for t in block["security_inputs"]["both_sources_tokens"] if _compact(t) not in body
    ]
    if missing:
        fail(
            "SecurityFactorInputs must carry BOTH an Option market and an Option fundamental "
            f"summary so a security missing either is skippable: missing {', '.join(missing)}"
        )
    return (
        "atp-factor-pipeline declares MarketFactorInput (trailing_return, realized_volatility) and "
        "FundamentalFactorInput (earnings_yield, book_to_price), both carried as Option on "
        "SecurityFactorInputs (a security missing either source is skipped, not fabricated)"
    )


def check_schedule_and_config(config: dict, src: str) -> str:
    block = contract_block(config)
    _check_struct_fields(src, block["schedule"], "schedule")
    _check_struct_fields(src, block["config"], "config")
    return (
        "atp-factor-pipeline declares FactorJobSchedule (session + start/deadline minutes before "
        "open, resolved against the session open) and FactorJobConfig (min_scored_ratio -- the "
        "fractional coverage policy; the full-universe INPUT floor is the hard constant, not config)"
    )


def check_score_outputs(config: dict, src: str) -> str:
    block = contract_block(config)
    _check_struct_fields(src, block["score"], "factor score")
    _check_struct_fields(src, block["score_set"], "factor score set")
    skip = block["skip"]
    _check_struct_fields(
        src, {"struct": skip["struct"], "fields": skip["struct_fields"]}, "skipped security"
    )
    skip_body = _enum_body(src, skip["enum"])
    missing = [v for v in skip["variants"] if not re.search(rf"\b{re.escape(v)}\b", skip_body)]
    if missing:
        fail(f"FactorSkipReason is missing variants: {', '.join(missing)}")
    return (
        "atp-factor-pipeline declares FactorScore (security, factor_value, rank), FactorScoreSet "
        "(session, ranked scores, universe_size, skipped) and SkippedSecurity + FactorSkipReason "
        "(MissingMarketData / MissingFundamentalData / FactorAbstained) -- every unscored security "
        "is an auditable absence"
    )


def check_panel_inputs(config: dict, src: str) -> str:
    block = contract_block(config)
    _check_struct_fields(src, block["realized_session"], "realized session")
    _check_struct_fields(src, block["realized_observation"], "realized observation")
    return (
        "atp-factor-pipeline declares RealizedFactorSession + RealizedObservation -- the realized "
        "per-rebalance cross-section assemble_regular_panel turns into a FactorPanel"
    )


def _signature(src: str, fn: str) -> str:
    if not re.search(rf"\bpub\s+fn\s+{re.escape(fn)}\b", src):
        fail(f"factor_job must expose `pub fn {fn}`")
    return _compact(src[src.index(f"pub fn {fn}") :].split("{", 1)[0])


def check_run_fn(config: dict, src: str) -> str:
    spec = contract_block(config)["run_fn"]
    signature = _signature(src, spec["fn"])
    missing = [p for p in spec["param_tokens"] if _compact(p) not in signature]
    if missing:
        fail(f"`{spec['fn']}` must take {', '.join(spec['param_tokens'])}: missing {missing}")
    if _compact(spec["return_token"]) not in signature:
        fail(f"`{spec['fn']}` must return `{spec['return_token']}`")
    return (
        "atp-factor-pipeline exposes `pub fn run_factor_job(schedule, calendar, config, model, "
        "clock, universe) -> Result<FactorJobOutcome, FactorJobError>` -- the scheduled "
        "full-universe factor run, gating its deadline against the injected clock (the "
        "SRS-FAC-001 entry point)"
    )


def check_assemble_fn(config: dict, src: str) -> str:
    spec = contract_block(config)["assemble_fn"]
    signature = _signature(src, spec["fn"])
    missing = [p for p in spec["param_tokens"] if _compact(p) not in signature]
    if missing:
        fail(f"`{spec['fn']}` must take {', '.join(spec['param_tokens'])}: missing {missing}")
    if _compact(spec["return_token"]) not in signature:
        fail(f"`{spec['fn']}` must return `{spec['return_token']}`")
    return (
        "atp-factor-pipeline exposes `pub fn assemble_regular_panel(calendar, sessions, quantiles, "
        "forward_horizon_sessions) -> Result<FactorPanel, FactorJobError>` -- the SRS-BT-006 "
        "regular-panel producer bridge"
    )


def check_outcome_and_error_enums(config: dict, src: str) -> str:
    block = contract_block(config)
    outcome = block["outcome_enum"]
    outcome_body = _enum_body(src, outcome["enum"])
    missing = [
        v for v in outcome["variants"] if not re.search(rf"\b{re.escape(v)}\b", outcome_body)
    ]
    if missing:
        fail(f"{outcome['enum']} is missing variants: {', '.join(missing)}")
    error = block["error_enum"]
    error_body = _enum_body(src, error["enum"])
    error_missing = [
        v for v in error["variants"] if not re.search(rf"\b{re.escape(v)}\b", error_body)
    ]
    if error_missing:
        fail(f"{error['enum']} is missing fail-closed variants: {', '.join(error_missing)}")
    return (
        f"atp-factor-pipeline declares FactorJobOutcome (WithinDeadline / DeadlineExceeded) and "
        f"FactorJobError with {len(error['variants'])} fail-closed variants"
    )


def check_calendar_resolution(config: dict, src: str) -> str:
    spec = contract_block(config)["calendar_resolution"]
    compact_src = _compact(src)
    for key, label in (
        (
            "session_open_token",
            "resolve the before-open offsets against the calendar's session_open instant",
        ),
        ("is_session_guard", "refuse a session with no resolvable open (NotASession)"),
        (
            "before_day_start_guard",
            "refuse a lead that precedes the day start (ScheduleBeforeDayStart)",
        ),
        ("window_guard", "refuse an empty/inverted schedule window (EmptyScheduleWindow)"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"run_factor_job must {label} (`{spec[key]}`) -- the schedule resolves through the "
                "trading calendar's session_open (SyRS SYS-51), not an ad-hoc clock"
            )
    return (
        "atp-factor-pipeline run_factor_job resolves the schedule through the calendar's "
        "session_open: a session with no resolvable open fails closed (NotASession), a lead before "
        "the day start fails closed (ScheduleBeforeDayStart), and an empty window fails closed "
        "(EmptyScheduleWindow), so SYS-51 is enforced -- the offsets are resolved against the "
        "session open, not merely checked for being a session"
    )


def check_full_universe_gate(config: dict, src: str) -> str:
    spec = contract_block(config)["full_universe_gate"]
    compact_src = _compact(src)
    for key, label in (
        ("empty_guard", "reject an empty universe"),
        ("floor_guard", "reject a universe below the full-universe floor"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"run_factor_job must {label} (`{spec[key]}`) (SYS-32/33)")
    # The floor must compare against the HARD constant FULL_UNIVERSE_MIN, not a caller config --
    # otherwise a caller could weaken full-universe coverage by lowering a configurable minimum.
    if _compact(spec["hard_floor_token"]) not in compact_src:
        fail(
            f"the full-universe floor must compare against the hard constant "
            f"(`{spec['hard_floor_token']}`), NOT a caller-supplied minimum -- otherwise coverage "
            "is bypassable from config (SYS-32/33)"
        )
    return (
        "atp-factor-pipeline run_factor_job fails closed on an empty universe (EmptyUniverse) and "
        "on a universe below the HARD 8,000 full-universe floor (UniverseBelowMinimum, compared "
        "against the constant FULL_UNIVERSE_MIN -- not a caller config, so coverage cannot be "
        "weakened from outside) -- SYS-32/33"
    )


def check_deadline_gate(config: dict, src: str) -> str:
    spec = contract_block(config)["deadline_gate"]
    compact_src = _compact(src)
    if _compact(spec["clock_read_token"]) not in compact_src:
        fail(
            f"run_factor_job must read the injected session-aware clock "
            f"(`{spec['clock_read_token']}`) to check its time against the resolved deadline "
            "instant (NFR-P7)"
        )
    if _compact(spec["early_start_token"]) not in compact_src:
        fail(
            f"run_factor_job must reject an EARLY START (`{spec['early_start_token']}`): a run "
            "invoked before its scheduled start fails closed (NFR-P7)"
        )
    if _compact(spec["early_start_guard"]) not in compact_src:
        fail(
            f"an early start must fail closed with `{spec['early_start_guard']}` -- the run must "
            "not proceed ahead of its scheduled start"
        )
    if _compact(spec["late_start_token"]) not in compact_src:
        fail(
            f"run_factor_job must reject a LATE START against the session-aware instant "
            f"(`{spec['late_start_token']}`): a run invoked after the deadline -- including on a "
            "later session -- cannot complete on time (NFR-P7)"
        )
    if _compact(spec["completion_token"]) not in compact_src:
        fail(
            f"run_factor_job must check COMPLETION after ranking/output construction "
            f"(`{spec['completion_token']}`) so a slow finalization is caught, not excluded from the "
            "deadline (NFR-P7)"
        )
    if _compact(spec["exceeded_variant"]) not in compact_src:
        fail(
            f"a late run must fail closed with `{spec['exceeded_variant']}` (no ranked set "
            "emitted), not present a late run as on-time"
        )
    if _compact(spec["monotonic_token"]) not in compact_src:
        fail(
            f"run_factor_job must verify the clock is non-decreasing (`{spec['monotonic_token']}`) "
            "so a backward wall clock cannot make a late completion read as on-time (Codex-flagged)"
        )
    if _compact(spec["monotonic_guard"]) not in compact_src:
        fail(
            f"a backward clock must fail closed (`{spec['monotonic_guard']}`), not trust the "
            "regressed reading against the deadline"
        )
    return (
        "atp-factor-pipeline run_factor_job gates its NFR-P7 deadline against the ABSOLUTE "
        "session-aware instant read from the injected Clock (clock.now()): it rejects an early "
        "start (started < start_instant), a late start (started > deadline_instant -- session-aware, "
        "so a later-day run is caught), and a late finalization (completed > deadline_instant, "
        "checked after ranking + output construction) -- fail-closed -- so a mistimed run is never "
        "presented as on-time"
    )


def check_deterministic_output(config: dict, src: str) -> str:
    spec = contract_block(config)["deterministic_output"]
    if _compact(spec["skipped_sort_token"]) not in _compact(src):
        fail(
            f"run_factor_job must sort the skipped securities (`{spec['skipped_sort_token']}`) so "
            "the WHOLE outcome (scores AND skipped) is order-independent -- otherwise reversing an "
            "input with skips changes the result, breaking the determinism claim"
        )
    if _compact(spec["canonical_scan_token"]) not in _compact(src):
        fail(
            f"run_factor_job must score in CANONICAL key order (`{spec['canonical_scan_token']}`), "
            "not caller-input order, so even a stateful FactorModel yields an order-independent "
            "output (Codex-flagged)"
        )
    return (
        "atp-factor-pipeline run_factor_job invokes the FactorModel in canonical SecurityKey order "
        "and sorts BOTH the ranked scores and the skipped list by SecurityKey, so the whole "
        "FactorJobOutcome is a pure function of the input set, independent of input order -- even a "
        "stateful model cannot make it order-dependent within a run (SRS-BT-010)"
    )


def check_coverage_gate(config: dict, src: str) -> str:
    spec = contract_block(config)["coverage_gate"]
    compact_src = _compact(src)
    if _compact(spec["guard_token"]) not in compact_src:
        fail(
            f"run_factor_job must fail closed when too few securities are scored "
            f"(`{spec['guard_token']}`) -- an all-skipped or thin run is not a successful computation"
        )
    if _compact(spec["ratio_token"]) not in compact_src:
        fail(
            f"the coverage floor must be a FRACTION of the universe (`{spec['ratio_token']}`), so it "
            "scales and a one-scored 'success' cannot slip through (Codex-flagged)"
        )
    if _compact(spec["floor_token"]) not in compact_src:
        fail(
            f"the coverage floor must always be at least 1 (`{spec['floor_token']}`), so a "
            "zero-scored run fails even at a degenerate ratio"
        )
    if _compact(spec["ratio_range_token"]) not in compact_src:
        fail(
            f"the coverage ratio must be validated to [0, 1] (`{spec['ratio_range_token']}`), so a "
            "negative or out-of-range ratio is not silently treated as permissive (Codex-flagged)"
        )
    if _compact(spec["ratio_validation_guard"]) not in compact_src:
        fail(
            f"an invalid coverage ratio must fail closed (`{spec['ratio_validation_guard']}`), not "
            "weaken the gate to a one-scored success"
        )
    if _compact(spec["platform_floor_token"]) not in compact_src:
        fail(
            f"the coverage floor must take the GREATER of the config ratio and the hard platform "
            f"minimum (`{spec['platform_floor_token']}`), so a config of 0.0 cannot collapse it to "
            "one security (Codex-flagged)"
        )
    return (
        "atp-factor-pipeline run_factor_job validates min_scored_ratio is in [0, 1] "
        "(InvalidCoverageRatio) and fails closed with NoUsableCoverage when the scored count is "
        "below ceil(max(min_scored_ratio, MIN_SCORED_COVERAGE_RATIO) * universe) -- the hard "
        "platform floor means a config of 0.0 cannot collapse coverage to a single security"
    )


def check_equity_gate(config: dict, src: str) -> str:
    spec = contract_block(config)["equity_gate"]
    compact_src = _compact(src)
    if _compact(spec["class_token"]) not in compact_src:
        fail(
            f"run_factor_job must check each security is an EQUITY (`{spec['class_token']}`) -- the "
            "factor universe is US equities (SyRS SYS-32)"
        )
    if _compact(spec["guard_token"]) not in compact_src:
        fail(
            f"a non-equity security must fail closed (`{spec['guard_token']}`), not be certified as "
            "part of a full-US-equity run"
        )
    return (
        "atp-factor-pipeline run_factor_job rejects any non-equity security (NonEquitySecurity) so "
        "only US equities are certified -- binding to a trusted session-versioned universe manifest "
        "is the deferred SRS-DATA-001 catalog"
    )


def check_forward_window(config: dict, src: str) -> str:
    spec = contract_block(config)["forward_window"]
    compact_src = _compact(src)
    if _compact(spec["match_token"]) not in compact_src:
        fail(
            f"assemble_regular_panel must VERIFY each period's forward window against the declared "
            f"horizon (`{spec['match_token']}`), not trust the caller's label (Codex-flagged)"
        )
    if _compact(spec["guard_token"]) not in compact_src:
        fail(
            f"a mislabeled forward window must fail closed (`{spec['guard_token']}`) so mixed/"
            "overlapping returns cannot be certified as a regular panel"
        )
    return (
        "atp-factor-pipeline assemble_regular_panel checks each period's declared forward_window_end "
        "is exactly the declared horizon of trading sessions out (via the calendar) for LABEL "
        "CONSISTENCY (ForwardWindowMismatch) -- a mixed/mislabeled-horizon period is rejected; "
        "proving the returns were computed over that window is the deferred SRS-DATA-007 data layer"
    )


def check_skip_not_fabricate(config: dict, src: str) -> str:
    spec = contract_block(config)["skip_not_fabricate"]
    compact_src = _compact(src)
    for key, label in (
        ("missing_market", "record a missing-market-data skip"),
        ("missing_fundamental", "record a missing-fundamental-data skip"),
        ("abstain", "record a factor-abstained skip"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"run_factor_job must {label} (`{spec[key]}`) -- a security it cannot score on both "
                "sources is an auditable absence, never a fabricated score"
            )
    return (
        "atp-factor-pipeline run_factor_job records every unscored security as a SkippedSecurity "
        "with a reason (MissingMarketData / MissingFundamentalData / FactorAbstained) rather than "
        "fabricating a score -- the SYS-32 'both market and fundamental data' requirement"
    )


def check_regularity(config: dict, src: str) -> str:
    spec = contract_block(config)["regularity"]
    compact_src = _compact(src)
    if not re.search(rf"\bfn\s+{re.escape(spec['gap_fn'])}\b", src):
        fail(f"assemble_regular_panel must resolve the rebalance gap through `fn {spec['gap_fn']}`")
    for key, label in (
        ("interval_guard", "reject a non-constant trading-session rebalance interval"),
        ("overlap_guard", "reject a forward horizon that overlaps consecutive windows"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"assemble_regular_panel must {label} (`{spec[key]}`) so the produced panel is "
                "REGULAR (the SRS-BT-006 mean-aggregate precondition it owns)"
            )
    if _compact(spec["panel_validate_token"]) not in compact_src:
        fail(
            f"assemble_regular_panel must re-validate the assembled panel at the FactorPanel trust "
            f"boundary (`{spec['panel_validate_token']}`)"
        )
    return (
        "atp-factor-pipeline assemble_regular_panel resolves the rebalance interval through the "
        "calendar (trading_session_gap) and fails closed on an irregular interval "
        "(IrregularRebalanceInterval) or an overlapping forward horizon "
        "(OverlappingForwardWindows), then re-validates the panel -- producing the REGULAR panel "
        "the SRS-BT-006 mean aggregates require"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    leaked = [t for t in spec["forbidden_tokens"] if t in src]
    if leaked:
        fail(
            f"factor_job must be deterministic (SRS-BT-010): found nondeterminism source(s) "
            f"{', '.join(leaked)} -- it must use fixed left-to-right folds, total-order ranking, "
            "and the injected clock with no parallelism, RNG, or wall-clock read of its own"
        )
    return (
        "atp-factor-pipeline factor job is deterministic: no parallelism / RNG / wall-clock token "
        "(the deadline reads the injected clock), ranking by the total order (factor_value desc, "
        "SecurityKey asc) -- identical inputs + clock yield bit-identical outputs (SRS-BT-010)"
    )


def check_numeric_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    compact_src = _compact(src)
    for key, label in (
        ("factor_input_token", "factor score"),
        ("return_input_token", "forward return"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"the {label} must be a dimensionless f64 (`{spec[key]}`)")
    return (
        "atp-factor-pipeline keeps factor scores and forward returns as dimensionless f64 "
        "(factor_value: f64, forward_return: f64) -- the factor domain, not a money leak"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-factor-pipeline lib.rs must re-export `{spec['lib_reexport_token']}` so the "
            "scheduled-factor-job surface is part of the factor pipeline runtime"
        )
    return f"atp-factor-pipeline lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-factor-pipeline Cargo.toml must NOT depend on the live/broker/simulation path: "
            f"found {', '.join(leaked)} -- the factor-job surface is self-contained over the data "
            "layer"
        )
    return (
        f"atp-factor-pipeline Cargo.toml declares no dependency on the live/broker/simulation path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- the factor-job surface is independent"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-factor-pipeline factor_job module leaks vendor SDK token(s): "
            f"{', '.join(leaked)} (the core engine must isolate vendors behind adapters per "
            "SRS-ARCH-003)"
        )
    return (
        f"atp-factor-pipeline factor_job module is free of all {len(tokens)} forbidden vendor SDK "
        "tokens (SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["factor_pipeline_crate"]["crate"]
    integration = block["rust_integration_test"]
    store_integration = block["store_backed_integration_test"]
    module = block["module"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "factor-job path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration} + {store_integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", module, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib {module} failed:\n{lib.stdout}\n{lib.stderr}")
    for test in (integration, store_integration):
        integ = subprocess.run(
            [cargo, "test", "-p", crate, "--test", test, "--quiet"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if integ.returncode != 0:
            fail(f"cargo test -p {crate} --test {test} failed:\n{integ.stdout}\n{integ.stderr}")
    return (
        f"cargo test -p {crate} --lib {module} + {integration} + {store_integration}: PASS "
        "(ranks the full universe within the calendar-resolved deadline; skips a security missing "
        "either source; fails closed below the universe floor / over the deadline / on an irregular "
        "panel; produces a regular panel that feeds compute_tear_sheet; AND runs the full-universe "
        "8,000+ job over store-resident market + fundamental data via "
        "run_scheduled_factor_job_over_store -- the SRS-DATA-007 factor-job READ consumer, a "
        "point-in-time read primitive)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "module" reads factor_job.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("full_universe_floor", check_full_universe_floor, "module"),
    ("trading_calendar_port", check_trading_calendar_port, "module"),
    ("clock", check_clock, "module"),
    ("factor_model", check_factor_model, "module"),
    ("inputs", check_inputs, "module"),
    ("schedule_and_config", check_schedule_and_config, "module"),
    ("score_outputs", check_score_outputs, "module"),
    ("panel_inputs", check_panel_inputs, "module"),
    ("run_fn", check_run_fn, "module"),
    ("assemble_fn", check_assemble_fn, "module"),
    ("outcome_and_error_enums", check_outcome_and_error_enums, "module"),
    ("calendar_resolution", check_calendar_resolution, "module"),
    ("full_universe_gate", check_full_universe_gate, "module"),
    ("deadline_gate", check_deadline_gate, "module"),
    ("coverage_gate", check_coverage_gate, "module"),
    ("equity_gate", check_equity_gate, "module"),
    ("forward_window", check_forward_window, "module"),
    ("deterministic_output", check_deterministic_output, "module"),
    ("skip_not_fabricate", check_skip_not_fabricate, "module"),
    ("regularity", check_regularity, "module"),
    ("determinism", check_determinism, "module"),
    ("numeric_boundary", check_numeric_boundary, "module"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "module"),
)

_DEFERRED_OWNERS = (
    "the live wall-clock performance test over 8,000+ real securities completing before the real "
    "deadline (NFR-P7) -- this slice gates the deadline against an injected Clock so the late-start "
    "/ late-finalization decision is falsifiable in-process; the real wall-clock Clock is the "
    "runtime owner",
    "supervised HARD-deadline termination/cancellation of a hung or pathologically-slow "
    "FactorModel -- the in-process gate is OBSERVATIONAL (start + completion, catching a "
    "slow-but-completing run at the completion check); a synchronous dependency-free core cannot "
    "preempt a hung call, so the supervised execution context (the orchestrator container, SYS-57) "
    "is the owner, validated by the NFR-P7 performance test",
    "the concrete US-equity TradingCalendar implementation (SRS-SDK-002 / SyRS SYS-50) that provides the "
    "REAL SessionOrdinal <-> epoch session_as_of_ts mapping -- run_scheduled_factor_job_over_store DERIVES "
    "the point-in-time as_of_ts from the calendar's session_as_of_ts(schedule.session) (NOT a caller "
    "timestamp), so a caller CANNOT pair a session with a future as_of; the binding is structural via the "
    "TradingCalendar port, only the concrete mapping is deferred (test calendars stand in). The "
    "SRS-DATA-007 store READ itself is DONE (store_inputs loaders + assemble_factor_inputs source BOTH "
    "market and fundamental inputs from the unified store as point-in-time read primitives); and the REAL "
    "Databento/Sharadar network adapters that materialize the store records (SRS-DATA-001/005; fixture "
    "sources stand in)",
    "binding a successful run to a TRUSTED session-versioned US-equity universe manifest and "
    "market/Sharadar source-provenance manifest -- the inputs are caller-supplied, so a successful "
    "FactorScoreSet certifies a correct COMPUTATION over the inputs given, not their trustworthiness "
    "(owner: the SRS-DATA-001 universe catalog + SRS-DATA-007 historical interface)",
    "distinguishing a DEGRADED/STALE calendar from a legitimate non-session -- the TradingCalendar "
    "port mirrors the SRS-SDK-002 value-returning surface; calendar/dependency health is the "
    "readiness/connectivity gates' job (SRS-ARCH-005, ERR-2) and the concrete calendar service",
    "the SYS-57 workload-priority admission of the factor job (priority 5; the 2 GB host-memory "
    "safety margin -- owned by the orchestrator workload-priority slice)",
    "the operator schedule + factor-score surface (SRS-UI / SRS-API)",
)


def assert_factor_job_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "module": module_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_factor_job_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-FAC-001 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable factor-job path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except FactorJobCheckError as error:
        print(f"SRS-FAC-001 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-FAC-001 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-FAC-001 passes:false (store-backed wiring is foundational "
        "substrate; the live wall-clock NFR-P7 performance test over real securities is the deferred "
        "close blocker -- the fixture run uses a deterministic clock, proving the gating logic)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
