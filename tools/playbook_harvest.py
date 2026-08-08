#!/usr/bin/env python3
"""Playbook harvest — the deterministic half of the weekly self-improvement loop.

Collects the raw material a harvest needs and checks the playbook set's own
integrity. It makes NO judgement about which lessons are worth keeping: that is the
model's job in ``.claude/skills/harvest-playbooks``. This script only answers
"what is new, how many rounds did it cost, and is the playbook set still coherent."

Why it is a script at all: the mechanical parts (which notes are new, what the round
counts were, whether INDEX.md and the files still agree) are the parts a model does
slowly and inconsistently, and they are exactly the parts that must not be skipped.

    python3 tools/playbook_harvest.py                 # human report
    python3 tools/playbook_harvest.py --json          # machine
    python3 tools/playbook_harvest.py --since <sha>   # override the marker

Exit codes: 0 = ran (with or without new material), 1 = an integrity check failed,
2 = usage/internal error. New material is NOT an error — check ``new_notes``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOKS = ROOT / "docs" / "playbooks"
INDEX = PLAYBOOKS / "INDEX.md"
STATE = PLAYBOOKS / ".harvest-state.json"
NOTES_DIR = "progress.d"

# The pre-playbook baseline, from the sessions logged before docs/playbooks/ existed
# (LOG-001 38, DATA-018 15, REL-001 20, DATA-007 17, RESV-003 14, API-001 14,
#  DATA-012 14, SAFE-002 13, DATA-015 13, UI-5 13, ORCH-005 10, NOTIF-001 9, MD-003 9).
BASELINE_ROUNDS = (38, 20, 17, 15, 14, 14, 14, 13, 13, 13, 10, 9, 9)
LINE_BUDGET = 165  # a playbook nobody finishes reading is not a playbook


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args], check=False, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class Note:
    feature: str
    path: str
    outcome: str = ""
    rounds: int | None = None
    playbook_updates: str = ""
    findings: list[str] = field(default_factory=list)
    zero_code: bool = False
    # Filled from .harness/runs/<feature>/review.jsonl when the reviewer recorded it.
    rounds_source: str = "note"
    rules: list[str] = field(default_factory=list)
    rounds_disagree: bool = False


def _parse_note(text: str, feature: str, path: str) -> Note:
    note = Note(feature=feature, path=path)
    for line in text.splitlines():
        s = line.strip()
        low = s.lower()
        if not note.outcome and low.startswith("outcome:"):
            note.outcome = s.split(":", 1)[1].strip()
        elif note.rounds is None and low.startswith("adversarial rounds:"):
            m = re.search(r"\d+", s.split(":", 1)[1])
            if m:
                note.rounds = int(m.group())
        elif not note.playbook_updates and low.startswith("playbook updates:"):
            note.playbook_updates = s.split(":", 1)[1].strip()
        # Round bullets carry the defect classes -- the actual harvest material.
        if re.match(r"^r\d+\s*(→|->)", low) or re.match(r"^round\s*\d+\b", low):
            note.findings.append(s)
        if "no code changed" in low or "this was churn" in low:
            note.zero_code = True
    return note


def review_telemetry(feature: str) -> dict | None:
    """Structured rounds for a feature, if adversarial_review.py recorded them.

    The prose ``Adversarial rounds:`` line is what an agent remembered to type; this
    is what the reviewer actually did. Prefer the latter and keep the former as the
    fallback, because notes folded into progress.txt at close no longer have a
    .harness directory to read.
    """
    path = ROOT / ".harness" / "runs" / feature / "review.jsonl"
    if not path.is_file():
        return None
    rounds, rules = [], set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rounds.append(rec)
            rules.update(rec.get("rules") or [])
    except (OSError, json.JSONDecodeError):
        # Unreadable telemetry is not zero rounds — say nothing and let the note
        # speak, rather than reporting a confident 0 (CLAUDE.md rule 3).
        return None
    if not rounds:
        return None
    return {
        "count": len(rounds),
        "rules": sorted(rules),
        "verdicts": [r.get("verdict") for r in rounds],
    }


def _read_at(ref: str, path: str) -> str | None:
    try:
        return _git("show", f"{ref}:{path}")
    except RuntimeError:
        return None


def collect(since: str | None) -> dict:
    """Notes added or changed since the marker, parsed."""
    marker = since
    if marker is None and STATE.exists():
        marker = json.loads(STATE.read_text(encoding="utf-8")).get("last_harvest_commit")

    if marker:
        raw = _git("diff", "--name-only", f"{marker}..HEAD", "--", f"{NOTES_DIR}/")
        # A note folded into progress.txt at close is deleted from progress.d; the
        # folded text is still new material, so take progress.txt's diff as a signal.
        folded = _git("diff", "--stat", f"{marker}..HEAD", "--", "progress.txt").strip()
    else:
        raw = _git("ls-files", f"{NOTES_DIR}/")
        folded = ""

    paths = [
        p
        for p in (ln.strip() for ln in raw.splitlines())
        if p.startswith(f"{NOTES_DIR}/session-") and p.endswith(".md")
    ]

    notes: list[Note] = []
    for p in sorted(set(paths)):
        text = (
            (ROOT / p).read_text(encoding="utf-8", errors="replace")
            if (ROOT / p).exists()
            else (_read_at(marker, p) if marker else None)
        )
        if text is None:
            continue  # folded away and unreadable at the marker -- progress.txt has it
        feature = Path(p).stem[len("session-") :]
        note = _parse_note(text, feature, p)
        if (telem := review_telemetry(feature)) is not None:
            if note.rounds is not None and note.rounds != telem["count"]:
                note.rounds_disagree = True
            note.rounds = telem["count"]
            note.rounds_source = "review.jsonl"
            note.rules = telem["rules"]
        notes.append(note)

    return {
        "marker": marker,
        "head": _git("rev-parse", "HEAD").strip(),
        "progress_txt_changed": bool(folded),
        "notes": [asdict(n) for n in notes],
    }


def round_series(notes: list[dict]) -> dict:
    """Is the loop working? The claim is falsifiable only if we keep the series."""
    measured = [n["rounds"] for n in notes if n["rounds"] is not None]
    unreported = [n["feature"] for n in notes if n["rounds"] is None]

    def median(xs: list[int]) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        mid = len(s) // 2
        return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    return {
        "measured": measured,
        "median": median(measured),
        "baseline_median": median(list(BASELINE_ROUNDS)),
        "unreported": unreported,
    }


def integrity() -> list[str]:
    """The playbook set's own coherence. A stale index is a rule nobody reads."""
    problems: list[str] = []
    if not INDEX.exists():
        return [f"{INDEX.relative_to(ROOT)} is missing"]

    index_text = INDEX.read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\((([a-z0-9-]+)\.md)\)", index_text))
    linked_names = {name for name, _stem in linked}
    on_disk = {p.name for p in PLAYBOOKS.glob("*.md") if p.name != "INDEX.md"}

    for missing in sorted(linked_names - on_disk):
        problems.append(f"INDEX.md links {missing}, which does not exist")
    for unlisted in sorted(on_disk - linked_names):
        problems.append(f"{unlisted} exists but INDEX.md does not route to it")

    for p in sorted(PLAYBOOKS.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        n = len(text.splitlines())
        if n > LINE_BUDGET:
            problems.append(f"{p.name} is {n} lines (budget {LINE_BUDGET}) — split or prune it")
        for target in re.findall(r"\]\((([a-z0-9-]+)\.md)(?:#[^)]*)?\)", text):
            if not (PLAYBOOKS / target[0]).exists():
                problems.append(f"{p.name} links {target[0]}, which does not exist")
        # A rule with no provenance cannot be judged for relevance or retired.
        if p.name != "INDEX.md" and "(" not in text:
            problems.append(f"{p.name} carries no provenance tags")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--since", help="commit to harvest from (default: the recorded marker)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--set-marker",
        metavar="SHA",
        help="record SHA as harvested and exit (the skill does this at the end of a run)",
    )
    args = ap.parse_args(argv)

    if args.set_marker:
        sha = _git("rev-parse", args.set_marker).strip()
        STATE.write_text(
            json.dumps({"last_harvest_commit": sha}, indent=2) + "\n", encoding="utf-8"
        )
        print(f"✓ harvest marker set to {sha[:12]}")
        return 0

    data = collect(args.since)
    notes = data["notes"]
    data["rounds"] = round_series(notes)
    data["integrity"] = integrity()
    data["zero_code_sessions"] = [n["feature"] for n in notes if n["zero_code"]]

    if args.json:
        print(json.dumps(data, indent=2))
        return 1 if data["integrity"] else 0

    marker = data["marker"] or "(no marker — first harvest, all notes)"
    print(f"== playbook harvest ==  since {marker[:12] if data['marker'] else marker}")
    print(f"   HEAD {data['head'][:12]}   new/changed notes: {len(notes)}")
    if data["progress_txt_changed"]:
        print("   progress.txt also changed — a note was folded at close; read that diff too")

    if not notes and not data["progress_txt_changed"]:
        print("\n   Nothing new to harvest. This is the common case; stop here.")
    else:
        print("\n-- new material --")
        for n in notes:
            rounds = n["rounds"] if n["rounds"] is not None else "?"
            print(f"  {n['feature']:20} rounds={rounds:<4} {n['outcome'][:44]}")
            if n["playbook_updates"]:
                print(f"  {'':20} playbook updates: {n['playbook_updates'][:60]}")
            if n["zero_code"]:
                print(f"  {'':20} ⚠ zero-code session — a churn signal, not a lesson")
            for f in n["findings"][:40]:
                print(f"  {'':22}· {f[:110]}")

    r = data["rounds"]
    if r["measured"]:
        print(f"\n-- round counts --\n  this batch: {r['measured']}  median {r['median']}")
        print(f"  pre-playbook baseline median: {r['baseline_median']}")
    if r["unreported"]:
        print(f"  ⚠ no 'Adversarial rounds:' line: {', '.join(r['unreported'])}")
    measured_by_tool = [n["feature"] for n in notes if n.get("rounds_source") == "review.jsonl"]
    if measured_by_tool:
        print(f"  {len(measured_by_tool)} count(s) taken from review.jsonl, not from prose")
    disagreed = [n["feature"] for n in notes if n.get("rounds_disagree")]
    if disagreed:
        print(f"  ⚠ note disagrees with the recorded rounds: {', '.join(disagreed)}")

    # Defect classes the reviewer raised across this batch. A rule id that keeps
    # recurring across FEATURES is a candidate for promotion into
    # tools/critic_check.py — a regex is cheaper than a review round.
    freq: dict[str, set] = {}
    for n in notes:
        for rule in n.get("rules") or []:
            freq.setdefault(rule, set()).add(n["feature"])
    recurring = sorted(
        ((rule, sorted(f)) for rule, f in freq.items() if len(f) >= 3),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )
    if recurring:
        print("\n-- promotion candidates (a rule the reviewer raised on 3+ features) --")
        for rule, feats in recurring:
            print(f"  {rule:44} {len(feats)} features: {', '.join(feats[:5])}")
        print("  Mechanising one of these in tools/critic_check.py retires a review round.")

    if data["integrity"]:
        print("\n-- integrity FAILURES --")
        for p in data["integrity"]:
            print(f"  ✗ {p}")
        return 1
    print("\n-- integrity --\n  ✓ index and files agree; all links resolve; all within budget")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - a harvest must not fail silently
        print(f"✗ playbook_harvest: {exc}", file=sys.stderr)
        sys.exit(2)
