#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-015 (schema evolution for stored data entities).

SRS-DATA-015 (SyRS SYS-66; StRS SN-1.26 / SN-1.27 / C-5). The acceptance criterion: "Each persisted
entity records a schema version; data written under older schema versions remains queryable after
schema updates without bulk migration."

The first clause quantifies over EVERY persisted entity, so it is only as true as the enumeration
behind it. `crates/atp-data/src/schema_registry.rs` is that enumeration; this script is what stops it
from becoming a stale list of good intentions:

  (a) REGISTRY   -- parse `PERSISTED_ENTITIES` out of the Rust registry and check its structural
      invariants (unique ids/magics, 1 <= min <= current, pinned => min == current).
  (b) BINDING    -- for every registered entity, open the writer it names and confirm the source
      really defines its version `marker` with the version the registry claims, and really contains
      its declared magic. A registry that drifted from the code fails here.
  (c) TOTALITY   -- scan every `crates/*/src/**.rs` and `python/**.py` for a durable-persistence
      write surface. Any file that writes persistently and is neither a registered writer nor on the
      explicit NON_ENTITY_WRITERS allow-list (with a reason) FAILS the check. This is the clause-1
      guarantee: a new persisted format cannot enter the repo unversioned and unnoticed.
  (d) EVOLUTION  -- confirm the golden corpus under `tests/fixtures/schema_evolution/` still holds a
      historical payload for every entity that accepts a version-less payload, so clause 2 keeps a
      live regression lock rather than a one-off manual check.

The PASS line is ``SRS-DATA-015 SCHEMA-EVOLUTION PASS``.

Invoke:
    python3 tools/data015_schema_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "crates" / "atp-data" / "src" / "schema_registry.rs"
CORPUS = ROOT / "tests" / "fixtures" / "schema_evolution"
CONTRACT_FILE = ROOT / "architecture" / "runtime_services.json"
CONTRACT_KEY = "schema_evolution_contract"

# Durable-persistence write surfaces. Serialising to stdout, an HTTP body, or a WebSocket frame is
# NOT persistence — only these actually put bytes on disk to be read back later.
RUST_WRITE_SURFACES = ("fs::write", "File::create", "OpenOptions")
PYTHON_WRITE_SURFACES = (".write_text(", "os.replace(", "open(")

# Files that touch a write surface but do NOT own a persisted format. Each needs a reason: this list
# is the only way to stay silent about a write, so an entry without justification is a hole.
NON_ENTITY_WRITERS: dict[str, str] = {
    "crates/atp-simulation/src/bin/sim004_persist_cli.rs": (
        "fault-injection demo: deliberately corrupts/truncates a paper-state snapshot to prove the "
        "reader fails closed. It defines no format of its own — the bytes it damages belong to "
        "paper-state-snapshot, which is registered."
    ),
    "crates/atp-data/src/backup.rs": (
        "SRS-DATA-018 export: writes a byte-for-byte copy of an ALREADY-REGISTERED entity's blob "
        "(market-data-store or backtest-record-store) and re-verifies it through that entity's own "
        "codec. Its `envelope` framing is `<magic>\\n<checksum>\\n<body>` — the store layout itself, "
        "not a second format — so an exported backup identifies and version-reports exactly like the "
        "source, which `srs_data_015_a_backup_export_is_still_the_source_entity` proves rather than "
        "assumes. A backup that invented its own header would be a new persisted entity and would "
        "belong in the registry instead."
    ),
}


class SchemaCheckError(AssertionError):
    """Raised when a structural contract is violated."""


@dataclass(frozen=True)
class Descriptor:
    entity_id: str
    owner_srs: str
    writer_path: str
    marker: str
    magic: str | None
    current_version: int
    min_supported_version: int
    posture: str
    legacy_unversioned: bool


# --------------------------------------------------------------------------- #
# (a) parse the registry
# --------------------------------------------------------------------------- #

_ENTITY_BLOCK = re.compile(r"SchemaDescriptor\s*\{(.*?)\}\s*,\s*\n", re.DOTALL)


def _field(block: str, name: str) -> str:
    match = re.search(rf"{name}\s*:\s*(.+?),\s*\n", block, re.DOTALL)
    if match is None:
        raise SchemaCheckError(f"registry entry is missing field {name!r}:\n{block}")
    return match.group(1).strip()


