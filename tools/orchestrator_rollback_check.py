#!/usr/bin/env python3
"""Contract evidence script for SRS-ORCH-005 — rollback to the previous deployed
strategy version (SyRS SYS-80 / NFR-S2).

Acceptance: "Rollback is available through dashboard, CLI, and REST API; rollback
of the live strategy requires the same confirmation control as live promotion."

What this pins (each check is non-vacuous — the L3 test injects a regression and
proves it fires):

  (a) the retention port — `RetainedDeployedVersionRegistry` is a SUPERTRAIT
      extension of the frozen SRS-ORCH-004 `DeployedVersionRegistry` (whose
      record/lookup contract is unchanged) exposing `previous`; the concrete
      `RetainingVersionRegistry` moves current -> previous on record and never
      makes a same-hash redeploy its own rollback target;
  (b) the gate order — `StrategyOrchestrator::rollback` runs EVERY guard before
      the single registry write, in a fixed order: target-hash wire-form
      validation -> lookup -> previous -> exact-target match -> live/confirmation
      -> record; a live-probe failure refuses (`LiveStatusUnavailable`, fail
      closed) and a record failure PROPAGATES (`RegistryFailed` — unlike launch's
      best-effort record, the write IS the rollback);
  (c) NFR-S2 confirmation parity — `RollbackConfirmation` structurally mirrors
      live promotion's `LiveDesignationConfirmation`
      (crates/atp-execution/src/designation.rs): the same two private fields, the
      sole `from_operator` constructor rejecting an empty acknowledgement, no
      `Default`, no public boolean — checked against BOTH sources so the mirror
      cannot silently drift (the "same confirmation control" clause, made
      checkable);
  (d) the operator bin — `orch005_rollback_cli` exposes record/show/rollback over
      a fail-closed, durably-written state snapshot (magic-headed; a tampered or
      foreign file refuses the whole load; scratch write + fsync + atomic
      rename);
  (e) the surface wiring — python/atp_orchestration mounts the CLI
      `strategy rollback` command and the REST lifecycle route's rollback action
      onto the runtime registry, re-checks `request.confirmed` (defense in depth
      under the transport guard), transcribes the operator's confirm act into
      the strategy- and surface-naming acknowledgement, and delegates every
      non-rollback lifecycle action to the honest 501 naming SRS-ORCH-004.

Plus a cargo smoke (default; ``--skip-cargo`` for harnesses that already run
cargo): the crate's orch_5 rollback-contract + CLI fail-closed test suites.

PASS line: ``SRS-ORCH-005 PASS``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _fn_block, _struct_body, _trait_body  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


class RollbackCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise RollbackCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "rollback_contract" not in config:
        fail("architecture metadata is missing rollback_contract")
    return config["rollback_contract"]


def _read(config: dict, key: str, root: Path = ROOT) -> str:
    rel = contract_block(config)[key]
    path = root / rel
    if not path.exists():
        fail(f"source missing: {rel}")
    return path.read_text(encoding="utf-8")


def _ordered(haystack: str, needles: list[str], context: str) -> None:
    """Assert every needle appears, in the given order (static ordering check)."""

    position = 0
    for needle in needles:
        found = haystack.find(needle, position)
        if found < 0:
            fail(f"{context}: `{needle}` missing or out of order")
        position = found + len(needle)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def check_retention_port(config: dict, orch_src: str) -> str:
    if "pub trait RetainedDeployedVersionRegistry: DeployedVersionRegistry" not in orch_src:
        fail(
            "the retention port must be a SUPERTRAIT extension "
            "(`RetainedDeployedVersionRegistry: DeployedVersionRegistry`) so the frozen "
            "SRS-ORCH-004 record/lookup contract is unchanged"
        )
    trait_body = _trait_body(orch_src, "RetainedDeployedVersionRegistry")
    if "fn previous(" not in trait_body:
        fail("RetainedDeployedVersionRegistry must expose `previous` (the SYS-80 read path)")
    struct_body = _struct_body(orch_src, "RetainedVersions")
    for field in ("current: DeployedVersion", "previous: Option<DeployedVersion>"):
        if field not in struct_body:
            fail(f"RetainedVersions must carry `{field}` (the SYS-80 retained pair)")
    if "pub struct RetainingVersionRegistry" not in orch_src:
        fail("the concrete RetainingVersionRegistry (in-memory retention) must exist")
    # (`record` is declared on the trait AND implemented on the concrete registry, so a
    # single-fn block extraction is ambiguous; the same-hash token lives only in the impl.)
    if "existing.current.source_hash == version.source_hash" not in orch_src:
        fail(
            "RetainingVersionRegistry::record must keep retention unchanged on a same-hash "
            "redeploy — a version must never become its own rollback target"
        )
    return (
        "retention port: RetainedDeployedVersionRegistry supertrait (previous) over the frozen "
        "ORCH-004 record/lookup; RetainedVersions{current, previous}; the concrete retaining "
        "registry moves current->previous on record and ignores same-hash redeploys for retention"
    )


def check_rollback_gate_order(config: dict, orch_src: str) -> str:
    gate = _fn_block(orch_src, "rollback")
    if not gate:
        fail("StrategyOrchestrator::rollback missing")
    _ordered(
        gate,
        [
            "target_version_hash.validate()",
            ".lookup(",
            ".previous(",
            "TargetMismatch",
            "current_live()",
            ".record(",
        ],
        "rollback gate order (validate -> lookup -> previous -> target match -> live -> record)",
    )
    for token, why in (
        (
            "LiveStatusUnavailable",
            "a probe failure must refuse (fail closed), never assume not-live",
        ),
        ("MissingConfirmation", "a live rollback without a token must refuse (NFR-S2)"),
        ("ConfirmationMismatch", "a token bound to another strategy must refuse (no replay)"),
        (
            ".map_err(RollbackError::RegistryFailed)?",
            "a record failure must PROPAGATE — the write IS the rollback",
        ),
    ):
        if token not in gate:
            fail(f"rollback gate: `{token}` missing — {why}")
    # The single write comes last: no `.record(` may appear before the live check.
    if gate.find(".record(") < gate.find("current_live()"):
        fail("rollback gate: the registry write must come AFTER the live/confirmation guard")
    return (
        "gate order: rollback validates the target wire form, resolves current + retained "
        "previous, exact-matches the target, enforces the live confirmation (probe failure = "
        "fail closed), and only then performs the single registry write (whose failure "
        "propagates as RegistryFailed)"
    )


def check_confirmation_parity(config: dict, orch_src: str, designation_src: str) -> str:
    """NFR-S2 'same confirmation control as live promotion', made structurally checkable."""

    pairs = (
        ("RollbackConfirmation", orch_src, "atp-orchestrator"),
        ("LiveDesignationConfirmation", designation_src, "atp-execution designation.rs"),
    )
    for type_name, source, where in pairs:
        body = _struct_body(source, type_name)
        if not body:
            fail(f"{type_name} missing from {where}")
        for field in ("strategy_id: StrategyId", "operator_acknowledgement: String"):
            if field not in body:
                fail(f"{type_name} must carry the private field `{field}`")
            if f"pub {field}" in body:
                fail(f"{type_name}.{field} must be PRIVATE (no public constructor bypass)")
        declaration_at = source.find(f"pub struct {type_name}")
        derive_at = source.rfind("#[derive", 0, declaration_at)
        derive_line = source[derive_at : source.find("\n", derive_at)]
        if "Default" in derive_line:
            fail(f"{type_name} must not derive Default (an implicit value is not a confirmation)")
        constructor = source[declaration_at:]
        if "fn from_operator(" not in constructor:
            fail(f"{type_name} must have `from_operator` as its sole constructor")
        if "trim().is_empty()" not in constructor:
            fail(f"{type_name}::from_operator must reject an empty operator acknowledgement")
        for accessor in ("confirmed_strategy", "operator_acknowledgement"):
            if f"pub fn {accessor}(" not in constructor:
                fail(f"{type_name} must expose the `{accessor}` accessor")
    return (
        "NFR-S2 parity: RollbackConfirmation structurally mirrors live promotion's "
        "LiveDesignationConfirmation — the same two private fields, the sole from_operator "
        "constructor rejecting an empty acknowledgement, no Default — checked against BOTH "
        "sources so the mirror cannot drift; distinct types keep a token minted for one "
        "workflow from being replayed on the other"
    )


def check_rollback_cli(config: dict, bin_src: str) -> str:
    for token, why in (
        ('"record" => cmd_record', "the SYS-80 retention write subcommand"),
        ('"show" => cmd_show', "the retained-pair read subcommand"),
        ('"rollback" => cmd_rollback', "the rollback subcommand"),
        ("--acknowledge", "the operator acknowledgement flag (the NFR-S2 control)"),
        ("STATE_MAGIC", "the magic-headed snapshot (foreign files refused before parsing)"),
        ("refusing a foreign/truncated file", "fail-closed load on a wrong magic"),
        ("invalid source hash", "fail-closed load on a tampered hash"),
        ("sync_all", "durable scratch write (fsync)"),
        ("fs::rename", "atomic publish (scratch -> state)"),
        ("--degraded-live-probe", "the degraded-probe simulation (must refuse, fail closed)"),
    ):
        if token not in bin_src:
            fail(f"orch005_rollback_cli: `{token}` missing — {why}")
    return (
        "operator bin: orch005_rollback_cli exposes record/show/rollback over a magic-headed, "
        "fail-closed state snapshot written scratch -> fsync -> atomic rename, with the "
        "--acknowledge confirmation flag and a degraded-probe simulation that refuses"
    )


def check_handler_surface(config: dict, handler_src: str) -> str:
    for token, why in (
        (
            'OperationKey(Surface.CLI, "strategy rollback")',
            "the CLI operation this feature owns",
        ),
        (
            'OperationKey(\n    Surface.REST, "POST /api/v1/strategies/{strategy_id}/lifecycle"\n)',
            "the REST lifecycle operation the rollback action rides on",
        ),
        ("if not request.confirmed:", "defense-in-depth confirmation re-check"),
        (
            "operator confirmed rollback of {strategy_id} via {request.surface.value}",
            "the strategy- and surface-naming audit acknowledgement",
        ),
        (
            'DeferredHandler(\n            owner="SRS-ORCH-004"',
            "non-rollback lifecycle actions keep their honest 501 owner",
        ),
        ("runtime.registry.register(CLI_ROLLBACK_OPERATION", "the CLI registration"),
        (
            "runtime.registry.register(REST_LIFECYCLE_OPERATION",
            "the REST registration",
        ),
    ):
        if token not in handler_src:
            fail(f"atp_orchestration handler: `{token}` missing — {why}")
    return (
        "surface wiring: mount_rollback registers the CLI `strategy rollback` command and the "
        "REST lifecycle route (rollback action only; other actions keep the SRS-ORCH-004 501), "
        "re-checks request.confirmed, and transcribes the operator's confirm act into the "
        "strategy-bound audit acknowledgement"
    )


CHECKS = (
    ("retention_port", check_retention_port, ("orch",)),
    ("rollback_gate_order", check_rollback_gate_order, ("orch",)),
    ("confirmation_parity", check_confirmation_parity, ("orch", "designation")),
    ("rollback_cli", check_rollback_cli, ("bin",)),
    ("handler_surface", check_handler_surface, ("handler",)),
)


def _sources(config: dict, root: Path) -> dict[str, str]:
    return {
        "orch": _read(config, "orchestrator_source", root),
        "designation": _read(config, "designation_source", root),
        "bin": _read(config, "cli_bin_source", root),
        "handler": _read(config, "handler_source", root),
    }


def assert_rollback_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""

    sources = _sources(config, root)
    return [fn(config, *(sources[key] for key in keys)) for _, fn, keys in CHECKS]


def run_cargo_smoke(root: Path = ROOT) -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        fail("cargo not on PATH: cannot run the rollback contract test suites")
    for suite in ("orch_5_rollback_contract", "orch_5_cli_fail_closed"):
        result = subprocess.run(
            [cargo, "test", "-p", "atp-orchestrator", "--test", suite],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            fail(
                f"cargo test -p atp-orchestrator --test {suite} failed:\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
    return "cargo test orch_5_rollback_contract + orch_5_cli_fail_closed passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-cargo",
        action="store_true",
        help="Skip the cargo test smoke (for harnesses that already invoke cargo).",
    )
    args = parser.parse_args()
    try:
        config = load_config()
        evidence = assert_rollback_static(config)
        if not args.skip_cargo:
            evidence.append(run_cargo_smoke())
    except RollbackCheckError as error:
        print(f"SRS-ORCH-005 FAIL: {error}")
        return 1
    print("SRS-ORCH-005 PASS")
    for line in evidence:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
