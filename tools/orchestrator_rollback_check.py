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
      non-rollback lifecycle action to the honest 501 naming SRS-ORCH-004;
  (f) the dashboard arm — the third surface the AC names. `mount_default_dashboard`
      composes `mount_rollback` under the same `ATP_DEPLOYMENT_STATE` knob that
      builds the inventory, and EVERY composition binds a capability probe read
      from the live registry, so a dashboard mounted without the handler reports
      `rollback_available: false` and renders the control inert instead of
      posting into the bare runtime's 501. The control itself is the SAME
      arm-then-confirm affordance as live promotion (mutual exclusion, the
      `confirm` token on the contract route, a target that is the retained
      previous version, and fail-closed success rendering — an unevidenced 2xx
      is never shown as a rollback that happened).

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
        (
            "serves_rollback = True",
            "the ACTION-level capability marker a consumer surface probes (registration on "
            "the lifecycle route is shared with SRS-ORCH-004 and proves nothing about rollback)",
        ),
        (
            'getattr(handler, "serves_rollback", False) is True',
            "the capability probe fails closed: an unmarked or non-True handler is not one",
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


def _py_def_block(src: str, name: str) -> str:
    """The body of a Python ``def name(...)`` — from the def line to the next
    line at the same or lower indentation. (`_fn_block` is the Rust parser; the
    dashboard sources are Python.)"""

    marker = f"def {name}("
    start = src.find(marker)
    if start < 0:
        return ""
    line_start = src.rfind("\n", 0, start) + 1
    indent = len(src[line_start:start])
    lines = src[line_start:].splitlines()
    body = [lines[0]]
    for line in lines[1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        body.append(line)
    return "\n".join(body)


def check_dashboard_capability_authority(config: dict, inventory_src: str) -> str:
    """The mounting runtime — not the caller — answers "is rollback served?".

    ``bind_rollback_probe`` must assign UNCONDITIONALLY. If a constructor-
    supplied callback could survive the bind, a composer could assert the
    capability on a runtime with no rollback handler and hand the operator an
    actionable control that posts into the bare lifecycle route's 501.
    """

    body = _py_def_block(inventory_src, "bind_rollback_probe")
    if not body:
        fail("StrategyInventoryProvider.bind_rollback_probe missing (the capability seam)")
    if "if self._rollback_available is None" in body:
        fail(
            "bind_rollback_probe must not let a constructor-supplied callback survive the "
            "bind — the mounting runtime is authoritative; a caller must not be able to "
            "claim a rollback capability the runtime does not have"
        )
    if "self._rollback_available = probe" not in body:
        fail("bind_rollback_probe must assign the runtime's probe")
    if "return False" not in _py_def_block(inventory_src, "_rollback_capability"):
        fail("_rollback_capability must fail closed (no probe / raising probe -> False)")
    return (
        "capability authority: bind_rollback_probe assigns unconditionally (the mounting "
        "runtime overrides any caller-supplied claim) and the capability fails closed "
        "without a working probe"
    )


def check_dashboard_arm(config: dict, server_src: str, app_src: str) -> str:
    """SYS-80 names THREE surfaces — dashboard, CLI, REST. The CLI and REST arms
    are pinned above; this pins the dashboard arm so it cannot silently regress
    to the read-only surface it used to be.

    Two halves, both required: the production composition must mount the handler
    on the dashboard's own runtime (else the control POSTs into a 501), and the
    control must carry the SAME confirmation affordance live promotion uses
    (NFR-S2 parity) with fail-closed success rendering.
    """

    for token, why in (
        (
            "mount_rollback(runtime, state_path=deployment_state)",
            "the production dashboard composes the ORCH-005 handler on its own runtime",
        ),
        (
            "from atp_orchestration import REST_LIFECYCLE_OPERATION, mount_rollback, rollback_is_served",
            "...and imports both from the owning package rather than reimplementing them",
        ),
        (
            "inventory.bind_rollback_probe(",
            "EVERY composition reports its real rollback capability, not just the default one",
        ),
        (
            "if not runtime.registry.is_registered(REST_LIFECYCLE_OPERATION):",
            "mounting the dashboard never raises on a already-composed lifecycle route; it "
            "skips and lets the capability probe report the truth",
        ),
        (
            "rollback_is_served(runtime)",
            "the capability is the rollback ACTION, not registration on the SHARED lifecycle "
            "route that SRS-ORCH-004's start/stop/restart also binds",
        ),
    ):
        if token not in server_src:
            fail(f"atp_dashboard server: `{token}` missing — {why}")

    for token, why in (
        ('"/lifecycle?confirm=true"', "the operator's confirm act rides the contract route"),
        ('action: "rollback"', "the lifecycle action the handler dispatches on"),
        ("target_version_hash: target", "the rollback names the retained previous version"),
        (
            "previous_version_identifier",
            "the target is read from the inventory's retained previous version",
        ),
        (
            'rollbackAvailable === true && targetHash !== ""',
            "an actionable rollback needs BOTH a served route AND a retained previous version",
        ),
        (
            "rollbackAvailable = data.rollback_available === true",
            "the capability comes from the server's own report, never inferred from row data",
        ),
        ("rb.disabled = true", "a strategy with no previous version presents an INERT control"),
        (
            "restored === target",
            "success is bound to the EXACT target the operator armed, not any restored hash",
        ),
        (
            "ROLLBACK_PRE_WRITE_REFUSALS",
            "only refusals the gate raises BEFORE its single write may re-arm the control; "
            "an allow-list, so an unknown refusal type holds rather than re-arming",
        ),
        (
            "holdRollbackAmbiguous(",
            "an unknown outcome holds controls inert (the server may still be completing an "
            "irreversible rollback; aborting the fetch does not cancel it)",
        ),
        (
            "const ROLLBACK_FETCH_TIMEOUT_MS = 35000",
            "the client deadline must EXCEED the server's 30 s rollback subprocess budget",
        ),
        (
            'body.lifecycle_state === "rolled-back"',
            "fail-closed success: an unevidenced 2xx is never rendered as a rollback",
        ),
        (
            "controlsBusy()",
            "promote and rollback are serialized against each other (one live-state mutation)",
        ),
        (
            "disarmRollback(rollbackArmedId !== null)",
            "arming promote disarms a staged rollback",
        ),
        (
            "disarmPromote(promoteArmedId !== null)",
            "...and arming rollback disarms a staged promote",
        ),
    ):
        if token not in app_src:
            fail(f"dashboard rollback control: `{token}` missing — {why}")

    return (
        "dashboard arm: mount_default_dashboard composes mount_rollback under "
        "ATP_DEPLOYMENT_STATE and EVERY composition reports its real capability from the "
        "live registry, so a dashboard without the handler renders the control inert; the "
        "per-row ROLLBACK control is the same arm-then-confirm affordance as promote-live "
        "(mutually exclusive, inert without a retained previous version, fail-closed "
        "success rendering)"
    )


CHECKS = (
    ("retention_port", check_retention_port, ("orch",)),
    ("rollback_gate_order", check_rollback_gate_order, ("orch",)),
    ("confirmation_parity", check_confirmation_parity, ("orch", "designation")),
    ("rollback_cli", check_rollback_cli, ("bin",)),
    ("handler_surface", check_handler_surface, ("handler",)),
    ("dashboard_arm", check_dashboard_arm, ("dashboard_server", "dashboard_app")),
    (
        "dashboard_capability_authority",
        check_dashboard_capability_authority,
        ("dashboard_inventory",),
    ),
)


def _sources(config: dict, root: Path) -> dict[str, str]:
    return {
        "orch": _read(config, "orchestrator_source", root),
        "designation": _read(config, "designation_source", root),
        "bin": _read(config, "cli_bin_source", root),
        "handler": _read(config, "handler_source", root),
        "dashboard_server": _read(config, "dashboard_server_source", root),
        "dashboard_app": _read(config, "dashboard_app_source", root),
        "dashboard_inventory": _read(config, "dashboard_inventory_source", root),
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
