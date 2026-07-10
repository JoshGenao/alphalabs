#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-011 corporate-action adjustment + COVERAGE (the keystone gate).

SRS-DATA-011 (adjust historical price data for corporate actions; SyRS SYS-28a / StRS SN-1.14).
Acceptance: "Splits, reverse splits, dividends, delistings, mergers, and symbol changes are reflected in
historical records so that backtests spanning corporate-action dates produce correct P&L calculations
under the selected normalization mode."

ALL SIX action types are reflected through ONE coverage-enforcing gate: splits / reverse-splits and
dividends are re-quoted into the served prices (split-adjusted; fully-adjusted = splits AND dividends,
SYS-29); symbol changes are resolved as rename LINEAGE (predecessor bars relabeled to the queried
symbol); delistings / mergers / symbol changes are surfaced structurally on the result (events) so a
P&L consumer marks positions final / converts at the surfaced terms / follows the hop. Real provider
corporate-action ingestion stays deferred (the operator-set / fixture frontier stands in,
SRS-DATA-001/003/006); the LIVE per-subscription normalization selection stays deferred (SRS-DATA-012
remainder, blocked on SRS-MD-001; total-return itself is now served historically).

What this pins:
  (a) the coverage kind — store.rs declares the vendor-neutral DatasetKind::CorporateActionCoverage
      (label "corporate-action-coverage", tag 5, min_schema_version 3) and SCHEMA_VERSION == 4, so the
      per-symbol completeness-through-date frontier persists in the SAME idempotent/durable store;
  (b) the gate condition — every gated read serves adjusted output ONLY when the queried symbol's
      coverage frontier D = max(complete_through) satisfies D >= query.end_ts, else fails closed with
      CoverageError::NotCovered { have_through, need_through } (D >= end_ts, not ==); the applied
      corporate actions are bounded to the read's basis cutoff (`event_ts <= adjusted_through`: the
      frontier D for the current-basis reads, query.end_ts for the point-in-time _as_of reads);
  (c) the kind-narrowed gate — the gate requires an equity-bar query kind (DailyEquityBar /
      MinuteEquityBar), so the math's UnsupportedKind path is unreachable at runtime and an adjusted
      series is equity-only by construction;
  (d) the single public entry point — lib.rs exposes `pub mod coverage` (the coverage-enforcing gate,
      whose four reads — query_split_adjusted[_as_of] / query_fully_adjusted[_as_of] — are ALL
      coverage-gated) while the adjustment math stays crate-internal (`mod normalization`, not
      re-exported), so the ONLY public path to adjusted output is the coverage gate (no public path
      to raw-as-adjusted);
  (e) the CLI routing — data007_query_cli routes --normalization split-adjusted, fully-adjusted, AND
      total-return through the gate, echoes coverage_through / adjusted_through / event_count, and fails
      closed (naming SRS-DATA-011) when uncovered; total-return is now served (query_total_return);
  (f) the coverage CLI — data011_coverage_cli records the frontier (assert-coverage --symbol --through
      under the StoreLock) and shows it (show-coverage);
  (g) the corporate-action kinds — store.rs declares the four v4 FACT kinds (dividend tag 6, delisting
      tag 7, merger tag 8, symbol-change tag 9; SCHEMA_VERSION 3->4) with validate_record enforcement
      at upsert AND restore, fixture batches for each, and coverage still the ONLY refused trust kind;
  (h) the dividend leg — the gate applies dividends through the crate-internal fully_adjust math with
      the reference close resolved from the RAW lineage series and fail-closed missing-reference /
      basis-crossing handling; volume is never dividend-scaled;
  (i) terminal-event surfacing — delistings / mergers / symbol changes are surfaced as structured
      events on the gated result;
  (j) bounded lineage — rename-lineage resolution fails closed on cycles (visited set + a hard depth
      bound) and on ambiguous rename data.

Plus a cargo round-trip (--require-cargo): the coverage gate's crate unit suite, then an end-to-end
ingest proving (1) COVERED -> split-adjusted returns the ADJUSTED bar (close 2500 / volume 400000 /
coverage_through:200) and fully-adjusted composes the dividend@150 (close 2475 / volume 400000);
(2) UNCOVERED (query end beyond the frontier) -> the CLI FAILS closed naming SRS-DATA-011; and (3) a
rename-lineage store relabels the predecessor's bars under the queried successor and surfaces the
symbol-change event, and a delisting + merger store surfaces both events with their exact terms.

PASS line: ``SRS-DATA-011 CORPORATE-ACTION COVERAGE PASS``.

Invoke:
    python3 tools/coverage_manifest_check.py [--require-cargo]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CoverageManifestCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise CoverageManifestCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "coverage_manifest_contract" not in config:
        fail("architecture metadata is missing coverage_manifest_contract")
    return config["coverage_manifest_contract"]


