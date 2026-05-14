"""Shared Rust-source brace-matching helpers for the tools/*_check.py scripts.

Hoisted out of the per-feature check scripts (SESSION 16 named the trigger
condition: hoist when the 5th ERR-* feature reuses these helpers; the
7th feature SRS-ORCH-001 actually performed the lift). The helpers are
deliberately small, pure, and depend only on ``re`` and the
``RustParserError`` raised below — no script-specific state.

Callers catch ``AssertionError`` (or its ``RustParserError`` subclass)
and surface the failure through their own ``fail()`` reporter.
"""

from __future__ import annotations

import re


class RustParserError(AssertionError):
    """Raised when a helper cannot locate or close the requested construct."""


def _fail(message: str) -> None:
    raise RustParserError(message)


def _fn_block(source: str, fn_name: str) -> str:
    """Return the body of ``pub fn <fn_name>`` up to its closing brace."""
    match = re.search(rf"\bpub\s+fn\s+{re.escape(fn_name)}\b[^\{{]*\{{", source)
    if not match:
        _fail(f"Rust source is missing function `{fn_name}`")
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
        _fail(f"could not parse function body for `{fn_name}`")
    return source[start : index - 1]


def _match_arm(body: str, pattern: str) -> str:
    """Return the body of the match arm whose pattern matches ``pattern``.

    Looks for ``<pattern> =>`` and returns the expression up to the next
    top-level ``,`` (skipping nested braces, parens, and string literals).
    """
    arm_match = re.search(rf"{re.escape(pattern)}\s*=>\s*", body)
    if not arm_match:
        _fail(f"match body is missing arm for `{pattern}`")
    start = arm_match.end()
    depth = 0
    index = start
    in_string = False
    string_char = ""
    while index < len(body):
        char = body[index]
        if in_string:
            if char == "\\" and index + 1 < len(body):
                index += 2
                continue
            if char == string_char:
                in_string = False
        elif char in ('"', "'"):
            in_string = True
            string_char = char
        elif char == "{" or char == "(":
            depth += 1
        elif char == "}" or char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            break
        index += 1
    return body[start:index]


def _variant_arm(body: str, variant_token: str) -> str:
    """Return the body of the match arm whose pattern starts with
    ``variant_token`` (e.g. ``LaunchReadiness::ReadyWithinDeadline``).
    Handles both unit variants (``Variant =>``) and struct variants
    (``Variant { field, .. } =>``). The arm body is everything up to
    the next top-level ``,`` or the closing ``}`` of the match.
    """
    pattern = re.compile(
        rf"{re.escape(variant_token)}\s*(?:\{{[^}}]*\}})?\s*=>\s*",
        re.DOTALL,
    )
    arm_match = pattern.search(body)
    if arm_match is None:
        _fail(f"match body is missing arm for `{variant_token}`")
    start = arm_match.end()
    depth = 0
    index = start
    in_string = False
    string_char = ""
    while index < len(body):
        char = body[index]
        if in_string:
            if char == "\\" and index + 1 < len(body):
                index += 2
                continue
            if char == string_char:
                in_string = False
        elif char in ('"', "'"):
            in_string = True
            string_char = char
        elif char in ("{", "("):
            depth += 1
        elif char in ("}", ")"):
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            break
        index += 1
    return body[start:index]


def _trait_body(source: str, name: str) -> str:
    """Return the body of ``pub trait <name>`` between braces."""
    match = re.search(rf"\bpub\s+trait\s+{re.escape(name)}\b[^\{{]*\{{", source)
    if not match:
        _fail(f"Rust source is missing public trait `{name}`")
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
        _fail(f"could not parse trait body for `{name}`")
    return source[start : index - 1]


def _struct_body(source: str, struct: str) -> str:
    """Return the body of ``pub struct <struct>`` between braces."""
    match = re.search(rf"\bpub\s+struct\s+{re.escape(struct)}\b[^\{{]*\{{", source)
    if not match:
        _fail(f"Rust source is missing public struct `{struct}`")
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
        _fail(f"could not parse struct body for `{struct}`")
    return source[start : index - 1]


def _enum_body(source: str, name: str) -> str:
    """Return the body of ``pub enum <name>`` between braces."""
    match = re.search(rf"\bpub\s+enum\s+{re.escape(name)}\b[^\{{]*\{{", source)
    if not match:
        _fail(f"Rust source is missing public enum `{name}`")
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
        _fail(f"could not parse enum body for `{name}`")
    return source[start : index - 1]
