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

    tools/evidence.py run    <FID> --step 2 -- pytest tests/domain/test_x.py -q
    tools/evidence.py record <FID> --step 2 --command "..." --observed "..." --status pass
    tools/evidence.py gate   <FID> --name run_ci_locally --status pass
    tools/evidence.py critic <FID> --layer judgment --verdict approve --reviewer codex
    tools/evidence.py verify <FID> [--json] [--allow-attested]
    tools/evidence.py show   <FID>
    tools/evidence.py stamp-pre-gate --all

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
        raise EvidenceError(f"{path.relative_to(ROOT)} is corrupt: {exc}") from exc


def save_record(fid: str, rec: dict) -> None:
    rec["updated"] = _now()
    _atomic_write(record_path(fid), json.dumps(rec, indent=2, sort_keys=True) + "\n")


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
                f"no evidence record at {path.relative_to(ROOT)} — record one with "
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

    summary = {
        "steps_total": len(steps),
        "steps_evidenced": sum(
            1 for i in range(1, len(steps) + 1) if by_n.get(i, {}).get("status") == "pass"
        ),
        "critic": {k: v.get("verdict") for k, v in critic.items()},
    }
    return (not problems, problems, summary)


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
            "command": " ".join(cmd),
            "observed": out[-4000:],
            "exit_code": proc.returncode,
            "status": status,
            "executed": True,
            "ts": _now(),
        },
    )
    tail = out.splitlines()[-1][:100] if out else "(no output)"
    print(f"{'✓' if status == 'pass' else '✗'} {args.id} step {args.step}: {status} — {tail}")
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
        "stamp-pre-gate": cmd_stamp_pre_gate,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvidenceError as exc:
        print(f"✗ evidence: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