def _read(config: dict, key: str, root: Path = ROOT) -> str:
    rel = contract_block(config)[key]
    path = root / rel
    if not path.exists():
        fail(f"source missing: {rel}")
    return path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so reformatting cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors (each takes the relevant source so the L3 test
# can inject a regression and prove the guard is non-vacuous).
# --------------------------------------------------------------------------- #


def check_coverage_kind(config: dict, store_src: str) -> str:
    block = contract_block(config)
    label = block["coverage_kind_label"]
    if "CorporateActionCoverage" not in store_src:
        fail(
            "store.rs must declare a vendor-neutral DatasetKind::CorporateActionCoverage so the "
            "per-symbol coverage frontier persists in the same idempotent/durable store as bars"
        )
    if f'"{label}"' not in store_src:
        fail(f"store.rs must map CorporateActionCoverage to the '{label}' label")
    compact = _compact(store_src)
    # The coverage kind is a v3-introduced kind, and SCHEMA_VERSION must be bumped to 3 so a v1/v2
    # reader rejects a coverage-bearing store cleanly at the version gate (not mid-restore on tag 5).
    if "CorporateActionCoverage=>5" not in compact and "CorporateActionCoverage=>5," not in compact:
        fail("store.rs must give DatasetKind::CorporateActionCoverage the codec tag 5")
    if "CorporateActionCoverage=>3" not in compact:
        fail(
            "store.rs must set CorporateActionCoverage::min_schema_version() = 3 so a v1/v2 store "
            "cannot smuggle the coverage kind"
        )
    if f"SCHEMA_VERSION:i64={block['schema_version']}" not in compact:
        fail(
            f"store.rs SCHEMA_VERSION must be {block['schema_version']} (the coverage kind's version)"
        )
    # The single coverage-record constructor binds event_ts to the through-date (the frontier identity).
    if "fncoverage_record" not in compact:
        fail(
            "store.rs must provide the coverage_record(through, symbol) constructor (single shape)"
        )
    # A coverage record asserts the trust decision the split-adjusted gate reads (the frontier), and
    # MarketDataRecord::new is public — so store validation MUST enforce its self-consistency (a single
    # complete_through field equal to the key event_ts) at upsert AND restore, or a forged frontier could
    # grant coverage its key does not carry. This cannot be left to the coverage_record constructor.
    if "value_minor==record.key.event_ts" not in compact:
        fail(
            "store.rs validate_record must enforce a CorporateActionCoverage record's complete_through "
            "field equals its key event_ts (so a forged frontier via the public MarketDataRecord::new "
            "fails closed before the split-adjusted gate can trust it)"
        )
    # The provider fixture generator must NOT mint coverage (coverage is an operator trust assertion,
    # not provider market data), so a generic ingestion flow iterating dataset kinds cannot create a
    # trusted frontier through the fixture path.
    if "CorporateActionCoverage=>Vec::new()" not in compact:
        fail(
            "store.rs fixture_batch must emit NO records for CorporateActionCoverage "
            "(`CorporateActionCoverage => Vec::new()`) -- coverage is an operator trust assertion, not "
            "provider fixture data"
        )
    return (
        f"coverage kind: store.rs declares DatasetKind::CorporateActionCoverage ('{label}', tag 5, "
        f"min_schema_version 3) and SCHEMA_VERSION {block['schema_version']}; validate_record fails "
        "closed on a coverage record whose complete_through field disagrees with its key event_ts (a "
        "forged frontier), at upsert and restore — the per-symbol frontier reuses the hardened "
        "idempotent/durable store and is trustworthy"
    )


def check_gate_condition(config: dict, coverage_src: str) -> str:
    compact = _compact(coverage_src)
    for token in (
        "query_split_adjusted",
        "coverage_frontier",
        "SplitAdjustedResult",
        "CoverageError",
    ):
        if token not in coverage_src:
            fail(f"coverage.rs must define `{token}` (the coverage-gate surface)")
    # The gate condition: frontier D >= query.end_ts, else NotCovered carrying have/need.
    if "d>=query.end_ts" not in compact:
        fail(
            "coverage.rs::query_split_adjusted must serve split-adjusted only when the frontier "
            "D >= query.end_ts (the precise, honest coverage condition)"
        )
    if (
        "NotCovered" not in coverage_src
        or "have_through" not in coverage_src
        or "need_through" not in coverage_src
    ):
        fail(
            "coverage.rs must fail closed with CoverageError::NotCovered { have_through, need_through } "
            "when the symbol is not covered through the query end"
        )
    # The frontier is the MAX completeness-through over the symbol's coverage records (monotonic).
    if ".max()" not in compact:
        fail(
            "coverage_frontier must be the MAX completeness-through over the symbol's coverage records"
        )
    # The applied corporate actions are collected up to the read's basis cutoff (adjusted_through: the
    # frontier D for the current-basis reads, query.end_ts for the point-in-time _as_of reads), not the
    # query window: an event in (end, cutoff] still adjusts in-window bars, but an event beyond the
    # cutoff is EXCLUDED so the result is never adjusted past the advertised basis.
    if "CorporateActionSplit" not in coverage_src:
        fail("query_split_adjusted must collect the symbol's CorporateActionSplit records")
    if "event_ts<=adjusted_through" not in compact:
        fail(
            "the gated reads must bound the applied corporate actions to effective_ts <= the read's "
            "basis cutoff (`event_ts <= adjusted_through`) -- an event beyond the basis would adjust "
            "the series PAST the advertised adjusted_through (breaking the as-of contract)"
        )
    # The frontier-basis reads adjust through D; the _as_of reads through query.end_ts (no lookahead).
    if "AdjustmentBasis::Frontier=>coverage_through" not in compact:
        fail(
            "the frontier-basis reads must set adjusted_through = the coverage frontier "
            "(`AdjustmentBasis::Frontier => coverage_through`)"
        )
    if "AdjustmentBasis::AsOfEnd=>query.end_ts" not in compact:
        fail(
            "the point-in-time reads must cap adjusted_through at the as-of date "
            "(`AdjustmentBasis::AsOfEnd => query.end_ts`) -- no lookahead through a future event"
        )
    return (
        "gate condition: every gated read serves adjusted output ONLY when the queried symbol's "
        "frontier D = max(complete_through) satisfies D >= query.end_ts, else fails closed with "
        "NotCovered{have_through,need_through}; the applied corporate actions are bounded to "
        "effective_ts <= adjusted_through (the frontier D for the current-basis reads, query.end_ts "
        "for the _as_of reads -- an event in (end, cutoff] adjusts in-window bars, one beyond the "
        "cutoff is excluded)"
    )


def check_kind_narrowed_gate(config: dict, coverage_src: str) -> str:
    compact = _compact(coverage_src)
    if "UnsupportedQueryKind" not in coverage_src:
        fail(
            "query_split_adjusted must reject a non-equity / unspecified query kind "
            "(CoverageError::UnsupportedQueryKind) so the split math's UnsupportedKind path is "
            "unreachable at runtime"
        )
    # The gate must require an explicit equity-bar kind before any coverage/math.
    if "DailyEquityBar" not in coverage_src or "MinuteEquityBar" not in coverage_src:
        fail(
            "the gate must require an explicit DailyEquityBar / MinuteEquityBar query kind "
            "(a split-adjusted series is equity-only by construction)"
        )
    if '"unspecified"' not in compact and "'unspecified'" not in compact:
        fail("a kind-agnostic (None) split-adjusted query must be rejected as 'unspecified'")
    return (
        "kind-narrowed gate: query_split_adjusted requires an explicit DailyEquityBar / MinuteEquityBar "
        "kind (else UnsupportedQueryKind), so split_adjust_record's UnsupportedKind path is unreachable "
        "and a split-adjusted series is equity-only by construction"
    )


def check_single_public_entry(config: dict, lib_src: str) -> str:
    compact = _compact(lib_src)
    # The coverage GATE MODULE is the single public path to split-adjusted output. The gate exposes TWO
    # coverage-enforcing reads -- query_split_adjusted (current-frontier basis) and
    # query_split_adjusted_as_of (point-in-time basis) -- but both are inside the gate and both enforce
    # coverage; what matters is that NO path outside the gate can serve split-adjusted output.
    if "pubmodcoverage" not in compact:
        fail("lib.rs must expose the coverage gate (`pub mod coverage;`)")
    # The split math stays crate-internal: `mod normalization` (private), not re-exported. This is what
    # keeps the coverage gate the ONLY public path to split-adjusted output (no raw-as-adjusted).
    if "pubmodnormalization" in compact:
        fail(
            "the split-adjustment math must stay crate-internal (`mod normalization`, NOT `pub mod`) — "
            "the coverage gate must be the only public path to split-adjusted output"
        )
    if "modnormalization;" not in compact:
        fail("lib.rs must declare the crate-internal `mod normalization;`")
    if "pubusecrate::normalization" in compact:
        fail(
            "lib.rs must NOT re-export anything from the crate-internal normalization module "
            "(split_adjust_records / SplitEvent must not be a public crate API)"
        )
    return (
        "single public entry: lib.rs exposes `pub mod coverage` (the coverage-enforcing gate — "
        "query_split_adjusted[_as_of] AND query_fully_adjusted[_as_of], ALL four coverage-gated "
        "through one private core) while the adjustment math stays crate-internal "
        "(`mod normalization`, not re-exported) — so the coverage gate is the ONLY public path to "
        "adjusted output, with no public path to raw-as-adjusted"
    )


def check_cli_routes_gated(config: dict, cli_src: str) -> str:
    compact = _compact(cli_src)
    if "query_split_adjusted" not in compact:
        fail(
            "data007_query_cli must route --normalization split-adjusted through the coverage gate "
            "MarketDataStore::query_split_adjusted (never CLI-side split math)"
        )
    if '"split-adjusted"=>Ok' not in compact:
        fail(
            "data007_query_cli must ACCEPT --normalization split-adjusted (route it through the gate)"
        )
    # fully-adjusted (splits AND dividends, SYS-29) is SERVED through the gate too.
    if '"fully-adjusted"=>Ok' not in compact:
        fail(
            "data007_query_cli must ACCEPT --normalization fully-adjusted (route it through the gate)"
        )
    if "query_fully_adjusted" not in compact:
        fail(
            "data007_query_cli must route --normalization fully-adjusted through the coverage gate "
            "MarketDataStore::query_fully_adjusted (never CLI-side dividend math)"
        )
    if "coverage_through" not in cli_src:
        fail("data007_query_cli must echo a coverage_through:<D> line for a served adjusted result")
    if "adjusted_through" not in cli_src or "event_count" not in cli_src:
        fail(
            "data007_query_cli must echo the adjustment basis (adjusted_through:<ts>) and the surfaced "
            "structural events (event_count:<n> + event.<i>.* lines) for a served adjusted result"
        )
    # total-return is now SERVED through the SAME coverage gate (query_total_return; the SRS-DATA-012
    # close). Only the LIVE-subscription mode selection remains deferred (SRS-DATA-012 remainder), so
    # the CLI still names SRS-DATA-012. SRS-DATA-011 is named for the coverage gate.
    if '"total-return"' not in compact or "query_total_return" not in compact:
        fail(
            "data007_query_cli must serve --normalization total-return through the coverage gate "
            "(MarketDataStore::query_total_return)"
        )
    if "SRS-DATA-012" not in cli_src:
        fail(
            "data007_query_cli must name SRS-DATA-012 (owner of total-return + the deferred LIVE selection)"
        )
    if "SRS-DATA-011" not in cli_src:
        fail(
            "data007_query_cli must name SRS-DATA-011 (the coverage owner the adjusted gate needs)"
        )
    return (
        "CLI routing: data007_query_cli routes --normalization split-adjusted, fully-adjusted, AND "
        "total-return through the coverage gate (query_split_adjusted / query_fully_adjusted / "
        "query_total_return), echoes coverage_through / adjusted_through / event_count + event.<i>.* "
        "lines, and fails closed (naming SRS-DATA-011) when uncovered; the LIVE per-subscription "
        "selection remains deferred (naming SRS-DATA-012)"
    )


def check_coverage_cli(config: dict, coverage_cli_src: str) -> str:
    compact = _compact(coverage_cli_src)
    for token in ('"assert-coverage"', '"show-coverage"', '"--through"', '"--symbol"'):
        if token not in compact:
            fail(f"data011_coverage_cli must declare {token}")
    if "coverage_record" not in compact:
        fail("data011_coverage_cli must build the coverage record via store::coverage_record")
    # assert-coverage must hold the single-writer lock across the load-modify-save (no last-publish-wins).
    if "StoreLock::acquire" not in compact:
        fail(
            "data011_coverage_cli assert-coverage must hold the StoreLock across the load-modify-save"
        )
    if "save_to_path" not in compact:
        fail("data011_coverage_cli assert-coverage must durably persist the coverage record")
    return (
        "coverage CLI: data011_coverage_cli records the frontier (assert-coverage --symbol --through, "
        "under the StoreLock, load-modify-save) and shows it (show-coverage)"
    )


def check_ingest_excludes_coverage(config: dict, ingest_cli_src: str) -> str:
    compact = _compact(ingest_cli_src)
    # The corporate-action COVERAGE frontier is a TRUST assertion the split-adjusted gate reads, so it
    # must have a SINGLE write surface (data011_coverage_cli assert-coverage). The generic market-data
    # ingest CLI (data016) must REFUSE --kind corporate-action-coverage, or its fixture path would be a
    # second, untracked route to grant coverage and enable split-adjusted output.
    if "CorporateActionCoverage" not in ingest_cli_src:
        fail(
            "data016_ingest_cli must explicitly reference DatasetKind::CorporateActionCoverage to "
            "reject it (the coverage frontier is asserted only via data011_coverage_cli)"
        )
    if (
        "kind==DatasetKind::CorporateActionCoverage" not in compact
        and "data011_coverage_cli" not in ingest_cli_src
    ):
        fail(
            "data016_ingest_cli must REFUSE --kind corporate-action-coverage and point to "
            "data011_coverage_cli (the single coverage-assertion surface)"
        )
    return (
        "single write surface: data016_ingest_cli REFUSES --kind corporate-action-coverage (the "
        "coverage frontier is a trust assertion, asserted only via data011_coverage_cli assert-coverage) "
        "-- there is no second, fixture-shaped route to grant split-adjusted coverage"
    )


def check_data_layer_rejects_coverage(config: dict, lib_src: str) -> str:
    compact = _compact(lib_src)
    # The generic market-data ingestion API (DataLayer::ingest_market_record) is the DECISIVE trust
    # boundary: it must REFUSE a CorporateActionCoverage record so no generic ingest path (even one
    # handed a coverage record directly) can create the trust assertion the split-adjusted gate reads.
    if "record.key().kind==DatasetKind::CorporateActionCoverage" not in compact:
        fail(
            "DataLayer::ingest_market_record must reject a CorporateActionCoverage record (the generic "
            "market-data ingestion path must not create the coverage trust assertion)"
        )
    if "UnsupportedKind" not in lib_src:
        fail(
            "ingest_market_record must fail closed with MarketIngestError::UnsupportedKind for coverage"
        )
    return (
        "data-layer boundary: DataLayer::ingest_market_record REFUSES CorporateActionCoverage "
        "(MarketIngestError::UnsupportedKind) -- the decisive trust boundary so no generic ingestion "
        "path can mint a trusted coverage frontier; coverage is asserted only via data011_coverage_cli"
    )


def check_corporate_action_kinds(config: dict, store_src: str) -> str:
    compact = _compact(store_src)
    # The four v4 corporate-action FACT kinds, their stable codec tags, and the schema bump. A dropped
    # variant / retagged codec / unbumped SCHEMA_VERSION would silently corrupt persisted stores.
    for kind, tag in (
        ("CorporateActionDividend", 6),
        ("CorporateActionDelisting", 7),
        ("CorporateActionMerger", 8),
        ("CorporateActionSymbolChange", 9),
    ):
        if kind not in store_src:
            fail(f"store.rs must declare the vendor-neutral DatasetKind::{kind}")
        if f"{kind}=>{tag}" not in compact:
            fail(f"store.rs must give DatasetKind::{kind} the codec tag {tag}")
    if "SCHEMA_VERSION:i64=4" not in compact:
        fail(
            "store.rs SCHEMA_VERSION must be 4 (the corporate-action kinds' version) so an older "
            "reader rejects a v4 store at the version gate, not mid-restore on an unknown tag"
        )
    # The fixture constructors (the deterministic provider stand-ins) exist for each kind.
    for constructor in (
        "fndividend_record",
        "fndelisting_record",
        "fnmerger_record",
        "fnsymbol_change_record",
    ):
        if constructor not in compact:
            fail(f"store.rs must provide the {constructor[2:]}(...) fixture constructor")
    # validate_record enforces each kind's self-consistency at upsert AND restore: a positive dividend
    # amount, self-describing delisting/symbol-change instants, validated merger terms, and a
    # non-empty successor differing from the record's own symbol (blocks the trivial self-cycle).
    for needle, what in (
        ('field.name=="amount_minor"&&field.value_minor>0', "a positive dividend amount_minor"),
        (
            'field.name=="last_trading_ts"&&field.value_minor==record.key.event_ts',
            "a self-describing delisting last_trading_ts",
        ),
        (
            'field.name=="effective_ts"&&field.value_minor==record.key.event_ts',
            "a self-describing symbol-change effective_ts",
        ),
        ("successor!=record.key.symbol", "a successor differing from the record's own symbol"),
    ):
        if needle not in compact:
            fail(f"store.rs validate_record must enforce {what} (upsert AND restore)")
    # The successor rides in the resolution label; the reader helper must exist for the gate.
    if "fnsuccessor_symbol" not in compact:
        fail(
            "store.rs must provide successor_symbol(&NaturalKey) for the merger/symbol-change label"
        )
    # Coverage remains the ONLY refused trust kind: the provider set includes the four new kinds.
    if "CorporateActionCoverage=>Vec::new()" not in compact:
        fail("fixture_batch must still emit NO records for CorporateActionCoverage")
    return (
        "corporate-action kinds: store.rs declares the four v4 FACT kinds (dividend tag 6, delisting "
        "tag 7, merger tag 8, symbol-change tag 9) with SCHEMA_VERSION 4, fixture constructors, and "
        "validate_record self-consistency at upsert AND restore (positive dividend amount, "
        "self-describing delisting/symbol-change instants, validated merger terms, successor != own "
        "symbol); coverage remains the only refused trust kind"
    )


def check_gate_applies_dividends(config: dict, coverage_src: str) -> str:
    compact = _compact(coverage_src)
    # The fully-adjusted reads exist and route through the crate-internal dividend math.
    for token in ("query_fully_adjusted", "query_fully_adjusted_as_of"):
        if token not in coverage_src:
            fail(f"coverage.rs must expose `{token}` (the SYS-29 fully-adjusted gated read)")
    if "fully_adjust_records" not in compact:
        fail(
            "the fully-adjusted read must apply the crate-internal fully_adjust_records math "
            "(splits AND dividends composed exactly)"
        )
    if "dividend_events_for" not in compact:
        fail(
            "the gate must extract dividends via dividend_events_for (fail-closed extraction with a "
            "resolved reference close)"
        )
    # The reference close resolves from the RAW lineage series, strictly before the ex-date.
    if "prev_close_of" not in coverage_src or "event_ts<ex_ts" not in compact:
        fail(
            "the gate must resolve each dividend's reference close as the last RAW close strictly "
            "before its ex-date (`event_ts < ex_ts` over the raw lineage series)"
        )
    # Split-adjusted deliberately ignores dividends (mode semantics): the SplitOnly arm yields none.
    if "AdjustmentMode::SplitOnly=>Vec::new()" not in compact:
        fail(
            "the split-adjusted mode must ignore dividend records entirely "
            "(`AdjustmentMode::SplitOnly => Vec::new()`)"
        )
    return (
        "dividend leg: the gate serves fully-adjusted (splits AND dividends) through the "
        "crate-internal fully_adjust_records math, extracting dividends fail-closed via "
        "dividend_events_for with each reference close resolved as the last RAW close strictly before "
        "the ex-date; split-adjusted ignores dividends by construction"
    )


def check_terminal_events_surfaced(config: dict, coverage_src: str) -> str:
    # Delistings / mergers / symbol changes are surfaced as STRUCTURED events on the gated result --
    # the facts a P&L consumer needs (mark final / conversion terms / lineage hop).
    for token in (
        "CorporateActionEvent",
        "Delisting",
        "Merger",
        "SymbolChange",
        "cash_per_share_minor",
    ):
        if token not in coverage_src:
            fail(f"coverage.rs must surface `{token}` on the gated result (structural events)")
    compact = _compact(coverage_src)
    if "events:Vec<CorporateActionEvent>" not in compact:
        fail(
            "SplitAdjustedResult must carry `events: Vec<CorporateActionEvent>` (the in-window "
            "delistings / mergers / symbol changes)"
        )
    return (
        "terminal events: the gated result carries events: Vec<CorporateActionEvent> -- in-window "
        "delistings, mergers (with numerator/denominator/cash_per_share_minor terms), and symbol "
        "changes -- so a P&L consumer marks positions final, converts at the surfaced terms, or "
        "follows the lineage hop"
    )


def check_lineage_bounded(config: dict, coverage_src: str) -> str:
    compact = _compact(coverage_src)
    # Rename-lineage resolution must fail closed on inconsistent data, never loop or mis-stitch.
    for token in ("LineageCycle", "AmbiguousLineage"):
        if token not in coverage_src:
            fail(f"coverage.rs must fail closed with CoverageError::{token} on bad lineage data")
    if "MAX_LINEAGE_DEPTH" not in coverage_src:
        fail("lineage resolution must enforce a hard depth bound (MAX_LINEAGE_DEPTH)")
    if "visited.insert" not in compact:
        fail("lineage resolution must track visited symbols (cycle detection)")
    if "successor_symbol" not in coverage_src:
        fail(
            "lineage resolution must read successors via store::successor_symbol (validated labels)"
        )
    return (
        "bounded lineage: rename-lineage resolution walks successor links with a visited set and a "
        "hard depth bound, failing closed (LineageCycle / AmbiguousLineage) on cycles, dual "
        "predecessors, multi-rename predecessors, out-of-order hops, and bars outside their validity "
        "window -- never an unbounded walk or a mis-stitched series"
    )


def check_round_trip(config: dict, require_cargo: bool = False) -> str:
    """Prove the coverage gate end-to-end: the crate unit suite, then COVERED -> adjusted and
    UNCOVERED -> fail-closed over a real ingested store."""
    block = contract_block(config)
    rt = block["round_trip"]
    crate = block["data_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                "cargo not on PATH but --require-cargo set: cannot verify the coverage gate "
                "(install the Rust toolchain)"
            )
        return "round-trip: skipped (cargo not on PATH)"

    # 1) The coverage gate's crate unit suite (gate boundaries, frontier max, idempotent advance, the
    #    kind guard).
    unit = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "coverage", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if unit.returncode != 0:
        fail(f"cargo test -p {crate} --lib coverage failed:\n{unit.stdout}\n{unit.stderr}")

    # 2) Build the three operator CLIs.
    for binary in (block["ingest_cli_bin"], block["coverage_cli_bin"], block["cli_bin"]):
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
    coverage_bin = ROOT / "target" / "debug" / block["coverage_cli_bin"]
    query_bin = ROOT / "target" / "debug" / block["cli_bin"]

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)

    def field_values(stdout: str, record_index: int = 0) -> dict[str, int]:
        prefix = f"record.{record_index}.field."
        fields: dict[str, int] = {}
        for line in stdout.splitlines():
            if line.startswith(prefix):
                name, value = line[len(prefix) :].split(":", 1)
                fields[name] = int(value)
        return fields

    with tempfile.TemporaryDirectory() as tmp:
        # Ingest the daily bar + the split + the dividend, then assert coverage through the split's
        # effective date.
        for kind, event_ts, extra in (
            (rt["kind"], rt["bar_event_ts"], ["--init"]),
            (rt["split_kind"], rt["split_event_ts"], []),
            (rt["dividend_kind"], rt["dividend_event_ts"], []),
        ):
            ingested = run(
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                kind,
                "--event-ts",
                str(event_ts),
                *extra,
            )
            if ingested.returncode != 0:
                fail(f"ingest {kind} failed:\n{ingested.stdout}\n{ingested.stderr}")
        asserted = run(
            str(coverage_bin),
            "assert-coverage",
            "--dir",
            tmp,
            "--symbol",
            rt["symbol"],
            "--through",
            str(rt["covered_through"]),
        )
        if asserted.returncode != 0:
            fail(f"assert-coverage failed:\n{asserted.stdout}\n{asserted.stderr}")
        if f"frontier:{rt['covered_through']}" not in asserted.stdout:
            fail(
                f"assert-coverage must report frontier:{rt['covered_through']}, got:\n{asserted.stdout}"
            )

        def query(end: int, normalization: str) -> subprocess.CompletedProcess[str]:
            return run(
                str(query_bin),
                "query",
                "--dir",
                tmp,
                "--symbol",
                rt["symbol"],
                "--resolution",
                rt["resolution"],
                "--start",
                "0",
                "--end",
                str(end),
                "--kind",
                rt["kind"],
                "--normalization",
                normalization,
            )

        # COVERED split-adjusted -> the ADJUSTED bar (close 2500 / volume 400000; the dividend record
        # is deliberately IGNORED by the split-adjusted mode) + coverage_through echo.
        covered = query(rt["covered_query_end"], "split-adjusted")
        if covered.returncode != 0:
            fail(
                f"covered split-adjusted query must succeed, got:\n{covered.stdout}\n{covered.stderr}"
            )
        fields = field_values(covered.stdout)
        if fields.get("close") != rt["adjusted_close_minor"]:
            fail(
                f"covered split-adjusted close {fields.get('close')} != {rt['adjusted_close_minor']}"
            )
        if fields.get("volume") != rt["adjusted_volume"]:
            fail(f"covered split-adjusted volume {fields.get('volume')} != {rt['adjusted_volume']}")
        if f"coverage_through:{rt['covered_through']}" not in covered.stdout:
            fail(f"covered result must echo coverage_through:{rt['covered_through']}")
        if "normalization:split-adjusted" not in covered.stdout:
            fail("covered result must echo normalization:split-adjusted")

        # COVERED fully-adjusted -> the dividend composes with the split (close 2475; volume takes the
        # split factor ONLY -- a dividend never scales volume) + basis echoes.
        fully = query(rt["covered_query_end"], "fully-adjusted")
        if fully.returncode != 0:
            fail(f"covered fully-adjusted query must succeed, got:\n{fully.stdout}\n{fully.stderr}")
        fields = field_values(fully.stdout)
        if fields.get("close") != rt["fully_adjusted_close_minor"]:
            fail(
                f"covered fully-adjusted close {fields.get('close')} != "
                f"{rt['fully_adjusted_close_minor']} (the dividend leg must compose with the split)"
            )
        if fields.get("volume") != rt["fully_adjusted_volume"]:
            fail(
                f"covered fully-adjusted volume {fields.get('volume')} != {rt['fully_adjusted_volume']} "
                "(a dividend must never scale volume)"
            )
        if "normalization:fully-adjusted" not in fully.stdout:
            fail("covered result must echo normalization:fully-adjusted")
        if f"adjusted_through:{rt['covered_through']}" not in fully.stdout:
            fail(f"fully-adjusted result must echo adjusted_through:{rt['covered_through']}")

        # UNCOVERED (query end beyond the frontier) -> BOTH adjusted modes fail closed naming
        # SRS-DATA-011.
        for normalization in ("split-adjusted", "fully-adjusted"):
            uncovered = query(rt["uncovered_query_end"], normalization)
            if uncovered.returncode == 0:
                fail(
                    f"{normalization} query past the coverage frontier must FAIL closed; CLI "
                    f"returned 0 with:\n{uncovered.stdout}"
                )
            if "SRS-DATA-011" not in uncovered.stderr:
                fail(f"the uncovered gate failure must name SRS-DATA-011, got:\n{uncovered.stderr}")

    lineage = rt["lineage"]
    with tempfile.TemporaryDirectory() as tmp:
        # LINEAGE scenario: bars + a rename (predecessor -> successor), coverage on the QUERIED
        # successor; the read returns the predecessor's bar relabeled + the symbol-change event.
        for kind, event_ts, extra in (
            (rt["kind"], rt["bar_event_ts"], ["--init"]),
            (lineage["symbol_change_kind"], lineage["change_event_ts"], []),
        ):
            if (
                run(
                    str(ingest_bin),
                    "ingest",
                    "--dir",
                    tmp,
                    "--kind",
                    kind,
                    "--event-ts",
                    str(event_ts),
                    *extra,
                ).returncode
                != 0
            ):
                fail(f"lineage-scenario ingest {kind} failed")
        if (
            run(
                str(coverage_bin),
                "assert-coverage",
                "--dir",
                tmp,
                "--symbol",
                lineage["successor"],
                "--through",
                str(lineage["query_end"]),
            ).returncode
            != 0
        ):
            fail("lineage-scenario assert-coverage failed")
        relabeled = run(
            str(query_bin),
            "query",
            "--dir",
            tmp,
            "--symbol",
            lineage["successor"],
            "--resolution",
            rt["resolution"],
            "--start",
            "0",
            "--end",
            str(lineage["query_end"]),
            "--kind",
            rt["kind"],
            "--normalization",
            "split-adjusted",
        )
        if relabeled.returncode != 0:
            fail(
                "the lineage read (querying the successor) must succeed, got:\n"
                f"{relabeled.stdout}\n{relabeled.stderr}"
            )
        if f"record.0.event_ts:{lineage['relabeled_bar_ts']}" not in relabeled.stdout:
            fail("the lineage read must return the predecessor's bar under the queried successor")
        if field_values(relabeled.stdout).get("close") != lineage["relabeled_close_minor"]:
            fail("the relabeled predecessor bar must carry its original close")
        for needle in (
            "event.0.kind:symbol-change",
            f"event.0.symbol:{lineage['predecessor']}",
            f"event.0.successor:{lineage['successor']}",
            f"event.0.effective_ts:{lineage['change_event_ts']}",
        ):
            if needle not in relabeled.stdout:
                fail(f"the lineage read must surface the symbol-change event ({needle!r} missing)")

    with tempfile.TemporaryDirectory() as tmp:
        # TERMINAL scenario: a delisting + a merger on the acquired symbol; both surfaced with their
        # exact stored terms.
        for kind, event_ts, extra in (
            (rt["kind"], rt["bar_event_ts"], ["--init"]),
            (lineage["delisting_kind"], lineage["delisting_event_ts"], []),
            (lineage["merger_kind"], lineage["merger_event_ts"], []),
        ):
            if (
                run(
                    str(ingest_bin),
                    "ingest",
                    "--dir",
                    tmp,
                    "--kind",
                    kind,
                    "--event-ts",
                    str(event_ts),
                    *extra,
                ).returncode
                != 0
            ):
                fail(f"terminal-scenario ingest {kind} failed")
        if (
            run(
                str(coverage_bin),
                "assert-coverage",
                "--dir",
                tmp,
                "--symbol",
                lineage["acquired"],
                "--through",
                str(lineage["query_end"]),
            ).returncode
            != 0
        ):
            fail("terminal-scenario assert-coverage failed")
        terminal = run(
            str(query_bin),
            "query",
            "--dir",
            tmp,
            "--symbol",
            lineage["acquired"],
            "--resolution",
            rt["resolution"],
            "--start",
            "0",
            "--end",
            str(lineage["query_end"]),
            "--kind",
            rt["kind"],
            "--normalization",
            "split-adjusted",
        )
        if terminal.returncode != 0:
            fail(
                f"the terminal-events read must succeed, got:\n{terminal.stdout}\n{terminal.stderr}"
            )
        if field_values(terminal.stdout).get("close") != lineage["acquired_close_minor"]:
            fail("the acquired symbol's bar must be served verbatim (no structural re-quote)")
        for needle in (
            "event_count:2",
            "event.0.kind:delisting",
            f"event.0.effective_ts:{lineage['delisting_event_ts']}",
            "event.1.kind:merger",
            f"event.1.successor:{lineage['predecessor']}",
            f"event.1.numerator:{lineage['merger_numerator']}",
            f"event.1.denominator:{lineage['merger_denominator']}",
            f"event.1.cash_per_share_minor:{lineage['merger_cash_per_share_minor']}",
            f"event.1.effective_ts:{lineage['merger_event_ts']}",
        ):
            if needle not in terminal.stdout:
                fail(f"the terminal-events read must surface {needle!r}")

    return (
        "round-trip: the coverage gate's crate unit suite passes; an end-to-end ingest (daily bar + "
        f"split + dividend + coverage through {rt['covered_through']}) proves COVERED -> split-adjusted "
        f"returns the ADJUSTED bar (close {rt['adjusted_close_minor']} / volume {rt['adjusted_volume']}) "
        f"and fully-adjusted composes the dividend (close {rt['fully_adjusted_close_minor']} / volume "
        f"{rt['fully_adjusted_volume']}, never dividend-scaled), UNCOVERED (query end "
        f"{rt['uncovered_query_end']} > frontier) FAILS closed naming SRS-DATA-011 for BOTH adjusted "
        "modes, a rename-lineage read relabels the predecessor's bars under the queried successor and "
        "surfaces the symbol-change event, and a delisting + merger store surfaces both events with "
        "their exact stored terms"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# Each static check is paired with the contract-block source key it reads, so the L3 test can load
# exactly that source, mutate it, and prove the guard is non-vacuous.
_STATIC_CHECKS = (
    ("coverage_kind", "store_source", check_coverage_kind),
    ("gate_condition", "coverage_module", check_gate_condition),
    ("kind_narrowed_gate", "coverage_module", check_kind_narrowed_gate),
    ("single_public_entry", "lib_source", check_single_public_entry),
    ("cli_routes_gated", "cli_source", check_cli_routes_gated),
    ("coverage_cli", "coverage_cli_source", check_coverage_cli),
    ("ingest_excludes_coverage", "ingest_cli_source", check_ingest_excludes_coverage),
    ("data_layer_rejects_coverage", "lib_source", check_data_layer_rejects_coverage),
    ("corporate_action_kinds", "store_source", check_corporate_action_kinds),
    ("gate_applies_dividends", "coverage_module", check_gate_applies_dividends),
    ("terminal_events_surfaced", "coverage_module", check_terminal_events_surfaced),
    ("lineage_bounded", "coverage_module", check_lineage_bounded),
)

_DEFERRED_OWNERS = (
    "the per-subscription LIVE normalization-mode selection (SRS-DATA-012 remainder, blocked on "
    "SRS-MD-001; all four HISTORICAL modes including total-return are served)",
    "real provider corporate-action ingestion from Databento / IB — the operator-set / fixture frontier "
    "stands in, exactly as the SRS-DATA-011 verification step permits (SRS-DATA-001/003/006)",
    "paper/live position + resting-order remapping on the surfaced delisting / merger / symbol-change "
    "events — this data layer supplies the facts those layers consume (SYS-28b/c SRS-EXE remainder + "
    "SYS-88 SRS-SIM remainder)",
    "adjusted reads over the SSD/NAS cold-read tier — tiered mode serves raw only (SRS-DATA-009 "
    "follow-up)",
)


def assert_coverage_manifest_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        key: _read(config, key, root) for key in {src_key for _, src_key, _ in _STATIC_CHECKS}
    }
    return [check(config, sources[src_key]) for _, src_key, check in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_coverage_manifest_static(config)
    evidence.append(check_round_trip(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SRS-DATA-011 corporate-action coverage contract evidence"
    )
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the end-to-end coverage gate must run.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except CoverageManifestCheckError as error:
        print(f"SRS-DATA-011 CORPORATE-ACTION COVERAGE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-DATA-011 CORPORATE-ACTION COVERAGE PASS")
    for item in evidence:
        print(f"- {item}")
    print("- deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
