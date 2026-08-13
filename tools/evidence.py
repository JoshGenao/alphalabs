#!/usr/bin/env python3
"""Verification evidence — what was actually run, and what was actually observed.

``feature_list.json``'s ``passes: true`` is supposed to mean *evidence says done*,
not *the agent says done*. It did not. ``close_feature.py`` refuses to flip without
``--verified``, and its own error text says the flag "is supplied only when the PR
carries the 'verified-e2e' label" — but ``agent_pool.py integrate --mode complete``
passed ``--verified`` itself. The assertion and the party it constrained were the
same party.

This module is the missing artifact. An agent records, per feature step, the exact
command it ran and what it observed; ``verify`` answers whether that record covers
every step in the feature's ``steps[]`` with a passing result and both critic
verdicts. ``close_feature.py`` consults it, and ``agent_pool.py`` degrades a
``complete`` integrate to ``serialized`` when the record is incomplete — so the
honest outcome is the automatic one.

    tools/evidence.py run      <FID> --step 2 -- pytest tests/domain/test_x.py -q
    tools/evidence.py record   <FID> --step 2 --command "..." --observed "..." --status pass
    tools/evidence.py artifact <FID> --step 3 --file shot.png --caption "stale row"
    tools/evidence.py gate     <FID> --name run_ci_locally --status pass
    tools/evidence.py critic   <FID> --layer judgment --verdict approve --reviewer codex
    tools/evidence.py verify   <FID> [--json] [--allow-attested]
    tools/evidence.py render   <FID> [--stdout]
    tools/evidence.py show     <FID>
    tools/evidence.py stamp-pre-gate --all

``artifact`` attaches a screenshot, recording, or trace to a step and ``render``
writes ``.harness/runs/<FID>/EVIDENCE.md`` — the form a human reviews on GitHub,
with the images inline. A captured exit code proves a command ran; it cannot show
that the dashboard displayed the stale row. For ``e2e`` and ``live-ib`` features,
whose acceptance criteria are stated in terms of what is displayed, ``verify``
REQUIRES an image on the acceptance-criterion step.

``run`` executes the command and stores its real exit code and captured output;
``verify`` accepts only those. ``record`` takes the caller's word (executed:false)
and counts only when a human closes with ``--attested-by``. The record is also bound
to the feature's ``steps[]`` digest, and is retired (archived alongside itself) when
the feature closes, so a reopened feature cannot inherit the evidence of an earlier
session and a re-specified feature cannot be satisfied by a stale one.

Exit codes: 0 = ok / record complete, 1 = incomplete or missing, 2 = usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEATURE_FILE = ROOT / "feature_list.json"
HARNESS_DIR = ROOT / ".harness"
# Per-feature evidence is worktree-local: the agent commits its own record with its
# branch, and shared_state_violations() confines it to .harness/runs/<own-id>/.
RUNS_DIR = HARNESS_DIR / "runs"


def _primary_checkout() -> Path:
    """The main worktree (git lists it first), shared by every worktree."""
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "worktree", "list", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree ") :]).resolve()
    return ROOT


# The audit ledgers live in the PRIMARY checkout, never in the agent's worktree.
# When they lived alongside the evidence, `.harness` had to join INTEGRATE_ALLOWLIST
# so the integrator could stage what it wrote — and `git add -A -- .harness` then
# swept in whatever the agent had left uncommitted there, including a hand-appended
# closes.jsonl, and pushed it to main. A branch-side guard could not catch it because
# it only inspects committed paths. Keeping the ledgers outside the tree the agent
# controls removes the vector instead of policing it.
LEDGER_DIR = _primary_checkout() / ".harness"
CLOSES_LOG = LEDGER_DIR / "closes.jsonl"
OVERRIDES_LOG = LEDGER_DIR / "overrides.jsonl"

# A feature closed before this gate existed. Not re-verified, and deliberately not
# equated with an evidenced close — `agent_pool.py status` counts them separately.
PRE_GATE = "pre-gate"


class EvidenceError(Exception):
    """Something is wrong with an evidence record itself, not with the work."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    """Temp file + rename in the same dir (same pattern as close_feature.py)."""
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


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True) + "\n")


def record_path(fid: str) -> Path:
    return RUNS_DIR / fid / "evidence.json"


def _rel(path: Path) -> str:
    """Repo-relative for display, absolute when the path is outside the repo.

    ``Path.relative_to`` RAISES on a non-descendant, and it was being called on the
    record path inside the two messages that report a MISSING or CORRUPT record —
    the failure paths. A ValueError there escapes ``evidence.verify`` as neither a
    verdict nor an EvidenceError, so ``close_feature.py``'s ``except
    EvidenceError`` would not catch it and the close would traceback instead of
    refusing. Formatting a path must never be able to fail.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# ----------------------------------------------------------------------------
