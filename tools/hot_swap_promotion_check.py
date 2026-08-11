#!/usr/bin/env python3
"""Contract evidence script for feature SRS-RESV-005 (SyRS SYS-49d / AC-14; StRS SN-1.25).

Verifies the Hot-Swap PROMOTION gate declared in
``architecture/runtime_services.json`` (block ``hot_swap_promotion_contract``).

The requirement is an ordering constraint — "promote ... only after successful
demotion" — plus three post-conditions. An ordering constraint is exactly the kind
of rule that a future call site forgets, so most of what this script pins is the
ENCAPSULATION that makes forgetting it a compile error, not the happy path:

  (a) ``DemotionReceipt`` keeps PRIVATE fields, derives neither ``Clone`` nor
      ``Default``, and its sole constructor ``mint`` stays ``pub(crate)``. Any one
      of those relaxing lets a caller forge the proof that a demotion succeeded.
  (b) ``promote_after_demotion`` stays ``pub(crate)`` and takes the receipt BY
      VALUE, so the only public promotion path is ``execute_hot_swap``.
  (c) ``execute_hot_swap`` calls ``resolve_demotion`` and reaches the gate ONLY
      from the minted receipt — the mechanical form of demote-before-promote.
  (d) every ordered guard is present in the gate body; the single
      ``designate(`` write is the only one; the position probe is read BEFORE it;
      and the post-condition re-reads come AFTER it with a rollback on drift.
  (e) the input ports stay READ-ONLY (no mutator can be added to a port the gate
      holds, or the gate could promote through it).
  (f) the two ``compile_fail`` doctests that prove (a) from OUTSIDE the crate are
      still present — they are the actual proof; the greps above only stop the
      proof being deleted.
  (g) the REST surface is declared and bound honestly: ``served_by``, the
      registered route, and a subprocess budget that OUTLASTS the demotion
      timeout it waits on.
  (h) **the stale-deferral collector** — no source, contract block, or docstring
      still claims swap promotion is unbuilt or that ``POST /api/v1/hot-swap`` is
      unbound. Fixing those one at a time provably does not converge
      (docs/playbooks/contract-drift.md r9), so this is written as a check rather
      than as a one-off sweep.

Mirrors the PASS/FAIL output style of ``tools/hot_swap_demotion_check.py``.

Invoke:
    python3 tools/hot_swap_promotion_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
BLOCK = "hot_swap_promotion_contract"


class HotSwapPromotionCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise HotSwapPromotionCheckError(message)


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    block = config.get(BLOCK)
    if not isinstance(block, dict):
        fail(f"architecture/runtime_services.json is missing the `{BLOCK}` block")
    return block


def module_source(config: dict, root: Path = ROOT) -> str:
    path = root / contract_block(config)["module"]["path"]
    if not path.exists():
        fail(f"promotion gate source missing: {path}")
    return path.read_text(encoding="utf-8")


def orchestrator_source(config: dict, root: Path = ROOT) -> str:
    path = root / contract_block(config)["orchestrator_crate"]["path"] / "src" / "lib.rs"
    if not path.exists():
        fail(f"orchestrator crate source missing: {path}")
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Block extractor that understands `pub(crate)`
# --------------------------------------------------------------------------- #
#
# `tools/_rust_parser._fn_block` only matches `pub fn`. The gate here is
# DELIBERATELY `pub(crate) fn`, so a shared helper that silently failed to find it
# would make every guard below vacuous.


def fn_block(source: str, name: str) -> str:
    """Body of ``fn <name>`` at any visibility, brace-balanced."""

    match = re.search(rf"\b(?:pub(?:\([^)]*\))?\s+)?fn\s+{re.escape(name)}\b", source)
    if not match:
        fail(f"the promotion module is missing function `{name}`")
    brace = source.find("{", match.end())
    if brace < 0:
        fail(f"function `{name}` has no body")
    depth, index = 1, brace + 1
    while index < len(source) and depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    return source[brace + 1 : index - 1]


def struct_body(source: str, name: str) -> str:
    match = re.search(rf"\bpub\s+struct\s+{re.escape(name)}\b[^\{{]*\{{", source)
    if not match:
        fail(f"the promotion module is missing public struct `{name}`")
    depth, index = 1, match.end()
    while index < len(source) and depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    return source[match.end() : index - 1]


def derives_above(source: str, name: str) -> str:
    """The ``#[derive(...)]`` attribute immediately preceding ``pub struct <name>``."""

    match = re.search(rf"((?:#\[[^\]]*\]\s*)*)pub\s+struct\s+{re.escape(name)}\b", source)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------- #
