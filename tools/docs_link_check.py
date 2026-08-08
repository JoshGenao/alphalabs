#!/usr/bin/env python3
"""Every reference an agent is told to follow must resolve.

The audit found these by reading, one at a time: the root ``README.md`` pointed at
``docs/StRS.md`` and ``docs/SyRS.md``, neither of which has ever existed, while
``init.sh`` curled that same file as its smoke test; ``prompts/critic_prompt.md``
sent reviewers to "Step 6.5", renumbered to 6.6 long before; ``AGENTS.md`` described
a command allowlist that did not exist. None of them broke anything loudly. All of
them sent an agent somewhere that was not there.

``playbook_harvest.integrity()`` already does this for ``docs/playbooks/``. This is
the same check over every file the harness tells an agent to read.

Three reference kinds are validated:

* markdown links — ``[text](path)``, skipping URLs and pure anchors
* inline paths — ``` `tools/foo.py` ``` — deliberately conservative (see PATH_RE)
* step references — ``Step 6.6`` must exist as a heading in coding_prompt.md

    python3 tools/docs_link_check.py            # human
    python3 tools/docs_link_check.py --json     # machine
    python3 tools/docs_link_check.py --list     # what is scanned, and exit

Exit codes: 0 = every reference resolves, 1 = at least one does not, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The files the harness routes agents through. A stale pointer in any of these is a
# stale pointer in the agent's own instructions.
SCANNED = (
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "docs/README.md",
    "progress.d/README.md",
    "architecture/README.md",
    "prompts/*.md",
    "docs/playbooks/*.md",
)

# The step-numbered workflow every other document cites.
STEP_SOURCE = "prompts/coding_prompt.md"

MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
STEP_REF_RE = re.compile(r"\bStep\s+(\d+(?:\.\d+)?)\b")
STEP_HEADING_RE = re.compile(r"^##+\s*Step\s+(\d+(?:\.\d+)?)", re.MULTILINE)

# Inline `backticked` paths. Conservative on purpose, and the first draft was not:
# it flagged 66 references of which ~64 were prose — bare filenames (`close_feature.py`),
# git refs (`origin/main`), slash commands (`/codex:adversarial-review`), a bare
# extension (`.py`), a branch prefix (`agent/`). A checker with that false-positive
# rate gets switched off the first day, which is worse than not having one.
#
# A candidate must therefore be an unambiguous REPO-RELATIVE path: it contains a
# slash, its first segment is a real top-level entry of this repo, and it is not a
# command, glob, git ref or home path. Bare filenames are deliberately NOT checked —
# `close_feature.py` is prose referring to a file whose location the reader knows.
PATH_RE = re.compile(r"`([^`\n]+)`")
NOT_A_PATH = re.compile(r"[\s*?\$|<>()\[\]{}=,:]|^-|--")
# Prefixes that look like paths and are not.
NON_PATH_PREFIXES = (
    "http",
    "//",
    "~",
    "/",
    "origin/",
    "refs/",
    "agent/",
    "chore/",
    "docs/deployment-",
)


def _top_level_entries() -> frozenset[str]:
    return frozenset(p.name for p in ROOT.iterdir())


def scanned_files() -> list[Path]:
    out: list[Path] = []
    for pattern in SCANNED:
        if "*" in pattern:
            out.extend(sorted(ROOT.glob(pattern)))
        elif (ROOT / pattern).is_file():
            out.append(ROOT / pattern)
    return out


def _looks_like_path(text: str, top_level: frozenset[str]) -> bool:
    """Only unambiguous repo-relative paths. See NOT_A_PATH for why."""
    if NOT_A_PATH.search(text) or text.startswith(NON_PATH_PREFIXES):
        return False
    if "/" not in text:
        return False  # a bare filename is prose, not a navigable reference
    return text.split("/", 1)[0] in top_level


def _resolve(ref: str, source: Path) -> bool:
    """Does this reference point at something that exists?"""
    ref = ref.split("#", 1)[0].strip().rstrip("/")
    if not ref:
        return True  # a pure anchor
    # Relative to the referring file first (how markdown links read), then to root.
    return (source.parent / ref).exists() or (ROOT / ref).exists()


def known_steps() -> set[str]:
    src = ROOT / STEP_SOURCE
    if not src.is_file():
        return set()
    return set(STEP_HEADING_RE.findall(src.read_text(encoding="utf-8")))


def check_file(path: Path, steps: set[str], top_level: frozenset[str]) -> list[str]:
    problems: list[str] = []
    rel = path.relative_to(ROOT)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{rel}: unreadable ({exc})"]

    for line_no, line in enumerate(text.splitlines(), 1):
        for target in MD_LINK_RE.findall(line):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not _resolve(target, path):
                problems.append(f"{rel}:{line_no}: link to {target!r} does not resolve")

        # Playbooks quote paths that were created BY the bug they describe —
        # `progress.d/plan-.md` really was written into main by an empty
        # ATP_FEATURE_ID. Those are evidence, not references to follow. Their
        # markdown links are still validated above.
        quotes_incidents = rel.parts[:2] == ("docs", "playbooks")
        for candidate in [] if quotes_incidents else PATH_RE.findall(line):
            if not _looks_like_path(candidate, top_level):
                continue
            if not _resolve(candidate, path):
                problems.append(f"{rel}:{line_no}: `{candidate}` does not exist")

    # Step references. A document that defines its OWN `## Step N` headings — the
    # initializer prompt has nine — is describing its own workflow, not citing the
    # coding prompt's, so its steps join the valid set. And a heading line is a
    # definition, never a reference to check.
    own = set(STEP_HEADING_RE.findall(text))
    valid = steps | own
    if rel.as_posix() != STEP_SOURCE and valid:
        for line_no, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for step in STEP_REF_RE.findall(line):
                if step not in valid:
                    problems.append(
                        f"{rel}:{line_no}: cites 'Step {step}', which neither this "
                        f"file nor {STEP_SOURCE} defines "
                        f"(known: {', '.join(sorted(valid))})"
                    )
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--list", action="store_true", help="print the scanned files and exit")
    args = ap.parse_args(argv)

    files = scanned_files()
    if args.list:
        for f in files:
            print(f"  {f.relative_to(ROOT)}")
        return 0

    steps = known_steps()
    problems: list[str] = []
    for f in files:
        problems.extend(check_file(f, steps, _top_level_entries()))

    if args.json:
        print(json.dumps({"files": len(files), "problems": problems}, indent=2))
        return 1 if problems else 0

    if problems:
        print(f"✗ docs link check: {len(problems)} unresolved reference(s)")
        for p in problems:
            print(f"    · {p}")
        return 1
    print(f"✓ docs link check: every reference in {len(files)} file(s) resolves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
