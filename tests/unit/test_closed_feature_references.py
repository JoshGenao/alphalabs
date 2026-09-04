"""L1 — a CLOSED feature must not be described anywhere as deferred or unbuilt.

Closing a widely-referenced feature turns every "deferred SRS-X" into a public
contradiction. SRS-DATA-007 was referenced ~50 times and took seven review
rounds of whack-a-mole to close cleanly; the playbook's remedy is to grep
repo-wide FIRST and then *write the guard*, because manual sweeps miss instances
and the drift returns (docs/playbooks/scope-and-serialization.md rules 18-19).

This is that guard, and it is deliberately a RATCHET rather than a repo-wide
gate. A measurement on 2026-09-01 found **274 contradiction-shaped lines across
38 of the 58 closed features** — a pre-existing, repo-wide condition. Enforcing
all of it at once would mean either a 274-line sweep nobody has budgeted or an
exemption list so large the check means nothing.

So: `SWEPT_FEATURES` lists the ids whose references have actually been audited.
Closing a feature and adding it here is the moment the sweep gets done. The set
only grows, and the backlog above is the honest measure of what is left.

THIS GUARD PASSED LOCALLY FOR THE WRONG REASON, once. ``_scannable_files()``
reads ``git ls-files``, and this file was still UNTRACKED when the local suite
ran — so the scan could not see its own planted fixtures, and CI failed on the
first commit that tracked it. A scan whose subject list comes from git is blind
to exactly the file you are adding. Re-run it after ``git add``.

Not every match is a real contradiction — that is why this is an audit, not a
regex applied blindly. "the deferred SRS-LOG-001 / SRS-UI-001 consumers" can be
TRUE about a closed feature when it means *that consumer's own wiring* is
deferred. Auditing an id means rewriting each site to say which part is still
missing, then adding the id here.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Feature ids whose references have been audited. Add an id here as part of
#: closing it — that is the point at which the sweep is cheap and the reasoning
#: is fresh.
SWEPT_FEATURES = frozenset({"SRS-NOTIF-001"})

#: Paths that legitimately record history, where a past "deferred" statement is
#: a true account of what was believed at the time.
EXCLUDED_PREFIXES = (
    "progress.d/",
    "progress.txt",
    ".harness/",
    "feature_list.json",
    "docs/verification-queue.md",
    # THIS FILE. Its parametrized fixtures are contradictions BY DESIGN — the
    # only way to prove the matcher fires at all. Scanning itself made the
    # guard indict its own evidence.
    "tests/unit/test_closed_feature_references.py",
    # The playbooks describe defect classes by quoting them. A rule that says
    # 'never write "deferred SRS-NOTIF-001"' has to write it once to say so.
    "docs/playbooks/",
)

SCANNED_SUFFIXES = (
    ".py",
    ".rs",
    ".js",
    ".css",
    ".md",
    ".json",
    ".toml",
    ".yml",
    ".yaml",
    ".sh",
    ".html",
)

#: Phrases that assert a feature is absent. Anchored near the id by PROXIMITY —
#: a line naming three features and deferring one of them must not indict the
#: other two.
_ABSENT = re.compile(
    r"(deferred|unbuilt|not (yet )?(built|delivered|landed)"
    r"|until .{0,40}lands?|stays? passes:? ?false)",
    re.IGNORECASE,
)
PROXIMITY = 140

#: An affirmation in the SAME window means the sentence is telling the reader the
#: feature HAS landed — usually while deferring something else beside it ("the
#: deferred atp-adapters leg; the transports LANDED with SRS-NOTIF-001"), or
#: asserting the id is absent from a deferred-owners list. Flagging those is how
#: a guard becomes noise people switch off. The cost is that a line asserting
#: both is left to its author; the benefit is that every remaining finding is
#: real.
_AFFIRMED = re.compile(
    r"(landed|has closed|closed on|no longer deferred|is delivered|now exists"
    r"|not in .{0,30}deferred|proven end to end)",
    re.IGNORECASE,
)

#: Per-site exemptions. Each MUST carry a reason. Empty is the goal.
EXEMPT: dict[tuple[str, str], str] = {}


def _scannable_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [f for f in out if f.endswith(SCANNED_SUFFIXES) and not f.startswith(EXCLUDED_PREFIXES)]


def _contradictions_in(text: str, fid: str) -> list[str]:
    """Lines in `text` that call `fid` absent, matched by proximity.

    ASYMMETRIC ON PURPOSE. The CLAIM must sit on the same line as the id: a
    character window that spanned line breaks pushed the count from 7 to 56,
    because ``architecture/runtime_services.json`` packs unrelated contract
    blocks within 140 characters of each other and every neighbouring
    "deferred" became a finding.

    The EXONERATION may wrap, because comments do. A per-line window read
    ``/// binding is deferred wiring: SRS-NOTIF-001's`` as a contradiction while
    the word ``landed`` sat on the next line — generating findings whose author
    had already fixed them.

    So: state a contradiction on one line, and you are flagged. Answer it
    anywhere nearby, and you are not.
    """

    hits = []
    start = 0
    while (idx := text.find(fid, start)) >= 0:
        start = idx + len(fid)
        line_start = text.rfind("\n", 0, idx) + 1
        line_end = text.find("\n", idx)
        line_end = line_end if line_end >= 0 else len(text)
        line = text[line_start:line_end]
        # Near the id AND on its line. `runtime_services.json` holds description
        # strings thousands of characters long; searching the whole line made
        # every unrelated "deferred" in the same blob a finding (13 of them).
        col = idx - line_start
        claim_window = line[max(0, col - PROXIMITY) : col + len(fid) + PROXIMITY]
        if not _ABSENT.search(claim_window):
            continue
        exoneration = text[max(0, idx - PROXIMITY) : idx + len(fid) + PROXIMITY]
        if not _AFFIRMED.search(exoneration):
            hits.append(line.strip())
    return hits


def test_swept_features_are_never_described_as_deferred() -> None:
    findings: list[str] = []
    for path in _scannable_files():
        try:
            text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for fid in SWEPT_FEATURES:
            if fid not in text:
                continue
            for line in _contradictions_in(text, fid):
                if EXEMPT.get((path, fid)):
                    continue
                findings.append(f"{path}: {line[:130]}")

    assert not findings, (
        f"{len(findings)} site(s) describe a SWEPT feature as deferred or unbuilt.\n"
        "A closed feature named as the thing being awaited is a contradiction a\n"
        "reader cannot resolve. Rewrite each to say which part is still missing,\n"
        "or add a (path, id) entry to EXEMPT with a reason.\n\n  " + "\n  ".join(findings)
    )


def test_the_scan_actually_reaches_the_tree() -> None:
    """A scan that examines nothing reports a clean tree.

    The failure mode that looks most like the guard working
    (docs/playbooks/test-integrity.md rule 28). Both halves matter: the file
    list must be real, and every swept id must still exist in feature_list.json
    — a typo there would silently enforce nothing.
    """

    files = _scannable_files()
    assert len(files) > 200, f"only {len(files)} files scanned; the glob is broken"

    known = {f["id"] for f in json.loads((ROOT / "feature_list.json").read_text())}
    unknown = SWEPT_FEATURES - known
    assert not unknown, f"SWEPT_FEATURES names ids not in feature_list.json: {sorted(unknown)}"


@pytest.mark.parametrize(
    "line",
    [
        "// the deferred SRS-NOTIF-001 dispatcher owns this",
        "SRS-NOTIF-001 is not yet built",
        "stays passes:false until SRS-NOTIF-001 lands",
        "the SRS-NOTIF-001 transports are unbuilt",
    ],
)
def test_the_matcher_catches_a_planted_contradiction(line: str) -> None:
    """Prove the detector fires, rather than trusting an empty result."""

    assert _contradictions_in(line, "SRS-NOTIF-001") == [line]


@pytest.mark.parametrize(
    "line",
    [
        "SRS-NOTIF-001 dispatch begins within 60 seconds",
        # A long single-line JSON blob: the deferral is about something else,
        # far away on the same line.
        '"desc": "the deferred SRS-LOG-001 sink'
        + " and much else besides, "
        + "y" * 200
        + ' then separately SRS-NOTIF-001 dispatch"',
        "the deferred SRS-LOG-001 sink, far away from any mention of the other one, "
        + "x" * 200
        + " SRS-NOTIF-001",
        # An affirmation in the same window: one thing deferred, this one landed.
        "the deferred atp-adapters leg; the transports LANDED with SRS-NOTIF-001",
        # An assertion that the id is NOT among the deferred owners.
        'assert "SRS-NOTIF-001" not in logs["deferred_owners"]',
        # A WRAPPED sentence: the affirmation is on the following line. A
        # per-line window called this a contradiction; it is the opposite.
        "/// binding is deferred composition-root wiring: SRS-NOTIF-001's\n"
        "/// notifier landed, but nothing binds it to this port yet.",
    ],
)
def test_the_matcher_does_not_fire_on_a_clean_line(line: str) -> None:
    """The other direction: a guard that flags everything gets switched off.

    The second case is the proximity rule doing its job — one feature deferred,
    another merely named far enough away that the sentence is not about it.
    """

    assert _contradictions_in(line, "SRS-NOTIF-001") == []


# --- The queue must not hand the operator a command that cannot succeed ------


def _blocked_layers(fid: str) -> list[str]:
    path = ROOT / ".harness" / "runs" / fid / "evidence.json"
    if not path.exists():
        return []
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return sorted(
        layer
        for layer, entry in (rec.get("critic") or {}).items()
        if isinstance(entry, dict) and entry.get("verdict") != "approve"
    )


def test_a_queue_row_promising_a_close_discloses_a_standing_critic_block() -> None:
    """`--attested-by` relaxes which STEPS count, never the critic gate.

    A row that says "Nothing" is missing and hands over
    `close_feature.py <id> --verified --attested-by operator` sends the operator
    to a command that exits 3 (`close_feature.py` -> `evidence.py`, which refuses
    any layer whose verdict is not `approve`). The operator reads a green row,
    runs the command, and gets an error the row said would not happen.

    This is generic over the queue, so the next feature to record a `block`
    cannot ship a row that hides it.
    """
    queue = ROOT / "docs" / "verification-queue.md"
    if not queue.exists():
        pytest.skip("no verification queue in this tree")

    problems = []
    for line in queue.read_text(encoding="utf-8").splitlines():
        for fid in set(re.findall(r"close_feature\.py\s+(SRS-[A-Z]+-\d+)", line)):
            blocked = _blocked_layers(fid)
            if blocked and "block" not in line.lower():
                problems.append(f"{fid}: critic {', '.join(blocked)} is not `approve`")
    assert not problems, (
        "queue row(s) promise a close that `evidence.py verify` will refuse, "
        "without disclosing the standing verdict: " + "; ".join(problems)
    )


def test_the_queue_guard_fires_on_a_row_that_hides_a_block(tmp_path, monkeypatch) -> None:
    """Non-vacuity: the test above passes trivially if nothing records a block."""
    fid = "SRS-FAKE-001"
    run = tmp_path / ".harness" / "runs" / fid
    run.mkdir(parents=True)
    (run / "evidence.json").write_text(
        json.dumps({"critic": {"judgment": {"verdict": "block"}}}), encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "verification-queue.md").write_text(
        f"| **{fid}** | 1 | Nothing. | `close_feature.py {fid} --verified --attested-by operator` |",
        encoding="utf-8",
    )
    monkeypatch.setattr("tests.unit.test_closed_feature_references.ROOT", tmp_path, raising=False)
    import tests.unit.test_closed_feature_references as mod

    monkeypatch.setattr(mod, "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="judgment"):
        mod.test_a_queue_row_promising_a_close_discloses_a_standing_critic_block()