# (a) The receipt cannot be forged
# --------------------------------------------------------------------------- #


def check_receipt_encapsulation(config: dict, module_src: str) -> str:
    spec = contract_block(config)["receipt"]
    name = spec["type"]

    body = struct_body(module_src, name)
    public_fields = re.findall(r"^\s*pub\s+(\w+)\s*:", body, flags=re.MULTILINE)
    if public_fields:
        fail(
            f"{name} exposes public field(s) {public_fields} — a caller could then build "
            "the demotion proof with a struct literal, and 'promote only after successful "
            "demotion' would be satisfiable without any demotion"
        )
    for declared in spec["private_fields"]:
        if not re.search(rf"^\s*{re.escape(declared)}\s*:", body, flags=re.MULTILINE):
            fail(f"{name} is missing the declared field `{declared}`")

    derives = derives_above(module_src, name)
    for forbidden in spec["forbidden_derives"]:
        if re.search(rf"\b{re.escape(forbidden)}\b", derives):
            fail(f"{name} derives `{forbidden}` — {spec['note']}")

    constructor, visibility = spec["constructor"], spec["constructor_visibility"]
    if not re.search(rf"{re.escape(visibility)}\s+fn\s+{re.escape(constructor)}\b", module_src):
        fail(
            f"{name}::{constructor} is not `{visibility}` — the sole minting path must not be "
            "callable from outside the crate"
        )
    mint = fn_block(module_src, constructor)
    if "promotion_allowed" not in mint:
        fail(
            f"{name}::{constructor} does not consult `promotion_allowed` — an acceptance that "
            "does not allow promotion must not mint a receipt (fail closed)"
        )
    return (
        f"{name}: private fields {spec['private_fields']}, derives neither "
        f"{spec['forbidden_derives']}, sole constructor `{constructor}` is {visibility} and "
        "refuses a promotion_allowed:false acceptance"
    )


# --------------------------------------------------------------------------- #
# (b)+(c) The only public promotion path runs the demotion first
# --------------------------------------------------------------------------- #


def check_entry_point_sequencing(config: dict, module_src: str) -> str:
    block = contract_block(config)
    entry = block["entry_point"]["method"]
    gate = block["gate"]["method"]
    gate_visibility = block["gate"]["visibility"]
    receipt = block["receipt"]["type"]
    constructor = block["receipt"]["constructor"]

    if not re.search(rf"{re.escape(gate_visibility)}\s+fn\s+{re.escape(gate)}\b", module_src):
        fail(
            f"`{gate}` is not `{gate_visibility}` — an externally-callable gate would let a "
            f"caller skip `{entry}` and with it the demotion"
        )
    if not re.search(rf"\bpub\s+fn\s+{re.escape(entry)}\b", module_src):
        fail(f"`{entry}` must be the public entry point")

    body = fn_block(module_src, entry)
    if "resolve_demotion(" not in body:
        fail(
            f"`{entry}` does not call `resolve_demotion(` — the promotion path must run the "
            "SRS-RESV-004 demotion gate itself, not trust a caller that says it did"
        )
    if f"{receipt}::{constructor}(" not in body:
        fail(f"`{entry}` does not mint the receipt via `{receipt}::{constructor}(`")
    if f"self.{gate}(" not in body:
        fail(f"`{entry}` does not call the gate `{gate}`")

    # The gate call must be textually INSIDE the arm that got a receipt. Both the
    # demotion Ok and the mint Some must precede it, or a refactor could route an
    # Err arm straight into the gate.
    resolve_at = body.index("resolve_demotion(")
    mint_at = body.index(f"{receipt}::{constructor}(")
    gate_at = body.index(f"self.{gate}(")
    if not resolve_at < mint_at < gate_at:
        fail(
            f"`{entry}` does not order resolve_demotion -> {constructor} -> {gate}; the gate "
            "must be reachable only from a minted receipt"
        )

    # The gate takes the receipt BY VALUE (no `&`), so it is consumed.
    signature = module_src[module_src.index(f"fn {gate}") :][:400]
    if re.search(r"receipt\s*:\s*&", signature):
        fail(
            f"`{gate}` takes the receipt by reference — a by-value receipt is what stops one "
            "demotion authorizing two promotions"
        )
    return (
        f"`{entry}` is the sole public path: resolve_demotion -> {receipt}::{constructor} -> "
        f"`{gate}` ({gate_visibility}, receipt by value)"
    )


