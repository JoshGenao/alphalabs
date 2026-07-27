#!/usr/bin/env python3
"""SRS-EXE-007 / SyRS SYS-65 — the IB TWS API version-upgrade gate.

SRS-EXE-007 has two clauses. The first ("the adapter documents the supported IB
TWS API version") was already satisfied by SRS-EXE-006's contract surface:
``INTERACTIVE_BROKERS_TWS_API_VERSION`` in ``crates/atp-adapters/src/lib.rs``,
mirrored by ``adapter_contract.interactive_brokers.protocol_version`` in
``architecture/runtime_services.json`` and cross-checked by
``tools/adapter_check.py``.

The second clause — "API version upgrades are tested against the IB paper trading
account **before** deployment to live trading" — had no enforcement, and that was
a fail-open hole: the declared version string was bound to nothing that a paper
run produces. The operator evidence artifact
(``architecture/ib_paper_account_evidence.json``) is bound by ``code_digest`` to
the *wire sources only*, so editing the version constant from ``10.19.4`` to any
other value (and the matching metadata value) passed every check in the tree — an
unvalidated API-version upgrade shipped green.

This check closes that hole. The declared version is bound to a validation record
(``architecture/ib_api_version_support.json``) which in turn is bound to the
paper-account evidence artifact by that artifact's ``code_digest`` — and only an
operator paper run regenerates it::

    ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py   # port 4002

So a version bump cannot pass this gate until a real IB paper-account round trip
has been re-run at the new version and the support record updated from the fresh
artifact. Declaring a version is no longer enough to deploy it.

**Fail closed, always.** A missing, unreadable, duplicate-keyed, schema-drifted,
or partially-populated artifact is a FAILURE, never a skip: a gate that cannot
read its inputs can never report the upgrade validated. The check is pure
inspection — no network, no subprocess, no IB — so it runs in the default CI
mirror alongside the other contract checks.

Run::

    python3 tools/ib_api_version_check.py
    python3 tools/ib_api_version_check.py --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SUPPORT_REL = "architecture/ib_api_version_support.json"
EVIDENCE_REL = "architecture/ib_paper_account_evidence.json"
RUNTIME_REL = "architecture/runtime_services.json"
ADAPTERS_LIB_REL = "crates/atp-adapters/src/lib.rs"

#: Schema version of the support record this check understands.
SUPPORT_SCHEMA = 1

#: Exact top-level key set of the support record. Unknown keys are refused (an
#: alias or a typo must never be silently ignored on a safety gate).
SUPPORT_KEYS = frozenset(
    {
        "schema_version",
        "srs_ref",
        "supported_tws_api_version",
        "negotiated_server_version",
        "validated_against_paper_account",
    }
)
VALIDATION_KEYS = frozenset(
    {
        "evidence_ref",
        "evidence_code_digest",
        "evidence_generated_at",
        "provenance",
    }
)

#: The IB TWS API package generation, e.g. "10.19.4". Refuse anything else so a
#: degenerate value ("", "latest", "10.19.4 ") can never stand in for a version.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DECLARED_VERSION_RE = re.compile(
    r'pub const INTERACTIVE_BROKERS_TWS_API_VERSION: &str = "([^"]*)";'
)
PINNED_SERVER_RE = re.compile(r"IB_PINNED_SERVER_VERSION: i32 = (\d+);")


class IbApiVersionError(AssertionError):
    """A version-compatibility invariant is unmet — the gate refuses."""


def fail(message: str) -> None:
    raise IbApiVersionError(message)


class _DuplicateKeyError(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _DuplicateKeyError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def load_json_strict(path: Path, label: str) -> dict[str, Any]:
    """Read a JSON object, failing closed on every unreadable shape.

    Duplicate keys are refused rather than last-one-wins: on a gate artifact, a
    repeated ``supported_tws_api_version`` must never resolve silently.
    """
    if not path.exists():
        fail(f"{label} is missing at {path} — the version gate cannot verify an absent record")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"{label} at {path} is unreadable: {exc}")
    try:
        loaded = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKeyError as exc:
        fail(f"{label} at {path} has {exc} — refusing an ambiguous record")
    except ValueError as exc:
        fail(f"{label} at {path} is not valid JSON: {exc}")
    if not isinstance(loaded, dict):
        fail(f"{label} at {path} must be a JSON object, got {type(loaded).__name__}")
    return loaded


def read_source(rel: str, root: Path = ROOT) -> str:
    path = root / rel
    if not path.exists():
        fail(f"expected source file {rel} is missing")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"source file {rel} is unreadable: {exc}")
    return ""  # pragma: no cover — fail() always raises


def declared_tws_api_version(root: Path = ROOT) -> str:
    """The version the adapter DOCUMENTS, parsed from the Rust source of truth."""
    match = DECLARED_VERSION_RE.search(read_source(ADAPTERS_LIB_REL, root))
    if not match:
        fail(
            f"could not find INTERACTIVE_BROKERS_TWS_API_VERSION in {ADAPTERS_LIB_REL} — "
            f"the documented-version source of truth moved or was removed"
        )
    return match.group(1)


def pinned_server_version(runtime: dict[str, Any], root: Path = ROOT) -> int:
    """The negotiated wire protocol version the handshake pins (distinct from the
    TWS API package generation above — see architecture/README.md)."""
    module = runtime.get("module")
    if not isinstance(module, str) or not module:
        fail("ib_brokerage_runtime.module is missing — cannot locate the wire source")
    src = read_source(module, root) + read_source(module.replace(".rs", "/wire.rs"), root)
    match = PINNED_SERVER_RE.search(src)
    if not match:
        fail("could not find IB_PINNED_SERVER_VERSION in the adapter wire source")
    return int(match.group(1))


def _ib_runtime(root: Path = ROOT) -> tuple[dict[str, Any], dict[str, Any]]:
    services = load_json_strict(root / RUNTIME_REL, "runtime services metadata")
    contract = services.get("adapter_contract")
    if not isinstance(contract, dict):
        fail(f"{RUNTIME_REL} has no adapter_contract block")
    ib_meta = contract.get("interactive_brokers")
    runtime = contract.get("ib_brokerage_runtime")
    if not isinstance(ib_meta, dict):
        fail(f"{RUNTIME_REL}: adapter_contract.interactive_brokers is missing")
    if not isinstance(runtime, dict):
        fail(f"{RUNTIME_REL}: adapter_contract.ib_brokerage_runtime is missing")
    return ib_meta, runtime


def check_support_record(support: dict[str, Any]) -> dict[str, Any]:
    """Shape + value validation of the support record itself (no cross-artifact
    comparison yet). Every refusal here means "unverifiable", never "assume ok"."""
    if support.get("schema_version") != SUPPORT_SCHEMA:
        fail(
            f"{SUPPORT_REL}: schema_version must be {SUPPORT_SCHEMA}, got "
            f"{support.get('schema_version')!r} — refusing to interpret a drifted record"
        )
    keys = set(support)
    if keys != SUPPORT_KEYS:
        missing = sorted(SUPPORT_KEYS - keys)
        unknown = sorted(keys - SUPPORT_KEYS)
        fail(
            f"{SUPPORT_REL}: key set must be exactly {sorted(SUPPORT_KEYS)} "
            f"(missing={missing}, unknown={unknown})"
        )
    if support.get("srs_ref") != "SRS-EXE-007":
        fail(f"{SUPPORT_REL}: srs_ref must be 'SRS-EXE-007', got {support.get('srs_ref')!r}")

    version = support["supported_tws_api_version"]
    if not isinstance(version, str) or not VERSION_RE.match(version):
        fail(
            f"{SUPPORT_REL}: supported_tws_api_version must be a MAJOR.MINOR.PATCH string, "
            f"got {version!r}"
        )
    server = support["negotiated_server_version"]
    # bool is an int subclass — exclude it explicitly so `true` cannot pass as 1.
    if isinstance(server, bool) or not isinstance(server, int) or server <= 0:
        fail(f"{SUPPORT_REL}: negotiated_server_version must be a positive integer, got {server!r}")

    validation = support["validated_against_paper_account"]
    if not isinstance(validation, dict):
        fail(f"{SUPPORT_REL}: validated_against_paper_account must be an object")
    vkeys = set(validation)
    if vkeys != VALIDATION_KEYS:
        missing = sorted(VALIDATION_KEYS - vkeys)
        unknown = sorted(vkeys - VALIDATION_KEYS)
        fail(
            f"{SUPPORT_REL}: validated_against_paper_account key set must be exactly "
            f"{sorted(VALIDATION_KEYS)} (missing={missing}, unknown={unknown})"
        )
    if validation["evidence_ref"] != EVIDENCE_REL:
        fail(
            f"{SUPPORT_REL}: evidence_ref must be {EVIDENCE_REL!r} (the artifact the operator "
            f"paper run writes), got {validation['evidence_ref']!r}"
        )
    digest = validation["evidence_code_digest"]
    if not isinstance(digest, str) or not SHA256_RE.match(digest):
        fail(f"{SUPPORT_REL}: evidence_code_digest must be a 64-hex-char SHA-256, got {digest!r}")
    for field in ("evidence_generated_at", "provenance"):
        value = validation[field]
        if not isinstance(value, str) or not value.strip():
            fail(
                f"{SUPPORT_REL}: validated_against_paper_account.{field} must be a non-empty string"
            )
    return validation


def check_documented_version(support: dict[str, Any], ib_meta: dict[str, Any], root: Path) -> str:
    """Clause 1 — the documented version agrees across every surface that states it."""
    declared = declared_tws_api_version(root)
    supported = support["supported_tws_api_version"]
    if declared != supported:
        fail(
            f"IB TWS API version upgrade is NOT validated: the adapter declares "
            f"{declared!r} (INTERACTIVE_BROKERS_TWS_API_VERSION in {ADAPTERS_LIB_REL}) but the "
            f"paper-account support record {SUPPORT_REL} validates {supported!r}. Re-run the "
            f"operator paper-account round trip at the new version "
            f"(ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py, port 4002) and update "
            f"the support record from the regenerated evidence BEFORE deploying live "
            f"(SRS-EXE-007 / SyRS SYS-65)."
        )
    metadata = ib_meta.get("protocol_version")
    if metadata != supported:
        fail(
            f"{RUNTIME_REL}: adapter_contract.interactive_brokers.protocol_version "
            f"{metadata!r} disagrees with the validated version {supported!r}"
        )
    return declared


def check_wire_pin(support: dict[str, Any], runtime: dict[str, Any], root: Path) -> int:
    """The negotiated server protocol version must agree everywhere it is stated."""
    pinned = pinned_server_version(runtime, root)
    if support["negotiated_server_version"] != pinned:
        fail(
            f"{SUPPORT_REL}: negotiated_server_version "
            f"{support['negotiated_server_version']} disagrees with the Rust "
            f"IB_PINNED_SERVER_VERSION={pinned} — the validated wire version and the code's "
            f"handshake pin must never drift"
        )
    if runtime.get("pinned_server_version") != pinned:
        fail(
            f"{RUNTIME_REL}: ib_brokerage_runtime.pinned_server_version "
            f"{runtime.get('pinned_server_version')!r} disagrees with the Rust "
            f"IB_PINNED_SERVER_VERSION={pinned}"
        )
    return pinned


def _ib_adapter_check_module():
    """Load ``tools/ib_adapter_check.py`` so its evidence semantics are REUSED here.

    The digest definition and the evidence schema version have exactly one home;
    re-declaring them would let the two checks disagree about what "the same code"
    and "the operator writer's schema" mean.
    """
    spec = importlib.util.spec_from_file_location(
        "_ib_adapter_check_for_version_gate", Path(__file__).with_name("ib_adapter_check.py")
    )
    if spec is None or spec.loader is None:  # pragma: no cover — packaging accident
        fail("could not load tools/ib_adapter_check.py to reuse its code-digest definition")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover — packaging accident
        fail(f"could not import tools/ib_adapter_check.py: {exc}")
    return module


def _load_code_digest_fn():
    return _ib_adapter_check_module()._code_digest


def check_operator_run_shape(evidence: dict[str, Any], runtime: dict[str, Any]) -> None:
    """The artifact must look like what the OPERATOR PATH writes, not like a record
    someone hand-assembled.

    ``ib_adapter_check._write_evidence`` (the ``ATP_RUN_INTEGRATION=1`` path) stamps
    the operator-gated test name, the gate env var, the paper port and the cargo
    ``test result: ok`` line, all taken from the runtime metadata. Requiring every one
    of them — in ``run`` AND in ``--sync``, through this single function so the two can
    never diverge — means a partial or invented record is refused.

    **Trust boundary, stated plainly:** these are integrity checks on a file that lives
    in the repository, so they defeat an incomplete or careless edit, not a determined
    forgery. Nothing here is a cryptographic attestation that a paper-account run
    happened; the whole repo's operator-evidence convention (SRS-EXE-006, SRS-MD-006's
    EvidenceFileIbProbe, SRS-REL-001) shares that property. See
    ``progress.d/session-SRS-EXE-007.md`` for the named follow-up.
    """
    expected_schema = _ib_adapter_check_module().EVIDENCE_SCHEMA
    if evidence.get("schema_version") != expected_schema:
        fail(
            f"{EVIDENCE_REL}: schema_version must be {expected_schema} (the operator writer's "
            f"schema), got {evidence.get('schema_version')!r}"
        )
    integration = runtime.get("integration_test")
    if not isinstance(integration, dict):
        fail(f"{RUNTIME_REL}: ib_brokerage_runtime.integration_test is missing")
    for field, expected in (
        ("test", integration.get("operator_gated_test")),
        ("gate_env", integration.get("gate_env")),
        ("paper_port", integration.get("paper_port")),
    ):
        if evidence.get(field) != expected:
            fail(
                f"{EVIDENCE_REL}: {field}={evidence.get(field)!r} does not match the operator "
                f"round trip declared in {RUNTIME_REL} ({expected!r}) — this artifact does not "
                f"record the run the gate requires"
            )
    result_line = evidence.get("result_line")
    if not isinstance(result_line, str) or "test result: ok" not in result_line:
        fail(
            f"{EVIDENCE_REL}: result_line does not carry the passing cargo output "
            f"('test result: ok'), so it records no successful paper-account run"
        )


def check_paper_validation(
    validation: dict[str, Any], runtime: dict[str, Any], support: dict[str, Any], root: Path
) -> str:
    """Clause 2 — the validated version rides on a live paper-account artifact that
    is still bound to the current wire code."""
    evidence = load_json_strict(root / EVIDENCE_REL, "IB paper-account evidence")
    check_operator_run_shape(evidence, runtime)
    if evidence.get("returncode") != 0:
        fail(
            f"{EVIDENCE_REL}: returncode {evidence.get('returncode')!r} — the recorded "
            f"paper-account run did not pass, so no version is validated by it"
        )
    # The binding that makes clause 2 real: the artifact records WHICH IB TWS API
    # generation the paper account exercised (written by the operator path from the
    # declared constant). Without it, any passing run at any version would appear to
    # validate whatever version happens to be declared today.
    ran_at = evidence.get("tws_api_version")
    if not isinstance(ran_at, str) or not VERSION_RE.match(ran_at):
        fail(
            f"{EVIDENCE_REL}: tws_api_version is missing or malformed ({ran_at!r}) — the "
            f"recorded run does not say which IB TWS API version it exercised, so it "
            f"validates none. Re-run ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py "
            f"against the paper account (port 4002) to regenerate it."
        )
    if ran_at != support["supported_tws_api_version"]:
        fail(
            f"IB TWS API version upgrade is NOT validated: the paper-account run recorded in "
            f"{EVIDENCE_REL} exercised {ran_at!r}, but {SUPPORT_REL} claims {support['supported_tws_api_version']!r} "
            f"is validated. Only a paper-account round trip run AT the declared version "
            f"validates it (ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py, port 4002) "
            f"— SRS-EXE-007 / SyRS SYS-65."
        )
    if evidence.get("pinned_server_version") != support["negotiated_server_version"]:
        fail(
            f"{EVIDENCE_REL}: the recorded run negotiated server version "
            f"{evidence.get('pinned_server_version')!r}, but the support record validates "
            f"{support['negotiated_server_version']!r} — the evidence is for a different wire"
        )

    recorded = evidence.get("code_digest")
    if not isinstance(recorded, str) or not SHA256_RE.match(recorded):
        fail(f"{EVIDENCE_REL}: code_digest is missing or not a SHA-256 ({recorded!r})")
    if validation["evidence_code_digest"] != recorded:
        fail(
            f"IB TWS API version upgrade is NOT validated: {SUPPORT_REL} cites evidence digest "
            f"{validation['evidence_code_digest']} but {EVIDENCE_REL} currently records "
            f"{recorded}. The support record must be regenerated FROM the paper-account "
            f"artifact — a hand-edited citation is not validation (SRS-EXE-007)."
        )
    if validation["evidence_generated_at"] != evidence.get("generated_at"):
        fail(
            f"{SUPPORT_REL}: evidence_generated_at "
            f"{validation['evidence_generated_at']!r} disagrees with {EVIDENCE_REL} "
            f"generated_at {evidence.get('generated_at')!r}"
        )

    current = _load_code_digest_fn()(runtime, root)
    if recorded != current:
        fail(
            f"the IB paper-account evidence is stale: it was recorded against wire code digest "
            f"{recorded} but the tree now hashes to {current}. Re-run the operator paper-account "
            f"round trip before deploying live (SRS-EXE-007 / SRS-EXE-006)."
        )
    return recorded


def run(as_json: bool = False, root: Path = ROOT) -> int:
    support = load_json_strict(root / SUPPORT_REL, "IB API version support record")
    validation = check_support_record(support)
    ib_meta, runtime = _ib_runtime(root)

    version = check_documented_version(support, ib_meta, root)
    pinned = check_wire_pin(support, runtime, root)
    digest = check_paper_validation(validation, runtime, support, root)

    findings = [
        (
            "documented_version",
            f"adapter documents IB TWS API {version} consistently across "
            f"{ADAPTERS_LIB_REL}, {RUNTIME_REL} and {SUPPORT_REL}",
        ),
        (
            "wire_pin",
            f"negotiated server protocol version {pinned} agrees across the Rust pin, "
            f"the runtime metadata and the support record",
        ),
        (
            "paper_validation",
            f"IB TWS API {version} is validated against the IB paper trading account by "
            f"{EVIDENCE_REL} (code digest {digest[:12]}…, still bound to the current wire)",
        ),
        (
            "upgrade_gate",
            "a version bump without a fresh operator paper-account run fails this check "
            "before live deployment (SRS-EXE-007 clause 2 / SyRS SYS-65)",
        ),
    ]
    if as_json:
        print(json.dumps({"status": "PASS", "checks": dict(findings)}, indent=2))
    else:
        for label, detail in findings:
            print(f"PASS [{label}]: {detail}")
        print("SRS-EXE-007 IB TWS API VERSION GATE PASS")
    return 0


def sync(root: Path = ROOT) -> int:
    """Operator path: record a JUST-RUN paper-account validation in the support record.

    Deliberately NOT a way to make the gate green. It refuses unless the evidence
    artifact carries a ``generated_at`` the support record has not already cited —
    i.e. unless a genuinely new paper-account round trip has been run since the last
    time the version was validated. Bumping the version constant and re-syncing
    therefore cannot launder an unvalidated upgrade: with no new run there is nothing
    to sync, and the gate stays red until the operator runs

        ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py    # port 4002
    """
    support = load_json_strict(root / SUPPORT_REL, "IB API version support record")
    validation = check_support_record(support)
    _, runtime = _ib_runtime(root)
    evidence = load_json_strict(root / EVIDENCE_REL, "IB paper-account evidence")

    # Same operator-run shape the gate demands — sync must never accept an artifact
    # that `run` would reject, or it becomes the weak way in.
    check_operator_run_shape(evidence, runtime)
    if evidence.get("returncode") != 0:
        fail(
            f"{EVIDENCE_REL}: returncode {evidence.get('returncode')!r} — refusing to record a "
            f"failed paper-account run as a validation"
        )
    generated_at = evidence.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        fail(f"{EVIDENCE_REL}: generated_at is missing — refusing to sync an untimestamped run")
    if generated_at == validation["evidence_generated_at"]:
        fail(
            f"nothing to sync: {EVIDENCE_REL} still records the run of {generated_at} that "
            f"{SUPPORT_REL} already cites. A version upgrade must be validated by a NEW IB "
            f"paper-account round trip (ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py, "
            f"port 4002) — re-syncing an old run does not validate a new version (SRS-EXE-007)."
        )
    recorded = evidence.get("code_digest")
    if not isinstance(recorded, str) or not SHA256_RE.match(recorded):
        fail(f"{EVIDENCE_REL}: code_digest is missing or not a SHA-256 ({recorded!r})")
    current = _load_code_digest_fn()(runtime, root)
    if recorded != current:
        fail(
            f"{EVIDENCE_REL} was recorded against wire digest {recorded} but the tree now hashes "
            f"to {current} — re-run the paper-account round trip against this code before syncing"
        )

    declared = declared_tws_api_version(root)
    if not VERSION_RE.match(declared):
        fail(
            f"{ADAPTERS_LIB_REL}: INTERACTIVE_BROKERS_TWS_API_VERSION={declared!r} is not a "
            f"MAJOR.MINOR.PATCH version — refusing to record it as validated"
        )
    ran_at = evidence.get("tws_api_version")
    if ran_at != declared:
        fail(
            f"refusing to record an unvalidated upgrade: the paper-account run of "
            f"{generated_at} exercised IB TWS API {ran_at!r}, but the adapter now declares "
            f"{declared!r}. Re-run the round trip against the paper account WITH the new "
            f"version declared (ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py, "
            f"port 4002) — SRS-EXE-007."
        )
    updated = {
        "schema_version": SUPPORT_SCHEMA,
        "srs_ref": "SRS-EXE-007",
        "supported_tws_api_version": declared,
        "negotiated_server_version": pinned_server_version(runtime, root),
        "validated_against_paper_account": {
            "evidence_ref": EVIDENCE_REL,
            "evidence_code_digest": recorded,
            "evidence_generated_at": generated_at,
            "provenance": (
                f"recorded by `tools/ib_api_version_check.py --sync` from the operator IB "
                f"paper-account round trip of {generated_at} (ATP_RUN_INTEGRATION=1, port 4002)"
            ),
        },
    }
    (root / SUPPORT_REL).write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    print(
        f"✓ {SUPPORT_REL}: IB TWS API {declared} recorded as validated against the paper account "
        f"by the run of {generated_at}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SRS-EXE-007 IB TWS API version-upgrade gate")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.add_argument(
        "--sync",
        action="store_true",
        help=(
            "operator: record a NEW paper-account run in the support record "
            "(refuses when no new run exists — it cannot launder an unvalidated upgrade)"
        ),
    )
    args = parser.parse_args()
    try:
        if args.sync:
            return sync()
        return run(as_json=args.json)
    except IbApiVersionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