# Artifacts — the part of the evidence a human can look at
# ----------------------------------------------------------------------------
# A captured exit code proves a command ran. It does not let a reviewer SEE that
# the dashboard showed the stale row, or that the liquidation banner appeared. For
# an e2e or live-IB acceptance criterion — the ones stated in terms of what is
# displayed — a reviewer who cannot see it is taking the record's word for it,
# which is the same trust boundary this module exists to remove.
#
# Storage is `.harness/runs/<fid>/artifacts/`, alongside the record and inside the
# path shared_state_violations already confines a branch to, so artifacts ride the
# feature's own PR and land on main with its close.
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
VIDEO_EXT = (".webm", ".mp4")
TRACE_EXT = (".zip",)
ARTIFACT_EXT = IMAGE_EXT + VIDEO_EXT + TRACE_EXT + (".txt", ".log", ".json")

# This repository has no git-lfs, so every byte committed here is permanent
# history for every future clone and every worktree cut from it. A 10-second
# Playwright webm at 800x600 is tens of kilobytes; anything near these ceilings is
# a full-length screen recording that belongs in a CI artifact, not in git.
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_VIDEO_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024

# Methods whose acceptance criteria are stated in terms of what a human SEES, and
# which therefore cannot be closed on captured stdout alone.
VISUAL_METHODS = ("e2e", "live-ib")


def artifacts_dir(fid: str) -> Path:
    return RUNS_DIR / fid / "artifacts"


def artifact_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in TRACE_EXT:
        return "trace"
    return "file"


def attach(fid: str, n: int, src: Path, caption: str = "") -> dict:
    """Copy a file into the feature's artifact dir and record it against step n.

    Refuses rather than truncates. A silently-dropped artifact would leave a record
    that claims a screenshot exists and a directory that does not contain it —
    "unreadable, absent, or unknown is NEVER empty" (CLAUDE.md rule 3) applies to
    the thing being written, not only to the thing being read.
    """
    src = Path(src)
    if not src.is_file():
        raise EvidenceError(f"artifact not found: {src}")
    ext = src.suffix.lower()
    if ext not in ARTIFACT_EXT:
        raise EvidenceError(
            f"{src.name}: {ext or '(no extension)'} is not an artifact type this "
            f"repo stores ({', '.join(ARTIFACT_EXT)})"
        )
    size = src.stat().st_size
    kind = artifact_kind(src.name)
    cap = {"image": MAX_IMAGE_BYTES, "video": MAX_VIDEO_BYTES}.get(kind)
    if cap and size > cap:
        raise EvidenceError(
            f"{src.name} is {size / 1e6:.1f} MB; the cap for a {kind} is "
            f"{cap / 1e6:.0f} MB. This repo has no git-lfs, so every byte is "
            f"permanent history for every future clone. Crop the screenshot, or "
            f"shorten/downscale the recording."
        )
    dest_dir = artifacts_dir(fid)
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = sum(p.stat().st_size for p in dest_dir.glob("*") if p.is_file())
    dest = dest_dir / f"step{n}-{src.name}"
    already = dest.stat().st_size if dest.exists() else 0
    if existing - already + size > MAX_TOTAL_BYTES:
        raise EvidenceError(
            f"{fid} artifacts would total "
            f"{(existing - already + size) / 1e6:.1f} MB, over the "
            f"{MAX_TOTAL_BYTES / 1e6:.0f} MB per-feature cap"
        )
    dest.write_bytes(src.read_bytes())

    rec = load_record(fid)
    entry = next((s for s in rec.get("steps", []) if s.get("n") == n), None)
    if entry is None:
        raise EvidenceError(
            f"step {n} has no record yet — run or record it first, then attach. An "
            f"artifact with no step is evidence of nothing in particular."
        )
    art = {
        "name": dest.name,
        "kind": kind,
        "bytes": size,
        "caption": caption,
        "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()[:16],
        "ts": _now(),
    }
    entry["artifacts"] = [a for a in entry.get("artifacts", []) if a.get("name") != dest.name]
    entry["artifacts"].append(art)
    save_record(fid, rec)
    return art


def step_artifacts(rec: dict, n: int) -> list:
    entry = next((s for s in rec.get("steps", []) if s.get("n") == n), None)
    return list((entry or {}).get("artifacts", []))


def ac_step_index(steps: list) -> int | None:
    """Which step states the acceptance criterion.

    Matched on the text rather than hard-coded to 3: the 4-step shape is a
    generator template, not a guarantee, and a feature written by hand would
    silently get its artifact requirement applied to the wrong step.
    """
    for i, text in enumerate(steps, start=1):
        if "verify acceptance criteria" in str(text).lower():
            return i
    return None


def steps_digest(steps: list[str]) -> str:
    """Fingerprint of the acceptance steps this record claims to have satisfied.

    Without it, ``verify`` matched step *indices* only, so re-specifying a feature —
    editing what step 3 demands — left the old record satisfying the new criteria.
    """
    return hashlib.sha256("\n".join(steps).encode("utf-8")).hexdigest()[:16]


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args], check=False, capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _head() -> str:
    return _git("rev-parse", "HEAD")


