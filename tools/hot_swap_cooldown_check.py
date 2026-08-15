#!/usr/bin/env python3
"""Contract evidence script for feature SRS-RESV-006.

Verifies that the Hot-Swap cool-down window declared in
``architecture/runtime_services.json`` (block ``hot_swap_cooldown_contract``) is
actually enforced by ``crates/atp-orchestrator``.

SRS-RESV-006 traces SyRS SYS-49e (StRS SN-1.25). The contract guarantees:

  (a) ``CooldownState`` declares the four-state taxonomy and maps each variant
      1:1 to its UPPER_SNAKE wire string.
  (b) ``CooldownPeriodDays::new`` refuses 0 and anything past
      ``COOLDOWN_DAYS_MAX``, and the SYS-49e default is 7 calendar days of
      exactly 86_400 seconds.
  (c) ``ManualCooldownAcknowledgement`` declares both variants and derives NO
      ``Default`` — a caller must state which one it means.
  (d) ``classify`` uses ``saturating_sub`` and ``checked_add``, and contains no
      bare ``now_seconds -``. This is the whole backwards-clock/overflow
      hardening: a wrapping subtraction reads an NTP step back as ~5.8e11 years
      elapsed and silently retires a live safety window.
  (e) ``proven_clear`` matches exactly ``NeverSwapped | Expired`` and never names
      ``Unknown``. Adding ``Unknown`` there IS the fail-open.
  (f) **The anti-bypass check, in two halves.** A CLOSED set checked by name —
      both SRS-RESV-003 trigger arms (``evaluate_automatic_triggers`` /
      ``request_manual_promotion``) consult ``proven_clear`` — and an OPEN set
      DISCOVERED from source: every ``pub fn`` in the orchestrator crate whose body
      reaches a demotion-side side effect must consult it too, with reasoned
      exemptions declared in ``guard.ungated_swap_paths``.

      The second half exists because the first was not enough. This check's first
      version named exactly those two methods and asserted nothing about anything
      else, and adversarial review r2 found ``execute_hot_swap`` demoting and
      promoting with no cool-down at all (``cooldown-execution-bypass``): the two
      trigger arms only MINT a proposal, and nothing in the type graph requires a
      swap to have come from one. A checklist of known arms cannot catch the arm
      nobody added to it, so the arms are now enumerated from the code.

      ``TriggerEvaluation`` / ``ManualPromotionError`` additionally carry the states
      that make a suppressed pass distinguishable from a healthy quiet one.
  (g) the durable store declares its magic + version marker and the four
      durability tokens, and ``record_completion`` holds the exclusive guard and
      keeps the newer completion.
  (h) **The fabrication + clock guard.** Both operator subcommands obtain their
      state from ``cooldown_store::resolve(`` and construct no ``CooldownState``
      literal of their own, and the trigger CLI reads a real clock rather than
      the frozen constant it used to hardcode.
  (i) the persisted entity is registered in ``crates/atp-data``'s schema
      registry with the same id, magic and marker.

Static-only (no cargo): the behavioural post-conditions are anchored by
``crates/atp-orchestrator/tests/resv_6_*.rs`` and ``tests/domain/``. Reached
through ``tools/architecture_check.py``, so it runs in both ``ci.yml`` and
``tools/run_ci_locally.sh`` via the aggregated ``architecture`` step.

Invoke:
    python3 tools/hot_swap_cooldown_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"
CONTRACT_KEY = "hot_swap_cooldown_contract"


class HotSwapCooldownCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise HotSwapCooldownCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if CONTRACT_KEY not in config:
        fail(f"architecture metadata is missing {CONTRACT_KEY}")
    return config[CONTRACT_KEY]


def _read(root: Path, relative: str) -> str:
    path = root / relative
    if not path.exists():
        fail(f"source missing: {relative}")
    return path.read_text(encoding="utf-8")


def _strip_comments(source: str) -> str:
    """Drop `//`-comments so a token scan reads CODE, not prose.

    Found the hard way, by this very script: the first version rejected
    ``cooldown.rs`` because ``classify``'s comment *explains* why a bare
    ``now_seconds - started`` would be wrong. A checker that cannot tell the
    warning from the defect fires on documentation and — worse in the other
    direction — could be silenced by rewording a comment. Block comments are not
    stripped: nothing here uses them, and a half-correct stripper is its own trap.
    """
    return re.sub(r"//[^\n]*", "", source)


def _any_fn_block(source: str, fn_name: str) -> str:
    """Body of ``fn <name>``, whether or not it is ``pub``.

    ``_rust_parser._fn_block`` requires ``pub fn``; several of the subjects here
    (the CLI subcommand handlers) are deliberately private.
    """
    match = re.search(rf"\bfn\s+{re.escape(fn_name)}\b[^{{]*{{", source)
    if not match:
        fail(f"Rust source is missing function `{fn_name}`")
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


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_state_enum(config: dict, cooldown_src: str) -> str:
    spec = contract_block(config)["state_enum"]
    body = _enum_body(cooldown_src, spec["enum"])
    for variant in spec["variants"]:
        if not re.search(rf"\b{re.escape(variant)}\b", body):
            fail(f"{spec['enum']} is missing variant `{variant}`")

    as_str = _strip_comments(_fn_block(cooldown_src, "as_str"))
    seen: dict[str, str] = {}
    for variant, wire in spec["wire_strings"].items():
        if not re.search(rf"Self::{re.escape(variant)}\b[^=]*=>\s*\"{re.escape(wire)}\"", as_str):
            fail(f'{spec["enum"]}::as_str does not map `{variant}` to "{wire}"')
        if wire in seen:
            fail(f"wire string {wire!r} is claimed by both {seen[wire]} and {variant}")
        seen[wire] = variant
    return (
        f"{spec['enum']} declares {len(spec['variants'])} states "
        f"({', '.join(spec['variants'])}) and maps each 1:1 to its wire string"
    )


def check_period_bounds(config: dict, cooldown_src: str) -> str:
    spec = contract_block(config)["period"]
    if spec.get("zero_is_disabled") is not False:
        fail("the contract must declare zero_is_disabled:false — 0 is a refused period")

    new_body = _strip_comments(_fn_block(cooldown_src, "new"))
    if "days == 0" not in new_body:
        fail(
            "CooldownPeriodDays::new must refuse 0 explicitly; a zero-length window "
            "silently defeats SYS-49e while looking like configuration"
        )
    if "COOLDOWN_DAYS_MAX" not in new_body:
        fail("CooldownPeriodDays::new must bound the period by COOLDOWN_DAYS_MAX")

    default_days = spec["default_days"]
    if not re.search(
        rf"pub const COOLDOWN_DAYS_DEFAULT:\s*u32\s*=\s*{default_days}\s*;", cooldown_src
    ):
        fail(f"COOLDOWN_DAYS_DEFAULT must be declared as {default_days}")
    day_seconds = spec["calendar_day_seconds"]
    if not re.search(
        rf"pub const SECONDS_PER_CALENDAR_DAY:\s*u64\s*=\s*{day_seconds:_}\s*;", cooldown_src
    ):
        fail(f"SECONDS_PER_CALENDAR_DAY must be declared as {day_seconds}")
    return (
        f"CooldownPeriodDays refuses 0 and > COOLDOWN_DAYS_MAX; the SYS-49e default is "
        f"{default_days} calendar days of {day_seconds}s"
    )


def check_acknowledgement(config: dict, cooldown_src: str) -> str:
    spec = contract_block(config)["acknowledgement"]
    body = _enum_body(cooldown_src, spec["enum"])
    for variant in spec["variants"]:
        if not re.search(rf"\b{re.escape(variant)}\b", body):
            fail(f"{spec['enum']} is missing variant `{variant}`")

    # The derive list sits on the line(s) immediately above the enum.
    match = re.search(rf"((?:#\[[^\]]*\]\s*)*)pub enum {re.escape(spec['enum'])}\b", cooldown_src)
    derives = match.group(1) if match else ""
    if spec.get("derives_default") is not False:
        fail("the contract must declare derives_default:false for the acknowledgement")
    if re.search(r"derive\([^)]*\bDefault\b", derives):
        fail(
            f"{spec['enum']} must NOT derive Default: there is no sensible default "
            "acknowledgement, and one would silently pick a side for every caller that "
            "forgot to state which it meant"
        )
    return f"{spec['enum']} declares both variants and derives no Default"


def check_classify_arithmetic(config: dict, cooldown_src: str) -> str:
    guard = contract_block(config)["guard"]
    body = _strip_comments(_any_fn_block(cooldown_src, guard["classifier"]))

    # Anchored on the SPECIFIC expressions, not on the bare tokens. The first version of
    # this check asserted only that `saturating_sub` appeared SOMEWHERE in `classify`, and
    # a mutation to `now_seconds.wrapping_sub(started)` passed it clean: the unrelated
    # `expires.saturating_sub(now_seconds)` two lines down kept the token present. A guard
    # satisfied by a different occurrence of its own token is not a guard
    # (test-integrity rule 27 — anchor on the span, not on a token the file repeats).
    if not re.search(rf"now_seconds\.{re.escape(guard['backwards_clock_token'])}\(", body):
        fail(
            f"`{guard['classifier']}` must compute elapsed time as "
            f"`now_seconds.{guard['backwards_clock_token']}(...)`: a wrapping "
            "`now - started` reads a backwards NTP step as ~5.8e11 years elapsed, so the "
            "window reports EXPIRED at exactly the moment the clock stopped being trustworthy"
        )
    if not re.search(rf"\.{re.escape(guard['overflow_token'])}\(", body):
        fail(
            f"`{guard['classifier']}` must compute the expiry with "
            f"`{guard['overflow_token']}`: a saturating expiry renders as a window that "
            "never ends, with nothing to tell an operator why the automatic triggers went quiet"
        )
    for banned in ("wrapping_sub", "unchecked_sub", "wrapping_add", "saturating_add"):
        if re.search(rf"now_seconds\.{banned}\(|started\.{banned}\(", body):
            fail(
                f"`{guard['classifier']}` uses `{banned}` on a window boundary; that is the "
                "arithmetic this check exists to forbid"
            )
    if re.search(r"now_seconds\s*-\s*(?!=)", body):
        fail(
            f"`{guard['classifier']}` contains a bare `now_seconds -` subtraction; use "
            f"{guard['backwards_clock_token']} so a backwards clock cannot retire the window"
        )
    return (
        f"`{guard['classifier']}` computes elapsed with "
        f"now_seconds.{guard['backwards_clock_token']}( and the expiry with "
        f"{guard['overflow_token']}, and uses no wrapping/unchecked boundary arithmetic"
    )


def check_clear_predicate(config: dict, cooldown_src: str) -> str:
    block = contract_block(config)
    guard = block["guard"]
    clear = block["state_enum"]["clear_variants"]
    body = _strip_comments(_fn_block(cooldown_src, guard["clear_predicate"]))
    for variant in clear:
        if not re.search(rf"Self::{re.escape(variant)}\b", body):
            fail(f"`{guard['clear_predicate']}` must treat `{variant}` as clear")
    for variant in block["state_enum"]["variants"]:
        if variant in clear:
            continue
        if re.search(rf"Self::{re.escape(variant)}\b", body):
            fail(
                f"`{guard['clear_predicate']}` names `{variant}`, which is NOT a clear "
                "state. An unreadable or open window that read as clear is the fail-open "
                "this feature exists to prevent (CLAUDE.md rule 3)"
            )
    return (
        f"`{guard['clear_predicate']}` is clear for exactly {', '.join(clear)} and never "
        "for Unknown"
    )


def _struct_body_named(source: str, name: str) -> str:
    """Body of `pub struct <name>`, tolerating generics and a `where` clause."""
    match = re.search(rf"\bpub\s+struct\s+{re.escape(name)}\b[^{{]*{{", source)
    if not match:
        fail(f"the promotion module has no `pub struct {name}`")
    start, depth, index = match.end(), 1, match.end()
    while index < len(source) and depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    return source[start : index - 1]


def check_trigger_arms_are_gated(config: dict, orchestrator_src: str) -> str:
    """The CLOSED half of the anti-bypass rule: the two trigger arms, by name.

    These mint a proposal and never touch a demotion-side port, so the source scan
    in :func:`check_every_swap_path_is_gated` does not see them — correctly, since
    they are a different class. They are a finite, enumerable set (exactly two, both
    on ``StrategyOrchestrator``), so they are checked by name here.

    A gate enforced on one arm leaves the other able to fire a swap trigger inside a
    cool-down. Enumerating the arms is the rule RESV-003 r2 and EXE-003 r3/r4 both
    paid for (adversarial-precheck rule 1).
    """
    guard = contract_block(config)["guard"]
    predicate = guard["clear_predicate"]
    for method in (guard["evaluate_method"], guard["manual_method"]):
        body = _strip_comments(_fn_block(orchestrator_src, method))
        if predicate not in body:
            fail(
                f"`{method}` does not consult `{predicate}`; a SYS-49e window it never "
                "reads is a window it cannot enforce"
            )
    return f"both {guard['evaluate_method']} and {guard['manual_method']} consult `{predicate}`"


def check_the_window_is_committed_after_the_publish(config: dict, root: Path) -> str:
    """The window opens only once the swap is DURABLE (adversarial review r6).

    ``execute_hot_swap`` designates the candidate live in memory; the CLI publishes
    that durably afterwards. A window recorded inside the gate would survive a
    publish that failed before its rename — seven days of suppressed automatic
    triggers for a swap the durable authority never accepted.

    Two halves, both checked here because either alone is satisfiable while the
    defect is live: the gate must NOT record (the write must not appear in
    ``execute_hot_swap``'s body), and the CLI must redeem the token AFTER its
    ``save_designation``, not before.
    """
    guard = contract_block(config)["guard"]
    commit = guard["commit_method"]
    entry = guard["execute_method"]
    module = _strip_comments(_read(root, guard["promotion_module"]))

    entry_body = _any_fn_block(module, entry)
    # The gate must READ its own window, not take one. `CooldownState` is a public
    # enum, so a caller-supplied proof is a forgeable proof: an external caller could
    # hand in `NeverSwapped` and execute straight through an active window. Pinning
    # the CLIs to `resolve(` is not a property of the API (adversarial review r10).
    resolver = guard["window_resolver"]
    if f"{resolver}(" not in entry_body:
        fail(
            f"`{entry}` does not call `{resolver}(` — it must read the window from the "
            "store at execution time rather than accept one from its caller, which any "
            "external caller could forge"
        )
    control = _struct_body_named(module, "CooldownControl")
    if re.search(r"state\s*:", control):
        fail(
            "`CooldownControl` carries a caller-supplied `state` — that is the forgeable "
            "proof r10 removed; the window comes from the port"
        )
    if "record_swap_completion(" in entry_body:
        fail(
            f"`{entry}` records the swap completion itself; the window must be minted "
            f"as a {guard['pending_window_token']} and redeemed by the caller AFTER the "
            "durable publish, or a publish that fails before its rename leaves a window "
            "open for a swap that never became durable"
        )

    # ONE call site, and it is the redeem method. The mint's location inside the
    # module is incidental (the gate constructs the acceptance that carries it); what
    # must not drift is where the WRITE happens.
    writers = [
        name
        for match in re.finditer(r"\bfn\s+(\w+)", module)
        for name in [match.group(1)]
        if "record_swap_completion(" in _any_fn_block(module, name)
    ]
    if writers != [commit]:
        fail(
            f"`record_swap_completion(` is called from {writers or 'nowhere'} — it must be "
            f"called from `{commit}` and nowhere else, because that is the only function "
            "the caller invokes after its durable publish"
        )
    if guard["pending_window_token"] not in module:
        fail(f"the promotion module never mints a `{guard['pending_window_token']}`")

    cli = _strip_comments(_read(root, guard["promote_cli"]))
    swap_body = _any_fn_block(cli, "cmd_swap")
    publish, redeem = guard["commit_after_publish_tokens"]
    for token in (publish, redeem):
        if f"{token}(" not in swap_body:
            fail(f"`cmd_swap` never calls `{token}(`")
    if swap_body.index(f"{publish}(") > swap_body.index(f"{redeem}("):
        fail(
            f"`cmd_swap` calls `{redeem}(` BEFORE `{publish}(` — the cool-down window "
            "would be opened for a swap whose designation is not yet durable"
        )
    return (
        f"`{entry}` mints a {guard['pending_window_token']} and records nothing; "
        f"`cmd_swap` redeems it with `{commit}` only after `{publish}`"
    )


def check_every_swap_path_is_gated(config: dict, root: Path) -> str:
    """The anti-bypass check — DISCOVERED from the code, not listed in the contract.

    The first version of this check named two methods, and that is exactly how the
    bypass survived to review round 2: it pinned `evaluate_automatic_triggers` and
    `request_manual_promotion` and asserted nothing about anything else, while
    `execute_hot_swap` — the function that actually demotes and promotes — ran
    ungated. A checklist of known arms cannot catch the arm nobody added to it.

    So this enumerates the arms FROM THE SOURCE. Any `pub fn` in the orchestrator
    crate whose body reaches a demotion-side side effect is a swap path, and every
    swap path must consult the clear predicate. A future third entry point fails
    this check the moment it is written, rather than being re-found by a reviewer
    (CLAUDE.md rule 1; adversarial-precheck rule 0 — "write the guard").

    The exemptions are contract-declared and reasoned, not silent: see
    `guard.ungated_swap_paths`.
    """
    guard = contract_block(config)["guard"]
    predicate = guard["clear_predicate"]
    markers = tuple(guard["swap_path_markers"])
    exempt = {entry["method"]: entry["reason"] for entry in guard["ungated_swap_paths"]}

    modules = sorted((root / "crates/atp-orchestrator/src").rglob("*.rs"))
    if not modules:
        fail("no orchestrator sources found; the anti-bypass check would pass vacuously")

    found: list[str] = []
    ungated: list[str] = []
    for module in modules:
        source = _strip_comments(module.read_text(encoding="utf-8"))
        for match in re.finditer(r"\bpub(?:\([^)]*\))?\s+fn\s+(\w+)", source):
            name = match.group(1)
            try:
                body = _any_fn_block(source, name)
            except HotSwapCooldownCheckError:
                continue
            if not any(marker in body for marker in markers):
                continue
            found.append(name)
            if predicate in body or name in exempt:
                continue
            ungated.append(f"{module.relative_to(root)}::{name}")

    if ungated:
        fail(
            f"{len(ungated)} swap-executing entry point(s) never consult `{predicate}`: "
            f"{sorted(ungated)}. A SYS-49e window enforced only on the trigger arms is "
            "not enforced at all — a caller can build a HotSwapDemotionRequest from argv "
            "and reach the demotion without ever minting a proposal. Gate it, or declare "
            "it in `hot_swap_cooldown_contract.guard.ungated_swap_paths` with the reason."
        )
    if not found:
        fail(
            f"the anti-bypass scan matched NO function against markers {list(markers)} — "
            "the markers have drifted from the code and this check is passing vacuously"
        )
    # Non-vacuity in the other direction: the two trigger arms and the execution
    # entry point must each be among the discovered set, or the scan is not looking
    # where the requirement lives.
    for required in guard["must_be_discovered"]:
        if required not in found:
            fail(
                f"the anti-bypass scan did not discover `{required}`, which is a known "
                "swap path — the discovery is broken, not the code"
            )
    return (
        f"{len(found)} swap-executing entry point(s) discovered from source, all gated on "
        f"`{predicate}` ({len(exempt)} declared exemption(s))"
    )


def check_evaluation_and_error_carry_the_state(config: dict, orchestrator_src: str) -> str:
    guard = contract_block(config)["guard"]
    from _rust_parser import _struct_body

    evaluation = _struct_body(orchestrator_src, "TriggerEvaluation")
    if not re.search(rf"\bpub {re.escape(guard['evaluation_field'])}\s*:", evaluation):
        fail(
            f"TriggerEvaluation must carry a `{guard['evaluation_field']}` field, or a pass "
            "suppressed by a cool-down is byte-identical to a healthy 'nothing fired'"
        )
    error = _enum_body(orchestrator_src, "ManualPromotionError")
    if guard["manual_error_variant"] not in error:
        fail(f"ManualPromotionError must declare `{guard['manual_error_variant']}`")
    return (
        f"TriggerEvaluation.{guard['evaluation_field']} and "
        f"ManualPromotionError::{guard['manual_error_variant']} both carry the window"
    )


def check_store_durability(config: dict, store_src: str) -> str:
    spec = contract_block(config)["cooldown_store"]
    if f'pub const MAGIC: &str = "{spec["magic"]}"' not in store_src:
        fail(f"cooldown_store must declare MAGIC = {spec['magic']!r}")
    if not re.search(rf"pub const {re.escape(spec['marker'])}:\s*i64", store_src):
        fail(f"cooldown_store must declare {spec['marker']}")

    save = _strip_comments(_fn_block(store_src, "save"))
    for token, why in (
        ("sync_all", "fsync the scratch bytes AND the parent directory"),
        ("rename", "publish atomically so a reader never sees a half-written window"),
        ("create_dir_all", "the parent directory must exist before the scratch write"),
        ("process::id", "a per-call-unique scratch name, so two writers cannot collide"),
    ):
        if token not in save:
            fail(f"cooldown_store::save must use `{token}` — {why}")
    if save.count("sync_all") < 2:
        fail(
            "cooldown_store::save must fsync TWICE: the file (its bytes) and the parent "
            "directory (the rename). fsync on a file makes its contents durable, not its name"
        )

    record = _strip_comments(_fn_block(store_src, "record_completion"))
    if "ExclusiveGuard" not in record:
        fail("record_completion must hold the exclusive guard across its read-modify-write")
    if "KeptNewer" not in record:
        fail(
            "record_completion must keep a newer stored completion; a backwards clock would "
            "otherwise shorten a live safety window"
        )
    return (
        f"cooldown_store publishes {spec['magic']} v{spec['marker']} via "
        f"{spec['durability']!s:.44}… and record_completion is guarded + monotone"
    )


def check_resolver_is_the_only_producer(config: dict, root: Path) -> str:
    """The fabrication + clock guard.

    ``CooldownState`` is a plain matchable enum so the ~14 gate tests can inject windows
    with no I/O. The price is that an operator surface COULD construct one instead of
    reading the durable store, which would let it assert a window it never read. That
    hole is closed here rather than by an opaque type that would poison every match site.
    """
    guard = contract_block(config)["guard"]
    findings: list[str] = []
    for cli_key, subcommands in (
        ("cooldown_cli", ("cmd_status", "cmd_configure", "cmd_record_completion")),
        ("trigger_cli", ("cmd_evaluate", "cmd_manual")),
    ):
        relative = guard[cli_key]
        source = _read(root, relative)
        name = Path(relative).name
        for subcommand in subcommands:
            body = _strip_comments(_any_fn_block(source, subcommand))
            if cli_key == "cooldown_cli" and subcommand in ("cmd_configure",):
                # `configure` writes the period; it does not classify an instant.
                continue
            if f"{guard['resolver']}(" not in body:
                fail(
                    f"{name}::{subcommand} must obtain its window from "
                    f"cooldown_store::{guard['resolver']}( — a surface that constructs its "
                    "own state can assert a cool-down it never read"
                )
            if re.search(r"CooldownState::(?!unknown)", body):
                fail(
                    f"{name}::{subcommand} constructs a CooldownState literal; the durable "
                    "store is the only production producer of one"
                )
        findings.append(name)

    trigger_src = _read(root, guard["trigger_cli"])
    if guard["real_clock_token"] not in trigger_src:
        fail(
            f"{Path(guard['trigger_cli']).name} must read a real clock "
            f"({guard['real_clock_token']}): a cool-down is a decision about an INSTANT, and "
            "a frozen default makes a scheduled evaluate see zero elapsed time forever"
        )
    if guard["frozen_clock_token"] in trigger_src:
        fail(
            f"{Path(guard['trigger_cli']).name} still hardcodes "
            f"{guard['frozen_clock_token']}; that frozen instant is what rule 23 of the "
            "durable-writes playbook is about"
        )
    return (
        f"{' + '.join(findings)} resolve their window through cooldown_store."
        f"{guard['resolver']}, construct no state literal, and the trigger CLI reads a real clock"
    )


def check_schema_registry_row(config: dict, root: Path) -> str:
    spec = contract_block(config)["cooldown_store"]
    registry = _read(root, "crates/atp-data/src/schema_registry.rs")
    match = re.search(
        r"SchemaDescriptor\s*\{[^}]*?entity_id:\s*\""
        + re.escape(spec["schema_entity_id"])
        + r"\"[^}]*?\}",
        registry,
        re.DOTALL,
    )
    if not match:
        fail(
            f"crates/atp-data/src/schema_registry.rs has no PERSISTED_ENTITIES row for "
            f"{spec['schema_entity_id']!r}; an unregistered persistence surface fails "
            "tools/data015_schema_check.py"
        )
    row = match.group(0)
    for field, expected in (
        ("owner_srs", "SRS-RESV-006"),
        ("writer_path", spec["module"]),
        ("marker", spec["marker"]),
    ):
        if f'{field}: "{expected}"' not in row:
            fail(f"the {spec['schema_entity_id']} registry row must record {field} {expected!r}")
    if f'magic: Some("{spec["magic"]}")' not in row:
        fail(f"the {spec['schema_entity_id']} registry row must record magic {spec['magic']!r}")
    return (
        f"{spec['schema_entity_id']} is registered in PERSISTED_ENTITIES with the same "
        "magic, marker and writer path the contract declares"
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def assert_hot_swap_cooldown_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    block = contract_block(config)
    cooldown_src = _read(root, block["cooldown_module"])
    store_src = _read(root, block["cooldown_store"]["module"])
    orchestrator_src = _read(root, "crates/atp-orchestrator/src/lib.rs")

    return [
        check_state_enum(config, cooldown_src),
        check_period_bounds(config, cooldown_src),
        check_acknowledgement(config, cooldown_src),
        check_classify_arithmetic(config, cooldown_src),
        check_clear_predicate(config, cooldown_src),
        check_trigger_arms_are_gated(config, orchestrator_src),
        check_every_swap_path_is_gated(config, root),
        check_the_window_is_committed_after_the_publish(config, root),
        check_evaluation_and_error_carry_the_state(config, orchestrator_src),
        check_store_durability(config, store_src),
        check_resolver_is_the_only_producer(config, root),
        check_schema_registry_row(config, root),
    ]


def run_checks() -> list[str]:
    return assert_hot_swap_cooldown_static(load_config())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-RESV-006 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except HotSwapCooldownCheckError as error:
        print(f"SRS-RESV-006 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-RESV-006 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