def _unquote(value: str) -> str:
    match = re.fullmatch(r'"(.*)"', value, re.DOTALL)
    if match is None:
        raise SchemaCheckError(f"expected a string literal, got {value!r}")
    return match.group(1)


def parse_registry(source: str) -> list[Descriptor]:
    """Parse the `PERSISTED_ENTITIES` table out of the Rust registry source.

    Parsed rather than imported so the check has no build dependency: it must run (and fail) even
    when the crate does not compile.
    """

    start = source.find("pub const PERSISTED_ENTITIES")
    if start < 0:
        raise SchemaCheckError("schema_registry.rs does not define PERSISTED_ENTITIES")
    end = source.find("\n];", start)
    if end < 0:
        raise SchemaCheckError("PERSISTED_ENTITIES table is not terminated")
    # Keep a trailing newline: the entry regex anchors on the newline after each entry's `},`, and
    # the last entry in the table would otherwise be silently dropped — a parse hole that would make
    # this whole check quietly under-report.
    table = source[start:end] + "\n"

    entities: list[Descriptor] = []
    for block in _ENTITY_BLOCK.findall(table):
        magic_raw = _field(block, "magic")
        if magic_raw == "None":
            magic: str | None = None
        else:
            inner = re.fullmatch(r"Some\((.*)\)", magic_raw, re.DOTALL)
            if inner is None:
                raise SchemaCheckError(f"unparseable magic field: {magic_raw!r}")
            magic = _unquote(inner.group(1).strip())
        entities.append(
            Descriptor(
                entity_id=_unquote(_field(block, "entity_id")),
                owner_srs=_unquote(_field(block, "owner_srs")),
                writer_path=_unquote(_field(block, "writer_path")),
                marker=_unquote(_field(block, "marker")),
                magic=magic,
                current_version=int(_field(block, "current_version")),
                min_supported_version=int(_field(block, "min_supported_version")),
                posture=_field(block, "posture").split("::")[-1],
                legacy_unversioned=_field(block, "legacy_unversioned") == "true",
            )
        )
    if not entities:
        raise SchemaCheckError("PERSISTED_ENTITIES parsed as empty")
    # A partial parse is worse than no parse: an entity this regex silently skipped would be exempt
    # from every check below AND would look like an unregistered writer. Count the literal
    # constructors and demand they all came through.
    declared = table.count("SchemaDescriptor {")
    if declared != len(entities):
        raise SchemaCheckError(
            f"parsed {len(entities)} registry entries but the table declares {declared} — the "
            "registry parser dropped an entry and cannot be trusted"
        )
    return entities


def check_registry_invariants(entities: list[Descriptor]) -> None:
    seen_ids: set[str] = set()
    seen_magics: set[str] = set()
    for entity in entities:
        if entity.entity_id in seen_ids:
            raise SchemaCheckError(f"duplicate entity id: {entity.entity_id}")
        seen_ids.add(entity.entity_id)
        if entity.magic is not None:
            if entity.magic in seen_magics:
                raise SchemaCheckError(f"two entities share the magic {entity.magic!r}")
            seen_magics.add(entity.magic)
        if entity.min_supported_version < 1 or entity.current_version < 1:
            raise SchemaCheckError(f"{entity.entity_id}: versions must be >= 1")
        if entity.min_supported_version > entity.current_version:
            raise SchemaCheckError(
                f"{entity.entity_id}: min_supported_version exceeds current_version"
            )
        if entity.posture == "Pinned" and entity.min_supported_version != entity.current_version:
            raise SchemaCheckError(
                f"{entity.entity_id}: a pinned entity must accept exactly one version"
            )
        if entity.legacy_unversioned and entity.posture != "MigrateOnRead":
            raise SchemaCheckError(
                f"{entity.entity_id}: reading a version-less payload IS an on-read migration; "
                "posture must be MigrateOnRead"
            )


# --------------------------------------------------------------------------- #
# (b) bind the registry to the real writers
# --------------------------------------------------------------------------- #