# --------------------------------------------------------------------------- #
# (d) The ordered guards, the single write, and the rollback
# --------------------------------------------------------------------------- #


def promotion_path_text(module_src: str, gate: str) -> str:
    """The gate body PLUS the bodies of the module-local helpers it calls.

    Some guards are constructed inside small named helpers (``read_paper_history``,
    ``read_deployed_version``) rather than inline. Scanning only the gate body would
    report them missing; scanning the whole module would pass even for a guard
    sitting in dead code. Following one level of calls FROM the gate is the faithful
    reading: it covers exactly the text a promotion actually executes.
    """

    body = fn_block(module_src, gate)
    defined = set(re.findall(r"\bfn\s+(\w+)\s*[<(]", module_src))
    called = {name for name in re.findall(r"\b(\w+)\s*\(", body) if name in defined}
    parts = [body]
    for helper in sorted(called - {gate}):
        parts.append(fn_block(module_src, helper))
    return "\n".join(parts)


def check_ordered_guards(config: dict, module_src: str) -> str:
    block = contract_block(config)
    gate = block["gate"]["method"]
    body = fn_block(module_src, gate)
    reachable = promotion_path_text(module_src, gate)

    missing = [g for g in block["ordered_guards"] if g not in reachable]
    if missing:
        fail(
            f"`{gate}` is missing guard(s) {missing} — each one is a way a promotion could "
            "proceed on an unverified precondition. (Searched the gate body and the bodies "
            "of the module-local helpers it calls; a guard defined but never reached from "
            "the gate does not count.)"
        )

    writes = body.count(".designate(")
    if writes != 1:
        fail(
            f"`{gate}` performs {writes} `designate(` calls — there must be exactly one live "
            "designation write, so a refused promotion has no partial effect"
        )

    probe_at = body.index("open_positions()")
    write_at = body.index(".designate(")
    if probe_at > write_at:
        fail(
            f"`{gate}` probes open positions AFTER designating the candidate live — the "
            "flat-start check must gate the write, not follow it"
        )

    # The post-condition re-reads must come after the write, and drift must roll back.
    if "rollback(" not in body:
        fail(
            f"`{gate}` never calls `rollback(` — a post-condition failure after the write must "
            "undo the designation, or a refused promotion leaves the candidate live"
        )
    if body.index("rollback(") < write_at:
        fail(f"`{gate}` rolls back before the designation write")
    rollback_body = fn_block(module_src, "rollback")
    if ".demote(" not in rollback_body:
        fail("`rollback` does not demote — it must actually release the designation")
    return (
        f"`{gate}`: {len(block['ordered_guards'])} ordered guards present, exactly one "
        "`designate(` write, positions probed before it, drift rolled back after it"
    )


# --------------------------------------------------------------------------- #
# (e) The ports stay read-only
# --------------------------------------------------------------------------- #


def check_ports_read_only(config: dict, module_src: str) -> str:
    forbidden = ("promote", "designate", "demote", "set_live", "go_live", "record_promotion")
    names = []
    for port in contract_block(config)["ports"]:
        trait, method = port["trait"], port["method"]
        match = re.search(rf"\bpub\s+trait\s+{re.escape(trait)}\b[^\{{]*\{{", module_src)
        if not match:
            fail(f"the promotion module is missing port trait `{trait}`")
        depth, index = 1, match.end()
        while index < len(module_src) and depth:
            if module_src[index] == "{":
                depth += 1
            elif module_src[index] == "}":
                depth -= 1
            index += 1
        body = module_src[match.end() : index - 1]
        declared = re.findall(r"\bfn\s+(\w+)", body)
        if method not in declared:
            fail(f"port `{trait}` does not declare `{method}`")
        if port.get("read_only") and len(declared) != 1:
            fail(
                f"read-only port `{trait}` declares {declared} — it must expose exactly one "
                "observation method, or the gate could act through it instead of on it"
            )
        leaked = [fn for fn in declared if any(token in fn for token in forbidden)]
        if leaked:
            fail(f"port `{trait}` declares mutator-shaped method(s) {leaked}")
        names.append(trait)
    return f"ports {names} are observation-only (no promote/designate/demote method)"


