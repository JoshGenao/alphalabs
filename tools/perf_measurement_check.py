#!/usr/bin/env python3
"""Contract evidence script for feature SRS-PERF-001.

Verifies the latency-percentile verification substrate declared in
``architecture/runtime_services.json`` (block ``perf_measurement_contract``) and
implemented in ``crates/atp-types/src/perf.rs``, and — critically — that its
measurement boundaries and budgets MATCH THE SPEC measurement conditions: the
SyRS §5.1 performance table for NFR-P1/P4/P5/P6/P9/P10, and the SRS-MD-001
requirement row (docs/SRS.md) for the fan-out 100 ms budget.

SRS-PERF-001 (SyRS §5.1 NFR-P1 / NFR-P4 / NFR-P5 / NFR-P6 / NFR-P9 / NFR-P10 +
SRS-MD-001 fan-out latency; StRS SN-1.01 / SN-2.03) — "measure latency-sensitive
performance metrics against a PTP-disciplined system clock with documented offset
bounds and report p50, p95, p99, and p99.9 percentiles in verification
artifacts." The contract guarantees:

  (a) the reported-percentile set is exactly {p50, p95, p99, p99.9} in both the
      metadata and ``perf.rs`` (``REPORTED_PERCENTILES`` + ``Percentile::per_mille``);
  (b) the ``LatencyNfr`` catalog covers exactly the seven AC NFRs;
  (c) each NFR's budget(s) MATCH its spec measurement condition — NFR-P1/P4/P5/
      P6/P9/P10 against the SyRS §5.1 table (``docs/SyRS_v0.7.md`` `<`/`≤` ms) and
      SRS-MD-001 fan-out against the SRS requirement row (``docs/SRS.md`` prose
      "no more than 100 ms") — the "boundaries match the measurement conditions"
      AC clause, enforced by inspection;
  (d) each NFR's measurement boundary phrase appears in BOTH its spec row and the
      ``perf.rs`` ``boundary()`` arm;
  (e) the Rust threshold constants match the metadata (and the reused NFR-P4 /
      NFR-P9 constants keep their authoritative values);
  (f) ``PtpClockDiscipline`` documents an offset bound (``Disciplined`` /
      ``Undisciplined`` + ``max_offset_ns``);
  (g) ``LatencyVerificationArtifact::from_samples`` FAILS CLOSED on a
      non-disciplined clock, an empty window, and no samples, and the artifact
      documents the max clock offset;
  (h) ``perf.rs`` leaks no vendor SDK token; and
  (i) SRS-PERF-001 stays ``passes:false`` until the deferred runtimes + a
      PTP-disciplined host produce the real end-to-end artifacts.

Mirrors the PASS/FAIL output style of ``tools/sequence_gap_check.py``.

Invoke:
    python3 tools/perf_measurement_check.py
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
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
CONTRACT = "perf_measurement_contract"
FEATURE = "SRS-PERF-001"


class PerfMeasurementCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise PerfMeasurementCheckError(message)


def _fn_body(source: str, fn_name: str) -> str:
    """Body of ``[pub] [const] fn <fn_name>`` up to its closing brace.

    Like ``_rust_parser._fn_block`` but tolerant of an optional ``const`` (the
    perf catalog is heavily ``const fn``) and of a non-``pub`` fn (trait-impl
    methods like ``fmt`` are not ``pub``); local so the shared parser stays
    untouched.
    """
    match = re.search(rf"\b(?:pub\s+)?(?:const\s+)?fn\s+{re.escape(fn_name)}\b[^{{]*{{", source)
    if not match:
        fail(f"perf.rs is missing function `{fn_name}`")
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


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if CONTRACT not in config:
        fail(f"architecture metadata is missing {CONTRACT}")
    return config[CONTRACT]


def _crate_root(config: dict, root: Path) -> Path:
    return root / contract_block(config)["types_crate"]["path"]


def perf_source(config: dict, root: Path = ROOT) -> str:
    path = _crate_root(config, root) / contract_block(config)["module_file"]
    if not path.exists():
        fail(f"perf module source not found at {path}")
    return path.read_text(encoding="utf-8")


def order_event_source(config: dict, root: Path = ROOT) -> str:
    path = _crate_root(config, root) / contract_block(config)["order_event_file"]
    if not path.exists():
        fail(f"order-event source not found at {path}")
    return path.read_text(encoding="utf-8")


def types_lib_source(config: dict, root: Path = ROOT) -> str:
    path = _crate_root(config, root) / contract_block(config)["types_lib_file"]
    if not path.exists():
        fail(f"types lib source not found at {path}")
    return path.read_text(encoding="utf-8")


def syrs_text(config: dict, root: Path = ROOT) -> str:
    path = root / contract_block(config)["syrs_doc"]
    if not path.exists():
        fail(f"SyRS document not found at {path}")
    return path.read_text(encoding="utf-8")


def srs_doc_text(config: dict, root: Path = ROOT) -> str:
    path = root / contract_block(config)["srs_doc"]
    if not path.exists():
        fail(f"SRS document not found at {path}")
    return path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Spec-doc parsing (the "boundaries match the measurement conditions" authority)
#
# Most budgets live in the SyRS §5.1 performance table (docs/SyRS_v0.7.md) with an
# explicit `<`/`≤ N ms`. The SRS-MD-001 fan-out budget lives in the SRS
# requirement row (docs/SRS.md) stated in prose ("no more than 100 ms additional
# latency"), so it is validated separately.
# --------------------------------------------------------------------------- #

_MS_RE = re.compile(r"([<≤])\s*([0-9][0-9,]*)\s*ms")
_UPPER_BOUND_PHRASES = ("no more than", "at most", "within", "under", "≤", "<=", "<")


def _doc_row_text(doc_text: str, row_id: str) -> str:
    """Return the content text (all columns except the id and the trailing
    trace column) of the markdown table row for ``row_id`` in ``doc_text``.

    Selects the WIDEST matching row (the full spec table, not a narrow
    reverse-trace table). Joining all content cells rather than reading a fixed
    column index tolerates layout differences (NFR-P10's merged requirement/metric
    cell; the SRS requirement table's extra columns).
    """
    row_re = re.compile(rf"^\|\s*{re.escape(row_id)}\s*\|", re.MULTILINE)
    best: list[str] | None = None
    best_pipes = -1
    for line in doc_text.splitlines():
        if not row_re.match(line):
            continue
        pipes = line.count("|")
        if pipes > best_pipes:
            best = [c.strip() for c in line.split("|")]
            best_pipes = pipes
    if best is None:
        fail(f"spec doc has no table row for {row_id}")
    if len(best) < 5:
        fail(f"spec row for {row_id} is not a full spec table row: {best}")
    # cells = ['', id, <content...>, trace, '']; drop id + the trailing trace column.
    return " ".join(best[2:-2])


def _parse_ms_pairs(cell: str) -> list[tuple[int, str]]:
    """(bound_ms, comparison) pairs parsed from a `<`/`≤ N ms` metric cell."""
    pairs: list[tuple[int, str]] = []
    for sym, num in _MS_RE.findall(cell):
        comparison = "<=" if sym == "≤" else "<"
        pairs.append((int(num.replace(",", "")), comparison))
    return pairs


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_percentile_set(config: dict, perf_src: str) -> str:
    spec = contract_block(config)["reported_percentiles"]
    expected_labels = [p["label"] for p in spec]
    if expected_labels != ["p50", "p95", "p99", "p99.9"]:
        fail(f"reported_percentiles must be exactly p50/p95/p99/p99.9, got {expected_labels}")
    per_mille_body = _compact(_fn_body(perf_src, "per_mille"))
    for p in spec:
        arm = f"Self::{_variant_for_label(p['label'])}=>{p['per_mille']},"
        if arm not in per_mille_body:
            fail(f"Percentile::per_mille is missing/incorrect arm for {p['label']} ({arm})")
    # REPORTED_PERCENTILES declares all four, in order (not anchored on the
    # closing bracket, to tolerate rustfmt's trailing comma).
    reported = _compact(perf_src)
    if (
        "REPORTED_PERCENTILES:[Percentile;4]=[Percentile::P50,Percentile::P95,Percentile::P99,Percentile::P999"
        not in reported
    ):
        fail("REPORTED_PERCENTILES must list [P50, P95, P99, P999] in ascending order")
    return "reported percentiles are exactly p50/p95/p99/p99.9 (per_mille 500/950/990/999), pinned in metadata + perf.rs"


def _variant_for_label(label: str) -> str:
    return {"p50": "P50", "p95": "P95", "p99": "P99", "p99.9": "P999"}[label]


def check_nfr_catalog(config: dict, perf_src: str) -> str:
    nfrs = contract_block(config)["nfrs"]
    id_arm = _compact(_fn_body(perf_src, "id"))
    for nfr in nfrs:
        arm = f'Self::{nfr["rust_variant"]}=>"{nfr["id"]}",'
        if arm not in id_arm:
            fail(f"LatencyNfr::id is missing arm {arm} for {nfr['id']}")
    # LATENCY_NFRS lists exactly the seven variants, in order.
    listed = _compact(perf_src)
    variants = ",".join(f"LatencyNfr::{n['rust_variant']}" for n in nfrs)
    if f"LATENCY_NFRS:[LatencyNfr;7]=[{variants}," not in listed:
        fail("LATENCY_NFRS must list exactly the seven catalog variants in AC order")
    return f"LatencyNfr catalog covers the {len(nfrs)} AC NFRs ({', '.join(n['id'] for n in nfrs)})"


def _budget_doc_text(nfr: dict, syrs: str, srs: str) -> str:
    return srs if nfr.get("budget_doc", "syrs") == "srs" else syrs


def check_thresholds_match_syrs(config: dict, syrs: str, srs: str) -> str:
    nfrs = contract_block(config)["nfrs"]
    checked = 0
    for nfr in nfrs:
        json_pairs = [(t["bound_ms"], t["comparison"]) for t in nfr["thresholds"]]
        mode = nfr["match_mode"]
        if mode == "none":
            if json_pairs:
                fail(f"{nfr['id']} declares match_mode none but carries thresholds {json_pairs}")
            continue
        row_text = _doc_row_text(_budget_doc_text(nfr, syrs, srs), nfr["doc_row"])
        if mode == "prose":
            # Budget stated in prose (e.g. SRS-MD-001 "no more than 100 ms
            # additional latency"): assert the literal `N ms` + an upper-bound
            # qualifier consistent with the `<=` comparison.
            for bound_ms, comparison in json_pairs:
                if f"{bound_ms} ms" not in row_text:
                    fail(
                        f"{nfr['id']} budget {bound_ms} ms not found in its spec row: {row_text!r}"
                    )
                if comparison == "<=" and not any(p in row_text for p in _UPPER_BOUND_PHRASES):
                    fail(
                        f"{nfr['id']} declares a <= budget but its spec row states no upper bound "
                        f"(expected e.g. 'no more than'): {row_text!r}"
                    )
            checked += 1
            continue
        doc_pairs = _parse_ms_pairs(row_text)
        if not doc_pairs:
            fail(f"{nfr['id']} spec row has no ms budget: {row_text!r}")
        if mode == "exact":
            if sorted(doc_pairs) != sorted(json_pairs):
                fail(
                    f"{nfr['id']} budget mismatch: metadata {sorted(json_pairs)} != "
                    f"spec {sorted(doc_pairs)} (row {row_text!r})"
                )
        elif mode == "subset":
            for pair in json_pairs:
                if pair not in doc_pairs:
                    fail(
                        f"{nfr['id']} budget {pair} not found in spec row values "
                        f"{doc_pairs} (row {row_text!r})"
                    )
        else:
            fail(f"{nfr['id']} has unknown match_mode {mode!r}")
        checked += 1
    return f"all {checked} NFR budgets match their SRS/SyRS measurement conditions (docs/)"


def check_boundary_matches_syrs(config: dict, perf_src: str, syrs: str, srs: str) -> str:
    nfrs = contract_block(config)["nfrs"]
    boundary_body = _fn_body(perf_src, "boundary")
    for nfr in nfrs:
        phrase = nfr["boundary_phrase"]
        if phrase not in boundary_body:
            fail(f"{nfr['id']} boundary phrase {phrase!r} missing from perf.rs boundary()")
        if nfr.get("doc_row"):
            if phrase not in _doc_row_text(_budget_doc_text(nfr, syrs, srs), nfr["doc_row"]):
                fail(
                    f"{nfr['id']} boundary phrase {phrase!r} not found in its spec row "
                    "— boundary does not match the measurement condition"
                )
    return f"all {len(nfrs)} NFR boundary phrases match perf.rs and their SRS/SyRS conditions"


def check_rust_thresholds_match_metadata(
    config: dict, perf_src: str, order_event_src: str, lib_src: str
) -> str:
    block = contract_block(config)
    compact_perf = _compact(perf_src)
    # Constants defined in perf.rs.
    for _id, spec in block["defined_threshold_constants"].items():
        pat = rf"pubconst{re.escape(spec['const'])}:u64=" + _int_variants(spec["value"])
        if not re.search(pat, compact_perf):
            fail(f"perf.rs constant {spec['const']} must equal {spec['value']}")
    # Reused NFR-P4 live/paper constants (order_event.rs) keep their values.
    p4 = block["reused_threshold_constants"]["NFR-P4"]
    for key in ("live", "paper"):
        c = p4[key]
        if not re.search(
            rf"pubconst{re.escape(c['const'])}:u32=" + _int_variants(c["value"]),
            _compact(order_event_src),
        ):
            fail(f"order_event.rs constant {c['const']} must equal {c['value']} (NFR-P4 {key})")
    # Reused NFR-P9 startup constant (lib.rs).
    p9 = block["reused_threshold_constants"]["NFR-P9"]
    if not re.search(
        rf"pubconst{re.escape(p9['const'])}:u64=" + _int_variants(p9["value"]),
        _compact(lib_src),
    ):
        fail(f"lib.rs constant {p9['const']} must equal {p9['value']} (NFR-P9)")
    # perf.rs actually reuses those constants (no silent re-hardcode).
    for token in (
        "LIVE_CALLBACK_LATENCY_P95_MSasu64",
        "PAPER_CALLBACK_LATENCY_P95_MSasu64",
        "STRATEGY_STARTUP_DEADLINE_MS",
    ):
        if token not in compact_perf:
            fail(f"perf.rs must reuse the authoritative constant {token} (found none)")
    return "Rust threshold constants match metadata; NFR-P4/P9 reuse the authoritative constants"


def _int_variants(value: int) -> str:
    """Regex alternation matching an integer with or without `_` digit grouping."""
    plain = str(value)
    grouped = f"{value:_}"
    return f"(?:{re.escape(grouped)}|{re.escape(plain)})"


def check_stated_percentiles(config: dict, perf_src: str) -> str:
    # The stated percentile is PER LEG (a `LatencyThreshold` field), so a mixed
    # NFR (NFR-P10: order_latency=p95, dashboard_refresh=flat) cannot evaluate its
    # flat leg as p95. Verify each leg's stated_percentile inside its own static
    # threshold array (anchored by the array name, so `""` labels don't collide
    # across NFRs).
    if "stated_percentile: Option<Percentile>" not in perf_src:
        fail("LatencyThreshold must carry a per-leg `stated_percentile: Option<Percentile>`")
    p95_legs = 0
    flat_legs = 0
    mixed = []
    for nfr in contract_block(config)["nfrs"]:
        static_name = nfr["thresholds_static"]
        m = re.search(
            rf"static\s+{re.escape(static_name)}\s*:\s*\[LatencyThreshold;\s*\d+\]\s*=\s*\[(.*?)\];",
            perf_src,
            re.DOTALL,
        )
        if not m:
            fail(f"perf.rs is missing the static threshold array {static_name} for {nfr['id']}")
        body = _compact(m.group(1))
        seen_percentiles = set()
        for t in nfr["thresholds"]:
            sp = t["stated_percentile"]
            expected = "Some(Percentile::P95)" if sp == "p95" else "None"
            pat = rf'label:"{re.escape(t["label"])}"[^}}]*stated_percentile:{re.escape(expected)}'
            if not re.search(pat, body):
                fail(
                    f"{nfr['id']} leg {t['label']!r} must declare stated_percentile "
                    f"{sp!r} in {static_name}"
                )
            seen_percentiles.add(sp)
            if sp == "p95":
                p95_legs += 1
            else:
                flat_legs += 1
        if len(seen_percentiles) > 1:
            mixed.append(nfr["id"])
    return (
        f"stated percentile is per-leg: {p95_legs} p95 legs, {flat_legs} flat-max legs; "
        f"mixed-semantics NFRs ({', '.join(mixed) or 'none'}) never evaluate a flat leg as p95"
    )


def check_clock_discipline(config: dict, perf_src: str) -> str:
    spec = contract_block(config)["clock_discipline"]
    body = _compact(_enum_body(perf_src, spec["enum"]))
    for variant in spec["variants"]:
        if variant not in body:
            fail(f"{spec['enum']} is missing variant {variant}")
    if f"{spec['offset_field']}:u64" not in body:
        fail(
            f"{spec['enum']}::Disciplined must carry {spec['offset_field']}: u64 (documented offset bound)"
        )
    if f"fn{spec['accessor']}(self)->Option<u64>" not in _compact(perf_src):
        fail(f"{spec['enum']} must expose {spec['accessor']}(self) -> Option<u64>")
    return f"{spec['enum']} documents an offset bound ({spec['offset_field']}) with {len(spec['variants'])} states"


def check_artifact_fail_closed(config: dict, perf_src: str) -> str:
    spec = contract_block(config)["artifact"]
    fc = spec["fail_closed"]
    enum_body = _compact(_enum_body(perf_src, fc["error_enum"]))
    for variant in fc["variants"]:
        if variant not in enum_body:
            fail(f"{fc['error_enum']} is missing fail-closed variant {variant}")
    # Scope the constructor lookup to the artifact's impl — `from_samples` also
    # names the (guard-free) LatencyPercentiles constructor defined earlier.
    impl_at = perf_src.find(f"impl {spec['struct']}")
    if impl_at < 0:
        fail(f"perf.rs is missing `impl {spec['struct']}`")
    ctor = _compact(_fn_body(perf_src[impl_at:], spec["constructor"]))
    if _compact(fc["clock_guard"]) not in ctor:
        fail(f"{spec['constructor']} must reject a non-disciplined clock via {fc['clock_guard']}")
    if fc["window_guard"] not in ctor:
        fail(f"{spec['constructor']} must reject an empty/inverted window ({fc['window_guard']})")
    if "checked_sub" not in ctor or fc["overflow_guard"] not in ctor:
        fail(
            f"{spec['constructor']} must reject a window whose duration overflows i64 "
            f"(checked_sub -> {fc['overflow_guard']})"
        )
    if "threshold_for_leg" not in ctor or fc["leg_guard"] not in ctor:
        fail(
            f"{spec['constructor']} must reject a leg the NFR does not define "
            f"(threshold_for_leg -> {fc['leg_guard']}) so multi-leg samples cannot be mis-attributed"
        )
    if fc["samples_guard"] not in _compact(perf_src):
        fail(f"the artifact path must reject empty samples ({fc['samples_guard']})")
    return (
        f"{spec['struct']}::{spec['constructor']} fails closed on non-disciplined clock, "
        f"unknown leg, empty/inverted window, window-duration overflow, and no samples"
    )


def check_multi_leg_verification(config: dict, perf_src: str) -> str:
    spec = contract_block(config)["artifact"]
    # The artifact records which leg it measured.
    struct_body = _compact(_struct_body(perf_src, spec["struct"]))
    if f"{spec['leg_field']}:&'staticstr" not in struct_body:
        fail(f"{spec['struct']} must carry the leg field {spec['leg_field']}: &'static str")
    if f"fn{spec['leg_accessor']}(&self)->&'staticstr" not in _compact(perf_src):
        fail(f"{spec['struct']} must expose {spec['leg_accessor']}(&self) -> &'static str")
    # The completeness bundle: assemble() must reject an incomplete leg set by
    # comparing against the NFR's declared leg labels.
    mlv = spec["multi_leg_verification"]
    if f"pub struct {mlv['struct']}" not in perf_src:
        fail(f"perf.rs must define the multi-leg completeness bundle `{mlv['struct']}`")
    impl_at = perf_src.find(f"impl {mlv['struct']}")
    if impl_at < 0:
        fail(f"perf.rs is missing `impl {mlv['struct']}`")
    assemble = _compact(_fn_body(perf_src[impl_at:], mlv["constructor"]))
    if mlv["incomplete_variant"] not in assemble:
        fail(
            f"{mlv['struct']}::{mlv['constructor']} must fail closed via {mlv['incomplete_variant']} "
            "when a leg is missing/duplicated"
        )
    if mlv["labels_method"] not in assemble:
        fail(
            f"{mlv['struct']}::{mlv['constructor']} must enforce coverage against the NFR's "
            f"{mlv['labels_method']}() (every leg present)"
        )
    # Simultaneity: NFRs flagged simultaneous_legs (NFR-P10) require overlapping
    # leg windows — assemble must consult the discriminator and the leg windows.
    sim = mlv["simultaneity_method"]
    if f"fn{sim}(self)->bool" not in _compact(perf_src):
        fail(f"perf.rs must define {sim}(self) -> bool")
    if sim not in assemble or "measurement_window_ns" not in assemble:
        fail(
            f"{mlv['struct']}::{mlv['constructor']} must enforce leg-window overlap for "
            f"simultaneity-required NFRs (consult {sim}() + measurement_window_ns)"
        )
    sim_body = _compact(_fn_body(perf_src, sim))
    # The multi-leg NFRs actually declare >1 distinct non-empty leg.
    multi = {
        n["id"]: [t["label"] for t in n["thresholds"]]
        for n in contract_block(config)["nfrs"]
        if len(n["thresholds"]) > 1
    }
    for nfr_id, labels in multi.items():
        if len(set(labels)) != len(labels) or "" in labels:
            fail(f"{nfr_id} multi-leg labels must be distinct and non-empty, got {labels}")
        for label in labels:
            if f'"{label}"' not in perf_src:
                fail(f"{nfr_id} leg label {label!r} is not present in perf.rs")
    # NFRs the metadata marks simultaneous must be simultaneity-required in Rust.
    for n in contract_block(config)["nfrs"]:
        if n.get("simultaneous_legs") and f"Self::{n['rust_variant']}" not in sim_body:
            fail(f"{n['id']} is metadata-marked simultaneous but {sim}() does not require it")
    simultaneous = [n["id"] for n in contract_block(config)["nfrs"] if n.get("simultaneous_legs")]
    return (
        f"{spec['struct']} binds samples to a leg; {mlv['struct']} requires every leg of a "
        f"multi-leg NFR ({', '.join(sorted(multi))}) with simultaneous windows for "
        f"{', '.join(simultaneous) or 'none'}"
    )


def check_artifact_documents_offset(config: dict, perf_src: str) -> str:
    spec = contract_block(config)["artifact"]
    struct_body = _compact(_struct_body(perf_src, spec["struct"]))
    if f"{spec['offset_field']}:u64" not in struct_body:
        fail(f"{spec['struct']} must carry the documented offset field {spec['offset_field']}: u64")
    if f"fn{spec['offset_accessor']}(&self)->u64" not in _compact(perf_src):
        fail(f"{spec['struct']} must expose {spec['offset_accessor']}(&self) -> u64")
    # Verify the *rendering* itself — scope to the artifact's Display fmt body so
    # a doc-comment mention cannot satisfy the check.
    display_at = perf_src.find(f"impl fmt::Display for {spec['struct']}")
    if display_at < 0:
        fail(f"{spec['struct']} must implement fmt::Display (the inspectable artifact)")
    display_body = _fn_body(perf_src[display_at:], "fmt")
    for token in spec["render_tokens"]:
        if token not in display_body:
            fail(f"{spec['struct']} Display must render {token!r} in the verification artifact")
    # The four percentiles are rendered by iterating the reported-percentile set
    # (so p50/p95/p99/p99.9 are all emitted, not a hand-picked subset).
    if spec["percentile_iteration"] not in display_body:
        fail(
            f"{spec['struct']} Display must iterate {spec['percentile_iteration']} so all four "
            "percentiles (incl. p99.9) are rendered"
        )
    return (
        f"{spec['struct']} Display documents max clock offset, the window, and all four percentiles"
    )


def check_vendor_isolation(config: dict, perf_src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [tok for tok in tokens if tok in perf_src]
    if leaked:
        fail(
            f"perf.rs leaks vendor SDK token(s): {', '.join(leaked)} (measurement substrate is vendor-neutral)"
        )
    return f"perf.rs free of all {len(tokens)} forbidden vendor SDK tokens"


def check_feature_stays_false(config: dict, root: Path = ROOT) -> str:
    features = json.loads((root / "feature_list.json").read_text(encoding="utf-8"))
    entry = next((f for f in features if f["id"] == FEATURE), None)
    if entry is None:
        fail(f"feature_list.json is missing {FEATURE}")
    if entry["passes"] is not False:
        fail(
            f"{FEATURE} must remain passes:false until the deferred runtimes + a "
            f"PTP-disciplined host produce the real end-to-end artifacts "
            f"(see {CONTRACT}.deferred[])"
        )
    if not contract_block(config).get("deferred"):
        fail(f"{CONTRACT} must name the deferred owners in deferred[]")
    return (
        f"feature_list.json keeps {FEATURE} passes:false (serialized; runtimes + PTP host deferred)"
    )


_TYPES_CHECKS = (
    check_percentile_set,
    check_nfr_catalog,
    check_stated_percentiles,
    check_clock_discipline,
    check_artifact_fail_closed,
    check_artifact_documents_offset,
    check_multi_leg_verification,
    check_vendor_isolation,
)


def assert_perf_measurement_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (e.g. from architecture_check.py)."""
    perf_src = perf_source(config, root)
    order_event_src = order_event_source(config, root)
    lib_src = types_lib_source(config, root)
    syrs = syrs_text(config, root)
    srs = srs_doc_text(config, root)
    evidence = [check(config, perf_src) for check in _TYPES_CHECKS]
    evidence.append(check_thresholds_match_syrs(config, syrs, srs))
    evidence.append(check_boundary_matches_syrs(config, perf_src, syrs, srs))
    evidence.append(
        check_rust_thresholds_match_metadata(config, perf_src, order_event_src, lib_src)
    )
    evidence.append(check_feature_stays_false(config, root))
    return evidence


def _cargo_smoke(config: dict, root: Path = ROOT) -> str:
    test_name = contract_block(config)["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo smoke skipped (cargo not on PATH): would run {test_name}"
    result = subprocess.run(
        [cargo, "test", "-p", "atp-types", "--test", test_name],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(
            f"cargo test -p atp-types --test {test_name} failed:\n{result.stdout}\n{result.stderr}"
        )
    return f"cargo integration test {test_name} passes"


def run_checks(root: Path = ROOT) -> list[str]:
    config = load_config(root)
    evidence = assert_perf_measurement_static(config, root)
    evidence.append(_cargo_smoke(config, root))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-PERF-001 perf-measurement contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except PerfMeasurementCheckError as error:
        print(f"{FEATURE} FAIL: {error}", file=sys.stderr)
        return 1

    print(f"{FEATURE} PERF-MEASUREMENT PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