def _declared_constant(source: str, marker: str) -> int | None:
    """The integer `marker` is defined as, in either Rust or Python.

    The name is matched with a boundary on the LEFT so `SCHEMA_VERSION` never matches
    `MIN_SUPPORTED_SCHEMA_VERSION`, and with an explicit `:` type or `=` on the right so it never
    matches `SCHEMA_VERSION_V1`.
    """

    rust = re.search(rf"(?<![A-Za-z0-9_]){re.escape(marker)}\s*:\s*[iu]64\s*=\s*(\d+)", source)
    if rust is not None:
        return int(rust.group(1))
    python = re.search(rf"(?<![A-Za-z0-9_]){re.escape(marker)}\s*=\s*(\d+)\b", source)
    if python is not None:
        return int(python.group(1))
    return None


def check_writer_binding(entities: list[Descriptor]) -> None:
    for entity in entities:
        writer = ROOT / entity.writer_path
        if not writer.is_file():
            raise SchemaCheckError(
                f"{entity.entity_id}: registered writer {entity.writer_path} does not exist"
            )
        source = writer.read_text(encoding="utf-8")
        if entity.marker not in source:
            raise SchemaCheckError(
                f"{entity.entity_id}: writer {entity.writer_path} does not mention its version "
                f"marker {entity.marker} — the version does not reach the persisted bytes"
            )
        declared = _declared_constant(source, entity.marker)
        if declared is None:
            raise SchemaCheckError(
                f"{entity.entity_id}: could not read a version constant for {entity.marker} in "
                f"{entity.writer_path}"
            )
        if declared != entity.current_version:
            raise SchemaCheckError(
                f"{entity.entity_id}: registry says current_version={entity.current_version} but "
                f"{entity.writer_path} defines {entity.marker}={declared}"
            )
        if entity.magic is not None and entity.magic not in source:
            raise SchemaCheckError(
                f"{entity.entity_id}: writer {entity.writer_path} does not contain its declared "
                f"magic {entity.magic!r}"
            )


# --------------------------------------------------------------------------- #
# (c) totality — no unregistered persisted format
# --------------------------------------------------------------------------- #


def _persists(path: Path, source: str) -> bool:
    """Whether `source` durably writes bytes to a file.

    `open(...)` alone is not persistence (reads use it too), so the Python side requires a write
    mode. Test modules are excluded: a test that writes a fixture does not define a format.
    """

    if path.suffix == ".rs":
        return any(surface in source for surface in RUST_WRITE_SURFACES)
    if ".write_text(" in source or "os.replace(" in source:
        return True
    return bool(re.search(r"open\([^)]*['\"][wxa]", source))


def scan_write_surfaces() -> list[str]:
    """Every repo-relative source file that durably persists bytes."""

    found: list[str] = []
    roots = [ROOT / "crates", ROOT / "python"]
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".rs", ".py") or not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            # Rust unit tests live inline; only `src/` files define shipped formats, and a `tests/`
            # directory holds test code in both languages.
            if "/tests/" in relative or relative.startswith("tests/"):
                continue
            if path.suffix == ".rs" and "/src/" not in relative:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix == ".rs":
                # Strip the inline `#[cfg(test)]` module so a test-only fs::write does not read as a
                # shipped persistence surface.
                cut = source.find("#[cfg(test)]")
                if cut >= 0:
                    source = source[:cut]
            if _persists(path, source):
                found.append(relative)
    return found


def check_totality(entities: list[Descriptor]) -> list[str]:
    registered = {entity.writer_path for entity in entities}
    unregistered: list[str] = []
    for relative in scan_write_surfaces():
        if relative in registered or relative in NON_ENTITY_WRITERS:
            continue
        unregistered.append(relative)
    if unregistered:
        raise SchemaCheckError(
            "these files persist bytes but are neither a registered persisted entity nor an "
            "explicitly-justified non-entity writer — SRS-DATA-015 requires EVERY persisted entity "
            "to record a schema version:\n  " + "\n  ".join(unregistered)
        )
    # An allow-list entry for a file that no longer persists anything is stale and must be pruned,
    # or it silently widens the exemption for a future file at that path.
    scanned = set(scan_write_surfaces())
    stale = [path for path in NON_ENTITY_WRITERS if path not in scanned]
    if stale:
        raise SchemaCheckError(
            "NON_ENTITY_WRITERS names files that no longer persist anything (prune them): "
            + ", ".join(sorted(stale))
        )
    return sorted(registered)


