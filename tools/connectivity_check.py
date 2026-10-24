#!/usr/bin/env python3
"""Contract evidence script for feature ERR-2.

Verifies that the IB-Gateway connectivity gate declared in
``architecture/runtime_services.json`` (block ``connectivity_contract``) is
reachable from the Rust crates ``crates/atp-types`` and ``crates/atp-execution``.

ERR-2 traces SRS-SAFE-003 + SRS-MD-005 (SyRS SYS-45 / SYS-46 / NFR-R2). The
contract guarantees: (a) ``ConnectivityState`` declares Connected /
Unreachable / ScheduledRestartWindow in ``atp-types``; (b)
``ConnectivityEvent`` carries the four required fields (state, strategy_id,
symbol, scheduled_restart) and no broker/vendor leakage; (c) the
``BrokerageConnectivity`` and ``ConnectivityEventSink`` ports live in
``atp-execution``; (d) inside the ``StrategyMode::Live`` arm of
``ExecutionEngine::submit_live_order``, the broker is only called when
``ConnectivityState::Connected``, and the Unreachable /
ScheduledRestartWindow branch produces
``OrderErrorCategory::ConnectivityBlocked``, records a
``ConnectivityEvent``, and requests a reconnect — all without invoking
the brokerage port.

Mirrors the PASS/FAIL output style of ``tools/error_handling_check.py``.

Invoke:
    python3 tools/connectivity_check.py
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from _rust_parser import _enum_body, _fn_block, _match_arm, _struct_body, _trait_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class ConnectivityCheckError(AssertionError):
    pass


def fail(message: str) -> NoReturn:
    raise ConnectivityCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def connectivity_block(config: dict) -> dict:
    if "connectivity_contract" not in config:
        fail("architecture metadata is missing connectivity_contract")
    return config["connectivity_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = connectivity_block(config)
    crate_path = root / block["types_crate"]["path"]
    # The admission decision and its proof live in their own module (SRS-MD-005:
    # the proof's private field is what makes `Admitted` unforgeable), so the
    # types surface these checks read is the crate root PLUS that module.
    parts = []
    for name in ("lib.rs", "admission.rs"):
        source_path = crate_path / "src" / name
        if not source_path.exists():
            fail(f"types crate source missing: {source_path.relative_to(root)}")
        parts.append(source_path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _code_only(source: str) -> str:
    """`source` with every comment blanked, so a guard tests CODE, not the prose about it.

    Without this, a doc comment that says "never `to_socket_addrs`" trips the
    check that forbids `to_socket_addrs` — the guard fires on the sentence
    explaining why the guard exists, and a check that flags its own
    documentation is one people learn to route around.

    String literals are KEPT: the forbidden-token scans read them as code.

    It reads through `_mask_non_code`, the one lexer. It used to drop every
    line whose first non-space characters were `//`, which deleted real code
    twice. Dropping lines starting `*` (meant for block-comment continuations)
    removed every deref-assignment, so an ungated
    `*self.subscribers.entry(k).or_default() = vec![s];` was invisible. And
    inside a block comment a `//` line is still comment text only up to its
    `*/`: `/*\n// */ impl Gate for Evil {}` compiles to an implementor that the
    line-dropper hid from the enumeration (r28). A stripper that removes real
    code is worse than the false positive it was added for.
    """
    return _mask_non_code(source, strings=False)


def _fn_body_any(source: str, fn_name: str) -> str:
    """Return a function body, tolerating ``const`` / ``async`` modifiers.

    ``_rust_parser._fn_block`` matches ``pub fn`` exactly, and the restart-window
    classifiers are ``pub const fn`` (they are pure and evaluated at compile time
    in the crate's own tests). Widening the SHARED parser would change what ~20
    other check tools match, so the tolerance lives here instead.
    """
    match = re.search(rf"\bpub\s+(?:const\s+|async\s+)*fn\s+{re.escape(fn_name)}\b[^{{]*{{", source)
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


def restart_window_block(config: dict) -> dict:
    block = connectivity_block(config)
    if "restart_window" not in block:
        fail("connectivity_contract is missing the SRS-MD-005 restart_window block")
    return block["restart_window"]


def _crate_lib_source(config: dict, crate_key: str, root: Path = ROOT) -> str:
    block = restart_window_block(config)
    source_path = root / block[crate_key]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"{crate_key} source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def market_data_source(config: dict, root: Path = ROOT) -> str:
    """The module that OWNS the consolidated subscription set.

    Declared in the contract rather than assumed to be `lib.rs`. The registry
    lives in its own module precisely so this file is the complete set of code
    that can reach `subscribers` — Rust exposes a parent's privates to its
    CHILDREN, so while it sat in the crate root a sibling module could reach in
    and no scan over the root could see it.
    """
    block = restart_window_block(config)
    source_path = root / block["admission_sites"]["module"]
    if not source_path.exists():
        fail(
            f"the consolidated subscription module is missing: {block['admission_sites']['module']}"
        )
    return source_path.read_text(encoding="utf-8")


def market_data_lib_source(config: dict, root: Path = ROOT) -> str:
    """The crate root, for checks about the crate's public surface."""
    return _crate_lib_source(config, "market_data_crate", root)


def producer_source(config: dict, root: Path = ROOT) -> str:
    block = restart_window_block(config)
    source_path = root / block["producer"]["module"]
    if not source_path.exists():
        fail(f"restart-window producer missing: {block['producer']['module']}")
    return source_path.read_text(encoding="utf-8")


def reachability_source(config: dict, root: Path = ROOT) -> str:
    block = restart_window_block(config)
    source_path = root / block["reachability_port"]["module"]
    if not source_path.exists():
        fail(f"gateway-reachability seam missing: {block['reachability_port']['module']}")
    return source_path.read_text(encoding="utf-8")


def execution_source(config: dict, root: Path = ROOT) -> str:
    block = connectivity_block(config)
    crate_path = root / block["execution_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"execution crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_connectivity_state_enum(config: dict, types_src: str) -> str:
    block = connectivity_block(config)
    spec = block["connectivity_state"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"states ({', '.join(spec['variants'])}) — connectivity safety gate "
        "(SRS-SAFE-003 / SRS-MD-005)"
    )


def check_connectivity_event_struct(config: dict, types_src: str) -> str:
    block = connectivity_block(config)
    spec = block["connectivity_event"]
    try:
        body = _struct_body(types_src, spec["struct"])
    except AssertionError as error:
        fail(str(error))
    missing = [
        field
        for field in spec["required_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    leaks = [
        field
        for field in spec["forbidden_fields"]
        if re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if leaks:
        fail(
            f"{spec['struct']} leaks broker/vendor field(s): {', '.join(leaks)} "
            "(ERR-2 events must not carry broker/session identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/vendor fields"
    )


def check_brokerage_connectivity_port(config: dict, exec_src: str) -> str:
    block = connectivity_block(config)
    spec = block["brokerage_connectivity_port"]
    body = _trait_body(exec_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-execution declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} methods ({', '.join(spec['methods'])}) — "
        "the SRS-SAFE-003 connectivity probe + reconnect request"
    )


def check_connectivity_event_sink_port(config: dict, exec_src: str) -> str:
    block = connectivity_block(config)
    spec = block["connectivity_event_sink_port"]
    body = _trait_body(exec_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-execution declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method ({', '.join(spec['methods'])}) — "
        "the structured-event publication channel for ERR-2"
    )


def check_connectivity_guard_in_submit_live_order(config: dict, exec_src: str) -> str:
    block = connectivity_block(config)
    entry = block["entry_point"]
    guard = block["guard"]
    try:
        body = _fn_block(exec_src, entry["method"])
    except AssertionError as error:
        fail(str(error))

    try:
        live_arm = _match_arm(body, "StrategyMode::Live")
    except AssertionError as error:
        fail(str(error))

    # Inside the Live arm we expect a nested match on connectivity.state().
    connected_token = f"{guard['state_enum']}::{guard['connected_variant']}"
    if connected_token not in live_arm:
        fail(
            f"{entry['method']} Live arm is missing the connectivity-gate "
            f"`{connected_token}` branch — Live submissions would bypass the "
            "SRS-SAFE-003 connectivity check"
        )

    # The Connected sub-arm must be the only call site of broker.submit_order(.
    call_token = guard["broker_call"] + "("
    try:
        connected_arm = _match_arm(live_arm, connected_token)
    except AssertionError as error:
        fail(str(error))
    if call_token not in connected_arm:
        fail(
            f"{entry['method']} {connected_token} sub-arm does not call "
            f"`{call_token}` — Connected submissions would never reach the broker"
        )

    # The blocked-states branch must carry CONNECTIVITY_BLOCKED + events.record(
    # + connectivity.request_reconnect( AND must NOT call broker.submit_order(.
    blocked_block = live_arm
    for variant in guard["blocked_variants"]:
        if f"{guard['state_enum']}::{variant}" not in blocked_block:
            fail(
                f"{entry['method']} Live arm is missing the "
                f"{guard['state_enum']}::{variant} branch — "
                "ERR-2 requires both Unreachable and ScheduledRestartWindow "
                "to be blocked"
            )

    blocked_only = live_arm.replace(connected_arm, "")
    rejection = block["rejection_category"]
    if f"OrderErrorCategory::{rejection}" not in blocked_only:
        fail(f"{entry['method']} blocked-state branch must produce OrderErrorCategory::{rejection}")
    for token in (
        guard["event_call"] + "(",
        guard["reconnect_call"] + "(",
    ):
        if token not in blocked_only:
            fail(
                f"{entry['method']} blocked-state branch is missing required "
                f"call `{token}` (SRS-SAFE-003 logging + reconnect attempt)"
            )
    if call_token in blocked_only:
        fail(
            f"{entry['method']} blocked-state branch calls `{call_token}` — "
            "ERR-2 requires zero broker side effect when IB is unreachable"
        )

    return (
        f"atp-execution::{entry['method']} gates `{guard['broker_call']}` on "
        f"{connected_token}; Unreachable / ScheduledRestartWindow branches "
        f"emit OrderErrorCategory::{rejection}, record a "
        f"{block['connectivity_event']['struct']}, and call "
        f"`{guard['reconnect_call']}` with zero broker side effect (ERR-2)"
    )


# --------------------------------------------------------------------------- #
# SRS-MD-005 — the scheduled restart window (SyRS SYS-75)
# --------------------------------------------------------------------------- #


def check_restart_phase_enum(config: dict, types_src: str) -> str:
    spec = restart_window_block(config)["restart_phase"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} phases "
        f"({', '.join(spec['variants'])}) — the SyRS SYS-75 restart window (SRS-MD-005)"
    )


def check_market_data_admission_enum(config: dict, types_src: str) -> str:
    spec = restart_window_block(config)["market_data_admission"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} outcomes "
        f"({', '.join(spec['variants'])}) — a market-data refusal states WHETHER and WHY, "
        "so planned maintenance is never rendered as an outage or the reverse"
    )


def check_restart_window_defaults(config: dict, types_src: str) -> str:
    spec = restart_window_block(config)["window_type"]
    try:
        body = _struct_body(types_src, spec["struct"])
    except AssertionError as error:
        fail(str(error))
    if re.search(r"\bpub\s+\w+\s*:", body):
        fail(
            f"{spec['struct']} exposes a public field — the window is a safety input and "
            "must be constructible only through its validating constructor"
        )
    for method in spec["methods"]:
        if f"fn {method}(" not in types_src:
            fail(f"{spec['struct']} is missing the `{method}` method")
    defaults = spec["defaults"]
    for const_key, value_key in (
        ("suspend_lead_seconds_const", "suspend_lead_seconds"),
        ("window_seconds_const", "window_seconds"),
    ):
        name = defaults[const_key]
        expected = defaults[value_key]
        match = re.search(rf"{re.escape(name)}\s*:\s*i64\s*=\s*(\d+)\s*;", types_src)
        if match is None:
            fail(f"atp-types is missing the `{name}` default constant")
        if int(match.group(1)) != expected:
            fail(
                f"{name} is {match.group(1)} but the SyRS SYS-75 catalogue default is "
                f"{expected} — a documented default the code does not implement"
            )
    return (
        f"atp-types declares {spec['struct']} with private fields, "
        f"{len(spec['methods'])} classification methods, and the SyRS SYS-75 defaults "
        f"({defaults['suspend_lead_seconds']}s lead / {defaults['window_seconds']}s window)"
    )


def check_restart_escalation_arm(config: dict, types_src: str) -> str:
    """The sharpest clause: the window must END.

    A `RestartPhase::Elapsed` that still returned the maintenance state would
    suppress a genuine outage forever, and nothing else in the tree would
    notice — the dispatcher would keep honouring a marker that never cleared.
    """
    block = restart_window_block(config)
    escalation = block["escalation"]
    body = _fn_body_any(types_src, "connectivity_state")
    phase = block["restart_phase"]["enum"]
    elapsed = f"{phase}::{escalation['phase']}"
    if elapsed not in body:
        fail(
            f"connectivity_state does not handle {elapsed} — without it the restart "
            "window never closes and a real outage stays suppressed"
        )
    unreachable = f"ConnectivityState::{escalation['unreachable_state']}"
    if unreachable not in body:
        fail(
            f"connectivity_state never produces {unreachable} — the SyRS SYS-75 "
            "escalation to standard connectivity-loss handling is unreachable"
        )
    if re.search(r"^\s*_\s*=>", body, re.MULTILINE):
        fail(
            "connectivity_state uses a catch-all match arm — a phase added later would "
            "inherit whichever answer happened to be there instead of failing to compile"
        )
    return (
        f"atp-types::connectivity_state maps {elapsed} onto {unreachable} with no "
        "catch-all arm — the SyRS SYS-75 escalation is exhaustive by construction"
    )


def _mask_non_code(source: str, *, strings: bool = True) -> str:
    """`source` with every comment - and, by default, every literal - blanked.

    Same length, newlines kept, so an index into the result is an index into the
    source. Every scan that counts brackets reads THIS, not the raw text: a brace
    or angle bracket inside a literal or a comment is not code. `strings=False`
    blanks comments only, for the scans that read literals as code on purpose.

    It follows rustc's lexical rules, not a likeness of them, because every place
    this lexer disagrees with the compiler about where code is, is a place to
    hide code. Rust has five ways to hide a bracket from a naive counter, and the
    real tree uses them: a string literal, a raw string (`r#"{"id":1}"#`, 46 of
    them in one adapter), a line comment, a block comment and a char literal.
    The rules that were got wrong, each of them once:

    - A block comment NESTS (`/* a /* b */ c */` is one comment). Closing at the
      first `*/` let `/* outer /* inner */ " */` read the last `"` as a string
      opener and blank the implementor after it; all 13 checks passed (r28).
    - Nothing is tokenized inside a comment: a `"`, `r#"` or `'` there opens
      nothing.
    - A lifetime (`'a`) is not a char literal; reading one as an open quote
      swallowed the rest of the file.
    - A char escape is a unit: `'\\''` is one literal, not `'\\'` plus a stray `'`.

    A comment, string or char literal that never closes is REFUSED, never blanked
    to end of file or guessed at: what the lexer cannot bound, no scan built on
    it can call clean (CLAUDE.md rule 3). There is exactly one copy of these
    rules; every scan in this file reads through it.
    """
    out = list(source)
    size = len(source)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    cursor = 0
    while cursor < size:
        two = source[cursor : cursor + 2]
        if two == "//":
            nl = source.find("\n", cursor)
            stop = size if nl == -1 else nl
            blank(cursor, stop)
            cursor = stop
            continue
        if two == "/*":
            depth, probe = 1, cursor + 2
            while depth:
                if probe >= size:
                    _refuse(source, cursor, "block comment")
                pair = source[probe : probe + 2]
                if pair == "/*":
                    depth, probe = depth + 1, probe + 2
                elif pair == "*/":
                    depth, probe = depth - 1, probe + 2
                else:
                    probe += 1
            blank(cursor, probe)
            cursor = probe
            continue
        literal_end = _literal_end(source, cursor)
        if literal_end is None:
            cursor += 1
            continue
        # A literal is always skipped whole, so a `//` or `/*` inside one is never
        # read as a comment; `strings=False` keeps its text.
        if strings:
            blank(cursor, literal_end)
        cursor = literal_end
    # A raw identifier IS its plain name: `fn r#is_subscribed` declares
    # `is_subscribed`. The prefix is erased HERE, once, so no scan can read the
    # name differently from another - the span walker stripped it and the
    # declaration counter did not, and a raw-named mutator inherited a read-only
    # exemption through the gap (r31). A raw STRING keeps its prefix: its `r#` is
    # followed by `"` or `#`, never by an identifier character.
    return re.sub(r"\br#(?=[A-Za-z_])", "  ", "".join(out))


# A char literal's escape and its closing quote: `\u{..}` (hex digits and `_`),
# `\x` and two hex digits, or the backslash and any one character.
_CHAR_ESCAPE = re.compile(r"\\(?:u\{[0-9A-Fa-f_]{1,8}\}|x[0-9A-Fa-f]{2}|.)'", re.S)


def _refuse(source: str, at: int, what: str) -> NoReturn:
    line = source.count("\n", 0, at) + 1
    raise ConnectivityCheckError(
        f"unterminated {what} at line {line} of a scanned Rust source; this scan "
        "cannot tell where code resumes, so it cannot bound what follows"
    )


def _literal_end(source: str, cursor: int) -> int | None:
    """The index just past the string, raw string or char literal at `cursor`.

    None when `cursor` does not open one (a lifetime, a raw identifier, code).
    A prefix (`b"`, `c"`, `br#"`) needs no case of its own: the prefix letter is
    code, and the literal is found at its quote or its `r`.
    """
    size = len(source)
    char = source[cursor]
    if char == "r" and source[cursor + 1 : cursor + 2] in ("#", '"'):
        if cursor and (source[cursor - 1].isalnum() or source[cursor - 1] == "_"):
            if source[cursor - 1] not in "bc" or (
                cursor > 1 and (source[cursor - 2].isalnum() or source[cursor - 2] == "_")
            ):
                return None  # the tail of an identifier, not a raw-string prefix
        hashes, probe = 0, cursor + 1
        while source[probe : probe + 1] == "#":
            hashes, probe = hashes + 1, probe + 1
        if source[probe : probe + 1] != '"':
            return None  # a raw identifier (`r#type`)
        close = source.find('"' + "#" * hashes, probe + 1)
        if close == -1:
            _refuse(source, cursor, "raw string")
        return close + 1 + hashes
    if char == '"':
        probe = cursor + 1
        while probe < size:
            if source[probe] == "\\":
                probe += 2
            elif source[probe] == '"':
                return probe + 1
            else:
                probe += 1
        _refuse(source, cursor, "string literal")
    if char == "'":
        if source[cursor + 1 : cursor + 2] == "\\":
            # An escape is a unit, in exactly rustc's forms; the literal must
            # close right after it.
            escaped = _CHAR_ESCAPE.match(source, cursor + 1)
            if escaped is None:
                _refuse(source, cursor, "char literal")
            return escaped.end()
        if source[cursor + 2 : cursor + 3] == "'" and source[cursor + 1 : cursor + 2] != "\n":
            return cursor + 3
    return None


def _without_test_module(source: str) -> str:
    """Drop every `#[cfg(test)] mod .. { .. }` BLOCK, keeping the rest.

    Cuts the module's balanced-brace span, not everything to end of file. The
    first version truncated at the marker, so any production code declared AFTER
    a test module — which Rust permits — was invisible to every scan built on
    this, including the one that enumerates who may implement the admission
    gate. A stripper that removes real code is the same defect as the `*` rule
    that hid a deref-assignment, one file along.

    Braces are counted in `_mask_non_code`'s output. Counting them in the raw text
    meant a test module containing `let s = "{";` never closed, so the stripper
    swallowed the whole rest of the file - and every scan built on it, including
    the implementor enumeration, then read an empty tail as a clean, closed set.
    """
    masked = _mask_non_code(source)
    out: list[str] = []
    index = 0
    for match in re.finditer(r"#\[cfg\(test\)\]\s*(?:pub\s+)?mod\s+\w+\s*\{", masked):
        if match.start() < index:
            continue
        # A `#[cfg(test)] mod` handed to a macro is whatever the macro emits: a
        # macro can drop the cfg and compile the body into production. Cutting it
        # as a test module hid an implementor (red team, after r31).
        macro = _enclosing_macro(masked, match.start())
        if macro:
            fail(
                f"a `#[cfg(test)] mod` is the input to {macro}, which decides what it "
                "expands to; this scan cannot tell whether it is test code, so it refuses "
                "rather than stripping it"
            )
        out.append(source[index : match.start()])
        depth, cursor = 1, match.end()
        while cursor < len(masked) and depth:
            if masked[cursor] == "{":
                depth += 1
            elif masked[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth:
            fail(
                "an unterminated `#[cfg(test)] mod` was found while stripping test code; "
                "the remainder of the file cannot be classified, so this scan refuses "
                "rather than reading a truncated source as a clean tree"
            )
        index = cursor
    out.append(source[index:])
    return "".join(out)


def check_market_data_admission_sites(config: dict, market_data_src: str) -> str:
    """SYS-75(a) for market data: nothing joins the subscription set unadmitted.

    The COMPILER enforces it now; this check pins the structure the compiler
    relies on. Seven versions of this guard tried to recognise, in source text,
    every way Rust can write to a map, and review kept finding the next one -
    `entry().or_default()`, a trait-impl method, a sibling module, a macro body,
    a raw identifier, `Vec::push(..)`, `mem::swap`, a gate call whose answer is
    ignored. Text cannot decide those; types can:

    1. `atp_types::AdmissionProof` has a private field and is built in ONE
       place, `RestartWindow::market_data_admission`, as the payload of
       `MarketDataAdmission::Admitted`. No other code can produce one.
    2. The registry's map is an `AdmittedSet`, whose map is private to its own
       module and whose ONLY add method consumes a proof. Every write that
       skipped the decision is a compile error, wherever it is written.
    3. The acceptance envelope carries the proof, so an acceptance cannot be
       built without a decision either.

    What is pinned here, because the compiler relies on it: the two files that
    alone can make a proof or grow the set are frozen by SHA-256, so a second
    way in is a visible contract change; the proof's field stays private; the
    registry's field stays a private `AdmittedSet`; the envelope keeps its
    proof; the `compile_fail` doctests that prove (1) and (2) stay in place; and
    atp-market-data never asks a `RestartWindow` itself - its only source of a
    proof is the gate port, so it cannot decide on inputs of its own choosing.
    """
    block = restart_window_block(config)
    spec = block["admission_sites"]
    port = block["gate_port"]

    lib_src = market_data_lib_source(config)
    try:
        trait_body = _trait_body(lib_src, port["trait"])
    except AssertionError as error:
        fail(str(error))
    for method in port["methods"]:
        if f"fn {method}(" not in trait_body:
            fail(f"{port['trait']} is missing the `{method}` method")

    for pinned in (spec["admission_proof"], spec["admitted_set"]):
        path = ROOT / pinned["module"]
        if not path.is_file():
            fail(
                f"{pinned['module']} is missing; it is one of the two files the compiler's guarantee rests on"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != pinned["sha256"]:
            fail(
                f"{pinned['module']} changed (sha256 {digest[:12]}..., pinned "
                f"{pinned['sha256'][:12]}...). It is one of the two files that alone can "
                "make an admission proof or grow the subscription set, so any edit to it "
                "is a change to what may admit: re-review it, then update its sha256 in "
                "connectivity_contract.restart_window.admission_sites"
            )

    proof_src = _mask_non_code(
        (ROOT / spec["admission_proof"]["module"]).read_text(encoding="utf-8")
    )
    proof_fields = _struct_body(proof_src, spec["admission_proof"]["type"])
    if re.search(r"\bpub\b", proof_fields):
        fail(
            f"`{spec['admission_proof']['type']}` has a public field, so any crate can build "
            "one and claim an admission no window decided"
        )
    set_src = _mask_non_code((ROOT / spec["admitted_set"]["module"]).read_text(encoding="utf-8"))
    if re.search(r"\bpub\b", _struct_body(set_src, spec["admitted_set"]["type"])):
        fail(
            f"`{spec['admitted_set']['type']}` exposes its map, so code can grow the "
            "subscription set without spending a proof"
        )

    # Review r32: a `use` swapped in a same-named set and every check passed,
    # because they compared SPELLINGS. Each type the guarantee rests on is now
    # spelled as an absolute path no `use` or local item can redirect, and
    # `_check_paths_resolve_to_the_pinned_items` proves where each path lands.
    module_src = _mask_non_code(_without_test_module(market_data_src))
    field = spec["private_field"]
    registry = spec["registry"]
    fields = _struct_fields(module_src, registry["struct"])
    if field not in fields or fields[field][1] != spec["field_type"]:
        fail(
            f"the registry's `{field}` is no longer an `{spec['field_type']}` (spelled "
            "exactly so, as an absolute path); only that type makes an addition spend an "
            "admission proof"
        )
    if fields[field][0]:
        fail(f"the registry's `{field}` is `{fields[field][0]}`; it must stay private")
    if {name: ty for name, (_, ty) in fields.items()} != registry["fields"]:
        fail(
            f"`{registry['struct']}` holds {sorted(fields)}, not exactly "
            f"{sorted(registry['fields'])}; a field the contract does not name could hold "
            "a second subscription set that no admission gates"
        )

    envelope = _struct_fields(_mask_non_code(lib_src), spec["acceptance_envelope"])
    if spec["envelope_proof_type"] not in {ty for _, ty in envelope.values()}:
        fail(
            f"`{spec['acceptance_envelope']}` no longer carries an "
            f"`{spec['envelope_proof_type']}` (spelled exactly so), so an acceptance can be "
            "built without a restart-window decision"
        )

    # The owning module is read from the text under test, the rest from disk.
    _check_paths_resolve_to_the_pinned_items(ROOT, spec, {spec["module"]: market_data_src})

    for pinned, codes in (
        (spec["admission_proof"], ("E0451",)),
        (spec["admitted_set"], ("E0616", "E0451")),
    ):
        text = (ROOT / pinned["module"]).read_text(encoding="utf-8")
        for code in codes:
            if f"```compile_fail,{code}" not in text:
                fail(
                    f"{pinned['module']} lost its `compile_fail,{code}` doctest, the proof "
                    "that the compiler refuses the forbidden shape"
                )

    _check_unsafe_code_is_forbidden(ROOT)

    crate_src = (ROOT / spec["crate_path"]).parent
    for path in sorted(crate_src.rglob("*.rs")):
        # The owning module is read from the text under test, the rest from disk.
        if path == ROOT / spec["module"]:
            masked = module_src
        else:
            masked = _mask_non_code(_without_test_module(path.read_text(encoding="utf-8")))
        asked = re.search(
            r"\b(RestartWindow|market_data_admission|admits_market_data_requests)\b", masked
        )
        if asked:
            fail(
                f"{path.relative_to(ROOT)} names `{asked.group(1)}`. atp-market-data must "
                "take its admission decisions from the gate port only; a window it builds "
                "itself is one whose inputs it chooses, so it could admit at will"
            )

    return (
        f"atp-market-data cannot grow its subscription set without an admission proof: "
        f"`{field}` is a private `{spec['field_type']}` whose only add method spends an "
        f"`{spec['admission_proof']['type']}`, which only `RestartWindow` can make "
        f"(both files pinned by sha256, both rules proved by compile_fail doctests); "
        f"`{spec['acceptance_envelope']}` carries the proof, the crate never asks a "
        "window itself, and every crate forbids `unsafe` code"
    )


def _struct_fields(masked: str, struct: str) -> dict[str, tuple[str, str]]:
    """`{name: (visibility, type)}` of a named-field struct, types without whitespace.

    Refuses an attribute inside the body: `#[cfg(..)]` on a field makes two
    declarations of one name, only one of which compiles.
    """
    body = _struct_body(masked, struct)
    if "#" in body:
        fail(f"`{struct}` has an attribute on a field; its fields must be plain")
    items, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(body[start:i])
            start = i + 1
    items.append(body[start:])
    fields: dict[str, tuple[str, str]] = {}
    for item in filter(str.strip, items):
        m = re.fullmatch(r"\s*(pub(?:\s*\([^)]*\))?\s+)?(\w+)\s*:(.+)", item, re.S)
        if not m:
            fail(f"cannot read a field of `{struct}`: {' '.join(item.split())!r}")
        vis = re.sub(r"\s+", "", m.group(1) or "")
        fields[m.group(2)] = (vis, re.sub(r"\s+", "", m.group(3)))
    return fields


def _check_paths_resolve_to_the_pinned_items(
    root: Path, spec: dict, under_test: dict[str, str]
) -> None:
    """Prove that the absolute paths the check compares name the pinned files.

    `crate::subscriptions::admitted_set::AdmittedSet` and
    `::atp_types::admission::AdmissionProof` are spelled so that no `use` and no
    local item can redirect them. What can still redirect them is the wiring
    under each segment, so it is pinned here:

    - each file the paths run through is the file Cargo compiles at that path
      (`_check_cargo_compiles_the_files_read`), so it IS the file the digest pins;
    - `atp_types` is the real atp-types crate: the dependent's manifest names
      it exactly by path, with no `package` rename, the crate keeps its name and
      library name, and atp-market-data has no `extern crate`, which could bind
      the name to something else;
    - no other atp-types code names `market_data_admission`, so the window is the
      only source of a proof and no wrapper hands one out under another name.
    """
    _check_cargo_compiles_the_files_read(
        root,
        (
            spec["module"],
            spec["admitted_set"]["module"],
            spec["admission_proof"]["module"],
            f"{Path(spec['module']).parent}/lib.rs",
        ),
        under_test,
    )

    crate = spec["types_crate"]
    types_manifest = tomllib.loads((root / crate["manifest"]).read_text(encoding="utf-8"))
    lib_name = types_manifest.get("lib", {}).get("name", crate["package"].replace("-", "_"))
    if types_manifest.get("package", {}).get("name") != crate["package"] or lib_name != crate[
        "package"
    ].replace("-", "_"):
        fail(f"{crate['manifest']} no longer builds the `{crate['package']}` library")
    dependent = tomllib.loads((root / crate["dependent_manifest"]).read_text(encoding="utf-8"))
    tables = [dependent.get("dependencies", {})]
    tables += [t.get("dependencies", {}) for t in dependent.get("target", {}).values()]
    want = crate["package"].replace("-", "_")
    for table in tables:
        for key, dep in table.items():
            package = dep.get("package", key) if isinstance(dep, dict) else key
            if want in (key.replace("-", "_"), package.replace("-", "_")):
                if key != crate["package"] or dep != crate["dependency"]:
                    fail(
                        f"{crate['dependent_manifest']} binds `{key}` as {dep!r}, not exactly "
                        f"{crate['dependency']!r}: `atp_types` would name another crate"
                    )
    if dependent.get("dependencies", {}).get(crate["package"]) != crate["dependency"]:
        fail(f"{crate['dependent_manifest']} no longer depends on {crate['package']} by path")
    crate_src = (root / spec["crate_path"]).parent
    for path in sorted(crate_src.rglob("*.rs")):
        if re.search(r"\bextern\s+crate\b", _mask_non_code(path.read_text(encoding="utf-8"))):
            fail(
                f"{path.relative_to(root)} has an `extern crate`, which can bind a crate "
                "name the pinned paths rely on to another crate"
            )

    proof_module = root / spec["admission_proof"]["module"]
    for path in sorted(proof_module.parent.rglob("*.rs")):
        if path == proof_module:
            continue
        masked = _mask_non_code(_without_test_module(path.read_text(encoding="utf-8")))
        if re.search(r"\bmarket_data_admission\b", masked):
            fail(
                f"{path.relative_to(root)} names `market_data_admission` outside "
                f"{spec['admission_proof']['module']}; a wrapper there could hand "
                "atp-market-data a proof under a name this check does not know"
            )


def _check_cargo_compiles_the_files_read(
    root: Path, rel_paths: tuple[str, ...], under_test: dict[str, str] | None = None
) -> None:
    """Prove that each file a check reads by name is the file Cargo compiles there.

    A check that reads `src/lib.rs` certifies nothing if Cargo builds the library
    from somewhere else. Review r33 pointed atp-market-data's `[lib] path` at a
    copy of `lib.rs` whose envelope carried no proof, and every check passed on
    the original file that no longer compiled. Review r32 did the same one level
    down, with a module the checker read by path. So for every file read:

    - the crate's library root is `src/lib.rs`, the file the checks start from;
    - each module on the way from that root to the file is declared exactly
      once, with no attribute in front of it (`#[path]` points it at another
      file; `#[cfg]` makes a twin) and no inline body.

    The rest of what could put other code into a crate is refused for the whole
    tree by `_refuse_code_outside_the_scan`, which this gate also runs: build
    scripts, proc-macro crates, symlinks, and `include!` and `#[path]` in any file.
    """
    under_test = under_test or {}
    for rel in rel_paths:
        parts = Path(rel).parts
        if len(parts) < 4 or parts[0] != "crates" or parts[2] != "src" or ".." in parts:
            fail(
                f"{rel} is not under crates/<crate>/src, so no check can tell that Cargo builds it"
            )
        crate = f"crates/{parts[1]}"
        manifest = tomllib.loads((root / crate / "Cargo.toml").read_text(encoding="utf-8"))
        lib_path = manifest.get("lib", {}).get("path", "src/lib.rs")
        if PurePosixPath(lib_path) != PurePosixPath("src/lib.rs"):
            fail(
                f"{crate}/Cargo.toml builds its library from `{lib_path}`, not src/lib.rs; "
                "these checks read src/lib.rs and its modules, so they would certify code "
                "Cargo does not compile"
            )
        modules = list(parts[3:])
        if modules == ["lib.rs"]:
            continue
        modules[-1:] = [] if modules[-1] == "mod.rs" else [modules[-1].removesuffix(".rs")]
        declared_in = f"{crate}/src/lib.rs"
        for depth, name in enumerate(modules):
            text = under_test.get(declared_in)
            if text is None:
                text = (root / declared_in).read_text(encoding="utf-8")
            masked = _mask_non_code(_without_test_module(text))
            mentions = re.findall(rf"\bmod\s+{name}\b\s*[;{{]", masked)
            declared = re.findall(
                rf"(?:^|[;}}])\s*(#\s*!?\s*\[[^\]]*\]\s*)?(?:pub\s*(?:\([^)]*\)\s*)?)?mod\s+{name}\s*;",
                masked,
            )
            if len(mentions) != 1 or len(declared) != 1 or declared[0]:
                fail(
                    f"{declared_in} must declare `mod {name};` exactly once, with no "
                    "attribute and no inline body; otherwise the path to "
                    f"{rel} can resolve to another file"
                )
            parent = "/".join([crate, "src", *modules[: depth + 1]])
            declared_in = (
                f"{parent}.rs" if (root / f"{parent}.rs").is_file() else f"{parent}/mod.rs"
            )


def _check_unsafe_code_is_forbidden(root: Path) -> None:
    """The proof is unforgeable only in SAFE Rust.

    `unsafe { std::mem::transmute::<(), AdmissionProof>(()) }` and
    `std::mem::zeroed()` both make a zero-sized proof without its constructor,
    and a raw pointer can write the private map. The workspace has forbidden
    `unsafe_code` since SRS-ARCH-001; MD-005 now rests on that, so it is pinned:
    the workspace forbids it, and every crate inherits the workspace lints and
    nothing else. `forbid`, unlike `deny`, cannot be lowered by an
    `#![allow(unsafe_code)]` inside a crate (rustc E0453).
    """
    manifest = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    level = manifest.get("workspace", {}).get("lints", {}).get("rust", {}).get("unsafe_code")
    if isinstance(level, dict):
        level = level.get("level")
    if level != "forbid":
        fail(
            f'the workspace sets `unsafe_code = {level!r}`, not "forbid"; unsafe code '
            "can fabricate an AdmissionProof (transmute or zeroed) and write the "
            "admitted set behind the type system"
        )
    for toml_path in sorted((root / "crates").glob("*/Cargo.toml")):
        lints = tomllib.loads(toml_path.read_text(encoding="utf-8")).get("lints")
        if lints != {"workspace": True}:
            fail(
                f"{toml_path.relative_to(root)} does not inherit the workspace lints "
                f"(`[lints]` is {lints!r}, expected `workspace = true` alone), so it "
                "may compile unsafe code that forges an AdmissionProof"
            )


def check_restart_window_producer(config: dict, producer_src: str) -> str:
    block = restart_window_block(config)
    spec = block["producer"]
    if f"pub struct {spec['struct']}" not in producer_src:
        fail(f"{spec['module']} does not declare `{spec['struct']}`")
    for trait in spec["implements"]:
        # `[^{};]*?`, not `<[^>]*>`: the latter cannot span a `Fn() -> i64`
        # bound, so a producer refactored to carry one would make this
        # `fail()` claiming the trait is not implemented at all. Same defect
        # as the implementor scan below; fixed in both, not just the one the
        # reviewer pointed at.
        if not re.search(rf"\bimpl\b[^{{}};]*?\s+(?:\w+::)*{re.escape(trait)}\s+for", producer_src):
            fail(
                f"{spec['struct']} does not implement `{trait}` — the order gate and the "
                "market-data gate must read ONE window, or they drift"
            )
    return (
        f"atp-orchestrator::{spec['struct']} implements "
        f"{' + '.join(spec['implements'])} — one producer behind both suspensions "
        "(SRS-MD-005)"
    )


# A generic list, allowing parenthesised bounds (`Fn() -> bool`) and one level of
# nesting (`Foo<Bar>`). `->` is an explicit alternative because its `>` is
# otherwise excluded by the same class that stops the match running away - the
# THIRD time in this feature an arrow has defeated a bracket-matching pattern.
# Stops at `{`, `}` or `;`, none of which can appear inside a generic list.
def _local_aliases(source: str, trait: str) -> set[str]:
    """Names this file has bound to `trait` through `use ... as ...`.

    Reads the rename PAIRS, not the shape of the use tree around them. A rename
    `X as Y` can only appear inside a use declaration, and a use declaration
    always ends at its first `;` (a use tree cannot contain one), so every
    `X as Y` between `use` and that `;` is a rename at whatever depth it sits.
    The previous version matched a braced group with `\\{([^}]*)\\}`, so a
    NESTED group ended the match early: `use md::{RestartWindowGate as G,
    live_feed::{LiveFeedLoop}};` bound no alias and `impl G for AlwaysOpen`
    passed every check (review r29). A guard keyed on one spelling is a guard a
    two-word rename walks past; one keyed on one tree shape is a guard a pair of
    braces walks past.

    `self as Y` renames the segment in front of its group:
    `use md::RestartWindowGate::{self as G};` binds `G` to the trait.

    A rename through a macro metavariable (`RestartWindowGate as $n`,
    `$t as G`) binds a name only the call site knows, so it is refused.
    """
    out: set[str] = set()
    masked = _mask_non_code(source)
    for use in re.finditer(r"\buse\b", masked):
        stop = masked.find(";", use.end())
        if stop == -1:
            fail(
                f"a use declaration at offset {use.start()} never ends; this scan cannot "
                f"tell which names it binds, so it cannot bound the `{trait}` aliases"
            )
        tree = masked[use.end() : stop]
        for rename in re.finditer(r"(\$?\w+)\s+as\s+(\$?\w+)", tree):
            item, alias = rename.groups()
            if item == "self":
                item = _group_parent(tree, rename.start())
            if "$" in item.replace("$crate", "") or "$" in alias:
                fail(
                    f"a use declaration renames through a macro metavariable "
                    f"(`{rename.group(0)}`); the name it binds is chosen at the call site, "
                    f"so this scan cannot tell whether it is an alias for `{trait}`"
                )
            # `as _` binds no name, so nothing can be written under it.
            if item == trait and alias != "_":
                out.add(alias)
    return out


def _refuse_code_outside_the_scan(root: Path) -> None:
    """Fail unless every compiled production source is under `crates/*/src`.

    The implementor enumeration reads `crates/*/src/**/*.rs` and claims the set
    it finds is closed. Rust compiles code from elsewhere, and each route below
    put an implementor where the scan never looked: a workspace member outside
    `crates/`, a root package, a build script (its output is `include!`d), a
    Cargo target whose `path` leaves `src/`, and a proc-macro crate, which can
    emit an impl assembled from strings. None is used by this tree, so each is
    refused rather than followed. (`#[path]` and `include!` are refused per file,
    where the scan reads them.)
    """
    workspace = root / "Cargo.toml"
    if not workspace.is_file():
        fail(f"no workspace Cargo.toml under {root}: this scan cannot see which crates compile")
    manifest = tomllib.loads(workspace.read_text(encoding="utf-8"))
    if "package" in manifest:
        fail(
            "the workspace root is itself a package, compiled from sources outside "
            "crates/*/src that this scan cannot see"
        )
    _refuse_dependencies_outside_the_scan(manifest, root, root, "the workspace root")
    for member in manifest.get("workspace", {}).get("members", []):
        if not re.fullmatch(r"crates/(?:\*|[\w.-]+)", member):
            fail(f"workspace member `{member}` is outside crates/, so this scan cannot see it")
    # rustc follows a symlinked module file or directory; `Path.rglob` does not
    # descend into a symlinked directory, so an implementor behind one was never
    # read (red team, after r31). None is used by this tree.
    for dirpath, dirnames, filenames in os.walk(root / "crates"):
        for name in (*dirnames, *filenames):
            link = Path(dirpath) / name
            if link.is_symlink():
                fail(
                    f"{link.relative_to(root)} is a symlink; rustc follows it and this "
                    "scan cannot see what it points at"
                )
    for crate in sorted(p for p in (root / "crates").iterdir() if p.is_dir()):
        if (crate / "build.rs").exists():
            fail(f"{crate.name} has a build script, whose generated code this scan cannot see")
        toml_path = crate / "Cargo.toml"
        if not toml_path.is_file():
            continue  # not a crate: cargo compiles nothing here
        crate_manifest = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        _refuse_dependencies_outside_the_scan(crate_manifest, crate, root, crate.name)
        if crate_manifest.get("package", {}).get("build", False) is not False:
            fail(f"{crate.name} declares a build script, whose output this scan cannot see")
        lib = crate_manifest.get("lib", {})
        if lib.get("proc-macro") or lib.get("proc_macro"):
            fail(
                f"{crate.name} is a proc-macro crate; the code it emits is assembled at "
                "compile time, where this scan cannot see it"
            )
        for target in [lib, *crate_manifest.get("bin", [])]:
            target_path = target.get("path")
            if target_path is None:
                continue
            parts = Path(target_path).parts
            if not parts or parts[0] != "src" or ".." in parts:
                fail(
                    f"{crate.name} compiles `{target_path}`, outside src/, so this scan "
                    "cannot see it"
                )


def _attributes(masked: str) -> Iterator[str]:
    """The text of every `#[...]` / `#![...]` attribute in `masked`, bracket-balanced."""
    for start in re.finditer(r"#\s*!?\s*\[", masked):
        depth = 0
        for end in range(start.end() - 1, len(masked)):
            if masked[end] in "([{":
                depth += 1
            elif masked[end] in ")]}":
                depth -= 1
                if depth == 0:
                    break
        yield masked[start.start() : end + 1]


def _refuse_dependencies_outside_the_scan(
    manifest: dict[str, Any], manifest_dir: Path, root: Path, label: str
) -> None:
    """A dependency is compiled code too, so it must be a scanned crate.

    A path dependency outside `crates/` (`evil = { path = "../../evil" }`), in
    any dependency table including a target-specific one or the workspace's, is
    a crate that can export an always-admitting gate this scan never reads; a git
    dependency or a `[patch]` / `[replace]` swaps in source from elsewhere. None
    is used by this tree.
    """
    for section in ("patch", "replace"):
        if section in manifest:
            fail(f"{label} has a [{section}] section, which swaps in source this scan cannot see")
    tables = [manifest.get(key, {}) for key in _DEPENDENCY_TABLES]
    for target in manifest.get("target", {}).values():
        tables += [target.get(key, {}) for key in _DEPENDENCY_TABLES]
    tables.append(manifest.get("workspace", {}).get("dependencies", {}))
    crates_dir = (root / "crates").resolve()
    for table in tables:
        for name, dependency in table.items():
            if not isinstance(dependency, dict):
                continue
            if "git" in dependency:
                fail(f"{label} depends on `{name}` from git, source this scan cannot see")
            if "path" in dependency:
                resolved = (manifest_dir / dependency["path"]).resolve()
                if resolved.parent != crates_dir:
                    fail(
                        f"{label} depends on `{name}` at `{dependency['path']}`, outside "
                        "crates/, so this scan cannot see it"
                    )


_DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")


def _enclosing_macro(masked: str, at: int) -> str | None:
    """The attribute or macro invocation whose delimiters enclose `at`, if any.

    Walks outward through every unmatched `(`, `[` or `{`. One opened by `#[`
    or `#![` is an attribute; one right after `name!` is that macro's input. A
    `macro_rules! name {` body is neither: its `{` follows the name, not a `!`,
    and an `impl` written there is read like any other.
    """
    depth = 0
    for i in range(at - 1, -1, -1):
        ch = masked[i]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth:
                depth -= 1
                continue
            before = masked[:i].rstrip()
            if ch == "[" and (before.endswith("#") or before.endswith("#!")):
                return "an attribute"
            invoked = re.search(r"(\w+)\s*!$", before)
            if invoked and not before.endswith("macro_rules!"):
                return f"the macro `{invoked.group(1)}!`"
    return None


def _group_parent(tree: str, at: int) -> str:
    """The path segment a `{` group at `at` hangs from: `Trait` in `Trait::{self}`.

    Walks back to the group's unmatched `{`, then over `::` to the segment. A
    `self` with no parent segment cannot be resolved here, so it is refused.
    """
    depth = 0
    for i in range(at - 1, -1, -1):
        if tree[i] == "}":
            depth += 1
        elif tree[i] == "{":
            if depth:
                depth -= 1
                continue
            parent = re.search(r"(\w+)\s*::\s*$", tree[:i])
            if parent:
                return parent.group(1)
            break
    fail(
        f"a use declaration renames `self` (`{tree.strip()}`) with no path segment "
        "in front of its group; this scan cannot tell what it renames"
    )


def _strip_generic_args(text: str) -> str:
    """Drop `<...>` spans so type PARAMETERS are not read as type names.

    `ScheduledRestartConnectivity<P, C>` must yield one name, not three. Reads
    masked text (`_mask_non_code`).

    NOT the `>` of a `->`: closing on it made depth fall to 0 inside
    `Wrapper<fn() -> EpochNanos>`, so ` EpochNanos` was emitted at depth 0 and
    reported as an undeclared production implementor. And not a `>` inside a
    const-generic BRACE either: `Wrapper<{ N > 1 }>` closes nothing until its
    braces do (review r27).
    """
    out, angle, brace, nest, prev = [], 0, 0, 0, ""
    for ch in text:
        if angle and ch == "{":
            brace += 1
        elif brace and ch == "}":
            brace -= 1
        elif not brace and ch in "([":
            nest += 1
            if angle == 0:
                out.append(ch)
        elif not brace and ch in ")]":
            nest = max(0, nest - 1)
            if angle == 0:
                out.append(ch)
        # _ANGLE_RULE: inside `(..)` / `[..]` an angle bracket is a comparison.
        elif not brace and not nest and ch == "<":
            angle += 1
        elif not brace and not nest and ch == ">" and prev != "-":
            angle = max(0, angle - 1)
        elif angle == 0:
            out.append(ch)
        prev = ch
    return "".join(out)


def _impl_headers(masked: str) -> list[str]:
    """The header of every `impl` ITEM in `masked`: the text between `impl` and `{`/`;`.

    Bounded by bracket DEPTH, not by a pattern (adversarial review r27). Two
    regexes - a strict match and a "loose" backstop - both assumed no `{` could
    precede the trait name. Rust permits one inside a const-generic argument,
    `impl<T: HasWidth<{ 1 + 1 }>> RestartWindowGate for AlwaysOpen<T>`, so both
    missed it, their counts agreed, and the backstop never fired. Seven earlier
    defects in this scan were the same shape: a pattern that did not anticipate
    a legal header. A walker has no shapes to anticipate: an impl's header ends
    at the first `{` or `;` with every `<`, `(`, `[` closed, and a brace opened
    INSIDE one of those is a const expression, counted until it closes.

    An `impl` that closes a bracket it never opened is `impl Trait` in a TYPE
    position (`fn f(x: impl Gate, y: u8)` meets the `)`), not an impl item. A
    top-level `,` is NOT that signal: a where clause has them (`where P: X, C: Y`).
    A header that runs to end of file cannot be bounded, so it fails.
    """
    headers: list[str] = []
    for match in re.finditer(r"\bimpl\b", masked):
        angle = paren = bracket = brace = 0
        prev, end, item = "", None, True
        j = match.end()
        while j < len(masked):
            ch = masked[j]
            if brace:
                if ch == "{":
                    brace += 1
                elif ch == "}":
                    brace -= 1
            elif ch in "{;" and angle == paren == bracket == 0:
                end = j
                break
            elif ch == "{":
                brace += 1
            # _ANGLE_RULE: inside `(..)` / `[..]` an angle bracket is a comparison.
            elif ch == "<" and paren == bracket == 0:
                angle += 1
            elif ch == ">" and prev != "-" and paren == bracket == 0:
                angle -= 1
            elif ch == "(":
                paren += 1
            elif ch == ")":
                paren -= 1
            elif ch == "[":
                bracket += 1
            elif ch == "]":
                bracket -= 1
            if min(angle, paren, bracket) < 0:
                item = False
                break
            prev = ch
            j += 1
        if not item:
            continue
        if end is None:
            fail(
                "an `impl` header runs to end of file without a `{` or `;` this scan can "
                "bound; a header it cannot bound is one it cannot classify"
            )
        headers.append(masked[match.end() : end])
    return headers


def _top_level_keyword(text: str, word: str) -> int:
    """Index of `word` as a whole word OUTSIDE every bracket in `text`, or -1."""
    angle = paren = bracket = brace = 0
    prev = ""
    for k, ch in enumerate(text):
        if brace:
            brace += {"{": 1, "}": -1}.get(ch, 0)
        elif ch == "{":
            brace += 1
        # _ANGLE_RULE: inside `(..)` / `[..]` an angle bracket is a comparison.
        elif ch == "<" and paren == bracket == 0:
            angle += 1
        elif ch == ">" and prev != "-" and paren == bracket == 0:
            angle -= 1
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket -= 1
        elif (
            angle == paren == bracket == 0
            and text.startswith(word, k)
            and (k == 0 or not (text[k - 1].isalnum() or text[k - 1] == "_"))
            and not (
                text[k + len(word) : k + len(word) + 1].isalnum()
                or text[k + len(word) : k + len(word) + 1] == "_"
            )
        ):
            return k
        prev = ch
    return -1


def _split_impl_header(header: str) -> tuple[str, str] | None:
    """`(trait, target)` for a trait impl header; None for an inherent impl.

    The impl's own generic list is skipped by depth, then the header splits at
    the first TOP-LEVEL `for`. A `for` inside a bound (`for<'a> Fn(&'a u8)`) is
    inside brackets, and one after a top-level `where` belongs to the where
    clause, so neither is the split.
    """
    text = header.lstrip()
    if text.startswith("<"):
        angle, brace, paren, bracket, prev, close = 0, 0, 0, 0, "", -1
        for index, ch in enumerate(text):
            if brace:
                brace += {"{": 1, "}": -1}.get(ch, 0)
            elif ch == "{":
                brace += 1
            elif ch in "([":
                paren, bracket = paren + (ch == "("), bracket + (ch == "[")
            elif ch in ")]":
                paren, bracket = paren - (ch == ")"), bracket - (ch == "]")
            # _ANGLE_RULE: inside `(..)` / `[..]` an angle bracket is a comparison.
            elif ch == "<" and paren == bracket == 0:
                angle += 1
            elif ch == ">" and prev != "-" and paren == bracket == 0:
                angle -= 1
                if angle == 0:
                    close = index
                    break
            prev = ch
        if close < 0:
            # An impl generic list that never closes. Skipping the header would
            # be the fail-open this walker replaced; refuse instead.
            fail(f"cannot find the end of the impl generic list in `impl{header.rstrip()}`")
        text = text[close + 1 :]
    for_at = _top_level_keyword(text, "for")
    where_at = _top_level_keyword(text, "where")
    if for_at < 0 or 0 <= where_at < for_at:
        return None
    target = text[for_at + 3 :]
    target_where = _top_level_keyword(target, "where")
    if target_where >= 0:
        target = target[:target_where]
    return text[:for_at], target


def check_restart_window_gate_implementors(config: dict, _unused: str, root: Path = ROOT) -> str:
    """No PRODUCTION type may implement the gate except the declared producer.

    The port's own rustdoc used to argue that taking a port rather than a
    boolean closed a forgery hole. It does not, and the reviewer was right to
    say so: an `impl RestartWindowGate` that always returns `Admitted` is
    exactly as forgeable as `true`, three test files write one, and nothing
    stopped a production type doing the same — every static guard, the L7 suite
    and all three CLI proofs would have stayed green while SYS-75(a) was
    bypassed. The only defence was a comment saying the test double is "not
    exported", and this feature's own history says a comment is not a guard.

    What the port DOES buy is freshness: the fact is read at the instant the
    decision is made rather than whenever a caller last looked. Unforgeability
    has to come from somewhere else, and this is it — the set of production
    implementors is enumerated from the source and must match the contract.

    Test doubles are excluded by construction: `tests/` files are not shipped,
    and a `#[cfg(test)]` module is cut before scanning.
    """
    block = restart_window_block(config)
    spec = block["gate_port"]
    declared = set(spec["production_implementors"])
    sites: dict[str, str] = spec["production_implementor_sites"]
    if set(sites) != declared:
        fail(
            f"the `{spec['trait']}` contract declares implementors {sorted(declared)} but "
            f"sites for {sorted(sites)}; each declared implementor needs exactly one site"
        )
    # Every impl is its own OCCURRENCE: (bare type name, file). Keying by name
    # alone let a namesake overwrite the producer's entry (review r30).
    occurrences: list[tuple[str, str]] = []
    # EVERY workspace crate, not a hard-coded four. The first version listed the
    # four crates that had the trait in view at the time; a fifth gaining an
    # atp-market-data dependency later would have been unscanned, and the
    # contract claims this walks the sources.
    crate_roots = sorted((root / "crates").glob("*/src")) if (root / "crates").is_dir() else []
    if not crate_roots:
        fail("no crate sources found under crates/*/src — this scan cannot bound anything")
    _refuse_code_outside_the_scan(root)
    # Aliases are collected across the WHOLE tree first, because a rename can be
    # two hops: `pub use RestartWindowGate as Gate;` in one module, then
    # `use crate::gates::Gate; impl Gate for AlwaysOpen` in another. Reading
    # only the file being scanned found no implementor under the alias,
    # and reported a clean set - while the playbook recorded the rename class as
    # closed.
    crate_aliases: set[str] = set()
    sources: dict = {}
    for crate_dir in crate_roots:
        for path in sorted(crate_dir.rglob("*.rs")):
            src = _code_only(_without_test_module(path.read_text(encoding="utf-8")))
            sources[path] = src
            crate_aliases |= _local_aliases(src, spec["trait"])
    # A second hop renames an ALIAS, so close over what we just found.
    for _ in range(8):
        grown = set(crate_aliases)
        for src in sources.values():
            for alias in list(crate_aliases):
                grown |= _local_aliases(src, alias)
        if grown == crate_aliases:
            break
        crate_aliases = grown
    else:
        # STILL GROWING after the last pass. Falling through here would scan
        # with an incomplete alias set and report a clean tree - the same
        # fail-open shape `_skip_generic_list` was changed to stop having. A
        # bounded closure that has not converged has not answered the question.
        fail(
            f"the `{spec['trait']}` alias closure did not converge after 8 passes "
            f"({len(crate_aliases)} alias(es) so far). A rename chain this long is "
            "either a mistake or an attack; either way this scan cannot bound the "
            "implementor set until it settles"
        )
    for path, source in sources.items():
        # The trait may be RENAMED at the use site, in this file or two hops
        # away: `use atp_market_data::RestartWindowGate as Gate; impl Gate for
        # AlwaysOpen`. Search for every name the trait answers to.
        names = {spec["trait"], *crate_aliases, *_local_aliases(source, spec["trait"])}
        masked = _mask_non_code(source)
        where = path.relative_to(root)
        # `include` as a WORD, not `include!`: `use std::include as splice;` calls it
        # under another name. And `path =` anywhere in an attribute, not just
        # `#[path`: `#[cfg_attr(not(test), path = "..")]` is the same redirect.
        include = re.search(r"\binclude\b", masked)
        redirect = next((a for a in _attributes(masked) if re.search(r"\bpath\s*=", a)), None)
        unseen = include.group(0) if include else redirect
        if unseen:
            fail(
                f"{where} uses `{unseen}`, which compiles a file this scan cannot "
                f"see; an implementor of `{spec['trait']}` there would go uncounted"
            )
        for name in sorted(names):
            # Searched in `source`, where string literals are KEPT: a proc macro
            # can be handed the name as a string (`#[delegate(target = "Gate")]`)
            # and parse it into the impl it emits.
            for hit in re.finditer(rf"\b{re.escape(name)}\b", source):
                macro = _enclosing_macro(masked, hit.start())
                if macro:
                    fail(
                        f"{where} hands `{name}` (the `{spec['trait']}` gate) to {macro}; a "
                        "macro given the gate's name can implement it where this scan "
                        "cannot look"
                    )
        for header in _impl_headers(_mask_non_code(source)):
            split = _split_impl_header(header)
            if split is None:
                continue
            trait_part, target = split
            if "$" in trait_part:
                # A macro that takes the trait as an argument can implement the
                # gate under a name no scan of the definition can see.
                fail(
                    f"{path.relative_to(root)} has a macro-generated impl whose trait is "
                    f"a metavariable (`impl{header.rstrip()}`); this scan cannot tell "
                    f"whether it implements `{spec['trait']}`, so it cannot bound the set"
                )
            idents = re.findall(r"[A-Za-z_]\w*", _strip_generic_args(trait_part))
            if not idents or idents[-1] not in names:
                continue
            type_names = re.findall(r"\b([A-Z]\w*)", _strip_generic_args(target))
            if not type_names:
                # An impl target with no uppercase identifier - a primitive
                # (`impl Gate for i64`), a lowercase type alias, a macro's `$t` -
                # collected NOTHING, so the "closed set" stayed open while the
                # check reported a clean tree. Unnameable is not absent
                # (CLAUDE.md rule 3): report the raw span.
                fail(
                    f"`{spec['trait']}` is implemented for `{target.strip()}` in "
                    f"{path.relative_to(root)}, whose type this scan cannot name. "
                    "An implementor it cannot name is one it cannot bound, so this "
                    "fails rather than reporting a closed set"
                )
            # The WHOLE target, generics dropped and spacing removed, is the
            # identity. `bypass::ScheduledRestartConnectivity` is a different type
            # from the producer, and keeping its qualifier is what stops it being
            # filed under the producer's name (review r30).
            identity = re.sub(r"\s+", "", _strip_generic_args(target))
            occurrences.append((identity, str(path.relative_to(root))))
    found: dict[str, list[str]] = {}
    for name, where in occurrences:
        found.setdefault(name, []).append(where)
    if not found:
        fail(
            f"no production type implements `{spec['trait']}` — either the producer was "
            "removed or this scan no longer matches it, and a scan that finds nothing "
            "must fail rather than report a clean tree"
        )
    undeclared = sorted(set(found) - declared)
    if undeclared:
        fail(
            f"undeclared production implementor(s) of `{spec['trait']}`: "
            + ", ".join(f"{name} ({', '.join(found[name])})" for name in undeclared)
            + ". An implementor that always admits bypasses the SyRS SYS-75(a) "
            "suspension with every other guard still green, so the production set is "
            "enumerated here rather than trusted"
        )
    missing = sorted(declared - set(found))
    if missing:
        fail(
            f"declared implementor(s) {', '.join(missing)} no longer implement "
            f"`{spec['trait']}` — the contract and the source disagree"
        )
    for name in sorted(found):
        if found[name] != [sites[name]]:
            fail(
                f"`{spec['trait']}` is implemented for {name} at {', '.join(found[name])}; "
                f"the contract allows exactly one impl, at {sites[name]}. A second impl "
                "for the same name is a namesake, not the producer"
            )
    return (
        f"exactly {len(found)} production type implements `{spec['trait']}` "
        f"({', '.join(f'{name}, once, at {sites[name]}' for name in sorted(found))}) — "
        "the port buys freshness; `Admitted` itself is unforgeable by type "
        "(AdmissionProof), and this enumeration bounds which types may ask the window"
    )


def check_reachability_seam_is_unpinned(config: dict, reachability_src: str) -> str:
    """The probe must NOT live in the digest-pinned transport module.

    `tools/ib_adapter_check.py` SHA-256s `interactive_brokers.rs` against the
    operator's live paper-account evidence, so a probe added there would flip a
    closed-green feature red and need a fresh live run to recover.
    """
    block = restart_window_block(config)
    spec = block["reachability_port"]
    if "interactive_brokers" in spec["module"]:
        fail(
            "the reachability seam is declared inside the digest-pinned transport "
            "module; it must be its own file (see connection_control.rs)"
        )
    try:
        trait_body = _trait_body(reachability_src, spec["trait"])
    except AssertionError as error:
        fail(str(error))
    for method in spec["methods"]:
        if f"fn {method}(" not in trait_body:
            fail(f"{spec['trait']} is missing the `{method}` method")
    code = _code_only(reachability_src)
    if "to_socket_addrs" in code:
        fail(
            "the reachability probe resolves a hostname — a blocking getaddrinfo sits "
            "OUTSIDE connect_timeout's deadline, so the bound would be a suggestion"
        )
    if "connect_timeout" not in code:
        fail(
            "the reachability probe has no explicit connect deadline — a bare "
            "TcpStream::connect hangs on the OS default"
        )
    return (
        f"atp-adapters::{spec['trait']} lives outside the digest-pinned transport module, "
        "probes with an explicit connect deadline, and never resolves a hostname"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = connectivity_block(config)
    crate = block["execution_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
    result = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"cargo test -p {crate} failed:\n{result.stdout}\n{result.stderr}")
    combined = result.stdout + result.stderr
    if "test result: ok" not in combined and "0 failed" not in combined:
        fail(f"cargo test output did not include `test result: ok`:\n{combined}")
    integ = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            crate,
            "--test",
            "err_2_connectivity_blocked",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test err_2_connectivity_blocked failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + err_2_connectivity_blocked: PASS "
        "(connectivity-gated rejection + zero broker side effect verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("connectivity_state", check_connectivity_state_enum, "types"),
    ("connectivity_event", check_connectivity_event_struct, "types"),
    ("brokerage_connectivity_port", check_brokerage_connectivity_port, "execution"),
    ("connectivity_event_sink_port", check_connectivity_event_sink_port, "execution"),
    ("connectivity_guard", check_connectivity_guard_in_submit_live_order, "execution"),
    # SRS-MD-005 — the producer of ConnectivityState::ScheduledRestartWindow.
    ("restart_phase", check_restart_phase_enum, "types"),
    ("market_data_admission", check_market_data_admission_enum, "types"),
    ("restart_window_defaults", check_restart_window_defaults, "types"),
    ("restart_escalation", check_restart_escalation_arm, "types"),
    ("market_data_admission_sites", check_market_data_admission_sites, "market_data"),
    ("restart_window_producer", check_restart_window_producer, "producer"),
    ("reachability_seam", check_reachability_seam_is_unpinned, "reachability"),
    ("restart_window_gate_implementors", check_restart_window_gate_implementors, "types"),
)


def _sources(config: dict, root: Path = ROOT) -> dict[str, str]:
    block = connectivity_block(config)
    restart = restart_window_block(config)
    types_src = f"{block['types_crate']['path']}/src"
    _check_cargo_compiles_the_files_read(
        root,
        (
            f"{types_src}/lib.rs",
            f"{types_src}/admission.rs",
            f"{block['execution_crate']['path']}/src/lib.rs",
            f"{restart['market_data_crate']['path']}/src/lib.rs",
            restart["admission_sites"]["module"],
            restart["producer"]["module"],
            restart["reachability_port"]["module"],
        ),
    )
    return {
        "types": types_source(config, root),
        "execution": execution_source(config, root),
        "market_data": market_data_source(config, root),
        "producer": producer_source(config, root),
        "reachability": reachability_source(config, root),
    }


def run_checks() -> list[str]:
    config = load_config()
    sources = _sources(config)
    evidence = [check(config, sources[scope]) for _, check, scope in _STATIC_CHECKS]
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def _accepts_root(check) -> bool:
    return "root" in inspect.signature(check).parameters


def assert_connectivity_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo).

    `root` reaches EVERY check that can take it, decided from the signature
    rather than from a list somebody has to remember to update. It previously
    reached only `_sources`, so `check_restart_window_gate_implementors` - which
    walks the tree itself rather than reading a prepared source string - scanned
    the real repository while its twelve siblings scanned the caller's. That is
    the same finding round 14 recorded as fixed and left half-done: the
    parameter was added to the check and never passed to it.
    """
    sources = _sources(config, root)
    return [
        check(config, sources[scope], root=root)
        if _accepts_root(check)
        else check(config, sources[scope])
        for _, check, scope in _STATIC_CHECKS
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERR-2 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except ConnectivityCheckError as error:
        print(f"ERR-2 FAIL: {error}", file=sys.stderr)
        return 1

    print("ERR-2 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
