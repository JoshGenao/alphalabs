#!/usr/bin/env python3
"""Query progress.txt instead of reading it.

``progress.txt`` is the canonical session log: 101 sessions, ~1 MB, ~170k tokens.
``AGENTS.md`` used to open with ``cat progress.txt`` in its start-of-session
checklist, which would spend most of an agent's context window before it had read
a single requirement. The archive is worth keeping; reading all of it never is.

    tools/progress_query.py --feature SRS-LOG-001   # every session touching it
    tools/progress_query.py --last 3                # the 3 most recent sessions
    tools/progress_query.py --grep "kill switch"    # sessions mentioning a phrase
    tools/progress_query.py --list                  # one line per session

Sessions are ``=== SESSION N ===`` blocks, newest first.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRESS = ROOT / "progress.txt"
HEADER_RE = re.compile(r"^=== SESSION (\S+) ===\s*$", re.MULTILINE)


def sessions() -> list[tuple[str, str]]:
    """(header, body) per session block, in file order (newest first)."""
    if not PROGRESS.exists():
        return []
    text = PROGRESS.read_text(encoding="utf-8", errors="replace")
    marks = list(HEADER_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(0).strip(), text[m.end() : end].strip("\n")))
    return out


def _first(body: str, prefix: str) -> str:
    for line in body.splitlines():
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--feature", help="sessions whose Feature: line or body names this id")
    ap.add_argument("--last", type=int, help="the N most recent sessions, in full")
    ap.add_argument("--grep", help="sessions containing this (case-insensitive) text")
    ap.add_argument("--list", action="store_true", help="one summary line per session")
    args = ap.parse_args(argv)

    blocks = sessions()
    if not blocks:
        print(f"✗ {PROGRESS.name} has no '=== SESSION N ===' blocks", file=sys.stderr)
        return 1

    if args.list:
        for header, body in blocks:
            feat = _first(body, "feature:") or "?"
            outcome = _first(body, "outcome:") or "?"
            print(f"{header:22} {feat[:44]:44} {outcome[:40]}")
        return 0

    if args.last:
        picked = blocks[: args.last]
    elif args.feature:
        needle = args.feature.lower()
        picked = [b for b in blocks if needle in b[1].lower() or needle in b[0].lower()]
    elif args.grep:
        needle = args.grep.lower()
        picked = [b for b in blocks if needle in b[1].lower()]
    else:
        ap.error("give one of --feature / --last / --grep / --list")

    if not picked:
        print("(no matching session)")
        return 1
    for header, body in picked:
        print(f"{header}\n{body}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
