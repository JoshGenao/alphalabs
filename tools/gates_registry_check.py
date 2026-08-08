#!/usr/bin/env python3
"""Validate tools/gates.json against what is actually on disk.

The registry is only a single source of truth if it cannot silently fall out of
step with the check corpus. Before it existed, three runners carried three
hand-maintained lists and 21 checks were enforced nowhere but ``init.sh``; a new
``tools/<name>_check.py`` joined none of them unless its author remembered to
edit three files. This check makes that omission a CI failure.

It asserts:

1. every ``tools/*_check.py`` is either registered in ``checks`` or declared in
   ``excluded`` with a reason — a new check must make a conscious scope decision;
2. every registered/excluded name resolves to a file that exists — so a rename or
   deletion cannot leave a runner invoking a missing module;
3. no name appears twice, in either list or across both;
4. every ``scope`` is one of the declared scopes, and every entry has a non-empty
   ``why``.

Run directly (ci.yml runs this BEFORE verify_contracts.sh, deliberately: a broken
registry must be caught by something that does not itself read the registry):

    python3 tools/gates_registry_check.py
    python3 tools/gates_registry_check.py --json

Exit codes: 0 = registry is coherent, 1 = at least one problem, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
REGISTRY = TOOLS / "gates.json"


class RegistryError(Exception):
    """The registry does not describe the checks that are actually on disk."""


def discover_checks() -> set[str]:
    """Every standalone check module on disk, by bare name."""
    return {p.stem for p in TOOLS.glob("*_check.py")}


def load_registry(path: Path = REGISTRY) -> dict:
    if not path.exists():
        raise RegistryError(f"{path.relative_to(ROOT)} is missing — no gate registry to run")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # A JSON file is valid Python syntax; `ruff format` on an explicit path
        # will happily rewrite it into something that is not JSON (CLAUDE.md r8).
        raise RegistryError(f"{path.name} is not valid JSON: {exc}") from exc


def audit(registry: dict, on_disk: set[str]) -> list[str]:
    """Return every problem found. Empty list means the registry is coherent."""
    problems: list[str] = []

    valid_scopes = set(registry.get("scopes", {}))
    if not valid_scopes:
        problems.append("gates.json declares no `scopes` block")

    checks = registry.get("checks", [])
    excluded = registry.get("excluded", [])

    seen: dict[str, str] = {}
    for entry, where in [(c, "checks") for c in checks] + [(e, "excluded") for e in excluded]:
        name = entry.get("name", "")
        if not name:
            problems.append(f"an entry in `{where}` has no `name`")
            continue
        if name in seen:
            problems.append(f"{name} is listed twice ({seen[name]} and {where})")
            continue
        seen[name] = where
        if not (entry.get("why") or "").strip():
            problems.append(f"{name} ({where}) has no `why` — every entry states its purpose")
        if not (TOOLS / f"{name}.py").exists():
            problems.append(f"{name} is registered in `{where}` but tools/{name}.py does not exist")

    for entry in checks:
        name = entry.get("name", "?")
        scopes = entry.get("scopes")
        if not isinstance(scopes, dict) or not scopes:
            problems.append(f"{name} declares no `scopes` object")
            continue
        for scope, argv in scopes.items():
            if scope not in valid_scopes:
                problems.append(
                    f"{name} has scope {scope!r}; expected one of {sorted(valid_scopes)}"
                )
            if not isinstance(argv, list) or any(not isinstance(a, str) for a in argv):
                problems.append(f"{name} scope {scope!r} argv must be a list of strings")
                continue
            # verify_contracts.sh word-splits these, so an arg containing
            # whitespace would silently become two arguments.
            for arg in argv:
                if arg != arg.strip() or " " in arg or "\t" in arg:
                    problems.append(
                        f"{name} scope {scope!r} argv {arg!r} contains whitespace; "
                        f"the runner word-splits and would mis-pass it"
                    )

    # The load-bearing assertion: a new check cannot join the tree unnoticed.
    for missing in sorted(on_disk - set(seen)):
        problems.append(
            f"tools/{missing}.py exists but is in neither `checks` nor `excluded` — "
            f"register it with a scope, or exclude it with a reason"
        )

    return problems


def scoped(registry: dict, scope: str) -> list[str]:
    """`name arg…` lines for a scope, in registry order.

    Emitted whitespace-separated because verify_contracts.sh word-splits them;
    `audit` rejects any arg containing whitespace so that stays safe.
    """
    out = []
    for c in registry.get("checks", []):
        scopes = c.get("scopes") or {}
        if scope in scopes:
            out.append(" ".join([c["name"], *scopes[scope]]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--scope", help="print the check names for a scope and exit (used by verify_contracts.sh)"
    )
    ap.add_argument(
        "--list-scopes",
        action="store_true",
        help="print every declared scope name (verify_contracts.sh --scope all iterates these)",
    )
    args = ap.parse_args(argv)

    try:
        registry = load_registry()
    except RegistryError as exc:
        print(f"✗ gates registry: {exc}", file=sys.stderr)
        return 1

    if args.list_scopes:
        print("\n".join(sorted(registry.get("scopes", {}))))
        return 0

    if args.scope:
        names = scoped(registry, args.scope)
        if not names:
            print(
                f"✗ gates registry: no checks registered for scope {args.scope!r}", file=sys.stderr
            )
            return 1
        print("\n".join(names))
        return 0

    on_disk = discover_checks()
    problems = audit(registry, on_disk)
    registered = len(registry.get("checks", []))

    if args.json:
        print(
            json.dumps(
                {"problems": problems, "registered": registered, "on_disk": len(on_disk)}, indent=2
            )
        )
        return 1 if problems else 0

    if problems:
        print(f"✗ gates registry: {len(problems)} problem(s)")
        for p in problems:
            print(f"    · {p}")
        return 1

    print(
        f"✓ gates registry: {registered} check(s) registered, "
        f"{len(registry.get('excluded', []))} excluded, {len(on_disk)} on disk — coherent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