def retire(fid: str, *, dry_run: bool = False) -> Path | None:
    """Archive the live record once its feature has been closed.

    ``.harness/runs/`` is tracked, so every worktree cut from ``origin/main``
    inherits the evidence of every feature closed before it — a reopened feature
    would arrive already "verified" by a session that was not this one.

    An earlier attempt rejected any record that had reached main. That made the gate
    unsatisfiable: the record legitimately arrives on main as part of the very close
    it justifies, and ``close-feature.yml`` runs ON main after the merge, so the
    human-attested path could never pass. Retiring the record instead is the same
    lifecycle ``close_feature.py`` already applies to ``progress.d/session-<id>.md``:
    the live file is consumed by the close, so a reopened feature starts with none.
    """
    stamp = _now().replace(":", "").replace("-", "")
    live = record_path(fid)
    # review.jsonl is tracked and staged to main by every integrate mode, so without
    # this a later session on the same feature inherits the previous session's rounds
    # and agent_pool's own note_rounds_mismatch hard-blocks its integrate. Same
    # inheritance defect as the evidence record, in the file added to detect it.
    rounds = live.with_name("review.jsonl")
    if rounds.exists() and not dry_run:
        rounds.rename(rounds.with_name(f"closed-{stamp}-review.jsonl"))
    # Artifacts and the rendered page retire with the record, for the same reason:
    # they are tracked, so a reopened feature would otherwise start with a
    # screenshot of the PREVIOUS session's dashboard sitting where its own
    # evidence belongs — and a stale screenshot is more convincing than a stale
    # exit code, not less.
    arts = artifacts_dir(fid)
    if arts.is_dir() and any(arts.iterdir()) and not dry_run:
        arts.rename(arts.with_name(f"closed-{stamp}-artifacts"))
    page = live.with_name("EVIDENCE.md")
    if page.exists() and not dry_run:
        page.rename(page.with_name(f"closed-{stamp}-EVIDENCE.md"))
    if not live.exists():
        return None
    archived = live.with_name(f"closed-{stamp}.json")
    if not dry_run:
        live.rename(archived)
    return archived


def load_features() -> list:
    if not FEATURE_FILE.exists():
        raise EvidenceError(f"{FEATURE_FILE.name} not found")
    return json.loads(FEATURE_FILE.read_text(encoding="utf-8"))


def feat_of(fid: str, features: list | None = None) -> dict | None:
    feats = features if features is not None else load_features()
    return next((f for f in feats if f.get("id") == fid), None)


def feature_steps(fid: str, features: list | None = None) -> list[str]:
    feats = features if features is not None else load_features()
    match = next((f for f in feats if f.get("id") == fid), None)
    if match is None:
        raise EvidenceError(f"{fid} not found in feature_list.json")
    return list(match.get("steps", []))


