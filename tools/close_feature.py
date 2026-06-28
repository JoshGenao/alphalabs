#!/usr/bin/env python3
"""Integration-time feature closer.

Run on ``main`` after merging an agent's PR. For each feature id it:

1. flips that feature's ``"passes"`` from ``false`` to ``true`` in
   ``feature_list.json`` (atomic, minimal-diff write), and
2. folds the merged ``progress.d/session-<id>.md`` note into ``progress.txt``
   under the next sequential ``=== SESSION N ===`` header, then removes the
   per-session note.

This keeps the source-of-truth mutation (the ``passes`` flip) and the canonical
session log serialized on one branch, so parallel agent branches never touch
either shared file. See ``prompts/coding_prompt.md`` (Steps 6 and 8) and
``progress.d/README.md``.

Usage:
    tools/close_feature.py SRS-EXE-001 [SRS-DATA-007 ...]
    tools/close_feature.py --dry-run SRS-EXE-001
    tools/close_feature.py --no-fold SRS-EXE-001     # flip only
    tools/close_feature.py --keep-note SRS-EXE-001   # fold but keep the note
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEATURE_FILE = ROOT / "feature_list.json"
PROGRESS_FILE = ROOT / "progress.txt"
PROGRESS_DIR = ROOT / "progress.d"

SESSION_RE = re.compile(r"^=== SESSION (\d+) ===", re.MULTILINE)


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + rename in the same dir."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def flip_passes(feature_id: str, *, dry_run: bool) -> bool:
    """Flip a single feature's passes to true. Returns True if a change is needed."""
    raw = FEATURE_FILE.read_text(encoding="utf-8")
    features = json.loads(raw)
    match = next((f for f in features if f.get("id") == feature_id), None)
    if match is None:
        print(f"  ✗ {feature_id}: not found in feature_list.json", file=sys.stderr)
        return False
    if match.get("passes") is True:
        print(f"  • {feature_id}: already passes:true (no flip)")
        return False
    if dry_run:
        print(f"  [dry-run] would flip {feature_id} passes:false → true")
        return True
    match["passes"] = True
    # json.dump default (no trailing newline) is what the agent flow uses; match
    # the original file's trailing-newline state to keep the diff to one line.
    body = json.dumps(features, indent=2)
    if raw.endswith("\n"):
        body += "\n"
    _atomic_write(FEATURE_FILE, body)
    print(f"  ✓ {feature_id}: flipped passes:false → true")
    return True


def _next_session_number() -> int:
    if not PROGRESS_FILE.exists():
        return 1
    nums = [int(m) for m in SESSION_RE.findall(PROGRESS_FILE.read_text(encoding="utf-8"))]
    return (max(nums) + 1) if nums else 1


def fold_note(feature_id: str, *, keep_note: bool, dry_run: bool) -> None:
    """Prepend the per-session note to progress.txt with the next session number."""
    note_path = PROGRESS_DIR / f"session-{feature_id}.md"
    if not note_path.exists():
        print(f"  • {feature_id}: no progress.d/session-{feature_id}.md to fold")
        return

    session_no = _next_session_number()
    note = note_path.read_text(encoding="utf-8")
    lines = note.splitlines()
    if lines and lines[0].startswith("=== SESSION"):
        lines = lines[1:]  # drop the note's own placeholder header
    body = "\n".join(lines).strip("\n")
    block = f"=== SESSION {session_no} ===\n{body}\n"

    if dry_run:
        print(f"  [dry-run] would fold {note_path.name} as SESSION {session_no}"
              f"{'' if keep_note else ' and remove the note'}")
        return

    existing = PROGRESS_FILE.read_text(encoding="utf-8") if PROGRESS_FILE.exists() else ""
    combined = block + ("\n\n" + existing.lstrip("\n") if existing.strip() else "\n")
    _atomic_write(PROGRESS_FILE, combined)
    print(f"  ✓ {feature_id}: folded into progress.txt as SESSION {session_no}")

    if not keep_note:
        note_path.unlink()
        print(f"  ✓ {feature_id}: removed {note_path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Close verified features on main.")
    parser.add_argument("feature_ids", nargs="+", help="feature ids to close")
    parser.add_argument("--verified", action="store_true",
                        help="REQUIRED to flip. Asserts every step in the feature "
                             "passed end-to-end — not partial, not a unit test "
                             "alone, not 'works in isolation', and you are "
                             "confident a human running the steps would pass. The "
                             "close-feature workflow passes this only when the PR "
                             "carries the 'verified-e2e' label.")
    parser.add_argument("--no-fold", action="store_true",
                        help="only flip passes; do not fold the progress note")
    parser.add_argument("--keep-note", action="store_true",
                        help="fold the note but keep the per-session file")
    parser.add_argument("--dry-run", action="store_true",
                        help="print actions without writing anything")
    args = parser.parse_args()

    if not FEATURE_FILE.exists():
        print(f"✗ {FEATURE_FILE} not found", file=sys.stderr)
        return 1

    # A feature must never be marked passing just because work merged. Refuse to
    # touch feature_list.json unless the caller explicitly attests verification.
    if not args.verified and not args.dry_run:
        print(
            "✗ refusing to close without --verified.\n"
            "  Only mark a feature passing if EVERY step passes end-to-end:\n"
            "  not partial, not a unit test alone, not 'works in isolation',\n"
            "  and you are confident a human running the steps would pass.\n"
            "  Re-run with --verified once that is confirmed. In CI this flag is\n"
            "  supplied only when the PR carries the 'verified-e2e' label.",
            file=sys.stderr,
        )
        return 2

    for feature_id in args.feature_ids:
        print(f"→ {feature_id}")
        flip_passes(feature_id, dry_run=args.dry_run)
        if not args.no_fold:
            fold_note(feature_id, keep_note=args.keep_note, dry_run=args.dry_run)

    print("\n✓ done. Review with: git diff -- feature_list.json progress.txt progress.d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