# --------------------------------------------------------------------------- #
# (f) The compile_fail doctests that carry the actual proof
# --------------------------------------------------------------------------- #


def check_compile_fail_doctests(config: dict, module_src: str) -> str:
    receipt = contract_block(config)["receipt"]["type"]
    blocks = re.findall(r"```compile_fail(.*?)```", module_src, flags=re.DOTALL)
    if len(blocks) < 2:
        fail(
            f"expected at least 2 `compile_fail` doctests proving {receipt} is unforgeable "
            f"from outside the crate, found {len(blocks)}. The greps in this script only stop "
            "the proof being deleted; the doctests ARE the proof"
        )
    joined = "\n".join(blocks)
    if f"{receipt} {{" not in joined:
        fail("no compile_fail doctest attempts the struct-literal forge")
    if f"{receipt}::mint" not in joined:
        fail("no compile_fail doctest attempts to name the pub(crate) constructor")
    return (
        f"{len(blocks)} compile_fail doctests: struct-literal forge and `{receipt}::mint` are "
        "both proven unreachable from an external crate"
    )


# --------------------------------------------------------------------------- #
# (g) The REST surface is declared and bound honestly
# --------------------------------------------------------------------------- #


def check_rest_surface(config: dict, root: Path = ROOT) -> str:
    spec = contract_block(config)["rest_surface"]

    routes_src = (root / "python" / "atp_api" / "routes.py").read_text(encoding="utf-8")
    if f'served_by="{spec["served_by"]}"' not in routes_src:
        fail(
            f'python/atp_api/routes.py does not declare `served_by="{spec["served_by"]}"` — a '
            "route with a live handler must not keep the 'Contract only' placeholder"
        )

    handler_path = root / spec["handler"].split("::")[0]
    if not handler_path.exists():
        fail(f"REST handler module missing: {handler_path}")
    handler_src = handler_path.read_text(encoding="utf-8")
    if f'"{spec["route"]}"' not in handler_src:
        fail(f"{handler_path.name} does not register the declared route `{spec['route']}`")

    match = re.search(r"_DEFAULT_TIMEOUT_S\s*=\s*([0-9.]+)", handler_src)
    if not match:
        fail(f"{handler_path.name} declares no subprocess timeout")
    budget = float(match.group(1))
    declared = float(spec["subprocess_timeout_seconds"])
    if budget != declared:
        fail(f"{handler_path.name} timeout {budget}s disagrees with the contract's {declared}s")
    if budget <= 60:
        fail(
            f"the subprocess budget ({budget}s) does not outlast the 60s SYS-49b demotion "
            "timeout it waits on — a slow but SUCCESSFUL swap would return as ambiguous"
        )

    for vocabulary in ("demotion_state_vocabulary", "promotion_state_vocabulary"):
        for value in spec[vocabulary]:
            if f'"{value}"' not in handler_src:
                fail(f"{handler_path.name} never emits declared {vocabulary} value `{value}`")
    return (
        f"REST: `{spec['route']}` served_by={spec['served_by']}, closed vocabularies emitted, "
        f"subprocess budget {budget}s > 60s demotion timeout"
    )