# --------------------------------------------------------------------------- #
# (d) evolution — the golden corpus keeps clause 2 under test
# --------------------------------------------------------------------------- #

CORPUS_FIXTURES = {
    "access-journal": "access_journal_legacy.log",
    "kill-switch-last-activation": "kill_switch_last_activation_legacy.json",
    "system-log-segment": "system_log_segment_legacy.jsonl",
    "hot-swap-trigger-log": "hot_swap_trigger_log_legacy.jsonl",
    "readiness-alert-sink": "readiness_alert_sink_legacy.jsonl",
}


def check_corpus(entities: list[Descriptor]) -> None:
    if not CORPUS.is_dir():
        raise SchemaCheckError(f"golden corpus missing at {CORPUS}")
    for entity in entities:
        if not entity.legacy_unversioned:
            continue
        fixture = CORPUS_FIXTURES.get(entity.entity_id)
        if fixture is None:
            raise SchemaCheckError(
                f"{entity.entity_id} accepts a version-less payload but CORPUS_FIXTURES names no "
                "historical fixture proving it stays readable"
            )
        if not (CORPUS / fixture).is_file():
            raise SchemaCheckError(f"{entity.entity_id}: missing golden fixture {fixture}")
    # The market store's multi-version history is the ranged-evolution evidence.
    for version in (1, 2, 3, 4):
        blob = CORPUS / f"market_store_v{version}.store"
        if not blob.is_file():
            raise SchemaCheckError(f"missing market-store v{version} fixture")
        declared = blob.read_text(encoding="utf-8").splitlines()[2]
        if declared != str(version):
            raise SchemaCheckError(
                f"market_store_v{version}.store declares version {declared!r}, not {version}"
            )


def check_contract_block(entities: list[Descriptor]) -> None:
    contract = json.loads(CONTRACT_FILE.read_text(encoding="utf-8")).get(CONTRACT_KEY)
    if contract is None:
        raise SchemaCheckError(f"{CONTRACT_FILE.name} has no {CONTRACT_KEY} block")
    declared = contract.get("entity_count")
    if declared != len(entities):
        raise SchemaCheckError(
            f"{CONTRACT_KEY}.entity_count is {declared} but the registry holds {len(entities)}"
        )
    for key in ("registry_module", "check_script", "cli", "golden_corpus"):
        if not contract.get(key):
            raise SchemaCheckError(f"{CONTRACT_KEY} is missing '{key}'")
    if not (ROOT / contract["registry_module"]).is_file():
        raise SchemaCheckError(f"{CONTRACT_KEY}.registry_module does not exist")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="print the parsed registry instead of checking"
    )
    args = parser.parse_args()

    try:
        entities = parse_registry(REGISTRY.read_text(encoding="utf-8"))
        if args.list:
            for entity in entities:
                print(
                    f"{entity.entity_id}\t{entity.owner_srs}\t{entity.writer_path}\t"
                    f"v{entity.min_supported_version}..{entity.current_version}\t{entity.posture}"
                )
            return 0
        check_registry_invariants(entities)
        check_writer_binding(entities)
        writers = check_totality(entities)
        check_corpus(entities)
        check_contract_block(entities)
    except SchemaCheckError as error:
        print(f"SRS-DATA-015 SCHEMA-EVOLUTION FAIL: {error}", file=sys.stderr)
        return 1

    retrofitted = [entity.entity_id for entity in entities if entity.legacy_unversioned]
    print(
        f"SRS-DATA-015 SCHEMA-EVOLUTION PASS: {len(entities)} persisted entities registered across "
        f"{len(writers)} writers; every one records a schema version and declares the range it "
        f"reads. {len(retrofitted)} entities accept a version-less legacy payload "
        f"({', '.join(retrofitted)}) and each has a byte-frozen fixture in "
        f"tests/fixtures/schema_evolution/ proving it stays queryable in place. "
        f"{len(NON_ENTITY_WRITERS)} non-entity writer"
        f"{'' if len(NON_ENTITY_WRITERS) == 1 else 's'} explicitly justified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