def load_record(fid: str) -> dict:
    path = record_path(fid)
    if not path.exists():
        return {"feature": fid, "created": _now(), "steps": [], "gates": {}, "critic": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # An unreadable record is NOT an empty one (CLAUDE.md rule 3): a corrupt
        # file must fail closed, never present as "no evidence recorded yet".
        raise EvidenceError(f"{_rel(path)} is corrupt: {exc}") from exc


def save_record(fid: str, rec: dict) -> None:
    rec["updated"] = _now()
    _atomic_write(record_path(fid), json.dumps(rec, indent=2, sort_keys=True) + "\n")


# Commands the INTEGRATOR is willing to run itself. Re-execution is what turns a
# record from the agent's testimony into the integrator's own observation — but a
# recorded command is a string the AGENT chose, so re-running it unrestricted would
# have the integrator execute agent-authored shell. These are the verification
# commands the coding prompt actually tells a session to run; anything else is not
# re-executable, which costs the feature an explicit human attestation rather than
# silently granting it a machine one.
REEXECUTABLE = (
    r"^\./init\.sh(\s|$)",
    r"^(python3?|\.venv/bin/python3?)\s+-m\s+pytest(\s|$)",
    r"^pytest(\s|$)",
    r"^cargo\s+(test|fmt|clippy)(\s|$)",
    r"^(python3?|\.venv/bin/python3?)\s+tools/[a-z0-9_]+_check\.py(\s|$)",
    r"^tools/(verify_contracts|run_ci_locally)\.sh(\s|$)",
    r"^(python3?|\.venv/bin/python3?)\s+tools/(gates_registry_check|docs_link_check|portability_probe)\.py(\s|$)",
    r"^ruff\s+(check|format)(\s|$)",
    r"^mypy(\s|$)",
)

# A single verification command that has not finished in this long is not a pass.
REEXEC_TIMEOUT = int(os.environ.get("ATP_REEXEC_TIMEOUT", 900))


def reexecutable(command: str) -> bool:
    """May the integrator run this itself?"""
    cmd = (command or "").strip()
    # No shell metacharacters: the integrator runs argv, never a shell, so a command
    # that needs one cannot be reproduced faithfully and must not be approximated.
    if any(ch in cmd for ch in ";|&><`$()"):
        return False
    return any(re.match(pat, cmd) for pat in REEXECUTABLE)


def reexecute(fid: str, cwd: Path, features: list | None = None) -> tuple[bool, list[str]]:
    """Re-run this feature's recorded commands and compare against what was recorded.

    The record is written by the party being audited. `run` makes the honest path the
    easy one, but ``executed: true`` is still a boolean in a file the agent's branch
    controls. Re-execution removes the agent from the loop: the pass/fail the close
    depends on is produced HERE, by the integrator, from the same commands.

    WHAT THIS DOES AND DOES NOT ESTABLISH — stated precisely, because the difference
    is easy to overclaim:

    * It establishes that every recorded command IS a real verification command and
      DOES exit as recorded, right now, run by the integrator. A record claiming a
      passing test run that would actually fail is caught. So is one whose "command"
      is ``true``, ``echo``, or anything needing a shell.
    * It does NOT establish that the agent personally ran anything first — and it
      does not need to. If the claim is true when the integrator checks it, the claim
      is true; who typed it first is not the property worth defending.
    * It does NOT establish that the commands are RELEVANT to the feature's steps. An
      agent can record a real, passing, unrelated test suite. Nothing mechanical can
      close that: judging whether the evidence matches the acceptance criteria is
      what the adversarial reviewer does, reading the diff and the session note.

    Returns (ok, problems). Never raises for a command's own failure — a failing
    command is a finding, not an error.
    """
    problems: list[str] = []
    try:
        rec = load_record(fid)
    except EvidenceError as exc:
        return False, [f"evidence record unreadable: {exc}"]

    steps = rec.get("steps", [])
    if not steps:
        return False, ["no recorded steps to re-execute"]

    for entry in sorted(steps, key=lambda s: s.get("n", 0)):
        n, cmd = entry.get("n"), (entry.get("command") or "").strip()
        if not entry.get("executed"):
            continue  # hand-recorded: the human attestation path covers these
        if not reexecutable(cmd):
            problems.append(
                f"step {n}: {cmd!r} is not a command the integrator will re-run — "
                f"close with an explicit --attested-by instead"
            )
            continue
        # Replay the recorded argv. Fall back to shlex.split (never .split) for a
        # record written before argv was stored: splitting on whitespace regroups
        # every quoted argument, which turned `pytest -m "not integration and not
        # e2e"` — the standard solo-test command — into seven separate tokens.
        argv = entry.get("argv") or shlex.split(cmd)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=REEXEC_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            problems.append(f"step {n}: {cmd!r} did not finish in {REEXEC_TIMEOUT}s")
            continue
        except OSError as exc:
            problems.append(f"step {n}: {cmd!r} could not be run ({exc})")
            continue
        recorded = entry.get("exit_code")
        if proc.returncode != recorded:
            problems.append(
                f"step {n}: recorded exit {recorded}, integrator observed "
                f"{proc.returncode} re-running {cmd!r}"
            )
    return (not problems), problems


def verify(
    fid: str, features: list | None = None, *, allow_attested: bool = False
) -> tuple[bool, list[str], dict]:
    """Does the record cover every step with a passing result and both verdicts?

    Returns (ok, problems, summary). `ok` is the only thing entitled to flip
    ``passes`` to true on the local path.

    ``allow_attested`` admits steps the tool did not execute itself. It exists for
    the human path — a live IB window or a browser check cannot be captured by a
    subprocess — and ``close_feature.py`` passes it only together with an explicit
    ``--attested-by``. Without it, only ``evidence.py run`` output counts, so the
    record is something the agent produced by doing the work rather than by
    describing it.
    """
    problems: list[str] = []
    path = record_path(fid)

    if not path.exists():
        # Report the real denominator. Saying "0/0 steps" for a feature with four
        # unverified steps understates the gap to exactly the size of nothing —
        # the caller (agent_pool's degrade message) prints this to the operator.
        return (
            False,
            [
                f"no evidence record at {_rel(path)} — record one with "
                f"`tools/evidence.py record {fid} --step N --command ... --observed ...`"
            ],
            {"steps_total": len(feature_steps(fid, features)), "steps_evidenced": 0},
        )

    rec = load_record(fid)
    steps = feature_steps(fid, features)

    want = steps_digest(steps)
    stale = sorted(
        e.get("n")
        for e in rec.get("steps", [])
        if e.get("steps_digest", rec.get("steps_digest")) != want
    )
    if stale:
        problems.append(
            f"step(s) {stale} were recorded against a different specification (the "
            f"feature's steps[] changed since); re-verify them — one fresh step does "
            f"not re-bless the rest"
        )

    by_n = {}
    for entry in rec.get("steps", []):
        n = entry.get("n")
        if n in by_n:
            problems.append(f"step {n} is recorded twice")
        by_n[n] = entry

    for idx in range(1, len(steps) + 1):
        entry = by_n.get(idx)
        if entry is None:
            problems.append(f"step {idx} has no evidence: {steps[idx - 1][:70]}")
            continue
        if not (entry.get("command") or "").strip():
            problems.append(f"step {idx} records no command")
        # A command that exits 0 with no output is a normal, meaningful verification
        # (`cargo fmt --check`, `git diff --exit-code`). For an EXECUTED step the exit
        # code is the observation, so requiring non-empty text would push exactly those
        # steps onto the hand-recorded path and demand a human attestation for them.
        # For a hand-recorded step the text is the only evidence there is.
        if not (entry.get("observed") or "").strip() and not entry.get("executed"):
            problems.append(f"step {idx} records no observed output")
        status = entry.get("status")
        if status != "pass":
            problems.append(f"step {idx} status is {status!r}, not 'pass'")
        if not entry.get("executed") and not allow_attested:
            problems.append(
                f"step {idx} was hand-recorded, not executed — re-run it with "
                f"`tools/evidence.py run {fid} --step {idx} -- <command>`, or close "
                f"with an explicit human --attested-by"
            )
        if entry.get("executed"):
            # `not in (0, None)` treated a MISSING exit_code as success, so a
            # hand-written record claiming executed:true and omitting it passed.
            # An absent exit code is unknown, and unknown is not zero.
            code = entry.get("exit_code")
            if code is None:
                problems.append(
                    f"step {idx} claims executed but records no exit_code — "
                    f"re-run it with `tools/evidence.py run`"
                )
            elif code != 0:
                problems.append(f"step {idx} exited {code}, not 0")

    for extra in sorted(n for n in by_n if isinstance(n, int) and n > len(steps)):
        problems.append(f"step {extra} is recorded but the feature has only {len(steps)} steps")

    critic = rec.get("critic", {})
    for layer in ("deterministic", "judgment"):
        got = critic.get(layer)
        if not got:
            problems.append(f"no {layer} critic verdict recorded")
        elif got.get("verdict") != "approve":
            problems.append(f"{layer} critic verdict is {got.get('verdict')!r}, not 'approve'")

    # A visual acceptance criterion needs a visual artifact. "the dashboard shows
    # IB equity, daily and cumulative P&L, margin usage" is a claim about what a
    # human would SEE; closing it on a captured exit code asks the reviewer to take
    # the record's word for exactly the thing the record cannot show. Applied only
    # to e2e and live-ib: for a solo feature the captured stdout IS the artifact,
    # and demanding a screenshot of `cargo fmt --check` would teach everyone to
    # produce a meaningless one.
    method = str((feat_of(fid, features) or {}).get("verification_method") or "").strip().lower()
    if method in VISUAL_METHODS and steps:
        ac_n = ac_step_index(steps)
        targets = [ac_n] if ac_n else list(range(1, len(steps) + 1))
        images = [a for n in targets for a in step_artifacts(rec, n) if a.get("kind") == "image"]
        if not images:
            where = f"step {ac_n}" if ac_n else "any step"
            problems.append(
                f"verification_method is {method!r} but no image artifact is attached to "
                f"{where} — its acceptance criterion is about what a reviewer can SEE. "
                f"Attach one: `tools/evidence.py artifact {fid} --step "
                f"{ac_n or 'N'} --file <screenshot.png> --caption '...'`"
            )

    summary = {
        "steps_total": len(steps),
        "steps_evidenced": sum(
            1 for i in range(1, len(steps) + 1) if by_n.get(i, {}).get("status") == "pass"
        ),
        "critic": {k: v.get("verdict") for k, v in critic.items()},
        "artifacts": sum(len(step_artifacts(rec, i)) for i in range(1, len(steps) + 1)),
    }
    return (not problems, problems, summary)


def render_markdown(fid: str, features: list | None = None) -> str:
    """The reviewable form of the record, for GitHub.

    evidence.json is the machine's copy; nobody reviews a 3 KB blob of escaped
    stdout in a PR diff. This renders the same facts as a page GitHub displays:
    the acceptance criterion, each step's command and captured output, and the
    screenshots INLINE.

    Images are written as relative links because that is what GitHub renders from
    a repo path. Video is NOT rendered inline — GitHub plays video only for files
    uploaded into a comment, never from a repo path — so it is linked, and the
    link says so rather than leaving a reviewer clicking a broken player.
    """
    feat = feat_of(fid, features) or {}
    steps = list(feat.get("steps", []))
    try:
        rec = load_record(fid)
    except EvidenceError as exc:
        return f"# {fid}\n\n**The evidence record is unreadable:** {exc}\n"
    ok, problems, summary = verify(fid, features, allow_attested=True)
    ac_n = ac_step_index(steps)

    out = [f"# {fid} — verification evidence", ""]
    out.append(f"> {feat.get('description', '')}")
    out.append("")
    out.append(f"- **method**: `{feat.get('verification_method') or '(unclassified)'}`")
    out.append(f"- **steps evidenced**: {summary.get('steps_evidenced', 0)}/{len(steps)}")
    out.append(
        "- **critics**: "
        + (
            ", ".join(f"{k} `{v}`" for k, v in (summary.get("critic") or {}).items())
            or "none recorded"
        )
    )
    out.append(f"- **artifacts**: {summary.get('artifacts', 0)}")
    out.append(f"- **record complete**: {'yes' if ok else 'NO'}")
    out.append("")
    if problems:
        out += ["## Outstanding", ""]
        out += [f"- {p}" for p in problems]
        out.append("")
    if ac_n:
        out += ["## Acceptance criterion", "", f"> {steps[ac_n - 1]}", ""]

    out.append("## Steps")
    for i in range(1, len(steps) + 1):
        entry = next((s for s in rec.get("steps", []) if s.get("n") == i), None)
        out += ["", f"### Step {i}", "", f"{steps[i - 1]}", ""]
        if entry is None:
            out.append("**No evidence recorded for this step.**")
            continue
        how = (
            "executed by the tool" if entry.get("executed") else "hand-recorded (needs attestation)"
        )
        out.append(f"`{entry.get('status', '?')}` · exit `{entry.get('exit_code', 'n/a')}` · {how}")
        if entry.get("command"):
            out += ["", "```", str(entry["command"]), "```"]
        observed = (entry.get("observed") or "").strip()
        if observed:
            # Long captures are the norm (a pytest run is thousands of lines) and a
            # PR page that scrolls for a screen and a half gets skipped. Collapsed,
            # with the tail — which is where the verdict is.
            tail = "\n".join(observed.splitlines()[-40:])
            out += [
                "",
                "<details><summary>observed output (tail)</summary>",
                "",
                "```",
                tail,
                "```",
                "",
                "</details>",
            ]
        arts = entry.get("artifacts") or []
        if arts:
            out.append("")
            for a in arts:
                rel = f"artifacts/{a['name']}"
                cap = a.get("caption") or a["name"]
                if a.get("kind") == "image":
                    out.append(f"![{cap}]({rel})")
                    out.append("")
                    out.append(f"*{cap}*")
                elif a.get("kind") == "video":
                    out.append(
                        f"🎬 [{cap}]({rel}) — {a['bytes'] / 1e6:.1f} MB. GitHub does not "
                        f"play video from a repo path; download it, or open the PR's "
                        f"CI artifact."
                    )
                else:
                    out.append(f"📎 [{cap}]({rel}) — {a['bytes'] / 1e3:.0f} KB")
                out.append("")
    out += [
        "",
        "---",
        "",
        "Generated by `tools/evidence.py render`. `passes: true` requires either every "
        "step executed by the tool with both critics approving, or a named human "
        "attestation — see `AGENTS.md`.",
    ]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------
def _store_step(fid: str, n: int, entry: dict) -> None:
    rec = load_record(fid)
    # Bind EACH step to the spec in force when that step was taken. Stamping one
    # whole-record digest on every write meant recording a single step after a
    # respecification silently re-blessed every earlier step, which had been captured
    # against criteria that no longer existed.
    entry["steps_digest"] = steps_digest(feature_steps(fid))
    entry["head"] = _head()
    # Carry forward artifacts already attached to this step.
    #
    # A browser test attaches its screenshots from INSIDE the subprocess that
    # `run` is executing, so they land on the step entry before `run` writes its
    # own. Replacing the entry wholesale then dropped every one of them: the
    # record came back with `artifacts: []` while the files sat in the artifacts
    # directory, which is exactly the inverse of the failure `attach` refuses to
    # cause ("a record that claims a screenshot exists and a directory that does
    # not contain it"). Same rule, other direction.
    #
    # `attach` de-duplicates by stored name, so a re-run replaces its own shots
    # rather than piling up duplicates.
    # Only those whose FILE is still on disk: carrying a name whose bytes were
    # cleaned up would make the record claim an artifact the directory does not
    # hold, which is the same lie in the other direction.
    previous = next((s for s in rec.get("steps", []) if s.get("n") == n), None)
    if previous and previous.get("artifacts") and not entry.get("artifacts"):
        live_dir = artifacts_dir(fid)
        entry["artifacts"] = [
            a for a in previous["artifacts"] if (live_dir / str(a.get("name"))).is_file()
        ]
    rec["steps"] = [s for s in rec.get("steps", []) if s.get("n") != n]
    rec["steps"].append(entry)
    rec["steps"].sort(key=lambda s: s["n"])
    save_record(fid, rec)


def _check_step_range(fid: str, n: int) -> list[str] | None:
    steps = feature_steps(fid)
    if not 1 <= n <= len(steps):
        print(f"✗ step {n} is out of range: {fid} has {len(steps)} step(s)", file=sys.stderr)
        return None
    return steps


def cmd_run(args) -> int:
    """Execute the step's command here and record what actually happened.

    This is the difference between evidence and testimony. ``record`` takes the
    caller's word for both the command and its output, so a determined agent can
    satisfy the whole gate with shell echoes — it raises the cost of a false green
    without removing it. ``run`` executes the command itself and stamps
    ``executed: true`` with the real exit code and captured output, which is the
    only thing ``verify`` accepts by default (see ``--allow-attested``).
    """
    steps = _check_step_range(args.id, args.step)
    if steps is None:
        return 2
    cmd = args.command
    print(f"→ step {args.step}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    status = "pass" if proc.returncode == 0 else "fail"
    _store_step(
        args.id,
        args.step,
        {
            "n": args.step,
            "step_text": steps[args.step - 1],
            # BOTH forms. `command` is the human-readable record; `argv` is what the
            # integrator replays. Storing only the joined string and re-splitting it
            # regrouped `pytest -m "not integration and not e2e"` — the standard
            # solo-test command the coding prompt tells agents to record — into seven
            # tokens, so re-execution failed on the very command it exists to check.
            "command": shlex.join(cmd),
            "argv": list(cmd),
            "observed": out[-4000:],
            "exit_code": proc.returncode,
            "status": status,
            "executed": True,
            "ts": _now(),
        },
    )
    tail = out.splitlines()[-1][:100] if out else "(no output)"
    print(f"{'✓' if status == 'pass' else '✗'} {args.id} step {args.step}: {status} — {tail}")
    _refresh_markdown(args.id)
    return 0 if status == "pass" else 1


def cmd_record(args) -> int:
    """Record a step the tool could not execute (a live IB window, a browser check).

    Marked ``executed: false``. ``verify`` rejects these unless the caller passes
    ``--allow-attested``, which ``close_feature.py`` only does alongside an explicit
    human ``--attested-by``. An agent cannot promote its own hand-written record.
    """
    steps = _check_step_range(args.id, args.step)
    if steps is None:
        return 2
    _store_step(
        args.id,
        args.step,
        {
            "n": args.step,
            "step_text": steps[args.step - 1],
            "command": args.command,
            "observed": args.observed,
            "status": args.status,
            "executed": False,
            "ts": _now(),
        },
    )
    print(f"✓ {args.id} step {args.step}: {args.status} (attested, NOT executed by the tool)")
    _refresh_markdown(args.id)
    return 0


def cmd_gate(args) -> int:
    rec = load_record(args.id)
    rec.setdefault("gates", {})[args.name] = {"status": args.status, "ts": _now()}
    save_record(args.id, rec)
    print(f"✓ {args.id} gate {args.name}: {args.status}")
    return 0


def cmd_critic(args) -> int:
    rec = load_record(args.id)
    entry = {"verdict": args.verdict, "ts": _now()}
    if args.reviewer:
        entry["reviewer"] = args.reviewer
    if args.rounds is not None:
        entry["rounds"] = args.rounds
    rec.setdefault("critic", {})[args.layer] = entry
    save_record(args.id, rec)
    print(f"✓ {args.id} critic[{args.layer}]: {args.verdict}")
    return 0


def cmd_verify(args) -> int:
    ok, problems, summary = verify(args.id, allow_attested=getattr(args, "allow_attested", False))
    if args.json:
        print(json.dumps({"ok": ok, "problems": problems, "summary": summary}, indent=2))
        return 0 if ok else 1
    if ok:
        print(
            f"✓ {args.id}: evidence complete — {summary['steps_evidenced']}/"
            f"{summary['steps_total']} steps, both critics approve"
        )
        return 0
    print(f"✗ {args.id}: evidence INCOMPLETE ({len(problems)} problem(s))")
    for p in problems:
        print(f"    · {p}")
    return 1


def cmd_show(args) -> int:
    path = record_path(args.id)
    if not path.exists():
        print(f"✗ no evidence record for {args.id}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_artifact(args) -> int:
    """Attach a screenshot / recording / trace to a step."""
    try:
        art = attach(args.id, args.step, Path(args.file), args.caption or "")
    except EvidenceError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    print(
        f"✓ {args.id} step {args.step}: attached {art['name']} "
        f"({art['kind']}, {art['bytes'] / 1e3:.0f} KB)"
    )
    _write_markdown(args.id)
    return 0


def _refresh_markdown(fid: str) -> None:
    """Keep EVIDENCE.md in step with the record, without ever costing a record.

    Best-effort on purpose: writing the evidence is the load-bearing action, and a
    rendering failure (a missing feature entry, an unwritable dir) must not turn a
    successful capture into a non-zero exit that the caller reads as "the step
    failed". The page is regenerable from the record at any time; the record is not
    regenerable from anything.
    """
    try:
        _write_markdown(fid)
    except (EvidenceError, OSError):
        pass


def _write_markdown(fid: str) -> Path:
    out = RUNS_DIR / fid / "EVIDENCE.md"
    _atomic_write(out, render_markdown(fid))
    return out


def cmd_render(args) -> int:
    """Regenerate EVIDENCE.md — the form a human reviews in the PR."""
    if args.stdout:
        print(render_markdown(args.id), end="")
        return 0
    out = _write_markdown(args.id)
    print(f"✓ wrote {_rel(out)}")
    return 0


def cmd_stamp_pre_gate(args) -> int:
    """Mark already-passing features as closed before this gate existed.

    Honest bookkeeping, not a backfill: these were never evidenced, and pretending
    otherwise would be the exact defect this module exists to remove.
    """
    features = load_features()
    targets = (
        [f for f in features if f.get("passes") is True]
        if args.all
        else [f for f in features if f.get("id") in set(args.ids)]
    )
    if not targets:
        print("no matching features")
        return 0
    changed = 0
    for feat in targets:
        if feat.get("evidence") == PRE_GATE:
            continue
        if args.dry_run:
            print(f"  [dry-run] would stamp {feat['id']} evidence={PRE_GATE}")
        else:
            feat["evidence"] = PRE_GATE
        changed += 1
    if args.dry_run:
        print(f"[dry-run] {changed} feature(s) would be stamped")
        return 0
    raw = FEATURE_FILE.read_text(encoding="utf-8")
    body = json.dumps(features, indent=2) + ("\n" if raw.endswith("\n") else "")
    _atomic_write(FEATURE_FILE, body)
    print(f"✓ stamped {changed} feature(s) evidence={PRE_GATE}")
    return 0


def log_close(fid: str, mode: str, attestation: str, detail: str = "") -> None:
    """Every flip of `passes` is auditable, whatever granted it."""
    _append_jsonl(
        CLOSES_LOG,
        {"ts": _now(), "feature": fid, "mode": mode, "attestation": attestation, "detail": detail},
    )


def log_override(fid: str, kind: str, reason: str) -> None:
    """`--force-complete` was previously unlogged and indistinguishable after the fact."""
    _append_jsonl(OVERRIDES_LOG, {"ts": _now(), "feature": fid, "kind": kind, "reason": reason})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    rn = sub.add_parser("run", help="EXECUTE a step's command and record what happened")
    rn.add_argument("id")
    rn.add_argument("--step", type=int, required=True)
    rn.add_argument("command", nargs="+", help="the command, after a `--` separator")

    r = sub.add_parser(
        "record", help="hand-record a step the tool cannot execute (needs --attested-by to close)"
    )
    r.add_argument("id")
    r.add_argument("--step", type=int, required=True)
    r.add_argument("--command", required=True)
    r.add_argument("--observed", required=True)
    r.add_argument("--status", choices=["pass", "fail"], required=True)

    g = sub.add_parser("gate", help="record a gate result (run_ci_locally, cargo test, ...)")
    g.add_argument("id")
    g.add_argument("--name", required=True)
    g.add_argument("--status", choices=["pass", "fail"], required=True)

    c = sub.add_parser("critic", help="record a critic verdict")
    c.add_argument("id")
    c.add_argument("--layer", choices=["deterministic", "judgment"], required=True)
    c.add_argument("--verdict", choices=["approve", "warn", "block"], required=True)
    c.add_argument("--reviewer", help="codex | claude-fallback (judgment layer)")
    c.add_argument("--rounds", type=int, help="adversarial rounds spent")

    v = sub.add_parser("verify", help="is the record complete enough to close?")
    v.add_argument("id")
    v.add_argument("--json", action="store_true")
    v.add_argument(
        "--allow-attested",
        action="store_true",
        help="accept hand-recorded steps (human path only; close_feature.py passes "
        "this only alongside --attested-by)",
    )

    s = sub.add_parser("show", help="print the raw record")
    s.add_argument("id")

    at = sub.add_parser("artifact", help="attach a screenshot / recording / trace to a step")
    at.add_argument("id")
    at.add_argument("--step", type=int, required=True)
    at.add_argument("--file", required=True, help="path to the image, video, or trace")
    at.add_argument("--caption", help="what it shows — this becomes the figure caption")

    rd = sub.add_parser("render", help="(re)generate EVIDENCE.md, the reviewable page")
    rd.add_argument("id")
    rd.add_argument("--stdout", action="store_true")

    p = sub.add_parser("stamp-pre-gate", help="mark already-passing features as un-evidenced")
    p.add_argument("ids", nargs="*")
    p.add_argument("--all", action="store_true", help="every feature with passes:true")
    p.add_argument("--dry-run", action="store_true")

    args = ap.parse_args(argv)
    handlers = {
        "run": cmd_run,
        "record": cmd_record,
        "gate": cmd_gate,
        "critic": cmd_critic,
        "verify": cmd_verify,
        "show": cmd_show,
        "artifact": cmd_artifact,
        "render": cmd_render,
        "stamp-pre-gate": cmd_stamp_pre_gate,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvidenceError as exc:
        print(f"✗ evidence: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
