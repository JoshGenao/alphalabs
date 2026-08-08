#!/usr/bin/env python3
"""Derive, review, and apply each feature's ``verification_method``.

The honesty guard that decides whether a feature may close as ``complete`` used to
substring-match the feature's own prose. The prose is templated — three of every
four ``steps[]`` entries are boilerplate — so "dashboard" matched 47 features and
" ib " matched 32, and the guard fired on 90 of 120. A guard that fires on
three-quarters of the corpus teaches everyone where the override lives.

This replaces it with a declared field. The derivation here is a *proposal*: it is
the same kind of text inference, so it is not allowed to be the final answer. It
writes a flat review file, a human corrects it, and only then is it applied.

    tools/classify_verification.py propose            # write the review file
    tools/classify_verification.py propose --stdout   # preview it
    tools/classify_verification.py apply              # feature_list.json <- review file
    tools/classify_verification.py status             # how many are classified

Methods: solo | integration | live-ib | e2e  (see agent_pool.SOLO_METHODS).
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
REVIEW_FILE = ROOT / ".harness" / "verification-method-review.txt"

VALID = ("solo", "integration", "live-ib", "e2e")

# Where the signal actually lives. The generated features follow a 4-step template:
# steps 2 and 4 name the *verification method* ("Exercise X using browser automation
# against the dashboard…"), and step 3 states the *acceptance criterion* — the only
# feature-specific prose in the record. The description says what the feature DOES,
# which is why the old keyword guard scored a REST feature that merely exposes a
# live-designation endpoint as live-ib.
#
# Step 3 must be included. Reading only 2 and 4 dropped the AC and produced
# permissive `solo` calls on features whose criterion names a real resource:
# SRS-BT-001 ("selectable through API and dashboard"), SRS-DATA-009 (NAS-tier
# reads), SRS-DATA-017 (strategy containers and notebooks), SRS-FAC-001 (8,000+
# securities over Sharadar data). Permissive is the wrong direction for this guard.
METHOD_STEPS = (2, 3, 4)

# Something the method text says it uses INSTEAD of the real resource. This is the
# correction that matters most: 16 features say "integration or fault-injection
# workflows using mocked IB/data-provider" — the old guard matched " ib " and
# serialized every one of them, which is the exact inverse of what the text says.
STUBBED = ("mock", "fixture", "stub", "simulated", "fake", "recorded")

# Evaluated in order; first hit wins. A real IB dependency outranks a dashboard one
# because the single-live-IB invariant is the binding constraint.
BROWSER = ("browser automation", "playwright", "selenium")
REAL_IB = (
    "ib gateway",
    "interactive brokers",
    "live ib",
    "real ib",
    "live execution",
    "live order",
    "live trading",
    "paper account",
)
INTEGRATION = (
    "integration test",
    "real container",
    "docker compose",
    "docker-compose",
    "integration or fault-injection",
)
SURFACE = ("dashboard", "websocket", "jupyter")

# Any other named external resource. Enumeration will always have gaps — the AC for
# SRS-DATA-009 says "served from NAS", SRS-DATA-017 says "strategy containers …  and
# notebooks", SRS-FAC-001 says "8,000+ securities using market and Sharadar data",
# and none of those words appear above. That is why SELF_CONTAINED below inverts the
# burden of proof rather than relying on this list being complete.
RESOURCE = (
    "nas",
    "ssd",
    "container",
    "notebook",
    "sharadar",
    "databento",
    "docker",
    "provider data",
    "market data feed",
    "8,000+",
    "retention",
)

# A positive claim that the method needs nothing outside this process. `solo` is the
# permissive answer — it is the one that lets `integrate --mode complete` flip
# `passes` — so it must be EARNED by the text, never fallen back to. Anything that
# names no self-contained means is proposed non-solo and flagged for a human.
SELF_CONTAINED = (
    "fixture",
    "mock",
    "stub",
    "in-process",
    "file inspection",
    "file reads",
    "build manifest",
    "unit test",
    "static",
    "source inspection",
    "persisted output inspection",
)

LINE_RE = re.compile(r"^\s*([A-Za-z0-9-]+)\s+(solo|integration|live-ib|e2e)\b")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_features() -> list:
    return json.loads(FEATURE_FILE.read_text(encoding="utf-8"))


def method_text(feat: dict) -> str:
    """The steps that describe HOW to verify, not what the feature does."""
    steps = feat.get("steps", [])
    picked = [steps[i - 1] for i in METHOD_STEPS if len(steps) >= i]
    return (" " + " ".join(picked or steps) + " ").lower()


def observed_serialized() -> dict[str, str]:
    """Features a REAL session already found non-solo, with its note text.

    This is the only ground truth available. The templated ``steps[]`` prose is
    not trustworthy in either direction: SRS-MD-003's step 2 says "fixture market
    data, provider mocks", and its session note records an actual live IB window
    with a wedged gateway. An observation beats an inference.
    """
    notes: dict[str, str] = {}
    notes_dir = ROOT / "progress.d"
    if not notes_dir.is_dir():
        return notes
    for path in sorted(notes_dir.glob("session-*.md")):
        fid = path.stem[len("session-") :]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        outcome = next((ln for ln in lines if ln.lower().startswith("outcome:")), "")
        if "serialized" not in outcome.lower():
            continue
        # Only the sentences that say WHY it was serialized. Scanning the whole
        # note is the same mistake the old guard made on descriptions: a 74 KB
        # note mentions "dashboard" incidentally a dozen times.
        why_lines = [outcome]
        for i, ln in enumerate(lines):
            low = ln.lower()
            if low.startswith(("resume / next:", "resume/next:", "outcome:")) or (
                "serialized" in low and not low.startswith("adversarial")
            ):
                why_lines.extend(lines[i : i + 3])
        notes[fid] = " ".join(why_lines).lower()
    return notes


def _flavour(hay: str, default: str = "integration") -> str:
    """Which non-solo method does this text point at?"""
    if any(n in hay for n in BROWSER) or any(n in hay for n in SURFACE):
        flavour = "e2e"
    else:
        flavour = default
    # A real IB dependency outranks a dashboard one: the single-live-IB invariant
    # is the binding constraint on parallelism.
    if any(n in hay for n in REAL_IB):
        return "live-ib"
    return flavour


def derive(feat: dict, observed: dict[str, str] | None = None) -> tuple[str, str, bool]:
    """Propose (method, why, needs_review).

    Never downgrades to ``solo`` on ambiguity. Marking a feature solo when it is
    not is the permissive error — it lets `integrate --mode complete` flip
    `passes` on work nobody verified. Marking it non-solo merely costs an explicit
    operator attestation. Unknown fails to the safe side (CLAUDE.md rule 3).
    """
    observed = observed if observed is not None else observed_serialized()
    fid = feat.get("id", "")
    hay = method_text(feat)

    if fid in observed:
        return _flavour(observed[fid]), "OBSERVED: a session recorded Outcome: serialized", False

    stubbed = next((s for s in STUBBED if s in hay), "")
    for label, needles in (
        ("e2e", BROWSER),
        ("live-ib", REAL_IB),
        ("integration", INTEGRATION),
        ("e2e", SURFACE),
        ("integration", RESOURCE),
    ):
        for n in needles:
            if n in hay:
                if stubbed:
                    # Conflict: the method names a real resource AND a stub for it.
                    # Propose the conservative answer and make a human look.
                    return label, f"REVIEW: names {n!r} but also {stubbed!r}", True
                return label, f"method says {n!r}", False

    # Nothing external was NAMED — but the list of names can never be complete, so
    # absence of a hit is not evidence of self-containment. Require a positive one.
    contained = next((s for s in SELF_CONTAINED if s in hay), "")
    if contained:
        return "solo", f"method names only self-contained means ({contained!r})", False
    return (
        "integration",
        "REVIEW: names no external resource, but also no self-contained means — "
        "solo must be earned, not assumed",
        True,
    )


def cmd_propose(args) -> int:
    features = load_features()
    lines = [
        "# verification_method review — EDIT THE METHOD COLUMN, then:",
        "#     python3 tools/classify_verification.py apply",
        "#",
        "# solo        every step verifiable in a parallel session (no shared resource)",
        "# integration needs real containers / gated I/O (ATP_RUN_INTEGRATION=1)",
        "# live-ib     needs the IB Gateway — serialized by the single-live invariant",
        "# e2e         needs the dashboard / Jupyter / Playwright stack",
        "#",
        "# The 'why' column is the phrase that produced the proposal. It is derived from",
        "# the same templated prose the old keyword guard used, so treat it as a hint,",
        "# not an answer — the whole point of this file is that a human decides.",
        "#",
        f"# {'id':<20} {'method':<12} why",
    ]
    observed = observed_serialized()
    counts: dict[str, int] = {}
    rows: list[tuple[bool, str, str, str]] = []
    for feat in sorted(features, key=lambda f: f["id"]):
        current = str(feat.get("verification_method") or "").strip()
        if current and not args.rederive:
            method, why, review = current, "already declared", False
        else:
            method, why, review = derive(feat, observed)
            if current and method != current:
                why = f"RE-DERIVED {current} → {method}: {why}"
        counts[method] = counts.get(method, 0) + 1
        rows.append((review, feat["id"], method, why))

    # Conflicts first — they are the only lines that genuinely need a decision.
    needs_review = [r for r in rows if r[0]]
    if needs_review:
        lines.append(
            f"# ── {len(needs_review)} CONFLICT(S): the method names a real resource AND a stub"
        )
        lines.append("#    for it. Proposed conservatively (non-solo). Decide these first.")
        for _, fid, method, why in needs_review:
            lines.append(f"  {fid:<20} {method:<12} # {why}")
        lines.append("# ── the rest")
    for review, fid, method, why in rows:
        if not review:
            lines.append(f"  {fid:<20} {method:<12} # {why}")

    text = "\n".join(lines) + "\n"
    if args.stdout:
        print(text, end="")
    else:
        _atomic_write(REVIEW_FILE, text)
        print(f"✓ wrote {REVIEW_FILE.relative_to(ROOT)} ({len(features)} features)")
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"  proposal: {summary}", file=sys.stderr)
    print(
        "  Review it — especially every 'e2e' and 'live-ib'. A feature marked non-solo\n"
        "  can never close as `complete` without an explicit human attestation.",
        file=sys.stderr,
    )
    return 0


def cmd_apply(args) -> int:
    if not REVIEW_FILE.exists():
        print(f"✗ {REVIEW_FILE.relative_to(ROOT)} not found — run `propose` first", file=sys.stderr)
        return 2

    chosen: dict[str, str] = {}
    for raw in REVIEW_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0] if not raw.lstrip().startswith("#") else ""
        m = LINE_RE.match(line)
        if m:
            chosen[m.group(1)] = m.group(2)

    features = load_features()
    ids = {f["id"] for f in features}
    unknown = sorted(set(chosen) - ids)
    if unknown:
        print(f"✗ review file names features that do not exist: {unknown}", file=sys.stderr)
        return 1
    missing = sorted(ids - set(chosen))
    if missing:
        print(
            f"✗ {len(missing)} feature(s) have no line in the review file: "
            f"{missing[:6]}{'…' if len(missing) > 6 else ''}",
            file=sys.stderr,
        )
        return 1

    changed = 0
    for feat in features:
        want = chosen[feat["id"]]
        if feat.get("verification_method") != want:
            if args.dry_run:
                print(f"  [dry-run] {feat['id']}: {feat.get('verification_method')} → {want}")
            else:
                feat["verification_method"] = want
            changed += 1

    if args.dry_run:
        print(f"[dry-run] would set/change {changed} feature(s)")
        return 0

    raw = FEATURE_FILE.read_text(encoding="utf-8")
    body = json.dumps(features, indent=2) + ("\n" if raw.endswith("\n") else "")
    _atomic_write(FEATURE_FILE, body)
    print(f"✓ applied {len(chosen)} verification_method value(s); {changed} changed")
    return 0


def cmd_status(args) -> int:
    features = load_features()
    have = [f for f in features if str(f.get("verification_method") or "").strip()]
    counts: dict[str, int] = {}
    for f in have:
        counts[f["verification_method"]] = counts.get(f["verification_method"], 0) + 1
    print(f"classified: {len(have)}/{len(features)}")
    for k, v in sorted(counts.items()):
        print(f"  {k:<12} {v}")
    missing = [f["id"] for f in features if f not in have]
    if missing:
        print(f"  unclassified (still on the keyword fallback): {len(missing)}")
    bad = sorted({f["verification_method"] for f in have} - set(VALID))
    if bad:
        print(f"✗ invalid method value(s): {bad}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose", help="write the operator review file")
    p.add_argument("--stdout", action="store_true")
    p.add_argument(
        "--rederive",
        action="store_true",
        help="ignore existing verification_method values and derive afresh. Needed "
        "whenever the derivation itself changes: without it `propose` reports "
        "'already declared' for every classified feature and a fix to the rules "
        "silently has no effect.",
    )
    a = sub.add_parser("apply", help="apply the reviewed file to feature_list.json")
    a.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="how many features are classified")
    args = ap.parse_args(argv)
    return {"propose": cmd_propose, "apply": cmd_apply, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
