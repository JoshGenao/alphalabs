#!/usr/bin/env python3
"""Prove the new tests would fail without the change they claim to cover.

``CLAUDE.md`` rule 6: *a test that cannot fail is not evidence. Mutation-verify every
regression test: remove the fix, watch the test go red, restore it.* Until now that
was enforced by asking. It is the rule most worth mechanising, because the failure it
prevents is invisible: a passing suite that proves nothing looks exactly like a
passing suite that proves everything.

This session shipped the defect the rule describes while quoting the rule — a
parametrize id collided with a pytest marker name and two of three guard cases were
silently skipped, in a run that reported "832 passed".

How it works: revert the range's SOURCE hunks (test files untouched), run the tests,
and require every test function ADDED in the range to fail. Then restore, always.

    tools/mutation_verify.py origin/main..HEAD --tests tests/unit/test_evidence.py
    tools/mutation_verify.py origin/main..HEAD --tests tests/unit/ --json

Exit codes: 0 = every added test failed without the change (they are evidence),
1 = at least one still passed (it is not), 2 = usage/internal error.

ADVISORY in CI by design. A blocking version produces false failures on refactors,
on multi-commit branches, and on tests whose subject is deleted rather than changed.
The agent runs it at Step 6.6 and records the result with `evidence.py gate`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ADDED_TEST_RE = re.compile(r"^\+\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)
TEST_PATH_RE = re.compile(r"(^|/)(tests?/|conftest\.py$|test_[^/]*\.py$|[^/]*_test\.py$)")


def _git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def changed_files(rng: str) -> tuple[list[str], list[str], list[str]]:
    """(modified_sources, added_sources, test_files) for the range.

    Added and modified sources revert differently, and getting that wrong is not a
    detail: `git checkout <base> -- <path>` fails outright for a file the base does
    not have, which aborts the whole run. An added file is reverted by DELETING it.
    """
    mods, adds, tsts = [], [], []
    for line in _git("diff", "--name-status", "--no-renames", rng).splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        path = path.strip()
        if not path or not (ROOT / path).exists():
            continue  # deleted by the range; nothing to revert
        if is_test_path(path):
            tsts.append(path)
        elif status.startswith("A"):
            adds.append(path)
        else:
            mods.append(path)
    return mods, adds, tsts


def added_tests(rng: str, test_files: list[str]) -> dict[str, list[str]]:
    """Test functions ADDED by this range, per file."""
    out: dict[str, list[str]] = {}
    for f in test_files:
        diff = _git("diff", rng, "--", f)
        names = ADDED_TEST_RE.findall(diff)
        if names:
            out[f] = sorted(set(names))
    return out


def run_tests(targets: list[str], names: list[str]) -> dict[str, str]:
    """Run the named tests. Returns {test_name: "failed"|"passed"|"skipped"|"uncollectable"}.

    The three outcomes are not interchangeable:

    * ``failed``       — the test noticed its subject was gone. It is evidence.
    * ``uncollectable``— the module would not even import, because the change ADDED
      the module under test. That is the strongest possible dependence, not a
      failure of the check. Treating it as "did not fail" flagged all 21 tests of a
      commit that introduced tools/evidence.py.
    * ``passed``/``skipped`` — the test does not depend on the change, or never ran
      at all. Both mean it cannot be evidence; a silent skip is exactly how the
      parametrize-id defect hid behind "832 passed".
    """
    if not names:
        return {}
    # -k over the union of names: robust to parametrisation and class nesting, and
    # avoids constructing node ids we would have to keep in sync with pytest.
    expr = " or ".join(names)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *targets,
            "-k",
            expr,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "-rA",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout + proc.stderr
    # A collection failure names the file, not the test, so it must be detected once
    # for the whole run rather than per test.
    uncollectable = (
        bool(
            re.search(
                r"^(ERROR collecting|E\s+ModuleNotFoundError|E\s+ImportError)", out, re.MULTILINE
            )
        )
        or "errors during collection" in out
    )
    result: dict[str, str] = {}
    for name in names:
        if re.search(rf"^(FAILED|ERROR) .*::{re.escape(name)}\b", out, re.MULTILINE):
            result[name] = "failed"
        elif re.search(rf"^PASSED .*::{re.escape(name)}\b", out, re.MULTILINE):
            result[name] = "passed"
        elif re.search(rf"^SKIPPED .*::{re.escape(name)}\b", out, re.MULTILINE):
            result[name] = "skipped"
        else:
            result[name] = "uncollectable" if uncollectable else "skipped"
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("range", help="commit range, e.g. origin/main..HEAD")
    ap.add_argument(
        "--tests",
        nargs="+",
        help="test paths to run (default: the test files the range itself changed). "
        "Passing a path that does not contain the added tests made every one of them "
        "look like it 'cannot fail', which is a false accusation, so the default is "
        "derived rather than required.",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    # TRACKED modifications only. `git checkout` never touches untracked files, so
    # they need no restoring — and refusing on them made the tool unusable in exactly
    # the situation it exists for: an agent that has just recorded evidence has an
    # untracked .harness/runs/ and could not mutation-verify anything.
    dirty = _git("status", "--porcelain", "--untracked-files=no").strip()
    if dirty:
        print(
            "✗ tracked files are modified; mutation_verify reverts source files and "
            "must be able to restore them exactly. Commit or set them aside first:\n"
            + "\n".join(f"    {line}" for line in dirty.splitlines()[:10]),
            file=sys.stderr,
        )
        return 2

    mods, adds, tst = changed_files(args.range)
    src = mods + adds
    added = added_tests(args.range, tst)
    names = sorted({n for v in added.values() for n in v})

    if not names:
        msg = f"no test functions added in {args.range} — nothing to mutation-verify"
        print(json.dumps({"added_tests": [], "note": msg}) if args.json else f"· {msg}")
        return 0
    if not src:
        msg = "range changes no source files; reverting nothing would prove nothing"
        print(json.dumps({"added_tests": names, "note": msg}) if args.json else f"· {msg}")
        return 0

    # Default the targets to the test files this range changed. A caller who names
    # the wrong path otherwise gets every added test reported as unable to fail.
    targets = args.tests or tst
    uncovered = sorted(
        f for f in added if not any(f.startswith(t.rstrip("/")) or t.startswith(f) for t in targets)
    )
    if uncovered:
        print(
            f"⚠ these files hold added tests but are outside --tests {targets}: "
            f"{uncovered}\n  Their tests cannot run, and a test that does not run is "
            f"not a finding about the code.",
            file=sys.stderr,
        )

    base = args.range.split("..")[0] or "HEAD~1"
    survivors: dict[str, bool] = {}
    try:
        # Revert the SOURCE side only. The tests stay as written — that is the
        # whole experiment: do they notice their subject is gone?
        if mods:
            _git("checkout", base, "--", *mods)
        for path in adds:
            (ROOT / path).unlink()  # the base has no version to check out
        survivors = run_tests(targets, names)
    finally:
        # Always restore, even on an exception: leaving a half-reverted tree behind
        # would be far worse than the check not running.
        if src:
            _git("checkout", "HEAD", "--", *src, check=False)

    cannot_fail = sorted(n for n, r in survivors.items() if r in ("passed", "skipped"))
    uncollected = sorted(n for n, r in survivors.items() if r == "uncollectable")

    if args.json:
        print(
            json.dumps(
                {
                    "range": args.range,
                    "reverted_files": src,
                    "added_tests": names,
                    "cannot_fail": cannot_fail,
                    "uncollectable": uncollected,
                },
                indent=2,
            )
        )
        return 1 if cannot_fail else 0

    print(f"→ reverted {len(src)} source file(s); ran {len(names)} added test(s)")
    if uncollected:
        print(
            f"  {len(uncollected)} test(s) could not even be collected without the "
            f"change — the module under test is part of it. That is dependence, not "
            f"a gap."
        )
    if cannot_fail:
        print(f"✗ {len(cannot_fail)} added test(s) still pass without the change:")
        for n in cannot_fail:
            print(f"    · {n}")
        print("  These do not test what the change did. Rule 6: a test that cannot fail")
        print("  is not evidence — tighten the assertion or delete the test.")
        return 1
    print(f"✓ all {len(names)} added test(s) went red without the change — they are evidence")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"✗ mutation_verify: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