def check_safety_input_tier(config: dict, root: Path = ROOT) -> str:
    """The two SAFETY facts must never be servable from a silent default.

    Round-1 adversarial review [critical]: the served route could report PROMOTED
    without proving the account was flat, because the CLI defaulted `--positions`
    to `flat`. The fix is opt-in at BOTH layers, so this guard checks both — a
    single-layer fix would leave the other able to reintroduce it.
    """

    block = contract_block(config)
    tier = block["rest_surface"]["safety_input_tier"]

    cli_path = root / "crates/atp-orchestrator/src/bin/resv005_hot_swap_promote_cli.rs"
    cli_src = cli_path.read_text(encoding="utf-8")
    if tier["cli_flag"] not in cli_src:
        fail(f"the CLI does not declare `{tier['cli_flag']}`")
    swap = fn_block(cli_src, "cmd_swap")
    if "allow_fixture_safety_inputs" not in swap:
        fail(
            "`cmd_swap` never consults the fixture-tier opt-in — fixture safety facts "
            "would again be usable without a caller saying so"
        )
    # The refusal must precede the state read, so a refused drill leaves nothing.
    refusal_at = swap.index("allow_fixture_safety_inputs")
    load_at = swap.index("load_designation(")
    if refusal_at > load_at:
        fail(
            "the fixture-tier refusal runs AFTER the designation is loaded — it must "
            "gate the whole sequence so a refused drill touches nothing"
        )

    # No SUCCESS DEFAULTS: each fixture fact must be individually required. A
    # default here decides the requirement's own facts by omission.
    for fact in tier["required_fixture_facts"]:
        parser = {
            "--liquidation": "parse_liquidation",
            "--positions": "parse_positions",
            "--deployed-version": "parse_version",
        }[fact]
        body = fn_block(cli_src, parser)
        if "unwrap_or(" in body:
            fail(
                f"`{parser}` defaults a missing `{fact}` — declaring the fixture TIER is "
                "not the same as stating the fixture FACTS, and a default decides one by "
                "omission"
            )
        if "is required" not in body:
            fail(f"`{parser}` does not require `{fact}` explicitly")

    handler_path = root / block["rest_surface"]["handler"].split("::")[0]
    handler_src = handler_path.read_text(encoding="utf-8")
    for fact in tier["required_fixture_facts"]:
        key = fact.removeprefix("--").replace("-", "_")
        if f'"{key}"' not in handler_src:
            fail(
                f"the handler never states `{fact}` — the binary would refuse, or worse, default it"
            )
    if tier["declaration"] not in handler_src:
        fail(f"{handler_path.name} does not accept a `{tier['declaration']}` declaration")
    if tier["refusal_type"] not in handler_src:
        fail(f"{handler_path.name} never raises `{tier['refusal_type']}`")
    for owner in tier["owners"]:
        if owner not in handler_src:
            fail(f"{handler_path.name} does not name deferred owner `{owner}` in its refusal")
    return (
        f"safety-input tier: opt-in at BOTH layers (`{tier['cli_flag']}` before the state "
        f"read; `{tier['declaration']}` or a {tier['refusal_type']} 501 naming "
        f"{tier['owners']})"
    )


def check_swap_is_serialized(config: dict, root: Path = ROOT) -> str:
    """The read-execute-write sequence must be under a lock, held for its lifetime."""

    spec = contract_block(config)["rest_surface"]["concurrency"]
    cli_path = root / "crates/atp-orchestrator/src/bin/resv005_hot_swap_promote_cli.rs"
    swap = fn_block(cli_path.read_text(encoding="utf-8"), "cmd_swap")

    if spec["guard"] not in swap:
        fail(
            f"`cmd_swap` does not acquire `{spec['guard']}` — two concurrent swaps would "
            "read the same live strategy and both promote"
        )
    # LIFETIME, not presence: a `let _ = acquire(...)` drops the guard immediately and
    # reopens the race while still containing the token this check greps for.
    if "let _swap_guard" not in swap:
        fail(
            "the swap lock is not bound to a named guard held for the critical section. "
            "`let _ = acquire(...)` drops it immediately and reopens the race while still "
            "mentioning the acquire call"
        )
    guard_at = swap.index("let _swap_guard")
    load_at = swap.index("load_designation(")
    if guard_at > load_at:
        fail("the swap lock is acquired AFTER the designation is loaded — the read is unprotected")
    return f"swap serialized: `{spec['guard']}` bound for the lifetime of {spec['scope']}"


# --------------------------------------------------------------------------- #
# (h) The stale-deferral collector
# --------------------------------------------------------------------------- #

#: Files that describe this surface and could go stale. Kept explicit rather than a
#: whole-tree walk so the check names the file it means.
_PROSE_FILES = (
    "architecture/runtime_services.json",
    "python/atp_orchestration/hot_swap_triggers.py",
    "python/atp_orchestration/hot_swap_execution.py",
    "python/atp_orchestration/__init__.py",
    "python/atp_api/routes.py",
    "python/atp_api/openapi.json",
    "python/atp_hotswap/__init__.py",
    "crates/atp-orchestrator/src/lib.rs",
    "crates/atp-orchestrator/src/hot_swap_promotion.rs",
)

#: Each entry is (regex, why-it-is-now-false). These are claims that STOPPED being
#: true the moment the promotion gate and its REST binding landed.
_STALE_CLAIMS = (
    (
        r"swap execution is unbuilt",
        "swap execution is built (SRS-RESV-005 execute_hot_swap + POST /api/v1/hot-swap)",
    ),
    (
        r"unbuilt\s+``?SRS-RESV-004``?\s*/\s*``?005``?",
        "SRS-RESV-005 is built; only its cross-attempt lockout is deferred to SRS-RESV-004",
    ),
    (
        r"SRS-RESV-005[^.\"]{0,40}(?:neither of which is built|is not built|unbuilt)",
        "SRS-RESV-005 is built",
    ),
    (
        r"flat-start promotion is SRS-RESV-005;",
        "that phrasing implies it is still pending; it must name the built contract block",
    ),
    (
        r"POST /api/v1/hot-swap — swap EXECUTION — is deliberately NOT bound and",
        "the route IS bound by SRS-RESV-005's mount_hot_swap_execution",
    ),
    # SRS-RESV-004 landed its durable lockout mid-flight, which turned this
    # feature's own deferral prose false. Kept as a pattern, not a one-off sweep:
    # the same sentence lived in three places and would have gone stale again.
    (
        r"does not persist a demotion-pending lock",
        "SRS-RESV-004's DemotionPendingLock is built, and resolve_demotion consults "
        "it before its probe, so the promotion path inherits the block",
    ),
    (
        r"not yet enforced across a \*{0,2}later retry",
        "the cross-attempt lockout is enforced by SRS-RESV-004's store",
    ),
)


def check_no_stale_deferral(root: Path = ROOT) -> str:
    findings: list[str] = []
    for relative in _PROSE_FILES:
        path = root / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, why in _STALE_CLAIMS:
            for match in re.finditer(pattern, text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: {match.group(0)!r} — {why}")
    if findings:
        fail(
            "stale deferral claim(s) survived the sweep:\n  " + "\n  ".join(findings) + "\n"
            "A claim that outruns the code is its own defect: it sends the next session to "
            "rebuild something that already exists."
        )
    return (
        f"stale-deferral collector: {len(_STALE_CLAIMS)} claim patterns, "
        f"{len(_PROSE_FILES)} files, 0 matches"
    )


# --------------------------------------------------------------------------- #
# cargo smoke
# --------------------------------------------------------------------------- #


def check_cargo_smoke(config: dict, *, require_cargo: bool = False) -> str:
    crate = contract_block(config)["orchestrator_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail("cargo is not on PATH and --require-cargo was given")
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
    for args in (["--test", "resv_5_hot_swap_promotion"], ["--doc"]):
        result = subprocess.run(
            [cargo, "test", "-p", crate, *args, "--quiet"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            fail(
                f"cargo test -p {crate} {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
            )
    return f"cargo test -p {crate} --test resv_5_hot_swap_promotion + --doc: PASS"


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("receipt_encapsulation", check_receipt_encapsulation),
    ("entry_point_sequencing", check_entry_point_sequencing),
    ("ordered_guards", check_ordered_guards),
    ("ports_read_only", check_ports_read_only),
    ("compile_fail_doctests", check_compile_fail_doctests),
)


def _run_static(config: dict, module_src: str, root: Path = ROOT) -> list[str]:
    evidence = [check(config, module_src) for _, check in _STATIC_CHECKS]
    evidence.append(check_rest_surface(config, root))
    evidence.append(check_safety_input_tier(config, root))
    evidence.append(check_swap_is_serialized(config, root))
    evidence.append(check_no_stale_deferral(root))
    return evidence


def assert_hot_swap_promotion_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""

    return _run_static(config, module_source(config, root), root)


def run_checks(*, require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = _run_static(config, module_source(config))
    evidence.append(check_cargo_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-RESV-005 contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="fail instead of skipping when cargo is not on PATH",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except HotSwapPromotionCheckError as error:
        print(f"SRS-RESV-005 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-RESV-005 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
